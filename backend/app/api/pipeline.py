"""盘后管道 API — 异步触发 + 进度跟踪。"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.jobs import daily_pipeline
from app.services.pipeline_jobs import (
    JobCancelledError,
    job_store,
    may_cancel,
    release_run_slot,
    run_with_capacity,
    try_acquire_run_slot,
)
from app.api.data import invalidate_storage_cache

# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


def _identity(request: Request) -> tuple[int | None, bool]:
    """当前请求的 (account_id, is_admin)。

    身份由认证中间件注入 (app.main), **不接受**客户端提交的账户参数 —— 归属判定
    只能以会话为准。读到 None 就是"无账户身份": 单密码应急入口本来就没有
    account_id, 游客则进不到这些端点 (它们在"需登录"侧)。此时任务记为无主,
    而无主任务只有管理员能取消 —— 降级方向是收紧, 与 app/api/deps.py 一致。
    """
    state = getattr(request, "state", None)
    raw = getattr(state, "account_id", None)
    account_id = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    return account_id, getattr(state, "role", "guest") == "admin"


def _project_job(job: dict[str, Any], request: Request) -> dict[str, Any]:
    """给客户端看的任务视图: 用 cancel_allowed 取代 owner_account_id。

    账户主键不外发 (面板不向他人暴露账号 id); 客户端真正需要的是"我能不能停它"。
    """
    account_id, is_admin = _identity(request)
    projected = {k: v for k, v in job.items() if k != "owner_account_id"}
    projected["cancel_allowed"] = may_cancel(
        job, account_id=account_id, is_admin=is_admin
    )
    return projected


@router.post("/run")
async def run_now(request: Request) -> dict:
    """异步触发盘后管道,立即返回 job_id。客户端轮询 /jobs/{id} 拿进度。

    若已有任务在跑,**返回该任务 id 而不是开新任务**(防止并发拉数据撞限流)。
    卡死判定按「进度停滞」而非总时长(慢带宽下长任务不会被误杀), 见 reap_stale。

    复用时返回的可能是**别人**发起的任务: 管道是部署级资源 (一份共享行情),
    复用是刻意的; 归属只影响"谁能取消", 见 may_cancel。
    """
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    account_id, _is_admin = _identity(request)

    # 检测卡死的 running job (如 reload 后孤儿 task / 网络读无限阻塞)。
    # reap_stale 会在 /run 和 /jobs/{id} 轮询端点都调用,保证卡死后能自愈。
    job_store.reap_stale()

    # 单飞: 复用任何活跃 (pending∨running) 任务, is_new=False 时不再调度新任务
    job_id, is_new = job_store.create(owner_account_id=account_id)
    if not is_new:
        return {"job_id": job_id, "reused": True}

    # 在 executor 里跑同步任务(pipeline 内部都是阻塞 IO + CPU)
    async def task() -> None:
        # 重任务执行槽: 防僵尸并发(reap 后线程仍活时新任务不得并行写 parquet)
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        # 管道运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = getattr(request.app.state, "quote_service", None)
        try:
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                         skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

            def _run() -> dict:
                try:
                    if qs:
                        with qs.paused():
                            return daily_pipeline.run_now(repo, capset, on_progress=progress)
                    return daily_pipeline.run_now(repo, capset, on_progress=progress)
                finally:
                    repo.refresh_cache()

            result = await loop.run_in_executor(_long_task_executor, run_with_capacity, job_id, _run)
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
        except JobCancelledError:
            # 已被 reap/手动取消终止: job 状态已由 terminate() 写为 failed,
            # 拉取线程在分块回调处自行退出, 这里无需(也无法)再写状态。
            logger.warning("pipeline job %s cancelled", job_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("pipeline failed")
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    asyncio.create_task(task())
    return {"job_id": job_id, "reused": False}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    # 每次轮询都检查卡死 job — 前端持续轮询, 进度停滞超阈值后必定自愈,
    # 无需用户再次手动点「同步」。
    job_store.reap_stale()
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return _project_job(j, request)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict:
    """手动取消一个 running 的 job(协作式: 拉取线程在当前分块完成后自行退出)。

    归属校验: 管道是**部署级**资源 (全站共用一份行情与一个执行槽), 触发谁都可以,
    但停掉它影响到所有人 —— 因此只有发起者本人或管理员能取消。缺这道校验时,
    任何账户都能 set 掉别人 (或调度器) 正在跑的同步任务。
    """
    account_id, is_admin = _identity(request)
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    if not may_cancel(j, account_id=account_id, is_admin=is_admin):
        raise HTTPException(
            status_code=403,
            detail="该数据任务由其它账户或系统调度发起，只有发起者或管理员可以取消",
        )
    if j["status"] not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"job status is {j['status']}, cannot cancel")
    job_store.terminate(job_id, "用户手动取消")
    return {"cancelled": job_id}


@router.get("/jobs")
def list_jobs(request: Request, limit: int = 20) -> dict:
    """任务列表是**全站**的 (管道是部署级资源, 且 /api/data/status 这个公开只读
    端点也读同一份记录), 各账户看到的是同一次全站同步的进度。归属不外发, 只发
    cancel_allowed。"""
    return {
        "active_id": job_store.active_id(),
        "jobs": [_project_job(j, request) for j in job_store.list_recent(limit=limit)],
    }
