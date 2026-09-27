"""Key / 凭据本地存储(§14)。

存储位置:`<user_root>/user_data/secrets.json`,权限 0600。**每账户一份** ——
Sub2API / SMTP 等凭据是个人凭据, 不能跨账户共享。user_root 由
``user_paths.resolve_user_root()`` 解析: 请求路径走认证中间件注入的 contextvar,
后台线程/调度器/子进程必须显式传 ``user_root=`` (没有账户上下文时抛
MissingUserContextError, 刻意**不**回退到共享文件)。

优先级:secrets.json > 部署级 env 默认 > 空(Free 模式)。
UI 改 Key 时只动这个文件,不动 .env。

**每用户配置的回落档一律读 `config.AI_ENV_DEFAULTS`(import 期冻结的只读快照),
不读进程级 `settings` 单例** —— 单例会被任意账户的保存请求改写, 读它等于把未自配
账户的凭据交给最后一个保存者(复核 A5, 详见 `_env_default`)。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from app.services.fs_utils import atomic_write_text
from app.services.user_paths import MissingUserContextError, resolve_user_root

logger = logging.getLogger(__name__)


# ── B1: 读改写序列化 ──────────────────────────────────────────
# `save()` / `save_deployment()` / `clear()` / `clear_deployment()` 都是
# `load → update → atomic_write_text` 三步。原子写只保证**不出现半截文件**, 不保证
# **不丢更新**: 两个请求并发保存时, 后写者拿的是先写者之前的旧快照, 先写者的键被
# 静默抹掉(同 `preferences._SAVE_LOCK` 的动机)。RLock 而非 Lock —— `save()` 内部
# 会调用 `save_deployment()`, 需要可重入。
_SAVE_LOCK = threading.RLock()


# ── B2: 热路径 mkdir 去重 ────────────────────────────────────
# 凭证文件所在目录建好之后不会再消失(没有任何代码删账户目录), 而 `_path()` 在
# **每次读**都被调用 —— 行情轮询每轮 8~12 次多余的系统调用。用进程内「已确保」
# 集合记忆, 命中即跳过。只在新路径上真正 mkdir, 失败不进集合(下次照旧重试)。
_ENSURED_DIRS: set[str] = set()
_ENSURED_LOCK = threading.Lock()


def _ensure_dir(path: Path) -> Path:
    key = str(path)
    if key in _ENSURED_DIRS:
        return path
    with _ENSURED_LOCK:
        if key not in _ENSURED_DIRS:
            path.mkdir(parents=True, exist_ok=True)
            _ENSURED_DIRS.add(key)
    return path


def _path(user_root: Path | None = None) -> Path:
    p = resolve_user_root(user_root) / "user_data" / "secrets.json"
    _ensure_dir(p.parent)
    return p


# 部署级凭据: 与账户无关, 所有账户共享一份。
#
# 为什么必须分开: secrets.json 里混着两类凭据 ——
#   - **部署级**: TickFlow 数据源 Key。行情全站共享一份(用户裁定), 取数路径
#     tickflow/client.get_client() 遍布后台线程与子进程, **没有账户上下文**。
#   - **每用户**: Sub2API Key / SMTP 密码 / 自定义 webhook secret —— 个人凭据。
# 把部署级密钥也按账户分, 会让所有后台行情取数抛 MissingUserContextError:
# 行情是共享的, 取数的凭据也必须能在无上下文时拿到。
DEPLOYMENT_KEYS: frozenset[str] = frozenset({
    "tickflow_api_key",
    "tickflow_base_url",   # 数据源端点, 与 key 成对(api/settings.py:121,200 写入)
})


def _deployment_path() -> Path:
    from app.config import settings
    p = settings.data_dir / "deployment_secrets.json"
    _ensure_dir(p.parent)
    return p


def load_deployment() -> dict:
    """读**部署级**凭据文件(与账户无关); 不存在/损坏返回空 dict。"""
    p = _deployment_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001
            logger.warning("deployment_secrets.json malformed: %s", e)
    return {}


def save_deployment(updates: dict) -> dict:
    """合并写入**部署级**凭据(与账户无关)。返回新内容。

    动态键(如自定义数据源插件的 `{name}_api_key`、扩展数据配置的
    `ext_{id}_api_key`)无法用静态 DEPLOYMENT_KEYS 覆盖, 由调用方显式调本函数
    而不是 save() —— 显式优于按名字猜测。
    """
    with _SAVE_LOCK:
        current = load_deployment()
        current.update({k: v for k, v in updates.items() if v is not None})
        atomic_write_text(
            _deployment_path(),
            json.dumps(current, indent=2, ensure_ascii=False), mode=0o600,
        )
        return current


def get_deployment(field: str, default: str = "") -> str:
    """取**部署级**凭据字段(如 tickflow_base_url)。"""
    val = load_deployment().get(field)
    return str(val).strip() if val else default


def clear_deployment(*keys: str) -> dict:
    """清掉部署级凭据字段(留空清全部)。供"清除数据源配置"这类管理操作使用。"""
    with _SAVE_LOCK:
        p = _deployment_path()
        if not p.exists():
            return {}
        if not keys:
            p.unlink()
            return {}
        current = load_deployment()
        for k in keys:
            current.pop(k, None)
        atomic_write_text(
            p, json.dumps(current, indent=2, ensure_ascii=False), mode=0o600,
        )
        return current


def load(user_root: Path | None = None) -> dict:
    """读**当前账户**的凭据文件; 不存在/损坏返回空 dict。

    注意: **不含**部署级凭据(见 DEPLOYMENT_KEYS)。需要 TickFlow Key 请用
    get_tickflow_key(), 它会走部署级文件。
    """
    p = _path(user_root)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001
            logger.warning("secrets.json malformed: %s", e)
    return {}


def save(updates: dict, user_root: Path | None = None) -> dict:
    """合并写入(不会清掉未提及的字段), **按作用域分派**到两个文件。返回新内容。

    分派做在这里而不是各 set_* 里: 与 preferences.save() 同一模式 —— setter 层
    分派必漏, 且部署级键可能在**没有账户上下文**时写入。

    只有当入参含**每用户**键时才需要账户上下文; 只写部署级键时不需要。
    """
    with _SAVE_LOCK:
        dep = {k: v for k, v in updates.items() if k in DEPLOYMENT_KEYS}
        usr = {k: v for k, v in updates.items() if k not in DEPLOYMENT_KEYS}

        merged: dict = {}
        if dep:
            merged.update(save_deployment(dep))
        if usr:
            # 每用户部分才解析账户根 —— 无上下文且含每用户键时在此 fail-closed
            p = _path(user_root)
            current = load(user_root)
            current.update({k: v for k, v in usr.items() if v is not None})
            atomic_write_text(
                p, json.dumps(current, indent=2, ensure_ascii=False), mode=0o600,
            )
            merged.update(current)
        return merged


def clear(*keys: str, user_root: Path | None = None) -> dict:
    """清掉当前账户的指定字段(留空清全部)。

    只作用于**每用户**凭据; 部署级键不在本函数范围内(避免无上下文时误删全站 Key)。
    """
    with _SAVE_LOCK:
        p = _path(user_root)
        if not p.exists():
            return {}
        if not keys:
            p.unlink()
            return {}
        current = load(user_root)
        for k in keys:
            current.pop(k, None)
        atomic_write_text(
            p, json.dumps(current, indent=2, ensure_ascii=False), mode=0o600,
        )
        return current


def get_tickflow_key(user_root: Path | None = None) -> str:
    """取 TickFlow Key(**部署级**, 全站共享一份): 部署凭据文件优先, 否则 .env。

    刻意**忽略** user_root: 该键与账户无关, 且取数路径遍布无账户上下文的后台线程
    与子进程。签名保留 user_root 是为了不改动既有调用方, 但传进来的账户根对此键
    无意义 —— 这也是本模块把 DEPLOYMENT_KEYS 单独分派的原因。
    """
    val = load_deployment().get("tickflow_api_key")
    if val:
        return val
    from app.config import settings
    return settings.tickflow_api_key or ""


def _env_default(key: str, default: Any = "") -> Any:
    """每用户 AI 配置的**回落档**: 只读的 env 初值快照(``config.AI_ENV_DEFAULTS``)。

    刻意不读 `settings.<key>` —— 那是进程级单例, 会被任意账户的保存请求就地改写。
    读它等于「谁最后保存, 所有未自配账户就用谁的 Key 出网」: 串号 + 计费错位 +
    设置页回显他人密钥(2026-09-27 复核 A5, 已实证)。快照在 import 期冻结, 之后
    没有任何请求改得到它, 所以「env 里给了部署级默认 Key」这个正当能力仍在。
    """
    from app.config import AI_ENV_DEFAULTS

    val = AI_ENV_DEFAULTS.get(key)
    if val is None or val == "":
        return default
    return val


def get_ai_key(user_root: Path | None = None) -> str:
    """取当前账户的 AI Key:secrets.json 优先, 否则部署级 env 默认(只读快照)。"""
    val = load(user_root).get("ai_api_key")
    if val:
        return val
    return str(_env_default("ai_api_key", "") or "")


def get_ai_config(key: str, default: str = "", user_root: Path | None = None) -> str:
    """取当前账户的 AI 配置项:secrets.json 优先, 否则部署级 env 默认(只读快照)。"""
    val = load(user_root).get(key)
    if val:
        return val
    return _env_default(key, default) or default


def get_ai_config_int(key: str, default: int, user_root: Path | None = None) -> int:
    """取 AI 数值配置项 (如 ai_max_output_tokens): secrets.json 优先, 否则 env 默认。"""
    val = load(user_root).get(key)
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError):
            logger.warning("ai config %s is not an int: %r", key, val)
    try:
        return int(_env_default(key, default) or default)
    except (TypeError, ValueError):
        logger.warning("env default for %s is not an int: %r", key, _env_default(key, default))
        return int(default)


def get_custom_webhook_secret(user_root: Path | None = None) -> str:
    """Return the optional HMAC secret for the generic outbound webhook."""
    return str(load(user_root).get("custom_webhook_secret") or "")


def set_custom_webhook_secret(secret: str, user_root: Path | None = None) -> str:
    """Persist or clear the generic outbound webhook HMAC secret."""
    value = (secret or "").strip()
    if value:
        save({"custom_webhook_secret": value}, user_root)
    else:
        clear("custom_webhook_secret", user_root=user_root)
    return value


def get_email_smtp_password(user_root: Path | None = None) -> str:
    """Return the SMTP password used by the email notification channel."""
    return str(load(user_root).get("email_smtp_password") or "")


def set_email_smtp_password(password: str, user_root: Path | None = None) -> str:
    """Persist or clear the SMTP password used by email notifications."""
    value = password or ""
    if value:
        save({"email_smtp_password": value}, user_root)
    else:
        clear("email_smtp_password", user_root=user_root)
    return value


def user_secret_field(field: str) -> str:
    """把部署级字段名映射到它的**每用户**版本: ``fuyao_api_key`` → ``user_fuyao_api_key``。

    为什么不复用同名键: `save()` 按名字分派作用域, 同名键会被写进**部署**文件 ——
    用户填的 key 会污染站点共享凭据。
    """
    return f"user_{field}"


def get_user_secret(field: str, user_root: Path | None = None) -> str:
    """取**当前账户**在该字段上的值(未设置 / 无账户上下文 → 空串, 不抛)。

    无上下文返回空串是刻意的: 账户层是**可选覆盖**, 它读不到时调用方应回落到部署级,
    而不是让 import 期的插件解析整个失败。
    """
    try:
        val = load(user_root).get(user_secret_field(field))
    except MissingUserContextError:
        return ""
    return str(val).strip() if val else ""


def save_user_secret(field: str, value: str, user_root: Path | None = None) -> None:
    """把某字段写进**当前账户**的凭据文件。无账户上下文时由 `save()` fail-closed 拒绝。"""
    save({user_secret_field(field): value}, user_root=user_root)


def clear_user_secret(field: str, user_root: Path | None = None) -> None:
    """清掉**当前账户**在该字段上的值(清后自动回落到部署级)。"""
    clear(user_secret_field(field), user_root=user_root)


def get_env_backed_secret(field: str, env_name: str, user_root: Path | None = None) -> str:
    """取环境变量后备的密钥。优先级: **用户自己的 > 部署级 > 环境变量**。

    三个调用方(自定义数据源插件 loader.py:103、内置插件 plugins/fuyao/provider.py:84、
    扩展数据配置 ext_data.py:175)取的是**共享行情**的凭据, 所以:

      - **有账户上下文**(用户在页面上触发的读取) → 用户自己的 key 优先。这就是
        「用户填写后以用户填写的为准」的生效点, 无需任何新增取数路径。
      - **无账户上下文**(import 期、后台线程、调度器、worker 子进程) → 跳过用户层,
        回落部署级。**这条是关键**: 此前「按账户分」的失败(已实测: fuyao 插件清单
        在 import 期解析失败)源于「读不到每用户文件即判不可用」; 这里**跳过而非判失败**,
        所以那些时机**永远有值**, 共享层行为与此前逐位一致。

    写入侧: 站点共享凭据用 `save_deployment()`, 用户自己的用 `save_user_secret()`。
    """
    user_val = get_user_secret(field, user_root)
    if user_val:
        return user_val
    val = load_deployment().get(field)
    if val:
        return str(val).strip()
    return os.environ.get(env_name, "").strip()


def mask(key: str, prefix: int = 4, suffix: int = 4) -> str:
    """脱敏显示。"""
    if not key:
        return ""
    if len(key) <= prefix + suffix:
        return "•" * len(key)
    return f"{key[:prefix]}{'•' * 6}{key[-suffix:]}"
