"""TickFlow SDK 封装(§5)。

进程内单例;Key 来源(优先级):secrets.json > .env。
用户改 Key 后需要 `reset_clients()`,然后 `get_client()` 会拿新的。

5 档体系下服务器归属:
  - none 档(无 key / 无效 key) → TickFlow.free()(free-api 服务器)
  - free 档(免费有效 key)      → TickFlow.free()(key 被 SDK 忽略,运行时走 free-api)
  - starter/pro/expert(付费 key) → TickFlow(api_key=key, base_url)
"""
from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict

from tickflow import AsyncTickFlow, TickFlow

from app import secrets_store

logger = logging.getLogger(__name__)

# SDK 默认超时配置 (见 tickflow/_base_client.py): timeout=30s, max_retries=3。
# 单次请求最坏 4×30s + 退避 ≈ 127s。日志中标注此值, 便于在卡死时对照耗时。

_sync_client: TickFlow | None = None
_paid_realtime_client: TickFlow | None = None

# 异步客户端**按 key 缓存** —— 不能是进程级单例。
#
# 请求路径上不同账户带着**不同的 key**(用户在设置页填了自己的数据源 key), 共用一个
# 实例等于 A 的 key 被 B 的请求用上 —— 那是凭据串号, 不是性能取舍。用 LRU + TTL
# 兜住内存与连接数: 上限够覆盖同时在线的账户数, 空闲 15 分钟释放连接池。
_ASYNC_CLIENT_CACHE_MAX = 8
_ASYNC_CLIENT_TTL_S = 900.0
_async_clients: OrderedDict[str, tuple[float, AsyncTickFlow]] = OrderedDict()


# ===== 服务器归属判定 =====

# free-api 服务器默认节点(SDK 默认值),none/free 档运行时走这里。
FREE_ENDPOINT = "https://free-api.tickflow.org"
# 付费端点默认节点(starter+ 运行时走这里,也是端点切换的默认值)。
PAID_ENDPOINT = "https://api.tickflow.org"


def _should_use_free_server() -> bool:
    """是否应走 free-api 服务器。

    判定依据:无 key,或当前档位为 none/free。
    付费档(starter+)走付费端点。
    """
    if not secrets_store.get_tickflow_key():
        return True
    # 有 key 时按探测出的档位判定(避免读 capabilities.json 在首次启动前未生成的边界)
    from app.tickflow.policy import base_tier_name
    return base_tier_name() in ("none", "free")


def _base_url() -> str | None:
    """读部署级自定义端点, 没有则返回 None(用 SDK 默认)。

    必须走部署级凭据: 本函数在后台线程/子进程里被行情取数路径调用, 那里没有
    账户上下文, 走每用户 secrets.json 会抛 MissingUserContextError。
    """
    return secrets_store.get_deployment("tickflow_base_url") or None


def get_client() -> TickFlow:
    """同步客户端。能力探测、盘后管道用。"""
    global _sync_client
    if _sync_client is None:
        key = secrets_store.get_tickflow_key()
        if _should_use_free_server():
            # none/free 档:走 free-api 服务器(无 key 或免费 key 被 SDK 忽略)
            _sync_client = TickFlow.free()
            logger.info("创建同步 SDK 客户端 (free-api, SDK超时=30s×重试3)")
        else:
            _sync_client = TickFlow(api_key=key, base_url=_base_url())
            logger.info("创建同步 SDK 客户端 (付费端点=%s, SDK超时=30s×重试3)", current_endpoint())
    return _sync_client


def get_async_client() -> AsyncTickFlow:
    """异步客户端。FastAPI 请求路径上用。

    **按 key 缓存**(不是单例): 每个账户可能带着自己的数据源 key, 见模块顶部的说明。
    缓存键含 base_url —— 同一把 key 配不同端点也是两条链路。
    """
    key = secrets_store.get_tickflow_key()
    free = _should_use_free_server()
    cache_key = "free" if free else f"{key}|{_base_url() or ''}"

    now = time.monotonic()
    hit = _async_clients.get(cache_key)
    if hit is not None:
        created_at, cached = hit
        if now - created_at < _ASYNC_CLIENT_TTL_S:
            _async_clients.move_to_end(cache_key)
            return cached
        del _async_clients[cache_key]  # 空闲超时: 释放连接池

    if free:
        client = AsyncTickFlow.free()
        logger.info("创建异步 SDK 客户端 (free-api, SDK超时=30s×重试3)")
    else:
        client = AsyncTickFlow(api_key=key, base_url=_base_url())
        logger.info("创建异步 SDK 客户端 (付费端点=%s, SDK超时=30s×重试3)", current_endpoint())

    _async_clients[cache_key] = (now, client)
    while len(_async_clients) > _ASYNC_CLIENT_CACHE_MAX:
        _async_clients.popitem(last=False)
    return client


def get_paid_realtime_client() -> TickFlow | None:
    """实时行情专用付费服务器客户端。

    none/free 的历史日K仍走 get_client() 的 free-api；实时行情全部走付费服务器。
    Free 档如果有有效 key，也使用这里的 paid endpoint 调按标的实时接口。
    """
    global _paid_realtime_client
    key = secrets_store.get_tickflow_key()
    if not key:
        return None
    if _paid_realtime_client is None:
        _paid_realtime_client = TickFlow(api_key=key, base_url=_base_url())
        logger.info("创建实时行情 SDK 客户端 (付费端点=%s, SDK超时=30s×重试3)", current_endpoint())
    return _paid_realtime_client


def reset_clients() -> None:
    """Key 变化后调用 — 让下一次 get_client() 拿新实例。"""
    global _sync_client, _paid_realtime_client
    _sync_client = None
    _paid_realtime_client = None
    # 异步侧是按 key 缓存的, key 变了缓存键自然不同; 但清空能立刻释放旧连接池
    _async_clients.clear()


def current_mode() -> str:
    """供 UI 显示当前模式。三态:

    - "none"    : 无 key / 无效 key(走 free-api,仅历史日K)
    - "free"    : 免费有效 key(走 free-api,仅历史日K)
    - "api_key" : 付费 key(starter+,走付费端点,有实时行情)
    """
    if not secrets_store.get_tickflow_key():
        return "none"
    from app.tickflow.policy import base_tier_name
    tier = base_tier_name()
    if tier in ("none", "free"):
        return "free" if tier == "free" else "none"
    return "api_key"


def current_endpoint() -> str:
    """返回当前显示用的端点 URL(对应 endpoints.json 列表项)。

    - none/free 档:显示 free-api 服务器节点
    - 付费档:显示用户自定义端点(测速切换后)或默认付费节点 api.tickflow.org
    """
    if _should_use_free_server():
        return FREE_ENDPOINT
    # 自定义端点(付费模式测速切换后):优先返回
    base = _base_url()
    if base:
        return base.rstrip("/")
    return PAID_ENDPOINT
