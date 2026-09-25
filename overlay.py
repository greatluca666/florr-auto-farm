"""悬浮状态窗 — 全屏运行main.py时显示寻路/移动进度.

macOS: 用PyObjC(AppKit)直接建原生窗口, 不用tkinter —— 实测tkinter的-topmost
只能在同一个macOS Space内置顶, florr.io开的是原生全屏(独立Space)时, tkinter
窗口会跳到另一个桌面上, 根本盖不到游戏画面上。AppKit窗口配合
NSWindowCollectionBehaviorCanJoinAllSpaces + FullScreenAuxiliary +
screensaver级别的窗口层级(1000), 才能真正跨Space盖在全屏游戏上面。

Windows: Windows没有macOS那种独立Space, 浏览器F11全屏是普通的无边框窗口
(不是独占全屏), 常规-topmost就能盖上去, 所以用tkinter无边框窗口即可, 不需要
AppKit那套技巧。但仍需win32扩展窗口样式做到点击穿透 + 不抢键盘焦点, 见
_WindowsOverlay。
"""
import sys
import time

_IS_MACOS = sys.platform == "darwin"
_IS_WINDOWS = sys.platform == "win32"

try:
    import AppKit
    import Foundation
except ImportError:
    AppKit = None
    Foundation = None

try:
    # 给 layer 背景色要 CGColor. NSColor.CGColor() 在没 import Quartz 时返回裸指针
    # (PyObjCPointer 警告), 直接用 Quartz 建更干净.
    import Quartz
except ImportError:
    Quartz = None

try:
    import tkinter as tk
except ImportError:
    tk = None

try:
    import ctypes
except ImportError:
    ctypes = None


