import math

import pytest

from canvas_decode import (
    group_by_frame, camera_from_frame, screen_to_world, mobs_from_frame,
    player_world_distance, player_world_position,
    portal_glow_screen_anchor, portal_glow_world_offset,
)
from canvas_frame_fixtures import (
    ZOOM, arc_rec, minimap_rec, text_rec, healthbar_recs, nameplate, player_recs,
    gameplay_frame,
)


def test_group_by_frame():
    recs = [arc_rec(0, 10, 10, 5, "red"), arc_rec(0, 20, 20, 5, "blue"), arc_rec(1, 10, 10, 5, "red")]
    frames = group_by_frame(recs)
    assert len(frames[0]) == 2 and len(frames[1]) == 1


def test_camera_read_directly_from_draw_calls():
    recs = gameplay_frame(0, player_world=(5640.0, 6911.0), mobs=[(800.0, -60.0, "Rock", 1.0)])
    cam = camera_from_frame(recs)
    assert abs(cam["zoom"] - ZOOM) < 1e-6
    assert abs(cam["player_world"][0] - 5640.0) < 0.5
    assert abs(cam["player_world"][1] - 6911.0) < 0.5
    assert cam["player_screen"] == (600.0, 453.5)


def test_camera_raises_without_minimap_dot():
    recs = [r for r in gameplay_frame(0) if abs(r["m"][0]) > 0.05]
    with pytest.raises(ValueError, match="camera"):
        camera_from_frame(recs)


# ── player_world_distance: 蚁穴洞口的世界坐标精确验证用 ────────────────────────

def test_player_world_distance_computes_real_world_units():
    recs = gameplay_frame(0, player_world=(27392.0, 48896.0))
    # 玩家就站在目标点上 —— 距离该是 0(容许浮点误差).
    assert player_world_distance(recs, (27392.0, 48896.0)) == pytest.approx(0.0, abs=0.5)


def test_player_world_distance_is_the_real_hypot_not_a_screen_distance():
    recs = gameplay_frame(0, player_world=(0.0, 0.0))
    # (300,400) 是经典的 3-4-5 直角三角形, 距离该是 500 —— 跟 zoom/player_screen
    # 这些屏幕空间的东西完全无关, 纯世界坐标欧氏距离.
    assert player_world_distance(recs, (300.0, 400.0)) == pytest.approx(500.0, abs=0.5)


def test_player_world_distance_none_when_minimap_dot_missing():
    # camera_from_frame 需要三样东西齐全才不抛; player_world_distance 只需要
    # 玩家小地图点这一项 —— 这条测的是"这一项也没有"时安静返回 None, 不是
    # "三项缺一"的那种更宽松的场景(那种场景在 best_effort 分支里, 不归这个函数管).
    recs = [r for r in gameplay_frame(0) if not (_is_minimap_like(r))]
    assert player_world_distance(recs, (0.0, 0.0)) is None


def _is_minimap_like(rec):
    m = rec.get("m")
    return m is not None and abs(m[0]) < 0.05


def test_player_world_distance_ignores_non_player_minimap_dots():
    # 小地图上还有别的东西(比如传送点自己那个绿点)不该被当成玩家 —— 只认
    # PLAYER_BODY_COLOR("#FFE763")这一个颜色.
    recs = gameplay_frame(0, player_world=(0.0, 0.0)) + [
        minimap_rec(0, world_x=9999.0, world_y=9999.0, color="#7EEF6D"),
    ]
    assert player_world_distance(recs, (0.0, 0.0)) == pytest.approx(0.0, abs=0.5)


# ── player_world_position: "朝洞口精确坐标走" 那一段用来读玩家当前世界坐标 ──────

def test_player_world_position_returns_the_players_world_coordinate():
    recs = gameplay_frame(0, player_world=(27392.0, 48896.0))
    wx, wy = player_world_position(recs)
    assert wx == pytest.approx(27392.0, abs=0.5)
    assert wy == pytest.approx(48896.0, abs=0.5)


def test_player_world_position_none_when_minimap_dot_missing():
    recs = [r for r in gameplay_frame(0) if not (_is_minimap_like(r))]
    assert player_world_position(recs) is None


