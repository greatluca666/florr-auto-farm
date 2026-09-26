"""Load canvas draw-call logs and decode them into per-frame game state.

The camera is READ, not estimated. florr.io hands us everything we need inside the draw calls:

  * every draw belonging to one visual entity is emitted under that entity's own CTM, so the
    matrix translation `m[4], m[5]` IS the entity's screen anchor — grouping by it aggregates
    body + outline + eyes + health bar into one entity with no spatial clustering heuristics;
  * mob nameplates (a 3-stroke health bar plus name and rarity text) are drawn at the plain
    camera scale, which gives the world->screen zoom exactly;
  * the minimap draws the player under a tiny CTM whose pre-transform coordinate is the
    player's ABSOLUTE world position, recoverable as `(x - m[4]) / m[0]`.

An earlier version estimated camera motion from the median displacement of appearance-matched
draws. That assumption ("most on-screen content is world-static") is false for this game — the
player's own petals and the mobs outnumber static scenery — and it is unnecessary given the
above, so it was removed rather than tuned. Everything here fails loudly (ValueError) rather
than returning coordinates it cannot justify.
"""
# VENDORED from florragent's scripts/canvas_decode.py (2026-09-01). Trimmed to
# the camera + mob subset florr-auto-pathing's enemy_detect needs. Keep in sync with upstream.
import math
import re
from pathlib import Path
import json

# Draw-call signatures observed in data/raw_captures/canvas_move_right_only.ndjson (see render_spec.md).
MINIMAP_MAX_SCALE = 0.05      # minimap CTM scale is ~0.0084; world draws are ~0.76+
HEALTHBAR_BG = "#222222"      # nameplate bar: dark background (full width)
HEALTHBAR_DAMAGE = "#DD3434"  # red damage track
HEALTHBAR_SECONDARY = "#42E3F5"  # a narrower cyan bar some entities draw above the health bar
PLAYER_BODY_COLOR = "#FFE763" # the player flower's gold body, and its minimap dot
ABSORB_FRACTION = 1.0         # sub-anchors within this fraction of a body's radius belong to it

# Another player's nameplate shows their account level ("2级") in the slot a mob's rarity tier
# name would occupy. No real mob rarity name has this shape (they're all quality words --
# 普通/不凡/稀有/etc. -- never a bare number). See
# docs/superpowers/specs/2026-08-18-alone-and-danger-awareness-design.md.
PLAYER_RARITY_PATTERN = re.compile(r"^\d+级$")

# UI-space fills (the petal inventory bar, HUD icons) render at CTM scale ~1.0, distinct from
# the world/camera zoom (measured 0.7-0.95 throughout this project) and the minimap's ~0.01.
# Tightened to 0.01 to exclude HUD avatar-card eyes (scale ~1.05) that falsely matched at 0.05.
INVENTORY_UI_SCALE_TOL = 0.01

# How close a player-style nameplate ("37级") must be to a gold-body candidate's screen anchor
# to count as belonging to it, when camera_from_frame needs to break a same-radius tie between
# multiple gold flower bodies (see that function). Measured live 2026-08-19 (main account, a
# crowded area) at ~17-20px between a real other-player's nameplate and their own body -- 100px
# leaves generous margin without risking a match onto an unrelated nearby nameplate.
SELF_DISAMBIGUATION_RADIUS = 100.0

