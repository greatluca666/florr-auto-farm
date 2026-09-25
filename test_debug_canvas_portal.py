import cv2
import numpy as np
import pytest

from debug_canvas_portal import is_greenish, candidate_fills, world_position_of
from canvas_frame_fixtures import arc_rec, minimap_rec, player_recs
import canvas_decode


# ── is_greenish ──────────────────────────────────────────────────────────────

def test_is_greenish_matches_the_calibrated_portal_hue():
    # #6fe46b 换算出来的 HSV(59,135,228) 就是 utils._PORTAL_GREEN_HSV_LO/HI 标定
    # 那次实测的传送点绿本尊(小地图截图上量的) —— 这条锁住"至少这个具体色号能
    # 被认出来", 不是泛泛地测个颜色轮.
    assert is_greenish("#6fe46b") is True


@pytest.mark.parametrize("fill", ["#FFE763", "#CFBB50", "#42E3F5", None, "not-a-color", "#zzzzzz"])
def test_is_greenish_rejects_known_non_green_fills(fill):
    # 玩家本体金色 / 描边色 / 青色次血条 —— 都不该被当成候选里的"绿"。
    # 顺带盖住几种非法输入(None/畸形字符串)不该抛异常, 只是返回 False.
    assert is_greenish(fill) is False


def test_is_greenish_handles_gradient_fill_without_crashing():
    # 实机复盘(2026-09-17): fillStyle 不一定是字符串 —— CanvasGradient/
    # CanvasPattern 经 CDP returnByValue 序列化出来是个空 dict, 拿去当字符串
    # 处理(.startswith)会直接 AttributeError. 拿不到颜色信息只能说"不知道",
    # 用 False 表达(不打★), 不能崩.
    assert is_greenish({}) is False
    assert is_greenish({"some": "gradient-ish junk"}) is False


def test_is_greenish_also_matches_mob_body_green_by_design():
    # #8AC255(gameplay_frame 夹具里怪物本体描边色)的色相(H=45)也落在这条粗筛的
    # 40~80范围里 —— 这是设计上就接受的不精确(docstring 说得很清楚: 这只是给
    # 人眼扫列表用的粗提示, 真正确认还是要看屏幕坐标跟肉眼看到的洞口对不对得上,
    # 不是靠这一个函数单独判定)。这里写成断言而不是留白, 是为了以后有人手滑把
    # 阈值收窄导致它悄悄变严格时, 至少有一条测试会因为这条断言的语义变化被看到
    # (虽然收窄不会让这条测试失败——它只在被放宽到连这个都不认的时候失败),
    # 主要价值是把这份"已知不精确"用可执行的方式记录下来, 不是纯靠注释.
    assert is_greenish("#8AC255") is True


# ── candidate_fills ──────────────────────────────────────────────────────────

def test_candidate_fills_keeps_gradient_or_pattern_fills_without_crashing():
    # 实机复盘(2026-09-17): 真实抓到的一帧里就有一条 fill 是 {}(渐变/图案序列化
    # 结果)。改之前 `r.get("fill") in _KNOWN_PLAYER_FILLS` 直接
    # TypeError: unhashable type: 'dict' 把整个脚本炸掉。这类填充恰恰可能就是
    # 带光效的洞口本体, 不能被这条判断误伤/崩掉——必须收进候选列表, 不是排除.
    recs = [
        arc_rec(0, 700.0, 500.0, 8.0, {}),               # 真实撞到过的形状
        arc_rec(0, 720.0, 520.0, 8.0, {"stops": [1, 2]}),  # 万一以后序列化出更多内容
    ]
    cands = candidate_fills(recs)   # 不该抛异常
    assert len(cands) == 2


def test_candidate_fills_excludes_known_player_body_colors():
    recs = player_recs(0, sx=600.0, sy=453.5) + [
        arc_rec(0, 700.0, 500.0, 8.0, "#6fe46b"),   # 候选: 传送点绿
    ]
    cands = candidate_fills(recs)
    fills = [r["fill"] for r in cands]
    assert "#6fe46b" in fills
    assert not ({"#CFBB50", "#FFE763", "#111111"} & set(fills))


def test_candidate_fills_excludes_non_fill_and_radiusless_records():
    recs = [
        arc_rec(0, 700.0, 500.0, 8.0, "#6fe46b"),
        {"op": "stroke", "x": 1, "y": 1, "r": None, "fill": "#6fe46b", "m": [1, 0, 0, 1, 1, 1]},
        {"op": "fill", "x": 1, "y": 1, "r": None, "fill": "#6fe46b", "m": [1, 0, 0, 1, 1, 1]},
    ]
    cands = candidate_fills(recs)
    assert len(cands) == 1
    assert cands[0]["fill"] == "#6fe46b"


def test_candidate_fills_sorts_by_distance_to_player_screen_anchor():
    camera = {"zoom": 0.7558333, "player_world": (0.0, 0.0), "player_screen": (600.0, 453.5)}
    near = arc_rec(0, 620.0, 460.0, 5.0, "#aaaaaa")    # 离玩家很近
    far = arc_rec(0, 50.0, 50.0, 5.0, "#bbbbbb")       # 离玩家很远(但离画布原点近)
    cands = candidate_fills([far, near], camera=camera)
    assert [r["fill"] for r in cands] == ["#aaaaaa", "#bbbbbb"]   # 近的排前面


def test_candidate_fills_falls_back_to_canvas_origin_without_camera():
    # camera 解不出时(比如设置面板挡住了玩家本体)退化成按画布原点排序, 不崩.
    near_origin = arc_rec(0, 10.0, 10.0, 5.0, "#aaaaaa")
    far_from_origin = arc_rec(0, 900.0, 900.0, 5.0, "#bbbbbb")
    cands = candidate_fills([far_from_origin, near_origin], camera=None)
    assert [r["fill"] for r in cands] == ["#aaaaaa", "#bbbbbb"]


# ── world_position_of ────────────────────────────────────────────────────────

def test_world_position_of_minimap_record_needs_no_camera():
    # 小地图绘制直接用小地图公式反解, 不需要相机信息.
    rec = minimap_rec(0, world_x=5640.0, world_y=6911.0)
    wx, wy = world_position_of(rec, camera=None)
    assert wx == pytest.approx(5640.0, abs=0.5)
    assert wy == pytest.approx(6911.0, abs=0.5)


def test_world_position_of_main_view_record_uses_camera():
    camera = {"zoom": 1.0, "player_world": (100.0, 100.0), "player_screen": (600.0, 453.5)}
    # 主视图里, 屏幕锚点比玩家锚点偏移(50,50), zoom=1 时世界坐标就该偏移同样的量.
    rec = arc_rec(0, 650.0, 503.5, 8.0, "#6fe46b")
    wx, wy = world_position_of(rec, camera)
    assert (wx, wy) == pytest.approx((150.0, 150.0))


def test_world_position_of_main_view_record_without_camera_is_none():
    # 主视图记录解不出世界坐标时(相机没解出来)老实返回 None, 不瞎猜一个坐标.
    rec = arc_rec(0, 650.0, 503.5, 8.0, "#6fe46b")
    assert world_position_of(rec, camera=None) is None
