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


# ---- Windows tk 悬浮窗随 DPI 缩放 ----
# worker 进程是 DPI-aware 的(utils.py), Tk 会按真实 DPI 放大「磅」字号, 但 place() /
# geometry 的像素不会跟着变. 版面是 100% 下量的, 不乘倍数 125% 起字就比格子大, 全被截断.

def test_dpi_factor_is_relative_to_96_dpi_and_never_shrinks():
    assert overlay_module._dpi_factor(96 / 72) == 1.0
    assert overlay_module._dpi_factor(144 / 72) == pytest.approx(1.5)
    assert overlay_module._dpi_factor(192 / 72) == pytest.approx(2.0)
    assert overlay_module._dpi_factor(1.0) == 1.0      # macOS 的 Tk 报 ~1.0, 不缩小
    assert overlay_module._dpi_factor("bad") == 1.0


# 布局测试要真 Tk. 放子进程里跑: macOS 上同一进程先起过 AppKit 悬浮窗(上面那些用例)
# 再建 tk.Tk() 会直接 abort.
_TK_LAYOUT_PROBE = r"""
import json, tkinter as tk
import overlay as o

def boxes(widget):
    out = []
    for c in widget.winfo_children():
        info = c.place_info()
        if info:
            out.append([int(info[k]) for k in ("x", "y", "width", "height")])
        out.extend(boxes(c))
    return out

root = tk.Tk()
root.withdraw()
res = {}
for k in (1.0, 1.25, 1.5, 2.0):
    kw = {} if k == 1.0 else {"k": k}
    r = res[str(k)] = {}
    win = tk.Toplevel(root)
    hud = o._build_tk_hud(win, **kw)
    r["hud"] = boxes(win)
    r["wrap"] = int(hud["message"].cget("wraplength"))
    win = tk.Toplevel(root)
    o._build_tk_card(win, "t", 560, 200, **kw)
    r["card"] = boxes(win)
root.destroy()
print(json.dumps(res))
"""


@pytest.fixture(scope="module")
def tk_layouts():
    import json
    import os
    import subprocess
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.run([sys.executable, "-c", _TK_LAYOUT_PROBE], cwd=here,
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 and "TclError" in proc.stderr:
        pytest.skip("没有可用的显示, 建不了 tk 窗口")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_scaled(base, scaled, k):
    assert len(base) == len(scaled) and base
    for b, s in zip(base, scaled):
        for bv, sv in zip(b, s):
            assert abs(sv - bv * k) <= 1, (b, s, k)


@pytest.mark.parametrize("k", [1.25, 1.5, 2.0])
@pytest.mark.parametrize("part", [
    "hud",
    "card",
])
def test_tk_layout_scales_with_dpi(tk_layouts, part, k):
    _assert_scaled(tk_layouts["1.0"][part], tk_layouts[str(k)][part], k)


@pytest.mark.parametrize("k", [1.25, 1.5, 2.0])
def test_tk_wraplength_scales_with_dpi(tk_layouts, k):
    base, scaled = tk_layouts["1.0"], tk_layouts[str(k)]
    assert scaled["wrap"] == pytest.approx(base["wrap"] * k, abs=1)


def test_tk_window_geometries_scale_sizes_but_keep_the_hud_anchor():
    g1 = overlay_module._tk_geometries(1.0, 1920, 1080)
    g = overlay_module._tk_geometries(1.5, 1920, 1080)
    assert g1["hud"] == f"300x132+{overlay_module._LEFT}+{overlay_module._TOP_OFFSET}"
    assert g["hud"] == f"450x198+{overlay_module._LEFT}+{overlay_module._TOP_OFFSET}"
    # 警告 / 确认弹窗放大后仍在屏幕正中
    assert g["warning"] == "840x300+540+390"
    assert g["confirm"] == "540x255+690+412"
