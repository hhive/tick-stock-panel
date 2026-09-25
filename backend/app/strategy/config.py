"""策略配置持久化 — 读写用户覆盖值。

职责: 将每个策略的用户定制设置（基础参数、策略参数、评分、买卖信号）持久化到 JSON。
不知道: 引擎、AI、前端、回测。
存储: ``<user_root>/user_data/strategy_overrides/{strategy_id}.json`` —— **每账户一份**,
user_root 由 ``user_paths.resolve_user_root()`` 解析 (请求路径走认证中间件注入的
contextvar, 后台线程/worker 必须显式传 ``user_root=``)。注意: 覆盖值只影响"我的参数",
策略源码本身是另一套 (见 app.api.strategy 的策略目录)。
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.services.user_paths import resolve_user_root

logger = logging.getLogger(__name__)

# 进程内缓存: 监控引擎每轮对每条策略规则调用 load_override, 每次读盘+parse 纯重复;
# override 仅在用户编辑时变化, 以 (mtime_ns, size) 签名判断是否重读。
# 键为 override 文件路径 —— 路径里含账户根, 因此缓存天然按账户分开;
# 进程内可能有多个 data_dir (测试), 故是 dict 而非单变量。
_override_cache: dict[str, dict] = {}
_override_cache_sig: dict[str, tuple[int, int]] = {}


def _invalidate_override_cache(path: Path) -> None:
    key = str(path)
    _override_cache.pop(key, None)
    _override_cache_sig.pop(key, None)


def _overrides_dir(user_root: Path) -> Path:
    d = user_root / "user_data" / "strategy_overrides"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(user_root: Path, strategy_id: str, *, ensure_dir: bool = True) -> Path:
    # ensure_dir=False 供热路径读取: mkdir 系统调用在 Windows 上 ~0.07ms,
    # 读缓存命中时跳过它 (目录由写路径保证存在)。
    if ensure_dir:
        d = _overrides_dir(user_root)
    else:
        d = user_root / "user_data" / "strategy_overrides"
    return d / f"{strategy_id}.json"


def load_override(strategy_id: str, user_root: Path | None = None) -> dict:
    """读取**当前账户**对该策略的覆盖配置, 不存在返回空 dict (带 mtime 签名缓存, 返回深拷贝)"""
    p = _path(resolve_user_root(user_root), strategy_id, ensure_dir=False)
    key = str(p)
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        _invalidate_override_cache(p)
        return {}
    cached = _override_cache.get(key)
    if cached is not None and sig == _override_cache_sig.get(key):
        return copy.deepcopy(cached)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # 清理 basic_filter 中值为 None/空的键（避免固化无意义的空值）
        bf = data.get("basic_filter")
        if isinstance(bf, dict):
            cleaned = {k: v for k, v in bf.items() if v is not None}
            if cleaned:
                data["basic_filter"] = cleaned
            else:
                del data["basic_filter"]
        _override_cache[key] = data
        _override_cache_sig[key] = sig
        return copy.deepcopy(data)
    except Exception as e:
        logger.warning("load override %s failed: %s", strategy_id, e)
        return {}


def save_override(strategy_id: str, overrides: dict, user_root: Path | None = None) -> None:
    """保存**当前账户**对该策略的覆盖配置（全量覆盖写）"""
    p = _path(resolve_user_root(user_root), strategy_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(overrides, ensure_ascii=False, indent=2))
    _invalidate_override_cache(p)


def delete_override(strategy_id: str, user_root: Path | None = None) -> None:
    """删除**当前账户**对该策略的覆盖配置（重置为默认值）"""
    p = _path(resolve_user_root(user_root), strategy_id)
    _invalidate_override_cache(p)
    if p.exists():
        p.unlink()


def list_overrides(user_root: Path | None = None) -> dict[str, dict]:
    """返回**当前账户**所有策略的覆盖配置 {strategy_id: overrides}"""
    d = _overrides_dir(resolve_user_root(user_root))
    result: dict[str, dict] = {}
    for f in d.glob("*.json"):
        try:
            sid = f.stem
            result[sid] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return result
