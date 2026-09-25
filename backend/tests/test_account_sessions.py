"""多用户账号会话表 (app.services.account_sessions) 的存储语义测试。

覆盖: 创建/查询往返、未知 token、过期(返回 None 且落盘清理)、注销、按账号批量注销、
进程重启后的恢复、文件权限 0600、损坏文件降级, 以及**与单密码应急入口 auth.py 互不
干扰**这条硬约束。
"""
from __future__ import annotations

import importlib
import json
import time

import pytest

from app import config as app_config
from app.services import account_sessions


class _FakeTime:
    """只替换 account_sessions 模块内的 `time` 名字, 不碰全局 stdlib time 模块。"""

    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """每个用例一个独立 DATA_DIR, 并重载模块拿到干净的内存会话表。

    重载是必要的: `_sessions` 是模块级字典, 不重载会跨用例串味。重载顺带覆盖了
    「模块加载时 restore_sessions()」这条路径。
    """
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    importlib.reload(account_sessions)
    yield tmp_path


def _sessions_file(tmp_path):
    return tmp_path / "accounts" / "sessions.json"


# ================================================================
# 创建 / 查询
# ================================================================

def test_create_then_get_roundtrip():
    token = account_sessions.create_session(7)
    assert isinstance(token, str) and token
    assert account_sessions.get_session(token) == 7


def test_tokens_are_unique_per_session():
    t1 = account_sessions.create_session(1)
    t2 = account_sessions.create_session(1)
    assert t1 != t2
    assert account_sessions.get_session(t1) == 1
    assert account_sessions.get_session(t2) == 1


def test_unknown_token_returns_none():
    account_sessions.create_session(1)
    assert account_sessions.get_session("not-a-real-token") is None


@pytest.mark.parametrize("token", ["", None])
def test_blank_token_returns_none(token):
    assert account_sessions.get_session(token) is None


def test_persisted_entry_shape(tmp_path):
    """其他任务依赖这个落盘结构, 锁住字段名与类型。"""
    token = account_sessions.create_session(9)
    raw = json.loads(_sessions_file(tmp_path).read_text(encoding="utf-8"))

    entry = raw[token]
    assert entry["account_id"] == 9
    assert entry["expires_at"] == pytest.approx(
        time.time() + account_sessions.SESSION_TTL, abs=60,
    )


# ================================================================
# 过期
# ================================================================

def test_expired_token_returns_none_and_is_pruned_from_disk(tmp_path, monkeypatch):
    """过期注入方式: 用假时钟替换模块内的 `time` 引用, 再把表针拨过 TTL。

    比「直接改时间戳写文件」更贴近真实场景 —— 会话是在内存里放着放着过期的, 走的
    正是 get_session 的惰性淘汰分支; 也不必去动全局 stdlib time 模块。
    """
    clock = _FakeTime(time.time())
    monkeypatch.setattr(account_sessions, "time", clock)

    token = account_sessions.create_session(7)
    assert account_sessions.get_session(token) == 7

    clock.now += account_sessions.SESSION_TTL + 1  # 拨过有效期

    assert account_sessions.get_session(token) is None
    # 过期条目必须已经从磁盘上清掉, 而不只是这次返回 None
    raw = json.loads(_sessions_file(tmp_path).read_text(encoding="utf-8"))
    assert token not in raw


def test_expired_token_does_not_affect_live_one(tmp_path, monkeypatch):
    clock = _FakeTime(time.time())
    monkeypatch.setattr(account_sessions, "time", clock)

    dead = account_sessions.create_session(1)
    clock.now += account_sessions.SESSION_TTL + 1
    live = account_sessions.create_session(2)

    assert account_sessions.get_session(dead) is None
    assert account_sessions.get_session(live) == 2


def test_restore_prunes_expired_entries_from_disk(tmp_path):
    """停机期间到期的会话不该在重启后复活。

    过期注入方式: 直接把带过去 expires_at 的记录写进文件, 再重载模块走恢复路径。
    """
    p = _sessions_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "dead-token": {"account_id": 1, "expires_at": time.time() - 10},
        "live-token": {"account_id": 2, "expires_at": time.time() + 3600},
    }), encoding="utf-8")

    importlib.reload(account_sessions)

    assert account_sessions.get_session("dead-token") is None
    assert account_sessions.get_session("live-token") == 2
    assert "dead-token" not in json.loads(p.read_text(encoding="utf-8"))


