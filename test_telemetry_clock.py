import random
import threading

import pytest

import phase_marks
import telemetry_clock
from telemetry_clock import TelemetryClock


class T:
    """可以手拨的假时钟."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make():
    t = T()
    return t, TelemetryClock(clock=t)


def test_window_splits_alive_seconds_by_phase_and_other_is_the_remainder():
    t, c = make()
    c.worker_started()
    t.t = 10
    c.mark("travel")
    t.t = 40
    c.mark("farm")
    t.t = 100
    assert c.take_window() == {"dur": 100, "ph": {"farm": 60, "travel": 30, "other": 10}}


def test_taking_a_window_resets_it_but_keeps_the_current_phase():
    t, c = make()
    c.worker_started()
    t.t = 40
    c.mark("farm")
    t.t = 100
    c.take_window()
    t.t = 130
    assert c.take_window() == {"dur": 30, "ph": {"farm": 30, "travel": 0, "other": 0}}


def test_a_clock_that_never_started_has_nothing_to_report():
    _, c = make()
    assert c.take_window() is None


def test_a_running_worker_with_no_time_yet_reports_a_zero_window_not_none():
    _, c = make()
    c.worker_started()
    assert c.take_window() == {"dur": 0, "ph": {"farm": 0, "travel": 0, "other": 0}}


def test_a_stopped_clock_stops_counting_and_the_leftover_is_reported_once():
    t, c = make()
    c.worker_started()
    t.t = 50
    c.worker_stopped()
    t.t = 500
    assert c.take_window() == {"dur": 50, "ph": {"farm": 0, "travel": 0, "other": 50}}
    assert c.take_window() is None


def test_stopping_twice_does_not_double_count():
    t, c = make()
    c.worker_started()
    t.t = 50
    c.worker_stopped()
    t.t = 80
    c.worker_stopped()
    assert c.take_window()["dur"] == 50


def test_marks_are_ignored_when_not_running_or_unknown():
    t, c = make()
    c.mark("farm")                                   # 没在跑: 忽略
    c.worker_started()
    t.t = 10
    c.mark("nap")                                    # 不认识: 忽略
    t.t = 20
    assert c.take_window()["ph"] == {"farm": 0, "travel": 0, "other": 20}


def test_a_restarted_worker_begins_in_other_again():
    t, c = make()
    c.worker_started()
    c.mark("farm")
    t.t = 10
    c.worker_stopped()
    c.take_window()
    t.t = 20
    c.worker_started()
    t.t = 30
    assert c.take_window()["ph"] == {"farm": 0, "travel": 0, "other": 10}


def test_a_second_start_while_running_settles_the_old_phase_first():
    t, c = make()
    c.worker_started()
    c.mark("farm")
    t.t = 10
    c.worker_started()                               # 重复 start: 先把已经过去的 10 秒算给 farm
    t.t = 15
    assert c.take_window() == {"dur": 15, "ph": {"farm": 10, "travel": 0, "other": 5}}


def test_rounding_up_both_phases_is_corrected_so_other_never_goes_negative():
    t, c = make()
    c.worker_started()
    c.mark("farm")                                   # t=0: other 一秒都没占
    t.t = 0.6
    c.mark("travel")
    t.t = 1.2
    # farm 0.6 -> 1, travel 0.6 -> 1, 但总共只有 1.2 -> dur 1: 多出来的 1 秒先从 travel 扣
    assert c.take_window() == {"dur": 1, "ph": {"farm": 1, "travel": 0, "other": 0}}


def test_rounding_never_makes_the_phases_exceed_dur():
    rng = random.Random(1)
    for _ in range(600):
        t, c = make()
        c.worker_started()
        for _ in range(rng.randint(1, 6)):
            t.t += rng.random() * rng.choice([1, 3, 40])      # 多造几段只有几秒、带小数的
            c.mark(rng.choice(phase_marks.PHASES))
        t.t += rng.random() * rng.choice([1, 3, 40])
        total = t.t
        w = c.take_window()
        ph = w["ph"]
        assert min(ph.values()) >= 0
        assert sum(ph.values()) == w["dur"]
        assert abs(w["dur"] - total) <= 0.5 + 1e-9


@pytest.mark.parametrize("line,want", [
    ("@@florr-phase:farm\n", "farm"),
    ("  @@florr-phase:travel \r\n", "travel"),
    ("@@florr-phase:other", "other"),
    ("@@florr-phase:nap\n", None),
    ("@@florr-phase:\n", None),
    ("hello @@florr-phase:farm\n", None),
    ("普通日志\n", None),
    ("", None),
])
def test_parse_marker(line, want):
    assert telemetry_clock.parse_marker(line) == want


@pytest.mark.parametrize("line,want", [
    ("@@florr-phase:farm\n", True), ("@@florr-phase:nap\n", True),
    ("普通日志\n", False), ("hello @@florr-phase:farm\n", False),
])
def test_is_marker_swallows_even_unknown_names_but_not_lookalikes(line, want):
    assert telemetry_clock.is_marker(line) is want


def test_handle_line_swallows_only_marker_lines_and_switches_the_phase():
    t, c = make()
    c.worker_started()
    t.t = 10
    assert c.handle_line("普通日志\n") is False
    assert c.handle_line("@@florr-phase:farm\n") is True
    t.t = 30
    assert c.handle_line("@@florr-phase:nap\n") is True       # 吞掉, 但不切阶段
    assert c.take_window()["ph"] == {"farm": 20, "travel": 0, "other": 10}


def test_the_line_the_worker_prints_is_the_line_the_clock_parses(monkeypatch, capsys):
    monkeypatch.setenv(phase_marks.ENV_VAR, "1")
    phase_marks._reset_for_tests()
    with phase_marks.phase("farm"):
        pass
    out = capsys.readouterr().out.splitlines()
    assert [telemetry_clock.parse_marker(ln) for ln in out] == ["farm", "other"]
    phase_marks._reset_for_tests()


def test_markers_survive_a_real_child_process_and_stdout_pipe():
    """假对象测不到: 真的子进程(像 worker 那样带 FLORR_PHASE_MARKERS=1、无缓冲、UTF-8 文本管道)打的标记行,
    能被控制面板这边逐行读到、认出来, 而且不混进普通日志."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parent)
    code = ("import phase_marks\n"
            "with phase_marks.phase('travel'):\n"
            "    print('去刷怪区', flush=True)\n"
            "    with phase_marks.phase('farm'):\n"
            "        print('刷怪中', flush=True)\n")
    env = {**os.environ, phase_marks.ENV_VAR: "1", "PYTHONUNBUFFERED": "1",
           "PYTHONIOENCODING": "utf-8", "PYTHONPATH": root}
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=root, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    marks, logs = [], []

    class Spy(TelemetryClock):
        def mark(self, name, now=None):
            marks.append(name)
            super().mark(name, now)

    clock = Spy()
    clock.worker_started()
    for line in proc.stdout:
        if not clock.handle_line(line):
            logs.append(line)
    assert proc.wait(timeout=30) == 0
    assert marks == ["travel", "farm", "travel", "other"]
    assert logs == ["去刷怪区\n", "刷怪中\n"]


def test_marks_from_the_reader_thread_and_windows_from_the_ui_thread_do_not_clash():
    c = TelemetryClock()
    c.worker_started()
    errors, windows = [], []

    def reader():
        try:
            for i in range(3000):
                c.handle_line(f"@@florr-phase:{phase_marks.PHASES[i % 3]}\n")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for th in threads:
        th.start()
    for _ in range(200):
        windows.append(c.take_window())
    for th in threads:
        th.join()
    assert not errors
    assert all(w is not None and w["dur"] >= 0 and min(w["ph"].values()) >= 0 for w in windows)
