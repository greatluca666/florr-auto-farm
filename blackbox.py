"""黑匣子: 控制面板在后台一直滚动录最近几分钟的画面, 用户点「上报」(主动反馈或出错弹窗)时把最近 5 分钟
连同本次运行的完整 worker 日志打成一个 zip 一起发(bug_report.submit 的第二步)。

为什么要一直录: 粉丝点反馈时 bug 往往已经过去了, 事先录着才拿得到「当时」。

录什么(帧的格式: 每拍一行 JSON, 带墙钟时间 t, 跟 logs/worker-*.log 的时间戳对得上, 离线能照着重跑认怪 / 索敌):
  - canvas 原始帧, 每秒 2 拍, **偷看**不 drain(不跟刷图程序抢帧);
  - 每 5 秒一张 0.5 倍 JPEG 截图(CDP Page.captureScreenshot);
  - Windows 上的鼠标位置(刷图程序靠鼠标方向走路)。
不录: 击杀计数器、按键。

**对刷图只读、什么都不注入**: 只做只读求值(偷看帧 / 量页面大小)和截图; 页面上没有 canvas hook 就只记一个
nohook(截图照拍), 永远不重载页面(注入 / 重载归刷图程序管)。

**不 import pyautogui / utils**: 它们在 Windows 上一 import 就改进程的 DPI 设置, 这里跑在控制面板进程里,
控制面板的 DPI 由 CustomTkinter 管, 不能被动到。鼠标位置用 ctypes 直接读。

写到 logs/blackbox/seg-YYYYmmdd-HHMMSS-ffffff/(frames.jsonl.gz + shots/<毫秒>.jpg), 60 秒换一段, 只留最近 6 段。
任何异常都吞掉: 黑匣子是附带的, 不能影响刷图和界面。
"""
import base64
import gzip
import io
import json
import os
import platform
import re
import shutil
import sys
import threading
import time
import zipfile
import zlib
from datetime import datetime

import bug_report
import cdp_bridge
import worker_log

FORMAT_VERSION = 1
HZ = 2.0
SHOT_EVERY_S = 5.0
SEGMENT_S = 60
KEEP_SEGMENTS = 6
RETRY_S = 5.0              # Chrome 不在 / 连接断了, 歇这么久再试
MAX_ERRORS = 10            # 连着这么多拍偷看出错(标签页没了 / 换号重开中), 断开重连


def blackbox_dir(root):
    return os.path.join(root, "logs", "blackbox")


def _win_cursor():
    import ctypes
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        raise OSError("GetCursorPos 失败")
    return pt.x, pt.y


_os_mouse = _win_cursor if sys.platform == "win32" else None


def _utc_offset_s():
    return int(datetime.now().astimezone().utcoffset().total_seconds())


# 偷看画布里最新的一帧, **不动 window.__canvasLog**: 刷图程序的 scan_enemies 每 0.12 秒从那里 drain 一次,
# 我们要是也 drain, 就会从它手里抢走半帧, 它解出半张画面。瘦身规则跟 canvas_decode 要的字段一致
# (CTM 缩放分量按有效数字留, 小地图那个 0.0084 按小数位截会把世界坐标带偏几十个单位)。
_PEEK_JS = """(() => {
  var log = window.__canvasLog || [];
  var recs = [], target = -1;
  if (log.length) {
    var newest = log[log.length - 1].frame;
    for (var i = log.length - 1; i >= 0; i--) {
      if (log[i].frame < newest) { target = log[i].frame; break; }
    }
    if (target >= 0) {
      for (var j = 0; j < log.length; j++) {
        if (log[j].frame === target) recs.push(slim(log[j]));
      }
    }
  }
  function r4(v) { return Math.round(v * 1e4) / 1e4; }
  function rp(v) { return v ? Number(v.toPrecision(9)) : v; }
  function slim(o) {
    var out = {frame: o.frame, op: o.op, x: r4(o.x), y: r4(o.y), r: o.r,
               fill: o.fill, stroke: o.stroke};
    if (o.bbox) out.bbox = [r4(o.bbox[0]), r4(o.bbox[1]), r4(o.bbox[2]), r4(o.bbox[3])];
    if (o.m) out.m = [rp(o.m[0]), rp(o.m[1]), rp(o.m[2]), rp(o.m[3]), r4(o.m[4]), r4(o.m[5])];
    if (o.text !== undefined) out.text = o.text;
    return out;
  }
  return {f: document.hasFocus(), fr: target, r: recs, hook: !!window.__canvasHookInstalled,
          w: window.innerWidth, h: window.innerHeight};
})()"""
# 刷图程序刚 drain 完的那一瞬页面里不到 2 帧, 偷看拿不到完整帧(2 帧/秒时约 30% 的拍子撞上)。隔一两帧再看一眼就有了。
PEEK_RETRIES = 3
PEEK_RETRY_S = 0.035


