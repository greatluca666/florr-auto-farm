import os
import sys

import pytest

import gui_app
import telemetry_clock


def test_worker_command_script_mode(monkeypatch):
    monkeypatch.setattr(gui_app.sys, "frozen", False, raising=False)
    cmd = gui_app.worker_command()
    assert cmd[0] == sys.executable
    assert cmd[1] == "-u"
    assert cmd[-1] == "--worker"
    assert cmd[-2].endswith("main.py")
    assert os.path.isabs(cmd[-2])


def test_worker_command_frozen_mode(monkeypatch):
    monkeypatch.setattr(gui_app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gui_app.sys, "executable", "/opt/florr/florr-auto-pathing")
    assert gui_app.worker_command() == ["/opt/florr/florr-auto-pathing", "--worker"]


def test_plan_transition_noop_same_block():
    blk = {"id": "b1", "profile": "默认"}
    assert gui_app.plan_transition("b1", blk, "默认") == {"action": "noop"}


def test_plan_transition_idle_when_leaving_to_gap():
    assert gui_app.plan_transition("b1", None, "默认") == {"action": "idle"}


def test_plan_transition_noop_when_already_idle():
    assert gui_app.plan_transition(None, None, None) == {"action": "noop"}


def test_plan_transition_run_same_profile_no_relaunch():
    blk = {"id": "b2", "profile": "默认"}
    assert gui_app.plan_transition("b1", blk, "默认") == {
        "action": "run", "relaunch_chrome": False, "profile": "默认"}


def test_plan_transition_run_other_profile_relaunches():
    blk = {"id": "b2", "profile": "小号2"}
    assert gui_app.plan_transition("b1", blk, "默认") == {
        "action": "run", "relaunch_chrome": True, "profile": "小号2"}


def test_plan_transition_run_from_idle_after_worker_crash():
    blk = {"id": "b1", "profile": "默认"}
    # worker 崩了 -> _running_block_id 清成 None, chrome 还在『默认』
    assert gui_app.plan_transition(None, blk, "默认") == {
        "action": "run", "relaunch_chrome": False, "profile": "默认"}


@pytest.mark.parametrize("args, expected", [
    (("300", "2"), [300, 2]),
    (("0", "2"), None),
    (("-5", "2"), None),
    (("", "2"), None),
    (("3.5", "2"), None),
    (("  5  ", "2"), [5, 2]),  # int() 会自己 strip 两边空白
])
def test_parse_positive_ints(args, expected):
    assert gui_app.parse_positive_ints(*args) == expected


def test_start_afk_already_running():
    assert gui_app.start_afk(exe_exists=True, running=True,
                             confirm_download=lambda: pytest.fail()) == "already"


def test_start_afk_missing_exe_declined():
    assert gui_app.start_afk(exe_exists=False, running=False,
                             confirm_download=lambda: False) == "declined"


def test_start_afk_missing_exe_download_ok(monkeypatch):
    monkeypatch.setattr(gui_app.afk_watch, "download_florr_auto_afk", lambda: True)
    assert gui_app.start_afk(exe_exists=False, running=False,
                             confirm_download=lambda: True) == "downloaded"


def test_start_afk_missing_exe_download_fails(monkeypatch):
    monkeypatch.setattr(gui_app.afk_watch, "download_florr_auto_afk", lambda: False)
    assert gui_app.start_afk(exe_exists=False, running=False,
                             confirm_download=lambda: True) == "download_failed"


def test_start_afk_exe_present_not_running():
    assert gui_app.start_afk(exe_exists=True, running=False,
                             confirm_download=lambda: pytest.fail()) == "started"


def test_resolve_point_and_area_both_given_unchanged():
    p, a = gui_app.resolve_point_and_area((20, 30), [(5, 5), (40, 40)])
    assert p == (20, 30)
    assert a == [(5, 5), (40, 40)]


def test_resolve_point_and_area_neither():
    assert gui_app.resolve_point_and_area(None, None) == (None, None)


def test_resolve_point_and_area_only_area_derives_center():
    p, a = gui_app.resolve_point_and_area(None, [(10, 20), (30, 60)])
    assert p == (20, 40)          # ((10+30)//2, (20+60)//2)
    assert a == [(10, 20), (30, 60)]


