"""地图选择器: 在二值地图(maps/<name>.png, 300x300)上左键点=目标点、左键拖=刷怪
矩形、滚轮=1~4x 缩放(视图中心跟随指针, 不做独立拖动平移). 二值图就是寻路用的
坐标系本身(utils.load_binary_map() 读的同一张图), 所以点出来的坐标可以直接进
config.json 的 location / farming_area.

坐标变换(image_to_widget / widget_to_image)抽成模块级纯函数, 不碰 tk, 好单测.
"""
import os
import sys
from dataclasses import dataclass

import customtkinter as ctk
import tkinter as tk
from PIL import Image, ImageTk

# 跟 app_config.CONFIG_PATH 一样按 sys.argv[0] 定位(= 可执行文件同级), 不用 cwd ——
# 从别的目录双击/命令行拉起 exe 时 cwd 不一定是 exe 目录, 那样会读不到 maps/.
_MAP_DIR = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "maps")
_DRAG_THRESHOLD_PX = 4  # 按下到松开在这个像素内算"点一下"(设目标点), 超过算"拖框"


@dataclass(frozen=True)
class View:
    """当前把 img_w x img_h 的图画到控件里的方式.
    s: 一个图像像素 = 多少控件像素 (= fit * zoom). offset: 图左上角在控件里的坐标.
    """
    s: float
    offset_x: float
    offset_y: float
    img_w: int
    img_h: int


def image_to_widget(ix, iy, view):
    return (view.offset_x + ix * view.s, view.offset_y + iy * view.s)


def clamp_image_point(ix, iy, img_w, img_h):
    return (min(max(ix, 0), img_w - 1), min(max(iy, 0), img_h - 1))


def widget_to_image(wx, wy, view):
    ix = round((wx - view.offset_x) / view.s)
    iy = round((wy - view.offset_y) / view.s)
    return clamp_image_point(ix, iy, view.img_w, view.img_h)


def _fit_scale(canvas_w, canvas_h, img_w, img_h):
    """把整张图塞进控件、保持长宽比的基准缩放."""
    if img_w <= 0 or img_h <= 0:
        return 1.0
    return min(canvas_w / img_w, canvas_h / img_h)


def anchored_pan(old_view, cursor_x, cursor_y, new_s, canvas_w, canvas_h):
    """滚轮缩放时, 让指针 (cursor_x, cursor_y) 下的那个图像点在缩放后仍落在指针处.
    返回叠加在"居中偏移"之上的平移量 (pan_x, pan_y)。
    """
    img_x = (cursor_x - old_view.offset_x) / old_view.s
    img_y = (cursor_y - old_view.offset_y) / old_view.s
    centered_x = (canvas_w - old_view.img_w * new_s) / 2
    centered_y = (canvas_h - old_view.img_h * new_s) / 2
    return (cursor_x - img_x * new_s - centered_x,
            cursor_y - img_y * new_s - centered_y)


