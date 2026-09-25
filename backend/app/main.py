"""FastAPI 入口。"""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import (
    abnormal,
    account,
    alerts,
    analysis,
    backtest,
    data,
    ext_data,
    factors,
    financials,
    indices,
    intraday,
    kline,
    lots,
    market_recap,
    mining,
    monitor_rules,
    overview,
    paper,
    pipeline,
    regime,
    rps,
    screener,
    sector_rotation,
    signals,
    stock_analysis,
    strategy,
    watchlist,
)
from app.api import auth as auth_api
from app.api import settings as settings_api
from app.api.routes import router as core_router
from app.config import settings
from app.enriched_generation import EnrichedGenerationUnavailableError
from app.extensions.loader import (
    configure_backend_extensions,
    current_extension_context,
    start_backend_extensions,
)
from app.jobs import daily_pipeline
from app.services.matrix_prewarm_owner import MatrixCachePrewarmOwner
from app.services.mining_process_lock import MiningProcessLock
from app.services.quote_service import QuoteService
from app.tickflow import client as tf_client
from app.tickflow.policy import detect_capabilities
from app.tickflow.repository import DataStore, KlineRepository

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 追加文件日志: uvicorn (含 --reload 开发模式) 默认只有 StreamHandler, 同步/管道等
# 运行时日志仅出现在 dev 终端, 关掉或滚屏后即丢失, 排查「同步后日志没落」时无处可查。
# 落盘到 data/backend.log 与桌面版 (desktop.py:_setup_logging → desktop.log) 行为对齐,
# 事后可查。桌面版 (frozen) 已由 desktop.py 写 desktop.log, 此处跳过避免重复落盘。
# RotatingFileHandler 防止长期运行/频繁 reload 导致文件无限增长。
if not getattr(sys, "frozen", False):
    try:
        from logging.handlers import RotatingFileHandler

        _log_path = settings.data_dir / "backend.log"
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = RotatingFileHandler(
            _log_path, maxBytes=10 * 1024 * 1024, backupCount=3,
            mode="a", encoding="utf-8", errors="replace",
        )
        _file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(_file_handler)
    except Exception as _e:  # noqa: BLE001
        logger.warning("文件日志初始化失败, 仅输出到终端: %s", _e)