def test_player_world_distance_is_now_a_thin_wrapper_over_player_world_position():
    # player_world_distance 之前自己重新解一遍小地图玩家点, 现在改成调
    # player_world_position 再算 hypot —— 这条测的是两者读到的是同一个来源,
    # 不是两套各自维护、容易漂移的逻辑.
    recs = gameplay_frame(0, player_world=(123.0, 456.0))
    wx, wy = player_world_position(recs)
    assert player_world_distance(recs, (223.0, 456.0)) == pytest.approx(
        math.hypot(wx - 223.0, wy - 456.0), abs=1e-6)


def test_camera_raises_without_world_scale_reference():
    with pytest.raises(ValueError, match="camera"):
        camera_from_frame([minimap_rec(0, 100.0, 200.0)])


def _gold_body(x, y, r=7.4):
    return {"frame": 0, "op": "fill", "x": x, "y": y, "r": r, "bbox": [x - r, y - r, x + r, y + r],
            "n": 1, "fill": "#FFE763", "stroke": None, "lw": None, "alpha": 1,
            "m": [ZOOM, 0, 0, ZOOM, x, y]}


def test_camera_resolves_self_when_other_players_level_text_is_orphaned():
    # two identical-radius default-skin flowers (self + another player). The other player's
    # HP bar has no #222222 background so no _bar_blocks block is built for them; their "108级"
    # label is loose text. The tie-break must still scan raw text and exclude them.
    recs = list(player_recs(0, 700.0, 470.0))
    recs += [_gold_body(700.0, 470.0)]                     # self
    recs += [_gold_body(200.0, 110.0)]                     # other player, same radius
    recs += ([text_rec(0, 178.0, 131.0, "108级")] * 2)     # their level label, near their body
    recs += healthbar_recs(0, 900.0, 700.0, hp=1.0)        # a mob (gives zoom + a nameplate)
    recs += _labelled(900.0, 700.0, "Sandstorm", "Legendary", "#DE1F1F")
    recs += [minimap_rec(0, 5000.0, 6000.0)]

    cam = camera_from_frame(recs)
    assert cam["player_screen"] == (700.0, 470.0)


def _bare_hp_bar(frame, ax, ay, hp=1.0):
    def s(color, w):
        return {"frame": frame, "op": "stroke", "x": ax, "y": ay, "r": None,
                "bbox": [ax - 30, ay, ax - 30 + w, ay], "n": 2, "fill": "#FFFFFF",
                "stroke": color, "lw": 6, "alpha": 1, "m": [ZOOM, 0, 0, ZOOM, ax, ay]}
    return [s("#222222", 60.0), s("#DD3434", 60.0), s("#75DD34", 60.0 * hp)]


def _black_base(frame, ax, ay, r=22.5):
    return {"frame": frame, "op": "fill", "x": ax, "y": ay, "r": r,
            "bbox": [ax - r, ay - r, ax + r, ay + r], "n": 1, "fill": "#000000",
            "stroke": None, "lw": None, "alpha": 1, "m": [ZOOM, 0, 0, ZOOM, ax, ay]}


def test_camera_strict_raises_but_best_effort_uses_black_base_circle():
    # the player gold body renders off-palette (#F5BF39 skin / damage flash), so the exact
    # #FFE763 match finds nothing. The flower's solid black base circle is still at the anchor.
    recs = []
    for ax, ay in [(300.0, 300.0), (500.0, 700.0), (700.0, 200.0)]:
        recs += healthbar_recs(0, ax, ay, hp=1.0)
        recs += _labelled(ax, ay, "Sandstorm", "Legendary", "#DE1F1F")
    recs += [_black_base(0, 960.0, 472.0)]                 # flower base, at the player anchor
    recs += _bare_hp_bar(0, 960.0, 472.0, hp=1.0)
    recs += [minimap_rec(0, 5000.0, 6000.0)]

    with pytest.raises(ValueError, match="player_screen"):
        camera_from_frame(recs)
    cam = camera_from_frame(recs, best_effort=True)
    assert cam["player_screen"] == (960.0, 472.0)
    assert cam["approx"] is True


