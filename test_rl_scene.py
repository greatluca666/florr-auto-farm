"""帧读取层的测试。用假的 drain, 不碰 CDP。"""
import canvas_decode
import pytest

from rl_scene import EMPTY, RANK_BY_COLOR, FrameReader


def rec(frame, **kw):
    d = {"frame": frame, "type": "draw"}
    d.update(kw)
    return d


def reader_over(batches):
    """每次 drain 依次吐一批记录, 吐完之后一直是空。"""
    q = list(batches)
    return FrameReader(drain=lambda: q.pop(0) if q else [])


def test_takes_the_second_newest_frame():
    """最新那帧可能还在画一半 —— 用它会解出半张场景。"""
    r = reader_over([[rec(1, tag="a"), rec(2, tag="b"), rec(3, tag="c")]])
    got = r.latest_frame()
    assert [x["tag"] for x in got] == ["b"]


def test_one_frame_is_not_enough():
    assert reader_over([[rec(1)]]).latest_frame() is None


def test_buffer_keeps_only_the_newest_frame_after_a_take():
    """取过的帧要丢掉, 否则缓冲会一直涨, 而且下一拍还会把旧帧再解一遍。"""
    r = reader_over([[rec(1), rec(2), rec(3)], []])
    r.latest_frame()
    assert {x["frame"] for x in r._buf} == {3}


def test_records_spanning_two_drains_are_stitched():
    """一帧的 draw call 可能被 drain 从中间切断 —— 缓冲就是为这个存在的。"""
    r = reader_over([[rec(1, tag="a")], [rec(1, tag="b"), rec(2)]])
    assert r.latest_frame() is None
    assert [x["tag"] for x in r.latest_frame()] == ["a", "b"]


def test_two_readers_do_not_share_a_buffer():
    """原来这段是模块级全局缓冲: 录制和在线训练同时跑会互相把帧吃掉。"""
    a = reader_over([[rec(1), rec(2), rec(3)]])
    b = reader_over([[rec(1), rec(2), rec(3)]])
    assert a.latest_frame() is not None
    assert b.latest_frame() is not None


def test_clear_drops_the_buffer():
    r = reader_over([[rec(1, tag="a")], []])
    r.latest_frame()
    r.clear()
    assert r._buf == []


# ── scene() ──────────────────────────────────────────────────────────────────

def stub_decoders(monkeypatch, *, camera=None, player=None, mobs=None, others=None,
                  hud=None):
    monkeypatch.setattr(canvas_decode, "camera_from_frame",
                        lambda recs, best_effort=False: camera or {})
    monkeypatch.setattr(canvas_decode, "hud_self_from_frame", lambda recs: hud)
    monkeypatch.setattr(canvas_decode, "player_from_frame", lambda recs, cam: player)
    monkeypatch.setattr(canvas_decode, "mobs_from_frame", lambda recs, cam: list(mobs or []))
    monkeypatch.setattr(canvas_decode, "other_players_from_frame",
                        lambda recs, cam, **kw: list(others or []))


def test_no_frame_gives_empty():
    sc = reader_over([[]]).scene()
    assert sc.player is None and sc.mobs == [] and sc.miss == "no_frame"


def test_scene_attaches_rarity_rank_from_colour(monkeypatch):
    stub_decoders(monkeypatch, mobs=[{"x": 1.0, "y": 2.0, "rarity_color": "#1FDBDE"}])
    sc = reader_over([[rec(1), rec(2)]]).scene()
    assert sc.mobs[0]["rarity_rank"] == RANK_BY_COLOR["#1FDBDE"] == 5


def test_unknown_colour_reads_as_minus_one(monkeypatch):
    stub_decoders(monkeypatch, mobs=[{"x": 1.0, "y": 2.0, "rarity_color": "#ABCDEF"}])
    assert reader_over([[rec(1), rec(2)]]).scene().mobs[0]["rarity_rank"] == -1


def test_scene_carries_the_approx_flag_and_anchor(monkeypatch):
    stub_decoders(monkeypatch, camera={"approx": True, "player_screen": (960.0, 540.0)})
    sc = reader_over([[rec(1), rec(2)]]).scene()
    assert sc.approx is True and sc.player_screen == (960.0, 540.0)


