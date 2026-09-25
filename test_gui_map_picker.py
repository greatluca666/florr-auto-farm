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
