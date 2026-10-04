import functools
import math
import time

import utils
import cdp_bridge
import canvas_decode
from enemy_species import (
    MAP_SPECIES, RARITY_ORDER, AVOID_MIN_RARITY_DEFAULT, AVOID_MIN_RARITY_NEVER,
)


RARITY_RANK = {name: i for i, name in enumerate(RARITY_ORDER)}


# 数值越大优先级越高(故意跟RARITY_RANK同方向, 好用max()一起挑目标).
# sandstorm > cactus > beetle > scorpion > {sand_centipede, soldier_fire_ant}(并列最低)
SPECIES_RANK = {
    "sandstorm": 5,
    "cactus": 4,
    "beetle": 3,
    "scorpion": 2,
    "sand_centipede": 1,
    "soldier_fire_ant": 1,
    # 蚁穴: 走"最近优先"(TARGET_POLICY), 不看这张表 —— 放这儿只为满足
    # MAP_SPECIES ⊆ SPECIES_RANK 这条不变量(priority_score 不会 KeyError)。
    "baby_ant": 1,
    "worker_ant": 1,
    "soldier_ant": 1,
    "worm": 1,
    "queen_ant": 1,
    "ant_egg": 1,
    # 花园 / 海洋 / 丛林 / 下水道 / 工厂(2026-10-01): 同上, 都走"最近优先", 这张表只为满足
    # 不变量。选目标靠 SPECIES_TRAITS(停步半径 / 代价 / 绕圈), 不靠这里的数。
    "rock": 1, "ladybug": 1, "bee": 1, "bumble_bee": 1, "ant_hole": 1, "hornet": 1,
    "spider": 1, "centipede": 1, "dandelion": 1, "mecha_flower": 1, "wasp": 1, "crab": 1,
    "bubble": 1, "shell": 1, "jellyfish": 1, "starfish": 1, "sponge": 1, "leech": 1,
    "baby_termite": 1, "worker_termite": 1, "soldier_termite": 1, "termite_overmind": 1,
    "termite_egg": 1, "termite_mound": 1, "bush": 1, "firefly": 1, "leafbug": 1,
    "mantis": 1, "roach": 1, "moth": 1, "fly": 1, "silverfish": 1, "garbage": 1,
    "barrel": 1,
}

_AVOID_PAIRS = {("scorpion", "Ultra"), ("beetle", "Ultra")}
_CAUTIOUS_PAIRS = {
    ("sandstorm", "Ultra"), ("cactus", "Ultra"),
    ("sand_centipede", "Ultra"), ("soldier_fire_ant", "Ultra"),
}


# ── 用户自配规则(第②轮) ───────────────────────────────────────────────────────
# 时块里的 enemy_rules(见 app_config._coerce_enemy_rules): worker 启动时 main._apply_worker_config
# 调 set_rules() 装进来, 一个 worker 进程一份。空表 = 全部走内置行为(第①轮及以前的行为)。
#   species: slug -> 打法 或 {稀有度: 打法}
#     打法四种: "fight"(打)/ "cautious"(谨慎: 打, 但保持距离)/ "avoid"(躲)/ "ignore"(忽略)。
#     字符串 = 对所有稀有度生效(老写法); 字典 = 逐稀有度, 键是稀有度档名, "*" = 没单独配的稀有度的默认。
#     优先级: 该稀有度的格子 > "*" / 字符串 > 稀有度起点旋钮 / 内置表(classify_action)。
#     avoid / fight / cautious 在 classify_action 里覆盖; ignore 在 drop_ignored 里按(物种, 稀有度)剔除检测
#     (main._maybe_scan_enemies 扫描后立刻调, 所以 flee 规划 / 人群判定 / Mythic 锁定都看不见它)。
#   knobs: 旋钮, 用 knob(名字, 内置值) 取 —— 用户没设就是内置值。范围由 app_config 校验。
#     avoid_min_rarity 是档名不是数值: classify_action 用它决定「稀有度多高才躲」(默认 Ultra)。
_RULES = {"species": {}, "knobs": {}}


def _copy_species(species):
    """species 表的深一层拷贝(逐稀有度的字典也要拷, 不然调用方改了它, 引擎里的规则跟着变)。"""
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in species.items()}


def set_rules(rules):
    """整体替换用户规则(不合并)。None / 缺键 / 类型不对 = 清空。拷贝一份, 不持有调用方的 dict。"""
    rules = rules if isinstance(rules, dict) else {}
    species, knobs = rules.get("species"), rules.get("knobs")
    _RULES["species"] = _copy_species(species) if isinstance(species, dict) else {}
    _RULES["knobs"] = dict(knobs) if isinstance(knobs, dict) else {}


def get_rules():
    return {"species": _copy_species(_RULES["species"]), "knobs": dict(_RULES["knobs"])}


def rule_for(species, rarity=None):
    """这个(物种, 稀有度)的用户打法: "fight" / "cautious" / "avoid" / "ignore", 没设返回 None。
    该稀有度的格子优先, 其次 "*"(字符串写法就是 "*"); 不给 rarity 时只看 "*" / 字符串。"""
    v = _RULES["species"].get(species)
    if isinstance(v, dict):
        return v.get(rarity) or v.get("*")            # rarity=None 时 v.get(None) 就是 None, 只剩 "*"
    return v


def knob(name, default):
    """用户设的旋钮值, 没设返回 default。False 是合法值(swarm 关), 所以只认 None 为"没设"。"""
    v = _RULES["knobs"].get(name)
    return default if v is None else v


def drop_ignored(detections):
    """剔掉被用户设成 ignore 的(物种, 稀有度)。返回新列表, 不改入参。"""
    if not any(v == "ignore" or (isinstance(v, dict) and "ignore" in v.values())
               for v in _RULES["species"].values()):
        return list(detections)
    return [d for d in detections if rule_for(d["species"], d.get("rarity")) != "ignore"]


def _avoid_min_rank():
    """旋钮 avoid_min_rarity 换算成 RARITY_RANK 下标: 稀有度 >= 它才进「躲 / 谨慎」。
    "never" -> 比最高档还高(永远不到); 没设或值不认识(set_rules 不校验, 配置层在外面拦, 这里兜底)
    -> 默认档 Ultra, 不崩。"""
    v = knob("avoid_min_rarity", AVOID_MIN_RARITY_DEFAULT)
    if v == AVOID_MIN_RARITY_NEVER:
        return len(RARITY_ORDER)
    rank = RARITY_RANK.get(v) if isinstance(v, str) else None
    return rank if rank is not None else RARITY_RANK[AVOID_MIN_RARITY_DEFAULT]


def classify_action(species, rarity):
    """按(物种, 稀有度)分档: ENGAGE(正常接战)/CAUTIOUS(可打但保持距离)/
    AVOID(不打, 触发规避)。稀有度低于「躲避起点」(旋钮 avoid_min_rarity, 默认 Ultra)全 ENGAGE;
    达到起点: 内置表里 Ultra 档蝎子/甲虫 AVOID, 沙尘暴/仙人掌/沙蜈蚣/火蚁 CAUTIOUS, 其余一律
    AVOID —— 包括比 Ultra 还稀有的(Super/Eternal/Unique, 实测这个刷怪区不会刷新这个档位)和起点
    被调低后多出来的 Epic..Mythic 档: 失败方向选"别惹", 不选"谨慎打"。内置表只写了 Ultra 档, 所以
    起点调高到 Ultra 以上时 Ultra 的内置躲 / 谨慎一并失效(用户选的: Ultra 当普通怪打),
    调低时内置表照旧只在 Ultra 档生效。

    用户规则(set_rules)优先于稀有度起点和内置表: 这个(物种, 稀有度)设成 avoid → AVOID, fight → ENGAGE,
    cautious → CAUTIOUS(打, 但在 cautious_hold_px 外保持距离); 规则可以是整个物种的, 也可以逐稀有度
    (见 rule_for)。ignore 不在这里处理(检测在 drop_ignored 里就被剔掉了), 万一漏进来按内置分类。"""
    rule = rule_for(species, rarity)
    if rule == "avoid":
        return "AVOID"
    if rule == "fight":
        return "ENGAGE"
    if rule == "cautious":
        return "CAUTIOUS"
    if RARITY_RANK[rarity] < _avoid_min_rank():
        return "ENGAGE"
    if (species, rarity) in _AVOID_PAIRS:
        return "AVOID"
    if (species, rarity) in _CAUTIOUS_PAIRS:
        return "CAUTIOUS"
    return "AVOID"


def priority_score(species, rarity):
    """排序键, 数值越大优先级越高. 稀有度档位是第一比较项(碾压式), 物种优先级
    只在同稀有度档位时当平手规则."""
    return (RARITY_RANK[rarity], SPECIES_RANK[species])


# ── Mythic 近身处理 ("先清青怪") ──────────────────────────────────────────
# 青怪 = Mythic 档 (青 = Mythic 名牌的青色). 沙漠的 6 种怪里, sandstorm 是刷怪
# 目标本身, 不进这套 —— 其余 5 种每种按下面的策略走位磨死.
MYTHIC_KITE_SPECIES = {
    "beetle": "strafe",           # 直冲型, 垂直环绕让它打空
    "soldier_fire_ant": "strafe",
    "scorpion": "ram",            # 直接撞
    "sand_centipede": "ram",
    "cactus": "hold",             # 站桩带刺, 保持距离在旁边
}

