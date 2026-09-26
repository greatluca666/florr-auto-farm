"""拿真实客户端抓下来的帧当回归用例。

test_canvas_decode.py 里的合成用例钉的是"我以为 florr 这样画"; 这里钉的是"florr 实际
就这样画过"。两份都是用户 2026-09-22 在真实沙漠里用
`debug_canvas_enemies.py desert --dump` 抓的:

  desert_mythic_26mobs.json  26 只怪, 含 4 只神话 (旧 hook 抓的)
  desert_ultra_44mobs.json   44 只怪, 含 2 只神话 + 1 只究极 + 一整条蜈蚣 (新 hook 抓的)

后者是"大体型怪丢稀有度"修完之后重抓的 —— 它同时是那次修复的验收证据: 旧 hook 抓的
那一份里有"有名字没稀有度"的怪, 新的这份里一个都没有。
"""
import json
import math
from pathlib import Path

import pytest

import canvas_decode as cd
import enemy_detect
import utils

FRAMES = Path(__file__).with_name("test_frames")


def load(name):
    return json.loads((FRAMES / name).read_text(encoding="utf-8"))["raw"]


@pytest.fixture(autouse=True)
def _desert():
    utils.apply_map("desert")


# ── 究极帧 (新 hook 抓的, 44 只怪) ────────────────────────────────────────────

@pytest.fixture
def ultra():
    raw = load("desert_ultra_44mobs.json")
    cam = cd.camera_from_frame(raw, best_effort=True)
    return raw, cam, cd.mobs_from_frame(raw, cam)


def test_ultra_frame_camera_is_exact_not_approximate(ultra):
    _, cam, _ = ultra
    assert cam.get("approx") is not True
    assert cam["player_screen"] == (960, 472.5)


def test_every_named_mob_also_has_a_rarity(ultra):
    """这就是"大体型怪丢稀有度"那个 bug 的验收条件。

    旧 hook 抓的帧里最大那只沙尘暴只有名字没有稀有度词(稀有度落在 100.7px, 越过了
    当时那个半径 100 的圆形门), 于是 tier 退成 Common, 神话/究极的锁定和规避全不触发。
    """
    _, _, mobs = ultra
    named = [m for m in mobs if m.get("name")]
    # 先钉住"名字本身没少" —— 否则门一收紧, 名字和稀有度一起丢光, 下面那条会假通过
    assert len(named) == 37, f"解出名字的怪只有 {len(named)} 只, 应为 37"
    broken = [m for m in named if not m.get("rarity")]
    assert broken == [], f"有名字没稀有度的怪: {broken}"


def test_ultra_rarity_decodes_end_to_end(ultra):
    """究极怪从颜色到档名整条链路通。#FF2B75 / "究极" 都由这一帧实证。"""
    _, _, mobs = ultra
    ultras = [m for m in mobs if m.get("rarity") == "究极"]
    assert len(ultras) == 1
    assert ultras[0]["rarity_color"] == "#FF2B75"
    assert enemy_detect._tier_from_color(ultras[0]["rarity_color"],
                                         ultras[0]["rarity"]) == "Ultra"
    assert enemy_detect._species_from_name(ultras[0]["name"]) == "sandstorm"


def test_mythic_rarity_decodes_end_to_end(ultra):
    _, _, mobs = ultra
    mythics = [m for m in mobs if m.get("rarity") == "神话"]
    assert len(mythics) == 2
    assert {m["rarity_color"] for m in mythics} == {"#1FDBDE"}


def test_unnamed_blocks_are_the_centipede_body(ultra):
    """没有名牌的块不是"漏掉的怪" —— 蜈蚣每一节都画自己的血条, 只有头顶着名牌。

    钉这条是防止以后有人看到"7 个块没名字"就去放宽某个阈值硬凑。
    """
    raw, cam, mobs = ultra
    unnamed = [b for b in cd._bar_blocks(raw)
               if not b["texts"] and not cd._is_player_anchor(b["anchor"], cam)]
    assert len(unnamed) == 7
    head = [m for m in mobs if m.get("name") == "蜈蚣"]
    assert len(head) == 1
    # 7 节连成一条链, 一路通到头部, 相邻两节间距不超过一个身位
    chain = sorted([b["anchor"] for b in unnamed] + [(head[0]["sx"], head[0]["sy"])],
                   key=lambda a: -a[1])
    gaps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(chain, chain[1:])]
    assert max(gaps) < 100, f"间距 {gaps} —— 这些块不像同一条蜈蚣"


