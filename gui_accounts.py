"""账号(Chrome profile)管理: 纯数据操作 + 账号页控件.

纯函数对 cfg dict 操作, 返回 (cfg, err|None), 不写盘 —— 调用方负责 save_config
+ os.makedirs / os.rename 那些副作用. 控件类 AccountsPage 在文件下半段.
"""
import copy
import os
from tkinter import messagebox

import customtkinter as ctk

# 这两个纯数据函数住在 app_config —— 无头服务器上 `main.py --launch-chrome` 也要
# 解析 profile 目录, 而本模块 import customtkinter, 没有 X 的机器上 import 就炸.
# 这里只做转出, GUI 那边的调用点(gui_app / AccountsPage)不用改.
from app_config import abs_profile_path, profile_dir
import gui_theme as theme
from gui_schedule import _safe_dirname


def _aliases(cfg):
    return [p["alias"] for p in cfg.get("profiles", [])]


def add_profile(cfg, alias):
    alias = (alias or "").strip()
    if not alias:
        return cfg, "账号名不能为空"
    if not _safe_dirname(alias):
        return cfg, "账号名里没有可用作目录名的字符"
    if alias in _aliases(cfg):
        return cfg, f"账号名『{alias}』已存在"
    cfg["profiles"].append({"alias": alias, "dir": f"chrome-profiles/{alias}"})
    return cfg, None


def rename_profile(cfg, old, new):
    new = (new or "").strip()
    if not new:
        return cfg, "新名字不能为空"
    if not _safe_dirname(new):
        return cfg, "新名字里没有可用作目录名的字符"
    if new == old:
        return cfg, None
    if new in _aliases(cfg):
        return cfg, f"账号名『{new}』已存在"
    for p in cfg["profiles"]:
        if p["alias"] == old:
            p["alias"] = new
            p["dir"] = f"chrome-profiles/{new}"
            break
    else:
        return cfg, f"没有账号『{old}』"
    for b in cfg.get("schedule", []):
        if b.get("profile") == old:
            b["profile"] = new
    return cfg, None


def delete_profile(cfg, alias):
    used = [b.get("id") for b in cfg.get("schedule", []) if b.get("profile") == alias]
    if used:
        return cfg, f"时块 {', '.join(used)} 还在用『{alias}』, 先改掉那些时块的账号"
    cfg["profiles"] = [p for p in cfg["profiles"] if p["alias"] != alias]
    return cfg, None


# ─────────────────────────────── 控件层 ───────────────────────────────

class AccountsPage(ctk.CTkScrollableFrame):
    """profile 列表: 新建 / 登录 / 改名 / 删除. 调度运行中整页只读.
    new_profile_cb(alias, on_ready) 由 App 注入(建目录 + save + 登录引导)。"""

    def __init__(self, master, *, get_cfg, save_cfg, login_guide):
        super().__init__(master, fg_color="transparent")
        self._get_cfg = get_cfg
        self._save_cfg = save_cfg
        self._login = login_guide
        self.new_profile_cb = None
        self._readonly = False
        self.refresh()

    def set_readonly(self, ro):
        self._readonly = bool(ro)
        self.refresh()

    def refresh(self):
        for w in list(self.winfo_children()):
            w.destroy()
        st = "disabled" if self._readonly else "normal"
        if self._readonly:
            theme.hint(self, "调度运行中, 账号只读 —— 停止调度后才能修改",
                       text_color=theme.WARN).pack(anchor="w", padx=2, pady=(0, 6))
        cfg = self._get_cfg()
        for p in cfg.get("profiles", []):
            used = sum(1 for b in cfg.get("schedule", []) if b.get("profile") == p["alias"])
            # profile 目录存在 = 至少走过一次登录引导(Chrome 会往里写东西).
            logged = os.path.isdir(abs_profile_path(p["dir"]))
            row = theme.card(self)
            row.pack(fill="x", pady=4)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text=p["alias"][:1].upper(), width=36, height=36,
                         corner_radius=18, fg_color=theme.SURFACE_HI,
                         font=theme.font(15, "bold"), text_color=theme.TEXT).grid(
                row=0, column=0, rowspan=2, padx=(14, 10), pady=12)
            top = ctk.CTkFrame(row, fg_color="transparent")
            top.grid(row=0, column=1, sticky="w", pady=(10, 0))
            ctk.CTkLabel(top, text=p["alias"], font=theme.font(14, "bold"),
                         text_color=theme.TEXT).pack(side="left")
            ctk.CTkLabel(top, text=("  ● 已登录过" if logged else "  ○ 还没登录"),
                         font=theme.font(11),
                         text_color=theme.SUCCESS if logged else theme.WARN).pack(side="left")
            theme.hint(row, f"{p['dir']}  ·  {used} 个时块在用").grid(
                row=1, column=1, sticky="w", pady=(0, 10))
            btns = ctk.CTkFrame(row, fg_color="transparent")
            btns.grid(row=0, column=2, rowspan=2, padx=(6, 12))
            a = p["alias"]
            theme.primary_button(btns, "登录", lambda a=a: self._login_profile(a),
                                 width=56, height=28, state=st,
                                 font=theme.font(12)).pack(side="left", padx=(0, 6))
            theme.ghost_button(btns, "改名", lambda a=a: self._rename(a), width=56,
                               height=28, state=st).pack(side="left", padx=(0, 6))
            theme.danger_button(btns, "删除", lambda a=a: self._delete(a), width=56,
                                height=28, state=st).pack(side="left")
        theme.primary_button(self, "＋  新建账号", self._new, state=st, height=36).pack(
            fill="x", pady=(8, 2))

    def _login_profile(self, alias):
        d = profile_dir(self._get_cfg(), alias)
        if d is None:
            return
        self._login.start(abs_profile_path(d),
                          on_done=self.refresh, on_cancel=lambda: None)

    def _rename(self, alias):
        dlg = ctk.CTkInputDialog(text=f"把『{alias}』改成:", title="改名")
        new = (dlg.get_input() or "").strip()
        if not new:
            return
        cfg = self._get_cfg()
        old_rel = profile_dir(cfg, alias)
        cfg, err = rename_profile(cfg, alias, new)
        if err:
            messagebox.showwarning("账号", err, parent=self)
            return
        try:
            old_abs = abs_profile_path(old_rel) if old_rel else None
            if old_abs and os.path.isdir(old_abs):
                os.rename(old_abs, os.path.join(os.path.dirname(old_abs), new))
        except OSError:
            messagebox.showwarning(
                "账号", "配置已改, 但目录没改成(该账号的 Chrome 可能还开着)")
        self._save_cfg(cfg)
        self.refresh()

    def _delete(self, alias):
        # delete_profile 会原地改 cfg; 用户在确认框点"否"时不能已经删了 —— 先在副本上跑.
        cfg, err = delete_profile(copy.deepcopy(self._get_cfg()), alias)
        if err:
            messagebox.showwarning("账号", err, parent=self)
            return
        # 以前点一下就删, 没有确认. 只删配置里的条目, 登录数据目录留在盘上.
        if not messagebox.askyesno(
                "删除账号",
                f"从列表里删掉账号『{alias}』?\n(它的 Chrome 登录数据目录会保留在磁盘上)",
                parent=self):
            return
        self._save_cfg(cfg)
        self.refresh()

    def _new(self):
        dlg = ctk.CTkInputDialog(text="新账号别名:", title="新建账号")
        alias = (dlg.get_input() or "").strip()
        if not alias:
            return
        if self.new_profile_cb:
            self.new_profile_cb(
                alias, lambda *_a: self.winfo_exists() and self.refresh())