class _Recorder:
    """一段录像: tick() 记一拍(偷看一帧 + 到点就截一张图), 写进 <out_dir>/frames.jsonl.gz 和 shots/。
    所有跟外界打交道的(求值 / 截图 / 鼠标 / 时钟)都是注入点, 测试里换成假的。"""

    def __init__(self, out_dir, *, ev, screenshot, os_mouse=None, clock=time.time, sleep=time.sleep,
                 shot_every=SHOT_EVERY_S):
        self.out_dir = out_dir
        self.ev, self.screenshot, self.os_mouse = ev, screenshot, os_mouse
        self.clock, self.sleep, self.shot_every = clock, sleep, shot_every
        self.n_err = 0
        self.last_shot = -1e18
        self._f = None

    def open(self, meta):
        os.makedirs(os.path.join(self.out_dir, "shots"), exist_ok=True)
        self._f = gzip.open(os.path.join(self.out_dir, "frames.jsonl.gz"), "wt", encoding="utf-8")
        self._f.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")

    def close(self):
        f, self._f = self._f, None
        if f is not None:
            f.close()

    def tick(self):
        now = self.clock()
        try:
            peek = self.ev(_PEEK_JS)
            for _ in range(PEEK_RETRIES):
                if not peek or not peek.get("hook") or peek.get("r"):
                    break
                self.sleep(PEEK_RETRY_S)
                again = self.ev(_PEEK_JS) or {}
                peek = dict(peek, r=again.get("r"), fr=again.get("fr", -1))
        except Exception as e:
            self.n_err += 1
            rec = {"t": round(now, 3), "err": f"{type(e).__name__}: {e}"[:200]}
        else:
            rec = {"t": round(now, 3)}
            if peek is None:
                rec["err"] = "no_peek"
            else:
                rec["f"] = 1 if peek.get("f") else 0
                rec["fr"] = peek.get("fr", -1)
                rec["r"] = peek.get("r") or []
                if not peek.get("hook"):
                    rec["nohook"] = 1
                mouse = None
                if self.os_mouse is not None:
                    try:
                        mouse = self.os_mouse()
                    except Exception:
                        mouse = None
                if mouse is not None:
                    rec["om"] = [int(mouse[0]), int(mouse[1])]
        if self.shot_every > 0 and now - self.last_shot >= self.shot_every:
            self.last_shot = now
            try:
                name = f"shots/{int(now * 1000)}.jpg"
                with open(os.path.join(self.out_dir, name), "wb") as f:
                    f.write(self.screenshot())
                rec["shot"] = name
            except Exception as e:
                rec["shot_err"] = f"{type(e).__name__}"
        self._f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        return rec


