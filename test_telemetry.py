import json
import sys
import threading
from pathlib import Path

import pytest

import gui_app
import gui_schedule
import telemetry

ROOT = Path(__file__).resolve().parent


USER_ID = "user-data/florr-auto-farm/install_id"      # 这台电脑这个 Windows 用户共用的
FOLDER_ID = "program-a/install_id"                     # 老版本存在程序文件夹里的


@pytest.fixture(autouse=True)
def _id_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "ID_PATH", str(tmp_path / USER_ID))
    monkeypatch.setattr(telemetry, "LEGACY_ID_PATH", str(tmp_path / FOLDER_ID))
    (tmp_path / "program-a").mkdir()
    monkeypatch.delenv("FLORR_TELEMETRY", raising=False)


def test_install_id_is_created_once_and_reused(tmp_path):
    a = telemetry.install_id()
    assert len(a) == 32 and int(a, 16) >= 0
    assert (tmp_path / USER_ID).read_text(encoding="utf-8").strip() == a
    assert telemetry.install_id() == a


def test_two_program_folders_on_one_computer_share_the_id(tmp_path, monkeypatch):
    # 实机 2026-09-28: 打包版文件夹和源码文件夹各生成一个编号, 同一台电脑被算成两台
    a = telemetry.install_id()
    (tmp_path / "program-b").mkdir()
    monkeypatch.setattr(telemetry, "LEGACY_ID_PATH", str(tmp_path / "program-b" / "install_id"))
    assert telemetry.install_id() == a


def test_id_from_an_older_version_folder_is_kept(tmp_path):
    old = "0123456789abcdef0123456789abcdef"
    (tmp_path / FOLDER_ID).write_text(old + "\n", encoding="utf-8")
    assert telemetry.install_id() == old
    assert (tmp_path / USER_ID).read_text(encoding="utf-8").strip() == old


def test_shared_id_wins_over_a_folder_id(tmp_path):
    shared = "fedcba9876543210fedcba9876543210"
    (tmp_path / USER_ID).parent.mkdir(parents=True)
    (tmp_path / USER_ID).write_text(shared, encoding="utf-8")
    (tmp_path / FOLDER_ID).write_text("0123456789abcdef0123456789abcdef", encoding="utf-8")
    assert telemetry.install_id() == shared


def test_corrupt_install_id_file_is_replaced(tmp_path):
    (tmp_path / USER_ID).parent.mkdir(parents=True)
    (tmp_path / USER_ID).write_text("not-an-id\n", encoding="utf-8")
    a = telemetry.install_id()
    assert a != "not-an-id" and len(a) == 32
    assert telemetry.install_id() == a


