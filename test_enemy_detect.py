import math
import pytest
from enemy_detect import (
    classify_action, priority_score, aim_mouse_target, flee_mouse_target,
)


_ALL_SPECIES = [
    "scorpion", "beetle", "cactus", "sandstorm",
    "sand_centipede", "soldier_fire_ant",
]
_BELOW_ULTRA = ["Common", "Unusual", "Rare", "Epic", "Legendary", "Mythic"]
_ABOVE_ULTRA = ["Super", "Eternal", "Unique"]


def test_classify_action_engage_below_ultra_any_species():
    for species in _ALL_SPECIES:
        for rarity in _BELOW_ULTRA:
            assert classify_action(species, rarity) == "ENGAGE"


def test_classify_action_ultra_avoid_species():
    assert classify_action("scorpion", "Ultra") == "AVOID"
    assert classify_action("beetle", "Ultra") == "AVOID"


def test_classify_action_ultra_cautious_species():
    for species in ["sandstorm", "cactus", "sand_centipede", "soldier_fire_ant"]:
        assert classify_action(species, "Ultra") == "CAUTIOUS"


def test_classify_action_above_ultra_falls_back_to_avoid():
    for species in _ALL_SPECIES:
        for rarity in _ABOVE_ULTRA:
            assert classify_action(species, rarity) == "AVOID"


def test_priority_score_rarity_dominates_species():
    # Rare sand_centipede(物种优先级最低)该压过Common sandstorm(物种优先级最高) ——
    # 稀有度是第一比较项, 碾压式的.
    assert priority_score("sand_centipede", "Rare") > priority_score("sandstorm", "Common")


def test_priority_score_species_tiebreak_within_same_rarity():
    assert priority_score("sandstorm", "Common") > priority_score("cactus", "Common")
    assert priority_score("cactus", "Common") > priority_score("beetle", "Common")
    assert priority_score("beetle", "Common") > priority_score("scorpion", "Common")
    assert priority_score("scorpion", "Common") > priority_score("sand_centipede", "Common")
    assert priority_score("sand_centipede", "Common") == priority_score("soldier_fire_ant", "Common")


def test_aim_mouse_target_points_toward_target_beyond_hold():
    result = aim_mouse_target((1460, 540), hold_px=None, center=(960, 540), max_extend=500)
    assert result[0] > 960
    assert abs(result[1] - 540) < 1e-6


def test_aim_mouse_target_stops_at_hold_distance():
    result = aim_mouse_target((1200, 540), hold_px=250, center=(960, 540))
    assert result == (960, 540)


def test_aim_mouse_target_clamps_to_max_extend():
    result = aim_mouse_target((3000, 540), hold_px=None, center=(960, 540), max_extend=500)
    assert result == (1460, 540)


def test_aim_mouse_target_chases_to_actual_distance_when_within_max_extend():
    # dist=100 is well inside max_extend=500 — the mouse should land exactly
    # at the target's offset from center, not jump all the way to max_extend.
    result = aim_mouse_target((1060, 540), hold_px=None, center=(960, 540), max_extend=500)
    assert result == (1060, 540)


def test_aim_mouse_target_no_repel_positions_is_unchanged():
    # repel_positions 为 None/空 -> 跟没这个参数时结果完全一致
    a = aim_mouse_target((1060, 540), hold_px=None, center=(960, 540), max_extend=500)
    b = aim_mouse_target((1060, 540), hold_px=None, center=(960, 540), max_extend=500,
                         repel_positions=[])
    assert a == b == (1060, 540)


def test_aim_mouse_target_bends_away_from_danger_on_the_path():
    # 目标正右方, 一只危险怪在右上方近处 -> 瞄点应被往下压 (远离危险), 但整体
    # 仍朝右 (还在追)
    result = aim_mouse_target((1460, 540), hold_px=None, center=(960, 540), max_extend=500,
                              repel_positions=[(1160, 440)], repel_px=400)
    assert result[0] > 960          # 仍在朝目标方向 (右)
    assert result[1] > 540          # 被危险怪 (在上方, y 小) 往下推


def test_aim_mouse_target_repels_even_while_holding_distance():
    # 已进 CAUTIOUS 保持距离内, 平时返回 center; 但半路有危险怪 -> 仍往远离方向挪
    held = aim_mouse_target((1100, 540), hold_px=250, center=(960, 540))
    assert held == (960, 540)
    with_danger = aim_mouse_target((1100, 540), hold_px=250, center=(960, 540),
                                   repel_positions=[(960, 440)], repel_px=400)
    assert with_danger != (960, 540)
    assert with_danger[1] > 540     # 危险怪在上方 -> 往下挪


def test_flee_mouse_target_points_away_from_single_threat():
    result = flee_mouse_target([(1460, 540)], center=(960, 540), extend=400)
    assert result[0] < 960
    assert abs(result[1] - 540) < 1e-6


def test_flee_mouse_target_returns_center_when_forces_cancel():
    result = flee_mouse_target([(1460, 540), (460, 540)], center=(960, 540))
    assert result == (960, 540)


from enemy_detect import (
    mythic_candidates, pick_mythic_target, mythic_move_target,
    MYTHIC_KITE_SPECIES, MYTHIC_TARGET_RANK,
)

from enemy_detect import select_action, chase_is_stalled


def _det(species, rarity, screen_pos, conf=0.9):
    return {
        "species": species, "rarity": rarity, "screen_pos": screen_pos,
        "bbox": (0, 0, 0, 0), "confidence": conf,
    }


def test_mythic_kite_species_table_is_the_five_non_sandstorm_species():
    assert set(MYTHIC_KITE_SPECIES) == {
        "beetle", "soldier_fire_ant", "scorpion", "sand_centipede", "cactus",
    }
    assert set(MYTHIC_KITE_SPECIES.values()) <= {"strafe", "ram", "hold"}
    assert MYTHIC_KITE_SPECIES["beetle"] == "strafe"
    assert MYTHIC_KITE_SPECIES["soldier_fire_ant"] == "strafe"
    assert MYTHIC_KITE_SPECIES["scorpion"] == "ram"
    assert MYTHIC_KITE_SPECIES["sand_centipede"] == "ram"
    assert MYTHIC_KITE_SPECIES["cactus"] == "hold"


def test_mythic_target_rank_order():
    order = ["beetle", "soldier_fire_ant", "scorpion", "sand_centipede", "cactus"]
    ranks = [MYTHIC_TARGET_RANK[s] for s in order]
    assert ranks == sorted(ranks, reverse=True)
    assert len(set(ranks)) == 5


def test_mythic_candidates_filters_rarity_species_and_conf():
    dets = [
        _det("beetle", "Mythic", (100, 100), conf=0.9),        # keep
        _det("cactus", "Mythic", (200, 200), conf=0.9),        # keep
        _det("beetle", "Ultra", (300, 300), conf=0.9),         # wrong rarity
        _det("sandstorm", "Mythic", (400, 400), conf=0.9),     # sandstorm excluded
        _det("scorpion", "Mythic", (500, 500), conf=0.4),      # below conf gate
    ]
    got = mythic_candidates(dets, chase_min_conf=0.55)
    assert [d["species"] for d in got] == ["beetle", "cactus"]


def test_mythic_candidates_empty_when_nothing_qualifies():
    assert mythic_candidates([]) == []
    assert mythic_candidates([_det("sandstorm", "Mythic", (10, 10))]) == []


def test_pick_mythic_target_none_when_empty_or_out_of_radius():
    assert pick_mythic_target([], center=(960, 540)) is None
    far = [_det("beetle", "Mythic", (960 + 500, 540), conf=0.9)]  # 500px > 450 engage
    assert pick_mythic_target(far, center=(960, 540), latched=False) is None


def test_pick_mythic_target_uses_release_radius_when_latched():
    d = [_det("beetle", "Mythic", (960 + 500, 540), conf=0.9)]     # 500px
    assert pick_mythic_target(d, center=(960, 540), latched=False) is None       # >450
    got = pick_mythic_target(d, center=(960, 540), latched=True)                 # <600
    assert got is not None and got["species"] == "beetle"


def test_pick_mythic_target_prefers_higher_rank():
    dets = [
        _det("cactus", "Mythic", (1000, 540), conf=0.9),   # rank 1, closer
        _det("beetle", "Mythic", (1100, 540), conf=0.9),   # rank 5, farther
    ]
    got = pick_mythic_target(dets, center=(960, 540))
    assert got["species"] == "beetle"


def test_pick_mythic_target_nearest_breaks_a_rank_tie():
    dets = [
        _det("beetle", "Mythic", (960 + 300, 540), conf=0.9),  # 300px
        _det("beetle", "Mythic", (960 + 120, 540), conf=0.9),  # 120px — nearer
    ]
    got = pick_mythic_target(dets, center=(960, 540))
    assert got["screen_pos"] == (960 + 120, 540)


def test_pick_mythic_target_latched_holds_continuity_across_jitter():
    """两只同 rank 甲虫分居中心两侧, 已锁定 + prev_pos 贴右边那只 —— 每 tick 都该
    锁右边那只, 就算两只都有 ±2px 抖动也不翻 180° (纯 -dist_to_center tiebreak
    会因亚像素抖动来回跳)."""
    prev = (960 + 150, 540)
    for dl, dr in [(0, 0), (2, -2), (-2, 2), (2, 2), (-2, -2)]:
        left = _det("beetle", "Mythic", (960 - 150 + dl, 540), conf=0.9)
        right = _det("beetle", "Mythic", (960 + 150 + dr, 540), conf=0.9)
        got = pick_mythic_target([left, right], center=(960, 540),
                                 latched=True, prev_pos=prev)
        assert got["screen_pos"][0] > 960          # 始终是右边那只


def test_pick_mythic_target_latched_switches_only_for_strictly_higher_rank():
    """已锁定 + prev_pos 贴着一只仙人掌, 但范围内还有一只甲虫 (rank 更高) ——
    真来了更值得打的目标, 切过去."""
    cactus = _det("cactus", "Mythic", (960 + 100, 540), conf=0.9)   # 贴 prev_pos
    beetle = _det("beetle", "Mythic", (960 - 220, 540), conf=0.9)   # rank 更高, 离 prev 远
    prev = (960 + 100, 540)
    got = pick_mythic_target([cactus, beetle], center=(960, 540),
                             latched=True, prev_pos=prev)
    assert got["species"] == "beetle"


def test_pick_mythic_target_not_latched_ignores_prev_pos():
    """没锁定时 prev_pos 不参与 —— 仍是 rank 优先 + 离中心近 tiebreak."""
    dets = [
        _det("cactus", "Mythic", (1000, 540), conf=0.9),   # rank 1, 更近, 贴 prev
        _det("beetle", "Mythic", (1150, 540), conf=0.9),   # rank 5, 更远
    ]
    got = pick_mythic_target(dets, center=(960, 540), latched=False,
                             prev_pos=(1000, 540))
    assert got["species"] == "beetle"


def test_select_action_flees_when_avoid_mob_in_range():
    detections = [
        _det("scorpion", "Ultra", (1100, 540)),   # 160px from center, 在触发半径内
        _det("sandstorm", "Common", (960, 700)),  # 优先级再高也不该盖过flee
    ]
    action, payload = select_action(detections, avoid_trigger_px=400, center=(960, 540))
    assert action == "flee"
    assert (1100, 540) in payload


def test_select_action_ignores_avoid_mob_outside_trigger_radius():
    detections = [
        _det("scorpion", "Ultra", (2000, 540)),      # 1040px, 远超触发半径
        _det("sandstorm", "Mythic", (1000, 560)),    # Mythic 才够格当追击目标
    ]
    action, target, hold_px, repel = select_action(detections, avoid_trigger_px=400, center=(960, 540))
    assert action == "chase"
    assert target["species"] == "sandstorm"
    assert hold_px is None
    # AVOID怪没触发flee, 但还是进repel列表让追击路径绕开它
    assert (2000, 540) in repel


def test_select_action_chases_best_priority_candidate():
    detections = [
        _det("scorpion", "Common", (1000, 540)),          # Common: 不到 Mythic 档, 不追
        _det("sand_centipede", "Mythic", (1010, 540)),    # Mythic: 唯一够格的目标
    ]
    action, target, hold_px, repel = select_action(detections, center=(960, 540))
    assert action == "chase"
    assert target["species"] == "sand_centipede"
    assert repel == []   # 没有AVOID/别的CAUTIOUS, 没什么要绕的


def test_select_action_wanders_when_best_candidate_below_mythic():
    # 密集刷怪区的实况: 一堆 Common/传奇沙尘暴, 一个 Mythic 都没有 -> 交回 wander,
    # 别对着乱跳的沙尘暴原地打转 (旧行为 move_count=0 的根因).
    detections = [
        _det("sandstorm", "Common", (1000, 540), conf=0.95),
        _det("sandstorm", "Legendary", (900, 600), conf=0.95),
        _det("beetle", "Epic", (1100, 500), conf=0.95),
    ]
    action, payload = select_action(detections, center=(960, 540))
    assert action == "wander" and payload is None


def test_select_action_holds_distance_for_cautious_target():
    detections = [_det("cactus", "Ultra", (1000, 540))]
    action, target, hold_px, repel = select_action(detections, cautious_hold_px=250, center=(960, 540))
    assert action == "chase"
    assert hold_px == 250
    assert repel == []   # 目标本身是CAUTIOUS, 不该把自己放进repel


def test_select_action_chase_repels_around_other_danger_mobs():
    detections = [
        _det("sandstorm", "Ultra", (1100, 540)),   # CAUTIOUS, 物种优先级最高 -> 目标
        _det("cactus", "Ultra", (900, 400)),        # CAUTIOUS, 不是目标 -> 要绕开
        _det("scorpion", "Ultra", (300, 540)),      # AVOID, 660px>400 不触发flee -> 也要绕开
    ]
    action, target, hold_px, repel = select_action(detections, avoid_trigger_px=400, center=(960, 540))
    assert action == "chase"
    assert target["species"] == "sandstorm"
    assert hold_px == 250                           # 目标是 CAUTIOUS
    assert (900, 400) in repel and (300, 540) in repel
    assert (1100, 540) not in repel                 # 目标本身不进 repel


def test_select_action_wanders_with_no_relevant_detections():
    action, payload = select_action([])
    assert action == "wander"
    assert payload is None


def test_select_action_skips_low_confidence_chase_target():
    # 唯一的候选是个 0.45 的幻影框 -> 不追, 回漫游
    action, payload = select_action([_det("sandstorm", "Common", (1100, 540), conf=0.45)],
                                    chase_min_conf=0.55, center=(960, 540))
    assert action == "wander" and payload is None


def test_select_action_prefers_confident_target_over_higher_priority_ghost():
    detections = [
        _det("sand_centipede", "Mythic", (1200, 540), conf=0.45),  # 优先级更高但是幻影
        _det("scorpion", "Mythic", (1000, 540), conf=0.92),        # 优先级低但确实存在
    ]
    action, target, hold_px, repel = select_action(detections, chase_min_conf=0.55, center=(960, 540))
    assert action == "chase"
    assert target["species"] == "scorpion"


def test_select_action_low_conf_avoid_still_flees():
    # 危险怪不吃置信度关: 0.42 的 Ultra 蝎子进半径照样触发规避
    action, payload = select_action([_det("scorpion", "Ultra", (1100, 540), conf=0.42)],
                                    avoid_trigger_px=400, chase_min_conf=0.55, center=(960, 540))
    assert action == "flee"
    assert (1100, 540) in payload


def test_select_action_low_conf_cautious_repels_but_is_not_chased():
    detections = [
        _det("scorpion", "Mythic", (1000, 540), conf=0.9),      # 确实存在的 Mythic -> 目标
        _det("cactus", "Ultra", (900, 400), conf=0.4),          # 低置信 CAUTIOUS -> 只当危险源
    ]
    action, target, hold_px, repel = select_action(detections, chase_min_conf=0.55, center=(960, 540))
    assert action == "chase"
    assert target["species"] == "scorpion"
    assert (900, 400) in repel          # 仍要绕开
    assert hold_px is None              # 目标是 ENGAGE(Mythic), 不是那只低置信 CAUTIOUS