def test_camera_best_effort_rejects_offscreen_other_players_body():
    # another player's gold body sits way off-screen (y far below the mob cluster). best_effort
    # must reject it as an outlier and fall back to our own HP bar near the cluster centre.
    recs = list(_bare_hp_bar(0, 400.0, 300.0, hp=1.0))     # a mob bar (no label)
    recs += [_gold_body(1094.0, 2536.0, r=ZOOM)]           # other player, far off-screen
    recs += ([text_rec(0, 1095.0, 2587.0, "106级")] * 2)
    recs += _bare_hp_bar(0, 960.0, 472.0, hp=1.0)          # our own bar
    recs += [minimap_rec(0, 3700.0, 3700.0)]

    cam = camera_from_frame(recs, best_effort=True)
    assert cam["player_screen"] == (960.0, 472.0)
    assert cam["approx"] is True


def test_bar_blocks_ignores_floating_damage_numbers():
    recs = list(player_recs(0))
    recs += healthbar_recs(0, 400.0, 300.0, hp=1.0)
    recs += _labelled(400.0, 300.0, "Beetle", "Mythic", "#1FDBDE")
    recs += ([text_rec(0, 410.0, 305.0, "4729", "#FF5555")] * 2)   # a crit number on the mob
    recs += ([text_rec(0, 395.0, 312.0, "35", "#CE76DB")] * 2)     # a poison tick
    recs += [minimap_rec(0, 5000.0, 6000.0)]

    mob = mobs_from_frame(recs, camera_from_frame(recs))[0]
    assert mob["name"] == "Beetle" and mob["rarity"] == "Mythic"


def test_screen_to_world_is_anchored_on_the_player():
    recs = gameplay_frame(0, player_world=(5000.0, 6000.0), mobs=[(800.0, 300.0, "Rock", 1.0)])
    cam = camera_from_frame(recs)
    # the player's own screen anchor maps back to the player's world position
    wx, wy = screen_to_world(*cam["player_screen"], cam)
    assert abs(wx - 5000.0) < 0.5 and abs(wy - 6000.0) < 0.5


def test_mob_carries_name_rarity_and_hp():
    # florr draws a nameplate as bar strokes, then name text x2, then rarity text x2, all
    # contiguous at the mob's anchor. Build the frame from primitives so the Mythic rarity
    # override lands inside that run rather than after gameplay_frame's trailing minimap draw
    # (which is outside the block's contiguous text scan). The assertion targets are what
    # matter: mobs_from_frame reads name[0], rarity[1], rarity_color, hp.
    ax, ay = 400.0, 200.0
    recs = (
        list(player_recs(0))
        + healthbar_recs(0, ax, ay, hp=0.5)
        + [text_rec(0, ax - 34, ay + 39, "Beetle")] * 2
        + [text_rec(0, ax + 8, ay + 60, "Mythic", "#1FDBDE")] * 2
        + [minimap_rec(0, 5640.0, 6911.0)]
    )
    mob = mobs_from_frame(recs, camera_from_frame(recs))[0]
    assert mob["name"] == "Beetle"
    assert mob["rarity"] == "Mythic"
    assert mob["rarity_color"] == "#1FDBDE"
    assert abs(mob["hp"] - 0.5) < 0.05
    assert mob["sx"] == 400.0 and mob["sy"] == 200.0


def test_mob_with_a_bar_but_no_nameplate_text_reports_no_name():
    recs = list(gameplay_frame(0))                     # player + minimap, no mob
    recs += healthbar_recs(0, 400.0, 200.0, hp=1.0)    # a lone bar, no text
    mob = mobs_from_frame(recs, camera_from_frame(recs))[0]
    assert mob["name"] is None


def test_two_mobs_decode_independently():
    recs = gameplay_frame(0, mobs=[(400.0, 200.0, "Beetle", 1.0), (700.0, 500.0, "Scorpion", 1.0)])
    mobs = mobs_from_frame(recs, camera_from_frame(recs))
    assert sorted(m["name"] for m in mobs) == ["Beetle", "Scorpion"]


def _labelled(ax, ay, name, rarity, color):
    return ([text_rec(0, ax - 21, ay + 39, name)] * 2
            + [text_rec(0, ax + 10, ay + 48, rarity, color)] * 2)


