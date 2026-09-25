"""部署级共享配置的写面必须管理员专属 (走真实 HTTP, 两账户 A/B)。

原缺陷(同一形状的两个家族: 客户端给标识符, 服务端从不检查它属于谁):
  - 扩展数据 (`/api/ext-data/*`): 配置在 `<data_dir>/ext_data/<id>/`、数据是那一份共享
    parquet、拉取 Key 走 `secrets_store.save_deployment`。存在性检查泄漏 id 是否已存在,
    PUT/DELETE 只查存在不查归属 —— 普通用户能改/删别人的扩展表配置与它的 parquet。
  - 自定义数据源 (`/api/settings/data-sources`): `save_config` 的 name 由客户端给, yaml
    落在 `<data_dir>/data_sources/`。创建即覆盖且无归属校验 —— 普通用户能静默覆盖
    取数配置(URL/字段映射/鉴权), 把**全站**行情取数重定向到任意地址。

修法为什么是**角色门控**而不是"按账户分家": 这两个资源都只可能有一份。扩展表的配置/
数据/凭据三样都在共享 data_dir 下, 且喂所有人的共享计算(改一次要扇出让**每个**账户的
策略缓存失效, 见 user_paths.iter_user_roots); 数据源 yaml 是全站行情的取数入口, 而源
选择本身是**全局偏好**(daily_data_provider 等不在 preferences.PER_USER_KEYS 里)。
给它们按账户分家只会造出"N 份配置抢一份数据"的假隔离。因此保护它们的是角色, 与既有的
自定义因子/自定义信号定义端点同法(commit bd02fcb / a052b26)。

读路径**不**门控: 列表/行/维度/试拉照旧对普通用户开放 —— 与 factors/custom-signals
的"读开放, 写专属"分法一致, 本文件最后一条用例守住这一点。
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
CAPS = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
ID_A, ID_B = 1, 2

EXT_ID = "tags"
SOURCE_NAME = "demo"

EXT_CREATE_BODY = {
    "id": EXT_ID,
    "label": "标签",
    "mode": "snapshot",
    "fields": [{"name": "symbol", "dtype": "string", "label": "代码"},
               {"name": "tags", "dtype": "string", "label": "标签"}],
}

# 缺陷面: 每个端点都是"写扩展表这份共享资源"的一条路径。普通用户全部必须 403。
EXT_WRITE_CALLS = [
    ("create", "POST", "/api/ext-data", {"json": EXT_CREATE_BODY}),
    ("update", "PUT", f"/api/ext-data/{EXT_ID}", {"json": {"label": "被 B 改了"}}),
    ("delete", "DELETE", f"/api/ext-data/{EXT_ID}", {}),
    ("pull-config", "PUT", f"/api/ext-data/{EXT_ID}/pull",
     {"json": {"url": "https://evil.example.com/x"}}),
    ("api-key", "PUT", f"/api/ext-data/{EXT_ID}/api-key", {"json": {"key": "B-KEY"}}),
    ("preset-fetch", "POST", "/api/ext-data/presets/ths_concepts/fetch", {}),
    ("upload", "POST", f"/api/ext-data/{EXT_ID}/upload",
     {"files": {"file": ("a.csv", b"symbol,tags\n600519.SH,x\n", "text/csv")}}),
    ("ingest", "POST", f"/api/ext-data/{EXT_ID}/ingest",
     {"json": {"rows": [{"symbol": "600519.SH", "tags": "B 写的"}]}}),
    ("backfill", "POST", f"/api/ext-data/{EXT_ID}/backfill",
     {"json": {"start": "2026-01-01", "end": "2026-01-02"}}),
    ("fix-symbol", "POST", f"/api/ext-data/{EXT_ID}/fix-symbol", {}),
    ("pull-run", "POST", f"/api/ext-data/{EXT_ID}/pull/run", {}),
]
EXT_WRITE_IDS = [c[0] for c in EXT_WRITE_CALLS]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()

    app_main.app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda symbol: "stock",
    )
    app_main.app.state.capabilities = CAPS
    monkeypatch.setattr("app.api.monitor_rules._sync_engine", lambda request: None)
    # 扩展表的写端点在落盘后要重新注册 DuckDB 视图 (repo.store.db); 本套件只验**归属**
    # 与"文件没被动过", 视图注册与这两个断言无关, 故打桩掉(harness 里也没有 db)。
    monkeypatch.setattr("app.api.ext_data._refresh_views", lambda request: None)
    yield tmp_path


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_main.app)


def _login(client: TestClient, email: str) -> None:
    client.post("/api/account/logout")
    logged_in = client.post("/api/account/login", json={"email": email, "password": PASSWORD})
    if logged_in.status_code != 200:
        registered = client.post("/api/account/register", json={"email": email, "password": PASSWORD})
        assert registered.status_code == 200, registered.text
        client.post("/api/account/logout")
        logged_in = client.post("/api/account/login", json={"email": email, "password": PASSWORD})
    assert logged_in.status_code == 200, logged_in.text


class _Account:
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
    """A=首个注册者(admin, id=1), B=普通用户(id=2)。"""
    a = _Account(client, "account-a@example.com")
    b = _Account(client, "account-b@example.com")
    a.get("/api/account/me")
    b.get("/api/account/me")
    # B 必须是普通用户, 否则"403"这条断言会因为角色不对而失去意义
    assert accounts.get_role(ID_A) == "admin"
    assert accounts.get_role(ID_B) == "user"
    return a, b


def _denied(resp, what: str) -> None:
    assert resp.status_code == 403, f"{what}: 期望 403, 实际 {resp.status_code} {resp.text}"
    assert resp.json()["code"] == "ADMIN_REQUIRED", resp.text


# ================================================================
# 家族 1: 扩展表 (定义 / 数据 / 部署级 Key)
# ================================================================
def test_admin_creates_ext_config_then_user_cannot_touch_anything(two_accounts, _isolated):
    """A(admin) 建表 → B 的每一条写路径都被拒 → 配置与 parquet 一个字节都没变。

    端到端: A 走真实端点落盘(证明门控没把管理路径一起挡住), 之后 B 逐个端点打。
    """
    a, b = two_accounts
    created = a.post("/api/ext-data", json=EXT_CREATE_BODY)
    assert created.status_code == 200, created.text

    cfg_path = _isolated / "ext_data" / EXT_ID / "config.json"
    assert cfg_path.is_file()
    before = cfg_path.read_bytes()
    assert not (cfg_path.parent / "part.parquet").exists()

    for label, method, path, kwargs in EXT_WRITE_CALLS:
        resp = getattr(b, method.lower())(path, **kwargs)
        _denied(resp, f"B 的 ext-data {label}")

    assert cfg_path.read_bytes() == before, "普通用户的写请求竟然改到了配置"
    assert sorted(p.name for p in cfg_path.parent.iterdir()) == ["config.json"], (
        "普通用户的 upload/ingest 竟然往共享 parquet 写了数据"
    )


def test_user_cannot_overwrite_the_deployment_pull_api_key(two_accounts, _isolated):
    """拉取 Key 是**部署级**凭据(`save_deployment`), 普通用户不得改写。"""
    a, b = two_accounts
    assert a.post("/api/ext-data", json=EXT_CREATE_BODY).status_code == 200

    _denied(b.put(f"/api/ext-data/{EXT_ID}/api-key", json={"key": "B-KEY"}), "B 写 api-key")

    secrets_file = _isolated / "deployment_secrets.json"
    written = json.loads(secrets_file.read_text(encoding="utf-8")) if secrets_file.is_file() else {}
    assert f"ext_{EXT_ID}_api_key" not in written
    assert all("B-KEY" not in str(v) for v in written.values())


def test_duplicate_id_no_longer_leaks_existence_to_a_user(two_accounts, _isolated):
    """原实现: 重复 id 返回 400 "配置已存在" —— 既是存在性泄漏也是越权面。

    门控后普通用户拿到的是 403, 请求根本到不了存在性检查。
    """
    a, b = two_accounts
    assert a.post("/api/ext-data", json=EXT_CREATE_BODY).status_code == 200

    denied = b.post("/api/ext-data", json=EXT_CREATE_BODY)
    _denied(denied, "B 建重名扩展表")
    assert "已存在" not in denied.text


def test_user_can_still_read_ext_configs(two_accounts, _isolated):
    """门控只落在写面: 普通用户照旧能读列表 —— 不是把入口收窄成"什么都不收"。"""
    a, b = two_accounts
    assert a.post("/api/ext-data", json=EXT_CREATE_BODY).status_code == 200

    listed = b.get("/api/ext-data")
    assert listed.status_code == 200, listed.text
    assert [c["id"] for c in listed.json()["items"]] == [EXT_ID]


# ================================================================
# 家族 2: 自定义数据源 yaml (全站共享行情的取数入口)
# ================================================================
@pytest.fixture
def _no_reload(monkeypatch):
    """打桩注册表重扫。

    `save_data_source` 会调 `custom_sources.load_all()`, 而 `_PROVIDERS` 是**进程级
    全局** —— 真跑会把测试源的 provider 残留给别的用例(能力矩阵/数据源列表)。
    本套件验的是"谁有权写 yaml", 与重扫无关。
    """
    from app.data_providers import custom as custom_sources

    monkeypatch.setattr(custom_sources, "load_all", lambda: None)


def test_admin_can_write_data_source_then_user_cannot(two_accounts, _isolated, _no_reload):
    """A(admin) 建源 → B 覆盖/删除同一份 yaml 都被拒, 且 yaml 内容一字未改。"""
    a, b = two_accounts
    saved = a.post("/api/settings/data-sources",
                   json={"name": SOURCE_NAME, "display_name": "示例源"})
    assert saved.status_code == 200, saved.text

    yaml_path = _isolated / "data_sources" / f"{SOURCE_NAME}.yaml"
    assert yaml_path.is_file()
    before = yaml_path.read_bytes()

    # B 试图把取数地址改成自己的(命中即等于把全站行情重定向到任意 URL)
    overwritten = b.post("/api/settings/data-sources", json={
        "name": SOURCE_NAME,
        "display_name": "B 的源",
        "datasets": {"daily": {"url": "https://evil.example.com/daily"}},
    })
    _denied(overwritten, "B 覆盖数据源")
    assert yaml_path.read_bytes() == before, "普通用户覆盖了全站取数配置"

    _denied(b.delete(f"/api/settings/data-sources/{SOURCE_NAME}"), "B 删数据源")
    assert yaml_path.is_file(), "普通用户删掉了全站取数配置"
    assert yaml_path.read_bytes() == before


def test_admin_is_not_blocked_by_the_new_gate(two_accounts, _isolated):
    """门控必须按**角色**判别, 不能按路径一刀切。

    用"缺字段的非法请求"探管理侧: 拿到 422(而不是 403)就说明中间件放行了 ——
    同时不触发任何落盘副作用。
    """
    a, _b = two_accounts

    # 空 body: CustomSourceIn 缺必填 name → 422; 且 CreateExtReq 缺必填字段 → 422
    assert a.post("/api/settings/data-sources", json={}).status_code == 422
    assert a.post("/api/ext-data", json={}).status_code == 422
    assert a.put(f"/api/ext-data/{EXT_ID}", json={}).status_code in (404, 422)
    assert a.delete(f"/api/ext-data/{EXT_ID}").status_code == 404


# ================================================================
# 清单完整性: 新增的门控模式必须命中**真实**路由
# ================================================================
def test_new_admin_only_patterns_match_real_routes():
    """门控清单里写了路径却拼错 → "看着关着其实没关", 而且不会有人发现。

    这条只覆盖本文件新增的模式(ext-data / settings/data-sources)。既有清单里的
    `/api/strategy/*` 是**单数**、真实路由是 `/api/strategies/*`(复数), 那批模式实际
    命中不了任何路由 —— 已单独上报, 不在此测试的范围内。
    """
    patterns = (
        [r for r in app_main._ADMIN_ONLY_RE_POST if "/api/ext-data" in r.pattern
         or "/api/settings/data-sources" in r.pattern]
        + list(app_main._ADMIN_ONLY_RE_PUT)
        + [r for r in app_main._ADMIN_ONLY_RE_DELETE if "/api/ext-data" in r.pattern
           or "/api/settings/data-sources" in r.pattern]
    )
    assert patterns, "新增的门控模式一个都没找到 —— 清单被改动了?"
    routes = {r.path for r in app_main.app.routes}
    dead = [p.pattern for p in patterns if not any(p.match(path) for path in routes)]
    assert not dead, f"这些门控模式匹配不到任何真实路由: {dead}"

    # 反向: 真实存在的写路由都必须被覆盖到(逐个方法核对, 防漏一条)
    assert app_main._is_admin_only("POST", "/api/ext-data")
    assert app_main._is_admin_only("PUT", f"/api/ext-data/{EXT_ID}")
    assert app_main._is_admin_only("DELETE", f"/api/ext-data/{EXT_ID}")
    assert app_main._is_admin_only("PUT", f"/api/ext-data/{EXT_ID}/pull")
    assert app_main._is_admin_only("PUT", f"/api/ext-data/{EXT_ID}/api-key")
    assert app_main._is_admin_only("POST", "/api/ext-data/presets/ths_concepts/fetch")
    assert app_main._is_admin_only("POST", f"/api/ext-data/{EXT_ID}/upload")
    assert app_main._is_admin_only("POST", f"/api/ext-data/{EXT_ID}/ingest")
    assert app_main._is_admin_only("POST", f"/api/ext-data/{EXT_ID}/backfill")
    assert app_main._is_admin_only("POST", f"/api/ext-data/{EXT_ID}/fix-symbol")
    assert app_main._is_admin_only("POST", f"/api/ext-data/{EXT_ID}/pull/run")
    assert app_main._is_admin_only("POST", "/api/settings/data-sources")
    assert app_main._is_admin_only("DELETE", f"/api/settings/data-sources/{SOURCE_NAME}")
    # 只读/试拉端**刻意**不门控 (与 factors/custom-signals 的"读开放, 写专属"一致)
    for method, path in [
        ("GET", "/api/ext-data"),
        ("GET", f"/api/ext-data/{EXT_ID}/rows"),
        ("GET", f"/api/ext-data/{EXT_ID}/dimension-members"),
        ("GET", f"/api/ext-data/{EXT_ID}/api-key"),
        ("GET", "/api/settings/data-sources"),
        ("GET", f"/api/settings/data-sources/{SOURCE_NAME}"),
        ("POST", "/api/settings/data-sources/test"),
        ("POST", "/api/ext-data/detect-fields"),
        ("POST", "/api/ext-data/detect-url"),
        ("POST", f"/api/ext-data/{EXT_ID}/pull/test"),
    ]:
        assert not app_main._is_admin_only(method, path), f"{method} {path} 被误门控"
