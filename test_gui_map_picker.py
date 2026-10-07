import pytest

from gui_map_picker import (
    View,
    anchored_pan,
    clamp_image_point,
    image_to_widget,
    widget_to_image,
)


def test_image_to_widget_applies_scale_and_offset():
    v = View(s=2.0, offset_x=10.0, offset_y=20.0, img_w=300, img_h=300)
    assert image_to_widget(0, 0, v) == (10.0, 20.0)
    assert image_to_widget(5, 7, v) == (20.0, 34.0)


def test_widget_to_image_is_inverse_of_image_to_widget():
    v = View(s=1.5, offset_x=12.0, offset_y=8.0, img_w=300, img_h=300)
    for ix, iy in [(0, 0), (150, 150), (299, 299), (33, 210)]:
        wx, wy = image_to_widget(ix, iy, v)
        assert widget_to_image(wx, wy, v) == (ix, iy)


def test_widget_to_image_rounds_to_nearest_pixel():
    v = View(s=4.0, offset_x=0.0, offset_y=0.0, img_w=300, img_h=300)
    assert widget_to_image(9.0, 9.0, v) == (2, 2)    # 2.25 -> 2
    assert widget_to_image(13.0, 13.0, v) == (3, 3)  # 3.25 -> 3


def test_widget_to_image_clamps_outside_the_image():
    v = View(s=2.0, offset_x=0.0, offset_y=0.0, img_w=300, img_h=300)
    assert widget_to_image(-50.0, -50.0, v) == (0, 0)
    assert widget_to_image(99999.0, 99999.0, v) == (299, 299)


def test_clamp_image_point():
    assert clamp_image_point(-1, 500, 300, 300) == (0, 299)
    assert clamp_image_point(10, 20, 300, 300) == (10, 20)


def _reproject_after_zoom(old, cursor_x, cursor_y, new_s, canvas_w, canvas_h):
    """Helper: apply anchored_pan, rebuild the post-zoom View, and return where the
    image point that was under the cursor lands after the zoom."""
    img_pt = (
        (cursor_x - old.offset_x) / old.s,
        (cursor_y - old.offset_y) / old.s,
    )
    pan_x, pan_y = anchored_pan(old, cursor_x, cursor_y, new_s, canvas_w, canvas_h)
    new = View(
        s=new_s,
        offset_x=(canvas_w - old.img_w * new_s) / 2 + pan_x,
        offset_y=(canvas_h - old.img_h * new_s) / 2 + pan_y,
        img_w=old.img_w,
        img_h=old.img_h,
    )
    return image_to_widget(*img_pt, new)


def test_anchored_pan_keeps_cursor_pixel_fixed_on_zoom_in():
    old = View(s=2.0, offset_x=0.0, offset_y=0.0, img_w=300, img_h=300)
    rx, ry = _reproject_after_zoom(old, 123.0, 77.0, new_s=3.0,
                                   canvas_w=640.0, canvas_h=480.0)
    assert rx == pytest.approx(123.0, abs=1e-9)
    assert ry == pytest.approx(77.0, abs=1e-9)


def test_anchored_pan_keeps_cursor_pixel_fixed_on_zoom_out_with_offset():
    old = View(s=3.5, offset_x=-40.0, offset_y=25.0, img_w=300, img_h=300)
    rx, ry = _reproject_after_zoom(old, 210.0, 190.0, new_s=2.0,
                                   canvas_w=500.0, canvas_h=500.0)
    assert rx == pytest.approx(210.0, abs=1e-9)
    assert ry == pytest.approx(190.0, abs=1e-9)


# ---- 放大后够不到地图上部 / 左上角(粉丝反馈 2026-10-05) ----
from types import SimpleNamespace

import gui_map_picker
from gui_map_picker import clamp_pan


def test_clamp_pan_keeps_a_zoomed_image_covering_the_canvas():
    # 300x300 图放到 s=2 -> 600x600, 控件 400x300: 偏移只能在 [控件-图, 0] 里,
    # 图边不会离开控件边(不留空, 四个角都拖得到).
    cw, ch, s = 400, 300, 2.0
    for pan in [(1e6, 1e6), (-1e6, -1e6), (37.0, -12.0)]:
        px, py = clamp_pan(pan, s, cw, ch, 300, 300)
        offx = (cw - 600) / 2 + px
        offy = (ch - 600) / 2 + py
        assert cw - 600 <= offx <= 0
        assert ch - 600 <= offy <= 0
    # 拖到底: 图左上角正好在控件左上角
    px, py = clamp_pan((1e6, 1e6), s, cw, ch, 300, 300)
    assert ((cw - 600) / 2 + px, (ch - 600) / 2 + py) == (0, 0)


