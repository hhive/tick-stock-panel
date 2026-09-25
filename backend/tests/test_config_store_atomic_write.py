"""配置/凭证 JSON 存储的原子写测试 — 写到一半不该把已有配置换成空文件。

`fs_utils` 的模块 docstring 写着「新代码统一用本模块的 atomic_write_text」,
`lots.py`、`monitor_rules.py` 也都照做了; 但下面这些存储还在裸 `write_text`:
secrets_store、auth、preferences、ExtConfigStore、策略 override、自定义信号、
自定义因子、自定义分析菜单、自定义数据源 YAML。它们的读侧都吞掉解析错误返回
默认值 (`{}` 或跳过该项), 所以半截文件不会报错, 只会安静地把配置清空。

这里用「写入过程中失败」模拟磁盘写满/进程被杀: 让 `Path.write_text` 只写前几个
字节就抛 OSError。裸写会把目标文件本身截断; 原子写截断的是 .tmp, `os.replace`
不会执行, 目标文件原封不动。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import secrets_store
from app.api import analysis
from app.data_providers.custom import loader as custom_loader
from app.factors import store as factor_store
from app.services import preferences
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField
from app.strategy import config as strat_config
from app.strategy import custom_signals


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """共享数据目录 (settings.data_dir): 行情、扩展表配置、自定义数据源 YAML 等
    所有人一份。注意它**不是**账户根 —— 拿它当账户根会让所有账户读写同一份文件。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    preferences._invalidate_cache()
    yield tmp_path
    preferences._invalidate_cache()


@pytest.fixture()
def user_root(data_dir: Path) -> Path:
    """**本账户**的根目录 —— 生产形态 ``<data_dir>/users/<账号ID>``。

    每用户存储 (secrets/覆盖配置/自定义因子) 没有共享回退, 需要账户上下文:
    真实请求由认证中间件注入 user_root, 这里手工注入同一 contextvar。
    (偏好按归属分派: 每用户键落账户根, 其余落共享 data_dir; 自定义信号是部署级。)
    """
    from app.services import user_paths

    return user_paths.user_root(1)


@pytest.fixture(autouse=True)
def _user_ctx(user_root: Path):
    token = preferences.set_current_user_root(user_root)
    yield user_root
    preferences.reset_current_user_root(token)


@pytest.fixture()
def torn_write(monkeypatch: pytest.MonkeyPatch):
    """让下一次 write_text 只写前 8 个字节然后失败。"""
    real = Path.write_text

    def _torn(self: Path, data: str, *args, **kwargs):
        real(self, data[:8], *args, **kwargs)
        raise OSError(28, "No space left on device")

    def _arm() -> None:
        monkeypatch.setattr(Path, "write_text", _torn)

    return _arm


def test_preferences_survive_a_torn_write(data_dir: Path, torn_write) -> None:
    preferences.save({"theme": "dark", "kline_compress": True})
    assert preferences.load()["theme"] == "dark"

    torn_write()
    with pytest.raises(OSError):
        preferences.save({"theme": "light"})

    preferences._invalidate_cache()
    assert preferences.load() == {"theme": "dark", "kline_compress": True}


def test_secrets_survive_a_torn_write(data_dir: Path, torn_write) -> None:
    secrets_store.save({"tickflow_token": "keep-me"})
    assert secrets_store.load()["tickflow_token"] == "keep-me"

    torn_write()
    with pytest.raises(OSError):
        secrets_store.save({"tickflow_token": "replacement"})

    assert secrets_store.load() == {"tickflow_token": "keep-me"}


def test_ext_config_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    store = ExtConfigStore(data_dir / "ext_data")
    config = ExtConfig(
        id="hot",
        label="人气",
        mode="timeseries",
        fields=[ExtField("symbol", "string"), ExtField("heat", "float")],
    )
    store.upsert(config)
    assert [c.id for c in store.load_all()] == ["hot"]

    config.label = "人气榜"
    torn_write()
    with pytest.raises(OSError):
        store.upsert(config)

    reloaded = ExtConfigStore(data_dir / "ext_data").load_all()
    assert [c.id for c in reloaded] == ["hot"]
    assert reloaded[0].label == "人气"


def test_strategy_override_survives_a_torn_write(user_root: Path, torn_write) -> None:
    """load_override 吞异常返回 {} —— 半截文件会让策略参数静默回默认值。"""
    strat_config._override_cache.clear()
    strat_config._override_cache_sig.clear()
    strat_config.save_override("s1", {"params": {"period": 20}}, user_root=user_root)
    # 覆盖配置落在账户根之下 (生产形态), 不落共享 data_dir
    assert (user_root / "user_data" / "strategy_overrides" / "s1.json").exists()
    assert strat_config.load_override("s1", user_root=user_root) == {"params": {"period": 20}}

    torn_write()
    with pytest.raises(OSError):
        strat_config.save_override("s1", {"params": {"period": 60}}, user_root=user_root)

    strat_config._override_cache.clear()
    strat_config._override_cache_sig.clear()
    assert strat_config.load_override("s1", user_root=user_root) == {"params": {"period": 20}}


