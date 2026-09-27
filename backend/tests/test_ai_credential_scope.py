"""每用户 AI 凭据的作用域(A5) + 凭据存储并发一致性(B1)。

背景(2026-09-27 多用户复核, 已实证):`POST /api/settings/ai` 保存时把**每用户**配置
同步写进进程级 `settings` 单例(`ai_api_key` / `ai_model` / `ai_user_agent` /
`ai_max_output_tokens` / `ai_context_window`), 而 `secrets_store.get_ai_key()` 的
第三档回落读的正是这个可变单例。串号链路:

    甲(普通用户)保存自己的 Key
      → settings.ai_api_key = 甲的 Key(全局单例, 进程内所有账户共用)
      → 乙(未自配)调 ``get_ai_key()`` → 拿到**甲的 Key**
      → 乙的提问以甲的 Key 出网, 记在甲的网关账上, 且设置页回显甲的脱敏 Key

`DEPLOYMENT_KEYS` 里只有两条 tickflow 键, 说明 `ai_api_key` 本就是**每用户**键;
「env 里给了部署级默认 Key」是正当能力(未自配账户应能拿到), 出错的是回落目标从
「env 初值快照」变成了「可被任意请求改写的运行时状态」。

B1:`save()` / `save_deployment()` / `clear()` / `clear_deployment()` 都是
`load → update → atomic_write_text`, 全程无锁。原子写只保证**不出现半截文件**,
不保证**不丢更新** —— 并发保存时后写者拿的是先写者之前的旧快照, 先写者的键被静默抹掉。

本文件每个用例都钉住一条可被单独破坏的保证; 变异反证(去掉对应修复即红)见各用例注释。
"""
from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path

import pytest

from app import config as app_config
from app import secrets_store
from app.api import settings as settings_api
from app.config import settings
from app.services import preferences

USER_A_KEY = "sk-user-A"

