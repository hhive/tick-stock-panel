"""多用户账号 API。

端点:
  POST   /api/account/jump      — 从 Sub2API 跳转带来的 apikey 自动登录
  POST   /api/account/register  — 注册(首个注册者为管理员)
  POST   /api/account/login     — 邮箱 + 密码登录
  POST   /api/account/logout    — 登出
  GET    /api/account/me        — 当前账号信息
  POST   /api/account/bindings  — 绑定 apikey 到当前账号
  DELETE /api/account/bindings  — 解绑

会话 cookie: 与单密码应急入口**复用同一个 cookie 名** `tf_session`。中间件先按
账号会话解析、解析不到再回落单密码会话, 因此一个 cookie 足以承载两类登录, 前端
完全不需要知道 token 的两套来源。两个 store 的 token 都是 32 字节随机串, 跨 store
碰撞概率可忽略; 万一某 token 只存在于其中一个 store, 另一个查不到即回落, 语义正确。

安全:
  - 明文 apikey 只在请求体里出现一次, 立刻转 sha256; 不落日志、不回显。
  - 注册接口按 IP 限流(5 次/小时), 登录沿用 auth.py 的「失败 5 次锁 5 分钟」。
  - cookie 在 HTTPS 下必须带 Secure; 反代场景读 X-Forwarded-Proto 判定。
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api import auth as auth_api
from app.services import account_sessions, accounts, sub2api_verify, user_paths

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])

COOKIE_NAME = auth_api.COOKIE_NAME  # 复用 tf_session, 见模块 docstring
_COOKIE_MAX_AGE = account_sessions.SESSION_TTL

# 注册限流: 开源注册 + 无邮箱验证 ⇒ 必须按 IP 限流, 否则垃圾账号可无限注册。
# 窗口 1 小时、上限 5 次。成功注册才计数(失败的重试不计, 避免误伤打错密码的人)。
_REGISTER_LIMIT = 5
_REGISTER_WINDOW_S = 3600
_register_hits: dict[str, list[float]] = defaultdict(list)
_register_lock = Lock()


# ================================================================
# 入参模型
# ================================================================

class JumpIn(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=6, max_length=128)
    # 从 Sub2API 跳转而来的用户会带上这把 key, 注册成功即绑定
    api_key: str | None = Field(default=None, max_length=512)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class BindingIn(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


class SourceKeyIn(BaseModel):
    """用户自己填的数据源 Key。与绑定用同一长度约束。"""
    api_key: str = Field(min_length=1, max_length=512)


# ================================================================
# 辅助
# ================================================================

def _cookie_secure(request: Request) -> bool:
    """cookie 是否该带 Secure。

    反代(nginx/Cloudflare)场景下 request.url.scheme 可能仍是 http —— 真实协议在
    X-Forwarded-Proto 里。两个来源都看, 任一为 https 即认为走的是 HTTPS。
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,      # 防 XSS 窃取
        samesite="lax",     # 防 CSRF
        path="/",
        secure=_cookie_secure(request),
    )


def _current_account_id(request: Request) -> int:
    """取当前账号 id; 未登录抛 401。"""
    account_id = getattr(request.state, "account_id", None)
    if account_id is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return int(account_id)


def _signed_in_account_id(request: Request) -> int | None:
    """取当前账号 id, **未登录返回 None 而不是抛错**。

    跳转端点需要它: 「key 有效但未绑定」有两种截然不同的结局 —— 有会话就绑到当前
    账号, 没会话才交给前端引导注册/登录。
    """
    account_id = getattr(request.state, "account_id", None)
    return int(account_id) if account_id is not None else None


