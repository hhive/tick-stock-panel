"""挖掘/回测 worker 的账户根贯穿 (复核 C2)。

**共享行情根不是账户根。** ``resolve_user_root`` 对显式传入的共享目录 fail-closed
抛 ``InvalidAccountIdError`` (``app/services/user_paths.py`` 的 ``_validate_explicit_root``)。
把 ``settings.data_dir`` 当 ``user_root`` 传进 ``strategy_config.load_override`` 有两条
后果, 本文件各钉一条:

  ① ``_prepare_base_market`` 处没有兜底 ``except`` → 异常冒泡 → 勾选**任意**既有策略的
     挖掘必然失败 (而且失败文案会把内部路径原样回给用户);
  ② ``MatcherCandidateEvaluator._evaluate_labels`` 处的 ``except (OSError, ValueError,
     TypeError)`` 把 ``InvalidAccountIdError`` (``ValueError`` 子类) 当"这个候选不行"
     吞掉 → 所有 ``kind == "existing_strategy"`` 候选静默 ``score=None`` 被淘汰,
     看起来"没挖到", 实际是根传错了。账户根传错属**编程错误**, 必须响。

对客文案: 运行失败原文由 worker 子进程写入 manifest, 可能含内部路径 / ``data_dir``
字样 / 异常类名, 一律不随 API 响应外发 (原始 error 仍留在 manifest 里供日志/审计)。
"""

from __future__ import annotations

import ast
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config
from app.api import mining as mining_api
from app.backtest import mining_runtime, worker as worker_module
from app.backtest.mining_runtime import MatcherCandidateEvaluator, _prepare_base_market
from app.services import preferences, user_paths
from app.services.mining_jobs import MiningRunStore
from app.services.user_paths import InvalidAccountIdError
from app.strategy import config as strategy_config


@pytest.fixture(autouse=True)
def _shared_data_dir_is_the_accounts_parent(monkeypatch, tmp_path: Path) -> Path:
    """部署形态: 共享行情在 ``data_dir`` 顶层, 账户私有数据在 ``data_dir/users/<id>``。

    绝大多数用例的关键就在这个形态 —— 只有把 ``settings.data_dir`` 本身当账户根时
    ``_validate_explicit_root`` 才会拒绝, 用两个互不相干的 tmp 目录反而测不出来。
    """
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    return tmp_path


def _spy_override_loader(monkeypatch, result: dict | None = None) -> list[Any]:
    """记录 ``load_override`` 收到的 ``user_root``, 不落到真实读盘。"""
    seen: list[Any] = []

    def spy(_strategy_id: str, user_root: Any = None) -> dict:
        seen.append(user_root)
        return dict(result or {})

    monkeypatch.setattr(strategy_config, "load_override", spy)
    return seen


