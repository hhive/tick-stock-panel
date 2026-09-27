"""跳转绑定 + 跳转 key 自动成为 AI 凭据。

背景(2026-09-27 用户报告「从 xiaoni-model 携带 apikey 跳转过来，怎么没自动配置上」):
  1. 已登录用户带跳转凭证落地时, 凭证被静默丢弃 —— JumpGate 把人转到 /login,
     而 Auth 页挂载即因「已认证」直接回面板, 绑定那一步从未执行;
  2. 跳转/绑定拿到的 key 从不写入该账号的 AI 配置 —— 全后端只有 AI 设置页手动保存
     会写 `ai_api_key`, 这段从未实现。

本文件钉住两条: **key 绑得上**, **绑上之后 AI 配置里有它**。用户裁定(2026-09-27):
写入策略 = 仅未设置时填(不覆盖手动填过的 key); ai_configured = 有 key **且** 有模型。
"""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app.api import account as account_api
from app.services import account_sessions, accounts, sub2api_verify, user_paths

VALID_KEY = "sk-valid-test-key"
OTHER_KEY = "sk-other-valid-key"


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
    return TestClient(app_main.app)


@pytest.fixture
def verified_keys(monkeypatch):
    """只认两把 key, 测试绝不外呼 Sub2API。"""

    def fake_verify(api_key: str) -> bool:
        return (api_key or "").strip() in (VALID_KEY, OTHER_KEY)

    monkeypatch.setattr(sub2api_verify, "verify_api_key", fake_verify)


def _secrets_of(account_id: int) -> dict:
    path = user_paths.ensure_user_dirs(account_id) / "user_data" / "secrets.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _register(client: TestClient, email: str) -> None:
    res = client.post("/api/account/register", json={"email": email, "password": "secret123"})
    assert res.status_code == 200, res.text


# ── 缺陷 1: 已登录用户带 key 落地, key 必须绑上 ──────────────────


def test_jump_binds_key_to_the_logged_in_account(client, verified_keys):
    """已登录 + 有效 key + 未绑定 → 绑到当前账号并放行, 不再丢去登录页。"""
    _register(client, "owner@example.com")

    res = client.post("/api/account/jump", json={"api_key": VALID_KEY})

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "logged_in"
    me = client.get("/api/account/me").json()
    assert len(me["bindings"]) == 1


def test_jump_without_a_session_still_asks_for_auth(client, verified_keys):
    """无会话时保持既有语义: needs_auth, 且不把 key 落到任何账号名下。"""
    res = client.post("/api/account/jump", json={"api_key": VALID_KEY})

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "needs_auth"
    assert _secrets_of(1) == {}


def test_jump_for_a_key_owned_by_another_account_logs_in_as_that_owner(client, verified_keys):
    """「持有 key 即身份」不变: key 已属他人 → 以他人身份登录, 不动自己的 AI 凭据。"""
    _register(client, "a@example.com")
    client.post("/api/account/jump", json={"api_key": VALID_KEY})  # A 绑上 VALID_KEY
    client.post("/api/account/logout")
    _register(client, "b@example.com")
    client.post("/api/account/bindings", json={"api_key": OTHER_KEY})  # B 绑 OTHER_KEY

    res = client.post("/api/account/jump", json={"api_key": VALID_KEY})

    assert res.json() == {"status": "logged_in", "email": "a@example.com"}
    assert _secrets_of(2)["ai_api_key"] == OTHER_KEY  # B 的凭据未被 A 的 key 覆盖


# ── 缺陷 2: 跳转/绑定的 key 成为 AI 凭据 ─────────────────────────


def test_jump_fills_ai_key_when_unset(client, verified_keys):
    _register(client, "owner@example.com")

    client.post("/api/account/jump", json={"api_key": VALID_KEY})

    assert _secrets_of(1)["ai_api_key"] == VALID_KEY


def test_jump_never_overwrites_a_manually_entered_ai_key(client, verified_keys):
    """用户裁定「仅未设置时填」: 手动填过的 key 是显式选择, 跳转不得改写。"""
    _register(client, "owner@example.com")
    root = user_paths.ensure_user_dirs(1)
    from app import secrets_store

    secrets_store.save({"ai_api_key": "sk-manual"}, user_root=root)

    client.post("/api/account/jump", json={"api_key": VALID_KEY})

    assert _secrets_of(1)["ai_api_key"] == "sk-manual"
    # 但绑定本身必须发生 —— 两件事互不牵连
    assert len(client.get("/api/account/me").json()["bindings"]) == 1


def test_register_with_key_fills_ai_key(client, verified_keys):
    client.post(
        "/api/account/register",
        json={"email": "owner@example.com", "password": "secret123", "api_key": VALID_KEY},
    )

    assert _secrets_of(1)["ai_api_key"] == VALID_KEY


def test_bindings_endpoint_fills_ai_key(client, verified_keys):
    _register(client, "owner@example.com")

    client.post("/api/account/bindings", json={"api_key": VALID_KEY})

    assert _secrets_of(1)["ai_api_key"] == VALID_KEY


def test_ai_key_write_failure_does_not_block_the_binding(client, verified_keys, monkeypatch):
    """凭据落盘失败不能让登录/绑定失败 —— 绑定已完成, 用户可手动补 AI Key。"""
    from app import secrets_store

    def boom(*args, **kwargs):
        raise OSError("disk full")

    _register(client, "owner@example.com")
    monkeypatch.setattr(secrets_store, "save", boom)

    res = client.post("/api/account/jump", json={"api_key": VALID_KEY})

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "logged_in"
    assert len(client.get("/api/account/me").json()["bindings"]) == 1


# ── 用户裁定 D2: 有 key 无模型不算「已配置」─────────────────────


@pytest.fixture
def _ai_user_ctx(tmp_path, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    # 内存里的 settings 是全局单例, 会被别处的 save_ai_settings 改掉(它同步写
    # settings.ai_model)。想要真隔离就得连这几个字段一起钉住, 否则本用例读到的
    # 「模型为空」取决于谁先跑过。
    for field, value in (("ai_api_key", ""), ("ai_model", ""), ("ai_provider", "openai_compat")):
        monkeypatch.setattr(app_config.settings, field, value)
    token = preferences.set_current_user_root(tmp_path)
    yield tmp_path
    preferences.reset_current_user_root(token)


def test_ai_configured_requires_both_key_and_model(_ai_user_ctx):
    from app import secrets_store
    from app.services import ai_provider

    assert ai_provider.ai_configured() is False

    secrets_store.save({"ai_api_key": "sk-x"})
    assert ai_provider.ai_configured() is False, "只有 key、没有模型 → 调用必失败, 不得算已配置"

    secrets_store.save({"ai_model": "gpt-x"})
    assert ai_provider.ai_configured() is True
