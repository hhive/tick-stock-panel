"""AI 财务分析报告持久化存储。

存储位置: <user_root>/user_data/ai_reports.json (数组,按 created_at 降序)
保留最近 MAX_REPORTS 条;超出自动裁剪最旧的。

存储机制统一委托给 JsonReportStore(原子写 + 实例锁), 本模块只固化
财务分析报告 的文件名 / 上限 / id 前缀, 对外保持原有函数签名不变。

每条报告结构:
{
  "id": "rpt_xxx",           # 唯一 id
  "symbol": "600519.SH",
  "name": "贵州茅台",
  "focus": "",               # 用户追加的关心点(可为空)
  "content": "# ...markdown", # 报告正文
  "periods": 4,              # 基于几期数据生成
  "summary": "metrics: 1期...",  # 数据摘要
  "created_at": "2026-06-25T10:00:00"
}
"""
from __future__ import annotations

from pathlib import Path

from app.services.json_report_store import JsonReportStore

MAX_REPORTS = 20

_store = JsonReportStore("ai_reports.json", MAX_REPORTS, id_prefix="rpt")


def list_reports(user_root: Path | None = None) -> list[dict]:
    """返回**当前账户**的全部报告(按 created_at 降序)。"""
    return _store.list_reports(user_root)


def save_report(report: dict, user_root: Path | None = None) -> dict:
    """新增一条报告并持久化到当前账户。返回保存后的报告(含 id / created_at)。

    自动补全 id 与 created_at(若缺),并裁剪到上限。
    """
    return _store.save_report(report, user_root)


def delete_report(report_id: str, user_root: Path | None = None) -> bool:
    """删除当前账户的指定报告。返回是否删除成功。"""
    return _store.delete_report(report_id, user_root)


def clear_reports(user_root: Path | None = None) -> int:
    """清空当前账户的全部报告。返回删除数量。"""
    return _store.clear_reports(user_root)
