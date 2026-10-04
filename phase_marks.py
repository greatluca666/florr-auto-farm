"""worker 往 stdout 打「阶段标记」, 控制面板读日志时顺手记账(见 telemetry_clock.py).

三类阶段: farm(刷怪: auto_farming 里的时间, 含就地复活)、travel(赶路: 进场路线 / 寻路去刷怪区 /
走门换服)、other(其余: 死亡结算、点开始、等 AFK 解题……, 也是默认值)。

用法: `with phase_marks.phase("travel"): ...`。嵌套时退出自动恢复外层阶段; 相邻重复的阶段不重复打。
只有环境变量 FLORR_PHASE_MARKERS=1(控制面板拉起 worker 时设)才真的打印 —— 直接 `python main.py`
调试、Ubuntu 无头部署都不设, 不产生这行; 阶段本身照样跟踪(current() 给测试用)。

只从 worker 主线程用: 栈是全局的, 多线程交错进出会乱(锁只保证不崩)。
"""
import contextlib
import os
import threading

PREFIX = "@@florr-phase:"
PHASES = ("farm", "travel", "other")
DEFAULT = "other"
ENV_VAR = "FLORR_PHASE_MARKERS"

_lock = threading.Lock()
_stack = [DEFAULT]
_last_printed = None


def enabled():
    return os.environ.get(ENV_VAR) == "1"


def current():
    with _lock:
        return _stack[-1]


def _emit(name):
    """调用方已持有 _lock. 关着、或和上一次打的一样就不打."""
    global _last_printed
    if not enabled() or name == _last_printed:
        return
    _last_printed = name
    print(f"{PREFIX}{name}", flush=True)


@contextlib.contextmanager
def phase(name):
    if name not in PHASES:
        raise ValueError(f"未知阶段: {name!r}")
    with _lock:
        _stack.append(name)
        _emit(name)
    try:
        yield
    finally:
        with _lock:
            _stack.pop()
            _emit(_stack[-1])


def _reset_for_tests():
    global _last_printed
    with _lock:
        _stack[:] = [DEFAULT]
        _last_printed = None