@asynccontextmanager
async def _application_lifespan(app: FastAPI):
    logger.info(
        "Tick Stock Panel v%s starting (mode=%s)",
        __version__, tf_client.current_mode(),
    )

    # 首次启动: 若配置了 AUTH_PASSWORD 环境变量且未设过密码, 用它初始化。
    # 公网部署免 SSH 端口转发; 已设过密码则不覆盖 (改密码走 UI)。
    try:
        from app.services import auth as auth_service
        auth_service.bootstrap_from_env()
    except Exception as e:  # noqa: BLE001
        logger.warning("auth bootstrap failed: %s", e)

    # 数据层
    store = DataStore()
    repo = KlineRepository(store)
    app.state.datastore = store
    app.state.repo = repo
    # 自定义/复合因子载入注册表 (P3); 单个失败只跳过该因子 (fail-隔离)。
    # 因子已按账户存放, 启动期没有账户上下文 —— **不回退**到共享目录 (那会把某个
    # 账户的因子读成所有人的), 这里只留痕; 每账户扇出补齐后再显式传 user_root。
    from app.factors.store import load_into_registry

    try:
        loaded_factors = load_into_registry()
        if loaded_factors:
            logger.info("custom factors loaded: %s", len(loaded_factors))
    except Exception as exc:  # noqa: BLE001
        logger.warning("custom factors load failed: %s", exc)
    from app.services.mining_manager import MiningJobManager

    mining_manager = MiningJobManager(store.data_dir)
    recovered_mining_runs = mining_manager.recover_interrupted()
    app.state.mining_manager = mining_manager
    if recovered_mining_runs:
        logger.warning("recovered %d interrupted mining runs", recovered_mining_runs)
    # 在接受回测请求前固定 managed generation，避免首批并发 worker 各自创建版本。
    if settings.backtest_matrix_disk_cache_enabled:
        try:
            repo.get_matrix_data_generation("stock")
        except EnrichedGenerationUnavailableError as exc:
            logger.warning("enriched generation requires a full rebuild: %s", exc)
    # 指标异步预热标志: enriched 缓存在后台线程构建, 完成后置 True
    app.state.indicators_ready = False
    repo._on_warmup_done = lambda: setattr(app.state, "indicators_ready", True)  # noqa: SLF001

    # Polars 缓存预热 — enriched 的重计算 (107万行 compute_indicators) 推后台,
    # instruments/index/ETF 仍同步 (毫秒级)。应用立即 ready, 指标算完后自动替换。
    repo.refresh_cache(background=True)

    # 自定义数据源配置(可选): 失败只记录错误, 不影响 TickFlow 基准路径。
    try:
        from app.data_providers import custom as custom_sources
        custom_sources.load_all()
        logger.info("custom data sources loaded: %d", len(custom_sources.list_sources()))
    except Exception as e:  # noqa: BLE001
        logger.warning("custom data sources init failed: %s", e)

    # 自定义源必须先注册,能力探测才能补充其数据集能力。
    capset = detect_capabilities()
    app.state.capabilities = capset
    logger.info("ready; %d capabilities active", len(capset.all()))

    # 全局行情服务
    qs = QuoteService()
    app.state.quote_service = qs
    qs.set_repo(repo)
    qs.boot_check()

    # QuoteService 需要访问 strategy_monitor 等单例
    # 先创建 strategy_monitor，再注入 app.state
    from app.strategy.monitor import StrategyMonitorService
    strategy_monitor = StrategyMonitorService()
    app.state.strategy_monitor = strategy_monitor
    qs.set_app_state(app.state)

    # 五档盘口 sealed 服务(真假涨停/跌停, 独立旁路线)
    from app.services.depth_service import DepthService
    depth_service = DepthService()
    depth_service.set_repo(repo)
    depth_service.set_app_state(app.state)
    app.state.depth_service = depth_service

    # 启动调度器(若 enriched 数据为空,首次启动可手动 POST /api/pipeline/run)
    try:
        daily_pipeline.set_app_state(app.state)  # 供 depth_finalize job 访问 depth_service
        scheduler = daily_pipeline.start_scheduler(repo, capset)
        app.state.scheduler = scheduler
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduler not started: %s", e)
        app.state.scheduler = None

    # depth sealed: 启动补跑(当天文件不存在) + 盘中轮询(有能力时)
    try:
        depth_service.boot_check()
        depth_service.start_polling()
    except Exception as e:  # noqa: BLE001
        logger.warning("depth_service init failed: %s", e)

    # 盘中分钟增量刷新 (Expert 专有): 线程常驻, 开关/时段/能力门控在循环内每轮判断
    try:
        from app.services.minute_refresh import MinuteRefreshService
        minute_refresh = MinuteRefreshService(repo)
        minute_refresh.set_app_state(app.state)
        app.state.minute_refresh = minute_refresh
        minute_refresh.start()
    except Exception as e:
        logger.warning("minute_refresh init failed: %s", e)

    # 停机缺口自检: 延迟后台扫描, 发现最近交易日的盘中快照/缺口时自动创建
    # 修复任务 (盘中停机→次日开实时场景, 不修则坏数据被"只刷今天"分支永久留存)
    try:
        import threading

        from app.services.data_integrity import boot_integrity_check

        timer = threading.Timer(30.0, boot_integrity_check, args=(app.state,))
        timer.daemon = True  # 不阻塞进程退出
        timer.start()
    except Exception as e:  # noqa: BLE001
        logger.warning("integrity boot check scheduling failed: %s", e)

    # 企业微信智能机器人长连接(可选通道, 失败不阻断启动)
    try:
        from app.services.wecom_bot_service import WecomBotService
        wecom_bot_service = WecomBotService()
        wecom_bot_service.set_app_state(app.state)
        app.state.wecom_bot_service = wecom_bot_service
        wecom_bot_service.boot_check()
    except Exception as e:  # noqa: BLE001
        logger.warning("wecom_bot_service init failed: %s", e)

    # 内置扩展表 (概念/行业): 先创建 config (含拉取配置), 默认开启定时拉取。
    # 必须在 pull_scheduler.refresh() 之前执行, 否则全新部署时 scheduler 读不到
    # 刚创建的预设, 定时任务不会启动。
    try:
        from app.services.ext_presets import ensure_builtin_presets
        await ensure_builtin_presets(store.data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("内置扩展表初始化失败 (不影响启动): %s", e)

    # 扩展数据定时拉取: 在预设配置就绪后启动, 自动调度 enabled 的预设。
    from app.services.ext_pull import pull_scheduler
    pull_scheduler.start(store.data_dir)
    pull_scheduler.refresh(store.data_dir)
    app.state.pull_scheduler = pull_scheduler

    # 财务数据 (需 Expert 套餐): 仅初始化调度器供 /api/financials/sync/* 手动同步,
    # 不启动自动调度——用户在「财务分析」页点「同步」手动拉取。
    from app.services.financial_sync import financial_scheduler
    financial_scheduler.start(store.data_dir, capset)
    app.state.financial_scheduler = financial_scheduler

    # 自愈看门狗: 探测 polars 闸与写锁, 僵死时退出交由 supervisor 拉起 (兜底层)。
    from app.watchdog import start_watchdog
    app.state.watchdog = start_watchdog(app.state, repo)

    # 策略引擎
    from app.strategy.engine import StrategyEngine
    from app.strategy import config as strategy_config
    from app.strategy.monitor import StrategyMonitorService
    from app.services.screener import ScreenerService

    _screener_svc = ScreenerService(repo)
    _etf_screener_svc = ScreenerService(repo, asset_type="etf")
    strategy_dirs = [
        Path(__file__).resolve().parent / "strategy" / "builtin",
        store.data_dir / "strategies" / "custom",
        store.data_dir / "strategies" / "ai",
        store.data_dir / "strategies" / "composite",
    ]
    strategy_engine = StrategyEngine(
        strategy_dirs=strategy_dirs,
        # TODO(multiuser): 引擎启动加载仍用共享 data_dir, 需改为按账户扇出 (S3)。
        override_loader=lambda sid: strategy_config.load_override(sid, user_root=store.data_dir),
    )
    app.state.strategy_engine = strategy_engine
    logger.info("strategy engine loaded: %d strategies", len(strategy_engine.list_strategies()))

    matrix_prewarm_owner = MatrixCachePrewarmOwner()

    def _schedule_matrix_cache_prewarm() -> None:
        if (
            not settings.backtest_matrix_disk_cache_enabled
            or not settings.backtest_matrix_cache_prewarm
        ):
            return

        def _prewarm() -> None:
            from app.backtest.engine import BacktestEngine
            from app.backtest.matrix import MatrixPrewarmCancelledError
            from app.backtest.strategy import prewarm_matrix_cache
            from app.services.heavy_job_limiter import (
                HeavyJobCancelledError,
                shared_heavy_job_limiter,
            )

            try:
                latest = repo.latest_enriched_date("stock")
                if latest is None:
                    logger.info("matrix cache prewarm skipped: no stock enriched data")
                    return

                with shared_heavy_job_limiter.slot(
                    "exclusive",
                    cancel_event=matrix_prewarm_owner.cancel_event,
                ):
                    result = prewarm_matrix_cache(
                        BacktestEngine(repo),
                        strategy_engine,
                        asset_type="stock",
                        latest_date=latest,
                        years=settings.backtest_matrix_cache_prewarm_years,
                        cancel_event=matrix_prewarm_owner.cancel_event,
                    )
                logger.info("matrix cache prewarm done: %s", result)
            except (HeavyJobCancelledError, MatrixPrewarmCancelledError):
                logger.info("matrix cache prewarm cancelled")
            except Exception:  # noqa: BLE001
                logger.exception("matrix cache prewarm failed")

        if not matrix_prewarm_owner.schedule(_prewarm):
            logger.info("matrix cache prewarm already running or shutting down, skip")

    repo._on_refresh_done = _schedule_matrix_cache_prewarm  # noqa: SLF001
    if repo.enriched_ready:
        _schedule_matrix_cache_prewarm()

    # 通用监控规则引擎: 启动时 reload 规则到内存态 (修复重启后告警失效)
    from app.strategy.monitor import MonitorRuleEngine
    from app.strategy import monitor_rules as mr_store
    from app.services import preferences
    from app.services.sector_monitor import SectorMonitorService
    monitor_engine = MonitorRuleEngine()
    sector_monitor_service = SectorMonitorService(repo)
    monitor_engine.set_strategy_engine(strategy_engine)
    monitor_engine.set_data_dir(store.data_dir)
    monitor_engine.set_sector_monitor_service(sector_monitor_service)
    # 复用 ScreenerService 的历史窗口加载器 (三级缓存, 启动预计算命中 ~0ms),
    # 让声明 filter_history 的策略 (如反包) 也能在实时监控里跑选股 → 盘中触发通知。
    monitor_engine.set_history_loader(_screener_svc._load_enriched_history)
    # ETF 版历史加载器: asset_type=etf 的 strategy 型规则用 (读 kline_etf_enriched)。
    monitor_engine.set_history_loader_etf(_etf_screener_svc._load_enriched_history)

    # 自动迁移: 把旧 strategy_monitor_ids 同步为 type=strategy 规则 (统一到监控页)
    try:
        if preferences.get_strategy_monitor_enabled():
            ids = preferences.get_strategy_monitor_ids()
            if ids:
                names = {s["id"]: s["name"] for s in strategy_engine.list_strategies()}
                mr_store.migrate_strategy_monitors(store.data_dir, ids, names)
                logger.info("strategy monitor migrated: %d strategies", len(ids))
    except Exception as e:  # noqa: BLE001
        logger.warning("strategy monitor migration failed: %s", e)

    try:
        rules = mr_store.load_all(store.data_dir)
        monitor_engine.set_rules(rules)
        logger.info("monitor engine loaded: %d rules", monitor_engine.rule_count)
    except Exception as e:  # noqa: BLE001
        logger.warning("monitor engine load failed: %s", e)
    app.state.monitor_engine = monitor_engine
    app.state.sector_monitor_service = sector_monitor_service

    # 源码内二次开发启动钩子: 仅暴露稳定只读上下文, 单个扩展失败不影响核心启动。
    extension_registry = app.state.extension_registry
    start_backend_extensions(
        current_extension_context(data_dir=store.data_dir, repository=repo),
        extension_registry,
    )

    try:
        yield
    finally:
        repo._on_refresh_done = None  # noqa: SLF001
        wd = getattr(app.state, "watchdog", None)
        if wd:
            await wd.stop()
        if not matrix_prewarm_owner.shutdown(timeout=5.0):
            logger.warning("matrix cache prewarm did not stop within 5 seconds")
        mmanager = getattr(app.state, "mining_manager", None)
        if mmanager:
            mmanager.shutdown()
        if app.state.scheduler:
            app.state.scheduler.shutdown(wait=False)
        ps = getattr(app.state, "pull_scheduler", None)
        if ps:
            ps.stop()
        fsc = getattr(app.state, "financial_scheduler", None)
        if fsc:
            fsc.stop()
        qs = getattr(app.state, "quote_service", None)
        if qs:
            qs.stop()
        dsvc = getattr(app.state, "depth_service", None)
        if dsvc:
            dsvc.stop_polling()
        wbot = getattr(app.state, "wecom_bot_service", None)
        if wbot:
            wbot.stop()
        mrs = getattr(app.state, "minute_refresh", None)
        if mrs:
            mrs.stop()
        logger.info("shutdown")


@asynccontextmanager
async def lifespan(app: FastAPI):
    mining_process_lock = MiningProcessLock(settings.data_dir)
    mining_process_lock.acquire()
    try:
        async with _application_lifespan(app):
            yield
    finally:
        mining_process_lock.release()


app = FastAPI(
    title="Tick Stock Panel",
    version=__version__,
    description="A 股选股 + 回测面板 — TickFlow 适配",
    lifespan=lifespan,
)

# CORS: 允许局域网访问 (自托管场景, 放开所有来源)
# 注: allow_credentials=True 与 allow_origins=['*'] 不能共存 (浏览器规范),
# 本项目认证走 header (API Key), 不依赖 cookie, 故关闭 credentials 换取通配来源。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================================================================
# 访问认证中间件
# ================================================================
# 拦截所有 /api/ 请求, 四种状态:
#   1. 有效账号会话          → 放行, 注入 request.state.account_id / role
#   2. 有效单密码应急会话     → 放行, role="admin"(account_id 为 None)
#   3. 未登录 + 公开只读路径  → 放行, role="guest"(受限限流)
#   4. 未登录 + 其它          → 401; 未设密码的面板对公网仍 403(防抢占)
#
# 精确白名单, **刻意不用前缀**: 原实现用 ("/api/auth/",) 前缀, 会把未来任何
# 新增的 /api/auth/* 路由默认公开(它同时也无条件放行了 logout / change-password)。
_AUTH_WHITELIST_EXACT = (
    "/api/auth/status",     # 前端据此决定要不要跳登录页
    "/api/auth/setup",      # 自带本机/内网限制(防公网抢占)
    "/api/auth/login",      # 自带失败限流
    "/api/account/jump",    # 跳转登录: 此刻必然还没有会话
    "/api/account/register",
    "/api/account/login",
    "/health",
)

# 文档端点。它们**不在 /api/ 前缀下**, 会被下面的早退分支无条件放行, 所以必须
# 单独拦一道 —— 否则"把它从白名单移除"是无效操作, 完整路由 schema 仍对匿名开放。
_DOCS_PATHS = ("/openapi.json", "/docs", "/redoc")

# 公开只读: 游客(匿名)可访问的受限子集。精确匹配 + 少量前缀(带路径参数的)。
# 新增路由**不会**自动进入这里, 未分类即落在"需登录"侧(fail-closed)。
_PUBLIC_READ_EXACT = (
    "/api/regime/latest", "/api/regime/states", "/api/regime/history",
    "/api/regime/phases", "/api/regime/coverage", "/api/regime/mainline",
    "/api/market-recap/dragon-tiger", "/api/market-recap/auction-benchmark",
    "/api/data/version", "/api/data/status",
    "/api/intraday/status", "/api/intraday/indices",
    "/api/kline/daily", "/api/kline/daily/latest",
    "/api/kline/minute", "/api/kline/minute-range",
    "/api/kline/instruments/search",
    "/api/stock-analysis/levels",
    "/api/overview/market",
    "/api/sector-rotation",
    "/api/rps/rotation",
    "/api/abnormal/intraday", "/api/abnormal/overview",
    "/api/screener/cached-summary", "/api/screener/strategies",
)
_PUBLIC_READ_PREFIX = ("/api/screener/cached-result/",)
# 唯一的公开 POST: 纯粹的 code→name 批量查询, 无副作用、不打上游。
_PUBLIC_READ_POST = ("/api/kline/instruments/names",)

# 公开只读里**背后是全市场重建**的那批: 缓存 TTL 仅 5s/30s/120s, 匿名流量可低成本
# 反复击穿 → 单独给更紧的额度。全站原本零限流, 这是游客分层引入的新放大面。
_PUBLIC_READ_EXPENSIVE = (
    "/api/overview/market", "/api/sector-rotation", "/api/rps/rotation",
    "/api/screener/cached-summary", "/api/abnormal/intraday",
    "/api/abnormal/overview", "/api/stock-analysis/levels",
)

# 管理员专属端点(精确匹配)。判断依据与完整理由见 _is_admin_only 的 docstring。
_ADMIN_ONLY_EXACT = frozenset({
    "/api/data/clear",
    "/api/strategy/build",
    "/api/strategy/build/stream",
    "/api/strategy/ai/test",
    "/api/strategy/ai/generate",
    "/api/strategy/ai/iterate",
    "/api/strategy/ai/save",
    "/api/strategy/code/validate",
    "/api/strategy/code/save",
    "/api/strategy/composite/save",
    "/api/strategy/reload",
    # 自定义信号的定义端点。信号是**部署级**的(产出共享 enriched 表的 csg_* 列),
    # 且用户提交的是表达式 —— 与策略创作面同类, 故创作侧仅管理员可用。
    # 只读端点(/options、列表)不门控; /intraday/replay 是回放分析, 不改定义。
    "/api/custom-signals",
    "/api/custom-signals/ai/generate",
})
# 带路径参数的端点。**必须按方法分开**:
#   - `^/api/strategy/[^/]+$` 若对 POST 也生效, 会连 `POST /api/strategy/run`
#     一起挡掉 —— 那是普通用户的核心功能(用内置策略跑自己的参数), 不能门控。
#   - DELETE 下它匹配的才是 `DELETE /api/strategy/{id}`(删除策略本体)。
_ADMIN_ONLY_RE_POST = (
    re.compile(r"^/api/strategy/[^/]+/publish$"),
)
_ADMIN_ONLY_RE_DELETE = (
    re.compile(r"^/api/strategy/[^/]+$"),
    re.compile(r"^/api/custom-signals/[^/]+$"),
)

# 游客限流额度(按 IP, 滑动窗口 60s)。普通只读 / 重算类分开计量。
_GUEST_LIMIT_PLAIN = 120
_GUEST_LIMIT_EXPENSIVE = 15
_GUEST_LIMIT_WINDOW_S = 60.0
_guest_hits: dict[str, dict[str, list[float]]] = {}
_guest_lock = threading.Lock()


def _is_public_read(method: str, path: str) -> bool:
    if path in _PUBLIC_READ_POST:
        return method == "POST"
    if method != "GET":
        return False
    if path in _PUBLIC_READ_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_READ_PREFIX)


