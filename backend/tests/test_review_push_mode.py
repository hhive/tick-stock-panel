"""复盘推送触发方式 review_push_mode 测试 — auto/manual 白名单与默认值 + 推送门控。"""
from __future__ import annotations

import asyncio

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_global_path", lambda: path)
    # review_push_mode / review_push_channels 是**每用户键**: save() 按归属分派到
    # <user_root>/user_data/preferences.json, 且无账户上下文时会 fail-closed 抛
    # RuntimeError。
    #
    # 这里建立上下文, 是为了**测 getter 本身的逻辑**(默认值/白名单/旧格式兼容)。
    #
    # 生产侧的定时复盘已经**逐账户扇出**(见下方 _patch_scheduled_review 的说明):
    # jobs/daily_pipeline.py 的 _run_scheduled_review 遍历账户并显式把 user_root
    # 传下去, 因此后台线程不需要 contextvar 也能读到该账户的推送配置。
    # 账户根 = <data_dir>/users/1 (与生产布局一致): 后台扇出按 account_id 解析出
    # 同一个根, 只有测试的 contextvar 指向它, "请求侧写入的偏好"与"后台扇出读到的
    # 偏好"才是同一份 —— 否则测的就成了另一条路径。
    user_root = tmp_path / "users" / "1"
    token = preferences.set_current_user_root(user_root)
    preferences._invalidate_cache()
    try:
        yield path
    finally:
        preferences.reset_current_user_root(token)
        preferences._invalidate_cache()


def test_review_push_mode_defaults_to_manual():
    assert preferences.get_review_push_mode() == "manual"


def test_set_and_get_review_push_mode():
    assert preferences.set_review_push_mode("auto") == "auto"
    assert preferences.get_review_push_mode() == "auto"

    assert preferences.set_review_push_mode("manual") == "manual"
    assert preferences.get_review_push_mode() == "manual"


def test_set_review_push_mode_rejects_invalid_value():
    assert preferences.set_review_push_mode("bogus") == "manual"
    assert preferences.get_review_push_mode() == "manual"


# ── 推送门控 ────────────────────────────────────────────────────────
# 门控语义:
#   manual: 定时复盘只归档不推送; 手动保存需显式 push=True 才推
#   auto:   归档即推(与旧逻辑一致)

def test_save_report_manual_requires_explicit_push(monkeypatch):
    from app.api import market_recap
    from app.jobs import daily_pipeline

    pushed: list[dict] = []
    monkeypatch.setattr(
        "app.services.market_recap_reports.save_report",
        lambda d: {"id": "r1"},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "_maybe_push_review",
        lambda content, meta: pushed.append(meta),
    )
    preferences.set_review_push_mode("manual")

    # 默认 push=False: manual 模式下只归档, 不外发
    market_recap.save_report(None, market_recap.SaveReportRequest(as_of="2026-07-18", content="正文"))
    assert pushed == []

    # 显式 push=True: manual 模式下外发
    market_recap.save_report(None, market_recap.SaveReportRequest(as_of="2026-07-18", content="正文", push=True))
    assert pushed == [{"as_of": "2026-07-18", "emotion_label": ""}]


def test_save_report_auto_pushes_without_flag(monkeypatch):
    from app.api import market_recap
    from app.jobs import daily_pipeline

    pushed: list[dict] = []
    monkeypatch.setattr(
        "app.services.market_recap_reports.save_report",
        lambda d: {"id": "r1"},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "_maybe_push_review",
        lambda content, meta: pushed.append(meta),
    )
    preferences.set_review_push_mode("auto")

    # auto 模式: 无需 push 标志即外发
    market_recap.save_report(None, market_recap.SaveReportRequest(as_of="2026-07-18", content="正文"))
    assert pushed == [{"as_of": "2026-07-18", "emotion_label": ""}]


def _patch_scheduled_review(monkeypatch, pushed: list, archived: list, account_root):
    """装配定时复盘的依赖: 有 AI key、流式产出固定内容、捕获归档与推送调用。

    归档/推送都是**每账户**动作 (报告存在 <user_root>/user_data 下, 推送渠道也是每账户
    偏好), 所以后台 job 必须逐账户扇出 —— 这里把账户列表钉成"一个账户", 并断言
    扇出时把该账户的根显式传了下去 (不传会退回部署级默认值 ⇒ 推送静默失效)。
    """
    from app.jobs import daily_pipeline

    async def _fake_stream(*a, **k):
        return "正文", {"as_of": "2026-07-18", "emotion_label": "中性"}

    monkeypatch.setattr("app.secrets_store.get_ai_key", lambda: "sk-test")
    monkeypatch.setattr(daily_pipeline, "_stream_review_with_retry", _fake_stream)
    monkeypatch.setattr(
        "app.jobs.daily_pipeline.user_paths.iter_user_roots",
        lambda: [(1, account_root)],
    )
    monkeypatch.setattr(
        "app.services.market_recap_reports.save_report",
        lambda d, user_root=None: archived.append((d, user_root)) or {"id": "r1"},
    )
    monkeypatch.setattr(
        daily_pipeline,
        "_maybe_push_review",
        lambda content, meta, user_root=None: pushed.append((meta, user_root)),
    )


def test_scheduled_review_manual_archives_without_push(monkeypatch, tmp_path):
    from app.jobs import daily_pipeline

    pushed: list = []
    archived: list = []
    account_root = tmp_path / "users" / "1"
    _patch_scheduled_review(monkeypatch, pushed, archived, account_root)
    preferences.set_review_push_mode("manual")

    asyncio.run(daily_pipeline._run_scheduled_review(None))

    # manual 模式: 归档发生 (且落进该账户的根), 但不外发
    assert [root for _, root in archived] == [account_root]
    assert pushed == []


def test_scheduled_review_auto_pushes(monkeypatch, tmp_path):
    from app.jobs import daily_pipeline

    pushed: list = []
    archived: list = []
    account_root = tmp_path / "users" / "1"
    _patch_scheduled_review(monkeypatch, pushed, archived, account_root)
    preferences.set_review_push_mode("auto")

    asyncio.run(daily_pipeline._run_scheduled_review(None))

    # auto 模式: 归档并外发 (推送同样显式带该账户的根, 否则读不到该账户的渠道配置)
    assert [root for _, root in archived] == [account_root]
    assert pushed == [({"as_of": "2026-07-18", "emotion_label": "中性"}, account_root)]