# 名牌相对血条锚点的位置门。florr 把名牌画在怪**身体下方**, 所以垂直距离随体型增大,
# 而水平偏移基本恒定。实测 ultra.json (zoom 0.315) 的垂直偏移: 甲虫/蝎子 +38.9,
# 沙尘暴 +59~+78, 神话仙人掌 +83.1, 最大那只 +91.9(它的稀有度词落在 +100.7) ——
# 水平偏移则始终是名字 -20.5 / 稀有度 +9.8。
# 老代码两边(这里和 canvas_hook.js)都用半径 100 的**圆**当门, 于是体型一大, 稀有度词
# 先越界被丢 —— 名字比它近 8.8px 还在, 结果"有名字没稀有度", tier 退成 Common,
# 神话/究极的锁定和规避一次都不触发。florr 里体型随稀有度增大, 所以这道门精确地卡掉
# 高稀有度怪; 而且那一帧 zoom 只有 0.315, 正常 zoom 下体型的屏幕半径还要大几倍。
# 改成**盒子**: 水平卡死(隔壁怪的名牌不能串过来), 垂直放宽。
LABEL_MAX_DX = 120.0
LABEL_MAX_DY = 600.0
LABEL_MIN_DY = -20.0   # 名牌永远在血条下方; 留一点余量给抗锯齿抖动


# 战队标签长 "[ulgr]" 这样, 夹在名字和等级之间 —— 实测帧 afk_20260920-182816 确认。
CLAN_TAG_PATTERN = re.compile(r"^\[.*\]$")

# 稀有度词自己的专属颜色(florr 客户端的稀有度色阶). 名牌里认稀有度靠颜色, 不靠它排在
# 第几个 —— 实测里名牌的文字顺序并不稳定, 而且块里可能混进旁边飘来的文本。
RARITY_TEXT_COLORS = frozenset({
    "#7EEF6D", "#FFE65D", "#4D52E3", "#861FDE",
    "#DE1F1F", "#1FDBDE", "#FF2B75", "#2BFFA3", "#555555",
})


# 玩家生成的召唤物(2026-09-25 实机确认): 名牌是 [名字, "召唤", 稀有度词] 三行, 多出来的
# "召唤" 用的是金色 #FFE763。它们是别的玩家放出来的, 不是野怪 —— 从不主动打人, 追它们纯属
# 白费时间(实测一帧里围着玩家有 30 多只"神话/究极沙尘暴"召唤物, 全是这种)。
# 目前只认中文客户端的写法; 英文客户端这个词长什么样没在实机上见过。
SUMMON_LABELS = frozenset({"召唤"})


def _is_summon_block(texts):
    """名牌块里有"召唤"这一行 = 玩家生成的召唤物, 不是野怪."""
    return any(str(t) in SUMMON_LABELS for t in texts)


# HUD 上的区域名 -> 寻路图名。只收实机帧里核对过的: 蚁穴的抓帧里一直有 "蚂蚁地狱",
# 沙漠的抓帧里一直有 "沙漠" (2026-09-27)。花园/海洋的字样没见过, 不猜。
ZONE_LABELS = {"蚂蚁地狱": "anthell", "沙漠": "desert"}


def zone_map_from_frame(records):
    """从 HUD 上的区域名读出"人现在在哪张图". 认不出 -> None.

    为什么要它: 死在蚁穴后重生还在蚁穴, 但服务器号 / 阶段猜测都会说"花园"(蚁穴走的是
    花园那台服务器), 进场路线就拿花园的路线去走蚁穴的墙, 一轮白烧 180 秒。区域名是游戏
    自己画在 HUD 上的, 跟服务器号无关。
    名牌文字画在世界缩放(zoom)下, HUD 是 UI 缩放 —— 跳过跟 zoom 同缩放的文本, 玩家/怪
    起名叫"蚂蚁地狱"也骗不了它。"""
    zoom = None
    for r in records:
        if r["op"] == "stroke" and r.get("stroke") == HEALTHBAR_BG and not _is_minimap(r):
            zoom = r["m"][0]
            break
    for r in records:
        if r["op"] != "text":
            continue
        zone = ZONE_LABELS.get(str(r.get("text", "")))
        if zone is None:
            continue
        if zoom is not None and r.get("m") is not None and abs(_scale(r) - zoom) < 1e-6:
            continue
        return zone
    return None