def _guest_rate_limited(ip: str, path: str) -> bool:
    """游客额度检查(超限返回 True)。两档分开计数, 互不挤占。"""
    bucket = "expensive" if path in _PUBLIC_READ_EXPENSIVE else "plain"
    limit = _GUEST_LIMIT_EXPENSIVE if bucket == "expensive" else _GUEST_LIMIT_PLAIN
    now = time.time()
    with _guest_lock:
        if len(_guest_hits) > 5000:
            _guest_hits.clear()  # 防内存膨胀(粗暴但安全, 只影响限流精度)
        per_ip = _guest_hits.setdefault(ip, {"plain": [], "expensive": []})
        hits = [t for t in per_ip[bucket] if now - t < _GUEST_LIMIT_WINDOW_S]
        if len(hits) >= limit:
            per_ip[bucket] = hits
            return True
        hits.append(now)
        per_ip[bucket] = hits
    return False


def _resolve_identity(request: Request) -> tuple[int | None, str]:
    """解析请求身份 → (account_id, role)。

    两份会话存储彼此独立: 先查多用户账号会话, 再回落单密码应急会话。
    """
    from app.services import account_sessions, accounts, auth as auth_service

    token = request.cookies.get(auth_api.COOKIE_NAME)
    if not token:
        return None, "guest"
    account_id = account_sessions.get_session(token)
    if account_id is not None:
        return account_id, (accounts.get_role(account_id) or "user")
    if auth_service.is_configured() and auth_service.is_valid_session(token):
        # 单密码应急入口: 等价管理员, 但没有账号 id
        return None, "admin"
    return None, "guest"


