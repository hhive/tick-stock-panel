"""管道任务归属 — 走**真实 HTTP** (真实认证中间件) 的两账户验证。

判定与理由 (见 app/services/pipeline_jobs.may_cancel 的 docstring):
数据管道是**部署级**资源 —— 一份共享行情、一个执行槽。因此任何已登录账户都能
触发全站同步, 且单飞复用会返回**别人**发起的那条任务 (各账户看到的是同一次全站
同步的进度, 前端也把它当"全站数据同步中"的指示灯)。跨账户要防的不是"看得见",
而是**谁有权停掉全站同步**: 该端点的效果超出单个账户, 与 /api/data/clear 同一判据,
所以只有发起者本人或管理员可以取消。

本套件证明的是**接缝**: 认证中间件 → request.state.account_id → may_cancel 这条路
真的接通了。只测 store 层的话, "中间件没接上"会照样全绿 —— 而那种情况下用户的
任务将变成谁都停不了 (或反过来)。

与 test_multiuser_job_registry_isolation.py 同构: 真实 app + 认证中间件, 不进 lifespan。
"""
from __future__ import annotations

import importlib
import threading

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app.api import account as account_api
from app.services import account_sessions, accounts, pipeline_jobs
from app.services.pipeline_jobs import JobStore

PASSWORD = "secret123"
# 三个账户: 首个注册者是 admin, 所以"两个普通账户互不可停"要用第 2、3 个来验。
ADMIN, OWNER, STRANGER = 1, 2, 3


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()

    app_main.app.state.repo = _StubRepo()
    app_main.app.state.capabilities = object()

    # 模块级 job_store 在 import 时就把 store_dir 绑到了真实 data_dir —— 测试里必须
    # 换成独立目录, 否则用例会往仓库 data/job_store 里写记录 (跨用例残留)。
    store = JobStore(store_dir=tmp_path / "job_store")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr("app.api.pipeline.job_store", store)
    yield tmp_path


class _StubRepo:
    """管道替身依赖: 本套件验归属, 不验管道计算。"""

    @property
    def store(self):
        from types import SimpleNamespace

        return SimpleNamespace(data_dir=app_config.settings.data_dir)

    @staticmethod
    def refresh_cache() -> None:
        return None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_main.app)


@pytest.fixture
def pipeline_worker(monkeypatch):
    """把管道主体换成"阻塞到测试放行"的替身。

    任务必须**保持 running** 才能验证取消权限: 若它已经跑完, 取消会走到 400
    (终态不可取消), 那 403 与 400 就分不清了。
    """
    entered = threading.Event()
    release = threading.Event()

    def run_now(repo, capset, on_progress=None):
        if on_progress is not None:
            on_progress("stub", 10, "替身管道运行中")
        entered.set()
        assert release.wait(5)
        return {"rows": 0}

    monkeypatch.setattr("app.api.pipeline.daily_pipeline.run_now", run_now)
    return entered, release


def _login(client: TestClient, email: str) -> None:
    """以该邮箱登录 (首次先注册), 会话 cookie 落在 client 上。"""
    client.post("/api/account/logout")
    logged_in = client.post("/api/account/login", json={"email": email, "password": PASSWORD})
    if logged_in.status_code != 200:
        registered = client.post(
            "/api/account/register", json={"email": email, "password": PASSWORD}
        )
        assert registered.status_code == 200, registered.text
        client.post("/api/account/logout")
        logged_in = client.post(
            "/api/account/login", json={"email": email, "password": PASSWORD}
        )
    assert logged_in.status_code == 200, logged_in.text


class _Account:
    """某个面板账户的会话句柄 (TestClient 只有一个 cookie, 每次请求前显式登录)。"""

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
def three_accounts(client):
    admin = _Account(client, "admin@example.com")
    owner = _Account(client, "owner@example.com")
    stranger = _Account(client, "stranger@example.com")
    for handle in (admin, owner, stranger):
        assert handle.get("/api/account/me").status_code == 200
    # 断言角色分布: "两个普通账户互不可停"只在 OWNER/STRANGER 都不是管理员时成立
    assert accounts.get_role(ADMIN) == "admin"
    assert accounts.get_role(OWNER) == "user"
    assert accounts.get_role(STRANGER) == "user"
    return admin, owner, stranger


def test_only_the_triggering_account_can_cancel_the_shared_pipeline_job(
    three_accounts,
    pipeline_worker,
):
    admin, owner, stranger = three_accounts
    entered, release = pipeline_worker
    try:
        started = owner.post("/api/pipeline/run")
        assert started.status_code == 200, started.text
        job_id = started.json()["job_id"]
        assert entered.wait(3)

        # 归属确实按会话落到了发起者名下
        job = pipeline_jobs.job_store.get(job_id)
        assert job["owner_account_id"] == OWNER

        # 另一普通账户: 403, 且任务必须原封不动 (进程内取消标志也不得被置位)
        refused = stranger.post(f"/api/pipeline/jobs/{job_id}/cancel")
        assert refused.status_code == 403, refused.text
        assert pipeline_jobs.job_store.get(job_id)["status"] == "running"
        assert not pipeline_jobs.is_cancelled(job_id)

        # 别人的任务也**不能**通过轮询端点拿到"可取消"的许可
        listed = stranger.get("/api/pipeline/jobs").json()["jobs"]
        assert all(item["cancel_allowed"] is False for item in listed)
        assert owner.get("/api/pipeline/jobs").json()["jobs"][0]["cancel_allowed"] is True
        # 账户主键不外发
        assert all("owner_account_id" not in item for item in listed)

        # 管理员可以停
        assert admin.post(f"/api/pipeline/jobs/{job_id}/cancel").status_code == 200
        assert pipeline_jobs.job_store.get(job_id)["status"] == "failed"
    finally:
        release.set()


def test_scheduled_job_without_owner_is_admin_only(three_accounts, monkeypatch, tmp_path):
    """调度器/系统发起的任务 (owner=None) 代表全站, 普通账户一律不能停。"""
    admin, owner, _stranger = three_accounts
    store = pipeline_jobs.job_store
    jid, _ = store.create(owner_account_id=None)
    store.start(jid)

    assert owner.post(f"/api/pipeline/jobs/{jid}/cancel").status_code == 403
    assert store.get(jid)["status"] == "running"
    assert admin.post(f"/api/pipeline/jobs/{jid}/cancel").status_code == 200
    assert store.get(jid)["status"] == "failed"


def test_listing_a_foreign_job_does_not_teach_who_started_it(three_accounts, pipeline_worker):
    """可见性是共享的 (管道是部署级资源), 但**归属不外发**。

    这条同时是"不要顺手把 job_store 改成每账户"的反向说明: 列表必须能在没有
    账户上下文的情况下被读 (公开只读端点 /api/data/status 就是这么读它的),
    所以跨账户要防的是权限而不是可见性。
    """
    _admin, owner, stranger = three_accounts
    entered, release = pipeline_worker
    try:
        job_id = owner.post("/api/pipeline/run").json()["job_id"]
        assert entered.wait(3)

        seen_by_stranger = stranger.get("/api/pipeline/jobs").json()["jobs"]
        assert [item["id"] for item in seen_by_stranger] == [job_id]
        assert "owner_account_id" not in seen_by_stranger[0]

        detail = stranger.get(f"/api/pipeline/jobs/{job_id}").json()
        assert "owner_account_id" not in detail
        assert detail["cancel_allowed"] is False
    finally:
        release.set()