def _request(**overrides: Any) -> SimpleNamespace:
    values = {
        "factor_names": ("turnover_rate",),
        "strategy_ids": ("low_volatility_leader",),
        "symbols": None,
        "asset_type": "stock",
        "forward_horizon": 1,
        "commission_pct": 0.0,
        "stamp_tax_pct": 0.0,
        "slippage_bps": 0.0,
        "start": date(2024, 1, 2),
        "end": date(2024, 1, 3),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ---------------------------------------------------------------------------
# ① 有账户根时, 既有策略的挖掘不再抛 InvalidAccountIdError
# ---------------------------------------------------------------------------

def test_prepare_base_market_reads_overrides_from_account_root(
    monkeypatch, tmp_path: Path
) -> None:
    """``_prepare_base_market`` 的覆盖值必须按**账户根**读, 不是共享 data_dir。"""
    account_root = user_paths.user_root(1)
    seen = _spy_override_loader(monkeypatch)
    plan = SimpleNamespace(
        base_columns=frozenset(),
        intermediate_columns=frozenset(),
        indicator_columns=frozenset(),
        signal_columns=frozenset(),
        matrix_columns=frozenset(),
        instrument_columns=frozenset(),
        warmup_bars=1,
        full_feature_fallback=False,
        execution_backend="matrix_native",
        fundamental_columns=frozenset(),
    )
    strategy = SimpleNamespace(entry_signals=[], exit_signals=[])
    strategy_engine = SimpleNamespace(
        get=lambda _strategy_id: strategy,
        resolve_params=lambda _strategy, overrides=None: {},
    )
    service = SimpleNamespace(
        _effective_basic_filter=lambda *_args: {},
        _effective_signals=lambda _overrides, _name, default: default,
        engine=SimpleNamespace(
            load_market_data_matrix_for_backtest=lambda *_args, **_kwargs: "market",
        ),
    )
    monkeypatch.setattr(
        "app.backtest.mining_runtime.StrategyDependencyResolver.resolve",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(
        "app.backtest.mining_runtime.build_matrix_cache_profile",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    result = _prepare_base_market(
        service,
        strategy_engine,
        _request(),
        user_root=account_root,
        expected_generation="generation",
        cancel_check=None,
    )

    assert result == "market"
    assert len(seen) == 1
    assert Path(seen[0]).resolve() == account_root.resolve()


def test_prepare_base_market_survives_real_shared_dir_validation(
    monkeypatch, tmp_path: Path
) -> None:
    """不替换 ``load_override``: 真校验跑一遍, 共享目录当根必须不再被撞上。

    这是修复前的**必然失败**路径: 旧实现传 ``data_dir`` → ``_validate_explicit_root``
    抛 ``InvalidAccountIdError`` → ``_prepare_base_market`` 无兜底 → 整轮挖掘失败。
    """
    account_root = user_paths.user_root(1)
    plan = SimpleNamespace(
        base_columns=frozenset(),
        intermediate_columns=frozenset(),
        indicator_columns=frozenset(),
        signal_columns=frozenset(),
        matrix_columns=frozenset(),
        instrument_columns=frozenset(),
        warmup_bars=1,
        full_feature_fallback=False,
        execution_backend="matrix_native",
        fundamental_columns=frozenset(),
    )
    strategy = SimpleNamespace(entry_signals=[], exit_signals=[])
    strategy_engine = SimpleNamespace(
        get=lambda _strategy_id: strategy,
        resolve_params=lambda _strategy, overrides=None: {},
    )
    service = SimpleNamespace(
        _effective_basic_filter=lambda *_args: {},
        _effective_signals=lambda _overrides, _name, default: default,
        engine=SimpleNamespace(
            load_market_data_matrix_for_backtest=lambda *_args, **_kwargs: "market",
        ),
    )
    monkeypatch.setattr(
        "app.backtest.mining_runtime.StrategyDependencyResolver.resolve",
        lambda *_args, **_kwargs: plan,
    )
    monkeypatch.setattr(
        "app.backtest.mining_runtime.build_matrix_cache_profile",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    assert _prepare_base_market(
        service,
        strategy_engine,
        _request(),
        user_root=account_root,
        expected_generation="generation",
        cancel_check=None,
    ) == "market"
    # 账户根确实是账户根 (而不是被静默丢掉的参数): 覆盖值文件读的位置在账户私有树下。
    assert strategy_config.load_override("low_volatility_leader", user_root=account_root) == {}


def _evaluator(user_root: Path) -> MatcherCandidateEvaluator:
    """最小可用的评估器: 只要走过 ``_backtest_config`` 的按根读覆盖值即可。"""
    return MatcherCandidateEvaluator(
        SimpleNamespace(),
        SimpleNamespace(
            get=lambda _strategy_id: SimpleNamespace(),
            resolve_params=lambda _strategy, overrides=None: {},
        ),
        user_root,
        _request(),
        None,
        None,
    )


def test_existing_strategy_candidate_reads_overrides_from_account_root(
    monkeypatch, tmp_path: Path
) -> None:
    """候选评估按账户根读覆盖值 (旧实现传的是共享 data_dir)。"""
    account_root = user_paths.user_root(1)
    seen = _spy_override_loader(monkeypatch, result={"params": {"period": 20}})

    config = _evaluator(account_root)._backtest_config(
        {"kind": "existing_strategy", "strategy_id": "low_volatility_leader"},
        date(2024, 1, 2),
        date(2024, 1, 3),
        "overall",
    )

    assert len(seen) == 1
    assert Path(seen[0]).resolve() == account_root.resolve()
    assert config.overrides == {"params": {"period": 20}}


def test_candidate_evaluation_does_not_swallow_invalid_account_root(
    tmp_path: Path,
) -> None:
    """账户根传错必须冒泡, 不得被 ``except`` 吞成 ``score=None``。

    旧实现里 ``InvalidAccountIdError`` 是 ``ValueError`` 子类, 正好命中
    ``except (OSError, ValueError, TypeError)`` → 每个 ``existing_strategy`` 候选
    静默淘汰, 运行"成功"但没有任何策略候选, 没人知道为什么。
    """
    panel = pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [date(2024, 1, 2)],
    })
    evaluator = _evaluator(tmp_path)  # tmp_path == 共享 data_dir, 不是账户根

    with pytest.raises(InvalidAccountIdError):
        evaluator.evaluate_test(
            panel,
            {"kind": "existing_strategy", "strategy_id": "low_volatility_leader"},
        )


# ---------------------------------------------------------------------------
# ② 载荷: make_worker_task 带账户根, worker 缺参数时回落旧行为
# ---------------------------------------------------------------------------

def _backtest_config_for_worker() -> Any:
    from app.backtest.strategy import StrategyBacktestConfig

    return StrategyBacktestConfig(
        strategy_id="demo_a",
        symbols=["000001.SZ"],
        start=date(2024, 1, 2),
        end=date(2024, 1, 31),
    )


def test_make_worker_task_carries_explicit_user_root(tmp_path: Path) -> None:
    """任务载荷带账户根 —— worker 子进程没有请求上下文, 只能靠载荷。"""
    account_root = tmp_path / "users" / "1"
    task = worker_module.make_worker_task(
        "backtest", tmp_path, _backtest_config_for_worker(), user_root=account_root,
    )

    assert Path(task["user_root"]).resolve() == account_root.resolve()
    assert Path(task["data_dir"]).resolve() == tmp_path.resolve()


def test_make_worker_task_hoists_mining_payload_user_root(tmp_path: Path) -> None:
    """挖掘载荷把账户根放在 config 里 (manager 的既有约定), 任务级也要能读到。"""
    account_root = tmp_path / "users" / "1"
    task = worker_module.make_worker_task(
        "mining", tmp_path,
        {"run_id": "r1", "user_root": str(account_root / ".." / "1")},
    )

    assert Path(task["user_root"]) == account_root.resolve()


def test_worker_task_without_user_root_falls_back_to_data_dir(tmp_path: Path) -> None:
    """向后兼容: 载荷没带 ``user_root`` 的既有调用方 (api/backtest.py、工具桥)
    必须仍能跑, 回落到旧行为 (把 data_dir 当根), 不得因为缺参数直接炸。"""
    task = worker_module.make_worker_task(
        "backtest", tmp_path, _backtest_config_for_worker(),
    )

    assert "user_root" not in task
    assert worker_module.worker_user_root(task) == Path(task["data_dir"]).resolve()


def test_worker_user_root_prefers_payload_and_normalizes(tmp_path: Path) -> None:
    account_root = tmp_path / "users" / "1"
    task = {"data_dir": str(tmp_path), "user_root": str(account_root / ".." / "1")}

    assert worker_module.worker_user_root(task) == account_root.resolve()


def test_worker_engine_override_loader_uses_the_payload_account_root(
    monkeypatch, tmp_path: Path
) -> None:
    """worker 构造的策略引擎必须按**载荷里的账户根**读覆盖值 (复合策略子策略)。"""
    account_root = tmp_path / "users" / "1"
    seen = _spy_override_loader(monkeypatch)

    engine = worker_module.build_worker_strategy_engine(tmp_path, account_root)
    engine._override_loader("composite_demo")

    assert len(seen) == 1
    assert Path(seen[0]).resolve() == account_root.resolve()


def test_run_mining_runtime_uses_explicit_account_root(
    monkeypatch, tmp_path: Path
) -> None:
    """挖掘运行时的运行产物落在**账户根**, 而不是共享行情根。"""
    account_root = user_paths.user_root(1)
    captured: dict[str, Any] = {}

    class _StopHere(RuntimeError):
        pass

    class _StoreSpy:
        def __init__(self, root: Any) -> None:
            captured["root"] = Path(root)
            raise _StopHere

    monkeypatch.setattr(mining_runtime, "MiningRunStore", _StoreSpy)
    monkeypatch.setattr(
        mining_runtime, "_decode_runtime_request", lambda *_args, **_kwargs: object(),
    )
    payload = {"run_id": "r1", "data_fingerprint": {"generation": "g"}}

    with pytest.raises(_StopHere):
        mining_runtime.run_mining_runtime(
            payload,
            data_dir=tmp_path,
            user_root=account_root,
            service=SimpleNamespace(),
            strategy_engine=SimpleNamespace(),
        )

    assert captured["root"] == account_root.resolve()


def test_run_mining_runtime_still_accepts_payload_user_root(
    monkeypatch, tmp_path: Path
) -> None:
    """直调方 (既有测试/内部脚本) 只传载荷也应继续可用。"""
    captured: dict[str, Any] = {}

    class _StopHere(RuntimeError):
        pass

    class _StoreSpy:
        def __init__(self, root: Any) -> None:
            captured["root"] = Path(root)
            raise _StopHere

    monkeypatch.setattr(mining_runtime, "MiningRunStore", _StoreSpy)
    monkeypatch.setattr(
        mining_runtime, "_decode_runtime_request", lambda *_args, **_kwargs: object(),
    )
    payload = {
        "run_id": "r1",
        "data_fingerprint": {"generation": "g"},
        "user_root": str(tmp_path / "users" / "2"),
    }

    with pytest.raises(_StopHere):
        mining_runtime.run_mining_runtime(
            payload,
            data_dir=tmp_path,
            service=SimpleNamespace(),
            strategy_engine=SimpleNamespace(),
        )

    assert captured["root"] == (tmp_path / "users" / "2").resolve()


# ---------------------------------------------------------------------------
# ③ 对客返回的 error 不含内部路径 / data_dir 字样 / 异常类名
# ---------------------------------------------------------------------------

def test_manager_payload_root_matches_the_store_root(monkeypatch, tmp_path: Path) -> None:
    """item 5 核对: manager 随载荷发给 worker 的账户根, 与运行产物实际落盘的根一致。

    worker 子进程拿载荷里的根建 ``MiningRunStore`` (见 ``run_mining_runtime``); 两者
    若是不同的规范形态 ("store 落在 A、子进程按 B 建 store"), 运行产物就分家了。
    这里直接看 manager 写进载荷的原文 (自定义 task_factory 是它的既有接缝)。
    """
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    from app.services.mining_manager import MiningJobManager

    captured: dict[str, Any] = {}

    def factory(kind: str, data_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
        captured["kind"] = kind
        captured["user_root"] = payload["user_root"]
        return {"kind": kind, "data_dir": str(data_dir), "config": payload}

    def runner(task, _progress_cb, _cancel_event):
        captured.setdefault("ran", True)
        return {"status": "succeeded"}

    manager = MiningJobManager(tmp_path, worker_runner=runner, task_factory=factory)
    try:
        account_root = user_paths.user_root(1)
        # 故意用"绕一圈"的写法: 归一化必须发生在入队处, 而不是靠下游各自 resolve。
        created = manager.start(
            {"factor_names": ["turnover_rate"]},
            {"generation": "g"},
            user_root=account_root / ".." / "1",
            run_id="payload-root",
        )
        store = manager.store_for(account_root)
        deadline = time.monotonic() + 2.0
        while "ran" not in captured and time.monotonic() < deadline:
            time.sleep(0.005)
        assert created["run_id"] == "payload-root"
        assert captured["kind"] == "mining"
        shipped = Path(captured["user_root"])
        assert shipped == account_root.resolve()
        assert shipped / "research" / "mining" / "runs" == store.runs_root
    finally:
        manager.shutdown()


_LEAKY_ERROR = (
    "data_dir 之下只有 users/<id> 可作为账户根(其余是共享位置): "
    "/opt/tick-stock-panel/data\n"
    "Traceback (most recent call last):\n"
    '  File "/opt/tick-stock-panel/backend/app/strategy/config.py", line 54, in load_override\n'
    "app.services.user_paths.InvalidAccountIdError: data_dir 之下只有 users/<id> 可作为账户根"
)


class _StubManager:
    """只提供端点用到的 ``store_for`` 接缝 (存储按账户根分家)。"""

    def __init__(self) -> None:
        self._stores: dict[Path, MiningRunStore] = {}

    def store_for(self, user_root: Path) -> MiningRunStore:
        root = Path(user_root)
        store = self._stores.get(root)
        if store is None:
            store = MiningRunStore(root)
            self._stores[root] = store
        return store


@pytest.fixture
def failed_run_client(tmp_path: Path):
    """挂上 mining 路由的最小 app + 一条**失败**运行 (error 为内部原文)。"""
    account_root = user_paths.user_root(1)
    token = preferences.set_current_user_root(account_root)
    app = FastAPI()
    app.include_router(mining_api.router)
    app.state.mining_manager = _StubManager()
    app.state.strategy_engine = SimpleNamespace()
    store = app.state.mining_manager.store_for(account_root)
    manifest = store.create(
        {"factor_names": ["turnover_rate"], "strategy_ids": [], "asset_type": "stock"},
        {"generation": "test"},
        run_id="failed-run",
    )
    queued = store.append_event(
        "failed-run", "queued", {"status": "queued", "source": "manual"},
    )
    store.transition_status("failed-run", "failed", error=_LEAKY_ERROR)
    error_event = store.append_event(
        "failed-run", "error", {"status": "failed", "message": _LEAKY_ERROR},
    )
    try:
        yield TestClient(app), store, tmp_path, queued, error_event
    finally:
        preferences.reset_current_user_root(token)


def _assert_no_internals(text: str, data_dir: Path) -> None:
    assert "data_dir" not in text
    assert "Traceback" not in text
    assert "InvalidAccountIdError" not in text
    assert "/opt/" not in text
    assert str(data_dir) not in text
    assert "users/<id>" not in text


def test_run_projection_redacts_internal_error(failed_run_client) -> None:
    """``_project_run`` 外发的只能是面向用户的文案 (内部原文留在 manifest)。"""
    client, store, tmp_path, _, _ = failed_run_client

    response = client.get("/api/backtest/mining/runs/failed-run")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == mining_api.MINING_ERROR_FALLBACK
    _assert_no_internals(response.text, tmp_path)
    # 内部原文没有被破坏: 日志/审计仍能拿到它。
    raw = store.get("failed-run")
    assert raw is not None and raw["error"] == _LEAKY_ERROR


def test_sse_failure_message_is_redacted(failed_run_client) -> None:
    """SSE 的失败消息走同一套脱敏 (事件记录里的 message 同样是内部原文)。"""
    client, store, tmp_path, queued, error_event = failed_run_client

    response = client.get(
        "/api/backtest/mining/runs/failed-run/events",
        headers={"Last-Event-ID": str(queued["id"])},
    )

    assert response.status_code == 200
    assert f"id: {error_event['id']}" in response.text
    assert "event: failed" in response.text
    assert mining_api.MINING_ERROR_FALLBACK in response.text
    _assert_no_internals(response.text, tmp_path)


def test_public_error_message_keeps_our_own_guidance() -> None:
    """白名单里的**我方指引文案**原样透出 (含被追加的 traceback 时只取那一句)。"""
    from app.backtest.worker import _error_message
    from app.enriched_generation import EnrichedGenerationUnavailableError

    guidance = _error_message(EnrichedGenerationUnavailableError("boom"))
    assert mining_api.public_error_message(guidance) == guidance
    assert mining_api.public_error_message(f"{guidance}\nTraceback ...") == guidance
    # 其余一律收敛, 且收敛结果与输入无关 (by construction 不含内部结构)。
    assert mining_api.public_error_message(_LEAKY_ERROR) == mining_api.MINING_ERROR_FALLBACK
    assert mining_api.public_error_message(None) is None
    assert mining_api.public_error_message("") is None


def test_snapshot_guidance_is_whitelisted_verbatim() -> None:
    """白名单里的快照指引必须与运行时**真正抛出的那句**逐字一致。

    差一个标点就会退化成通用兜底, 用户就失去了"数据正在更新, 等更新完再挖"这个
    可操作信息 (它们长得很像, 肉眼比对挡不住这类漂移)。
    """
    source = Path(mining_runtime.__file__).read_text(encoding="utf-8")
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    matched = [message for message in mining_api.PUBLIC_ERROR_MESSAGES if message in literals]
    assert matched, (
        "mining_runtime 抛出的指引文案没有一条逐字命中对客白名单: "
        f"{mining_api.PUBLIC_ERROR_MESSAGES}"
    )
