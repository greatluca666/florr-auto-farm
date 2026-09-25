import overlay as overlay_module
from overlay import _format_elapsed, _format_pos, _merge_state
from unittest.mock import patch


def test_format_elapsed_zero():
    assert _format_elapsed(0) == "00:00"


def test_format_elapsed_under_a_minute():
    assert _format_elapsed(45) == "00:45"


def test_format_elapsed_minutes_and_seconds():
    assert _format_elapsed(125) == "02:05"


def test_format_elapsed_negative_clamps_to_zero():
    assert _format_elapsed(-5) == "00:00"


def test_format_pos_none():
    assert _format_pos(None) == "-"


def test_format_pos_tuple():
    assert _format_pos((14, 45)) == "(14, 45)"


def test_merge_state_overwrites_only_provided_fields():
    current = {"state": "idle", "pos": None, "target": None, "message": "-"}
    updated = _merge_state(current, state="寻路中", pos=(1, 2))
    assert updated == {"state": "寻路中", "pos": (1, 2), "target": None, "message": "-"}


def test_merge_state_none_values_do_not_overwrite():
    current = {"state": "寻路中", "pos": (1, 2), "target": None, "message": "-"}
    updated = _merge_state(current, state=None, message="卡住了")
    assert updated == {"state": "寻路中", "pos": (1, 2), "target": None, "message": "卡住了"}


def test_merge_state_does_not_mutate_input():
    current = {"state": "idle"}
    _merge_state(current, state="移动中")
    assert current == {"state": "idle"}