def test_one_decoder_blowing_up_does_not_take_the_others_down(monkeypatch):
    """自己解不出来(常事: 自己的金色身体那一帧没画)不该连带把怪也丢掉。"""
    stub_decoders(monkeypatch, mobs=[{"x": 1.0, "y": 2.0, "rarity_color": "#7EEF6D"}])
    monkeypatch.setattr(canvas_decode, "player_from_frame",
                        lambda recs, cam: (_ for _ in ()).throw(ValueError("boom")))
    sc = reader_over([[rec(1), rec(2)]]).scene()
    assert sc.player is None and len(sc.mobs) == 1


def test_camera_blowing_up_gives_empty(monkeypatch):
    """相机解不出来, 后面所有坐标都没有参照系 —— 整帧作废才对。"""
    stub_decoders(monkeypatch)
    monkeypatch.setattr(canvas_decode, "camera_from_frame",
                        lambda recs, best_effort=False: (_ for _ in ()).throw(ValueError()))
    sc = reader_over([[rec(1), rec(2)]]).scene()
    assert sc.player is None and sc.mobs == [] and sc.player_screen is None


# ── 自己: 位置跟相机走, 血量先读左上角 ──────────────────────────────────────
#
# 原来"自己"= 脚下那条血条: 读不到(挨打闪色/被怪挤住/相机只能近似)就整个 None, 观测
# 连怪带人清零。可位置本来就在相机里(小地图 + 屏幕锚点), 真正只能从自己身上读的只有
# 血量 —— 而左上角头像卡上就有, 不用先在世界里找到自己(用户 2026-09-24 指出)。

CAM = {"player_world": (5000.0, 6000.0), "player_screen": (960.0, 540.0), "zoom": 0.3}
WORLD_BAR = {"hp": 0.8, "hp_secondary": 0.1, "x": 5000.0, "y": 6000.0}


def scene(monkeypatch, **kw):
    stub_decoders(monkeypatch, **kw)
    return reader_over([[rec(1), rec(2)]]).scene()


def test_the_hud_hp_is_used_even_when_the_bar_under_the_flower_is_lost(monkeypatch):
    sc = scene(monkeypatch, camera=CAM, player=None, hud={"hp": 0.6, "hp_secondary": 0.2})
    assert sc.player == {"hp": 0.6, "hp_secondary": 0.2, "x": 5000.0, "y": 6000.0}
    assert sc.miss is None


def test_the_bar_under_the_flower_is_the_fallback(monkeypatch):
    sc = scene(monkeypatch, camera=CAM, player=WORLD_BAR, hud=None)
    assert sc.player["hp"] == 0.8 and sc.player["hp_secondary"] == 0.1
    assert sc.miss is None


def test_losing_both_hp_readings_keeps_the_position(monkeypatch):
    """血量读不到不该让策略连怪都看不见 —— 位置照给, 血量标未知。"""
    sc = scene(monkeypatch, camera=CAM, player=None, hud=None,
               mobs=[{"x": 5100.0, "y": 6000.0, "rarity_color": "#7EEF6D"}])
    assert sc.player == {"hp": None, "hp_secondary": None, "x": 5000.0, "y": 6000.0}
    assert sc.miss == "no_hp" and len(sc.mobs) == 1


def test_an_approximate_camera_still_gives_a_position(monkeypatch):
    sc = scene(monkeypatch, camera=dict(CAM, approx=True), player=None,
               hud={"hp": 0.9, "hp_secondary": 0.0})
    assert sc.player["hp"] == 0.9 and sc.approx is True and sc.miss is None


def test_a_camera_failure_names_what_was_missing(monkeypatch):
    stub_decoders(monkeypatch)
    monkeypatch.setattr(
        canvas_decode, "camera_from_frame",
        lambda recs, best_effort=False: (_ for _ in ()).throw(
            ValueError("camera undetermined for this frame, missing: zoom, player_world")))
    sc = reader_over([[rec(1), rec(2)]]).scene()
    assert sc.player is None and sc.miss == "camera:zoom,player_world"


def test_a_player_decoder_crash_is_reported_not_swallowed(monkeypatch):
    stub_decoders(monkeypatch, camera=CAM)
    monkeypatch.setattr(canvas_decode, "player_from_frame",
                        lambda recs, cam: (_ for _ in ()).throw(KeyError("hp")))
    sc = reader_over([[rec(1), rec(2)]]).scene()
    assert sc.miss == "player_error:KeyError" and sc.player["x"] == 5000.0
