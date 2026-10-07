"""黑匣子(blackbox.py): 控制面板里滚动录最近几分钟的画面, 反馈时打包。录制部分用假的 CDP / 假时钟驱动。"""
import base64
import gzip
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

import blackbox

ROOT = Path(__file__).resolve().parent
JPG = b"\xff\xd8fake-jpeg"
T_REC = 1_790_000_000.0


class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSession:
    def __init__(self, fail_after=None):
        self.closed = 0
        self.peeks = 0
        self.fail_after = fail_after          # 第几次偷看开始出错(None = 不出错)

    def eval_value(self, expr, timeout=None):
        if "__canvasLog" not in expr:          # 新开一段时问页面大小
            return [1920, 1080, 1]
        self.peeks += 1
        if self.fail_after is not None and self.peeks > self.fail_after:
            raise RuntimeError("标签页没了")
        return {"hook": True, "r": [{"frame": self.peeks, "op": "arc", "x": 1, "y": 2}], "fr": self.peeks,
                "f": True, "w": 1920, "h": 1080, "c": None, "inp": None}

    def close(self):
        self.closed += 1


def make_box(tmp_path, clock, *, session=None, reachable=True, **kw):
    sessions = []

    def factory():
        s = session or FakeSession()
        sessions.append(s)
        return s

    shots = []

    def cdp(method, params=None, timeout=5):
        shots.append((method, params))
        return {"result": {"data": base64.b64encode(JPG).decode()}}

    box = blackbox.BlackBox(str(tmp_path), clock=clock, reachable=lambda: reachable, session_factory=factory,
                            cdp_command=cdp, os_mouse=None, **kw)
    return box, sessions, shots


def run(box, clock, seconds, step=0.5):
    for _ in range(int(seconds / step)):
        box._step()
        clock.t += step


def segs(tmp_path):
    d = Path(blackbox.blackbox_dir(str(tmp_path)))
    return sorted(p for p in d.iterdir() if p.is_dir()) if d.exists() else []


