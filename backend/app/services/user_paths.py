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
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

# 账号私有数据骨架 — 与迁移前的共享目录一一对应 (data/user_data、data/backtest_results…),
# 迁移后同一份数据按账号分家。新增私有目录时必须同步登记到这里。
USER_SUBDIRS: tuple[str, ...] = (
    "user_data",
    "user_data/lots",            # 手数批次 (strategy.lots)
    "user_data/custom_factors",  # 自定义/复合因子 (factors.store)
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


class MissingUserContextError(RuntimeError):
    """无法解析账户根目录: 既无显式 user_root, 也无请求上下文。

    刻意**不**提供"回退到某个共享目录"的行为。静默回退有两条严重后果, 都比报错更糟:
      - 写入落到所有人共享的文件 ⇒ A 的改动覆盖 B 的;
      - 后台读取拿到别人(或默认)的数据 ⇒ 跨用户串号。
    所以这里 fail-closed: 调用方要么在请求路径上(中间件已设上下文), 要么显式传参。
    """


def resolve_user_root(explicit: Path | None = None) -> Path:
    """解析**当前账户**的数据根目录。

    这是全部每用户存储的统一接缝。优先级:
      1. 显式传入的 ``explicit`` —— 后台线程/调度器**必须**用这条;
      2. 请求上下文(contextvar, 由认证中间件设置);
      3. 都没有 → 抛 MissingUserContextError。

    第 3 条是刻意的 fail-closed, 理由见 MissingUserContextError 的 docstring。
    """
    if explicit is not None:
        return _validate_explicit_root(explicit)
    from app.services import preferences  # 惰性导入: 避免与本模块形成导入环

    root = preferences.current_user_root()
    if root is None:
        raise MissingUserContextError(
            "无法解析账户根目录: 既未显式传 user_root, 也没有请求上下文。"
            "后台线程/调度器必须显式传 user_root=。"
        )
    # 上下文里的根**不**在此处校验归属: 它由认证中间件经
    # preferences.set_current_user_root(user_paths.user_root(account_id)) 注入,
    # 而 user_root() 已经校验过 account_id。在此重复校验会要求根必须严格位于
    # <data_dir>/users/ 之下 —— 那会把"测试用 tmp 根做行为兼容"这类正当用法一并
    # 拒掉(曾因此一次打红 258 条测试), 收益却只是挡住一个本就不存在的调用方。
    return Path(root)


def _validate_explicit_root(p: Path) -> Path:
    """拒绝把**共享目录**当作账户根, 并归一化返回值。

    这是"传错目录却静默通过"的闸门。但校验按**危害分级**, 只挡真正会导致跨用户
    串号的那几类, 而不是要求路径必须在 ``<data_dir>/users/`` 之下 —— 后者过严:
    它会连带拒掉"测试用独立 tmp 根隔离数据"这类正当用法(曾一次打红 258 条测试),
    而那种路径并不共享任何东西, 不会造成跨账户泄漏。

    真正致命的是**共享目录**被当成账户根, 数据会落到所有人都读写的位置:

      - ``data_dir`` 本身: 每用户存储退化成共享文件 ⇒ A 的写入覆盖 B 的;
      - ``<data_dir>/users`` 这个**容器**: 数据落到 ``users/user_data/…``, 同样是共享;
      - ``data_dir`` 的**祖先**(如 ``..``): 同上, 且范围更大。

    用 ``resolve()`` 归一化后再比较: 能识别 ``users/1/..`` 这类绕过, 也让返回值成为
    规范形态(符号链接展开), 调用方拿到的路径与实际落盘位置一致。
    """
    from app.config import settings

    data_dir = Path(settings.data_dir).resolve()
    users_base = data_dir / "users"
    resolved = Path(p).resolve()

    if resolved == data_dir or resolved.is_relative_to(data_dir):
        # 位于 data_dir 之下: 只有 users/<id> 子树才是账户私有, 其余
        # (data_dir 自身、user_data/、strategies/、pools/… 以及 users/ 容器本身)
        # 都是**共享位置**, 拿它们当账户根会让所有账户读写同一份文件。
        if resolved == users_base or not resolved.is_relative_to(users_base):
            raise InvalidAccountIdError(
                f"data_dir 之下只有 users/<id> 可作为账户根(其余是共享位置): {p}",
            )
    elif data_dir.is_relative_to(resolved):
        # 位于 data_dir 之外、但是它的祖先(如 .. 或 /): 会把整个共享树包进来
        raise InvalidAccountIdError(
            f"data_dir 的祖先目录不能作为账户根: {p}",
        )
    return resolved


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


def iter_user_roots() -> Iterator[tuple[int, Path]]:
    """遍历全部账户的 ``(account_id, user_root)``。

    用途: **共享**数据变更后需要让所有账户的派生状态失效。典型场景是扩展数据
    (概念/行业)变更 —— 它喂的是所有人共用的计算, 因此每个账户基于它算出的策略结果
    缓存都过期了。只清"当前账户"会留下其它账户展示旧口径结果, 且没有任何提示。

    开销与边界:
      - 会读账号注册表, 属进程级遍历 —— **只在低频路径使用**(配置变更、管理操作),
        不要放进每请求或行情轮询的热路径。
      - 账户 id 非法时跳过并记警告, 不让一个坏账号中断整轮扇出。
      - 无账号时产出空序列(全新部署即是如此), 调用方无需特判。
    """
    from app.services import accounts  # 惰性导入: 避免导入环

    for acc in accounts.list_accounts():
        try:
            yield acc.id, user_root(acc.id)
        except InvalidAccountIdError:
            logger.warning("fan-out: 跳过非法账号 id %r", getattr(acc, "id", None))


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
