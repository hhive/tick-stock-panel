"""domain3 (模拟盘 / 手数批次 / AI 报告 / 密钥库 / 自定义因子) 多租户隔离。

被测的不变量只有一条: **账户 A 的数据, 账户 B 读不到**。证据必须是「用真实的
服务函数读」, 而不是断言文件路径 —— 路径断言在「两个账户写在同一个文件」时
依然会通过。

三层覆盖:
  1. 上下文路径 (真实请求走这条): context=A 写入 → 切到 B 读 → 空;
  2. 显式 user_root (后台线程走这条): 显式参数压过上下文;
  3. fail-closed: 两者都没有 → MissingUserContextError, **绝不**回退到共享文件。
"""
from __future__ import annotations

import contextlib
import stat
from pathlib import Path

import pytest

from app import config as app_config
from app import secrets_store
from app.factors import store as factors_store
from app.services import (
    ai_reports,
    json_report_store,
    market_recap_reports,
    preferences,
    stock_reports,
    user_paths,
)
from app.strategy import lots, paper, paper_auto

# 一个最小可保存的批次 (lots 不做校验的写入路径, 校验在 api 层)
_LOT = {"id": "lot_iso", "symbol": "600519.SH", "cost_price": 1500.0, "qty": 100}
_FACTOR = {"id": "uf_iso", "kind": "custom", "label": "隔离因子", "status": "draft"}


@contextlib.contextmanager
def as_account(account_id: int):
    """模拟认证中间件: 请求期间把 contextvar 指向该账户的根目录。"""
    root = user_paths.user_root(account_id)
    token = preferences.set_current_user_root(root)
    try:
        yield root
    finally:
        preferences.reset_current_user_root(token)


@contextlib.contextmanager
def no_account():
    """模拟无账号上下文 (后台线程/游客)。"""
    token = preferences.set_current_user_root(None)
    try:
        yield
    finally:
        preferences.reset_current_user_root(token)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    with no_account():
        yield tmp_path


# (存储名, 写入, 读取, 空值) —— 每个 store 一组, 全部按账户根目录为唯一入参
STORES = [
    (
        "模拟盘(账户)",
        lambda root: paper.create_account(100_000.0, user_root=root),
        lambda root: [a["id"] for a in paper.list_accounts(root)],
        [],
    ),
    (
        "模拟盘(订单)",
        lambda root: (
            paper.create_account(100_000.0, user_root=root),
            paper.create_order(
                "600519.SH", "buy", account_id="default", qty=100, ref_price=10.0, user_root=root,
            ),
        ),
        lambda root: [o["symbol"] for o in paper.load_orders("default", user_root=root)],
        [],
    ),
    (
        "模拟盘(自动跟单)",
        lambda root: paper_auto.create_auto_rule(
            {
                "name": "跟策略", "match_kind": "strategy", "match_id": "s1",
                "side": "buy", "size_mode": "fixed_amount", "size_value": 10000,
            },
            user_root=root,
        ),
        lambda root: [r["name"] for r in paper_auto.load_auto_rules(user_root=root)],
        [],
    ),
    (
        "手数批次",
        lambda root: lots.save_one(dict(_LOT), root),
        lambda root: [lot["id"] for lot in lots.load_all(root)],
        [],
    ),
    (
        "AI 财务报告",
        lambda root: ai_reports.save_report({"symbol": "600519.SH", "content": "A 的报告"}, root),
        lambda root: [r["content"] for r in ai_reports.list_reports(root)],
        [],
    ),
    (
        "AI 个股报告",
        lambda root: stock_reports.save_report({"symbol": "600519.SH", "content": "A 的报告"}, root),
        lambda root: [r["content"] for r in stock_reports.list_reports(root)],
        [],
    ),
    (
        "AI 大盘复盘",
        lambda root: market_recap_reports.save_report({"as_of": "2026-09-25", "content": "A 的复盘"}, root),
        lambda root: [r["content"] for r in market_recap_reports.list_reports(root)],
        [],
    ),
    (
        # 用**每账户**的 AI Key 代表密钥库: TickFlow Key / 数据源插件 Key 是
        # **部署级**凭据(共享行情全站一份, 且要在无账户上下文的后台线程里可读),
        # 天然不按账户隔离, 放进本表会把「共享」误判成「串号」。
        # 部署级/每账户的作用域划分由 tests/test_secrets_scope.py 专门钉住。
        "密钥库",
        lambda root: secrets_store.save({"ai_api_key": "sk-A"}, root),
        lambda root: [secrets_store.load(root).get("ai_api_key")],
        [None],
    ),
    (
        "自定义因子",
        lambda root: factors_store.save_one(dict(_FACTOR), root),
        lambda root: [d["id"] for d in factors_store.load_all(root)],
        [],
    ),
]