def _is_player_block(texts):
    """这个名牌块属于玩家(而不是怪)吗 —— 块里**任意一条**文本是 "37级" 就算。

    原来只看 texts[1]。实测两种真实结构都会漏:
      ['conf281630', '[ulgr]', '105级']  带战队标签, 等级被挤到第 3 格
      ['2级', 'Guest #8121']             等级在前、名字在后
    漏掉的后果不只是"少认一个玩家": 这种块会掉进 mobs_from_frame 当成怪, 而且
    rarity_color 取到战队标签的 #1FDBDE(正好是神话色)或等级的 #FF2B75(究极色) ——
    监视器里就会冒出根本不存在的神话/究极"怪"。
    """
    return any(PLAYER_RARITY_PATTERN.match(str(t)) for t in texts)


def load_ndjson(path):
    records = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def group_by_frame(records):
    frames = {}
    for r in records:
        frames.setdefault(r["frame"], []).append(r)
    return frames


def _scale(rec):
    m = rec.get("m")
    return math.hypot(m[0], m[1]) if m else 0.0


def _anchor(rec):
    m = rec["m"]
    return (m[4], m[5])


# 花身挨打会闪色: 从 PLAYER_BODY_COLOR 往白(#FFEE91、#FFF4BB)或往红(#FF9248、#FF7230)
# 按同一个比例插值。只认精确色号的话, 一闪就认不出自己 —— florragent 战斗录像 165 帧里
# 正好 39 帧(24%)是这样, 全落进"相机只能近似"、自身血量读不出来; 实机进度行
# "解出自己"只有 18~50% 就是它。
_FLASH_MAX = 0.9          # 闪到头的纯白/纯红太常见(别的东西也这个色), 不认
_FLASH_TOL = 0.06         # G/B 两个分量解出来的插值比例得对得上


def _is_self_body_color(color):
    """这个颜色是不是自己的花身 —— 正常金色, 或它往白/往红闪到一半。"""
    try:
        s = str(color)
        r, g, b = int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)
    except (ValueError, IndexError):
        return False
    if len(str(color)) != 7 or r != 0xFF:
        return False
    base_g, base_b = int(PLAYER_BODY_COLOR[3:5], 16), int(PLAYER_BODY_COLOR[5:7], 16)
    # 往白: 实测是严格的线性插值(#FFEE91 两个分量都是 0.29, #FFF4BB 是 0.54/0.56)。
    tg = (g - base_g) / (0xFF - base_g)
    tb = (b - base_b) / (0xFF - base_b)
    if 0.0 <= tg <= _FLASH_MAX and 0.0 <= tb <= _FLASH_MAX and abs(tg - tb) <= _FLASH_TOL:
        return True
    # 往红: 只有三个样本(#FF9248 #FF823C #FF7230), 它们共线但那条线不过金色 —— 不是
    # 往某个固定色的插值, 没法按比例卡。退一步: G/B 都比金色低、仍是橙色(G > B ——
    # 纯红 #FF0000 和超神怪的 #FF2B75 都不满足)。
    return g <= base_g and b <= base_b and g > b


def _is_minimap(rec):
    return rec.get("m") is not None and _scale(rec) < MINIMAP_MAX_SCALE


def _is_rotated(rec):
    return abs(rec["m"][1]) > 1e-9


