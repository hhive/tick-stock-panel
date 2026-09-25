"""多用户路径解析 — 账号 ID → 该账号私有数据根目录的唯一映射点。

布局:
  - 共享行情数据仍在 ``settings.data_dir`` 下 (行情/K线/因子等所有人一份);
  - 每个账号的私有数据在 ``settings.data_dir/users/<account_id>/`` 下, 账号之间
    完全隔离。本模块是全项目**唯一**构造该路径的地方, 其它模块一律调用
    ``user_root`` / ``ensure_user_dirs``, 不得自己拼 ``users/`` 字符串, 否则
    校验会各写一套并出现漏网。

安全姿态: fail-closed。账号 ID 由面板发号 (见 app.services.accounts, 单调递增
正整数、不复用), 因此这里的校验比模拟盘那套「字符串账户名」更严: 只接受正整数,
或能干净解析为正整数的十进制字符串; 其余一律抛 InvalidAccountIdError。
路径穿越 (``"../"``、``"1/2"``、``".."`) 由「必须能转成正整数」这一条天然挡死,
不会走到 ``Path`` 拼接; 拒绝时不创建任何目录。

注意: 本模块的 ``account_id`` (面板账号, 整数主键) 与模拟盘的
``data/paper/accounts/{account_id}`` (那里是字符串账户名, 见 app.strategy.paper)
是**两个互不相干的概念**, 命名相近但命名空间不同, 永远不要互相传递或混用。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 账号私有数据骨架 — 与迁移前的共享目录一一对应 (data/user_data、data/backtest_results…),
# 迁移后同一份数据按账号分家。新增私有目录时必须同步登记到这里。
USER_SUBDIRS: tuple[str, ...] = (
    "user_data",
    "strategies/custom",
    "strategies/ai",
    "strategies/composite",
    "backtest_results",
    "research/mining/runs",
    "paper/accounts",
)

# 十进制字符串只认 ASCII 数字: 不用 str.isdigit(), 否则全角数字 (U+FF10 起)
# 与阿拉伯-印度数字 (U+0660 起) 也会被判为合法, 而它们与目录名预期的形态并不一致。
_DECIMAL_RE = re.compile(r"[0-9]+")


class InvalidAccountIdError(ValueError):
    """账号 ID 非法 (非正整数 / 非法十进制字符串)。"""


def validate_account_id(raw: object) -> int:
    """校验并归一化账号 ID 为正整数, 非法抛 InvalidAccountIdError。

    接受: 正整数 int (含大于 0 的任意大小), 以及仅由 ASCII 数字组成的十进制字符串
    (如 ``"7"``) —— 账号 ID 可能来自 URL 路径或 JSON, 这两种来源都可能是字符串。

    拒绝 (fail-closed):
      - bool: Python 里 True/False 是 int 子类, 必须显式挡掉, 否则 True 会变成 1;
      - float (含 ``1.0``)、None、list/dict 等非 int/str 类型;
      - 0 与负数;
      - 不干净的十进制字符串: 带空白 (``"1 "``)、带正负号 (``"+1"``)、
        小数点 (``"1.0"``)、十六进制 (``"0x1"``)、含分隔符或路径成分
        (``"1/2"``、``".."``、``"../etc"``)。

    关于上限: 有意**不设**账号 ID 大小上限。上限需要一个有依据的业务理由, 而目录名
    (str(int)) 本身与数值大小无关; 面板发号是单调递增整数, 实际远达不到任何瓶颈。
    这里唯一处理的是 Python 3.11+ 对超大整数转字符串的 4300 位限制 —— 那会让
    ``str()`` 抛异常, 属于「转不成合法目录名」, 一并归入 ID 非法, 而不是新增策略上限。
    """
    if isinstance(raw, bool):
        # 必须先于 int 分支: False/True 是 int 子类, 放行会静默变成 0/1
        raise InvalidAccountIdError(f"账号 ID 非法 (不接受 bool): {raw!r}")
    if isinstance(raw, str):
        if not _DECIMAL_RE.fullmatch(raw):
            raise InvalidAccountIdError(f"账号 ID 非法 (需为十进制数字): {raw!r}")
        value = int(raw)
    elif isinstance(raw, int):
        value = raw
    else:
        raise InvalidAccountIdError(f"账号 ID 非法 (需为正整数): {raw!r}")
    if value <= 0:
        raise InvalidAccountIdError(f"账号 ID 非法 (需为正整数): {raw!r}")
    return value


def _as_dirname(account_id: int) -> str:
    """正整数 → 目录名; 超大整数无法转字符串时同样视为 ID 非法。"""
    try:
        return str(account_id)
    except ValueError as e:  # Python 3.11+ int→str 位数上限
        raise InvalidAccountIdError(f"账号 ID 非法 (无法作为目录名): {e}") from e


def user_root(account_id: int) -> Path:
    """返回账号私有数据根目录 (可能尚不存在): ``<data_dir>/users/<account_id>``。

    先校验再拼接, 校验失败不会产生任何副作用。
    """
    from app.config import settings

    return Path(settings.data_dir) / "users" / _as_dirname(validate_account_id(account_id))


def ensure_user_dirs(account_id: int) -> Path:
    """幂等创建账号私有目录骨架, 返回该账号根目录。

    ``parents=True, exist_ok=True``, 重复调用不报错也不破坏已有数据 (骨架目录只
    新建缺失项, 不清理多余内容, 故对老账号安全)。返回路径与 ``user_root`` 完全一致,
    调用方可以只用这一个函数。
    """
    root = user_root(account_id)
    root.mkdir(parents=True, exist_ok=True)
    for sub in USER_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
