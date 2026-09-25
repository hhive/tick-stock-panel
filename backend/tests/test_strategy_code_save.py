from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import config as app_config
from app.api.strategy import (
    StrategyCodeSaveRequest,
    StrategyCodeValidateRequest,
    _prepare_strategy_code,
    _save_strategy_code,
)
from app.strategy.engine import StrategyEngine

@pytest.fixture(autouse=True)
def _current_user_context(tmp_path, monkeypatch):
    """把「当前账户根」设为本次用例的临时目录。

    HTTP handler 通过 user_paths 的统一接缝解析账户私有目录 (真实请求里由认证
    中间件注入 contextvar); 这里直接调用 handler 或只挂了 router 的 TestClient,
    必须自己注入, 否则 fail-closed 抛 MissingUserContextError。

    账户根用**生产形态** ``<data_dir>/users/1``, 而不是把共享 data_dir 当根。
    后者有两个问题: 每用户存储会退化成共享文件(所有账户读写同一份), 且
    handler 把根**显式**传给 store 时 resolve_user_root 会拒绝它 —— 共享目录
    不能作账户根。

    **必须先 patch settings.data_dir**: user_root(1) 是按全局 data_dir 解析的,
    不 patch 就会落到**真实仓库的 data/users/1/** 里 —— 测试会把策略源码与
    __pycache__ 写进真实目录(实测污染过)。不是"顺手加一行", 而是本 fixture 成立
    的前提。
    """
    from app.services import preferences, user_paths

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    token = preferences.set_current_user_root(user_paths.user_root(1))
    yield tmp_path
    preferences.reset_current_user_root(token)



def _code(strategy_id: str, name: str = "测试策略") -> str:
    return f'''"""测试策略"""
import polars as pl

META = {{
    "id": "{strategy_id}",
    "name": "{name}",
    "description": "测试描述",
    "tags": ["测试"],
    "params": [],
    "scoring": {{}},
}}

ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 20

RULES = """
1. 测试规则一
2. 测试规则二
3. 测试规则三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return pl.lit(True)
'''


def _user_root():
    """账户私有根的**生产形态** ``<data_dir>/users/1`` (与 fixture 注入的一致)。"""
    from app.services import user_paths

    return user_paths.user_root(1)


def _request(tmp_path):
    """请求替身: 策略源码写在**账户根**之下, 共享行情仍读 data_dir。"""
    user_root = _user_root()
    ai_dir = user_root / "strategies" / "ai"
    custom_dir = user_root / "strategies" / "custom"
    engine = StrategyEngine(strategy_dirs=[custom_dir, ai_dir])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, strategy_engine=engine)))


def test_prepare_strategy_code_rejects_forbidden_import():
    req = StrategyCodeValidateRequest(
        strategy_id="custom_bad",
        code='''import os\nMETA = {"id": "custom_bad"}\n''',
    )

    with pytest.raises(ValueError, match="禁止 import os"):
        _prepare_strategy_code(req)


def test_prepare_strategy_code_rejects_unknown_scoring_field():
    req = StrategyCodeValidateRequest(
        strategy_id="custom_bad_score",
        code=_code("custom_bad_score").replace(
            '"scoring": {},',
            '"scoring": {"volume_surge": 1.0},',
        ),
    )

    with pytest.raises(ValueError, match="volume_surge"):
        _prepare_strategy_code(req)


def test_save_strategy_code_creates_ai_strategy_in_ai_dir(tmp_path):
    request = _request(tmp_path)
    req = StrategyCodeSaveRequest(
        strategy_id="ai_saved",
        target_source="ai",
        mode="create",
        code=_code("wrong"),
        name="AI 策略",
    )

    result = _save_strategy_code(req, request)

    assert result["ok"] is True
    assert result["source"] == "ai"
    assert (_user_root() / "strategies" / "ai" / "ai_saved.py").exists()
    loaded = request.app.state.strategy_engine.get("ai_saved")
    assert loaded.source == "ai"
    assert loaded.file_path == _user_root() / "strategies" / "ai" / "ai_saved.py"


