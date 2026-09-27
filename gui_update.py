"""控制面板里的「发现新版本」横幅, 以及它背后的线程协调. 查版本/下载/验签/替换的逻辑
都在 updater.py; 这里只管界面和「后台线程 → root.after 回主线程」的切换.
能单独测的判断放在模块级纯函数和 UpdateController 里(测试不需要 Tk)."""
import os
import threading
import tkinter
import webbrowser
from tkinter import messagebox

import customtkinter as ctk

import gui_theme as theme
import updater
import version

_MB = 1024 * 1024


def version_label(v):
    return f"版本 v{v}" if updater.parse_version(v) else "版本 开发版"


def describe_available(info, can_install):
    if can_install:
        return f"发现新版本 v{info.version}"
    return f"发现新版本 v{info.version}, 源码运行请用 git pull 更新"


def format_progress(done, total):
    if total:
        return f"正在下载 {done / _MB:.1f} / {total / _MB:.1f} MB ({done * 100 // total}%)"
    return f"正在下载 {done / _MB:.1f} MB"


class UpdateBanner(ctk.CTkFrame):
    """页面标题上方的一条横幅. 默认不显示; 任何 show_* 都会把它 pack 到 before 控件前面."""

    def __init__(self, master, before, on_update, on_notes, on_retry):
        super().__init__(master, fg_color=theme.SURFACE, corner_radius=10,
                         border_width=1, border_color=theme.ACCENT)
        self._before = before
        self.grid_columnconfigure(0, weight=1)
        self._label = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                   font=theme.font(13), text_color=theme.TEXT)
        self._label.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=8)
        self._bar = ctk.CTkProgressBar(self, progress_color=theme.ACCENT)
        self._notes_btn = theme.ghost_button(self, "查看更新内容", on_notes, width=100, height=28)
        self._update_btn = theme.primary_button(self, "立即更新", on_update, width=90, height=28)
        self._retry_btn = theme.ghost_button(self, "重试", on_retry, width=56, height=28)
        self._site_btn = theme.ghost_button(self, "去官网下载",
                                            lambda: webbrowser.open(updater.SITE_URL),
                                            width=90, height=28)
        self._close_btn = theme.ghost_button(self, "×", self.hide, width=28, height=28)
        self._buttons = (self._notes_btn, self._update_btn, self._retry_btn,
                         self._site_btn, self._close_btn)

    def _layout(self, text, color, buttons, progress=None):
        self._label.configure(text=text, text_color=color)
        for b in self._buttons:
            b.grid_forget()
        for i, b in enumerate(buttons, start=1):
            b.grid(row=0, column=i, padx=(0, 8), pady=8)
        if progress is None:
            self._bar.grid_forget()
        else:
            self._bar.grid(row=1, column=0, columnspan=len(buttons) + 1, sticky="ew",
                           padx=12, pady=(0, 10))
            self._bar.set(progress)
        self.pack(fill="x", pady=(0, 10), before=self._before)

    def show_available(self, text, can_install):
        buttons = [self._notes_btn] + ([self._update_btn] if can_install else []) + [self._close_btn]
        self._layout(text, theme.TEXT, buttons)

    def show_progress(self, text, fraction):
        self._layout(text, theme.TEXT, [], progress=0 if fraction is None else fraction)

    def show_error(self, text):
        self._layout(text, theme.DANGER, [self._retry_btn, self._site_btn, self._close_btn])

    def show_message(self, text, ok=True):
        self._layout(text, theme.SUCCESS if ok else theme.DANGER, [self._close_btn])

    def hide(self):
        self.pack_forget()