def test_unwritable_user_dir_falls_back_to_the_program_folder(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(telemetry, "ID_PATH", str(blocker / "install_id"))
    a = telemetry.install_id()
    assert (tmp_path / FOLDER_ID).read_text(encoding="utf-8").strip() == a
    assert telemetry.install_id() == a


def test_nowhere_writable_still_yields_an_id(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "ID_PATH", str(tmp_path / "x" / "\0bad"))
    monkeypatch.setattr(telemetry, "LEGACY_ID_PATH", str(tmp_path / "missing" / "install_id"))
    assert len(telemetry.install_id()) == 32


@pytest.mark.parametrize("plat,env,want", [
    ("win32", {"LOCALAPPDATA": "D:/LocalAppData"}, "D:/LocalAppData/florr-auto-farm"),
    ("darwin", {}, "~/Library/Application Support/florr-auto-farm"),
    ("linux", {"XDG_DATA_HOME": "/xdg"}, "/xdg/florr-auto-farm"),
    ("linux", {}, "~/.local/share/florr-auto-farm"),
])
def test_user_data_dir_per_platform(plat, env, want, monkeypatch):
    for k in ("LOCALAPPDATA", "XDG_DATA_HOME"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    got = telemetry.user_data_dir(plat)
    assert Path(got) == Path(want.replace("~", str(Path.home())))


def test_heartbeat_carries_map_and_area_but_no_account():
    blk = {**gui_schedule.new_block_template({"schedule": [], "profiles": [{"alias": "luka"}]}),
           "map": "anthell", "farming_area": [(3, 4), (50, 60)]}
    e = telemetry.heartbeat_event(blk)
    assert e["type"] == "hb" and e["map"] == "anthell" and e["area"] == [[3, 4], [50, 60]]
    assert "luka" not in json.dumps(e, ensure_ascii=False)
    assert telemetry.heartbeat_event({**blk, "farming_area": None})["area"] is None


def test_send_posts_in_a_background_thread_and_swallows_errors():
    got, main_thread = [], threading.current_thread()

    def post(payload):
        got.append((payload, threading.current_thread() is main_thread))
        raise OSError("offline")

    t = telemetry.send(lambda: {"type": "x"}, post=post)
    t.join(2)
    assert got == [({"type": "x"}, False)]


def test_send_survives_a_failing_payload_builder():
    def boom():
        raise RuntimeError("disk")
    posted = []
    telemetry.send(boom, post=posted.append).join(2)
    assert posted == []


def test_send_is_off_when_disabled(monkeypatch):
    monkeypatch.setenv("FLORR_TELEMETRY", "0")
    posted = []
    assert telemetry.send(telemetry.start_event, post=posted.append) is None
    assert posted == []


def test_post_goes_to_the_endpoint_as_json_with_a_timeout(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def urlopen(req, timeout, context):
        seen.update(url=req.full_url, body=json.loads(req.data), timeout=timeout,
                    ctype=req.get_header("Content-type"), ctx=context)
        return Resp()

    monkeypatch.setattr(telemetry.urllib.request, "urlopen", urlopen)
    telemetry._post({"type": "start"})
    assert seen["url"] == telemetry.ENDPOINT and seen["body"] == {"type": "start"}
    assert seen["timeout"] == telemetry.TIMEOUT_S and seen["ctype"] == "application/json"
    assert seen["ctx"] is telemetry._SSL_CONTEXT


# ---- 和服务器(deploy/stats, 只在私有仓库)对得上 ----

def _stats_store():
    here = ROOT / "deploy" / "stats"
    if not here.is_dir():
        pytest.skip("公开仓库里没有 deploy/stats")
    sys.path.insert(0, str(here))
    import stats_store
    return stats_store


def test_events_pass_the_servers_validation(tmp_path):
    stats_store = _stats_store()
    store = stats_store.Store(str(tmp_path / "s.sqlite3"))
    region = {"country": "中国", "province": "广东省", "city": "广州市"}
    blk = {**gui_schedule.new_block_template({"schedule": [], "profiles": []}),
           "farming_area": [[9, 8], [51, 56]]}
    assert store.record_event(telemetry.start_event(), region) is True
    assert store.record_event(telemetry.heartbeat_event(blk), region) is True
    assert store.record_event(telemetry.heartbeat_event({**blk, "id": "b2", "farming_area": None}),
                              region) is False       # 同一台电脑 4 分钟内第二次: 收下但不计时
    store.close()


def test_heartbeat_event_without_a_window_is_still_the_v1_payload():
    blk = {"map": "desert", "farming_area": [[9, 8], [51, 56]]}
    e = telemetry.heartbeat_event(blk)
    assert e["v"] == 1 and "dur" not in e and "ph" not in e


def test_heartbeat_event_with_a_window_is_v2():
    blk = {"map": "desert", "farming_area": [[9, 8], [51, 56]]}
    window = {"dur": 300, "ph": {"farm": 200, "travel": 60, "other": 40}}
    e = telemetry.heartbeat_event(blk, window)
    assert e["v"] == 2 and e["type"] == "hb" and e["map"] == "desert" and e["area"] == [[9, 8], [51, 56]]
    assert e["dur"] == 300 and e["ph"] == {"farm": 200, "travel": 60, "other": 40}
    assert set(e) == {"v", "type", "id", "ver", "map", "area", "dur", "ph"}


def test_v2_events_pass_the_servers_validation_and_credit_real_time(tmp_path):
    stats_store = _stats_store()
    from datetime import datetime, timedelta, timezone
    store = stats_store.Store(str(tmp_path / "s.sqlite3"))
    region = {"country": "中国", "province": "广东省", "city": "广州市"}
    blk = {**gui_schedule.new_block_template({"schedule": [], "profiles": []}),
           "farming_area": [[9, 8], [51, 56]]}
    t0 = datetime(2026, 9, 30, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    zero = {"dur": 0, "ph": {"farm": 0, "travel": 0, "other": 0}}
    window = {"dur": 300, "ph": {"farm": 200, "travel": 60, "other": 40}}
    assert store.record_event(telemetry.heartbeat_event(blk, zero), region, t0) is False   # 启动那一条
    assert store.record_event(telemetry.heartbeat_event(blk, window), region,
                              t0 + timedelta(seconds=300)) is True
    ph = store.summary(30, t0 + timedelta(minutes=10))["phase"]
    assert (ph["farm"], ph["travel"], ph["other"]) == (3.3, 1, 0.7)    # 200 / 60 / 40 秒
    store.close()


def test_heartbeat_interval_and_endpoint_match_the_server():
    stats_store = _stats_store()
    assert telemetry.HEARTBEAT_S == stats_store.HEARTBEAT_MINUTES * 60
    assert telemetry.HEARTBEAT_S > stats_store.MIN_HEARTBEAT_GAP_S
    caddy = (ROOT / "deploy" / "mirror" / "florrfarm.caddy").read_text(encoding="utf-8")
    path = telemetry.ENDPOINT.split("florrfarm.cc.cd", 1)[1]
    assert f"handle {path} {{" in caddy


# ---- 控制面板什么时候报心跳 ----

class _Proc:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


SCHED = [{"id": "blk-1", "map": "desert"}, {"id": "blk-2", "map": "anthell"}]


@pytest.mark.parametrize("running,proc,want", [
    ("blk-2", _Proc(), "blk-2"),
    ("blk-2", _Proc(code=1), None),      # worker 已经退了
    ("blk-2", None, None),
    (None, _Proc(), None),               # 空档
    ("gone", _Proc(), None),             # 时块被删了
])
def test_heartbeat_block(running, proc, want):
    blk = gui_app.heartbeat_block(SCHED, running, proc)
    assert (blk["id"] if blk else None) == want


# ---- 心跳 v2: 什么时候报、报什么 ----

import types  # noqa: E402

import telemetry_clock  # noqa: E402

BLOCK = {"id": "blk-1", "map": "desert", "farming_area": [[9, 8], [51, 56]]}


class _Clk:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _FakeThread:
    def __init__(self):
        self.joined = []

    def join(self, timeout=None):
        self.joined.append(timeout)


def _capture_send(monkeypatch):
    sent = []

    def send(make_payload):
        sent.append(make_payload())
        return _FakeThread()

    monkeypatch.setattr(gui_app.telemetry, "send", send)
    return sent


def _app(block_id="blk-1"):
    clk = _Clk()
    app = types.SimpleNamespace(
        _closing=False, _cfg={"schedule": [dict(BLOCK)], "profiles": []}, _running_block_id=block_id,
        proc=None, _tclock=telemetry_clock.TelemetryClock(clock=clk), _hb_thread=None,
        _telemetry_job=None, _telemetry_tick=lambda: None, after=lambda ms, fn: "job",
        _log_line=lambda s: None,
        _sched_running=False, _set_status=lambda *a: None, _set_start_btn=lambda *a: None,
        _set_running_block=lambda bid: None)
    app._send_heartbeat = lambda blk: gui_app.App._send_heartbeat(app, blk)
    return clk, app


def test_block_by_id():
    assert gui_app.block_by_id(SCHED, "blk-2")["map"] == "anthell"
    assert gui_app.block_by_id(SCHED, "gone") is None and gui_app.block_by_id(SCHED, None) is None


def test_tick_sends_a_v2_window_and_the_next_window_starts_empty(monkeypatch):
    sent = _capture_send(monkeypatch)
    clk, app = _app()
    app.proc = _Proc()
    app._tclock.worker_started()
    clk.t = 40
    app._tclock.mark("farm")
    clk.t = 300
    gui_app.App._telemetry_tick(app)
    assert len(sent) == 1 and sent[0]["v"] == 2
    assert sent[0]["dur"] == 300 and sent[0]["ph"] == {"farm": 260, "travel": 0, "other": 40}
    assert sent[0]["map"] == "desert" and sent[0]["area"] == [[9, 8], [51, 56]]
    clk.t = 360
    gui_app.App._telemetry_tick(app)
    assert sent[1]["dur"] == 60 and sent[1]["ph"]["farm"] == 60


def test_tick_sends_nothing_when_no_worker_is_running(monkeypatch):
    sent = _capture_send(monkeypatch)
    _, app = _app()                                       # proc 是 None
    gui_app.App._telemetry_tick(app)
    assert sent == []


def test_stopping_the_worker_reports_the_last_partial_window_exactly_once(monkeypatch):
    sent = _capture_send(monkeypatch)
    clk, app = _app()

    class _Stoppable:
        stdin = None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    app.proc = _Stoppable()
    app._tclock.worker_started()
    clk.t = 70
    gui_app.App._stop_worker_sync(app)
    assert app.proc is None and len(sent) == 1
    assert sent[0]["dur"] == 70 and sent[0]["v"] == 2
    gui_app.App._stop_worker_sync(app)                    # 已经停了: 不再报
    assert len(sent) == 1


def test_a_worker_that_exits_on_its_own_reports_its_last_window(monkeypatch):
    sent = _capture_send(monkeypatch)
    clk, app = _app()
    app.proc = proc = _Proc(code=1)
    app._tclock.worker_started()
    clk.t = 45
    gui_app.App._on_worker_exit(app, proc, 1)
    assert app.proc is None and len(sent) == 1 and sent[0]["dur"] == 45
    gui_app.App._on_worker_exit(app, proc, 1)             # 慢半拍的重复回调: proc 已经不是当前的, 早退
    assert len(sent) == 1


def test_entering_a_block_reports_a_heartbeat_right_away(monkeypatch):
    sent = _capture_send(monkeypatch)
    clk, app = _app(block_id=None)
    blk = {**gui_schedule.new_block_template({"schedule": [], "profiles": []}),
           "id": "blk-1", "farming_area": [[9, 8], [51, 56]]}
    app._stop_worker_sync = lambda: None
    app._spawn_worker = lambda: app._tclock.worker_started()
    app._set_running_block = lambda bid: setattr(app, "_running_block_id", bid)
    monkeypatch.setattr(gui_app.app_config, "save_config", lambda cfg: None)
    monkeypatch.setattr(gui_app.gui_schedule, "block_to_active", lambda b: {})   # 这里不关心 active 怎么算
    gui_app.App._enter_block(app, blk, False)
    assert len(sent) == 1 and sent[0]["v"] == 2 and sent[0]["dur"] == 0
    assert sent[0]["map"] == blk["map"]


def test_closing_the_window_waits_for_the_last_heartbeat_to_go_out():
    order = []
    t = _FakeThread()
    app = types.SimpleNamespace(
        _updates=None, _closing=False, _tick_job=None, _telemetry_job=None, after_cancel=lambda j: None,
        _stop_worker_sync=lambda: order.append("stop"), _hb_thread=t,
        destroy=lambda: order.append("destroy"))
    gui_app.App.on_closing(app)
    assert order == ["stop", "destroy"] and t.joined == [2]


def test_spawned_workers_are_asked_to_print_phase_markers(monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["env"] = kw["env"]
            self.stdout = iter(())
            self.stdin = None

    monkeypatch.setattr(gui_app.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(gui_app.worker_log, "WorkerLog",
                        lambda *a, **k: types.SimpleNamespace(write=lambda s: None, close=lambda: None))
    monkeypatch.setattr(gui_app.threading, "Thread",
                        lambda **k: types.SimpleNamespace(start=lambda: None))
    clk, app = _app()
    app._pump_log = lambda *a: None
    app._on_worker_traceback = lambda *a: None      # _spawn_worker 现在会给输出泵配一个 traceback 监视器
    gui_app.App._spawn_worker(app)
    assert seen["env"]["FLORR_PHASE_MARKERS"] == "1"
    clk.t = 25
    assert app._tclock.take_window()["dur"] == 25          # 起 worker 的同时开始记账