def test_batched_bars_then_batched_labels_first_mob_recovered():
    # florr at desert density draws several mobs' HP bars back to back, THEN their name +
    # rarity text back to back. Old stream-order consumption gave the first mob in the batch
    # an empty nameplate and dropped it — a point-blank Mythic sandstorm was lost this way.
    recs = list(player_recs(0))
    recs += healthbar_recs(0, 400.0, 200.0, hp=1.0)          # mob A bars
    recs += healthbar_recs(0, 700.0, 500.0, hp=1.0)          # mob B bars, straight after
    recs += _labelled(400.0, 200.0, "Sandstorm", "Mythic", "#1FDBDE")   # A labels
    recs += _labelled(700.0, 500.0, "Beetle", "Legendary", "#DE1F1F")   # B labels
    recs += [minimap_rec(0, 5000.0, 6000.0)]

    got = {m["name"]: m["rarity_color"] for m in mobs_from_frame(recs, camera_from_frame(recs))}
    assert got == {"Sandstorm": "#1FDBDE", "Beetle": "#DE1F1F"}


def test_stacked_nameplates_no_cross_contamination():
    # 3 nameplates ~40px apart (a corner pile at min zoom). Clean per-mob draw order.
    # Nearest-anchor assignment let a neighbour's rarity word land in slot 0 (name); each
    # mob must keep its OWN rarity, read from its OWN label run.
    recs = list(player_recs(0))
    for ax, ay, rarity, color in [(120.0, 1040.0, "Rare", "#4D52E3"),
                                  (150.0, 1048.0, "Epic", "#861FDE"),
                                  (95.0, 1055.0, "Unusual", "#FFE65D")]:
        recs += healthbar_recs(0, ax, ay, hp=1.0)
        recs += _labelled(ax, ay, "Fire Ant", rarity, color)
    recs += [minimap_rec(0, 5000.0, 6000.0)]

    mobs = mobs_from_frame(recs, camera_from_frame(recs))
    assert all(m["name"] == "Fire Ant" for m in mobs)
    assert sorted(m["rarity"] for m in mobs) == ["Epic", "Rare", "Unusual"]


# ── portal_glow_screen_anchor / portal_glow_world_offset: 洞口光效实时定位 ──────
# debug_canvas_portal.py 实机(2026-09-18/19)两次独立采样里, 洞口那个带光效的
# 同心圆光晕唯一特征: fillStyle 不是字符串(CanvasGradient序列化成空dict)、
# 半径大(实测90, 玩家花瓣/UI这类东西最大在20上下)、画在主视图不是小地图.

def _glow_rec(frame, x, y, r, scale=ZOOM):
    return {"frame": frame, "op": "fill", "x": x, "y": y, "r": r,
            "bbox": [x - r, y - r, x + r, y + r], "n": 1, "fill": {},
            "stroke": None, "lw": None, "alpha": 1, "m": [scale, 0, 0, scale, x, y]}


def test_portal_glow_screen_anchor_finds_the_gradient_ring():
    recs = gameplay_frame(0) + [_glow_rec(0, 663.0, 317.4, 90.0)]
    assert portal_glow_screen_anchor(recs) == (663.0, 317.4)


def test_portal_glow_screen_anchor_none_when_no_glow_record():
    recs = gameplay_frame(0)
    assert portal_glow_screen_anchor(recs) is None


def test_portal_glow_screen_anchor_ignores_small_gradient_fills():
    # 玩家自己的花瓣/UI元素半径小得多 —— 实测没见过带光效的小玩意超过20,
    # 40这个下限就是为了不把它们也算进来.
    recs = gameplay_frame(0) + [_glow_rec(0, 663.0, 317.4, 10.0)]
    assert portal_glow_screen_anchor(recs) is None


def test_portal_glow_screen_anchor_ignores_minimap_scale_gradients():
    # 小地图上的东西即使凑巧也是渐变填充(目前没见过, 防御性的), 也不该被
    # 当成主视图那个洞口光效 —— 两者的用途/精度完全不是一回事.
    recs = gameplay_frame(0) + [_glow_rec(0, 1700.0, 250.0, 90.0, scale=0.0084)]
    assert portal_glow_screen_anchor(recs) is None


def test_portal_glow_world_offset_converts_using_zoom_and_player_screen():
    # portal_glow_world_offset 只用得到 camera 里 player_screen/zoom 这两项 ——
    # 直接手搭一个, 不用凑齐 camera_from_frame 的全部前提(比如画面里得有怪物
    # 才解得出 zoom), 只测这个函数自己的换算逻辑.
    recs = [_glow_rec(0, 663.0, 317.4, 90.0)]
    camera = {"player_screen": (600.0, 453.5), "zoom": ZOOM}
    dx, dy = portal_glow_world_offset(recs, camera)
    assert dx == pytest.approx((663.0 - 600.0) / ZOOM, abs=0.01)
    assert dy == pytest.approx((317.4 - 453.5) / ZOOM, abs=0.01)


