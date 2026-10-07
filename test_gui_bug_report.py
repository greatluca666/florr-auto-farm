"""bug 上报弹窗(gui_bug_report.BugReportDialog): 真造 Tk(跟别的 GUI 测试一样需要显示)。"""
import pytest

import bug_report
import gui_bug_report
import gui_schedule as gs          # 只借它的 ctk

TB = ('Traceback (most recent call last):\n  File "main.py", line 1, in run_worker\n    boom()\n'
      "ValueError: 坏了")


@pytest.fixture
def root():
    r = gs.ctk.CTk()
    r.withdraw()
    yield r
    r.destroy()


def _payload():
    return bug_report.build_report("worker_traceback", tb=TB, log_lines=["l1", "l2"], ctx={"map": "garden"})


def _dialog(root, **kw):
    calls = {"submit": [], "close": 0}
    kw.setdefault("on_submit", lambda payload, note, done: calls["submit"].append((payload, note, done)))
    kw.setdefault("on_close", lambda: calls.__setitem__("close", calls["close"] + 1))
    dlg = gui_bug_report.BugReportDialog(root, _payload(), **kw)
    return dlg, calls


def test_the_dialog_shows_exactly_the_preview_and_it_is_read_only(root):
    dlg, _ = _dialog(root)
    shown = dlg._preview.get("1.0", "end").rstrip("\n")
    assert shown == bug_report.render_preview(dlg._payload)
    assert "ValueError: 坏了" in shown and "l1" in shown
    assert dlg._preview.cget("state") == "disabled"                  # 用户只能看, 不能改要发的内容
    assert dlg._note_text() == ""
    assert dlg._yes_btn.cget("state") == "normal" and dlg._no_btn.cget("state") == "normal"
    dlg.destroy()


def test_it_is_non_modal_and_not_topmost(root):
    dlg, _ = _dialog(root)
    root.update()
    assert root.grab_current() is None and dlg.grab_current() is None
    assert not dlg.attributes("-topmost")
    dlg.destroy()


def test_nothing_is_submitted_until_the_user_clicks_report(root):
    dlg, calls = _dialog(root)
    root.update()
    assert calls["submit"] == []
    dlg.destroy()
    assert calls["submit"] == []


def test_clicking_report_hands_over_the_payload_and_note_and_locks_the_buttons(root):
    dlg, calls = _dialog(root)
    dlg._note.insert("1.0", "刷到一半卡死了")
    dlg._submit()
    (payload, note, done), = calls["submit"]
    assert payload is dlg._payload and note == "刷到一半卡死了"
    assert dlg._yes_btn.cget("state") == "disabled" and dlg._no_btn.cget("state") == "disabled"
    assert "上报中" in dlg._status.cget("text")
    dlg._submit()                                                    # 上报中再点: 不会重复提交
    assert len(calls["submit"]) == 1
    dlg.destroy()


def test_a_successful_upload_shows_the_message_and_only_offers_to_close(root):
    dlg, calls = _dialog(root)
    dlg._submit()
    calls["submit"][0][2](True, "已上报, 谢谢(问题签名 abc)")
    assert "已上报, 谢谢" in dlg._status.cget("text")
    assert dlg._yes_btn.cget("state") == "disabled"                  # 已经报过了, 不能再报一份
    assert dlg._no_btn.cget("state") == "normal" and dlg._no_btn.cget("text") == "关闭"
    dlg._submit()
    assert len(calls["submit"]) == 1
    dlg._dismiss()
    assert calls["close"] == 1


def test_a_failed_upload_keeps_the_window_and_lets_the_user_try_again(root):
    dlg, calls = _dialog(root)
    dlg._submit()
    calls["submit"][0][2](False, "上传失败(OSError), 可以稍后再点一次「上报」")
    assert "上传失败" in dlg._status.cget("text")
    assert dlg._yes_btn.cget("state") == "normal" and dlg._no_btn.cget("state") == "normal"
    assert dlg._no_btn.cget("text") == "不上报"
    dlg._submit()
    assert len(calls["submit"]) == 2                                 # 可以再点一次
    calls["submit"][1][2](True, "已上报")
    assert dlg._yes_btn.cget("state") == "disabled"
    dlg.destroy()


