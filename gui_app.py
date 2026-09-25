"""florr-auto-pathing 的控制面板 GUI. 无参跑 `python main.py` / 双击 exe 就进这里.

阶段2: 中间是"时块列表" —— 按星期几 + 时间段配 账号/地图/目标点/刷怪区.
点"开始调度"后, GUI 内的调度器(self.after 循环)每 30s 查一次此刻命中哪个时块,
按需 关掉 Chrome 换 profile 重开(--start-fullscreen + florr.io)、把该时块的刷怪
参数刷进 config['active']、(重)起一个 `main.py --worker` 子进程. 空档期停 worker.
调度驱动的运行全程零人工; 只有用户主动"新建账号 / 重新登录"才弹非模态登录引导.
"""
import os
import subprocess
import sys
import threading
import time
import traceback
from tkinter import messagebox

import customtkinter as ctk

import app_config
import afk_watch
import cdp_bridge
import gui_accounts
import gui_chrome_flow
import gui_schedule
import gui_theme as theme

_IS_WINDOWS = sys.platform == "win32"
_LOG_MAX_LINES = 2000  # 日志框最多留这么多行, 再多就从头截掉
_TICK_MS = 30_000      # 调度器 tick 间隔


def worker_command():
    """拉起 worker 子进程的命令行. frozen(PyInstaller)时 sys.executable 就是我们
    自己的 exe, 直接带 --worker; 脚本模式下要显式 python + main.py 路径, 加 -u 让
    子进程 stdout 行缓冲(日志实时进面板)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker"]
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return [sys.executable, "-u", main_py, "--worker"]


def parse_positive_ints(*strs):
    """把界面上的数字框字符串批量转成正整数. 任一非法(非整数 / <=0)返回 None.
    不用 assert —— python -O 会把 assert 整个剥掉."""
    out = []
    for s in strs:
        try:
            n = int(s)
        except (TypeError, ValueError):
            return None
        if n <= 0:
            return None
        out.append(n)
    return out


_MAP_PX = 300           # maps/*.png 都是 300x300; 派生坐标 clamp 到 [0, _MAP_PX-1]
_DERIVED_AREA_HALF = 12  # 只点了目标点没框区域时, 以点为中心生成的方块半边长(图像像素)


def _clamp_px(v):
    return max(0, min(_MAP_PX - 1, int(v)))


def resolve_point_and_area(point, area):
    """目标点和刷怪区域二选一即可(都给也行). 返回补全后的 (point, area);
    两个都没有则返回 (None, None), 让调用方报错.
      - 只有区域: 目标点 = 区域中心
      - 只有点: 刷怪区域 = 以点为中心的小方块(clamp 进地图范围)
    """
    if point is None and area is None:
        return None, None
    if area is None:
        x, y = int(point[0]), int(point[1])
        h = _DERIVED_AREA_HALF
        area = [(_clamp_px(x - h), _clamp_px(y - h)),
                (_clamp_px(x + h), _clamp_px(y + h))]
    if point is None:
        (x1, y1), (x2, y2) = area
        point = ((int(x1) + int(x2)) // 2, (int(y1) + int(y2)) // 2)
    return point, area


def start_afk(*, exe_exists, running, confirm_download):
    """AFK 开关打开时的决策(纯函数, 副作用由调用方按返回值执行).
    already: 已在跑, 什么都不用做
    declined: exe 缺, 用户拒绝下载
    downloaded / download_failed: exe 缺, 下过了(成/败)
    started: exe 在, 需要调用方去 ensure_florr_auto_afk_running()
    """
    if running:
        return "already"
    if not exe_exists:
        if not confirm_download():
            return "declined"
        return "downloaded" if afk_watch.download_florr_auto_afk() else "download_failed"
    return "started"


def plan_transition(running_id, new_block, chrome_profile):
    """给定当前在跑的时块 id + 此刻命中的时块 + 当前 Chrome 用的 profile,
    算出调度该干什么。纯函数, 无 I/O。
      noop  —— 什么都不用做(同一个时块, 或本来就空档)
      idle  —— 停 worker(从跑着变成空档)
      run   —— 停 worker + (可能)换 Chrome + 起 worker
    """
    if new_block is None:
        return {"action": "idle"} if running_id is not None else {"action": "noop"}
    if new_block["id"] == running_id:
        return {"action": "noop"}
    return {
        "action": "run",
        "relaunch_chrome": new_block["profile"] != chrome_profile,
        "profile": new_block["profile"],
    }


class _GuideHost:
    """把主窗口里一块 CTkFrame 包成 LoginGuide 要的 show()/hide()/detected() 接口。"""

    def __init__(self, frame, grid_kw, state_label=None):
        self._frame = frame
        self._grid_kw = grid_kw
        self._state = state_label

    def show(self):
        if self._state is not None:
            self._state.configure(text="等待 florr.io 页面…", text_color=theme.MUTED)
        self._frame.grid(**self._grid_kw)

    def hide(self):
        self._frame.grid_remove()

    def detected(self):
        if self._state is not None:
            self._state.configure(text="✓ 已检测到 florr.io 页面, 登录好就可以点「完成」",
                                  text_color=theme.SUCCESS)


class App(ctk.CTk):
    _PAGES = (
        ("时间表", "按星期 + 时间段安排: 用哪个账号、去哪张图、在哪刷怪"),
        ("账号", "每个账号是一个独立的 Chrome profile, 登录一次后调度自动切换"),
    )

    def __init__(self):
        ctk.set_appearance_mode("dark")
        super().__init__(fg_color=theme.BG)
        self.title("florr-auto-pathing 控制面板")
        self.geometry("980x680")
        self.minsize(820, 560)

        self._cfg = app_config.load_config()
        self.proc = None
        self._reader = None
        self._closing = False

        # 调度器状态
        self._sched_running = False
        self._running_block_id = None
        self._chrome_profile = None
        self._tick_job = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()

        # ---- 主区 ----
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=18, pady=16)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=3)   # 页面
        main.grid_rowconfigure(3, weight=2)   # 日志

        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._page_title = ctk.CTkLabel(head, text="", font=theme.font(20, "bold"),
                                        text_color=theme.TEXT, anchor="w")
        self._page_title.pack(anchor="w")
        self._page_sub = theme.hint(head)
        self._page_sub.pack(anchor="w")

        # 页面宿主: 时间表 / 账号 两页叠在同一格, 切页 = grid / grid_remove.
        self.content = ctk.CTkFrame(main, fg_color="transparent")
        self.content.grid(row=1, column=0, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        # 登录引导区(默认隐藏) —— 先建, 下面账号页要用它
        self._build_login_guide(main)

        self._sched_list = gui_schedule.ScheduleList(
            self.content, get_cfg=self._get_cfg, save_cfg=self._save_cfg,
            open_editor=self._open_editor)
        self._accounts = gui_accounts.AccountsPage(
            self.content, get_cfg=self._get_cfg, save_cfg=self._save_cfg,
            login_guide=self._login_guide)
        self._accounts.new_profile_cb = self._make_profile_then

        # ---- 日志 ----
        log_card = theme.card(main)
        log_card.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)
        lh = ctk.CTkFrame(log_card, fg_color="transparent")
        lh.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        ctk.CTkLabel(lh, text="运行日志", font=theme.font(13, "bold"),
                     text_color=theme.TEXT).pack(side="left")
        theme.ghost_button(lh, "清空", self._clear_log, width=56, height=24).pack(
            side="right")
        self.log_box = ctk.CTkTextbox(log_card, font=theme.mono(12), state="disabled",
                                      fg_color=theme.LOG_BG, text_color="#c9ced6",
                                      corner_radius=8, wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        # ---- 底栏: 状态 + 开始/停止 ----
        bar = ctk.CTkFrame(main, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        bar.grid_columnconfigure(1, weight=1)
        self._status_dot = ctk.CTkLabel(bar, text="●", font=theme.font(16),
                                        text_color=theme.FAINT, width=18)
        self._status_dot.grid(row=0, column=0, padx=(2, 6))
        self.status_label = ctk.CTkLabel(bar, text="", anchor="w",
                                         font=theme.font(13), text_color=theme.TEXT)
        self.status_label.grid(row=0, column=1, sticky="ew")
        self.start_btn = ctk.CTkButton(bar, text="", height=42, width=170,
                                       corner_radius=10, font=theme.font(14, "bold"),
                                       command=self._on_start_stop)
        self.start_btn.grid(row=0, column=2, sticky="e")
        self._set_status("未运行", "off")
        self._set_start_btn(False)

        self._page_widgets = {"时间表": self._sched_list, "账号": self._accounts}
        self._show_page("时间表")

        # 放在最后: 这个钩子会往 self.log_box 里写, 得等控件都建好.
        # 方法名别叫 _report_exception —— 那是 tkinter.Misc 的内部方法(无参调用),
        # 覆盖掉它 App 自己 .after() 的回调一抛异常就变成 TypeError, 真错误被吞掉.
        self.report_callback_exception = self._on_callback_exception

    def _build_sidebar(self):
        side = ctk.CTkFrame(self, width=190, corner_radius=0, fg_color=theme.SIDEBAR)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(3, weight=1)  # spacer

        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(20, 18))
        ctk.CTkLabel(brand, text="florr 自动寻路", font=theme.font(17, "bold"),
                     text_color=theme.TEXT, anchor="w").pack(anchor="w")
        theme.hint(brand, "auto-pathing 控制面板").pack(anchor="w")

        self._nav = {}
        for i, (name, _sub) in enumerate(self._PAGES):
            b = ctk.CTkButton(side, text=name, anchor="w", height=36, corner_radius=8,
                              font=theme.font(14), fg_color="transparent",
                              hover_color=theme.SURFACE_HI, text_color=theme.MUTED,
                              command=lambda n=name: self._show_page(n))
            b.grid(row=1 + i, column=0, padx=10, pady=2, sticky="ew")
            self._nav[name] = b

        afk = theme.card(side, fg_color=theme.SURFACE)
        afk.grid(row=4, column=0, padx=10, pady=12, sticky="ew")
        row = ctk.CTkFrame(afk, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(row, text="AFK 自动处理", font=theme.font(13, "bold"),
                     text_color=theme.TEXT).pack(side="left")
        self.afk_switch = theme.switch(afk, text="", width=44,
                                       command=self._on_afk_toggle)
        self.afk_switch.pack(anchor="w", padx=12, pady=(4, 0))
        theme.hint(afk, "后台跑 florr-auto-afk,\n自动点掉 AFK 检测弹窗" if _IS_WINDOWS
                   else "仅 Windows 可用").pack(anchor="w", padx=12, pady=(2, 10))
        if self._cfg["afk_enabled"]:
            self.afk_switch.select()
            self.after(400, self._ensure_afk)
        if not _IS_WINDOWS:
            self.afk_switch.configure(state="disabled")

    def _build_login_guide(self, main):
        self._guide_frame = theme.card(main, border_color=theme.ACCENT)
        self._guide_grid_kw = dict(row=2, column=0, sticky="ew", pady=(12, 0))
        self._guide_frame.grid_columnconfigure(0, weight=1)
        left = ctk.CTkFrame(self._guide_frame, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=14, pady=10)
        ctk.CTkLabel(left, text="登录账号", font=theme.font(14, "bold"),
                     text_color=theme.TEXT).pack(anchor="w")
        for txt in ("① 已为这个账号打开一个 Chrome 窗口(florr.io)",
                    "② 在那个窗口里登录你的 florr 账号",
                    "③ 登录好后回来点「完成」"):
            ctk.CTkLabel(left, text=txt, font=theme.font(12), text_color=theme.TEXT,
                         anchor="w").pack(anchor="w")
        self._guide_state = theme.hint(left, "等待 florr.io 页面…")
        self._guide_state.pack(anchor="w", pady=(4, 0))
        gbtns = ctk.CTkFrame(self._guide_frame, fg_color="transparent")
        gbtns.grid(row=0, column=1, sticky="e", padx=14)
        theme.ghost_button(gbtns, "取消", lambda: self._login_guide.cancel(),
                           width=72).pack(side="left", padx=(0, 6))
        theme.primary_button(gbtns, "完成", lambda: self._login_guide.finish(),
                             width=72).pack(side="left")
        self._login_guide = gui_chrome_flow.LoginGuide(
            _GuideHost(self._guide_frame, self._guide_grid_kw, self._guide_state),
            after=self.after)

    # ---- 状态展示 ----
    _STATUS_COLORS = {"off": theme.FAINT, "run": theme.SUCCESS, "idle": theme.WARN}

    def _set_status(self, text, kind):
        self.status_label.configure(text=text)
        self._status_dot.configure(text_color=self._STATUS_COLORS.get(kind, theme.FAINT))

    def _set_start_btn(self, running):
        if running:
            self.start_btn.configure(text="■  停止调度", fg_color=theme.DANGER,
                                     hover_color=theme.DANGER_HOVER)
        else:
            self.start_btn.configure(text="▶  开始调度", fg_color=theme.SUCCESS,
                                     hover_color=theme.SUCCESS_HOVER)

    def _set_running_block(self, block_id):
        self._running_block_id = block_id
        self._sched_list.set_running(block_id)

    # ---- cfg 读写 ----
    def _get_cfg(self):
        return self._cfg

    def _save_cfg(self, cfg):
        app_config.save_config(cfg)
        self._cfg = app_config.load_config()
        return self._cfg

    # ---- 页面切换 ----
    def _show_page(self, name):
        # 两页一直 grid 在同一格里, 切页只 lift(). 以前用 grid_remove() + grid()
        # 来回切, 两页都是 CTkScrollableFrame(内容挂在 canvas 的嵌入窗口里), 重新
        # 映射后 macOS Tk 不重画嵌入窗口 —— 从「账号」切回「时间表」列表整片空白.
        w = self._page_widgets[name]
        w.grid(row=0, column=0, sticky="nsew")
        w.lift()
        for n, b in self._nav.items():
            on = n == name
            b.configure(fg_color=theme.SURFACE_HI if on else "transparent",
                        text_color=theme.TEXT if on else theme.MUTED,
                        font=theme.font(14, "bold" if on else "normal"))
        self._page_title.configure(text=name)
        self._page_sub.configure(text=dict(self._PAGES)[name])

    # ---- 时块编辑 ----
    def _open_editor(self, block):
        if self._sched_running:
            return
        # 同一时刻只开一个编辑窗: 开两个改同一个时块, 后存的会悄悄盖掉先存的.
        ed = getattr(self, "_editor", None)
        if ed is not None:
            try:
                if ed.winfo_exists():
                    ed.deiconify()
                    ed.lift()
                    ed.focus_force()
                    return
            except Exception:
                pass
        cfg = self._cfg
        tpl = block if block is not None else gui_schedule.new_block_template(cfg)
        others = [b for b in cfg["schedule"] if b.get("id") != tpl.get("id")]
        ed = gui_schedule.TimeBlockEditor(
            self, block=tpl, others=others, profiles=cfg["profiles"],
            on_save=self._save_block, is_new=block is None)
        ed.new_profile_cb = self._make_profile_then
        self._editor = ed

    def _save_block(self, blk):
        sched = self._cfg["schedule"]
        for i, b in enumerate(sched):
            if b["id"] == blk["id"]:
                sched[i] = blk
                break
        else:
            sched.append(blk)
        self._save_cfg(self._cfg)
        self._sched_list.refresh()

    def _make_profile_then(self, alias, on_ready):
        """账号页 / 编辑器下拉里"＋新建"共用: 建 profile + 目录 + 存 + 登录引导。"""
        cfg, err = gui_accounts.add_profile(self._cfg, alias)
        if err:
            self._log_line(f"新建账号失败: {err}\n")
            return
        rel = gui_accounts.profile_dir(cfg, alias)
        try:
            os.makedirs(gui_accounts.abs_profile_path(rel), exist_ok=True)
        except OSError as e:
            self._log_line(f"建 profile 目录失败: {e}\n")
            return
        self._save_cfg(cfg)
        self._accounts.refresh()
        self._login_guide.start(
            gui_accounts.abs_profile_path(rel),
            on_done=lambda: (self._accounts.refresh(), on_ready(alias)),
            on_cancel=lambda: None)

    # ---- 调度器 ----
    def _on_start_stop(self):
        if self._sched_running:
            self._sched_running = False
            if self._tick_job is not None:
                self.after_cancel(self._tick_job)
                self._tick_job = None
            self._stop_worker_sync()
            self._set_running_block(None)
            self._sched_list.set_readonly(False)
            self._accounts.set_readonly(False)
            self._set_start_btn(False)
            self._set_status("未运行", "off")
            self._log_line("—— 调度已停止 ——\n")
            return

        if not any(b.get("enabled") for b in self._cfg["schedule"]):
            self._log_line("⚠️ 时间表里没有启用的时块, 先加一个(或勾上左边的启用开关)\n")
            self._show_page("时间表")
            return
        self._sched_running = True
        self._sched_list.set_readonly(True)
        self._accounts.set_readonly(True)
        self._set_start_btn(True)
        self._log_line("—— 调度已启动 ——\n")
        self._sched_tick()

    def _sched_tick(self):
        lt = time.localtime()
        weekday = lt.tm_wday                       # Python: 周一=0 —— 跟本项目编号一致
        hhmm = "%02d:%02d" % (lt.tm_hour, lt.tm_min)
        blk = app_config.active_block(self._cfg["schedule"], weekday, hhmm)
        plan = plan_transition(self._running_block_id, blk, self._chrome_profile)
        if plan["action"] == "idle":
            self._stop_worker_sync()
            self._set_running_block(None)
            self._log_line("⏸ 空档, worker 已停\n")
        elif plan["action"] == "run":
            self._enter_block(blk, plan["relaunch_chrome"])
        self._update_status(blk, weekday, hhmm)
        if self._sched_running:
            self._tick_job = self.after(_TICK_MS, self._sched_tick)

    def _enter_block(self, blk, relaunch):
        self._stop_worker_sync()
        if relaunch:
            rel = gui_accounts.profile_dir(self._cfg, blk["profile"])
            if rel is None:
                self._log_line(f"⚠️ 账号『{blk['profile']}』不存在, 跳过时块 {blk['id']}\n")
                self._set_running_block(None)
                return
            pdir = gui_accounts.abs_profile_path(rel)
            if not os.path.isdir(pdir):
                self._log_line(f"⚠️ 账号『{blk['profile']}』还没登录过, 跳过时块 {blk['id']}\n")
                self._set_running_block(None)
                return
            self._log_line(f"切到账号『{blk['profile']}』, 重开 Chrome…\n")
            try:
                cdp_bridge.launch_chrome_for_profile(pdir, fullscreen=True)
            except RuntimeError as e:
                self._log_line(f"⚠️ 起 Chrome 失败: {e}, 跳过时块 {blk['id']}\n")
                self._set_running_block(None)
                return
            if cdp_bridge.wait_for_florr_tab(30) is None:
                self._log_line(f"⚠️ 账号『{blk['profile']}』未登录 / florr.io 没起来, "
                               f"跳过时块 {blk['id']}\n")
                self._set_running_block(None)
                return
            self._chrome_profile = blk["profile"]

        self._cfg["active"] = gui_schedule.block_to_active(blk)
        app_config.save_config(self._cfg)
        self._spawn_worker()
        self._set_running_block(blk["id"])
        self._log_line(f"▶ 进入时块 {blk['id']}({blk['profile']} / {theme.map_label(blk['map'])}) "
                       f"{blk['start']}–{blk['end']}\n")

    def _update_status(self, blk, weekday, hhmm):
        nb = app_config.next_start(self._cfg["schedule"], weekday, hhmm)
        if nb is None:
            nxt = "之后没有安排"
        else:
            d, t = nb
            same = "今天 " if d == weekday else f"周{gui_schedule.WEEKDAY_LABELS[d]} "
            nxt = f"下一个时块 {same}{t}"
        if blk is None:
            self._set_status(f"空档, 等待中 · {nxt}", "idle")
            return
        desc = (f"{blk['profile']} · {theme.map_label(blk['map'])} · "
                f"{blk['start']}–{blk['end']}")
        if self._running_block_id == blk["id"]:
            self._set_status(f"运行中  {desc}", "run")
        else:
            # 命中了时块但没跑起来(账号没登录 / Chrome 没起来, 原因见日志) ——
            # 别再显示成"运行中", 下个 tick 会重试.
            self._set_status(f"时块没能启动, {_TICK_MS // 1000} 秒后重试(原因见日志)  {desc}",
                             "idle")

    # ---- worker 子进程 ----
    def _spawn_worker(self):
        # 明确告诉 worker: 我关 stdin 就是让你收尾退出。
        # 不用环境变量而让 worker 无条件监听 stdin 的话, `nohup python main.py < /dev/null`
        # 这种起法会在启动瞬间读到 EOF 直接退出(Ubuntu 无头部署就是这么跑的)。
        kwargs = {"env": {**os.environ, "PYTHONUNBUFFERED": "1",
                          "PYTHONIOENCODING": "utf-8",
                          "FLORR_WORKER_STDIN_EOF_EXIT": "1"}}
        self.proc = subprocess.Popen(
            worker_command(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, **kwargs)
        self._log_line("—— worker 已启动 ——\n")
        self._reader = threading.Thread(target=self._pump_log, args=(self.proc,),
                                        daemon=True)
        self._reader.start()

    def _pump_log(self, proc):
        for line in proc.stdout:
            if self._closing:
                return
            self.after(0, self._log_line, line)
        code = proc.wait()
        if self._closing:
            return
        self.after(0, self._on_worker_exit, proc, code)

    def _stop_worker_sync(self):
        """同步、有上限地收干净当前 worker. 关 stdin(EOF)让 worker 自己
        reset_keyboard() 再退; POSIX 上补一发 SIGTERM; 最多等 3s, 还活着就 kill.
        收完把 self.proc 置 None —— 慢半拍的 _pump_log 回调会因 proc != self.proc 早退."""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception as e:
            self._log_line(f"发送停止信号失败: {e}\n")
        if not _IS_WINDOWS:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
            self._log_line("—— worker 未响应, 已强制结束 ——\n")
        self.proc = None

    def _on_worker_exit(self, proc, code):
        if proc is not self.proc:
            return
        self._log_line(f"—— worker 结束 (退出码 {code}) ——\n")
        self.proc = None
        if self._sched_running:
            # 崩溃自愈: 清掉当前时块记号, 下次 tick 会重新进这个时块.
            self._set_running_block(None)
            self._set_status(f"worker 已退出, {_TICK_MS // 1000} 秒内自动重启", "idle")
        else:
            self._set_status("未运行", "off")
            self._set_start_btn(False)

    # ---- AFK ----
    def _persist_afk(self, enabled):
        self._persist_flag("afk_enabled", enabled)

    def _persist_flag(self, key, value):
        """通用: 把一个顶层 bool 开关落盘. 不做 CDP 写 —— 下一轮 worker 生效."""
        cfg = app_config.load_config()
        cfg[key] = bool(value)
        app_config.save_config(cfg)
        self._cfg = app_config.load_config()

    def _busy_modal(self, text):
        top = ctk.CTkToplevel(self, fg_color=theme.SURFACE)
        top.title("请稍候")
        theme.center_on(top, self, 340, 110)
        top.resizable(False, False)
        top.transient(self)
        top.protocol("WM_DELETE_WINDOW", lambda: None)
        ctk.CTkLabel(top, text=text, font=theme.font(13), text_color=theme.TEXT,
                     justify="center").pack(expand=True, padx=20, pady=(18, 6))
        bar = ctk.CTkProgressBar(top, mode="indeterminate", width=260,
                                 progress_color=theme.ACCENT)
        bar.pack(pady=(0, 18))
        bar.start()
        return top

    def _ensure_afk(self):
        if not _IS_WINDOWS:
            return
        if getattr(self, "_afk_busy", False):
            return
        exe_exists = os.path.isfile(afk_watch._EXE_PATH)
        running = afk_watch.is_florr_auto_afk_running()
        if running:
            self._log_line("AFK: already\n")
            return
        if not exe_exists:
            if not messagebox.askyesno(
                    "下载 florr-auto-afk?",
                    "没检测到 florr-auto-afk(处理 AFK 弹窗用). 现在下载? 约 350MB.",
                    parent=self):
                self._log_line("AFK: declined\n")
                return

        self._afk_busy = True
        self.afk_switch.configure(state="disabled")
        modal = self._busy_modal("AFK 助手准备中，请稍候…\n(界面仍可操作)")

        def _work():
            try:
                outcome = start_afk(exe_exists=exe_exists, running=False,
                                    confirm_download=lambda: True)
                if outcome in ("started", "downloaded"):
                    afk_watch.ensure_florr_auto_afk_running()
            except Exception as e:
                outcome = f"出错: {e}"
            self.after(0, self._finish_ensure_afk, modal, outcome)

        threading.Thread(target=_work, daemon=True).start()

    def _finish_ensure_afk(self, modal, outcome):
        self._afk_busy = False
        try:
            self.afk_switch.configure(state="normal")
        except Exception:
            pass
        try:
            modal.destroy()
        except Exception:
            pass
        self._log_line(f"AFK: {outcome}\n")

    def _on_afk_toggle(self):
        enabled = bool(self.afk_switch.get())
        if enabled:
            self._ensure_afk()
        else:
            afk_watch.stop_florr_auto_afk()
            self._log_line("AFK: 已停止 florr-auto-afk\n")
        self._persist_afk(enabled)

    # ---- 杂项 ----
    def _on_callback_exception(self, exc_type, exc_value, exc_tb):
        self._log_line(f"❌ {exc_type.__name__}: {exc_value}\n")
        traceback.print_exception(exc_type, exc_value, exc_tb)

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _log_line(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > _LOG_MAX_LINES:
            self.log_box.delete("1.0", f"end-{_LOG_MAX_LINES}l")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def on_closing(self):
        self._closing = True
        if self._tick_job is not None:
            try:
                self.after_cancel(self._tick_job)
            except Exception:
                pass
        self._stop_worker_sync()
        self.destroy()


def main():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
