"""控制面板这一侧的「挂机时间」记账: 只数 worker 活着的秒数, 按阶段(farm / travel / other)累计,
每次发心跳取走一个窗口. 阶段来自 worker 打在 stdout 里的 `@@florr-phase:<name>` 标记行(见 phase_marks.py).

`dur` 是这里用自己的时钟量出来的, 不信 worker 报的数: worker 被强杀、卡死, 这里照样知道它活了多久.
`_pump_log` 在读线程里调 handle_line, 界面线程调 take_window —— 所以全程带锁.
"""
import threading
import time

from phase_marks import DEFAULT, PHASES, PREFIX


def is_marker(line):
    """是不是标记行(名字认不认识都算: 认不得的也要吞掉, 别漏进日志)."""
    return line.strip().startswith(PREFIX)


def parse_marker(line):
    """'@@florr-phase:farm\\n' -> 'farm'; 不是标记行 / 名字不认识 -> None."""
    s = line.strip()
    if not s.startswith(PREFIX):
        return None
    name = s[len(PREFIX):]
    return name if name in PHASES else None


class TelemetryClock:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._running = False
        self._phase = DEFAULT
        self._since = 0.0
        self._secs = {p: 0.0 for p in PHASES}

    def _now(self, now):
        return self._clock() if now is None else now

    def _settle(self, now):
        """把 [_since, now) 这段记给当前阶段(只在 worker 活着时). 调用方已持锁."""
        if self._running:
            self._secs[self._phase] += max(0.0, now - self._since)
        self._since = now

    def worker_started(self, now=None):
        with self._lock:
            now = self._now(now)
            self._settle(now)                        # 重复 start: 先把已经过去的算给旧阶段
            self._running, self._phase, self._since = True, DEFAULT, now

    def worker_stopped(self, now=None):
        with self._lock:
            self._settle(self._now(now))
            self._running = False

    def mark(self, name, now=None):
        if name not in PHASES:
            return
        with self._lock:
            if not self._running:
                return
            self._settle(self._now(now))
            self._phase = name

    def handle_line(self, line):
        """worker 输出的一行: 标记行 -> 切阶段并返回 True(调用方别把它当日志); 否则返回 False."""
        if not is_marker(line):
            return False
        self.mark(parse_marker(line))
        return True

    def take_window(self, now=None):
        """取走自上次以来的窗口并清零. worker 没在跑、也没有遗留秒数 -> None.
        dur 四舍五入到整秒; farm / travel 各自取整, 超过 dur 的部分(取整抖动)先从 travel 再从 farm 扣;
        other = dur - farm - travel, 所以三项之和恒等于 dur."""
        with self._lock:
            self._settle(self._now(now))
            secs, self._secs = self._secs, {p: 0.0 for p in PHASES}
            dur = int(round(sum(secs.values())))
            if dur == 0 and not self._running:
                return None
            farm, travel = int(round(secs["farm"])), int(round(secs["travel"]))
            over = farm + travel - dur
            if over > 0:
                cut = min(over, travel)
                travel -= cut
                farm -= over - cut
            return {"dur": dur, "ph": {"farm": farm, "travel": travel, "other": dur - farm - travel}}
