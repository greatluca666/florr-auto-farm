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

MAP_LABELS = {"desert": "沙漠", "ocean": "海洋", "anthell": "蚁狱"}


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


def center_on(win, parent, w, h):
    """把 Toplevel 摆到 parent 正中(而不是 Tk 默认的屏幕左上角)."""
    parent.update_idletasks()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    x = max(0, px + (pw - w) // 2)
    y = max(0, py + (ph - h) // 2)
    win.geometry(f"{w}x{h}+{x}+{y}")
