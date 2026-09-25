"""时块调度 UI: 编辑器(CTkToplevel) + 列表折叠行 + 校验纯函数 + Tooltip.

纯函数(_safe_dirname / validate_block / block_to_active)不碰 tk, 单测直接调.
控件类(TimeBlockEditor / ScheduleList)才 import customtkinter —— 放在文件下半段.
"""
import re
import tkinter as tk

import customtkinter as ctk

import app_config
import gui_theme as theme

WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")

_ACTIVE_KEYS = app_config._ACTIVE_KEYS

# loadout 切换和弦: 每个字段 = 开关 + 修饰键下拉 + 数字下拉.
_SWAP_MOD_LABELS = {"none": "无", "k": "k", "l": "l"}
_SWAP_MOD_FROM_LABEL = {v: k for k, v in _SWAP_MOD_LABELS.items()}
_SWAP_DIGIT_VALUES = list("1234567890")
_coerce_swap_obj = app_config._coerce_swap_obj   # 单一真源


# 目录名: 保留 \w(含汉字)和连字符, 其余替换成 _, 首尾 _ 去掉.
_SAFE_DIR_RE = re.compile(r"[^\w\-]", re.UNICODE)


def _safe_dirname(name):
    if not isinstance(name, str):
        return ""
    cleaned = _SAFE_DIR_RE.sub("_", name.strip())
    return cleaned.strip("_")


def block_to_active(block):
    """一个时块 -> worker 只读的 active 切片(7 个刷怪参数, 数值规整)."""
    loc = block["location"]
    area = block["farming_area"]
    return {
        "map": block["map"],
        "location": [int(loc[0]), int(loc[1])],
        "farming_area": [[int(area[0][0]), int(area[0][1])],
                         [int(area[1][0]), int(area[1][1])]],
        "farming_duration": int(block["farming_duration"]),
        "consecutive_short_round_limit": int(block["consecutive_short_round_limit"]),
        "enemy_ai_enabled": bool(block["enemy_ai_enabled"]),
        "auto_switch_server": bool(block["auto_switch_server"]),
        "invert_attack": bool(block.get("invert_attack", True)),
        "invert_defense": bool(block.get("invert_defense", False)),
        "enter_game_swap": _coerce_swap_obj(block.get("enter_game_swap")),
        "reach_area_swap": _coerce_swap_obj(block.get("reach_area_swap")),
    }


def _positive_int(v):
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def validate_block(block, others):
    """返回错误中文串, 或 None 表示通过. others 里跟 block 同 id 的会被跳过."""
    if not block.get("days"):
        return "至少勾一个星期"
    start, end = block.get("start"), block.get("end")
    if not (app_config._valid_time(start) and app_config._valid_time(end)):
        return "时间格式要是 HH:MM"
    if start == end and start != "00:00":
        return "起止时间不能相同(全天请填 00:00–00:00)"
    if block.get("map") not in app_config._GUI_ENABLED_MAPS:
        return "海洋暂不可用, 请选沙漠或蚁狱"
    if not block.get("location") and not block.get("farming_area"):
        return "在地图上点个目标点, 或框个刷怪区"
    if not _positive_int(block.get("farming_duration")):
        return "刷怪时长要是正整数"
    if not _positive_int(block.get("consecutive_short_round_limit")):
        return "连续短局阈值要是正整数"
    for o in others:
        if o.get("id") == block.get("id"):
            continue
        if app_config.blocks_overlap(block, o):
            return f"跟时块 {o.get('id')} 时间重叠"
    return None


def _map_radio_state(map_name):
    """时块编辑器地图 radio 的 tk state: 不在 _GUI_ENABLED_MAPS 里的置灰."""
    return "normal" if map_name in app_config._GUI_ENABLED_MAPS else "disabled"


def _anthell_calibration_hint():
    """蚁狱没标定时会被 main._route_blocker 拦停 —— worker 一启动就退出。
    编辑器里提前提个醒, 不弹窗、不挡保存, 真正把关的还是 worker 自己。"""
    return ("蚁狱暂缺标定: maps/garden.png(跑 capture_map.py garden 生成)、"
            "传送点坐标 map_routes.ANTHELL_PORTAL —— 缺了 worker 启动即退出。")