# 多个 Mythic 同时在场时先处理谁 (用户给的顺序, 只用于 Mythic 锁定, 跟 SPECIES_RANK
# 那套普通追击优先级无关).
MYTHIC_TARGET_RANK = {
    "beetle": 5,
    "soldier_fire_ant": 4,
    "scorpion": 3,
    "sand_centipede": 2,
    "cactus": 1,
}


def mythic_candidates(detections, chase_min_conf=None):
    """从 detections 里挑出够格进 Mythic 锁定池的: rarity 是 Mythic、species 在
    MYTHIC_KITE_SPECIES (sandstorm 排除)、置信度过 chase_min_conf (同追击的幻影框
    过滤). 返回列表, 可能为空."""
    if chase_min_conf is None:
        chase_min_conf = CHASE_MIN_CONF
    return [
        d for d in detections
        if d.get("rarity") == "Mythic"
        and d.get("species") in MYTHIC_KITE_SPECIES
        and d.get("confidence", 1.0) >= chase_min_conf
    ]


SCREEN_CENTER = (utils.SCREEN_WIDTH / 2, utils.SCREEN_HEIGHT / 2)  # 屏幕中心, 同时也是
                              # "停止移动"的鼠标位置约定(见utils.keyup()) ——
                              # aim_mouse_target/flee_mouse_target在"保持距离"/
                              # "没有明确方向"时都退回这个值, 调用方(main.py)靠跟这个
                              # 常量比较来判断"这tick是不是故意停住"。跟utils.py共用
                              # 同一份SCREEN_WIDTH/SCREEN_HEIGHT, 不再自己独立写死一份.


_last_center = None   # 最近一次扫描解出的玩家屏幕锚点; None = 不可信, 退回 SCREEN_CENTER
_last_player_world = None   # 同一次扫描里自己的世界坐标(小地图点反解); 给 ApproachTracker 用


def last_player_world():
    """最近一次扫描里自己的世界坐标; 解不出 / 相机只是近似 -> None。"""
    return _last_player_world


def current_center():
    """做距离判定时该用的"玩家在画面上的位置"。

    SCREEN_CENTER 只是个近似: florr 在地图边界会顶住相机不再跟随, 玩家就不在画布
    中心了(实测 desert_mythic_26mobs.json 偏 162px); 而且画布只有在 Chrome 全屏
    (生产环境的 --start-fullscreen)时才等于屏幕 —— 用普通窗口调试时实测画布是
    1920x945, 玩家 y=472.5, 拿 SCREEN_CENTER 的 540 当玩家位置垂直方向恒定差 67.5px。

    这个偏差不是无害的: 拿那一帧实测, 有 13 条距离判定会被它翻转, 其中包括一只神话
    沙尘暴 —— 错误中心下算出 303px(< avoid_trigger 400, 会掉头逃), 真实中心下是
    417px(不该逃)。

    所以 scan_enemies 每次都把解出来的锚点记下来, 这里回。解不出 / 相机只是近似
    (approx, 实测那种锚点会落在别的实体身上) / 扫描抛错 -> 退回 SCREEN_CENTER。
    """
    return SCREEN_CENTER if _last_center is None else _last_center


def pick_mythic_target(detections, center=SCREEN_CENTER, latched=False,
                       engage_px=450, release_px=600, chase_min_conf=None,
                       prev_pos=None):
    """挑这一 tick 要处理的那只 Mythic. 搜索半径: 已锁定用 release_px (放宽, 迟滞),
    没锁定用 engage_px. 半径内没有合格 Mythic → None.

    没锁定 / 没给 prev_pos: 按 (MYTHIC_TARGET_RANK, 离屏幕中心近) 取最高.
    已锁定且给了 prev_pos: 目标位置连续性优先 —— 取离 prev_pos 最近的候选, 只有当
    另一个候选 MYTHIC_TARGET_RANK 严格更高 (真来了更值得打的) 才切过去, 同 rank
    之间仍按离 prev_pos 近取. 免得两只同 rank Mythic 因亚像素抖动每 tick 翻 180°."""
    radius = release_px if latched else engage_px
    cx, cy = center

    def dist(d):
        px, py = d["screen_pos"]
        return math.hypot(px - cx, py - cy)

    in_range = [d for d in mythic_candidates(detections, chase_min_conf=chase_min_conf)
                if dist(d) <= radius]
    if not in_range:
        return None

    if latched and prev_pos is not None:
        ppx, ppy = prev_pos

        def dist_prev(d):
            px, py = d["screen_pos"]
            return math.hypot(px - ppx, py - ppy)

        nearest = min(in_range, key=dist_prev)
        best_rank = max(MYTHIC_TARGET_RANK[d["species"]] for d in in_range)
        if best_rank > MYTHIC_TARGET_RANK[nearest["species"]]:
            better = [d for d in in_range
                      if MYTHIC_TARGET_RANK[d["species"]] == best_rank]
            return min(better, key=dist_prev)
        return nearest

    return max(in_range, key=lambda d: (MYTHIC_TARGET_RANK[d["species"]], -dist(d)))


def mythic_move_target(target, center=SCREEN_CENTER, *, strafe_radius, cactus_hold_px,
                       max_extend=None, repel_positions=None, k_radial=0.8):
    """按 target 的物种策略算这一 tick 鼠标该移到哪:
      ram   (蝎子/蜈蚣)   —— 直接朝目标全速贴, 等同 aim_mouse_target(hold_px=None)
      hold  (仙人掌)       —— 远于 hold*1.15 逼近; 近于 hold*0.85 沿 -u 后撤;
                             中间沿垂直方向 perp 绕圈
      strafe(甲虫/火蚁)    —— 垂直环绕 perp + 朝 strafe_radius 的径向修正
                             (d>r 往里带, d<r 往外推), 归一化后 ×max_extend
    perp 取固定一侧 (-u_y, u_x). d==0 无方向 → 返回 center."""
    if max_extend is None:
        max_extend = 500 * utils.mouse_scale()
    policy = MYTHIC_KITE_SPECIES.get(target["species"], "ram")
    px, py = target["screen_pos"]
    cx, cy = center
    vx, vy = px - cx, py - cy
    d = math.hypot(vx, vy)
    if d == 0:
        return center
    ux, uy = vx / d, vy / d
    perp = (-uy, ux)

    if policy == "ram":
        return aim_mouse_target(target["screen_pos"], hold_px=None, center=center,
                                max_extend=max_extend, repel_positions=repel_positions)

    if policy == "hold":
        if d > cactus_hold_px * 1.15:
            return aim_mouse_target(target["screen_pos"], hold_px=None, center=center,
                                    max_extend=max_extend, repel_positions=repel_positions)
        if d < cactus_hold_px * 0.85:
            dx, dy = -ux, -uy            # 后撤
        else:
            dx, dy = perp               # 绕圈
        return (cx + dx * max_extend, cy + dy * max_extend)

    # policy == "strafe"
    return _strafe_target(target["screen_pos"], center, strafe_radius, max_extend, k_radial)


def _strafe_target(target_pos, center, radius, max_extend, k_radial, zero_dir=None):
    """绕着 target_pos 转圈: 垂直方向 perp + 朝 radius 的径向修正(d>r 往里带, d<r 往外推),
    归一化后 ×max_extend。perp 取固定一侧 (-u_y, u_x)。
    d==0 没方向: 给了 zero_dir 就朝它走(蠕虫 —— 站着不动正好让它钻出来), 否则返回 center。"""
    px, py = target_pos
    cx, cy = center
    vx, vy = px - cx, py - cy
    d = math.hypot(vx, vy)
    if d == 0:
        if zero_dir is None:
            return center
        return (cx + zero_dir[0] * max_extend, cy + zero_dir[1] * max_extend)
    ux, uy = vx / d, vy / d
    perp = (-uy, ux)
    radial = (d - radius) / radius * k_radial
    dx = perp[0] + ux * radial
    dy = perp[1] + uy * radial
    m = math.hypot(dx, dy)
    if m < 1e-6:
        return center
    return (cx + dx / m * max_extend, cy + dy / m * max_extend)


