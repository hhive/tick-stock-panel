"""domain3 多租户隔离 — 走**真实 HTTP** 的端到端验证 (两个面板账户 A/B)。

与 test_user_store_isolation_domain3.py 的分工: 那边证明「服务函数按 user_root 分家」,
这边证明「请求路径真的把 user_root 传下去了」—— 即认证中间件 → contextvar → 各存储
这条链路。少了这层, store 层修对了但中间件没接上也会「测试全绿、生产串号」。

用真实 app (含认证中间件) 但**不进 lifespan**: 那样会拉起调度器/行情轮询等后台
服务。只需手工补 app.state 里端点用到的部分 (repo / capabilities)。
"""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import main as app_main
from app.api import account as account_api
from app.services import account_sessions, accounts
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

PASSWORD = "secret123"
# 模拟盘 / 报告的写入都需要这些能力 (否则端点直接 403)
CAPS = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """独立 DATA_DIR + 清掉账号/会话的内存态 + 补齐真实 app 的 app.state。"""
    from types import SimpleNamespace

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()

    # 未进 lifespan: 手动注入端点需要的共享数据层与能力集
    app_main.app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda symbol: "stock",
    )
    app_main.app.state.capabilities = CAPS
    # 批次写入会级联同步监控规则并重载引擎 (与账户隔离无关, 这里置空实现)
    monkeypatch.setattr("app.api.monitor_rules._sync_engine", lambda request: None)
    yield tmp_path


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_main.app)


def _login(client: TestClient, email: str) -> None:
    """以该邮箱登录 (首次先注册), 会话 cookie 落在 client 上。

    同一次用例内多次切账户时账号已存在, 直接登录即可。
    """
    client.post("/api/account/logout")
    logged_in = client.post("/api/account/login", json={"email": email, "password": PASSWORD})
    if logged_in.status_code != 200:
        registered = client.post("/api/account/register", json={"email": email, "password": PASSWORD})
        assert registered.status_code == 200, registered.text
        client.post("/api/account/logout")
        logged_in = client.post("/api/account/login", json={"email": email, "password": PASSWORD})
    assert logged_in.status_code == 200, logged_in.text


