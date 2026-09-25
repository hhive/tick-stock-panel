"""Sub2API apikey 校验 — 下游应用确认用户带来的 apikey 真实有效。

场景: 用户从 Sub2API 站点跳转到本应用, apikey 随 URL 带来。URL 是用户可控的,
任何人都能拼一个形如 `sk-xxxx` 的字符串, 所以必须回源 Sub2API 确认该 key 真的
存在且可用, 校验通过后才可信任。

实现: 调用 Sub2API **既有**的只读接口 `GET {base}/v1/usage`, 带
`Authorization: Bearer <key>`; 仅 HTTP 200 视为有效。本模块不改动 Sub2API 侧
任何代码, 也不调用任何写接口。

安全取舍:
  - **fail-closed**: 只有 200 返回 True。401/403、其它 4xx/5xx、超时、连接失败、
    DNS 解析失败、响应畸形、任何异常一律 False。宁可误拒合法用户 (可重试),
    也不放过伪造 key。
  - **绝不落明文 key**: 日志最多出现脱敏形态 (前 3 字符 + `****`), 异常信息里
    同样不含 key。apikey 是账号级凭证, 一旦进日志即等同泄露。
  - **不走环境代理**: 见 _build_client 注释 — 避免把 Bearer 凭证交给环境变量里
    的代理地址。
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Sub2API 基地址默认值(生产)。部署到其它环境时用环境变量 SUB2API_BASE_URL 覆盖。
#
# 为什么读 os.environ 而不是 app.config.settings:
#   1) app/config.py 里没有对应字段, 而本任务明确不改动其它文件;
#   2) Settings 是 pydantic-settings 且 extra="ignore", 未声明的环境变量根本
#      进不到 settings 对象里, 想加就得改 config.py;
#   3) 每次调用惰性读取, 测试用 monkeypatch.setenv 即可覆盖, 不依赖 import 时机。
SUB2API_BASE_URL = "https://xiaoni-apikey.top"

# 校验接口路径(Sub2API 既有只读接口, 不新增/不改动 Sub2API)。
_USAGE_PATH = "/v1/usage"

# 单次校验超时(秒)。校验发生在跳转落地路径上, 不能让用户干等。
_TIMEOUT_S = 10.0

# 脱敏时保留的明文前缀长度: 够人工比对, 不足以还原 key。
_REDACT_KEEP = 3

# 测试注入点: httpx 传输层。生产恒为 None(走真实网络); 单元测试用
# httpx.MockTransport 覆盖, 保证测试绝不外呼。
_TRANSPORT: httpx.BaseTransport | None = None


def _redact(api_key: str) -> str:
    """脱敏 apikey — 只保留前 3 个字符, 其余以 **** 代替。"""
    return api_key[:_REDACT_KEEP] + "****"


def _resolve_base_url() -> str:
    """解析 Sub2API 基地址(环境变量优先, 去掉尾部斜杠避免拼出 //v1/usage)。"""
    base = os.environ.get("SUB2API_BASE_URL") or SUB2API_BASE_URL
    return base.strip().rstrip("/")


def _build_client() -> httpx.Client:
    """构造一次性校验用客户端。

    trust_env=False: 不让 HTTP_PROXY/ALL_PROXY 等环境变量接管本次请求 —— 那是把
    用户的 Bearer 凭证交给环境里配置的任意代理; 目标主机是本项目固定的 Sub2API,
    直连即可, 也更符合本模块 fail-closed 的姿态。
    """
    return httpx.Client(
        timeout=_TIMEOUT_S,
        transport=_TRANSPORT,
        trust_env=False,
    )


def verify_api_key(api_key: str) -> bool:
    """向 Sub2API 校验 apikey 是否有效(只读, fail-closed)。

    返回 True 仅当 `GET {base}/v1/usage` 返回 HTTP 200; 其余一切情况(空 key、
    401/403、其它 4xx/5xx、超时、连接失败、DNS 失败、响应畸形、任何异常)返回 False。

    响应体不解析: 契约是「200 即有效」, 体内容仅为用量数据, 与有效性判定无关;
    不解析也就不会有「畸形 JSON → 抛错」这条额外失败路径。
    """
    # key 是 URL 传来的, 可能带首尾空白/换行(URL 编码或复制粘贴), 先 strip 再用。
    key = (api_key or "").strip()
    if not key:
        # 空 key 不发请求: 结果必然是 401, 白等一个 RTT; 也避免拿空凭证去打上游。
        return False

    url = f"{_resolve_base_url()}{_USAGE_PATH}"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with _build_client() as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            return True
        logger.info("sub2api 校验未通过 (%s): HTTP %s", _redact(key), resp.status_code)
        return False
    except Exception as e:  # noqa: BLE001 — fail-closed: 任何异常都判定为无效
        # 异常信息来自 httpx(URL/状态/网络原因), 不含请求头, 因此不会泄露 key;
        # key 本身在此处也只以脱敏形态出现。
        logger.warning("sub2api 校验异常 (%s): %s", _redact(key), e)
        return False