# ================================================================
# 注销
# ================================================================

def test_revoke_makes_session_invalid():
    token = account_sessions.create_session(5)
    account_sessions.revoke(token)
    assert account_sessions.get_session(token) is None


def test_revoke_only_touches_that_token():
    keep = account_sessions.create_session(5)
    drop = account_sessions.create_session(5)
    account_sessions.revoke(drop)
    assert account_sessions.get_session(keep) == 5


def test_revoke_unknown_token_is_noop():
    account_sessions.revoke("never-existed")  # 不抛异常


def test_revoke_all_for_account_revokes_only_that_account():
    a = account_sessions.create_session(1)
    b = account_sessions.create_session(1)
    other = account_sessions.create_session(2)

    assert account_sessions.revoke_all_for_account(1) == 2

    assert account_sessions.get_session(a) is None
    assert account_sessions.get_session(b) is None
    assert account_sessions.get_session(other) == 2


def test_revoke_all_for_account_returns_zero_when_none():
    keep = account_sessions.create_session(1)
    assert account_sessions.revoke_all_for_account(99) == 0
    assert account_sessions.get_session(keep) == 1  # 未命中账号的会话不受影响


def test_revoke_all_survives_reload(tmp_path):
    """批量注销必须落盘, 否则重启后被踢的会话会回来。"""
    token = account_sessions.create_session(3)
    account_sessions.revoke_all_for_account(3)

    importlib.reload(account_sessions)
    assert account_sessions.get_session(token) is None


# ================================================================
# 重启持久化
# ================================================================

def test_session_survives_module_reload():
    """重载模块 = 模拟进程重启: 内存表重建后会话仍有效。"""
    token = account_sessions.create_session(42)

    importlib.reload(account_sessions)

    assert account_sessions.get_session(token) == 42


def test_revoke_survives_module_reload():
    token = account_sessions.create_session(42)
    account_sessions.revoke(token)

    importlib.reload(account_sessions)

    assert account_sessions.get_session(token) is None


# ================================================================
# 文件权限 / 损坏降级
# ================================================================

def test_sessions_file_is_mode_0600(tmp_path):
    account_sessions.create_session(1)
    mode = _sessions_file(tmp_path).stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.parametrize("content", ["{ not json", "[]", "null", '"a string"'])
def test_malformed_sessions_file_degrades_to_empty(tmp_path, content):
    """损坏的 JSON 应降级为「空会话表」, 不得让服务启动失败。"""
    p = _sessions_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")

    importlib.reload(account_sessions)

    assert account_sessions.get_session("anything") is None
    # 之后仍能正常建会话(不因旧文件损坏而失败)
    token = account_sessions.create_session(3)
    assert account_sessions.get_session(token) == 3


def test_malformed_entries_are_dropped_on_restore(tmp_path):
    """单条记录坏掉只丢这条, 不牵连同一文件里的其它会话。"""
    p = _sessions_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "garbage": "not-an-object",
        "missing-expiry": {"account_id": 1},
        "bad-type": {"account_id": "x", "expires_at": "y"},
        "ok": {"account_id": 8, "expires_at": time.time() + 3600},
    }), encoding="utf-8")

    importlib.reload(account_sessions)

    assert account_sessions.get_session("garbage") is None
    assert account_sessions.get_session("missing-expiry") is None
    assert account_sessions.get_session("bad-type") is None
    assert account_sessions.get_session("ok") == 8


# ================================================================
# 与单密码应急入口互不干扰(硬约束)
# ================================================================

def test_independent_of_legacy_auth_sessions(tmp_path):
    """多用户会话与 auth.py 的应急会话各写各的文件、各存各的 token。"""
    from app.services import auth

    legacy_before = dict(auth._sessions)

    token = account_sessions.create_session(1)

    # 不塞进 auth 的内存表, 也不在它那边落盘
    assert token not in auth._sessions
    assert auth._sessions == legacy_before
    assert account_sessions._path() != auth._path()
    assert not (tmp_path / "user_data" / "auth.json").exists()


def test_only_writes_under_accounts_dir(tmp_path):
    """本模块只在 data/accounts/ 下工作, 不碰 auth.py 的 data/user_data/。"""
    token = account_sessions.create_session(1)
    assert account_sessions.get_session(token) == 1
    assert _sessions_file(tmp_path).exists()
    assert not (tmp_path / "user_data").exists()
