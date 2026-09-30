"""躲究极时往哪跑: 在小地图格子上找「自己比追兵先到、而且能跑得最远」的点, 朝去那儿的路走。

老办法(enemy_detect.flee_mouse_target + main._steer_clear_of_walls)只算"背离究极"的方向,
再往前看 3 格有没有墙。2026-09-30 第五份录像两次死亡都死在这上面:
  - 究极兵蚁从左下追来, 一直往右上跑, 6 秒后扎进刷怪区右上角的死胡同 (48,85), 被贴死。
    3 格前瞻看不出死胡同; 位置取整后按格子中心判墙, 斜着擦墙角也看不出被挡。
  - 背离究极的方向上正好是 12~14 只传奇兵蚁, 一头撞进去, 1.2 秒满血到 0。

这里换成追逃问题的标准做法: 以自己为中心的一块格子上跑两遍最短路(8 邻接, 不许斜穿墙角),
一遍从自己出发(D_me), 一遍从所有追兵出发(D_chaser)。D_chaser - D_me 就是"我比追兵早到几格";
在早到至少 SAFE_MARGIN 格的格子里挑最远的那个 —— 死胡同的格子跑不远, 自然落选; 最短路上
每一格也都满足"我先到"(三角不等式), 不会半路被截。危险怪群(传奇以上的兵蚁等)身边的格子
对自己当墙, 规划绕开它们; 追兵不受这个限制。

纯函数, 不碰屏幕/CDP, 坐标全是小地图像素(浮点, 北在上跟屏幕同朝向)。
"""
import heapq
import math

HORIZON = 20          # 格: 只在自己周围 ±20 格里规划(≈4300 世界单位, 刷怪装 300/秒 跑 14 秒)。
                      # 12 格时第五份录像里刷怪区东边那片湾的尽头在窗口外, 看着像条活路
SAFE_MARGIN = 1.0     # 格: 自己比追兵早到这么多才算安全(1 格 ≈ 217 世界单位 ≈ 0.7 秒)
MARGIN_CAP = 4.0      # 格: 早到再多也不再加分, 免得为了"更安全"放弃"跑得远"
MARGIN_WEIGHT = 0.5
CROWD_BLOCK_R = 0.8   # 格: 危险怪群身边这么近的格子, 自己不走
WAYPOINT_MAX = 5      # 格: 朝路径上最远的、直线看得见的点走, 最多看这么远
BEYOND_STEPS = 10     # 窗口边上的格, 往窗口外还能再走这么多步才算"活路"
KEEP_R = 3.0          # 格: 离上一拍目标这么近的格算"原来那条路"
KEEP_BONUS = 2.0      # 格: 原来那条路的加分; 安全线也放宽到一半 —— 第五份录像回放里早到
                      # 1.0 格上下一抖, 目标就在南口走廊和东边死湾之间来回翻
_LOS_STEP = 0.25

_SQRT2 = math.sqrt(2.0)
_STEPS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
          (1, 1, _SQRT2), (1, -1, _SQRT2), (-1, 1, _SQRT2), (-1, -1, _SQRT2))


def _free(bm, x, y):
    return 0 <= y < bm.shape[0] and 0 <= x < bm.shape[1] and bm[y, x] == 255


def _snap(bm, p, search=2):
    """浮点位置 -> 最近的可走格(地图墙比游戏里厚一格, 人/怪经常"站在墙上")。附近没有 -> None。"""
    cx, cy = int(round(p[0])), int(round(p[1]))
    best = None
    for y in range(cy - search, cy + search + 1):
        for x in range(cx - search, cx + search + 1):
            if _free(bm, x, y):
                d = math.hypot(x - p[0], y - p[1])
                if best is None or d < best[0]:
                    best = (d, (x, y))
    return best


def _dijkstra(bm, seeds, box, blocked=frozenset()):
    """多源最短路。seeds: [(初始代价, (x, y))]。box=(x0, y0, x1, y1) 闭区间。
    斜走要两个正交邻格都能走 —— 花有体积, 挤不过墙角。返回 (dist, parent)。"""
    dist, parent = {}, {}
    heap = [(c, cell, None) for c, cell in seeds]
    heapq.heapify(heap)
    while heap:
        d, cell, par = heapq.heappop(heap)
        if cell in dist:
            continue
        dist[cell] = d
        parent[cell] = par
        x, y = cell
        for dx, dy, c in _STEPS:
            nx, ny = x + dx, y + dy
            nxt = (nx, ny)
            if (nxt in dist or nxt in blocked
                    or not (box[0] <= nx <= box[2] and box[1] <= ny <= box[3])
                    or not _free(bm, nx, ny)):
                continue
            if dx and dy and not (_free(bm, x + dx, y) and _free(bm, x, y + dy)):
                continue
            heapq.heappush(heap, (d + c, nxt, cell))
    return dist, parent


def _los(bm, a, b, blocked):
    """a -> b 的直线(每 1/4 格采一个点)全在可走、没被怪群占的格子上。"""
    n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / _LOS_STEP))
    for i in range(1, n + 1):
        x = int(round(a[0] + (b[0] - a[0]) * i / n))
        y = int(round(a[1] + (b[1] - a[1]) * i / n))
        if not _free(bm, x, y) or (x, y) in blocked:
            return False
    return True