def test_resolve_point_and_area_only_point_derives_box():
    p, a = gui_app.resolve_point_and_area((100, 120), None)
    assert p == (100, 120)
    h = gui_app._DERIVED_AREA_HALF
    assert a == [(100 - h, 120 - h), (100 + h, 120 + h)]


def test_resolve_point_and_area_only_point_clamps_to_map_edges():
    p, a = gui_app.resolve_point_and_area((2, 297), None)
    assert a == [(0, 297 - gui_app._DERIVED_AREA_HALF), (2 + gui_app._DERIVED_AREA_HALF, 299)]


class TestPersistFlag:
    def test_persist_flag_writes_key_and_reloads(self, monkeypatch, tmp_path):
        import app_config
        p = tmp_path / "config.json"
        monkeypatch.setattr(app_config, "CONFIG_PATH", str(p))
        app_config.save_config(app_config.load_config())   # 落一份带默认键的

        # 不起 Tk —— 直接在一个裸对象上绑方法
        obj = type("X", (), {})()
        obj._cfg = app_config.load_config()
        # 真 CTkSwitch.get() 回 int 1/0, 不是 bool —— _persist_flag 里的 bool() 转换
        # 必须把它规整成 True/False, 否则 save_config 的 _coerce 会把非 bool 打回默认.
        gui_app.App._persist_flag(obj, "afk_enabled", 1)
        assert app_config.load_config()["afk_enabled"] is True
        assert obj._cfg["afk_enabled"] is True

        gui_app.App._persist_flag(obj, "afk_enabled", 0)
        assert app_config.load_config()["afk_enabled"] is False
        assert obj._cfg["afk_enabled"] is False


def test_app_does_not_shadow_tkinter_report_exception():
    # tkinter 的 CallWrapper 出错时无参调用 widget._report_exception(); App 以前
    # 自己定义了一个同名 3 参方法, App.after() 回调一抛异常就变成 TypeError.
    assert "_report_exception" not in gui_app.App.__dict__


def test_pump_log_tees_worker_output_to_the_log_file():
    import types
    shown, written = [], []

    class FakeLog:
        closed = False

        def write(self, line):
            written.append(line)

        def close(self):
            FakeLog.closed = True

    proc = types.SimpleNamespace(stdout=iter(["a\n", "b\n"]), wait=lambda: 0)
    app = types.SimpleNamespace(_closing=False, _tclock=telemetry_clock.TelemetryClock(),
                                after=lambda ms, fn, *a: shown.append((fn, a)),
                                _log_line="log_line", _on_worker_exit="exit")
    gui_app.App._pump_log(app, proc, FakeLog())
    assert written == ["a\n", "b\n"]
    assert [a for fn, a in shown if fn == "log_line"] == [("a\n",), ("b\n",)]
    assert FakeLog.closed


def test_pump_log_swallows_phase_marker_lines_and_feeds_the_clock():
    import types
    shown, written = [], []

    class FakeLog:
        def write(self, line):
            written.append(line)

        def close(self):
            pass

    t = [0.0]
    clock = telemetry_clock.TelemetryClock(clock=lambda: t[0])
    clock.worker_started()
    lines = ["a\n", "@@florr-phase:farm\n", "b\n", "@@florr-phase:nap\n"]
    proc = types.SimpleNamespace(stdout=iter(lines), wait=lambda: 0)
    app = types.SimpleNamespace(_closing=False, _tclock=clock,
                                after=lambda ms, fn, *a: shown.append((fn, a)),
                                _log_line="log_line", _on_worker_exit="exit")
    gui_app.App._pump_log(app, proc, FakeLog())
    assert written == ["a\n", "b\n"]                                   # 标记行不进 logs/
    assert [a for fn, a in shown if fn == "log_line"] == [("a\n",), ("b\n",)]   # 也不进面板
    t[0] = 10
    assert clock.take_window()["ph"]["farm"] == 10                     # 但阶段记上了


