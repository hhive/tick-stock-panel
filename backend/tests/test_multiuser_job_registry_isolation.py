"""回测任务表按账户分家 — 走**真实 HTTP** 的两账户端到端验证 (A/B)。

原缺陷(两个):
  1. `app/api/backtest.py` 的 `_running_jobs` 用**裸 job_key** 当键, 而 job_key 是回测
     入参的 md5 前缀、客户端可原样回传。于是账户 B 用与 A 相同的入参请求就命中 A 的
     任务: B 订阅 A 的进度与净值/成交结果, 而 B 自己那份根本没跑。
  2. `/optimize/cancel`、`/walkforward/cancel` 直接拿客户端给的 `job_key` 查表, 没有
     任何归属校验 —— 任何账户都能 set 别人的 cancel_event。

本套件证明的是**归属**: 键里带账户维度后, "查到"与"属于我"是同一件事, 客户端给的
job_key 只能在自己的命名空间内定位。因此既验证"别人的任务拿不到", 也验证"自己的任务
照旧能取消"(后一条同时是反空转护栏: 键若写错, 它自己会红)。

与 test_multiuser_api_isolation_domain3.py 同构: 真实 app + 认证中间件, 不进 lifespan。
"""
from __future__ import annotations

import importlib
import time

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app.api import account as account_api
from app.api.backtest import (
    _BacktestJob,
    _get_or_create_job,
    _make_job_key,
    _owned_job,
    _running_jobs,
)
from app.services import account_sessions, accounts
from app.services import auth as auth_service
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

PASSWORD = "secret123"
CAPS = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})

# 首个注册者即 admin(id=1), 第二个是普通用户(id=2) —— 见 _resolve_identity/accounts。
ID_A, ID_B = 1, 2

# SSE 回测入参 (A/B 用**完全相同**的一份, 这正是原缺陷的触发条件)
STREAM_PARAMS = {"strategy_id": "ma_cross", "start": "2026-01-05", "end": "2026-02-05"}

# 与 stream 侧 _make_job_key 的逐字段口径一致 (含默认值): A 预置的任务必须用这把键,
# B 的请求才会算出同一把键 —— 键不一致的话本套件的核心断言会失去意义(空转)。
STREAM_JOB_KEY = _make_job_key(
    "ma_cross", None, "2026-01-05", "2026-02-05",
    "open_t+1", None, None,
    0.0002, 5.0, 10, 1.0, 1_000_000.0, "equal",
    None, None,
    "position", 5,
)

# strategy/cancel 侧不读 qs 里的 minute_fill/regime_filter, 只按下面这些字段重算键。
STRATEGY_QS = {
    "strategy_id": "ma_cross",
    "start": "2026-01-05",
    "end": "2026-02-05",
}
STRATEGY_JOB_KEY = _make_job_key(
    STRATEGY_QS["strategy_id"], None, STRATEGY_QS["start"], STRATEGY_QS["end"],
    "open_t+1", None, None,
    0.0002, 5.0, 10, 1.0, 1_000_000.0, "equal",
    None, None,
    "position", 5,
)

# (标签, cancel 路径, 请求体, 预先落在 A 名下的 job_key)
CANCEL_CASES = [
    ("optimize", "/api/backtest/optimize/cancel", {"job_key": "optkey_iso"}, "optkey_iso"),
    ("walkforward", "/api/backtest/walkforward/cancel", {"job_key": "wfkey_iso"}, "wfkey_iso"),
    ("strategy", "/api/backtest/strategy/cancel",
     {"qs": "&".join(f"{k}={v}" for k, v in STRATEGY_QS.items())}, STRATEGY_JOB_KEY),
]
CANCEL_IDS = [c[0] for c in CANCEL_CASES]


@pytest.fixture(autouse=True)
def _clean_auth_cache():
    """清 ``auth.is_configured()`` 的进程级缓存 —— 本文件有一条用例会真的设单密码。

    缓存只在 ``set_password`` 里失效, **没有任何 fixture 会清它**: 设过密码并登录一次
    之后它就停在 True, 于是后面任何"全新面板应对公网回 403"的用例都会拿到 401
    (它读缓存、不去看已被 monkeypatch 到新 tmp 目录的 disk)。两边都清: 进入前不继承
    别人的脏值, 退出后也不把自己的留给别人。
    """
    auth_service._configured_cache = None
    yield
    auth_service._configured_cache = None


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """独立 DATA_DIR + 清账号/会话内存态 + 清任务表(模块级, 会跨用例残留)。"""
    from types import SimpleNamespace

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()

    app_main.app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda symbol: "stock",
    )
    app_main.app.state.capabilities = CAPS
    monkeypatch.setattr("app.api.monitor_rules._sync_engine", lambda request: None)

    saved_jobs = dict(_running_jobs)
    _running_jobs.clear()
    yield tmp_path
    _running_jobs.clear()
    _running_jobs.update(saved_jobs)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_main.app)