def _adopt_ai_key_if_unset(account_id: int, api_key: str) -> None:
    """把已验证的跳转 key 记为**该账号的 AI 凭据**, 并顺手挑一个默认模型。

    为什么在这里做: AI 消耗走用户自己的 Sub2API key(spec 8.2), 而面板拿到这把明文
    key 的唯一时机就是跳转/绑定 —— 在此之前全后端只有 AI 设置页的手动保存会写
    `ai_api_key`, 所以「跳进来就能用 AI」这件事一直缺一段。

    写入策略由用户裁定(2026-09-27): **两件事各自「仅未设置时填」**, 互不牵连 ——

    - `ai_api_key`: 手动填过的 key 是显式选择(可能是给 AI 单独计费的另一个子账号
      key), 不能被跳转静默改写。
    - `ai_model`: 跳转能带回来的只有 key, 带不回「该用哪个模型」。只填 key 会让
      `ai_configured()` 恒 false、界面停在「还差一步: 选择模型」—— 这正是 2026-09-27
      用户报的实况。故按**账号实际生效的 key** 拉一次可见清单挑一个; 挑不到就不写,
      让部署级默认(`config.DEFAULT_AI_MODEL`)生效 —— 写空串会把默认盖掉。
      用账号的 key 而不是跳转那把: 两者可能不同, 用错会挑到该账号其实调不到的模型。

    静默失败是刻意的: 这是登录/绑定的**附带收益**, 凭据落盘或挑模型失败都不该让用户
    连面板都进不去; 失败只记日志, 用户仍可在 AI 设置页手动填。
    """
    from app import secrets_store
    from app.services.ai_provider import pick_model_for_key

    try:
        root = user_paths.ensure_user_dirs(account_id)
        stored = secrets_store.load(root)

        if not stored.get("ai_api_key"):
            secrets_store.save({"ai_api_key": api_key}, user_root=root)
            logger.info("ai key adopted from jump: account_id=%s", account_id)

        if not stored.get("ai_model"):
            # get_ai_key 内部重新读盘, 所以上面刚补写的 key 也算数。
            effective_key = secrets_store.get_ai_key(root)
            picked = pick_model_for_key(effective_key) if effective_key else ""
            if picked:
                secrets_store.save({"ai_model": picked}, user_root=root)
                logger.info("ai model picked: account_id=%s model=%s", account_id, picked)
    except Exception:  # noqa: BLE001
        logger.exception("adopt ai key failed: account_id=%s", account_id)


def _check_register_rate_limit(ip: str) -> None:
    now = time.time()
    with _register_lock:
        hits = [t for t in _register_hits.get(ip, []) if now - t < _REGISTER_WINDOW_S]
        if len(hits) >= _REGISTER_LIMIT:
            wait = int(_REGISTER_WINDOW_S - (now - hits[0]))
            raise HTTPException(
                status_code=429,
                detail=f"注册过于频繁, 请 {wait} 秒后重试",
            )
        _register_hits[ip] = hits


def _record_register(ip: str) -> None:
    now = time.time()
    with _register_lock:
        # 防内存膨胀: 条目过多时清掉窗口外的记录
        if len(_register_hits) > 1000:
            for stale in [k for k, v in _register_hits.items()
                          if not [t for t in v if now - t < _REGISTER_WINDOW_S]]:
                _register_hits.pop(stale, None)
        _register_hits[ip].append(now)


# ================================================================
# 端点
# ================================================================

@router.post("/jump")
def jump(req: JumpIn, request: Request, response: Response) -> dict:
    """从 Sub2API 跳转带来的 apikey 自动登录。

    key 无效 → 401(绝不放行)。

    key 已绑定到某账号 → 以该账号身份登录。这是「持有 key 即身份」的直接推论:
    URL 上的 key 本身就是 bearer 凭据, 不因当前浏览器另有一个登录态而改变归属。

    key 有效但**未绑定任何账号**时分两种, 按当前有无会话:
      - **已有会话** → 直接绑到当前账号并放行。这是最常见的情形(用户注册过、之后每次
        都从 Sub2API 跳进来), 此前会被丢到登录页, 而登录页因「已认证」立刻回面板,
        **绑定那一步从不执行、凭证被静默丢弃**(2026-09-27 用户报告的根因)。
      - 无会话 → needs_auth, 前端引导注册/登录, 成功后由前端调 /bindings 补绑。

    两条路径都会把这把 key 采纳为该账号的 AI 凭据(仅当尚未设置, 见
    `_adopt_ai_key_if_unset`)。
    """
    if not sub2api_verify.verify_api_key(req.api_key):
        # 不区分「key 不存在」与「校验服务不可用」, 对外一律 401, 避免探测。
        raise HTTPException(status_code=401, detail="API Key 无效或已失效")

    key_hash = accounts.hash_api_key(req.api_key)
    owner = accounts.find_by_key_hash(key_hash)

    if owner is None:
        current = _signed_in_account_id(request)
        if current is None:
            return {"status": "needs_auth"}
        try:
            accounts.bind_api_key(current, key_hash)
        except accounts.BindingConflictError:
            # 查询与绑定之间的竞态: 另一个请求先绑走了。退回引导路径, 不吞掉事实。
            logger.warning("jump bind conflicted: account_id=%s", current)
            return {"status": "needs_auth"}
        except accounts.AccountNotFoundError:
            # 会话指向已不存在的账号: 当作未登录, 由前端走登录页
            return {"status": "needs_auth"}
        _adopt_ai_key_if_unset(current, req.api_key)
        acc = accounts.get_by_id(current)
        logger.info("jump bind ok: account_id=%s", current)
        # 不重发会话 cookie —— 调用者本来就带着有效会话, 只是把 key 补绑上去
        return {"status": "logged_in", "email": acc.email if acc else ""}

    token = account_sessions.create_session(owner.id)
    _set_session_cookie(request, response, token)
    _adopt_ai_key_if_unset(owner.id, req.api_key)
    logger.info("jump login ok: account_id=%s", owner.id)
    return {"status": "logged_in", "email": owner.email}