def aim_mouse_target(target_pos, hold_px=None, center=SCREEN_CENTER, max_extend=None,
                     repel_positions=None, repel_px=None, repel_gain=1.6):
    """把目标的屏幕坐标换算成鼠标该移到的位置 —— 纯屏幕坐标系计算, 跟
    move_to_position()那套小地图坐标系是两套独立空间, 不能互相传参数。
    hold_px设了值时, 一旦已经进到这个距离内就不再继续靠近(退回屏幕中心, 停止
    输出"继续接近"的方向), 给CAUTIOUS档的怪用; hold_px=None时无视距离上限一直
    往目标方向贴(只按max_extend限速度), 给ENGAGE档用。

    repel_positions给了值时(一串危险怪的屏幕坐标), 会往"远离它们"的方向叠一个
    排斥分量到追击方向上 —— 追归追, 但路径绕开半路的危险怪, 不是直直怼过去。
    只有危险怪进到repel_px以内才起作用, 越近推得越狠(线性衰减×repel_gain);
    repel_positions为空/None时行为跟以前完全一样。合成方向被排斥力抵消到约0 →
    这一tick退回屏幕中心(停一下), 等下一帧重新算。

    max_extend默认None时按1920x1080参照值500乘utils.mouse_scale()换算 —— 跟
    utils.keydown()的delta是同一种"1920x1080量出来的屏幕转向距离"，同样需要
    按分辨率缩放。repel_px默认None时同理按参照值450换算。显式传值(比如测试里传
    500)会跳过默认换算, 保持既有调用点行为不变。"""
    if max_extend is None:
        max_extend = 500 * utils.mouse_scale()
    tx, ty = target_pos
    cx, cy = center
    dx, dy = tx - cx, ty - cy
    dist = math.hypot(dx, dy)
    if dist == 0:
        return center

    rx, ry = 0.0, 0.0
    if repel_positions:
        if repel_px is None:
            repel_px = 450 * utils.mouse_scale()
        for px, py in repel_positions:
            adx, ady = cx - px, cy - py
            ad = math.hypot(adx, ady)
            if ad == 0 or ad >= repel_px:
                continue
            w = (1.0 - ad / repel_px) * repel_gain
            rx += adx / ad * w
            ry += ady / ad * w

    if hold_px is not None and dist <= hold_px:
        # 已经进到CAUTIOUS保持距离内: 平时就停(退回中心), 但半路有危险怪在推 →
        # 这一tick还是往远离危险的方向挪一下, 别傻站着被撞。
        if rx == 0.0 and ry == 0.0:
            return center
        rmag = math.hypot(rx, ry)
        step = min(max_extend, repel_px)
        return (cx + rx / rmag * step, cy + ry / rmag * step)

    ux, uy = dx / dist + rx, dy / dist + ry
    umag = math.hypot(ux, uy)
    if umag < 1e-6:
        return center
    extend = min(dist, max_extend)
    return (cx + ux / umag * extend, cy + uy / umag * extend)


# 躲究极时别撞进去的怪群(flee_planner 的 crowd): 传奇及以上、会还手的。被动的幼蚁 / 不动的
# 蚁卵不算。第五份录像: 背离究极的方向上正好 12~14 只传奇兵蚁, 撞进去 1.2 秒满血到 0。
FLEE_CROWD_MIN_RARITY = "Legendary"
FLEE_CROWD_PASSIVE = frozenset({"baby_ant", "ant_egg", "bubble"})   # 泡泡几乎无害(海洋)


def is_flee_crowd(det):
    return (det["species"] not in FLEE_CROWD_PASSIVE
            and RARITY_RANK.get(det["rarity"], 0) >= RARITY_RANK[FLEE_CROWD_MIN_RARITY]
            and classify_action(det["species"], det["rarity"]) != "AVOID")


def flee_mouse_target(avoid_positions, center=SCREEN_CENTER, extend=None):
    """算所有AVOID怪的排斥力合向量, 换算成鼠标该移到的位置(往远离它们的方向)。
    合力互相抵消成约0向量(比如两个AVOID怪分别在玩家两侧)时没有明确逃离方向,
    退回屏幕中心 —— 等同于"停止移动", 跟utils.keyup()把鼠标收回中心停止移动是
    同一个约定。

    extend默认None时按1920x1080参照值400乘utils.mouse_scale()换算, 理由同
    aim_mouse_target的max_extend。"""
    if extend is None:
        extend = 400 * utils.mouse_scale()
    cx, cy = center
    fx, fy = 0.0, 0.0
    for px, py in avoid_positions:
        dx, dy = cx - px, cy - py
        dist = math.hypot(dx, dy)
        if dist == 0:
            continue
        fx += dx / dist
        fy += dy / dist
    mag = math.hypot(fx, fy)
    if mag < 0.05:
        return center
    return (cx + fx / mag * extend, cy + fy / mag * extend)


CHASE_STALL_WINDOW = 25  # tick, ≈1.25s @ time.sleep(0.05); 判"追击途中卡住"看的时间窗


def chase_is_stalled(pos_history, min_progress=4.0, window=CHASE_STALL_WINDOW):
    """追击/规避途中判断是否真的卡住了 —— 看整段时间窗内玩家的**净位移**, 不是
    看相邻两tick挪了多少。追一个会走位的目标时, 相邻tick位移小是常态(绕圈、
    微调), 旧写法(相邻tick差<1.5就+1, 连续15次就脱困)会把正常追击误判成卡住、
    半路触发execute_anti_stuck()把玩家怼向目标。改成: 攒满一个window的位置样本
    后, 窗口首尾净位移 < min_progress(minimap坐标单位)才算卡住 —— 贴墙被顶住
    净位移≈0, 正常追击哪怕绕圈净位移也会累积过阈值。

    pos_history: 调用方维护的近期minimap坐标列表(get_player_position()的返回值,
    不是屏幕坐标), 最新的在末尾。样本不足一个window → 返回False(还没攒够, 不判)。
    只返回bool(该不该让步脱困), 不再回传计数 —— 状态在调用方那个列表里。"""
    if pos_history is None or len(pos_history) < window:
        return False
    x0, y0 = pos_history[-window]
    x1, y1 = pos_history[-1]
    return math.hypot(x1 - x0, y1 - y0) < min_progress


CHASE_MIN_CONF = 0.55  # 只有置信度到这个数的检测框才够格当"追击目标". 0.4~0.55
                        # 那档框经常是幻影(半透明沙尘暴边缘、影子), 拿它当目标就是
                        # 朝空气全速冲. 危险怪(AVOID/CAUTIOUS)不受此限 —— 宁可对着
                        # 一个可能不存在的强怪多绕一下, 不能漏躲。


# 蚁穴"最近优先"模式下, 目标进到这个屏幕像素半径内就原地不动(鼠标回中心)打它。
# 停住的 tick 不会往卡住检测里记样本, 所以不会在怪堆里被误判"卡住"。未标定: 太小 =
# 一直贴着目标挪, 太大 = 够不着; 先取小值, 实机按花瓣够得着的距离调。
ENGAGE_HOLD_PX = 120

# 最近优先模式只追这个屏幕像素半径内的怪, 再远的交回漫游。未标定(先取 300, 实机调)。
# 2026-09-27 实机: 蚁穴刷怪区是一条 y 86~93 的窄带, 直线最近的怪常常隔着墙或在带外,
# 追过去顶墙 -> "索敌中途中卡住", 出带 -> "离开刷怪区域" 拉回来, 来回空转; 规避途中
# 卡住还直接死了一次。限制半径 = 只处理够得着的, 不追远处的。
CHASE_MAX_PX = 300
# 300px 外、这个半径内的怪, 调用方给的 can_reach 说"走得到"(在刷怪区里 + 小地图上直线没墙)
# 也追。2026-09-29 用户: 刷怪时怪"看不见" —— 录像里刷怪时 14~22% 的拍子在漫游, 其中 85%
# 以上最近的可打怪在 300~600px, 画面上看得见, 只是被上面那个 300 一刀切掉了。
FAR_CHASE_PX = 600

# 蚁群 = 一堆挤在一起的蚂蚁(用户 2026-09-27): 有就先打蚁群, 不直接冲进去, 保持距离、
# 近了就退(蚂蚁追上来撞在花瓣圈上)。以下全是**未标定**的初值, 实机调:
SWARM_MIN_COUNT = 5         # 至少这么多只(非 AVOID)挤在一起才算蚁群
SWARM_RADIUS_PX = 250       # "挤在一起" = 都在某一只的这个屏幕半径内
SWARM_CHASE_MAX_PX = 600    # 蚁群中心离玩家这么远以内才去; 比单只的 CHASE_MAX_PX
                            # 放宽(用户要"率先"打蚁群), 但仍设上限 —— 太远的多半隔着墙
SWARM_KEEP_PX = 80          # 跟蚁群里最近那只保持的距离。2026-09-27 蚁穴帧(zoom 0.315)里
                            # 正在掉血的兵蚁离玩家 36~82px, 所以花瓣够得着的大约在这以内
SWARM_KEEP_BAND = 0.15      # 距离在 KEEP×(1±BAND) 之间就停着打, 出了这个带才进/退

# 谨慎(CAUTIOUS)怪保持的距离(屏幕像素)默认值: main 传给 select_action 的 cautious_hold_px 取用户旋钮
# cautious_px, 没设就是它。250 实测太近, 等于直接撞上去(沙漠 Ultra 沙尘暴)。
CAUTIOUS_HOLD_PX = 500
# 追到 hold 就停; 目标比 hold×(1-CAUTIOUS_RETREAT_BAND) 还近就背离它走(以前站桩, 怪贴到脸上也不动 ——
# 录像: 站着被机械花 / 蜘蛛打死, 9 秒血量 0.68→0)。hold×(1-带宽) ~ hold 之间是站桩带, 不进不退, 免得在边界上
# 一拍进一拍退地抖。0.15 跟蚁群保持距离的 SWARM_KEEP_BAND 同一个数, **未标定**。
CAUTIOUS_RETREAT_BAND = 0.15

# AVOID 怪规避触发半径的默认值(屏幕像素)。main.AVOID_TRIGGER_PX 取它, select_action 的默认实参取它,
# GUI 索敌设置的 placeholder 也读它。
DEFAULT_AVOID_TRIGGER_PX = 400

# 每张图的 AVOID 怪规避触发半径(屏幕像素)。不在表里的图用 DEFAULT_AVOID_TRIGGER_PX。
# 蚁穴: 用户 2026-09-27 嫌 400 太远(究极兵蚁一出现就跑, 刷不了怪), 改 200。
AVOID_TRIGGER_PX_BY_MAP = {"anthell": 200}