def test_ultra_frame_decision_is_cautious_engage(ultra):
    """究极沙尘暴走 CAUTIOUS: 接战但保持距离, 不是无视也不是掉头逃。"""
    _, cam, mobs = ultra
    dets = [{"species": enemy_detect._species_from_name(m["name"]),
             "rarity": enemy_detect._tier_from_color(m.get("rarity_color"), m.get("rarity")),
             "screen_pos": (m["sx"], m["sy"]), "confidence": 1.0}
            for m in mobs if enemy_detect._species_from_name(m.get("name"))]
    action = enemy_detect.select_action(dets, avoid_trigger_px=400, cautious_hold_px=250,
                                        center=cam["player_screen"], chase_min_conf=0.4)
    assert action[0] == "chase"
    assert action[1]["rarity"] == "Ultra"


# ── 神话帧 (旧 hook 抓的, 26 只怪) ────────────────────────────────────────────

@pytest.fixture
def mythic():
    raw = load("desert_mythic_26mobs.json")
    cam = cd.camera_from_frame(raw, best_effort=True)
    return raw, cam, cd.mobs_from_frame(raw, cam)


def test_mythic_frame_player_is_off_centre_at_the_map_edge(mythic):
    """地图边界上相机顶住不再跟随, 玩家锚点离画布中心有 162px。

    钉这条是因为 enemy_detect 拿 SCREEN_CENTER 当玩家位置 —— 哪天要修那个偏差,
    这帧就是现成的证据。
    """
    _, cam, _ = mythic
    assert cam["player_screen"] == (798.0162963867188, 472.5)


def test_mythic_frame_finds_all_four_mythics(mythic):
    _, _, mobs = mythic
    assert len([m for m in mobs if m.get("rarity") == "神话"]) == 4


# ── 距离判定必须用真实锚点 (2026-09-22) ──────────────────────────────────────

def test_real_frame_centre_offset_flips_real_decisions(mythic):
    """地图边界那一帧: 真实锚点离屏幕中心 162px, 实测有 13 条距离判定被它翻转。

    最扎眼的一条: 一只神话沙尘暴, 按屏幕中心算 303px(< avoid_trigger 400 -> 掉头逃),
    按真实锚点算 417px(不该逃)。钉住这条, 以后谁把 center 改回 SCREEN_CENTER 会红。
    """
    _, cam, mobs = mythic
    true_c = cam["player_screen"]
    wrong_c = enemy_detect.SCREEN_CENTER
    flips = 0
    for m in mobs:
        if not m.get("name"):
            continue
        dw = math.hypot(m["sx"] - wrong_c[0], m["sy"] - wrong_c[1])
        dt = math.hypot(m["sx"] - true_c[0], m["sy"] - true_c[1])
        for thr in (250, 400, 650, 850):
            flips += (dw < thr) != (dt < thr)
    assert flips == 13

    mythic_sandstorms = [m for m in mobs
                         if m.get("rarity") == "神话" and m.get("name") == "沙尘暴"]
    crossing = [m for m in mythic_sandstorms
                if math.hypot(m["sx"] - wrong_c[0], m["sy"] - wrong_c[1]) < 400
                <= math.hypot(m["sx"] - true_c[0], m["sy"] - true_c[1])]
    assert len(crossing) == 1


def test_scan_enemies_reports_the_decoded_anchor(monkeypatch):
    """端到端: scan_enemies 跑完, current_center() 要是这一帧解出来的玩家锚点,
    不是 SCREEN_CENTER。"""
    import cdp_bridge
    raw = load("desert_mythic_26mobs.json")
    monkeypatch.setattr(cdp_bridge, "inject_canvas_hook", lambda *a, **k: None)
    # scan_enemies 取"次新"那一帧, 所以真实记录挂 frame 0, 再垫一条 frame 1
    monkeypatch.setattr(cdp_bridge, "drain_canvas_log",
                        lambda *a, **k: [dict(r, frame=0) for r in raw] + [{"frame": 1}])
    enemy_detect._frame_buffer[:] = []
    enemy_detect._last_center = None

    dets = enemy_detect.scan_enemies()
    assert len(dets) > 20
    cx, cy = enemy_detect.current_center()
    assert abs(cx - 798.0) < 0.1 and abs(cy - 472.5) < 0.1
    assert (cx, cy) != enemy_detect.SCREEN_CENTER