@router.post("/register")
def register(req: RegisterIn, request: Request, response: Response) -> dict:
    """注册。首个注册者为管理员。可携带 apikey, 成功后一并绑定。"""
    ip = auth_api._client_ip(request)
    _check_register_rate_limit(ip)

    # 若带了 key, 必须先证明它有效 —— 不允许把一把假 key 绑进账号
    key_hash: str | None = None
    if req.api_key and req.api_key.strip():
        if not sub2api_verify.verify_api_key(req.api_key):
            raise HTTPException(status_code=401, detail="API Key 无效或已失效")
        key_hash = accounts.hash_api_key(req.api_key)
        if accounts.find_by_key_hash(key_hash) is not None:
            raise HTTPException(status_code=409, detail="该 API Key 已绑定到其它账号")

    try:
        acc = accounts.create_account(req.email, req.password)
    except accounts.EmailTakenError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        user_paths.ensure_user_dirs(acc.id)
    except Exception:  # noqa: BLE001
        # 目录建不出来就不该给这个账号发会话, 否则第一次写入才炸
        logger.exception("ensure_user_dirs failed: account_id=%s", acc.id)
        raise HTTPException(status_code=500, detail="账号初始化失败, 请稍后重试") from None

    if key_hash:
        try:
            accounts.bind_api_key(acc.id, key_hash)
        except accounts.BindingConflictError:
            # 建号与绑定之间存在竞态: 另一个请求先绑走了这把 key。
            # 账号本身已创建成功, 不回滚(用户可直接登录), 仅提示绑定未完成。
            logger.warning("bind after register conflicted: account_id=%s", acc.id)
            raise HTTPException(
                status_code=409,
                detail="账号已创建, 但该 API Key 已被其它账号绑定, 请登录后更换",
            ) from None
        _adopt_ai_key_if_unset(acc.id, req.api_key)

    _record_register(ip)
    token = account_sessions.create_session(acc.id)
    _set_session_cookie(request, response, token)
    logger.info("account registered: id=%s role=%s", acc.id, acc.role)
    return {"ok": True, "email": acc.email, "role": acc.role}


@router.post("/login")
def login(req: LoginIn, request: Request, response: Response) -> dict:
    """邮箱 + 密码登录。失败沿用既有按 IP 限流(5 次失败锁 5 分钟)。"""
    ip = auth_api._client_ip(request)
    auth_api._check_login_rate_limit(ip)

    acc = accounts.verify_credentials(req.email, req.password)
    if acc is None:
        auth_api._record_login_fail(ip)
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    auth_api._clear_login_fails(ip)
    token = account_sessions.create_session(acc.id)
    _set_session_cookie(request, response, token)
    return {"ok": True, "email": acc.email, "role": acc.role}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    """登出当前账号会话。仅撤销账号会话, 不触碰单密码应急会话(两者独立)。"""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        account_sessions.revoke(token)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict:
    """当前账号信息。绑定的 key 只回哈希前缀, 明文永不回显。"""
    account_id = _current_account_id(request)
    acc = accounts.get_by_id(account_id)
    if acc is None:
        # 会话有效但账号已不存在(理论上不应发生): 视为未登录
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return {
        "email": acc.email,
        "role": acc.role,
        "bindings": [h[:12] for h in acc.api_key_bindings],
    }