def _anthell_blocker():
    """蚁狱现在能不能跑: 能跑返回 None, 否则返回 _anthell_calibration_hint().
    以前不管选没选蚁狱、标没标定都常驻显示那条警告 —— 标定早已完成, 那句话只会吓人."""
    import os
    import map_routes
    import gui_map_picker
    route = map_routes.route_for("anthell")
    if not route.is_calibrated():
        return _anthell_calibration_hint()
    for stage in route.stages:
        if not os.path.exists(os.path.join(gui_map_picker._MAP_DIR, f"{stage.map_name}.png")):
            return _anthell_calibration_hint()
    return None


def new_block_template(cfg):
    """新建时块的默认值. 索敌 / 换服默认开(canvas 解码识怪, 不需要模型文件)."""
    return {
        "id": fresh_block_id(cfg), "enabled": True, "days": [],
        "start": "09:00", "end": "12:00",
        "profile": cfg["profiles"][0]["alias"] if cfg.get("profiles") else "默认",
        "map": "desert", "location": None, "farming_area": None,
        "farming_duration": 300, "consecutive_short_round_limit": 2,
        "enemy_ai_enabled": True, "auto_switch_server": True,
        "invert_attack": True, "invert_defense": False,
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
    }


def fresh_block_id(cfg):
    n = 0
    for b in cfg.get("schedule", []):
        m = re.match(r"blk-(\d+)$", str(b.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return f"blk-{n + 1}"


def weekday_short(days):
    return "".join(WEEKDAY_LABELS[d] for d in sorted(days)) or "—"


def weekday_text(days):
    """列表里给人看的星期: 每天 / 工作日 / 周末 / 周一 三 五."""
    ds = sorted(set(days))
    if ds == list(range(7)):
        return "每天"
    if ds == list(range(5)):
        return "工作日"
    if ds == [5, 6]:
        return "周末"
    if not ds:
        return "未选星期"
    return "周" + " ".join(WEEKDAY_LABELS[d] for d in ds)


# ─────────────────────────────── 控件层 ───────────────────────────────

class _Tooltip:
    """悬停解释. CustomTkinter 没内置, 纯 tkinter 手搓: <Enter> 后 400ms 弹一个
    无边框 Toplevel, <Leave> / 点击销毁."""

    def __init__(self, widget, text):
        self._w = widget
        self._text = text
        self._tip = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._job = self._w.after(400, self._show)

    def _show(self):
        if self._tip is not None:
            return
        x = self._w.winfo_rootx() + 12
        y = self._w.winfo_rooty() + self._w.winfo_height() + 6
        self._tip = tk.Toplevel(self._w)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self._text, justify="left", wraplength=300,
                 bg=theme.SURFACE_HI, fg=theme.TEXT, relief="solid", borderwidth=1,
                 font=(theme.UI_FAMILY, 10), padx=10, pady=6).pack()

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _cancel(self):
        if self._job is not None:
            self._w.after_cancel(self._job)
            self._job = None


def _help_mark(master, text):
    """一个灰色的 ⓘ, 悬停出解释."""
    q = ctk.CTkLabel(master, text="ⓘ", text_color=theme.MUTED, font=theme.font(13),
                     cursor="question_arrow")
    _Tooltip(q, text)
    return q


def _fmt_pt(pt):
    return f"({int(pt[0])}, {int(pt[1])})"


class TimeBlockEditor(ctk.CTkToplevel):
    """一个时块的编辑窗. 非模态: 不 grab_set / 不 -topmost, 用户能最小化去干别的.
    保存前跑 validate_block, 失败红字不关窗. on_save(block_dict) 由调用方接。
    new_profile_cb(alias, on_ready) 由 App 注入 —— 处理"账号下拉里选＋新建"。"""

    _TIP_DURATION = ("一轮在刷怪区停留多少秒。也是『刷满』判定线 —— "
                     "一条命活过这个秒数才算这轮刷满。")
    _TIP_SHORT = ("连续这么多轮没撑到刷怪时长(被秒 / 到不了区), "
                  "且『自动换服务器』开着, 就自动跳服。")
    _NEW_PROFILE = "＋ 新建账号…"
    _LABEL_W = 72   # 表单左侧标签列宽

    def __init__(self, master, *, block, others, profiles, on_save, is_new=False):
        super().__init__(master, fg_color=theme.BG)
        self.title("新建时块" if is_new else f"编辑时块 · {block.get('id', '')}")
        theme.center_on(self, master, 640, 780)
        self.minsize(560, 480)
        self.resizable(True, True)
        self.transient(master)
        self._block = dict(block)
        self._others = others
        self._profiles = list(profiles)
        self._on_save = on_save
        self.new_profile_cb = None
        self._point = tuple(block["location"]) if block.get("location") else None
        self._area = ([tuple(block["farming_area"][0]), tuple(block["farming_area"][1])]
                      if block.get("farming_area") else None)
        self._build()
        self.bind("<Escape>", lambda _e: self.destroy())

    # ---- 布局小工具 ----
    def _section(self, title, sub=None):
        c = theme.card(self._body)
        c.pack(fill="x", padx=14, pady=(0, 12))
        theme.section_title(c, title, sub).pack(anchor="w", fill="x", padx=14, pady=(12, 8))
        inner = ctk.CTkFrame(c, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=(0, 12))
        return inner

    def _form_row(self, parent, label, pady=(0, 8)):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=pady)
        ctk.CTkLabel(row, text=label, width=self._LABEL_W, anchor="w",
                     font=theme.font(13), text_color=theme.MUTED).pack(side="left")
        return row

    def _build(self):
        # 字段多 —— 字段区放进滚动框, 按钮条 + 报错 pin 在窗口底部永远可见
        # (报错以前在滚动区最底下, 字段一多用户点"保存"没反应也看不到原因).
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", pady=(12, 0))
        self._body = body

        self._build_time()
        self._build_account_map()
        self._build_location()
        self._build_combat()
        self._build_server_and_loadout()

        # ---- 底栏 ----
        br = ctk.CTkFrame(self, fg_color=theme.SIDEBAR, corner_radius=0)
        br.grid(row=1, column=0, sticky="ew")
        br.grid_columnconfigure(0, weight=1)
        self._err = ctk.CTkLabel(br, text="", text_color="#ff6b6f", font=theme.font(13),
                                 anchor="w", justify="left", wraplength=380)
        self._err.grid(row=0, column=0, sticky="w", padx=16)
        theme.ghost_button(br, "取消", self.destroy, width=84, height=34).grid(
            row=0, column=1, padx=(0, 8), pady=12)
        theme.primary_button(br, "保存", self._save, width=96, height=34).grid(
            row=0, column=2, padx=(0, 16), pady=12)

    def _build_time(self):
        sec = self._section("时间", "跨午夜: 开始晚于结束(如 22:00–02:00); 全天: 00:00–00:00")
        row = self._form_row(sec, "星期")
        self._day_vars = []
        self._day_btns = []
        for i, lab in enumerate(WEEKDAY_LABELS):
            v = ctk.IntVar(value=1 if i in self._block.get("days", []) else 0)
            b = ctk.CTkButton(row, text=lab, width=36, height=32, corner_radius=16,
                              font=theme.font(13), border_width=1,
                              command=lambda k=i: self._toggle_day(k))
            b.pack(side="left", padx=(0, 5))
            self._day_vars.append(v)
            self._day_btns.append(b)
        self._paint_days()

        quick = ctk.CTkFrame(sec, fg_color="transparent")
        quick.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(quick, text="", width=self._LABEL_W).pack(side="left")
        for txt, days in (("工作日", range(5)), ("周末", (5, 6)), ("每天", range(7)),
                          ("清空", ())):
            ctk.CTkButton(quick, text=txt, width=52, height=24, font=theme.font(11),
                          fg_color="transparent", hover_color=theme.SURFACE_HI,
                          text_color=theme.ACCENT,
                          command=lambda d=tuple(days): self._set_days(d)).pack(
                side="left", padx=(0, 4))

        tr = self._form_row(sec, "时段", pady=(0, 0))
        self._start_e = ctk.CTkEntry(tr, width=76, placeholder_text="09:00",
                                     font=theme.font(13), justify="center")
        self._start_e.insert(0, self._block.get("start", ""))
        self._start_e.pack(side="left")
        ctk.CTkLabel(tr, text="  到  ", text_color=theme.MUTED,
                     font=theme.font(13)).pack(side="left")
        self._end_e = ctk.CTkEntry(tr, width=76, placeholder_text="12:00",
                                   font=theme.font(13), justify="center")
        self._end_e.insert(0, self._block.get("end", ""))
        self._end_e.pack(side="left")
        for e in (self._start_e, self._end_e):
            e.bind("<FocusOut>", lambda ev, w=e: self._normalize_entry(w), add="+")
        theme.hint(tr, "   可以只写 9 或 930, 会自动规整").pack(side="left")

    def _toggle_day(self, i):
        v = self._day_vars[i]
        v.set(0 if v.get() else 1)
        self._paint_days()

    def _set_days(self, days):
        for i, v in enumerate(self._day_vars):
            v.set(1 if i in days else 0)
        self._paint_days()

    def _paint_days(self):
        for v, b in zip(self._day_vars, self._day_btns):
            if v.get():
                b.configure(fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                            border_color=theme.ACCENT, text_color="white")
            else:
                b.configure(fg_color="transparent", hover_color=theme.SURFACE_HI,
                            border_color=theme.BORDER, text_color=theme.MUTED)

    def _build_account_map(self):
        sec = self._section("账号与地图")
        row = self._form_row(sec, "账号")
        vals = [p["alias"] for p in self._profiles] + [self._NEW_PROFILE]
        self._acct = ctk.CTkOptionMenu(row, values=vals, command=self._on_acct_pick,
                                       width=200, font=theme.font(13),
                                       dropdown_font=theme.font(13),
                                       fg_color=theme.SURFACE_HI,
                                       button_color=theme.BORDER,
                                       button_hover_color=theme.FAINT)
        self._acct.set(self._block.get("profile", vals[0]))
        self._acct_prev = self._acct.get()
        self._acct.pack(side="left")

        self._map = tk.StringVar(value=self._block.get("map", "desert"))
        map_row = self._form_row(sec, "地图", pady=(0, 0))
        for m in app_config._VALID_MAPS:
            state = _map_radio_state(m)
            text = theme.map_label(m)
            if state == "disabled":
                text += "(暂不可用)"
            ctk.CTkRadioButton(map_row, text=text, variable=self._map, value=m,
                               state=state, font=theme.font(13),
                               fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                               command=self._on_map_change).pack(side="left", padx=(0, 16))
        self._map_hint = theme.hint(sec, wraplength=520, text_color=theme.WARN)
        self._sync_map_hint()

    def _build_location(self):
        sec = self._section(
            "刷怪位置",
            "左键点一下 = 目标点(绿十字);左键拖动 = 刷怪区域(蓝框);滚轮缩放。二选一即可")
        from gui_map_picker import MapPicker
        self._picker = MapPicker(sec, on_point_change=self._on_point,
                                 on_area_change=self._on_area, fg_color=theme.LOG_BG,
                                 corner_radius=8)
        # 滚动框里不能 expand=True(会把整条滚动内容撑没边); 给个固定高度.
        self._picker.configure(height=320)
        self._picker.pack(fill="x", pady=(0, 8))
        try:
            self._picker.pack_propagate(False)
        except Exception:
            pass
        self._picker.load_map(self._map.get())
        self._picker.set_point(self._point)
        self._picker.set_area(self._area)

        rd = ctk.CTkFrame(sec, fg_color="transparent")
        rd.pack(fill="x", pady=(0, 8))
        self._loc_readout = ctk.CTkLabel(rd, text="", font=theme.mono(12),
                                         text_color=theme.TEXT, anchor="w")
        self._loc_readout.pack(side="left")
        theme.ghost_button(rd, "清除", self._clear_location, width=56, height=26).pack(
            side="right")
        self._sync_readout()

        dur = self._form_row(sec, "刷怪时长", pady=(0, 0))
        self._dur_e = ctk.CTkEntry(dur, width=76, font=theme.font(13), justify="center")
        self._dur_e.insert(0, str(self._block.get("farming_duration", 300)))
        self._dur_e.pack(side="left")
        ctk.CTkLabel(dur, text=" 秒 ", text_color=theme.MUTED,
                     font=theme.font(13)).pack(side="left")
        _help_mark(dur, self._TIP_DURATION).pack(side="left")

    def _build_combat(self):
        sec = self._section("战斗")

        self._enemy = theme.switch(sec, "索敌 AI(追击 / 先清青怪)")
        if self._block.get("enemy_ai_enabled", True):
            self._enemy.select()
        self._enemy.pack(anchor="w")
        self._enemy_hint = theme.hint(sec)
        self._enemy_hint.pack(anchor="w", padx=(46, 0), pady=(0, 8))

        inv = ctk.CTkFrame(sec, fg_color="transparent")
        inv.pack(fill="x")
        self._inv_attack = theme.switch(inv, "反转攻击键")
        if self._block.get("invert_attack", True):
            self._inv_attack.select()
        self._inv_attack.pack(side="left", padx=(0, 24))
        self._inv_defense = theme.switch(inv, "反转防御键")
        if self._block.get("invert_defense", False):
            self._inv_defense.select()
        self._inv_defense.pack(side="left")
        _help_mark(inv, "florr 设置里的「反转攻击 / 防御键」。每轮开局由 worker 自动写进游戏;"
                        "寻路时脚本不按攻击键, 靠反转攻击才能边走边打。").pack(
            side="left", padx=6)
        self._sync_enemy_enabled()

    def _build_server_and_loadout(self):
        sec = self._section("换服与换装")
        sw = ctk.CTkFrame(sec, fg_color="transparent")
        sw.pack(fill="x", pady=(0, 10))
        self._autosw = theme.switch(sw, "自动换服务器", command=self._sync_short_enabled)
        if self._block.get("auto_switch_server", True):
            self._autosw.select()
        self._autosw.pack(side="left")
        ctk.CTkLabel(sw, text="   连续短局", text_color=theme.MUTED,
                     font=theme.font(13)).pack(side="left")
        self._short_e = ctk.CTkEntry(sw, width=50, font=theme.font(13), justify="center")
        self._short_e.insert(0, str(self._block.get("consecutive_short_round_limit", 2)))
        self._short_e.pack(side="left", padx=6)
        ctk.CTkLabel(sw, text="轮就换", text_color=theme.MUTED,
                     font=theme.font(13)).pack(side="left")
        _help_mark(sw, self._TIP_SHORT).pack(side="left", padx=6)
        self._sync_short_enabled()

        self._enter_swap_w = self._build_swap_field(
            sec, "enter_game_swap", "进游戏时换装",
            "每轮真的(重新)进游戏后按一次和弦换 loadout: 按住修饰键 → 按数字 → 松开.")
        self._reach_swap_w = self._build_swap_field(
            sec, "reach_area_swap", "到刷怪区换装",
            "寻路到刷怪区后按一次和弦换 loadout.")

    def _build_swap_field(self, parent, key, title, tip):
        """一个 loadout 切换字段: 开关 + 修饰键下拉(无/k/l) + 数字下拉(1..0).
        返回 (switch, mod_menu, digit_menu); _collect 从这三个读回 {enabled,mod,digit}."""
        cur = _coerce_swap_obj(self._block.get(key))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, 6))
        sw = theme.switch(row, title, width=150)
        if cur["enabled"]:
            sw.select()
        sw.pack(side="left")
        _Tooltip(sw, tip)
        menu_kw = dict(font=theme.font(12), dropdown_font=theme.font(12),
                       fg_color=theme.SURFACE_HI, button_color=theme.BORDER,
                       button_hover_color=theme.FAINT, height=28)
        ctk.CTkLabel(row, text="按住", text_color=theme.MUTED,
                     font=theme.font(12)).pack(side="left", padx=(12, 4))
        mod = ctk.CTkOptionMenu(row, width=64, values=list(_SWAP_MOD_LABELS.values()),
                                **menu_kw)
        mod.set(_SWAP_MOD_LABELS[cur["mod"]])
        mod.pack(side="left")
        ctk.CTkLabel(row, text="再按", text_color=theme.MUTED,
                     font=theme.font(12)).pack(side="left", padx=(8, 4))
        digit = ctk.CTkOptionMenu(row, width=56, values=_SWAP_DIGIT_VALUES, **menu_kw)
        digit.set(cur["digit"])
        digit.pack(side="left")
        return sw, mod, digit

    def _collect_swap(self, widgets):
        sw, mod, digit = widgets
        return {
            "enabled": bool(sw.get()),
            "mod": _SWAP_MOD_FROM_LABEL.get(mod.get(), "none"),
            "digit": digit.get(),
        }

    def _normalize_entry(self, entry):
        """失焦: 能规整就把输入框内容替换成规范 HH:MM; 规整不了就原样留着,
        交给保存时的 validate_block 红字. 纯 UI 便利, 不参与校验闭环(_collect 自己也规整)."""
        fixed = app_config.normalize_time(entry.get().strip())
        if fixed is not None and fixed != entry.get():
            entry.delete(0, "end")
            entry.insert(0, fixed)

    def _sync_short_enabled(self):
        theme.set_entry_enabled(self._short_e, bool(self._autosw.get()))

    def _on_acct_pick(self, val):
        if val != self._NEW_PROFILE:
            self._acct_prev = val
            return
        # 取消 / 出错时回到用户原来选的那个账号, 而不是一律跳回列表第一个.
        dlg = ctk.CTkInputDialog(text="新账号别名:", title="新建账号")
        alias = (dlg.get_input() or "").strip()
        if not alias:
            self._acct.set(self._acct_prev)
            return
        if self.new_profile_cb is None:
            self._err.configure(text="新建账号未接线")
            self._acct.set(self._acct_prev)
            return
        # 登录引导完成前先回到原账号; 完成后 _add_profile_to_menu 会选中新账号.
        self._acct.set(self._acct_prev)
        self.new_profile_cb(alias, self._add_profile_to_menu)

    def _add_profile_to_menu(self, new_alias):
        """新建账号 + 登录引导完成后回调: 把新别名加进账号下拉并选中.
        登录引导是非模态的, 用户可能在登录完成前把这个编辑器关掉了 —— 那样
        self / self._acct 的 tk 控件已销毁, configure 会 TclError. profile 本身
        已被 new_profile_cb 建好 + 存盘, 这里只是刷下拉, 编辑器没了就直接跳过."""
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._profiles.append({"alias": new_alias, "dir": f"chrome-profiles/{new_alias}"})
        self._acct.configure(values=[p["alias"] for p in self._profiles] + [self._NEW_PROFILE])
        self._acct.set(new_alias)
        self._acct_prev = new_alias

    def _on_map_change(self):
        # CTkRadioButton 的 command 不带值, 从 StringVar 自己读.
        self._point = None
        self._area = None
        self._picker.load_map(self._map.get())
        self._picker.set_point(None)
        self._picker.set_area(None)
        self._sync_readout()
        self._sync_enemy_enabled()
        self._sync_map_hint()

    def _sync_map_hint(self):
        text = _anthell_blocker() if self._map.get() == "anthell" else None
        if text:
            self._map_hint.configure(text="⚠ " + text)
            self._map_hint.pack(anchor="w", pady=(8, 0))
        else:
            self._map_hint.pack_forget()

    def _sync_enemy_enabled(self):
        """索敌只在物种表非空的图上可用(现在只有沙漠)。别的图上开关置灰 ——
        worker 那边 main._apply_worker_config 本来就会强制关, 界面上跟它一致,
        免得用户以为开了就生效。"""
        # 延迟导入: enemy_detect 会拉进 cv2 / cdp_bridge / canvas_decode, GUI 的
        # 启动路径不该为了一个开关状态背这些。(同文件里 MapPicker 也是这么导的。)
        from enemy_detect import species_supported
        supported = species_supported(self._map.get())
        self._enemy.configure(state="normal" if supported else "disabled")
        self._enemy_hint.configure(
            text="本图支持索敌" if supported else "本图暂不支持索敌(目前只有沙漠)")


    def _on_point(self, pt):
        self._point = pt
        self._sync_readout()

    def _on_area(self, area):
        self._area = [tuple(area[0]), tuple(area[1])]
        self._sync_readout()

    def _clear_location(self):
        self._point = None
        self._area = None
        self._picker.set_point(None)
        self._picker.set_area(None)
        self._sync_readout()

    def _sync_readout(self):
        parts = []
        if self._point:
            parts.append(f"目标点 {_fmt_pt(self._point)}")
        if self._area:
            parts.append(f"区域 {_fmt_pt(self._area[0])}–{_fmt_pt(self._area[1])}")
        if parts:
            self._loc_readout.configure(text="  ·  ".join(parts), text_color=theme.TEXT)
        else:
            self._loc_readout.configure(text="还没选位置", text_color=theme.FAINT)

    def _collect(self):
        from gui_app import resolve_point_and_area
        point, area = resolve_point_and_area(self._point, self._area)
        days = [i for i, v in enumerate(self._day_vars) if v.get()]
        start_raw = self._start_e.get().strip()
        end_raw = self._end_e.get().strip()
        blk = dict(self._block)
        blk.update(
            days=days,
            start=app_config.normalize_time(start_raw) or start_raw,
            end=app_config.normalize_time(end_raw) or end_raw,
            profile=self._acct.get(), map=self._map.get(),
            location=list(point) if point else None,
            farming_area=[list(area[0]), list(area[1])] if area else None,
            enemy_ai_enabled=bool(self._enemy.get()),
            auto_switch_server=bool(self._autosw.get()),
            invert_attack=bool(self._inv_attack.get()),
            invert_defense=bool(self._inv_defense.get()),
            enter_game_swap=self._collect_swap(self._enter_swap_w),
            reach_area_swap=self._collect_swap(self._reach_swap_w),
        )
        try:
            blk["farming_duration"] = int(self._dur_e.get())
        except ValueError:
            blk["farming_duration"] = 0
        try:
            blk["consecutive_short_round_limit"] = int(self._short_e.get())
        except ValueError:
            blk["consecutive_short_round_limit"] = 0
        return blk

    def _save(self):
        blk = self._collect()
        err = validate_block(blk, self._others)
        if err:
            self._err.configure(text="⚠ " + err)
            self.bell()
            return
        self._on_save(blk)
        self.destroy()


