"""domain 2 存储的每账户隔离 — 策略私有数据不得跨账户互见。

覆盖的存储 (全部按 ``<user_root>/…`` 落盘):
  1. 策略参数覆写   app/strategy/config.py
  2. 策略结果缓存   app/services/strategy_cache.py
  3. 策略运行耗时   app/services/strategy_run_queue.py
  4. 自定义信号     app/strategy/custom_signals.py
  5. 回测结果       app/services/backtest.py
  6. 回测候选池     app/backtest/candidates.py
  7. 挖掘运行       app/services/mining_jobs.py
  8. 策略源码目录   app/api/strategy.py

断言一律走**真实存储函数**(不是断言路径字符串), 否则"路径对了但读的时候没按账户读"
这类缺陷照样漏网。三条固定负例对每个存储都成立:
  - 无上下文且不传 user_root → MissingUserContextError (fail-closed, 不回退共享目录);
  - 显式 user_root 压过上下文;
  - 新账户起步为空 (不继承策略/回测结果/缓存/信号)。
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config as app_config
from app.api import strategy as strategy_api
from app.backtest.candidates import CandidateStore
from app.services import preferences, strategy_cache, strategy_run_queue, user_paths
from app.services.backtest import BacktestResult, BacktestService
from app.services.mining_jobs import MiningRunStore
from app.services.user_paths import MissingUserContextError
from app.strategy import config as strategy_config
from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """独立 DATA_DIR, 且默认**无**账户上下文 (用例自己决定要不要注入)。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    _clear_in_process_caches()
    token = preferences.set_current_user_root(None)
    yield tmp_path
    preferences.reset_current_user_root(token)
    _clear_in_process_caches()


def _clear_in_process_caches() -> None:
    """模块级缓存是进程级状态, 不清会跨用例串味。"""
    strategy_config._override_cache.clear()
    strategy_config._override_cache_sig.clear()
    custom_signals.invalidate_intraday_cache()


@contextlib.contextmanager
def _as_account(account_id: int):
    """以某个面板账号的身份执行 (认证中间件在真实请求里做的就是这件事)。"""
    root = user_paths.user_root(account_id)
    token = preferences.set_current_user_root(root)
    try:
        yield root
    finally:
        preferences.reset_current_user_root(token)


def _data_dir(account_id: int) -> Path:
    return user_paths.user_root(account_id) / "user_data"


# ================================================================
# fail-closed: 无上下文 + 无显式 user_root
# ================================================================

def _backtest_persist_without_context() -> None:
    BacktestService(SimpleNamespace())._persist(
        BacktestResult(
            run_id="r", config={}, stats={}, equity_curve=[], trades=[], per_symbol_stats=[],
        )
    )


_NO_CONTEXT_CALLS = {
    "策略覆写.load": lambda: strategy_config.load_override("s1"),
    "策略覆写.save": lambda: strategy_config.save_override("s1", {}),
    "策略覆写.list": lambda: strategy_config.list_overrides(),
    "策略覆写.delete": lambda: strategy_config.delete_override("s1"),
    "结果缓存.read": lambda: strategy_cache.read_cache(),
    "结果缓存.write": lambda: strategy_cache.write_cache("2026-01-05", {}),
    "结果缓存.clear": lambda: strategy_cache.clear_cache(),
    "运行耗时.load": lambda: strategy_run_queue.load_run_timings(),
    "运行耗时.record": lambda: strategy_run_queue.record_run_timings({"s1": 1.0}),
    "自定义信号.load_all": lambda: custom_signals.load_all(),
    "自定义信号.load_intraday_all": lambda: custom_signals.load_intraday_all(),
    "自定义信号.signal_names": lambda: custom_signals.signal_names(),
    "自定义信号.save": lambda: custom_signals.save_one({"id": "sig"}),
    "自定义信号.delete": lambda: custom_signals.delete_one("sig"),
    "候选池.list": lambda: CandidateStore().list(),
    "候选池.create": lambda: CandidateStore().create(
        kind="factor", name="候选", source_id="rsi_14",
        config={"factor_name": "rsi_14"}, metrics={"ic_mean": 0.03}, data_as_of=None,
    ),
    "挖掘运行.list": lambda: MiningRunStore().list_runs(),
    "挖掘运行.create": lambda: MiningRunStore().create(
        {"symbols": ["000001.SZ"]}, {"daily_generation": 1}, run_id="r1",
    ),
    "回测结果.persist": _backtest_persist_without_context,
}