def test_select_action_flee_excludes_out_of_range_avoid_mobs():
    detections = [
        _det("scorpion", "Ultra", (1060, 540)),  # 100px, in range
        _det("beetle", "Ultra", (60, 540)),       # 900px, out of range — must not dilute the flee vector
    ]
    action, payload = select_action(detections, avoid_trigger_px=400, center=(960, 540))
    assert action == "flee"
    assert payload == [(1060, 540)]


def test_chase_is_stalled_false_until_window_full():
    # 样本还没攒满一个 window -> 不判 (返回 False)
    hist = [(0.0, 0.0)] * 10
    assert chase_is_stalled(hist, window=25) is False


def test_chase_is_stalled_true_when_net_displacement_below_threshold():
    # 攒满 window, 首尾净位移几乎为 0 (贴墙被顶住) -> 卡住
    hist = [(5.0 + 0.1 * (i % 2), 5.0) for i in range(25)]   # 只在 0.1 之间抖
    assert chase_is_stalled(hist, min_progress=4.0, window=25) is True


def test_chase_is_stalled_false_when_circling_but_making_progress():
    # 追一个走位的目标: 每 tick 挪一点点 (相邻差 < 1.5, 旧写法会误判卡住),
    # 但一个 window 下来净位移累积过阈值 -> 不算卡住
    hist = [(i * 0.5, 0.0) for i in range(25)]   # 24*0.5 = 12 净位移 > 4.0
    assert chase_is_stalled(hist, min_progress=4.0, window=25) is False


def test_chase_is_stalled_uses_last_window_of_a_longer_history():
    # history 比 window 长时只看最后 window 个样本
    hist = [(i * 5.0, 0.0) for i in range(20)]           # 早期大位移
    hist += [(95.0 + 0.1 * (i % 2), 0.0) for i in range(25)]  # 最近 25 tick 停住
    assert chase_is_stalled(hist, min_progress=4.0, window=25) is True


def test_chase_is_stalled_handles_none_and_empty():
    assert chase_is_stalled(None) is False
    assert chase_is_stalled([]) is False


from enemy_detect import scan_enemies, _species_from_name, _tier_from_color
import enemy_detect as _ed
from canvas_frame_fixtures import gameplay_frame, minimap_rec, nameplate, player_recs