def _median_bar_anchor(records):
    anchors = [_anchor(r) for r in records
               if r["op"] == "stroke" and r.get("stroke") == HEALTHBAR_BG and not _is_minimap(r)]
    if not anchors:
        return None
    xs = sorted(a[0] for a in anchors)
    ys = sorted(a[1] for a in anchors)
    return (xs[len(xs) // 2], ys[len(ys) // 2])


_SELF_ANCHOR_OUTLIER_PX = 600.0   # a resolved player anchor further than this from the mob
                                  # cluster's median bar anchor is not the local player


def _has_bare_player_bar_at(records, anchor, tol=2.0):
    """anchor 上是不是画着一条"玩家式"裸血条: 没有名牌, 且带 #42E3F5 那条护盾窄条.

    给 best_effort 的离群判定当佐证用。那个判定原本只拿"所有名牌血条锚点的中位数"
    当参照, 一旦超过 _SELF_ANCHOR_OUTLIER_PX 就把金身锚点扔掉 —— 但中位数这个参照在
    血条分布不对称时是错的: 实测 florragent 的真实战斗录像(canvas_combat_test.ndjson)
    里, 一条蜈蚣的 9 个节段各画一条血条全挤在屏幕左外侧, 把中位数拽到 (-134, 72),
    离真实玩家锚点 (960, 540) 有 1190px, 于是 165 帧里有 70 帧(42%)扔掉了算对的答案,
    回退到 _self_bar_anchor 后全部认错人 —— 那 70 帧 player_from_frame 报出的血量
    100% 都是别的实体的 1.0。

    金身锚点上正好压着一条玩家式裸血条, 就是一条与中位数无关的独立佐证, 足以否决
    那个离群判定。反过来, 真的是"另一个玩家的身体飘在视野外"时, 那个锚点上不会同时
    有这样一条裸血条(别的玩家画的是带名牌的血条), 原判定继续生效。
    """
    for b in _bar_blocks(records):
        if b["texts"] or b["secondary"] is None:
            continue
        if math.hypot(b["anchor"][0] - anchor[0], b["anchor"][1] - anchor[1]) <= tol:
            return True
    return False


def _self_bar_anchor(records):
    """Best-effort player screen anchor when the gold-body method fails or is an outlier.

    1. The player flower's base is a solid black circle (`#000000`, r≈18-30) drawn at its
       screen anchor every frame — seen in every live capture at the same point even when the
       gold body itself renders in an off-palette gold (`#F5BF39`) or is occluded. Take that.
    2. Failing that, the player draws its own `#222222` HP bar (no nameplate, and it carries
       the `#42E3F5` shield stroke). Prefer a bare bar block with that secondary; else the
       bare healthy-hp block nearest the median of every bar anchor.
    None if nothing usable.
    """
    for r in records:
        if (r["op"] == "fill" and r.get("fill") == "#000000" and not _is_minimap(r)
                and r.get("r") is not None and 18.0 <= r["r"] <= 30.0):
            ax, ay = _anchor(r)
            if (ax, ay) != (0.0, 0.0):
                return (ax, ay)

    blocks = _bar_blocks(records)
    bare = [b for b in blocks if not b["texts"] and b["hp"] is not None and b["hp"] > 0.4]
    shielded = [b["anchor"] for b in bare if b["secondary"] is not None]
    if len(shielded) == 1:
        return shielded[0]
    med = _median_bar_anchor(records)
    if med is None or not bare:
        return None
    pool = shielded or [b["anchor"] for b in bare]
    return min(pool, key=lambda a: math.hypot(a[0] - med[0], a[1] - med[1]))


def camera_from_frame(records, best_effort=False):
    """Read the camera for one frame: {"zoom", "player_world", "player_screen"}.

    zoom comes from a nameplate health bar (drawn unrotated at the camera scale), the absolute
    world position from the minimap player dot, and the screen anchor from the player's own gold
    body. Raises ValueError if any of the three is missing — guessing one would silently corrupt
    every world coordinate derived from it.

    best_effort=True (used by enemy_detect.scan_enemies, which works in screen space and
    discards world coords): a gold-body anchor that lands far outside the mob cluster is
    another player's body, not ours — discard it. If player_screen is then still unknown but
    zoom is, fall back to the player's own bare HP bar near the cluster centre and set
    player_world to 0. Only _is_player_anchor filtering needs player_screen pixel-exact;
    screen_pos comes straight from each mob's own anchor."""
    zoom = None
    for r in records:
        if r["op"] == "stroke" and r.get("stroke") == HEALTHBAR_BG and not _is_minimap(r):
            zoom = r["m"][0]
            break

    player_world = None
    for r in records:
        if _is_minimap(r) and r.get("fill") == PLAYER_BODY_COLOR and r.get("r") is not None:
            m = r["m"]
            player_world = ((r["x"] - m[4]) / m[0], (r["y"] - m[5]) / m[3])
            break

    player_screen = None
    if zoom is not None:
        candidates = [r for r in records
                      if (r["op"] == "fill" and r.get("r") is not None
                          and _is_self_body_color(r.get("fill"))
                          and not _is_minimap(r) and not _is_rotated(r)
                          and abs(_scale(r) - zoom) < 1e-6)]   # excludes the larger UI-card avatar
        if candidates:
            max_r = max(c["r"] for c in candidates)
            largest = [c for c in candidates if abs(c["r"] - max_r) < 1e-6]
            if len(largest) > 1:
                # A same-radius tie: other real players can share the exact default flower
                # appearance (confirmed live 2026-08-19, main account, a crowded area -- three
                # identical gold bodies at once). Break it: OTHER players draw a floating "NNN级"
                # level label near their own body; the env's own player does not. Scan the raw
                # text records for it -- NOT _bar_blocks, because another player's HP bar often
                # lacks the #222222 background so no block (hence no captured level text) is
                # built for them.
                lvl = [_anchor(r) for r in records
                       if r["op"] == "text"
                       and PLAYER_RARITY_PATTERN.match(str(r.get("text", "")))]
                without = [
                    c for c in largest
                    if not any(math.hypot(lx - c["m"][4], ly - c["m"][5]) <= SELF_DISAMBIGUATION_RADIUS
                               for lx, ly in lvl)
                ]
                if len(without) == 1:
                    largest = without
                elif without:
                    # Still ambiguous. The player is camera-locked near the centre of the mob
                    # cluster; pick the candidate closest to the centroid of all nameplate bar
                    # anchors. Best-effort -- an approximate self-anchor beats losing the whole
                    # frame's detections (only _is_player_anchor filtering depends on it being
                    # pixel-exact; screen_pos comes straight from each mob's own anchor).
                    bars = [_anchor(r) for r in records
                            if r["op"] == "stroke" and r.get("stroke") == HEALTHBAR_BG
                            and not _is_minimap(r)]
                    if bars:
                        cx = sum(a[0] for a in bars) / len(bars)
                        cy = sum(a[1] for a in bars) / len(bars)
                        largest = [min(without,
                                       key=lambda c: math.hypot(c["m"][4] - cx, c["m"][5] - cy))]
            # Exactly one candidate (after the tie-break) is genuinely our own player. A tie
            # that STILL isn't resolved (every candidate has a level label nearby -- shouldn't
            # happen for self) can't be broken -- fail loud rather than pick by draw order.
            if len(largest) == 1:
                player_screen = _anchor(largest[0])

    if best_effort and player_screen is not None:
        med = _median_bar_anchor(records)
        if (med is not None
                and math.hypot(player_screen[0] - med[0],
                               player_screen[1] - med[1]) > _SELF_ANCHOR_OUTLIER_PX
                and not _has_bare_player_bar_at(records, player_screen)):
            player_screen = None            # that gold body is another player, off-screen

    if best_effort and player_screen is None and zoom is not None:
        fallback = _self_bar_anchor(records)
        if fallback is not None:
            return {"zoom": zoom, "player_world": player_world or (0.0, 0.0),
                    "player_screen": fallback, "approx": True}

    missing = [n for n, v in (("zoom", zoom), ("player_world", player_world),
                              ("player_screen", player_screen)) if v is None]
    if missing:
        raise ValueError(f"camera undetermined for this frame, missing: {', '.join(missing)}")
    return {"zoom": zoom, "player_world": player_world, "player_screen": player_screen,
            "approx": False}


def screen_to_world(x, y, camera):
    """Convert a screen anchor to absolute world coordinates, anchored on the player."""
    px, py = camera["player_screen"]
    wx, wy = camera["player_world"]
    z = camera["zoom"]
    return (wx + (x - px) / z, wy + (y - py) / z)


def player_world_position(records):
    """这一帧里玩家的绝对世界坐标, 只需要小地图上 PLAYER_BODY_COLOR 那个点 ——
    不需要 camera_from_frame 那三项(zoom/player_screen/player_world)全齐,
    所以特意不直接调它, 自己单独解, 可用性比完整相机解析更强(比如设置面板
    挡住了主视图玩家本体, 小地图这个点通常还在)。解不出来时返回 None, 调用方
    自己决定怎么退化 —— 不猜一个位置出来.
    """
    for r in records:
        if _is_minimap(r) and r.get("fill") == PLAYER_BODY_COLOR and r.get("r") is not None:
            m = r["m"]
            return ((r["x"] - m[4]) / m[0], (r["y"] - m[5]) / m[3])
    return None


def player_world_distance(records, target_world):
    """这一帧里, 玩家跟 target_world(某个静止地标的绝对世界坐标)的真实距离
    (世界单位, 不是屏幕像素也不是小地图量化像素)。解不出玩家世界坐标时返回
    None, 调用方自己决定怎么退化.

    注意: 这里的"玩家世界坐标"来自小地图上 PLAYER_BODY_COLOR 那个点 ——
    实机复盘(2026-09-19)证实小地图那个点本身的绘制坐标是取整过的(跟玩家
    真实连续位置比, 挪1个小地图像素≈200多个世界单位), 所以这个距离适合
    "判断大致还在不在原地"(容差本来就以百为单位, 见 main.py 里 PORTAL_
    WORLD_HOLD_BACK_RADIUS 的注释), 不适合当"精确走到某个点"的逐帧反馈 ——
    那种场景要用 portal_glow_world_offset(), 走主视图渲染, 没有这层量化.
    """
    pos = player_world_position(records)
    if pos is None:
        return None
    wx, wy = pos
    tx, ty = target_world
    return math.hypot(wx - tx, wy - ty)


# 传送门光效(同心圆光晕)的圆弧半径下限 —— 实机(2026-09-18/19, debug_canvas_portal.py)
# 两次独立采样里, 唯一 fillStyle 不是字符串(CanvasGradient/CanvasPattern, 带光效的
# 圆常用这个)的圆弧记录, 半径都是90; 玩家自己的花瓣/UI元素观测到的最大半径在20
# 上下。40留了足够余量, 两头都不贴线.
PORTAL_GLOW_MIN_RADIUS = 40.0


def portal_glow_screen_anchor(records):
    """在这一帧的绘制记录里找"传送门光效"那个特征记录的屏幕锚点 —— 非字符串
    fillStyle(渐变/图案)、半径够大、不在小地图上。目前唯一已知会画出这种
    记录的东西就是蚁穴洞口那个同心圆光效(debug_canvas_portal.py 两次独立
    实机采样都只匹配到这一条)。这一帧没找到就返回 None, 调用方自己决定怎么
    退化 —— 不能保证画面里洞口光效一定可见(比如离得太远还没渲染出来)."""
    for r in records:
        if (r.get("op") == "fill" and r.get("r") is not None
                and r["r"] > PORTAL_GLOW_MIN_RADIUS
                and not isinstance(r.get("fill"), str)
                and not _is_minimap(r)):
            return (r["x"], r["y"])
    return None


def portal_glow_world_offset(records, camera):
    """洞口光效相对玩家的世界坐标偏移量 (dx, dy) —— 走主视图渲染这条路径,
    不经过小地图那层量化: minimap 玩家点的绘制坐标是取整过的(见
    player_world_distance 的说明), 挪1个小地图像素≈200多个世界单位, 精度
    跟"走到某个点触发传送"这种任务对不上; 主视图渲染没有这层量化, 花瓣这类
    近距离物件测出来的距离精度到个位数, debug_canvas_portal.py 实机验证过
    同一个洞口两条独立路径(小地图/主视图)反解出的绝对坐标完全一致, 说明
    主视图这条路径本身没错, 只是没有 player_world 那层取整。

    camera 需要 player_screen/zoom 齐全(camera_from_frame 的返回值); 这一帧
    没找到光效记录时返回 None, 调用方自己决定怎么退化."""
    anchor = portal_glow_screen_anchor(records)
    if anchor is None:
        return None
    px, py = camera["player_screen"]
    zoom = camera["zoom"]
    return ((anchor[0] - px) / zoom, (anchor[1] - py) / zoom)


def _bar_width(rec):
    bbox = rec.get("bbox")
    return (bbox[2] - bbox[0]) if bbox else 0.0


def _bar_blocks(records, label_radius=100.0):
    """Yield one dict per nameplate block.

    Bars first: a block is a run of health-bar strokes sharing an anchor. Each coloured bar is
    measured against the most recent `#222222` background, because entities that draw a second,
    narrower cyan bar give each bar its own background. The remaining-health bar is identified
    by position, not colour: it is the stroke drawn immediately after the `#DD3434` damage
    track (its colour is a damage-flash gradient).

    Text is assigned in a second pass: each label goes to the block whose stroke run was drawn
    most recently before it (draw order), if within `label_radius` of that block's anchor.
    Any block left empty then claims the nearest unassigned label within a tighter radius.

    Why not just consume the text right after each block's strokes (the old way): at desert
    mob density a `fill` particle between the bar and the label ended the run early, and when
    florr batched two mobs' bars back-to-back then their labels, the first block consumed
    nothing and the second consumed+rejected the first's label on distance — the whole
    nameplate vanished (a point-blank Mythic sandstorm was lost this way). Why not
    nearest-anchor: 8 stacked nameplates in a ~150px corner let a neighbour's rarity word
    jump into slot 0. Stream-order primary + distance gate + empty-block rescue handles both.
    """
    blocks = []
    i, n = 0, len(records)
    while i < n:
        r = records[i]
        if not (r["op"] == "stroke" and r.get("stroke") == HEALTHBAR_BG and not _is_minimap(r)):
            i += 1
            continue
        anchor = _anchor(r)
        bg_width, hp, secondary = _bar_width(r), None, None
        value_pending = False
        while i < n and records[i]["op"] == "stroke" and _anchor(records[i]) == anchor:
            stroke, width = records[i].get("stroke"), _bar_width(records[i])
            if stroke == HEALTHBAR_BG:
                bg_width = width
                value_pending = False
            elif stroke == HEALTHBAR_DAMAGE:
                value_pending = True          # the next stroke is the remaining-health bar
            elif stroke == HEALTHBAR_SECONDARY and bg_width:
                secondary = width / bg_width
            elif value_pending and bg_width:
                hp = width / bg_width
                value_pending = False
            i += 1
        blocks.append({"anchor": anchor, "hp": hp, "secondary": secondary,
                       "texts": [], "text_colors": [], "_end": i, "_scale": _scale(r)})

    def _add(block, rec):
        t = rec.get("text")
        if t not in block["texts"]:
            block["texts"].append(t)
            block["text_colors"].append(rec.get("fill"))

    def _d(rec, block):
        lx, ly = _anchor(rec)
        return math.hypot(lx - block["anchor"][0], ly - block["anchor"][1])

    def _in_box(rec, block, max_dx, max_dy):
        """这条文本落在这个血条块的"名牌盒子"里吗 —— 水平 ±max_dx, 垂直 (下方) max_dy."""
        lx, ly = _anchor(rec)
        dx = abs(lx - block["anchor"][0])
        dy = ly - block["anchor"][1]
        return dx <= max_dx and LABEL_MIN_DY <= dy <= max_dy

    def _same_scale(rec, block):
        """这条文本跟这个血条画在同一个坐标尺度上吗。

        florr 的 HUD(左上角自己的头像卡)把名字和等级画在 UI 尺度(实测 1.25)上, 世界里的
        名牌画在相机缩放上(实测 0.90). 不卡这一条, 下面的"空块救济"会把自己 HUD 上的
        "2级"贴到自己那条裸血条上 —— 实测 canvas_combat_test.ndjson 里 165 帧有 39 帧
        这样, 结果"自己"被当成另一个玩家报出来; 同理 HUD/公告文本也可能挤进怪的名牌块,
        把稀有度词挤到后面去。
        """
        return abs(_scale(rec) - block["_scale"]) <= 1e-3

    unclaimed = []
    for k, rec in enumerate(records):
        if rec["op"] != "text":
            continue
        if str(rec.get("text", "")).strip().isdigit():
            continue                           # floating damage numbers — never a name or rarity
        prev = [b for b in blocks if b["_end"] <= k]
        owner = max(prev, key=lambda b: b["_end"]) if prev else None
        if (owner is not None and _same_scale(rec, owner)
                and _in_box(rec, owner, label_radius, LABEL_MAX_DY)):
            _add(owner, rec)
        else:
            unclaimed.append(rec)

    tight = label_radius * 0.6
    for b in blocks:
        if b["texts"]:
            continue
        near = sorted((r for r in unclaimed if _same_scale(r, b)
                       and _in_box(r, b, tight, LABEL_MAX_DY * 0.5)),
                      key=lambda r: _d(r, b))
        for r in near[:4]:                     # name x2 + rarity x2
            _add(b, r)

    for b in blocks:
        del b["_end"]
        del b["_scale"]
    return blocks


def _is_player_anchor(anchor, camera, tol=1.0):
    px, py = camera["player_screen"]
    return math.hypot(anchor[0] - px, anchor[1] - py) <= tol


def mobs_from_frame(records, camera, label_radius=100.0):
    """Parse nameplate blocks into mobs: name, rarity, health fraction, screen and world position.

    florr.io emits one block per mob in draw order — the bar strokes at the mob's anchor, then
    the name text, then the rarity text, each drawn twice (stroke pass then fill pass). Label
    text is only claimed within `label_radius` of the bar anchor, so a banner drawn straight
    after a nameplate is not mistaken for that mob's name, and a bar with no nameplate reports
    no name. The block at the player's own anchor is excluded — see `player_from_frame`.
    Blocks carrying a "召唤" line are player-summoned minions, not wild mobs, and are dropped.
    """
    mobs = []
    for block in _bar_blocks(records, label_radius):
        if _is_player_anchor(block["anchor"], camera):
            continue
        if _is_player_block(block["texts"]):
            continue
        if _is_summon_block(block["texts"]):
            continue
        ax, ay = block["anchor"]
        wx, wy = screen_to_world(ax, ay, camera)
        name, rarity, rarity_color = _split_mob_texts(block["texts"], block["text_colors"])
        mobs.append({
            "name": name,
            "rarity": rarity,
            "rarity_color": rarity_color,
            "hp": block["hp"],
            "sx": ax, "sy": ay, "x": wx, "y": wy,
        })
    return mobs


def _split_mob_texts(texts, colors):
    """一个怪名牌块的文本 -> (名字, 稀有度词, 稀有度色).

    稀有度按**颜色**认(RARITY_TEXT_COLORS), 不按"排第几个"认: 实测名牌块里会混进
    旁边飘过来的文本, 一旦多出一条, 按位置取的 texts[1] 就把稀有度读成了那条干扰
    文本, 而 rarity_color 会跟着读成白色 -> _tier_from_color 认不出 -> 整只怪被
    当成普通怪, 神话/究极的走位和锁定逻辑全部不触发。

    颜色里一条都不匹配(客户端改了色, 或这块只有名字)时退回老的按位置取法, 保持
    跟以前一样的行为, 不把"认不出稀有度"变成"连名字都没了"。
    """
    if not texts:
        return None, None, None
    for i, c in enumerate(colors):
        if c in RARITY_TEXT_COLORS:
            name = next((t for j, t in enumerate(texts) if j != i), None)
            return name, texts[i], c
    return (texts[0],
            texts[1] if len(texts) > 1 else None,
            colors[1] if len(colors) > 1 else None)