def avoid_trigger_px_for(map_name, default):
    return AVOID_TRIGGER_PX_BY_MAP.get(map_name, default)


# 究极"冲过来"就提前躲的半径(屏幕像素)。用户 2026-09-28 定: 400px 内且正在朝你冲过来就躲,
# 站着 / 闲逛的不管, AVOID_TRIGGER_PX_BY_MAP 那个半径内一律躲。录像依据: 究极兵蚁追人的速度
# ≈ 自己(~300 世界单位/秒), 200px 才起跑甩不掉, 两次死亡都是这样。不在表里的图不提前躲。
AVOID_EARLY_PX_BY_MAP = {"anthell": 400}
APPROACH_CLOSING_MIN = 120.0   # 世界单位/秒: 相对距离缩得比这快 = 冲过来(追人时 ~300)
APPROACH_WINDOW_S = 0.3        # 两次读数至少隔这么久才算速度, 单帧抖动不算
APPROACH_MATCH_WORLD = 250.0   # 前后两次扫描, 同物种、世界坐标这么近的算同一只
APPROACH_TTL_S = 1.5           # 这么久没再见到就忘掉


def avoid_early_px_for(map_name):
    return AVOID_EARLY_PX_BY_MAP.get(map_name)


class ApproachTracker:
    """跨扫描跟踪 AVOID 怪跟自己的相对距离, 给检测打 approaching 标记。

    用世界坐标(scan_enemies 给的 "world" + 自己的 player_world), 不用屏幕坐标: 屏幕跟着
    自己动, 同一只怪在屏幕上的位置每拍都在变, 按世界坐标认同一只才稳。相对距离缩短就算
    "冲过来" —— 怪冲过来和自己朝它走过去都算, 两种都该躲。"""

    def __init__(self):
        self._tracks = []

    def reset(self):
        self._tracks = []

    def update(self, detections, player_world, now):
        live = [tr for tr in self._tracks if now - tr["seen"] <= APPROACH_TTL_S]
        used = set()
        for d in detections:
            d["approaching"] = False
            if classify_action(d["species"], d["rarity"]) != "AVOID":
                continue
            w = d.get("world")
            if w is None or player_world is None:
                continue
            dist = math.hypot(w[0] - player_world[0], w[1] - player_world[1])
            pool = [tr for tr in live if id(tr) not in used and tr["species"] == d["species"]
                    and math.hypot(tr["world"][0] - w[0], tr["world"][1] - w[1])
                    <= APPROACH_MATCH_WORLD]
            if pool:
                tr = min(pool, key=lambda tr: math.hypot(tr["world"][0] - w[0],
                                                        tr["world"][1] - w[1]))
            else:
                tr = {"species": d["species"], "hist": []}
                live.append(tr)
            used.add(id(tr))
            tr["world"], tr["seen"] = w, now
            tr["hist"] = [h for h in tr["hist"] if now - h[0] <= 1.0] + [(now, dist)]
            old = [h for h in tr["hist"] if now - h[0] >= APPROACH_WINDOW_S]
            if old:
                t0, d0 = old[-1]
                d["approaching"] = (d0 - dist) / (now - t0) >= APPROACH_CLOSING_MIN
        self._tracks = live


# ── 物种打法: 蚁穴的维基资料 + 全图共用的物种特性表 ──────────────────────────
# 依据 florr 维基(official-florrio.fandom.com, 2026-09 查): 同稀有度下
#            血量   体伤   神话经验  行为
#   幼蚁     0.25×  1×    8164     被动, 慢, 不还手
#   工蚁     0.6×   1×    4105     中立, 挨打才追(追到你死)
#   兵蚁     1×     1×    5437     主动, 仇恨范围全游戏最小, 走开一段就不追; 单只好打、成群危险
#   蚁后     2.5×   1×    4425     主动, 判定圈比外观大(没碰到也掉血), 边走边下蛋孵低一档兵蚁
#   蠕虫     1×     3×    3680     主动, 钻地移动、从目标脚下冒出; 维基:「打蠕虫永远别停下」
#   蚁卵     -      -     -        不动, 打碎 30% 孵出同档主动幼蚁
# (倍数以兵蚁为 1; 数值见 docs/superpowers/specs/2026-09-27-anthell-enemy-rules-design.md)
#
# ── 物种特性表(2026-10-01 起全图共用; 以前是三张蚁穴专用的按物种表) ────────────────
# SPECIES_TRAITS[slug] 只写偏离默认的字段:
#   kind         "active" 会自己凑过来 / "passive" 被动或中立, 不会主动来 / "static" 不动。默认 active。
#   hold_px      停步半径(屏幕像素), 覆盖 kind 的默认。默认: active 用 select_action 的 engage_hold_px
#                (ENGAGE_HOLD_PX), passive / static 用 PASSIVE_HOLD_PX。主动怪会自己凑过来,
#                停在原地等它就行; 被动/中立/不动的不会, 停在花瓣够不着的地方(掉血的兵蚁实测离玩家
#                36~82px, 见 SWARM_KEEP_PX)就永远打不到, 所以要贴到花瓣圈里。
#   tactic       "approach" 逼近到停步半径就停(默认) / "strafe" 以 tactic_px 为半径绕圈, 永远不停下。
#   tactic_px    strafe 的绕圈半径(屏幕像素); 绕圈时它就是 select_action 返回的 hold_px。
#   cost         还没接上战时挑谁: 距离 × cost 小的先打。蚁穴那几个按同档"经验/血量"(兵蚁=1: 幼蚁 6.2、
#                工蚁 1.3、蠕虫 0.7、蚁后 0.3)压缩到 0.5~1.5 —— 只在远近差不多时改主意, 不会为了
#                幼蚁跑过半个屏幕。默认 1.0。
#   swarm        算不算蚁群成员。默认: 会主动凑过来且不绕圈的(active 且 approach)。蚁穴的幼蚁 / 工蚁 /
#                蚁卵显式写 True —— 这三种一直算(用户要"蚁卵也算怪"), 保持蚁穴行为不变。
#   priority_px  这么近就压过蚁群先打(蠕虫: 站着遛蚁群 = 等它从脚下钻出来)。
#   stall_exempt 追击这个物种时不往卡住检测里记样本(main._drive_and_check_stall 的 track_stall=False)。
#                只给蠕虫: 它是不动的, 绕圈半径 60px 只合 ~3 格小地图, 净位移低于 chase_is_stalled 的
#                4 格, 喂进去必定误判。远程射手(黄蜂/胡蜂/螳螂)会飞、半径 120px, 不豁免 —— 绕进墙角
#                要能脱困; 代价是绕圈净位移若低于阈值也会被误判卡住、触发 execute_anti_stuck 乱跳
#                (阈值和半径都没标定, 看第一份新图录像)。默认 False。
# 全部**未标定**。蚁穴六种(其中 soldier_ant 全默认, 表里没有它的行)是 2026-09-27 起按维基 + 实机调的, 新图的见本表下半。
PASSIVE_HOLD_PX = 50
STRAFE_K_RADIAL = 0.8           # 同 main.MYTHIC_STRAFE_K_RADIAL
ANTHELL_WORM_STRAFE_PX = 60     # 冒出地面的蠕虫是不动的, 绕圈半径得在花瓣够得着的范围内
ANTHELL_WORM_PRIORITY_PX = 200
# 远程射手(黄蜂 / 胡蜂 / 螳螂)的绕圈半径。**占位**: 单位是屏幕像素, 随 zoom 变 —— 蚁穴(zoom
# 0.315)里花瓣只够到约 80px, 沙漠甲虫绕圈半径是 180; 新图的 zoom 没量过, 取中间 120, 等录像调。
STRAFE_SHOOTER_PX = 120

SPECIES_TRAITS = {
    "baby_ant": {"kind": "passive", "cost": 0.5, "swarm": True},
    "worker_ant": {"kind": "passive", "cost": 0.9, "swarm": True},
    # 蚁卵不动、经验不明, 排最后(用户要"蚁卵也算怪", 所以仍会打, 只是不抢先)。
    "ant_egg": {"kind": "static", "cost": 1.5, "swarm": True},
    "queen_ant": {"hold_px": 180, "cost": 1.5},   # 判定圈比外观大: 兵蚁那个 120 已经在挨她的体伤了
    "worm": {"tactic": "strafe", "tactic_px": ANTHELL_WORM_STRAFE_PX, "cost": 1.2,
             "priority_px": ANTHELL_WORM_PRIORITY_PX, "stall_exempt": True},

    # ── 花园 / 海洋 / 丛林 / 下水道 / 工厂(2026-10-01): 没有任何实机录像, 依据是维基文字描述。
    # 没列出来的物种全默认(active + approach + cost 1.0): spider crab jellyfish starfish leech
    # silverfish soldier_termite termite_overmind mecha_flower —— 主动怪, 自己凑过来。
    # 被动 / 中立: 到处晃, 挨打才追(维基 Passive / Neutral), 不会自己过来, 要贴进去打。
    "ladybug": {"kind": "passive"},
    "bee": {"kind": "passive"},
    "bumble_bee": {"kind": "passive"},
    "centipede": {"kind": "passive"},
    "shell": {"kind": "passive"},
    "roach": {"kind": "passive"},               # 维基 Neutral, 同工蚁
    "leafbug": {"kind": "passive"},
    "baby_termite": {"kind": "passive"},
    "worker_termite": {"kind": "passive"},
    "firefly": {"kind": "passive", "hold_px": 80},          # 接触放电, 不贴脸
    "moth": {"kind": "passive", "cost": 2.0},               # 被打会逃, 低优先
    "bubble": {"kind": "passive", "cost": 2.5},             # 几乎无害, 死时还会推开别的怪
    # 不动。
    "rock": {"kind": "static"},
    "bush": {"kind": "static"},
    "sponge": {"kind": "static"},
    "dandelion": {"kind": "static"},
    "termite_egg": {"kind": "static"},
    "garbage": {"kind": "static", "cost": 0.8},             # 会持续放苍蝇, 先清
    "barrel": {"kind": "static", "hold_px": 90, "cost": 3.0},   # 受伤后放毒圈, 最后才打, 停远一点
    "ant_hole": {"kind": "static", "cost": 1.5},            # 打碎出一窝怪, 不抢先
    "termite_mound": {"kind": "static", "cost": 1.5},
    # 主动但打得费劲。
    "fly": {"cost": 2.0},                                   # 90% 闪避物理伤害
    # 远程射手: 绕圈(黄蜂 Epic+ 预判射击, 必须变向才躲得掉; 胡蜂导弹走弧线; 螳螂的豌豆
    # "横向微动容易瞄偏")。
    "hornet": {"tactic": "strafe", "tactic_px": STRAFE_SHOOTER_PX},
    "wasp": {"tactic": "strafe", "tactic_px": STRAFE_SHOOTER_PX},
    "mantis": {"tactic": "strafe", "tactic_px": STRAFE_SHOOTER_PX},
}