def test_declining_closes_without_submitting_and_notifies_once(root):
    dlg, calls = _dialog(root)
    dlg._no_btn.invoke()
    assert calls["submit"] == [] and calls["close"] == 1
    assert not dlg.winfo_exists()


def test_escape_means_no(root):
    dlg, calls = _dialog(root)
    dlg.deiconify()
    dlg.focus_force()
    root.update()
    dlg.event_generate("<Escape>")
    root.update()
    assert calls["submit"] == [] and calls["close"] == 1


def test_the_window_close_button_means_no(root):
    dlg, calls = _dialog(root)
    # protocol(name) 返回注册的 Tcl 回调名; 调它就等于用户点了窗口右上角的 ×
    root.tk.call(dlg.protocol("WM_DELETE_WINDOW"))
    assert calls["submit"] == [] and calls["close"] == 1
    assert not dlg.winfo_exists()


def test_it_cannot_be_dismissed_while_an_upload_is_in_flight(root):
    dlg, calls = _dialog(root)
    dlg._submit()
    dlg._dismiss()
    assert dlg.winfo_exists() and calls["close"] == 0                # 结果还要回来告诉用户
    calls["submit"][0][2](False, "上传失败")
    dlg._dismiss()
    assert calls["close"] == 1


def test_the_result_arriving_after_the_window_is_gone_is_harmless(root):
    dlg, calls = _dialog(root)
    dlg._submit()
    done = calls["submit"][0][2]
    dlg.destroy()
    done(True, "已上报")                                              # 不抛