_IDS = [case[0] for case in STORES]


# ================================================================
# 跨账户不可见 (上下文路径 = 真实请求路径)
# ================================================================
@pytest.mark.parametrize("label, write, read, empty", STORES, ids=_IDS)
def test_account_b_cannot_see_account_a_data(label, write, read, empty, _isolated):
    with as_account(1) as root_a:
        write(root_a)
        assert read(root_a) != empty, f"{label}: A 自己应当读得到"

    with as_account(2) as root_b:
        assert read(root_b) == empty, f"{label}: B 读到了 A 的数据"

    # A 再读一次仍然在 (写入没有被 B 的空读取影响)
    with as_account(1) as root_a:
        assert read(root_a) != empty, f"{label}: A 的数据被 B 的读取抹掉了"


@pytest.mark.parametrize("label, write, read, empty", STORES, ids=_IDS)
def test_new_account_starts_empty(label, write, read, empty, _isolated):
    """全新账户是默认空态 —— 不继承任何既有数据。"""
    with as_account(1) as root_a:
        write(root_a)

    with as_account(7) as root_new:
        assert read(root_new) == empty, f"{label}: 新账户继承了别人的数据"
        assert root_new == _isolated / "users" / "7"


# ================================================================
# 显式 user_root 压过上下文 (后台线程路径)
# ================================================================
@pytest.mark.parametrize("label, write, read, empty", STORES, ids=_IDS)
def test_explicit_user_root_overrides_context(label, write, read, empty, _isolated):
    """后台线程没有请求上下文: 显式传 root 必须写进/读到**该**账户。"""
    root_a = user_paths.user_root(1)
    with as_account(2):
        # 上下文是 B, 但显式指定 A → 落在 A
        write(root_a)
        assert read(root_a) != empty, f"{label}: 显式 root 没生效"
        assert read(user_paths.user_root(2)) == empty, f"{label}: 写到了上下文里的 B"


# ================================================================
# fail-closed: 无上下文 + 无显式 root → 报错, 不回退共享文件
# ================================================================
@pytest.mark.parametrize("label, write, read, empty", STORES, ids=_IDS)
def test_read_fails_closed_without_context_or_root(label, write, read, empty, _isolated):
    with no_account(), pytest.raises(user_paths.MissingUserContextError):
        read(None)


@pytest.mark.parametrize("label, write, read, empty", STORES, ids=_IDS)
def test_write_fails_closed_without_context_or_root(label, write, read, empty, _isolated):
    with no_account(), pytest.raises(user_paths.MissingUserContextError):
        write(None)
    # 失败后不得留下任何共享目录落地物 (data/user_data 等)
    assert not (_isolated / "user_data").exists(), f"{label}: 报错路径仍创建了共享目录"