def _traits(species):
    return SPECIES_TRAITS.get(species, {})


def tactic_for(species):
    """"approach"(默认)或 "strafe"。"""
    return _traits(species).get("tactic", "approach")


def hold_px_for(species, engage_hold_px=None):
    """这个物种停下来打(strafe 的物种是绕圈半径)的屏幕像素距离。

    顺序: strafe 的绕圈半径 > 物种自带 hold_px > 按 kind 的默认。kind 默认里: active 用
    engage_hold_px(没传 = 运行时取用户旋钮 hold_active_px, 再没有就是 ENGAGE_HOLD_PX), passive /
    static 取用户旋钮 hold_passive_px(没有就是 PASSIVE_HOLD_PX)。物种自带值不被旋钮覆盖。
    默认实参是 None 而不是 ENGAGE_HOLD_PX: 后者在定义时就绑死了, 旋钮改了它不会跟。"""
    t = _traits(species)
    if t.get("tactic") == "strafe":
        return t["tactic_px"]
    if "hold_px" in t:
        return t["hold_px"]
    if t.get("kind", "active") == "active":
        if engage_hold_px is not None:
            return engage_hold_px
        return knob("hold_active_px", ENGAGE_HOLD_PX)
    return knob("hold_passive_px", PASSIVE_HOLD_PX)


def cost_for(species):
    return _traits(species).get("cost", 1.0)


def counts_for_swarm(species):
    t = _traits(species)
    if "swarm" in t:
        return t["swarm"]
    return t.get("kind", "active") == "active" and t.get("tactic", "approach") != "strafe"


def priority_px_for(species):
    """None = 这个物种不压过蚁群。"""
    return _traits(species).get("priority_px")


def stall_exempt_for(species):
    """True = 追击这个物种时不记卡住样本(见 SPECIES_TRAITS 说明; 目前只有蠕虫)。"""
    return _traits(species).get("stall_exempt", False)


def _step_away(vx, vy, center, max_extend, repel_positions):
    """朝 (vx, vy) 方向满幅走一步(调用方保证它不是零向量)。反方向上取个远点当"目标", 步长由
    aim_mouse_target 的 max_extend 限住; 半路有危险怪(repel_positions)就顺带绕开。蚁群的"近了就退"和
    谨慎的"太近就后退"共用。"""
    cx, cy = center
    m = math.hypot(vx, vy)
    far = 10000.0
    return aim_mouse_target((cx + vx / m * far, cy + vy / m * far), hold_px=None, center=center,
                            max_extend=max_extend, repel_positions=repel_positions)


def _is_cautious_target(target):
    """这个目标的打法是 CAUTIOUS(用户设的「谨慎」或内置表)。稀有度缺 / 不认识就不猜, 当不是 ——
    classify_action 拿不认识的稀有度会 KeyError, 一个坏检测不能把整拍索敌拖垮。"""
    rarity = target.get("rarity")
    return rarity in RARITY_RANK and classify_action(target["species"], rarity) == "CAUTIOUS"


def is_cautious_retreat(target, hold_px, center=SCREEN_CENTER):
    """这一拍该不该背离 target 走: 它是谨慎怪(绕圈型除外 —— 它们绕着 hold_px 转, 径向修正自己会往外推),
    且离 center 比 hold_px×(1-CAUTIOUS_RETREAT_BAND) 还近。main 据此在后退时避开墙、改状态文字。"""
    if hold_px is None or tactic_for(target["species"]) == "strafe" or not _is_cautious_target(target):
        return False
    d = math.hypot(target["screen_pos"][0] - center[0], target["screen_pos"][1] - center[1])
    return d < hold_px * (1 - CAUTIOUS_RETREAT_BAND)


def chase_move_target(target, hold_px, center=SCREEN_CENTER, *, max_extend=None,
                      repel_positions=None):
    """普通追击这一 tick 鼠标该移到哪。绕圈打的物种(tactic == "strafe": 蠕虫、黄蜂/胡蜂/螳螂)绕着它转、
    hold_px 当半径, 任何距离都不返回 center(包括正好在脚下); 其余等同 aim_mouse_target(到 hold_px
    内就停)。绕圈跟 Mythic 锁定的 strafe 一样不看 repel —— 进了规避半径的 AVOID 怪由 flee 管。

    例外: 打法是谨慎(CAUTIOUS)的目标, 比 hold_px×(1-CAUTIOUS_RETREAT_BAND) 还近就背离它走
    (is_cautious_retreat), 半路的危险怪照样绕开; 带内站着, hold_px 外照旧靠近。"""
    if is_cautious_retreat(target, hold_px, center):
        cx, cy = center
        vx, vy = cx - target["screen_pos"][0], cy - target["screen_pos"][1]
        if math.hypot(vx, vy) < 1e-6:
            vx, vy = 1.0, 0.0             # 正好压在脚下没有"背离"的方向: 横着走, 总比站着强(同绕圈的回退)
        return _step_away(vx, vy, center, max_extend, repel_positions)
    if tactic_for(target["species"]) == "strafe":
        if max_extend is None:
            max_extend = 500 * utils.mouse_scale()
        # 正好在脚下时横着走: 这个回退最初是给蚁穴蠕虫写的(蚁穴刷怪区是一条横向窄带, 竖着走会顶墙),
        # 现在所有绕圈物种共用 —— 新图的刷怪区形状没看过, 先沿用。
        return _strafe_target(target["screen_pos"], center, hold_px, max_extend,
                              STRAFE_K_RADIAL, zero_dir=(1.0, 0.0))
    return aim_mouse_target(target["screen_pos"], hold_px=hold_px, center=center,
                            max_extend=max_extend, repel_positions=repel_positions)


SWARM_NEAR_SLACK_PX = 100   # prefer_near: 最近一只比最贴身那堆远不到这么多的, 都算"一样近"


def find_swarm(dets, center, radius_px=SWARM_RADIUS_PX, min_count=SWARM_MIN_COUNT,
               max_center_px=None, prefer_near=False):
    """一堆挤在一起的怪: {"center", "nearest", "count"}; 凑不够 min_count 只 -> None.

    以每只为圆心数 radius_px 内有几只, 取最多的那堆(同样多取离玩家近的)。center 是
    那堆的平均位置, nearest 是那堆里离玩家(center 参数)最近的那只的屏幕坐标。

    max_center_px: 只在中心离玩家这么近的堆里挑。不给的话会挑全屏最大的那堆 —— 第六份录像
    02:10:27, 身边 7 只神话/传奇兵蚁, 左边 870px 外另有一堆 11 只; 挑中了远的那堆, 调用方
    嫌它太远不去, 于是退回"追最近一只", 人就站在 7 只中间被打死。

    prefer_near: 只在"最近一只"跟最贴身那堆差不到 SWARM_NEAR_SLACK_PX 的堆里比只数。
    遛蚁群要躲的是贴身那堆: 同一拍 370px 外有一堆 7 只, 贴身 76px 那堆只有 5 只, 按只数会去
    遛远的那堆, 身边那只神话兵蚁没人管。"""
    cx, cy = center
    piles = []
    for d in dets:
        px, py = d["screen_pos"]
        members = [m["screen_pos"] for m in dets
                   if math.hypot(m["screen_pos"][0] - px, m["screen_pos"][1] - py) <= radius_px]
        if len(members) < min_count:
            continue
        mx = sum(p[0] for p in members) / len(members)
        my = sum(p[1] for p in members) / len(members)
        center_dist = math.hypot(mx - cx, my - cy)
        if max_center_px is not None and center_dist > max_center_px:
            continue
        nearest = min(members, key=lambda p: math.hypot(p[0] - cx, p[1] - cy))
        piles.append({"near_dist": math.hypot(nearest[0] - cx, nearest[1] - cy),
                      "key": (len(members), -center_dist),
                      "swarm": {"center": (mx, my), "nearest": nearest, "count": len(members)}})
    if not piles:
        return None
    if prefer_near:
        closest = min(p["near_dist"] for p in piles)
        piles = [p for p in piles if p["near_dist"] <= closest + SWARM_NEAR_SLACK_PX]
    return max(piles, key=lambda p: p["key"])["swarm"]


