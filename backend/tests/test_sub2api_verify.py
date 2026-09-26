"""sub2api_verify 单元测试 — 全部用 httpx.MockTransport, 绝不外呼网络。

覆盖点(与任务约定一一对应):
  - 200 → True; 401/403/500 → False
  - 超时 / 连接失败(DNS 失败同族) / 非预期异常 → False (fail-closed)
  - 空 key、纯空白 key → False, 且**明确不发出任何请求**
  - 正常路径带 `Authorization: Bearer <key>`, 且只发一个 GET /v1/usage (只读)
  - 明文 key 绝不出现在日志里 (caplog)
"""
from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest

from app.services import sub2api_verify

# 测试基地址: 用 .test 保留域, 即便传输层注入失效也不可能打到真实 Sub2API。
_TEST_BASE_URL = "https://sub2api.test"


@pytest.fixture(autouse=True)
def _force_test_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """把基地址指到测试域, 保证任何用例都不会命中生产地址。"""
    monkeypatch.setenv("SUB2API_BASE_URL", _TEST_BASE_URL)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """注入 MockTransport 并记录每次请求, 返回记录列表。"""
    calls: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(sub2api_verify, "_TRANSPORT", httpx.MockTransport(_record))
    return calls


def _status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    """返回固定状态码的 handler。"""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(code)

    return _handler


def _raiser(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    """返回直接抛异常的 handler, 模拟网络层失败。"""

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return _handler


# --- 状态码分支 ---------------------------------------------------------------


def test_http_200_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, _status(200))
    assert sub2api_verify.verify_api_key("sk-abc123") is True
    assert len(calls) == 1


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failures_are_invalid(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    calls = _install(monkeypatch, _status(code))
    assert sub2api_verify.verify_api_key("sk-abc123") is False
    assert len(calls) == 1  # 仍然发了请求, 只是判定为无效


@pytest.mark.parametrize("code", [400, 404, 429, 500, 502, 503])
def test_other_status_codes_are_invalid(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    _install(monkeypatch, _status(code))
    assert sub2api_verify.verify_api_key("sk-abc123") is False


def test_redirect_is_not_treated_as_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """3xx 不是 200 → False (跟随跳转默认关闭, fail-closed)。"""
    _install(monkeypatch, _status(302))
    assert sub2api_verify.verify_api_key("sk-abc123") is False


# --- 网络层失败分支 -----------------------------------------------------------


def test_timeout_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _raiser(httpx.TimeoutException("read timeout")))
    assert sub2api_verify.verify_api_key("sk-abc123") is False


def test_connect_error_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接失败 / DNS 解析失败在 httpx 里同为 ConnectError。"""
    _install(monkeypatch, _raiser(httpx.ConnectError("getaddrinfo failed")))
    assert sub2api_verify.verify_api_key("sk-abc123") is False


def test_unexpected_exception_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何未预期异常都必须被吞掉并判为无效, 不能把异常抛给调用方。"""
    _install(monkeypatch, _raiser(ValueError("响应畸形")))
    assert sub2api_verify.verify_api_key("sk-abc123") is False


# --- 空 key: 不发出请求 -------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
def test_blank_key_is_invalid_without_any_request(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    calls = _install(monkeypatch, _status(200))
    assert sub2api_verify.verify_api_key(value) is False
    assert calls == []  # 关键: 空/空白 key 连请求都不发


# --- 请求形态 -----------------------------------------------------------------


def test_sends_bearer_header_and_single_readonly_get(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, _status(200))
    key = "sk-abc123"
    assert sub2api_verify.verify_api_key(key) is True

    assert len(calls) == 1
    request = calls[0]
    assert request.method == "GET"  # 只读: 绝不发写请求
    assert str(request.url) == f"{_TEST_BASE_URL}/v1/usage"
    assert request.headers["Authorization"] == f"Bearer {key}"


def test_surrounding_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, _status(200))
    assert sub2api_verify.verify_api_key("  sk-abc123\n") is True
    assert calls[0].headers["Authorization"] == "Bearer sk-abc123"


def test_base_url_trailing_slash_does_not_double_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUB2API_BASE_URL", f"{_TEST_BASE_URL}/")
    calls = _install(monkeypatch, _status(200))
    assert sub2api_verify.verify_api_key("sk-abc123") is True
    assert str(calls[0].url) == f"{_TEST_BASE_URL}/v1/usage"


def test_default_base_url_is_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设环境变量时回落到生产地址 (此处仅比对 URL, 传输层已被 mock)。

    本站对应的 Sub2API 实例 = xiaoni-model.top(2026-09-26 用户确认), 与 AI 上游
    `app.config.AI_GATEWAY_BASE_URL` 同一实例 —— 校验与出网打到两处会拿用户的 key
    去别处换 401。
    """
    assert sub2api_verify.SUB2API_BASE_URL == "https://xiaoni-model.top"
    monkeypatch.delenv("SUB2API_BASE_URL", raising=False)
    calls = _install(monkeypatch, _status(200))
    assert sub2api_verify.verify_api_key("sk-abc123") is True
    assert str(calls[0].url) == "https://xiaoni-model.top/v1/usage"


# --- 日志不泄露明文 key -------------------------------------------------------


def test_plaintext_key_never_appears_in_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """成功、被拒、上游异常三条路径都不得把明文 key 写进日志。"""
    caplog.set_level(logging.DEBUG)
    key = "sk-live-do-not-log-9f3c2a"

    for handler in (
        _status(200),
        _status(401),
        _status(500),
        _raiser(httpx.TimeoutException("read timeout")),
        _raiser(httpx.ConnectError("getaddrinfo failed")),
    ):
        _install(monkeypatch, handler)
        sub2api_verify.verify_api_key(key)

    assert key not in caplog.text
    assert "9f3c2a" not in caplog.text  # 连尾部片段也不出现
    # 允许出现脱敏形态, 便于人工排查是哪把 key 的问题
    assert "****" in caplog.text


def test_redaction_keeps_only_short_prefix() -> None:
    """脱敏函数只保留前 3 字符, 剩余不可还原。"""
    redacted = sub2api_verify._redact("sk-live-9f3c2a")
    assert redacted == "sk-****"
    assert "live" not in redacted


def test_module_contract_constants() -> None:
    """模块级契约: 超时 10s, 且生产模式下传输层注入点为 None (真实网络)。"""
    assert sub2api_verify._TIMEOUT_S == 10.0
    assert sub2api_verify.SUB2API_BASE_URL == "https://xiaoni-model.top"
    assert sub2api_verify._TRANSPORT is None