def _block_sort_key(blk):
    return (min(blk.get("days") or [7]), blk.get("start", ""))


class ScheduleList(ctk.CTkScrollableFrame):
    """时块卡片列表 + ＋新增. get_cfg() 返回当前 cfg dict; save_cfg(cfg) 落盘并
    回读; open_editor(block_or_None) 由 App 提供(弹 TimeBlockEditor)。"""

    def __init__(self, master, *, get_cfg, save_cfg, open_editor):
        super().__init__(master, fg_color="transparent")
        self._get_cfg = get_cfg
        self._save_cfg = save_cfg
        self._open_editor = open_editor
        self._readonly = False
        self._running_id = None
        self.refresh()

    def set_readonly(self, ro):
        self._readonly = bool(ro)
        self.refresh()

    def set_running(self, block_id):
        if block_id != self._running_id:
            self._running_id = block_id
            self.refresh()

    def refresh(self):
        for w in list(self.winfo_children()):
            w.destroy()
        st = "disabled" if self._readonly else "normal"
        if self._readonly:
            theme.hint(self, "调度运行中, 时间表只读 —— 停止调度后才能修改",
                       text_color=theme.WARN).pack(anchor="w", padx=2, pady=(0, 6))
        blocks = sorted(self._get_cfg().get("schedule", []), key=_block_sort_key)
        if not blocks:
            empty = theme.card(self)
            empty.pack(fill="x", pady=4)
            ctk.CTkLabel(empty, text="还没有时块", font=theme.font(15, "bold"),
                         text_color=theme.TEXT).pack(pady=(22, 2))
            theme.hint(empty, "新增一个时块: 选星期和时段、账号、地图, 再在地图上点出刷怪位置",
                       anchor="center").pack(pady=(0, 22))
        for blk in blocks:
            self._build_row(blk, st)
        theme.primary_button(self, "＋  新增时块", lambda: self._open_editor(None),
                             state=st, height=36).pack(fill="x", pady=(8, 2))

    def _build_row(self, blk, st):
        running = blk["id"] == self._running_id
        on = bool(blk["enabled"])
        row = theme.card(self, border_color=theme.SUCCESS if running else theme.BORDER,
                         border_width=2 if running else 1)
        row.pack(fill="x", pady=4)
        row.grid_columnconfigure(1, weight=1)

        ev = ctk.IntVar(value=1 if on else 0)
        sw = theme.switch(row, "", width=44, variable=ev, state=st,
                          command=lambda b=blk, v=ev: self._toggle(b, v))
        sw.grid(row=0, column=0, rowspan=2, padx=(14, 6), pady=12)
        _Tooltip(sw, "启用 / 停用这个时块(停用的不参与调度)")

        main_c = theme.TEXT if on else theme.FAINT
        sub_c = theme.MUTED if on else theme.FAINT
        top = ctk.CTkFrame(row, fg_color="transparent")
        top.grid(row=0, column=1, sticky="w", pady=(10, 0))
        ctk.CTkLabel(top, text=f"{blk['start']} – {blk['end']}", font=theme.mono(15),
                     text_color=main_c).pack(side="left")
        ctk.CTkLabel(top, text="   " + weekday_text(blk["days"]), font=theme.font(13),
                     text_color=main_c).pack(side="left")
        if running:
            ctk.CTkLabel(top, text=" ● 运行中 ", font=theme.font(11, "bold"),
                         text_color=theme.SUCCESS).pack(side="left", padx=8)
        elif not on:
            ctk.CTkLabel(top, text="  已停用", font=theme.font(11),
                         text_color=theme.FAINT).pack(side="left", padx=4)

        tags = [blk["profile"], theme.map_label(blk["map"]),
                ("索敌" if blk.get("enemy_ai_enabled") else "不索敌"),
                f"刷 {blk.get('farming_duration', 0)} 秒"]
        if blk.get("auto_switch_server"):
            tags.append("自动换服")
        ctk.CTkLabel(row, text="  ·  ".join(str(t) for t in tags), font=theme.font(12),
                     text_color=sub_c, anchor="w").grid(row=1, column=1, sticky="w",
                                                        pady=(0, 10))

        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=2, padx=(6, 12))
        theme.ghost_button(btns, "编辑", lambda b=blk: self._open_editor(b), width=56,
                           height=28, state=st).pack(side="left", padx=(0, 6))
        theme.danger_button(btns, "删除", lambda b=blk: self._delete(b), width=56,
                            height=28, state=st).pack(side="left")

    def _toggle(self, blk, var):
        cfg = self._get_cfg()
        for b in cfg["schedule"]:
            if b["id"] == blk["id"]:
                b["enabled"] = bool(var.get())
        self._save_cfg(cfg)
        self.refresh()

    def _delete(self, blk):
        from tkinter import messagebox
        if not messagebox.askyesno(
                "删除时块",
                f"删掉「{weekday_text(blk['days'])} {blk['start']}–{blk['end']}」"
                f"({blk['profile']} · {theme.map_label(blk['map'])}) 这个时块?",
                parent=self):
            return
        cfg = self._get_cfg()
        cfg["schedule"] = [b for b in cfg["schedule"] if b["id"] != blk["id"]]
        self._save_cfg(cfg)
        self.refresh()