def test_species_from_name_english_slugs(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    assert _species_from_name("Beetle") == "beetle"
    assert _species_from_name("Scorpion") == "scorpion"
    assert _species_from_name("Sand Centipede") == "sand_centipede"
    assert _species_from_name("Soldier Fire Ant") == "soldier_fire_ant"
    assert _species_from_name("Sandstorm") == "sandstorm"
    assert _species_from_name("Cactus") == "cactus"


def test_species_from_name_chinese_client_aliases(monkeypatch):
    # canvas 解出的名字随客户端语言; 中文客户端下英文名一个都不匹配,
    # 全靠 SPECIES_NAMES 里的中文名折回 slug (否则每个沙漠怪都被丢掉)。
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    assert _species_from_name("沙尘暴") == "sandstorm"
    assert _species_from_name("仙人掌") == "cactus"
    assert _species_from_name("甲虫") == "beetle"
    assert _species_from_name("蝎子") == "scorpion"
    assert _species_from_name("蜈蚣") == "sand_centipede"
    assert _species_from_name("火兵蚁") == "soldier_fire_ant"
    assert _species_from_name("火蚁") == "soldier_fire_ant"      # 工蚁, 同归 soldier_fire_ant
    # 每个登记了名字的 slug 必须是 SPECIES_RANK 认得的 —— 否则 priority_score KeyError
    for slug in _ed.SPECIES_NAMES:
        assert slug in _ed.SPECIES_RANK, slug


def test_species_from_name_rejects_non_desert_and_none(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    assert _species_from_name("Rock") is None         # 花园的怪, 不是沙漠的
    assert _species_from_name("Player #12") is None
    assert _species_from_name(None) is None
    assert _species_from_name("") is None


def test_species_from_name_ignores_the_fire_ant_hole_spawner_silently(capsys, monkeypatch):
    # 火蚁穴 (Fire Ant Hole) is a spawner structure, not a mob — recognised, returns None,
    # no log spam. 瓢虫 (Ladybug) is a rare high-value desert intruder and DOES map.
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    assert _species_from_name("火蚁穴") is None
    assert capsys.readouterr().out == ""
    assert _species_from_name("瓢虫") == "sandstorm"


def test_tier_from_color():
    assert _tier_from_color("#1FDBDE") == "Mythic"
    assert _tier_from_color("#7EEF6D") == "Common"
    assert _tier_from_color("#FF2B75") == "Ultra"
    assert _tier_from_color("#555555") == "Unique"
    assert _tier_from_color(None) == "Common"
    assert _tier_from_color("#abcdef") == "Common"


def _stub_canvas(monkeypatch, records):
    monkeypatch.setattr(_ed.cdp_bridge, "inject_canvas_hook", lambda *a, **k: None)
    monkeypatch.setattr(_ed.cdp_bridge, "drain_canvas_log", lambda *a, **k: list(records))
    _ed._frame_buffer[:] = []


def test_scan_enemies_maps_a_two_mob_frame(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    # frame 0 is complete (both mobs); frame 1 is newer but may still be drawing, so
    # scan_enemies decodes frame 0 and keeps frame 1 buffered for next time.
    f_old = (player_recs(0)
             + nameplate(0, 400.0, 200.0, "Beetle", rarity="Mythic", rarity_color="#1FDBDE")
             + nameplate(0, 720.0, 480.0, "Scorpion")            # fixture default: Common / #7EEF6D
             + [minimap_rec(0, 5640.0, 6911.0)])
    f_new = gameplay_frame(1)                                     # player + minimap only, newer
    _stub_canvas(monkeypatch, f_old + f_new)

    dets = {d["species"]: d for d in scan_enemies()}

    assert set(dets) == {"beetle", "scorpion"}
    beetle = dets["beetle"]
    assert beetle["rarity"] == "Mythic"
    assert beetle["screen_pos"] == (400.0, 200.0)
    assert beetle["bbox"] == (399.0, 199.0, 401.0, 201.0)
    assert beetle["confidence"] == 1.0
    assert dets["scorpion"]["rarity"] == "Common"


def test_scan_enemies_reports_world_positions_for_the_approach_tracker(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    f_old = (player_recs(0)
             + nameplate(0, 400.0, 200.0, "Beetle", rarity="Mythic", rarity_color="#1FDBDE")
             + [minimap_rec(0, 5640.0, 6911.0)])
    _stub_canvas(monkeypatch, f_old + gameplay_frame(1))
    (beetle,) = scan_enemies()
    cam = _ed.canvas_decode.camera_from_frame(f_old, best_effort=True)
    want = _ed.canvas_decode.mobs_from_frame(f_old, cam)[0]
    assert beetle["world"] == (want["x"], want["y"])
    assert _ed.last_player_world() == pytest.approx(cam["player_world"])


def test_scan_enemies_forgets_the_player_world_when_nothing_decodes(monkeypatch):
    monkeypatch.setattr(_ed, "_last_player_world", (1.0, 2.0))
    _stub_canvas(monkeypatch, [])
    assert scan_enemies() == [] and _ed.last_player_world() is None


def test_scan_enemies_drops_non_desert_names(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    # Rock 是花园的怪; (英文 Ladybug 现在会折成沙漠的 sandstorm, 见 _NAME_OVERRIDES, 不能再拿它当"非沙漠名")
    f_old = gameplay_frame(0, mobs=[(400.0, 200.0, "Rock", 1.0)])
    f_new = gameplay_frame(1)
    _stub_canvas(monkeypatch, f_old + f_new)
    assert scan_enemies() == []


def test_scan_enemies_empty_when_fewer_than_two_frames(monkeypatch):
    _stub_canvas(monkeypatch, gameplay_frame(0, mobs=[(400.0, 200.0, "Beetle", 1.0)]))
    assert scan_enemies() == []


def test_scan_enemies_empty_when_camera_undecodable(monkeypatch):
    # frames present, but the minimap player-dot (the only absolute-position anchor) is
    # stripped -> camera_from_frame raises ValueError -> scan_enemies degrades to [].
    f0 = [r for r in gameplay_frame(0, mobs=[(400.0, 200.0, "Beetle", 1.0)])
          if abs(r["m"][0]) > 0.05]
    f1 = [r for r in gameplay_frame(1) if abs(r["m"][0]) > 0.05]
    _stub_canvas(monkeypatch, f0 + f1)
    assert scan_enemies() == []


def test_scan_enemies_swallows_non_tuple_exception_types(monkeypatch):
    # the decode path now leans on cdp_bridge (websocket.WebSocketException),
    # file reads (OSError/FileNotFoundError) and division (ZeroDivisionError) —
    # none of which are ValueError/RuntimeError/KeyError/TypeError. scan_enemies'
    # contract is "undecodable -> []", so it must catch all of them.
    monkeypatch.setattr(_ed.cdp_bridge, "inject_canvas_hook", lambda *a, **k: None)

    def boom(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(_ed.cdp_bridge, "drain_canvas_log", boom)
    _ed._frame_buffer[:] = []
    assert scan_enemies() == []


def test_scan_enemies_frame_buffer_stays_bounded_when_frame_number_stuck(monkeypatch):
    # __canvasFrame stuck at 0 -> every drained record is frame 0 -> group_by_frame
    # yields one key -> scan_enemies hits the "< 2 frames" early return every tick and
    # never runs the by-frame prune below it. The _FRAME_BUFFER_CAP hard-cap is the
    # only thing keeping _frame_buffer from growing unbounded over a multi-hour run.
    stuck = [{"frame": 0, "op": "fill", "m": [0.7, 0, 0, 0.7, 1.0, 2.0]}
             for _ in range(8000)]
    monkeypatch.setattr(_ed.cdp_bridge, "inject_canvas_hook", lambda *a, **k: None)
    monkeypatch.setattr(_ed.cdp_bridge, "drain_canvas_log", lambda *a, **k: list(stuck))
    _ed._frame_buffer[:] = []
    for _ in range(12):                       # 12 * 8000 = 96k records drained total
        assert scan_enemies() == []
    assert len(_ed._frame_buffer) <= _ed._FRAME_BUFFER_CAP


def test_species_from_name_logs_unknown_name_once(capsys, monkeypatch):
    # recovers the diagnostic the deleted debug_enemy_detect.py used to provide: an
    # unrecognised mob name gets named in the log exactly once, so a slug mismatch
    # between YOLO's old class labels and florr's live English names is visible.
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    _ed._seen_unknown_names.discard("desert_weirdo")

    assert _species_from_name("Desert Weirdo") is None
    first = capsys.readouterr().out
    assert "Desert Weirdo" in first and "desert_weirdo" in first

    assert _species_from_name("Desert Weirdo") is None      # same name -> silent
    assert capsys.readouterr().out == ""

    assert _species_from_name("Beetle") == "beetle"         # known slug -> never logs
    assert capsys.readouterr().out == ""


def _mdet(species, screen_pos):
    return {"species": species, "rarity": "Mythic", "screen_pos": screen_pos,
            "bbox": (0, 0, 0, 0), "confidence": 0.9}


def test_mythic_move_ram_matches_aim_mouse_target():
    from enemy_detect import aim_mouse_target
    tgt = _mdet("scorpion", (1460, 540))
    got = mythic_move_target(tgt, center=(960, 540), strafe_radius=180,
                             cactus_hold_px=220, max_extend=500)
    assert got == aim_mouse_target((1460, 540), hold_px=None, center=(960, 540),
                                   max_extend=500)
    assert got == (1460, 540)


def test_mythic_move_hold_approaches_when_far():
    tgt = _mdet("cactus", (1360, 540))          # d = 400 > 220*1.15
    got = mythic_move_target(tgt, center=(960, 540), strafe_radius=180,
                             cactus_hold_px=220, max_extend=500)
    assert got == (1360, 540)                   # straight-in, dist within max_extend


def test_mythic_move_hold_backs_off_when_too_close():
    tgt = _mdet("cactus", (1110, 540))          # d = 150 < 220*0.85 = 187
    x, y = mythic_move_target(tgt, center=(960, 540), strafe_radius=180,
                              cactus_hold_px=220, max_extend=500)
    assert x < 960 and abs(y - 540) < 1e-6      # moved away along -u


def test_mythic_move_hold_orbits_in_the_band():
    tgt = _mdet("cactus", (1180, 540))          # d = 220, inside [187, 253]
    x, y = mythic_move_target(tgt, center=(960, 540), strafe_radius=180,
                              cactus_hold_px=220, max_extend=500)
    assert abs(x - 960) < 1e-6 and abs(abs(y - 540) - 500) < 1e-6   # pure perpendicular


def test_mythic_move_strafe_is_perpendicular_when_at_radius():
    tgt = _mdet("beetle", (1140, 540))          # d = 180 == strafe_radius
    x, y = mythic_move_target(tgt, center=(960, 540), strafe_radius=180,
                              cactus_hold_px=220, max_extend=500)
    assert abs(x - 960) < 1e-6 and abs(abs(y - 540) - 500) < 1e-6


def test_mythic_move_strafe_pulls_inward_when_far():
    tgt = _mdet("beetle", (1440, 540))          # d = 480 > radius -> inward (+u) component
    x, y = mythic_move_target(tgt, center=(960, 540), strafe_radius=180,
                              cactus_hold_px=220, max_extend=500)
    assert x > 960 and y > 540                  # perp (down) + inward (toward mob, right)


def test_mythic_move_strafe_pushes_outward_when_too_close():
    tgt = _mdet("soldier_fire_ant", (1040, 540))  # d = 80 < radius -> outward (-u)
    x, y = mythic_move_target(tgt, center=(960, 540), strafe_radius=180,
                              cactus_hold_px=220, max_extend=500)
    assert x < 960 and y > 540


def test_mythic_move_zero_distance_returns_center():
    tgt = _mdet("beetle", (960, 540))
    assert mythic_move_target(tgt, center=(960, 540), strafe_radius=180,
                              cactus_hold_px=220, max_extend=500) == (960, 540)


def test_map_species_desert_set_is_unchanged():
    # 沙漠的 6 个 slug 一个不能少/不能改 —— 改了 priority_score 会 KeyError.
    assert _ed.MAP_SPECIES["desert"] == frozenset(
        {"scorpion", "beetle", "cactus", "sandstorm",
         "sand_centipede", "soldier_fire_ant"})


def test_every_mapped_species_has_a_rank():
    # MAP_SPECIES 的值必须全部落在 SPECIES_RANK 里, 否则 priority_score KeyError.
    for map_name, species in _ed.MAP_SPECIES.items():
        for slug in species:
            assert slug in _ed.SPECIES_RANK, f"{map_name}: {slug} 没有 SPECIES_RANK"


_PLAYABLE_MAPS = ("garden", "desert", "ocean", "jungle", "anthell", "sewers", "factory")


def test_species_supported_for_every_playable_map():
    for name in _PLAYABLE_MAPS:
        assert _ed.species_supported(name) is True, name
    assert _ed.species_supported("nope") is False
    assert _ed.species_supported(None) is False


def test_a_map_with_an_empty_species_table_has_enemy_ai_off(monkeypatch):
    # 现在没有真实地图是空表了; "空表 = 没做索敌"这条机制靠合成一张空表图来测。
    monkeypatch.setitem(_ed.MAP_SPECIES, "ocean", frozenset())
    assert _ed.species_supported("ocean") is False


def test_species_from_name_still_works_on_desert():
    assert _ed._species_from_name("Scorpion", map_name="desert") == "scorpion"
    assert _ed._species_from_name("沙尘暴", map_name="desert") == "sandstorm"
    assert _ed._species_from_name("Sand Centipede", map_name="desert") == "sand_centipede"


def test_species_from_name_returns_none_on_unsupported_map(monkeypatch):
    # 没做索敌的图(合成一张空表): 什么怪都不认.
    monkeypatch.setitem(_ed.MAP_SPECIES, "garden", frozenset())
    assert _ed._species_from_name("Scorpion", map_name="garden") is None
    assert _ed._species_from_name("Worker Ant", map_name="garden") is None


def test_species_from_name_is_silent_on_unsupported_map(capsys, monkeypatch):
    # 关键: 空表图上绝不能走"未识别怪物名"那条日志 —— 一屏几十只怪,
    # 会把日志刷爆.
    monkeypatch.setitem(_ed.MAP_SPECIES, "garden", frozenset())
    _ed._seen_unknown_names.clear()
    for _ in range(3):
        _ed._species_from_name("Worker Ant", map_name="garden")
    assert capsys.readouterr().out == ""
    assert _ed._seen_unknown_names == set()


def test_species_from_name_defaults_to_utils_MAP(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    assert _ed._species_from_name("Cactus") == "cactus"
    monkeypatch.setattr(_ed.utils, "MAP", "garden")
    assert _ed._species_from_name("Cactus") is None


def test_alias_not_in_this_maps_species_is_rejected(monkeypatch):
    # 名字只在**本图认得的物种**里查, 否则等于把沙漠怪混进别的图. 花园是空集合时,
    # `if not known` 那层 guard 会先一步拦下, 根本轮不到按名字查找那一步 —— 那一步
    # 才是"折出来的 slug 必须属于当前图"真正的守门人. 所以额外借用
    # "garden" 造一张非空、但不含 cactus 的物种表, 才能真正跑到那步去测.
    monkeypatch.setitem(_ed.MAP_SPECIES, "garden", frozenset())
    assert _ed._species_from_name("仙人掌", map_name="garden") is None   # 空表: guard 先拦下

    monkeypatch.setitem(_ed.MAP_SPECIES, "garden", frozenset({"scorpion"}))
    assert _ed._species_from_name("仙人掌", map_name="garden") is None   # 非空但没 cactus: 别名判断本身拦下

    # 同一个别名在沙漠(cactus 就在沙漠的物种表里)必须照常命中 —— 不然就是
    # 把别名解析本身也测坏了.
    assert _ed._species_from_name("仙人掌", map_name="desert") == "cactus"


# ── 稀有度认不出颜色时按名牌上的词兜底 (2026-09-22) ──────────────────────────

def test_tier_falls_back_to_the_nameplate_word_when_colour_is_unknown():
    """颜色表是硬编码的, florr 换一版色就全线退成 Common —— 那会让神话/究极的
    遛怪和锁定逻辑一次都不触发, 表现就是"神话/究极怪识别不到"。名牌上那个词是同一帧
    里的第二个独立信源, 拿来兜底。

    实测确认过的中文词: 普通/罕见/史诗/传奇/神话 (canvas_combat_test.ndjson +
    mythic.json); 究极 由用户确认。
    """
    from enemy_detect import _tier_from_color
    assert _tier_from_color("#DEADBE", "神话") == "Mythic"
    assert _tier_from_color("#DEADBE", "究极") == "Ultra"
    assert _tier_from_color("#DEADBE", "传奇") == "Legendary"
    assert _tier_from_color(None, "Ultra") == "Ultra"          # 英文客户端


def test_colour_still_wins_over_the_word():
    """颜色是实测过的主信源, 词只是兜底 —— 两者冲突时以颜色为准."""
    from enemy_detect import _tier_from_color
    assert _tier_from_color("#1FDBDE", "普通") == "Mythic"


def test_unknown_colour_and_unknown_word_is_still_common():
    from enemy_detect import _tier_from_color
    assert _tier_from_color("#DEADBE", "???") == "Common"
    assert _tier_from_color(None, None) == "Common"


def test_scan_enemies_passes_the_word_through(monkeypatch):
    """端到端: scan_enemies 必须把名牌上的词一起交给稀有度判定, 否则上面那层兜底
    永远用不上."""
    import canvas_decode, cdp_bridge, enemy_detect, utils
    utils.apply_map("desert")
    monkeypatch.setattr(cdp_bridge, "inject_canvas_hook", lambda *a, **k: None)
    monkeypatch.setattr(cdp_bridge, "drain_canvas_log", lambda *a, **k: [{"frame": 0}, {"frame": 1}])
    monkeypatch.setattr(canvas_decode, "group_by_frame",
                        lambda recs: {0: ["f0"], 1: ["f1"]})
    monkeypatch.setattr(canvas_decode, "camera_from_frame", lambda *a, **k: {"zoom": 1.0})
    monkeypatch.setattr(canvas_decode, "mobs_from_frame", lambda *a, **k: [
        {"name": "沙尘暴", "rarity": "究极", "rarity_color": "#DEADBE",
         "hp": 1.0, "sx": 10.0, "sy": 20.0, "x": 0.0, "y": 0.0}])
    enemy_detect._frame_buffer[:] = []
    dets = enemy_detect.scan_enemies()
    assert [d["rarity"] for d in dets] == ["Ultra"]


# ── 距离判定用解码出的真实玩家锚点, 不用屏幕中心 (2026-09-22) ─────────────────

def _feed_frame(monkeypatch, records, camera):
    """把一帧喂给 scan_enemies, 绕开 CDP。scan_enemies 取的是"次新"那一帧,
    所以要再垫一条更新的记录。"""
    import canvas_decode, cdp_bridge, enemy_detect
    monkeypatch.setattr(cdp_bridge, "inject_canvas_hook", lambda *a, **k: None)
    monkeypatch.setattr(cdp_bridge, "drain_canvas_log",
                        lambda *a, **k: [{"frame": 0}, {"frame": 1}])
    monkeypatch.setattr(canvas_decode, "group_by_frame",
                        lambda recs: {0: records, 1: [{"frame": 1}]})
    monkeypatch.setattr(canvas_decode, "camera_from_frame", lambda *a, **k: camera)
    monkeypatch.setattr(canvas_decode, "mobs_from_frame", lambda *a, **k: [])
    enemy_detect._frame_buffer[:] = []


def test_current_center_defaults_to_screen_centre_before_any_scan():
    import enemy_detect
    enemy_detect._last_center = None
    assert enemy_detect.current_center() == enemy_detect.SCREEN_CENTER


def test_current_center_uses_the_decoded_player_anchor(monkeypatch):
    """florr 在地图边界会顶住相机不再跟随, 玩家就不在画布中心了; 非全屏调试时画布
    本身也不等于屏幕。实测三份转储的画布都是 1920x945, 玩家 y=472.5 —— 拿
    SCREEN_CENTER(960,540) 当玩家位置, 垂直方向恒定差 67.5px。"""
    import enemy_detect
    _feed_frame(monkeypatch, [], {"zoom": 1.0, "player_screen": (798.0, 472.5)})
    enemy_detect.scan_enemies()
    assert enemy_detect.current_center() == (798.0, 472.5)


def test_current_center_falls_back_when_the_camera_is_approximate(monkeypatch):
    """approx 锚点是 best_effort 的兜底, 实测会落在别的实体身上 —— 不能拿它当玩家位置."""
    import enemy_detect
    _feed_frame(monkeypatch, [], {"zoom": 1.0, "player_screen": (10.0, 20.0), "approx": True})
    enemy_detect.scan_enemies()
    assert enemy_detect.current_center() == enemy_detect.SCREEN_CENTER


def test_current_center_is_reset_when_a_scan_fails(monkeypatch):
    """扫描抛错时不能留着上一帧的锚点接着用 —— 那是"过期位置"。"""
    import canvas_decode, cdp_bridge, enemy_detect
    _feed_frame(monkeypatch, [], {"zoom": 1.0, "player_screen": (798.0, 472.5)})
    enemy_detect.scan_enemies()
    assert enemy_detect.current_center() == (798.0, 472.5)

    def _boom(*a, **k):
        raise RuntimeError("CDP 掉了")
    monkeypatch.setattr(cdp_bridge, "drain_canvas_log", _boom)
    enemy_detect._frame_buffer[:] = []
    assert enemy_detect.scan_enemies() == []
    assert enemy_detect.current_center() == enemy_detect.SCREEN_CENTER


# ── 蚁穴 (2026-09-27 实机抓帧) ───────────────────────────────────────────────

def test_anthell_species_table_and_aliases():
    assert _ed.MAP_SPECIES["anthell"] == frozenset(
        {"baby_ant", "worker_ant", "soldier_ant", "worm", "queen_ant", "ant_egg"})
    for name, slug in (("幼蚁", "baby_ant"), ("工蚁", "worker_ant"),
                       ("兵蚁", "soldier_ant"), ("蠕虫", "worm"), ("蚁后", "queen_ant"),
                       ("蚁卵", "ant_egg")):
        assert _ed._species_from_name(name, map_name="anthell") == slug


def test_anthell_ant_names_do_not_leak_into_desert():
    # 蚁穴的兵蚁不是沙漠的火兵蚁 —— 沙漠那张表里没有它们, 不能被认成沙漠怪.
    assert _ed._species_from_name("兵蚁", map_name="desert") is None
    assert _ed._species_from_name("火兵蚁", map_name="anthell") is None


def test_anthell_ultra_and_above_are_avoid():
    for species in ("baby_ant", "worker_ant", "soldier_ant", "worm", "queen_ant", "ant_egg"):
        for rarity in ("Ultra", "Super", "Eternal", "Unique"):
            assert classify_action(species, rarity) == "AVOID"
        for rarity in _BELOW_ULTRA:
            assert classify_action(species, rarity) == "ENGAGE"


def test_target_policy_for_map():
    assert _ed.target_policy_for("desert") == "priority"            # 沙漠: 只追神话+
    for name in ("anthell", "garden", "ocean", "jungle", "sewers", "factory"):
        assert _ed.target_policy_for(name) == "nearest", name
    assert _ed.target_policy_for("") == "priority"
    assert _ed.target_policy_for(None) == "priority"


def _nearest(detections, **kw):
    kw.setdefault("center", (960, 540))
    return select_action(detections, target_policy="nearest", **kw)


def test_nearest_policy_picks_closest_regardless_of_rarity():
    # 神话兵蚁 300px, 传奇幼蚁 100px, 普通工蚁 200px —— 最近的传奇幼蚁先打.
    detections = [
        _det("soldier_ant", "Mythic", (1260, 540)),
        _det("baby_ant", "Legendary", (1060, 540)),
        _det("worker_ant", "Common", (760, 540)),
    ]
    action, target, hold_px, repel = _nearest(detections)
    assert action == "chase"
    assert target["species"] == "baby_ant"
    assert repel == []


def test_nearest_policy_chases_even_common_mobs():
    # 优先级模式下 Common 交回 wander; 最近优先模式下有怪就追.
    action, target, _, _ = _nearest([_det("worker_ant", "Common", (1200, 540))])
    assert action == "chase" and target["species"] == "worker_ant"


def test_nearest_policy_holds_when_target_is_close():
    action, target, hold_px, _ = _nearest([_det("soldier_ant", "Mythic", (1000, 540))])
    assert action == "chase"
    assert hold_px == _ed.ENGAGE_HOLD_PX


def test_nearest_policy_ultra_ant_is_never_the_target():
    # 究极兵蚁最近(50px), 神话工蚁 500px 外 —— 究极的不能当目标, 而且进了 400px 要先躲.
    detections = [
        _det("soldier_ant", "Ultra", (1010, 540)),
        _det("worker_ant", "Mythic", (1460, 540)),
    ]
    action, avoid_positions = _nearest(detections, avoid_trigger_px=400)
    assert action == "flee"
    assert avoid_positions == [(1010, 540)]


def test_nearest_policy_far_ultra_ant_is_repelled_not_targeted():
    detections = [
        _det("soldier_ant", "Ultra", (1660, 540)),      # 700px, 不触发 flee
        _det("worker_ant", "Mythic", (1160, 540)),
    ]
    action, target, _, repel = _nearest(detections, avoid_trigger_px=400)
    assert action == "chase"
    assert target["species"] == "worker_ant"
    assert (1660, 540) in repel


def test_nearest_policy_wanders_with_nothing_to_chase():
    action, payload = _nearest([])
    assert action == "wander" and payload is None
    action, payload = _nearest([_det("soldier_ant", "Ultra", (1900, 540))],
                               avoid_trigger_px=400)
    assert action == "wander" and payload is None


def test_nearest_policy_ignores_low_confidence_targets():
    action, payload = _nearest([_det("worker_ant", "Mythic", (1000, 540), conf=0.2)],
                               chase_min_conf=0.55)
    assert action == "wander" and payload is None


def test_priority_policy_is_still_the_default():
    # 沙漠行为不能变: 默认参数下 Common 交回 wander.
    action, payload = select_action([_det("sandstorm", "Common", (1000, 540))],
                                    center=(960, 540))
    assert action == "wander" and payload is None


def test_nearest_policy_targets_ant_eggs_like_any_other_mob():
    # 蚁卵也算怪物(用户 2026-09-27): 最近的是卵就打卵.
    detections = [
        _det("ant_egg", "Common", (1000, 540)),
        _det("soldier_ant", "Mythic", (1200, 540)),
    ]
    action, target, _, _ = _nearest(detections)
    assert action == "chase" and target["species"] == "ant_egg"


def test_nearest_policy_ignores_targets_beyond_chase_radius():
    # 只有一堆远处的怪(隔着墙/出了刷怪带) -> 不追, 交回漫游. 不能退回按稀有度那套去追远处的神话.
    far = _ed.CHASE_MAX_PX + 50
    action, payload = _nearest([_det("soldier_ant", "Mythic", (960 + far, 540))])
    assert action == "wander" and payload is None


def test_nearest_policy_chase_radius_is_inclusive_and_picks_nearest_inside():
    inside = _ed.CHASE_MAX_PX
    detections = [
        _det("worker_ant", "Common", (960 + inside, 540)),          # 刚好在半径上
        _det("soldier_ant", "Mythic", (960 + inside + 200, 540)),   # 半径外
    ]
    action, target, _, _ = _nearest(detections)
    assert action == "chase" and target["species"] == "worker_ant"


def test_nearest_policy_far_ultra_still_repels_while_chasing_near_target():
    detections = [
        _det("worker_ant", "Mythic", (1060, 540)),
        _det("soldier_ant", "Ultra", (960 - 700, 540)),   # 半径和 flee 触发都之外, 但要绕开
    ]
    action, target, _, repel = _nearest(detections, avoid_trigger_px=400)
    assert action == "chase" and (260, 540) in repel


# ── 蚁群 (用户 2026-09-27): 一堆蚂蚁挤在一起 -> 先打蚁群, 保持距离、近了就退 ──────────

def _swarm_at(cx, cy, n, species="soldier_ant", rarity="Mythic", spread=40):
    """n 只蚂蚁围着 (cx, cy) 摆一圈, 两两距离都在蚁群半径内."""
    return [_det(species, rarity, (cx + spread * math.cos(2 * math.pi * i / n),
                                   cy + spread * math.sin(2 * math.pi * i / n)))
            for i in range(n)]


def test_swarm_is_preferred_over_a_closer_single_ant():
    members = _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT)
    detections = members + [
        _det("worker_ant", "Common", (1010, 540)),          # 单只, 更近
    ]
    action, swarm, repel = _nearest(detections)
    assert action == "swarm"
    assert swarm["count"] == _ed.SWARM_MIN_COUNT
    assert math.hypot(swarm["center"][0] - 1300, swarm["center"][1] - 540) < 1
    # nearest 是蚁群里离玩家最近的那只, 不是那只单独的工蚁
    want = min((d["screen_pos"] for d in members),
               key=lambda p: math.hypot(p[0] - 960, p[1] - 540))
    assert swarm["nearest"] == want
    assert repel == []


def test_too_few_ants_is_not_a_swarm():
    detections = _swarm_at(1200, 540, _ed.SWARM_MIN_COUNT - 1)
    action, target, _, _ = _nearest(detections)
    assert action == "chase"


def test_ultra_ants_do_not_count_toward_a_swarm():
    # 4 只神话 + 1 只究极挤一起: 究极是 AVOID, 不算进蚁群 -> 凑不够数.
    dets = _swarm_at(1400, 540, _ed.SWARM_MIN_COUNT)
    dets[0] = dict(dets[0], rarity="Ultra")
    action = _nearest(dets, avoid_trigger_px=200)[0]
    assert action != "swarm"


def test_flee_beats_the_swarm():
    detections = _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT) + [
        _det("soldier_ant", "Ultra", (1060, 540)),          # 100px, 进了 200 的规避半径
    ]
    action, payload = _nearest(detections, avoid_trigger_px=200)
    assert action == "flee" and payload == [(1060, 540)]


def test_far_swarm_is_ignored():
    far = _ed.SWARM_CHASE_MAX_PX + 100
    detections = _swarm_at(960 + far, 540, _ed.SWARM_MIN_COUNT) + [
        _det("worker_ant", "Common", (1060, 540)),
    ]
    action, target, _, _ = _nearest(detections)
    assert action == "chase" and target["species"] == "worker_ant"


def test_larger_swarm_wins():
    detections = (_swarm_at(1200, 300, _ed.SWARM_MIN_COUNT)
                  + _swarm_at(700, 800, _ed.SWARM_MIN_COUNT + 3))
    action, swarm, _ = _nearest(detections)
    assert action == "swarm" and swarm["count"] == _ed.SWARM_MIN_COUNT + 3


def test_desert_priority_policy_never_returns_swarm():
    detections = _swarm_at(1200, 540, 8, species="sandstorm", rarity="Mythic")
    assert select_action(detections, center=(960, 540))[0] == "chase"


def test_swarm_move_approaches_when_far():
    swarm = {"center": (1400, 540), "nearest": (1360, 540), "count": 5}
    mx, my = _ed.swarm_move_target(swarm, center=(960, 540), keep_px=80, max_extend=500)
    assert mx > 960 and abs(my - 540) < 1e-6


def test_swarm_move_holds_inside_the_band():
    swarm = {"center": (1080, 540), "nearest": (1040, 540), "count": 5}   # 最近那只 80px
    assert _ed.swarm_move_target(swarm, center=(960, 540), keep_px=80,
                                 max_extend=500) == (960, 540)


def test_swarm_move_backs_off_when_too_close():
    swarm = {"center": (1040, 540), "nearest": (1000, 540), "count": 5}   # 最近那只 40px
    mx, my = _ed.swarm_move_target(swarm, center=(960, 540), keep_px=80, max_extend=500)
    assert mx < 960 and abs(my - 540) < 1e-6        # 往远离蚁群中心的方向退


def test_swarm_move_backs_off_even_when_standing_in_the_middle():
    # 人就站在蚁群中心上: 离中心的方向没有定义, 退回"离最近那只远一点".
    swarm = {"center": (960, 540), "nearest": (1000, 540), "count": 5}
    mx, my = _ed.swarm_move_target(swarm, center=(960, 540), keep_px=80, max_extend=500)
    assert mx < 960


def test_avoid_trigger_is_per_map():
    assert _ed.avoid_trigger_px_for("anthell", 400) == 200
    assert _ed.avoid_trigger_px_for("desert", 400) == 400
    assert _ed.avoid_trigger_px_for(None, 400) == 400


# ── 蚁穴按物种打法 (florr 维基资料, 2026-09-27) ──────────────────────────────
# 主动怪(兵蚁/蚁后)会自己凑上来, 停在远处等它就行; 被动/中立/不动的(幼蚁/工蚁/蚁卵)
# 不会 —— 停在花瓣够不着的地方等它们 = 永远打不到。蠕虫体伤是同档兵蚁的 3 倍, 维基:
# 「打蠕虫永远别停下」。蚁后判定圈比外观大。

def test_passive_and_static_ants_are_closed_in_on_within_petal_reach():
    # 花瓣够得着的大约 SWARM_KEEP_PX(实机帧里掉血的兵蚁 36~82px) —— 停下的距离必须在这以内.
    for species in ("baby_ant", "worker_ant", "ant_egg"):
        action, target, hold_px, _ = _nearest([_det(species, "Mythic", (1200, 540))])
        assert action == "chase" and target["species"] == species
        assert hold_px < _ed.SWARM_KEEP_PX, species


def test_soldier_ant_still_holds_at_the_engage_radius():
    _, _, hold_px, _ = _nearest([_det("soldier_ant", "Mythic", (1200, 540))])
    assert hold_px == _ed.ENGAGE_HOLD_PX


def test_queen_ant_is_held_off_further_than_a_soldier():
    # 判定圈比外观大: 贴到兵蚁那个距离就已经在挨她的体伤了.
    _, _, hold_px, _ = _nearest([_det("queen_ant", "Mythic", (1250, 540))])
    assert hold_px > _ed.ENGAGE_HOLD_PX


def test_worm_is_circled_at_petal_reach():
    _, target, hold_px, _ = _nearest([_det("worm", "Mythic", (1200, 540))])
    assert target["species"] == "worm"
    assert hold_px == _ed.ANTHELL_WORM_STRAFE_PX
    assert _ed.ANTHELL_WORM_STRAFE_PX <= _ed.SWARM_KEEP_PX


def test_passive_ant_is_preferred_over_a_slightly_nearer_soldier():
    # 同档幼蚁血量是兵蚁 1/4、经验更高: 都还没接上战时, 幼蚁远一点也先打它.
    detections = [
        _det("soldier_ant", "Mythic", (960 + 150, 540)),
        _det("baby_ant", "Mythic", (960, 540 - 200)),
    ]
    _, target, _, _ = _nearest(detections)
    assert target["species"] == "baby_ant"


def test_a_fight_in_progress_is_not_abandoned_for_a_juicier_target():
    # 兵蚁已经进了原地开打半径 —— 正在打, 别撇下它跑去追幼蚁.
    detections = [
        _det("soldier_ant", "Mythic", (960 + 100, 540)),
        _det("baby_ant", "Mythic", (960, 540 - 150)),
    ]
    _, target, hold_px, _ = _nearest(detections)
    assert target["species"] == "soldier_ant"
    assert hold_px == _ed.ENGAGE_HOLD_PX


def test_queen_ant_is_left_for_last_when_a_soldier_is_about_as_close():
    # 蚁后血量是兵蚁 2.5 倍、经验反而更少, 还一直下蛋孵兵蚁 —— 近处有兵蚁先打兵蚁.
    detections = [
        _det("queen_ant", "Mythic", (960 + 200, 540)),      # 还在她的停步半径外
        _det("soldier_ant", "Mythic", (960, 540 + 240)),
    ]
    _, target, _, _ = _nearest(detections)
    assert target["species"] == "soldier_ant"


def test_nearby_worm_beats_the_swarm():
    # 蠕虫就在旁边时站着遛蚁群 = 等它从脚下钻出来; 先边走边打蠕虫.
    detections = _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT) + [
        _det("worm", "Mythic", (960 - 150, 540)),
    ]
    action, target, hold_px, _ = _nearest(detections)
    assert action == "chase" and target["species"] == "worm"


def test_far_worm_does_not_pull_the_bot_off_the_swarm():
    far = _ed.ANTHELL_WORM_PRIORITY_PX + 50
    detections = _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT) + [
        _det("worm", "Mythic", (960 - far, 540)),
    ]
    assert _nearest(detections)[0] == "swarm"


def test_worms_do_not_count_toward_a_swarm():
    # 蚁群是一堆蚂蚁; 蠕虫不是蚂蚁, 也不能站着跟它耗.
    dets = _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT)
    dets[0] = dict(dets[0], species="worm")
    assert _nearest(dets)[0] != "swarm"


def test_chase_move_target_holds_still_on_an_ant_inside_the_hold_radius():
    tgt = _det("soldier_ant", "Mythic", (1000, 540))
    assert _ed.chase_move_target(tgt, 120, center=(960, 540), max_extend=500) == (960, 540)


def test_chase_move_target_matches_aim_for_ants():
    tgt = _det("baby_ant", "Mythic", (1300, 700))
    want = _ed.aim_mouse_target((1300, 700), hold_px=50, center=(960, 540), max_extend=500,
                                repel_positions=[(900, 400)])
    assert _ed.chase_move_target(tgt, 50, center=(960, 540), max_extend=500,
                                 repel_positions=[(900, 400)]) == want


def test_chase_move_target_never_stands_still_next_to_a_worm():
    # 在绕圈半径上、半径内、半径外, 都得在动.
    for dist in (40, 80, 200):
        tgt = _det("worm", "Mythic", (960 + dist, 540))
        got = _ed.chase_move_target(tgt, 80, center=(960, 540), max_extend=500)
        assert got != (960, 540), dist
        assert math.hypot(got[0] - 960, got[1] - 540) == pytest.approx(500)


def test_chase_move_target_circles_a_worm_on_the_radius():
    # 正好在半径上: 纯切向, 不往里也不往外.
    tgt = _det("worm", "Mythic", (1040, 540))
    x, y = _ed.chase_move_target(tgt, 80, center=(960, 540), max_extend=500)
    assert x == pytest.approx(960) and abs(y - 540) == pytest.approx(500)


def test_chase_move_target_circles_a_worm_on_top_of_you_somewhere():
    # 蠕虫正好在脚下(距离 0, 方向没定义): 也不能停.
    tgt = _det("worm", "Mythic", (960, 540))
    assert _ed.chase_move_target(tgt, 80, center=(960, 540), max_extend=500) != (960, 540)


# ── 究极冲过来 400px 就躲 (用户 2026-09-28) ─────────────────────────────────
# 录像: 究极兵蚁追人 ≈ 自己的速度(~300 世界单位/秒), 200px 才起跑甩不掉。用户定:
# 400px 内且正在朝你冲过来就躲; 站着 / 闲逛的不管; 200px 内一律躲。

def _ultra(world, screen=(1300, 540), species="soldier_ant"):
    return dict(_det(species, "Ultra", screen), world=world)


def test_tracker_flags_an_ultra_closing_in():
    tr = _ed.ApproachTracker()
    me = (0.0, 0.0)
    marks = []
    for i, x in enumerate([900.0, 830.0, 760.0, 690.0]):          # 每 0.12s 近 70 = 580/s
        d = _ultra((x, 0.0))
        tr.update([d], me, 10.0 + 0.12 * i)
        marks.append(d["approaching"])
    assert marks == [False, False, False, True]     # 攒够 APPROACH_WINDOW_S 才下结论


def test_tracker_ignores_an_idle_or_retreating_ultra():
    tr = _ed.ApproachTracker()
    for i in range(6):
        idle = _ultra((800.0 + (i % 2) * 5, 0.0))                    # 原地抖
        away = _ultra((0.0, 600.0 + 60 * i), screen=(960, 900))      # 越走越远
        tr.update([idle, away], (0.0, 0.0), 10.0 + 0.12 * i)
    assert idle["approaching"] is False and away["approaching"] is False


def test_tracker_counts_us_walking_into_it_as_closing():
    # 相对距离缩短就算 —— 自己朝一只站着的究极走过去也该躲
    tr = _ed.ApproachTracker()
    for i in range(5):
        d = _ultra((900.0, 0.0))
        tr.update([d], (70.0 * i, 0.0), 10.0 + 0.12 * i)
    assert d["approaching"] is True


def test_tracker_keeps_two_ultras_apart():
    tr = _ed.ApproachTracker()
    for i in range(5):
        a = _ultra((900.0 - 70 * i, 0.0))                   # 冲过来
        b = _ultra((0.0, 900.0), screen=(960, 940))         # 不动
        tr.update([b, a], (0.0, 0.0), 10.0 + 0.12 * i)
    assert a["approaching"] is True and b["approaching"] is False


def test_tracker_leaves_non_avoid_mobs_alone():
    tr = _ed.ApproachTracker()
    for i in range(5):
        d = dict(_det("soldier_ant", "Mythic", (1300, 540)), world=(900.0 - 70 * i, 0.0))
        tr.update([d], (0.0, 0.0), 10.0 + 0.12 * i)
    assert d["approaching"] is False


def test_tracker_without_world_positions_marks_nothing():
    tr = _ed.ApproachTracker()
    d = _det("soldier_ant", "Ultra", (1300, 540))
    tr.update([d], None, 10.0)
    assert d["approaching"] is False


def test_select_action_flees_a_charging_ultra_inside_the_early_radius():
    d = dict(_det("soldier_ant", "Ultra", (960 + 350, 540)), approaching=True)
    action, where = _nearest([d], avoid_trigger_px=200, avoid_early_px=400)
    assert action == "flee" and where == [(1310, 540)]


def test_select_action_ignores_an_idle_ultra_between_the_radii():
    d = dict(_det("soldier_ant", "Ultra", (960 + 350, 540)), approaching=False)
    action = _nearest([d], avoid_trigger_px=200, avoid_early_px=400)[0]
    assert action != "flee"


def test_select_action_early_radius_is_off_by_default():
    d = dict(_det("soldier_ant", "Ultra", (960 + 350, 540)), approaching=True)
    assert _nearest([d], avoid_trigger_px=200)[0] != "flee"


def test_early_flee_is_anthell_only():
    assert _ed.avoid_early_px_for("anthell") == 400
    assert _ed.avoid_early_px_for("desert") is None


# ── 300~600px 的怪: 看得见、走得到就追 (2026-09-29, 用户: 刷怪时怪"看不见") ──────────
# 录像: 刷怪时 14~22% 的拍子在漫游, 其中 85% 以上最近的可打怪在 300~600px, 画面上看得见。
# 300px 的上限是当初怕"隔着墙 / 出了刷怪带"加的 —— 现在由调用方给 can_reach 判这两件事。

def test_far_mob_is_chased_when_reachable():
    d = _det("soldier_ant", "Mythic", (960 + 450, 540))
    action, target, _, _ = _nearest([d], can_reach=lambda det: True)
    assert action == "chase" and target is d


def test_far_mob_behind_a_wall_is_still_ignored():
    d = _det("soldier_ant", "Mythic", (960 + 450, 540))
    assert _nearest([d], can_reach=lambda det: False)[0] == "wander"


def test_far_chase_needs_a_reach_check():
    d = _det("soldier_ant", "Mythic", (960 + 450, 540))
    assert _nearest([d])[0] == "wander"                    # 没给 can_reach = 老行为


def test_far_chase_has_an_outer_limit():
    d = _det("soldier_ant", "Mythic", (960 + _ed.FAR_CHASE_PX + 30, 540))
    assert _nearest([d], can_reach=lambda det: True)[0] == "wander"


def test_near_mobs_do_not_ask_can_reach():
    d = _det("soldier_ant", "Mythic", (960 + 200, 540))
    action = _nearest([d], can_reach=lambda det: pytest.fail("近的不用问"))[0]
    assert action == "chase"


def test_a_near_mob_still_beats_a_farther_reachable_one():
    near = _det("worker_ant", "Mythic", (960 + 250, 540))
    far = _det("worker_ant", "Mythic", (960 + 500, 540))
    _, target, _, _ = _nearest([far, near], can_reach=lambda det: True)
    assert target is near


# ── 刷怪区外的怪不追 (第四份录像: 8 次出区, 全是追着带边/带外 300px 内的怪出去的) ─────

def test_mob_outside_the_farming_area_is_not_chased():
    out = dict(_det("soldier_ant", "Mythic", (960 + 200, 540)), where="out")
    inside = dict(_det("soldier_ant", "Mythic", (960, 540 + 280)), where="in")
    _, target, _, _ = _nearest([out, inside], in_area=lambda d: d["where"] == "in")
    assert target is inside


def test_mob_outside_the_area_already_in_our_face_is_still_fought():
    # 已经贴到停步半径里(正在打)的不管在不在区里 —— 站着打, 不会被带出去
    out = dict(_det("soldier_ant", "Mythic", (960 + 80, 540)), where="out")
    action, target, _, _ = _nearest([out], in_area=lambda d: False)
    assert action == "chase" and target is out


def test_swarm_outside_the_area_is_not_chased():
    dets = [dict(d, where="out") for d in _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT)]
    assert _nearest(dets, in_area=lambda d: False)[0] == "wander"


def test_in_area_is_optional():
    d = _det("soldier_ant", "Mythic", (960 + 200, 540))
    assert _nearest([d])[0] == "chase"


# ── 躲究极时要绕开的怪群(flee_planner 的 crowd) ─────────────────────────────

@pytest.mark.parametrize("species, rarity, want", [
    ("soldier_ant", "Legendary", True),     # 第五份录像: 撞进 12~14 只传奇兵蚁里死的
    ("soldier_ant", "Mythic", True),
    ("worker_ant", "Mythic", True),
    ("soldier_ant", "Epic", False),         # 史诗以下不绕
    ("baby_ant", "Mythic", False),          # 被动
    ("ant_egg", "Legendary", False),        # 不动
    ("soldier_ant", "Ultra", False),        # 究极是追兵, 不是怪群
])
def test_flee_crowd_is_legendary_and_up_that_fights_back(species, rarity, want):
    assert _ed.is_flee_crowd({"species": species, "rarity": rarity}) is want


# ── 钩子装没装隔 2 秒才查一次: 每次 CDP ~56ms, 原来每拍扫描 3 次调用(第六份录像每拍近 1 秒) ──

def _hook_env(monkeypatch, drains):
    calls = {"inject": 0}
    monkeypatch.setattr(_ed, "_hook_checked_at", float("-inf"))
    monkeypatch.setattr(_ed.cdp_bridge, "inject_canvas_hook",
                        lambda *a, **k: calls.__setitem__("inject", calls["inject"] + 1))
    seq = iter(drains)
    monkeypatch.setattr(_ed.cdp_bridge, "drain_canvas_log", lambda *a, **k: next(seq))
    clock = {"t": 100.0}
    monkeypatch.setattr(_ed.time, "time", lambda: clock["t"])
    return calls, clock


def test_scan_checks_the_hook_only_every_couple_of_seconds(monkeypatch):
    frames = [{"frame": 1, "op": "x"}, {"frame": 2, "op": "x"}]
    calls, clock = _hook_env(monkeypatch, [list(frames) for _ in range(10)])
    for _ in range(5):
        _ed.scan_enemies()
        clock["t"] += 0.12
    assert calls["inject"] == 1
    clock["t"] += _ed.HOOK_CHECK_S
    _ed.scan_enemies()
    assert calls["inject"] == 2


def test_scan_rechecks_the_hook_right_after_an_empty_drain(monkeypatch):
    # 页面 reload 了: 钩子没了, 日志读出来是空的 -> 下一拍马上重注, 不等 2 秒
    frames = [{"frame": 1, "op": "x"}, {"frame": 2, "op": "x"}]
    calls, clock = _hook_env(monkeypatch, [list(frames), [], list(frames)])
    _ed.scan_enemies()
    clock["t"] += 0.12
    _ed.scan_enemies()
    clock["t"] += 0.12
    _ed.scan_enemies()
    assert calls["inject"] == 2


def test_swarm_next_to_you_wins_over_a_bigger_one_far_away():
    # 第六份录像 02:10:27: 身边 7 只, 左边 870px 外一堆 11 只 -> 挑了远的, 嫌远不去, 退回追单只
    near = [{"species": "soldier_ant", "rarity": "Mythic", "screen_pos": (960 + 70 * math.cos(a),
                                                                       540 + 70 * math.sin(a)),
             "bbox": (0, 0, 0, 0), "confidence": 1.0} for a in [i * 0.9 for i in range(7)]]
    far = [{"species": "soldier_ant", "rarity": "Legendary", "screen_pos": (90 + 20 * i, 400),
            "bbox": (0, 0, 0, 0), "confidence": 1.0} for i in range(11)]
    act = _ed.select_action(near + far, avoid_trigger_px=200, cautious_hold_px=500,
                            center=(960, 540), target_policy="nearest")
    assert act[0] == "swarm" and act[1]["count"] == 7


def test_swarm_right_next_to_you_wins_over_a_bigger_one_a_bit_further():
    # 第六份录像 02:10:27 修完远近后又一拍: 370px 外一堆 7 只, 贴身(76px)那堆 5 只 -> 去遛远的
    def ant(x, y):
        return {"species": "soldier_ant", "rarity": "Mythic", "screen_pos": (x, y),
                "bbox": (0, 0, 0, 0), "confidence": 1.0}
    close = [ant(960 + 76 + 30 * i, 540) for i in range(5)]
    bigger = [ant(960 - 370 - 25 * i, 540 + 10 * (i % 2)) for i in range(7)]
    sw = _ed.find_swarm(close + bigger, (960, 540), max_center_px=600, prefer_near=True)
    assert sw["count"] == 5 and sw["nearest"] == (1036, 540)
    # 不开 prefer_near 还是老规矩(只数多的赢), 别的调用方不受影响
    assert _ed.find_swarm(close + bigger, (960, 540), max_center_px=600)["count"] == 7


def test_select_action_kites_the_pile_next_to_you():
    def ant(x, y):
        return {"species": "soldier_ant", "rarity": "Mythic", "screen_pos": (x, y),
                "bbox": (0, 0, 0, 0), "confidence": 1.0}
    close = [ant(960 + 76 + 30 * i, 540) for i in range(5)]
    bigger = [ant(960 - 370 - 25 * i, 540 + 10 * (i % 2)) for i in range(7)]
    act = _ed.select_action(close + bigger, avoid_trigger_px=200, cautious_hold_px=500,
                            center=(960, 540), target_policy="nearest")
    assert act[0] == "swarm" and act[1]["nearest"] == (1036, 540)


# ── 名字表: 中英文都认, 按图分流 (2026-10-01) ──────────────────────────────────

# (地图, 英文名, 中文名, slug) —— 英文/中文名取自 florr client.wasm 的本地化表 Mobs/<slug>/Name。
_NAME_ROWS = [
    ("desert", "Scorpion", "蝎子", "scorpion"),
    ("desert", "Beetle", "甲虫", "beetle"),
    ("desert", "Cactus", "仙人掌", "cactus"),
    ("desert", "Sandstorm", "沙尘暴", "sandstorm"),
    ("desert", "Centipede", "蜈蚣", "sand_centipede"),
    ("desert", "Soldier Fire Ant", "火兵蚁", "soldier_fire_ant"),
    ("anthell", "Baby Ant", "幼蚁", "baby_ant"),
    ("anthell", "Worker Ant", "工蚁", "worker_ant"),
    ("anthell", "Soldier Ant", "兵蚁", "soldier_ant"),
    ("anthell", "Worm", "蠕虫", "worm"),
    ("anthell", "Queen Ant", "蚁后", "queen_ant"),
    ("anthell", "Ant Egg", "蚁卵", "ant_egg"),
    # 花园 / 海洋 / 丛林 / 下水道 / 工厂(机械版蜘蛛 / 胡蜂 / 螃蟹跟本体同名, 共用本体 slug)
    ("garden", "Rock", "岩石", "rock"),
    ("garden", "Ladybug", "瓢虫", "ladybug"),
    ("garden", "Bee", "蜜蜂", "bee"),
    ("garden", "Bumble Bee", "熊蜂", "bumble_bee"),
    ("garden", "Baby Ant", "幼蚁", "baby_ant"),
    ("garden", "Worker Ant", "工蚁", "worker_ant"),
    ("garden", "Soldier Ant", "兵蚁", "soldier_ant"),
    ("garden", "Queen Ant", "蚁后", "queen_ant"),
    ("garden", "Ant Egg", "蚁卵", "ant_egg"),
    ("garden", "Ant Hole", "蚁穴", "ant_hole"),
    ("garden", "Hornet", "黄蜂", "hornet"),
    ("garden", "Spider", "蜘蛛", "spider"),
    ("garden", "Centipede", "蜈蚣", "centipede"),
    ("garden", "Dandelion", "蒲公英", "dandelion"),
    ("garden", "Mecha Flower", "机械花", "mecha_flower"),
    ("garden", "Wasp", "胡蜂", "wasp"),
    ("garden", "Crab", "螃蟹", "crab"),
    ("ocean", "Bubble", "泡泡", "bubble"),
    ("ocean", "Shell", "扇贝", "shell"),
    ("ocean", "Crab", "螃蟹", "crab"),
    ("ocean", "Jellyfish", "水母", "jellyfish"),
    ("ocean", "Starfish", "海星", "starfish"),
    ("ocean", "Sponge", "海绵", "sponge"),
    ("ocean", "Leech", "水蛭", "leech"),
    ("ocean", "Soldier Ant", "兵蚁", "soldier_ant"),
    ("jungle", "Baby Termite", "白幼蚁", "baby_termite"),
    ("jungle", "Worker Termite", "白工蚁", "worker_termite"),
    ("jungle", "Soldier Termite", "白兵蚁", "soldier_termite"),
    ("jungle", "Termite Overmind", "白蚁主宰者", "termite_overmind"),
    ("jungle", "Termite Egg", "白蚁卵", "termite_egg"),
    ("jungle", "Termite Mound", "白蚁丘", "termite_mound"),
    ("jungle", "Bush", "灌木丛", "bush"),
    ("jungle", "Centipede", "蜈蚣", "centipede"),
    ("jungle", "Firefly", "萤火虫", "firefly"),
    ("jungle", "Ladybug", "瓢虫", "ladybug"),
    ("jungle", "Leafbug", "叶虫", "leafbug"),
    ("jungle", "Mantis", "螳螂", "mantis"),
    ("jungle", "Wasp", "胡蜂", "wasp"),
    ("jungle", "Crab", "螃蟹", "crab"),
    ("sewers", "Spider", "蜘蛛", "spider"),
    ("sewers", "Roach", "蟑螂", "roach"),
    ("sewers", "Moth", "飞蛾", "moth"),
    ("sewers", "Fly", "苍蝇", "fly"),
    ("sewers", "Silverfish", "蠹虫", "silverfish"),
    ("sewers", "Garbage", "垃圾袋", "garbage"),
    ("factory", "Mecha Flower", "机械花", "mecha_flower"),
    ("factory", "Spider", "蜘蛛", "spider"),
    ("factory", "Wasp", "胡蜂", "wasp"),
    ("factory", "Crab", "螃蟹", "crab"),
    ("factory", "Barrel", "铀桶", "barrel"),
]


@pytest.mark.parametrize("map_name, en, zh, slug", _NAME_ROWS)
def test_species_names_resolve_in_both_languages(map_name, en, zh, slug):
    assert _ed._species_from_name(en, map_name=map_name) == slug
    assert _ed._species_from_name(zh, map_name=map_name) == slug
    assert _ed._species_from_name(en.upper(), map_name=map_name) == slug       # 英文不分大小写
    assert _ed._species_from_name("  " + en + " ", map_name=map_name) == slug  # 去首尾空格


def test_every_species_of_a_supported_map_has_a_name_row():
    covered = {}
    for map_name, _, _, slug in _NAME_ROWS:
        covered.setdefault(map_name, set()).add(slug)
    for map_name, species in _ed.MAP_SPECIES.items():
        if species:
            assert covered.get(map_name) == set(species), map_name


def test_desert_english_client_gaps_are_closed(capsys):
    # wasm 英文表里沙漠蜈蚣叫 "Centipede"(不是 "Sand Centipede"), 以前英文客户端会把它丢掉;
    # 瓢虫中文有折到沙尘暴的覆盖, 英文也得有。
    assert _ed._species_from_name("Centipede", map_name="desert") == "sand_centipede"
    assert _ed._species_from_name("Ladybug", map_name="desert") == "sandstorm"
    assert _ed._species_from_name("Sand Centipede", map_name="desert") == "sand_centipede"  # 旧写法照认
    capsys.readouterr()
    assert _ed._species_from_name("Fire Ant Burrow", map_name="desert") is None
    assert capsys.readouterr().out == ""                       # 出怪口: 认得但不是目标, 不刷日志


def test_desert_worker_fire_ant_folds_into_soldier_fire_ant():
    # wasm 本地化表里火工蚁叫 火工蚁 / Worker Fire Ant; 本项目不分工/兵(同旧的 火蚁 约定)。
    for name in ("火工蚁", "Worker Fire Ant", "worker fire ant", "火蚁"):
        assert _ed._species_from_name(name, map_name="desert") == "soldier_fire_ant", name
    assert _ed._species_from_name("火工蚁", map_name="anthell") is None   # 只在沙漠


def test_species_names_never_leak_across_maps():
    assert _ed._species_from_name("Soldier Ant", map_name="desert") is None
    assert _ed._species_from_name("Soldier Fire Ant", map_name="anthell") is None
    assert _ed._species_from_name("Scorpion", map_name="anthell") is None


def test_names_are_unique_within_each_map():
    for map_name, species in _ed.MAP_SPECIES.items():
        for lang in ("en", "zh"):
            names = [_ed._norm_name(_ed.SPECIES_NAMES[s][lang]) for s in species]
            assert len(names) == len(set(names)), (map_name, lang)


def test_no_name_form_belongs_to_two_species_within_a_map():
    # en 名 / zh 名 / slug 三种形式放进同一个索引(_name_index 是 last-write-wins): 同图里任何一种
    # 形式都不许属于两个物种, 也不许撞 _IGNORE_NAMES —— 否则查找结果取决于 frozenset 的迭代顺序。
    for map_name, species in _ed.MAP_SPECIES.items():
        owners = {}
        for slug in species:
            names = _ed.SPECIES_NAMES[slug]
            for form in {_ed._norm_name(names["en"]), _ed._norm_name(names["zh"]),
                         _ed._norm_name(slug)}:
                assert owners.setdefault(form, slug) == slug, (map_name, form, owners[form], slug)
                assert form not in _ed._IGNORE_NAMES, (map_name, form)


def test_every_named_species_has_both_languages_and_a_rank():
    for slug, names in _ed.SPECIES_NAMES.items():
        assert names.get("en") and names.get("zh"), slug
        assert slug in _ed.SPECIES_RANK, slug
    for map_name, species in _ed.MAP_SPECIES.items():
        for slug in species:
            assert slug in _ed.SPECIES_NAMES, (map_name, slug)


def test_name_overrides_point_at_species_of_that_map():
    for map_name, table in _ed._NAME_OVERRIDES.items():
        for name, slug in table.items():
            assert name == _ed._norm_name(name), name           # key 必须是已规范化的形式
            assert slug in _ed.MAP_SPECIES[map_name], (map_name, name)


def test_override_into_a_species_not_on_the_map_is_ignored(monkeypatch):
    monkeypatch.setitem(_ed.MAP_SPECIES, "desert", frozenset({"scorpion"}))   # 这张图没有 sandstorm 了
    assert _ed._species_from_name("瓢虫", map_name="desert") is None


def test_unknown_name_is_logged_once_with_the_normalized_name(capsys):
    _ed._seen_unknown_names.clear()
    for _ in range(3):
        assert _ed._species_from_name("Weird Thing", map_name="desert") is None
    out = capsys.readouterr().out
    assert out.count("未识别怪物名") == 1 and "weird_thing" in out


# ── 花园 / 海洋 / 丛林 / 下水道 / 工厂的物种表 (2026-10-01) ────────────────────────


def test_the_same_name_is_a_different_species_on_different_maps():
    # 蜈蚣: 沙漠 sand_centipede / 花园、丛林 centipede; 瓢虫: 沙漠折成 sandstorm / 花园、丛林 ladybug。
    for name in ("蜈蚣", "Centipede"):
        assert _ed._species_from_name(name, map_name="desert") == "sand_centipede"
        assert _ed._species_from_name(name, map_name="garden") == "centipede"
        assert _ed._species_from_name(name, map_name="jungle") == "centipede"
    for name in ("瓢虫", "Ladybug"):
        assert _ed._species_from_name(name, map_name="desert") == "sandstorm"
        assert _ed._species_from_name(name, map_name="garden") == "ladybug"
        assert _ed._species_from_name(name, map_name="jungle") == "ladybug"


@pytest.mark.parametrize("name", ["Digger", "挖掘者", "Ghost", "幽灵", "Square", "正方形",
                                  "Target Dummy", "训练假花", "Titan", "泰坦",
                                  "Assembler", "重构机"])
@pytest.mark.parametrize("map_name", ["garden", "jungle", "sewers", "factory"])
def test_non_target_entities_are_ignored_silently(name, map_name, capsys):
    _ed._seen_unknown_names.clear()
    assert _ed._species_from_name(name, map_name=map_name) is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("slug", sorted({
    row[3] for row in _NAME_ROWS
    if row[0] in ("garden", "ocean", "jungle", "sewers", "factory")}))
def test_new_species_are_engaged_up_to_mythic_and_avoided_from_ultra(slug):
    for rarity in _BELOW_ULTRA:
        assert classify_action(slug, rarity) == "ENGAGE"
    for rarity in ("Ultra", "Super", "Eternal", "Unique"):
        assert classify_action(slug, rarity) == "AVOID"


def test_scan_enemies_resolves_ocean_names_in_both_languages(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "ocean")
    f_old = gameplay_frame(0, mobs=[(400.0, 200.0, "水母", 1.0), (300.0, 300.0, "Jellyfish", 1.0),
                                    (200.0, 100.0, "Digger", 1.0)])
    _stub_canvas(monkeypatch, f_old + gameplay_frame(1))
    assert [d["species"] for d in scan_enemies()] == ["jellyfish", "jellyfish"]


# ── 物种特性表 SPECIES_TRAITS (2026-10-01) ─────────────────────────────────────
# 先是蚁穴六种旧行为的特征化测试: 它们在三张蚁穴专用的表搬进 SPECIES_TRAITS **之前**就写好、
# 在旧代码上全绿, 搬完仍然全绿 —— 这是"蚁穴行为逐项不变"的证据。旧值硬写在这里, 不从表里读。

_ANTHELL_HOLD = {"baby_ant": 50, "worker_ant": 50, "ant_egg": 50, "queen_ant": 180,
                 "soldier_ant": _ed.ENGAGE_HOLD_PX, "worm": 60}
_ANTHELL_COST = {"baby_ant": 0.5, "worker_ant": 0.9, "worm": 1.2, "queen_ant": 1.5,
                 "ant_egg": 1.5}      # soldier_ant 是 1.0, 当对照物


@pytest.mark.parametrize("species, hold", sorted(_ANTHELL_HOLD.items()))
def test_anthell_hold_radius_is_pinned(species, hold):
    action, target, hold_px, _ = _nearest([_det(species, "Mythic", (1210, 540))])
    assert action == "chase" and target["species"] == species
    assert hold_px == hold


def _first_pick(species, ref_species, ref_dist, my_dist=400):
    """species 在 my_dist 处、ref_species 在 ref_dist 处, 都没进各自的停步半径时先打谁。"""
    dets = [_det(species, "Mythic", (960 + my_dist, 540)),
            _det(ref_species, "Mythic", (960, 540 - ref_dist))]
    return _nearest(dets, chase_max_px=2000)[1]["species"]


@pytest.mark.parametrize("species, cost", sorted(_ANTHELL_COST.items()))
def test_anthell_target_cost_is_pinned(species, cost):
    # 对照物 soldier_ant 的距离 = my_dist × cost 上下各 4%: cost 差超过 4% 就会翻。
    assert _first_pick(species, "soldier_ant", 400 * cost * 1.04) == species
    assert _first_pick(species, "soldier_ant", 400 * cost * 0.96) == "soldier_ant"


@pytest.mark.parametrize("species", ["baby_ant", "worker_ant", "ant_egg", "soldier_ant",
                                     "queen_ant"])
def test_anthell_species_count_toward_a_swarm(species):
    dets = _swarm_at(1200, 540, _ed.SWARM_MIN_COUNT, species=species)
    assert _nearest(dets)[0] == "swarm"


def test_anthell_worm_does_not_count_toward_a_swarm():
    dets = _swarm_at(1200, 540, _ed.SWARM_MIN_COUNT, species="worm")
    assert _nearest(dets)[0] != "swarm"


# 引擎: 用合成物种(monkeypatch 进表, 不碰真表)逐个字段测行为。

def _with_traits(monkeypatch, slug, **fields):
    monkeypatch.setitem(_ed.SPECIES_TRAITS, slug, fields)


def test_unknown_species_gets_the_default_traits():
    assert _ed.tactic_for("zzz") == "approach"
    assert _ed.hold_px_for("zzz") == _ed.ENGAGE_HOLD_PX
    assert _ed.hold_px_for("zzz", engage_hold_px=77) == 77
    assert _ed.cost_for("zzz") == 1.0
    assert _ed.counts_for_swarm("zzz") is True
    assert _ed.priority_px_for("zzz") is None


@pytest.mark.parametrize("kind, want", [("active", _ed.ENGAGE_HOLD_PX),
                                        ("passive", _ed.PASSIVE_HOLD_PX),
                                        ("static", _ed.PASSIVE_HOLD_PX)])
def test_kind_picks_the_default_hold_radius(monkeypatch, kind, want):
    _with_traits(monkeypatch, "zzz", kind=kind)
    assert _ed.hold_px_for("zzz") == want
    action, target, hold_px, _ = _nearest([_det("zzz", "Mythic", (1210, 540))])
    assert action == "chase" and hold_px == want


def test_an_explicit_hold_px_beats_the_kind_default(monkeypatch):
    _with_traits(monkeypatch, "zzz", kind="passive", hold_px=90)
    assert _ed.hold_px_for("zzz") == 90
    assert _nearest([_det("zzz", "Mythic", (1210, 540))])[2] == 90


def test_the_cautious_bucket_still_uses_the_cautious_hold():
    # Ultra 沙尘暴是 CAUTIOUS: 停步半径永远是 cautious_hold_px, 不看特性表。
    action, target, hold_px, _ = _nearest([_det("sandstorm", "Ultra", (1210, 540))],
                                          cautious_hold_px=250)
    assert action == "chase" and target["species"] == "sandstorm" and hold_px == 250


def test_a_strafing_species_is_circled_at_its_tactic_radius(monkeypatch):
    _with_traits(monkeypatch, "zzz", tactic="strafe", tactic_px=120)
    assert _ed.tactic_for("zzz") == "strafe"
    assert _ed.hold_px_for("zzz") == 120
    action, target, hold_px, _ = _nearest([_det("zzz", "Mythic", (1210, 540))])
    assert action == "chase" and hold_px == 120
    # 正好在半径上: 纯切向, 不往里也不往外
    tgt = _det("zzz", "Mythic", (1080, 540))
    x, y = _ed.chase_move_target(tgt, 120, center=(960, 540), max_extend=500)
    assert x == pytest.approx(960) and abs(y - 540) == pytest.approx(500)


def test_a_strafing_species_never_stands_still(monkeypatch):
    _with_traits(monkeypatch, "zzz", tactic="strafe", tactic_px=120)
    for dist in (0, 40, 120, 300):
        tgt = _det("zzz", "Mythic", (960 + dist, 540))
        got = _ed.chase_move_target(tgt, 120, center=(960, 540), max_extend=500)
        assert got != (960, 540), dist
        assert math.hypot(got[0] - 960, got[1] - 540) == pytest.approx(500)


def test_an_approach_species_is_not_circled(monkeypatch):
    _with_traits(monkeypatch, "zzz", kind="passive")
    tgt = _det("zzz", "Mythic", (1000, 540))
    assert _ed.chase_move_target(tgt, 50, center=(960, 540), max_extend=500) == (960, 540)


@pytest.mark.parametrize("fields, joins", [
    ({}, True),                                       # active + approach: 默认算
    ({"kind": "active"}, True),
    ({"kind": "passive"}, False),
    ({"kind": "static"}, False),
    ({"tactic": "strafe", "tactic_px": 120}, False),  # 绕圈的射手不是"凑过来撞花瓣"的怪
    ({"kind": "static", "swarm": True}, True),        # 显式写 True 覆盖默认
    ({"kind": "active", "swarm": False}, False),
])
def test_swarm_membership_follows_kind_tactic_and_the_explicit_flag(monkeypatch, fields, joins):
    _with_traits(monkeypatch, "zzz", **fields)
    assert _ed.counts_for_swarm("zzz") is joins
    dets = _swarm_at(1200, 540, _ed.SWARM_MIN_COUNT, species="zzz")
    action = _nearest(dets)[0]
    assert (action == "swarm") is joins


def test_priority_px_pulls_the_bot_off_a_swarm_only_when_close(monkeypatch):
    _with_traits(monkeypatch, "zzz", tactic="strafe", tactic_px=60, priority_px=200)
    near = _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT) + [_det("zzz", "Mythic", (960 - 150, 540))]
    action, target, _, _ = _nearest(near)
    assert action == "chase" and target["species"] == "zzz"
    far = _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT) + [_det("zzz", "Mythic", (960 - 250, 540))]
    assert _nearest(far)[0] == "swarm"