# 被错误写进单例的每用户字段 —— 就是它们让乙读到了甲的配置
PER_USER_AI_FIELDS = (
    "ai_api_key",
    "ai_model",
    "ai_user_agent",
    "ai_max_output_tokens",
    "ai_context_window",
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """隔离共享 data_dir + 账户上下文, 并把单例上的每用户 AI 字段钉成 env 初值。

    钉值是必须的: `settings` 是进程级单例, 同进程里别的用例(以及缺陷本身)会把它
    改掉; 不钉的话「保存前后对比」会被上一个用例留下的污染值骗过去。
    """
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    for field, value in (
        ("ai_api_key", ""),
        ("ai_model", ""),
        ("ai_user_agent", "UA-env-initial"),
        ("ai_max_output_tokens", 16384),
        ("ai_context_window", 128000),
    ):
        monkeypatch.setattr(settings, field, value, raising=False)
    token = preferences.set_current_user_root(None)
    yield tmp_path
    preferences.reset_current_user_root(token)


def _root(tmp_path: Path, n: int) -> Path:
    return tmp_path / "users" / str(n)


def _as(root: Path, fn):
    """以某账户身份执行(模拟请求路径的 contextvar 注入)。"""
    token = preferences.set_current_user_root(root)
    try:
        return fn()
    finally:
        preferences.reset_current_user_root(token)


def _save_a_own_ai_config(root: Path) -> None:
    _as(root, lambda: settings_api.save_ai_settings(
        settings_api.AiSettingsIn(
            api_key=USER_A_KEY,
            model="gpt-of-A",
            user_agent="UA-of-A",
            max_output_tokens=4096,
            context_window=64000,
        )
    ))


# ================================================================
# A5 每用户 AI 凭据不得回落进进程级单例
# ================================================================

def test_a_saved_key_never_leaks_to_another_account(tmp_path, monkeypatch):
    """甲保存自己的 Key 后, 乙(未自配)读数不得变成甲的 Key。

    变异反证(两条独立的):
      - 把 `get_ai_key` 的回落改回读 `settings.ai_api_key` → 本用例红;
      - 把 `settings.ai_api_key = req.api_key` 加回保存路径 → 本用例红。
    """
    a, b = _root(tmp_path, 1), _root(tmp_path, 2)

    _save_a_own_ai_config(a)

    assert secrets_store.load(a)["ai_api_key"] == USER_A_KEY, "前置: 甲确实存下了自己的 Key"
    got = secrets_store.get_ai_key(b)
    assert got != USER_A_KEY, "乙读到了甲的 Key"
    assert got != "gpt-of-A"
    # 未自配账户只能拿到部署级 env 默认(此处 env 未给默认值 → 空)
    assert got == app_config.AI_ENV_DEFAULTS.get("ai_api_key", "")

    # 甲的配置项同理: 不得成为乙的回落值
    assert secrets_store.get_ai_config("ai_model", "", user_root=b) != "gpt-of-A"
    assert secrets_store.get_ai_config("ai_user_agent", "", user_root=b) != "UA-of-A"

    # 直接钉住「回落档不读进程单例」: 把单例摆成旧实现留下的形态(上一个保存者的值),
    # 乙的读取必须逐位不受影响。少了这条, 只测「保存 + 读取」的组合会让
    # 「回落改回单例」这一半单独撤掉时测试仍然绿。
    monkeypatch.setattr(settings, "ai_api_key", "sk-left-over-by-last-saver")
    monkeypatch.setattr(settings, "ai_model", "gpt-left-over")
    assert secrets_store.get_ai_key(b) == ""
    # 期望值取 env 快照而不是写死空串: 2026-09-27 起部署级默认模型非空
    # (`config.DEFAULT_AI_MODEL`)。写死空串会让「把回落改回读单例」这种回归
    # 因为默认值本身变了而被误报成通过 —— 所以这里显式点名它**不得**是单例里那个值。
    assert secrets_store.get_ai_config("ai_model", "", user_root=b) == app_config.AI_ENV_DEFAULTS.get("ai_model", "")
    assert secrets_store.get_ai_config("ai_model", "", user_root=b) != "gpt-left-over"


def test_saving_never_mutates_the_process_singleton(tmp_path):
    """保存只写**该账户**的凭据文件, 不得改写进程级单例。

    变异反证: 恢复任一行 `settings.ai_<每用户字段> = req.*` → 本用例红。
    """
    a = _root(tmp_path, 1)
    before = {f: getattr(settings, f) for f in PER_USER_AI_FIELDS}

    _save_a_own_ai_config(a)

    after = {f: getattr(settings, f) for f in PER_USER_AI_FIELDS}
    assert after == before, "保存请求改写了进程级单例"
    # 值确实落进了甲自己的文件, 不是「什么都没存」
    stored = secrets_store.load(a)
    assert stored["ai_api_key"] == USER_A_KEY
    assert stored["ai_model"] == "gpt-of-A"
    assert stored["ai_max_output_tokens"] == 4096
    assert stored["ai_context_window"] == 64000


def test_env_default_still_reaches_unconfigured_accounts(tmp_path, monkeypatch):
    """「部署级默认 Key」是正当能力: 修 A5 不能把回落整条砍掉。

    env(或 .env)里给了默认 Key 时, 未自配账户仍应以它为凭据, 出网不受影响。
    变异反证: 把回落改成硬编码空串(「无账户凭据就是没有」)→ 本用例红。
    """
    monkeypatch.setattr(
        app_config,
        "AI_ENV_DEFAULTS",
        {
            **app_config.AI_ENV_DEFAULTS,
            "ai_api_key": "sk-deploy-default",
            "ai_model": "gpt-deploy-default",
            "ai_max_output_tokens": 8192,
        },
    )
    a, b = _root(tmp_path, 1), _root(tmp_path, 2)

    assert secrets_store.get_ai_key(b) == "sk-deploy-default"
    assert secrets_store.get_ai_config("ai_model", "", user_root=b) == "gpt-deploy-default"
    assert secrets_store.get_ai_config_int("ai_max_output_tokens", 0, user_root=b) == 8192

    # 自配账户仍然以自己的为准 —— 部署默认只是回落, 不是覆盖
    _save_a_own_ai_config(a)
    assert secrets_store.get_ai_key(a) == USER_A_KEY
    assert secrets_store.get_ai_key(b) == "sk-deploy-default"


def test_ai_settings_default_user_agent_comes_from_env_snapshot(tmp_path, monkeypatch):
    """GET /api/settings 展示的 UA 默认值取 env 快照, 不取可被请求改写的单例。

    两个方向都钉住:
      - env 给了该字段 → 展示 env 初值; 甲保存过的 UA 不得顶替它;
      - env 没给该字段 → 展示空(「没有默认」), 同样不得顶替成甲保存过的值。
    后一条是这条 getter 的**默认值参数**的判别点。

    变异反证: 把 `get_settings` 里那处读回 `settings.ai_user_agent` → 第二条断言红。
    """
    a, b = _root(tmp_path, 1), _root(tmp_path, 2)
    _save_a_own_ai_config(a)  # 甲自己的 UA 存进了甲的凭据文件(UA-of-A)
    monkeypatch.setattr(settings, "ai_user_agent", "UA-of-A", raising=False)

    monkeypatch.setattr(app_config, "AI_ENV_DEFAULTS", {**app_config.AI_ENV_DEFAULTS, "ai_user_agent": "UA-env"})
    assert secrets_store.get_ai_key(a) == USER_A_KEY
    assert secrets_store.get_ai_config("ai_user_agent", "", user_root=b) == "UA-env"
    assert _as(b, settings_api.get_settings)["ai_user_agent"] == "UA-env"

    # env 未提供该字段的默认值(快照里没有该键)
    monkeypatch.setattr(app_config, "AI_ENV_DEFAULTS", {})
    assert _as(b, settings_api.get_settings)["ai_user_agent"] == "", "展示值来自单例残留"


# ================================================================
# B1 凭据存储的 read-modify-write 必须串行化
# ================================================================

def _race_two_writers(monkeypatch, targets: list) -> list[BaseException]:
    """让两个写线程用屏障把各自的**写**卡在同一时刻。

    无锁实现下两次 `load()` 都发生在任何写之前 ⇒ 后写者用旧快照覆盖先写者,
    必有一个键消失。有锁时第二个线程根本进不来, 屏障因超时破裂被吞掉
    (BrokenBarrierError), 两次写先后完成, 两个键都在。
    """
    barrier = threading.Barrier(len(targets), timeout=0.25)
    real_write = secrets_store.atomic_write_text

    def _barriered_write(path, text, *, mode=None):
        # 有锁时其余线程根本没到屏障 → 超时破裂, 这里吞掉即可
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        real_write(path, text, mode=mode)

    errors: list[BaseException] = []

    def _run(fn) -> None:
        try:
            fn()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=_run, args=(fn,)) for fn in targets]
    monkeypatch.setattr(secrets_store, "atomic_write_text", _barriered_write)
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "线程未在超时内结束"
    return errors