def swarm_move_target(swarm, center=SCREEN_CENTER, *, keep_px=SWARM_KEEP_PX,
                      band=SWARM_KEEP_BAND, max_extend=None, repel_positions=None):
    """打蚁群时这一 tick 鼠标该移到哪: 保持距离, 近了就退.

    看的是蚁群里**离玩家最近那只**的距离 d:
      d > keep×(1+band)  朝蚁群中心靠过去(不是朝最近那只 —— 往中间收, 而不是被边上一只带跑)
      d < keep×(1-band)  往远离蚁群中心的方向退; 人正站在中心上时, 往远离最近那只的方向退
      中间               停着(返回 center), 让追上来的蚂蚁撞花瓣
    停着的 tick 返回 center, _drive_and_check_stall 不记卡住样本。"""
    cx, cy = center
    nx, ny = swarm["nearest"]
    d = math.hypot(nx - cx, ny - cy)
    if d > keep_px * (1 + band):
        return aim_mouse_target(swarm["center"], hold_px=None, center=center,
                                max_extend=max_extend, repel_positions=repel_positions)
    if d >= keep_px * (1 - band):
        return center
    sx, sy = swarm["center"]
    vx, vy = cx - sx, cy - sy
    if math.hypot(vx, vy) < 1e-6:
        vx, vy = cx - nx, cy - ny
    m = math.hypot(vx, vy)
    if m < 1e-6:
        return center
    return _step_away(vx, vy, center, max_extend, repel_positions)


# 每张图的选目标策略: "priority" = 按稀有度/物种优先级挑, 只追 Mythic+ (沙漠);
# "nearest" = 有蚁群先打蚁群, 否则谁离玩家最近追谁, 不看稀有度 (蚁穴: 一帧 25+ 只神话兵蚁, 按稀有度
# 挑会每 tick 都在追, 跟沙漠那个"密集刷怪区死循环"是同一个坑)。
# 2026-10-01: 花园/海洋/丛林/下水道/工厂也走 nearest —— 沙漠那套"只追神话+"依赖沙漠专属的
# SPECIES_RANK 和清青怪走位, 套不到别的图上; 这五张图的数值全部未标定。
TARGET_POLICY = {"anthell": "nearest", "garden": "nearest", "ocean": "nearest",
                 "jungle": "nearest", "sewers": "nearest", "factory": "nearest"}


def target_policy_for(map_name):
    return TARGET_POLICY.get(map_name, "priority")


def select_action(detections, avoid_trigger_px=DEFAULT_AVOID_TRIGGER_PX, cautious_hold_px=250,
                  center=SCREEN_CENTER, chase_min_conf=CHASE_MIN_CONF,
                  target_policy="priority", engage_hold_px=None,
                  chase_max_px=CHASE_MAX_PX, swarm_min_count=SWARM_MIN_COUNT,
                  swarm_radius_px=SWARM_RADIUS_PX, swarm_chase_max_px=SWARM_CHASE_MAX_PX,
                  avoid_early_px=None, can_reach=None, far_chase_px=FAR_CHASE_PX,
                  in_area=None, swarm_enabled=True):
    """每tick的索敌决策入口. detections是scan_enemies()给的检测列表(或测试里
    手搭的同结构字典列表). 返回三选一:
      ("flee", avoid_positions)             —— 触发半径内有AVOID怪, 优先规避
      ("chase", target, hold_px, repel)     —— 有值得专门追的目标(稀有度>=Mythic,
                                               或Ultra档CAUTIOUS怪); repel是半路要
                                               绕开的危险怪坐标(AVOID全部 + 除目标外
                                               的CAUTIOUS), 传给aim_mouse_target当
                                               排斥源
      ("wander", None)                      —— 没有到Mythic档的目标, 交回随机漫游
      ("swarm", swarm, repel)               —— 只在 target_policy="nearest": 有蚁群
                                               (find_swarm), 交给 swarm_move_target 遛
    target_policy="nearest"(蚁穴 / 花园 / 海洋 / 丛林 / 下水道 / 工厂): 物种在 priority_px
    (SPECIES_TRAITS, 现在只有蠕虫)内就先打它(hold_px 是绕圈半径); 否则先看蚁群
    (swarm_chase_max_px 内, 只数 ENGAGE 且 counts_for_swarm 的怪); 再否则 chase_max_px 内挑一只:
    已进自己停步半径的取最近, 都没进的取 距离×cost 最小。不看稀有度、不设Mythic门槛。hold_px 按物种
    (hold_px_for: 看 SPECIES_TRAITS, 表外的物种用 engage_hold_px —— 没传(None)时取用户旋钮 hold_active_px,
    再没有就是 ENGAGE_HOLD_PX; CAUTIOUS怪仍用cautious_hold_px)。
    flee优先、AVOID不进候选池这两条两种模式都一样。 swarm_enabled=False(用户旋钮 swarm 关)时不找蚁群,
    蚁群成员当单只处理。
    AVOID怪永远进不了"chase"候选池, 哪怕它稀有度算下来优先级最高。追击目标还要
    过chase_min_conf置信度关; 没过关的ENGAGE直接丢, 没过关的AVOID/CAUTIOUS仍算
    危险源(进flee判定/repel), 只是不当追击目标。

    ★ chase只留给Mythic及以上(和Ultra CAUTIOUS): 密集刷怪区(比如沙尘暴区)每tick
    都有Common/传奇沙尘暴当最高分候选, 早先版本每tick都返回chase去追它 —— 结果
    auto_farming永远走chase分支、从不进wander, 对着一个被打死/新刷/乱动的目标
    原地微振, chase_is_stalled误判"卡住"触发execute_anti_stuck乱跳, 整轮
    move_count=0一点没刷. Common..传奇这些交回wander撞怪 + 外部"一直攻击"就够了,
    不值得专门追。"""
    avoid_positions = []
    charging = []          # 打了 approaching 标记的 AVOID 怪(ApproachTracker)
    cautious_dets = []
    candidates = []
    for d in detections:
        bucket = classify_action(d["species"], d["rarity"])
        conf = d.get("confidence", 1.0)
        if bucket == "AVOID":
            avoid_positions.append(d["screen_pos"])
            if d.get("approaching"):
                charging.append(d["screen_pos"])
            continue
        if bucket == "CAUTIOUS":
            cautious_dets.append(d)
        if conf >= chase_min_conf:
            candidates.append((d, bucket))

    if avoid_positions:
        cx, cy = center
        in_range = [
            p for p in avoid_positions
            if math.hypot(p[0] - cx, p[1] - cy) <= avoid_trigger_px
        ]
        # 冲过来的究极: 在更大的 avoid_early_px 内就躲(见 AVOID_EARLY_PX_BY_MAP)
        if avoid_early_px:
            in_range += [p for p in charging if p not in in_range
                         and math.hypot(p[0] - cx, p[1] - cy) <= avoid_early_px]
        if in_range:
            return ("flee", in_range)

    if target_policy == "nearest":
        # 最近优先: 不看稀有度, 不设 Mythic 门槛 —— AVOID 怪早在上面被挡在候选池外。
        # 只追 chase_max_px 内的; 没有 -> 漫游(不能掉进下面按稀有度那套去追远处的神话)。
        # 物种打法(见 SPECIES_TRAITS 上面的说明): 带 priority_px 的物种(蠕虫)近处先打它;
        # 蚁群只数 counts_for_swarm 的怪; 已经在打的不换; 还没接上战时按 距离×cost 挑。
        cx, cy = center

        def _dist(pair):
            return math.hypot(pair[0]["screen_pos"][0] - cx, pair[0]["screen_pos"][1] - cy)

        def _hold(pair):
            if pair[1] == "CAUTIOUS":
                return cautious_hold_px
            return hold_px_for(pair[0]["species"], engage_hold_px)

        def _prioritized(pair):
            px = priority_px_for(pair[0]["species"])
            return px is not None and _dist(pair) <= px

        if in_area is not None:
            # 刷怪区外的怪不追(也不算进蚁群) —— 第四份录像 8 次出区全是追着带边 / 带外的怪
            # 出去的。已经贴到停步半径里的(正在交手)照打: 站着打不会被带出去。
            candidates = [pair for pair in candidates
                          if _dist(pair) <= _hold(pair) or in_area(pair[0])]
        prioritized = [pair for pair in candidates if _prioritized(pair)]
        if prioritized:
            pool = prioritized
        else:
            repel = list(avoid_positions) + [d["screen_pos"] for d in cautious_dets]
            swarm = (find_swarm([d for d, b in candidates
                                 if b == "ENGAGE" and counts_for_swarm(d["species"])],
                                center, swarm_radius_px, swarm_min_count,
                                max_center_px=swarm_chase_max_px, prefer_near=True)
                     if swarm_enabled else None)
            if (swarm is not None and math.hypot(swarm["center"][0] - cx,
                                                 swarm["center"][1] - cy) <= swarm_chase_max_px):
                return ("swarm", swarm, repel)
            # 谨慎怪进了后退区(比保持距离×(1-带宽)还近)就一定要处理, 不论超没超追击上限 ——
            # 否则保持距离 500 > 追击上限 300 时, 300~425px 的怪被丢回漫游, 退都不退。
            pool = [pair for pair in candidates
                    if _dist(pair) <= chase_max_px
                    or (can_reach is not None and _dist(pair) <= far_chase_px
                        and can_reach(pair[0]))
                    or (pair[1] == "CAUTIOUS"
                        and _dist(pair) < cautious_hold_px * (1 - CAUTIOUS_RETREAT_BAND))]
        if not pool:
            return ("wander", None)
        engaged = [pair for pair in pool if _dist(pair) <= _hold(pair)]
        if engaged:
            best, best_bucket = min(engaged, key=_dist)
        else:
            best, best_bucket = min(
                pool, key=lambda pair: _dist(pair) * cost_for(pair[0]["species"]))
        hold_px = _hold((best, best_bucket))
        repel = list(avoid_positions)
        repel += [d["screen_pos"] for d in cautious_dets if d is not best]
        return ("chase", best, hold_px, repel)

    if candidates:
        best, best_bucket = max(
            candidates,
            key=lambda pair: priority_score(pair[0]["species"], pair[0]["rarity"]))
        # max()按稀有度档优先, 所以best没到Mythic档 == 所有候选都没到. 没到就不追,
        # 交回wander(见docstring里的"密集刷怪区死循环"). Ultra CAUTIOUS(rank 6)恒
        # >= Mythic, 保留"对Ultra沙尘暴/仙人掌保持距离接战"这条.
        if RARITY_RANK[best["rarity"]] >= RARITY_RANK["Mythic"]:
            hold_px = cautious_hold_px if best_bucket == "CAUTIOUS" else None
            # 半路危险源: 所有AVOID怪(不管在不在flee触发半径内 —— 402px的Ultra蝎子
            # 不该触发flee, 但追别的怪时也不能直直穿过它) + 除目标外的CAUTIOUS怪。
            repel = list(avoid_positions)
            repel += [d["screen_pos"] for d in cautious_dets if d is not best]
            return ("chase", best, hold_px, repel)

    return ("wander", None)