def test_a_strafing_species_without_priority_px_does_not_beat_a_swarm(monkeypatch):
    _with_traits(monkeypatch, "zzz", tactic="strafe", tactic_px=120)
    dets = _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT) + [_det("zzz", "Mythic", (960 - 150, 540))]
    assert _nearest(dets)[0] == "swarm"


def test_stall_exemption_is_opt_in_per_species(monkeypatch):
    assert _ed.stall_exempt_for("zzz") is False
    _with_traits(monkeypatch, "zzz", tactic="strafe", tactic_px=120)
    assert _ed.stall_exempt_for("zzz") is False        # 绕圈本身不豁免
    _with_traits(monkeypatch, "zzz", tactic="strafe", tactic_px=120, stall_exempt=True)
    assert _ed.stall_exempt_for("zzz") is True


def test_only_the_worm_is_exempt_from_stall_tracking():
    exempt = {slug for slug in _ed.SPECIES_TRAITS if _ed.stall_exempt_for(slug)}
    assert exempt == {"worm"}


@pytest.mark.parametrize("cost", [0.5, 2.0, 3.0])
def test_cost_decides_who_is_hit_first_when_nobody_is_engaged(monkeypatch, cost):
    _with_traits(monkeypatch, "zzz", cost=cost)
    assert _first_pick("zzz", "soldier_ant", 400 * cost * 1.04) == "zzz"
    assert _first_pick("zzz", "soldier_ant", 400 * cost * 0.96) == "soldier_ant"