@pytest.mark.parametrize("name", sorted(_NO_CONTEXT_CALLS))
def test_store_fails_closed_without_user_context(_isolated, name):
    """没有账户上下文时必须报错, **不得**静默写/读共享目录。

    静默回退有两条严重后果, 都比报错更糟: A 的写入覆盖 B 的; 后台读到别人的数据。
    """
    with pytest.raises(MissingUserContextError):
        _NO_CONTEXT_CALLS[name]()


def test_http_user_root_helper_fails_closed(_isolated):
    """HTTP 层的账户根解析同样 fail-closed (handler 不该回退到共享目录)。"""
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=SimpleNamespace(store=SimpleNamespace(data_dir=_isolated)),
            )
        )
    )
    with pytest.raises(MissingUserContextError):
        strategy_api._user_root(request)


# ================================================================
# 1) 策略参数覆写
# ================================================================

def test_strategy_overrides_are_isolated_between_accounts(_isolated):
    with _as_account(1):
        strategy_config.save_override("s1", {"params": {"period": 20}})
        assert strategy_config.load_override("s1")["params"]["period"] == 20
        assert strategy_config.list_overrides() == {"s1": {"params": {"period": 20}}}

    with _as_account(2):
        # 新账户起步为空: 读不到 A 的覆写, 列表里也没有
        assert strategy_config.load_override("s1") == {}
        assert strategy_config.list_overrides() == {}

    assert (_data_dir(1) / "strategy_overrides" / "s1.json").exists()
    assert not (_data_dir(2) / "strategy_overrides" / "s1.json").exists()

    # A 的覆写仍在 (B 的读取没有把它清掉/改写)
    with _as_account(1):
        assert strategy_config.load_override("s1")["params"]["period"] == 20


def test_strategy_override_explicit_user_root_beats_context(_isolated):
    """后台线程用显式 user_root —— 必须压过请求上下文。"""
    with _as_account(1):
        strategy_config.save_override("s1", {"params": {"from": "ctx"}}, user_root=user_paths.user_root(2))
        # 上下文账户没被写; 显式账户写到了
        assert strategy_config.load_override("s1") == {}
    assert strategy_config.load_override("s1", user_root=user_paths.user_root(2))["params"]["from"] == "ctx"


def test_delete_override_only_touches_own_account(_isolated):
    for account_id in (1, 2):
        with _as_account(account_id):
            strategy_config.save_override("s1", {"params": {"account": account_id}})

    with _as_account(1):
        strategy_config.delete_override("s1")
        assert strategy_config.load_override("s1") == {}

    assert strategy_config.load_override("s1", user_root=user_paths.user_root(2))["params"]["account"] == 2


# ================================================================
# 2) 策略结果缓存 (最高风险: 缓存键是文件路径)
# ================================================================

def test_strategy_cache_never_returns_a_result_to_b(_isolated):
    """缓存键包含账户根, 所以 A 算出的结果绝不能被 B 读到。

    这是最容易静默串号的一处: 缓存没有归属字段, 隔离完全落在路径上。
    """
    with _as_account(1):
        strategy_cache.write_cache(
            "2026-01-05",
            {"s1": {"total": 3, "as_of": "2026-01-05", "rows": [{"symbol": "000001.SZ"}]}},
        )
        assert strategy_cache.read_cache()["results"]["s1"]["total"] == 3

    with _as_account(2):
        # 新账户: 没有继承任何缓存
        assert strategy_cache.read_cache() is None
        strategy_cache.write_cache(
            "2026-01-05",
            {"s2": {"total": 9, "as_of": "2026-01-05", "rows": [{"symbol": "600000.SH"}]}},
        )
        assert set(strategy_cache.read_cache()["results"]) == {"s2"}

    with _as_account(1):
        cached = strategy_cache.read_cache()
        assert set(cached["results"]) == {"s1"}
        assert cached["results"]["s1"]["total"] == 3

    assert (_data_dir(1) / "strategy_cache.json").exists()
    assert (_data_dir(2) / "strategy_cache.json").exists()