class _Account:
    """某个面板账户的会话句柄。

    TestClient 的会话是**一个 cookie**, 同一时刻只能属于一个账户 —— 所以每次请求
    前都显式以本账户重新登录。若直接持有 client 变量发请求, 中间任何一次 as_other()
    都会把 cookie 换走, 断言就会在错误的身份下执行 (而且看起来"通过")。
    """

    def __init__(self, client: TestClient, email: str) -> None:
        self._client = client
        self._email = email

    def _call(self, method: str, path: str, **kwargs):
        _login(self._client, self._email)
        return getattr(self._client, method)(path, **kwargs)

    def get(self, path: str, **kwargs):
        return self._call("get", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self._call("post", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self._call("put", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self._call("delete", path, **kwargs)


@pytest.fixture
def two_accounts(client):
    """两个面板账户的会话句柄 (A / B), 每次请求都自带登录。"""
    a = _Account(client, "account-a@example.com")
    b = _Account(client, "account-b@example.com")
    a.get("/api/account/me")  # 触发注册 (首个账号即 admin)
    b.get("/api/account/me")
    return a, b


# ================================================================
# 模拟盘 (账户 / 订单 / 自动跟单规则)
# ================================================================
def test_paper_data_is_isolated_between_panel_accounts(two_accounts, _isolated):
    a, b = two_accounts
    assert a.post("/api/paper/account", json={"initial_cash": 1_000_000}).status_code == 200
    assert a.post(
        "/api/paper/orders?account=default",
        json={"symbol": "600519.SH", "side": "buy", "qty": 100, "ref_price": 1500.0},
    ).status_code == 200
    assert a.post("/api/paper/auto_rules", json={
        "name": "跟策略", "match_kind": "strategy", "match_id": "s1",
        "size_mode": "fixed_amount", "size_value": 10000,
    }).status_code == 200

    # A 自己看得到
    assert [x["id"] for x in a.get("/api/paper/accounts").json()["accounts"]] == ["default"]
    assert len(a.get("/api/paper/orders").json()["orders"]) == 1
    assert len(a.get("/api/paper/auto_rules").json()["rules"]) == 1

    # B 看不到 A 的任何东西
    assert b.get("/api/paper/accounts").json()["accounts"] == []
    assert b.get("/api/paper/orders").json()["orders"] == []
    assert b.get("/api/paper/auto_rules").json()["rules"] == []
    assert b.get("/api/paper/overview").json() == {"initialized": False, "account_id": "default"}
    assert b.get("/api/paper/nav").json() == {"nav": []}
    assert b.get("/api/paper/trades").json() == {"fills": []}

    # B 建自己的账户后, A 的数据不受影响
    assert b.post("/api/paper/account", json={"initial_cash": 500_000}).status_code == 200
    assert b.get("/api/paper/overview").json()["initial_cash"] == 500_000
    assert a.get("/api/paper/overview").json()["initial_cash"] == 1_000_000


def test_paper_data_lands_under_each_account_root(two_accounts, _isolated):
    """路径兜底: 两份数据分别落在 users/1 与 users/2 下, 共享目录不留痕。"""
    a, b = two_accounts
    a.post("/api/paper/account", json={"initial_cash": 100_000})
    b.post("/api/paper/account", json={"initial_cash": 100_000})

    assert (_isolated / "users" / "1" / "paper" / "accounts" / "default" / "account.json").is_file()
    assert (_isolated / "users" / "2" / "paper" / "accounts" / "default" / "account.json").is_file()
    assert not (_isolated / "paper").exists()


# ================================================================
# 手数批次 (+ 级联的监控规则)
# ================================================================
def test_lots_are_isolated_between_panel_accounts(two_accounts, _isolated):
    a, b = two_accounts
    lot = {
        "id": "lot_iso1", "symbol": "600519.SH", "qty": 100, "cost_price": 1500.0,
        "buy_date": "2026-08-01", "target_pct": 10,
    }

    assert a.post("/api/lots", json=lot).status_code == 200
    assert [x["id"] for x in a.get("/api/lots").json()["lots"]] == ["lot_iso1"]

    assert b.get("/api/lots").json()["lots"] == []

    # B 用同一个批次 id: 各存各的, 互不覆盖
    assert b.post("/api/lots", json={**lot, "qty": 300}).status_code == 200
    assert [x["qty"] for x in b.get("/api/lots").json()["lots"]] == [300]
    assert [x["qty"] for x in a.get("/api/lots").json()["lots"]] == [100]

    # B 删除自己的批次, 不影响 A 的同名批次
    assert b.delete("/api/lots/lot_iso1").status_code == 200
    assert b.get("/api/lots").json()["lots"] == []
    assert [x["id"] for x in a.get("/api/lots").json()["lots"]] == ["lot_iso1"]


# ================================================================
# AI 报告 (财务 / 个股 / 大盘复盘)
# ================================================================
REPORT_CASES = [
    ("ai_reports", "/api/financials/reports", {"symbol": "600519.SH", "content": "A 的财务分析"}),
    ("stock_reports", "/api/stock-analysis/reports", {"symbol": "600519.SH", "content": "A 的个股分析"}),
    ("market_recap_reports", "/api/market-recap/reports", {"as_of": "2026-09-25", "content": "A 的复盘"}),
]


@pytest.mark.parametrize("label, path, body", REPORT_CASES, ids=[c[0] for c in REPORT_CASES])
def test_ai_reports_are_isolated_between_panel_accounts(label, path, body, two_accounts, _isolated):
    a, b = two_accounts
    saved = a.post(path, json=body)
    assert saved.status_code == 200, saved.text
    report_id = saved.json()["report"]["id"]
    assert [r["id"] for r in a.get(path).json()["reports"]] == [report_id]

    assert b.get(path).json()["reports"] == []

    # 跨账户删除: B 拿着 A 的 report_id 删不掉, A 的报告仍在
    deleted = b.delete(f"{path}/{report_id}")
    assert deleted.status_code == 200 and deleted.json()["ok"] is False
    assert [r["id"] for r in a.get(path).json()["reports"]] == [report_id]


# ================================================================
# 密钥库 (通用 webhook HMAC 密钥: 无 .env 回退, 是纯每用户凭据)
# ================================================================
def test_secrets_are_isolated_between_panel_accounts(two_accounts, _isolated):
    a, b = two_accounts
    endpoint = "/api/settings/preferences/custom-webhook"

    saved = a.put(endpoint, json={"url": "https://a.example.com/hook", "secret": "sec-A"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["custom_webhook_secret_set"] is True
    prefs_a = a.get("/api/settings/preferences").json()
    assert prefs_a["custom_webhook_url"] == "https://a.example.com/hook"
    assert prefs_a["custom_webhook_secret_set"] is True

    prefs_b = b.get("/api/settings/preferences").json()
    assert prefs_b["custom_webhook_url"] == ""
    assert prefs_b["custom_webhook_secret_set"] is False  # 看不到 A 的密钥

    # B 配自己的: A 的不被覆盖
    assert b.put(endpoint, json={"url": "https://b.example.com/hook", "secret": "sec-B"}).status_code == 200
    assert a.get("/api/settings/preferences").json()["custom_webhook_url"] == "https://a.example.com/hook"
    assert b.get("/api/settings/preferences").json()["custom_webhook_secret_set"] is True


def test_secrets_file_is_written_per_account_with_0600(_isolated, two_accounts):
    import stat

    a, b = two_accounts
    a.put("/api/settings/preferences/custom-webhook", json={
        "url": "https://a.example.com/hook", "secret": "sec-A",
    })
    b.put("/api/settings/preferences/custom-webhook", json={
        "url": "https://b.example.com/hook", "secret": "sec-B",
    })

    path_a = _isolated / "users" / "1" / "user_data" / "secrets.json"
    path_b = _isolated / "users" / "2" / "user_data" / "secrets.json"
    assert path_a.is_file() and path_b.is_file()
    assert "sec-A" in path_a.read_text(encoding="utf-8")
    assert "sec-B" not in path_a.read_text(encoding="utf-8")
    assert stat.S_IMODE(path_a.stat().st_mode) == 0o600
    assert stat.S_IMODE(path_b.stat().st_mode) == 0o600


# ================================================================
# 自定义因子 (写路径需要试算门禁, 这里只验证读路径按账户分家)
# ================================================================
def test_custom_factors_are_isolated_between_panel_accounts(two_accounts, _isolated):
    """因子定义按账户存放: 同名的定义各存各的, 删除只作用于自己的账户。

    创建端点带试算门禁 (需要真实面板), 这里直接放一份定义文件, 用删除端点验证
    「读的是当前账户的目录」—— 该端点的存在性判定完全取决于 store 的按账户读取。
    """
    factor_id = "uf_orphan_iso"
    definition = {"id": factor_id, "kind": "custom", "label": "A 的因子", "status": "draft",
                  "formula": "close", "version": 1}

    factor_dir_a = _isolated / "users" / "1" / "user_data" / "custom_factors"
    factor_dir_a.mkdir(parents=True, exist_ok=True)
    factor_file_a = factor_dir_a / f"{factor_id}.json"
    factor_file_a.write_text(json.dumps(definition, ensure_ascii=False), encoding="utf-8")

    a, b = two_accounts
    # B 看不到 A 的定义 → 删除端点按「不存在」拒绝
    missing = b.delete(f"/api/factors/custom/{factor_id}")
    assert missing.status_code == 404, missing.text
    assert factor_file_a.is_file(), "B 的删除动到了 A 的文件"

    # A 删自己的 → 成功, 文件消失
    removed = a.delete(f"/api/factors/custom/{factor_id}")
    assert removed.status_code == 200, removed.text
    assert not factor_file_a.exists()
    assert not (_isolated / "user_data" / "custom_factors" / f"{factor_id}.json").exists()