# ── 自己挨打闪色 (florragent 真实战斗录像里抽的两帧) ──────────────────────────
#
# 实机(2026-09-24)进度行里"解出自己"常常只有 18~50%。战斗录像 165 帧里, 自己的
# 花身颜色不是 #FFE763 的正好 39 帧 —— 跟"相机只能近似"的 39 帧一一对上。花身挨打时
# 从金色往白/往红插值(#FFEE91 / #FFF4BB / #FF9248 / #FF7230), 只认精确色号就认不出
# 自己: 越是在打架, 越是认不出自己。

@pytest.mark.parametrize("name", ["self_hitflash_white.json", "self_hitflash_red.json"])
def test_self_is_still_found_while_the_body_flashes(name):
    raw = load(name)
    cam = cd.camera_from_frame(raw, best_effort=True)
    assert not cam["approx"]
    assert math.hypot(cam["player_screen"][0] - 960.5, cam["player_screen"][1] - 540.5) < 2


@pytest.mark.parametrize("color,ok", [
    ("#FFE763", True),                    # 正常金色
    ("#FFEE91", True), ("#FFF4BB", True),   # 往白闪
    ("#FF9248", True), ("#FF7230", True),   # 往红闪
    ("#CFBB50", False),                   # 花身描边(往黑), 不是花身
    ("#EAE6D2", False),                   # 闪白时的描边
    ("#FFFFFF", False), ("#FF0000", False),  # 闪到头的纯白/纯红: 太常见, 不认
    ("#7EEF6D", False), ("#DE1F1F", False),  # 普通/传说怪的稀有度色
])
def test_what_counts_as_our_body_color(color, ok):
    assert cd._is_self_body_color(color) is ok


# ── 状态染色: 头像和花身跟着中毒/受伤等状态变色 ──────────────────────────────
#
# 实机(2026-09-25)存下的 41 帧 no_hp: 左上角头像卡其实都在, 血条也在, 但
# 头像是 #CE76DA(紫, 中毒)、#F9D970、#E2658C、#FFE2B5 …… 几十种色 —— 按颜色认头像,
# 整张卡被跳过; 世界里的花身同色, 相机也找不到自己, 脚下血条那条备用路也跟着断。
# 颜色靠不住, 靠几何: 头像 = 血条左边同一行、同缩放的"描边圈 + 身体圈"(半径比 1.128)。
# 然后头像的颜色就是这一帧花身的颜色, 拿它去世界里找自己。

TINTED = ["self_tint_poison.json", "self_tint_dimgold.json", "self_tint_red.json",
          "self_tint_pale.json"]


@pytest.mark.parametrize("name", TINTED)
def test_the_camera_finds_our_tinted_body(name):
    cam = cd.camera_from_frame(load(name), best_effort=True)
    assert not cam["approx"]
    assert math.hypot(cam["player_screen"][0] - 960, cam["player_screen"][1] - 540) < 5


@pytest.mark.parametrize("name", TINTED)
def test_the_hud_avatar_colour_is_reported(name):
    raw = load(name)
    body = [r for r in raw if r["op"] == "fill" and r.get("m") and abs((r.get("r") or 0) - 29.375) < 0.01
            and abs(r["m"][4] - 60) < 1]
    assert cd.hud_self_colour(raw) == body[0]["fill"]


# 花身会整体旋转(矩阵带旋转分量, 缩放不变; 头像卡上的头像跟着一起转)。原来"不许旋转"
# 是为了挡掉转着画的怪, 顺手把转着的自己也挡了。另一种: 怪全挤在屏幕一边, 宽松模式的
# "离怪群中位数太远 = 别人的花身"判据把自己扔了(严格模式明明找到了)。
# 跟头像**同色**是独立佐证 —— 这两道闸对它放行。

@pytest.mark.parametrize("name", ["self_rotated_tinted.json", "self_rotated_gold.json",
                                  "self_off_cluster.json"])
def test_the_camera_trusts_a_body_that_matches_the_hud_avatar(name):
    cam = cd.camera_from_frame(load(name), best_effort=True)
    assert not cam["approx"]
    assert math.hypot(cam["player_screen"][0] - 960, cam["player_screen"][1] - 540) < 5