class BlackBox:
    """一个后台线程, start() / stop()。_step() 是一拍(测试直接调), 返回下一拍前要等几秒。"""

    def __init__(self, root, *, hz=HZ, shot_every=SHOT_EVERY_S, segment_s=SEGMENT_S, keep=KEEP_SEGMENTS,
                 clock=time.time, reachable=None, session_factory=None, cdp_command=None, os_mouse=_os_mouse,
                 retry_s=RETRY_S, max_errors=MAX_ERRORS):
        self.dir = blackbox_dir(root)
        self.hz, self.shot_every, self.segment_s, self.keep = hz, shot_every, segment_s, keep
        self._clock = clock
        self._reachable = reachable or cdp_bridge.is_cdp_port_reachable
        self._session_factory = session_factory or (lambda: cdp_bridge.CdpSession(timeout=10))
        self._cdp = cdp_command or cdp_bridge._send_cdp_command
        self._os_mouse = os_mouse
        self.retry_s, self.max_errors = retry_s, max_errors
        self._lock = threading.Lock()           # 录制线程和打包线程(flush)共用
        self._stop = threading.Event()
        self._thread = None
        self._session = None
        self._rec = None
        self._seg_started = 0.0
        self._errors = 0

    # ---- 线程 ----
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="blackbox", daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None
        self._drop()

    def _run(self):
        while not self._stop.is_set():
            try:
                delay = self._step()
            except Exception:
                delay = self.retry_s
            self._stop.wait(delay)

    # ---- 一拍 ----
    def _step(self):
        try:
            if self._session is None:
                if not self._reachable():
                    return self.retry_s
                self._session = self._session_factory()
            t0 = self._clock()
            with self._lock:
                if self._rec is None or t0 - self._seg_started >= self.segment_s:
                    self._rotate()
                before = self._rec.n_err
                self._rec.tick()
                self._errors = self._errors + 1 if self._rec.n_err > before else 0
            if self._errors >= self.max_errors:
                self._drop()
                return self.retry_s
            return max(0.0, 1.0 / self.hz - (self._clock() - t0))
        except Exception:
            self._drop()
            return self.retry_s

    def flush(self):
        """把正在写的那段收尾(gzip 写完整); 下一拍开新段。打包前调。"""
        with self._lock:
            self._close_rec()

    # ---- 内部 ----
    def _close_rec(self):
        rec, self._rec = self._rec, None
        if rec is not None:
            try:
                rec.close()
            except Exception:
                pass

    def _drop(self):
        with self._lock:
            self._close_rec()
            s, self._session = self._session, None
            self._errors = 0
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    def _prune(self, keep):
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.startswith("seg-"))
        except OSError:
            return
        for n in names[:max(0, len(names) - keep)]:
            shutil.rmtree(os.path.join(self.dir, n), ignore_errors=True)

    def _rotate(self):
        self._close_rec()
        os.makedirs(self.dir, exist_ok=True)
        self._prune(self.keep - 1)
        now = self._clock()
        seg = os.path.join(self.dir, datetime.fromtimestamp(now).strftime("seg-%Y%m%d-%H%M%S-%f"))
        wh = self._session.eval_value("[window.innerWidth, window.innerHeight, window.devicePixelRatio]")
        w, h = int(wh[0]), int(wh[1])
        cdp = self._cdp

        def shot():
            resp = cdp("Page.captureScreenshot", {
                "format": "jpeg", "quality": 55,
                "clip": {"x": 0, "y": 0, "width": w, "height": h, "scale": 0.5}}, timeout=10)
            return base64.b64decode(resp["result"]["data"])

        rec = _Recorder(seg, ev=self._session.eval_value, screenshot=shot, os_mouse=self._os_mouse,
                        clock=self._clock, sleep=time.sleep, shot_every=self.shot_every)
        rec.open({"version": FORMAT_VERSION, "started": now, "hz": self.hz,
                  "shot_every": self.shot_every, "platform": platform.platform(),
                  "python": sys.version.split()[0], "canvas_hook_by": "worker", "source": "blackbox",
                  "utc_offset_s": _utc_offset_s(), "page": wh})
        self._rec, self._seg_started = rec, now


# ── 打包: 用户点「上报」时把最近 5 分钟 + 完整日志打成一个 zip ─────────────────────────