def test_pump_log_closes_the_file_when_the_gui_is_closing():
    import types

    class FakeLog:
        lines, closed = [], False

        def write(self, line):
            FakeLog.lines.append(line)

        def close(self):
            FakeLog.closed = True

    proc = types.SimpleNamespace(stdout=iter(["a\n", "b\n"]), wait=lambda: 0)
    app = types.SimpleNamespace(_closing=True, _tclock=telemetry_clock.TelemetryClock(),
                                after=lambda *a: None)
    gui_app.App._pump_log(app, proc, FakeLog())
    assert FakeLog.lines == ["a\n"] and FakeLog.closed


# ── AFK "请稍候"小窗秒关: TclError: bad window path name ".!ctktoplevel2"(用户 2026-09-30) ──

class _FakeModal:
    def __init__(self, fail_withdraw=False):
        self.calls = []
        self.fail_withdraw = fail_withdraw
        self.busy_bar = type("Bar", (), {"stop": lambda _s: self.calls.append("bar.stop")})()

    def withdraw(self):
        self.calls.append("withdraw")
        if self.fail_withdraw:
            raise RuntimeError("窗口已经没了")

    def destroy(self):
        self.calls.append("destroy")


def test_busy_modal_hides_now_and_destroys_after_ctk_callbacks_ran():
    # CTkToplevel 在 Windows 上 1000ms 内还有延时回调要碰这个窗口, 立刻 destroy 它们就报错
    modal, scheduled = _FakeModal(), []
    gui_app.close_busy_modal(modal, lambda ms, fn: scheduled.append((ms, fn)))
    assert modal.calls == ["bar.stop", "withdraw"]
    assert len(scheduled) == 1 and scheduled[0][0] > 1000
    scheduled[0][1]()
    assert modal.calls[-1] == "destroy"


def test_busy_modal_close_never_raises():
    modal, scheduled = _FakeModal(fail_withdraw=True), []
    modal.destroy = lambda: (_ for _ in ()).throw(RuntimeError("早就关了"))
    gui_app.close_busy_modal(modal, lambda ms, fn: scheduled.append(fn))
    scheduled[0]()                      # 销毁时窗口已经没了也不往外抛


def test_finish_ensure_afk_does_not_destroy_the_modal_on_the_spot():
    modal, scheduled, logged = _FakeModal(), [], []
    fake = type("App", (), {})()
    fake._afk_busy = True
    fake.afk_switch = type("Sw", (), {"configure": lambda _s, **k: None})()
    fake.after = lambda ms, fn: scheduled.append((ms, fn))
    fake._log_line = logged.append
    gui_app.App._finish_ensure_afk(fake, modal, "started")
    assert "destroy" not in modal.calls and scheduled
    assert logged == ["AFK: started\n"]


# ── bug 上报接线: 检测到问题 -> 弹窗让用户选 ─────────────────────────────────

import types as _types

import app_config
import bug_report

_TB = (
    "Traceback (most recent call last):\n"
    '  File "main.py", line 10, in run_worker\n'
    "    boom()\n"
    "ValueError: 坏了"
)


def _bug_app(**over):
    scheduled, logged = [], []
    app = _types.SimpleNamespace(
        _cfg={"profiles": [{"alias": "默认", "dir": "d"}, {"alias": "小号A", "dir": "e"}],
              "schedule": [{"id": "blk-1", "map": "garden", "profile": "小号A", "enemy_ai_enabled": True}]},
        _running_block_id="blk-1", _closing=False, _bug_dialog=None,
        after=lambda ms, fn, *a: scheduled.append((ms, fn, a)), _log_line=logged.append,
        _submit_bug="submit_bug", _on_bug_dialog_closed="on_closed")
    app.scheduled, app.logged = scheduled, logged
    for k, v in over.items():
        setattr(app, k, v)
    return app


class _FakeDialog:
    made = []

    def __init__(self, master, payload, *, on_submit, on_close, aliases=()):
        self.master, self.payload, self.on_submit, self.on_close = master, payload, on_submit, on_close
        self.aliases = aliases
        _FakeDialog.made.append(self)


