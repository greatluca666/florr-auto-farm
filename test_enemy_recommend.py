"""推荐值(第③轮): 流派 + 花瓣稀有度 -> enemy_rules 旋钮。纯数据模块, 不碰 cv2 / tk。"""
import pytest

import app_config
import enemy_recommend as rec
import enemy_species


def test_the_option_lists():
    assert rec.STYLES == ("melee", "ranged", "summon")
    # 花瓣档: Common..Super。再往上的花瓣档没人配得出, 而且「+1」会走出稀有度表
    assert rec.PETAL_RARITIES == ("Common", "Unusual", "Rare", "Epic", "Legendary",
                                  "Mythic", "Ultra", "Super")
    assert set(rec.PETAL_RARITIES) <= set(enemy_species.RARITY_ORDER)


# 花瓣档 +1 起躲(用户 2026-10-03 定的换算); 下限夹到旋钮可选范围的最低档 Epic。
# (一度封顶 Ultra, 用户 2026-10-10 指出「不是所有 Ultra 都要躲」 —— 改成按物种给规则, 见下面 species_rules_for。)
@pytest.mark.parametrize("petal, tier", [
    ("Common", "Epic"), ("Unusual", "Epic"), ("Rare", "Epic"),      # +1 落在 Epic 以下 -> 夹到 Epic
    ("Epic", "Legendary"), ("Legendary", "Mythic"), ("Mythic", "Ultra"),
    ("Ultra", "Super"), ("Super", "Eternal"),
])
def test_avoid_min_rarity_is_one_tier_above_the_petals(petal, tier):
    assert rec.avoid_min_rarity_for(petal) == tier


def test_mythic_petals_reproduce_todays_default():
    # 内置默认(Ultra)本来就是按「花瓣 Mythic」量出来的, 推荐值跟它对得上才说得通
    assert rec.avoid_min_rarity_for("Mythic") == enemy_species.AVOID_MIN_RARITY_DEFAULT


def test_melee_leaves_the_stop_radii_at_the_engine_defaults():
    # 近战 = 内置值: 内置的停步半径(蚁穴实测)本来就是贴身打法, 不写覆盖, 引擎以后改默认也跟着走
    assert rec.recommend("melee", "Legendary") == {"avoid_min_rarity": "Mythic"}


def test_ranged_and_summon_stand_further_back_than_melee_and_ranged_the_furthest():
    import enemy_detect
    melee, ranged, summon = (rec.recommend(s, "Legendary") for s in rec.STYLES)
    for key, builtin in (("hold_active_px", enemy_detect.ENGAGE_HOLD_PX),
                         ("hold_passive_px", enemy_detect.PASSIVE_HOLD_PX)):
        assert key not in melee
        assert ranged[key] > summon[key] > builtin


def test_the_style_does_not_change_the_tier_and_the_petals_do_not_change_the_radii():
    for petal in rec.PETAL_RARITIES:
        assert len({rec.recommend(s, petal)["avoid_min_rarity"] for s in rec.STYLES}) == 1
    for style in rec.STYLES:
        radii = {tuple(sorted((k, v) for k, v in rec.recommend(style, p).items()
                              if k != "avoid_min_rarity")) for p in rec.PETAL_RARITIES}
        assert len(radii) == 1


@pytest.mark.parametrize("style", rec.STYLES)
@pytest.mark.parametrize("petal", rec.PETAL_RARITIES)
def test_every_recommendation_is_something_the_config_layer_accepts(style, petal):
    # 输出契约: 推荐只用第②/③轮已有的旋钮, 并且值都过 app_config 的校验 —— 否则「套用」之后一点确定,
    # 保存时被 validate_config 打回来。
    out = rec.recommend(style, petal)
    assert set(out) <= set(rec.GOVERNED_KNOBS) <= set(app_config.ENEMY_KNOB_KEYS)
    for key, val in out.items():
        assert app_config._enemy_knob_ok(key, val), (key, val)
    cleaned = app_config._coerce_enemy_rules({"knobs": out}, "garden")
    assert cleaned["knobs"] == out                                   # 规整时一项都没被丢
    assert app_config._validate_enemy_rules({"knobs": out}, "garden", "x") == []


def test_governed_knobs_are_exactly_what_the_recommendation_can_set():
    seen = set()
    for style in rec.STYLES:
        for petal in rec.PETAL_RARITIES:
            seen |= set(rec.recommend(style, petal))
    assert seen == set(rec.GOVERNED_KNOBS)


def test_recommend_returns_a_fresh_dict_each_call():
    a = rec.recommend("ranged", "Epic")
    a["avoid_min_rarity"] = "never"
    a["hold_active_px"] = 1
    b = rec.recommend("ranged", "Epic")
    assert b["avoid_min_rarity"] == "Legendary" and b["hold_active_px"] != 1


