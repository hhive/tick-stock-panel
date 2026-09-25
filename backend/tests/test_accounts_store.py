"""多用户账号注册表 (app.services.accounts) 的存储语义测试。

覆盖: 首个注册者为管理员、邮箱大小写不敏感唯一、密码往返、id 单调不复用、
apikey 绑定的「首个绑定者占有」、以及明文 apikey 永不落盘。
"""
from __future__ import annotations

import json

import pytest

from app import config as app_config
from app.services import accounts


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """每个用例一个独立 DATA_DIR, 账号文件随之隔离。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    yield tmp_path


# ================================================================
# 创建 / 角色
# ================================================================

def test_first_account_becomes_admin():
    acc = accounts.create_account("owner@example.com", "secret123")
    assert acc.id == 1
    assert acc.role == "admin"


def test_second_account_is_regular_user():
    accounts.create_account("owner@example.com", "secret123")
    acc = accounts.create_account("user@example.com", "secret123")
    assert acc.id == 2
    assert acc.role == "user"


def test_email_uniqueness_is_case_insensitive():
    accounts.create_account("Owner@Example.COM", "secret123")
    with pytest.raises(accounts.EmailTakenError):
        accounts.create_account("owner@example.com", "secret123")


def test_id_is_monotonic_and_not_reused():
    a1 = accounts.create_account("a@example.com", "secret123")
    a2 = accounts.create_account("b@example.com", "secret123")
    a3 = accounts.create_account("c@example.com", "secret123")
    assert [a1.id, a2.id, a3.id] == [1, 2, 3]
    assert accounts.count() == 3


def test_email_is_normalized_on_store():
    acc = accounts.create_account("  MiXeD@Example.Com  ", "secret123")
    assert acc.email == "mixed@example.com"
    assert accounts.get_by_email("MIXED@EXAMPLE.COM") is not None


# ================================================================
# 入参校验
# ================================================================

@pytest.mark.parametrize("email", ["", "   ", "noatsign", "@nodomain"])
def test_invalid_email_rejected(email):
    with pytest.raises(ValueError):
        accounts.create_account(email, "secret123")


@pytest.mark.parametrize("password", ["", "12345"])
def test_short_password_rejected(password):
    """下限与 app.services.auth.set_password 保持一致 (6 位)。"""
    with pytest.raises(ValueError):
        accounts.create_account("a@example.com", password)


# ================================================================
# 凭据校验
# ================================================================

def test_verify_credentials_roundtrip():
    accounts.create_account("a@example.com", "secret123")
    assert accounts.verify_credentials("a@example.com", "secret123") is not None
    assert accounts.verify_credentials("A@Example.com", "secret123") is not None


def test_verify_credentials_rejects_wrong_password():
    accounts.create_account("a@example.com", "secret123")
    assert accounts.verify_credentials("a@example.com", "wrong-password") is None


def test_verify_credentials_rejects_unknown_email():
    assert accounts.verify_credentials("nobody@example.com", "secret123") is None


def test_password_is_not_stored_in_plaintext(_isolated_data_dir):
    accounts.create_account("a@example.com", "secret123")
    raw = (_isolated_data_dir / "accounts" / "accounts.json").read_text(encoding="utf-8")
    assert "secret123" not in raw


# ================================================================
# apikey 绑定: 首个绑定者占有
# ================================================================

def test_bind_and_find_by_key_hash():
    acc = accounts.create_account("a@example.com", "secret123")
    kh = accounts.hash_api_key("sk-abc123")
    accounts.bind_api_key(acc.id, kh)
    found = accounts.find_by_key_hash(kh)
    assert found is not None and found.id == acc.id


def test_bind_is_idempotent_for_same_account():
    acc = accounts.create_account("a@example.com", "secret123")
    kh = accounts.hash_api_key("sk-abc123")
    accounts.bind_api_key(acc.id, kh)
    accounts.bind_api_key(acc.id, kh)
    assert accounts.get_by_id(acc.id).api_key_bindings.count(kh) == 1


def test_bind_conflicts_when_key_owned_by_another_account():
    """核心不变量: key 一旦属于某账号, 不允许改绑他人(否则等于可夺号)。"""
    first = accounts.create_account("first@example.com", "secret123")
    second = accounts.create_account("second@example.com", "secret123")
    kh = accounts.hash_api_key("sk-abc123")
    accounts.bind_api_key(first.id, kh)

    with pytest.raises(accounts.BindingConflictError):
        accounts.bind_api_key(second.id, kh)

    # 冲突后归属不变
    assert accounts.find_by_key_hash(kh).id == first.id
    assert accounts.get_by_id(second.id).api_key_bindings == []


def test_unbind_removes_binding():
    acc = accounts.create_account("a@example.com", "secret123")
    kh = accounts.hash_api_key("sk-abc123")
    accounts.bind_api_key(acc.id, kh)
    accounts.unbind_api_key(acc.id, kh)
    assert accounts.find_by_key_hash(kh) is None


def test_unbind_is_idempotent():
    acc = accounts.create_account("a@example.com", "secret123")
    accounts.unbind_api_key(acc.id, accounts.hash_api_key("sk-never-bound"))
    assert accounts.get_by_id(acc.id).api_key_bindings == []


def test_unbind_frees_key_for_another_account():
    """解绑后该 key 可被他人绑定 —— 这是用户换 key 的正规路径。"""
    first = accounts.create_account("first@example.com", "secret123")
    second = accounts.create_account("second@example.com", "secret123")
    kh = accounts.hash_api_key("sk-abc123")
    accounts.bind_api_key(first.id, kh)
    accounts.unbind_api_key(first.id, kh)
    accounts.bind_api_key(second.id, kh)
    assert accounts.find_by_key_hash(kh).id == second.id


def test_bind_unknown_account_raises():
    with pytest.raises(accounts.AccountNotFoundError):
        accounts.bind_api_key(999, accounts.hash_api_key("sk-x"))


def test_bind_empty_key_hash_rejected():
    acc = accounts.create_account("a@example.com", "secret123")
    with pytest.raises(ValueError):
        accounts.bind_api_key(acc.id, "   ")


# ================================================================
# 明文不入库
# ================================================================

def test_api_key_hash_is_stable_and_hides_plaintext(_isolated_data_dir):
    kh1 = accounts.hash_api_key("sk-secret-value")
    kh2 = accounts.hash_api_key("sk-secret-value")
    assert kh1 == kh2
    assert "sk-secret-value" not in kh1

    acc = accounts.create_account("a@example.com", "secret123")
    accounts.bind_api_key(acc.id, kh1)
    raw = (_isolated_data_dir / "accounts" / "accounts.json").read_text(encoding="utf-8")
    assert "sk-secret-value" not in raw


def test_accounts_file_is_mode_0600(_isolated_data_dir):
    accounts.create_account("a@example.com", "secret123")
    mode = (_isolated_data_dir / "accounts" / "accounts.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_malformed_accounts_file_does_not_crash(_isolated_data_dir):
    """损坏的 JSON 应降级为"空注册表", 不得让服务启动失败。"""
    p = _isolated_data_dir / "accounts" / "accounts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")

    assert accounts.count() == 0
    acc = accounts.create_account("a@example.com", "secret123")
    assert acc.role == "admin"  # 视为首个账号
    assert json.loads(p.read_text(encoding="utf-8"))["accounts"][0]["email"] == "a@example.com"