class UpdateController:
    """启动检查 / 手动检查 / 一键更新. 网络和解压都在后台线程, 结果用 root.after(0, ...)
    回主线程再动界面(和 gui_app 里 worker 日志线程是同一个模式)."""

    def __init__(self, root, banner, *, log, is_busy, stop_all, quit_app,
                 is_closing=lambda: False, current=version.__version__):
        self._root = root
        self._banner = banner
        self._log = log
        self._is_busy = is_busy
        self._stop_all = stop_all
        self._quit_app = quit_app
        self._is_closing = is_closing
        self._current = current
        self._info = None
        self._checking = False
        self._updating = False

    def _post(self, fn, *args):
        """后台线程往主线程发回调. 窗口正在/已经关掉时不发 —— 关窗口和这次回调之间
        总有个时间差, root 可能在检查之后、真正调用 after() 之前就被销毁了, 所以两头都要挡."""
        if self._is_closing():
            return
        try:
            self._root.after(0, fn, *args)
        except (RuntimeError, tkinter.TclError):
            pass

    # ---- 检查 ----
    def check(self, manual=False):
        if self._checking or self._updating:
            return
        self._checking = True
        threading.Thread(target=self._check_worker, args=(manual,), daemon=True).start()

    def _check_worker(self, manual):
        problem = None
        if updater.enabled():
            # 先看上次更新有没有完成, 再清掉它留下的 *.old-update / .update/. 放在检查线程里:
            # check() 在更新中不会启动它, 「立即更新」按钮也要等它跑完才出现, 所以清理
            # 不可能和 stage() 同时进行
            try:
                install = updater.install_dir()
                problem = updater.last_update_problem(install)
                if problem:
                    updater.acknowledge_update_problem(install)
                updater.cleanup_leftovers(install)
            except Exception as e:
                self._post(self._log, f"清理上次更新的残留失败: {e}\n")
        try:
            info, err = updater.check_latest(self._current), None
        except Exception as e:
            info, err = None, e
        self._post(self._on_checked, manual, info, err, problem)

    def _on_checked(self, manual, info, err, problem=None):
        self._checking = False
        last = f"上次更新没完成: {problem}" if problem else None
        if last:
            self._log(last + "\n")
        if err is not None:
            self._log(f"检查更新失败: {err}\n")
            if last:
                self._banner.show_error(last)
            elif manual:
                self._banner.show_error(f"检查更新失败: {err}")
            return
        if info is None:
            self._log(f"已是最新版本({version_label(self._current)})\n")
            if last:
                self._banner.show_error(last)
            elif manual:
                self._banner.show_message("已是最新版本")
            return
        self._info = info
        can_install = updater.enabled()
        text = describe_available(info, can_install)
        self._log(text + "\n")
        if not can_install and not manual:
            return          # 源码运行: 启动时只写日志, 不每次都弹横幅; 手动检查才显示
        self._banner.show_available(text + (f" —— {last}" if last else ""), can_install)

    def show_notes(self):
        if self._info is None:
            return
        top = ctk.CTkToplevel(self._root, fg_color=theme.SURFACE)
        top.title(f"v{self._info.version} 更新内容")
        theme.center_on(top, self._root, 520, 420)
        top.transient(self._root)
        box = ctk.CTkTextbox(top, font=theme.font(13), wrap="word",
                             fg_color=theme.LOG_BG, text_color=theme.TEXT)
        box.pack(fill="both", expand=True, padx=12, pady=12)
        box.insert("1.0", self._info.notes or "(这个版本没有写更新说明)")
        box.configure(state="disabled")

    def is_updating(self):
        return self._updating

    def retry(self):
        if self._info is not None and updater.enabled():
            self.start_update()
        else:
            self.check(manual=True)

    # ---- 更新 ----
    def start_update(self):
        info = self._info
        if info is None or self._updating or not updater.enabled():
            return
        if self._is_busy() and not messagebox.askyesno(
                "更新", "更新会停止当前调度和正在运行的 worker, 继续?", parent=self._root):
            return
        self._stop_all()
        self._updating = True
        self._banner.show_progress("准备下载…", 0)
        threading.Thread(target=self._update_worker, args=(info,), daemon=True).start()

    def _update_worker(self, info):
        install = updater.install_dir()
        zip_path = install / updater.DOWNLOAD_NAME
        last = {"key": None}

        def progress(done, total):
            # 每个百分点(总大小未知时每 MB)才刷新一次界面, 别把 after 队列塞满
            key = done * 100 // total if total else done // _MB
            if key != last["key"]:
                last["key"] = key
                self._post(self._banner.show_progress, format_progress(done, total),
                          done / total if total else None)

        try:
            digest = updater.download(info, zip_path, progress)
            self._post(self._banner.show_progress, "正在校验签名…", 1)
            updater.verify(info, digest)
            staged = updater.stage(zip_path, install)
            os.remove(zip_path)
            self._post(self._banner.show_progress, "正在启动替换脚本…", 1)
            updater.launch_swap(install, staged, os.getpid())
            # 脚本真跑起来了才能退出; 被杀毒软件拦了还退出, 就没人把程序重新打开了.
            # 这种情况下程序留着, 脚本(如果晚些起来)等 60 秒等不到退出会自己放弃
            if not updater.wait_for_swap_start(install, os.getpid()):
                raise updater.UpdateError(
                    "更新脚本没能启动(可能被杀毒软件拦截了), 请到官网手动下载新版本")
        except Exception as e:
            self._post(self._on_update_failed, e)
            return
        self._post(self._on_swap_launched, info)

    def _on_update_failed(self, err):
        self._updating = False
        self._log(f"更新失败: {err}\n")
        self._banner.show_error(f"更新失败: {err}")

    def _on_swap_launched(self, info):
        self._log(f"正在更新到 v{info.version}: 程序马上关闭, 替换完会自动重新打开\n")
        self._updating = False  # 先清掉, 免得下面 quit_app 触发的关窗口确认又把自己拦下来
        self._quit_app()