class MapPicker(ctk.CTkFrame):
    def __init__(self, master, *, on_point_change=None, on_area_change=None, **kw):
        super().__init__(master, **kw)
        self._on_point_change = on_point_change
        self._on_area_change = on_area_change
        self._pil = None
        self._tk_img = None
        self._zoom = 1
        self._pan = (0.0, 0.0)      # 叠加在"居中偏移"之上的指针锚定平移(控件像素)
        self._point = None          # (ix, iy) 图像像素
        self._area = None           # [(ix,iy),(ix,iy)] 图像像素
        self._drag_start = None     # (wx, wy) 按下时的控件坐标
        self._area_before = None    # 按下前的矩形, "只是点了一下"时用来还原

        self._canvas = tk.Canvas(self, highlightthickness=0, bg="#0f1115")
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Configure>", lambda e: self._redraw())
        self._canvas.bind("<Button-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<MouseWheel>", self._on_wheel)          # Win/mac
        self._canvas.bind("<Button-4>", lambda e: self._on_wheel(e, +1))  # X11
        self._canvas.bind("<Button-5>", lambda e: self._on_wheel(e, -1))

    # ---- 公有 API ----
    def load_map(self, name):
        self._pil = Image.open(os.path.join(_MAP_DIR, f"{name}.png")).convert("RGB")
        self._zoom = 1
        self._pan = (0.0, 0.0)
        self._redraw()

    def set_point(self, pt):
        self._point = tuple(pt) if pt is not None else None
        self._redraw()

    def set_area(self, area):
        self._area = [tuple(area[0]), tuple(area[1])] if area else None
        self._redraw()

    # ---- 内部 ----
    def _view(self):
        if self._pil is None:
            return None
        cw = max(self._canvas.winfo_width(), 1)
        ch = max(self._canvas.winfo_height(), 1)
        iw, ih = self._pil.size
        s = _fit_scale(cw, ch, iw, ih) * self._zoom
        # 缩放后若比控件大, 让图居中(zoom=1 时正好完全贴合); _pan 叠加指针锚定的平移
        offx = (cw - iw * s) / 2 + self._pan[0]
        offy = (ch - ih * s) / 2 + self._pan[1]
        return View(s=s, offset_x=offx, offset_y=offy, img_w=iw, img_h=ih)

    def _redraw(self):
        self._canvas.delete("all")
        v = self._view()
        if v is None:
            return
        disp_w = max(int(v.img_w * v.s), 1)
        disp_h = max(int(v.img_h * v.s), 1)
        # NEAREST: 这是张二值(墙/可走)地图, 默认的双三次会把边界糊成灰渐变 ——
        # 用户放大正是为了对准某个具体像素, 插值只会让他对不准.
        resized = self._pil.resize((disp_w, disp_h), resample=Image.NEAREST)
        self._tk_img = ImageTk.PhotoImage(resized)
        self._canvas.create_image(v.offset_x, v.offset_y, anchor="nw", image=self._tk_img)

        if self._area:
            (x1, y1) = image_to_widget(*self._area[0], v)
            (x2, y2) = image_to_widget(*self._area[1], v)
            self._canvas.create_rectangle(x1, y1, x2, y2, outline="#0a84ff", width=2)
        if self._point:
            (px, py) = image_to_widget(*self._point, v)
            self._canvas.create_line(px - 8, py, px + 8, py, fill="#30d158", width=2)
            self._canvas.create_line(px, py - 8, px, py + 8, fill="#30d158", width=2)

    def _on_press(self, e):
        self._drag_start = (e.x, e.y)
        # <B1-Motion> 移动 1px 就会触发, 所以"点一下"也会先被 _on_drag 当成拖框
        # 把 _area 改掉. 先存一份, 松开时若判定为点击就还原回去 —— 否则用户每点
        # 一次目标点, 画好的刷怪矩形就凭空消失, 而 App._area 里还留着旧值,
        # 界面跟即将存进 config.json 的东西对不上.
        self._area_before = list(self._area) if self._area else None

    def _on_drag(self, e):
        if self._drag_start is None:
            return
        v = self._view()
        if v is None:
            return
        a = widget_to_image(*self._drag_start, v)
        b = widget_to_image(e.x, e.y, v)
        self._area = [a, b]
        self._redraw()

    def _on_release(self, e):
        start = self._drag_start
        self._drag_start = None
        if start is None:
            return
        v = self._view()
        if v is None:
            return
        dx, dy = e.x - start[0], e.y - start[1]
        if (dx * dx + dy * dy) ** 0.5 <= _DRAG_THRESHOLD_PX:
            self._point = widget_to_image(e.x, e.y, v)
            self._area = self._area_before   # 把 _on_drag 抖出来的退化矩形还原掉
            self._redraw()
            if self._on_point_change:
                self._on_point_change(self._point)
        else:
            self._area = [widget_to_image(*start, v), widget_to_image(e.x, e.y, v)]
            self._redraw()
            if self._on_area_change:
                self._on_area_change(list(self._area))
        self._area_before = None

    def _on_wheel(self, e, direction=None):
        if direction is None:
            direction = 1 if getattr(e, "delta", 0) > 0 else -1
        old = self._view()
        new_zoom = min(4, max(1, self._zoom + direction))
        if new_zoom == self._zoom:
            return
        if new_zoom == 1:
            # zoom=1 时图正好贴合控件, 没有可平移的余量
            self._zoom = 1
            self._pan = (0.0, 0.0)
            self._redraw()
            return
        if old is None:
            self._zoom = new_zoom
            self._redraw()
            return
        cw = max(self._canvas.winfo_width(), 1)
        ch = max(self._canvas.winfo_height(), 1)
        iw, ih = self._pil.size
        self._zoom = new_zoom
        s_new = _fit_scale(cw, ch, iw, ih) * self._zoom
        self._pan = anchored_pan(old, e.x, e.y, s_new, cw, ch)
        self._redraw()
