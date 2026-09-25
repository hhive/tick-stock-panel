"""Threaded orchestration for persistent mining jobs."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.backtest.worker import make_worker_task, run_worker_task
from app.services.heavy_job_limiter import (
    HeavyJobCancelledError,
    shared_heavy_job_limiter,
)
from app.services.mining_jobs import (
    ACTIVE_RUN_STATUSES,
    SUCCESS_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    MiningRunStore,
    MiningRunValidationError,
    compute_run_signature,
)
from app.services.user_paths import iter_user_roots, resolve_user_root

WorkerRunner = Callable[
    [dict[str, Any], Callable[[dict[str, Any]], None], threading.Event],
    dict[str, Any],
]
TaskFactory = Callable[[str, Path, dict[str, Any]], dict[str, Any]]

_SUCCESS_STATUSES = {"succeeded", "succeeded_with_budget_exhausted"}
_SHUTDOWN_JOIN_SECONDS = 1.0


class MiningJobManager:
    """Coordinate mining persistence, capacity, cancellation, and worker threads.

    进程级**单例**, 但运行产物按账户分家 —— 两件事必须分开看:

      - 单例的理由: 它持有线程表/取消事件表/关停标志, 而挖掘本身受
        mining_process_lock 约束为「一个数据目录一个进程」; 并发上限由模块级
        ``shared_heavy_job_limiter`` 决定, 多开实例不会提高它, 只会把取消表与
        关停扇出复制 N 份。
      - 「单例」不等于「单账户」: 运行产物 (manifest/事件/工件) 是**账户私有**
        数据 (见 ``user_paths.USER_SUBDIRS`` 的 ``research/mining/runs``)。
        因此存储按**账户根**解析并逐个缓存到 ``self._stores``, 每个账户只能看到
        自己的 ``list_runs()``。

    反面样板 (本类此前的写法, 已修): 启动期用**共享** ``data_dir`` 构造唯一的
    store。``resolve_user_root`` 现在会直接拒绝共享目录 (InvalidAccountIdError)
    —— 也就是说它不只是"内存里串号", 而是启动即崩; 即便不崩, 各账户的
    list_runs() 也会互相看见。账户根是唯一权威入口, 不做任何回退。
    """

    def __init__(
        self,
        data_dir: Path | str,
        worker_runner: WorkerRunner = run_worker_task,
        task_factory: TaskFactory = make_worker_task,
    ) -> None:
        # data_dir 是**共享行情数据目录** (K线/因子所有人一份), 只用于喂 worker 子进程;
        # 运行产物一律走 store_for(user_root), 绝不落在这里。
        self._data_dir = Path(data_dir).resolve()
        self._stores: dict[Path, MiningRunStore] = {}
        self._stores_lock = threading.Lock()
        self._worker_runner = worker_runner
        self._task_factory = task_factory
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        # 在飞运行 → 该运行所属账户的 store。关停时没有调用方可以借账户根
        # (shutdown() 无参), 因此账户归属必须随运行一并记住, 否则关停要么无法
        # 取消 (签名强制传根), 要么退回"猜一个共享 store"。
        self._run_stores: dict[str, MiningRunStore] = {}
        self._shutdown = False

    def store_for(self, user_root: Path | str) -> MiningRunStore:
        """返回**该账户**的运行存储 (按账户根缓存)。

        账户根先经 ``resolve_user_root`` 校验并归一化: 共享目录 (data_dir、
        data_dir/users 容器、data_dir 的祖先) 一律拒绝。这是"把共享根当账户根"
        这类缺陷的闸门 —— 传错目录必须响, 不能静默退化成一个所有人共读的 store。

        缓存键是归一化后的路径, 所以同一账户的不同写法 (``users/1/..``、
        符号链接) 命中同一实例; 账户之间天然分开。
        """
        root = resolve_user_root(Path(user_root))
        with self._stores_lock:
            store = self._stores.get(root)
            if store is None:
                store = MiningRunStore(root)
                self._stores[root] = store
            return store

    def start(
        self,
        request: dict[str, Any],
        data_fingerprint: Any,
        *,
        user_root: Path | str,
        force: bool = False,
        source: str = "manual",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """为该账户入队一次挖掘运行。

        user_root 必填 (keyword-only): 账户归属只可能来自调用方 —— 请求路径有
        上下文可解析, 后台/调度器必须显式传。刻意**不给默认值**, 否则漏传的调用方
        会静默落到共享目录, 正是本类要根除的形态。
        """
        store = self.store_for(user_root)
        signature = compute_run_signature(request, data_fingerprint)
        with self._lock:
            if self._shutdown:
                raise RuntimeError("mining job manager is shut down")
            if not force:
                active = store.find_by_signature(
                    signature,
                    statuses=ACTIVE_RUN_STATUSES,
                )
                if active is not None:
                    return active
                succeeded = store.find_by_signature(
                    signature,
                    statuses=SUCCESS_RUN_STATUSES,
                )
                if succeeded is not None:
                    return succeeded

            try:
                manifest = store.create(
                    request,
                    data_fingerprint,
                    run_id=run_id,
                )
            except MiningRunValidationError:
                if run_id is None:
                    raise
                existing = store.get(run_id)
                if existing is None:
                    raise
                return existing
            run_id = manifest["run_id"]
            store.append_event(
                run_id,
                "queued",
                {"status": "queued", "source": source},
            )
            self._start_thread_locked(store, run_id, user_root, source)
            return manifest

    def cancel(self, run_id: str, *, user_root: Path | str) -> dict[str, Any]:
        """取消该账户的某个运行。跨账户的 run_id 查不到 → KeyError (不落他人盘)。"""
        return self._cancel(self.store_for(user_root), run_id)

    def _cancel(self, store: MiningRunStore, run_id: str) -> dict[str, Any]:
        with self._lock:
            manifest = store.get(run_id)
            if manifest is None:
                raise KeyError(run_id)
            if manifest["status"] in TERMINAL_RUN_STATUSES:
                return manifest

            cancel_event = self._cancel_events.get(run_id)
            if cancel_event is None:
                cancelled = store.transition_status(run_id, "cancelled")
                store.append_event(run_id, "cancelled", {"status": "cancelled"})
                return cancelled

            cancel_event.set()
            if manifest["status"] != "cancelling":
                manifest = store.transition_status(run_id, "cancelling")
                store.append_event(run_id, "cancelling", {"status": "cancelling"})
            return manifest

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            in_flight = list(self._run_stores.items())
        for run_id, store in in_flight:
            # 走已记住的账户 store: shutdown() 没有账户参数, 也不该猜一个。
            self._cancel(store, run_id)

        deadline = time.monotonic() + _SHUTDOWN_JOIN_SECONDS
        current = threading.current_thread()
        for run_id, _store in in_flight:
            with self._lock:
                thread = self._threads.get(run_id)
            if thread is None or thread is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def recover_interrupted(self) -> int:
        """启动补录: **逐账户**扫描, 把上个进程遗留的活跃运行标为中断。

        只在进程启动时调用 —— ``iter_user_roots`` 会读账号注册表, 属低频路径
        (与 ``strategy_cache.clear_all_accounts`` 同一姿态), 不得放进请求热路径。
        单个账户失败只记日志并继续, 不让一个坏账户中断整轮扇出。
        """
        recovered = 0
        for _account_id, user_root in iter_user_roots():
            recovered += self.store_for(user_root).recover_interrupted()
        return recovered

    def _start_thread_locked(
        self,
        store: MiningRunStore,
        run_id: str,
        user_root: Path,
        source: str,
    ) -> None:
        if run_id in self._threads:
            return
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._run_job,
            # store 在此解析并随线程参数传递: 账户归属在入队时已确定, 线程内不再
            # 依赖 contextvar (后台线程没有请求上下文), 也不再重复解析账户根。
            args=(store, run_id, user_root, source, cancel_event),
            name=f"mining-{run_id}",
            daemon=True,
        )
        self._cancel_events[run_id] = cancel_event
        self._run_stores[run_id] = store
        self._threads[run_id] = thread
        thread.start()

    def _run_job(
        self,
        store: MiningRunStore,
        run_id: str,
        user_root: Path,
        source: str,
        cancel_event: threading.Event,
    ) -> None:
        try:
            with shared_heavy_job_limiter.slot("mining", cancel_event=cancel_event):
                if not self._mark_running(store, run_id, cancel_event):
                    return
                manifest = store.get(run_id)
                if manifest is None:
                    raise KeyError(run_id)
                payload = {
                    "run_id": run_id,
                    "request": manifest["request"],
                    "data_fingerprint": manifest["data_fingerprint"],
                    "source": source,
                    # worker 是 spawn 出的独立进程, 没有请求上下文: 账户根必须随载荷
                    # 显式传下去, 否则子进程既写不到本账户的 runs 目录, 也无法解析
                    # (resolve_user_root 对缺失上下文 fail-closed)。
                    "user_root": str(user_root),
                }
                task = self._task_factory("mining", self._data_dir, payload)
                result = self._worker_runner(
                    task,
                    lambda progress: self._record_progress(store, run_id, progress, cancel_event),
                    cancel_event,
                )
                if not isinstance(result, dict):
                    raise TypeError("mining worker result must be a compact dict")
                self._finish_success(store, run_id, result, cancel_event)
        except HeavyJobCancelledError:
            self._finish_cancelled(store, run_id)
        except Exception as exc:
            if cancel_event.is_set():
                self._finish_cancelled(store, run_id)
            else:
                self._finish_failed(store, run_id, exc)
        finally:
            with self._lock:
                self._threads.pop(run_id, None)
                self._cancel_events.pop(run_id, None)
                self._run_stores.pop(run_id, None)

    def _mark_running(
        self,
        store: MiningRunStore,
        run_id: str,
        cancel_event: threading.Event,
    ) -> bool:
        with self._lock:
            if cancel_event.is_set():
                self._finish_cancelled_locked(store, run_id)
                return False
            store.transition_status(run_id, "running")
            store.append_event(run_id, "running", {"status": "running"})
            return True

    def _record_progress(
        self,
        store: MiningRunStore,
        run_id: str,
        progress: dict[str, Any],
        cancel_event: threading.Event,
    ) -> None:
        if not isinstance(progress, dict):
            raise TypeError("mining progress must be a compact dict")
        with self._lock:
            if cancel_event.is_set():
                return
            store.append_event(run_id, "progress", progress)
            store.write_summary(run_id, {"progress": progress})

    def _finish_success(
        self,
        store: MiningRunStore,
        run_id: str,
        result: dict[str, Any],
        cancel_event: threading.Event,
    ) -> None:
        status = result.get("status", "succeeded")
        if status not in _SUCCESS_STATUSES:
            raise ValueError(f"unsupported mining worker status: {status!r}")
        with self._lock:
            if cancel_event.is_set():
                self._finish_cancelled_locked(store, run_id)
                return
            store.write_summary(run_id, result)
            store.transition_status(run_id, status)
            store.append_event(run_id, status, {"status": status})

    def _finish_cancelled(self, store: MiningRunStore, run_id: str) -> None:
        with self._lock:
            self._finish_cancelled_locked(store, run_id)

    def _finish_cancelled_locked(self, store: MiningRunStore, run_id: str) -> None:
        manifest = store.get(run_id)
        if manifest is None or manifest["status"] in TERMINAL_RUN_STATUSES:
            return
        store.transition_status(run_id, "cancelled")
        store.append_event(run_id, "cancelled", {"status": "cancelled"})

    def _finish_failed(self, store: MiningRunStore, run_id: str, exc: Exception) -> None:
        message = str(exc)[:2000]
        with self._lock:
            manifest = store.get(run_id)
            if manifest is None or manifest["status"] in TERMINAL_RUN_STATUSES:
                return
            store.transition_status(run_id, "failed", error=message)
            store.append_event(
                run_id,
                "error",
                {"status": "failed", "message": message},
            )
