"""监控引擎 / 告警投递的**账户隔离**证明。

这是本次改动里风险最高的一类行为: 任何一处漏掉账户维度, 后果都是**静默**的
—— A 收到 B 的告警、A 的规则用 B 的自选分组评估、A 的冷却吞掉 B 的提醒、A 的
飞书群收到 B 的推送。这些测试就是那份"读代码的人可以接受的证据":

  1. 两个账户各有自己的规则 + 自己的 SSE 连接 → 双向零串投 (断言的是订阅者
     **实际收到**的事件, 不是内部变量);
  2. 同名策略在两个账户 (rule_id 由策略 id 派生, 必然撞) → 两条规则都活着, 事件
     各自归属正确;
  3. 两个账户各有自己的自选分组 → 各自的 watchlist_group 规则按**自己**的分组评估,
     且绑定到别人的分组 id 时必须解析为空 (fail-closed) 而不是拿到别人的成员;
  4. cooldown 隔离 → A 触发不吞掉 B 的提醒;
  5. 外部推送按账户扇出 → 各用各的地址与密钥, 只推本账户的事件;
  6. 复盘进度事件按账户投递;
  7. 告警推送路径不再出现 "read without account context" 告警 (并带正对照, 证明
     那个探测器真的在工作, 断言不是空过)。
"""
from __future__ import annotations

import logging

import polars as pl
import pytest

from app.config import settings
from app.services import preferences, quote_service, user_paths, watchlist
from app.services.quote_service import QuoteService
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine

