"""C1 回归: 模拟盘后台链路按面板账户扇出 — 不得整段跳过, 不得跨账户串号。

修复前: ``quote_service._evaluate_monitors`` 与 ``daily_pipeline.run_now`` 都在
**后台线程**里调 ``paper.list_account_ids()`` (不传 user_root) →
``resolve_user_root(None)`` 抛 ``MissingUserContextError`` → ``account_ids = []`` →
盘中撮合 / 盘后结算 / 自动跟单 / 成交留痕四条链路整段跳过 (生产日志实证
``paper_settle skipped`` 出现 6 次, 而 UI 照常展示模拟盘、照常接受下单)。

本文件用**真实**存储函数与真实 ``run_now`` 校验:
  ① 两个面板账户各有模拟盘账户时, 后台结算对**两个**账户都执行;
  ② 成交留痕写进**各自**账户的 alerts.jsonl, 不串号, 且一次成交只留一条
     (留痕由 ``paper._fill_order`` 在成交当时写; 调用方**不得**再写一遍);
  ③ 一个账户的数据坏掉, 另一个账户仍完成结算;
  ④ 盘中钩子同构: 事件必须带**面板账户主键** (正整数) 才能通过 ``_group_by_account``
     的 fail-closed 校验, 模拟盘账户名保留在独立字段 ``paper_account`` (决策 D3);
  ⑤ 盘中热路径的账户名单带 30 秒 TTL 缓存 (不每轮读盘), 到期必须看到新账户;
  ⑥ Webhook 腿按账户: 各自的事件用各自的渠道地址与签名密钥。
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app import config as app_config
from app.jobs import daily_pipeline
from app.market_time import cn_today
from app.services import accounts, preferences, user_paths
from app.services.quote_service import QuoteService, _group_by_account
from app.strategy import paper as paper_trading
from app.tickflow.capabilities import CapabilitySet

SYMBOL = "000001.SZ"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """独立 DATA_DIR, 且默认**无**请求上下文 (模拟后台线程)。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    token = preferences.set_current_user_root(None)
    yield tmp_path
    preferences.reset_current_user_root(token)
    accounts.reset_state_for_tests()


def _panel_accounts(n: int = 2) -> list[int]:
    """建 n 个面板账户, 返回其主键 (正整数)。"""
    return [accounts.create_account(f"user{i}@example.com", "secret123").id for i in range(n)]


def _write_daily_bar(data_dir: Path, day: str, symbol: str, close: float) -> None:
    part = data_dir / "kline_daily" / f"date={day}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [symbol],
        "date": [date.fromisoformat(day)],
        "open": [close],
        "close": [close],
    }).write_parquet(part / "part.parquet")


def _setup_paper_account(
    panel_id: int,
    paper_account: str,
    *,
    symbol: str = SYMBOL,
    order_type: str = "close",
    qty: int = 100,
    ref_price: float = 10.0,
) -> tuple[Path, dict]:
    """在某面板账户下建模拟盘账户 + 一张 pending 买单 (order_type=close/market)。"""
    root = user_paths.ensure_user_dirs(panel_id)
    paper_trading.create_account(
        1_000_000.0, account_id=paper_account, name=paper_account, user_root=root,
    )
    order, err = paper_trading.create_order(
        symbol, "buy", account_id=paper_account, qty=qty,
        order_type=order_type, ref_price=ref_price, user_root=root,
    )
    assert err is None, err
    assert order is not None
    return root, order


def _run_pipeline(tmp_path, monkeypatch, *, day: str) -> dict:
    """跑真实 run_now, 只桩掉与本用例无关的取数/视图阶段 (口径同既有管道用例)。"""
    monkeypatch.setattr(preferences, "load", lambda: {
        "pipeline_pull_a_share": False, "pipeline_regime_enabled": False,
        "minute_sync_enabled": False, "adj_factor_provider": "tickflow",
    })
    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", lambda *_: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *_: [])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "run_pipeline", lambda *a, **k: 0)
    from app.services import data_integrity
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *a, **k: [])

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: date.fromisoformat(day),
    )
    return daily_pipeline.run_now(repo, CapabilitySet(set()))  # type: ignore[arg-type]