def _is_admin_only(method: str, path: str) -> bool:
    """管理员专属端点。

    **必须先看清这是干什么用的再往里加东西** —— 这里的判断依据是"该端点的效果
    超出单个账户":

      - `/api/data/clear`: 删光**共享**行情/enriched/financials 与任务表。用户决策
        要求开放注册, 所以任何登录用户都能一次请求毁掉全站数据面。
      - `/api/strategy/{build,ai/*,code/*,composite/*,reload}` 与
        `POST /{id}/publish`、`DELETE /{id}`: 写策略源码, 而源码会被写盘并在
        **服务进程内** import 执行(`strategy/engine.py` spec_from_file_location →
        exec_module)。唯一防护是静态 AST 名单, 面板作者自己在 docstring 里写明
        "不是真正的沙箱"。因此对不可信用户开放该功能等于放弃隔离 —— 用户决策:
        先把自定义 Python 策略对普通用户隐藏, 沙箱化留作后续 P0。

    刻意**不**设为 admin 的: `POST /run`、`/run-all`、`PATCH|DELETE /config/{id}`
    —— 用内置策略跑自己的参数、存自己的覆盖值, 是多用户的核心产品功能。
    """
    if path in _ADMIN_ONLY_EXACT:
        return True
    if method == "POST" and any(r.match(path) for r in _ADMIN_ONLY_RE_POST):
        return True
    if method == "DELETE" and any(r.match(path) for r in _ADMIN_ONLY_RE_DELETE):
        return True
    return False