def test_create_overlay_falls_back_when_appkit_construction_fails(monkeypatch):
    def raise_error(*args, **kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(overlay_module.AppKit, "NSApplication", raise_error)
    result = overlay_module.create_overlay()
    assert isinstance(result, overlay_module._NullOverlay)
    # must never raise, whatever it's called with
    result.update(state="寻路中", pos=(1, 2), target=(3, 4), message="test")
    result.close()


def test_null_overlay_update_ignores_all_args():
    stub = overlay_module._NullOverlay()
    assert stub.update(state="x", pos=(1, 1), target=(2, 2), message="y") is None


def test_create_overlay_returns_null_overlay_when_appkit_is_none(monkeypatch):
    # import overlay 时如果没有 pyobjc, AppKit 会被置为 None (never-raises契约的核心场景).
    monkeypatch.setattr(overlay_module, "AppKit", None)
    result = overlay_module.create_overlay()
    assert isinstance(result, overlay_module._NullOverlay)
    # must never raise, whatever it's called with
    result.update(state="寻路中", pos=(1, 2), target=(3, 4), message="test")
    result.close()


def test_status_overlay_update_is_noop_after_dead_latched():
    overlay = overlay_module.create_overlay()
    assert isinstance(overlay, overlay_module.StatusOverlay)
    try:
        # 模拟窗口在运行中挂掉之后再次调用update/close的情况.
        overlay._dead = True
        assert overlay.update(state="出错", message="不应该抛异常") is None
        assert overlay.close() is None
    finally:
        overlay._dead = False
        overlay.close()


def test_status_overlay_update_latches_dead_on_exception(monkeypatch):
    overlay = overlay_module.create_overlay()
    assert isinstance(overlay, overlay_module.StatusOverlay)
    try:
        def raise_error(*args, **kwargs):
            raise RuntimeError("window server gone")

        monkeypatch.setattr(overlay, "_pump_events", raise_error)
        assert overlay._dead is False
        # update() 内部抛异常时应吞掉异常并锁死_dead, 而不是把异常传给main.py.
        result = overlay.update(state="出错")
        assert result is None
        assert overlay._dead is True
        # 锁死之后再调用也必须是no-op, 不再抛异常.
        assert overlay.update(state="再来一次") is None
    finally:
        monkeypatch.undo()
        overlay._dead = False
        overlay.close()


def test_mac_confirm_dialog_click_sets_confirmed_and_wait_returns():
    dialog = overlay_module._MacConfirmDialog()
    assert dialog._confirmed is False
    dialog._button.performClick_(None)  # 跟真实鼠标点击走同一条target/action路径
    assert dialog._confirmed is True
    dialog.wait_for_confirm()  # 已经confirmed了, 应该立刻返回(不阻塞)并关闭窗口


def test_show_fullscreen_confirm_falls_back_to_console_when_appkit_is_none(monkeypatch):
    monkeypatch.setattr(overlay_module, "AppKit", None)
    with patch("builtins.input", return_value="") as mock_input:
        overlay_module.show_fullscreen_confirm()
    mock_input.assert_called_once()


def test_show_fullscreen_confirm_falls_back_to_console_when_mac_dialog_construction_fails(monkeypatch):
    def raise_error(*args, **kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(overlay_module, "_MacConfirmDialog", raise_error)
    with patch("builtins.input", return_value="") as mock_input:
        overlay_module.show_fullscreen_confirm()
    mock_input.assert_called_once()


def test_show_fullscreen_confirm_falls_back_to_console_when_tk_is_none(monkeypatch):
    monkeypatch.setattr(overlay_module, "_IS_MACOS", False)
    monkeypatch.setattr(overlay_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(overlay_module, "tk", None)
    with patch("builtins.input", return_value="") as mock_input:
        overlay_module.show_fullscreen_confirm()
    mock_input.assert_called_once()


def test_null_overlay_show_and_hide_warning_are_noops():
    stub = overlay_module._NullOverlay()
    assert stub.show_warning("无法检测到位置") is None
    assert stub.hide_warning() is None


def test_status_overlay_show_warning_builds_centered_window_once_and_reuses_it():
    overlay = overlay_module.create_overlay()
    assert isinstance(overlay, overlay_module.StatusOverlay)
    try:
        assert overlay._warning_window is None
        overlay.show_warning("无法检测到位置，请查看地图是否放大（M键）或窗口是否全屏（F11）")
        assert overlay._warning_window is not None
        first_window = overlay._warning_window
        assert overlay._dead is False

        # 再调一次: 复用同一个窗口对象(不重新建), 只更新文字, 不炸.
        overlay.show_warning("第二条不同的警告文案")
        assert overlay._warning_window is first_window
        assert overlay._dead is False

        overlay.hide_warning()
        assert overlay._dead is False
    finally:
        overlay.close()


def test_status_overlay_hide_warning_before_any_show_is_noop():
    overlay = overlay_module.create_overlay()
    assert isinstance(overlay, overlay_module.StatusOverlay)
    try:
        assert overlay.hide_warning() is None
        assert overlay._dead is False
    finally:
        overlay.close()


def test_status_overlay_show_warning_latches_dead_on_exception(monkeypatch):
    overlay = overlay_module.create_overlay()
    assert isinstance(overlay, overlay_module.StatusOverlay)
    try:
        def raise_error(*args, **kwargs):
            raise RuntimeError("window server gone")

        monkeypatch.setattr(overlay, "_pump_events", raise_error)
        assert overlay._dead is False
        result = overlay.show_warning("无法检测到位置")
        assert result is None
        assert overlay._dead is True
        # 锁死之后再调用也必须是no-op, 不再抛异常.
        assert overlay.show_warning("再来一次") is None
        assert overlay.hide_warning() is None
    finally:
        monkeypatch.undo()
        overlay._dead = False
        overlay.close()


import pytest


@pytest.mark.parametrize("state, level", [
    ("刷怪中", "ok"),
    ("寻路中", "ok"), ("完成", "ok"),
    ("卡住", "warn"), ("无法检测位置", "warn"), ("AFK弹窗处理中", "warn"),
    ("出错", "bad"), ("已死亡", "bad"),
    ("-", "idle"), (None, "idle"), ("没见过的状态", "idle"),
])
def test_state_level(state, level):
    assert overlay_module._state_level(state) == level


def test_hud_view_formats_location_and_timers():
    st = {"state": "移动中", "pos": (1, 2), "target": (3, 4), "message": "-"}
    v = overlay_module._hud_view(st, start=0, state_since=100, now=125)
    assert v["loc"] == "位置 (1, 2)  →  目标 (3, 4)"
    assert v["message"] == ""          # "-" 占位不显示
    assert v["total"] == "已运行 02:05"
    assert v["in_state"] == "本状态 00:25"
    assert v["level"] == "ok"


def test_hud_view_location_without_target():
    st = {"state": "-", "pos": (5, 6), "target": None, "message": "x"}
    assert overlay_module._hud_view(st, 0, 0, 0)["loc"] == "位置 (5, 6)"


def test_advance_state_resets_timer_only_when_state_text_changes():
    cur = {"state": "移动中", "pos": None, "target": None, "message": "-"}
    new, since = overlay_module._advance_state(cur, 10, 50, pos=(1, 1))
    assert since == 10                 # 同一状态反复 update 不清零
    new, since = overlay_module._advance_state(new, since, 60, state="卡住")
    assert since == 60
    assert new["pos"] == (1, 1)
