"""用户自己的数据源 key —— 用户填了就以他的为准, 共享层不受影响。

设计要点(2026-09-27 用户授权「你自己定」, 目标「省性能 + 多用户独立」):
  - **两层作用域**: 共享层(部署级 key, 后台同步/import 期/worker 用)不动; 账户层
    (用户自己的 key)只在**有账户上下文**的读取上优先。无上下文时**跳过**账户层,
    回落部署级 —— 这正是绕开「按账户分会让 fuyao 插件 import 期解析失败」那堵墙的
    唯一区别: 跳过而非判失败, 所以 import 期**永远有值**。
  - 客户端**不能是进程级单例**: 请求路径一旦共用实例, A 的 key 会被 B 的请求用上 ——
    那是密钥串号, 不是性能问题。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app import secrets_store
from app.api import account as account_api
from app.services import account_sessions, accounts, preferences, sub2api_verify, user_paths

VALID_KEY = "sk-valid-test-key"
USER_KEY = "sk-user-own-key"
DEPLOY_KEY = "sk-deploy-key"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    yield tmp_path


@pytest.fixture
def ctx(tmp_path):
    """带账户上下文的读(请求路径就是这么走的)。"""
    token = preferences.set_current_user_root(tmp_path)
    yield tmp_path
    preferences.reset_current_user_root(token)


@pytest.fixture
def client():
    return TestClient(app_main.app)


@pytest.fixture
def verified(monkeypatch):
    monkeypatch.setattr(sub2api_verify, "verify_api_key", lambda k: (k or "").strip() == VALID_KEY)


# ── 解析优先级: 用户 > 部署 > 环境变量 ──────────────────────────


def test_user_key_wins_over_deployment(ctx):
    secrets_store.save_deployment({"fuyao_api_key": DEPLOY_KEY})
    secrets_store.save_user_secret("fuyao_api_key", USER_KEY)

    assert secrets_store.get_env_backed_secret("fuyao_api_key", "FUYAO_API_KEY") == USER_KEY


def test_falls_back_to_deployment_when_user_has_none(ctx):
    secrets_store.save_deployment({"fuyao_api_key": DEPLOY_KEY})

    assert secrets_store.get_env_backed_secret("fuyao_api_key", "FUYAO_API_KEY") == DEPLOY_KEY


def test_falls_back_to_env_when_nothing_stored(ctx, monkeypatch):
    monkeypatch.setenv("FUYAO_API_KEY", "sk-env")

    assert secrets_store.get_env_backed_secret("fuyao_api_key", "FUYAO_API_KEY") == "sk-env"


def test_clearing_user_key_falls_back_to_deployment(ctx):
    secrets_store.save_deployment({"fuyao_api_key": DEPLOY_KEY})
    secrets_store.save_user_secret("fuyao_api_key", USER_KEY)
    secrets_store.clear_user_secret("fuyao_api_key")

    assert secrets_store.get_env_backed_secret("fuyao_api_key", "FUYAO_API_KEY") == DEPLOY_KEY


# ── 无账户上下文: 跳过账户层, 绝不因缺用户 key 而失败 ────────────


def test_without_account_context_user_layer_is_skipped(_isolated):
    """import 期/后台线程/worker 子进程走这条 —— 必须拿到部署级值而不是报错或空串。

    这正是「按账户分会让 fuyao 插件 import 时解析失败」那堵墙的绕法: 跳过而非判失败。
    """
    root = user_paths.ensure_user_dirs(1)
    secrets_store.save_user_secret("fuyao_api_key", USER_KEY, user_root=root)
    secrets_store.save_deployment({"fuyao_api_key": DEPLOY_KEY})

    # 当前无账户上下文 —— 用户那份 key 在磁盘上存在, 但读不到也不该报错
    assert secrets_store.get_env_backed_secret("fuyao_api_key", "FUYAO_API_KEY") == DEPLOY_KEY


def test_without_account_context_and_nothing_stored_returns_empty(_isolated):
    assert secrets_store.get_env_backed_secret("fuyao_api_key", "FUYAO_API_KEY") == ""


# ── 客户端不得是进程级单例(串号风险) ────────────────────────────


@pytest.fixture
def fake_clients(monkeypatch):
    """拦截 SDK 构造, 记录每次实例化的 key。"""
    from app.tickflow import client as client_mod

    made: list[str | None] = []
    state = {"key": "sk-a"}

    class FakeAsync:
        def __init__(self, api_key=None, base_url=None):
            made.append(api_key)
            self.api_key = api_key

        @classmethod
        def free(cls):
            made.append(None)
            return cls(api_key=None)

    monkeypatch.setattr(client_mod, "AsyncTickFlow", FakeAsync)
    # 强制走付费分支(否则 key 为空时落到 free, 缓存键恒为 "free", 测不出分桶)
    monkeypatch.setattr(client_mod, "_should_use_free_server", lambda: False)
    monkeypatch.setattr(client_mod, "_base_url", lambda: None)
    monkeypatch.setattr(client_mod.secrets_store, "get_tickflow_key", lambda *a, **k: state["key"])
    client_mod._async_clients.clear()
    return made, state


def test_async_client_is_reused_for_the_same_key(fake_clients):
    """同一把 key 复用同一个实例 —— 每请求新建会把连接池打散。"""
    from app.tickflow.client import get_async_client

    made, _ = fake_clients
    a = get_async_client()
    b = get_async_client()

    assert a is b
    assert made == ["sk-a"]


def test_async_client_is_not_shared_across_different_keys(fake_clients):
    """不同 key 必须拿到各自的实例 —— 共用就是凭据串号。

    注: TickFlow 那把 key 目前仍是**部署级**(用户裁定按账户分只对数据源插件生效),
    所以这里的 key 由 fixture 直接驱动。留着这个用例是因为它钉的是**机制**: 一旦
    任何凭据变成按账户取值, 单例缓存就会静默串号。
    """
    from app.tickflow.client import get_async_client

    made, state = fake_clients
    a1 = get_async_client()
    a2 = get_async_client()
    state["key"] = "sk-b"
    b1 = get_async_client()

    assert a1 is a2
    assert a1 is not b1, "不同 key 绝不能共用实例"
    assert made == ["sk-a", "sk-b"]


def test_fuyao_provider_does_not_reuse_a_client_across_keys(monkeypatch, tmp_path):
    """扶摇 provider 长期存活(loader._PROVIDERS), 按 Key 分桶才不串号。"""
    from app.plugins.fuyao import provider as fp

    built: list[str] = []

    class FakeFuyao:
        def __init__(self, api_key=None, **kwargs):
            built.append(api_key)

        def close(self) -> None: ...

    monkeypatch.setattr(fp.fuyao_client, "FuyaoClient", FakeFuyao)
    monkeypatch.setattr(fp, "get_api_key", lambda: state["key"])

    state = {"key": "sk-a"}
    prov = fp.FuyaoProvider()

    a1 = prov._get_client()
    a2 = prov._get_client()
    state["key"] = "sk-b"
    b1 = prov._get_client()

    assert a1 is a2, "同一把 Key 应复用"
    assert a1 is not b1, "换了 Key 就必须换客户端"
    assert built == ["sk-a", "sk-b"]


# ── 端点: 用户填自己的 key ──────────────────────────────────────


def _register(client: TestClient, email: str) -> None:
    res = client.post("/api/account/register", json={"email": email, "password": "secret123"})
    assert res.status_code == 200, res.text


def test_user_can_save_own_source_key(client, verified, monkeypatch):
    from app.data_providers import custom as custom_sources

    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda name, key: (True, "ok"))
    _register(client, "owner@example.com")

    res = client.put("/api/account/source-keys/fuyao", json={"api_key": USER_KEY})

    assert res.status_code == 200, res.text
    root = user_paths.ensure_user_dirs(1)
    assert secrets_store.get_user_secret("fuyao_api_key", user_root=root) == USER_KEY
    # 站点那把必须纹丝不动
    assert secrets_store.load_deployment().get("fuyao_api_key") is None


def test_probe_failure_does_not_persist(client, verified, monkeypatch):
    from app.data_providers import custom as custom_sources

    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda name, key: (False, "bad key"))
    _register(client, "owner@example.com")

    res = client.put("/api/account/source-keys/fuyao", json={"api_key": "sk-nope"})

    assert res.status_code == 400, res.text
    root = user_paths.ensure_user_dirs(1)
    assert secrets_store.get_user_secret("fuyao_api_key", user_root=root) == ""


def test_user_can_clear_own_source_key(client, verified, monkeypatch):
    from app.data_providers import custom as custom_sources

    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda name, key: (True, "ok"))
    _register(client, "owner@example.com")
    client.put("/api/account/source-keys/fuyao", json={"api_key": USER_KEY})

    res = client.delete("/api/account/source-keys/fuyao")

    assert res.status_code == 200, res.text
    root = user_paths.ensure_user_dirs(1)
    assert secrets_store.get_user_secret("fuyao_api_key", user_root=root) == ""


def test_source_key_endpoints_require_login(client, verified):
    # 先认领面板: 未认领时全局门是 403(防公网抢占设密码), 认领后未登录才是 401
    _register(client, "owner@example.com")
    client.post("/api/account/logout")

    assert client.put("/api/account/source-keys/fuyao", json={"api_key": USER_KEY}).status_code == 401
    assert client.delete("/api/account/source-keys/fuyao").status_code == 401


# ── 插件状态里的「用户自己填过没有」 ─────────────────────────────


def test_plugin_status_reports_user_key_source(ctx):
    """界面靠这两个字段区分「使用中：你自己的 / 站点的」。"""
    from app.data_providers.custom import loader

    secrets_store.save_deployment({"fuyao_api_key": DEPLOY_KEY})
    assert loader._plugin_user_key_masked("fuyao", "FUYAO_API_KEY") == ""
    assert loader._plugin_key_masked("fuyao", "FUYAO_API_KEY") == secrets_store.mask(DEPLOY_KEY)

    secrets_store.save_user_secret("fuyao_api_key", USER_KEY)
    assert loader._plugin_user_key_masked("fuyao", "FUYAO_API_KEY") == secrets_store.mask(USER_KEY)
    # 生效的那把也随之为用户自己的
    assert loader._plugin_key_masked("fuyao", "FUYAO_API_KEY") == secrets_store.mask(USER_KEY)


def test_plugin_status_user_key_is_empty_without_account_context(_isolated):
    from app.data_providers.custom import loader

    root = user_paths.ensure_user_dirs(1)
    secrets_store.save_user_secret("fuyao_api_key", USER_KEY, user_root=root)

    assert loader._plugin_user_key_masked("fuyao", "FUYAO_API_KEY") == ""
