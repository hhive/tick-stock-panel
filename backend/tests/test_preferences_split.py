"""偏好拆分的归属分派测试（全局文件 vs 每用户文件）。

本套件是 S2 的关键护栏：偏好是最容易"悄悄落错文件"的一处 —— 落错不会报错，
只会让某个设置对其它账户静默消失（或反过来，让私有设置被全站共享）。
"""
from __future__ import annotations

import logging

import pytest

from app import config as app_config
from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    preferences._invalidate_cache()
    # 「同键只告警一次」是模块级状态, 不清会跨用例串味, 让告警断言假失败
    preferences._warned_missing_context.clear()
    token = preferences.set_current_user_root(None)
    yield tmp_path
    preferences.reset_current_user_root(token)
    preferences._invalidate_cache()
    preferences._warned_missing_context.clear()


@pytest.fixture
def user_root(tmp_path):
    """一个已存在的账户根目录。"""
    root = tmp_path / "users" / "1"
    (root / "user_data").mkdir(parents=True, exist_ok=True)
    return root


# ================================================================
# 归属表本身
# ================================================================

def test_per_user_key_set_is_frozen():
    """归属表是数据契约：任何增删都必须显式改这个测试。

    放宽或收紧归属会改变数据的可见范围，不能让它在重构中被顺手改掉。
    """
    assert preferences.PER_USER_KEYS == frozenset({
        "minute_intraday_refresh", "minute_intraday_refresh_interval",
        "review_push_channels", "review_push_mode",
        "review_push_channel", "review_push_enabled",
        "feishu_webhook_url", "feishu_webhook_secret",
        "wecom_webhook_url", "custom_webhook_url", "email_smtp_config",
        "system_notify_enabled",
        "webhook_default_channels", "webhook_enabled_default",
        "sse_refresh_pages", "strategy_monitor_enabled", "strategy_monitor_ids",
        "screener_auto_run", "monitor_ext_fields",
        "nav_order", "nav_hidden", "watchlist_columns",
        "screener_result_columns", "watchlist_groups_in_nav",
        "onboarding_completed",
    })
    assert len(preferences.PER_USER_KEYS) == 25


def test_singleton_thread_keys_are_not_per_user():
    """控制进程级单例线程的键必须在全局 —— 单例线程注定只能有一份。

    这组键最容易被"顺手"搬进每用户文件（名字里带 realtime/分钟/监控，看着像偏好），
    所以单独钉一条测试。
    """
    for key in ("realtime_quotes_enabled", "realtime_quote_interval",
                "realtime_pull_stock", "realtime_pull_etf",
                "limit_ladder_monitor_enabled", "depth_polling_interval",
                "minute_refresh_enabled", "minute_refresh_interval",
                "wecom_bot_id", "wecom_bot_secret", "wecom_bot_enabled"):
        assert key not in preferences.PER_USER_KEYS, f"{key} 不应是每用户键"
        assert key in preferences.GLOBAL_BY_DESIGN


def test_owner_of_unknown_key_defaults_to_global():
    """未知键默认 GLOBAL —— 不对称风险：落全局最坏是没人读，落每用户会静默消失。"""
    assert preferences._owner_of("some_future_key") == "global"
    assert preferences._owner_of("theme") == "global"
    assert preferences._owner_of("nav_order") == "per_user"


# ================================================================
# 写入分派
# ================================================================

def test_global_key_lands_in_global_file(_isolated):
    preferences.save({"enriched_batch_size": 1234})
    assert (_isolated / "user_data" / "preferences.json").is_file()
    assert not (_isolated / "users").exists()


def test_per_user_key_lands_in_user_file(user_root):
    preferences.save({"nav_order": ["a", "b"]}, user_root=user_root)
    assert (user_root / "user_data" / "preferences.json").is_file()
    assert preferences.load(user_root)["nav_order"] == ["a", "b"]


def test_per_user_key_without_context_raises(_isolated):
    """有每用户键却没有账户上下文 = 调用方 bug，必须炸出来而不是静默写全局。

    静默写全局会让一个本该私有的设置被所有账户共享，且不报错、不提醒。
    """
    with pytest.raises(RuntimeError, match="缺少账户上下文"):
        preferences.save({"nav_order": ["a"]})