def read_frames(seg):
    with gzip.open(seg / "frames.jsonl.gz", "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


# ── 录制 ─────────────────────────────────────────────────────────────────────

def test_it_records_one_minute_segments_and_keeps_only_the_last_six(tmp_path):
    clock = Clock()
    box, sessions, _ = make_box(tmp_path, clock)
    run(box, clock, 8 * 60)
    box.stop()
    s = segs(tmp_path)
    assert len(s) == 6 and len(sessions) == 1
    assert s[-1].name.startswith("seg-") and s[0].name < s[-1].name
    lines = read_frames(s[-2])
    meta = lines[0]["_meta"]
    assert meta["source"] == "blackbox" and meta["page"] == [1920, 1080, 1] and meta["hz"] == 2.0
    ticks = lines[1:]
    assert 115 <= len(ticks) <= 121 and all("t" in t and t["r"] for t in ticks)
    assert ticks[-1]["t"] - ticks[0]["t"] < 60
    shots = sorted((s[-2] / "shots").iterdir())
    assert 11 <= len(shots) <= 13 and shots[0].read_bytes() == JPG


def test_screenshots_are_small_jpegs_of_the_whole_page(tmp_path):
    clock = Clock()
    box, _, shots = make_box(tmp_path, clock)
    run(box, clock, 1)
    method, params = shots[0]
    assert method == "Page.captureScreenshot" and params["format"] == "jpeg"
    assert params["clip"]["width"] == 1920 and params["clip"]["height"] == 1080 and params["clip"]["scale"] == 0.5


def test_what_it_records_packs_and_reads_back(tmp_path):
    # 录制器写出来的(不是测试手工造的)经 pack 打包后, 真的分析工具读得出: 帧 / 时间 / 截图 / 页面大小
    clock = Clock(T_REC)
    box, _, _ = make_box(tmp_path, clock)
    run(box, clock, 130)
    box.flush()
    att = blackbox.pack(str(tmp_path), now=lambda: clock.t)
    rec = _load_recording(att["path"])
    assert rec.meta["source"] == "blackbox" and rec.meta["page"] == [1920, 1080, 1]
    assert len(rec.ticks) == att["frames"] >= 255 and all(t["r"] and t["f"] == 1 for t in rec.ticks)
    assert rec.ticks[0]["t"] >= T_REC and att["shots"] >= 25 and not any(t.get("err") for t in rec.ticks)
    ts = [t["t"] for t in rec.ticks]
    assert len(set(ts)) == len(ts) and 0.4 < (ts[-1] - ts[0]) / (len(ts) - 1) < 0.6     # 毫秒级墙钟, 2 拍/秒


def test_it_only_ever_reads_the_page(tmp_path):
    # 对刷图程序只读: 不 drain 帧、不改页面、不重载; 页面上没有 canvas hook 也不注(注入 / 重载归刷图程序管)
    clock = Clock()
    evaled = []

    class Spy(FakeSession):
        def eval_value(self, expr, timeout=None):
            evaled.append(expr)
            return super().eval_value(expr, timeout)

    box, _, cdp_calls = make_box(tmp_path, clock, session=Spy())
    run(box, clock, 3)
    assert evaled
    for expr in evaled:
        for bad in ("drain", ".splice(", ".length = 0", "__canvasLog =", "location.reload", "innerHTML", "eval(",
                    "<script", ".src ="):
            assert bad not in expr, bad
    assert {m for m, _ in cdp_calls} == {"Page.captureScreenshot"}


def test_flush_finishes_the_current_segment_and_the_next_tick_starts_a_new_one(tmp_path):
    clock = Clock()
    box, _, _ = make_box(tmp_path, clock)
    run(box, clock, 10)
    first = segs(tmp_path)[-1]
    box.flush()
    assert len(read_frames(first)) > 10                       # gzip 收尾了, 整个读得出来
    run(box, clock, 1)
    assert len(segs(tmp_path)) == 2


def test_nothing_happens_while_chrome_is_not_there(tmp_path):
    clock = Clock()
    box, sessions, _ = make_box(tmp_path, clock, reachable=False, retry_s=5.0)
    assert box._step() == 5.0
    assert sessions == [] and segs(tmp_path) == []


def test_a_page_that_keeps_failing_drops_the_connection_and_backs_off(tmp_path):
    clock = Clock()
    s = FakeSession(fail_after=3)
    box, sessions, _ = make_box(tmp_path, clock, session=s, max_errors=4, retry_s=5.0)
    delays = []
    for _ in range(12):
        delays.append(box._step())
        clock.t += 0.5
    assert s.closed >= 1 and 5.0 in delays                    # 连错 4 拍: 断开、歇 5 秒再连
    assert delays[:3] == [0.5, 0.5, 0.5]                       # 好好的时候按 2 拍/秒走


def test_an_exception_while_opening_a_segment_is_swallowed(tmp_path):
    class Broken(FakeSession):
        def eval_value(self, expr, timeout=None):
            raise RuntimeError("连不上")

    clock = Clock()
    s = Broken()
    box, _, _ = make_box(tmp_path, clock, session=s, retry_s=5.0)
    assert box._step() == 5.0 and s.closed == 1


def test_start_and_stop_a_real_thread(tmp_path):
    box = blackbox.BlackBox(str(tmp_path), reachable=lambda: False, retry_s=0.05,
                            session_factory=FakeSession, cdp_command=lambda *a, **k: None, os_mouse=None)
    box.start()
    box.start()                                               # 开着再开: 不起第二个线程
    assert box.running
    t0 = time.time()
    box.stop()
    box.stop()
    assert not box.running and time.time() - t0 < 3


def test_a_crash_inside_the_thread_does_not_escape(tmp_path):
    def boom():
        raise RuntimeError("意外")

    box = blackbox.BlackBox(str(tmp_path), reachable=boom, retry_s=0.05, session_factory=FakeSession,
                            cdp_command=lambda *a, **k: None, os_mouse=None)
    box.start()
    time.sleep(0.2)
    assert box.running                                        # 还活着, 照样在重试
    box.stop()


def test_importing_it_does_not_touch_the_dpi_setting_of_the_control_panel():
    # pyautogui / utils 在 Windows 上 import 时会改进程的 DPI 设置, 控制面板(CustomTkinter 自己管 DPI)不能被它们动到
    out = subprocess.run([sys.executable, "-c",
                          "import blackbox, sys; print('pyautogui' in sys.modules, 'utils' in sys.modules)"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.stdout.split() == ["False", "False"], out.stderr


def test_it_does_not_need_any_module_the_public_release_leaves_out():
    # 公开版不带离线录像 / 分析工具和计数探针(依赖未授权移植的脚本); blackbox 是控制面板启动时就 import 的,
    # 一旦依赖它们, 公开版一启动就崩。2026-10-07 发版前抓到过 import record_session 这一条。
    out = subprocess.run([sys.executable, "-c",
                          "import blackbox, sys; left_out = ('record_session', 'analyze_recording', 'live_monitor', "
                          "'wasm_table_solver'); print([m for m in left_out if m in sys.modules])"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr
    src = (ROOT / "blackbox.py").read_text(encoding="utf-8")
    for name in ("record_session", "analyze_recording"):
        assert f"import {name}" not in src and f"from {name}" not in src, name


# ── 打包 ─────────────────────────────────────────────────────────────────────

import random
import threading
import zipfile
from datetime import datetime

def _load_recording(path):
    """私有仓库用真的分析工具读一遍(格式兼容才算数); 公开版没有 analyze_recording, 那几条测试跳过。"""
    analyze_recording = pytest.importorskip("analyze_recording")
    return analyze_recording.load(path)

T0 = 1_790_000_000.0


def make_seg(root, start, ticks, shots, *, big=0, cut_tail=0, half_line=""):
    """手工造一段黑匣子录像。ticks / shots 是时间戳; big: 每帧塞这么多字节的随机字符(撑体积);
    cut_tail: 把 gzip 文件末尾截掉这么多字节(模拟没收尾的半截文件); half_line: 最后再接一行没写完的(没换行)。"""
    d = Path(blackbox.blackbox_dir(str(root))) / datetime.fromtimestamp(start).strftime("seg-%Y%m%d-%H%M%S-%f")
    (d / "shots").mkdir(parents=True)
    rnd = random.Random(int(start))
    lines = [json.dumps({"_meta": {"version": 1, "started": start, "source": "blackbox",
                                   "utc_offset_s": 28800, "page": [1920, 1080, 1]}}) + "\n"]
    for t in ticks:
        rec = {"t": round(t, 3), "f": 1, "fr": 1, "r": [{"op": "arc", "x": 1}]}
        if big:
            rec["pad"] = "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(big))
        lines.append(json.dumps(rec, separators=(",", ":")) + "\n")
    data = gzip.compress(("".join(lines) + half_line).encode("utf-8"))
    (d / "frames.jsonl.gz").write_bytes(data[:len(data) - cut_tail] if cut_tail else data)
    for t in shots:
        (d / "shots" / f"{int(t * 1000)}.jpg").write_bytes(JPG)
    return d


def make_log(root, name, text, mtime):
    d = Path(root) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def seven_minutes(root):
    for i in range(3):                                         # 0~180 / 180~360 / 360~420 秒
        start = T0 + 180 * i
        end = min(start + 180, T0 + 420)
        ticks = [start + k for k in range(int(end - start))]
        make_seg(root, start, ticks, [t for t in ticks if int(t - T0) % 5 == 0])


def test_it_packs_the_last_five_minutes_and_analyze_recording_can_read_it(tmp_path):
    seven_minutes(tmp_path)
    make_log(tmp_path, "worker-20260929-020000.log", "02:00:00.000 开始刷\n", T0 + 420)
    att = blackbox.pack(str(tmp_path), now=lambda: T0 + 425)
    newest = T0 + 419
    assert att["to"] == newest and newest - 301 <= att["from"] <= newest - 295
    assert 295 <= att["frames"] <= 301 and 59 <= att["shots"] <= 61
    assert att["logs"] == ["worker-20260929-020000.log"] and att["window_s"] == 300
    assert os.path.getsize(att["path"]) == att["bytes"]
    assert os.path.dirname(att["path"]) == os.path.join(str(tmp_path), "logs", "bug-reports")
    rec = _load_recording(att["path"])
    assert rec.meta["source"] == "blackbox" and rec.meta["utc_offset_s"] == 28800
    assert len(rec.ticks) == att["frames"] and rec.ticks[-1]["t"] == newest
    assert "开始刷" in rec.logs["worker-20260929-020000.log"]
    with zipfile.ZipFile(att["path"]) as z:
        names = z.namelist()
        manifest = json.loads(z.read("manifest.json"))
    assert len([n for n in names if n.startswith("shots/")]) == att["shots"]
    assert manifest == {k: v for k, v in att.items() if k not in ("path", "bytes")}


def test_the_window_follows_the_newest_recording_not_the_clock(tmp_path):
    # 黑匣子停了一小时才点反馈: 带的是停之前的最后 5 分钟(预览里会写时间范围)
    seven_minutes(tmp_path)
    att = blackbox.pack(str(tmp_path), now=lambda: T0 + 3600)
    assert att["to"] == T0 + 419 and 295 <= att["frames"] <= 301


def test_a_half_written_segment_is_read_as_far_as_it_goes(tmp_path):
    ticks = [T0 + k for k in range(200)]
    make_seg(tmp_path, T0, ticks, [], cut_tail=40)             # 崩溃留下的: 结尾不完整
    att = blackbox.pack(str(tmp_path), now=lambda: T0 + 300)
    rec = _load_recording(att["path"])
    assert 0 < att["frames"] < 200 and len(rec.ticks) == att["frames"]


def test_a_line_that_was_only_half_written_is_left_out(tmp_path):
    make_seg(tmp_path, T0, [T0 + k for k in range(10)], [], half_line='{"t":%r,"r":[{"op":"ar' % (T0 + 10))
    att = blackbox.pack(str(tmp_path), now=lambda: T0 + 20)
    rec = _load_recording(att["path"])
    assert att["frames"] == 10 and len(rec.ticks) == 10 and att["to"] == T0 + 9


def test_the_newest_log_and_recently_touched_ones_are_taken_redacted(tmp_path):
    make_seg(tmp_path, T0, [T0 + 1], [])
    make_log(tmp_path, "worker-20260929-000000.log", "很久以前\n", T0 - 3600)
    make_log(tmp_path, "worker-20260929-010000.log", "崩之前 C:\\Users\\Alice\\florr 小号A\n", T0 - 300)
    make_log(tmp_path, "worker-20260929-020000.log", "重启之后 bob@example.com\n", T0 + 10)
    att = blackbox.pack(str(tmp_path), aliases=["小号A"], now=lambda: T0 + 20)
    assert att["logs"] == ["worker-20260929-010000.log", "worker-20260929-020000.log"]
    with zipfile.ZipFile(att["path"]) as z:
        a = z.read("worker-20260929-010000.log").decode()
        b = z.read("worker-20260929-020000.log").decode()
    assert "Alice" not in a and "<user>" in a and "<profile>" in a and "小号A" not in a
    assert "<email>" in b and "bob@" not in b


def test_the_newest_log_is_taken_even_if_it_is_old(tmp_path):
    make_log(tmp_path, "worker-20260929-000000.log", "上一次跑\n", T0 - 86400)
    att = blackbox.pack(str(tmp_path), now=lambda: T0)
    assert att["logs"] == ["worker-20260929-000000.log"] and att["frames"] == 0 and att["from"] is None


def test_logs_over_the_budget_keep_their_last_whole_lines(tmp_path):
    lines = "".join(f"{i:06d} 一行日志\n" for i in range(5000))
    make_log(tmp_path, "worker-20260929-010000.log", "旧的一份\n" * 100, T0 - 60)
    make_log(tmp_path, "worker-20260929-020000.log", lines, T0)
    att = blackbox.pack(str(tmp_path), now=lambda: T0, log_max=2000)
    with zipfile.ZipFile(att["path"]) as z:
        got = z.read("worker-20260929-020000.log").decode()
    assert len(got.encode()) <= 2000 and got.endswith("004999 一行日志\n")
    assert re.fullmatch(r"\d{6} 一行日志", got.splitlines()[0])  # 从整行切开
    assert att["logs"] == ["worker-20260929-020000.log"]       # 预算用完, 更旧的那份不带


def test_too_big_drops_old_frames_first_but_keeps_screenshots_and_logs(tmp_path):
    ticks = [T0 + k for k in range(300)]
    make_seg(tmp_path, T0, ticks, ticks[::5], big=2000)
    make_log(tmp_path, "worker-20260929-020000.log", "日志\n", T0)
    full = blackbox.pack(str(tmp_path), now=lambda: T0 + 300)
    cap = full["bytes"] // 3
    att = blackbox.pack(str(tmp_path), now=lambda: T0 + 300, max_bytes=cap)
    assert att["bytes"] <= cap and 0 < att["frames"] < full["frames"]
    assert att["shots"] == full["shots"] and att["logs"] == full["logs"]
    assert att["to"] == full["to"]                              # 砍的是旧的那头
    tiny = blackbox.pack(str(tmp_path), now=lambda: T0 + 300, max_bytes=1)
    assert tiny["frames"] == 0 and tiny["shots"] == full["shots"]


def test_nothing_to_attach_means_no_attachment(tmp_path):
    assert blackbox.pack(str(tmp_path)) is None


def test_only_the_last_few_attachment_zips_are_kept(tmp_path):
    make_seg(tmp_path, T0, [T0 + 1], [])
    paths = [blackbox.pack(str(tmp_path), now=lambda: T0 + 10, keep=5)["path"] for _ in range(7)]
    left = sorted(os.listdir(os.path.join(str(tmp_path), "logs", "bug-reports")))
    assert len(left) == 5 and all(n.startswith("attach-") and n.endswith(".zip") for n in left)
    assert os.path.basename(paths[-1]) in left


class FakeBox:
    def __init__(self):
        self.flushed = 0

    def flush(self):
        self.flushed += 1


def test_pack_job_flushes_the_recorder_first_then_packs_in_the_background(tmp_path):
    box = FakeBox()
    seen = []

    def fake_pack(root, aliases=()):
        seen.append((root, list(aliases), box.flushed))
        return {"path": "x.zip"}

    job = blackbox.PackJob(str(tmp_path), aliases=["a"], box=box, pack_fn=fake_pack)
    assert job.wait(5) and job.done() and job.result() == {"path": "x.zip"}
    assert seen == [(str(tmp_path), ["a"], 1)]


def test_a_pack_job_that_fails_just_has_no_result(tmp_path):
    def boom(root, aliases=()):
        raise OSError("盘满了")

    job = blackbox.PackJob(str(tmp_path), pack_fn=boom)
    assert job.wait(5) and job.done() and job.result() is None


def test_a_pack_job_has_no_result_until_it_is_done(tmp_path):
    gate = threading.Event()

    def slow(root, aliases=()):
        gate.wait(5)
        return {"path": "y.zip"}

    job = blackbox.PackJob(str(tmp_path), pack_fn=slow)
    assert not job.done() and job.result() is None
    gate.set()
    assert job.wait(5) and job.result() == {"path": "y.zip"}