@pytest.mark.parametrize("style, petal", [
    ("tank", "Epic"), ("", "Epic"), (None, "Epic"), ("melee", "Eternal"), ("melee", "epic"),
    ("melee", ""), ("melee", None), (["melee"], "Epic"),
])
def test_unknown_style_or_petal_is_a_value_error_not_a_guess(style, petal):
    with pytest.raises(ValueError):
        rec.recommend(style, petal)


# ── 按物种的 Ultra 档打法 ─────────────────────────────────────────────────────────
# 2026-10-10 用户: "不是所有的 Ultra 都要躲避, 黄蜂和蜘蛛躲避没问题, 但螃蟹完全可以打, 蚁穴和沙漠里也是一部分怪"。

def test_factory_with_ultra_petals_avoids_wasp_and_spider_but_fights_crab():
    rules = rec.species_rules_for("Ultra", "factory")
    assert rules["wasp"] == {"Ultra": "avoid"} and rules["spider"] == {"Ultra": "avoid"}
    assert "crab" not in rules                                      # 螃蟹照打(起点是 Super, Ultra < Super 就是正常打)
    assert rules["barrel"] == {"Ultra": "cautious"}                 # 2026-10-11 录像: Ultra 铀桶 8 次挨打占 6 次, 打但离远点
    assert rec.avoid_min_rarity_for("Ultra") == "Super"


def test_desert_with_ultra_petals_keeps_the_builtin_ultra_table():
    # 起点高过 Ultra 时内置的 Ultra 蝎子 / 甲虫躲、沙尘暴等谨慎会一起失效 —— 推荐把它们写成显式规则, 行为不丢
    import enemy_detect
    rules = rec.species_rules_for("Ultra", "desert")
    for slug, tier in enemy_detect._AVOID_PAIRS:
        assert rules[slug][tier] == "avoid"
    for slug, tier in enemy_detect._CAUTIOUS_PAIRS:
        assert rules[slug][tier] == "cautious"


def test_anthell_fights_the_harmless_ants_and_avoids_the_soldiers_and_queen():
    rules = rec.species_rules_for("Ultra", "anthell")
    assert set(rules) == {"soldier_ant", "queen_ant"}
    for harmless in ("baby_ant", "worker_ant", "ant_egg", "worm"):
        assert harmless not in rules


def test_no_species_rules_when_ultra_is_already_avoided_across_the_board():
    # 花瓣 <= Mythic: 起点 <= Ultra, Ultra 本来就全躲, 不用再逐物种写
    for petal in ("Common", "Rare", "Epic", "Legendary", "Mythic"):
        for map_name in rec.TOP_TIER_RULES:
            assert rec.species_rules_for(petal, map_name) == {}


def test_super_petals_get_rules_for_both_tiers_below_the_start():
    rules = rec.species_rules_for("Super", "factory")
    assert rules["wasp"] == {"Ultra": "avoid", "Super": "avoid"}
    assert rec.species_rules_for("Super", "factory")["mecha_flower"] == {"Ultra": "cautious", "Super": "cautious"}


def test_maps_without_a_table_get_nothing_and_fresh_dicts_come_back():
    assert rec.species_rules_for("Ultra", "nowhere") == {}
    a = rec.species_rules_for("Ultra", "factory")
    a["wasp"]["Ultra"] = "fight"
    assert rec.species_rules_for("Ultra", "factory")["wasp"] == {"Ultra": "avoid"}


@pytest.mark.parametrize("map_name", sorted(enemy_species.MAP_SPECIES))
def test_every_table_entry_is_a_real_species_of_that_map_and_a_valid_rule(map_name):
    # 表里的物种必须是这张图认得的(否则子窗里没这一行 / 配置校验会拒), 状态必须在打法循环里
    table = rec.TOP_TIER_RULES.get(map_name, {})
    assert map_name in rec.TOP_TIER_RULES and map_name in rec.TOP_TIER_EVIDENCE
    for state, slugs in table.items():
        assert state in ("avoid", "cautious")
        for slug in slugs:
            assert slug in enemy_species.MAP_SPECIES[map_name], (map_name, slug)
    assert rec.governed_species(map_name) == {s for slugs in table.values() for s in slugs}


def test_the_species_rules_pass_the_config_layer():
    for petal in ("Ultra", "Super"):
        for map_name in enemy_species.MAP_SPECIES:
            rules = rec.species_rules_for(petal, map_name)
            cfg = {"species": rules, "knobs": {"avoid_min_rarity": rec.avoid_min_rarity_for(petal)}}
            # 配置层逐条校验、不认的会丢掉(只打警告): 原样出来 = 一条都没被丢
            assert app_config._coerce_enemy_rules(cfg, map_name) == cfg, (petal, map_name)