def test_unknown_key_is_written_to_global_file(user_root, _isolated):
    preferences.save({"brand_new_key": 1}, user_root=user_root)
    assert (_isolated / "user_data" / "preferences.json").is_file()
    # 未知键不得落进每用户文件
    assert not (user_root / "user_data" / "preferences.json").exists()


def test_mixed_write_splits_and_warns(user_root, caplog):
    """跨文件写入：按分区正确落盘，但要留下警告（跨文件原子性已丢失）。"""
    with caplog.at_level(logging.WARNING):
        preferences.save(
            {"nav_order": ["x"], "enriched_batch_size": 999},
            user_root=user_root,
        )
    assert preferences.load(user_root)["nav_order"] == ["x"]
    assert preferences.load(user_root)["enriched_batch_size"] == 999
    # 用 caplog.text 而非逐条 r.message % r.args: 后者会在没有占位符的记录上炸
    assert "spans both files" in caplog.text


# ================================================================
# 读取合并
# ================================================================

def test_load_merges_both_files(user_root):
    preferences.save({"enriched_batch_size": 7}, user_root=user_root)
    preferences.save({"nav_order": ["n"]}, user_root=user_root)
    merged = preferences.load(user_root)
    assert merged["enriched_batch_size"] == 7
    assert merged["nav_order"] == ["n"]


def test_load_without_context_excludes_per_user_keys(user_root):
    """无账户上下文只返回全局部分 —— 未登录/后台共享路径不得读到某账户私有偏好。"""
    preferences.save({"nav_order": ["private"]}, user_root=user_root)
    assert "nav_order" not in preferences.load()


def test_accounts_do_not_see_each_other(tmp_path):
    """核心隔离：A 的每用户偏好对 B 不可见。"""
    root_a = tmp_path / "users" / "1"
    root_b = tmp_path / "users" / "2"
    preferences.save({"nav_order": ["A"]}, user_root=root_a)
    preferences.save({"nav_order": ["B"]}, user_root=root_b)

    assert preferences.load(root_a)["nav_order"] == ["A"]
    assert preferences.load(root_b)["nav_order"] == ["B"]


def test_new_account_gets_preference_defaults(tmp_path):
    """新账户 = 全默认值（不迁移、不预置）。onboarding_completed 必须为 False，
    否则新用户看不到首次引导。"""
    fresh = tmp_path / "users" / "99"
    assert preferences.load(fresh) == {}
    assert preferences.load(fresh).get("onboarding_completed", False) is False


# ================================================================
# 上下文机制
# ================================================================

def test_contextvar_drives_implicit_user_root(user_root):
    token = preferences.set_current_user_root(user_root)
    try:
        preferences.save({"nav_hidden": ["x"]})
        assert preferences.load()["nav_hidden"] == ["x"]
    finally:
        preferences.reset_current_user_root(token)
    # 复位后不再可见
    assert "nav_hidden" not in preferences.load()


def test_explicit_root_overrides_context(user_root, tmp_path):
    """后台线程用显式参数，必须压过 contextvar。"""
    other = tmp_path / "users" / "2"
    token = preferences.set_current_user_root(other)
    try:
        preferences.save({"nav_order": ["explicit"]}, user_root=user_root)
    finally:
        preferences.reset_current_user_root(token)
    assert preferences.load(user_root)["nav_order"] == ["explicit"]
    assert "nav_order" not in preferences.load(other)


# ================================================================
# 缓存
# ================================================================

def test_cache_invalidated_after_write(user_root):
    preferences.save({"nav_order": ["first"]}, user_root=user_root)
    assert preferences.load(user_root)["nav_order"] == ["first"]
    preferences.save({"nav_order": ["second"]}, user_root=user_root)
    assert preferences.load(user_root)["nav_order"] == ["second"]