def test_custom_signal_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    """custom_signals.load_all 跳过损坏文件 —— 半截文件会让该信号静默消失。

    信号定义是**部署级**一份 (``<data_dir>/user_data/custom_signals``), 不是每用户存储。
    """
    sig = {
        "id": "vol_up", "name": "放量", "kind": "entry", "enabled": True,
        "conditions": [{"left": "volume", "op": ">", "right": "0", "leftDays": 0, "rightDays": 0}],
    }
    custom_signals.save_one(sig)
    assert (data_dir / "user_data" / "custom_signals" / "vol_up.json").exists()
    assert [s["id"] for s in custom_signals.load_all()] == ["vol_up"]

    torn_write()
    with pytest.raises(OSError):
        custom_signals.save_one({**sig, "name": "放量2"})

    assert custom_signals.load_all() == [sig]


def test_custom_factor_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    """factors.store.load_all 跳过损坏文件 —— 半截文件会让该因子静默消失。"""
    definition = {"id": "uf_mom", "kind": "custom", "label": "动量", "status": "draft"}
    factor_store.save_one(definition)
    assert (data_dir / "user_data" / "custom_factors" / "uf_mom.json").exists()
    assert [d["id"] for d in factor_store.load_all()] == ["uf_mom"]

    torn_write()
    with pytest.raises(OSError):
        factor_store.save_one({**definition, "label": "动量2"})

    assert factor_store.load_all() == [definition]


def test_analysis_menu_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    """analysis._load_saved 遇到解析失败直接 continue —— 半截文件会让菜单静默消失。"""
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))))
    )
    analysis._save(request, analysis.AnalysisMenu(id="limit_up", label="涨停分析", data_source="hot"))
    assert [m.label for m in analysis._load_saved(request)] == ["涨停分析"]

    torn_write()
    with pytest.raises(OSError):
        analysis._save(request, analysis.AnalysisMenu(id="limit_up", label="涨停复盘", data_source="hot"))

    assert [(m.id, m.label) for m in analysis._load_saved(request)] == [("limit_up", "涨停分析")]


def test_custom_source_yaml_survives_a_torn_write(data_dir: Path, torn_write) -> None:
    """自定义数据源 YAML 被截断后, loader.load_all 按单个文件 load_config 会失败或读到残缺配置。"""
    config = {
        "name": "demo",
        "display_name": "演示源",
        "datasets": {"daily": {"url": "https://example.test/daily", "method": "GET"}},
    }
    path = custom_loader.save_config("demo", config)
    before = path.read_text(encoding="utf-8")
    assert custom_loader.load_config(path).display_name == "演示源"

    torn_write()
    with pytest.raises(OSError):
        custom_loader.save_config("demo", {**config, "display_name": "演示源2"})

    assert path.read_text(encoding="utf-8") == before
    reloaded = custom_loader.load_config(path)
    assert reloaded.display_name == "演示源"
    assert list(reloaded.datasets) == ["daily"]


def test_a_normal_save_still_writes_what_it_was_given(data_dir: Path, user_root: Path) -> None:
    """没有失败时行为不变 —— 内容、合并语义和文件位置都照旧。

    偏好按归属分派: theme/kline_compress 是 GLOBAL 键 (落共享 data_dir); secrets
    是每用户存储 (落账户根之下)。
    """
    preferences.save({"theme": "dark"})
    preferences.save({"kline_compress": True})
    assert preferences.load() == {"theme": "dark", "kline_compress": True}

    secrets_store.save({"a": "1"})
    secrets_store.save({"b": "2"})
    assert secrets_store.load() == {"a": "1", "b": "2"}

    written = json.loads(
        (data_dir / "user_data" / "preferences.json").read_text(encoding="utf-8")
    )
    assert written == {"theme": "dark", "kline_compress": True}
    written_secrets = json.loads(
        (user_root / "user_data" / "secrets.json").read_text(encoding="utf-8")
    )
    assert written_secrets == {"a": "1", "b": "2"}
    # 每用户存储不得写进共享 data_dir
    assert not (data_dir / "user_data" / "secrets.json").exists()


def test_no_tmp_file_is_left_behind(data_dir: Path, user_root: Path) -> None:
    preferences.save({"theme": "dark"})
    secrets_store.save({"a": "1"})
    strat_config.save_override("s1", {"params": {}}, user_root=user_root)
    custom_signals.save_one({"id": "vol_up", "conditions": []})
    factor_store.save_one({"id": "uf_mom", "label": "动量"})
    custom_loader.save_config("demo", {"name": "demo", "datasets": {}})

    # 账户根在 data_dir 之下, rglob 覆盖到每用户存储 —— 断言扫描不是空转
    assert (user_root / "user_data" / "secrets.json").exists()
    assert (data_dir / "user_data" / "custom_factors" / "uf_mom.json").exists()
    leftovers = sorted(p.name for p in data_dir.rglob("*.tmp"))
    assert leftovers == []
