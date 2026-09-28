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
