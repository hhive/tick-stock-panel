from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import app.services.mining_manager as mining_manager_module
from app import config as app_config
from app.services import user_paths
from app.services.heavy_job_limiter import HeavyJobLimiter
from app.services.mining_jobs import MiningRunStore
from app.services.mining_manager import MiningJobManager
from app.services.user_paths import InvalidAccountIdError


class _AccountManager:
    """把**一个账户根**钉在 manager 上的薄壳 (仅测试用)。

    manager 是进程级单例 (``data_dir`` 只喂 worker 子进程), 而运行产物按账户根
    分家: 每个 ``start``/``cancel``/``store_for`` 都必须带账户。壳把测试账户根
    固定下来, 于是用例主体仍走**真** manager 方法, 却不必每处重复传根 ——
    同时双账户用例可以再建第二个壳, 证明两边互不可见。
    """

    def __init__(self, manager: MiningJobManager, user_root: Path) -> None:
        self._manager = manager
        self.user_root = user_root

    @property
    def store(self) -> MiningRunStore:
        return self._manager.store_for(self.user_root)

    def start(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # 默认本账户根; 用例可以显式传别的根 (负面用例: 传共享目录必须报错)。
        kwargs.setdefault("user_root", self.user_root)
        return self._manager.start(*args, **kwargs)

    def cancel(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("user_root", self.user_root)
        return self._manager.cancel(run_id, **kwargs)

    def for_account(self, account_id: int) -> _AccountManager:
        """同一 manager 上的**另一个**账户壳 —— 跨账户用例用它证明互不可见。"""
        return _AccountManager(self._manager, user_paths.user_root(account_id))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)


def _task_factory(kind: str, data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "data_dir": str(data_dir),
        "payload": payload,
    }


def _wait_for_status(
    manager: _AccountManager,
    run_id: str,
    status: str,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manifest = manager.store.get(run_id)
        assert manifest is not None
        if manifest["status"] == status:
            return manifest
        time.sleep(0.005)
    pytest.fail(f"run {run_id} did not reach {status}")


def _wait_for_event_types(
    manager: _AccountManager,
    run_id: str,
    expected: list[str],
    *,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    """轮询到事件序列收敛后返回。

    manager 各终态路径先 transition_status 再 append_event (两次独立写入),
    状态可见时终态事件可能尚未落盘 —— 状态到达后立即断言事件序列会偶发读到
    缺末尾事件的状态。终态后不会再有新事件, 轮询必然收敛。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = manager.store.read_events(run_id)
        if [event["type"] for event in events] == expected:
            return events
        time.sleep(0.005)
    pytest.fail(f"run {run_id} events did not converge to {expected}")


@pytest.fixture
def isolated_limiter(monkeypatch: pytest.MonkeyPatch) -> HeavyJobLimiter:
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.005)
    monkeypatch.setattr(mining_manager_module, "shared_heavy_job_limiter", limiter)
    return limiter


@pytest.fixture
def make_manager(
    tmp_path: Path,
    isolated_limiter: HeavyJobLimiter,
    monkeypatch: pytest.MonkeyPatch,
):
    # data_dir 指向 tmp_path: 账户根随之变成 tmp_path/users/<id>, 与部署布局一致
    # (共享行情在 data_dir 顶层, 账户私有数据在 users/<id> 之下)。
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    managers: list[MiningJobManager] = []

    def factory(
        runner: Callable[
            [dict[str, Any], Callable[[dict[str, Any]], None], threading.Event],
            dict[str, Any],
        ],
        *,
        account_id: int = 1,
    ) -> _AccountManager:
        manager = MiningJobManager(
            tmp_path,
            worker_runner=runner,
            task_factory=_task_factory,
        )
        managers.append(manager)
        return _AccountManager(manager, user_paths.user_root(account_id))

    yield factory

    for manager in managers:
        manager.shutdown()
    assert isolated_limiter.in_use == 0


def test_start_records_states_events_progress_and_worker_payload(
    make_manager, tmp_path: Path
) -> None:
    progress_recorded = threading.Event()
    finish = threading.Event()
    tasks: list[dict[str, Any]] = []
    progress = {"phase": "screen", "done": 1, "total": 2}
    result = {"status": "succeeded", "candidate_count": 3, "elapsed_ms": 12.5}

    def runner(task, progress_cb, cancel_event):
        tasks.append(task)
        progress_cb(progress)
        progress_recorded.set()
        assert finish.wait(2)
        assert not cancel_event.is_set()
        return result

    manager = make_manager(runner)
    request = {"factor_names": ["momentum"], "budget_profile": "balanced"}
    created = manager.start(request, {"daily": "v1"}, source="scheduled")
    run_id = created["run_id"]

    assert created["status"] == "queued"
    assert progress_recorded.wait(1)
    assert manager.store.read_summary(run_id) == {"progress": progress}
    assert [event["type"] for event in manager.store.read_events(run_id)] == [
        "queued",
        "running",
        "progress",
    ]
    assert manager.store.read_events(run_id)[0]["payload"]["source"] == "scheduled"
    assert tasks == [
        {
            "kind": "mining",
            "data_dir": str(tmp_path.resolve()),
            "payload": {
                "run_id": run_id,
                "request": request,
                "data_fingerprint": {"daily": "v1"},
                "source": "scheduled",
                # worker 是 spawn 出的独立进程: 账户根必须随载荷传下去,
                # 否则子进程写不到本账户的 runs 目录 (且无法解析账户根)。
                "user_root": str(user_paths.user_root(1)),
            },
        }
    ]

    finish.set()
    terminal = _wait_for_status(manager, run_id, "succeeded")
    assert terminal["started_at"] is not None
    assert terminal["finished_at"] is not None
    assert manager.store.read_summary(run_id) == result
    _wait_for_event_types(
        manager, run_id, ["queued", "running", "progress", "succeeded"]
    )


def test_start_accepts_valid_persistent_run_id(make_manager) -> None:
    def runner(task, progress_cb, cancel_event):
        return {"status": "succeeded"}

    manager = make_manager(runner)
    created = manager.start(
        {"factor_names": ["value"]},
        "data-v1",
        run_id="weekly_claim_2026_33",
    )

    assert created["run_id"] == "weekly_claim_2026_33"
    terminal = _wait_for_status(manager, created["run_id"], "succeeded")
    assert terminal["run_id"] == "weekly_claim_2026_33"


def test_duplicate_persistent_run_id_reuses_existing_without_starting_runner(
    make_manager,
) -> None:
    runner_called = threading.Event()

    def runner(task, progress_cb, cancel_event):
        runner_called.set()
        return {"status": "succeeded"}

    manager = make_manager(runner)
    existing = manager.store.create(
        {"factor_names": ["value"]},
        "data-v1",
        run_id="weekly_claim_2026_33",
    )

    reused = manager.start(
        {"factor_names": ["value"]},
        "data-v1",
        run_id="weekly_claim_2026_33",
    )

    assert reused == existing
    assert not runner_called.wait(0.05)


def test_start_reuses_active_and_success_but_force_creates_new_run(make_manager) -> None:
    started = threading.Event()
    release = threading.Event()
    tasks: list[dict[str, Any]] = []

    def runner(task, progress_cb, cancel_event):
        tasks.append(task)
        started.set()
        assert release.wait(2)
        return {"status": "succeeded", "candidate_count": 1}

    manager = make_manager(runner)
    request = {"factor_names": ["value"]}
    first = manager.start(request, "data-v1")
    assert started.wait(1)

    active_reuse = manager.start(request, "data-v1")
    assert active_reuse["run_id"] == first["run_id"]
    assert len(tasks) == 1

    release.set()
    _wait_for_status(manager, first["run_id"], "succeeded")
    success_reuse = manager.start(request, "data-v1")
    assert success_reuse["run_id"] == first["run_id"]
    assert len(tasks) == 1

    forced = manager.start(request, "data-v1", force=True)
    assert forced["run_id"] != first["run_id"]
    _wait_for_status(manager, forced["run_id"], "succeeded")
    assert len(tasks) == 2
    assert len(manager.store.list_runs()) == 2


def test_cancel_while_waiting_for_capacity_never_calls_runner(
    make_manager,
    isolated_limiter: HeavyJobLimiter,
) -> None:
    runner_called = threading.Event()

    def runner(task, progress_cb, cancel_event):
        runner_called.set()
        return {"status": "succeeded"}

    assert isolated_limiter.acquire("mining", timeout=0)
    try:
        manager = make_manager(runner)
        created = manager.start({"factor_names": ["quality"]}, "data-v1")
        run_id = created["run_id"]
        assert manager.store.get(run_id)["status"] == "queued"  # type: ignore[index]

        cancelling = manager.cancel(run_id)
        assert cancelling["status"] == "cancelling"
        _wait_for_status(manager, run_id, "cancelled")
        assert not runner_called.is_set()
        _wait_for_event_types(
            manager, run_id, ["queued", "cancelling", "cancelled"]
        )
    finally:
        isolated_limiter.release("mining")


def test_cancel_running_job_wins_over_worker_success(make_manager) -> None:
    runner_started = threading.Event()

    def runner(task, progress_cb, cancel_event):
        runner_started.set()
        assert cancel_event.wait(2)
        return {"status": "succeeded", "candidate_count": 9}

    manager = make_manager(runner)
    created = manager.start({"factor_names": ["growth"]}, "data-v1")
    run_id = created["run_id"]
    assert runner_started.wait(1)

    cancelling = manager.cancel(run_id)
    assert cancelling["status"] == "cancelling"
    _wait_for_status(manager, run_id, "cancelled")
    _wait_for_event_types(manager, run_id, ["queued", "running", "cancelling", "cancelled"])
    assert manager.store.read_summary(run_id) == {}


def test_runner_exception_marks_failed_and_appends_error_event(make_manager) -> None:
    def runner(task, progress_cb, cancel_event):
        raise RuntimeError("mining exploded")

    manager = make_manager(runner)
    created = manager.start({"factor_names": ["size"]}, "data-v1")
    run_id = created["run_id"]

    failed = _wait_for_status(manager, run_id, "failed")
    assert failed["error"] == "mining exploded"
    events = _wait_for_event_types(manager, run_id, ["queued", "running", "error"])
    assert events[-1]["payload"] == {
        "status": "failed",
        "message": "mining exploded",
    }


def test_non_dict_worker_result_is_rejected(make_manager) -> None:
    def runner(task, progress_cb, cancel_event):
        return ["full", "result"]

    manager = make_manager(runner)
    created = manager.start({"factor_names": ["liquidity"]}, "data-v1")

    failed = _wait_for_status(manager, created["run_id"], "failed")
    assert failed["error"] == "mining worker result must be a compact dict"


def test_budget_exhausted_result_uses_distinct_success_status(make_manager) -> None:
    result = {
        "status": "succeeded_with_budget_exhausted",
        "candidate_count": 2,
        "budget_exhausted": True,
    }

    def runner(task, progress_cb, cancel_event):
        return result

    manager = make_manager(runner)
    created = manager.start({"factor_names": ["volatility"]}, "data-v1")
    run_id = created["run_id"]

    _wait_for_status(manager, run_id, "succeeded_with_budget_exhausted")
    assert manager.store.read_summary(run_id) == result
    _wait_for_event_types(
        manager, run_id, ["queued", "running", "succeeded_with_budget_exhausted"]
    )


def test_recover_interrupted_delegates_to_store(
    make_manager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def runner(task, progress_cb, cancel_event):
        return {"status": "succeeded"}

    manager = make_manager(runner)
    _point_fanout_at(monkeypatch, tmp_path, (1,))
    manager.store.create({}, "v1", run_id="running_before_restart")
    manager.store.transition_status("running_before_restart", "running")
    manager.store.create({}, "v1", run_id="cancelling_before_restart")
    manager.store.transition_status("cancelling_before_restart", "cancelling")
    manager.store.create({}, "v1", run_id="queued_before_restart")

    assert manager.recover_interrupted() == 3
    assert manager.store.get("running_before_restart")["status"] == "interrupted"  # type: ignore[index]
    assert manager.store.get("cancelling_before_restart")["status"] == "interrupted"  # type: ignore[index]
    assert manager.store.get("queued_before_restart")["status"] == "interrupted"  # type: ignore[index]


def test_recover_interrupted_fans_out_over_every_account(
    make_manager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """启动补录逐账户扫描: 旧实现只有一个 (共享根) store, 只能看到第一个目录。"""

    def runner(task, progress_cb, cancel_event):
        return {"status": "succeeded"}

    account_a = make_manager(runner)
    account_b = account_a.for_account(2)
    _point_fanout_at(monkeypatch, tmp_path, (1, 2))
    account_a.store.create({}, "v1", run_id="a_running")
    account_a.store.transition_status("a_running", "running")
    account_b.store.create({}, "v1", run_id="b_queued")

    assert account_a.recover_interrupted() == 2
    assert account_a.store.get("a_running")["status"] == "interrupted"  # type: ignore[index]
    assert account_b.store.get("b_queued")["status"] == "interrupted"  # type: ignore[index]


def test_store_for_refuses_the_shared_data_dir(
    make_manager,
    tmp_path: Path,
) -> None:
    """共享行情根不得当作账户根 —— 这正是"启动期用共享根建 store"这个缺陷的闸门。

    真 manager 此前有 self._store = MiningRunStore(data_dir), 那不仅是"内存里看得见
    别人的运行", 而是启动即抛 InvalidAccountIdError; store_for 必须把这个错误显式
    暴露出来, 而不是悄悄退化成所有人共读一个 store。
    """

    def runner(task, progress_cb, cancel_event):
        return {"status": "succeeded"}

    manager = make_manager(runner)

    with pytest.raises(InvalidAccountIdError):
        manager.store_for(tmp_path)
    with pytest.raises(InvalidAccountIdError):
        manager.start({"factor_names": ["value"]}, "data-v1", user_root=tmp_path)


def _point_fanout_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    account_ids: tuple[int, ...],
) -> None:
    """把 manager 的账户扇出指向测试账户 (真实现读的是部署账号注册表)。"""
    monkeypatch.setattr(
        mining_manager_module,
        "iter_user_roots",
        lambda: [
            (account_id, user_paths.user_root(account_id)) for account_id in account_ids
        ],
    )


def test_shutdown_sets_cancel_uses_bounded_join_and_keeps_history(
    make_manager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_started = threading.Event()
    release_runner = threading.Event()

    def runner(task, progress_cb, cancel_event):
        runner_started.set()
        assert release_runner.wait(2)
        return {"status": "succeeded"}

    monkeypatch.setattr(mining_manager_module, "_SHUTDOWN_JOIN_SECONDS", 0.02)
    manager = make_manager(runner)
    created = manager.start({"factor_names": ["reversal"]}, "data-v1")
    run_id = created["run_id"]
    assert runner_started.wait(1)

    started = time.monotonic()
    manager.shutdown()
    elapsed = time.monotonic() - started
    assert elapsed < 0.2
    assert manager.store.get(run_id)["status"] == "cancelling"  # type: ignore[index]

    release_runner.set()
    _wait_for_status(manager, run_id, "cancelled")
    assert manager.store.get(run_id) is not None
    with pytest.raises(RuntimeError, match="shut down"):
        manager.start({"factor_names": ["new"]}, "data-v1")