@pytest.fixture
def stub_worker(monkeypatch):
    """把回测 worker 换成即时返回的替身。

    本套件验的是**任务归属**, 不是回测数值 —— 真跑一次回测要起子进程读行情, 既慢又
    与断言无关。handler 在请求期才 import worker 符号(不是模块顶层), 所以这里替换
    模块属性即可生效。替身把"这是谁的任务"写进结果, 供 SSE 断言。
    """
    from app.backtest import worker as worker_mod

    def _fake_make_worker_task(kind, data_dir, config):
        return {"kind": kind, "config": config}

    def _fake_run_worker_task(task, on_progress, cancel_event) -> dict:
        return {"ran_with": task["kind"], "params": dict(task["config"].__dict__)}

    monkeypatch.setattr(worker_mod, "make_worker_task", _fake_make_worker_task)
    monkeypatch.setattr(worker_mod, "run_worker_task", _fake_run_worker_task)


def _login(client: TestClient, email: str) -> None:
    """以该邮箱登录 (首次先注册), 会话 cookie 落在 client 上。"""
    client.post("/api/account/logout")
    logged_in = client.post("/api/account/login", json={"email": email, "password": PASSWORD})
    if logged_in.status_code != 200:
        registered = client.post("/api/account/register", json={"email": email, "password": PASSWORD})
        assert registered.status_code == 200, registered.text
        client.post("/api/account/logout")
        logged_in = client.post("/api/account/login", json={"email": email, "password": PASSWORD})
    assert logged_in.status_code == 200, logged_in.text