def test_concurrent_saves_to_one_account_do_not_lose_updates(tmp_path, monkeypatch):
    """并发保存两个不同键 → 两个键都在。

    变异反证: 去掉 `save()` 的 `_SAVE_LOCK` → 后写者覆盖先写者, 只剩一个键 → 红。
    """
    a = _root(tmp_path, 1)

    errors = _race_two_writers(monkeypatch, [
        lambda: secrets_store.save({"ai_api_key": USER_A_KEY}, user_root=a),
        lambda: secrets_store.save({"ai_model": "gpt-of-B"}, user_root=a),
    ])

    assert not errors, errors
    written = json.loads((a / "user_data" / "secrets.json").read_text(encoding="utf-8"))
    assert written.get("ai_api_key") == USER_A_KEY
    assert written.get("ai_model") == "gpt-of-B"


def test_concurrent_deployment_saves_do_not_lose_updates(tmp_path, monkeypatch):
    """部署级文件与每用户文件同一把锁: 并发保存两个不同键 → 两个键都在。

    变异反证: 只给 `save()` 加锁、`save_deployment()` 不加 → 本用例红
    (并发写同一 `.tmp` 还会直接抛 FileNotFoundError)。
    """
    errors = _race_two_writers(monkeypatch, [
        lambda: secrets_store.save_deployment({"tickflow_api_key": "tf-1"}),
        lambda: secrets_store.save_deployment({"tickflow_base_url": "https://paid.example.com"}),
    ])

    assert not errors, errors
    written = secrets_store.load_deployment()
    assert written.get("tickflow_api_key") == "tf-1"
    assert written.get("tickflow_base_url") == "https://paid.example.com"