def test_on_close_fires_once_even_if_destroy_is_called_twice_and_a_raising_callback_is_swallowed(root):
    dlg, calls = _dialog(root)
    dlg.destroy()
    dlg.destroy()                                                    # 第二次: 什么都不再发生
    assert calls["close"] == 1 and not dlg.winfo_exists()
    dlg2, _ = _dialog(root, on_close=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    dlg2.destroy()                                                   # 回调抛了也不影响销毁
    assert not dlg2.winfo_exists()


def test_the_wording_promises_nothing_is_sent_without_a_click(root):
    dlg, _ = _dialog(root)
    assert "不点什么都不会发" in gui_bug_report.BugReportDialog._EXPLAIN
    assert "logs/bug-reports" in gui_bug_report.BugReportDialog._SAVED_HINT
    dlg.destroy()


# ── 主动反馈: 侧栏「反馈问题」, 不用等出错 ───────────────────────────────────

import re


def _manual(root, **kw):
    calls = {"submit": [], "close": 0}
    kw.setdefault("on_submit", lambda payload, note, done: calls["submit"].append((payload, note, done)))
    kw.setdefault("on_close", lambda: calls.__setitem__("close", calls["close"] + 1))
    payload = bug_report.prepare_manual(log_lines=["l1", "l2"], ctx={"map": "garden"})
    dlg = gui_bug_report.BugReportDialog(root, payload, **kw)
    return dlg, calls


def _type(root, dlg, text):
    dlg._note.delete("1.0", "end")
    dlg._note.insert("1.0", text)
    root.update()                                                    # <<Modified>> 在空闲时才派发


def _shown(dlg):
    return dlg._preview.get("1.0", "end").rstrip("\n")


def test_the_manual_window_says_you_may_report_any_time_and_that_nothing_is_sent_without_a_click(root):
    dlg, _ = _manual(root)
    assert dlg.title() == "反馈问题"
    assert "不用等程序报错" in gui_bug_report.BugReportDialog._EXPLAIN_MANUAL
    assert "不点什么都不会发" in gui_bug_report.BugReportDialog._EXPLAIN_MANUAL
    assert "必填" in gui_bug_report.BugReportDialog._NOTE_HINT_MANUAL
    assert dlg._no_btn.cget("text") == "取消" and dlg._yes_btn.cget("text") == "上报"
    dlg.destroy()
    crash, _ = _dialog(root)
    assert crash.title() != "反馈问题" and crash._no_btn.cget("text") == "不上报"
    crash.destroy()


def test_a_manual_report_cannot_be_sent_until_there_is_a_description(root):
    dlg, calls = _manual(root)
    root.update()
    assert dlg._yes_btn.cget("state") == "disabled"
    dlg._submit()                                                    # 按钮灰着, 直接调也不会发
    assert calls["submit"] == []
    _type(root, dlg, "  \n  ")
    assert dlg._yes_btn.cget("state") == "disabled"
    _type(root, dlg, "走到一半不动了")
    assert dlg._yes_btn.cget("state") == "normal"
    _type(root, dlg, "")
    assert dlg._yes_btn.cget("state") == "disabled"
    _type(root, dlg, "又写了点")
    dlg._submit()
    (payload, note, _done), = calls["submit"]
    assert note == "又写了点" and payload["kind"] == "user_report"
    dlg.destroy()


def test_a_crash_report_can_still_be_sent_without_a_note(root):
    dlg, calls = _dialog(root)
    root.update()
    assert dlg._yes_btn.cget("state") == "normal"
    dlg._submit()
    assert len(calls["submit"]) == 1 and calls["submit"][0][1] == ""
    dlg.destroy()


def test_the_preview_follows_what_you_type_and_shows_it_already_redacted(root):
    dlg, _ = _manual(root)
    before = _shown(dlg)
    assert "【标题" in before and "你的补充说明" not in before
    _type(root, dlg, "走到一半不动了\n联系我 a@b.cc")
    shown = _shown(dlg)
    assert "你的补充说明" in shown and "联系我 <email>" in shown and "a@b.cc" not in shown
    assert re.search(r"【标题[^】]*】 走到一半不动了", shown)
    assert shown == bug_report.render_preview(bug_report.with_note(dlg._payload, "走到一半不动了\n联系我 a@b.cc"))
    _type(root, dlg, "")
    assert _shown(dlg) == before                                     # 清空说明, 预览回到原样
    dlg.destroy()


def test_what_the_preview_shows_is_exactly_what_would_be_submitted(root):
    dlg, calls = _manual(root, aliases=["小号A"])
    _type(root, dlg, "在 小号A 上出的, 联系 a@b.cc")
    shown = _shown(dlg)
    assert "<profile>" in shown and "小号A" not in shown
    dlg._submit()
    (payload, note, _), = calls["submit"]
    assert shown == bug_report.render_preview(bug_report.with_note(payload, note, ["小号A"]))
    dlg.destroy()


def test_the_counter_counts_what_will_actually_be_sent_after_redaction(root):
    dlg, _ = _manual(root, aliases=["小号A"])
    _type(root, dlg, "小号A")                                        # 发出去的是 "<profile>"(9 个字符)
    assert f"9/{bug_report.MAX_NOTE}" in dlg._count.cget("text")
    dlg.destroy()


def test_typing_in_a_crash_window_also_updates_its_preview(root):
    dlg, _ = _dialog(root)
    _type(root, dlg, "刷到一半卡死了")
    assert "你的补充说明" in _shown(dlg) and "刷到一半卡死了" in _shown(dlg)
    dlg.destroy()


def test_the_preview_keeps_its_scroll_position_while_you_type(root):
    dlg, _ = _manual(root)
    dlg._payload["log"] = "\n".join(f"line {i}" for i in range(400))
    dlg._refresh_preview()
    root.update()
    dlg._preview.yview_moveto(0.6)
    root.update()
    at = dlg._preview.yview()[0]
    assert at > 0.3                                                  # 先确认真滚下去了(窗口已布局)
    _type(root, dlg, "x")
    assert abs(dlg._preview.yview()[0] - at) < 0.05
    dlg.destroy()


def test_the_counter_warns_when_the_description_is_longer_than_what_will_be_sent(root):
    dlg, _ = _manual(root)
    _type(root, dlg, "字" * 10)
    assert f"10/{bug_report.MAX_NOTE}" in dlg._count.cget("text")
    _type(root, dlg, "字" * (bug_report.MAX_NOTE + 50))
    text = dlg._count.cget("text")
    assert f"{bug_report.MAX_NOTE + 50}/{bug_report.MAX_NOTE}" in text and "超出" in text
    assert len(bug_report.with_note(dlg._payload, "字" * (bug_report.MAX_NOTE + 50))["note"]) == bug_report.MAX_NOTE
    dlg.destroy()


def test_the_description_is_locked_while_uploading_and_after_success_but_not_after_failure(root):
    dlg, calls = _manual(root)
    _type(root, dlg, "卡住了")
    dlg._submit()
    assert str(dlg._note.cget("state")) == "disabled"                # 上传中: 发出去的和看到的不会再变
    calls["submit"][0][2](False, "上传失败(OSError), 可以稍后再点一次「上报」")
    assert str(dlg._note.cget("state")) == "normal" and dlg._yes_btn.cget("state") == "normal"
    _type(root, dlg, "")                                             # 失败后清空了说明: 不能再发
    assert dlg._yes_btn.cget("state") == "disabled"
    _type(root, dlg, "改了说法")
    dlg._submit()
    calls["submit"][1][2](True, "已上报, 谢谢")
    assert str(dlg._note.cget("state")) == "disabled" and dlg._yes_btn.cget("state") == "disabled"
    assert dlg._no_btn.cget("text") == "关闭"
    dlg.destroy()


def test_a_note_edited_event_arriving_after_the_window_is_gone_is_harmless(root):
    dlg, _ = _manual(root)
    dlg.destroy()
    dlg._refresh_preview()                                           # 不抛
    dlg._on_note_edited()


# ── 附件: 最近 5 分钟的录像 / 截图 / 日志, 一律一起发 ─────────────────────────

ATT = {"path": "/x/logs/bug-reports/attach-20261007-120000-000000.zip", "bytes": 4_800_000,
       "from": 1790000000.0, "to": 1790000300.0, "frames": 598, "shots": 60,
       "logs": ["worker-20261007-115500.log"], "window_s": 300}


class FakeJob:
    def __init__(self, result=None, done=False):
        self._result, self._done = result, done

    def done(self):
        return self._done

    def result(self):
        return self._result if self._done else None


def test_while_the_attachment_is_packing_it_cannot_be_sent_yet(root):
    job = FakeJob()
    dlg, calls = _dialog(root, attachment_job=job)
    assert "打包中" in _shown(dlg) and dlg._yes_btn.cget("state") == "disabled"
    dlg._submit()
    assert calls["submit"] == []
    job._result, job._done = ATT, True
    dlg._poll_attachment()
    shown = _shown(dlg)
    assert shown == bug_report.render_preview(dlg._payload, attachment=ATT)
    assert "598" in shown and "60 张" in shown and dlg._yes_btn.cget("state") == "normal"
    assert dlg._open_btn.winfo_manager() == "grid"                   # 能打开文件夹看看
    dlg.destroy()


def test_it_waits_for_the_packing_by_itself(root):
    job = FakeJob()
    dlg, _ = _dialog(root, attachment_job=job)
    job._result, job._done = ATT, True
    for _ in range(50):
        root.update()
        if "598" in _shown(dlg):
            break
        root.after(20)
    assert "598" in _shown(dlg)
    dlg.destroy()


def test_nothing_to_attach_still_lets_you_send_the_text(root):
    dlg, _ = _dialog(root, attachment_job=FakeJob(None, done=True))
    assert _shown(dlg) == bug_report.render_preview(dlg._payload)
    assert dlg._yes_btn.cget("state") == "normal" and dlg._open_btn.winfo_manager() == ""
    dlg.destroy()


def test_a_manual_report_with_an_attachment_still_needs_a_description(root):
    calls = {"submit": [], "close": 0}
    payload = bug_report.prepare_manual(log_lines=["l1"])
    dlg = gui_bug_report.BugReportDialog(root, payload, attachment_job=FakeJob(ATT, done=True),
                                         on_submit=lambda p, n, d: calls["submit"].append(n))
    assert dlg._yes_btn.cget("state") == "disabled"
    _type(root, dlg, "卡住了")
    assert dlg._yes_btn.cget("state") == "normal" and "【附件(会一起发送)】" in _shown(dlg)
    dlg.destroy()


def test_open_folder_opens_where_the_zip_is(root, monkeypatch):
    opened = []
    monkeypatch.setattr(gui_bug_report, "_open_folder", opened.append)
    dlg, _ = _dialog(root, attachment_job=FakeJob(ATT, done=True))
    dlg._open_btn.invoke()
    assert opened == ["/x/logs/bug-reports"]
    dlg.destroy()


def test_the_wording_says_screenshots_and_recordings_go_along():
    for text in (gui_bug_report.BugReportDialog._EXPLAIN, gui_bug_report.BugReportDialog._EXPLAIN_MANUAL):
        assert "截图" in text and "录像" in text and "日志" in text and "不点什么都不会发" in text