# ── 装配 ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _two_accounts(tmp_path, monkeypatch):
    """两个账户 (1/2) 的私有数据根, 布局与生产一致: <data_dir>/users/<id>/。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    roots = {1: user_paths.ensure_user_dirs(1), 2: user_paths.ensure_user_dirs(2)}
    # 账户注册表是进程级外部依赖: 测试只关心引擎/投递的账户维度, 直接钉住账户列表
    # (真实注册表由 test_accounts_store.py 覆盖)。
    monkeypatch.setattr(user_paths, "iter_user_roots", lambda: [(i, r) for i, r in roots.items()])
    return roots


def _stock_df():
    return pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "close": [10.0, 12.0],
        "change_pct": [1.0, -2.0],
        "rsi_14": [40.0, 60.0],
    })


def _price_rule(rule_id: str, symbol: str, **overrides) -> dict:
    """一条只会命中 symbol 的价格规则 (conditions 取 rsi_14 < 100 即恒真)。"""
    rule = {
        "id": rule_id,
        "name": f"价格 · {symbol}",
        "type": "price",
        "asset_type": "stock",
        "scope": "symbols",
        "symbols": [symbol],
        "logic": "and",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 100}],
        "cooldown_seconds": 0,
        "enabled": True,
        "severity": "info",
    }
    rule.update(overrides)
    rule["symbols"] = [symbol] if rule.get("scope") == "symbols" else rule.get("symbols", [])
    return rule


def _group_rule(rule_id: str, group_id: str) -> dict:
    return {
        "id": rule_id,
        "name": f"分组 · {group_id}",
        "type": "price",
        "asset_type": "stock",
        "scope": "watchlist_group",
        "group_id": group_id,
        "symbols": [],
        "logic": "and",
        "conditions": [{"field": "rsi_14", "op": "<", "value": 100}],
        "cooldown_seconds": 0,
        "enabled": True,
        "severity": "info",
    }


def _sse_alert(ev: dict) -> dict:
    """引擎事件 → SSE 载荷 (与 quote_service._evaluate_monitors 的转换同字段)。"""
    return {
        "account_id": ev["account_id"],
        "source": ev["source"],
        "type": ev["type"],
        "rule_id": ev["rule_id"],
        "symbol": ev["symbol"],
        "name": ev["name"],
        "message": ev["message"],
        "severity": ev["severity"],
    }


# ── 1. 双向零串投 (规则 → SSE 订阅者) ───────────────────────────────

def test_two_accounts_never_receive_each_others_alerts(_two_accounts):
    engine = MonitorRuleEngine()
    engine.set_rules_for(1, [_price_rule("r_a", "600000.SH")])
    engine.set_rules_for(2, [_price_rule("r_b", "000001.SZ")])
    df = _stock_df()

    service = QuoteService()
    sub_a1 = service.subscribe(1)
    sub_a2 = service.subscribe(1)          # A 的第二个标签页: 同账户应都收到
    sub_b = service.subscribe(2)

    events = engine.evaluate(df)
    # 引擎产出的事件必须自带账户标签 (路由的唯一依据)
    assert {(e["account_id"], e["symbol"]) for e in events} == {(1, "600000.SH"), (2, "000001.SZ")}

    service.push_alerts([_sse_alert(e) for e in events])

    got_a1 = sub_a1.pop()["alerts"]
    got_a2 = sub_a2.pop()["alerts"]
    got_b = sub_b.pop()["alerts"]

    assert [a["symbol"] for a in got_a1] == ["600000.SH"]
    assert [a["symbol"] for a in got_a2] == ["600000.SH"]
    assert [a["symbol"] for a in got_b] == ["000001.SZ"]

    # 双向零串投: A 永远看不到 B 的标的, B 也永远看不到 A 的
    assert all(a["account_id"] == 1 for a in got_a1 + got_a2)
    assert all(a["account_id"] == 2 for a in got_b)
    assert "000001.SZ" not in {a["symbol"] for a in got_a1 + got_a2}
    assert "600000.SH" not in {a["symbol"] for a in got_b}


def test_subscriber_of_other_account_gets_nothing(_two_accounts):
    """没有本账户事件时, 该账户的连接**一条都收不到** (不是收到别人的)。"""
    engine = MonitorRuleEngine()
    engine.set_rules_for(1, [_price_rule("r_a", "600000.SH")])

    service = QuoteService()
    idle = service.subscribe(2)   # 账户 2 没有任何规则
    events = engine.evaluate(_stock_df())
    service.push_alerts([_sse_alert(e) for e in events])

    assert idle.pop()["alerts"] == []


def test_alert_without_account_tag_is_dropped_not_broadcast(_two_accounts, caplog):
    """缺账户标签的事件丢弃 (fail-closed) —— 绝不退化为"发给所有人"。"""
    service = QuoteService()
    sub1 = service.subscribe(1)
    sub2 = service.subscribe(2)

    with caplog.at_level(logging.WARNING, logger="app.services.quote_service"):
        service.push_alerts([{"source": "price", "type": "price", "symbol": "600000.SH"}])

    assert sub1.pop()["alerts"] == []
    assert sub2.pop()["alerts"] == []
    assert "缺少合法账户标签" in caplog.text


# ── 2. 同名策略的 rule_id 碰撞 ──────────────────────────────────────

def test_same_named_strategy_in_two_accounts_keeps_both_rules(_two_accounts):
    """两个账户建同名策略 → 同一个派生 rule_id; 两条规则必须都活着且各自归属。"""
    rid = monitor_rules.strategy_rule_id("trend_breakout")
    # 前提: 派生规则 id 只看策略 id, 与账户无关 —— 扁平表必然互相覆盖
    assert rid == monitor_rules.strategy_rule_id("trend_breakout")

    engine = MonitorRuleEngine()
    engine.set_rules_for(1, [_price_rule(rid, "600000.SH", name="策略监控 · A趋势")])
    engine.set_rules_for(2, [_price_rule(rid, "000001.SZ", name="策略监控 · B趋势")])

    assert set(engine.rules_for(1)) == {rid}
    assert set(engine.rules_for(2)) == {rid}
    assert engine.rules_for(1)[rid]["name"] == "策略监控 · A趋势"
    assert engine.rules_for(2)[rid]["name"] == "策略监控 · B趋势"
    # 扁平 dict[rule_id, rule] 在这里只会剩下一条 (2 → 1)
    assert engine.rule_count == 2

    events = engine.evaluate(_stock_df())
    by_account = {(e["account_id"], e["rule_name"]) for e in events}
    assert by_account == {(1, "策略监控 · A趋势"), (2, "策略监控 · B趋势")}


def test_reload_engine_loads_each_account_from_own_root(_two_accounts):
    """全量 reload 逐账户读盘: 同名规则不会跨账户覆盖。"""
    roots = _two_accounts
    rid = monitor_rules.strategy_rule_id("same_name")
    monitor_rules.save_one(
        _price_rule(rid, "600000.SH", name="A的规则"), user_root=roots[1])
    monitor_rules.save_one(
        _price_rule(rid, "000001.SZ", name="B的规则"), user_root=roots[2])

    engine = MonitorRuleEngine()
    assert monitor_rules.reload_engine(engine) == 2

    assert engine.rules_for(1)[rid]["name"] == "A的规则"
    assert engine.rules_for(2)[rid]["name"] == "B的规则"
    assert {e["account_id"] for e in engine.evaluate(_stock_df())} == {1, 2}


# ── 3. watchlist_group 按各自账户的分组评估 ─────────────────────────

def test_watchlist_group_rules_use_own_account_groups(_two_accounts):
    roots = _two_accounts
    _, group_a = watchlist.create_group("A池", user_root=roots[1])
    watchlist.add("600000.SH", group_id=group_a["id"], user_root=roots[1])
    _, group_b = watchlist.create_group("B池", user_root=roots[2])
    watchlist.add("000001.SZ", group_id=group_b["id"], user_root=roots[2])

    engine = MonitorRuleEngine()
    engine.set_rules_for(1, [_group_rule("r_ga", group_a["id"])])
    engine.set_rules_for(2, [_group_rule("r_gb", group_b["id"])])

    events = engine.evaluate(_stock_df())
    assert {(e["account_id"], e["symbol"]) for e in events} == {(1, "600000.SH"), (2, "000001.SZ")}


def test_watchlist_group_from_other_account_resolves_empty(_two_accounts):
    """B 的规则绑定 A 的分组 id → 解析为空 (fail-closed), 绝不拿到 A 的成员。

    这是"第一轮评估把分组快照钉死给所有人"那个缺陷的直接反证: 先让账户 1 完成一轮
    评估 (把 A 的分组读进缓存), 再看账户 2 绑定 A 的 group_id 会拿到什么。
    """
    roots = _two_accounts
    _, group_a = watchlist.create_group("A池", user_root=roots[1])
    watchlist.add("600000.SH", group_id=group_a["id"], user_root=roots[1])

    engine = MonitorRuleEngine()
    engine.set_rules_for(1, [_group_rule("r_ga", group_a["id"])])
    assert {e["account_id"] for e in engine.evaluate(_stock_df())} == {1}

    engine.set_rules_for(2, [_group_rule("r_b_steals_a", group_a["id"])])
    # 账户 2 的规则一条都不触发 (解析不出成员 → 空); 触发的那条只能是账户 1 自己的
    assert {e["account_id"] for e in engine.evaluate(_stock_df())} == {1}


# ── 4. cooldown 隔离 ────────────────────────────────────────────────

def test_cooldown_does_not_cross_accounts(_two_accounts):
    """同 rule_id + 同标的 + 同事件类型: A 的冷却不得吞掉 B 的提醒。"""
    rid = monitor_rules.strategy_rule_id("same_name")   # 派生 id: 两账户必然相同
    engine = MonitorRuleEngine()
    engine.set_rules_for(1, [_price_rule(rid, "600000.SH", cooldown_seconds=3600)])
    engine.set_rules_for(2, [_price_rule(rid, "600000.SH", cooldown_seconds=3600)])
    df = _stock_df()

    first = engine.evaluate(df)
    assert [e["account_id"] for e in first] == [1, 2]   # 两家都响
    # 冷却仍在各自账户内生效 (第二轮谁都不再响)
    assert engine.evaluate(df) == []


# ── 5. 外部推送按账户扇出 ──────────────────────────────────────────

class _CaptureExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))


def _configure_feishu(roots, monkeypatch) -> None:
    """给两个账户各配一个**不同**的飞书地址与签名密钥 (写各自的 preferences)。"""
    for account_id, url in ((1, "https://feishu.example/A"), (2, "https://feishu.example/B")):
        preferences.save({"feishu_webhook_url": url}, user_root=roots[account_id])
        preferences.save({"feishu_webhook_secret": f"secret-{account_id}"}, user_root=roots[account_id])


def _webhook_events() -> list[dict]:
    return [
        {"account_id": 1, "rule_id": "r_a", "source": "price", "type": "price",
         "symbol": "600000.SH", "name": "浦发银行", "message": "A 的告警", "severity": "info"},
        {"account_id": 2, "rule_id": "r_b", "source": "price", "type": "price",
         "symbol": "000001.SZ", "name": "平安银行", "message": "B 的告警", "severity": "info"},
    ]


def _webhook_engine() -> object:
    rules = {
        1: {"r_a": {"webhook_channels": ["feishu"]}},
        2: {"r_b": {"webhook_channels": ["feishu"]}},
    }
    return type("Engine", (), {"rules_for": lambda self, account_id: rules[account_id]})()


def _run_webhook(_two_accounts, monkeypatch, caplog=None):
    """真实 preferences + 真实 _maybe_send_webhook, 只截住投递线程池。"""
    capture = _CaptureExecutor()
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", capture)
    sent: list[tuple] = []
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_feishu",
        lambda url, title, body, secret=None: sent.append((url, title, body, secret)) or True,
    )
    if caplog is not None:
        with caplog.at_level(logging.WARNING, logger="app.services.preferences"):
            QuoteService._maybe_send_webhook(
                object.__new__(QuoteService), _webhook_events(), _webhook_engine())
    else:
        QuoteService._maybe_send_webhook(
            object.__new__(QuoteService), _webhook_events(), _webhook_engine())
    # 同步执行被提交的任务 (测试里不依赖线程池调度顺序)
    for fn, args in capture.calls:
        fn(*args)
    return sent


def test_webhook_uses_each_account_channel_and_only_its_events(_two_accounts, monkeypatch):
    _configure_feishu(_two_accounts, monkeypatch)

    sent = _run_webhook(_two_accounts, monkeypatch)

    by_url: dict[str, list[str]] = {}
    for url, _title, body, _secret in sent:
        by_url.setdefault(url, []).append(body)

    assert set(by_url) == {"https://feishu.example/A", "https://feishu.example/B"}
    # 每个地址只收到本账户的那一条
    assert len(by_url["https://feishu.example/A"]) == 1
    assert len(by_url["https://feishu.example/B"]) == 1
    assert "600000.SH" in by_url["https://feishu.example/A"][0]
    assert "000001.SZ" not in by_url["https://feishu.example/A"][0]
    assert "000001.SZ" in by_url["https://feishu.example/B"][0]
    assert "600000.SH" not in by_url["https://feishu.example/B"][0]


def test_webhook_signs_with_own_account_secret(_two_accounts, monkeypatch):
    """签名密钥也是每账户的: 串了密钥会让飞书侧直接拒收。"""
    _configure_feishu(_two_accounts, monkeypatch)

    sent = _run_webhook(_two_accounts, monkeypatch)
    secrets_by_url = {url: secret for url, _t, _b, secret in sent}

    assert secrets_by_url["https://feishu.example/A"] == "secret-1"
    assert secrets_by_url["https://feishu.example/B"] == "secret-2"


def test_alert_push_reports_no_missing_account_context(_two_accounts, monkeypatch, caplog):
    """告警推送路径逐账户显式传 user_root ⇒ 不再出现"无账户上下文"告警。

    带正对照: 先证明探测器确实在工作 (无 user_root 读每用户键必须告警), 否则
    "没告警"可能只是因为告警被去重/根本没实现。
    """
    _configure_feishu(_two_accounts, monkeypatch)
    monkeypatch.setattr(preferences, "_warned_missing_context", set())

    with caplog.at_level(logging.WARNING, logger="app.services.preferences"):
        preferences.get_feishu_webhook_url()
    assert "read without account context" in caplog.text   # 正对照

    caplog.clear()
    monkeypatch.setattr(preferences, "_warned_missing_context", set())
    _run_webhook(_two_accounts, monkeypatch, caplog)
    assert "read without account context" not in caplog.text


# ── 6. 复盘进度事件按账户投递 ───────────────────────────────────────

def test_review_progress_is_routed_per_account(_two_accounts):
    service = QuoteService()
    sub1 = service.subscribe(1)
    sub2 = service.subscribe(2)

    service.push_review_event(1, '{"type":"delta","content":"A"}')
    service.push_review_event(2, '{"type":"delta","content":"B"}')

    assert sub1.pop()["reviews"] == ['{"type":"delta","content":"A"}']
    assert sub2.pop()["reviews"] == ['{"type":"delta","content":"B"}']


def test_shared_review_fans_out_to_every_account(_two_accounts):
    """共享复盘 (定时任务, 输入只有共享行情) 逐账户复制投递, 各自只收到自己那份。"""
    service = QuoteService()
    sub1 = service.subscribe(1)
    sub2 = service.subscribe(2)

    service.push_review_event_to_all_accounts('{"type":"done"}')

    assert sub1.pop()["reviews"] == ['{"type":"done"}']
    assert sub2.pop()["reviews"] == ['{"type":"done"}']


# ── 7. 市场级/部署级通知 ────────────────────────────────────────────

def test_system_alert_fans_out_with_each_account_tag(_two_accounts):
    """共享通知逐账户打标签投递: 不扇出就等于全站都收不到。"""
    service = QuoteService()
    sub1 = service.subscribe(1)
    sub2 = service.subscribe(2)

    service.push_system_alerts([{"source": "depth", "type": "takeover", "message": "系统接管"}])

    assert [a["account_id"] for a in sub1.pop()["alerts"]] == [1]
    assert [a["account_id"] for a in sub2.pop()["alerts"]] == [2]


# ── 8. 策略结果缓存按账户 ──────────────────────────────────────────

def test_latest_strategy_results_are_per_account(_two_accounts):
    engine = MonitorRuleEngine()
    engine._latest_strategy_results = {  # noqa: SLF001  # 直接摆两份结果, 只测读取口径
        1: {"s1": {"rows": [{"symbol": "600000.SH"}], "total": 1, "as_of": "2026-07-20"}},
        2: {"s1": {"rows": [{"symbol": "000001.SZ"}], "total": 1, "as_of": "2026-07-20"}},
    }

    assert [r["symbol"] for r in engine.latest_strategy_results(1)["s1"]["rows"]] == ["600000.SH"]
    assert [r["symbol"] for r in engine.latest_strategy_results(2)["s1"]["rows"]] == ["000001.SZ"]
    assert engine.latest_strategy_results(3) == {}   # 没有该账户 → 空, 不是别人的


def test_strategy_result_update_notification_targets_changed_accounts(_two_accounts):
    """只通知真正有结果变化的账户 (A 的策略重算不去刷新 B 的页面)。"""
    from app.services.quote_service import QuoteSubscriber

    service = QuoteService()
    sub1 = service.subscribe(1)
    sub2 = service.subscribe(2)

    service.notify_strategy_results_updated({1})

    assert sub1.pop()["strategy_results_updated"] is True
    assert sub2.pop()["strategy_results_updated"] is False
    # 兜底: 订阅者本身必须带上账户, 否则根本谈不上按账户投递
    assert QuoteSubscriber(7).account_id == 7


def test_engine_rejects_invalid_account_id():
    """账户维度 fail-closed: 非法 id 当场抛, 不在内存里留下"谁都取不到"的规则。"""
    from app.services.user_paths import InvalidAccountIdError

    engine = MonitorRuleEngine()
    for bad in (0, -1, None, "1/2", True):
        with pytest.raises(InvalidAccountIdError):
            engine.set_rules_for(bad, [_price_rule("r", "600000.SH")])
    assert engine.rule_count == 0


# ── 9. 告警 ext 富化按各自账户的字段配置 ────────────────────────────

def test_alert_ext_enrichment_uses_own_account_field_config(_two_accounts, monkeypatch):
    """每个账户的告警按**自己**配置的 ext 字段富化 (后台线程显式传 user_root 读)。"""
    from app.api import screener as screener_api

    roots = _two_accounts
    preferences.save(
        {"monitor_ext_fields": {"concept": {"field": "concept_A"}, "industry": None}},
        user_root=roots[1],
    )
    preferences.save(
        {"monitor_ext_fields": {"concept": {"field": "concept_B"}, "industry": None}},
        user_root=roots[2],
    )

    seen_columns: list[str] = []

    def _fake_load_ext(_repo, ext_columns):
        seen_columns.append(ext_columns)
        return {ext_columns: {"600000.SH": "标签", "000001.SZ": "标签"}}

    monkeypatch.setattr(screener_api, "_load_ext_value_maps", _fake_load_ext)

    service = QuoteService()
    service.set_app_state(type("S", (), {"repo": object()})())
    service._repo = object()
    alerts = [
        {"account_id": 1, "symbol": "600000.SH"},
        {"account_id": 2, "symbol": "000001.SZ"},
    ]
    service._enrich_alerts_ext(alerts)

    assert sorted(seen_columns) == ["concept_A", "concept_B"]
    assert "concept_A" in alerts[0] and "concept_B" not in alerts[0]
    assert "concept_B" in alerts[1] and "concept_A" not in alerts[1]