def _authorize(request: Request, path: str, role: str) -> JSONResponse | None:
    """授权判定。返回 None 表示放行, 否则返回应直接下发的拒绝响应。"""
    # 白名单放行(登录/注册/探活本身不拦)
    if path in _AUTH_WHITELIST_EXACT:
        return None

    if role != "guest":
        # 已登录: 管理员专属端点需 admin 角色。
        # 单密码应急入口等价 admin(见 _resolve_identity), 因此不受影响。
        if role != "admin" and _is_admin_only(request.method, path):
            return JSONResponse(
                status_code=403,
                content={"detail": "需要管理员权限", "code": "ADMIN_REQUIRED"},
            )
        return None

    # 未登录: 公开只读子集放行, 但必须限量
    if _is_public_read(request.method, path):
        ip = auth_api._client_ip(request)
        if _guest_rate_limited(ip, path):
            return JSONResponse(
                status_code=429,
                content={"detail": "访问过于频繁, 请稍后重试", "code": "GUEST_RATE_LIMITED"},
            )
        return None

    from app.services import accounts, auth as auth_service
    # 未**认领**的面板 = 既没有账号、也没有设过密码。此时保留既有「仅本机/内网」
    # 语义, 防公网陌生人抢先设密码。
    # 一旦注册出第一个账号, 面板即视为已认领 —— 之后未登录一律 401, 前端据此
    # 跳登录/注册页。否则已认领的面板会给未登录用户回「请通过 SSH 设置密码」的
    # 403, 前端会显示完全错误的引导。
    if not auth_service.is_configured() and not accounts.has_accounts():
        if auth_api._is_local_network(auth_api._client_ip(request)):
            return None
        return JSONResponse(
            status_code=403,
            content={
                "detail": "面板尚未初始化访问密码,请通过 SSH/本机浏览器访问以设置密码",
                "code": "NOT_INITIALIZED",
            },
        )

    # 未登录: 401(前端跳登录页)
    return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # 文档端点不在 /api/ 下, 必须在早退之前单独拦(否则移除白名单条目毫无作用)
    if path in _DOCS_PATHS:
        _account_id, role = _resolve_identity(request)
        if role == "guest":
            return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
        return await call_next(request)

    # 仅 /api/ 走认证; 静态资源(前端页面/assets)放行, 由前端处理跳转
    if not path.startswith("/api/"):
        return await call_next(request)

    from app.services import preferences, user_paths

    account_id, role = _resolve_identity(request)
    request.state.account_id = account_id
    request.state.role = role

    # 注入每用户偏好上下文 —— 这是请求路径读取/写入每用户偏好键的唯一入口。
    # 无账号(游客/应急入口)时置 None, 此时写每用户键会被 save() 拒绝(fail-closed),
    # 而不是静默写进全局文件被所有账户共享。
    ctx_token = preferences.set_current_user_root(
        user_paths.user_root(account_id) if account_id is not None else None,
    )
    try:
        denial = _authorize(request, path, role)
        if denial is not None:
            return denial
        return await call_next(request)
    finally:
        # 必须复位: contextvar 在同一次请求的并发任务间共享, 泄漏会串到别的请求
        preferences.reset_current_user_root(ctx_token)