def _escapes_beyond(bm, cell, box, steps=BEYOND_STEPS):
    """从窗口边上的这一格往窗口外走, 能不能走出 steps 步 —— 窗口边正好切在一片死湾中间时,
    湾里的格子也在窗口边上, 光看"在边上"会把死湾当活路(第五份录像刷怪区东边那片湾)。"""
    def outside(x, y):
        return not (box[0] < x < box[2] and box[1] < y < box[3])
    seen = {cell}
    frontier = [cell]
    for _ in range(steps):
        nxt = []
        for x, y in frontier:
            for dx, dy, _c in _STEPS:
                n = (x + dx, y + dy)
                if n in seen or not outside(*n) or not _free(bm, *n):
                    continue
                if dx and dy and not (_free(bm, x + dx, y) and _free(bm, x, y + dy)):
                    continue
                seen.add(n)
                nxt.append(n)
        if not nxt:
            return False
        frontier = nxt
    return True


def plan_flee(bm, me, chasers, crowd=(), horizon=HORIZON, margin=SAFE_MARGIN, prefer=None):
    """返回 {"dir": (ux, uy) 单位向量, "goal": 目标格, "lead": 早到几格, "safe": bool},
    规划不了(自己不在图上 / 没有追兵 / 一步都走不动)-> None, 调用方退回老办法。

    me / chasers / crowd: 小地图像素坐标(浮点)。chasers 是要躲的(究极), crowd 是别撞进去的
    怪群。safe=False 表示哪儿都不比追兵先到(被堵死), 挑的是早到最多(亏得最少)的格。
    prefer: 上一拍的 goal —— 它附近的格加分、安全线放宽, 别每拍换一条路。"""
    start = _snap(bm, me)
    if start is None:
        return None
    sx, sy = start[1]
    box = (sx - horizon, sy - horizon, sx + horizon, sy + horizon)

    seeds_c = []
    for c in chasers:
        snapped = _snap(bm, c)
        if snapped is not None:
            seeds_c.append(snapped)
    if not seeds_c:
        return None

    blocked = set()
    r = int(math.ceil(CROWD_BLOCK_R))
    for mx, my in crowd:
        for y in range(int(round(my)) - r, int(round(my)) + r + 1):
            for x in range(int(round(mx)) - r, int(round(mx)) + r + 1):
                if math.hypot(x - mx, y - my) <= CROWD_BLOCK_R:
                    blocked.add((x, y))
    blocked.discard(start[1])

    d_me, parent = _dijkstra(bm, [start], box, frozenset(blocked))
    if len(d_me) <= 1 and blocked:
        # 怪群贴脸, 身边一圈全被当成墙 —— 那就不绕了, 从怪群里挤出去也比站着强
        blocked = set()
        d_me, parent = _dijkstra(bm, [start], box)
    d_ch, _ = _dijkstra(bm, seeds_c, box)

    # 挑目标: 安全的活路(窗口边上、往外还走得出去) > 安全的死路里最远的 > 哪儿都不安全时
    # 早到最多(亏得最少)的。追兵跟人一样快, 进了死胡同迟早被堵住 —— 有活路先挑活路。
    ranked = []
    for cell, dm in d_me.items():
        if cell == start[1]:
            continue
        lead = d_ch.get(cell, math.inf) - dm
        kept = prefer is not None and math.hypot(cell[0] - prefer[0], cell[1] - prefer[1]) <= KEEP_R
        safe = lead >= (margin * 0.5 if kept else margin)
        if safe:
            score = (min(dm, horizon) + MARGIN_WEIGHT * min(lead, MARGIN_CAP)
                     + (KEEP_BONUS if kept else 0.0))
        else:
            score = lead
        on_edge = cell[0] in (box[0], box[2]) or cell[1] in (box[1], box[3])
        ranked.append((safe, score, on_edge, cell, lead))
    if not ranked:
        return None
    ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
    pick = None
    for safe, _score, on_edge, cell, lead in ranked:
        if not safe:
            break
        if on_edge and _escapes_beyond(bm, cell, box):
            pick = (cell, lead, safe)
            break
    if pick is None:
        _, _, _, cell, lead = ranked[0]
        pick = (cell, lead, ranked[0][0])
    goal, lead, safe = pick

    path = []
    cell = goal
    while cell is not None:
        path.append(cell)
        cell = parent[cell]
    path.reverse()                   # path[0] = start
    # 地图墙比游戏里厚一格, 人常"站在墙格上": 那时从格子中心量直线, 不然一步都看不远,
    # 只能朝一格外的点走(第五份录像回放: 走廊里朝上边的墙顶)
    origin = me if _free(bm, int(round(me[0])), int(round(me[1]))) else start[1]
    way = path[1]
    for cell in path[1:WAYPOINT_MAX + 1]:
        if _los(bm, origin, cell, blocked):
            way = cell
    dx, dy = way[0] - me[0], way[1] - me[1]
    n = math.hypot(dx, dy)
    if n < 1e-6:
        return None
    return {"dir": (dx / n, dy / n), "goal": goal, "lead": lead, "safe": safe}
