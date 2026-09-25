"""API 层身份依赖 — 从认证中间件注入的请求身份取**面板账号 id**。

认证中间件 (app/main.py) 在每个 ``/api/`` 请求上写 ``request.state.account_id``,
它是账户维度的唯一来源。凡是把账户当参数用的地方都必须走这里, 绝不允许:

  - 从查询串/请求体/请求头取 account_id —— 客户端可任意伪造 ⇒ 直接串号;
  - 拿不到账户时"回退到某个默认账户" —— 那等于把某个账户的数据发给所有人。

拿不到账户 (游客, 以及没有账号 id 的单密码应急入口) 一律 403 fail-closed: 这些入口
本来就读写不了任何每账户数据 (per-user 偏好/规则/告警的解析会抛
MissingUserContextError 或写进共享文件), 提前拒绝比让它们在深处以别的形式炸掉
更清楚, 也避免有人为了"兼容"给它补一个假账户。
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from app.services.user_paths import validate_account_id


def require_account_id(request: Request) -> int:
    """返回当前请求的面板账号 id (正整数); 无账户身份时抛 403。"""
    # 真实 FastAPI Request 必有 .state; 测试替身 (SimpleNamespace) 可能没有 ——
    # 缺 .state 等价于"没有账户身份", 走同一条 403 分支, 而不是抛 AttributeError
    state = getattr(request, "state", None)
    raw = getattr(state, "account_id", None)
    if raw is None:
        raise HTTPException(
            status_code=403,
            detail="当前会话没有账号身份 (游客或单密码应急入口), 无法访问每账户数据",
        )
    try:
        return validate_account_id(raw)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=f"账号身份非法: {e}") from e
