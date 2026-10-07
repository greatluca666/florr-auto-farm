"""bug 上报: worker 抛了 traceback / 异常退出 / 界面回调抛异常时, 控制面板弹窗问用户要不要把
脱敏后的诊断信息发给开发者(florrfarm.cc.cd/api/bug), 开发者在后台面板里按问题归并着看。
**用户点了「上报」才发, 不点什么都不发** —— 没有开关, 也没有后台悄悄上传。

不一定要等出错: 侧栏的「反馈问题」随时可以点, 用户自己写一段说明(必填), 连同下面这些环境信息一起发
(kind=user_report, 见 prepare_manual)。这条路没有 traceback, 标题取说明的第一行; 因为是用户主动点的,
不受下面「6 小时问一次 / 一天 10 次」的限制(那两条是防弹窗骚扰用户自己的)。

弹窗里展示的就是将要发送的全部内容(render_preview), 另有一个可选的「补充说明」框:
  安装 ID、程序版本、系统、Python 版本、是不是打包版、
  问题类型 + 签名 + 异常那一行、traceback、worker 输出的最后 60 行、
  当前时块的 地图 / 索敌开关 / 战斗模式 / 反转开关 / 索敌规则、用户自己写的说明。
**附件(2026-10-07 起, 一律带, 不能取消)**: 黑匣子(blackbox.py)录的最近 5 分钟画面录像(canvas 原始帧)+
截图(每 5 秒一张)+ 本次运行的完整 worker 日志(逐行同样脱敏), 打成一个 zip, 报告发出去之后用服务端给的
一次性令牌接着传(submit 的第二步)。截图和画面录像**没法脱敏** —— 里面能看到账号名、别的玩家的名字和聊天,
弹窗里写明了。
**不发**: 账号别名和 Chrome profile 目录、用户目录路径里的系统用户名(C:\\Users\\<名字>\\...)、
邮箱、IP、URL 的查询串、长得像密钥的长串(以上是文字部分和日志的脱敏)、config.json 原文。
脱敏是尽力而为的正则, 管不到游戏里别的玩家的昵称 —— worker 输出里偶尔会有(比如「未识别怪物名」),
所以才让用户在发之前先看一遍。

点了上报之后, 发出的内容原样存一份到 logs/bug-reports/(最近 20 份; 附件 zip 也在那里, 最近 5 个)。同一个问题 6 小时内只问一次
(不管选了什么), 一天最多问 10 次。FLORR_TELEMETRY=0(和匿名使用统计共用)就一个弹窗都不弹。

和 telemetry.py 一样: 上传在后台线程里做, 失败只在弹窗里提一句(可以再点一次), 绝不抛出来 ——
这条路自己出问题不能反过来弄崩控制面板或 worker 输出泵。
"""
import collections
import hashlib
import json
import os
import platform
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime

import telemetry
import version

ENDPOINT = "https://florrfarm.cc.cd/api/bug"
TIMEOUT_S = 10
ATTACH_TIMEOUT_S = 120     # 附件几 MB, 粉丝网慢时要传一会儿
ATTACH_TRIES = 3           # 附件传失败再试几次(文字报告已经到了, 只重传附件)
ATTACH_RETRY_S = 2.0

AUTO_KINDS = ("worker_traceback", "worker_exit", "gui_exception")    # 程序检测到的, 弹窗问用户
MANUAL_KIND = "user_report"                                           # 用户主动点「反馈问题」
KINDS = AUTO_KINDS + (MANUAL_KIND,)

MAX_TB = 8192              # traceback 最多留这么多字符(留尾巴: 离出错点近的帧和异常那一行)
MAX_TB_LINES = 300         # 流里识别 traceback 时最多缓存这么多行
MAX_MSG = 300
MAX_LINE = 300
MAX_LOG = 8192
TAIL_LINES = 60            # 报告里带 worker 输出的最后几行
TAIL_KEEP = 200            # 泵线程里滚动留多少行(每个 traceback 识别完成时取尾巴)
SAME_SIG_COOLDOWN_S = 6 * 3600     # 同一个问题问过一次(不管用户选了什么), 6 小时内不再问
MAX_PER_DAY = 10                   # 一天最多问这么多次
MAX_NOTE = 1000                    # 用户补充说明的长度上限(主动反馈时说明就是正文, 所以比较宽)
KEEP_LOCAL = 20

