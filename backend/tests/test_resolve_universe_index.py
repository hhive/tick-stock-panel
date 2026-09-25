"""_resolve_universe 指数过滤 / 共享管道不读自选 的测试。"""
import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


class _NoBatchCapset:
    """无 batch 能力 → 走 instruments 维表兜底路径。"""

    def has(self, cap):
        return False


def _write_instruments(tmp_path, symbols: list[str]) -> None:
    d = tmp_path / "instruments"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols}).write_parquet(d / "instruments.parquet")


def test_resolve_universe_excludes_index_symbols(repo, monkeypatch, tmp_path):
    """指数不进入股票日K/分钟K同步池(指数走独立 kline_index_* 存储), ETF 刻意保留。

    标的从 instruments 维表进来 —— 共享管道已不再并入自选(见下一个测试), 指数
    若出现在维表里仍必须被过滤掉, 否则会污染 kline_daily/kline_minute。
    """
    _write_instruments(tmp_path, ["600000.SH", "000001.SH", "399001.SZ", "510300.SH"])
    monkeypatch.setattr(daily_pipeline, "get_pool", lambda name, refresh=False: [])
    monkeypatch.setattr(daily_pipeline, "DEMO_SYMBOLS", [])
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: {"000001.SH", "399001.SZ"})

    universe = daily_pipeline._resolve_universe(_NoBatchCapset(), repo)
    assert "600000.SH" in universe
    assert "000001.SH" not in universe
    assert "399001.SZ" not in universe
    assert "510300.SH" in universe  # ETF 不在指数集合里, 保留 (既有行为)


def test_resolve_universe_never_consults_watchlist(repo, monkeypatch, tmp_path):
    """共享行情管道不得读任何账户的自选池。

    自选已按账户隔离(每账户一份), 而 _resolve_universe 是共享管道的标的池解析,
    它没有也不该有「当前账户」概念: 读某个账户的自选会让首个被碰到的账户悄悄决定
    全站同步范围(跨租户泄漏), 且换账户跑结果不可复现。instruments 维表已含全量
    标的, 自选本就是它的子集。
    """
    _write_instruments(tmp_path, ["600000.SH"])
    queried: list[str] = []

    def _tracked_pool(name, refresh=False):
        queried.append(name)
        return ["600519.SH"]

    monkeypatch.setattr(daily_pipeline, "get_pool", _tracked_pool)
    monkeypatch.setattr(daily_pipeline, "DEMO_SYMBOLS", [])
    monkeypatch.setattr(daily_pipeline.settings, "data_dir", tmp_path)

    universe = daily_pipeline._resolve_universe(_NoBatchCapset(), repo)
    assert queried == []  # 一次池都不查
    assert universe == ["600000.SH"]
