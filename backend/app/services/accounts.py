"""多用户账号注册表。

设计:
  - 文件即存储: `data/accounts/accounts.json` (chmod 0600), 原子写。与本项目既有的
    preferences / secrets / auth 保持同一姿态, 不引入 SQL 数据库。
  - 面板自己发号: `id` 单调递增且**不复用**, 用作 `data/users/<id>/` 的目录名。
  - 密码复用 app.services.auth 的 PBKDF2 参数 (200k 迭代 + 16B salt), 不另写一套,
    避免两处密码学参数漂移。
  - apikey **只存 sha256 十六进制**, 不存明文。
  - 绑定遵循「首个绑定者占有」: 一个 key 一旦绑定到某账号, 不得改绑他人。否则
    A 把 key 交给 B, 或 B 拿到 key 后改绑到自己账号, 等于可夺号。key 所属账号
    由 find_by_key_hash 反查, 命中即以其身份登录, 不走改绑分支。
  - 本模块与 app.services.auth (单密码应急入口) **完全独立**: 不共用文件、不共用
    token 空间, 避免互相清除会话。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.auth import hash_password, verify_password
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

_BEIJING = ZoneInfo("Asia/Shanghai")

_lock = threading.Lock()


class EmailTakenError(ValueError):
    """邮箱已被注册。"""


class BindingConflictError(ValueError):
    """该 apikey 已绑定到其它账号(首个绑定者占有, 不允许改绑)。"""


class AccountNotFoundError(ValueError):
    """账号不存在。"""


def _now() -> str:
    """统一北京时间 ISO 时间戳(秒精度)。"""
    return datetime.now(_BEIJING).isoformat(timespec="seconds")


@dataclass
class Account:
    id: int
    email: str
    password_hash: str
    salt: str
    role: str  # "admin" | "user"
    api_key_bindings: list[str] = field(default_factory=list)  # sha256 十六进制
    created_at: str = ""


def _path() -> Path:
    from app.config import settings

    p = settings.data_dir / "accounts" / "accounts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("accounts.json malformed: %s", e)
    return {}


def _save(data: dict) -> None:
    atomic_write_text(
        _path(), json.dumps(data, indent=2, ensure_ascii=False), mode=0o600,
    )


def _normalize_email(email: str) -> str:
    """邮箱归一化: 去空白 + 转小写。大小写不敏感的唯一性由此保证。"""
    return (email or "").strip().lower()


def _row_to_account(row: dict) -> Account:
    return Account(
        id=int(row["id"]),
        email=str(row.get("email", "")),
        password_hash=str(row.get("password_hash", "")),
        salt=str(row.get("password_salt", "")),
        role=str(row.get("role", "user")),
        api_key_bindings=[str(x) for x in (row.get("api_key_bindings") or [])],
        created_at=str(row.get("created_at", "")),
    )


def _account_to_row(acc: Account) -> dict:
    return {
        "id": acc.id,
        "email": acc.email,
        "password_hash": acc.password_hash,
        "password_salt": acc.salt,
        "role": acc.role,
        "api_key_bindings": list(acc.api_key_bindings),
        "created_at": acc.created_at,
    }


def _rows(data: dict) -> list[dict]:
    rows = data.get("accounts")
    return rows if isinstance(rows, list) else []


# ================================================================
# 查询
# ================================================================

def count() -> int:
    """账号总数。首个注册者判定管理员即依赖此值。"""
    with _lock:
        return len(_rows(_load()))


def list_accounts() -> list[Account]:
    with _lock:
        return [_row_to_account(r) for r in _rows(_load())]


def get_by_id(account_id: int) -> Account | None:
    with _lock:
        for r in _rows(_load()):
            if int(r["id"]) == int(account_id):
                return _row_to_account(r)
    return None


def get_by_email(email: str) -> Account | None:
    target = _normalize_email(email)
    if not target:
        return None
    with _lock:
        for r in _rows(_load()):
            if str(r.get("email", "")) == target:
                return _row_to_account(r)
    return None


def find_by_key_hash(key_hash: str) -> Account | None:
    """按 apikey 的 sha256 反查所属账号。绑定关系是唯一的(首个占有)。"""
    target = (key_hash or "").strip().lower()
    if not target:
        return None
    with _lock:
        for r in _rows(_load()):
            if target in [str(x).lower() for x in (r.get("api_key_bindings") or [])]:
                return _row_to_account(r)
    return None


def hash_api_key(api_key: str) -> str:
    """apikey → sha256 十六进制。明文永不落盘。"""
    return hashlib.sha256((api_key or "").strip().encode("utf-8")).hexdigest()


# ================================================================
# 写入
# ================================================================

def _validate_password(password: str) -> None:
    # 与 app.services.auth.set_password 的下限保持一致
    if not isinstance(password, str) or len(password) < 6:
        raise ValueError("密码至少 6 位")


def create_account(email: str, password: str) -> Account:
    """创建账号。**首个账号为管理员**, 其余为普通用户。"""
    email_norm = _normalize_email(email)
    local, _, domain = email_norm.partition("@")
    # 只要求 @ 两侧非空。刻意不强制域名含点 —— 过度严格的邮箱正则会把合法地址
    # (如内网域名) 挡在门外, 而这里的目的仅是拦下明显不是邮箱的输入。
    if not local or not domain:
        raise ValueError("邮箱格式不正确")
    _validate_password(password)

    with _lock:
        data = _load()
        rows = _rows(data)
        if any(str(r.get("email", "")) == email_norm for r in rows):
            raise EmailTakenError(f"邮箱已被注册: {email_norm}")

        salt_hex, hash_hex = hash_password(password)
        # 单调递增且不复用: 即使将来支持删除, 也不回收 id, 避免新账号继承旧目录
        next_id = max((int(r["id"]) for r in rows), default=0) + 1
        acc = Account(
            id=next_id,
            email=email_norm,
            password_hash=hash_hex,
            salt=salt_hex,
            role="admin" if not rows else "user",
            api_key_bindings=[],
            created_at=_now(),
        )
        rows.append(_account_to_row(acc))
        data["accounts"] = rows
        _save(data)

    logger.info("account created: id=%s email=%s role=%s", acc.id, acc.email, acc.role)
    return acc


def verify_credentials(email: str, password: str) -> Account | None:
    """校验邮箱+密码, 成功返回账号, 失败返回 None(不区分账号不存在与密码错误)。"""
    acc = get_by_email(email)
    if acc is None:
        return None
    if not verify_password(password, acc.salt, acc.password_hash):
        return None
    return acc


def _mutate(account_id: int, fn) -> Account:
    """在锁内对指定账号做一次修改并落盘。fn 接收 Account 返回 Account。"""
    with _lock:
        data = _load()
        rows = _rows(data)
        for idx, r in enumerate(rows):
            if int(r["id"]) != int(account_id):
                continue
            acc = fn(_row_to_account(r))
            rows[idx] = _account_to_row(acc)
            data["accounts"] = rows
            _save(data)
            return acc
    raise AccountNotFoundError(f"账号不存在: {account_id}")


def bind_api_key(account_id: int, key_hash: str) -> Account:
    """把 apikey 哈希绑定到账号。

    首个绑定者占有: 若该哈希已属于**其它**账号, 抛 BindingConflictError(不改绑)。
    对自己账号重复绑定是幂等的(直接返回)。
    """
    target = (key_hash or "").strip().lower()
    if not target:
        raise ValueError("key_hash 不能为空")

    owner = find_by_key_hash(target)
    if owner is not None:
        if owner.id == int(account_id):
            return owner  # 幂等
        raise BindingConflictError("该 API Key 已绑定到其它账号")

    def _apply(acc: Account) -> Account:
        if target not in acc.api_key_bindings:
            acc.api_key_bindings.append(target)
        return acc

    acc = _mutate(account_id, _apply)
    logger.info("api key bound: account_id=%s", acc.id)
    return acc


def unbind_api_key(account_id: int, key_hash: str) -> Account:
    """解绑当前账号下的某个 apikey 哈希(幂等)。"""
    target = (key_hash or "").strip().lower()

    def _apply(acc: Account) -> Account:
        acc.api_key_bindings = [h for h in acc.api_key_bindings if h.lower() != target]
        return acc

    acc = _mutate(account_id, _apply)
    logger.info("api key unbound: account_id=%s", acc.id)
    return acc


def reset_state_for_tests() -> None:
    """仅供测试: 丢弃内存态。账号数据本身在文件里, 由测试的临时 DATA_DIR 隔离。"""
    global _lock
    _lock = threading.Lock()
