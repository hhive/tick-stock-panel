"""AI 上游锁定 —— 面板 AI 只允许走本站 Sub2API 网关。

锁的语义(用户 2026-09-26 决策「ai 配置只能自定义，且自定义的域名也写死这个」):
- 站点只保留「自定义」一种形态, provider 恒为 openai_compat;
- 上游地址恒为 `app.config.AI_GATEWAY_BASE_URL`, **读侧也锁** —— 存量
  secrets.json 里的旧地址/旧 provider 必须失效, 否则「写死」只对新配置成立。

本文件里的每个用例都对应一条可被单独破坏的保证, 不做端到端拼装。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import secrets_store
from app.config import AI_GATEWAY_BASE_URL, settings
from app.api import settings as settings_api
from app.services import ai_provider


@pytest.fixture(autouse=True)
def _user_ctx(tmp_path, monkeypatch):
    """凭据按账户分家: 每用户存储没有共享回退, 单测里显式给出账户根。"""
    from app import config as app_config
    from app.services import preferences

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    token = preferences.set_current_user_root(tmp_path)
    yield tmp_path
    preferences.reset_current_user_root(token)


def test_gateway_constant_is_the_stock_sub2api_instance():
    """常量本身必须是用户指定的那个域名 —— 别的用例都以它为准, 故先钉死它。"""
    assert AI_GATEWAY_BASE_URL == "https://xiaoni-model.top/v1"


# ── 保存: 客户端提交什么都改不了地址 ────────────────────────────


def test_save_forces_locked_base_url_and_reports_it(_user_ctx):
    result = settings_api.save_ai_settings(
        settings_api.AiSettingsIn(
            provider="openai_compat",
            base_url="https://attacker.example/v1",
            api_key="sk-test",
            model="gpt-x",
        )
    )

    assert secrets_store.load()["ai_base_url"] == AI_GATEWAY_BASE_URL
    assert settings.ai_base_url == AI_GATEWAY_BASE_URL
    # 响应必须回传权威地址, 否则前端只能靠本地常量猜, 两边会漂移
    assert result["ai_base_url"] == AI_GATEWAY_BASE_URL


@pytest.mark.parametrize("provider", ["codex_cli", "openai"])
def test_save_rejects_every_provider_but_custom(provider):
    """只允许「自定义」。服务端拒绝, 而不是靠前端不渲染入口。"""
    with pytest.raises(HTTPException) as exc:
        settings_api.save_ai_settings(
            settings_api.AiSettingsIn(provider=provider, model="gpt-5.6-sol")
        )
    assert exc.value.status_code == 400


def test_save_clears_legacy_codex_credentials(_user_ctx):
    """存量 codex 配置在保存时被清掉 —— 锁死后它已不可选, 留着是幽灵配置。"""
    secrets_store.save({
        "ai_codex_command": "codex",
        "ai_codex_reasoning_effort": "high",
        "ai_codex_model": "gpt-5.6-sol",
    })

    settings_api.save_ai_settings(
        settings_api.AiSettingsIn(provider="openai_compat", api_key="sk-test", model="gpt-x")
    )

    stored = secrets_store.load()
    assert not any(k.startswith("ai_codex_") for k in stored)


# ── 读取: 存量旧值一律失效 ──────────────────────────────────────


def test_provider_lock_ignores_stored_codex_config(_user_ctx):
    secrets_store.save({"ai_provider": "codex_cli", "ai_codex_model": "gpt-5.6-sol"})

    assert ai_provider.current_ai_provider() == "openai_compat"
    assert ai_provider.is_codex_cli_provider() is False


def test_base_url_lock_ignores_legacy_secrets(_user_ctx):
    secrets_store.save({"ai_base_url": "https://llm.runninghub.ai/v1"})

    assert ai_provider.ai_base_url() == AI_GATEWAY_BASE_URL


def test_openai_client_is_built_with_locked_base_url(_user_ctx, monkeypatch):
    """真正的出网点: SDK 客户端的 base_url 必须来自常量, 而不是 secrets.json。"""
    secrets_store.save({"ai_base_url": "https://legacy.example/v1", "ai_api_key": "sk-x"})
    captured: dict = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    ai_provider._openai_client("sk-x", 10.0)

    assert captured["base_url"] == AI_GATEWAY_BASE_URL


def test_settings_get_reports_locked_base_url(_user_ctx, monkeypatch):
    secrets_store.save({"ai_base_url": "https://legacy.example/v1", "ai_provider": "codex_cli"})

    got = settings_api.get_settings()

    assert got["ai_base_url"] == AI_GATEWAY_BASE_URL
    assert got["ai_provider"] == "openai_compat"


# ── 清空: 回到常量, 不是回到空 ──────────────────────────────────


def test_clear_restores_locked_base_url(_user_ctx):
    secrets_store.save({"ai_base_url": "https://legacy.example/v1"})

    settings_api.clear_ai_settings()

    assert settings.ai_base_url == AI_GATEWAY_BASE_URL
    assert secrets_store.load().get("ai_base_url", "") in ("", AI_GATEWAY_BASE_URL)


# ── 模型列表: 从本站网关拉, 需要用户自己的 key ──────────────────


def test_models_endpoint_requires_a_key(_user_ctx):
    import asyncio

    with pytest.raises(HTTPException) as exc:
        asyncio.run(settings_api.list_ai_models(settings_api.AiModelsIn()))
    assert exc.value.status_code == 400


def test_models_endpoint_queries_locked_gateway_with_the_key(_user_ctx, monkeypatch):
    seen: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {"data": [{"id": "gpt-x"}, {"id": "gpt-y"}]}

    class FakeClient:
        def __init__(self, **kwargs): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None: ...

        async def get(self, url: str, **kwargs):
            seen["url"] = url
            seen["headers"] = kwargs.get("headers") or {}
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def run() -> dict:
        return await settings_api.list_ai_models(settings_api.AiModelsIn(api_key="sk-user"))

    import asyncio

    got = asyncio.run(run())

    assert seen["url"] == f"{AI_GATEWAY_BASE_URL}/models"
    assert "sk-user" in str(seen["headers"])
    assert got["models"] == ["gpt-x", "gpt-y"]