def _race_clear_against_save(monkeypatch, clear_call, save_call) -> None:
    """让 `clear` 读到旧快照后卡在写阶段, 同时并发跑一次 `save`。

    无锁实现: save 先落盘, 随后 clear 用**它早先读到的**旧快照覆盖 ⇒ 刚存的键消失。
    有锁实现: save 阻塞在同一把锁上, 只能排在 clear 之后, 结果确定。
    """
    entered = threading.Event()
    release = threading.Event()
    real_write = secrets_store.atomic_write_text
    gate = {"armed": True}
    errors: list[BaseException] = []

    def _run(fn) -> None:
        try:
            fn()
        except BaseException as e:  # 记下来由断言抛出, 避免线程里静默吞掉
            errors.append(e)

    clearer = threading.Thread(target=_run, args=(clear_call,))
    saver = threading.Thread(target=_run, args=(save_call,))

    def _gated_write(path, text, *, mode=None):
        if gate["armed"] and threading.current_thread() is clearer:
            gate["armed"] = False
            entered.set()
            release.wait(timeout=5)
        real_write(path, text, mode=mode)

    monkeypatch.setattr(secrets_store, "atomic_write_text", _gated_write)

    clearer.start()
    assert entered.wait(timeout=5), "clear 未进入写阶段"
    saver.start()
    saver.join(timeout=0.4)  # 有锁时 saver 必须阻塞在锁上, 不会在此结束
    release.set()
    clearer.join(timeout=5)
    saver.join(timeout=5)

    assert not errors, errors


def test_clear_and_save_are_serialized(tmp_path, monkeypatch):
    """`clear()` 与 `save()` 必须共用一把锁, 否则刚存的键会被陈旧的清空结果抹掉。

    变异反证: 只锁 `save`/`save_deployment`、不给 `clear()` 加锁 → 本用例红。
    """
    a = _root(tmp_path, 1)
    secrets_store.save({"ai_api_key": "old"}, user_root=a)

    _race_clear_against_save(
        monkeypatch,
        lambda: secrets_store.clear("ai_api_key", user_root=a),
        lambda: secrets_store.save({"ai_model": "gpt-of-B"}, user_root=a),
    )

    written = json.loads((a / "user_data" / "secrets.json").read_text(encoding="utf-8"))
    assert written.get("ai_model") == "gpt-of-B", "刚保存的键被并发的 clear 抹掉了"
    assert "ai_api_key" not in written


def test_clear_deployment_and_save_deployment_are_serialized(tmp_path, monkeypatch):
    """部署级同样: `clear_deployment()` 与 `save_deployment()` 共用一把锁。

    变异反证: 不给 `clear_deployment()` 加锁 → 本用例红。
    """
    secrets_store.save_deployment({"tickflow_api_key": "old"})

    _race_clear_against_save(
        monkeypatch,
        lambda: secrets_store.clear_deployment("tickflow_api_key"),
        lambda: secrets_store.save_deployment({"tickflow_base_url": "https://paid.example.com"}),
    )

    written = secrets_store.load_deployment()
    assert written.get("tickflow_base_url") == "https://paid.example.com", "刚存的键被并发的 clear 抹掉了"
    assert "tickflow_api_key" not in written


# ================================================================
# B2 热路径不再每次 mkdir
# ================================================================

def test_hot_read_path_does_not_remkdir(tmp_path, monkeypatch):
    """目录建好之后, 后续每次读不再触发 mkdir 系统调用。

    背景: 行情轮询每轮会读多次凭据, 每次都 `mkdir(parents=True, exist_ok=True)`
    是纯浪费的系统调用。改为进程内「已确保」集合后, 只有**首次**真的建目录。

    变异反证: 把 `_ensure_dir` 退回无条件 `mkdir` → 本用例红。
    """
    a = _root(tmp_path, 1)
    secrets_store.save({"ai_api_key": USER_A_KEY}, user_root=a)  # 首次: 建目录
    secrets_store.load_deployment()  # 预热部署级路径(它也要 mkdir 一次)

    calls: list[str] = []
    real_mkdir = Path.mkdir

    def _counting_mkdir(self, *args, **kwargs):
        calls.append(str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _counting_mkdir)
    for _ in range(10):
        assert secrets_store.get_ai_key(a) == USER_A_KEY
        secrets_store.load_deployment()

    assert calls == [], f"热路径仍在 mkdir: {calls}"
    # 写路径的功能性前提仍在: 文件确实能再写一次(目录已存在, 无需重建)
    secrets_store.save({"ai_model": "gpt-of-B"}, user_root=a)
    assert secrets_store.load(a)["ai_model"] == "gpt-of-B"