def test_strategy_cache_clear_only_clears_own_account(_isolated):
    for account_id in (1, 2):
        with _as_account(account_id):
            strategy_cache.write_cache("2026-01-05", {f"s{account_id}": {"total": account_id}})

    with _as_account(1):
        strategy_cache.clear_cache()
        assert strategy_cache.read_cache() is None

    with _as_account(2):
        assert strategy_cache.read_cache()["results"]["s2"]["total"] == 2


def test_strategy_cache_explicit_user_root_beats_context(_isolated):
    strategy_cache.write_cache("2026-01-05", {"s2": {"total": 2}}, user_root=user_paths.user_root(2))
    with _as_account(1):
        assert strategy_cache.read_cache() is None
        explicit = strategy_cache.read_cache(user_root=user_paths.user_root(2))
        assert explicit["results"]["s2"]["total"] == 2


# ================================================================
# 3) 策略运行耗时
# ================================================================

def test_strategy_run_timings_are_isolated_between_accounts(_isolated):
    with _as_account(1):
        strategy_run_queue.record_run_timings({"s1": 120.0})
        assert strategy_run_queue.load_run_timings() == {"s1": 120.0}

    with _as_account(2):
        # 新账户起步为空
        assert strategy_run_queue.load_run_timings() == {}
        strategy_run_queue.record_run_timings({"s2": 30.0, "s1": 5.0})
        assert strategy_run_queue.load_run_timings() == {"s2": 30.0, "s1": 5.0}

    with _as_account(1):
        # A 的耗时没有被 B 的同名策略覆盖 (s1 仍是 A 的 120ms, 不是 B 的 5ms)
        assert strategy_run_queue.load_run_timings() == {"s1": 120.0}


def test_strategy_run_timings_explicit_user_root_beats_context(_isolated):
    with _as_account(1):
        strategy_run_queue.record_run_timings({"s9": 7.0}, user_root=user_paths.user_root(2))
        assert strategy_run_queue.load_run_timings() == {}
    assert strategy_run_queue.load_run_timings(user_root=user_paths.user_root(2)) == {"s9": 7.0}


# ================================================================
# 4) 自定义信号 (含两处按账户根作键的指纹缓存)
# ================================================================

def _intraday_signal(signal_id: str, name: str) -> dict:
    return {
        "id": signal_id,
        "name": name,
        "timeframe": custom_signals.TIMEFRAME_INTRADAY,
        "kind": "entry",
        "enabled": True,
        "conditions": [{"left": "close", "op": ">", "right": "10"}],
    }


def test_custom_signals_are_isolated_between_accounts(_isolated):
    with _as_account(1):
        custom_signals.save_one({"id": "sig_a", "name": "A的信号"})
        assert [s["id"] for s in custom_signals.load_all()] == ["sig_a"]

    with _as_account(2):
        # 新账户起始为空, 且删不掉/改不了 A 的信号
        assert custom_signals.load_all() == []
        assert custom_signals.delete_one("sig_a") is False

    with _as_account(1):
        assert [s["id"] for s in custom_signals.load_all()] == ["sig_a"]


def test_custom_signal_fingerprint_caches_are_keyed_per_account(_isolated):
    """盘中定义/命名两处缓存是按**解析后的账户根**作键。

    键若用了入参 (contextvar 路径下是 None), 所有账户会共用同一个键 ——
    同一个进程内 B 就会命中 A 的缓存。这里刻意在同一进程里连续切换账户,
    不失效缓存: A 先写入并让缓存热起来, B 读到的必须是空。
    """
    with _as_account(1):
        custom_signals.save_one(_intraday_signal("sig_a", "A的信号"))
        assert [s["id"] for s in custom_signals.load_intraday_all()] == ["sig_a"]
        names_a = custom_signals.signal_names()
        assert names_a[custom_signals.column_name("sig_a")] == "A的信号"

    with _as_account(2):
        assert custom_signals.load_intraday_all() == []
        assert custom_signals.signal_names() == {}

    with _as_account(1):
        assert [s["id"] for s in custom_signals.load_intraday_all()] == ["sig_a"]
        assert custom_signals.signal_names() == names_a


