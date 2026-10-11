import threading
import time

import keep_awake


def test_does_nothing_off_windows(monkeypatch):
    monkeypatch.setattr(keep_awake.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(keep_awake, "_set_state", calls.append)
    assert keep_awake.start() is None
    assert calls == []


def test_asks_windows_to_keep_the_display_and_system_awake(monkeypatch):
    # 2026-10-08 第一份录像: 死后回到标题页, 页面连续 5 分钟没收到任何按键 / 鼠标移动(程序一直在注入),
    # 屏幕检测也啥都认不出, 用户一动鼠标就好了 —— 无人值守时显示器休眠 / 屏保 / 锁屏的症状。
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    calls, seen = [], threading.Event()

    def fake_set_state(flags):
        calls.append(flags)
        seen.set()
        return 1

    monkeypatch.setattr(keep_awake, "_set_state", fake_set_state)
    stop = keep_awake.start(interval=0.01)
    assert seen.wait(2)
    want = keep_awake.ES_CONTINUOUS | keep_awake.ES_SYSTEM_REQUIRED | keep_awake.ES_DISPLAY_REQUIRED
    assert calls[0] == want
    stop()


def test_stop_releases_the_request(monkeypatch):
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    calls, done = [], threading.Event()

    def fake_set_state(flags):
        calls.append(flags)
        if flags == keep_awake.ES_CONTINUOUS:
            done.set()
        return 1

    monkeypatch.setattr(keep_awake, "_set_state", fake_set_state)
    stop = keep_awake.start(interval=0.01)
    stop()
    assert done.wait(2)                      # 收尾时只留 ES_CONTINUOUS = 撤销要求
    assert calls[-1] == keep_awake.ES_CONTINUOUS


def test_renews_the_request_periodically(monkeypatch):
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(keep_awake, "_set_state", lambda f: calls.append(f) or 1)
    stop = keep_awake.start(interval=0.01)
    time.sleep(0.15)
    stop()
    assert sum(1 for f in calls if f != keep_awake.ES_CONTINUOUS) >= 3


def test_a_failing_windows_call_never_raises(monkeypatch):
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")
    attempted = threading.Event()

    def boom(flags):
        attempted.set()
        raise OSError("no kernel32")

    monkeypatch.setattr(keep_awake, "_set_state", boom)
    stop = keep_awake.start(interval=0.01)       # 防休眠是锦上添花, 不能拦住刷图
    assert attempted.wait(2)
    stop()
