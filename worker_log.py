"""worker 输出落盘: 控制面板的日志框只在内存里留 2000 行, 关掉就没了, 也没有时间戳。

每次拉起 worker 开一个 logs/worker-YYYYmmdd-HHMMSS.log, 每行前面加墙钟时间
(HH:MM:SS.mmm) —— record_session.py 录的画面按同一个墙钟对齐, 离线分析时才能知道
"这一帧的时候 worker 在干什么"。

写盘失败(磁盘满 / 没权限 / 目录被占)一律吞掉: 日志是附带的, 不能因为它把 worker
的输出泵停掉 —— 泵一停, 子进程写满管道就卡死了。
"""
import os
from datetime import datetime

KEEP = 20   # 只留最近这么多份, 再旧的删掉


def log_dir(root):
    return os.path.join(root, "logs")


def _prune(directory, keep):
    try:
        names = sorted(n for n in os.listdir(directory)
                       if n.startswith("worker-") and n.endswith(".log"))
    except OSError:
        return
    for n in names[:-keep] if keep > 0 else names:
        try:
            os.remove(os.path.join(directory, n))
        except OSError:
            pass


class WorkerLog:
    def __init__(self, root, *, now=datetime.now, keep=KEEP):
        self._now = now
        self._f = None
        self.path = None
        directory = log_dir(root)
        try:
            os.makedirs(directory, exist_ok=True)
            _prune(directory, keep - 1)          # 给这一份腾个位置
            self.path = os.path.join(directory, now().strftime("worker-%Y%m%d-%H%M%S.log"))
            self._f = open(self.path, "a", encoding="utf-8")
        except OSError:
            self._f = None

    def write(self, line):
        if self._f is None:
            return
        try:
            stamp = self._now().strftime("%H:%M:%S.%f")[:-3]
            self._f.write(f"{stamp} {line if line.endswith(chr(10)) else line + chr(10)}")
            self._f.flush()
        except (OSError, ValueError):
            pass

    def close(self):
        f, self._f = self._f, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
