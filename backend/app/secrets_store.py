"""Key / 凭据本地存储(§14)。

存储位置:`<user_root>/user_data/secrets.json`,权限 0600。**每账户一份** ——
Sub2API / SMTP 等凭据是个人凭据, 不能跨账户共享。user_root 由
``user_paths.resolve_user_root()`` 解析: 请求路径走认证中间件注入的 contextvar,
后台线程/调度器/子进程必须显式传 ``user_root=`` (没有账户上下文时抛
MissingUserContextError, 刻意**不**回退到共享文件)。

优先级:secrets.json > .env > 空(Free 模式)。
UI 改 Key 时只动这个文件,不动 .env。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.services.user_paths import resolve_user_root

logger = logging.getLogger(__name__)


def _path(user_root: Path | None = None) -> Path:
    p = resolve_user_root(user_root) / "user_data" / "secrets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
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
    p.parent.mkdir(parents=True, exist_ok=True)
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


def get_ai_key(user_root: Path | None = None) -> str:
    """取当前账户的 AI Key:secrets.json 优先,否则 .env。"""
    val = load(user_root).get("ai_api_key")
    if val:
        return val
    from app.config import settings
    return settings.ai_api_key or ""


def get_ai_config(key: str, default: str = "", user_root: Path | None = None) -> str:
    """取当前账户的 AI 配置项:secrets.json 优先,否则 config。"""
    val = load(user_root).get(key)
    if val:
        return val
    from app.config import settings
    return getattr(settings, key, default) or default


def get_ai_config_int(key: str, default: int, user_root: Path | None = None) -> int:
    """取 AI 数值配置项 (如 ai_max_output_tokens): secrets.json 优先,否则 config。"""
    val = load(user_root).get(key)
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError):
            logger.warning("ai config %s is not an int: %r", key, val)
    from app.config import settings
    return int(getattr(settings, key, default) or default)


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


def get_env_backed_secret(field: str, env_name: str, user_root: Path | None = None) -> str:
    """取环境变量后备的密钥(**部署级**):部署凭据文件优先, 否则环境变量。

    为什么是部署级而非每用户: 三个调用方(自定义数据源插件 loader.py:103、内置
    插件 plugins/fuyao/provider.py:84、扩展数据配置 ext_data.py:175)取的都是
    **共享行情**的凭据, 且会在 **import 期与后台线程**被读取 —— 那些时机没有账户
    上下文。按账户分会让插件在 import 时直接被判为不可用(已实测: fuyao 插件清单
    解析失败), 且行情取数整体失败。

    签名保留 user_root 是为了不改动既有调用方, 但传进来的账户根对此键无意义。
    写入侧请用 save_deployment()。
    """
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