def _alerts_of(root: Path) -> list[dict]:
    import json

    p = root / "user_data" / "alerts.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _paper_fill_records(root: Path) -> list[dict]:
    """该账户 alerts.jsonl 里的**全部**触发记录。

    成交留痕由 ``paper._fill_order`` 在成交当时按账户写盘 (盘中与结算两条路径都经过它),
    非 default 的模拟盘账户名出现在 message 的 ``[name]`` 前缀里。

    断言用**总数**而不是只看 ``type=paper_fill``: 调用方若再写一遍 (type=fill),
    总数会变成 2 —— 这正是「重复留痕」要挡的形态, 按 type 过滤会把它漏过去。
    """
    return _alerts_of(root)


# ================================================================
# ① 后台结算对每个面板账户都执行 (修复前: 恒跳过, 订单永远 pending)
# ================================================================

def test_run_now_settles_every_panel_account(_isolated, monkeypatch):
    day = cn_today().isoformat()
    _write_daily_bar(_isolated, day, SYMBOL, close=11.0)
    panel_ids = _panel_accounts(2)
    roots = []
    for i, panel_id in enumerate(panel_ids):
        root, _order = _setup_paper_account(panel_id, f"sim{i}")
        roots.append(root)

    result = _run_pipeline(_isolated, monkeypatch, day=day)

    assert result["paper_settle"]["filled"] == 2, result["paper_settle"]
    for i, (panel_id, root) in enumerate(zip(panel_ids, roots)):
        orders = paper_trading.load_orders(f"sim{i}", user_root=root)
        assert [o["status"] for o in orders] == ["filled"], (panel_id, orders)


# ================================================================
# ② 留痕写进各自账户, 不串号
# ================================================================

def test_settle_events_land_in_own_account_alert_log(_isolated, monkeypatch):
    day = cn_today().isoformat()
    _write_daily_bar(_isolated, day, SYMBOL, close=11.0)
    panel_ids = _panel_accounts(2)
    roots = []
    for i, panel_id in enumerate(panel_ids):
        root, _order = _setup_paper_account(panel_id, f"sim{i}")
        roots.append(root)

    _run_pipeline(_isolated, monkeypatch, day=day)

    per_account = [_paper_fill_records(root) for root in roots]
    for i, (panel_id, events) in enumerate(zip(panel_ids, per_account)):
        # 一次成交只留一条: 调用方 (管道/盘中钩子) 不再另写一条近似记录
        assert len(events) == 1, (panel_id, events)
        assert events[0].get("type") == "paper_fill", events[0]
        # 留痕落在**本账户**的模拟盘子账户名下
        assert f"[sim{i}]" in events[0]["message"], events[0]
    # 各自文件互不可见: 另一个模拟盘账户名不得出现在本账户的触发历史里
    for i, root in enumerate(roots):
        blob = "".join(str(ev.get("message")) for ev in _paper_fill_records(root))
        assert f"[sim{1 - i}]" not in blob, (i, blob)


# ================================================================
# ③ 一个账户坏掉不影响其余
# ================================================================

def test_broken_account_does_not_block_others(_isolated, monkeypatch):
    day = cn_today().isoformat()
    _write_daily_bar(_isolated, day, SYMBOL, close=11.0)
    healthy_id, broken_id = _panel_accounts(2)
    healthy_root, _o = _setup_paper_account(healthy_id, "sim_ok")
    broken_root, _o2 = _setup_paper_account(broken_id, "sim_bad")
    # 数据损坏: fills.jsonl 位置上放了一个目录 → 读取时抛 OSError (真实坏数据形态,
    # 不是桩异常)。若没有逐账户 try/except, 整段结算会中断。
    (broken_root / "paper" / "accounts" / "sim_bad" / "fills.jsonl").mkdir(parents=True)

    result = _run_pipeline(_isolated, monkeypatch, day=day)

    assert result["paper_settle"]["filled"] == 1, result["paper_settle"]
    assert broken_id in result["paper_settle"]["failed_accounts"]
    assert [o["status"] for o in paper_trading.load_orders("sim_ok", user_root=healthy_root)] == ["filled"]
    assert len(_paper_fill_records(healthy_root)) == 1
    # 坏账户的结算确实没跑完: 订单仍是 pending, 也没有任何留痕
    assert [o["status"] for o in paper_trading.load_orders("sim_bad", user_root=broken_root)] == ["pending"]
    assert _paper_fill_records(broken_root) == []