def test_custom_signals_explicit_user_root_beats_context(_isolated):
    with _as_account(1):
        custom_signals.save_one({"id": "sig_b"}, user_root=user_paths.user_root(2))
        assert custom_signals.load_all() == []
    assert [s["id"] for s in custom_signals.load_all(user_root=user_paths.user_root(2))] == ["sig_b"]


# ================================================================
# 5) 回测结果
# ================================================================

def _persist(account_id: int, run_id: str, *, explicit: bool) -> None:
    user_root = user_paths.user_root(account_id) if explicit else None
    result = BacktestResult(
        run_id=run_id, config={}, stats={"total_return": 0.1},
        equity_curve=[], trades=[], per_symbol_stats=[],
    )
    BacktestService(SimpleNamespace(), user_root=user_root)._persist(result)


def test_backtest_results_are_isolated_between_accounts(_isolated):
    with _as_account(1):
        _persist(1, "aaa", explicit=False)      # 走请求上下文
    _persist(2, "bbb", explicit=True)           # 走显式 user_root (后台线程那条路)

    assert (_isolated / "users" / "1" / "backtest_results" / "run_id=aaa.parquet").exists()
    assert (_isolated / "users" / "2" / "backtest_results" / "run_id=bbb.parquet").exists()
    # 互不可见: 没有落到对方目录, 也没有落到共享 data_dir/backtest_results
    assert not (_isolated / "users" / "1" / "backtest_results" / "run_id=bbb.parquet").exists()
    assert not (_isolated / "users" / "2" / "backtest_results" / "run_id=aaa.parquet").exists()
    assert not (_isolated / "backtest_results").exists()


def test_backtest_result_explicit_user_root_beats_context(_isolated):
    with _as_account(1):
        _persist(2, "ccc", explicit=True)
    assert (_isolated / "users" / "2" / "backtest_results" / "run_id=ccc.parquet").exists()
    assert not (_isolated / "users" / "1" / "backtest_results" / "run_id=ccc.parquet").exists()


# ================================================================
# 6) 回测候选池
# ================================================================

def _create_candidate(store: CandidateStore, name: str) -> dict:
    return store.create(
        kind="factor",
        name=name,
        source_id="rsi_14",
        config={"factor_name": "rsi_14"},
        metrics={"ic_mean": 0.03},
        data_as_of="2026-08-11",
    )


def test_candidate_pool_is_isolated_between_accounts(_isolated):
    with _as_account(1):
        created = _create_candidate(CandidateStore(), "A的候选")
        assert [item["id"] for item in CandidateStore().list()] == [created["id"]]

    with _as_account(2):
        # 新账户起步为空, 也删不掉 A 的候选 (不在自己的池里)
        assert CandidateStore().list() == []
        with pytest.raises(KeyError):
            CandidateStore().delete(created["id"])

    with _as_account(1):
        assert [item["name"] for item in CandidateStore().list()] == ["A的候选"]

    assert not (_data_dir(2) / "research_candidates.json").exists()


def test_candidate_pool_explicit_user_root_beats_context(_isolated):
    _create_candidate(CandidateStore(user_root=user_paths.user_root(2)), "B的候选")
    with _as_account(1):
        assert CandidateStore().list() == []
    assert len(CandidateStore(user_root=user_paths.user_root(2)).list()) == 1


# ================================================================
# 7) 挖掘运行
# ================================================================

