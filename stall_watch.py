"""卡顿看门狗: 进程 / 整台机器被卡住时, 把"当时主线程停在哪行代码 + 谁在吃 CPU"记进日志。

2026-10-15 工厂录像 02:53~03:05: 寻路规划平时 6~10 秒, 那 12 分钟里变成 60 / 159 / 165 / 121 秒,
录像器的 tick 间隔 40~60 秒, 日志两行之间隔 1~2 分钟; 截图里游戏画面本身是正常的。当时日志里没有任何线索
能说清是机器被占满、Chrome 窗口被遮挡后截图等不到帧、还是我们自己的代码卡在哪 —— 这个看门狗就是补这个证据。

原理: 单开一个守护线程, 每 INTERVAL_S 秒醒一次。线程只 sleep, 没有别的活, 所以它醒晚了 = 整个进程
(或整台机器)被卡了; 只有主线程卡住(比如阻塞在 CDP 往返上)它不会晚。醒晚超过 STALL_S 就记一行:
  - 主线程此刻的调用栈尾部(文件:行 函数): 卡在 CDP 收包 / 截图 / numpy 算路径一眼能看出来
  - 系统 CPU% / 内存% 和最占 CPU 的 3 个进程(psutil 在才有; 打包版不带, 缺了只少这一项)
持续卡着时每 COOLDOWN_S 秒才记一次, 别刷屏。看门狗本身绝不抛、绝不拦主程序。
"""
import sys
import threading
import time
import traceback

INTERVAL_S = 1.0
STALL_S = 3.0           # 睡 1 秒却隔了 4 秒才醒 = 卡了 3 秒
COOLDOWN_S = 30.0
_STACK_TAIL = 4         # 主线程调用栈只打最里面这几层
_TOP_PROCS = 3
_SAMPLE_S = 0.5         # 采最占 CPU 的进程时的采样窗口


def describe_main_thread(frames=None, main_ident=None):
    """主线程当前调用栈的尾部, 形如 'main.py:812 follow_path <- utils.py:90 get_player_position'。
    读不到就返回说明文字, 不抛。frames/main_ident 给测试注入用。"""
    try:
        if frames is None:
            frames = sys._current_frames()
        if main_ident is None:
            main_ident = threading.main_thread().ident
        frame = frames.get(main_ident)
        if frame is None:
            return "主线程栈读不到"
        stack = traceback.extract_stack(frame)[-_STACK_TAIL:]
        return " <- ".join(f"{_short(s.filename)}:{s.lineno} {s.name}" for s in reversed(stack))
    except Exception as e:
        return f"主线程栈读不到({e})"


def _short(path):
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def system_snapshot():
    """系统 CPU% / 内存% + 最占 CPU 的进程。psutil 没装或出错都只返回说明文字。"""
    try:
        import psutil
    except ImportError:
        return "psutil 没装, 看不到系统占用"
    try:
        procs = []
        for p in psutil.process_iter(["name"]):
            try:
                p.cpu_percent(None)             # 预热: 第一次调用恒为 0
                procs.append(p)
            except Exception:
                pass
        psutil.cpu_percent(None)                # 预热系统 CPU
        time.sleep(_SAMPLE_S)
        rows = []
        for p in procs:
            try:
                rows.append((p.cpu_percent(None), p.info.get("name") or "?", p.pid))
            except Exception:
                pass
        rows.sort(reverse=True)
        cpu = psutil.cpu_percent(None)
        mem = psutil.virtual_memory().percent
        top = ", ".join(f"{n}({pid}) {c:.0f}%" for c, n, pid in rows[:_TOP_PROCS])
        return f"系统 CPU {cpu:.0f}% 内存 {mem:.0f}%; 最占 CPU: {top}"
    except Exception as e:
        return f"系统占用读不到({e})"


class StallWatch:
    """step(now) 每秒调一次(测试里手动调); start() 起守护线程替你调。"""

    def __init__(self, *, report=print, snapshot=system_snapshot, describe=describe_main_thread,
                 interval=INTERVAL_S, stall=STALL_S, cooldown=COOLDOWN_S):
        self.report, self.snapshot, self.describe = report, snapshot, describe
        self.interval, self.stall, self.cooldown = interval, stall, cooldown
        self._last = None               # 上次醒来的时刻
        self._last_report = None
        self._stop = threading.Event()
        self._thread = None

    def step(self, now):
        """返回这次的醒晚秒数(没超过阈值返回 0)。"""
        last, self._last = self._last, now
        if last is None:
            return 0.0
        late = (now - last) - self.interval
        if late < self.stall:
            return 0.0
        if self._last_report is not None and now - self._last_report < self.cooldown:
            return late
        self._last_report = now
        try:
            self.report(f"🐢 进程被卡住了约 {late:.0f} 秒(看门狗线程睡 {self.interval:.0f} 秒实际隔了 "
                        f"{late + self.interval:.0f} 秒才醒): 主线程停在 {self.describe()}; {self.snapshot()}")
        except Exception:
            pass                        # 看门狗不能因为自己出错带崩 worker
        return late

    def start(self):
        if self._thread is not None:
            return self.stop
        self._last = time.monotonic()

        def run():
            while not self._stop.wait(self.interval):
                try:
                    self.step(time.monotonic())
                except Exception:
                    pass

        self._thread = threading.Thread(target=run, name="stall-watch", daemon=True)
        self._thread.start()
        return self.stop

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def start():
    """worker 起来时调一次。返回 stop()。"""
    return StallWatch().start()