# ================================================================
# 盘中钩子: 同样按账户扇出, 且事件带面板账户主键 (D3)
# ================================================================

def test_intraday_fanout_fills_every_panel_account_and_stamps_panel_key(_isolated):
    panel_ids = _panel_accounts(2)
    roots = []
    for i, panel_id in enumerate(panel_ids):
        root, _order = _setup_paper_account(panel_id, f"sim{i}", order_type="market")
        roots.append(root)

    svc = QuoteService()
    broadcasted: list[dict] = []
    svc._broadcast_alerts = lambda events: broadcasted.extend(events)  # type: ignore[method-assign]

    svc._run_paper_hooks(_isolated, {SYMBOL: 11.0}, [])

    for i, root in enumerate(roots):
        orders = paper_trading.load_orders(f"sim{i}", user_root=root)
        assert [o["status"] for o in orders] == ["filled"], orders
        events = _paper_fill_records(root)
        assert len(events) == 1, events
        assert events[0].get("type") == "paper_fill" and f"[sim{i}]" in events[0]["message"], events[0]

    # 事件必须能通过 _group_by_account 的 fail-closed 校验 (非正整数一律被丢弃):
    # account_id = 面板账户主键, 模拟盘账户名保留在 paper_account。
    grouped = _group_by_account(broadcasted, "测试投递")
    assert sum(len(v) for v in grouped.values()) == len(broadcasted) == 2
    assert set(grouped) == set(panel_ids)
    assert {ev["paper_account"] for ev in broadcasted} == {"sim0", "sim1"}


# ================================================================
# 盘中热路径的账户名单缓存 (TTL): 不每轮读盘, 但到期必须看到新账户
# ================================================================

def test_intraday_panel_roots_are_cached_with_ttl(_isolated):
    from app.services import quote_service

    first_id = _panel_accounts(1)[0]
    t0 = time.monotonic()

    assert [pid for pid, _root in quote_service._paper_account_roots(now=t0)] == [first_id]

    late_id = accounts.create_account("late@example.com", "secret123").id

    # TTL 内 → 命中缓存 (行情轮询不再每轮读 accounts.json)
    assert [pid for pid, _root in quote_service._paper_account_roots(now=t0 + 29)] == [first_id]
    # 到期 → 重建, 新账户 30 秒内出现在盘中撮合名单里
    assert sorted(pid for pid, _root in quote_service._paper_account_roots(now=t0 + 31)) == sorted(
        [first_id, late_id]
    )


# ================================================================
# Webhook 腿也按账户: 各自的事件用各自的渠道配置
# ================================================================

def test_paper_webhook_uses_each_account_own_config(_isolated, monkeypatch):
    from app.services import quote_service

    panel_ids = _panel_accounts(2)
    symbols = ["000001.SZ", "600000.SH"]
    urls = ["https://hook.example/account-a", "https://hook.example/account-b"]
    secrets = ["secret-a", "secret-b"]
    for i, panel_id in enumerate(panel_ids):
        _setup_paper_account(panel_id, f"sim{i}", symbol=symbols[i], order_type="market")
        token = preferences.set_current_user_root(user_paths.user_root(panel_id))
        try:
            preferences.set_feishu_webhook_url(urls[i])
            preferences.set_feishu_webhook_secret(secrets[i])
        finally:
            preferences.reset_current_user_root(token)

    calls: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        quote_service._WEBHOOK_EXECUTOR, "submit",
        lambda fn, *a, **k: calls.append((fn.__name__, a)),
    )
    svc = QuoteService()
    svc._broadcast_alerts = lambda events: None  # type: ignore[method-assign]

    svc._run_paper_hooks(_isolated, {symbols[0]: 11.0, symbols[1]: 22.0}, [])

    # send_feishu(url, title, body, secret) — 按 url 归并, 校验各自的 body/secret
    feishu = {args[0]: args for name, args in calls if name == "send_feishu"}
    assert set(feishu) == set(urls), calls
    assert symbols[0] in feishu[urls[0]][2] and symbols[1] not in feishu[urls[0]][2]
    assert symbols[1] in feishu[urls[1]][2] and symbols[0] not in feishu[urls[1]][2]
    assert feishu[urls[0]][3] == secrets[0]
    assert feishu[urls[1]][3] == secrets[1]