@pytest.fixture
def fake_dialog(monkeypatch):
    _FakeDialog.made = []
    monkeypatch.setattr(gui_app.gui_bug_report, "BugReportDialog", _FakeDialog)
    return _FakeDialog


def _prepare_returns(monkeypatch, payload):
    seen = []

    def fake(kind, **kw):
        seen.append((kind, kw))
        return payload

    monkeypatch.setattr(gui_app.bug_report, "prepare", fake)
    return seen


def test_a_detected_problem_opens_the_dialog_with_the_prepared_payload(monkeypatch, fake_dialog):
    payload = {"sig": "abc", "kind": "worker_traceback"}
    seen = _prepare_returns(monkeypatch, payload)
    app = _bug_app()
    gui_app.App._offer_bug_report(app, "worker_traceback", _TB, ["l1", "l2"])
    (kind, kw), = seen
    assert kind == "worker_traceback" and kw["tb"] == _TB and kw["log_lines"] == ["l1", "l2"]
    assert kw["ctx"] == app._cfg["schedule"][0]                       # 在跑的时块(bug_report 自己按白名单筛)
    assert kw["aliases"] == ["默认", "小号A"] and kw["exit_code"] is None
    (dlg,) = fake_dialog.made
    assert dlg.master is app and dlg.payload is payload
    assert dlg.on_submit == "submit_bug" and dlg.on_close == "on_closed"
    assert dlg.aliases == ["默认", "小号A"]                            # 弹窗里实时预览要按别名脱敏
    assert app._bug_dialog is dlg
    assert any("不点就不发" in ln for ln in app.logged)               # 日志面板里也提一句


def test_nothing_is_uploaded_or_even_prepared_for_upload_by_just_detecting_a_problem(monkeypatch, fake_dialog):
    _prepare_returns(monkeypatch, {"sig": "abc"})
    monkeypatch.setattr(gui_app.bug_report, "submit",
                        lambda *a, **k: pytest.fail("用户没点上报, 不该上传"))
    gui_app.App._offer_bug_report(_bug_app(), "worker_traceback", _TB)


def test_no_dialog_when_prepare_says_not_now(monkeypatch, fake_dialog):
    _prepare_returns(monkeypatch, None)                               # 关着 / 刚问过 / 今天问够了
    app = _bug_app()
    gui_app.App._offer_bug_report(app, "worker_traceback", _TB)
    assert fake_dialog.made == [] and app._bug_dialog is None and app.logged == []


def test_only_one_dialog_at_a_time_and_a_second_problem_is_not_even_recorded(monkeypatch, fake_dialog):
    seen = _prepare_returns(monkeypatch, {"sig": "abc"})
    app = _bug_app(_bug_dialog=object())
    gui_app.App._offer_bug_report(app, "worker_traceback", _TB)
    assert seen == [] and fake_dialog.made == []                      # 没调 prepare = 没消耗冷却, 以后再遇到还会问


def test_no_dialog_while_the_app_is_closing(monkeypatch, fake_dialog):
    seen = _prepare_returns(monkeypatch, {"sig": "abc"})
    gui_app.App._offer_bug_report(_bug_app(_closing=True), "worker_traceback", _TB)
    assert seen == [] and fake_dialog.made == []


def test_without_a_running_block_there_is_no_ctx_and_the_exit_code_is_passed_on(monkeypatch, fake_dialog):
    seen = _prepare_returns(monkeypatch, {"sig": "abc"})
    gui_app.App._offer_bug_report(_bug_app(_running_block_id=None), "worker_exit", "", ["x"], 3221225477)
    kw = seen[0][1]
    assert kw["ctx"] is None and kw["exit_code"] == 3221225477


