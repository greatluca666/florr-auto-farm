"""每张图上认得的怪物 slug + 稀有度档名 —— 纯数据, 不 import 任何东西。

app_config(GUI 启动时的 load_config、网页控制台的 validate_config)要用它校验 enemy_rules 里的物种
和 avoid_min_rarity 旋钮, 而 enemy_detect 会拉进 cv2 / cdp_bridge / canvas_decode / pyautogui,
所以数据单独放这里。enemy_detect.MAP_SPECIES / RARITY_ORDER 就是这里的同一个对象。
"""

# 稀有度档位, 低 -> 高; 下标就是 enemy_detect.RARITY_RANK。
RARITY_ORDER = [
    "Common", "Unusual", "Rare", "Epic", "Legendary",
    "Mythic", "Ultra", "Super", "Eternal", "Unique",
]

# 旋钮 avoid_min_rarity(第③轮): 没列规则的怪, 稀有度达到这一档才躲(见 enemy_detect.classify_action)。
# 默认 Ultra = 第②轮及以前写死的行为。"never" = 不按稀有度躲(物种规则「躲」仍然生效)。
# 可选范围只防手滑: 低于 Epic 的档(Common..Rare)基本等于每只怪都躲, 不给选。
AVOID_MIN_RARITY_DEFAULT = "Ultra"
AVOID_MIN_RARITY_NEVER = "never"
AVOID_MIN_RARITY_CHOICES = tuple(RARITY_ORDER[3:]) + (AVOID_MIN_RARITY_NEVER,)

# 每张图上认得的怪物 slug。值必须全部落在 SPECIES_RANK 和 SPECIES_NAMES 里, 否则
# priority_score() 会 KeyError / 认不出名字。空集合 = 这张图还没做索敌 —— _species_from_name()
# 一律返回 None 且**不刷"未识别"日志**(一屏几十只怪, 刷了会把日志淹掉), main 那边也会
# 把 enemy_ai_enabled 强制关掉(见 species_supported)。现在七张图都做了; 空集这条路留给以后新增的图。
#
# 给一张新图做索敌: 往它的集合里填 slug, 再补 SPECIES_RANK、SPECIES_NAMES(中英文名)
# 条目, 需要"最近优先"的话再加进 TARGET_POLICY —— 不用再动这里的结构。
MAP_SPECIES = {
    "desert": frozenset({"scorpion", "beetle", "cactus", "sandstorm",
                         "sand_centipede", "soldier_fire_ant"}),
    # 蚁穴 (2026-09-27 实机): 幼蚁/工蚁/兵蚁/蠕虫/蚁卵; queen_ant 是没抓到过的假设。
    "anthell": frozenset({"baby_ant", "worker_ant", "soldier_ant", "worm", "queen_ant",
                          "ant_egg"}),
    # 下面五张图(2026-10-01)**没有任何实机录像**: 物种表来自官方 .tmj 的 mobs 层 + florr 维基的
    # 生态区页面(怪物组成员读不出来, 以维基为准), 中英文名来自游戏本地化表。漏了的名字走
    # "未识别怪物名"日志, 贴出来补进 SPECIES_NAMES 就行。
    # 花园含工厂入口附近的机械怪(mecha_flower / wasp / crab; 机械蜘蛛同名 spider)。
    "garden": frozenset({"rock", "ladybug", "bee", "bumble_bee", "baby_ant", "worker_ant",
                         "soldier_ant", "queen_ant", "ant_egg", "ant_hole", "hornet", "spider",
                         "centipede", "dandelion", "mecha_flower", "wasp", "crab"}),
    "ocean": frozenset({"bubble", "shell", "crab", "jellyfish", "starfish", "sponge", "leech",
                        "soldier_ant"}),                        # 潜水兵蚁, 画布上同名「兵蚁」
    "jungle": frozenset({"baby_termite", "worker_termite", "soldier_termite", "termite_overmind",
                         "termite_egg", "termite_mound", "bush", "centipede", "firefly",
                         "ladybug", "leafbug", "mantis", "wasp", "crab"}),
    "sewers": frozenset({"spider", "roach", "moth", "fly", "silverfish", "garbage"}),
    "factory": frozenset({"mecha_flower", "spider", "wasp", "crab", "barrel"}),
}
