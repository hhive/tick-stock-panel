"""复合/叠加策略的子策略覆盖值: 必须按**账户**解析, 不能绑死共享目录。

缺陷本体 (main.py 的 override_loader): 原实现是
``load_override(sid, user_root=store.data_dir)``。覆盖值存在每账户的
``<user_root>/user_data/strategy_overrides/<sid>.json``, 而 ``store.data_dir`` 是
**共享行情根** —— ``resolve_user_root`` 对它 fail-closed 抛 ``InvalidAccountIdError``,
于是**任何**调用都拿不到覆盖值。子策略于是恒用默认参数出数, 而 engine 的
``except Exception: pass`` 把它吞成静默 (worker 子进程侧同症状的那份已另行修掉)。

为什么断言打在"子策略实际用的参数"上:
只断言"调过 override_loader"或"没抛异常"与缺陷**互为盲区** —— 缺陷的表现正是
不抛异常且安静地用默认值。所以这里构造一个真实 composite + 真实子策略, 用
**结果集**证明子策略拿到的是账户参数 (默认值选 000001.SZ, 账户覆盖选 600000.SH)。

为什么用 main.py 的工厂而不是自己写一个 lambda:
自造替身的测试在"main.py 那一行没接上"时照样全绿。用
``app_main._strategy_override_loader()`` 拿到的是**引擎真正使用的那一个**,
把那行改回去 (或改错) 本文件会红。
"""
from __future__ import annotations

import importlib
from datetime import date

import pytest

from app import config as app_config
from app import main as app_main
from app.services import accounts, user_paths
from app.services import preferences
from app.services import account_sessions
from app.api import account as account_api
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyDataContext, StrategyEngine

# 默认选 000001.SZ, 账户覆盖后选 600000.SH —— 两个结果集不相交, 无从混淆
_DEFAULT_SYMBOL = "000001.SZ"
_OVERRIDE_SYMBOL = "600000.SH"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    accounts.reset_state_for_tests()
    importlib.reload(account_sessions)
    account_api._register_hits.clear()
    app_main._guest_hits.clear()
    yield tmp_path
    preferences.set_current_user_root(None)


def _child_code(strategy_id: str) -> str:
    """子策略: 选中哪个标的完全由参数 pick 决定 (默认 000001.SZ)。"""
    return f'''import polars as pl
META = {{
    "id": "{strategy_id}",
    "name": "{strategy_id}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {{"id": "pick", "label": "选中标的", "type": "str", "default": "{_DEFAULT_SYMBOL}"}},
    ],
    "scoring": {{"close": 1.0}},
}}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.col("symbol") == params["pick"]
'''


def _composite_code(strategy_id: str, child_id: str) -> str:
    return f'''META = {{
    "id": "{strategy_id}",
    "name": "{strategy_id}",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {{"id": "merge_mode", "type": "select", "options": ["union", "intersect"], "default": "union"}},
        {{"id": "min_confirm", "type": "int", "default": 0}},
    ],
    "children": [{{"strategy_id": "{child_id}", "weight": 1.0}}],
}}
EXECUTION_BACKEND = "composite"
'''


def _panel():
    import polars as pl

    return pl.DataFrame({
        "symbol": [_DEFAULT_SYMBOL, _OVERRIDE_SYMBOL],
        "close": [10.0, 20.0],
        "open": [10.0, 20.0],
        "high": [10.5, 20.5],
        "low": [9.5, 19.5],
        "volume": [1000.0, 2000.0],
    })


def _build(tmp_path, *, with_pool: bool = True):
    """真实目录布局 + **main.py 的**加载器 (with_pool=False 时不给加载器)。"""
    child_dir = tmp_path / "strategies" / "custom"
    comp_dir = tmp_path / "strategies" / "composite"
    child_dir.mkdir(parents=True)
    comp_dir.mkdir(parents=True)
    (child_dir / "pick_one.py").write_text(_child_code("pick_one"), encoding="utf-8")
    (comp_dir / "blend.py").write_text(
        _composite_code("blend", "pick_one"), encoding="utf-8"
    )
    engine = StrategyEngine(
        strategy_dirs=[child_dir, comp_dir],
        override_loader=app_main._strategy_override_loader() if with_pool else None,
    )
    context = StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 1, 2),
        current=_panel(),
    )
    return engine, context


def _run_symbols(engine, context) -> set[str]:
    # 测试 panel 无 amount 等列, 关掉基础过滤 (composite 会把它透传给子策略)
    result = engine.run("blend", context, overrides={"basic_filter": {"enabled": False}})
    return {row["symbol"] for row in result.rows}


def test_composite_substrategy_uses_the_account_override_on_request_path(_isolated):
    """请求路径 (contextvar 已注入账户根) 上, 子策略必须用**该账户**的参数。

    这正是缺陷的可观测症状: 改前无论有没有账户上下文, 子策略都拿默认值。
    """
    account_root = user_paths.user_root(1)
    strategy_config.save_override(
        "pick_one", {"params": {"pick": _OVERRIDE_SYMBOL}}, user_root=account_root
    )

    # 模拟认证中间件的注入 (请求路径上由它 set)
    token = preferences.set_current_user_root(account_root)
    try:
        engine, context = _build(_isolated)
        assert _run_symbols(engine, context) == {_OVERRIDE_SYMBOL}
    finally:
        preferences.reset_current_user_root(token)


def test_composite_substrategy_isolation_between_accounts(_isolated):
    """A 的覆盖值不得串到 B —— 否则就是把"我的参数"变成了所有人的参数。"""
    strategy_config.save_override(
        "pick_one", {"params": {"pick": _OVERRIDE_SYMBOL}}, user_root=user_paths.user_root(1)
    )
    engine, context = _build(_isolated)

    token = preferences.set_current_user_root(user_paths.user_root(2))
    try:
        # 账户 2 没配过 ⇒ 默认参数
        assert _run_symbols(engine, context) == {_DEFAULT_SYMBOL}
    finally:
        preferences.reset_current_user_root(token)

    token = preferences.set_current_user_root(user_paths.user_root(1))
    try:
        assert _run_symbols(engine, context) == {_OVERRIDE_SYMBOL}
    finally:
        preferences.reset_current_user_root(token)


def test_composite_without_account_context_falls_back_to_defaults(_isolated):
    """后台线程/调度器没有账户上下文 ⇒ 退回默认参数, 且**不得炸**。

    没有上下文时 resolve_user_root 抛 MissingUserContextError, 由 engine 的
    ``except Exception: pass`` 吞掉 —— 与改动前**同样是空**, 后台路径不退化。
    这条守住"为了修请求路径而把后台线程打死"的另一个方向。
    """
    strategy_config.save_override(
        "pick_one", {"params": {"pick": _OVERRIDE_SYMBOL}}, user_root=user_paths.user_root(1)
    )
    assert preferences.current_user_root() is None

    engine, context = _build(_isolated)
    assert _run_symbols(engine, context) == {_DEFAULT_SYMBOL}


def test_loader_is_wired_into_the_engine_main_py_builds():
    """接缝检查: main.py 造引擎时必须用这个加载器 (而不是自造一个 / 传 None)。

    只断言工厂本身的行为是**不够**的 —— 引擎构造处把 override_loader 传成 None
    或另一个 lambda, 上面几条用例照样绿, 而线上依旧是"恒用默认参数"。
    """
    import inspect
    from app import main as main_module

    src = inspect.getsource(main_module._application_lifespan)
    assert "override_loader=_strategy_override_loader()" in src
    assert "user_root=store.data_dir" not in src