class _Account:
    """某个面板账户的会话句柄 (每次请求前显式重新登录, 同 domain3 的做法)。"""

    def __init__(self, client: TestClient, email: str) -> None:
        self._client = client
        self._email = email

    def _call(self, method: str, path: str, **kwargs):
        _login(self._client, self._email)
        return getattr(self._client, method)(path, **kwargs)

    def get(self, path: str, **kwargs):
        return self._call("get", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self._call("post", path, **kwargs)


@pytest.fixture
def two_accounts(client):
    """两个面板账户的会话句柄 (A=admin/1, B=user/2)。"""
    a = _Account(client, "account-a@example.com")
    b = _Account(client, "account-b@example.com")
    a.get("/api/account/me")
    b.get("/api/account/me")
    # 断言角色分布: 后面的"跨账户"断言只有在 A/B 确实都是普通登录账号时才成立
    assert accounts.get_role(ID_A) == "admin"
    assert accounts.get_role(ID_B) == "user"
    return a, b


def _seed_job(account_id: int, key: str, *, done: bool, result=None) -> _BacktestJob:
    """把任务预置到某个账户名下, 模拟"该账户已有一个任务"。"""
    job = _BacktestJob(account_id, key)
    if done:
        job.done = True
        job.finish_ts = time.time()   # 未完成 TTL 判定, 避免被 _cleanup_stale_jobs 清掉
        job.result = result
    _running_jobs[(account_id, key)] = job
    return job


# ================================================================
# 缺陷 1a: 相同入参的请求不得跨账户复用任务
# ================================================================
def test_same_params_from_another_account_does_not_hit_my_job(two_accounts, stub_worker, _isolated):
    """B 用与 A **完全相同**的入参请求, 必须跑自己的任务, 而不是订阅 A 的。

    修复前: `_running_jobs.get(job_key)` 命中 A 已完成的任务 → B 立刻收到 A 的
    净值/成交结果, B 自己的任务根本没建、没跑 —— 而 B 界面上看不出任何异常。
    """
    _a, b = two_accounts

    a_job = _seed_job(ID_A, STREAM_JOB_KEY, done=True, result={"owner": "A"})

    resp = b.get("/api/backtest/strategy/stream", params=STREAM_PARAMS)
    assert resp.status_code == 200, resp.text
    body = resp.text

    # B 的流里出现的是 B 自己那次执行的结果(替身把 kind 写进结果)
    assert '"ran_with"' in body, body
    # 且**没有** A 的结果
    assert '"owner": "A"' not in body, f"B 收到了 A 的任务结果:\n{body}"

    # 键一致性的反空转护栏: 入参相同 ⇒ 两侧算出的 job_key 必须相同, 否则本测试是空转
    assert (ID_B, STREAM_JOB_KEY) in _running_jobs, (
        "B 的任务没落在 (账户2, 同一把 job_key) 下 —— 说明键算错了, 上面的断言不成立"
    )
    # A 的任务原封不动
    assert _running_jobs[(ID_A, STREAM_JOB_KEY)] is a_job
    assert a_job.result == {"owner": "A"}
    assert a_job.cancel_event.is_set() is False


def test_job_registry_is_per_account(_isolated):
    """注册表层: 同一把 job_key 在不同账户下是**两个**互不相干的任务。"""
    key = "same-key"
    a_job, a_new = _get_or_create_job(ID_A, key)
    b_job, b_new = _get_or_create_job(ID_B, key)

    assert a_new is True and b_new is True
    assert a_job is not b_job
    assert (a_job.account_id, b_job.account_id) == (ID_A, ID_B)

    # A 再次请求才是"复用"; 且完成的写回只影响自己那一个
    assert _get_or_create_job(ID_A, key) == (a_job, False)
    a_job.done = True
    assert b_job.done is False


# ================================================================
# 缺陷 1b: 取消别人的任务必须被拒, 且别人的任务必须活着
# ================================================================
@pytest.mark.parametrize("label, path, body, job_key", CANCEL_CASES, ids=CANCEL_IDS)
def test_cancel_of_another_accounts_job_is_refused(
    label, path, body, job_key, two_accounts, _isolated,
):
    a, b = two_accounts
    victim = _seed_job(ID_A, job_key, done=False)

    denied = b.post(path, json=body)
    assert denied.status_code == 200, denied.text
    assert denied.json()["ok"] is False, f"{label}: B 竟然取消了 A 的任务"
    assert victim.cancel_event.is_set() is False, f"{label}: A 的任务被 B 取消了"
    assert victim.done is False

    # 反空转护栏: 同一个请求由 A 自己发必须成功 —— 否则上面的 ok=False 可能只是
    # "键本来就不对", 而不是"归属被挡住"。
    allowed = a.post(path, json=body)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["ok"] is True, f"{label}: 任务所有者自己都取消不了, 键算错了"
    assert victim.cancel_event.is_set() is True


def test_unknown_key_still_reports_not_found(two_accounts, _isolated):
    """查不到仍是 ok=False (不抛异常), 与既有契约一致。"""
    _a, b = two_accounts
    r = b.post("/api/backtest/optimize/cancel", json={"job_key": "never-existed"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ================================================================
# 账户身份是任务归属的唯一来源 (fail-closed)
# ================================================================
def test_stream_and_cancel_are_not_public_read():
    """任务表的分层前提: 这几个入口对游客一律不可达 (不在公开只读/白名单里)。"""
    for p in ("/api/backtest/strategy/stream", "/api/backtest/optimize/stream",
              "/api/backtest/walkforward/stream", "/api/backtest/strategy/cancel",
              "/api/backtest/optimize/cancel", "/api/backtest/walkforward/cancel"):
        assert p not in app_main._PUBLIC_READ_EXACT
        assert not any(p.startswith(x) for x in app_main._PUBLIC_READ_PREFIX)
        assert p not in app_main._AUTH_WHITELIST_EXACT


def test_legacy_password_session_has_no_account_and_is_refused(client, _isolated):
    """单密码应急会话没有账号 id, 因此**不能**跑/取消回测 (403, fail-closed)。

    这是刻意的取舍: 回测结果落在账户根下, 属每账户数据; 若无账户身份就放行, 就必须
    为它编一个"假账户"命名空间 —— deps.require_account_id 的 docstring 明确反对这一点
    (那正是"把某个账户的数据发给所有人"的温床)。同一道闸门已覆盖模拟盘/批次/告警/
    选股监控等所有每账户入口, 这里只是让回测与它们一致。
    """
    auth_service.set_password("legacy-pass-123")
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"password": "legacy-pass-123"}).status_code == 200

    stream = client.get("/api/backtest/strategy/stream", params=STREAM_PARAMS)
    assert stream.status_code == 403, stream.text
    assert "账号身份" in stream.json()["detail"]

    cancel = client.post("/api/backtest/optimize/cancel", json={"job_key": "whatever"})
    assert cancel.status_code == 403, cancel.text


def test_owned_job_rejects_a_foreign_entry(_isolated):
    """纵深防御: 任务表被塞进"键与归属不一致"的条目时必须拒绝, 而不是放行。

    键里已带账户维度, 所以这种条目在当前实现下不可能出现 —— 这条断言守的是**将来**:
    若有人把键改回不带账户维度的形态, 归属校验仍会挡住取消, 而不是静默放行。
    """
    key = "mismatched"
    foreign = _BacktestJob(ID_B, key)
    # 人为把 B 的任务放到 A 的命名空间下 (模拟"键被改回裸 job_key")
    _running_jobs[(ID_A, key)] = foreign

    assert _owned_job(ID_A, key) is None
    assert _owned_job(ID_B, key) is None  # B 的命名空间里没有它
