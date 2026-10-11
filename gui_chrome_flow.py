"""登录引导 —— 非模态版. 主窗口一块引导区 CTkFrame(host)由本控制器 show/hide.
不弹 Toplevel、不设 -topmost、不 grab_set: 登录期间用户能最小化窗口去干别的.

只有用户主动「新建账号 / 重新登录」才走这个流程. 调度器换账号时不用它 —— 那条路
零人工: 直接 launch_chrome_for_profile(..., fullscreen=True) + wait_for_florr_tab,
超时就跳过时块.
"""
import cdp_bridge

_POLL_MS = 2000


class LoginGuide:
    """host 需要 show() / hide() 两个方法(把引导区 frame 显隐), 可选 detected().
    after = 某个 widget 的 .after(ms, fn); launch / poll 可注入, 便于单测."""

    def __init__(self, host, *, after, launch=cdp_bridge.launch_chrome_for_profile,
                 poll=cdp_bridge.wait_for_florr_tab, on_launch_error=None):
        self._host = host
        self._after = after
        self._launch = launch
        self._poll = poll
        # Chrome 起不来(最常见: 没装)时怎么告诉用户; 不给就保持老行为, 异常往上抛
        self._on_launch_error = on_launch_error
        self._on_done = None
        self._on_cancel = None
        self._detected = False
        self._active = False

    def start(self, profile_dir, *, on_done, on_cancel):
        self._on_done, self._on_cancel = on_done, on_cancel
        self._detected = False
        self._active = True
        try:
            self._launch(profile_dir, open_url="https://florr.io", fullscreen=False)
        except RuntimeError as e:
            # 没装 Chrome 时 launch 抛 RuntimeError。2026-10-09 南京用户点「登录」就这样变成了一次
            # 未捕获的 GUI 异常(还被当成 bug 上报)。起不来 = 这次登录没成: 不亮引导区、不轮询,
            # 告诉用户原因, 再按「取消」通知调用方。
            if self._on_launch_error is None:
                raise
            self._active = False
            self._on_launch_error(e)
            if on_cancel:
                on_cancel()
            return
        self._host.show()
        self._after(_POLL_MS, self._poll_once)

    def _poll_once(self):
        if not self._active:
            return
        if self._poll(1) is not None:
            self._detected = True
            # 可选: host 有 detected() 就让它在界面上亮个"已检测到"(测试用的假 host 没有)
            detected = getattr(self._host, "detected", None)
            if detected is not None:
                detected()
            return
        self._after(_POLL_MS, self._poll_once)

    def finish(self):
        if not self._active:
            return
        self._active = False
        self._host.hide()
        if self._on_done:
            self._on_done()

    def cancel(self):
        if not self._active:
            return
        self._active = False
        self._host.hide()
        if self._on_cancel:
            self._on_cancel()