def test_portal_glow_world_offset_none_when_no_glow_record():
    camera = {"player_screen": (600.0, 453.5), "zoom": ZOOM}
    assert portal_glow_world_offset([], camera) is None


# ── 自身血量 / 其他玩家 (2026-09-21 从 florragent 移植) ──────────────────────

def _secondary_stroke(frame, ax, ay, frac, width=60.0):
    """血条上方那条青色窄条(护盾/再生). healthbar_recs 不画它, 这里手搭一条,
    锚点跟主血条一致 —— _bar_blocks 是按"同锚点连续 stroke"归组的."""
    return {"frame": frame, "op": "stroke", "x": ax, "y": ay, "r": None,
            "bbox": [ax - width / 2, ay, ax - width / 2 + width * frac, ay], "n": 2,
            "fill": "#FFFFFF", "stroke": "#42E3F5", "lw": 4, "alpha": 1,
            "m": [ZOOM, 0, 0, ZOOM, ax, ay]}


# ── 回归: 相机认错玩家 / 名牌文字按位置取值 (2026-09-21 实测帧根因) ──────────

def _stroke(frame, ax, ay, color, width, lw=8):
    return {"frame": frame, "op": "stroke", "x": ax, "y": ay, "r": None,
            "bbox": [ax - width / 2, ay, ax - width / 2 + width, ay], "n": 2,
            "fill": "#FFFFFF", "stroke": color, "lw": lw, "alpha": 1,
            "m": [ZOOM, 0, 0, ZOOM, ax, ay]}


def _bare_shielded_bar(frame, ax, ay, hp=1.0):
    """florr 里"自己"和"蜈蚣每一节"画的裸血条(没有名牌): 先 50.4 宽的护盾底 + 0 宽
    青条, 再 72 宽的血条. 实测帧 36767 确认两者结构完全一致, 单帧内没有区分特征."""
    return ([_stroke(frame, ax, ay, "#222222", 50.4),
             _secondary_stroke(frame, ax, ay, 0.0, width=50.4)]
            + healthbar_recs(frame, ax, ay, hp))


def _centipede_frame(frame=0, player_screen=(960.0, 540.0)):
    """真实帧 36767 的结构: 玩家在屏幕中心, 一条蜈蚣的 9 个节段全挤在屏幕左外侧,
    把"所有血条锚点的中位数"拽到 (-134, 72) —— 离玩家 1190px."""
    px, py = player_screen
    recs = list(player_recs(frame, px, py))
    recs += _bare_shielded_bar(frame, px, py, hp=0.37)
    for i in range(9):
        recs += _bare_shielded_bar(frame, -130.0 - i * 12, 72.0 - i * 40)
    recs += nameplate(frame, -283.0, -259.0, "蜈蚣", hp=1.0)
    recs += [minimap_rec(frame, 5640.0, 6911.0)]
    return recs


def test_camera_keeps_gold_body_anchor_when_bars_are_lopsided():
    """蜈蚣链把血条中位数拽到一边时, best_effort 不能把算对的金身锚点丢掉.

    丢了的后果实测过: 战斗录像 165 帧里 70 帧(42%)走进回退分支, 全部认错人."""
    recs = _centipede_frame()
    cam = camera_from_frame(recs, best_effort=True)
    assert cam["player_screen"] == (960.0, 540.0)
    assert not cam.get("approx")


def test_mob_rarity_read_by_colour_not_position():
    """名牌里混进一条额外文本时, 稀有度不能因为"位置挪到 texts[2]"就读错 ——
    稀有度词有自己专属的颜色, 按颜色认."""
    recs = gameplay_frame(0)
    recs += healthbar_recs(0, 400.0, 300.0, hp=0.5)
    recs += [text_rec(0, 360.0, 326.0, "沙尘暴")] * 2
    recs += [text_rec(0, 362.0, 340.0, "干扰文本")] * 2
    recs += [text_rec(0, 403.0, 351.0, "神话", "#1FDBDE")] * 2
    cam = camera_from_frame(recs)
    mob = [m for m in mobs_from_frame(recs, cam) if m["name"] == "沙尘暴"]
    assert len(mob) == 1
    assert mob[0]["rarity"] == "神话"
    assert mob[0]["rarity_color"] == "#1FDBDE"


