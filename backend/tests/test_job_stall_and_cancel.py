"""回归测试: 卡死判定从「总时长一刀切」改为「进度停滞」+ 协作式取消 + 执行槽所有权。

背景(用户反馈): 慢带宽环境冷启动全市场拉取超过 20 分钟被误标失败,
拉取线程(僵尸)仍在写盘, UI 状态与实际不对齐; 重复点击还可能撞执行锁。
均为纯逻辑, 不触网。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.services import pipeline_jobs, preferences
from app.services.pipeline_jobs import JobCancelledError, JobStore


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _reset_module_globals():
    """取消标志与执行槽是模块级单例, 每个用例前后复位, 避免相互污染。"""
    pipeline_jobs._CANCEL_FLAGS.clear()
    pipeline_jobs._run_slot_owner = None
    yield
    pipeline_jobs._CANCEL_FLAGS.clear()
    pipeline_jobs._run_slot_owner = None


def _make_running_job(
    store: JobStore,
    timeout_s: int,
    *,
    owner_account_id: int | None = None,
) -> str:
    jid, _ = store.create(owner_account_id=owner_account_id, timeout_s=timeout_s)
    store.start(jid)
    return jid


# ── 进度停滞判定 ────────────────────────────────────────────────────────

def test_stalled_job_is_reaped(monkeypatch, tmp_path):
    """无进度上报超过阈值 → 标记失败 + 置取消标志 + 释放执行槽。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    # 启动后 5 分钟无任何进度 → 停滞 300s > 60s
    stale = _iso(_now() - timedelta(minutes=5))
    store._active_jobs[jid]["started_at"] = stale
    store._active_jobs[jid]["last_progress_at"] = stale

    assert pipeline_jobs.try_acquire_run_slot(jid) is True
    store.reap_stale()

    j = store.get(jid)
    assert j["status"] == "failed"
    assert "进度停滞" in j["error"]
    # 协作式取消: 僵尸线程通过 flag 感知(记录已被 fail 弹出, flag 仍在)
    assert pipeline_jobs.is_cancelled(jid)
    # 执行槽已按所有权释放
    assert pipeline_jobs.try_acquire_run_slot("next") is True


def test_progressing_job_is_not_reaped(monkeypatch, tmp_path):
    """慢但在推进: 总时长远超阈值, 但进度心跳新鲜 → 不得误杀(核心回归)。"""
    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    # 总时长 2 小时(远超 60s 阈值), 但 10 秒前刚上报过进度
    store._active_jobs[jid]["started_at"] = _iso(_now() - timedelta(hours=2))
    store._active_jobs[jid]["last_progress_at"] = _iso(_now() - timedelta(seconds=10))

    store.reap_stale()
    assert store.get(jid)["status"] == "running"


def test_hard_cap_terminates_endless_progress(tmp_path):
    """进度回调持续但总时长超硬上限 → 兜底终止。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    beyond = timedelta(seconds=pipeline_jobs.HARD_JOB_TIMEOUT_S + 3600)
    store._active_jobs[jid]["started_at"] = _iso(_now() - beyond)
    store._active_jobs[jid]["last_progress_at"] = _iso(_now())

    store.reap_stale()
    j = store.get(jid)
    assert j["status"] == "failed"
    assert "硬上限" in j["error"]


def test_progress_updates_heartbeat(tmp_path):
    """progress() 刷新 last_progress_at(停滞计时的基准)。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    store.progress(jid, "sync", 10, "chunk 1/10")
    assert store.get(jid)["last_progress_at"] is not None


# ── 协作式取消 ──────────────────────────────────────────────────────────

