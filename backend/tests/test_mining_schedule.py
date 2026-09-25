from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app import config as app_config
from app.jobs import daily_pipeline
from app.services import mining_schedule, preferences, user_paths
from app.services.mining_jobs import MiningRunStore

ACCOUNT_A = 1
ACCOUNT_B = 2


class FakeRepo:
    def __init__(self, data_dir: Path, *, latest: date = date(2026, 8, 14)) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)
        self.latest = latest
        self.generation = "generation-1"

    def latest_enriched_date(self, asset_type: str = "stock") -> date | None:
        assert asset_type == "stock"
        return self.latest

    def get_matrix_data_generation(self, asset_type: str = "stock") -> str:
        assert asset_type == "stock"
        return self.generation

    def get_instruments_asset(self, asset_type: str = "stock") -> pl.DataFrame:
        assert asset_type == "stock"
        return pl.DataFrame({
            "symbol": ["000001.SZ"],
            "name": ["示例"],
            "total_shares": [1_000_000.0],
            "float_shares": [800_000.0],
        })


class FakeManager:
    """与真 manager 同一接缝的假实现: store_for(user_root) + start(..., user_root=)。

    刻意按账户根分家 —— 若这里退回"单个共享 store", 双账户用例就会互相看见,
    测试也就无法证明隔离。
    """

    def __init__(self) -> None:
        self._stores: dict[Path, MiningRunStore] = {}
        self.calls: list[dict] = []

    def store_for(self, user_root: Path) -> MiningRunStore:
        root = Path(user_root)
        store = self._stores.get(root)
        if store is None:
            store = MiningRunStore(root)
            self._stores[root] = store
        return store

    def start(
        self,
        request,
        fingerprint,
        *,
        user_root: Path,
        force: bool,
        source: str,
        run_id: str,
    ):
        call = {
            "request": request,
            "fingerprint": fingerprint,
            "force": force,
            "source": source,
            "run_id": run_id,
            "user_root": Path(user_root),
        }
        self.calls.append(call)
        manifest = self.store_for(user_root).create(request, fingerprint, run_id=run_id)
        return {"run_id": manifest["run_id"]}


def _account_root(tmp_path: Path, account_id: int) -> Path:
    """账户根走**真实**路径构造 (settings.data_dir 已指向 tmp_path)。"""
    return user_paths.user_root(account_id)


def _scheduled_state(
    tmp_path: Path,
    monkeypatch,
    *,
    account_ids: tuple[int, ...] = (ACCOUNT_A,),
    days: int = 1200,
    manager=None,
) -> SimpleNamespace:
    """构造一个已开启周度调度的 state, 并按 account_ids 扇出。

    `iter_user_roots` 读的是**真实**账号注册表, 测试里必须拦截, 否则用例会
    按部署里实际存在的账号扇出 (不 hermetic)。data_dir 指向 tmp_path, 于是
    user_root(id) == tmp_path/users/<id>, 与部署布局一致 (共享行情在 tmp_path
    顶层, 账户私有数据在 users/<id> 之下)。

    manager=None 时用 FakeManager (隔离 claim 逻辑); 传真 manager 时整条链路
    (schedule_claim → store_for → MiningRunStore) 都走生产实现。
    """
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    repo = FakeRepo(tmp_path)
    manager = FakeManager() if manager is None else manager
    state = SimpleNamespace(repo=repo, mining_manager=manager, strategy_engine=None)
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "balanced",
        },
    )
    monkeypatch.setattr(
        user_paths,
        "iter_user_roots",
        lambda: [
            (account_id, _account_root(tmp_path, account_id))
            for account_id in account_ids
        ],
    )
    _write_prerequisites(tmp_path, repo.latest, days=days)
    return state


@pytest.fixture
def scheduled_state(tmp_path: Path, monkeypatch):
    return _scheduled_state(tmp_path, monkeypatch)


def _friday(week_offset: int = 0) -> datetime:
    return datetime(2026, 8, 14, 16, tzinfo=ZoneInfo("Asia/Shanghai")) + timedelta(
        weeks=week_offset
    )


