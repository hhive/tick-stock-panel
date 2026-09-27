"""助手「回测工具桥」必须携带发起账户的根, 且失败原因不得外发内部痕迹。

两件事各修一条:
  1. `run_backtest` 原先把 `ctx.data_dir`(共享行情根)当账户根传给 worker ——
     worker 子进程没有请求上下文, 引擎的 override_loader 于是 fail-closed 抛
     `InvalidAccountIdError`, 而被 `except Exception: pass` 吞掉 ⇒ 复合/叠加策略的
     子策略覆盖值**静默变空**, 与选股页的口径对不上且无任何提示。
  2. `raise ValueError(f"回测失败: {error}")` 把 manifest 里的原文直接回填给 LLM,
     `/opt/.../data`、`InvalidAccountIdError` 这类内部痕迹会出现在助手回答里。

写法: 断言"载荷里的 user_root 等于该账户的根"与"对客文案里不含内部痕迹"这两个
**负面不变量**, 而不是断言"调用过某函数"。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config as app_config
from app.services import preferences, user_paths
from app.services import tool_catalog


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    token = preferences.set_current_user_root(user_paths.user_root(1))
    yield SimpleNamespace(data_dir=Path(tmp_path))
    preferences.reset_current_user_root(token)


def _stub_worker(monkeypatch, result: dict) -> list[dict]:
    """打桩 run_worker_task(真实 make_worker_task 仍参与), 记录下发的载荷。"""
    from app.backtest import worker as worker_mod

    seen: list[dict] = []

    def fake_run(task, *args, **kwargs):
        seen.append(task)
        return result

    monkeypatch.setattr(worker_mod, "run_worker_task", fake_run)
    return seen


def test_run_backtest_worker_task_carries_account_root(_isolated, monkeypatch) -> None:
    seen = _stub_worker(monkeypatch, {"stats": {}})

    tool_catalog.run_backtest(_isolated.data_dir, strategy_id="ma_cross")

    assert len(seen) == 1
    task = seen[0]
    account_root = user_paths.user_root(1)
    assert Path(task["user_root"]) == account_root, (
        f"载荷 user_root 应为账户根 {account_root}, 实际 {task.get('user_root')!r}"
    )
    # 负面不变量: 绝不能是共享行情根 —— 那正是缺陷本体。
    assert Path(task["user_root"]) != _isolated.data_dir


def test_run_backtest_error_text_hides_internal_traces(_isolated, monkeypatch) -> None:
    _stub_worker(
        monkeypatch,
        {"error": "data_dir 之下只有 users/<id> 可作为账户根(其余是共享位置): /opt/app/data"},
    )

    with pytest.raises(ValueError) as excinfo:
        tool_catalog.run_backtest(_isolated.data_dir, strategy_id="ma_cross")

    message = str(excinfo.value)
    for leaked in ("/opt/", "data_dir", "users/", "InvalidAccountIdError", "Traceback"):
        assert leaked not in message, f"对客文案泄漏了内部痕迹 {leaked!r}: {message}"


def test_run_backtest_keeps_actionable_non_internal_error(_isolated, monkeypatch) -> None:
    """不含有内部痕迹的原因照原样保留 —— 否则会把可操作信息一起抹掉。"""
    _stub_worker(monkeypatch, {"error": "策略参数不合法: 缺少 fast 参数"})

    with pytest.raises(ValueError) as excinfo:
        tool_catalog.run_backtest(_isolated.data_dir, strategy_id="ma_cross")

    assert "策略参数不合法" in str(excinfo.value)