def test_progress_raises_after_cancel(tmp_path):
    """取消后, 僵尸线程下一次 progress() 回调抛 JobCancelledError 自行退出。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)

    pipeline_jobs.request_cancel(jid)
    with pytest.raises(JobCancelledError):
        store.progress(jid, "sync", 20, "chunk 2/10")


def test_progress_raises_after_record_popped(tmp_path):
    """terminate() 已把记录弹出后, flag 仍需生效(僵尸靠 flag 而非记录感知)。"""
    store = JobStore(store_dir=tmp_path / "jobs")
    jid = _make_running_job(store, timeout_s=60)
    store.terminate(jid, "超时自动取消")

    # 记录已从内存弹出
    assert store.get(jid)["status"] == "failed"
    with pytest.raises(JobCancelledError):
        store.progress(jid, "sync", 20, "zombie chunk")


def test_cancelled_error_survives_chunk_isolation():
    """JobCancelledError 继承 BaseException: 同步循环的分块异常隔离不得吞掉它。"""
    def chunk_loop(cancel_at: int) -> str:
        for i in range(5):
            try:
                if i == cancel_at:
                    raise JobCancelledError("j1")
            except Exception:  # noqa: BLE001  — 分块隔离的典型写法
                continue
        return "completed"

    with pytest.raises(JobCancelledError):
        chunk_loop(2)


# ── 执行槽所有权 ────────────────────────────────────────────────────────

def test_run_slot_ownership_guard():
    """非持有者的释放一律忽略 —— 僵尸线程 finally 不得误释放新任务的槽。"""
    assert pipeline_jobs.try_acquire_run_slot("jobA") is True
    assert pipeline_jobs.try_acquire_run_slot("jobB") is False

    # 旧 job(僵尸)的 finally 释放: 槽属于 jobA, 忽略
    pipeline_jobs.release_run_slot("jobB")
    assert pipeline_jobs.try_acquire_run_slot("jobC") is False

    # 持有者自己释放后才可用
    pipeline_jobs.release_run_slot("jobA")
    assert pipeline_jobs.try_acquire_run_slot("jobC") is True
    pipeline_jobs.release_run_slot("jobC")


def test_run_slot_reap_release_prevents_zombie_release():
    """reap 强制释放后, 僵尸晚到的同 owner 释放是幂等 no-op, 不影响新持有者。"""
    assert pipeline_jobs.try_acquire_run_slot("jobA") is True
    pipeline_jobs.release_run_slot("jobA")  # terminate 的强制释放

    assert pipeline_jobs.try_acquire_run_slot("jobB") is True  # 新任务立即入槽
    pipeline_jobs.release_run_slot("jobA")  # 僵尸 finally: owner 不匹配 → 忽略
    assert pipeline_jobs.try_acquire_run_slot("jobC") is False  # jobB 仍持有

    pipeline_jobs.release_run_slot("jobB")
    assert pipeline_jobs.try_acquire_run_slot("jobC") is True
    pipeline_jobs.release_run_slot("jobC")


# ── 手动取消 API 端点契约 (数据页「停止」按钮) ──────────────────────────

def _install_test_identity(app) -> None:
    """**测试替身**: 补上认证中间件在真实应用里做的那一步 (写 request.state 身份)。

    真实应用从**会话 cookie** 解析身份 (app.main._resolve_identity); 测试只挂了
    router、没有账号注册表, 因此用一个测试专用请求头告诉替身"这次是谁"。这是测试
    夹具, 不是生产代码 —— 生产端点的归属判定永远只读 request.state (见
    app/api/pipeline.py 的 _identity), 绝不接受客户端提交的账户参数。
    """
    from app.services import preferences

    @app.middleware("http")
    async def _identity(request, call_next):
        raw = request.headers.get("x-test-account")
        request.state.account_id = int(raw) if raw else None
        request.state.role = request.headers.get("x-test-role", "user")
        # 端点在别处还会解析账户私有目录; 一并补上, 免得测试卡在无关的 fail-closed
        token = preferences.set_current_user_root(None)
        try:
            return await call_next(request)
        finally:
            preferences.reset_current_user_root(token)


class _AsAccount:
    """以某个账户 (或管理员) 的身份发请求 —— 只改请求头, 无需多开 app。"""

    def __init__(self, client, account_id: int | None, *, role: str = "user") -> None:
        self._client = client
        self._headers = {"x-test-role": role}
        if account_id is not None:
            self._headers["x-test-account"] = str(account_id)

    def post(self, url: str, **kwargs):
        return self._client.post(url, headers=self._headers, **kwargs)

    def get(self, url: str, **kwargs):
        return self._client.get(url, headers=self._headers, **kwargs)


def test_manual_cancel_endpoint_contract(monkeypatch, tmp_path):
    """POST /api/pipeline/jobs/{id}/cancel: 发起者可停, 终态 400, 未知 404。"""
    from fastapi import FastAPI

    from app.api.pipeline import router

    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr("app.api.pipeline.job_store", store)

    app = FastAPI()
    app.include_router(router)
    _install_test_identity(app)
    client = _AsAccount(TestClient(app), 7)

    # 未知 job → 404
    assert client.post("/api/pipeline/jobs/nope/cancel").status_code == 404

    # running → 协作式终止: 标 failed + 置取消标志 + 释放执行槽
    jid = _make_running_job(store, timeout_s=60, owner_account_id=7)
    pipeline_jobs.try_acquire_run_slot(jid)
    resp = client.post(f"/api/pipeline/jobs/{jid}/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": jid}
    j = store.get(jid)
    assert j["status"] == "failed"
    assert "手动取消" in j["error"]
    assert pipeline_jobs.is_cancelled(jid)
    assert pipeline_jobs.try_acquire_run_slot("next") is True

    # 已终态 (failed) → 400 拒绝重复取消
    assert client.post(f"/api/pipeline/jobs/{jid}/cancel").status_code == 400

    # 停止后可再建新任务 (再次拉取走完整管道的单飞基础)
    jid2, is_new = store.create(owner_account_id=7, timeout_s=60)
    assert is_new is True
    assert store.active_id() == jid2


def test_cancel_refuses_an_account_that_does_not_own_the_job(monkeypatch, tmp_path):
    """**归属校验**: 别人的任务不能取消, 无主任务只有管理员能取消。

    数据管道是部署级资源 (一份共享行情 + 一个执行槽), 因此任何已登录账户都能触发
    全站同步, 单飞复用返回已有任务也是刻意的; 但"停掉全站同步"的效果超出单个账户
    —— 该端点的权限判据与 /api/data/clear 同类。缺这道校验时, 任何账户都能 set 掉
    别人 (或调度器) 正在跑的同步任务。
    """
    from fastapi import FastAPI

    from app.api.pipeline import router

    monkeypatch.setattr(preferences, "load", lambda: {})
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr("app.api.pipeline.job_store", store)

    app = FastAPI()
    app.include_router(router)
    _install_test_identity(app)
    client = TestClient(app)
    owner = _AsAccount(client, 7)
    stranger = _AsAccount(client, 8)
    admin = _AsAccount(client, 1, role="admin")

    jid = _make_running_job(store, timeout_s=60, owner_account_id=7)

    # B (8 号账户) 不能停 7 号的任务, 且任务必须原封不动
    resp = stranger.post(f"/api/pipeline/jobs/{jid}/cancel")
    assert resp.status_code == 403
    assert store.get(jid)["status"] == "running"
    assert not pipeline_jobs.is_cancelled(jid)

    # 管理员可以
    assert admin.post(f"/api/pipeline/jobs/{jid}/cancel").status_code == 200
    assert store.get(jid)["status"] == "failed"

    # 无主任务 (调度器/系统发起, 代表全站): 普通账户不能停, 管理员可以
    scheduled = _make_running_job(store, timeout_s=60)
    assert store.get(scheduled)["owner_account_id"] is None
    assert stranger.post(f"/api/pipeline/jobs/{scheduled}/cancel").status_code == 403
    assert owner.post(f"/api/pipeline/jobs/{scheduled}/cancel").status_code == 403
    assert store.get(scheduled)["status"] == "running"
    assert admin.post(f"/api/pipeline/jobs/{scheduled}/cancel").status_code == 200
    assert store.get(scheduled)["status"] == "failed"
