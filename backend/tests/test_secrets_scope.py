"""凭据作用域划分测试：部署级 vs 每用户。

背景（本套件钉的就是这个缺陷）：``secrets_store`` 一度被整体划为每用户，于是
``get_tickflow_key()`` 在**没有账户上下文**的后台线程/子进程里抛
``MissingUserContextError`` —— 而行情是共享的、取数路径遍布那些地方，等于
**全部后台行情取数失败**。根因是把"一个文件里的密钥"当成了同一作用域，而
``secrets.json`` 里其实混着两类：

  - **部署级**：TickFlow Key / 端点、数据源插件 Key —— 喂共享行情，全站一份；
  - **每用户**：Sub2API Key、AI Key、SMTP 密码、webhook secret —— 个人凭据。
"""
from __future__ import annotations

import json

import pytest

from app import config as app_config
from app import secrets_store
from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    monkeypatch.delenv("MYPLUGIN_API_KEY", raising=False)
    token = preferences.set_current_user_root(None)
    yield tmp_path
    preferences.reset_current_user_root(token)


@pytest.fixture
def user_root(tmp_path):
    root = tmp_path / "users" / "1"
    (root / "user_data").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def as_user(user_root):
    """以账户 1 的身份执行（模拟请求路径）。"""
    token = preferences.set_current_user_root(user_root)
    yield user_root
    preferences.reset_current_user_root(token)


# ================================================================
# 部署级：无需账户上下文
# ================================================================

def test_tickflow_key_is_readable_without_any_context(_isolated):
    """核心回归：后台线程（无账户上下文）必须能拿到 TickFlow Key。

    这是修这个缺陷的意义所在 —— 行情取数遍布后台线程与子进程。
    """
    secrets_store.save_deployment({"tickflow_api_key": "sk-tf-123"})
    assert preferences.current_user_root() is None
    assert secrets_store.get_tickflow_key() == "sk-tf-123"


def test_tickflow_key_routed_to_deployment_file(_isolated):
    """走 save() 也应按 DEPLOYMENT_KEYS 分派到部署级文件，且不要求上下文。"""
    secrets_store.save({"tickflow_api_key": "sk-tf-abc"})
    dep = _isolated / "deployment_secrets.json"
    assert dep.is_file()
    assert json.loads(dep.read_text(encoding="utf-8"))["tickflow_api_key"] == "sk-tf-abc"
    # 不得落到任何账户目录
    assert not (_isolated / "users").exists()


def test_tickflow_base_url_is_deployment_level(_isolated):
    secrets_store.save({"tickflow_base_url": "https://paid.example.com"})
    assert secrets_store.get_deployment("tickflow_base_url") == "https://paid.example.com"


