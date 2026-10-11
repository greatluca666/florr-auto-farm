"""把一帧解码出来的画面编码成策略看的观测向量 —— 纯函数, 不碰 CDP。

**以自己为原点**: 怪和其他玩家都只给相对偏移, 策略不用去学地图的坐标系, 换张图也不用
重学。绝对世界坐标只保留一份粗归一化的, 让策略能分辨"在图的哪一片"(不同区域怪的密度和
稀有度不一样), 但不依赖它定位。

槽位按距离由近到远填, 填不满的尾部留零且 present 位清零 —— 那是网络该尊重的 mask。

## 跟当初设计的差异(有意的, 写在这)

设计里的自身块有 24 维, 含 5 个花瓣槽的 RGB(15 维)。**这里没有**, 原因:
`inventory_slots_from_frame` 还在 florragent 没搬过来, 而且它自己的文档记着一个实测
问题 —— 那个"固定 5 槽"的假设在花瓣栏扩展过的账号上(实测确认最多 20 槽)会失效, 光靠
几何分不出哪套 build 是当前生效的。与其编一个读不准的 15 维喂进去, 不如不给。
换上的是几个**今天真读得到**的量: loadout 奇偶、相机是否只是近似、周围怪的密度。
哪天把那个解码器搬过来并解决了扩展花瓣栏的问题, 再加回来。
"""
import math

import numpy as np

# 怪物种: canvas 解出来的是客户端语言的名字, 中文客户端就是中文。这里直接按名字索引 ——
# 不折成 slug, 因为 slug 那层映射(enemy_detect.SPECIES_NAMES + _NAME_OVERRIDES)是按图分流的,
# 换图就得改, 而这里要的是一张全局固定的表。
# 名字来源: 实拍帧 test_frames/ (沙漠) + florragent 的真实录像 (花园/蚁穴)。
SPECIES = (
    "沙尘暴", "仙人掌", "甲虫", "蝎子", "蜈蚣", "火兵蚁", "火蚁",       # 沙漠
    "岩石", "蒲公英", "蜜蜂", "熊蜂", "瓢虫", "幼蚁", "工蚁",           # 花园/蚁穴
)
UNKNOWN_SPECIES = len(SPECIES)
SPECIES_DIM = len(SPECIES) + 1
_SPECIES_INDEX = {name: i for i, name in enumerate(SPECIES)}

K_MOBS = 16
K_PLAYERS = 4

# 自身块的布局。用下标常量, 别在别处写死数字 —— 布局一变, 写死的下标会静默读错位。
# 2026-09-24 用户删掉了速度(原 4、5: 每拍位移 ÷ 1500 ≈ 0.005, 几乎看不见, 还随拍长抖)
# 和"附近有没有玩家"(原 6: 其他玩家第一个槽的 present 位已经是同一个信息)。
HP, SHIELD, POS_X, POS_Y, DANGER, PARITY, APPROX, MOB_COUNT = range(8)
PLAYER_DIM = 8
MOB_DIM = 6 + SPECIES_DIM
PLAYER_SLOT_DIM = 5
OBS_DIM = PLAYER_DIM + K_MOBS * MOB_DIM + K_PLAYERS * PLAYER_SLOT_DIM

MAP_NORM = 30000.0     # 世界坐标粗归一化; 实测坐标量级在几千到两万几
VIEW_RANGE = 1500.0    # 视野半径量级(世界单位), 怪的相对偏移按它归一
MAX_RANK = 9
LEVEL_NORM = 150.0     # 玩家等级归一; 实测见过 119/120 级
MOB_COUNT_NORM = 40.0  # 一屏怪数归一; 实测沙漠一屏见过 44 只
DANGER_WINDOW = 5      # 近几步的净掉血当"危险度"


def species_index(name):
    return _SPECIES_INDEX.get(name, UNKNOWN_SPECIES)


def rarity_norm(rank):
    """稀有度归一。认不出(-1/None) -> 0, 跟普通同值 —— 宁可低估不可高估: 高估会让策略
    去碰一只实际上很危险的怪。"""
    if rank is None or rank < 0:
        return 0.0
    return min(rank, MAX_RANK) / MAX_RANK


