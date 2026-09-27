"""回测/优化/walk-forward 的 worker 载荷必须携带**发起账户**的根目录。

为什么单独立这条测试
--------------------
worker 子进程没有请求上下文, 它读策略覆盖值 (`load_override`) 时必须拿到
**发起账户**的根; 若传共享 `data_dir`, `resolve_user_root` 会 fail-closed 抛
`InvalidAccountIdError`, 而回测引擎那侧是 `except Exception: pass` —— 结果是
composite 策略的子策略覆盖值**静默变空**, 跑出默认参数的结果, 页面上没有任何提示,
且与选股页(那里 user_root 是对的)对不上。

`api/backtest.py` 有四处 `make_worker_task(...)`: strategy_run / strategy_stream /
optimize_stream / walkforward_stream。本文件用真实 HTTP 链路 + 打桩 `run_worker_task`
记录载荷, 断言四处都带上账户根, 且**不等于**共享 data_dir。

写法说明(与本仓 §7 方法学一致): 断言打在"载荷里的 user_root 是否等于该账户的根"这个
**负面不变量**上; 只断言"调用过 make_worker_task"会与缺陷互为盲区。
"""
from __future__ import annotations

import importlib
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app.api import account as account_api
from app.services import account_sessions, accounts, user_paths

_PASSWORD = "Passw0rd!123"


@pytest.fixture(autouse=True)
def _http_env(tmp_path, monkeypatch) -> Iterator[SimpleNamespace]:
    """独立 DATA_DIR + 清账号相关内存态; 给 app.state 挂最小 repo。

    app **不**作为上下文管理器使用 —— 不触发 lifespan, 因此不会启动调度器/行情轮询。
    """
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()

    # 端点会问 repo 要"最早/最新交易日"来解析缺省 start; 这里给一个极早的日期,
    # 使 start 落到 2020-01-01, 既不触发范围守卫, 也不需要真实行情。
    _earliest = date(2020, 1, 1)

    def _earliest_daily_date(*_args, **_kwargs) -> date:
        return _earliest

    app_main.app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda _symbol: "stock",
        earliest_daily_date=_earliest_daily_date,
        latest_daily_date=lambda *_a, **_k: date(2026, 9, 24),
    )
    yield SimpleNamespace(data_dir=Path(tmp_path))


def _registered_client(email: str) -> TestClient:
    """注册即登录 (首个账号为 admin), 返回带会话 cookie 的客户端。"""
    client = TestClient(app_main.app)
    resp = client.post(
        "/api/account/register",
        json={"email": email, "password": _PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return client


def _record_worker_task(monkeypatch) -> list[dict]:
    """打桩 run_worker_task (真实 make_worker_task 仍参与), 记录下发的载荷。"""
    from app.backtest import worker as worker_mod

    seen: list[dict] = []

    def fake_run(task, *args, **kwargs):
        seen.append(task)
        return {"ok": True, "rows": [], "equity": [], "metrics": {}}

    monkeypatch.setattr(worker_mod, "run_worker_task", fake_run)
    return seen


def _payload_root(task: dict) -> Path | None:
    raw = task.get("user_root")
    return None if raw is None else Path(raw)


def test_strategy_run_worker_task_carries_account_root(_http_env, monkeypatch) -> None:
    """POST /api/backtest/strategy/run 下发的载荷必须带该账户的根。"""
    seen = _record_worker_task(monkeypatch)
    client = _registered_client("root-a@example.com")

    resp = client.post("/api/backtest/strategy/run", json={"strategy_id": "ma_cross"})
    assert resp.status_code == 200, resp.text
    assert len(seen) == 1, f"应恰好下发一次 worker 任务, 实际 {len(seen)}"

    task = seen[0]
    account_root = user_paths.user_root(1)

    assert _payload_root(task) == account_root, (
        f"载荷里的 user_root 应为账户根 {account_root}, 实际 {task.get('user_root')!r}"
    )
    # 负面不变量: 绝不能是共享 data_dir —— 那会让子策略覆盖值静默变空。
    assert _payload_root(task) != _http_env.data_dir, (
        "worker 载荷把共享 data_dir 当成了账户根"
    )


def test_every_backtest_worker_task_call_site_passes_user_root() -> None:
    """源码级回归守卫: `api/backtest.py` 里每一处 `make_worker_task(` 都必须传 `user_root=`。

    为什么是源码级而不是行为级 —— 说清楚, 免得被当成"空转测试":
    - 三条 SSE 端点 (`strategy_stream` / `optimize_stream` / `walkforward_stream`) 在
      **后台线程**里下发 worker 任务, 而 `TestClient` 不能跨线程使用 (线程里调用会立刻
      抛错), httpx 的流又只能迭代一次 ⇒ 在测试里无法确定性地观察到那次下发。
      试过"读一行就断言": 结果是一个竞态 —— 赢了绿、输了红, 比不测更糟。
    - 而本缺陷**复发的唯一方式**就是"新增/改动一处调用点忘了传 user_root" (本轮
      四处里三处就是这么漏的)。所以用源码扫描守住这个不变量, 判别力恰好覆盖复发路径。
    - 行为侧由 `test_strategy_run_worker_task_carries_account_root` 覆盖 (同步端点,
      可确定性观察), 两者互补。
    """
    source = (Path(__file__).resolve().parents[2] / "app" / "api" / "backtest.py").read_text(
        encoding="utf-8",
    )
    # 取每处调用的完整实参 (跨行), 直到右括号收口。
    calls: list[str] = []
    for match in re.finditer(r"make_worker_task\(", source):
        depth = 1
        i = match.end()
        while i < len(source) and depth:
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
            i += 1
        calls.append(source[match.end():i])

    assert len(calls) == 4, (
        f"api/backtest.py 里 make_worker_task 的调用点数量变了 (期望 4, 实际 {len(calls)}) —— "
        "新增调用点请一并确认它传了 user_root=, 再更新这条断言"
    )
    missing = [c.strip().splitlines()[0] for c in calls if "user_root" not in c]
    assert not missing, (
        "以下 make_worker_task 调用点没传 user_root, 会让该账户的 composite 子策略覆盖值"
        f"静默变空: {missing}"
    )
