import os
from datetime import datetime, timedelta

import worker_log


def _clock(start):
    t = [start]

    def now():
        return t[0]
    return now, t


def test_lines_get_a_wall_clock_stamp(tmp_path):
    now, t = _clock(datetime(2026, 9, 28, 14, 5, 7, 123456))
    log = worker_log.WorkerLog(str(tmp_path), now=now)
    log.write("🎮 开始自动寻路\n")
    t[0] += timedelta(seconds=1)
    log.write("没有换行的一行")
    log.close()
    assert os.path.basename(log.path) == "worker-20260928-140507.log"
    with open(log.path, encoding="utf-8") as f:
        assert f.read() == "14:05:07.123 🎮 开始自动寻路\n14:05:08.123 没有换行的一行\n"


def test_each_line_reaches_disk_before_close(tmp_path):
    # worker 崩了 / GUI 被杀时文件里也得有东西
    now, _ = _clock(datetime(2026, 9, 28, 14, 5, 7))
    log = worker_log.WorkerLog(str(tmp_path), now=now)
    log.write("a\n")
    with open(log.path, encoding="utf-8") as f:
        assert f.read().endswith("a\n")
    log.close()


def test_old_logs_are_pruned(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    for i in range(5):
        (d / f"worker-2026092{i}-000000.log").write_text("x")
    (d / "notes.txt").write_text("别动我")
    now, _ = _clock(datetime(2026, 9, 28, 1, 0, 0))
    worker_log.WorkerLog(str(tmp_path), now=now, keep=3).close()
    left = sorted(os.listdir(d))
    assert left == ["notes.txt", "worker-20260923-000000.log", "worker-20260924-000000.log",
                    "worker-20260928-010000.log"]


def test_unwritable_directory_never_raises(tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("这是个文件, 不是目录")
    log = worker_log.WorkerLog(str(tmp_path))
    log.write("没地方写也不能炸\n")
    log.close()
    assert log.path is None


def test_write_after_close_is_ignored(tmp_path):
    log = worker_log.WorkerLog(str(tmp_path))
    log.close()
    log.write("晚到的一行\n")
