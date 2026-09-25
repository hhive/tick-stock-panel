"""并发写 preferences 不得互相覆盖。

preferences.save 的 docstring 记着这个坑: "FastAPI 同步端点跑线程池, 并行 PUT
各自基于旧快照写盘会互相覆盖", 所以 save 的 read-modify-write 整段在 _SAVE_LOCK
里。set_realtime_quote_interval 曾是唯一一个绕开该锁、自己 load + write_text 的
setter —— 那时 PUT /api/settings/preferences/quote-interval 与任意另一个偏好 PUT
同时在飞, 后者会被前者用旧快照整体覆盖掉。

该 setter 现已改走 save(); 本文件继续守着 save() 的 RMW 不得移出 _SAVE_LOCK ——
锁一旦被去掉或缩小, test_interval_setter_does_not_clobber_a_concurrent_save 会失败。
"""
from __future__ import annotations

import json
import threading

import pytest

from app.services import preferences

_OTHER_KEY = "realtime_quotes_enabled"


@pytest.fixture
def prefs_path(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_global_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_interval_setter_does_not_clobber_a_concurrent_save(prefs_path, monkeypatch):
    """轮询间隔写入与另一个偏好写入并发时, 两个键都要留下。

    用一个会在第一次调用时挂起的 _load_file 替身制造交错: 间隔 setter 在 save 内
    拿到快照后停住, 另一个 save 完整跑完, 然后间隔 setter 继续写盘。

    拦截点是 _load_file 而非 load(): 偏好拆成两文件后, save() 的 read-modify-write
    改用 _load_file(_global_path()) 分区读, load() 只剩末尾"合并返回"那一次调用 ——
    拦 load() 拦到的是写盘之后的返回, 两线程的 RMW 不再重叠, 测试会变成恒过。
    """
    preferences.save({_OTHER_KEY: False})

    first_read_entered = threading.Event()
    release_first_read = threading.Event()
    other_save_done = threading.Event()
    read_calls = []
    real_load_file = preferences._load_file

    def _load_file_pausing_on_first_call(path) -> dict:
        # 拦截点必须是 save() 里 read-modify-write 的那次读。拦截点选错会让本测试
        # 退化成"永远通过": 必须让暂停落在写盘之前, 才能制造出"读旧快照 → 别人写 →
        # 再写盘"的覆盖窗口。
        snapshot = real_load_file(path)
        read_calls.append(1)
        if len(read_calls) == 1:
            first_read_entered.set()
            release_first_read.wait(10)
        return snapshot

    monkeypatch.setattr(preferences, "_load_file", _load_file_pausing_on_first_call)

    def _set_interval() -> None:
        preferences.set_realtime_quote_interval(9.0)

    def _save_other() -> None:
        preferences.save({_OTHER_KEY: True})
        other_save_done.set()

    interval_thread = threading.Thread(target=_set_interval, name="set-interval")
    interval_thread.start()
    assert first_read_entered.wait(10), "间隔 setter 没有进入 save 的读取"

    other_thread = threading.Thread(target=_save_other, name="save-other")
    other_thread.start()
    # 有锁时另一个 save 会一直等到间隔 setter 写完 (这里超时是预期的);
    # 无锁时它会立刻写完, 随后被间隔 setter 的旧快照覆盖。
    other_save_done.wait(0.5)
    release_first_read.set()

    interval_thread.join(10)
    other_thread.join(10)
    assert not interval_thread.is_alive() and not other_thread.is_alive()

    monkeypatch.setattr(preferences, "_load_file", real_load_file)
    preferences._invalidate_cache()
    stored = _read(prefs_path)
    assert stored["realtime_quote_interval"] == 9.0
    assert stored[_OTHER_KEY] is True, "并发的偏好写入被间隔 setter 的旧快照覆盖了"


def test_interval_setter_keeps_existing_keys(prefs_path):
    """顺序场景: 写间隔不能丢掉文件里已有的其它偏好。"""
    preferences.save({_OTHER_KEY: True, "minute_sync_enabled": True})

    preferences.set_realtime_quote_interval(3.0)

    stored = _read(prefs_path)
    assert stored == {
        _OTHER_KEY: True,
        "minute_sync_enabled": True,
        "realtime_quote_interval": 3.0,
    }


def test_interval_setter_returns_value_and_refreshes_cache(prefs_path):
    """返回值与缓存失效行为保持不变。"""
    assert preferences.set_realtime_quote_interval(12.5) == 12.5
    assert preferences.get_realtime_quote_interval() == 12.5