_TRAIT_KEYS = {"kind", "hold_px", "tactic", "tactic_px", "cost", "swarm", "priority_px",
               "stall_exempt"}


def test_species_traits_table_is_well_formed():
    for slug, t in _ed.SPECIES_TRAITS.items():
        assert slug in _ed.SPECIES_NAMES, slug
        assert set(t) <= _TRAIT_KEYS, (slug, set(t) - _TRAIT_KEYS)
        assert t.get("kind", "active") in ("active", "passive", "static"), slug
        assert t.get("tactic", "approach") in ("approach", "strafe"), slug
        for key in ("hold_px", "tactic_px", "cost", "priority_px"):
            if key in t:
                assert t[key] > 0, (slug, key)
        if t.get("tactic") == "strafe":
            assert t.get("tactic_px"), slug
        if "swarm" in t:
            assert isinstance(t["swarm"], bool), slug
        if "stall_exempt" in t:
            assert isinstance(t["stall_exempt"], bool), slug


# ── 花园 / 海洋 / 丛林 / 下水道 / 工厂的物种特性(2026-10-01, 依据维基, 全部未标定) ─────────
# 逐项钉住 spec「5.3 新图的特性」那张表; 蚁穴五种有行的钉旧值(soldier_ant 全默认, 见上面的特征化测试)。

