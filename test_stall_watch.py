import sys
import threading
import time

import stall_watch
from stall_watch import StallWatch


def _watch(lines, **kw):
    kw.setdefault("snapshot", lambda: "SNAP")
    kw.setdefault("describe", lambda: "STACK")
    return StallWatch(report=lines.append, interval=1.0, stall=3.0, cooldown=30.0, **kw)


def test_on_time_wakeups_log_nothing():
    lines = []
    w = _watch(lines)
    for t in range(0, 10):
        assert w.step(float(t)) == 0.0
    assert lines == []


def test_a_wakeup_that_is_late_logs_the_stall_with_stack_and_snapshot():
    lines = []
    w = _watch(lines)
    w.step(0.0)
    late = w.step(10.0)                  # 本该 1 秒后醒, 隔了 10 秒 -> 卡了 9 秒
    assert late == 9.0
    assert len(lines) == 1
    assert "9 秒" in lines[0] and "STACK" in lines[0] and "SNAP" in lines[0]


def test_a_small_delay_below_the_threshold_is_not_a_stall():
    lines = []
    w = _watch(lines)
    w.step(0.0)
    assert w.step(3.5) == 0.0            # 只晚了 2.5 秒 < 3 秒
    assert lines == []


def test_a_long_stall_does_not_spam_within_the_cooldown():
    lines = []
    w = _watch(lines)
    w.step(0.0)
    w.step(10.0)
    w.step(20.0)
    w.step(30.0)                          # 仍在冷却期(<30 秒)
    assert len(lines) == 1
    w.step(45.0)                          # 冷却过了, 又卡了一次
    assert len(lines) == 2


def test_a_failing_report_or_snapshot_never_raises():
    def boom():
        raise RuntimeError("x")
    w = StallWatch(report=lambda m: boom(), snapshot=boom, describe=boom)
    w.step(0.0)
    w.step(10.0)                          # 不抛就算过


def test_describe_main_thread_names_the_innermost_frames():
    out = stall_watch.describe_main_thread()
    # 测试跑在主线程里: 栈尾就是这个测试函数
    assert "test_describe_main_thread_names_the_innermost_frames" in out
    assert "test_stall_watch.py" in out


def test_describe_main_thread_when_the_frame_is_missing():
    assert "读不到" in stall_watch.describe_main_thread(frames={}, main_ident=123)


def test_describe_main_thread_sees_a_blocked_main_thread_from_another_thread():
    # 真实场景: 看门狗线程去看**另一个**线程(主线程)此刻停在哪
    release = threading.Event()
    started = threading.Event()

    def busy_worker():
        started.set()
        release.wait(5)

    t = threading.Thread(target=busy_worker)
    t.start()
    started.wait(2)
    try:
        out = stall_watch.describe_main_thread(main_ident=t.ident)
        assert "busy_worker" in out
    finally:
        release.set()
        t.join()


def test_system_snapshot_without_psutil(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)      # import psutil -> ImportError
    assert "psutil" in stall_watch.system_snapshot()


def test_real_system_snapshot_has_cpu_and_memory():
    out = stall_watch.system_snapshot()
    assert "CPU" in out or "psutil" in out or "读不到" in out    # 任何环境下都不抛、不空


def test_start_runs_a_daemon_thread_that_stops():
    w = StallWatch(report=lambda m: None, interval=0.01)
    stop = w.start()
    try:
        time.sleep(0.05)
        assert w._thread.is_alive() and w._thread.daemon
    finally:
        stop()
    assert not w._thread.is_alive()
