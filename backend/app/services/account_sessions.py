"""多用户账号会话表 — 每个账号一份登录态(token → account_id)。

设计:
  - 文件即存储: `data/accounts/sessions.json` (chmod 0600), 原子写。形如
    `{token: {"account_id": int, "expires_at": float}}`, 与本项目既有的
    preferences / secrets / auth 保持同一姿态, 不引入 SQL 数据库。
  - 内存 + 文件双存: 热路径(每个 /api/ 请求查会话)只碰内存, 模块加载时从磁盘恢复,
    进程重启不丢登录态。
  - token 用 secrets.token_urlsafe(32) (256 位熵) 明文落盘。它是高熵随机串而非口令,
    不需要像密码那样加盐哈希抗爆破; 文件本身已是 0600。
  - 过期采用惰性淘汰: 查到时顺手清理, 不在锁内做全表扫描(见 CONTRIBUTING 6.2)。

与 app.services.auth 的关系 —— **完全独立**:
  accounts.py / 本模块服务的是多用户登录; auth.py 是单密码应急入口(忘账号、域名被抢
  占时的兜底)。两者不共用文件、不共用 token 空间, 本模块不读、不写、不导入 auth 的
  `_sessions`, 也不调用它的 `_persist_sessions_locked`。否则一边登录/登出就会把另一边
  的应急会话清掉。
"""
from __future__ import annotations

import json
import logging
import secrets as _secrets
import threading
import time
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# token 随机字节数: 32 字节 = 256 位熵, 与 auth.py 的 _TOKEN_BYTES 同量级
_TOKEN_BYTES = 32

# 会话有效期: 30 天(自托管场景, 长一点减少重登频率)。
# 取值与 app.services.auth.SESSION_TTL 相同, 但各自独立生效 —— 改一处不影响另一处。
SESSION_TTL = 30 * 24 * 3600

_lock = threading.Lock()
# 内存中的会话: { token: {"account_id": int, "expires_at": float} }。
# 进程重启后由 restore_sessions() 从磁盘重建。
_sessions: dict[str, dict] = {}


def _path() -> Path:
    from app.config import settings

    p = settings.data_dir / "accounts" / "sessions.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> dict:
    """读磁盘会话表。缺失/损坏/类型不符一律降级为空表, 不让服务启动失败。"""
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("sessions.json malformed: %s", e)
        return {}
    if isinstance(data, dict):
        return data
    logger.warning("sessions.json is not an object, ignored")
    return {}


def _save(store: dict) -> None:
    atomic_write_text(
        _path(), json.dumps(store, indent=2, ensure_ascii=False), mode=0o600,
    )


def _coerce_entry(raw: object) -> tuple[int, float] | None:
    """把磁盘上的一条会话记录规整为 (account_id, expires_at); 非法记录返回 None。

    单条记录损坏(缺字段、类型不对)只丢这一条, 不影响其余会话。
    """
    if not isinstance(raw, dict):
        return None
    try:
        return int(raw["account_id"]), float(raw["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None


# ================================================================
# 会话读写
# ================================================================

def create_session(account_id: int) -> str:
    """为账号新建一个会话, 返回 token(有效期 SESSION_TTL)。"""
    token = _secrets.token_urlsafe(_TOKEN_BYTES)
    with _lock:
        _sessions[token] = {
            "account_id": int(account_id),
            "expires_at": time.time() + SESSION_TTL,
        }
        _save(_sessions)
    return token


def get_session(token: str) -> int | None:
    """按 token 取 account_id。不存在或已过期返回 None。

    过期条目在查到时顺手清理并落盘(惰性淘汰), 省一个后台清理任务。
    """
    if not token:
        return None
    with _lock:
        entry = _sessions.get(token)
        if entry is None:
            return None
        if time.time() > entry["expires_at"]:
            _sessions.pop(token, None)
            _save(_sessions)
            return None
        return int(entry["account_id"])


def revoke(token: str) -> None:
    """注销单个会话(登出)。token 不存在时静默忽略, 且不重写文件。"""
    with _lock:
        if _sessions.pop(token, None) is None:
            return
        _save(_sessions)


def revoke_all_for_account(account_id: int) -> int:
    """注销某账号的全部会话, 返回实际注销条数(改密码/踢下线用)。"""
    target = int(account_id)
    with _lock:
        doomed = [t for t, e in _sessions.items() if int(e["account_id"]) == target]
        if not doomed:
            return 0
        for token in doomed:
            _sessions.pop(token, None)
        _save(_sessions)

    logger.info("account sessions revoked: account_id=%s count=%s", target, len(doomed))
    return len(doomed)


def restore_sessions() -> None:
    """从 sessions.json 全量重建内存会话表(模块加载时调用)。

    以磁盘为准: 磁盘是唯一真值来源, 每次变更都落盘, 所以重载不会丢会话。
    恢复时清掉已过期与非法条目 —— 停机期间到期的会话不该在重启后复活。
    """
    with _lock:
        saved = _load()
        now = time.time()
        valid: dict[str, dict] = {}
        for token, raw in saved.items():
            parsed = _coerce_entry(raw)
            if parsed is not None and parsed[1] > now:
                valid[str(token)] = {"account_id": parsed[0], "expires_at": parsed[1]}
        _sessions.clear()
        _sessions.update(valid)
        if len(valid) != len(saved):
            # 有过期/非法条目被清理, 落盘一次; 文件本身损坏时 _load 返回空表,
            # 两边数量一致, 不会覆盖掉现场。
            _save(_sessions)


# 模块加载时恢复会话(与 auth.py 的 _restore_sessions() 同一姿态)
try:
    restore_sessions()
except Exception as e:  # noqa: BLE001
    logger.warning("restore account sessions failed: %s", e)
