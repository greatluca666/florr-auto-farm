"""「要不要上报」窗(bug_report.py 的界面), 两种来路共用:
  - 程序检测到出错时自己弹(自动类 kind): 说明框可选;
  - 用户随时点侧栏「反馈问题」(kind=user_report): 说明框必填, 没写不能发 —— 不用等出错。

用户点了「上报」才发, 「不上报」/「取消」/ 关窗口 / Esc 就什么都不发。窗里展示的是将要发送的全部内容
(bug_report.render_preview), 并且**随着用户写的说明实时更新**(说明发出去之前会脱敏, 用户能直接看到
脱敏后的样子); 预览和实际发出去的是同一个 with_note 算出来的。

非模态: 不 grab_set、不 -topmost —— worker 在后台继续跑(很多人挂机时不在电脑前), 窗口就在那儿等,
用户有空再看; 不会盖住游戏或挡住别的窗口。

附件(2026-10-07): 一律连同最近 5 分钟的画面录像、截图和本次运行的完整日志一起发(blackbox.py 打包, 窗口一开就在
后台打, 打好之前「上报」是灰的), 预览里列出时间范围 / 张数 / 大小, 「打开附件所在文件夹」能看到那个 zip。
"""
import os
import subprocess
import sys

import customtkinter as ctk

import bug_report
import gui_theme as theme


def _open_folder(path):
    """用系统的文件管理器打开一个文件夹。打不开就算了。"""
    try:
        if sys.platform == "win32":
            os.startfile(path)                                    # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


_ATTACH_NOTE = ("会连同最近 5 分钟的游戏截图和画面录像、本次运行的完整日志一起发(截图里能看到你的账号名、"
                "别的玩家的名字和聊天; 日志按同样的规则脱敏)。")


