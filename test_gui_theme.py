import gui_theme as t


def test_small_popup_is_just_centered_on_parent():
    assert t.fit_geometry(340, 110, (100, 60, 980, 708), (1920, 1080)) == (340, 110, 420, 359)


def test_tall_editor_is_shrunk_to_fit_a_1080p_screen_with_taskbar():
    # 1080p, 150% 缩放 -> 逻辑 1280x720; 780 高的编辑窗放不下, 必须缩到屏内.
    w, h, x, y = t.fit_geometry(640, 780, (50, 20, 980, 680), (1280, 720))
    assert h <= 720 - t._SCREEN_MARGIN_BOTTOM
    assert y >= 0 and y + h <= 720 - t._SCREEN_MARGIN_BOTTOM
    assert x >= 0 and x + w <= 1280


def test_window_pushed_off_the_bottom_edge_is_pulled_back():
    # 父窗口贴着屏幕下沿 -> 居中出来的 y 会越界.
    _w, h, _x, y = t.fit_geometry(600, 500, (0, 700, 800, 300), (1920, 1080))
    assert y + h <= 1080 - t._SCREEN_MARGIN_BOTTOM


def test_window_off_the_left_or_top_is_pulled_in():
    _w, _h, x, y = t.fit_geometry(400, 300, (-500, -300, 200, 100), (1920, 1080))
    assert x == 0 and y == 0


def test_never_wider_than_the_screen():
    w, *_ = t.fit_geometry(5000, 100, (0, 0, 800, 600), (1366, 768))
    assert w <= 1366