_CTX_KEYS = ("map", "enemy_ai_enabled", "auto_switch_server", "combat",
             "invert_attack", "invert_defense", "enemy_rules")


# ── 脱敏 ─────────────────────────────────────────────────────────────────────

_HOME_WIN = re.compile(r"(?i)\b([A-Z]:[\\/]+Users[\\/]+)[^\\/\s:\"'<>|]+")
_HOME_POSIX = re.compile(r"(/Users/|/home/)[^/\s:\"'<>|]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_URL_CRED = re.compile(r"(https?://)[^/\s@\"'<>]+@")
_URL_QUERY = re.compile(r"(https?://[^\s\"'<>?#]+)[?#][^\s\"'<>]*")
# 长串且字母数字都有(密钥 / 哈希); 纯字母的长标识符(函数名)和纯数字不动
_TOKEN = re.compile(r"(?<![\w/\\.-])(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}(?![\w])")
_DEFAULT_ALIAS = "默认"


def redact(text, aliases=()):
    """脱敏一段文本。aliases: 用户的 Chrome profile 别名。幂等, 对垃圾入参也不抛。"""
    if text is None:
        return ""
    text = str(text)
    for a in aliases or ():
        if isinstance(a, str) and len(a) >= 2 and a != _DEFAULT_ALIAS:
            text = text.replace(a, "<profile>")
    text = _HOME_WIN.sub(r"\1<user>", text)
    text = _HOME_POSIX.sub(r"\1<user>", text)
    text = _URL_CRED.sub(r"\1", text)
    text = _URL_QUERY.sub(r"\1", text)
    text = _EMAIL.sub("<email>", text)
    text = _IPV4.sub("<ip>", text)
    return _TOKEN.sub("<token>", text)


# ── traceback 识别 ───────────────────────────────────────────────────────────

_TB_HEAD = "Traceback (most recent call last):"
_CHAIN_LINES = ("During handling of the above exception, another exception occurred:",
                "The above exception was the direct cause of the following exception:")


class TracebackScanner:
    """从 worker 的输出流里认 traceback 块。feed(line) 在一个块完整时返回它的文本(否则 None);
    流结束时 flush() 取还没吐出来的那个。链式异常(During handling... / direct cause)合成一份。
    块是在「异常那一行之后的下一行不是链式延续」时才算完, 所以要等下一行到(或 flush)。"""

    def __init__(self):
        self._buf = None                  # None = 不在 traceback 里
        self._state = "idle"              # idle / frames / after

    def _emit(self):
        buf, self._buf, self._state = self._buf, None, "idle"
        while buf and not buf[-1].strip():
            buf.pop()
        return "\n".join(buf) if buf else None

    def _new(self, line):
        self._buf = collections.deque([line], maxlen=MAX_TB_LINES)
        self._state = "frames"

    def feed(self, line):
        s = str(line).rstrip("\r\n")
        if self._state == "idle":
            if s.startswith(_TB_HEAD):
                self._new(s)
            return None
        if self._state == "frames":
            self._buf.append(s)
            if not s.startswith((" ", "\t")) and not s.startswith(_TB_HEAD):
                self._state = "after"     # 第一条不缩进的行 = 异常那一行
            return None
        # after: 看是不是链式延续
        if s == "" or s in _CHAIN_LINES:
            self._buf.append(s)
            return None
        if s.startswith(_TB_HEAD):
            self._buf.append(s)
            self._state = "frames"
            return None
        done = self._emit()
        if s.startswith(_TB_HEAD):        # (保险: 上面已处理) 紧挨着的新块
            self._new(s)
        return done

    def flush(self):
        return self._emit() if self._buf is not None else None


_FRAME_RE = re.compile(r'^\s*File "(.*?)", line \d+, in (.+?)\s*$')


def _last_traceback(tb):
    lines = str(tb).splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(_TB_HEAD):
            return lines[i:]
    return lines


def _exception_line(tb):
    """traceback 文本里最后一条「异常那一行」(链式的也取最后一个块的)。"""
    for ln in reversed(_last_traceback(tb)):
        if ln.strip() and not ln.startswith((" ", "\t")) and not ln.startswith(_TB_HEAD) \
                and ln not in _CHAIN_LINES:
            return ln.strip()
    return ""


def signature(kind, tb="", exit_code=None):
    """把「同一个问题」归成一组的 12 位指纹: 异常类型 + 最后三帧的 文件名:函数名。
    不含行号 / 用户目录 / 异常消息, 所以换电脑、小改动、消息里带数字都归同一组。"""
    if kind == "worker_exit":
        key = f"{kind}|exit|{exit_code}"
    else:
        frames = []
        for ln in _last_traceback(tb):
            m = _FRAME_RE.match(ln)
            if m:
                frames.append(f"{os.path.basename(m.group(1).replace(chr(92), '/'))}:{m.group(2)}")
        exc_type = _exception_line(tb).split(":", 1)[0].strip()
        key = f"{kind}|{exc_type}|" + "|".join(frames[-3:])
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


# ── 报告 ─────────────────────────────────────────────────────────────────────

def _clean_ctx(block):
    out = {}
    if not isinstance(block, dict):
        return out
    for k in _CTX_KEYS:
        if k in block:
            try:
                out[k] = json.loads(json.dumps(block[k], ensure_ascii=False))
            except (TypeError, ValueError):
                continue
    return json.loads(redact(json.dumps(out, ensure_ascii=False))) if out else out


def _clip(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def build_report(kind, *, tb="", log_lines=(), ctx=None, exit_code=None, aliases=()):
    """组一份上报载荷(脱敏 + 限长)。对垃圾入参不抛。"""
    aliases = list(aliases or ())
    tb_text = redact(tb if isinstance(tb, str) else "", aliases)
    if len(tb_text) > MAX_TB:
        tb_text = tb_text[-MAX_TB:]
    if kind == "worker_exit":
        msg = f"worker 异常退出, 退出码 {exit_code}"
    else:
        msg = _exception_line(tb_text)          # tb_text 已脱敏
    lines = [str(x).rstrip("\r\n") for x in (log_lines if isinstance(log_lines, (list, tuple, collections.deque))
                                             else ())][-TAIL_LINES:]
    log = "\n".join(_clip(redact(x, aliases), MAX_LINE) for x in lines)
    if len(log) > MAX_LOG:
        log = log[-MAX_LOG:]
    report = {
        "v": 1, "type": "bug", "id": telemetry.install_id(), "ver": version.__version__,
        "os": f"{platform.system()} {platform.release()}"[:64], "py": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)), "kind": kind,
        "sig": signature(kind, tb_text, exit_code), "msg": _clip(msg, MAX_MSG),
        "tb": tb_text, "log": log, "ctx": _clean_ctx(ctx),
    }
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        report["exit"] = exit_code
    return report


# ── 节流 ─────────────────────────────────────────────────────────────────────

class Throttle:
    """同一签名冷却 + 每日总数上限(都是「弹窗问用户」的次数), 状态存在用户目录里(重启不清)。
    读写失败当没有 / 照常往下走, 进程内的计数仍然生效 —— 状态文件坏了也不会变成「一直弹」。"""

    def __init__(self, path, clock=time.time):
        self._path, self._clock = path, clock
        self._lock = threading.Lock()
        self._sigs = {}                  # sig -> 解禁时刻
        self._day, self._n = None, 0
        self._load()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        sigs = raw.get("sigs")
        if isinstance(sigs, dict):
            self._sigs = {k: float(v) for k, v in sigs.items()
                          if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)}
        if isinstance(raw.get("day"), int) and not isinstance(raw.get("day"), bool):
            self._day = raw["day"]
        n = raw.get("n")
        self._n = n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0

    def _roll(self, now):
        day = int(now // 86400)
        if day != self._day:
            self._day, self._n = day, 0

    def allow(self, sig):
        with self._lock:
            now = self._clock()
            self._roll(now)
            return self._n < MAX_PER_DAY and self._sigs.get(sig, 0) <= now

    def record(self, sig):
        with self._lock:
            now = self._clock()
            self._roll(now)
            self._n += 1
            self._sigs[sig] = now + SAME_SIG_COOLDOWN_S
            self._sigs = {k: v for k, v in self._sigs.items() if v > now}
            try:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump({"sigs": self._sigs, "day": self._day, "n": self._n}, f)
            except (OSError, ValueError):
                pass


def _default_throttle():
    return Throttle(os.path.join(telemetry.user_data_dir(), "bug_report_state.json"))


# ── 本地副本 ─────────────────────────────────────────────────────────────────

def reports_dir(root):
    return os.path.join(root, "logs", "bug-reports")


def save_local(root, payload, now=datetime.now):
    """把要上传的载荷原样存一份(用户想核对「到底发了什么」)。失败返回 None。"""
    try:
        d = reports_dir(root)
        os.makedirs(d, exist_ok=True)
        names = sorted(n for n in os.listdir(d) if n.startswith("bug-") and n.endswith(".json"))
        for n in names[:max(0, len(names) - (KEEP_LOCAL - 1))]:
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
        path = os.path.join(d, f"bug-{now().strftime('%Y%m%d-%H%M%S-%f')}-{payload.get('sig', 'x')}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except (OSError, ValueError, TypeError):
        return None


# ── 发送 ─────────────────────────────────────────────────────────────────────

def available():
    """能不能弹「要不要上报」: FLORR_TELEMETRY=0(跟匿名使用统计共用)就一个都不弹。"""
    return telemetry.enabled()


def prepare(kind, *, tb="", log_lines=(), ctx=None, exit_code=None, aliases=(), throttle=None):
    """检测到一个问题 -> 要不要弹窗问用户? 返回脱敏好的载荷(弹窗展示、用户同意后上传), 或 None:
    关着 / kind 不认识 / 同一个问题刚问过 / 今天问够了。返回载荷的同时记一次「问过了」(冷却 + 每日计数),
    所以不管用户选「上报」还是「不上报」, 同一个问题 6 小时内都不会再来烦。只算不发, 不碰网络、不存盘(状态文件除外)。"""
    if kind not in AUTO_KINDS or not available():
        return None
    try:
        payload = build_report(kind, tb=tb, log_lines=log_lines, ctx=ctx, exit_code=exit_code,
                               aliases=aliases)
        th = throttle or _default_throttle()
        if not th.allow(payload["sig"]):
            return None
        th.record(payload["sig"])
        return payload
    except Exception:
        return None


def prepare_manual(*, log_lines=(), ctx=None, aliases=()):
    """用户主动点了「反馈问题」: 组一份载荷给弹窗展示(环境信息 + 最近的输出; 说明由用户在弹窗里写,
    发送时才并进去, 见 with_note)。不受冷却 / 每日上限限制, 不碰网络、不存盘、不写节流状态。
    FLORR_TELEMETRY=0 或出了意外 -> None。"""
    if not available():
        return None
    try:
        return build_report(MANUAL_KIND, log_lines=log_lines, ctx=ctx, aliases=aliases)
    except Exception:
        return None


def _title(text):
    """说明的第一行非空内容(标题), 限长。"""
    for ln in text.splitlines():
        if ln.strip():
            return _clip(ln.strip(), MAX_MSG)
    return ""


def _manual_sig(text):
    """主动反馈没有堆栈可归并: 签名取自说明本身(空白折叠), 所以同一句话归一组、不同的话各自一行。"""
    key = f"{MANUAL_KIND}|" + " ".join(text.split())
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def with_note(payload, note, aliases=()):
    """给载荷加上用户写的补充说明(同样脱敏、限长; 空的不加)。返回新 dict, 不改入参。
    主动反馈(user_report)的说明就是正文: 标题和签名也由它定。"""
    out = dict(payload)
    text = redact(note if isinstance(note, str) else "", aliases).strip()
    if text:
        out["note"] = text[:MAX_NOTE]
        if out.get("kind") == MANUAL_KIND:
            out["msg"] = _title(out["note"])
            out["sig"] = _manual_sig(out["note"])
    return out


_PREVIEW_LABELS = {
    "kind": "问题类型", "msg": "错误", "tb": "错误堆栈", "log": "最近的输出", "ctx": "当时的时块设置",
    "ver": "程序版本", "os": "系统", "py": "Python", "frozen": "打包版", "id": "安装 ID",
    "sig": "问题签名", "exit": "退出码", "note": "你的补充说明", "v": "格式版本", "type": "类型",
}
_PREVIEW_ORDER = ("kind", "msg", "tb", "log", "ctx", "ver", "os", "py", "frozen", "id", "sig", "exit",
                  "note", "v", "type")


def _mb(n):
    return f"{n / 1048576:.1f} MB"


def describe_attachment(att):
    """附件(blackbox.pack 的结果)给人看的几行: 时间范围 / 帧数 / 截图张数 / 日志 / 大小。不含完整路径。"""
    mins = max(1, int(att.get("window_s") or 300) // 60)
    lines = []
    if att.get("frames") or att.get("shots"):
        span = ""
        if att.get("from") is not None and att.get("to") is not None:
            span = (f": {datetime.fromtimestamp(att['from']).strftime('%m-%d %H:%M:%S')} – "
                    f"{datetime.fromtimestamp(att['to']).strftime('%H:%M:%S')}(本机时间)")
        lines.append(f"最近 {mins} 分钟{span}")
        lines.append(f"画面录像 {att.get('frames', 0)} 帧, 截图 {att.get('shots', 0)} 张"
                     "(截图里能看到你的账号名、别的玩家的名字和聊天)")
    else:
        lines.append("没有录到画面(黑匣子只在刷图程序运行时录)")
    if att.get("logs"):
        lines.append("日志: " + "、".join(att["logs"]) + "(本次运行的完整日志, 已按上面的规则脱敏)")
    if att.get("bytes") is not None:
        lines.append(f"大小: {_mb(att['bytes'])}")
    return "\n".join(lines)


def render_preview(payload, attachment=None, packing=False):
    """弹窗里给用户看的「将要发送的全部内容」: 载荷里每个字段都列出来(不在标签表里的也照列,
    不会有字段悄悄发出去而预览里没有); 多行的(堆栈 / 输出)缩进成块, 方便读。
    attachment: 一起发的附件(blackbox.pack 的结果); packing: 附件还在打包。"""
    keys = [k for k in _PREVIEW_ORDER if k in payload] + [k for k in payload if k not in _PREVIEW_ORDER]
    labels = dict(_PREVIEW_LABELS)
    if payload.get("kind") == MANUAL_KIND:
        labels["msg"] = "标题(取你说明的第一行)"        # 主动反馈没有「错误」, msg 是说明的标题
    parts = []
    for k in keys:
        v = payload[k]
        label = labels.get(k, k)
        if k == "ctx":
            v = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            v = "是" if v else "否"
        v = str(v)
        if "\n" in v:
            parts.append(f"【{label}】\n" + "\n".join("    " + ln for ln in v.splitlines()))
        else:
            parts.append(f"【{label}】 {v}")
    if packing:
        parts.append("【附件(会一起发送)】 打包中…(最近 5 分钟的画面录像、截图和本次运行的日志)")
    elif attachment:
        parts.append("【附件(会一起发送)】\n" + "\n".join("    " + ln
                                                          for ln in describe_attachment(attachment).splitlines()))
    return "\n".join(parts)


def _ticket(reply):
    """服务端回的 {"id": int, "token": str}(传附件用); 别的样子(老服务端回 204 空体)-> None。"""
    if not isinstance(reply, dict):
        return None
    bid, token = reply.get("id"), reply.get("token")
    if not isinstance(bid, int) or isinstance(bid, bool) or not isinstance(token, str) or not token:
        return None
    return {"id": bid, "token": token}


def _post(payload):
    """发文字报告。返回服务端给的附件令牌 {"id", "token"}, 老服务端(回 204)或回得不对 -> None。"""
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": f"florr-auto-farm/{version.__version__}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=telemetry._SSL_CONTEXT) as resp:
        body = resp.read()
        if resp.status != 200:
            return None
    try:
        return _ticket(json.loads(body.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return None


def _put_attachment(bug_id, token, path):
    with open(path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(
        f"{ENDPOINT}/{int(bug_id)}/attachment", data=data, method="PUT",
        headers={"Content-Type": "application/zip", "X-Upload-Token": token,
                 "User-Agent": f"florr-auto-farm/{version.__version__}"})
    with urllib.request.urlopen(req, timeout=ATTACH_TIMEOUT_S, context=telemetry._SSL_CONTEXT) as resp:
        resp.read()


def _send_attachment(ticket, path, put, retry_s):
    """传附件, 失败重试; 返回 None = 成功, 否则最后一次的异常。"""
    err = None
    for i in range(ATTACH_TRIES):
        if i:
            time.sleep(retry_s)
        try:
            put(ticket["id"], ticket["token"], path)
            return None
        except Exception as e:
            err = e
    return err


def submit(payload, *, note="", aliases=(), root=None, post=None, done=None, attachment=None, put=None,
           retry_s=ATTACH_RETRY_S):
    """用户点了「上报」: 后台线程里 加说明 -> 存本地副本 -> 上传 -> (有附件且服务端给了令牌)传附件 ->
    done(是否成功, 一句话)。返回已启动的线程。done 在后台线程里被调, 调用方自己转回界面线程。文字报告上传失败
    只通过 done 说一声(弹窗留着, 用户可以再点一次), 不抛、不自动重试; 文字到了但附件没传上去仍算成功
    (附件重试 ATTACH_TRIES 次), 话里说清附件留在哪。attachment: 附件 zip 的路径(blackbox.pack 打的)。"""
    post = post or _post
    put = put or _put_attachment

    def run():
        try:
            body = with_note(payload, note, aliases)
            if body.get("kind") == MANUAL_KIND and not body.get("note"):
                if done is not None:               # 主动反馈的正文就是说明, 没写就什么都不发、不存
                    done(False, "还没写说明 —— 先写几句遇到了什么问题, 再点发送")
                return
            path = save_local(root, body) if root else None
            where = f"; 本地副本: {path}" if path else ""
            try:
                ticket = _ticket(post(body))
            except Exception as e:
                msg = f"上传失败({type(e).__name__}), 可以稍后再点一次「上报」{where}"
                ok = False
            else:
                ok = True
                msg = f"已上报, 谢谢(问题签名 {body['sig']}){where}"
                if attachment and ticket is None:
                    msg += f"; 服务器这次没收附件, 附件留在 {attachment}"
                elif attachment:
                    err = _send_attachment(ticket, attachment, put, retry_s)
                    if err is None:
                        size = os.path.getsize(attachment) if os.path.exists(attachment) else 0
                        msg = f"已上报, 谢谢(问题签名 {body['sig']}), 附件 {_mb(size)} 也传上去了{where}"
                    else:
                        msg = (f"文字已送达(问题签名 {body['sig']}), 但附件没传上去({type(err).__name__}); "
                               f"附件在 {attachment}{where}")
            if done is not None:
                done(ok, msg)
        except Exception:
            pass

    t = threading.Thread(target=run, name="bug-report", daemon=True)
    t.start()
    return t


def is_crash_exit(code):
    """worker 的退出码算不算「崩了」。0 正常; 1 要么是 Python 没捕获的异常(traceback 那条路已经报了)、
    要么是有意的 sys.exit(1)(Chrome 没就绪)、要么是 Windows 上被 terminate; -15 是 POSIX 上被 SIGTERM。
    其余(访问违规 0xC0000005、段错误、被 SIGKILL、别的非零码)才算, 它们没有 traceback。"""
    return isinstance(code, int) and code not in (0, 1, -15)      # True / False 本来就等于 1 / 0


class StreamWatcher:
    """worker 输出泵线程里用: 滚动留最近 tail_keep 行, 并从流里认 traceback ——
    每认出一个就调 on_traceback(tb文本, 那一刻的尾巴); 回调抛什么都吞掉, 不能把泵停了
    (泵一停, 子进程写满管道就卡死)。"""

    def __init__(self, on_traceback, tail_keep=TAIL_KEEP):
        self._scanner = TracebackScanner()
        self._tail = collections.deque(maxlen=tail_keep)
        self._cb = on_traceback
        self.fired = 0                    # 交出去过几个 traceback(GUI 据此不再重复报同一个 worker 的退出码)

    def tail(self):
        return list(self._tail)

    def _fire(self, tb):
        if not tb:
            return
        self.fired += 1
        try:
            self._cb(tb, list(self._tail))
        except Exception:
            pass

    def feed(self, line):
        self._tail.append(str(line).rstrip("\r\n"))
        self._fire(self._scanner.feed(line))

    def flush(self):
        self._fire(self._scanner.flush())