def _write_prerequisites(data_dir: Path, latest: date, *, days: int) -> None:
    enriched = data_dir / "kline_daily_enriched"
    trading_dates: list[date] = []
    for offset in range(days):
        day = latest - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        trading_dates.append(day)
        partition = enriched / f"date={day.isoformat()}"
        partition.mkdir(parents=True, exist_ok=True)
        (partition / "part.parquet").write_bytes(b"enriched")
    regime = data_dir / "regime_history" / "part.parquet"
    regime.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "date": sorted(trading_dates),
        "state": ["range"] * len(trading_dates),
    }).write_parquet(regime)


def test_beijing_date_and_iso_week_use_china_timezone():
    utc = ZoneInfo("UTC")
    instant = datetime(2026, 8, 13, 16, 30, tzinfo=utc)

    assert mining_schedule.beijing_date(instant) == date(2026, 8, 14)
    assert mining_schedule.iso_week(date(2027, 1, 1)) == (2026, 53)


def test_fingerprint_retries_generation_change_and_returns_stable_token(
    tmp_path,
    monkeypatch,
) -> None:
    repo = FakeRepo(tmp_path)
    state = SimpleNamespace(strategy_engine=None)
    generations = iter([
        "generation-1",
        "generation-2",
        "generation-2",
        "generation-2",
    ])
    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _asset: next(generations))

    fingerprint = mining_schedule.build_data_fingerprint(
        repo,
        state,
        {"asset_type": "stock", "strategy_ids": []},
    )

    assert fingerprint["generation"] == "generation-2"


def test_implementation_metadata_is_recursive_content_based_and_root_independent(
    tmp_path,
) -> None:
    roots = [tmp_path / "first" / "app", tmp_path / "second" / "app"]
    for root in roots:
        nested = root / "backtest"
        nested.mkdir(parents=True)
        (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
        (nested / "runtime.py").write_text("RESULT = 1\n", encoding="utf-8")
        (nested / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    first = mining_schedule._implementation_metadata(roots[0])
    second = mining_schedule._implementation_metadata(roots[1])
    (roots[1] / "backtest" / "runtime.py").write_text("RESULT = 2\n", encoding="utf-8")
    changed = mining_schedule._implementation_metadata(roots[1])

    assert first == second
    assert first["file_count"] == 2
    assert str(tmp_path) not in str(first)
    assert first["digest"] != changed["digest"]


def test_fingerprint_covers_result_implementation_digest(
    tmp_path,
    monkeypatch,
) -> None:
    repo = FakeRepo(tmp_path)
    state = SimpleNamespace(strategy_engine=None)
    first = mining_schedule.build_data_fingerprint(
        repo,
        state,
        {"asset_type": "stock", "strategy_ids": []},
    )
    monkeypatch.setattr(
        mining_schedule,
        "_implementation_metadata",
        lambda _root: {"file_count": 1, "digest": "changed-runtime"},
    )
    second = mining_schedule.build_data_fingerprint(
        repo,
        state,
        {"asset_type": "stock", "strategy_ids": []},
    )

    assert first["implementation"] != second["implementation"]
    assert first["digest"] != second["digest"]


def test_selected_strategy_metadata_changes_with_same_size_source_edit(tmp_path) -> None:
    source = tmp_path / "strategies" / "custom" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    strategy = SimpleNamespace(execution_backend="matrix_native", file_path=source)
    state = SimpleNamespace(
        strategy_engine=SimpleNamespace(get=lambda _strategy_id: strategy)
    )

    first = mining_schedule._selected_strategy_metadata(
        state,
        ["demo"],
        tmp_path,
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")
    second = mining_schedule._selected_strategy_metadata(
        state,
        ["demo"],
        tmp_path,
    )

    assert first[0]["source"]["size"] == second[0]["source"]["size"]
    assert first[0]["source"]["sha256"] != second[0]["source"]["sha256"]
    assert first[0]["source_tree"]["digest"] != second[0]["source_tree"]["digest"]


def test_fingerprint_rejects_continuously_changing_generation(
    tmp_path,
    monkeypatch,
) -> None:
    repo = FakeRepo(tmp_path)
    state = SimpleNamespace(strategy_engine=None)
    generations = iter(["a", "b", "c", "d"])
    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _asset: next(generations))

    with pytest.raises(ValueError, match="changed"):
        mining_schedule.build_data_fingerprint(
            repo,
            state,
            {"asset_type": "stock", "strategy_ids": []},
        )


def test_disabled_and_before_scheduled_weekday_do_not_enqueue(
    scheduled_state,
    monkeypatch,
):
    state = scheduled_state
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": False,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "balanced",
        },
    )
    assert mining_schedule.run_weekly_mining(state, now=_friday())["status"] == "disabled"

    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "balanced",
        },
    )
    thursday = _friday() - timedelta(days=1)
    assert mining_schedule.run_weekly_mining(state, now=thursday)["status"] == "weekday_mismatch"
    assert state.mining_manager.calls == []