def test_offering_a_report_never_raises_into_a_tk_callback(monkeypatch):
    monkeypatch.setattr(gui_app.bug_report, "prepare",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸了")))
    gui_app.App._offer_bug_report(_bug_app(), "worker_traceback", _TB)
    monkeypatch.setattr(gui_app.bug_report, "prepare", lambda *a, **k: {"sig": "x"})

    def broken(*a, **k):
        raise RuntimeError("弹窗建不出来")

    monkeypatch.setattr(gui_app.gui_bug_report, "BugReportDialog", broken)
    app = _bug_app()
    gui_app.App._offer_bug_report(app, "worker_traceback", _TB)
    assert app._bug_dialog is None                                    # 没建成就别留着占位, 否则以后永远弹不出来
    gui_app.App._offer_bug_report(_types.SimpleNamespace(), "worker_traceback", _TB)   # 连属性都缺也不抛


def test_the_dialog_closing_frees_the_slot():
    app = _bug_app(_bug_dialog=object())
    gui_app.App._on_bug_dialog_closed(app)
    assert app._bug_dialog is None


def test_submit_hands_over_the_note_aliases_and_root_and_reports_back_on_the_tk_thread(monkeypatch):
    seen = []
    monkeypatch.setattr(gui_app.bug_report, "submit", lambda payload, **kw: seen.append((payload, kw)))
    app = _bug_app()
    results = []
    payload = {"sig": "abc"}
    gui_app.App._submit_bug(app, payload, "我的说明", lambda ok, msg: results.append((ok, msg)))
    (p, kw), = seen
    assert p is payload and kw["note"] == "我的说明" and kw["aliases"] == ["默认", "小号A"]
    assert kw["root"] == os.path.dirname(app_config.CONFIG_PATH)
    kw["done"](True, "已上报, 谢谢")                                   # 上传线程里调: 只排队
    assert results == [] and len(app.scheduled) == 1 and app.scheduled[0][0] == 0
    app.scheduled[0][1]()                                             # 界面线程执行: 写日志 + 通知弹窗
    assert app.logged == ["📮 已上报, 谢谢\n"] and results == [(True, "已上报, 谢谢")]
    kw["done"](False, "上传失败")
    app.scheduled[1][1]()
    assert app.logged[-1] == "📮 ⚠ 上传失败\n" and results[-1] == (False, "上传失败")


def test_submit_that_cannot_even_start_tells_the_dialog_instead_of_hanging_it(monkeypatch):
    monkeypatch.setattr(gui_app.bug_report, "submit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("线程起不来")))
    app = _bug_app()
    results = []
    gui_app.App._submit_bug(app, {"sig": "abc"}, "", lambda ok, msg: results.append((ok, msg)))
    app.scheduled[0][1]()
    assert results and results[0][0] is False                         # 弹窗里「上报中…」不会一直转


def test_a_result_arriving_after_the_window_is_gone_does_not_raise(monkeypatch):
    seen = []
    monkeypatch.setattr(gui_app.bug_report, "submit", lambda payload, **kw: seen.append(kw))
    app = _bug_app(after=lambda *a: (_ for _ in ()).throw(RuntimeError("main thread is not in main loop")))
    gui_app.App._submit_bug(app, {"sig": "abc"}, "", lambda ok, msg: None)
    seen[0]["done"](True, "x")


# ── 主动反馈: 侧栏「反馈问题」, 不用等出错 ───────────────────────────────────

class _LogBox:
    def __init__(self, text):
        self.text = text

    def get(self, a, b):
        assert (a, b) == ("1.0", "end-1c")
        return self.text


def _feedback_app(**over):
    app = _bug_app(log_box=_LogBox("\n".join(f"行{i}" for i in range(200)) + "\n"))
    app._log_tail = lambda: gui_app.App._log_tail(app)
    for k, v in over.items():
        setattr(app, k, v)
    return app


@pytest.fixture
def manual_payload(monkeypatch):
    monkeypatch.delenv("FLORR_TELEMETRY", raising=False)
    seen = []

    def fake(**kw):
        seen.append(kw)
        return {"kind": "user_report", "sig": "abc"}

    monkeypatch.setattr(gui_app.bug_report, "prepare_manual", fake)
    return seen


def test_the_log_tail_is_what_the_user_sees_in_the_log_panel_capped_at_the_report_size():
    app = _feedback_app()
    tail = gui_app.App._log_tail(app)
    assert len(tail) == bug_report.TAIL_LINES and tail[-1] == "行199" and tail[0] == "行140"
    assert gui_app.App._log_tail(_types.SimpleNamespace()) == []      # 日志框没建好也不抛


def test_the_feedback_button_opens_a_manual_dialog_without_any_error(monkeypatch, fake_dialog, manual_payload):
    monkeypatch.setattr(gui_app.bug_report, "submit", lambda *a, **k: pytest.fail("没点上报, 不该上传"))
    app = _feedback_app()
    gui_app.App._on_feedback(app)
    (kw,) = manual_payload
    assert kw["log_lines"][-1] == "行199" and kw["aliases"] == ["默认", "小号A"]
    assert kw["ctx"] == app._cfg["schedule"][0]                       # 在跑的时块
    (dlg,) = fake_dialog.made
    assert dlg.payload["kind"] == "user_report" and dlg.master is app
    assert dlg.on_submit == "submit_bug" and dlg.on_close == "on_closed" and dlg.aliases == ["默认", "小号A"]
    assert app._bug_dialog is dlg


def test_feedback_works_when_nothing_is_running(fake_dialog, manual_payload):
    app = _feedback_app(_running_block_id=None, log_box=_LogBox(""))
    gui_app.App._on_feedback(app)
    assert manual_payload[0]["ctx"] is None and manual_payload[0]["log_lines"] == []
    assert len(fake_dialog.made) == 1


def test_feedback_while_a_window_is_already_open_brings_it_forward_instead_of_opening_another(
        fake_dialog, manual_payload):
    seen = []
    existing = _types.SimpleNamespace(lift=lambda: seen.append("lift"), focus_force=lambda: seen.append("focus"))
    app = _feedback_app(_bug_dialog=existing)
    gui_app.App._on_feedback(app)
    assert seen == ["lift", "focus"] and fake_dialog.made == [] and manual_payload == []
    assert app._bug_dialog is existing


def test_feedback_with_telemetry_off_explains_instead_of_opening(monkeypatch, fake_dialog, manual_payload):
    monkeypatch.setenv("FLORR_TELEMETRY", "0")
    shown = []
    monkeypatch.setattr(gui_app.messagebox, "showinfo", lambda *a, **k: shown.append((a, k)))
    monkeypatch.setattr(gui_app.bug_report, "prepare_manual", lambda **kw: pytest.fail("关着就不该准备载荷"))
    app = _feedback_app()
    gui_app.App._on_feedback(app)
    (args, kw), = shown
    assert "FLORR_TELEMETRY=0" in args[1] and kw["parent"] is app
    assert fake_dialog.made == [] and app._bug_dialog is None


def test_feedback_never_raises_into_a_tk_callback_and_does_not_leave_a_placeholder(monkeypatch, fake_dialog):
    monkeypatch.delenv("FLORR_TELEMETRY", raising=False)
    monkeypatch.setattr(gui_app.bug_report, "prepare_manual",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("炸了")))
    app = _feedback_app()
    gui_app.App._on_feedback(app)
    assert app._bug_dialog is None and fake_dialog.made == []

    monkeypatch.setattr(gui_app.bug_report, "prepare_manual", lambda **kw: {"kind": "user_report"})

    def broken(*a, **k):
        raise RuntimeError("弹窗建不出来")

    monkeypatch.setattr(gui_app.gui_bug_report, "BugReportDialog", broken)
    gui_app.App._on_feedback(app)
    assert app._bug_dialog is None                                    # 没建成就别占着位, 不然以后永远打不开
    assert any("没能打开反馈窗口" in ln for ln in app.logged)           # 点了没反应最摸不着头脑, 日志里留一行
    gui_app.App._on_feedback(_types.SimpleNamespace())                # 连属性都缺也不抛


def test_feedback_that_could_not_prepare_a_payload_says_so_in_the_log(monkeypatch, fake_dialog):
    monkeypatch.delenv("FLORR_TELEMETRY", raising=False)
    monkeypatch.setattr(gui_app.bug_report, "prepare_manual", lambda **kw: None)
    app = _feedback_app()
    gui_app.App._on_feedback(app)
    assert fake_dialog.made == [] and any("反馈" in ln for ln in app.logged)


def _find_button(widget, text):
    for child in widget.winfo_children():
        try:
            if child.cget("text") == text and hasattr(child, "invoke"):
                return child
        except Exception:
            pass
        found = _find_button(child, text)
        if found is not None:
            return found
    return None


def test_the_sidebar_feedback_button_opens_the_manual_dialog_in_a_real_app(tmp_path, monkeypatch):
    # 真造一个 App(需要显示, 跟别的真 Tk 测试一样): 侧栏按钮必须真的接到 _on_feedback 上
    monkeypatch.delenv("FLORR_TELEMETRY", raising=False)
    monkeypatch.setattr(app_config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(gui_app.telemetry, "send", lambda *a, **k: None)
    monkeypatch.setattr(gui_app.telemetry, "install_id", lambda: "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(gui_app.gui_update.UpdateController, "check", lambda self, *a, **k: None)
    monkeypatch.setattr(gui_app.bug_report, "submit",
                        lambda *a, **k: pytest.fail("没点上报, 不该上传"))
    app = gui_app.App()
    try:
        app._log_line("一行日志\n")
        _find_button(app, "反馈问题").invoke()
        dlg = app._bug_dialog
        assert dlg is not None and dlg._payload["kind"] == "user_report"
        assert "一行日志" in dlg._payload["log"]
        _find_button(app, "反馈问题").invoke()               # 再点: 还是同一个窗口
        assert app._bug_dialog is dlg
        dlg.destroy()
        assert app._bug_dialog is None
    finally:
        app.destroy()


def _exit_app(**over):
    calls = []
    app = _types.SimpleNamespace(
        _cfg={"schedule": [{"id": "blk-1"}]}, _running_block_id="blk-1", _sched_running=False,
        _log_line=lambda t: calls.append(("log", t)),
        _tclock=_types.SimpleNamespace(worker_stopped=lambda: None),
        _send_heartbeat=lambda blk: None, _set_running_block=lambda b: None,
        _set_status=lambda *a: None, _set_start_btn=lambda *a: None,
        _offer_bug_report=lambda *a, **k: calls.append(("bug", a, k)),
        _bug_watcher=bug_report.StreamWatcher(lambda tb, tail: None))
    app.proc = object()
    app.calls = calls
    for k, v in over.items():
        setattr(app, k, v)
    return app


@pytest.mark.parametrize("code", [3221225477, -11, 2, -9])
def test_a_crash_exit_code_offers_a_report_with_the_log_tail(code):
    app = _exit_app()
    proc = app.proc
    for ln in ["a\n", "b\n"]:
        app._bug_watcher.feed(ln)
    gui_app.App._on_worker_exit(app, proc, code)
    bugs = [c for c in app.calls if c[0] == "bug"]
    assert len(bugs) == 1
    assert bugs[0][1][0] == "worker_exit"
    assert bugs[0][2] == {"tail": ["a", "b"], "exit_code": code}


@pytest.mark.parametrize("code", [0, 1, -15])
def test_a_normal_or_deliberate_exit_offers_nothing(code):
    app = _exit_app()
    gui_app.App._on_worker_exit(app, app.proc, code)
    assert [c for c in app.calls if c[0] == "bug"] == []


def test_an_exit_after_an_offered_traceback_is_not_offered_twice():
    app = _exit_app()
    for ln in (_TB + "\nnext\n").splitlines(keepends=True):
        app._bug_watcher.feed(ln)
    assert app._bug_watcher.fired == 1
    gui_app.App._on_worker_exit(app, app.proc, 2)
    assert [c for c in app.calls if c[0] == "bug"] == []


def test_a_stale_worker_exit_is_ignored_entirely():
    app = _exit_app()
    gui_app.App._on_worker_exit(app, object(), 3221225477)           # 不是当前 proc(已被替换 / 手动停掉)
    assert app.calls == []


def test_exit_without_a_watcher_does_not_crash():
    app = _exit_app()
    del app._bug_watcher
    gui_app.App._on_worker_exit(app, app.proc, 3221225477)           # 老路径 / 没起过 worker 的假对象
    assert [c for c in app.calls if c[0] == "bug"][0][2]["tail"] == []


def test_callback_exception_offers_a_gui_exception_report_with_the_traceback_text():
    seen, logged = [], []
    app = _types.SimpleNamespace(_log_line=logged.append,
                                 _offer_bug_report=lambda *a, **k: seen.append((a, k)))
    try:
        raise KeyError("boom")
    except KeyError:
        import sys as _sys
        gui_app.App._on_callback_exception(app, *_sys.exc_info())
    (a, k), = seen
    assert a[0] == "gui_exception" and "KeyError: 'boom'" in a[1] and "Traceback" in a[1]
    assert logged and "KeyError" in logged[0]                        # 原有的日志行为不变


def test_pump_log_feeds_the_watcher_but_not_the_phase_markers_and_flushes_at_the_end():
    shown, got = [], []
    watcher = bug_report.StreamWatcher(lambda tb, tail: got.append((tb, tail)))
    lines = ["a\n", "@@florr-phase:farm\n"] + (_TB + "\n").splitlines(keepends=True)   # 流在 traceback 里结束
    proc = _types.SimpleNamespace(stdout=iter(lines), wait=lambda: 1)
    app = _types.SimpleNamespace(_closing=False, _tclock=telemetry_clock.TelemetryClock(),
                                 after=lambda ms, fn, *a: shown.append((fn, a)),
                                 _log_line="log_line", _on_worker_exit="exit")
    gui_app.App._pump_log(app, proc, None, watcher)
    assert len(got) == 1 and got[0][0] == _TB
    assert "@@florr-phase:farm" not in got[0][1] and got[0][1][0] == "a"
    assert [a for fn, a in shown if fn == "log_line"][0] == ("a\n",)             # 面板行为不变
    assert shown[-1][0] == "exit"                                                  # flush 在 wait 之前, 退出回调照常


def test_pump_log_still_works_without_a_watcher():
    shown = []
    proc = _types.SimpleNamespace(stdout=iter(["a\n"]), wait=lambda: 0)
    app = _types.SimpleNamespace(_closing=False, _tclock=telemetry_clock.TelemetryClock(),
                                 after=lambda ms, fn, *a: shown.append((fn, a)),
                                 _log_line="log_line", _on_worker_exit="exit")
    gui_app.App._pump_log(app, proc)
    assert shown[0] == ("log_line", ("a\n",))


def test_spawn_worker_starts_the_pump_with_a_watcher_that_queues_the_offer_for_the_tk_thread(monkeypatch):
    started, scheduled = [], []

    class FakeThread:
        def __init__(self, target, args, daemon):
            started.append((target, args))

        def start(self):
            pass

    proc = _types.SimpleNamespace(stdout=iter([]), stdin=None)
    monkeypatch.setattr(gui_app.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(gui_app.threading, "Thread", FakeThread)
    monkeypatch.setattr(gui_app.worker_log, "WorkerLog", lambda root: "wlog")
    app = _types.SimpleNamespace(_tclock=_types.SimpleNamespace(worker_started=lambda: None),
                                 _log_line=lambda t: None, _closing=False,
                                 after=lambda ms, fn, *a: scheduled.append((ms, fn, a)),
                                 _offer_bug_report=lambda *a: None, _pump_log="pump")
    app._on_worker_traceback = lambda tb, tail: gui_app.App._on_worker_traceback(app, tb, tail)
    gui_app.App._spawn_worker(app)
    (target, args), = started
    assert target == "pump" and args[0] is proc and args[1] == "wlog"
    watcher = args[2]
    assert isinstance(watcher, bug_report.StreamWatcher) and app._bug_watcher is watcher
    for ln in (_TB + "\nnext\n").splitlines(keepends=True):
        watcher.feed(ln)
    (ms, fn, a), = scheduled                                         # 泵线程里只排队, 弹窗在界面线程里建
    assert ms == 0 and fn is app._offer_bug_report and a[0] == "worker_traceback" and a[1] == _TB


def test_worker_traceback_during_shutdown_is_dropped():
    scheduled = []
    app = _types.SimpleNamespace(_closing=True, after=lambda *a: scheduled.append(a),
                                 _offer_bug_report=None)
    gui_app.App._on_worker_traceback(app, _TB, ["x"])
    assert scheduled == []