def test_scheduled_review_push_is_broken_until_fanout_lands(caplog):
    """已知缺陷钉子: 定时复盘推送在 S3 扇出落地前**读不到**每用户配置。

    这条测试断言的是**缺陷本身**, 不是期望行为 —— 属于特征化测试(characterization
    test)。它存在的意义有两条:

      ① 把"这个功能现在是坏的"变成可追踪的事实, 而不是没人知道的空洞;
        实现中有个陷阱: 只要给测试设上账户上下文, getter 就会返回配置,
         于是测试全绿、而生产后台线程依然拿不到值(它没有 contextvar)。
         那种"为生产不存在的条件开绿灯"的假绿, 比测试失败更危险。
      ② S3 修好后本测试会**失败**, 强制有人来更新它并确认功能真的恢复。

    刻意不 xfail / skip —— 那会让它悄悄从视野里消失。
    """
    preferences._warned_missing_context.clear()
    with caplog.at_level(logging.WARNING):
        mode = preferences.get_review_push_mode()
        channels = preferences.get_review_push_channels()
    # 后台(APScheduler 线程)没有账户上下文 ⇒ 退化为默认值, 推送不会发生
    assert mode == "manual"
    assert channels == []
    assert "without account context" in caplog.text


def test_malformed_file_degrades_to_empty(user_root):
    p = user_root / "user_data" / "preferences.json"
    p.write_text("{ not json", encoding="utf-8")
    preferences._invalidate_cache()
    assert preferences.load(user_root) == {}


# ================================================================
# 后台读取：无账户上下文必须**大声**失败，不能静默
# ================================================================

def test_per_user_get_warns_without_context(caplog):
    """后台单例(无账户上下文)读每用户键 → 必须留下警告。

    静默返回默认值意味着: 推送地址变成空串 ⇒ 告警与复盘推送悄悄停止工作,
    不报错、不记日志。这是本项目最危险的一类失效, 比崩溃难查得多。
    """
    with caplog.at_level(logging.WARNING):
        assert preferences.per_user_get("feishu_webhook_url", "") == ""
    assert "without account context" in caplog.text


def test_per_user_get_does_not_warn_with_context(user_root, caplog):
    """有账户上下文(请求路径)时不得告警 —— 否则告警本身就是噪声。"""
    token = preferences.set_current_user_root(user_root)
    try:
        with caplog.at_level(logging.WARNING):
            preferences.per_user_get("feishu_webhook_url", "")
    finally:
        preferences.reset_current_user_root(token)
    assert "without account context" not in caplog.text


def test_per_user_get_warns_only_once_per_key(caplog):
    """同一键只告警一次: 后台是高频路径, 每轮刷屏会淹没真正的错误。"""
    preferences._warned_missing_context.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            preferences.per_user_get("feishu_webhook_url", "")
    assert caplog.text.count("feishu_webhook_url") == 1


def test_global_getter_does_not_warn(caplog):
    """回归: 全局 getter 不得为每用户键误报警告。

    实现时曾在插入告警的过程中把 get_pipeline_schedule 当成了目标, 它会为
    review_push_channels 报假警告。假警告会训练人们忽略所有警告, 所以单独钉住。
    """
    with caplog.at_level(logging.WARNING):
        preferences.get_pipeline_schedule()
    assert "without account context" not in caplog.text


def test_background_push_getters_are_all_protected(caplog):
    """后台真正会调的那批每用户 getter 都必须有保护 —— 警告而非静默。

    这批就是 quote_service 与 daily_pipeline 的调用点。少一个, 该推送渠道就会
    在无人察觉的情况下失效。
    """
    for fn in (preferences.get_review_push_channels,
               preferences.get_review_push_mode,
               preferences.get_feishu_webhook_url,
               preferences.get_feishu_webhook_secret,
               preferences.get_wecom_webhook_url,
               preferences.get_custom_webhook_url,
               preferences.get_email_smtp_config,
               preferences.get_system_notify_enabled,
               preferences.get_monitor_ext_fields):
        caplog.clear()
        preferences._warned_missing_context.clear()
        with caplog.at_level(logging.WARNING):
            fn()
        assert "without account context" in caplog.text, f"{fn.__name__} 缺保护"