ATTACH_WINDOW_S = 300
MAX_ATTACH_BYTES = 15 * 1024 * 1024      # 服务端上限 20MB(stats_server.ATTACH_LIMIT), 留点余量
LOG_MAX_BYTES = 5 * 1024 * 1024          # 日志合计最多带这么多(留尾巴)
RECENT_LOG_S = 600                       # 最新那份之外, 这么久之内写过的 worker 日志也带(崩了重启会换文件)
KEEP_ATTACH = 5                          # logs/bug-reports/ 里只留最近这么多个附件 zip
SHRINK_TRIES = 3                         # 超大时把画面录像的时间窗砍半, 最多砍这么多次, 还超就不带画面录像

_T_RE = re.compile(r'^\{"t":\s*(-?[0-9.]+)')
_SHOT_RE = re.compile(r"^(\d{10,16})\.jpg$")


def _read_segment(path):
    """(meta, [(t, 整行)])。半截文件(录制时崩了 / 正在写)读到哪算哪; 没换行结尾的那行(写了一半)不要。
    帧只用正则取时间, 不整行解析(一帧几十 KB, 5 分钟几百帧)。"""
    meta, out = None, []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.endswith("\n"):
                    break
                m = _T_RE.match(line)
                if m:
                    out.append((float(m.group(1)), line))
                elif meta is None and line.startswith('{"_meta"'):
                    try:
                        meta = json.loads(line)["_meta"]
                    except (ValueError, KeyError, TypeError):
                        pass
    except (OSError, EOFError, zlib.error, ValueError, UnicodeDecodeError):
        pass
    return meta, out


def _collect(root):
    """所有段里的 (meta, 帧, 截图); 帧和截图都按时间排好。"""
    d = blackbox_dir(root)
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith("seg-"))
    except OSError:
        return None, [], []
    meta, frames, shots = None, [], []
    for n in names:
        seg = os.path.join(d, n)
        m, fr = _read_segment(os.path.join(seg, "frames.jsonl.gz"))
        if m is not None and fr:
            meta = m                      # 用有帧的最新那段的 meta(utc_offset / 页面大小)
        frames.extend(fr)
        try:
            for s in os.listdir(os.path.join(seg, "shots")):
                mm = _SHOT_RE.match(s)
                if mm:
                    shots.append((int(mm.group(1)) / 1000.0, os.path.join(seg, "shots", s)))
        except OSError:
            pass
    frames.sort(key=lambda x: x[0])
    shots.sort(key=lambda x: x[0])
    return meta, frames, shots