# 路由
app.include_router(core_router)
app.include_router(auth_api.router)
app.include_router(account.router)
app.include_router(kline.router)
app.include_router(watchlist.router)
app.include_router(screener.router)
app.include_router(backtest.router)
app.include_router(factors.router)
app.include_router(mining.router)
app.include_router(intraday.router)
app.include_router(indices.router)
app.include_router(overview.router)
app.include_router(paper.router)
app.include_router(abnormal.router)
app.include_router(regime.router)
app.include_router(analysis.router)
app.include_router(pipeline.router)
app.include_router(data.router)
app.include_router(ext_data.router)
app.include_router(financials.router)
app.include_router(stock_analysis.router)
app.include_router(market_recap.router)
app.include_router(settings_api.router)
app.include_router(strategy.router)
app.include_router(signals.router)
app.include_router(monitor_rules.router)
app.include_router(lots.router)
app.include_router(alerts.router)
app.include_router(rps.router)
app.include_router(sector_rotation.router)

# 二次开发路由与小粒度策略在所有核心路由后注册, 禁止覆盖核心路径。
extension_registry, extension_load_errors = configure_backend_extensions(app)
app.state.extension_registry = extension_registry
app.state.extension_load_errors = extension_load_errors


# 能力门控异常 → 403(而非默认 500)
# 业务代码用 capset.require(Cap.X) 断言能力,缺失时抛 CapabilityDenied;
# 若不注册 handler 会冒泡成 500 Internal Server Error,对前端不友好且语义错误。
from fastapi import Request
from fastapi.responses import JSONResponse
from app.tickflow.capabilities import CapabilityDenied


@app.exception_handler(CapabilityDenied)
async def capability_denied_handler(request: Request, exc: CapabilityDenied) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": str(exc), "suggestion": exc.suggestion},
    )

# 生产期静态文件(前端 dist)
_static = Path(settings.static_dir)
if _static.exists():
    if (_static / "assets").exists():
        app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):  # noqa: ARG001
        """所有未匹配路径回退到 index.html — React Router 接管。

        index.html 禁止缓存 (Cache-Control: no-store), 确保浏览器每次拿到
        最新版本引用的 JS/CSS 文件名 (assets 带 hash, 可长缓存)。
        """
        index = _static / "index.html"
        if index.exists():
            return FileResponse(
                index,
                headers={"Cache-Control": "no-store, must-revalidate"},
            )
        return {"error": "frontend not built"}