class BugReportDialog(ctk.CTkToplevel):
    """payload: bug_report.prepare / prepare_manual 给的载荷。on_submit(payload, note, done): 用户点了
    「上报」时调, 调用方负责真正上传, 完成后在**界面线程**里调 done(ok, 一句话)。on_close(): 窗口销毁时
    调一次。aliases: 用户的 Chrome profile 别名, 实时预览里的说明要按它脱敏(跟上传时一致)。
    attachment_job: blackbox.PackJob(done() / result()), 附件在后台打包; None = 没有附件。"""

    _EXPLAIN = ("程序刚才出了个错。可以把下面这份脱敏后的信息发给开发者帮忙修 —— 你点「上报」才会发, "
                "不点什么都不会发。" + _ATTACH_NOTE + "想先看看发什么: 下面就是全部内容。")
    _EXPLAIN_MANUAL = ("遇到了问题、觉得哪里不对、想提个建议, 都可以在这里告诉开发者, 不用等程序报错。"
                       "先写几句说明, 点「上报」才会发, 不点什么都不会发。" + _ATTACH_NOTE +
                       "下面是会一起发出去的全部内容, 随你写的说明实时更新, 发之前可以先看看。")
    _NOTE_HINT = "补充说明(可选, 比如你当时在干什么)。发出去之前会同样脱敏。"
    _NOTE_HINT_MANUAL = "你的说明(必填): 发生了什么、你原本期望怎样、怎么能重现。发出去之前会同样脱敏。"
    _SAVED_HINT = "点了「上报」之后, 发出的内容原样存一份在 logs/bug-reports/。"

    _POLL_MS = 200

    def __init__(self, master, payload, *, on_submit, on_close=None, aliases=(), attachment_job=None):
        super().__init__(master, fg_color=theme.BG)
        self._manual = payload.get("kind") == bug_report.MANUAL_KIND
        self.title("反馈问题" if self._manual else "发现问题 — 要不要上报?")
        theme.center_on(self, master, 720, 660, min_size=(680, 420))     # 底栏: 状态 + 打开文件夹 + 两个按钮
        self.resizable(True, True)
        self.transient(master)
        self._payload = payload
        self._aliases = list(aliases or ())
        self._on_submit = on_submit
        self._on_close = on_close
        self._pending = False
        self._sent = False
        self._closed = False
        self._job = attachment_job
        self._att = None
        self._att_pending = attachment_job is not None
        self._build()
        self._poll_attachment()
        self._refresh_preview()
        self._sync()
        self.protocol("WM_DELETE_WINDOW", self._dismiss)
        self.bind("<Escape>", lambda _e: self._dismiss())
        if self._manual:
            try:
                self._note.focus_set()              # 主动反馈: 光标直接在说明框里
            except Exception:
                pass

    def _build(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=14, pady=(12, 0))
        body.grid_columnconfigure(0, weight=1)
        # 自动弹出的: 先看内容, 说明在下面(可选); 主动反馈: 说明是正文, 放在预览上面
        preview_row, note_row = (2, 1) if self._manual else (1, 2)
        body.grid_rowconfigure(preview_row, weight=1)

        head = theme.card(body)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        if self._manual:
            theme.section_title(head, "反馈问题", "随时可以反馈, 不用等出错").pack(
                anchor="w", fill="x", padx=14, pady=(12, 2))
        else:
            theme.section_title(head, "发现问题", self._payload.get("msg", "")[:80]).pack(
                anchor="w", fill="x", padx=14, pady=(12, 2))
        theme.hint(head, text=self._EXPLAIN_MANUAL if self._manual else self._EXPLAIN,
                   wraplength=580).pack(anchor="w", fill="x", padx=14, pady=(0, 12))

        self._preview = ctk.CTkTextbox(body, font=theme.mono(11), fg_color=theme.LOG_BG,
                                       text_color="#c9ced6", corner_radius=8, wrap="word")
        self._preview.grid(row=preview_row, column=0, sticky="nsew")

        note_card = theme.card(body)
        note_card.grid(row=note_row, column=0, sticky="ew", pady=(0, 10) if self._manual else (10, 0))
        theme.hint(note_card, text=self._NOTE_HINT_MANUAL if self._manual else self._NOTE_HINT,
                   wraplength=580).pack(anchor="w", fill="x", padx=14, pady=(10, 4))
        self._note = ctk.CTkTextbox(note_card, font=theme.font(13), wrap="word",
                                    height=110 if self._manual else 56)
        self._note.pack(fill="x", padx=14, pady=(0, 4))
        self._count = theme.hint(note_card, text="", wraplength=580)
        self._count.pack(anchor="e", padx=14, pady=(0, 2))
        theme.hint(note_card, text=self._SAVED_HINT, wraplength=580).pack(
            anchor="w", fill="x", padx=14, pady=(0, 10))
        self._note.bind("<<Modified>>", self._on_note_edited)

        bar = ctk.CTkFrame(self, fg_color=theme.SIDEBAR, corner_radius=0)
        bar.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        bar.grid_columnconfigure(0, weight=1)
        self._status = ctk.CTkLabel(bar, text="", text_color=theme.MUTED, font=theme.font(13),
                                    anchor="w", justify="left", wraplength=300)
        self._status.grid(row=0, column=0, sticky="w", padx=16)
        self._open_btn = theme.ghost_button(bar, "打开附件所在文件夹", self._open_attachment_folder,
                                            width=150, height=34)
        self._open_btn.grid(row=0, column=1, padx=(0, 8), pady=12)
        self._open_btn.grid_remove()                                   # 附件打好了才露出来
        self._no_btn = theme.ghost_button(bar, "取消" if self._manual else "不上报", self._dismiss,
                                          width=84, height=34)
        self._no_btn.grid(row=0, column=2, padx=(0, 8), pady=12)
        self._yes_btn = theme.primary_button(bar, "上报", self._submit, width=84, height=34)
        self._yes_btn.grid(row=0, column=3, padx=(0, 16), pady=12)

    # ---- 说明框 / 预览 ----
    def _note_text(self):
        return self._note.get("1.0", "end-1c")

    def _note_redacted(self):
        return bug_report.redact(self._note_text(), self._aliases).strip()

    # ---- 附件 ----
    def _poll_attachment(self):
        """附件打好了没: 好了就更新预览、放开「上报」; 没好过一会儿再看。"""
        if self._closed or not self._att_pending:
            return
        if self._job.done():
            self._att_pending = False
            self._att = self._job.result()
            if self._att:
                self._open_btn.grid()
            self._refresh_preview()
            self._sync()
            return
        self.after(self._POLL_MS, self._poll_attachment)

    def _open_attachment_folder(self):
        if self._att and self._att.get("path"):
            _open_folder(os.path.dirname(self._att["path"]))

    def _can_send(self):
        if self._pending or self._sent or self._closed or self._att_pending:
            return False
        return bool(self._note_redacted()) if self._manual else True   # 主动反馈的正文就是说明, 必填

    def _sync(self):
        """按当前状态(上传中 / 已发 / 说明写没写)刷新按钮、说明框能不能改、字数。"""
        busy = self._pending or self._sent
        self._yes_btn.configure(state="normal" if self._can_send() else "disabled")
        self._no_btn.configure(state="disabled" if self._pending else "normal")
        self._note.configure(state="disabled" if busy else "normal")   # 发出去的和看到的不能再变
        n = len(self._note_redacted())
        over = n > bug_report.MAX_NOTE
        self._count.configure(
            text=f"{n}/{bug_report.MAX_NOTE}" + ("  超出的部分不会发送" if over else ""),
            text_color=theme.WARN if over else theme.MUTED)

    def _refresh_preview(self):
        """预览 = 载荷 + 用户写的说明(脱敏后) —— 就是 submit 时 with_note 算出来的那份。"""
        if self._closed:
            return
        body = bug_report.with_note(self._payload, self._note_text(), self._aliases)
        at = self._preview.yview()[0]
        self._preview.configure(state="normal")
        self._preview.delete("1.0", "end")
        self._preview.insert("1.0", bug_report.render_preview(body, attachment=self._att,
                                                             packing=self._att_pending))
        self._preview.configure(state="disabled")                      # 只读: 用户只能看
        self._preview.yview_moveto(at)                                 # 边写边看, 别每敲一个字就跳回顶部

    def _on_note_edited(self, _e=None):
        if self._closed:
            return
        box = self._note._textbox
        if not box.edit_modified():                                    # 是下面把标志复位又触发的, 不是编辑
            return
        box.edit_modified(False)
        self._refresh_preview()
        self._sync()

    # ---- 发送 / 关闭 ----
    def _submit(self):
        if not self._can_send():
            return
        note = self._note_text()
        self._pending = True
        self._sync()
        self._status.configure(text="上报中…", text_color=theme.MUTED)
        self._on_submit(self._payload, note, self._finished)

    def _finished(self, ok, msg):
        """上传结果(调用方在界面线程里调)。窗口已经没了就算了。"""
        if self._closed:
            return
        self._pending = False
        if ok:
            self._sent = True
            self._status.configure(text="✅ " + msg, text_color=theme.SUCCESS)
            self._no_btn.configure(text="关闭")
        else:                         # 失败: 弹窗留着, 「上报」重新可点(说明还在的话), 用户可以再试
            self._status.configure(text="⚠ " + msg, text_color=theme.WARN)
        self._sync()

    def _dismiss(self):
        if self._pending:             # 正在上传时别关: 结果还要回来告诉用户
            return
        self.destroy()

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                pass
        super().destroy()