def danger_level(hp_history):
    """近 DANGER_WINDOW 步的净掉血(只取正)。没攒够历史 -> 0。

    给策略一个"最近在挨打"的信号 —— 当前血量本身看不出这个: 80% 血在掉和 80% 血在回,
    该做的事完全不一样。
    """
    if len(hp_history) < DANGER_WINDOW + 1:
        return 0.0
    return max(0.0, hp_history[-(DANGER_WINDOW + 1)] - hp_history[-1])


HIGH_RARITY_RANK = 5      # 神话及以上: 值得为它单独留一个槽


def _sorted_by_distance(items, px, py, k):
    out = []
    for it in items:
        dx, dy = it["x"] - px, it["y"] - py
        out.append((dx * dx + dy * dy, dx, dy, it))
    out.sort(key=lambda t: t[0])
    return out[:k]


def _select_mobs(mobs, px, py, k):
    """挑进观测的怪: **神话及以上全留**, 剩下的槽位才给近处的低稀有度怪。

    纯按距离取前 k 会出真问题 —— 实拍帧 test_frames/desert_ultra_44mobs.json 一屏 44 只,
    2 神话 + 1 究极全都比最近的 16 只更远, 于是策略根本看不见它们。而奖励是按稀有度加权
    的, 最值钱的目标反而不在观测里。(监视器的显示截断踩过同一个坑。)

    返回值仍按距离升序, 只是中间会跳过一些近处的低稀有度怪 —— 槽位"由近到远"这个
    不变量保住, 网络可以依赖它。
    """
    ranked = []
    for it in mobs:
        dx, dy = it["x"] - px, it["y"] - py
        ranked.append((dx * dx + dy * dy, dx, dy, it))
    ranked.sort(key=lambda t: t[0])

    def _rank(t):
        r = t[3].get("rarity_rank")
        return -1 if r is None else r

    high = [t for t in ranked if _rank(t) >= HIGH_RARITY_RANK][:k]
    rest = [t for t in ranked if _rank(t) < HIGH_RARITY_RANK][:max(0, k - len(high))]
    chosen = high + rest
    chosen.sort(key=lambda t: t[0])
    return chosen


def encode(player, mobs, others, *, hp_history=(), loadout_parity=0, camera_approx=False):
    """一帧 -> OBS_DIM 维 float32。

    player 为 None(菜单/死亡结算/解码失败)时返回全零向量, 但 camera_approx 位照样置上 ——
    "这一帧什么都看不见"本身就是策略该能分辨的状态, 而不是让调用方去猜。
    """
    obs = np.zeros(OBS_DIM, dtype=np.float32)
    obs[APPROX] = 1.0 if camera_approx else 0.0
    if player is None:
        return obs

    px, py = player["x"], player["y"]
    obs[HP] = player.get("hp") or 0.0
    obs[SHIELD] = player.get("hp_secondary") or 0.0
    obs[POS_X] = px / MAP_NORM
    obs[POS_Y] = py / MAP_NORM
    obs[DANGER] = danger_level(list(hp_history))
    obs[PARITY] = float(loadout_parity % 2)
    # obs[APPROX] 上面已经置过
    obs[MOB_COUNT] = min(len(mobs), MOB_COUNT_NORM) / MOB_COUNT_NORM

    for slot, (d2, dx, dy, mob) in enumerate(_select_mobs(mobs, px, py, K_MOBS)):
        base = PLAYER_DIM + slot * MOB_DIM
        obs[base + 0] = 1.0
        obs[base + 1] = dx / VIEW_RANGE
        obs[base + 2] = dy / VIEW_RANGE
        obs[base + 3] = math.sqrt(d2) / VIEW_RANGE
        obs[base + 4] = mob.get("hp") or 0.0
        obs[base + 5] = rarity_norm(mob.get("rarity_rank"))
        obs[base + 6 + species_index(mob.get("name"))] = 1.0

    pbase = PLAYER_DIM + K_MOBS * MOB_DIM
    for slot, (d2, dx, dy, other) in enumerate(_sorted_by_distance(others, px, py, K_PLAYERS)):
        base = pbase + slot * PLAYER_SLOT_DIM
        obs[base + 0] = 1.0
        obs[base + 1] = dx / VIEW_RANGE
        obs[base + 2] = dy / VIEW_RANGE
        obs[base + 3] = math.sqrt(d2) / VIEW_RANGE
        level = other.get("level")
        obs[base + 4] = (level or 0) / LEVEL_NORM

    return obs
