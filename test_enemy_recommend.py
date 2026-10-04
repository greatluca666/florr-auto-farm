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