_ANTHELL_TRAITS = {
    "baby_ant": {"kind": "passive", "cost": 0.5, "swarm": True},
    "worker_ant": {"kind": "passive", "cost": 0.9, "swarm": True},
    "ant_egg": {"kind": "static", "cost": 1.5, "swarm": True},
    "queen_ant": {"hold_px": 180, "cost": 1.5},
    "worm": {"tactic": "strafe", "tactic_px": 60, "cost": 1.2, "priority_px": 200,
             "stall_exempt": True},
}
_NEW_MAP_TRAITS = {
    "ladybug": {"kind": "passive"},
    "bee": {"kind": "passive"},
    "bumble_bee": {"kind": "passive"},
    "centipede": {"kind": "passive"},
    "shell": {"kind": "passive"},
    "roach": {"kind": "passive"},
    "leafbug": {"kind": "passive"},
    "baby_termite": {"kind": "passive"},
    "worker_termite": {"kind": "passive"},
    "firefly": {"kind": "passive", "hold_px": 80},
    "moth": {"kind": "passive", "cost": 2.0},
    "bubble": {"kind": "passive", "cost": 2.5},
    "rock": {"kind": "static"},
    "bush": {"kind": "static"},
    "sponge": {"kind": "static"},
    "dandelion": {"kind": "static"},
    "termite_egg": {"kind": "static"},
    "garbage": {"kind": "static", "cost": 0.8},
    "barrel": {"kind": "static", "hold_px": 90, "cost": 3.0},
    "ant_hole": {"kind": "static", "cost": 1.5},
    "termite_mound": {"kind": "static", "cost": 1.5},
    "fly": {"cost": 2.0},
    "hornet": {"tactic": "strafe", "tactic_px": 120},
    "wasp": {"tactic": "strafe", "tactic_px": 120},
    "mantis": {"tactic": "strafe", "tactic_px": 120},
}
_DEFAULT_ACTIVE = ["soldier_ant", "spider", "crab", "jellyfish", "starfish", "leech", "silverfish",
                   "soldier_termite", "termite_overmind", "mecha_flower"]
_PASSIVE_OR_STATIC = [s for s, t in _NEW_MAP_TRAITS.items()
                      if t.get("kind") in ("passive", "static")]
_SHOOTERS = ["hornet", "wasp", "mantis"]


def test_species_traits_table_is_exactly_the_documented_one():
    assert _ed.SPECIES_TRAITS == {**_ANTHELL_TRAITS, **_NEW_MAP_TRAITS}


@pytest.mark.parametrize("slug", _DEFAULT_ACTIVE)
def test_plain_active_species_have_no_special_traits(slug):
    assert slug not in _ed.SPECIES_TRAITS
    assert _ed.hold_px_for(slug) == _ed.ENGAGE_HOLD_PX
    assert _ed.tactic_for(slug) == "approach"
    assert _ed.cost_for(slug) == 1.0
    assert _ed.counts_for_swarm(slug) is True


@pytest.mark.parametrize("slug", _PASSIVE_OR_STATIC + _SHOOTERS)
def test_species_that_do_not_come_to_you_never_form_a_swarm(slug):
    # 5 只岩石 / 灌木 / 海绵 / 射手挤一起: 站着等它们撞上来是干等 —— 去打。
    assert _ed.counts_for_swarm(slug) is False
    assert _nearest(_swarm_at(1200, 540, _ed.SWARM_MIN_COUNT, species=slug))[0] == "chase"


@pytest.mark.parametrize("slug", _DEFAULT_ACTIVE + ["fly"])
def test_active_species_still_form_a_swarm(slug):
    assert _nearest(_swarm_at(1200, 540, _ed.SWARM_MIN_COUNT, species=slug))[0] == "swarm"


@pytest.mark.parametrize("slug, hold", [
    ("rock", 50), ("bush", 50), ("sponge", 50), ("ladybug", 50), ("bee", 50), ("roach", 50),
    ("firefly", 80), ("barrel", 90), ("spider", 120), ("jellyfish", 120), ("mecha_flower", 120),
    ("hornet", 120), ("wasp", 120), ("mantis", 120),
])
def test_new_map_hold_radius(slug, hold):
    action, target, hold_px, _ = _nearest([_det(slug, "Mythic", (1210, 540))])
    assert action == "chase" and target["species"] == slug and hold_px == hold


@pytest.mark.parametrize("slug, cost", [("garbage", 0.8), ("barrel", 3.0), ("bubble", 2.5),
                                        ("moth", 2.0), ("fly", 2.0), ("ant_hole", 1.5),
                                        ("termite_mound", 1.5)])
def test_new_map_target_cost(slug, cost):
    assert _first_pick(slug, "spider", 400 * cost * 1.04) == slug
    assert _first_pick(slug, "spider", 400 * cost * 0.96) == "spider"


@pytest.mark.parametrize("slug", _SHOOTERS)
def test_ranged_shooters_are_circled_not_rammed(slug):
    assert _ed.tactic_for(slug) == "strafe"
    tgt = _det(slug, "Mythic", (1080, 540))                  # 正好在 120px 的绕圈半径上
    x, y = _ed.chase_move_target(tgt, 120, center=(960, 540), max_extend=500)
    assert x == pytest.approx(960) and abs(y - 540) == pytest.approx(500)
    for dist in (0, 60, 300):                                # 任何距离都不停下
        got = _ed.chase_move_target(_det(slug, "Mythic", (960 + dist, 540)), 120,
                                    center=(960, 540), max_extend=500)
        assert got != (960, 540), dist


def test_a_nearby_shooter_does_not_pull_the_bot_off_a_swarm():
    dets = _swarm_at(1300, 540, _ed.SWARM_MIN_COUNT) + [_det("hornet", "Mythic", (960 - 150, 540))]
    assert _nearest(dets)[0] == "swarm"


def test_bubbles_are_not_a_crowd_to_flee_through():
    assert _ed.is_flee_crowd(_det("bubble", "Legendary", (0, 0))) is False
    assert _ed.is_flee_crowd(_det("baby_ant", "Legendary", (0, 0))) is False
    assert _ed.is_flee_crowd(_det("spider", "Legendary", (0, 0))) is True


