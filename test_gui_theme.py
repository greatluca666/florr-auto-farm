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


# ---- 主窗口 / 弹窗在高 DPI 缩放、小屏上的摆放 ----

def test_main_window_and_its_min_size_shrink_to_fit_a_small_scaled_screen():
    # 1366x768 + Windows 125% 缩放 -> 逻辑 1093x614. 980x680 放不下, 最小尺寸 820x560
    # 也放不下 —— 以前 minsize 比屏幕还高, 用户连拖小窗口都做不到, 底部「开始调度」看不见.
    w, h, x, y, min_w, min_h = t.fit_main_window(980, 680, 820, 560, (1093, 614))
    assert x >= 0 and x + w <= 1093
    assert y >= 0 and y + h <= 614 - t._SCREEN_MARGIN_BOTTOM
    assert min_w <= w and min_h <= h


def test_main_window_keeps_its_size_and_is_centered_on_a_big_screen():
    assert t.fit_main_window(980, 680, 820, 560, (1920, 1080)) == (980, 680, 470, 200, 820, 560)


def test_geometry_string_puts_position_back_into_physical_pixels():
    # CTk 的 geometry() 只把「宽x高」乘缩放系数, "+x+y" 原样交给 Tk(物理像素).
    assert t.geometry_string(640, 500, 320, 110, 1.5) == "640x500+480+165"
    assert t.geometry_string(640, 500, 320, 110, 1.0) == "640x500+320+110"


class _FakeWin:
    def __init__(self, scale, screen):
        self._scale = scale
        self._screen = screen
        self.geo = None
        self.min = None

    def _get_window_scaling(self):
        return self._scale

    def winfo_screenwidth(self):
        return self._screen[0]

    def winfo_screenheight(self):
        return self._screen[1]

    def geometry(self, s):
        self.geo = s

    def minsize(self, w, h):
        self.min = (w, h)


class _FakeParent:
    def __init__(self, x, y, w, h):
        self._box = (x, y, w, h)

    def update_idletasks(self):
        pass

    def winfo_rootx(self):
        return self._box[0]

    def winfo_rooty(self):
        return self._box[1]

    def winfo_width(self):
        return self._box[2]

    def winfo_height(self):
        return self._box[3]


def test_center_on_lands_on_the_parents_physical_center_at_150_percent():
    # 1080p @150%: 父窗口物理 (225,30) 1470x1020, 中心 (960,540). 640x500 的弹窗物理是
    # 960x750 -> 左上应在 (480,165). 以前 x/y 按逻辑单位直接交给 Tk, 落在 (320,110), 偏左上.
    win = _FakeWin(1.5, (1920, 1080))
    w, h = t.center_on(win, _FakeParent(225, 30, 1470, 1020), 640, 500)
    assert (w, h) == (640, 500)
    assert win.geo == "640x500+480+165"


def test_place_main_window_fits_1366x768_at_125_percent():
    win = _FakeWin(1.25, (1366, 768))
    t.place_main_window(win, 980, 680, 820, 560)
    size, x, y = win.geo.split("+")
    w, h = (int(v) for v in size.split("x"))
    # 宽高是逻辑单位(CTk 会乘 1.25), 位置是物理像素
    assert int(x) + w * 1.25 <= 1366
    assert int(y) + h * 1.25 <= 768 - t._SCREEN_MARGIN_BOTTOM * 1.25
    assert win.min[0] <= w and win.min[1] <= h


def test_center_on_clamps_the_dialogs_min_size_to_what_fits_on_screen():
    # 1280x720 @150% -> 逻辑 853x480, 可用高 390. 索敌设置窗 minsize 780x400 比这还高,
    # 不夹的话 Tk 会把窗口撑回 400, 底部按钮又掉出屏幕.
    win = _FakeWin(1.5, (1280, 720))
    w, h = t.center_on(win, _FakeParent(0, 0, 1280, 720), 840, 700, min_size=(780, 400))
    assert win.min == (min(780, w), min(400, h))
    assert win.min[1] <= 480 - t._SCREEN_MARGIN_BOTTOM


def test_center_on_leaves_min_size_alone_when_not_given():
    win = _FakeWin(1.0, (1920, 1080))
    t.center_on(win, _FakeParent(0, 0, 980, 680), 340, 110)
    assert win.min is None
