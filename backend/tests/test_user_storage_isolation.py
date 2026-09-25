"""领域 1 每用户存储 (自选 / 自选池 / 监控规则 / 告警记录) 的跨账户隔离测试。

本文件断言的是**真实存储函数的行为**, 不是路径拼接:

  - 账户 A 写入的数据, 账户 B 通过同一套 API 读不到 (双向);
  - 没有账户上下文且没有显式 user_root 时必须 fail-closed 报错, **绝不**回退到
    共享目录 —— 静默回退是跨用户串号, 比报错严重得多;
  - 显式 user_root 压过请求上下文 (后台线程的口径);
  - 新账户起始为空 (不继承任何人的数据);
  - 共享行情构件 (pools/ 下的非 watchlist 池) 仍然共享, 且读取它们不需要账户
    上下文 —— 只有一个池 id 例外, 那是每账户私有数据。

账户上下文用 ``preferences.set_current_user_root()`` 注入 —— 与认证中间件在真实
请求里做的是同一件事; 显式参数那一路对应后台线程 (contextvar 跨线程不可靠)。
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import polars as pl
import pytest

from app import config as app_config
from app.services import alert_store, preferences, user_paths, watchlist
from app.services.user_paths import MissingUserContextError
from app.strategy import monitor_rules
from app.tickflow import pools


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每个用例一个独立 DATA_DIR; 默认**无**账户上下文, 由用例自己注入。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    token = preferences.set_current_user_root(None)
    yield tmp_path
    preferences.reset_current_user_root(token)


@contextmanager
def _acting_as(account_id: int) -> Iterator[Path]:
    """以某账户的身份执行 (等价于认证中间件给请求注入的 contextvar)。"""
    root = user_paths.user_root(account_id)
    token = preferences.set_current_user_root(root)
    try:
        yield root
    finally:
        preferences.reset_current_user_root(token)


def _rule(rid: str = "r1") -> dict:
    return monitor_rules.normalize({
        "id": rid,
        "name": f"规则 {rid}",
        "type": "price",
        "scope": "symbols",
        "symbols": ["600000.SH"],
        "conditions": [{"field": "close", "op": ">=", "value": 10.0}],
    })


def _event(tag: str, ts: int | None = None) -> dict:
    return {
        "ts": ts if ts is not None else int(time.time() * 1000),
        "rule_id": tag,
        "source": "monitor",
        "type": "price",
        "message": tag,
    }


# ================================================================
# 账户之间不可见 (走 contextvar, 即请求路径)
# ================================================================

def test_watchlist_entries_and_groups_are_not_visible_across_accounts(_isolated):
    with _acting_as(1):
        _, group = watchlist.create_group("核心池")
        watchlist.add("600000.SH", group_id=group["id"])

    with _acting_as(2):
        # B 看不到 A 的自选, 也看不到 A 的分组
        assert watchlist.list_symbols() == []
        assert watchlist.list_groups() == []
        watchlist.add("000001.SZ")

    with _acting_as(1):
        rows = watchlist.list_symbols()
        assert [r["symbol"] for r in rows] == ["600000.SH"]
        # 分组标签也要跟着账户走, 不能串
        assert rows[0]["group_ids"] == [watchlist.list_groups()[0]["id"]]

    with _acting_as(2):
        assert [r["symbol"] for r in watchlist.list_symbols()] == ["000001.SZ"]


def test_watchlist_clear_only_clears_the_current_account(_isolated):
    with _acting_as(1):
        watchlist.add("600000.SH")
    with _acting_as(2):
        watchlist.add("000001.SZ")
        assert watchlist.clear() == 1

    with _acting_as(1):
        assert [r["symbol"] for r in watchlist.list_symbols()] == ["600000.SH"]


def test_monitor_rules_are_not_visible_across_accounts(_isolated):
    with _acting_as(1):
        monitor_rules.save_one(_rule("a_only"))

    with _acting_as(2):
        assert monitor_rules.load_all() == []
        assert monitor_rules.load_one("a_only") is None
        # 同 id 规则在两个账户里可以各存一份, 互不覆盖
        monitor_rules.save_one(_rule("b_only"))

    with _acting_as(1):
        assert [r["id"] for r in monitor_rules.load_all()] == ["a_only"]
        assert monitor_rules.load_one("b_only") is None
        # 删除也只影响本账户
        assert monitor_rules.delete_one("b_only") is False
        assert monitor_rules.delete_one("a_only") is True
        assert monitor_rules.load_all() == []

    with _acting_as(2):
        assert [r["id"] for r in monitor_rules.load_all()] == ["b_only"]


def test_monitor_rule_migration_targets_only_the_current_account(_isolated):
    """策略监控自动迁移: 只能为**本账户**生成规则。"""
    with _acting_as(1):
        monitor_rules.migrate_strategy_monitors(["s1"], {"s1": "策略一"})

    with _acting_as(2):
        assert monitor_rules.load_all() == []

    with _acting_as(1):
        assert [r["id"] for r in monitor_rules.load_all()] == ["mr_strategy_s1"]


def test_alert_records_are_not_visible_across_accounts(_isolated):
    # ts 是 alert_store 的唯一标识 (JSONL 无主键), 两条记录必须取不同的 ts
    now_ms = int(time.time() * 1000)
    ev_a, ev_b = _event("a", now_ms - 1000), _event("b", now_ms - 2000)

    with _acting_as(1):
        alert_store.append_many([ev_a])

    with _acting_as(2):
        assert alert_store.list_recent() == []
        assert alert_store.count() == 0
        alert_store.append(ev_b)
        assert [e["rule_id"] for e in alert_store.list_recent()] == ["b"]

    with _acting_as(1):
        assert [e["rule_id"] for e in alert_store.list_recent()] == ["a"]
        assert alert_store.count() == 1
        # 删别人的记录: 不该命中 (B 的 ts 在 A 的账下不存在)
        assert alert_store.delete_one(ev_b["ts"]) is False
        assert alert_store.delete_one(ev_a["ts"]) is True
        assert alert_store.count() == 0

    with _acting_as(2):
        assert alert_store.count() == 1

    with _acting_as(2):
        assert alert_store.clear() == 1
        assert alert_store.count() == 0


# ================================================================
# 自选池 (pools.get_pool): 唯 watchlist 每账户, 其余共享
# ================================================================

def test_watchlist_pool_is_per_account_and_other_pools_stay_shared(_isolated):
    """最容易被做错的一处: pools/ 是共享缓存目录, 但 watchlist 池不是共享数据。"""
    shared = _isolated / "pools" / "CN_Equity_A.parquet"
    shared.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["600000.SH"]}).write_parquet(shared)

    # 共享池: 没有任何账户上下文也能读 (后台调度器拉全 A 就是这样)
    assert pools.get_pool("CN_Equity_A") == ["600000.SH"]

    with _acting_as(1):
        watchlist.add("600036.SH")
    with _acting_as(2):
        watchlist.add("000001.SZ")

    with _acting_as(1):
        assert pools.get_pool("watchlist") == ["600036.SH"]
    with _acting_as(2):
        assert pools.get_pool("watchlist") == ["000001.SZ"]

    # 共享池不受账户上下文影响, 账户里的自选也不污染共享池
    with _acting_as(1):
        assert pools.get_pool("CN_Equity_A") == ["600000.SH"]
    assert not (_isolated / "user_data" / "watchlist.parquet").exists()


def test_watchlist_pool_fails_closed_without_account_context(_isolated, monkeypatch):
    # 屏蔽上游拉取: 本用例只关心"要不要账户上下文", 不触网
    monkeypatch.setattr(pools, "_fetch_pool", lambda pool_id: ["600000.SH"])

    with pytest.raises(MissingUserContextError):
        pools.get_pool("watchlist")
    # 共享池无此约束
    assert pools.get_pool("CN_Equity_A") == ["600000.SH"]


def test_watchlist_pool_accepts_explicit_user_root_for_background_threads(_isolated):
    with _acting_as(1):
        watchlist.add("600036.SH")

    # 后台线程口径: 无 contextvar, 但显式传 user_root
    assert pools.get_pool("watchlist", user_root=user_paths.user_root(1)) == ["600036.SH"]
    assert pools.get_pool("watchlist", user_root=user_paths.user_root(2)) == []


# ================================================================
# fail-closed: 无上下文且无显式 user_root
# ================================================================

@pytest.mark.parametrize("call", [
    pytest.param(lambda: watchlist.list_symbols(), id="watchlist.list_symbols"),
    pytest.param(lambda: watchlist.list_groups(), id="watchlist.list_groups"),
    pytest.param(lambda: watchlist.add("600000.SH"), id="watchlist.add"),
    pytest.param(lambda: watchlist.create_group("池"), id="watchlist.create_group"),
    pytest.param(lambda: monitor_rules.load_all(), id="monitor_rules.load_all"),
    pytest.param(lambda: monitor_rules.load_one("r1"), id="monitor_rules.load_one"),
    pytest.param(lambda: monitor_rules.save_one(_rule()), id="monitor_rules.save_one"),
    pytest.param(lambda: monitor_rules.delete_one("r1"), id="monitor_rules.delete_one"),
    pytest.param(
        lambda: monitor_rules.migrate_strategy_monitors(["s1"], {"s1": "策略一"}),
        id="monitor_rules.migrate_strategy_monitors",
    ),
    pytest.param(lambda: alert_store.append(_event("x")), id="alert_store.append"),
    pytest.param(lambda: alert_store.append_many([_event("x")]), id="alert_store.append_many"),
    pytest.param(lambda: alert_store.list_recent(), id="alert_store.list_recent"),
    pytest.param(lambda: alert_store.count(), id="alert_store.count"),
    pytest.param(lambda: alert_store.clear(), id="alert_store.clear"),
])
def test_store_calls_fail_closed_without_any_account_context(_isolated, call):
    """读和写都必须报错, 而不是静默落到某个共享文件上。"""
    with pytest.raises(MissingUserContextError):
        call()
    # 失败调用不留任何痕迹: 共享目录下不该冒出 user_data/
    assert not (_isolated / "user_data").exists()
    assert not (_isolated / "users").exists()


# ================================================================
# 显式 user_root 压过上下文 (后台线程口径)
# ================================================================

def test_explicit_user_root_overrides_request_context(_isolated):
    other = user_paths.user_root(2)

    with _acting_as(1):
        # context 是账户 1, 但显式写账户 2 —— 必须落在 2 名下
        watchlist.add("600000.SH", user_root=other)
        monitor_rules.save_one(_rule("explicit"), user_root=other)
        alert_store.append(_event("explicit"), user_root=other)
        # 本账户 (1) 什么都读不到
        assert watchlist.list_symbols() == []
        assert monitor_rules.load_all() == []
        assert alert_store.list_recent() == []

    with _acting_as(2):
        assert [r["symbol"] for r in watchlist.list_symbols()] == ["600000.SH"]
        assert [r["id"] for r in monitor_rules.load_all()] == ["explicit"]
        assert [e["rule_id"] for e in alert_store.list_recent()] == ["explicit"]


# ================================================================
# 新账户起始为空 + 落盘位置
# ================================================================

def test_new_account_starts_empty_and_dirs_are_created_on_demand(_isolated):
    """全新部署: 老账户有数据, 新账户第一次读必须是空的, 且目录按需创建。"""
    with _acting_as(1):
        watchlist.add("600000.SH")
        monitor_rules.save_one(_rule())
        alert_store.append(_event("a"))

    fresh = user_paths.user_root(99)
    assert not fresh.exists()  # 尚未创建

    with _acting_as(99):
        assert watchlist.list_symbols() == []
        assert watchlist.list_groups() == []
        assert monitor_rules.load_all() == []
        assert alert_store.list_recent() == []
        assert alert_store.count() == 0

    # 首个写操作把目录建出来 (无需任何迁移/预置步骤)
    with _acting_as(99):
        watchlist.add("300750.SZ")
    assert (fresh / "user_data" / "watchlist.parquet").exists()


def test_each_store_writes_under_the_account_root_only(_isolated):
    """钉住落盘口径: 四个存储都在 <user_root>/user_data/ 下, 共享目录不掺和。"""
    with _acting_as(1) as root:
        watchlist.add("600000.SH")
        watchlist.create_group("核心池")
        monitor_rules.save_one(_rule("pinned"))
        alert_store.append_many([_event("pinned")])

    assert (root / "user_data" / "watchlist.parquet").exists()
    assert (root / "user_data" / "watchlist_groups.json").exists()
    assert (root / "user_data" / "monitor_rules" / "pinned.json").exists()
    assert (root / "user_data" / "alerts.jsonl").exists()
    # 共享 data_dir 下不再出现这些每用户文件 (老布局已废弃)
    assert not (_isolated / "user_data").exists()
    # 写盘全在各自账户根内 (没有文件逃到 users/<id>/ 之外)
    for p in (root / "user_data").rglob("*"):
        assert p.resolve().is_relative_to(root.resolve())