def _format_elapsed(seconds):
    """把秒数格式化成 mm:ss, 负数按0算."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _format_pos(pos):
    """把 (x, y) 或 None 格式化成显示用字符串."""
    if pos is None:
        return "-"
    return f"({pos[0]}, {pos[1]})"


def _merge_state(current, **fields):
    """把非None的字段合并进当前状态, 不修改传入的dict."""
    updated = dict(current)
    for key, value in fields.items():
        if value is not None:
            updated[key] = value
    return updated


class _NullOverlay:
    """悬浮窗建不起来时的空壳替代品, 调用什么都不做, 绝不炸主程序."""

    def update(self, state=None, pos=None, target=None, message=None):
        return None

    def show_warning(self, message):
        return None

    def hide_warning(self):
        return None


    def close(self):
        return None


# screensaver窗口层级(CGWindowLevel), 比普通置顶(status级别~25)高得多 ——
# 实测普通status级别 + canJoinAllSpaces 依然会被系统分到另一个Space,
# 只有这个级别才能真正跨越florr.io的全屏Space显示。
_SCREENSAVER_LEVEL = 1000

# 窗口位置需避开 utils.py 的屏幕探测区域:
#   check_stage() 探测像素 (316,32) 和 (156,35)
#   get_map() 截取小地图区域 [1600,20,1900,320] (右上角)
#   abandon_game() 点击 (307,32)
# 左上角、顶部往下200px, 落在这些区域下方, 留出安全边距.
_WIDTH, _HEIGHT = 300, 132
_LEFT, _TOP_OFFSET = 20, 200

# 布局(像素, 从上往下): 橙色标题条 + 奶白正文卡片, 外圈一道橙色描边.
_BORDER = 2
_HEADER_H = 26
_PAD = 10

# 配色. 实测过暗色半透明底(#1e1e1e)会跟游戏画面糊成一片肉眼看不见 —— 所以
# 保留亮橙作为整个窗口的"外框 + 标题条"(任意游戏背景上都扎眼), 正文换成
# 奶白底深字, 比整片橙底上的黑字好读, 而且能给状态上色.
_ORANGE_HEX = "#ff9900"
_BODY_HEX = "#fff8ec"
_INK_HEX = "#1f1f1f"        # 正文
_INK_DIM_HEX = "#5f5a52"    # 次要文字(计时 / 消息)
_ALPHA = 0.94

# 状态等级 -> 颜色. 一眼区分"正常干活 / 需要留意 / 出事了".
_LEVEL_HEX = {
    "ok": "#178a43",
    "warn": "#b86e00",
    "bad": "#d0312d",
    "idle": "#6b665e",
}
_LEVEL_PREFIXES = {
    "bad": ("出错", "已死亡"),
    "warn": ("卡住", "无法检测位置", "离开刷怪区域", "AFK", "换服务器", "重新开始", "菜单中"),
    "ok": ("寻路中", "移动中", "刷怪中", "完成", "进场路线", "启动"),
}

_CONFIRM_WIDTH, _CONFIRM_HEIGHT = 360, 170
_CONFIRM_MESSAGE = "florr.io 已就绪 —— 手动进入全屏(F11)后点击下方按钮开始"
_CONFIRM_BUTTON_LABEL = "开始运行"

# 比StatusOverlay的小状态框、_MacConfirmDialog/_WindowsConfirmDialog都大 ——
# 这个是给"连续检测不到玩家位置"这种需要用户立刻注意到的警告用的, 放屏幕
# 正中央, 字也大, 不能让人错过.
_WARNING_WIDTH, _WARNING_HEIGHT = 560, 200
_WARNING_TITLE = "⚠  需要你看一下"


def _hex_rgb(hex_str):
    """"#rrggbb" -> (r, g, b) 各 0~1, 给 AppKit 用."""
    h = hex_str.lstrip("#")
    return tuple(int(h[k:k + 2], 16) / 255 for k in (0, 2, 4))


def _state_level(state):
    """状态文字 -> "ok" / "warn" / "bad" / "idle". 按前缀认(main.py 里有
    "刷怪中(xx)" 这种带后缀的), 没认出来的一律 idle(灰)."""
    if not isinstance(state, str):
        return "idle"
    for level in ("bad", "warn", "ok"):
        if state.startswith(_LEVEL_PREFIXES[level]):
            return level
    return "idle"


def _hud_view(state, start, state_since, now):
    """把内部状态算成要显示的几段文字(纯函数, Mac/Windows 两版共用, 好单测)."""
    pos, target = state.get("pos"), state.get("target")
    if pos is None and target is None:
        loc = "位置 -"
    elif target is None:
        loc = f"位置 {_format_pos(pos)}"
    else:
        loc = f"位置 {_format_pos(pos)}  →  目标 {_format_pos(target)}"
    msg = state.get("message")
    return {
        "state": state.get("state") or "-",
        "level": _state_level(state.get("state")),
        "loc": loc,
        "message": "" if msg in (None, "-") else str(msg),
        "total": f"已运行 {_format_elapsed(now - start)}",
        "in_state": f"本状态 {_format_elapsed(now - state_since)}",
    }


def _advance_state(current, since, now, **fields):
    """_merge_state + 状态文字真的变了才重置"本状态"计时. 返回 (new_state, since).
    main.py 同一个状态会连续 update 很多次(移动中每步一次), 不能每次都清零."""
    new = _merge_state(current, **fields)
    if new.get("state") != current.get("state"):
        since = now
    return new, since


if Foundation is not None:
    class _ConfirmButtonTarget(Foundation.NSObject):
        """桥接NSButton点击事件回Python回调. 必须真的subclass NSObject, 类
        定义本身就引用了Foundation —— 所以这个class只能在Foundation可用时
        定义(Windows上pyobjc压根没装, Foundation是None, 定义这个class会在
        import overlay.py时就报AttributeError, 必须用if守住, 不能让整个模块
        导入失败, 拖累Windows上本来能正常工作的_WindowsOverlay)."""

        def setCallback_(self, callback):
            self._callback = callback

        def buttonClicked_(self, sender):
            if getattr(self, "_callback", None) is not None:
                self._callback()
else:
    _ConfirmButtonTarget = None


def _mac_overlay_window(rect, alpha):
    """无边框、橙色底(= 外框+标题条)、跨 Space 置顶的 NSWindow. HUD / 警告 /
    确认弹窗三处共用."""
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False,
    )
    window.setLevel_(_SCREENSAVER_LEVEL)
    window.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        | AppKit.NSWindowCollectionBehaviorStationary
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(_ns_color(_ORANGE_HEX, alpha))
    return window


def _ns_color(hex_str, alpha=1.0):
    r, g, b = _hex_rgb(hex_str)
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, alpha)


def _cg_color(hex_str):
    r, g, b = _hex_rgb(hex_str)
    if Quartz is not None:
        return Quartz.CGColorCreateGenericRGB(r, g, b, 1.0)
    return _ns_color(hex_str).CGColor()


def _ns_fill(frame, hex_str):
    """一块纯色矩形 NSView(layer 背景), 当标题条 / 正文卡片用."""
    v = AppKit.NSView.alloc().initWithFrame_(frame)
    v.setWantsLayer_(True)
    v.layer().setBackgroundColor_(_cg_color(hex_str))
    return v


def _ns_label(frame, text, size, color_hex, bold=False, align=None, wraps=False):
    label = AppKit.NSTextField.alloc().initWithFrame_(frame)
    label.setStringValue_(text)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setFont_((AppKit.NSFont.boldSystemFontOfSize_ if bold
                    else AppKit.NSFont.systemFontOfSize_)(size))
    label.setTextColor_(_ns_color(color_hex))
    if align is not None:
        label.setAlignment_(align)
    if wraps:
        # 默认NSTextField单行截断; 要换行必须显式开这两个.
        label.cell().setWraps_(True)
        label.setUsesSingleLineMode_(False)
    else:
        label.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    return label


class StatusOverlay:
    _FIELDS = ("state", "pos", "target", "message")

    def __init__(self):
        self._start = time.time()
        self._state_since = self._start
        self._state = {"state": "-", "pos": None, "target": None, "message": "-"}
        # 一旦update/close在窗口没了之后抛异常, 就锁死后续调用为空操作, 绝不再炸主程序.
        self._dead = False
        # 警告弹窗惰性建一次, 后续show_warning/hide_warning只切换显示/隐藏 —— 跟
        # StatusOverlay主窗口本身"建一次、update多次"是同一个模式.
        self._warning_window = None
        self._warning_label = None

        app = AppKit.NSApplication.sharedApplication()
        # Accessory: 不占Dock图标、不抢应用切换的焦点, 纯后台悬浮窗.
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self._app = app

        screen_frame = AppKit.NSScreen.mainScreen().frame()
        screen_height = screen_frame.size.height
        y_origin = screen_height - _TOP_OFFSET - _HEIGHT
        rect = Foundation.NSMakeRect(_LEFT, y_origin, _WIDTH, _HEIGHT)

        window = _mac_overlay_window(rect, _ALPHA)
        # 悬浮窗绝不能挡鼠标点击/抢键盘焦点 —— main.py靠鼠标位置和空格键控制游戏,
        # 焦点被抢走这些输入就发不到游戏里了. 只用orderFrontRegardless()显示,
        # 不调用makeKeyAndOrderFront_/activateIgnoringOtherApps_.
        window.setIgnoresMouseEvents_(True)
        self._window = window
        self._build(window.contentView())

        window.orderFrontRegardless()
        self._pump_events()

    def _build(self, content):
        """Cocoa 坐标原点在左下角 —— 下面的 y 都是从窗口底往上量."""
        W, H, B, P = _WIDTH, _HEIGHT, _BORDER, _PAD
        head_y = H - B - _HEADER_H
        content.addSubview_(_ns_fill(Foundation.NSMakeRect(B, B, W - 2 * B, head_y - B),
                                     _BODY_HEX))
        content.addSubview_(_ns_label(Foundation.NSMakeRect(P, head_y + 5, 170, 17),
                                      "florr auto-pathing", 12, _INK_HEX, bold=True))
        self._total_label = _ns_label(
            Foundation.NSMakeRect(W - P - 110, head_y + 6, 110, 16), "已运行 00:00", 11,
            _INK_HEX, align=AppKit.NSTextAlignmentRight)
        content.addSubview_(self._total_label)

        body_top = head_y - 8
        self._dot_label = _ns_label(Foundation.NSMakeRect(P, body_top - 22, 16, 20),
                                    "●", 13, _LEVEL_HEX["idle"])
        self._state_label = _ns_label(Foundation.NSMakeRect(P + 16, body_top - 22, 170, 20),
                                      "-", 15, _LEVEL_HEX["idle"], bold=True)
        self._in_state_label = _ns_label(
            Foundation.NSMakeRect(W - P - 110, body_top - 20, 110, 16), "本状态 00:00", 11,
            _INK_DIM_HEX, align=AppKit.NSTextAlignmentRight)
        self._loc_label = _ns_label(Foundation.NSMakeRect(P, body_top - 42, W - 2 * P, 17),
                                    "位置 -", 12, _INK_HEX)
        self._msg_label = _ns_label(Foundation.NSMakeRect(P, B + 4, W - 2 * P, 34),
                                    "", 11, _INK_DIM_HEX, wraps=True)
        for v in (self._dot_label, self._state_label, self._in_state_label,
                  self._loc_label, self._msg_label):
            content.addSubview_(v)

    def _pump_events(self):
        """非阻塞地把Cocoa事件循环转几圈, 让刚设置的内容真正画到屏幕上.

        没有跑常规的NSApp.run()主循环(跟main.py现有的同步while循环轮询模型
        保持一致, 不引入线程), 所以每次update()都要手动拉一下事件循环。
        """
        while True:
            event = self._app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                AppKit.NSEventMaskAny,
                Foundation.NSDate.dateWithTimeIntervalSinceNow_(0),
                AppKit.NSDefaultRunLoopMode,
                True,
            )
            if event is None:
                break
            self._app.sendEvent_(event)

    def update(self, state=None, pos=None, target=None, message=None):
        if self._dead:
            return None
        try:
            now = time.time()
            self._state, self._state_since = _advance_state(
                self._state, self._state_since, now,
                state=state, pos=pos, target=target, message=message,
            )
            v = _hud_view(self._state, self._start, self._state_since, now)
            color = _ns_color(_LEVEL_HEX[v["level"]])
            self._dot_label.setTextColor_(color)
            self._state_label.setTextColor_(color)
            self._state_label.setStringValue_(v["state"])
            self._in_state_label.setStringValue_(v["in_state"])
            self._total_label.setStringValue_(v["total"])
            self._loc_label.setStringValue_(v["loc"])
            self._msg_label.setStringValue_(v["message"])
            self._pump_events()
        except Exception:
            self._dead = True
        return None

    def show_warning(self, message):
        """屏幕正中央弹一个大号警告(比如"连续多次检测不到玩家位置") —— 跟主HUD
        一样点击穿透/不抢焦点(main.py这时候pyautogui还在正常操作鼠标键盘),
        窗口只建一次, 重复调用只是更新文字+重新显示."""
        if self._dead:
            return None
        try:
            if self._warning_window is None:
                screen_frame = AppKit.NSScreen.mainScreen().frame()
                x = (screen_frame.size.width - _WARNING_WIDTH) / 2
                y = (screen_frame.size.height - _WARNING_HEIGHT) / 2
                rect = Foundation.NSMakeRect(x, y, _WARNING_WIDTH, _WARNING_HEIGHT)
                window = _mac_overlay_window(rect, 0.97)
                window.setIgnoresMouseEvents_(True)
                content = window.contentView()
                W, H, B = _WARNING_WIDTH, _WARNING_HEIGHT, 3
                content.addSubview_(_ns_fill(Foundation.NSMakeRect(B, B, W - 2 * B, H - 2 * B - 40),
                                             _BODY_HEX))
                content.addSubview_(_ns_label(Foundation.NSMakeRect(16, H - 32, W - 32, 22),
                                              _WARNING_TITLE, 15, _INK_HEX, bold=True))
                label = _ns_label(Foundation.NSMakeRect(24, 20, W - 48, H - 80), "", 18,
                                  _LEVEL_HEX["bad"], bold=True,
                                  align=AppKit.NSTextAlignmentCenter, wraps=True)
                content.addSubview_(label)

                self._warning_window = window
                self._warning_label = label

            self._warning_label.setStringValue_(message)
            self._warning_window.orderFrontRegardless()
            self._pump_events()
        except Exception:
            self._dead = True
        return None

    def hide_warning(self):
        if self._dead or self._warning_window is None:
            return None
        try:
            self._warning_window.orderOut_(None)
            self._pump_events()
        except Exception:
            self._dead = True
        return None


    def close(self):
        if self._dead:
            return None
        try:
            self._window.close()
            if self._warning_window is not None:
                self._warning_window.close()
            self._dead = True
        except Exception:
            self._dead = True
        return None


class _MacConfirmDialog:
    """真能点击的确认弹窗 —— 跟StatusOverlay不同, 不设ignoresMouseEvents_(那个
    是给"不能抢游戏焦点"的状态HUD用的; 这个就是要能点). 复用StatusOverlay已经
    验证过的跨Space技巧(screensaver层级+collectionBehavior), 保证florr.io进了
    原生全屏Space之后这个弹窗依然显示在最上层、能点."""

    def __init__(self):
        self._confirmed = False

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self._app = app

        screen_frame = AppKit.NSScreen.mainScreen().frame()
        x = (screen_frame.size.width - _CONFIRM_WIDTH) / 2
        y = (screen_frame.size.height - _CONFIRM_HEIGHT) / 2
        rect = Foundation.NSMakeRect(x, y, _CONFIRM_WIDTH, _CONFIRM_HEIGHT)

        window = _mac_overlay_window(rect, 0.97)
        self._window = window

        content = window.contentView()
        W, H, B = _CONFIRM_WIDTH, _CONFIRM_HEIGHT, 3
        content.addSubview_(_ns_fill(Foundation.NSMakeRect(B, B, W - 2 * B, H - 2 * B - 30),
                                     _BODY_HEX))
        content.addSubview_(_ns_label(Foundation.NSMakeRect(14, H - 25, W - 28, 18),
                                      "florr auto-pathing", 13, _INK_HEX, bold=True))
        label = _ns_label(Foundation.NSMakeRect(20, 62, W - 40, 60), _CONFIRM_MESSAGE, 14,
                          _INK_HEX, align=AppKit.NSTextAlignmentCenter, wraps=True)
        content.addSubview_(label)

        # 保留target的强引用(self._target) —— PyObjC不会自动帮Python侧保住这个
        # 对象, target提前被GC掉的话setTarget_指向的就是悬空对象, 点击不会
        # 触发任何反应(没有异常, 静默不响应, 很难查).
        self._target = _ConfirmButtonTarget.alloc().init()
        self._target.setCallback_(self._on_confirmed)

        button = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect((_CONFIRM_WIDTH - 160) / 2, 16, 160, 34)
        )
        button.setTitle_(_CONFIRM_BUTTON_LABEL)
        button.setBezelStyle_(AppKit.NSBezelStyleRounded)
        button.setKeyEquivalent_("\r")   # 回车也能确认; 默认按钮会被系统画成强调色
        button.setTarget_(self._target)
        button.setAction_("buttonClicked:")
        content.addSubview_(button)
        self._button = button

        window.orderFrontRegardless()
        self._pump_events()

    def _on_confirmed(self):
        self._confirmed = True

    def _pump_events(self):
        while True:
            event = self._app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                AppKit.NSEventMaskAny,
                Foundation.NSDate.dateWithTimeIntervalSinceNow_(0),
                AppKit.NSDefaultRunLoopMode,
                True,
            )
            if event is None:
                break
            self._app.sendEvent_(event)

    def wait_for_confirm(self):
        while not self._confirmed:
            self._pump_events()
            time.sleep(0.05)
        self._window.close()


# --- Win32扩展窗口样式(GWL_EXSTYLE) + SetWindowPos/SetLayeredWindowAttributes常量 ---
_WIN_GWL_EXSTYLE = -20
_WIN_WS_EX_LAYERED = 0x00080000
_WIN_WS_EX_TRANSPARENT = 0x00000020
_WIN_WS_EX_NOACTIVATE = 0x08000000
_WIN_WS_EX_TOOLWINDOW = 0x00000080
_WIN_LWA_ALPHA = 0x2
_WIN_HWND_TOPMOST = -1
_WIN_SWP_NOMOVE = 0x0002
_WIN_SWP_NOSIZE = 0x0001
_WIN_SWP_NOACTIVATE = 0x0010
_WIN_SWP_SHOWWINDOW = 0x0040
_WIN_SWP_FRAMECHANGED = 0x0020

_ALPHA_BYTE = int(_ALPHA * 255)  # 跟mac版透明度对齐
_TK_FONT = "Microsoft YaHei UI"  # Segoe UI 没有中文字形, 中文会回落成别的字体、粗细不一


def _setup_user32():
    """给用到的user32函数声明正确的argtypes/restype再返回user32.

    不声明的话ctypes默认按C int(32位)编组参数, 但64位Windows上HWND是64位
    指针 —— 不声明时GetParent/SetWindowLongW/SetWindowPos拿到的hwnd会被
    截断成坏句柄, 静默操作在错误的(或无效的)窗口上, 不抛异常也看不到任何
    效果。这就是"没有警告日志但悬浮窗压根不显示"的根因。
    """
    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = [ctypes.c_void_p]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    user32.SetLayeredWindowAttributes.restype = ctypes.c_int
    user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = ctypes.c_int
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    return user32


def _apply_clickthrough_topmost(user32, hwnd):
    """给hwnd叠加点击穿透/不抢焦点/不进任务栏的扩展样式, 再强制置顶+设置透明度+
    显示. _WindowsOverlay的主HUD窗口和警告弹窗共用同一套win32调用(同一件事,
    不该写两遍) —— 谁的hwnd传进来就处理谁."""
    style = user32.GetWindowLongW(hwnd, _WIN_GWL_EXSTYLE)
    style |= (
        _WIN_WS_EX_LAYERED | _WIN_WS_EX_TRANSPARENT
        | _WIN_WS_EX_NOACTIVATE | _WIN_WS_EX_TOOLWINDOW
    )
    user32.SetWindowLongW(hwnd, _WIN_GWL_EXSTYLE, style)
    user32.SetLayeredWindowAttributes(hwnd, 0, _ALPHA_BYTE, _WIN_LWA_ALPHA)
    # SWP_FRAMECHANGED: 改完GWL_EXSTYLE后必须带这个flag, 否则新样式不会
    # 立即生效渲染(微软文档明确要求); 顺带把窗口顶到最上层+确保显示出来.
    user32.SetWindowPos(
        hwnd, _WIN_HWND_TOPMOST, 0, 0, 0, 0,
        _WIN_SWP_NOMOVE | _WIN_SWP_NOSIZE | _WIN_SWP_NOACTIVATE
        | _WIN_SWP_SHOWWINDOW | _WIN_SWP_FRAMECHANGED,
    )


def _tk_font(size, bold=False):
    return (_TK_FONT, size, "bold") if bold else (_TK_FONT, size)


def _build_tk_hud(root):
    """在 root(已设好尺寸) 上摆 HUD 控件: 橙色外框/标题条 + 奶白正文. 只用
    tkinter, 不碰 win32 —— 布局可以脱离 Windows 单独渲染检查. 返回控件表."""
    W, H, B, P = _WIDTH, _HEIGHT, _BORDER, _PAD
    root.configure(bg=_ORANGE_HEX)
    tk.Label(root, text="florr auto-pathing", bg=_ORANGE_HEX, fg=_INK_HEX,
             font=_tk_font(10, True), anchor="w").place(
        x=P, y=B + 2, width=170, height=_HEADER_H - 4)
    w = {}
    w["total"] = tk.Label(root, text="已运行 00:00", bg=_ORANGE_HEX, fg=_INK_HEX,
                          font=_tk_font(9), anchor="e")
    w["total"].place(x=W - P - 110, y=B + 2, width=110, height=_HEADER_H - 4)

    body = tk.Frame(root, bg=_BODY_HEX)
    body.place(x=B, y=B + _HEADER_H, width=W - 2 * B, height=H - 2 * B - _HEADER_H)
    bw = W - 2 * B
    w["dot"] = tk.Label(body, text="●", bg=_BODY_HEX, fg=_LEVEL_HEX["idle"],
                        font=_tk_font(11), anchor="w")
    w["dot"].place(x=P - B, y=6, width=16, height=24)
    w["state"] = tk.Label(body, text="-", bg=_BODY_HEX, fg=_LEVEL_HEX["idle"],
                          font=_tk_font(13, True), anchor="w")
    w["state"].place(x=P - B + 16, y=6, width=170, height=24)
    w["in_state"] = tk.Label(body, text="本状态 00:00", bg=_BODY_HEX, fg=_INK_DIM_HEX,
                             font=_tk_font(9), anchor="e")
    w["in_state"].place(x=bw - P + B - 110, y=8, width=110, height=20)
    w["loc"] = tk.Label(body, text="位置 -", bg=_BODY_HEX, fg=_INK_HEX,
                        font=_tk_font(10), anchor="w")
    w["loc"].place(x=P - B, y=32, width=bw - 2 * (P - B), height=20)
    # 消息最长两行自动换行(以前单行定宽, 长消息后半截看不到).
    w["message"] = tk.Label(body, text="", bg=_BODY_HEX, fg=_INK_DIM_HEX,
                            font=_tk_font(9), anchor="nw", justify="left",
                            wraplength=bw - 2 * (P - B))
    w["message"].place(x=P - B, y=54, width=bw - 2 * (P - B), height=44)
    return w


def _render_tk_hud(w, view):
    color = _LEVEL_HEX[view["level"]]
    w["dot"].configure(fg=color)
    w["state"].configure(text=view["state"], fg=color)
    w["in_state"].configure(text=view["in_state"])
    w["total"].configure(text=view["total"])
    w["loc"].configure(text=view["loc"])
    w["message"].configure(text=view["message"])


def _build_tk_card(win, title, width, height):
    """警告 / 确认弹窗的共同外观: 橙色外框 + 标题条 + 奶白正文区. 返回正文 Frame."""
    win.configure(bg=_ORANGE_HEX)
    tk.Label(win, text=title, bg=_ORANGE_HEX, fg=_INK_HEX, font=_tk_font(11, True),
             anchor="w").place(x=14, y=4, width=width - 28, height=26)
    body = tk.Frame(win, bg=_BODY_HEX)
    body.place(x=3, y=32, width=width - 6, height=height - 35)
    return body


class _WindowsOverlay:
    """Windows版悬浮窗 — tkinter无边框窗口 + 纯win32 API做置顶/透明度/点击穿透.

    florr.io在Windows上一般是浏览器"F11"式无边框全屏(不是独占全屏的游戏),
    常规-topmost窗口就能盖上去, 不需要macOS那套跨Space的screensaver级别技巧。
    置顶/透明度/点击穿透全部用SetWindowPos/SetLayeredWindowAttributes/
    SetWindowLongW直接做, 不用tkinter自己的-topmost/-alpha —— 两边都改同一个
    底层窗口属性会打架(改GWL_EXSTYLE不配套重发SetLayeredWindowAttributes,
    实测会导致窗口整个变不可见), 全交给win32 API一条路管到底更可靠。
    另需两件事保证main.py的输入不被打断:
      - WS_EX_TRANSPARENT: 悬浮窗完全不吃鼠标点击, 点击穿透到游戏画面.
      - WS_EX_NOACTIVATE:  悬浮窗永不抢键盘焦点(main.py靠空格键控制游戏).
    """

    _FIELDS = ("state", "pos", "target", "message")

    def __init__(self):
        self._start = time.time()
        self._state_since = self._start
        self._state = {"state": "-", "pos": None, "target": None, "message": "-"}
        # 一旦update/close在窗口没了之后抛异常, 就锁死后续调用为空操作, 绝不再炸主程序.
        self._dead = False
        # 警告弹窗惰性建一次(Toplevel挂在主HUD的root下, 不是独立的tk.Tk() ——
        # 一个进程里开两个Tcl解释器是自找麻烦), 后续show_warning/hide_warning
        # 只切换显示/隐藏.
        self._warning_window = None
        self._warning_label = None
        self._warning_hwnd = None

        root = tk.Tk()
        root.overrideredirect(True)  # 无标题栏/边框, 也不进Alt+Tab切换
        root.geometry(f"{_WIDTH}x{_HEIGHT}+{_LEFT}+{_TOP_OFFSET}")
        root.resizable(False, False)
        # 先落一次事件循环, 确保win32那边真正建出HWND, 再去取winfo_id()才有效.
        root.update_idletasks()
        self._root = root

        self._user32 = _setup_user32()
        # winfo_id()是绘图表面的句柄, 其父窗口才是真正被DWM管理的顶层HWND
        # (overrideredirect窗口下两者常常是同一个, GetParent失败就兜底用
        # winfo_id()本身, 双保险不炸)。
        hwnd = self._user32.GetParent(root.winfo_id())
        self._hwnd = hwnd if hwnd else root.winfo_id()
        self._apply_window_styles()

        self._widgets = _build_tk_hud(root)

        root.update_idletasks()
        root.update()

    def _apply_window_styles(self):
        """叠加点击穿透/不抢焦点/不进任务栏的扩展样式, 再强制置顶+设置透明度+显示."""
        _apply_clickthrough_topmost(self._user32, self._hwnd)

    def update(self, state=None, pos=None, target=None, message=None):
        if self._dead:
            return None
        try:
            now = time.time()
            self._state, self._state_since = _advance_state(
                self._state, self._state_since, now,
                state=state, pos=pos, target=target, message=message,
            )
            _render_tk_hud(self._widgets,
                           _hud_view(self._state, self._start, self._state_since, now))
            # 部分游戏/浏览器切换全屏时会把自己重新拉到最顶层, 每次update都
            # 重新断言一次置顶, 防止悬浮窗被压到游戏画面下面.
            self._user32.SetWindowPos(
                self._hwnd, _WIN_HWND_TOPMOST, 0, 0, 0, 0,
                _WIN_SWP_NOMOVE | _WIN_SWP_NOSIZE | _WIN_SWP_NOACTIVATE,
            )
            self._root.update_idletasks()
            self._root.update()
        except Exception:
            self._dead = True
        return None

    def show_warning(self, message):
        """屏幕正中央弹一个大号警告. Toplevel挂在主HUD的root下(共用同一个Tcl
        解释器), 跟主HUD一样点击穿透/不抢焦点, 窗口只建一次, 重复调用只更新
        文字+重新显示+重新断言置顶."""
        if self._dead:
            return None
        try:
            if self._warning_window is None:
                win = tk.Toplevel(self._root)
                win.overrideredirect(True)

                screen_w = self._root.winfo_screenwidth()
                screen_h = self._root.winfo_screenheight()
                x = (screen_w - _WARNING_WIDTH) // 2
                y = (screen_h - _WARNING_HEIGHT) // 2
                win.geometry(f"{_WARNING_WIDTH}x{_WARNING_HEIGHT}+{x}+{y}")
                win.resizable(False, False)
                win.update_idletasks()

                hwnd = self._user32.GetParent(win.winfo_id())
                self._warning_hwnd = hwnd if hwnd else win.winfo_id()

                body = _build_tk_card(win, _WARNING_TITLE, _WARNING_WIDTH, _WARNING_HEIGHT)
                label = tk.Label(
                    body, bg=_BODY_HEX, fg=_LEVEL_HEX["bad"], font=_tk_font(15, True),
                    wraplength=_WARNING_WIDTH - 60, justify="center",
                )
                label.pack(expand=True, fill="both", padx=20, pady=12)

                self._warning_window = win
                self._warning_label = label

            self._warning_label.configure(text=message)
            self._warning_window.deiconify()   # hide_warning() withdraw 过的话要重新显示
            _apply_clickthrough_topmost(self._user32, self._warning_hwnd)
            self._root.update_idletasks()
            self._root.update()
        except Exception:
            self._dead = True
        return None

    def hide_warning(self):
        if self._dead or self._warning_window is None:
            return None
        try:
            self._warning_window.withdraw()
            self._root.update_idletasks()
            self._root.update()
        except Exception:
            self._dead = True
        return None


    def close(self):
        if self._dead:
            return None
        try:
            self._root.destroy()
            self._dead = True
        except Exception:
            self._dead = True
        return None


class _WindowsConfirmDialog:
    """真能点击的确认弹窗. Windows不需要mac那套跨Space hack(F11全屏不换Space,
    见_WindowsOverlay类文档开头那段说明), 也不需要_WindowsOverlay的win32点击
    穿透样式 —— 这个窗口就是要能点的, 普通tkinter -topmost就够."""

    def __init__(self):
        self._confirmed = False

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)

        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = (screen_w - _CONFIRM_WIDTH) // 2
        y = (screen_h - _CONFIRM_HEIGHT) // 2
        root.geometry(f"{_CONFIRM_WIDTH}x{_CONFIRM_HEIGHT}+{x}+{y}")
        self._root = root

        body = _build_tk_card(root, "florr auto-pathing", _CONFIRM_WIDTH, _CONFIRM_HEIGHT)
        tk.Label(
            body, text=_CONFIRM_MESSAGE, bg=_BODY_HEX, fg=_INK_HEX, font=_tk_font(11),
            wraplength=_CONFIRM_WIDTH - 50, justify="center",
        ).pack(pady=(16, 10))

        # 扁平深色按钮(系统默认灰按钮在橙/白卡片上很突兀), 回车也能确认.
        button = tk.Button(
            body, text=_CONFIRM_BUTTON_LABEL, command=self._on_confirmed,
            font=_tk_font(11, True), bg=_INK_HEX, fg="white",
            activebackground="#3a3a3a", activeforeground="white",
            relief="flat", bd=0, padx=24, pady=6, cursor="hand2",
        )
        button.pack(pady=(0, 14))
        root.bind("<Return>", lambda _e: self._on_confirmed())
        self._button = button

        root.update_idletasks()
        root.update()

    def _on_confirmed(self):
        self._confirmed = True

    def wait_for_confirm(self):
        while not self._confirmed:
            self._root.update_idletasks()
            self._root.update()
            time.sleep(0.05)
        self._root.destroy()


def create_overlay():
    """建悬浮窗, 建不起来(缺依赖/没display)就退化成空壳, 不炸主程序."""
    if _IS_MACOS:
        if AppKit is None:
            print("⚠️ 悬浮窗启动失败: pyobjc(AppKit) 不可用", file=sys.stderr)
            return _NullOverlay()
        try:
            return StatusOverlay()
        except Exception as e:
            print(f"⚠️ 悬浮窗启动失败: {e}", file=sys.stderr)
            return _NullOverlay()

    if _IS_WINDOWS:
        if tk is None or ctypes is None:
            print("⚠️ 悬浮窗启动失败: tkinter/ctypes 不可用", file=sys.stderr)
            return _NullOverlay()
        try:
            return _WindowsOverlay()
        except Exception as e:
            print(f"⚠️ 悬浮窗启动失败: {e}", file=sys.stderr)
            return _NullOverlay()

    print(f"⚠️ 悬浮窗启动失败: 不支持的平台 {sys.platform}", file=sys.stderr)
    return _NullOverlay()


def _console_fallback(reason):
    print(f"⚠️ 确认弹窗启动失败: {reason}, 改用控制台确认", file=sys.stderr)
    input("请手动进入全屏(F11)后按回车继续: ")


def show_fullscreen_confirm():
    """弹一个真能点击的确认对话框, 阻塞到用户点击"开始运行"为止. 悬浮窗建不
    出来就退化成控制台input()确认, 绝不崩主程序 —— 跟create_overlay()的
    _NullOverlay降级哲学一致."""
    if _IS_MACOS:
        if AppKit is None:
            return _console_fallback("pyobjc(AppKit) 不可用")
        try:
            print("请在弹窗里手动进入全屏(F11)后点击「开始运行」")
            _MacConfirmDialog().wait_for_confirm()
            return
        except Exception as e:
            return _console_fallback(str(e))

    if _IS_WINDOWS:
        if tk is None:
            return _console_fallback("tkinter 不可用")
        try:
            print("请在弹窗里手动进入全屏(F11)后点击「开始运行」")
            _WindowsConfirmDialog().wait_for_confirm()
            return
        except Exception as e:
            return _console_fallback(str(e))

    return _console_fallback(f"不支持的平台 {sys.platform}")


if __name__ == "__main__":
    # 手动烟雾测试: 开窗, 循环几个假状态, 肉眼确认渲染/位置/跨Space显示对不对.
    demo = create_overlay()
    fake_states = [
        {"state": "寻路中", "pos": (53, 144), "target": (14, 45), "message": "规划路径..."},
        {"state": "移动中", "pos": (30, 90), "target": (14, 45), "message": "移动到 (30, 90)"},
        {"state": "卡住", "pos": (30, 90), "target": (14, 45), "message": "移动受阻"},
        {"state": "完成", "pos": (14, 45), "target": (14, 45), "message": "已到达目标区域"},
    ]
    for fake in fake_states:
        demo.update(**fake)
        time.sleep(2)
    demo.close()