def test_save_strategy_code_creates_custom_strategy_in_custom_dir(tmp_path):
    request = _request(tmp_path)
    req = StrategyCodeSaveRequest(
        strategy_id="custom_saved",
        target_source="custom",
        mode="create",
        code=_code("wrong"),
        name="自定义策略",
    )

    result = _save_strategy_code(req, request)

    assert result["ok"] is True
    assert result["source"] == "custom"
    assert (_user_root() / "strategies" / "custom" / "custom_saved.py").exists()
    loaded = request.app.state.strategy_engine.get("custom_saved")
    assert loaded.source == "custom"
    assert loaded.file_path == _user_root() / "strategies" / "custom" / "custom_saved.py"


def test_save_strategy_code_updates_existing_source_file(tmp_path):
    request = _request(tmp_path)
    create = StrategyCodeSaveRequest(
        strategy_id="custom_update",
        target_source="custom",
        mode="create",
        code=_code("custom_update", "旧名称"),
    )
    _save_strategy_code(create, request)

    update = StrategyCodeSaveRequest(
        strategy_id="custom_update",
        target_source="ai",
        mode="update",
        code=_code("custom_update", "新名称"),
    )
    result = _save_strategy_code(update, request)

    assert result["source"] == "custom"
    custom_path = _user_root() / "strategies" / "custom" / "custom_update.py"
    assert custom_path.exists()
    assert not (_user_root() / "strategies" / "ai" / "custom_update.py").exists()
    assert '"name": "新名称"' in custom_path.read_text(encoding="utf-8")


def test_save_strategy_code_rejects_undefined_custom_signal(tmp_path):
    """REQUIRED_FEATURES 引用未定义的自定义信号 → 拒绝保存并恢复文件。

    回归: 之前保存不校验, 运行期才抛 polars 缺列错 (500)。
    """
    request = _request(tmp_path)
    code = _code("custom_missing_sig") + (
        '\nREQUIRED_FEATURES = {"csg_oversold_macd_about_to_golden"}\n'
    )
    req = StrategyCodeSaveRequest(
        strategy_id="custom_missing_sig",
        target_source="custom",
        mode="create",
        code=code,
        name="引用不存在信号的策略",
    )

    with pytest.raises(ValueError, match="csg_oversold_macd_about_to_golden"):
        _save_strategy_code(req, request)

    # 校验失败不落盘
    assert not (_user_root() / "strategies" / "custom" / "custom_missing_sig.py").exists()


def test_save_strategy_code_ok_when_custom_signal_defined(tmp_path):
    """信号已定义时, 引用它的策略可以正常保存。

    自定义信号是**部署级**存储(见 custom_signals._dir), 与账户无关; fixture 已把
    settings.data_dir 指向 tmp, 因此 save_one 落在临时目录里。
    """
    from app.strategy import custom_signals

    custom_signals.save_one({
        "id": "oversold_macd_about_to_golden",
        "name": "超跌接近金叉",
        "kind": "entry",
        "conditions": [
            {"left": "momentum_60d", "op": "<=", "right": "-0.30",
             "leftDays": 0, "rightDays": 0},
        ],
        "enabled": True,
    })
    request = _request(tmp_path)
    code = _code("custom_with_sig") + (
        '\nREQUIRED_FEATURES = {"csg_oversold_macd_about_to_golden"}\n'
    )
    req = StrategyCodeSaveRequest(
        strategy_id="custom_with_sig",
        target_source="custom",
        mode="create",
        code=code,
        name="引用已定义信号的策略",
    )

    result = _save_strategy_code(req, request)
    assert result["ok"] is True
    loaded = request.app.state.strategy_engine.get("custom_with_sig")
    assert "csg_oversold_macd_about_to_golden" in loaded.required_features
