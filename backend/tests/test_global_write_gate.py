"""全局写面门控 + 只读方法不被误门控 (复核整改 A2/A3/A4/B2)。

本套件守的是**两条互补的闸门**, 以及它们的边界:

  A2-1 枚举: 删/重写**原始**数据的端点必须 admin-only。
        **判据的分界线**(复核裁定, 与 main.py::_is_admin_only 同一份) ——
        判据**不是**"有没有全站副作用", 而是
          「删/覆盖原始数据且本地无法重建」 或 「改部署配置/环境」。
        因此 `clear_minute`(rmtree 分钟K, 须回上游重下)、`minute-migrate`、
        `plugins/{name}/install` 门控; 而 `pipeline/run`、`data/refresh-cache`、
        `rebuild_enriched`、`repair_daily`、`refresh_views`、`regime/*/recompute`、
        `rps/rotation-analyze`、各种 `sync*` **保持开放** —— 它们能从本地重算或
        重跑取数恢复, 且多有普通用户可见入口, 门控它们是功能性回归。
        两个方向都有断言, 免得下一个人凭直觉把这条线来回挪。
  A2-2 结构: preferences.save() 里按**键的归属**判定 —— 有请求上下文且非 admin
        时写全局键一律拒绝。这条不依赖 URL 清单, 所以**以后新加的全局键端点
        自动受保护**; 反过来, 也不能把调度器/后台线程一起打死 (无上下文必须放行)。

  A3   清单是**方法相关**的。_ADMIN_ONLY_EXACT 是方法无关集合, 曾把
       GET /api/custom-signals 这类只读列表一起挡掉 —— "写专属"不等于"读也专属"。

  A4   依赖账户上下文的公开只读端点对游客返回 500 (MissingUserContextError 无
       handler)。它们本就该需要登录, 分类修掉 + 注册 handler → 401。

  B2   偏好目录每次读都 mkdir。行情轮询一轮 8~12 次 load(), 纯重复系统调用。

判据与 test_auth_tiers.py 一致: 无 lifespan 启动时 handler 可能 500, 那不算
"被门控"; 被门控的判据是中间件/闸门直接拒绝 (401/403/429)。
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app.api import account as account_api
from app.services import account_sessions, accounts, auth as auth_service
from app.services import preferences

# 中间件拒绝时会返回的状态码; 放行后 handler 返回什么都不算拒绝
_DENIED = (401, 403, 429)


@pytest.fixture(autouse=True)
def _no_context_leak():
    """contextvar 是同一次请求/线程内共享的 —— 测试之间必须复位, 否则会串。"""
    yield
    preferences.set_current_user_root(None)
    preferences.set_current_role(None)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()
    # 「是否已设密码」是模块级缓存, 不随 data_dir 走: 不失效的话, 本文件里
    # legacy_admin 相关测试留下的 True 会让同进程后续测试读到别的面板的状态。
    auth_service._configured_cache = None  # noqa: SLF001
    yield tmp_path


@pytest.fixture
def client():
    # raise_server_exceptions=False: 本套件只断言门控决策; 无 lifespan 时 handler
    # 抛出的 AttributeError 会被 TestClient 直接向上抛, 让断言拿不到响应。
    return TestClient(app_main.app, raise_server_exceptions=False)


@pytest.fixture
def regular_user(client):
    """已登录的**普通用户**（首个注册者是 admin，因此普通用户是第二个）。"""
    client.post("/api/account/register",
                json={"email": "admin@example.com", "password": "secret123"})
    client.post("/api/account/logout")
    client.post("/api/account/register",
                json={"email": "user@example.com", "password": "secret123"})
    return client


@pytest.fixture
def admin_user(client):
    client.post("/api/account/register",
                json={"email": "admin@example.com", "password": "secret123"})
    return client


@pytest.fixture
def guest(client):
    """已认领(存在账号)但当前无会话的面板 —— 等价公网游客。"""
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    client.post("/api/account/logout")
    return client


@pytest.fixture
def legacy_admin(client):
    """单密码应急会话: role=admin, 但**没有**账号 id ⇒ 没有账户根。"""
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    client.post("/api/account/logout")
    client.cookies.clear()
    auth_service.set_password("legacy-pass-123")
    assert client.post("/api/auth/login", json={"password": "legacy-pass-123"}).status_code == 200
    return client


# ================================================================
# A2-1 破坏性端点必须 admin-only
# ================================================================

# 逐条列出而不是遍历推导: 新增一个危险端点却忘了加门控, 必须在 diff 里看得见。
#
# 判据(2026-09-27 复核裁定, 与 main.py::_is_admin_only 的 docstring 同一份):
# **不是**"有没有全站副作用", 而是
#   「删/覆盖**原始**数据且本地无法重建」 或 「改部署配置/环境」。
# 由此能本地重算/可重跑取数的端点(pipeline/run、data/refresh-cache、
# rebuild_enriched、repair_daily、refresh_views、regime/recompute、
# rps/rotation-analyze、各种 sync*)一律**保持开放** —— 见下面的
# test_recomputable_endpoints_are_not_admin_gated, 它守的就是这条线别被再次上移。
_DESTRUCTIVE_ENDPOINTS = [
    # 删/重写**原始**分钟 K 数据, 本地无从恢复
    ("POST", "/api/kline/clear_minute"),          # shutil.rmtree(kline_minute)
    ("POST", "/api/kline/minute-migrate"),        # 全量重写分钟分区
    # 数据源插件的安装/卸载 (改部署的 pip/npm 环境)
    ("POST", "/api/settings/plugins/tickflow/install"),
    ("DELETE", "/api/settings/plugins/tickflow/install"),
]

# 刻意**保持开放**的一面。它们是"从本地重算/重跑取数即可恢复"的那批, 且大多有
# 普通用户可见入口 —— 给它们加门控是**功能性回归**(点一下就 403), 不是加固。
# 反向断言在这里, 是为了让"下次有人凭直觉把它们塞进 admin 清单"当场变红。
_RECOMPUTABLE_ENDPOINTS = [
    ("POST", "/api/pipeline/run"),
    ("POST", "/api/data/refresh-cache"),
    ("POST", "/api/kline/rebuild_enriched"),
    ("POST", "/api/kline/repair_daily"),
    ("POST", "/api/kline/refresh_views"),
    ("POST", "/api/regime/recompute"),
    ("POST", "/api/regime/mainline/recompute"),
    ("POST", "/api/rps/rotation-analyze"),
    ("POST", "/api/kline/sync"),
    ("POST", "/api/kline/sync_minute"),
    ("POST", "/api/kline/sync_minute_single"),
    ("POST", "/api/index/sync_daily"),
    ("POST", "/api/index/sync_instruments"),
    ("POST", "/api/financials/sync/metrics"),
]


@pytest.mark.parametrize(("method", "path"), _DESTRUCTIVE_ENDPOINTS)
def test_destructive_endpoints_are_admin_gated(method, path):
    assert app_main._is_admin_only(method, path), f"{method} {path} 未被门控"


@pytest.mark.parametrize(("method", "path"), _RECOMPUTABLE_ENDPOINTS)
def test_recomputable_endpoints_are_not_admin_gated(method, path):
    assert not app_main._is_admin_only(method, path), (
        f"{method} {path} 可从本地重算/重跑取数恢复, 不该被 admin 清单挡住"
    )


def test_gate_lists_hit_real_routes():
    """两个清单里的路径必须命中真实路由 —— 否则门控指向空气, 测试会空转。

    带路径参数的端点(plugins/{name}/install)在路由表里是模板串, 因此把
    `{param}` 当通配段做全串匹配。
    """
    paths = {r.path for r in app_main.app.routes if r.path.startswith("/api/")}
    templates = [re.compile("^" + re.sub(r"\{[^/}]+\}", "[^/]+", p) + "$") for p in paths]
    for _method, path in _DESTRUCTIVE_ENDPOINTS + _RECOMPUTABLE_ENDPOINTS:
        assert path in paths or any(t.match(path) for t in templates), (
            f"断言了一个不存在的路由(测试会空转): {path}"
        )


def test_regular_user_cannot_clear_shared_minute_kline(regular_user):
    """最严重的一条: 清分钟K 的实现是 rmtree 共享目录, 删了必须回上游重下。"""
    r = regular_user.post("/api/kline/clear_minute", json={"confirm": True})
    assert r.status_code == 403
    assert r.json()["code"] == "ADMIN_REQUIRED"


def test_regular_user_can_still_trigger_the_pipeline(regular_user):
    """既有契约: 管道是部署级**单飞**资源, 但"任何登录账户都能触发全站同步",
    跨账户防的是**谁能取消**(见 app/services/pipeline_jobs.may_cancel 与
    tests/test_multiuser_pipeline_job_ownership.py)。

    本用例只断言"中间件没拦"(无 lifespan, handler 返回什么不算):
    403 才是被门控的可观测症状。
    """
    assert not app_main._is_admin_only("POST", "/api/pipeline/run")
    assert regular_user.post("/api/pipeline/run").status_code != 403


# ================================================================
# A2-2 结构性闸门 (不依赖 URL 清单)
# ================================================================

def test_regular_user_cannot_write_global_preference_over_http(regular_user):
    """走结构性闸门: 该端点**不在**任何 URL 清单里, 由 save() 的键归属判定拒绝。"""
    assert "/api/settings/preferences/pipeline-schedule" not in app_main._ADMIN_ONLY_EXACT
    r = regular_user.put("/api/settings/preferences/pipeline-schedule",
                         json={"hour": 16, "minute": 0})
    assert r.status_code == 403
    assert r.json()["code"] == "ADMIN_REQUIRED"


def test_regular_user_cannot_write_arbitrary_global_key(regular_user):
    """端点是新的、键是新的 —— 只要它落在全局侧, 就必须被拒。

    这是"以后新增的全局键端点自动受保护"这句承诺的可执行形式: 判据在键的归属,
    不在 URL。
    """
    from app.services import user_paths

    root = user_paths.user_root(2)
    token_root = preferences.set_current_user_root(root)
    token_role = preferences.set_current_role("user")
    try:
        with pytest.raises(preferences.GlobalScopeWriteDenied):
            preferences.save({"brand_new_deployment_wide_key": 1})
    finally:
        preferences.reset_current_role(token_role)
        preferences.reset_current_user_root(token_root)


def test_denied_global_write_leaves_the_file_untouched(regular_user):
    """被拒时不得产生半截写入 —— 闸门必须在 read-modify-write **之前**。"""
    preferences.save({"pipeline_schedule": {"hour": 15, "minute": 35}})
    before = preferences._global_path().read_text(encoding="utf-8")

    from app.services import user_paths

    token_root = preferences.set_current_user_root(user_paths.user_root(2))
    token_role = preferences.set_current_role("user")
    try:
        with pytest.raises(preferences.GlobalScopeWriteDenied):
            preferences.save({"pipeline_schedule": {"hour": 9, "minute": 0}})
    finally:
        preferences.reset_current_role(token_role)
        preferences.reset_current_user_root(token_root)

    assert preferences._global_path().read_text(encoding="utf-8") == before


def test_admin_request_context_can_write_global_key(admin_user):
    r = admin_user.put("/api/settings/preferences/pipeline-schedule",
                       json={"hour": 16, "minute": 0})
    assert r.status_code == 200, r.text
    assert r.json() == {"hour": 16, "minute": 0}


def test_background_write_of_global_key_is_still_allowed(_isolated):
    """无请求上下文(调度器/后台线程)写全局键必须放行 —— 否则部署级写入全被打死。

    role 为 None 就是"没有请求上下文"的判据 (与 _current_user_root 完全同形)。
    """
    assert preferences.current_role() is None
    preferences.set_pipeline_schedule(16, 0)
    assert preferences.get_pipeline_schedule() == {"hour": 16, "minute": 0}


def test_guest_context_is_also_denied():
    """游客(理论上到不了 handler)同样不是 admin, 不得写全局键。"""
    token = preferences.set_current_role("guest")
    try:
        with pytest.raises(preferences.GlobalScopeWriteDenied):
            preferences.save({"pipeline_schedule": {"hour": 15, "minute": 35}})
    finally:
        preferences.reset_current_role(token)


def test_per_user_key_write_is_unaffected(regular_user):
    """每用户键的写入路径不得被新闸门波及 —— 那才是普通用户的核心功能。"""
    r = regular_user.put("/api/settings/preferences/nav-order", json={"nav_order": ["/"]})
    assert r.status_code == 200, r.text


# ================================================================
# A3 只读方法不被 admin 清单挡住
# ================================================================

# 这些端点的**读**是普通用户要用的列表; **写**才是部署级创作面。
_READ_WRITE_SPLIT_PATHS = [
    "/api/custom-signals",
    "/api/custom-signals/ai/generate",
    "/api/factors/custom",
    "/api/factors/composite",
]


@pytest.mark.parametrize("path", _READ_WRITE_SPLIT_PATHS)
def test_read_methods_are_not_admin_gated(path):
    assert not app_main._is_admin_only("GET", path), f"GET {path} 被 admin 清单误挡"


@pytest.mark.parametrize("path", _READ_WRITE_SPLIT_PATHS)
def test_write_methods_are_still_admin_gated(path):
    assert app_main._is_admin_only("POST", path), f"POST {path} 未被门控"


def test_regular_user_can_read_custom_signal_list(regular_user):
    """普通用户打开"自定义信号"页必须能读到列表 (原先 403)。"""
    r = regular_user.get("/api/custom-signals")
    assert r.status_code == 200, r.text
    assert "signals" in r.json()


def test_regular_user_can_read_custom_factor_list(regular_user):
    """因子列表: 普通用户必须能读。

    注意**路由实情**: `/api/factors/custom` 只有 POST 路由, 没有 GET ——
    复核报告里"普通用户 GET /api/factors/custom 403"其实是中间件对着一个**不存在
    的 GET 路由**返回 403 (GET 落到 SPA 兜底, 见下面的断言)。真正的读列表是
    `GET /api/factors`。所以这里断言的是: 真实列表 200, 而 `/api/factors/custom`
    的 GET 不再被**admin 清单**拦下(它由 `_is_admin_only` 的单元断言守)。
    """
    r = regular_user.get("/api/factors")
    assert r.status_code == 200, r.text
    assert "factors" in r.json()
    # 无 GET 路由 ⇒ 落到 SPA 兜底(200 text/html), 这是**既有**行为, 与门控无关;
    # 关键是它不再是 403 —— 403 才是"被 admin 清单误挡"的可观测症状。
    assert regular_user.get("/api/factors/custom").status_code != 403


def test_regular_user_still_cannot_create_signal(regular_user):
    """回归保护的另一半: 读放开了, 写必须仍然被挡。"""
    assert regular_user.post("/api/custom-signals", json={}).status_code == 403


# ================================================================
# A4 依赖账户上下文的端点 → 游客/无账户会话不得 500
# ================================================================

# 三个依赖账户上下文的端点, 之前被错放进了公开只读名单
_PER_ACCOUNT_ENDPOINTS = (
    "/api/screener/cached-summary",
    "/api/screener/strategies",
    "/api/screener/cached-result/anything",
)


@pytest.mark.parametrize("path", _PER_ACCOUNT_ENDPOINTS)
def test_per_account_endpoints_are_not_public_read(path):
    assert path not in app_main._PUBLIC_READ_EXACT
    assert not any(path.startswith(p) for p in app_main._PUBLIC_READ_PREFIX)


@pytest.mark.parametrize("path", _PER_ACCOUNT_ENDPOINTS)
def test_guest_gets_401_not_500(guest, path):
    """A4: 游客打到这几个端点应是 401(需登录), 而不是把 MissingUserContextError
    冒泡成 500 —— 500 既语义错误, 又把内部类名暴露给客户端。"""
    assert guest.get(path).status_code == 401, path


@pytest.mark.parametrize("path", ("/api/screener/cached-summary", "/api/screener/strategies"))
def test_missing_user_context_is_403_not_500(legacy_admin, path):
    """单密码应急会话是 admin 但没有账号 id ⇒ 这几个端点必然抛
    MissingUserContextError。没有 handler 就是 500; 注册后是 **403**。

    为什么是 403 而不是 401: 应急入口是**已认证**会话(`/api/auth/status` 对它回
    authenticated=true), 回"未登录或会话已过期"既是事实错误, 又会让前端整页跳登录、
    再被 Auth.tsx 弹回。403 与 api/deps.py 的 require_account_id 同语义同文案,
    且其文案不含拦截器匹配的 `未登录`/`会话已过期`/`401` ⇒ 不触发跳转。
    """
    assert legacy_admin.get(path).status_code == 403, path


def test_legacy_session_writing_per_user_preference_gets_403_not_500(legacy_admin):
    """写路径与读路径必须对**同一条件**给出同一个答案。

    单密码应急会话没有账号 id ⇒ ``preferences.save()`` 走到"有每用户键却没有账户
    上下文"分支。A4 把这个条件在读路径上(``MissingUserContextError``)映射成了
    403 + ACCOUNT_REQUIRED, 但写路径当时抛的是裸 ``RuntimeError`` ⇒ **同一个条件
    两个答案**(500 vs 403), 而 500 与"没有账号身份"这个真实原因完全对不上,
    语义上也不是服务端故障。

    触发场景是真实存在的: 应急入口在设置页改菜单排序 / 存飞书地址 / 走首次引导 ——
    这些都是 `_authorize` 放行的每用户偏好端点, 靠 contextvar 拿账户根。
    """
    resp = legacy_admin.put("/api/settings/preferences/nav-order", json={"nav_order": ["a", "b"]})
    assert resp.status_code == 403, resp.text
    assert resp.json().get("code") == "ACCOUNT_REQUIRED"


def test_missing_user_context_handler_is_registered():
    from app.services.user_paths import MissingUserContextError

    assert MissingUserContextError in app_main.app.exception_handlers
    assert preferences.GlobalScopeWriteDenied in app_main.app.exception_handlers


# ================================================================
# B2 热路径不得每次读都 mkdir
# ================================================================

def test_hot_path_does_not_mkdir_on_every_read(_isolated, monkeypatch):
    """行情轮询一轮会调 8~12 次 getter; 目录只需确保存在**一次**。"""
    root = _isolated / "users" / "1"
    preferences.load(root)          # 首次: 允许一次 mkdir 建立目录

    calls: list[str] = []
    real_mkdir = Path.mkdir

    def _spy(self, *args, **kwargs):
        calls.append(str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _spy)
    for _ in range(10):
        preferences.load(root)
        preferences.get_realtime_quote_interval()

    assert calls == [], f"每次读取都在 mkdir (轮询热路径): {calls}"


def test_write_still_creates_the_directory(_isolated):
    """去重的只是"重复 mkdir"; 目录不存在时写入仍必须自建。"""
    root = _isolated / "users" / "1" / "user_data"
    assert not root.exists()
    preferences.save({"nav_order": ["/a"]}, user_root=root.parent)
    assert (root / "preferences.json").exists()


def test_write_recovers_when_the_ensured_directory_vanished(_isolated):
    """目录被外部删掉后, 保存必须自愈 —— 这是 mkdir 去重所付出代价的对冲。

    _ensure_dir 的"已确保"是个进程内假设: 目录一旦建成不会再消失。假设不成立时
    (运维清理/测试删临时目录), 没有这条自愈就会把一次本该成功的保存变成
    FileNotFoundError —— 那才是真正的回归。
    """
    import shutil

    root = _isolated / "users" / "1"
    preferences.save({"nav_order": ["/a"]}, user_root=root)   # 目录已被确保
    shutil.rmtree(root)                                       # 外部清理

    preferences.save({"nav_order": ["/b"]}, user_root=root)    # 必须自愈, 而不是报错
    assert preferences.load(root)["nav_order"] == ["/b"]