# ── 第②轮: 用户自配规则 ──────────────────────────────────────────────────────
# 模块级 _RULES 的前后清理由根目录 conftest.py 的 autouse fixture(_reset_enemy_rules)统一做。

def test_set_rules_replaces_instead_of_merging():
    _ed.set_rules({"species": {"rock": "ignore"}, "knobs": {"swarm": False}})
    _ed.set_rules({"species": {"bee": "fight"}})
    assert _ed.get_rules() == {"species": {"bee": "fight"}, "knobs": {}}
    _ed.set_rules(None)
    assert _ed.get_rules() == {"species": {}, "knobs": {}}


def test_set_rules_ignores_garbage_input():
    for junk in (None, [], "x", 5, {"species": [1], "knobs": "y"}):
        _ed.set_rules(junk)
        assert _ed.get_rules() == {"species": {}, "knobs": {}}


def test_set_rules_does_not_alias_the_callers_dicts():
    src = {"species": {"rock": "ignore"}, "knobs": {"chase_max_px": 400}}
    _ed.set_rules(src)
    src["species"]["rock"] = "fight"
    src["knobs"]["chase_max_px"] = 999
    assert _ed.rule_for("rock") == "ignore"
    assert _ed.knob("chase_max_px", 300) == 400
    got = _ed.get_rules()
    got["species"]["rock"] = "avoid"
    assert _ed.rule_for("rock") == "ignore"        # get_rules 给的也是拷贝


def test_avoid_rule_makes_any_rarity_avoid():
    _ed.set_rules({"species": {"bee": "avoid"}})
    for rarity in ("Common", "Mythic", "Ultra", "Unique"):
        assert classify_action("bee", rarity) == "AVOID"
    assert classify_action("ladybug", "Common") == "ENGAGE"      # 别的物种不受影响


def test_fight_rule_makes_any_rarity_engage():
    assert classify_action("rock", "Ultra") == "AVOID"           # 内置: 静止 Ultra 兜底 AVOID
    _ed.set_rules({"species": {"rock": "fight"}})
    for rarity in ("Common", "Ultra", "Super", "Eternal", "Unique"):
        assert classify_action("rock", rarity) == "ENGAGE"


def test_fight_rule_overrides_the_desert_ultra_avoid_pair():
    assert classify_action("scorpion", "Ultra") == "AVOID"
    _ed.set_rules({"species": {"scorpion": "fight"}})
    assert classify_action("scorpion", "Ultra") == "ENGAGE"


def test_ignore_rule_does_not_change_classification():
    # ignore 在 drop_ignored 里剔除; classify_action 万一被漏网的检测问到, 按内置分类, 不崩
    _ed.set_rules({"species": {"scorpion": "ignore"}})
    assert classify_action("scorpion", "Ultra") == "AVOID"
    assert classify_action("scorpion", "Common") == "ENGAGE"


def test_an_avoid_rule_on_a_nearby_species_triggers_flee():
    _ed.set_rules({"species": {"bee": "avoid"}})
    action, positions = _nearest([_det("bee", "Common", (1060, 540))])
    assert action == "flee" and positions == [(1060, 540)]


def test_a_fight_rule_turns_a_static_ultra_from_a_flee_source_into_a_target():
    dets = [_det("rock", "Ultra", (1060, 540))]
    assert _nearest(dets)[0] == "flee"                           # 内置: 永久逃跑源(第①轮的已知问题)
    _ed.set_rules({"species": {"rock": "fight"}})
    action, target, _hold, _repel = _nearest(dets)
    assert action == "chase" and target["species"] == "rock"


def test_drop_ignored_removes_only_ignored_species_and_does_not_mutate():
    dets = [_det("rock", "Ultra", (1, 1)), _det("bee", "Common", (2, 2)),
            _det("rock", "Common", (3, 3)), _det("ladybug", "Common", (4, 4))]
    _ed.set_rules({"species": {"rock": "ignore", "bee": "fight", "ladybug": "avoid"}})
    kept = _ed.drop_ignored(dets)
    # 只剔 ignore: fight / avoid 的检测都得留着(avoid 的要留给 flee 判定用)
    assert [d["species"] for d in kept] == ["bee", "ladybug"]
    assert len(dets) == 4 and kept is not dets


def test_drop_ignored_without_rules_keeps_everything_in_a_new_list():
    dets = [_det("rock", "Ultra", (1, 1)), _det("bee", "Common", (2, 2))]
    kept = _ed.drop_ignored(dets)
    assert kept == dets and kept is not dets


def test_knob_returns_the_user_value_or_the_default():
    assert _ed.knob("chase_max_px", 300) == 300
    _ed.set_rules({"knobs": {"chase_max_px": 450, "swarm": False}})
    assert _ed.knob("chase_max_px", 300) == 450
    assert _ed.knob("swarm", True) is False          # False 是合法值, 不能当"没设"
    assert _ed.knob("avoid_px", 400) == 400


def test_hold_passive_knob_moves_passive_and_static_only():
    _ed.set_rules({"knobs": {"hold_passive_px": 70}})
    assert _ed.hold_px_for("ladybug") == 70                       # passive
    assert _ed.hold_px_for("rock") == 70                          # static
    assert _ed.hold_px_for("firefly") == 80                       # 自带 hold_px, 不被旋钮覆盖
    assert _ed.hold_px_for("queen_ant") == 180
    assert _ed.hold_px_for("barrel") == 90
    assert _ed.hold_px_for("hornet") == _ed.STRAFE_SHOOTER_PX     # strafe 半径
    assert _ed.hold_px_for("spider") == _ed.ENGAGE_HOLD_PX        # 主动怪不归它管


def test_hold_active_knob_moves_the_active_default_and_an_explicit_arg_still_wins():
    _ed.set_rules({"knobs": {"hold_active_px": 150}})
    assert _ed.hold_px_for("spider") == 150
    assert _ed.hold_px_for("zzz") == 150                          # 表外物种 = active
    assert _ed.hold_px_for("spider", 99) == 99
    assert _ed.hold_px_for("queen_ant") == 180                    # 自带值不动
    assert _ed.hold_px_for("hornet") == _ed.STRAFE_SHOOTER_PX


def test_hold_px_for_without_rules_is_unchanged():
    assert _ed.hold_px_for("spider") == _ed.ENGAGE_HOLD_PX == 120
    assert _ed.hold_px_for("ladybug") == _ed.PASSIVE_HOLD_PX == 50


def test_swarm_enabled_false_never_returns_swarm():
    dets = _swarm_at(1200, 540, _ed.SWARM_MIN_COUNT)
    assert _nearest(dets)[0] == "swarm"
    action, target, _hold, _repel = _nearest(dets, swarm_enabled=False)
    assert action == "chase" and target["species"] == "soldier_ant"


def test_default_avoid_trigger_constant_is_400():
    assert _ed.DEFAULT_AVOID_TRIGGER_PX == 400


def test_select_action_signature_defaults_follow_the_engines_constants():
    # avoid_trigger_px 的默认值曾经是字面量 400, 跟 DEFAULT_AVOID_TRIGGER_PX 各写各的;
    # engage_hold_px 的默认值必须是 None(= 运行时取旋钮), 写成 ENGAGE_HOLD_PX 就在定义时绑死了。
    import inspect
    params = inspect.signature(select_action).parameters
    assert params["avoid_trigger_px"].default == _ed.DEFAULT_AVOID_TRIGGER_PX
    assert params["engage_hold_px"].default is None


def test_select_action_without_engage_hold_px_honours_the_users_hold_active_knob():
    # 之前 engage_hold_px 默认 ENGAGE_HOLD_PX, 不传这个参数的调用方(测试 / 以后新增的调用点)会绕过旋钮。
    dets = [_det("spider", "Common", (1210, 540))]               # 250px: 在 120 外也在 150 外
    _ed.set_rules({"knobs": {"hold_active_px": 150}})
    action, target, hold_px, _repel = _nearest(dets)
    assert action == "chase" and target["species"] == "spider"
    assert hold_px == 150
    # 显式传的值仍然优先于旋钮(main._maybe_... 那条调用路径就是显式传)
    assert _nearest(dets, engage_hold_px=99)[2] == 99


def test_select_action_without_engage_hold_px_and_without_rules_uses_the_builtin():
    dets = [_det("spider", "Common", (1210, 540))]
    action, _target, hold_px, _repel = _nearest(dets)
    assert action == "chase" and hold_px == _ed.ENGAGE_HOLD_PX == 120


def test_map_species_is_the_pure_data_modules_own_dict():
    # app_config 要校验物种, 又不能 import enemy_detect(cv2 / cdp_bridge / pyautogui) ——
    # 所以数据放在 enemy_species.py, enemy_detect 里的就是同一个 dict 对象。
    import enemy_species
    assert _ed.MAP_SPECIES is enemy_species.MAP_SPECIES
    assert set(enemy_species.MAP_SPECIES) == {
        "garden", "desert", "ocean", "jungle", "anthell", "sewers", "factory"}
    assert all(isinstance(v, frozenset) and v for v in enemy_species.MAP_SPECIES.values())


# ── 第③轮: 躲避起点稀有度(avoid_min_rarity 旋钮) ─────────────────────────────────
# 稀有度 < 起点 → ENGAGE; >= 起点 → 内置表(Ultra 蝎子/甲虫 AVOID, Ultra 沙尘暴等 CAUTIOUS), 没列的 AVOID。
# 物种规则(打/躲)仍然优先。没设旋钮 = "Ultra", 跟第②轮及以前逐项一样(上面 classify_action 的老用例守着)。

def test_rarity_data_lives_in_the_pure_data_module_and_enemy_detect_reexports_it():
    import enemy_species
    assert _ed.RARITY_ORDER is enemy_species.RARITY_ORDER
    assert enemy_species.AVOID_MIN_RARITY_DEFAULT == "Ultra"
    assert enemy_species.AVOID_MIN_RARITY_CHOICES == (
        "Epic", "Legendary", "Mythic", "Ultra", "Super", "Eternal", "Unique", "never")
    # 可选的稀有度名必须都是真的档名(除了 never), 否则 classify_action 里查 RARITY_RANK 会 KeyError
    assert set(enemy_species.AVOID_MIN_RARITY_CHOICES) - {"never"} <= set(_ed.RARITY_ORDER)


def test_lowering_avoid_min_rarity_makes_legendary_and_up_avoid():
    _ed.set_rules({"knobs": {"avoid_min_rarity": "Legendary"}})
    assert _ed.classify_action("scorpion", "Epic") == "ENGAGE"          # 起点以下照旧打
    assert _ed.classify_action("scorpion", "Legendary") == "AVOID"
    assert _ed.classify_action("sandstorm", "Mythic") == "AVOID"        # 没列的 >= 起点 一律躲
    assert _ed.classify_action("sandstorm", "Ultra") == "CAUTIOUS"      # 内置表在 >= 起点 时仍然生效
    assert _ed.classify_action("scorpion", "Ultra") == "AVOID"


def test_raising_avoid_min_rarity_makes_ultra_engage_and_keeps_the_builtin_table_above():
    _ed.set_rules({"knobs": {"avoid_min_rarity": "Super"}})
    assert _ed.classify_action("scorpion", "Ultra") == "ENGAGE"         # 内置的 Ultra AVOID 被起点盖过
    assert _ed.classify_action("sandstorm", "Ultra") == "ENGAGE"        # 内置的 Ultra CAUTIOUS 也是
    assert _ed.classify_action("scorpion", "Super") == "AVOID"
    assert _ed.classify_action("rock", "Unique") == "AVOID"


def test_never_means_no_rarity_based_avoid_but_species_avoid_still_works():
    _ed.set_rules({"species": {"scorpion": "avoid"}, "knobs": {"avoid_min_rarity": "never"}})
    for rarity in _ed.RARITY_ORDER:
        assert _ed.classify_action("beetle", rarity) == "ENGAGE"
    assert _ed.classify_action("scorpion", "Common") == "AVOID"         # 物种规则不受影响


def test_species_rules_beat_the_rarity_threshold():
    _ed.set_rules({"species": {"rock": "fight", "bee": "avoid"},
                   "knobs": {"avoid_min_rarity": "Epic"}})
    assert _ed.classify_action("rock", "Unique") == "ENGAGE"            # fight: 高于起点也打
    assert _ed.classify_action("bee", "Common") == "AVOID"              # avoid: 低于起点也躲
    assert _ed.classify_action("ladybug", "Epic") == "AVOID"            # 没规则的走起点


@pytest.mark.parametrize("junk", ["ultra", "Foo", "", None, 5, True, ["Epic"], {"a": 1}])
def test_a_garbage_avoid_min_rarity_falls_back_to_ultra(junk):
    # app_config 会在更外面拦住, 但 set_rules 不校验 —— 万一漏进来也不能崩, 按默认 Ultra 走。
    _ed.set_rules({"knobs": {"avoid_min_rarity": junk}})
    assert _ed.classify_action("scorpion", "Mythic") == "ENGAGE"
    assert _ed.classify_action("scorpion", "Ultra") == "AVOID"


def test_the_threshold_reaches_the_flee_decision():
    dets = [_det("rock", "Legendary", (1000, 540))]                     # 40px 外的传奇岩石
    assert _nearest(dets)[0] == "chase"                                 # 默认: 传奇照打
    _ed.set_rules({"knobs": {"avoid_min_rarity": "Legendary"}})
    action, avoid_positions = _nearest(dets)
    assert action == "flee" and avoid_positions == [(1000, 540)]
    _ed.set_rules({"knobs": {"avoid_min_rarity": "never"}})
    assert _nearest([_det("rock", "Unique", (1000, 540))])[0] == "chase"


# ── 逐物种 × 逐稀有度: species[slug] 可以是 {稀有度: 打法}(含 "*" = 所有稀有度的默认) ──────────────
# 四种打法: fight(打)/ cautious(谨慎: 打, 但保持距离)/ avoid(躲)/ ignore(忽略)。
# 优先级: 该稀有度的格子 > "*" > 稀有度起点旋钮 / 内置表。字符串写法 = {"*": 字符串}, 老配置照旧。

def test_rule_for_resolves_rarity_entry_then_star_then_nothing():
    _ed.set_rules({"species": {"bee": {"*": "avoid", "Common": "fight"}, "rock": {"Epic": "ignore"},
                               "ladybug": "cautious"}})
    assert _ed.rule_for("bee", "Common") == "fight"          # 该稀有度的格子
    assert _ed.rule_for("bee", "Mythic") == "avoid"          # 没配 -> "*"
    assert _ed.rule_for("rock", "Epic") == "ignore"
    assert _ed.rule_for("rock", "Common") is None            # 没配也没有 "*" -> 没规则
    assert _ed.rule_for("ladybug", "Ultra") == "cautious"    # 字符串写法 = 所有稀有度
    assert _ed.rule_for("bee") == "avoid"                    # 不给稀有度: 只看 "*" / 字符串(老调用方)
    assert _ed.rule_for("rock") is None
    assert _ed.rule_for("scorpion", "Epic") is None


def test_a_rarity_entry_changes_only_that_rarity_and_the_rest_keeps_the_builtin_behaviour():
    _ed.set_rules({"species": {"scorpion": {"Epic": "avoid", "Mythic": "fight"}}})
    assert classify_action("scorpion", "Epic") == "AVOID"          # 内置是 ENGAGE
    assert classify_action("scorpion", "Mythic") == "ENGAGE"
    assert classify_action("scorpion", "Rare") == "ENGAGE"         # 没配: 内置
    assert classify_action("scorpion", "Ultra") == "AVOID"         # 没配: 内置(蝎子 Ultra 躲)
    assert classify_action("beetle", "Epic") == "ENGAGE"           # 别的物种不受影响


def test_the_star_entry_covers_unlisted_rarities_and_a_rarity_entry_beats_it():
    _ed.set_rules({"species": {"bee": {"*": "avoid", "Common": "fight"}}})
    assert classify_action("bee", "Common") == "ENGAGE"
    for rarity in ("Unusual", "Legendary", "Ultra", "Unique"):
        assert classify_action("bee", rarity) == "AVOID"


def test_cautious_is_a_real_fourth_action_at_species_level_and_per_rarity():
    _ed.set_rules({"species": {"ladybug": "cautious", "bee": {"Epic": "cautious"}}})
    for rarity in ("Common", "Mythic", "Ultra", "Unique"):
        assert classify_action("ladybug", rarity) == "CAUTIOUS"
    assert classify_action("bee", "Epic") == "CAUTIOUS"
    assert classify_action("bee", "Common") == "ENGAGE"


