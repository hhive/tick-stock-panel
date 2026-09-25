"""多用户账号端点 (/api/account/*) 的行为测试。

用 TestClient 但**不作为上下文管理器**使用 —— 那样不会触发 lifespan, 因此不会
启动调度器/行情轮询等后台服务, 测试保持轻量与离线。

隔离要点: 本套件涉及四个跨用例会串味的模块级全局, fixture 必须逐个重置:
  - accounts._roles_cache      (角色缓存)
  - account_sessions._sessions (内存会话表)
  - account_api._register_hits (注册限流计数)
  - main._guest_hits           (游客限流计数)
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app.api import account as account_api
from app.services import account_sessions, accounts, sub2api_verify

VALID_KEY = "sk-valid-test-key"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每个用例独立 DATA_DIR, 并清掉全部模块级内存态。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()
    yield tmp_path


@pytest.fixture
def client():
    return TestClient(app_main.app)


@pytest.fixture
def verified_keys(monkeypatch):
    """把校验函数替换为「只有 VALID_KEY 有效」, 测试绝不外呼 Sub2API。"""

    def fake_verify(api_key: str) -> bool:
        return (api_key or "").strip() == VALID_KEY

    monkeypatch.setattr(sub2api_verify, "verify_api_key", fake_verify)
    return VALID_KEY


@pytest.fixture
def claimed(client):
    """面板**已认领**但当前无会话: 注册一个账号后登出。

    用于区分两种未登录语义: 未认领的面板回 403(防公网抢占设密码),
    已认领的面板回 401(前端跳登录页)。
    """
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    client.post("/api/account/logout")
    return client


# ================================================================
# 注册
# ================================================================

def test_first_registration_becomes_admin(client):
    r = client.post("/api/account/register",
                    json={"email": "owner@example.com", "password": "secret123"})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_second_registration_is_regular_user(client):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    r = client.post("/api/account/register",
                    json={"email": "second@example.com", "password": "secret123"})
    assert r.status_code == 200
    assert r.json()["role"] == "user"


def test_duplicate_email_is_rejected_case_insensitively(client):
    client.post("/api/account/register",
                json={"email": "Owner@Example.com", "password": "secret123"})
    r = client.post("/api/account/register",
                    json={"email": "owner@example.com", "password": "secret123"})
    assert r.status_code == 409


def test_short_password_rejected_by_validation(client):
    """pydantic 的 min_length 由 FastAPI 转成 422(不是 handler 里的 400)。"""
    r = client.post("/api/account/register",
                    json={"email": "a@example.com", "password": "123"})
    assert r.status_code == 422


def test_register_creates_per_user_dirs(client, _isolated):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    assert (_isolated / "users" / "1" / "user_data").is_dir()


# ================================================================
# 登录 / 登出 / 我的
# ================================================================

def test_login_sets_session_and_me_works(client):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    client.post("/api/account/logout")

    r = client.post("/api/account/login",
                    json={"email": "owner@example.com", "password": "secret123"})
    assert r.status_code == 200

    me = client.get("/api/account/me")
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"
    assert me.json()["role"] == "admin"
    assert me.json()["bindings"] == []


def test_login_with_wrong_password_is_401(client):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    client.post("/api/account/logout")
    r = client.post("/api/account/login",
                    json={"email": "owner@example.com", "password": "wrong-pass"})
    assert r.status_code == 401


def test_me_without_session_on_unclaimed_panel_is_403(client):
    """全新面板(既无账号也未设密码)对公网仍回 403 —— 防陌生人抢先设密码。"""
    r = client.get("/api/account/me")
    assert r.status_code == 403
    assert r.json()["code"] == "NOT_INITIALIZED"


def test_me_without_session_on_claimed_panel_is_401(claimed):
    """已有账号的面板即视为已认领: 未登录回 401, 前端据此跳登录页。"""
    assert claimed.get("/api/account/me").status_code == 401


def test_logout_revokes_session(client):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    assert client.get("/api/account/me").status_code == 200
    client.post("/api/account/logout")
    assert client.get("/api/account/me").status_code == 401


# ================================================================
# 跳转登录
# ================================================================

def test_jump_rejects_invalid_key(claimed, verified_keys):
    r = claimed.post("/api/account/jump", json={"api_key": "sk-bogus"})
    assert r.status_code == 401
    assert claimed.get("/api/account/me").status_code == 401


def test_jump_with_valid_unbound_key_needs_auth(claimed, verified_keys):
    r = claimed.post("/api/account/jump", json={"api_key": VALID_KEY})
    assert r.status_code == 200
    assert r.json()["status"] == "needs_auth"
    # 未绑定不得发放会话
    assert claimed.get("/api/account/me").status_code == 401


def test_jump_logs_in_when_key_is_bound(client, verified_keys):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123",
                      "api_key": VALID_KEY})
    client.post("/api/account/logout")

    r = client.post("/api/account/jump", json={"api_key": VALID_KEY})
    assert r.status_code == 200
    assert r.json() == {"status": "logged_in", "email": "owner@example.com"}
    assert client.get("/api/account/me").status_code == 200


def test_register_with_invalid_key_is_rejected(client, verified_keys):
    r = client.post("/api/account/register",
                    json={"email": "a@example.com", "password": "secret123",
                          "api_key": "sk-bogus"})
    assert r.status_code == 401
    # 账号不应被创建
    assert accounts.count() == 0


def test_register_with_key_already_bound_elsewhere_is_409(client, verified_keys):
    client.post("/api/account/register",
                json={"email": "first@example.com", "password": "secret123",
                      "api_key": VALID_KEY})
    client.post("/api/account/logout")
    r = client.post("/api/account/register",
                    json={"email": "second@example.com", "password": "secret123",
                          "api_key": VALID_KEY})
    assert r.status_code == 409


# ================================================================
# 绑定管理
# ================================================================

def test_bind_then_jump_logs_in(client, verified_keys):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    assert client.post("/api/account/bindings",
                       json={"api_key": VALID_KEY}).status_code == 200
    assert client.get("/api/account/me").json()["bindings"] != []

    client.post("/api/account/logout")
    assert client.post("/api/account/jump",
                       json={"api_key": VALID_KEY}).json()["status"] == "logged_in"


def test_bind_conflict_with_another_account_is_409(client, verified_keys):
    client.post("/api/account/register",
                json={"email": "first@example.com", "password": "secret123",
                      "api_key": VALID_KEY})
    client.post("/api/account/logout")
    client.post("/api/account/register",
                json={"email": "second@example.com", "password": "secret123"})
    r = client.post("/api/account/bindings", json={"api_key": VALID_KEY})
    assert r.status_code == 409


def test_bind_requires_session(claimed, verified_keys):
    r = claimed.post("/api/account/bindings", json={"api_key": VALID_KEY})
    assert r.status_code == 401


def test_bind_rejects_invalid_key(client, verified_keys):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123"})
    assert client.post("/api/account/bindings",
                       json={"api_key": "sk-bogus"}).status_code == 401


def test_unbind_makes_jump_need_auth_again(client, verified_keys):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123",
                      "api_key": VALID_KEY})
    assert client.request("DELETE", "/api/account/bindings",
                          json={"api_key": VALID_KEY}).status_code == 200
    client.post("/api/account/logout")
    assert client.post("/api/account/jump",
                       json={"api_key": VALID_KEY}).json()["status"] == "needs_auth"


def test_me_never_returns_plaintext_key(client, verified_keys):
    client.post("/api/account/register",
                json={"email": "owner@example.com", "password": "secret123",
                      "api_key": VALID_KEY})
    body = client.get("/api/account/me").text
    assert VALID_KEY not in body


# ================================================================
# 注册限流
# ================================================================

def test_register_rate_limited_after_five(client):
    for i in range(5):
        r = client.post("/api/account/register",
                        json={"email": f"u{i}@example.com", "password": "secret123"})
        assert r.status_code == 200, f"第 {i + 1} 次注册应成功"
    r = client.post("/api/account/register",
                    json={"email": "blocked@example.com", "password": "secret123"})
    assert r.status_code == 429
