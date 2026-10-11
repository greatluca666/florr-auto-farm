"""按「流派 + 花瓣稀有度」推荐索敌旋钮(第③轮) —— 纯数据 + 纯函数, 只依赖 enemy_species。

输出是第②/③轮已有的旋钮(见 app_config.ENEMY_KNOB_KEYS), 不新增配置键: 子窗「按装备推荐」把它们填进
下面的控件, 用户还能手改, 点「确定」才落进时块的 enemy_rules。推荐值本身不存盘。

两部分, 来源不一样:

1. 躲避起点稀有度(avoid_min_rarity) = 花瓣稀有度 +1 档(用户 2026-10-03 定的换算: 打得过比花瓣高一档以内的
   怪, 再高的躲), 夹进旋钮的可选范围(下限 Epic)。花瓣 Mythic -> Ultra, 正好是内置默认。

   **但不是所有 Ultra 怪都该打 / 都该躲**(用户 2026-10-10: 黄蜂、蜘蛛躲没问题, 螃蟹完全可以打, 蚁穴和沙漠里也是
   一部分怪): 起点高过 Ultra 时(花瓣 Ultra -> Super), Ultra 全当普通怪打, 连内置要躲的 Ultra 蝎子 / 甲虫也一起放掉。
   所以推荐另外按**物种**给 Ultra 档的打法(species_rules_for, 见 TOP_TIER_RULES): 危险的躲 / 谨慎, 没列的照打。
   工厂那张表是实测的: 2026-10-10 三小时工厂录像(花瓣 Ultra、起点 Super), 分析到的 23 次死亡里 19 次死前 400px
   内有 Ultra 怪; 每千拍致死率 Ultra 黄蜂 60 / Ultra 蜘蛛 50, 对比 Mythic 同物种 16~18、Ultra 螃蟹只有 4
   (三小时只杀了 6 只 Ultra, 打它几乎没有收益)。沙漠那张是内置的 Ultra 表(用户标定过)。蚁穴只有零星受击事件、
   其余四张图没有任何录像, 是按物种习性(主动近战 / 远程射手躲, 被动 / 不动的照打)推的, 子窗里能手改。

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


# 物种规则能管的稀有度档: 起点(avoid_min_rarity)以下、Ultra 起的那几档。起点 <= Ultra 时 Ultra 本来就全躲, 不需要。
RULE_TIERS = ("Ultra", "Super")

# 每张图 Ultra(及以上同理)档的打法。没列的物种 = 不写规则 = 照起点打(起点以下就是正常打)。
# avoid = 躲, cautious = 打但保持距离。证据等级见模块说明。
TOP_TIER_RULES = {
    # 实测(2026-10-10 三小时录像)。机械花: 用户自己配的是谨慎(1 次死亡 / 39 次接触, 样本太小); 螃蟹照打。
    # 铀桶: 2026-10-11 那段录像里 Ultra 铀桶占了 8 次挨打里的 6 次、追了 49 拍, 当段唯一一次死亡就在它身边
    # (它受伤后放毒圈, 贴身打吃毒) —— 只有一段样本, 给谨慎(照打, 离远点)不给躲。
    "factory": {"avoid": ("wasp", "spider"), "cautious": ("mecha_flower", "barrel")},
    # 内置 Ultra 表(enemy_detect._AVOID_PAIRS / _CAUTIOUS_PAIRS, 用户标定过)。
    "desert": {"avoid": ("scorpion", "beetle"),
               "cautious": ("sandstorm", "cactus", "sand_centipede", "soldier_fire_ant")},
    # 兵蚁 / 蚁后是主动近战; 幼蚁 / 工蚁(被动)、蚁卵(不动)、蠕虫(有专门的绕圈打法)照打。
    "anthell": {"avoid": ("soldier_ant", "queen_ant")},
    # 下面四张图没有录像, 按维基习性推: 主动近战 / 远程射手躲, 被动 / 不动 / 螃蟹照打。
    "garden": {"avoid": ("soldier_ant", "queen_ant", "hornet", "spider", "wasp"),
               "cautious": ("mecha_flower",)},
    "ocean": {"avoid": ("jellyfish", "starfish", "leech", "soldier_ant")},
    "jungle": {"avoid": ("soldier_termite", "termite_overmind", "mantis", "wasp")},
    "sewers": {"avoid": ("spider", "silverfish")},
}

# 给子窗提示用: 这张图的规则靠什么定的。
TOP_TIER_EVIDENCE = {
    "factory": "三小时工厂录像实测", "desert": "内置 Ultra 打法表",
    "anthell": "零星录像 + 维基", "garden": "维基推测(没录像)", "ocean": "维基推测(没录像)",
    "jungle": "维基推测(没录像)", "sewers": "维基推测(没录像)",
}


def governed_species(map_name):
    """推荐会动的物种 slug(这张图 TOP_TIER_RULES 里列出来的)。子窗据此知道套用前该清回默认的格子。"""
    rules = TOP_TIER_RULES.get(map_name, {})
    return frozenset(rules.get("avoid", ())) | frozenset(rules.get("cautious", ()))


def species_rules_for(petal, map_name):
    """(花瓣稀有度, 地图) -> {slug: {稀有度档: "avoid" / "cautious"}}。只给**起点以下**的 Ultra 起的档
    (起点 <= Ultra 时 Ultra 本来就全躲, 返回空); 这张图没有表 -> 空。每次返回新 dict。"""
    order = enemy_species.RARITY_ORDER
    top = order.index(avoid_min_rarity_for(petal))
    tiers = [t for t in RULE_TIERS if order.index(t) < top]
    table = TOP_TIER_RULES.get(map_name)
    if not tiers or not table:
        return {}
    out = {}
    for state in ("avoid", "cautious"):
        for slug in table.get(state, ()):
            out[slug] = {t: state for t in tiers}
    return out


def recommend(style, petal):
    """(流派, 花瓣稀有度) -> 推荐的旋钮 dict(每次返回新 dict)。不认识的入参抛 ValueError, 不猜。"""
    if style not in STYLES:
        raise ValueError(f"流派 {style!r} 不认识, 可选: {', '.join(STYLES)}")
    out = {"avoid_min_rarity": avoid_min_rarity_for(petal)}
    out.update(_STYLE_KNOBS[style])
    return out