def _tail_bytes(path, budget):
    """文件最后 budget 字节(调用方多要一点, 脱敏以后再按整行切)。"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > budget:
            f.seek(size - budget)
        return f.read()


def _cut_to_lines(data, budget):
    if len(data) <= budget:
        return data
    cut = data[-budget:]
    i = cut.find(b"\n")
    return cut[i + 1:] if i >= 0 else b""


def _pick_logs(root, now, recent_s):
    d = worker_log.log_dir(root)
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith("worker-") and n.endswith(".log"))
    except OSError:
        return []
    pick = set(names[-1:])
    for n in names:
        try:
            if os.path.getmtime(os.path.join(d, n)) >= now - recent_s:
                pick.add(n)
        except OSError:
            pass
    return sorted(pick)


def _log_payloads(root, names, aliases, budget):
    """[(文件名, 脱敏后的字节)], 从最新的往前填预算, 每份留尾巴、从整行切开; 预算用完更旧的不带。"""
    out = []
    for name in reversed(names):
        if budget <= 0:
            break
        try:
            raw = _tail_bytes(os.path.join(worker_log.log_dir(root), name), budget + 4096)
        except OSError:
            continue
        lines = raw.decode("utf-8", "replace").splitlines()
        cut = len(raw) > budget
        if cut:
            lines = lines[1:]             # 第一行多半是从中间截开的
        full = "".join(bug_report.redact(ln, aliases) + "\n" for ln in lines).encode("utf-8")
        data = _cut_to_lines(full, budget)
        if data:
            out.append((name, data))
            budget -= len(data)
        if cut or len(data) < len(full):
            break                         # 这份都没装下: 更旧的只会剩些碎片, 不带
    return list(reversed(out))


def _write_zip(path, meta, frames, shots, logs, manifest):
    with zipfile.ZipFile(path, "w") as z:
        if frames:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as g:
                g.write((json.dumps({"_meta": meta}, ensure_ascii=False) + "\n").encode("utf-8"))
                for _t, line in frames:
                    g.write(line.encode("utf-8"))
            z.writestr("frames.jsonl.gz", buf.getvalue(), compress_type=zipfile.ZIP_STORED)
        for _t, p in shots:
            z.write(p, f"shots/{os.path.basename(p)}", compress_type=zipfile.ZIP_STORED)
        for name, data in logs:
            z.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2),
                   compress_type=zipfile.ZIP_DEFLATED)


def _prune_attachments(d, keep):
    try:
        names = sorted(n for n in os.listdir(d) if n.startswith("attach-") and n.endswith(".zip"))
    except OSError:
        return
    for n in names[:max(0, len(names) - keep)]:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


def pack(root, *, aliases=(), now=time.time, window_s=ATTACH_WINDOW_S, max_bytes=MAX_ATTACH_BYTES,
         log_max=LOG_MAX_BYTES, keep=KEEP_ATTACH, recent_logs_s=RECENT_LOG_S):
    """把黑匣子最近 window_s 秒(从**最后录到的那一刻**往前算, 不是从现在)的画面录像 + 截图, 加上最新的
    worker 日志(和 recent_logs_s 内写过的)打成 logs/bug-reports/attach-<时间>.zip, 返回
    {"path", "bytes", "from", "to", "frames", "shots", "logs", "window_s"}; 什么都没有 -> None。
    超过 max_bytes 先把画面录像的时间窗砍半(留新的那头), 截图和日志不砍。"""
    meta, frames, shots = _collect(root)
    logs = _log_payloads(root, _pick_logs(root, now(), recent_logs_s), aliases, log_max)
    newest = max([x[0] for x in frames[-1:] + shots[-1:]], default=None)
    if newest is None and not logs:
        return None
    sel_shots = [s for s in shots if s[0] >= newest - window_s] if newest is not None else []
    out_dir = bug_report.reports_dir(root)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, datetime.now().strftime("attach-%Y%m%d-%H%M%S-%f.zip"))
    tmp = path + ".part"
    frame_window = float(window_s)
    try:
        for attempt in range(SHRINK_TRIES + 2):
            if attempt > SHRINK_TRIES:
                frame_window = -1.0                       # 砍了几次还超: 不带画面录像
            sel = ([f for f in frames if f[0] >= newest - frame_window]
                   if newest is not None and frame_window >= 0 else [])
            times = [x[0] for x in sel + sel_shots]
            manifest = {"from": min(times) if times else None, "to": newest if times else None,
                        "frames": len(sel), "shots": len(sel_shots), "logs": [n for n, _ in logs],
                        "window_s": int(window_s)}
            m = dict(meta or {}, source="blackbox")
            if sel:
                m["started"] = sel[0][0]
            _write_zip(tmp, m, sel, sel_shots, logs, manifest)
            if os.path.getsize(tmp) <= max_bytes or not sel:
                break
            frame_window /= 2
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    _prune_attachments(out_dir, keep)
    return dict(manifest, path=path, bytes=os.path.getsize(path))


class PackJob:
    """弹窗一开就在后台打包: 先让黑匣子把正在写的那段收尾(box.flush), 再 pack。done() / result() 不阻塞,
    result() 在打包好之前、或者打包出错时是 None(照样能只发文字报告)。"""

    def __init__(self, root, *, aliases=(), box=None, pack_fn=None):
        pack_fn = pack_fn or pack
        self._result = None
        self._event = threading.Event()

        def run():
            try:
                if box is not None:
                    box.flush()
                self._result = pack_fn(root, aliases=list(aliases or ()))
            except Exception:
                self._result = None
            finally:
                self._event.set()

        threading.Thread(target=run, name="blackbox-pack", daemon=True).start()

    def done(self):
        return self._event.is_set()

    def result(self):
        return self._result if self._event.is_set() else None

    def wait(self, timeout=None):
        return self._event.wait(timeout)
