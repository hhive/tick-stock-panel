"""AI 报告 created_at 时区测试 — 必须是北京墙钟, 不随服务器时区漂移。

三类 AI 报告(财务分析 / 个股分析 / 大盘复盘)共用 JsonReportStore 补 created_at,
前端 fmtRelative 用 `new Date(created_at)` 按浏览器本地时区解析这串 naive 时间。
服务端若用宿主机时钟(Docker 镜像默认 UTC), 刚生成的报告会被显示成「8 小时前」,
「今天是否已生成过报告」的判定(created_at 前 10 位 == 浏览器今天)也会误判。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.json_report_store import JsonReportStore

CN_TZ = timezone(timedelta(hours=8))


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app import config as app_config
    from app.services import preferences

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    # 每用户存储没有共享回退, 需要账户上下文 (真实请求由认证中间件注入)
    token = preferences.set_current_user_root(tmp_path)
    try:
        yield JsonReportStore("ai_reports.json", 20, id_prefix="rpt")
    finally:
        preferences.reset_current_user_root(token)


def test_created_at_follows_beijing_clock(store):
    """created_at 与北京墙钟一致(宿主机时区非 UTC+8 时旧实现偏移整时区差)。"""
    saved = store.save_report({"symbol": "600519.SH", "content": "正文"})

    got = datetime.fromisoformat(saved["created_at"])
    expected = datetime.now(CN_TZ).replace(tzinfo=None)
    assert abs((got - expected).total_seconds()) < 5


def test_created_at_keeps_naive_second_precision_format(store):
    """格式不变: 秒精度、无时区后缀, 历史记录与前端解析保持兼容。"""
    saved = store.save_report({"symbol": "600519.SH", "content": "正文"})

    created_at = saved["created_at"]
    assert len(created_at) == 19
    assert created_at[10] == "T"
    assert datetime.fromisoformat(created_at).tzinfo is None