def test_a_cautious_target_is_fought_from_the_cautious_distance_not_the_normal_one():
    dets = [_det("bee", "Common", (1240, 540))]                    # 280px: 在追击上限(300)内, 在谨慎距离(250)外
    normal = _nearest(dets)
    assert normal[0] == "chase" and normal[2] == _ed.hold_px_for("bee") < 250      # 蜜蜂是被动怪, 停步半径小
    _ed.set_rules({"species": {"bee": {"Common": "cautious"}}})
    got = _nearest(dets)
    assert got[0] == "chase" and got[2] == 250                    # cautious_hold_px 默认 250
    assert _nearest(dets, cautious_hold_px=333)[2] == 333


def test_a_rarity_entry_beats_the_rarity_threshold_knob_and_unlisted_rarities_still_use_it():
    _ed.set_rules({"species": {"bee": {"Legendary": "fight", "Rare": "avoid"}},
                   "knobs": {"avoid_min_rarity": "Legendary"}})
    assert classify_action("bee", "Legendary") == "ENGAGE"         # 格子 > 起点(起点本来会躲)
    assert classify_action("bee", "Rare") == "AVOID"               # 格子 > 起点(起点以下本来会打)
    assert classify_action("bee", "Mythic") == "AVOID"             # 没配: 起点
    assert classify_action("bee", "Epic") == "ENGAGE"              # 没配: 起点以下照打


def test_a_species_level_string_still_beats_the_threshold_knob_like_before():
    _ed.set_rules({"species": {"bee": "fight"}, "knobs": {"avoid_min_rarity": "Epic"}})
    assert classify_action("bee", "Unique") == "ENGAGE"


def test_drop_ignored_is_per_rarity():
    dets = [_det("rock", "Common", (1, 1)), _det("rock", "Legendary", (2, 2)), _det("rock", "Ultra", (3, 3)),
            _det("bee", "Common", (4, 4))]
    _ed.set_rules({"species": {"rock": {"Common": "ignore", "Ultra": "ignore"}}})
    assert [(d["species"], d["rarity"]) for d in _ed.drop_ignored(dets)] == [("rock", "Legendary"), ("bee", "Common")]


def test_drop_ignored_star_ignore_with_a_rarity_exception():
    dets = [_det("rock", r, (i, i)) for i, r in enumerate(("Common", "Epic", "Ultra"))]
    _ed.set_rules({"species": {"rock": {"*": "ignore", "Epic": "fight"}}})
    assert [d["rarity"] for d in _ed.drop_ignored(dets)] == ["Epic"]


def test_drop_ignored_copes_with_a_detection_that_has_no_rarity():
    det = {"species": "rock", "screen_pos": (1, 1)}
    _ed.set_rules({"species": {"rock": {"Common": "ignore"}}})
    assert _ed.drop_ignored([det]) == [det]                        # 不知道稀有度: 只有 "*" / 字符串才适用
    _ed.set_rules({"species": {"rock": {"*": "ignore"}}})
    assert _ed.drop_ignored([det]) == []


def test_set_and_get_rules_deep_copy_the_per_rarity_dicts():
    src = {"species": {"bee": {"Epic": "avoid"}}}
    _ed.set_rules(src)
    src["species"]["bee"]["Epic"] = "fight"
    src["species"]["bee"]["Common"] = "ignore"
    assert _ed.rule_for("bee", "Epic") == "avoid" and _ed.rule_for("bee", "Common") is None
    got = _ed.get_rules()
    got["species"]["bee"]["Epic"] = "fight"
    assert _ed.rule_for("bee", "Epic") == "avoid"
    assert _ed.get_rules() == {"species": {"bee": {"Epic": "avoid"}}, "knobs": {}}


@pytest.mark.parametrize("junk", [{"bee": 5}, {"bee": {"Epic": 5}}, {"bee": {"Epic": None}}, {"bee": []}, {"bee": {}}])
def test_junk_rule_values_never_crash_classification_or_dropping(junk):
    # set_rules 不校验(app_config 在更外面拦); 万一漏进来也只能按内置走, 不能崩
    _ed.set_rules({"species": junk})
    assert classify_action("bee", "Epic") == "ENGAGE" and classify_action("bee", "Ultra") == "AVOID"
    dets = [_det("bee", "Epic", (1, 1))]
    assert _ed.drop_ignored(dets) == dets


def test_a_per_rarity_avoid_triggers_flee_only_for_that_rarity():
    _ed.set_rules({"species": {"bee": {"Epic": "avoid"}}})
    action, positions = _nearest([_det("bee", "Epic", (1060, 540))])
    assert action == "flee" and positions == [(1060, 540)]
    assert _nearest([_det("bee", "Common", (1060, 540))])[0] == "chase"      # 同一物种别的稀有度照打


def test_a_per_rarity_fight_turns_only_that_static_ultra_from_a_flee_source_into_a_target():
    ultra = [_det("rock", "Ultra", (1060, 540))]
    assert _nearest(ultra)[0] == "flee"
    _ed.set_rules({"species": {"rock": {"Ultra": "fight"}}})
    assert _nearest(ultra)[0] == "chase"
    assert _nearest([_det("rock", "Super", (1060, 540))])[0] == "flee"        # Super 没配: 内置兜底还是躲


def test_a_per_rarity_ignore_removes_only_that_rarity_from_the_flee_decision():
    _ed.set_rules({"species": {"rock": {"Ultra": "ignore"}}})
    dets = _ed.drop_ignored([_det("rock", "Ultra", (1060, 540))])
    assert _nearest(dets) == ("wander", None)


# ── 谨慎: 怪太近就后退(2026-10-04) ───────────────────────────────────────────────
# 以前谨慎 = 追到 cautious_hold_px 就站桩, 怪再近也不动(录像: 站着被蜘蛛 / 机械花打死, 9 秒血量 0.68→0)。
# 现在: 目标比 hold×(1-CAUTIOUS_RETREAT_BAND) 还近就背离它走, 带内(hold×(1-带宽) ~ hold)站着, hold 外靠近。
_C = (960, 540)


def _cautious_at(dx, dy=0, species="spider", rarity="Mythic"):
    _ed.set_rules({"species": {"spider": "cautious", "wasp": "cautious"}})     # 一条用例里会混着造几只
    return _det(species, rarity, (_C[0] + dx, _C[1] + dy))


def _retreat_edge(hold):
    return hold * (1 - _ed.CAUTIOUS_RETREAT_BAND)


def test_a_cautious_target_closer_than_the_keep_band_is_backed_away_from():
    tgt = _cautious_at(100)                                   # 正右方 100px, 保持距离 300
    x, y = _ed.chase_move_target(tgt, 300, center=_C, max_extend=500)
    assert x < _C[0] and abs(y - _C[1]) < 1e-6                # 往左(背离), 不是原地站


def test_the_retreat_is_a_full_stride_not_a_hesitant_step():
    # 退得慢了怪照样追上; 步长跟追击一样, 是 max_extend 满幅(往远处瞄, 由 max_extend 限速)
    tgt = _cautious_at(100)
    assert _ed.chase_move_target(tgt, 300, center=_C, max_extend=500) == (_C[0] - 500, _C[1])


def test_a_cautious_target_inside_the_keep_band_is_stood_against():
    edge = _retreat_edge(300)
    for d in (edge, edge + 5, 300):                           # 带内含两个端点: 正好在边上不算太近
        assert _ed.chase_move_target(_cautious_at(d), 300, center=_C, max_extend=500) == _C


def test_a_cautious_target_just_inside_the_edge_already_triggers_the_retreat():
    assert _ed.chase_move_target(_cautious_at(_retreat_edge(300) - 1), 300,
                                 center=_C, max_extend=500) != _C


def test_a_cautious_target_beyond_the_keep_distance_is_still_approached():
    x, y = _ed.chase_move_target(_cautious_at(400), 300, center=_C, max_extend=500)
    assert x > _C[0]                                          # 还是往它走, 走到 hold 为止


def test_the_retreat_goes_directly_away_in_any_direction():
    for dx, dy in ((0, 100), (-100, 0), (0, -100), (70, 70), (-70, 70)):
        tgt = _cautious_at(dx, dy)
        x, y = _ed.chase_move_target(tgt, 300, center=_C, max_extend=500)
        vx, vy = x - _C[0], y - _C[1]
        assert vx * dx + vy * dy < 0                          # 跟目标方向相反
        assert abs(vx * dy - vy * dx) < 1e-6 * max(1, abs(vx), abs(vy))   # 且共线: 不是歪着退


def test_the_retreat_bends_away_from_other_dangerous_mobs_too():
    tgt = _cautious_at(100)                                   # 目标在右, 另一只危险怪在上方近处
    x, y = _ed.chase_move_target(tgt, 300, center=_C, max_extend=500,
                                 repel_positions=[(_C[0], _C[1] - 60)])
    assert x < _C[0]                                          # 仍然在退
    assert y > _C[1]                                          # 被上面那只往下推


def test_the_retreat_still_goes_somewhere_when_the_target_is_exactly_on_top_of_you():
    assert _ed.chase_move_target(_cautious_at(0), 300, center=_C, max_extend=500) != _C


def test_a_fight_target_is_never_backed_away_from_however_close():
    # 近战要贴着打: 打法是「打」的怪, 停步半径内再近也只是站着(跟以前一样)
    tgt = _det("soldier_ant", "Mythic", (_C[0] + 10, _C[1]))
    assert _ed.chase_move_target(tgt, 120, center=_C, max_extend=500) == _C
    _ed.set_rules({"species": {"soldier_ant": "fight"}})
    assert _ed.chase_move_target(tgt, 120, center=_C, max_extend=500) == _C


def test_the_builtin_cautious_ultra_sandstorm_backs_off_too():
    tgt = _det("sandstorm", "Ultra", (_C[0] + 100, _C[1]))    # 内置表里的 CAUTIOUS, 没设任何规则
    x, _y = _ed.chase_move_target(tgt, 500, center=_C, max_extend=500)
    assert x < _C[0]


def test_a_cautious_circling_species_keeps_circling_instead_of_the_straight_retreat():
    # 胡蜂 / 黄蜂 / 螳螂本来就绕着 hold_px 转(径向修正会把太近的往外推), 不走直线后退
    tgt = _cautious_at(100, species="wasp")
    got = _ed.chase_move_target(tgt, 300, center=_C, max_extend=500)
    assert got == _ed._strafe_target(tgt["screen_pos"], _C, 300, 500, _ed.STRAFE_K_RADIAL,
                                     zero_dir=(1.0, 0.0))


def test_is_cautious_retreat_agrees_with_what_chase_move_target_does():
    edge = _retreat_edge(300)
    cases = [(_cautious_at(100), 300, True), (_cautious_at(edge - 1), 300, True),
             (_cautious_at(edge), 300, False), (_cautious_at(300), 300, False),
             (_cautious_at(400), 300, False), (_cautious_at(100, species="wasp"), 300, False)]
    for tgt, hold, want in cases:
        assert _ed.is_cautious_retreat(tgt, hold, _C) is want
    _ed.set_rules(None)
    assert _ed.is_cautious_retreat(_det("soldier_ant", "Mythic", (_C[0] + 10, _C[1])), 120, _C) is False


@pytest.mark.parametrize("junk", [{"species": "spider"}, {"species": "spider", "rarity": "???"},
                                  {"species": "spider", "rarity": None}])
def test_a_target_without_a_known_rarity_is_not_retreated_from_and_nothing_crashes(junk):
    _ed.set_rules({"species": {"spider": "cautious"}})
    tgt = dict(junk, screen_pos=(_C[0] + 100, _C[1]))
    assert _ed.is_cautious_retreat(tgt, 300, _C) is False
    assert _ed.chase_move_target(tgt, 300, center=_C, max_extend=500) == _C


def test_the_retreat_band_is_a_sane_fraction():
    assert 0 < _ed.CAUTIOUS_RETREAT_BAND < 0.5


# select_action: 退得开, 前提是这只怪进得了候选池(以前 >300px 又没有视线的怪被丢回漫游)
def test_a_cautious_mob_in_the_retreat_zone_is_handled_even_beyond_the_chase_limit():
    _ed.set_rules({"species": {"spider": "cautious"}})
    dets = [_det("spider", "Mythic", (_C[0] + 400, _C[1]))]   # 400px: 超追击上限(300), 但比 500×0.85 近
    got = _nearest(dets, cautious_hold_px=500)
    assert got[0] == "chase" and got[1]["species"] == "spider" and got[2] == 500


def test_a_cautious_mob_in_the_stand_band_but_beyond_the_chase_limit_is_left_to_wander_as_before():
    _ed.set_rules({"species": {"spider": "cautious"}})
    dets = [_det("spider", "Mythic", (_C[0] + 450, _C[1]))]   # 450 > 500×0.85: 不用退, 也不够近去追
    assert _nearest(dets, cautious_hold_px=500) == ("wander", None)


def test_a_far_cautious_mob_still_does_not_pull_the_bot_in():
    _ed.set_rules({"species": {"spider": "cautious"}})
    assert _nearest([_det("spider", "Mythic", (_C[0] + 700, _C[1]))], cautious_hold_px=500) == ("wander", None)


def test_a_low_confidence_cautious_mob_is_not_retreated_from_by_select_action():
    # 低置信的框常是幻影, 不当目标(只当危险源, 老行为)
    _ed.set_rules({"species": {"spider": "cautious"}})
    dets = [_det("spider", "Mythic", (_C[0] + 400, _C[1]), conf=0.4)]
    assert _nearest(dets, cautious_hold_px=500) == ("wander", None)


def test_an_out_of_area_cautious_mob_in_the_retreat_zone_is_still_retreated_from():
    # 刷怪区外的怪不追, 但贴到谨慎距离里的照样要躲 —— 站着打不会把人带出区, 退也不会(退是往远处走)
    _ed.set_rules({"species": {"spider": "cautious"}})
    dets = [_det("spider", "Mythic", (_C[0] + 400, _C[1]))]
    got = _nearest(dets, cautious_hold_px=500, in_area=lambda d: False)
    assert got[0] == "chase"


def test_with_a_closer_fight_mob_around_the_decision_is_still_that_mob():
    # 最近的是「打」的怪: 照旧站着打它(谨慎怪只是排斥源, 老行为不变)
    _ed.set_rules({"species": {"spider": "cautious"}})
    dets = [_det("soldier_ant", "Mythic", (_C[0] + 60, _C[1])),
            _det("spider", "Mythic", (_C[0] - 200, _C[1]))]
    got = _nearest(dets, cautious_hold_px=500)
    assert got[0] == "chase" and got[1]["species"] == "soldier_ant"
    assert got[3] == [(_C[0] - 200, _C[1])]


def test_a_fight_mob_in_the_same_zone_is_not_pulled_into_the_pool_by_the_cautious_rule():
    # 后退区只给谨慎怪开门: 普通怪 400px 外照旧是漫游(追击上限 300), 别因为谨慎距离大就跟着被追
    assert _nearest([_det("soldier_ant", "Mythic", (_C[0] + 400, _C[1]))],
                    cautious_hold_px=500) == ("wander", None)


def test_the_retreat_zone_edge_in_select_action_follows_the_keep_distance():
    _ed.set_rules({"species": {"spider": "cautious"}})
    inside = _nearest([_det("spider", "Mythic", (_C[0] + 424, _C[1]))], cautious_hold_px=500)
    outside = _nearest([_det("spider", "Mythic", (_C[0] + 426, _C[1]))], cautious_hold_px=500)
    assert inside[0] == "chase" and outside == ("wander", None)
    # 保持距离是调用方给的(main 取旋钮), 不是写死的 500
    assert _nearest([_det("spider", "Mythic", (_C[0] + 424, _C[1]))], cautious_hold_px=300) == ("wander", None)


def test_a_missing_keep_distance_means_no_retreat_and_no_crash():
    tgt = _cautious_at(100)
    assert _ed.is_cautious_retreat(tgt, None, _C) is False
    assert _ed.chase_move_target(tgt, None, center=_C, max_extend=500) != _C   # hold=None: 一直往它贴(老行为)