# ================================================================
# 账户 A 的删除/覆盖不影响账户 B (同名 id 的两个账户互不干扰)
# ================================================================
def test_same_inner_account_name_is_independent_per_panel_account(_isolated):
    """模拟盘内层 account_id (字符串账户名) 与外层面板账户是两套命名空间。

    两个面板账户下各建一个同名 "acc_a": 数据必须互不可见 —— 外层目录不同,
    内层 id 相同。
    """
    with as_account(1) as root_a:
        paper.create_account(100_000.0, account_id="acc_a", user_root=root_a)
    with as_account(2) as root_b:
        paper.create_account(555_000.0, account_id="acc_a", user_root=root_b)
        assert [a["initial_cash"] for a in paper.list_accounts(root_b)] == [555_000.0]

    with as_account(1) as root_a:
        assert [a["initial_cash"] for a in paper.list_accounts(root_a)] == [100_000.0]


def test_lot_delete_in_one_account_keeps_the_other(_isolated):
    """同名批次 id 分属两个账户: A 删除不影响 B。"""
    with as_account(1) as root_a:
        lots.save_one(dict(_LOT), root_a)
    with as_account(2) as root_b:
        lots.save_one(dict(_LOT), root_b)

    with as_account(1) as root_a:
        assert lots.delete_one(_LOT["id"], root_a) is True
        assert lots.load_all(root_a) == []

    with as_account(2) as root_b:
        assert [lot["id"] for lot in lots.load_all(root_b)] == [_LOT["id"]]


def test_report_delete_of_other_account_is_a_no_op(_isolated):
    """跨账户删除: 拿 A 的 report_id 去删 B 的库 → 失败, 且 A 的报告仍在。"""
    with as_account(1) as root_a:
        saved = ai_reports.save_report({"symbol": "600519.SH", "content": "A 的报告"}, root_a)

    with as_account(2) as root_b:
        assert ai_reports.delete_report(saved["id"], root_b) is False
        assert ai_reports.list_reports(root_b) == []

    with as_account(1) as root_a:
        assert [r["id"] for r in ai_reports.list_reports(root_a)] == [saved["id"]]


def test_factor_delete_in_one_account_keeps_the_other(_isolated):
    with as_account(1) as root_a:
        factors_store.save_one(dict(_FACTOR), root_a)
    with as_account(2) as root_b:
        factors_store.save_one(dict(_FACTOR), root_b)

    with as_account(1) as root_a:
        assert factors_store.delete_one(_FACTOR["id"], root_a) is True

    with as_account(2) as root_b:
        assert [d["id"] for d in factors_store.load_all(root_b)] == [_FACTOR["id"]]


def test_secrets_are_per_account_and_stay_0600(_isolated):
    """个人凭据(AI Key / SMTP 密码)分账户存放, 且仍然 0600 + 原子写 (无 .tmp 残留)。"""
    with as_account(1) as root_a:
        secrets_store.save({"ai_api_key": "sk-A", "email_smtp_password": "pw-A"}, root_a)
    with as_account(2) as root_b:
        secrets_store.save({"ai_api_key": "sk-B"}, root_b)

    with as_account(1) as root_a:
        assert secrets_store.load(root_a) == {"ai_api_key": "sk-A", "email_smtp_password": "pw-A"}
        assert secrets_store.get_ai_key(root_a) == "sk-A"
    with as_account(2) as root_b:
        assert secrets_store.load(root_b) == {"ai_api_key": "sk-B"}
        # SMTP 密码没配 → 空, 不会串到 A 的
        assert secrets_store.get_email_smtp_password(root_b) == ""

    path_a = root_a / "user_data" / "secrets.json"
    assert stat.S_IMODE(path_a.stat().st_mode) == 0o600
    assert list(_isolated.rglob("*.tmp")) == []


def test_report_store_default_path_is_under_the_user_root(_isolated):
    """路径形状的兜底断言 (证据仍以上面的服务级读取为准)。"""
    with as_account(1) as root_a:
        store = json_report_store.JsonReportStore("ai_reports.json", 20, id_prefix="rpt")
        assert store._path() == Path(root_a) / "user_data" / "ai_reports.json"
    assert not (_isolated / "user_data" / "ai_reports.json").exists()