def test_later_workday_catches_up_once_in_same_iso_week(scheduled_state, monkeypatch):
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 3,
            "mining_budget_profile": "balanced",
        },
    )

    first = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    second = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())

    assert first["status"] == "enqueued"
    assert second["status"] == "already_claimed"
    assert second["accounts"][0]["run_id"] == first["accounts"][0]["run_id"]
    assert len(scheduled_state.mining_manager.calls) == 1


def test_same_week_and_fingerprint_enqueue_once(scheduled_state):
    first = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    second = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())

    assert first["status"] == "enqueued"
    assert first["accounts"] == [
        {
            "account_id": ACCOUNT_A,
            "status": "enqueued",
            "run_id": f"weekly-{ACCOUNT_A}-2026-W33",
        }
    ]
    assert second["status"] == "already_claimed"
    assert second["accounts"][0]["run_id"] == first["accounts"][0]["run_id"]
    assert len(scheduled_state.mining_manager.calls) == 1
    call = scheduled_state.mining_manager.calls[0]
    assert call["force"] is False
    assert call["source"] == "scheduled"
    assert call["run_id"] == first["accounts"][0]["run_id"]
    assert call["run_id"] == call["fingerprint"]["source_claim"]
    assert call["user_root"] == _account_root(Path(scheduled_state.repo.store.data_dir), ACCOUNT_A)
    assert call["request"]["asset_type"] == "stock"
    assert call["request"]["symbols"] is None
    assert call["request"]["strategy_ids"] == []
    assert call["request"]["require_regime"] is True
    assert call["request"]["end"] == "2026-08-14"
    assert len(call["request"]["factor_names"]) <= 48


def test_profile_change_cannot_bypass_same_week_claim(
    scheduled_state,
    monkeypatch,
    tmp_path,
):
    first = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    store = scheduled_state.mining_manager.store_for(_account_root(tmp_path, ACCOUNT_A))
    store.transition_status(first["accounts"][0]["run_id"], "failed", error="worker failed")
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "strict",
        },
    )

    second = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())

    assert second["status"] == "already_claimed"
    assert second["accounts"][0]["run_id"] == first["accounts"][0]["run_id"]
    assert len(scheduled_state.mining_manager.calls) == 1


def test_new_week_creates_new_claim_but_same_week_metadata_change_does_not(
    scheduled_state,
):
    manager = scheduled_state.mining_manager
    first = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    next_week = mining_schedule.run_weekly_mining(scheduled_state, now=_friday(1))

    latest_file = (
        scheduled_state.repo.store.data_dir
        / "kline_daily_enriched"
        / "date=2026-08-14"
        / "part.parquet"
    )
    latest_file.write_bytes(b"changed-enriched-metadata")
    changed = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())

    assert first["accounts"][0]["run_id"] != next_week["accounts"][0]["run_id"]
    assert changed["status"] == "already_claimed"
    assert changed["accounts"][0]["run_id"] == first["accounts"][0]["run_id"]
    assert len(manager.calls) == 2


def test_missing_regime_records_visible_skipped_prerequisite(scheduled_state, tmp_path):
    regime = scheduled_state.repo.store.data_dir / "regime_history" / "part.parquet"
    regime.unlink()

    result = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    manifest = scheduled_state.mining_manager.store_for(
        _account_root(tmp_path, ACCOUNT_A)
    ).get(result["accounts"][0]["run_id"])

    assert result["status"] == "skipped_prerequisite"
    assert manifest is not None
    assert manifest["status"] == "skipped_prerequisite"
    assert "regime" in manifest["error"]
    assert scheduled_state.mining_manager.calls == []


