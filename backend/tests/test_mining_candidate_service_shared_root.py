"""候选服务的**共享策略库根**必须是显式传入的 (复核 C2 追加项)。

``MiningCandidateService`` 的发布产物落**共享策略库** ``<data_dir>/strategies``
(与 ``main.py`` 的引擎目录集同源 —— 引擎是进程级单例、目录集启动后固定, 产物写进
账户根的话 reload 后 ``get()`` 落空, 发布会被回滚)。

此前这个根由服务内部隐式解析 (``shared_strategies_root()`` → ``settings.data_dir``),
生产上恰好与 ``DataStore()`` 默认取到的目录相同 —— 那是"蒙对"而不是"接对": 一旦
``settings.data_dir`` 被覆盖 (测试、多部署实例、后续改成可配置), 发布产物就会与
引擎扫的目录漂移, 且失败点很远 (发布成功、reload 后策略消失)。

因此 API 侧**显式**把它从 ``request.app.state.repo.store.data_dir / "strategies"``
传进去 —— 与引擎目录同一个来源。

本文件的断言刻意让 ``settings.data_dir`` 与 ``repo.store.data_dir`` **不相等**:
两者相同时"隐式解析"与"显式传入"结果相同, 断言会空转 (蒙对测不出来)。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config
from app.api import mining as mining_api
from app.services import preferences
from app.services.mining_jobs import MiningRunStore


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


class _ServiceSpy:
    """替换候选服务: 只记录构造参数, 业务方法一律抛 KeyError (端点映射 404)。"""

    captured: dict = {}

    def __init__(self, user_root, run_store, candidate_store, strategy_engine, **kwargs):
        type(self).captured = {
            "user_root": user_root,
            "run_store": run_store,
            "candidate_store": candidate_store,
            "strategy_engine": strategy_engine,
            **kwargs,
        }

    def promote(self, run_id, signature):
        raise KeyError(run_id)

    def publish(self, run_id, signature):
        raise KeyError(run_id)


@pytest.fixture
def wired_client(monkeypatch, tmp_path: Path):
    """共享行情根 (repo.store.data_dir) 与 settings.data_dir **故意不同**。"""
    market_root = tmp_path / "market"      # 引擎目录集的来源: repo.store.data_dir
    decoy_settings_root = tmp_path / "decoy"  # settings.data_dir: 隐式解析会拿到这个
    monkeypatch.setattr(app_config.settings, "data_dir", decoy_settings_root)
    account_root = decoy_settings_root / "users" / "1"
    token = preferences.set_current_user_root(account_root)

    app = FastAPI()
    app.include_router(mining_api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=market_root))
    app.state.mining_manager = _StubManager()
    app.state.strategy_engine = SimpleNamespace()
    monkeypatch.setattr(
        "app.services.mining_candidates.MiningCandidateService", _ServiceSpy,
    )
    _ServiceSpy.captured = {}
    try:
        yield TestClient(app), market_root, decoy_settings_root, account_root
    finally:
        preferences.reset_current_user_root(token)


def _build_service(client: TestClient) -> None:
    """触发一次需要候选服务的端点 (构造发生在业务逻辑之前, 404 不影响断言)。"""
    response = client.post(
        "/api/backtest/mining/runs/missing-run/candidates/missing-sig/promote",
    )
    assert response.status_code == 404


def test_candidate_service_gets_shared_library_root_from_repo_store(wired_client) -> None:
    client, market_root, decoy_root, account_root = wired_client

    _build_service(client)

    assert "strategies_root" in _ServiceSpy.captured, (
        "共享策略库根必须以关键字显式传入 (不能靠服务内部读 settings.data_dir)"
    )
    assert Path(_ServiceSpy.captured["strategies_root"]) == (market_root / "strategies")
    # 关键: 不是"蒙对"的 settings.data_dir —— 两者在本用例里不同, 所以这条能真的挡住
    # "隐式解析" 的旧实现。
    assert Path(_ServiceSpy.captured["strategies_root"]) != (decoy_root / "strategies")
    assert market_root != decoy_root


def test_candidate_service_still_gets_the_account_root(wired_client) -> None:
    client, _market_root, _decoy_root, account_root = wired_client

    _build_service(client)

    assert Path(_ServiceSpy.captured["user_root"]) == account_root