def test_mining_runs_are_isolated_between_accounts(_isolated):
    with _as_account(1):
        store_a = MiningRunStore()
        store_a.create({"symbols": ["000001.SZ"]}, {"daily_generation": 1}, run_id="run_a")
        assert [m["run_id"] for m in store_a.list_runs()] == ["run_a"]

    with _as_account(2):
        store_b = MiningRunStore()
        assert store_b.list_runs() == []
        assert store_b.get("run_a") is None

    assert (_isolated / "users" / "1" / "research" / "mining" / "runs" / "run_a").is_dir()
    assert not (_isolated / "users" / "2" / "research" / "mining" / "runs" / "run_a").exists()
    assert not (_isolated / "research" / "mining" / "runs").exists()


def test_mining_run_explicit_user_root_beats_context(_isolated):
    MiningRunStore(user_root=user_paths.user_root(2)).create(
        {"symbols": ["000001.SZ"]}, {"daily_generation": 1}, run_id="run_b",
    )
    with _as_account(1):
        assert MiningRunStore().list_runs() == []
    assert [m["run_id"] for m in MiningRunStore(user_root=user_paths.user_root(2)).list_runs()] == ["run_b"]


# ================================================================
# 8) 策略源码目录
# ================================================================

def _strategy_code(strategy_id: str, name: str) -> str:
    return f'''"""测试策略"""
import polars as pl

META = {{
    "id": "{strategy_id}",
    "name": "{name}",
    "description": "测试描述",
    "tags": ["测试"],
    "params": [],
    "scoring": {{}},
}}

ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20

RULES = """
1. 测试规则一
2. 测试规则二
3. 测试规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


def _request(tmp_path: Path, engine: StrategyEngine) -> SimpleNamespace:
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, strategy_engine=engine)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _save_strategy(tmp_path: Path, account_id: int, strategy_id: str, name: str) -> Path:
    """以 account_id 的身份保存一个自定义策略, 返回其落盘路径。"""
    root = user_paths.user_root(account_id)
    engine = StrategyEngine(strategy_dirs=[root / "strategies" / "custom"])
    request = _request(tmp_path, engine)
    req = strategy_api.StrategyCodeSaveRequest(
        strategy_id=strategy_id,
        target_source="custom",
        mode="create",
        code=_strategy_code("wrong", name),
        name=name,
    )
    with _as_account(account_id):
        result = strategy_api._save_strategy_code(req, request)
    assert result["ok"] is True
    return Path(result["path"])


def test_strategy_source_lands_in_own_account_dir(_isolated):
    """策略源码按账户分家; 同一 strategy_id 两个账户各存一份, 互不覆盖。"""
    path_a = _save_strategy(_isolated, 1, "custom_shared", "A的策略")
    path_b = _save_strategy(_isolated, 2, "custom_shared", "B的策略")

    assert path_a == _isolated / "users" / "1" / "strategies" / "custom" / "custom_shared.py"
    assert path_b == _isolated / "users" / "2" / "strategies" / "custom" / "custom_shared.py"
    assert path_a != path_b
    assert "A的策略" in path_a.read_text(encoding="utf-8")
    assert "B的策略" in path_b.read_text(encoding="utf-8")

    # 新账户的目录里没有别人的策略; 共享 data_dir 下不出现 strategies/
    assert sorted(p.name for p in path_b.parent.glob("*.py")) == ["custom_shared.py"]
    assert not (_isolated / "strategies").exists()


def test_strategy_dir_helper_is_per_account(_isolated):
    """handler 解析出的账户根 + 源码目录 = 该账户自己的 strategies/<source>。"""
    def _dir_for(account_id: int, source: str) -> Path:
        engine = StrategyEngine(strategy_dirs=[])
        with _as_account(account_id):
            root = strategy_api._user_root(_request(_isolated, engine))
        return strategy_api._target_dir(root, source)

    dir_a = _dir_for(1, "custom")
    dir_b = _dir_for(2, "composite")

    assert dir_a == user_paths.user_root(1) / "strategies" / "custom"
    assert dir_b == user_paths.user_root(2) / "strategies" / "composite"
    assert dir_a.resolve().is_relative_to(user_paths.user_root(1).resolve())
    assert not dir_b.resolve().is_relative_to(user_paths.user_root(1).resolve())