# MAP_SPECIES 在 enemy_species.py(纯数据, app_config 也要用), 上面 import 进来的是同一个 dict 对象。


def species_supported(map_name):
    """这张图做了索敌没有。没做 -> main._apply_worker_config 强制关索敌 AI,
    GUI 时块编辑器里那个开关也置灰(gui_schedule._sync_enemy_enabled)。"""
    return bool(MAP_SPECIES.get(map_name))


# slug -> 游戏本地化表里的英文名 / 中文名。取自 florr client.wasm 数据段的
# `Mobs/<slug>/Name=...`(2026-10-01 抠出; 每种语言各一份表), 不是按译名猜的。
# canvas 解出的名字随客户端语言, 英文和中文都认。同一个名字在不同图可以是不同的怪
# (沙漠蜈蚣 = sand_centipede / 花园蜈蚣 = centipede), 所以名字只在**本图认得的物种**里查
# (见 _species_from_name); 同一张图里两个物种不许共用一个名字(测试守着)。
SPECIES_NAMES = {
    # 沙漠
    "scorpion": {"en": "Scorpion", "zh": "蝎子"},
    "beetle": {"en": "Beetle", "zh": "甲虫"},
    "cactus": {"en": "Cactus", "zh": "仙人掌"},
    "sandstorm": {"en": "Sandstorm", "zh": "沙尘暴"},
    "sand_centipede": {"en": "Centipede", "zh": "蜈蚣"},
    "soldier_fire_ant": {"en": "Soldier Fire Ant", "zh": "火兵蚁"},
    # 蚁穴。"兵蚁"跟沙漠的"火兵蚁"是两种怪, 各归各图。
    "baby_ant": {"en": "Baby Ant", "zh": "幼蚁"},
    "worker_ant": {"en": "Worker Ant", "zh": "工蚁"},
    "soldier_ant": {"en": "Soldier Ant", "zh": "兵蚁"},
    "worm": {"en": "Worm", "zh": "蠕虫"},
    "queen_ant": {"en": "Queen Ant", "zh": "蚁后"},       # 没抓到过蚁后, 名字取自本地化表
    "ant_egg": {"en": "Ant Egg", "zh": "蚁卵"},           # 2026-09-27 实机日志里出现, 用户确认也算怪物
    # 花园 / 海洋 / 丛林 / 下水道 / 工厂(2026-10-01)。机械版的蜘蛛 / 胡蜂 / 螃蟹在画布上跟本体
    # 同名, 共用本体的 slug。
    "rock": {"en": "Rock", "zh": "岩石"},
    "ladybug": {"en": "Ladybug", "zh": "瓢虫"},
    "bee": {"en": "Bee", "zh": "蜜蜂"},
    "bumble_bee": {"en": "Bumble Bee", "zh": "熊蜂"},
    "ant_hole": {"en": "Ant Hole", "zh": "蚁穴"},
    "hornet": {"en": "Hornet", "zh": "黄蜂"},
    "spider": {"en": "Spider", "zh": "蜘蛛"},
    "centipede": {"en": "Centipede", "zh": "蜈蚣"},
    "dandelion": {"en": "Dandelion", "zh": "蒲公英"},
    "mecha_flower": {"en": "Mecha Flower", "zh": "机械花"},
    "wasp": {"en": "Wasp", "zh": "胡蜂"},
    "crab": {"en": "Crab", "zh": "螃蟹"},
    "bubble": {"en": "Bubble", "zh": "泡泡"},
    "shell": {"en": "Shell", "zh": "扇贝"},
    "jellyfish": {"en": "Jellyfish", "zh": "水母"},
    "starfish": {"en": "Starfish", "zh": "海星"},
    "sponge": {"en": "Sponge", "zh": "海绵"},
    "leech": {"en": "Leech", "zh": "水蛭"},
    "baby_termite": {"en": "Baby Termite", "zh": "白幼蚁"},
    "worker_termite": {"en": "Worker Termite", "zh": "白工蚁"},
    "soldier_termite": {"en": "Soldier Termite", "zh": "白兵蚁"},
    "termite_overmind": {"en": "Termite Overmind", "zh": "白蚁主宰者"},
    "termite_egg": {"en": "Termite Egg", "zh": "白蚁卵"},
    "termite_mound": {"en": "Termite Mound", "zh": "白蚁丘"},
    "bush": {"en": "Bush", "zh": "灌木丛"},
    "firefly": {"en": "Firefly", "zh": "萤火虫"},
    "leafbug": {"en": "Leafbug", "zh": "叶虫"},
    "mantis": {"en": "Mantis", "zh": "螳螂"},
    "roach": {"en": "Roach", "zh": "蟑螂"},
    "moth": {"en": "Moth", "zh": "飞蛾"},
    "fly": {"en": "Fly", "zh": "苍蝇"},
    "silverfish": {"en": "Silverfish", "zh": "蠹虫"},
    "garbage": {"en": "Garbage", "zh": "垃圾袋"},
    "barrel": {"en": "Barrel", "zh": "铀桶"},
}

# 按图覆盖: 这张图里某个名字不按 SPECIES_NAMES 走, 而是折到另一个 slug (或补一个旧写法)。
# key 用 _norm_name 之后的形式(小写、空格换下划线, 中文原样), value 必须属于该图的物种表。
_NAME_OVERRIDES = {
    "desert": {
        # Ladybug: 沙漠里极罕见的乱入怪, 高价值 —— 借 SPECIES_RANK 最高档(sandstorm=5), 同稀有度时
        # 优先被挑; 不危险, 走普通追击(sandstorm 非 kite 物种)。中英文都折。
        "瓢虫": "sandstorm",
        "ladybug": "sandstorm",
        "火蚁": "soldier_fire_ant",       # 旧版客户端的写法, 保留
        # 本地化表里的真名: 火工蚁 / Worker Fire Ant。以前被静默丢掉; 本项目不分工/兵(同上面 火蚁 的
        # 约定), 归 soldier_fire_ant。火幼蚁 / 火蚁后 / 火蚁卵 没加 —— 出现了日志里会有"未识别怪物名"。
        "火工蚁": "soldier_fire_ant",
        "worker_fire_ant": "soldier_fire_ant",
    },
}

# 认得、但不是接战目标的名字(已 _norm_name): 直接 None, 也不刷"未识别"日志。
_IGNORE_NAMES = {
    "火蚁穴", "fire_ant_burrow",   # Fire Ant Hole: 不动的出怪口 / 建筑, 不是怪
    "挖掘者", "digger",            # 友军: 打碎蚁穴后有几率冒出来, 帮玩家打怪, 不伤玩家
    "幽灵", "ghost",               # 要戴特殊眼镜(Paranormal Goggles)才打得动, 追它白费力气
    "正方形", "square",            # 玩家用 Square 花瓣放出来的 / 下水道里偶尔刷的, 不是目标
    "训练假花", "target_dummy",    # 丛林的木桩假人
    "泰坦", "titan",               # 丛林的特殊实体, 敌我不明, 先不当目标(见 spec 风险一节)
    "重构机", "assembler",         # 工厂的合成机器(特殊实体, 不是怪)
}

_seen_unknown_names = set()   # 已经报告过的"未识别怪物名"(_norm_name 之后), 每个名字只报一次 ——
                              # 找回已删掉的 debug_enemy_detect.py 原来给的那条诊断: 本图某只怪
                              # 用了没登记的名字就会从所有检测里静默消失, 这条日志至少点一次名。


