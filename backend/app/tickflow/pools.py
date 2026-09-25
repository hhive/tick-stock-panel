"""标的池(Universe)定义(§6.3)。

Phase 1 实现:
  - 常用指数成份(沪深 300 / 中证 500 / 上证 50)用 TickFlow `quote.pool` 端点拉取并缓存
  - 全 A 通过 instruments.batch 获取
  - 自选池 = 用户的 watchlist

归属: 除 ``watchlist`` 外, 所有池都是**共享**行情构件 —— 同一份指数成份/全 A 名单
对每个账户都一样, 缓存文件仍在 ``settings.data_dir/pools/`` 下, 且**不需要**账户上下文。
唯独 ``pool_id == "watchlist"`` 是每账户私有数据(读的是该账户的自选),
必须按 ``user_root`` 解析 (请求路径走 contextvar, 后台线程显式传参), 见
``_load_watchlist``。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Literal

import polars as pl

from app.config import settings
from app.services.user_paths import resolve_user_root
from app.tickflow.client import get_client

logger = logging.getLogger(__name__)

PoolId = Literal["CSI300", "CSI500", "SSE50", "CN_Equity_A", "CN_Index", "watchlist"]

# TickFlow universe id 是它内部命名(见 tf.universes.list())。
# 没有官方对照表,启动时按名称模糊匹配从 universes.list() 里找。
# 常见名:沪深300 / 中证500 / 上证50 / 全 A
_POOL_NAME_HINTS = {
    "CSI300": ["沪深300", "HS300", "CSI300"],
    "CSI500": ["中证500", "ZZ500", "CSI500"],
    "SSE50":  ["上证50",  "SH50", "SSE50"],
}


def _find_universe_id(hints: list[str]) -> str | None:
    """从 universes.list() 里按 name/id 子串匹配找一个 universe id。"""
    try:
        tf = get_client()
        unis = tf.universes.list()
    except Exception as e:  # noqa: BLE001
        logger.warning("universes.list failed: %s", e)
        return None
    for u in unis or []:
        item = u if isinstance(u, dict) else {"id": getattr(u, "id", ""), "name": getattr(u, "name", "")}
        haystack = (item.get("id", "") + " " + item.get("name", "")).lower()
        for h in hints:
            if h.lower() in haystack:
                return item["id"]
    return None


def _pool_cache_path(pool_id: str) -> Path:
    return settings.data_dir / "pools" / f"{pool_id}.parquet"


def get_pool(
    pool_id: PoolId, refresh: bool = False, user_root: Path | None = None,
) -> list[str]:
    """返回标的池里的 symbol 列表。

    ``user_root`` 仅对每账户私有的 ``"watchlist"`` 池有意义; 其余池是共享行情构件,
    读取它们**不会**解析账户根目录 —— 后台调度器拉指数成份/全 A 时无需账户上下文。
    """
    if pool_id == "watchlist":
        return _load_watchlist(user_root)

    cache = _pool_cache_path(pool_id)
    if cache.exists() and not refresh:
        df = pl.read_parquet(cache)
        return df["symbol"].to_list()

    symbols = _fetch_pool(pool_id)
    if symbols:
        from app.services.fs_utils import atomic_write_parquet

        cache.parent.mkdir(parents=True, exist_ok=True)
        # 原子写: 半截缓存会让上面的 read_parquet 每次都抛错, 不会自愈
        atomic_write_parquet(pl.DataFrame({"symbol": symbols, "as_of": [date.today()] * len(symbols)}), cache)
    return symbols


def _fetch_pool(pool_id: PoolId) -> list[str]:
    """从 TickFlow 拉取池成份。

    实现:先用 universes.list 找到 universe id,再 quotes.get_by_universes 拉成份。
    """
    tf = get_client()

    if pool_id in _POOL_NAME_HINTS:
        uid = _find_universe_id(_POOL_NAME_HINTS[pool_id])
        if not uid:
            logger.warning("无法在 TickFlow universes 列表里匹配到 %s", pool_id)
            return []
        try:
            df = tf.quotes.get_by_universes([uid], as_dataframe=True)
            if df is not None and len(df) > 0 and "symbol" in df.columns:
                return df["symbol"].astype(str).tolist()
        except Exception as e:  # noqa: BLE001
            logger.warning("fetch pool %s via universe %s failed: %s", pool_id, uid, e)

    if pool_id == "CN_Equity_A":
        # 全 A — 优先直接用 CN_Equity_A universe (包含沪深京三市)
        uid = _find_universe_id(["CN_Equity_A", "沪深京A股", "全A"])
        if uid:
            try:
                df = tf.quotes.get_by_universes([uid], as_dataframe=True)
                if df is not None and len(df) > 0 and "symbol" in df.columns:
                    return sorted(set(df["symbol"].astype(str).tolist()))
            except Exception as e:  # noqa: BLE001
                logger.warning("fetch CN_Equity_A via universe %s failed: %s", uid, e)

        # fallback: 聚合申万一级行业 (覆盖度较低, 缺北交所/新股)
        try:
            unis = tf.universes.list()
        except Exception as e:  # noqa: BLE001
            logger.warning("universes.list failed: %s", e)
            unis = []
        sw1_ids = []
        for u in unis or []:
            item = u if isinstance(u, dict) else {"id": getattr(u, "id", "")}
            uid = item.get("id", "")
            if "SW1_" in uid:
                sw1_ids.append(uid)
        if sw1_ids:
            try:
                df = tf.quotes.get_by_universes(sw1_ids, as_dataframe=True)
                if df is not None and "symbol" in df.columns:
                    return sorted(set(df["symbol"].astype(str).tolist()))
            except Exception as e:  # noqa: BLE001
                logger.warning("aggregate SW1 fetch failed: %s", e)

    if pool_id == "CN_Index":
        uid = _find_universe_id(["CN_Index", "沪深指数", "指数"])
        ids = [uid] if uid else ["CN_Index"]
        try:
            df = tf.quotes.get_by_universes(ids, as_dataframe=True)
            if df is not None and len(df) > 0 and "symbol" in df.columns:
                return sorted(set(df["symbol"].astype(str).tolist()))
        except Exception as e:  # noqa: BLE001
            logger.warning("fetch CN_Index via universe %s failed: %s", ids, e)

    return []


def _load_watchlist(user_root: Path | None = None) -> list[str]:
    """读取**当前账户**的自选(由 watchlist service 维护)。

    这里直接读文件而不调 watchlist service: 池解析只关心 symbol 列, 不校验分组,
    也不希望首次读池就创建目录骨架。路径口径与 ``watchlist._path`` 保持一致
    (``<user_root>/user_data/watchlist.parquet``)。
    """
    path = resolve_user_root(user_root) / "user_data" / "watchlist.parquet"
    if not path.exists():
        return []
    df = pl.read_parquet(path)
    if df.is_empty() or "symbol" not in df.columns:
        return []
    return df["symbol"].to_list()


# 兜底:Free 用户/无 API 时给一个小型可用集合,让 UI 不至于空白
DEMO_SYMBOLS = [
    "600000.SH",  # 浦发银行
    "600036.SH",  # 招商银行
    "600519.SH",  # 贵州茅台
    "601318.SH",  # 中国平安
    "601398.SH",  # 工商银行
    "000001.SZ",  # 平安银行
    "000333.SZ",  # 美的集团
    "000651.SZ",  # 格力电器
    "000858.SZ",  # 五粮液
    "002594.SZ",  # 比亚迪
]