# ── 大体型怪的名牌离血条更远 (2026-09-22 实测 ultra.json) ────────────────────

def _big_mob_nameplate(frame, ax, ay, name, rarity, rarity_color, dy_name, hp=1.0):
    """florr 把名牌画在怪**身体下方**, 所以离血条锚点的垂直距离随体型增大。

    实测 ultra.json (zoom 0.315) 的 dy: 甲虫/蝎子 +38.9, 沙尘暴 +59~+78,
    神话仙人掌 +83.1, 最大那只 +91.9 —— 它的稀有度词落在 +100.7, 刚好越过
    canvas_hook.js 的 100px 门, 于是"有名字没稀有度", tier 退成 Common。
    正常 zoom 下体型的屏幕半径还要大几倍, dy 会远超 100。
    水平偏移则基本恒定: 名字 dx≈-20.5, 稀有度 dx≈+9.8。
    """
    return (healthbar_recs(frame, ax, ay, hp)
            + [text_rec(frame, ax - 20.5, ay + dy_name, name)] * 2
            + [text_rec(frame, ax + 9.8, ay + dy_name + 8.8, rarity, rarity_color)] * 2)


def test_big_mob_keeps_its_rarity_word():
    """体型大到名牌落在 100px 外时, 稀有度不能丢 —— 丢了就整只怪退成普通怪,
    神话/究极的锁定和规避一次都不触发."""
    recs = gameplay_frame(0)
    recs += _big_mob_nameplate(0, 400.0, 300.0, "沙尘暴", "神话", "#1FDBDE", dy_name=200.0)
    cam = camera_from_frame(recs)
    mob = [m for m in mobs_from_frame(recs, cam) if m["name"] == "沙尘暴"]
    assert len(mob) == 1
    assert mob[0]["rarity"] == "神话"
    assert mob[0]["rarity_color"] == "#1FDBDE"


def test_small_mob_nameplate_still_works():
    recs = gameplay_frame(0)
    recs += _big_mob_nameplate(0, 400.0, 300.0, "甲虫", "传奇", "#DE1F1F", dy_name=38.9)
    cam = camera_from_frame(recs)
    mob = [m for m in mobs_from_frame(recs, cam) if m["name"] == "甲虫"]
    assert mob and mob[0]["rarity"] == "传奇"


def test_text_far_to_the_side_is_not_claimed():
    """放宽的是垂直方向. 水平方向必须仍然卡死, 否则隔壁怪的名牌会串过来."""
    recs = gameplay_frame(0)
    recs += healthbar_recs(0, 400.0, 300.0, hp=1.0)
    recs += [text_rec(0, 900.0, 330.0, "隔壁的名字")] * 2
    cam = camera_from_frame(recs)
    named = [m for m in mobs_from_frame(recs, cam) if m["name"] == "隔壁的名字"]
    assert named == []


def test_text_above_the_bar_is_not_claimed():
    """名牌永远在血条下方; 上方的文本(公告/别的怪的名牌尾巴)不该被认领."""
    recs = gameplay_frame(0)
    recs += healthbar_recs(0, 400.0, 300.0, hp=1.0)
    recs += [text_rec(0, 395.0, 100.0, "上面的字")] * 2
    cam = camera_from_frame(recs)
    assert [m for m in mobs_from_frame(recs, cam) if m["name"] == "上面的字"] == []


def test_hook_label_gate_matches_python_side():
    """canvas_hook.js 在**记录期**就按同一道门丢文本 —— 两边不同步的话, Python 这边
    放得再宽也没用, 数据在页面里就已经没了。这条钉住两个常量一致。"""
    import re
    from pathlib import Path
    import canvas_decode
    js = Path(__file__).with_name("canvas_hook.js").read_text(encoding="utf-8")
    dx = float(re.search(r"var LABEL_MAX_DX = ([\d.]+)", js).group(1))
    dy = float(re.search(r"var LABEL_MAX_DY = ([\d.]+)", js).group(1))
    assert dx >= canvas_decode.LABEL_MAX_DX
    assert dy >= canvas_decode.LABEL_MAX_DY