def _norm_name(name):
    """名字的比较形式: 去首尾空格、小写、空格换下划线(中文原样)。slug 形式("sand_centipede")
    跟英文名("Sand Centipede")归一到同一个字符串, 旧的 slug 写法因此照常认。"""
    return str(name).strip().lower().replace(" ", "_")


@functools.lru_cache(maxsize=None)
def _name_index(known):
    """本图认得的物种 -> {_norm_name 后的名字: slug}。英文名、中文名、slug 本身都收。
    known 是 frozenset(可哈希): 换了 MAP_SPECIES 就是另一把 key, 不会读到旧缓存。注意 key 里没有
    SPECIES_NAMES —— 它启动时就定了; 第②轮若让名字可编辑, 改名后必须 _name_index.cache_clear()。"""
    index = {}
    for slug in known:
        names = SPECIES_NAMES.get(slug, {})
        for n in (names.get("en"), names.get("zh"), slug):
            if n:
                index[_norm_name(n)] = slug
    return index

# 名牌稀有度词的颜色 -> RARITY_ORDER 下标. 跟旧稀有度色表同一批值, 只是换成从
# canvas 绘制调用读到的、带 "#" 的 fill 色. Super(rank 7)/Eternal(rank 8) 实测不刷,
# rank 8 空着.
_RANK_BY_RARITY_COLOR = {
    "#7EEF6D": 0, "#FFE65D": 1, "#4D52E3": 2, "#861FDE": 3, "#DE1F1F": 4,
    "#1FDBDE": 5, "#FF2B75": 6, "#2BFFA3": 7, "#555555": 9,
}


def _species_from_name(name, map_name=None):
    """florr 客户端里的怪物名 -> 当前地图的物种 slug。不是本图的怪(路过的玩家、
    别的生态的怪)-> None(跳过)。客户端语言 English 或中文都行(见 SPECIES_NAMES)。

    查找顺序(全在**本图认得的物种**里找 —— 否则等于把别的图的怪混进来):
      1. _NAME_OVERRIDES[本图]
      2. 本图每个物种的 英文名 / 中文名 / slug 形式(英文不分大小写)
      3. _IGNORE_NAMES -> None, 不打日志
      4. 都不是 -> 第一次见时打一条"未识别怪物名", 返回 None

    map_name 不给就读 utils.MAP(worker 运行时的当前图)。这张图的物种表是空的
    (还没做索敌)-> 一律 None, 而且**一条日志都不打** —— 蚁穴一屏几十只蚂蚁,
    走"未识别怪物名"那条会把日志刷爆。
    """
    if not name:
        return None
    map_name = utils.MAP if map_name is None else map_name
    known = MAP_SPECIES.get(map_name, frozenset())
    if not known:
        return None
    key = _norm_name(name)
    override = _NAME_OVERRIDES.get(map_name, {}).get(key)
    if override in known:
        return override
    slug = _name_index(known).get(key)
    if slug is not None:
        return slug
    if key in _IGNORE_NAMES:
        return None
    if key and key not in _seen_unknown_names:
        _seen_unknown_names.add(key)
        print(f"ℹ️ canvas 解出未识别怪物名: {name!r} (slug={key!r}) —— 若是本图的怪, 加进 SPECIES_NAMES")
    return None


# 名牌上那个稀有度**词** -> RARITY_ORDER 下标。颜色表是硬编码的, florr 换一版色就
# 全线退成 Common, 神话/究极的遛怪和锁定逻辑一次都不触发 —— 表现就是"识别不到"。
# 词是同一帧里的第二个独立信源, 拿来兜底。
# 中文词实测来源: 普通/罕见 = canvas_combat_test.ndjson, 史诗/传奇/神话 = mythic.json
# (2026-09-22 用户实拍), 究极 = 用户确认。超神/独特 未经实测核对。
_RANK_BY_RARITY_WORD = {
    "普通": 0, "罕见": 1, "稀有": 2, "史诗": 3, "传奇": 4,
    "神话": 5, "究极": 6, "超神": 7, "独特": 9,
    "common": 0, "unusual": 1, "rare": 2, "epic": 3, "legendary": 4,
    "mythic": 5, "ultra": 6, "super": 7, "eternal": 8, "unique": 9,
}


def _tier_from_color(rarity_color, rarity_word=None):
    """名牌稀有度 -> RARITY_ORDER 里的档名. 都认不出 -> Common (跟旧稀有度采样读
    失败时同款兜底, 不会误触发规避).

    颜色是实测过的主信源, 优先; 颜色认不出时才退到名牌上的词。两者冲突以颜色为准。
    """
    rank = _RANK_BY_RARITY_COLOR.get(rarity_color)
    if rank is None and rarity_word is not None:
        rank = _RANK_BY_RARITY_WORD.get(str(rarity_word).strip().lower())
    return RARITY_ORDER[rank] if rank is not None else "Common"


_frame_buffer = []   # drain_canvas_log 每次读空页面 log, 跨调用在这里攒; 每次裁到最新一帧
_FRAME_BUFFER_CAP = 20000   # 硬上限, 防 _frame_buffer 无界增长: 若 __canvasFrame 卡住不动
                            # (florr 绑死了自己那份 requestAnimationFrame 引用), 每条记录都是
                            # frame 0 → group_by_frame 永远只有 1 个 key → scan_enemies 每 tick
                            # 命中 "< 2" 提前返回, 下面那句按帧裁剪永远跑不到, buffer 每 tick
                            # 涨一截. canvas_hook.js 自己把页面 log 裁到 FRAME_RETENTION=5 帧,
                            # 20000 条已经很宽裕.


# 钩子装没装的检查(inject_canvas_hook 里两次 CDP 求值)隔这么久才做一次。每次 CDP 调用在
# Windows 上 ~56ms(cdp_breakdown.py 实测), 原来每拍扫描 = 查两次 + 读一次 = 3 次; 第六份录像
# 刷怪主循环每拍将近 1 秒, 究极冲到脸上才开始躲。读出来是空的(页面 reload 了)下一拍马上重查。
HOOK_CHECK_S = 2.0
_hook_checked_at = float("-inf")


def scan_enemies(image=None, conf=0.4, model_path=None):
    """解码最新一帧完整的 canvas 绘制记录, 返回检测字典列表(跟旧 YOLO 版同结构:
    species / rarity / screen_pos / bbox / confidence). image/conf/model_path 保留
    只为兼容旧调用点, 不再用 -- 识别已经从"截图跑 YOLO"换成"解码 canvas 绘制调用".
    帧解不出(画面里没怪 -> camera_from_frame 抛) -> [](跟旧模型没框一个意思).

    每 tick 都调 inject_canvas_hook()(幂等) -- florr 重载后下一次扫描自动重注 hook.
    inject/drain/decode 整段套 try: inject_canvas_hook 版本不符会抛 RuntimeError,
    _send_cdp_command 找不到标签页也抛, canvas_decode 的除法可能抛
    ZeroDivisionError/IndexError, cdp_bridge 底下 websocket 可能抛 WebSocketException
    /OSError —— 全都当"这次没检测到"退化成 wander."""
    global _last_center, _last_player_world, _hook_checked_at
    try:
        now = time.time()
        if now - _hook_checked_at >= HOOK_CHECK_S:
            cdp_bridge.inject_canvas_hook()
            _hook_checked_at = now
        drained = cdp_bridge.drain_canvas_log()
        if not drained:
            _hook_checked_at = float("-inf")    # 页面 reload 了 / 钩子没了 -> 下一拍马上重查
        _frame_buffer.extend(drained)
        if len(_frame_buffer) > _FRAME_BUFFER_CAP:
            del _frame_buffer[:-_FRAME_BUFFER_CAP]   # 硬上限, 见 _FRAME_BUFFER_CAP 注释
        frames = canvas_decode.group_by_frame(_frame_buffer)
        if len(frames) < 2:
            _last_center = _last_player_world = None
            return []
        keys = sorted(frames)
        recs = frames[keys[-2]]                 # 最新那帧可能还在画, 取次新的
        _frame_buffer[:] = [r for r in _frame_buffer if r.get("frame", -1) >= keys[-1]]
        cam = canvas_decode.camera_from_frame(recs, best_effort=True)
        mobs = canvas_decode.mobs_from_frame(recs, cam)
    except Exception:
        _last_center = _last_player_world = None   # 别留着上一帧的锚点当"当前位置"用
        return []                              # 任何异常 → 当作这次没解出来, 返回 []

    # 近似相机(approx)的锚点实测会落在别的实体身上, 不能拿来当玩家位置 —— 宁可退回
    # SCREEN_CENTER, 那至少是个稳定的近似。
    _last_center = None if cam.get("approx") else cam.get("player_screen")
    _last_player_world = None if cam.get("approx") else cam.get("player_world")

    out = []
    for m in mobs:
        sp = _species_from_name(m.get("name"))
        if sp is None:
            continue
        sx, sy = m["sx"], m["sy"]
        out.append({
            "species": sp,
            "rarity": _tier_from_color(m.get("rarity_color"), m.get("rarity")),
            "screen_pos": (sx, sy),
            "bbox": (sx - 1, sy - 1, sx + 1, sy + 1),
            "confidence": 1.0,
            "world": (m["x"], m["y"]),
        })
    return out
