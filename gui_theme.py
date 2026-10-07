"""控制面板的配色 / 字体 / 几个复用的小控件工厂. 所有 gui_*.py 从这里取色取字,
不再各自写死 "#8a2b2b" / ("", 9) / ("Menlo", 11) 这种散落常量.

字体按平台挑: Windows 上 Tk 默认字体渲染中文很糊, 而 "Menlo" 在 Windows 上不存在
(会静默回落成等宽的 Courier, 中文日志一行长一行短). 这里显式给出每个平台都自带的字体.
"""
import sys

import customtkinter as ctk

if sys.platform == "win32":
    UI_FAMILY, MONO_FAMILY = "Microsoft YaHei UI", "Consolas"
elif sys.platform == "darwin":
    UI_FAMILY, MONO_FAMILY = "PingFang SC", "Menlo"
else:
    UI_FAMILY, MONO_FAMILY = "Noto Sans CJK SC", "DejaVu Sans Mono"

# ---- 配色(只做深色; App 启动时强制 dark) ----
BG = "#15171c"          # 窗口底
SIDEBAR = "#1b1e24"
SURFACE = "#20242b"     # 卡片
SURFACE_HI = "#282d36"  # 卡片内的次级块 / 悬停
BORDER = "#323843"
TEXT = "#e7e9ee"
MUTED = "#8c94a3"
FAINT = "#5d6470"
ACCENT = "#3d7eff"
ACCENT_HOVER = "#2f69dd"
SUCCESS = "#2fb86a"
SUCCESS_HOVER = "#27995a"
DANGER = "#e5484d"
DANGER_HOVER = "#c43a3f"
WARN = "#f0a020"
LOG_BG = "#0f1115"

MAP_LABELS = {"garden": "花园", "desert": "沙漠", "ocean": "海洋", "jungle": "丛林",
              "anthell": "蚁狱", "sewers": "下水道", "factory": "工厂"}


def map_label(name):
    return MAP_LABELS.get(name, name)


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=UI_FAMILY, size=size, weight=weight)


def mono(size=12):
    return ctk.CTkFont(family=MONO_FAMILY, size=size)


def card(master, **kw):
    kw.setdefault("fg_color", SURFACE)
    kw.setdefault("corner_radius", 10)
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", BORDER)
    return ctk.CTkFrame(master, **kw)


def section_title(master, text, sub=None):
    """卡片顶上的小标题(+ 可选灰色副标题). 返回外层 frame, 调用方自己 pack/grid."""
    box = ctk.CTkFrame(master, fg_color="transparent")
    ctk.CTkLabel(box, text=text, font=font(14, "bold"), text_color=TEXT,
                 anchor="w").pack(anchor="w")
    if sub:
        ctk.CTkLabel(box, text=sub, font=font(11), text_color=MUTED, anchor="w",
                     justify="left").pack(anchor="w")
    return box


def hint(master, text="", **kw):
    kw.setdefault("text_color", MUTED)
    kw.setdefault("anchor", "w")
    kw.setdefault("justify", "left")
    return ctk.CTkLabel(master, text=text, font=font(11), **kw)


def primary_button(master, text, command, **kw):
    kw.setdefault("fg_color", ACCENT)
    kw.setdefault("hover_color", ACCENT_HOVER)
    kw.setdefault("font", font(13, "bold"))
    return ctk.CTkButton(master, text=text, command=command, **kw)


def ghost_button(master, text, command, **kw):
    """次要按钮: 透明底 + 描边. 取消 / 编辑 / 改名 这类."""
    kw.setdefault("fg_color", "transparent")
    kw.setdefault("hover_color", SURFACE_HI)
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", BORDER)
    kw.setdefault("text_color", TEXT)
    kw.setdefault("font", font(12))
    return ctk.CTkButton(master, text=text, command=command, **kw)


def danger_button(master, text, command, **kw):
    kw.setdefault("fg_color", "transparent")
    kw.setdefault("hover_color", "#3a1f22")
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", "#5a2a2e")
    kw.setdefault("text_color", "#ff8a8e")
    kw.setdefault("font", font(12))
    return ctk.CTkButton(master, text=text, command=command, **kw)


def switch(master, text, **kw):
    kw.setdefault("progress_color", ACCENT)
    kw.setdefault("font", font(13))
    return ctk.CTkSwitch(master, text=text, **kw)


def set_entry_enabled(entry, enabled):
    """CTkEntry 置灰后外观几乎不变, 用户分不清能不能填 —— 顺手把字色也调暗."""
    entry.configure(state="normal" if enabled else "disabled",
                    text_color=TEXT if enabled else FAINT)