@router.post("/bindings")
def add_binding(req: BindingIn, request: Request) -> dict:
    """把一把有效 apikey 绑定到当前账号。"""
    account_id = _current_account_id(request)
    if not sub2api_verify.verify_api_key(req.api_key):
        raise HTTPException(status_code=401, detail="API Key 无效或已失效")
    try:
        accounts.bind_api_key(account_id, accounts.hash_api_key(req.api_key))
    except accounts.BindingConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except accounts.AccountNotFoundError as e:
        raise HTTPException(status_code=401, detail="未登录或会话已过期") from e
    _adopt_ai_key_if_unset(account_id, req.api_key)
    return {"ok": True}


@router.delete("/bindings")
def remove_binding(req: BindingIn, request: Request) -> dict:
    """解绑当前账号下的一把 apikey(幂等)。"""
    account_id = _current_account_id(request)
    try:
        accounts.unbind_api_key(account_id, accounts.hash_api_key(req.api_key))
    except accounts.AccountNotFoundError as e:
        raise HTTPException(status_code=401, detail="未登录或会话已过期") from e
    return {"ok": True}


# ================================================================
# 用户自己的数据源 Key
# ================================================================

def _require_plugin_with_key(name: str) -> str:
    """校验数据源存在且支持配置 Key, 返回规范化后的名字。"""
    from app.data_providers import custom as custom_sources

    source = (name or "").strip().lower()
    manifest = custom_sources.plugin_manifest(source)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"数据源 '{source}' 不存在")
    if not manifest.get("api_key_env"):
        raise HTTPException(status_code=400, detail=f"数据源 '{source}' 不支持配置 Key")
    return source


@router.put("/source-keys/{name}")
def save_source_key(name: str, req: SourceKeyIn, request: Request) -> dict:
    """保存**当前账号自己的**数据源 Key(先探后存)。

    与管理员那条 `/api/settings/plugin-key` 的分工: 那条写**部署级**(站点共享行情用
    哪把 Key), 本条写**每用户**。作用域必须与读取侧一致 —— 本仓此前踩过「写入每用户、
    读取部署级 ⇒ 界面里保存的 Key 永远读不到」的坑(`api/settings.py` 的注释留了档)。

    读取侧在**有账户上下文**时优先取用户自己的(`secrets_store.get_env_backed_secret`),
    所以用户在页面上触发的探测/试拉会直接用他填的 Key; 后台共享取数仍走部署级。
    """
    from app import secrets_store
    from app.data_providers import custom as custom_sources

    account_id = _current_account_id(request)
    source = _require_plugin_with_key(name)
    key = req.api_key.strip()

    ok, message = custom_sources.probe_plugin_key(source, key)
    if not ok:
        # 探测不过就不落盘 —— 与管理员端点同语义, 免得存下一把用不了的 Key
        raise HTTPException(status_code=400, detail=message or "API Key 无效")

    root = user_paths.ensure_user_dirs(account_id)
    secrets_store.save_user_secret(f"{source}_api_key", key, user_root=root)
    logger.info("user source key saved: account_id=%s source=%s", account_id, source)
    return {"ok": True, "scope": "user", "api_key_masked": secrets_store.mask(key)}


@router.delete("/source-keys/{name}")
def clear_source_key(name: str, request: Request) -> dict:
    """清除**当前账号自己的**数据源 Key —— 清除后自动回落到站点共享的那把。"""
    from app import secrets_store

    account_id = _current_account_id(request)
    source = (name or "").strip().lower()
    root = user_paths.ensure_user_dirs(account_id)
    secrets_store.clear_user_secret(f"{source}_api_key", user_root=root)
    logger.info("user source key cleared: account_id=%s source=%s", account_id, source)
    return {"ok": True, "scope": "user"}