def test_deployment_file_is_0600(_isolated):
    secrets_store.save_deployment({"tickflow_api_key": "sk-tf-123"})
    mode = (_isolated / "deployment_secrets.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_clear_deployment_removes_only_deployment_keys(_isolated, as_user):
    secrets_store.save({"tickflow_api_key": "sk-tf-123", "ai_api_key": "sk-ai-456"})
    secrets_store.clear_deployment("tickflow_api_key")
    assert secrets_store.get_tickflow_key() == ""
    # 每用户的那把不受影响
    assert secrets_store.load()["ai_api_key"] == "sk-ai-456"


# ================================================================
# 插件 Key 是部署级
# ================================================================

def test_plugin_key_readable_without_context(_isolated):
    """插件 Key 在 import 期就会被读（loader 的插件发现），那时没有账户上下文。

    按账户分会让插件直接被判为不可用 —— 已实测过 fuyao 插件清单解析失败。
    """
    secrets_store.save_deployment({"myplugin_api_key": "pk-123"})
    assert secrets_store.get_env_backed_secret("myplugin_api_key", "MYPLUGIN_API_KEY") == "pk-123"


def test_plugin_key_falls_back_to_env(_isolated, monkeypatch):
    monkeypatch.setenv("MYPLUGIN_API_KEY", "pk-from-env")
    assert secrets_store.get_env_backed_secret("myplugin_api_key", "MYPLUGIN_API_KEY") == "pk-from-env"


def test_plugin_key_write_and_read_use_the_same_scope(_isolated):
    """插件 Key 的**写入与读取必须同作用域** —— 否则保存了永远读不到。

    这是拆分凭据作用域时引入的真实缺陷: 写入侧(界面保存插件 Key)用 save(),
    而 ``{plugin}_api_key`` 不在 DEPLOYMENT_KEYS 里 ⇒ 落进**调用方的每用户文件**;
    读取侧 ``get_env_backed_secret`` 读的是**部署级文件** ⇒ 界面里保存的 Key
    永远不会被读到, 且**不报错**。

    这里钉住两端: 写进部署级文件, 且能被读取侧读到。
    """
    secrets_store.save_deployment({"myplugin_api_key": "pk-123"})
    dep = _isolated / "deployment_secrets.json"
    assert dep.is_file()
    assert "myplugin_api_key" in json.loads(dep.read_text(encoding="utf-8"))
    assert secrets_store.get_env_backed_secret("myplugin_api_key", "MYPLUGIN_API_KEY") == "pk-123"


def test_plugin_key_clear_targets_the_deployment_file(_isolated, as_user):
    """清除也必须作用于部署级文件 —— 清错文件等于没清, 且旧 Key 继续生效。"""
    secrets_store.save_deployment({"myplugin_api_key": "pk-123"})
    secrets_store.clear_deployment("myplugin_api_key")
    assert "myplugin_api_key" not in secrets_store.load_deployment()
    assert secrets_store.get_env_backed_secret("myplugin_api_key", "MYPLUGIN_API_KEY") == ""


def test_deployment_file_wins_over_env(_isolated, monkeypatch):
    monkeypatch.setenv("MYPLUGIN_API_KEY", "pk-from-env")
    secrets_store.save_deployment({"myplugin_api_key": "pk-from-file"})
    assert secrets_store.get_env_backed_secret("myplugin_api_key", "MYPLUGIN_API_KEY") == "pk-from-file"


# ================================================================
# 每用户：仍然按账户隔离
# ================================================================

def test_per_user_secret_lands_in_account_file(as_user, user_root):
    secrets_store.save({"ai_api_key": "sk-ai-1"})
    assert (user_root / "user_data" / "secrets.json").is_file()
    assert secrets_store.load()["ai_api_key"] == "sk-ai-1"


def test_per_user_secrets_are_isolated_between_accounts(tmp_path):
    a = tmp_path / "users" / "1"
    b = tmp_path / "users" / "2"
    secrets_store.save({"ai_api_key": "key-of-A"}, user_root=a)
    secrets_store.save({"ai_api_key": "key-of-B"}, user_root=b)
    assert secrets_store.load(a)["ai_api_key"] == "key-of-A"
    assert secrets_store.load(b)["ai_api_key"] == "key-of-B"
    assert "key-of-A" not in json.dumps(secrets_store.load(b))


def test_per_user_save_without_context_fails_closed(_isolated):
    """写每用户凭据却没有账户上下文 → 抛错，不得静默写进某个共享文件。"""
    from app.services.user_paths import MissingUserContextError

    with pytest.raises(MissingUserContextError):
        secrets_store.save({"ai_api_key": "sk-ai-1"})


def test_mixed_save_splits_both_scopes(as_user, user_root, _isolated):
    secrets_store.save({"tickflow_api_key": "sk-tf", "ai_api_key": "sk-ai"})
    assert secrets_store.get_tickflow_key() == "sk-tf"
    assert secrets_store.load()["ai_api_key"] == "sk-ai"
    assert "sk-ai" not in (_isolated / "deployment_secrets.json").read_text(encoding="utf-8")


def test_deployment_keys_are_not_returned_by_load(as_user):
    """load() 只返回每用户凭据 —— 避免调用方以为拿到了部署级 Key。"""
    secrets_store.save_deployment({"tickflow_api_key": "sk-tf"})
    assert "tickflow_api_key" not in secrets_store.load()


def test_malformed_deployment_file_degrades_to_empty(_isolated):
    (_isolated / "deployment_secrets.json").write_text("{ not json", encoding="utf-8")
    assert secrets_store.load_deployment() == {}
    assert secrets_store.get_tickflow_key() == ""