# 屏幕下沿给任务栏 / Dock 留的余量(逻辑像素). winfo_screenheight 报的是整块屏幕,
# 不扣任务栏 —— 1080p + Windows 任务栏 ≈ 只剩 1040 可用.
_SCREEN_MARGIN_BOTTOM = 90
_SCREEN_MARGIN_SIDE = 20


def fit_geometry(w, h, parent_box, screen, margin_bottom=_SCREEN_MARGIN_BOTTOM):
    """算一个弹窗的 (w, h, x, y): 先居中到 parent 上, 再整个夹进屏幕可用区.
    所有量用同一种单位(调用方负责把物理像素除以 DPI 缩放). 纯函数, 好单测.

    以前只做了 x/y >= 0, 窗口尺寸写死 —— 时块编辑窗 780 高, Windows 上 125%/150%
    缩放 + 任务栏时超出屏幕, 底部的「保存 / 取消」整条掉到屏幕外面点不到."""
    px, py, pw, ph = parent_box
    sw, sh = screen
    w = max(1, min(w, sw - 2 * _SCREEN_MARGIN_SIDE))
    h = max(1, min(h, sh - margin_bottom))
    x = px + (pw - w) // 2
    y = py + (ph - h) // 2
    x = max(0, min(x, sw - w))
    y = max(0, min(y, sh - margin_bottom - h))
    return int(w), int(h), int(x), int(y)


def fit_main_window(w, h, min_w, min_h, screen):
    """主窗口: 想要的 w x h 夹进屏幕可用区并居中, 最小尺寸也跟着夹 —— 不然 1366x768 +
    125% 缩放(逻辑 1093x614)下 minsize 820x560 本身就比可用区高, 窗口拖不小, 底部的
    「开始调度」整条在屏幕外面. 返回 (w, h, x, y, min_w, min_h), 单位同 screen."""
    w, h, x, y = fit_geometry(w, h, (0, 0, screen[0], screen[1]), screen)
    return w, h, x, y, min(min_w, w), min(min_h, h)


def geometry_string(w, h, x, y, scale):
    """CTk 的 geometry() 只把「宽x高」乘缩放系数, "+x+y" 原样交给 Tk 当物理像素 ——
    所以逻辑单位算出来的位置要自己乘回去. 以前没乘, 125%/150% 下弹窗都往左上偏."""
    return f"{int(w)}x{int(h)}+{round(x * scale)}+{round(y * scale)}"


def _window_scaling(win):
    try:
        return float(win._get_window_scaling()) or 1.0
    except Exception:
        return 1.0


def _logical_screen(win, scale):
    # winfo_screen* 报的是物理像素(CTk 让进程 DPI-aware 了)
    return win.winfo_screenwidth() / scale, win.winfo_screenheight() / scale


def place_main_window(win, w, h, min_w, min_h):
    """主窗口的初始尺寸 / 位置 / 最小尺寸. w/h/min_* 是 CTk 逻辑像素(会被 DPI 缩放放大)."""
    scale = _window_scaling(win)
    w, h, x, y, min_w, min_h = fit_main_window(w, h, min_w, min_h,
                                               _logical_screen(win, scale))
    win.minsize(min_w, min_h)
    win.geometry(geometry_string(w, h, x, y, scale))
    return w, h


def center_on(win, parent, w, h, min_size=None):
    """把 Toplevel 摆到 parent 正中(而不是 Tk 默认的屏幕左上角), 并保证整个窗口
    落在屏幕可用区里. w/h 是 CTk 的逻辑像素(会被 DPI 缩放放大).
    min_size=(min_w, min_h) 顺带设最小尺寸, 夹到摆好的尺寸以内 —— 单独 minsize() 的话
    小屏上最小尺寸比可用区还大, Tk 会把窗口撑回去, 底部按钮又掉出屏幕."""
    parent.update_idletasks()
    # 全部换成逻辑单位来算(winfo_* 报的是物理像素), 位置最后由 geometry_string 换回物理像素.
    scale = _window_scaling(win)
    box = (parent.winfo_rootx() / scale, parent.winfo_rooty() / scale,
           parent.winfo_width() / scale, parent.winfo_height() / scale)
    w, h, x, y = fit_geometry(w, h, box, _logical_screen(win, scale))
    win.geometry(geometry_string(w, h, x, y, scale))
    if min_size is not None:
        win.minsize(min(min_size[0], w), min(min_size[1], h))
    return w, h
