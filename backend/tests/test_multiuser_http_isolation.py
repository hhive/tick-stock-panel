"""领域 1 每用户存储在**真实 HTTP 链路上**的隔离测试。

前面的 test_user_storage_isolation.py 直接调存储函数 (手工注入 contextvar);
这里走完整链路: TestClient → 认证中间件 (按账号 set_current_user_root)
→ 路由 → 存储, 端点本身一行都没改 (数据落点从 data_dir 变成该账号的 user_root)。

这层测试是"HTTP 调用点没被改坏"的证据: 中间件注入的上下文足以让
watchlist / monitor-rules / alerts 各自读写**本账号**的文件。

app 不作为上下文管理器使用 —— 那样不触发 lifespan, 因此不会启动调度器/行情轮询。
"""
from __future__ import annotations

import importlib
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app.api import account as account_api
from app.services import account_sessions, accounts


@pytest.fixture(autouse=True)
def _http_env(tmp_path, monkeypatch) -> Iterator[SimpleNamespace]:
    """独立 DATA_DIR + 清空账号相关的模块级内存态; 给 app.state 挂最小 repo。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()

    # 监控规则路由要按标的解析资产类型; 其余路由不需要 repo
    app_main.app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda _symbol: "stock",
    )
    try:
        yield SimpleNamespace(data_dir=tmp_path)
    finally:
        if hasattr(app_main.app.state, "repo"):
            del app_main.app.state.repo


def _register(email: str) -> TestClient:
    """注册即登录 (首个账号是 admin), 返回带着会话 cookie 的客户端。"""
    client = TestClient(app_main.app)
    resp = client.post(
        "/api/account/register", json={"email": email, "password": "secret123"},
    )
    assert resp.status_code == 200, resp.text
    return client


def _price_rule(rid: str) -> dict:
    return {
        "id": rid,
        "name": f"规则 {rid}",
        "type": "price",
        "asset_type": "stock",
        "scope": "symbols",
        "symbols": ["600000.SH"],
        "conditions": [{"field": "close", "op": ">=", "value": 10.0}],
    }


def _symbols(client: TestClient) -> list[str]:
    resp = client.get("/api/watchlist")
    assert resp.status_code == 200, resp.text
    return [row["symbol"] for row in resp.json()["symbols"]]


def _rule_ids(client: TestClient) -> list[str]:
    resp = client.get("/api/monitor-rules")
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()["rules"]]


def test_http_requests_only_see_the_logged_in_account(_http_env):
    a = _register("a@example.com")
    b = _register("b@example.com")

    # A 写入三类数据
    assert a.post("/api/watchlist", json={"symbol": "600000.SH"}).status_code == 200
    assert a.post("/api/monitor-rules", json=_price_rule("http_rule")).status_code == 200
    assert a.post("/api/alerts/seed", params={"count": 3}).status_code == 200

    # B 全程看不到
    assert _symbols(b) == []
    assert _rule_ids(b) == []
    assert b.get("/api/alerts").json() == {"alerts": [], "total": 0}

    # A 自己的照旧可见 (端点行为未变)
    assert _symbols(a) == ["600000.SH"]
    assert _rule_ids(a) == ["http_rule"]
    assert a.get("/api/alerts").json()["total"] == 3

    # 落盘位置: 各归各的账号目录 (共享 data_dir/user_data 下只剩全局偏好, 不该有这些文件)
    shared = _http_env.data_dir
    assert (shared / "users" / "1" / "user_data" / "watchlist.parquet").exists()
    assert (shared / "users" / "1" / "user_data" / "monitor_rules" / "http_rule.json").exists()
    assert (shared / "users" / "1" / "user_data" / "alerts.jsonl").exists()
    assert not (shared / "user_data" / "watchlist.parquet").exists()
    assert not (shared / "user_data" / "monitor_rules").exists()
    assert not (shared / "user_data" / "alerts.jsonl").exists()
    # 账号 2 什么都没写
    assert not (shared / "users" / "2" / "user_data" / "watchlist.parquet").exists()
    assert not (shared / "users" / "2" / "user_data" / "alerts.jsonl").exists()


def test_http_writes_do_not_leak_into_other_accounts(_http_env):
    a = _register("a@example.com")
    b = _register("b@example.com")

    assert a.post("/api/watchlist", json={"symbol": "600036.SH"}).status_code == 200
    assert b.post("/api/watchlist", json={"symbol": "000001.SZ"}).status_code == 200

    assert _symbols(a) == ["600036.SH"]
    assert _symbols(b) == ["000001.SZ"]

    # 删除/清空只作用于本账号
    assert a.delete("/api/watchlist/600036.SH").status_code == 200
    assert _symbols(a) == []
    assert _symbols(b) == ["000001.SZ"]
