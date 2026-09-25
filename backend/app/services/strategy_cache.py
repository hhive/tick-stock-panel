"""策略结果缓存 — 写入本地文件，供策略页面秒加载。

缓存结构:
  {
    "as_of": "2024-01-15",
    "results": { strategy_id: { total, as_of, rows } },
    "today_ever_matched": { strategy_id: [symbol, ...] },    // 今日曾命中 symbol 并集
    "today_ever_rows": { strategy_id: { symbol: row_data } },// 今日曾命中的完整行数据
    "updated_at": 1705324800000  # Unix ms
  }

文件路径: ``<user_root>/user_data/strategy_cache.json`` —— **每账户一份**。
user_root 由 ``user_paths.resolve_user_root()`` 解析 (请求路径走认证中间件注入的
contextvar, 后台线程/worker 必须显式传 ``user_root=``)。

缓存键是"文件路径"而不是策略 ID, 所以账户隔离完全落在路径上: 一旦 user_root 分家,
A 的缓存就不可能被 B 读到。唯独 ``enriched_mtime`` 这一字段读的是**共享行情数据**
(``kline_daily_enriched``), 与账户无关, 故仍取 ``settings.data_dir``。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.services.user_paths import resolve_user_root


def _json_default(obj: Any) -> Any:
    """处理 date/datetime 等 JSON 不认识的类型。"""
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


logger = logging.getLogger(__name__)

_CACHE_FILENAME = "strategy_cache.json"

# 读写同一 JSON 文件的进程内锁: write_cache 的 read-modify-write 与并发 read_cache
# 无锁会丢更新/读到半写文件。read_cache 与 write_cache 共用此锁; write 内部复用
# _read_cache_unlocked 避免自死锁。写入用临时文件 + os.replace 做到原子替换。
_file_lock = threading.Lock()


def _cache_path(user_root: Path) -> Path:
    return user_root / "user_data" / _CACHE_FILENAME


def _enriched_parquet_path(as_of: str) -> Path:
    """返回 enriched parquet 文件路径 (**共享行情数据**, 不随账户变化)。"""
    from app.config import settings

    return settings.data_dir / "kline_daily_enriched" / f"date={as_of}" / "part.parquet"


def _get_enriched_mtime(as_of: str) -> float | None:
    """返回 enriched parquet 文件的 mtime (秒)。文件不存在返回 None。"""
    p = _enriched_parquet_path(as_of)
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


def read_cache(user_root: Path | None = None) -> dict | None:
    """读取**当前账户**的策略缓存文件。返回 None 表示无缓存或读取失败。

    说明: 原先有 enriched mtime 过期校验 (数据文件变化 → 判过期返回 None),
    但在有实时行情的系统里, enriched parquet 每轮被刷新 → mtime 必然变化 →
    缓存被永久判死, 策略页读不到数据。且判过期后不触发重算, 只能让用户手动重跑,
    保护价值有限。故移除: 盘后缓存总能读出, 实时新鲜度由 /api/screener/cached
    端点叠加监控引擎的内存实时结果 (latest_strategy_results) 来保证。
    """
    with _file_lock:
        return _read_cache_unlocked(resolve_user_root(user_root))


def clear_all_accounts() -> int:
    """清除**所有账户**的策略结果缓存, 返回实际清理的账户数。

    用于"**共享**数据变更"的场景 —— 例如扩展数据(概念/行业)变更: 它喂的是所有人
    共用的计算, 所以每个账户基于它算出的策略结果都已过期。此时只清"当前账户"是
    **不够的**: 其它账户会继续展示旧口径结果, 且不会有任何提示, 属于静默的错误结论。

    开销: 进程级遍历账号注册表 + 逐账户写盘, 只在低频路径(配置变更)调用。
    """
    from app.services import user_paths

    cleared = 0
    for _account_id, root in user_paths.iter_user_roots():
        try:
            clear_cache(root)
            cleared += 1
        except Exception as e:  # noqa: BLE001
            # 单个账户失败不中断整轮扇出 —— 否则一个坏账号会让其余账户永远留旧缓存
            logger.warning("fan-out clear_cache 失败 root=%s: %s", root, e)
    return cleared


def clear_cache(user_root: Path | None = None) -> None:
    """删除**当前账户**的策略结果缓存；策略代码 reload 后避免继续展示旧公式结果。"""
    import traceback

    # 运维可见性: 策略页依赖本缓存秒加载, 被清空即整页回退到全量重算。
    # 记录调用链 (最近 5 帧), 排查"缓存莫名消失"类问题不需要复现现场。
    frames = traceback.extract_stack()[:-1]
    chain = " <- ".join(
        f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in frames[-5:]
    )
    logger.warning("策略缓存被清除, 调用链: %s", chain)
    path = _cache_path(resolve_user_root(user_root))
    with _file_lock:
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".tmp").unlink(missing_ok=True)


def _read_cache_unlocked(resolved_root: Path) -> dict | None:
    """实际读取逻辑 (不持锁, 且已解析过的账户根)。供 read_cache 与 write_cache 复用, 避免重入死锁。"""
    path = _cache_path(resolved_root)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        cached = json.loads(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取策略缓存失败: %s", e)
        return None

    return cached


def _rows_to_symbol_map(rows: list[dict]) -> dict[str, dict]:
    """将 rows 列表转为 {symbol: row_data} 映射。"""
    result: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol")
        if sym:
            result[sym] = row
    return result


def write_cache(
    as_of: str,
    results: dict[str, Any],
    user_root: Path | None = None,
) -> None:
    """将策略结果写入**当前账户**的缓存文件，同时更新今日曾命中集合。

    - 日期变更时重置 today_ever_matched 和 today_ever_rows
    - 同一天内合并 (并集) 之前曾命中的 symbol，并用最新行数据更新
    """
    # 解析一次并向下传, 避免同一写路径里两次解析拿到不同账户 (contextvar 理论上是稳定的,
    # 但显式传下去也省掉重复解析)。
    resolved_root = resolve_user_root(user_root)
    path = _cache_path(resolved_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 整个 read-modify-write 持锁: 避免并发 write 丢更新, 也避免与 read_cache 撕裂
    with _file_lock:
        _write_cache_locked(path, resolved_root, as_of, results)


def _write_cache_locked(
    path: Path,
    resolved_root: Path,
    as_of: str,
    results: dict[str, Any],
) -> None:
    """持 _file_lock 后的实际写入逻辑 (read-merge-write + 原子替换)。"""
    # 读取旧缓存 (已持锁, 走不重入的 _read_cache_unlocked)
    old = _read_cache_unlocked(resolved_root)
    old_as_of = old.get("as_of") if old else None
    old_ever_rows: dict[str, dict[str, dict]] = old.get("today_ever_rows", {}) if old else {}

    if old_as_of == as_of:
        merged_results = {**(old.get("results") or {}), **results}
    else:
        merged_results = results

    # 当前命中的行数据 → symbol 映射
    current_row_maps: dict[str, dict[str, dict]] = {}
    for sid, r in results.items():
        current_row_maps[sid] = _rows_to_symbol_map(r.get("rows", []))

    if old_as_of and old_as_of == as_of and old_ever_rows:
        # 同一天: 合并 — 用当前行数据更新旧数据 (保持最新价格等)
        merged_rows: dict[str, dict[str, dict]] = {}
        all_keys = set(old_ever_rows.keys()) | set(current_row_maps.keys())
        for sid in all_keys:
            old_map = old_ever_rows.get(sid, {})
            cur_map = current_row_maps.get(sid, {})
            # 以旧数据为基础，用当前数据覆盖 (当前数据更新鲜)
            combined = {**old_map, **cur_map}
            merged_rows[sid] = combined
        today_ever_rows = merged_rows
    else:
        # 新的一天或首次写入
        today_ever_rows = current_row_maps

    # 从 ever_rows 提取 symbol 列表 (用于快速计数)
    today_ever_matched = {sid: sorted(maps.keys()) for sid, maps in today_ever_rows.items()}

    # enriched_mtime: 盘后缓存写入时记录 (向后兼容旧字段)。read_cache 已不再用它
    # 做过期校验, 实时新鲜度改由 /cached 端点叠加监控引擎内存结果保证。
    enriched_mtime = _get_enriched_mtime(as_of)

    payload = {
        "as_of": as_of,
        "results": merged_results,
        "today_ever_matched": today_ever_matched,
        "today_ever_rows": today_ever_rows,
        "enriched_mtime": enriched_mtime,
        "updated_at": int(time.time() * 1000),
    }
    try:
        # 原子写: 先写临时文件再 os.replace, 避免读侧读到半写的 JSON
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default), encoding="utf-8")
        os.replace(tmp, path)
        total_rows = sum(len(r.get("rows", [])) for r in merged_results.values())
        total_ever = sum(len(v) for v in today_ever_matched.values())
        logger.info("策略缓存已写入: %s, %d 策略, %d 命中, %d 曾命中", as_of, len(merged_results), total_rows, total_ever)
    except Exception as e:  # noqa: BLE001
        logger.warning("写入策略缓存失败: %s", e)
