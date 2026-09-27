"""AI 默认模型: 按用户自己的 key 挑, 挑不到退回部署级默认。

背景(2026-09-27「带 apikey 跳转过来 apikey 没生效」的定性结论):
  sub2api 两端与 key 送达都正常, 真正缺的是**模型** —— 跳转只填了 `ai_api_key`,
  没人知道该选哪个模型, 于是 `ai_configured()` 恒 false, 界面停在「还差一步: 选择模型」。

用户裁定(2026-09-27): **不写死**一个可能不在该用户分组里的模型名, 而是拿他自己的 key
拉一次 `/v1/models`(Sub2API 按分组过滤, 必须用用户自己的 key):
  清单里有偏好模型 → 用它; 没有 → 用清单第一个; 拉取失败 → 不写入, 退回部署级默认
  (`config.DEFAULT_AI_MODEL`)。

两件事各自「仅未设置时填」: 手动填过的 `ai_api_key` / `ai_model` 都是显式选择, 不得被改写。
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
    """只认一把 key, 测试绝不外呼 Sub2API 的校验端点。"""
    monkeypatch.setattr(sub2api_verify, "verify_api_key", lambda api_key: (api_key or "").strip() == VALID_KEY)


def _secrets_of(account_id: int) -> dict:
    path = user_paths.ensure_user_dirs(account_id) / "user_data" / "secrets.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _register(client: TestClient, email: str) -> None:
    res = client.post("/api/account/register", json={"email": email, "password": "secret123"})
    assert res.status_code == 200, res.text


# ── 纯函数: 清单归一化(与 POST /api/settings/ai/models 共用) ─────────


def test_normalize_models_keeps_only_nonempty_string_ids():
    from app.services.ai_provider import _normalize_models

    payload = {"data": [
        {"id": "gpt-b"},
        {"id": "gpt-a"},
        {"id": "gpt-b"},          # 重复
        {"id": ""},               # 空 id
        {"id": 42},               # 非字符串
        {"not_an_id": "x"},       # 缺 id
        "not-a-dict",
        None,
    ]}

    assert _normalize_models(payload) == ["gpt-a", "gpt-b"]


@pytest.mark.parametrize("payload", [None, [], "x", {}, {"data": None}, {"data": {}}, {"data": [1, 2]}])
def test_normalize_models_tolerates_malformed_payloads(payload):
    from app.services.ai_provider import _normalize_models

    assert _normalize_models(payload) == []


# ── 按 key 挑模型 ────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload=None, *, json_exc=None, http_exc=None):
        self._payload = payload
        self._json_exc = json_exc
        self._http_exc = http_exc

    def raise_for_status(self):
        if self._http_exc:
            raise self._http_exc

    def json(self):
        if self._json_exc:
            raise self._json_exc
        return self._payload


@pytest.fixture
def fake_httpx(monkeypatch):
    """把 ai_provider 里的 httpx 换成桩, 记录请求并按需返回/抛错。"""
    from app.services import ai_provider

    calls: list[tuple[str, dict]] = []
    box: dict = {"payload": None, "exc": None}

    class _FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            calls.append((url, dict(headers or {})))
            if box["exc"] is not None:
                raise box["exc"]
            return _FakeResponse(box["payload"])

    monkeypatch.setattr(ai_provider, "httpx", type("_ns", (), {"Client": _FakeClient}))
    return {"calls": calls, "box": box}


def test_pick_sends_the_users_own_key_to_the_gateway(fake_httpx):
    from app.services import ai_provider

    fake_httpx["box"]["payload"] = {"data": [{"id": "gpt-6-sol"}]}

    assert ai_provider.pick_model_for_key("sk-user") == "gpt-6-sol"

    url, headers = fake_httpx["calls"][0]
    assert url == "https://xiaoni-model.top/v1/models", "必须打本站网关的 models 端点"
    assert headers.get("Authorization") == "Bearer sk-user", "必须带用户自己的 key(按分组过滤)"


def test_pick_prefers_the_configured_default_model(fake_httpx):
    from app import config as cfg
    from app.services import ai_provider

    fake_httpx["box"]["payload"] = {"data": [{"id": "aaa"}, {"id": cfg.DEFAULT_AI_MODEL}, {"id": "zzz"}]}

    assert ai_provider.pick_model_for_key(VALID_KEY) == cfg.DEFAULT_AI_MODEL


def test_pick_falls_back_to_first_sorted_model_when_preference_absent(fake_httpx):
    from app.services import ai_provider

    fake_httpx["box"]["payload"] = {"data": [{"id": "zeta"}, {"id": "alpha"}, {"id": "mid"}]}

    assert ai_provider.pick_model_for_key(VALID_KEY) == "alpha"


@pytest.mark.parametrize("exc", [TimeoutError("boom"), OSError("conn refused"), RuntimeError("http 401")])
def test_pick_returns_empty_on_transport_failure(fake_httpx, exc):
    from app.services import ai_provider

    fake_httpx["box"]["exc"] = exc

    assert ai_provider.pick_model_for_key(VALID_KEY) == ""


def test_pick_returns_empty_on_malformed_payload(fake_httpx):
    from app.services import ai_provider

    for payload in ({"data": None}, {"unexpected": 1}, [1, 2, 3], {"data": [{"id": ""}]}):
        fake_httpx["box"]["payload"] = payload
        assert ai_provider.pick_model_for_key(VALID_KEY) == ""


def test_pick_does_not_call_the_gateway_without_a_key(fake_httpx):
    from app.services import ai_provider

    assert ai_provider.pick_model_for_key("") == ""
    assert fake_httpx["calls"] == [], "没 key 就不该出网"


# ── 跳转采纳: 模型与 key 各自「仅未设置时填」 ────────────────────────


def test_jump_picks_a_model_for_the_users_key(client, verified_keys, monkeypatch):
    from app.services import ai_provider

    monkeypatch.setattr(ai_provider, "pick_model_for_key", lambda key, **kw: "gpt-6-sol")
    _register(client, "owner@example.com")

    client.post("/api/account/jump", json={"api_key": VALID_KEY})

    secrets = _secrets_of(1)
    assert secrets["ai_api_key"] == VALID_KEY
    assert secrets["ai_model"] == "gpt-6-sol"


def test_jump_picks_the_model_with_the_accounts_effective_key(client, verified_keys, monkeypatch):
    """手动填过 AI Key 的账号: 挑模型要用**它的** key, 不是跳转那把。"""
    from app import secrets_store
    from app.services import ai_provider

    seen: list[str] = []

    def fake_pick(key: str, **kw) -> str:
        seen.append(key)
        return "gpt-picked"

    monkeypatch.setattr(ai_provider, "pick_model_for_key", fake_pick)
    _register(client, "owner@example.com")
    root = user_paths.ensure_user_dirs(1)
    secrets_store.save({"ai_api_key": "sk-manual"}, user_root=root)

    client.post("/api/account/jump", json={"api_key": VALID_KEY})

    assert seen == ["sk-manual"], "必须用账号实际生效的 key 挑, 否则可能挑到它没有的模型"
    assert _secrets_of(1)["ai_model"] == "gpt-picked"


def test_jump_never_overwrites_a_manually_chosen_model(client, verified_keys, monkeypatch):
    from app import secrets_store
    from app.services import ai_provider

    monkeypatch.setattr(ai_provider, "pick_model_for_key", lambda key, **kw: "gpt-picked")
    _register(client, "owner@example.com")
    root = user_paths.ensure_user_dirs(1)
    secrets_store.save({"ai_model": "gpt-manual"}, user_root=root)

    client.post("/api/account/jump", json={"api_key": VALID_KEY})

    assert _secrets_of(1)["ai_model"] == "gpt-manual", "手动选的模型是显式选择, 跳转不得改写"


def test_pick_failure_leaves_model_unset_so_deployment_default_applies(client, verified_keys, monkeypatch):
    from app.services import ai_provider

    monkeypatch.setattr(ai_provider, "pick_model_for_key", lambda key, **kw: "")
    _register(client, "owner@example.com")

    client.post("/api/account/jump", json={"api_key": VALID_KEY})

    secrets = _secrets_of(1)
    assert secrets["ai_api_key"] == VALID_KEY, "挑模型失败不影响 key 的采纳"
    assert "ai_model" not in secrets, "拉不到清单就不写模型, 让部署级默认生效(而不是落一个空字符串把默认盖掉)"


def test_pick_exception_never_blocks_the_binding(client, verified_keys, monkeypatch):
    """这是登录/绑定的附带收益 —— 任何异常都不能让用户连面板都进不去。"""
    from app.services import ai_provider

    def boom(key: str, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(ai_provider, "pick_model_for_key", boom)
    _register(client, "owner@example.com")

    res = client.post("/api/account/jump", json={"api_key": VALID_KEY})

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "logged_in"
    assert _secrets_of(1)["ai_api_key"] == VALID_KEY


def test_register_with_key_also_picks_a_model(client, verified_keys, monkeypatch):
    from app.services import ai_provider

    monkeypatch.setattr(ai_provider, "pick_model_for_key", lambda key, **kw: "gpt-6-sol")

    client.post(
        "/api/account/register",
        json={"email": "owner@example.com", "password": "secret123", "api_key": VALID_KEY},
    )

    assert _secrets_of(1)["ai_model"] == "gpt-6-sol"


def test_bindings_endpoint_also_picks_a_model(client, verified_keys, monkeypatch):
    from app.services import ai_provider

    monkeypatch.setattr(ai_provider, "pick_model_for_key", lambda key, **kw: "gpt-6-sol")
    _register(client, "owner@example.com")

    client.post("/api/account/bindings", json={"api_key": VALID_KEY})

    assert _secrets_of(1)["ai_model"] == "gpt-6-sol"


# ── 部署级默认: 让「带 key 过来」直接可用 ────────────────────────────


def test_deployment_default_is_wired_everywhere(tmp_path, monkeypatch):
    """默认模型只有一个事实来源: config.DEFAULT_AI_MODEL。"""
    from app import secrets_store
    from app.services import preferences

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    token = preferences.set_current_user_root(tmp_path)
    try:
        assert app_config.DEFAULT_AI_MODEL == "gpt-6-sol"
        assert app_config.settings.ai_model == app_config.DEFAULT_AI_MODEL
        assert app_config.AI_ENV_DEFAULTS["ai_model"] == app_config.DEFAULT_AI_MODEL

        secrets_store.save({"ai_api_key": "sk-only-key"})
        from app.services import ai_provider

        assert ai_provider.current_ai_model() == "gpt-6-sol"
        assert ai_provider.ai_configured() is True, "有 key + 部署级默认模型 ⇒ 跳转进来即可用"
    finally:
        preferences.reset_current_user_root(token)
