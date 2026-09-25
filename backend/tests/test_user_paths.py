"""账号私有路径解析 (app.services.user_paths) 的测试。

覆盖: 路径形状、账号 ID 的 fail-closed 校验 (含 bool 陷阱与路径穿越)、
目录骨架的创建与幂等、以及任何输入都不会逃出 data_dir。
"""
from __future__ import annotations

import pytest

from app import config as app_config
from app.services.user_paths import (
    USER_SUBDIRS,
    InvalidAccountIdError,
    ensure_user_dirs,
    user_root,
    validate_account_id,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每个用例一个独立 DATA_DIR, 用户目录随之隔离。"""
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    yield tmp_path


# ================================================================
# 路径形状
# ================================================================

def test_user_root_is_nested_under_data_dir(_isolated):
    assert user_root(7) == _isolated / "users" / "7"


def test_user_root_accepts_clean_decimal_string(_isolated):
    """字符串形态 (URL/JSON 里常见) 运行期同样放行, 虽然标注是 int。"""
    assert user_root("7") == _isolated / "users" / "7"  # type: ignore[arg-type]


def test_user_root_accepts_large_positive_int(_isolated):
    assert user_root(10**9) == _isolated / "users" / str(10**9)


def test_user_root_does_not_create_anything(_isolated):
    user_root(1)
    assert not (_isolated / "users").exists()


# ================================================================
# validate_account_id — 接受
# ================================================================

@pytest.mark.parametrize("raw", [1, 42, 10**9, "7", "42"])
def test_validate_accepts_positive(raw):
    assert validate_account_id(raw) == int(raw)


def test_validate_normalizes_string_to_int():
    value = validate_account_id("7")
    assert isinstance(value, int)
    assert value == 7


# ================================================================
# validate_account_id — 拒绝 (fail-closed)
# ================================================================

@pytest.mark.parametrize(
    "raw",
    [
        None,           # 非 int/str
        "abc",          # 非数字字符串
        1.5,            # float
        1.0,            # 整值 float 同样拒绝 (类型不对就是不对)
        [],             # list
        {},             # dict
        0,              # 零
        -1,             # 负数
        "-42",
        "0",
        True,           # bool 是 int 子类, 必须显式挡掉
        False,
        "1/2",          # 含分隔符
        "../etc",       # 路径穿越
        "..",
        "1 ",           # 尾随空格
        " 1",           # 前导空格
        "+1",           # 带正号
        "1.0",          # 小数点
        "0x1",          # 十六进制
        "1_000",        # 下划线分隔
        "１２３",  # noqa: RUF001 — 全角数字, int() 认得但目录名形态不一致
        "١٢٣",          # 阿拉伯-印度数字, 同上
        "",             # 空串
        " ",            # 仅空白
    ],
)
def test_validate_rejects(raw):
    with pytest.raises(InvalidAccountIdError):
        validate_account_id(raw)


def test_invalid_error_is_value_error_subclass():
    """调用方可能只捕获 ValueError, 异常必须是它的子类。"""
    assert issubclass(InvalidAccountIdError, ValueError)


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc",
        "../../../../etc/passwd",
        "..%2f..%2fetc",
        "1/../..",
        "/etc",
        "users/../../etc",
        "7/../../..",
    ],
)
def test_traversal_attempts_are_rejected(raw):
    with pytest.raises(InvalidAccountIdError):
        validate_account_id(raw)


# ================================================================
# 穿越防护 — 任何输入都不逃出 data_dir
# ================================================================

def test_traversal_never_escapes_data_dir(_isolated):
    """穿越输入必须在校验层就被拒, 连目录都不会被创建。"""
    for raw in ["../../etc", "..", "../..", "7/../../etc"]:
        with pytest.raises(InvalidAccountIdError):
            user_root(raw)  # type: ignore[arg-type]
        with pytest.raises(InvalidAccountIdError):
            ensure_user_dirs(raw)  # type: ignore[arg-type]
    assert not (_isolated / "users").exists()


def test_accepted_root_stays_under_data_dir(_isolated):
    for raw in [1, 42, "7", 10**9]:
        resolved = user_root(raw).resolve()
        assert resolved.is_relative_to(_isolated.resolve())


# ================================================================
# ensure_user_dirs — 骨架与幂等
# ================================================================

def test_ensure_creates_exactly_the_skeleton(_isolated):
    root = ensure_user_dirs(3)
    assert root == _isolated / "users" / "3"
    assert root.is_dir()
    for sub in USER_SUBDIRS:
        assert (root / sub).is_dir(), f"缺少骨架目录: {sub}"


def test_ensure_creates_no_unexpected_entries(_isolated):
    """实际落盘的目录集合 == 骨架项 + 它们的中间父目录, 无多余项。"""
    root = ensure_user_dirs(3)
    created = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir()}
    expected: set[str] = set()
    for sub in USER_SUBDIRS:
        parts = sub.split("/")
        expected.update("/".join(parts[: i + 1]) for i in range(len(parts)))
    assert created == expected


def test_ensure_subdirs_are_nested_where_declared(_isolated):
    root = ensure_user_dirs(3)
    assert (root / "strategies" / "custom").is_dir()
    assert (root / "strategies" / "ai").is_dir()
    assert (root / "strategies" / "composite").is_dir()
    assert (root / "research" / "mining" / "runs").is_dir()
    assert (root / "paper" / "accounts").is_dir()


def test_ensure_is_idempotent(_isolated):
    first = ensure_user_dirs(5)
    second = ensure_user_dirs(5)
    assert first == second
    for sub in USER_SUBDIRS:
        assert (first / sub).is_dir()


def test_ensure_keeps_existing_content(_isolated):
    root = ensure_user_dirs(5)
    marker = root / "user_data" / "keep.json"
    marker.write_text("{}", encoding="utf-8")
    ensure_user_dirs(5)
    assert marker.read_text(encoding="utf-8") == "{}"


def test_ensure_returns_same_path_as_user_root(_isolated):
    assert ensure_user_dirs(9) == user_root(9)


def test_different_accounts_are_separate_roots(_isolated):
    a = ensure_user_dirs(1)
    b = ensure_user_dirs(2)
    assert a != b
    assert user_root(1) != user_root(2)


def test_ensure_rejects_invalid_id_without_creating_dirs(_isolated):
    with pytest.raises(InvalidAccountIdError):
        ensure_user_dirs(0)  # type: ignore[arg-type]
    assert not (_isolated / "users").exists()


def test_skeleton_is_resolvable_relative_to_user_root(_isolated):
    """骨架项是相对路径片段, 拼接后仍必须落在该账号根目录内。"""
    root = user_root(7)
    for sub in USER_SUBDIRS:
        assert (root / sub).resolve().is_relative_to(root.resolve())
        assert ".." not in sub.split("/")
