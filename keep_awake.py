"""Windows 上无人值守跑的时候, 别让显示器休眠 / 进屏保 / 系统睡眠。

2026-10-08 第一份录像: 死后回到标题页, 页面连续 5 分钟没收到任何按键 / 鼠标移动(程序一直在注入),
屏幕检测也什么都认不出, 用户回来一动鼠标就好了。症状是"输入和画面都没到游戏", 对得上显示器休眠 /
屏保 / 锁屏, 但当时没留下能确认的证据(之后位置读不出来时会在日志里记屏幕体检, 见 main.py 的
_report_unreadable_position)。防休眠对长时间挂机本来就该有, 先加上。

SetThreadExecutionState 的 ES_CONTINUOUS 状态跟着调用它的**线程**走, 线程退出就失效, 所以单开一个
守护线程专门挂着它(顺便定期重申); 进程退出时 Windows 自动撤销。非 Windows 什么也不做。
管不了的: 手动锁屏(Win+L) / 组策略强制锁屏 / 远程桌面断开 —— 那些要从系统设置里关。
"""
import sys
import threading

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_RENEW_INTERVAL_S = 30.0


def _set_state(flags):
    import ctypes
    return ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))


def start(interval=_RENEW_INTERVAL_S):
    """开始要求 Windows 保持唤醒, 返回一个 stop() 函数(撤销要求)。非 Windows 返回 None。
    任何一步出错都只当没生效 —— 这是锦上添花, 不能拦住刷图。"""
    if sys.platform != "win32":
        return None
    stop_event = threading.Event()
    want = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

    def keep():
        while not stop_event.is_set():
            try:
                _set_state(want)
            except Exception:
                pass
            stop_event.wait(interval)
        try:
            _set_state(ES_CONTINUOUS)       # 只留 ES_CONTINUOUS = 撤销之前的要求
        except Exception:
            pass

    thread = threading.Thread(target=keep, name="keep-awake", daemon=True)
    thread.start()

    def stop():
        stop_event.set()
        thread.join(timeout=2)

    return stop