def test_incomplete_regime_coverage_records_visible_skip(scheduled_state, tmp_path):
    regime_path = (
        scheduled_state.repo.store.data_dir
        / "regime_history"
        / "part.parquet"
    )
    history = pl.read_parquet(regime_path).sort("date")
    history.filter(pl.col("date") != history["date"][-2]).write_parquet(regime_path)

    result = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    manifest = scheduled_state.mining_manager.store_for(
        _account_root(tmp_path, ACCOUNT_A)
    ).get(result["accounts"][0]["run_id"])

    assert result["status"] == "skipped_prerequisite"
    assert manifest is not None
    assert "T-1" in manifest["error"]
    assert scheduled_state.mining_manager.calls == []


def test_early_regime_gap_records_visible_skipped_prerequisite(scheduled_state, tmp_path):
    data_dir = scheduled_state.repo.store.data_dir
    regime_path = data_dir / "regime_history" / "part.parquet"
    history = pl.read_parquet(regime_path).sort("date")
    history.slice(1).write_parquet(regime_path)

    result = mining_schedule.run_weekly_mining(scheduled_state, now=_friday())
    manifest = scheduled_state.mining_manager.store_for(
        _account_root(tmp_path, ACCOUNT_A)
    ).get(result["accounts"][0]["run_id"])

    assert result["status"] == "skipped_prerequisite"
    assert manifest is not None
    assert "T-1" in manifest["error"]
    assert scheduled_state.mining_manager.calls == []


def test_insufficient_data_records_visible_skipped_prerequisite(tmp_path, monkeypatch):
    state = _scheduled_state(tmp_path, monkeypatch, days=30)
    manager = state.mining_manager
    monkeypatch.setattr(
        preferences,
        "get_mining_schedule",
        lambda: {
            "mining_schedule_enabled": True,
            "mining_schedule_weekday": 4,
            "mining_budget_profile": "strict",
        },
    )

    result = mining_schedule.run_weekly_mining(state, now=_friday())
    manifest = manager.store_for(_account_root(tmp_path, ACCOUNT_A)).get(
        result["accounts"][0]["run_id"]
    )

    assert result["status"] == "skipped_prerequisite"
    assert manifest is not None
    assert manifest["status"] == "skipped_prerequisite"
    assert "insufficient" in manifest["error"]
    assert manager.calls == []


# ================================================================
# 双账户: 周度 claim 必须按账户分家
# ================================================================

def test_two_accounts_each_run_their_own_weekly_mining_for_the_same_week(
    tmp_path,
    monkeypatch,
):
    """同一 ISO 周内两个账户各自跑一次 —— B 不得拿到 already_claimed。

    这是缺陷的正面证明: claim 此前只由日历推导 (weekly-2026-W33), A 先跑到
    就占掉整周, B 的调度直接 return already_claimed, 永远跑不了自己的挖掘。
    """
    state = _scheduled_state(
        tmp_path, monkeypatch, account_ids=(ACCOUNT_A, ACCOUNT_B)
    )
    manager = state.mining_manager

    result = mining_schedule.run_weekly_mining(state, now=_friday())

    assert [item["account_id"] for item in result["accounts"]] == [ACCOUNT_A, ACCOUNT_B]
    assert [item["status"] for item in result["accounts"]] == ["enqueued", "enqueued"]
    assert result["status"] == "enqueued"
    claim_a = result["accounts"][0]["run_id"]
    claim_b = result["accounts"][1]["run_id"]
    assert claim_a == f"weekly-{ACCOUNT_A}-2026-W33"
    assert claim_b == f"weekly-{ACCOUNT_B}-2026-W33"
    assert claim_a != claim_b
    assert len(manager.calls) == 2
    assert {call["user_root"] for call in manager.calls} == {
        _account_root(tmp_path, ACCOUNT_A),
        _account_root(tmp_path, ACCOUNT_B),
    }


def test_two_accounts_second_call_in_same_week_is_claimed_per_account(
    tmp_path,
    monkeypatch,
):
    state = _scheduled_state(
        tmp_path, monkeypatch, account_ids=(ACCOUNT_A, ACCOUNT_B)
    )
    manager = state.mining_manager

    first = mining_schedule.run_weekly_mining(state, now=_friday())
    second = mining_schedule.run_weekly_mining(state, now=_friday())

    assert [item["status"] for item in second["accounts"]] == [
        "already_claimed",
        "already_claimed",
    ]
    assert [item["run_id"] for item in second["accounts"]] == [
        item["run_id"] for item in first["accounts"]
    ]
    assert len(manager.calls) == 2


