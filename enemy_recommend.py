"""按「流派 + 花瓣稀有度」推荐索敌旋钮(第③轮) —— 纯数据 + 纯函数, 只依赖 enemy_species。

输出是第②/③轮已有的旋钮(见 app_config.ENEMY_KNOB_KEYS), 不新增配置键: 子窗「按装备推荐」把它们填进
下面的控件, 用户还能手改, 点「确定」才落进时块的 enemy_rules。推荐值本身不存盘。

两部分, 来源不一样:

1. 躲避起点稀有度(avoid_min_rarity) = 花瓣稀有度 +1 档(用户 2026-10-03 定的换算: 打得过比花瓣高一档以内的
   怪, 再高的躲), 夹进旋钮的可选范围(下限 Epic)。花瓣 Mythic -> Ultra, 正好是内置默认。

2. 停步半径(hold_active_px / hold_passive_px) 按流派给。**近战不覆盖** —— 引擎内置值是蚁穴实测的贴身打法,
   不写覆盖, 引擎以后改默认也跟着走。远程 / 召唤的数是**占位, 没有任何实机数据**: 想让远程流派离怪远一点,
   具体远多少没人量过。改表就行(下面的 _STYLE_KNOBS), 测试守的是「远程 > 召唤 > 近战内置」这个顺序和值能过校验,
   不守具体数字。单位是屏幕像素, 随游戏 zoom 变(跟其余旋钮一样)。
"""
import enemy_species

STYLES = ("melee", "ranged", "summon")

# 花瓣稀有度的可选档: Common..Super。再往上没人配得出, 而且「+1」会走出稀有度表。
PETAL_RARITIES = tuple(enemy_species.RARITY_ORDER[:8])

# 推荐能设置的旋钮(都在 app_config.ENEMY_KNOB_KEYS 里)。子窗据此知道「推荐没给的那几项该清回默认」:
# 比如先套用远程、再套用近战, 近战不该留着远程的停步半径。
GOVERNED_KNOBS = ("avoid_min_rarity", "hold_active_px", "hold_passive_px")

# 占位数值, 未标定(见模块说明)。近战没有条目 = 全用内置值。
_STYLE_KNOBS = {
    "melee": {},
    "ranged": {"hold_active_px": 200, "hold_passive_px": 100},
    "summon": {"hold_active_px": 160, "hold_passive_px": 80},
}


def avoid_min_rarity_for(petal):
    """花瓣稀有度 -> 躲避起点稀有度: 高一档, 夹进可选范围(Common..Rare 都落到最低档 Epic)。"""
    if petal not in PETAL_RARITIES:
        raise ValueError(f"花瓣稀有度 {petal!r} 不认识, 可选: {', '.join(PETAL_RARITIES)}")
    tier = enemy_species.RARITY_ORDER[enemy_species.RARITY_ORDER.index(petal) + 1]
    return tier if tier in enemy_species.AVOID_MIN_RARITY_CHOICES \
        else enemy_species.AVOID_MIN_RARITY_CHOICES[0]


def recommend(style, petal):
    """(流派, 花瓣稀有度) -> 推荐的旋钮 dict(每次返回新 dict)。不认识的入参抛 ValueError, 不猜。"""
    if style not in STYLES:
        raise ValueError(f"流派 {style!r} 不认识, 可选: {', '.join(STYLES)}")
    out = {"avoid_min_rarity": avoid_min_rarity_for(petal)}
    out.update(_STYLE_KNOBS[style])
    return out
