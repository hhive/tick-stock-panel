"""认证中间件四态与游客授权分层测试。

断言的是**中间件的分层决策**, 而非 handler 的返回内容: 无 lifespan 启动时
`app.state.repo` 等并不存在, handler 可能 500, 但那不是本套件要测的东西。
因此判据是「中间件是否放行」——放行则状态码不会落在 401/403/429 里。

TestClient 的 client host 是 "testclient", 不属于本机/内网, 因此等价于公网访客。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app.api import account as account_api
from app.services import account_sessions, accounts, auth as auth_service

# 中间件拒绝时会返回的状态码; 放行后 handler 返回什么都不算拒绝
_DENIED = (401, 403, 429)

# 一条典型的每用户(安装级)状态路由: 必须始终需要登录
_PER_USER_PATH = "/api/watchlist"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()
    yield tmp_path


@pytest.fixture
def client():
    # raise_server_exceptions=False: 本套件只断言**中间件的分层决策**, 不关心
    # handler 返回什么。无 lifespan 启动时 app.state.repo 等不存在, handler 会抛
    # AttributeError; 默认 TestClient 会把它直接抛出, 让断言拿不到响应。
    # 关掉之后 handler 错误变成 500, 而 500 不在 _DENIED 里, 判据依旧成立。
    return TestClient(app_main.app, raise_server_exceptions=False)


@pytest.fixture
def claimed(client):
    """已认领(存在账号)但当前无会话的面板。"""
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    client.post("/api/account/logout")
    return client


# ================================================================
# 分层清单的完整性(防意外放宽)
# ================================================================

def test_every_public_read_entry_matches_a_real_route():
    """清单里不得有拼错或已删除的路径 —— 否则是"以为开着其实没开"或反之。"""
    paths = {r.path for r in app_main.app.routes}
    for p in (app_main._PUBLIC_READ_EXACT + app_main._PUBLIC_READ_POST):
        assert p in paths, f"公开只读清单里的 {p} 不是真实路由"
    for prefix in app_main._PUBLIC_READ_PREFIX:
        assert any(p.startswith(prefix) for p in paths), f"前缀 {prefix} 未匹配任何路由"


def test_public_read_set_is_frozen():
    """把清爽单冻结: 任何放宽(新增公开路径)都必须显式改这个测试。

    这是 fail-closed 的护栏 —— 新增路由若被顺手加进公开清单, 这里会红。
    """
    assert sorted(app_main._PUBLIC_READ_EXACT) == sorted([
        "/api/abnormal/intraday", "/api/abnormal/overview",
        "/api/data/status", "/api/data/version",
        "/api/intraday/indices", "/api/intraday/status",
        "/api/kline/daily", "/api/kline/daily/latest",
        "/api/kline/instruments/search",
        "/api/kline/minute", "/api/kline/minute-range",
        "/api/market-recap/auction-benchmark", "/api/market-recap/dragon-tiger",
        "/api/overview/market",
        "/api/regime/coverage", "/api/regime/history", "/api/regime/latest",
        "/api/regime/mainline", "/api/regime/phases", "/api/regime/states",
        "/api/rps/rotation",
        "/api/screener/cached-summary", "/api/screener/strategies",
        "/api/sector-rotation",
        "/api/stock-analysis/levels",
    ])
    assert app_main._PUBLIC_READ_PREFIX == ("/api/screener/cached-result/",)
    assert app_main._PUBLIC_READ_POST == ("/api/kline/instruments/names",)


def test_per_user_route_is_not_public():
    assert _PER_USER_PATH not in app_main._PUBLIC_READ_EXACT
    assert not any(_PER_USER_PATH.startswith(p) for p in app_main._PUBLIC_READ_PREFIX)


def test_capabilities_and_settings_are_not_public():
    """勘察标为边界项的那批一律不公开(含混合了服务端配置的 preferences)。"""
    for p in ("/api/capabilities", "/api/settings/preferences",
              "/api/settings/data-sources", "/api/ext-data",
              "/api/settings/ai/sponsor-models"):
        assert p not in app_main._PUBLIC_READ_EXACT


# ================================================================
# 四态
# ================================================================

def test_guest_can_read_public_read_route(claimed):
    r = claimed.get("/api/data/version")
    assert r.status_code not in _DENIED, (
        f"游客应能访问公开只读路由, 实际 {r.status_code}"
    )


def test_guest_cannot_read_per_user_route(claimed):
    assert claimed.get(_PER_USER_PATH).status_code == 401


def test_account_session_can_read_per_user_route(client):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    r = client.get(_PER_USER_PATH)
    assert r.status_code not in (401, 403), (
        f"已登录账号应能访问每用户路由, 实际 {r.status_code}"
    )


def test_unclaimed_panel_denies_public_ip_with_403(client):
    """全新面板(无账号无密码)对公网回 403, 防陌生人抢先设密码。"""
    r = client.get(_PER_USER_PATH)
    assert r.status_code == 403
    assert r.json()["code"] == "NOT_INITIALIZED"


def test_claimed_panel_returns_401_for_unauthenticated(claimed):
    r = claimed.get(_PER_USER_PATH)
    assert r.status_code == 401


def test_public_read_allowed_even_on_unclaimed_panel(client):
    """未认领也不该挡公开只读 —— 游客样品页与认领状态无关。"""
    r = client.get("/api/data/version")
    assert r.status_code not in _DENIED


# ================================================================
# 单密码应急入口不回归
# ================================================================

def test_auth_status_recognises_account_session(client):
    """回归: /api/auth/status 必须把**账号会话**认作已登录。

    原先它只查单密码会话表, 导致已登录的多用户刷新页面即被打回登录页 ——
    前端只能各自再探 /api/account/me 绕过, 等于把服务端契约漏洞转嫁给每个客户端。
    """
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    body = client.get("/api/auth/status").json()
    assert body["authenticated"] is True
    assert body["mode"] == "account"
    assert body["role"] == "admin"
    assert body["email"] == "owner@example.com"


def test_auth_status_guest_is_not_authenticated(claimed):
    body = claimed.get("/api/auth/status").json()
    assert body["authenticated"] is False
    assert body["mode"] == "guest"
    # 已有账号 ⇒ 面板已认领(但 configured 仍只表示"设过单密码", 语义未变)
    assert body["claimed"] is True


def test_auth_status_legacy_session_is_authenticated(client):
    auth_service.set_password("legacy-pass-123")
    client.cookies.clear()
    client.post("/api/auth/login", json={"password": "legacy-pass-123"})
    body = client.get("/api/auth/status").json()
    assert body["authenticated"] is True
    assert body["mode"] == "legacy"
    assert body["role"] == "admin"


def test_legacy_password_session_still_works(client):
    """既有的单密码路径必须继续可用(应急入口), 且行为不变。"""
    auth_service.set_password("legacy-pass-123")
    client.cookies.clear()

    assert client.get(_PER_USER_PATH).status_code == 401  # 未登录仍是 401

    r = client.post("/api/auth/login", json={"password": "legacy-pass-123"})
    assert r.status_code == 200
    assert client.get(_PER_USER_PATH).status_code not in (401, 403)


def test_legacy_session_reports_admin_role(client):
    auth_service.set_password("legacy-pass-123")
    client.cookies.clear()
    client.post("/api/auth/login", json={"password": "legacy-pass-123"})
    # 应急入口等价管理员: 用它访问受保护路由应放行
    assert client.get(_PER_USER_PATH).status_code not in (401, 403)


# ================================================================
# 白名单收紧
# ================================================================

def test_docs_endpoints_are_gated_for_guests(claimed):
    """/openapi.json 不在 /api/ 前缀下, 必须被单独拦截, 否则等于没关。"""
    assert claimed.get("/openapi.json").status_code == 401


def test_logout_and_change_password_no_longer_prefix_whitelisted():
    """原 ("/api/auth/",) 前缀会无条件放行这两个; 现在改为精确白名单。"""
    assert "/api/auth/logout" not in app_main._AUTH_WHITELIST_EXACT
    assert "/api/auth/change-password" not in app_main._AUTH_WHITELIST_EXACT
    assert "/api/auth/status" in app_main._AUTH_WHITELIST_EXACT


def test_account_entrypoints_are_whitelisted():
    for p in ("/api/account/jump", "/api/account/register", "/api/account/login"):
        assert p in app_main._AUTH_WHITELIST_EXACT


# ================================================================
# 游客限流(新引入的放大面)
# ================================================================

def test_guest_rate_limit_on_expensive_public_route(claimed):
    """重算类只读(背后是全市场重建, TTL 仅 5s)必须限量。"""
    codes = [claimed.get("/api/overview/market").status_code
             for _ in range(app_main._GUEST_LIMIT_EXPENSIVE + 1)]
    assert 429 in codes, "超过额度后应出现 429"
    assert codes[-1] == 429


def test_guest_cheap_and_expensive_buckets_are_independent(claimed):
    """普通只读与重算类分开计量: 打满重算额度不应连累普通只读。"""
    for _ in range(app_main._GUEST_LIMIT_EXPENSIVE):
        claimed.get("/api/overview/market")
    assert claimed.get("/api/overview/market").status_code == 429
    assert claimed.get("/api/data/version").status_code not in _DENIED


def test_authenticated_user_is_not_guest_rate_limited(client):
    """限流只针对游客 —— 登录用户不该被游客额度影响。"""
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    codes = [client.get("/api/overview/market").status_code
             for _ in range(app_main._GUEST_LIMIT_EXPENSIVE + 5)]
    assert 429 not in codes