def test_each_account_sees_only_its_own_mining_runs(tmp_path, monkeypatch):
    """A 的周度运行不得出现在 B 的运行列表里 (含 B 直接猜 claim id 也读不到)。"""
    state = _scheduled_state(
        tmp_path, monkeypatch, account_ids=(ACCOUNT_A, ACCOUNT_B)
    )
    manager = state.mining_manager

    result = mining_schedule.run_weekly_mining(state, now=_friday())
    claim_a = result["accounts"][0]["run_id"]
    claim_b = result["accounts"][1]["run_id"]

    store_a = manager.store_for(_account_root(tmp_path, ACCOUNT_A))
    store_b = manager.store_for(_account_root(tmp_path, ACCOUNT_B))
    store_a.create({"factor_names": ["momentum"]}, {"v": 1}, run_id="a_only_run")

    assert {item["run_id"] for item in store_a.list_runs()} == {"a_only_run", claim_a}
    assert {item["run_id"] for item in store_b.list_runs()} == {claim_b}
    # 猜 id 也读不到别人的运行: B 的 store 里不存在 A 的 claim
    assert store_b.get(claim_a) is None
    assert store_a.get(claim_b) is None


def test_two_accounts_run_through_the_real_manager(tmp_path, monkeypatch):
    """端到端走**生产实现** (schedule_claim → MiningJobManager.store_for →
    MiningRunStore): 两个账户各自入队, 且各自只看得见自己的运行。

    前面的双账户用例用的是假 manager (隔离 claim 逻辑), 覆盖不到"存储是否真的
    按账户分家"—— 假 manager 自带正确的分家, 真 manager 退化成单一 store 时
    它们照样全绿。这条用例就是为了让那种退化**变红**: worker 用桩替掉 (不 spawn
    子进程), 其余全是生产代码。
    """
    from app.services.mining_manager import MiningJobManager

    def task_factory(kind: str, data_dir: Path, payload: dict) -> dict:
        return {"kind": kind, "data_dir": str(data_dir), "payload": payload}

    def runner(task, progress_cb, cancel_event):
        return {"status": "succeeded"}

    manager = MiningJobManager(tmp_path, worker_runner=runner, task_factory=task_factory)
    try:
        state = _scheduled_state(
            tmp_path,
            monkeypatch,
            account_ids=(ACCOUNT_A, ACCOUNT_B),
            manager=manager,
        )
        result = mining_schedule.run_weekly_mining(state, now=_friday())

        assert [item["account_id"] for item in result["accounts"]] == [
            ACCOUNT_A,
            ACCOUNT_B,
        ]
        assert [item["status"] for item in result["accounts"]] == [
            "enqueued",
            "enqueued",
        ]

        store_a = manager.store_for(_account_root(tmp_path, ACCOUNT_A))
        store_b = manager.store_for(_account_root(tmp_path, ACCOUNT_B))
        assert store_a.runs_root != store_b.runs_root
        assert store_b.runs_root == _account_root(tmp_path, ACCOUNT_B) / "research" / "mining" / "runs"
        run_ids = [item["run_id"] for item in result["accounts"]]
        assert {item["run_id"] for item in store_a.list_runs()} == {run_ids[0]}
        assert {item["run_id"] for item in store_b.list_runs()} == {run_ids[1]}
    finally:
        manager.shutdown()


def test_pipeline_failure_does_not_trigger_mining(monkeypatch):
    mining_calls = []
    monkeypatch.setattr(daily_pipeline, "_run_tracked", lambda *_args: False)
    monkeypatch.setattr(
        "app.services.mining_schedule.run_weekly_mining",
        lambda state: mining_calls.append(state),
    )

    daily_pipeline._scheduled_pipeline_task(lambda: None)

    assert mining_calls == []


def test_enqueue_failure_does_not_escape_successful_pipeline(monkeypatch):
    monkeypatch.setattr(daily_pipeline, "_run_tracked", lambda *_args: True)

    def fail_enqueue(_state):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr("app.services.mining_schedule.run_weekly_mining", fail_enqueue)

    daily_pipeline._scheduled_pipeline_task(lambda: None)