def test_clamp_pan_centers_the_dimension_that_already_fits():
    # 1 倍时图 300 宽塞进 600 宽控件: 横向没有可平移的余量, 只能居中
    assert clamp_pan((50.0, 0.0), 1.0, 600, 300, 300, 300) == (0.0, 0.0)


@pytest.fixture
def tk_root(monkeypatch):
    import os
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gui_map_picker.ctk.CTk()
    root.geometry("640x400")
    yield root
    root.destroy()


def _picker_in_scrollable_editor(root):
    """跟时块编辑器一样: 地图放在比窗口高的 CTkScrollableFrame 里."""
    ctk = gui_map_picker.ctk
    body = ctk.CTkScrollableFrame(root)
    body.pack(fill="both", expand=True)
    ctk.CTkFrame(body, height=300).pack(fill="x")
    picker = gui_map_picker.MapPicker(body)
    picker.configure(height=320)
    picker.pack(fill="x")
    picker.pack_propagate(False)
    ctk.CTkFrame(body, height=600).pack(fill="x")
    picker.load_map("desert")
    for _ in range(5):
        root.update()
    body._parent_canvas.yview_moveto(0.2)
    root.update()
    return body, picker


def test_wheel_over_the_map_zooms_without_scrolling_the_editor(tk_root):
    # 以前滚轮同时被 CTkScrollableFrame 的 bind_all 拿去滚整个编辑窗(Windows 一格 20px),
    # 地图从指针底下滑走 —— 在地图上沿放大两下指针就滑出地图了, 上部 / 左上角放大不进去.
    body, picker = _picker_in_scrollable_editor(tk_root)
    c = picker._canvas
    before = body._parent_canvas.yview()
    x, y = (int(round(t)) for t in gui_map_picker.image_to_widget(5, 5, picker._view()))
    c.event_generate("<MouseWheel>", x=x, y=y, delta=120,
                     rootx=c.winfo_rootx() + x, rooty=c.winfo_rooty() + y)
    tk_root.update()
    assert picker._zoom == 2
    assert body._parent_canvas.yview() == before
    # 平移不重新 resize, 但缩放后画的必须是新尺寸的图
    v = picker._view()
    assert picker._tk_img.width() == int(v.img_w * v.s)


def test_right_drag_pans_a_zoomed_map_to_its_top_left_corner(tk_root):
    _body, picker = _picker_in_scrollable_editor(tk_root)
    c = picker._canvas
    cw, ch = c.winfo_width(), c.winfo_height()
    # 在地图中间放大到 4 倍: 左上角已经出了控件
    for _ in range(3):
        picker._on_wheel(SimpleNamespace(x=cw // 2, y=ch // 2, delta=120))
    v = picker._view()
    assert gui_map_picker.widget_to_image(0, 0, v) != (0, 0)
    # 右键(Windows 是 Button-3, macOS 是 Button-2)按住往右下拖一大段
    for b in (2, 3):
        assert c.bind(f"<B{b}-Motion>")
    picker._on_pan_press(SimpleNamespace(x=10, y=10))
    picker._on_pan_drag(SimpleNamespace(x=5000, y=5000))
    picker._on_pan_release(SimpleNamespace(x=5000, y=5000))
    v = picker._view()
    assert (v.offset_x, v.offset_y) == (0, 0)      # 夹住: 图左上角贴控件左上角, 不会拖飞
    # 现在左键点控件左上角就是地图左上角
    picker._on_press(SimpleNamespace(x=2, y=2))
    picker._on_release(SimpleNamespace(x=2, y=2))
    assert picker._point == (0, 0)


def test_wheel_beside_the_map_image_still_scrolls_the_editor(tk_root):
    # 地图控件占满整行宽, 1 倍时图两侧是空白条: 指针在那里滚轮应该照常滚编辑窗,
    # 不然指针在这一行就滚不动了.
    body, picker = _picker_in_scrollable_editor(tk_root)
    c = picker._canvas
    assert picker._view().offset_x > 10          # 确实有空白条
    before = body._parent_canvas.yview()
    c.event_generate("<MouseWheel>", x=3, y=50, delta=-120,
                     rootx=c.winfo_rootx() + 3, rooty=c.winfo_rooty() + 50)
    tk_root.update()
    assert picker._zoom == 1
    assert body._parent_canvas.yview() != before
