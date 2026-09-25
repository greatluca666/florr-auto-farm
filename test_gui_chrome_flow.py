import gui_chrome_flow as flow


class FakeAfter:
    """把 widget.after(ms, fn) 收集起来, 手动 flush —— 不进 tk 主循环。"""
    def __init__(self):
        self.calls = []

    def __call__(self, ms, fn=None, *a):
        if fn is not None:
            self.calls.append((fn, a))
        return len(self.calls)

    def flush(self):
        pending, self.calls = self.calls, []
        for fn, a in pending:
            fn(*a)


class FakeHost:
    """冒充引导区 CTkFrame: 只记录 show/hide。"""
    def __init__(self):
        self.visible = False

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


def _guide(tab_results, after):
    launched = []
    it = iter(tab_results)

    def launch(profile_dir, **kw):
        launched.append((profile_dir, kw))

    def poll(timeout):
        try:
            return next(it)
        except StopIteration:
            return None

    host = FakeHost()
    g = flow.LoginGuide(host, after=after, launch=launch, poll=poll)
    g._launched = launched
    return g, host


def test_start_launches_windowed_florr_and_shows_host():
    after = FakeAfter()
    g, host = _guide([None], after)
    g.start("chrome-profiles/小号2", on_done=lambda: None, on_cancel=lambda: None)
    assert g._launched[0][0] == "chrome-profiles/小号2"
    assert g._launched[0][1]["fullscreen"] is False
    assert g._launched[0][1]["open_url"] == "https://florr.io"
    assert host.visible is True


def test_poll_until_tab_then_finish_calls_on_done():
    after = FakeAfter()
    g, host = _guide([None, None, {"url": "https://florr.io/"}], after)
    done = []
    g.start("d", on_done=lambda: done.append(1), on_cancel=lambda: None)
    after.flush()   # poll #1 -> None -> reschedule
    after.flush()   # poll #2 -> None -> reschedule
    after.flush()   # poll #3 -> tab found
    assert g._detected is True
    g.finish()
    assert done == [1]
    assert host.visible is False


def test_cancel_hides_and_calls_on_cancel_and_stops_polling():
    after = FakeAfter()
    g, host = _guide([None], after)
    cancelled = []
    g.start("d", on_done=lambda: None, on_cancel=lambda: cancelled.append(1))
    g.cancel()
    assert cancelled == [1]
    assert host.visible is False
    after.flush()   # 已取消 —— 不该再有 poll 回调执行(不抛即可)


def test_manual_finish_before_detection_still_works():
    after = FakeAfter()
    g, host = _guide([None], after)
    done = []
    g.start("d", on_done=lambda: done.append(1), on_cancel=lambda: None)
    g.finish()      # 用户没等检测就手点「完成」
    assert done == [1]
