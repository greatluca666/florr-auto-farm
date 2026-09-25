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
    # canvas 解出的名字随客户端语言; 中文客户端下 _DESERT_SPECIES 一个都不匹配,
    # 全靠 _SPECIES_ALIASES 折回 6 个 slug (否则每个沙漠怪都被丢掉)。
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    assert _species_from_name("沙尘暴") == "sandstorm"
    assert _species_from_name("仙人掌") == "cactus"
    assert _species_from_name("甲虫") == "beetle"
    assert _species_from_name("蝎子") == "scorpion"
    assert _species_from_name("蜈蚣") == "sand_centipede"
    assert _species_from_name("火兵蚁") == "soldier_fire_ant"
    assert _species_from_name("火蚁") == "soldier_fire_ant"      # 工蚁, 同归 soldier_fire_ant
    # 每个别名的 value 必须是 SPECIES_RANK 认得的 slug —— 否则 priority_score KeyError
    for slug in _ed._SPECIES_ALIASES.values():
        assert slug in _ed.SPECIES_RANK, slug


def test_species_from_name_rejects_non_desert_and_none(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    assert _species_from_name("Ladybug") is None      # English name, not a _SPECIES_ALIASES key
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


def test_scan_enemies_drops_non_desert_names(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    f_old = gameplay_frame(0, mobs=[(400.0, 200.0, "Ladybug", 1.0)])
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


def test_species_supported_only_desert_for_now():
    assert _ed.species_supported("desert") is True
    assert _ed.species_supported("anthell") is False
    assert _ed.species_supported("garden") is False
    assert _ed.species_supported("ocean") is False
    assert _ed.species_supported("nope") is False
    assert _ed.species_supported(None) is False


def test_species_from_name_still_works_on_desert():
    assert _ed._species_from_name("Scorpion", map_name="desert") == "scorpion"
    assert _ed._species_from_name("沙尘暴", map_name="desert") == "sandstorm"
    assert _ed._species_from_name("Sand Centipede", map_name="desert") == "sand_centipede"


def test_species_from_name_returns_none_on_unsupported_map():
    # 蚁穴还没做索敌: 蚂蚁类怪一律不认.
    assert _ed._species_from_name("Scorpion", map_name="anthell") is None
    assert _ed._species_from_name("Worker Ant", map_name="anthell") is None


def test_species_from_name_is_silent_on_unsupported_map(capsys):
    # 关键: 空表图上绝不能走"未识别怪物名"那条日志 —— 蚁穴每帧几十只蚂蚁,
    # 会把日志刷爆.
    _ed._seen_unknown_names.clear()
    for _ in range(3):
        _ed._species_from_name("Worker Ant", map_name="anthell")
    assert capsys.readouterr().out == ""
    assert _ed._seen_unknown_names == set()


def test_species_from_name_defaults_to_utils_MAP(monkeypatch):
    monkeypatch.setattr(_ed.utils, "MAP", "desert")
    assert _ed._species_from_name("Cactus") == "cactus"
    monkeypatch.setattr(_ed.utils, "MAP", "anthell")
    assert _ed._species_from_name("Cactus") is None


def test_alias_not_in_this_maps_species_is_rejected(monkeypatch):
    # 别名表是全局共用的, 但折出来的 slug 必须属于当前图, 否则等于把沙漠怪
    # 混进别的图. 蚁穴目前是空集合, `if not known` 那层 guard 会先一步拦下,
    # 根本轮不到别名判断那行(`_SPECIES_ALIASES[slug] in known`)—— 这行
    # 才是"折出来的 slug 必须属于当前图"真正的守门人. 所以额外借用
    # "anthell" 造一张非空、但不含 cactus 的物种表, 才能真正跑到那行去测.
    assert _ed._species_from_name("仙人掌", map_name="anthell") is None   # 空表: guard 先拦下

    monkeypatch.setitem(_ed.MAP_SPECIES, "anthell", frozenset({"scorpion"}))
    assert _ed._species_from_name("仙人掌", map_name="anthell") is None   # 非空但没 cactus: 别名判断本身拦下

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
