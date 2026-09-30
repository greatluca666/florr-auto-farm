from utils import *
from overlay import create_overlay
import argparse
import collections
import os
import signal
import stat
import sys
import threading
import cdp_bridge
import canvas_decode
import time
import random
import afk_watch
import enemy_detect
import utils
import app_config
import florr_settings
import os
import map_routes
import florr_server
import loadout_swap
import flee_planner

# ===== 索敌配置 (sszone敌怪检测/追击/规避) =====
ENEMY_SCAN_INTERVAL = 0.12  # 秒, 索敌扫描节流间隔. 这是"决策新鲜度"的主旋钮:
                              # 追击/规避途中每tick都拿这份决策里的怪坐标去moveTo,
                              # 间隔越大, 中间那几tick就越是照着旧坐标全速走 —— 怪
                              # 早挪窝了, 人一头撞上去. 实测一次推理≈0.1s(Mac MPS;
                              # Windows CUDA更快), 设0.12基本每帧都能重扫. 推理太慢
                              # 的机器上循环会被推理本身卡住, 那也没办法, 至少不比
                              # 大间隔更差. 漫游时每腿路另受move_to_position的
                              # max_attempts限制(见下方wander分支).
AVOID_TRIGGER_PX = 400      # 屏幕像素半径, AVOID怪进入此半径触发逃离
CAUTIOUS_HOLD_PX = 500      # 屏幕像素, CAUTIOUS怪(U沙尘暴等)保持的最小距离(不继续贴近). 250 实测太近, 等于直接撞上去
CHASE_MIN_CONF = 0.55      # 追击目标的最低置信度(幻影框过滤; 危险怪不受此限)
MYTHIC_LATCH_ENABLED  = True   # 贴脸有 Mythic 怪 → 锁定优先清掉再继续刷 (总开关)
MYTHIC_ENGAGE_PX      = 650    # Mythic 怪进此半径 → 锁定. 实测 --watch: 玩家眼里"贴脸"
                              # 的 Mythic 蝎子/甲虫 中心距其实 450~540px, 旧值 450 全卡在外
MYTHIC_RELEASE_PX     = 850    # 已锁定后, Mythic 出此半径才算脱离 (迟滞)
MYTHIC_RELEASE_MISSES = 3      # 连续多少次扫描没有合格 Mythic 才解锁
MYTHIC_STRAFE_RADIUS  = 180    # 甲虫/火蚁: 环绕它转圈的目标半径 (px)
MYTHIC_CACTUS_HOLD_PX = 220    # 仙人掌: 保持的距离 (px)
MYTHIC_STRAFE_K_RADIAL = 0.8   # 甲虫/火蚁环绕: 径向修正强度 (d 偏离半径时往里/外带多少)
# 以上数值是没实机测过的占位默认值, 实机跑一遍后再按观察到的效果调.
# ================================================

def lazy_heuristic(node1, node2):
    return math.sqrt((node1.x - node2.x) ** 2 + (node1.y - node2.y) ** 2)


class _Pt:
    def __init__(self, x, y):
        self.x, self.y = x, y


def line_of_sight(map, node1, node2):
    x0, y0 = node1.x, node1.y
    x1, y1 = node2.x, node2.y
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy

    while True:
        if map[y0][x0] == 0:
            return False
        if x0 == x1 and y0 == y1:
            return True
        e2 = err * 2
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy


def lazy_theta_star(map, start, goal):
    if start is None or goal is None:
        return None
    if map is None:
        return None
    
    class Node:
        def __init__(self, x, y, cost=math.inf, parent=None):
            self.x = x
            self.y = y
            self.cost = cost
            self.parent = parent

        def __lt__(self, other):
            return self.cost < other.cost
    
    open_list = []
    closed_list = set()
    start_node = Node(start[0], start[1], 0)
    goal_node = Node(goal[0], goal[1])
    heapq.heappush(open_list, (start_node.cost +
                   lazy_heuristic(start_node, goal_node), start_node))

    while open_list:
        _, current = heapq.heappop(open_list)
        if (current.x, current.y) in closed_list:
            continue
        closed_list.add((current.x, current.y))

        if current.x == goal_node.x and current.y == goal_node.y:
            path = []
            while current:
                path.append((current.x, current.y))
                current = current.parent
            return path[::-1]

        neighbors = [(current.x + dx, current.y + dy) for dx, dy in [(-1, 0),
                                                                     (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)]]
        for nx, ny in neighbors:
            if 0 <= nx < len(map[0]) and 0 <= ny < len(map) and map[ny][nx] == 255:
                neighbor = Node(nx, ny)
                if (nx, ny) not in closed_list:
                    if current.parent and line_of_sight(map, current.parent, neighbor):
                        new_cost = current.parent.cost + \
                            lazy_heuristic(current.parent, neighbor)
                        if new_cost < neighbor.cost:
                            neighbor.cost = new_cost
                            neighbor.parent = current.parent
                    else:
                        new_cost = current.cost + \
                            lazy_heuristic(current, neighbor)
                        if new_cost < neighbor.cost:
                            neighbor.cost = new_cost
                            neighbor.parent = current
                    heapq.heappush(open_list, (neighbor.cost +
                                   lazy_heuristic(neighbor, goal_node), neighbor))

    return None


class _NoPyautoguiPause:
    """这一段里的 pyautogui 调用不吃全局 PAUSE(默认每次调用后睡 0.1 秒)。

    不改全局: 寻路每 tick 的节奏(卡住判定的次数窗口)是按有 PAUSE 调出来的。只包住"本来就该一下子做完"的地方: 松键(reset_keyboard 6 次
    调用 = 白睡 0.6 秒, 鼠标还停在屏幕中心 —— 前几份录像每段路之间、每次交班人都原地停
    0.4~1 秒), 和走门那种要快速反馈的循环(第六份录像: 每拍 ~0.2 秒, 刹车来回振荡)。"""

    def __enter__(self):
        self.prev = pyautogui.PAUSE
        pyautogui.PAUSE = 0
        return self

    def __exit__(self, *exc):
        pyautogui.PAUSE = self.prev
        return False


def reset_keyboard():
    """把脚本可能按住的键全松开。

    shift 是 2026-09-24 补的: 防御键会按住它, 而这里原来只松 space/wasd —— 实机
    现象是 GUI 点"停止"之后**整台机器一直按着 shift**, 打字全变成大写。
    r 不在这里: 它是点一下就松的换装键, 从不按住。
    """
    with _NoPyautoguiPause():
        pyautogui.keyUp("space")
        pyautogui.keyUp("shift")
        keyup("w")
        keyup("a")
        keyup("s")
        keyup("d")


def release_keys():
    """只松攻击/防御键, **不动鼠标**。reset_keyboard 里的 keyup(w/a/s/d) 会把鼠标挪回屏幕
    中心 = 踩一脚刹车(utils.keyup); 该停的地方(到终点、贴洞口)要的就是这个, 但"交班"的地方
    不该停: 路上发现究极交给躲避、躲完交回寻路、进区交给刷怪 —— 第五份录像 01:10:38 究极在
    90px 外, 人在这一脚刹车里原地停了 1 秒。"""
    with _NoPyautoguiPause():
        pyautogui.keyUp("space")
        pyautogui.keyUp("shift")


_cleanup_done = False


def _release_everything():
    """收尾: 把键松开。可以重复调, 只真正跑一次。"""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    try:
        reset_keyboard()
    except Exception as e:      # 收尾路径上不许再抛, 否则连退出都退不干净
        print(f"⚠️ 退出时松键失败: {e}")


def install_shutdown_cleanup(*, register=None, on_signal=None, watch_stdin=None,
                             stdin_eof_exit=None):
    """进程收尾时保证键是松的。三道闸, 参数全可注入以便测试。

    实机(2026-09-24): GUI 点"停止"之后整台机器一直按着 shift。查下来两个原因叠在
    一起 —— reset_keyboard() 当时根本不松 shift(已修), 而且**没有任何路径会调它**:
    GUI 的注释写着"关 stdin 让 worker 自己 reset_keyboard 再退", 但 main.py 里一个
    stdin 监听都没有, 于是 Windows 上 3 秒后直接 TerminateProcess 硬杀。

      * stdin EOF —— GUI 停止 worker 的第一步。只在 GUI 明确要求时才监听(环境变量),
        否则 `nohup python main.py < /dev/null` 这种起法会在启动瞬间就读到 EOF 退出。
      * SIGTERM / SIGINT / SIGBREAK —— POSIX 上 GUI 会补一发 SIGTERM; Ctrl+C 也走这。
      * atexit —— 正常退出和未捕获异常。

    Windows 上最后那一下 proc.kill() 拦不住(TerminateProcess 不可捕获), 但前两道会
    在那之前 3 秒内就把进程收干净。
    """
    import atexit
    import signal as _signal
    import threading

    (register or atexit.register)(_release_everything)

    def _handler(_sig, _frame):
        _release_everything()
        os._exit(0)

    setter = on_signal or _signal.signal
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(_signal, name, None)
        if sig is None:
            continue
        try:
            setter(sig, _handler)
        except (ValueError, OSError):    # 非主线程 / 平台不支持
            pass

    want_stdin = (os.environ.get("FLORR_WORKER_STDIN_EOF_EXIT") == "1"
                  if stdin_eof_exit is None else stdin_eof_exit)
    if not want_stdin:
        return None

    def _watch():
        try:
            sys.stdin.read()             # 阻塞到对面把管子关掉
        except Exception:
            pass
        _release_everything()
        os._exit(0)

    t = threading.Thread(target=(watch_stdin or _watch), daemon=True)
    t.start()
    return t


def _move_mouse_safely(pos):
    """跟直接调用 pyautogui.moveTo(pos) 比, 多一层 FailSafeException 恢复.

    实机复盘(2026-09-20): 用户观察到蚁穴传送点附近角色和鼠标指针完全静止
    不动, 直到用户自己手动碰了一下鼠标才恢复正常; 同一份日志里
    lazy_theta_pathing 反复对着同一个目标"执行路径"、每一跳都报告成功,
    但复查读回来的位置从头到尾一模一样没变过, 跟"鼠标指针压根没在真的移动"
    这个观察完全吻合。

    pyautogui 默认开着 FAILSAFE: 只要**当前**鼠标位置停在屏幕四角之一,
    任何后续 pyautogui 调用都会在真正移动鼠标之前先抛 FailSafeException ——
    这条项目里从来没有任何地方捕获过这个异常。如果这就是那次卡死的真正
    原因, 之前的表现应该是: 异常要么把这条调用链默默中断在某个更外层的
    except Exception 里(不会崩溃退出, 但这一步之后的移动指令全部失效),
    要么确实整条链路崩了但复盘时没留意到报错。

    这里加一层防护: 抓到就打印清楚的诊断(下次实机复现能直接确认是不是
    这个原因), 临时关掉 FAILSAFE 把鼠标挪回屏幕中央再重新打开 —— 不确定
    这是不是真正原因, 但这段纯属防御性新增, 正常路径一次都不会碰到,
    不会比现在(完全没有任何处理)更差.
    """
    try:
        pyautogui.moveTo(pos)
    except pyautogui.FailSafeException:
        print("   ⚠️ pyautogui触发了FailSafe(鼠标停在屏幕角落) —— 挪回屏幕中央尝试恢复")
        pyautogui.FAILSAFE = False
        try:
            pyautogui.moveTo((SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2))
        finally:
            pyautogui.FAILSAFE = True


# move_to_position 认为"到了"的半径(小地图格). 任何想让角色**真的挪窝**的目标点,
# 都必须比这个远 —— 比它近的目标, move_to_position 一进循环就 return True, 一个
# moveTo 都不发。实机 2026-09-24 栽在这上面: 刷怪区外一格的花朵被拽向 3 格外的
# 边内点, 每次寻路都"成功"、每次都没动, lazy_theta_pathing 原地重规划 90 次 /118 秒。
ARRIVE_RADIUS = 5

# 路径跑完、人却既没进区域也没挪窝, 连续这么多次 -> 当卡住处理(脱困再规划),
# 而不是以 1.3Hz 无限重复同一次无效寻路。
NO_PROGRESS_EPSILON = 2.0
NO_PROGRESS_LIMIT = 3

# 离路点这么近(同一格或相邻格)就直接算到, 不再要求直线看得到下一个路点 —— 站在
# 路点上时规划器已经保证了两点之间直线可走, 剩下的只是位置读数的 1 格抖动.
WAYPOINT_SNAP_RADIUS = 1.5


def _can_turn_to_next(binary_map, current_pos, next_pos, dist):
    """离当前路点已经 <ARRIVE_RADIUS 了, 能不能现在就转向下一个路点.

    蚁穴密道只有 2 像素宽、还带弯, 路点之间只隔 1~5 格。只看"离路点 <5"的话, 还没
    拐过弯就转头直奔下一个路点, 直线穿墙, 原地打转判卡住(离线模拟: 出生点→刷怪区
    必经两条密道, 次次卡在里面)。所以提前转向的前提是从这里直线能看到下一个路点.
    """
    if next_pos is None or binary_map is None or dist < WAYPOINT_SNAP_RADIUS:
        return True
    return line_of_sight(binary_map, _Pt(*current_pos), _Pt(*next_pos))


STEER_SEARCH_LIMIT = 3000


def _steer_point(binary_map, current_pos, target_pos):
    """鼠标该朝哪个点推. 直线看得到目标就朝目标; 看不到(被挤偏了半格, 或者冲过头
    拐进了墙角)就在可走图上 BFS 一条到目标的格子路, 朝路上最远、从这里直线看得到的
    那个点推 —— 不然照直线顶着墙推, 原地打转直到判卡住.
    """
    if binary_map is None or current_pos == target_pos:
        return target_pos
    here, goal = _Pt(*current_pos), _Pt(*target_pos)
    if line_of_sight(binary_map, here, goal):
        return target_pos
    h, w = binary_map.shape[:2]
    parent = {current_pos: None}
    queue = collections.deque([current_pos])
    while queue and len(parent) < STEER_SEARCH_LIMIT:
        cx, cy = queue.popleft()
        if (cx, cy) == target_pos:
            break
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
            nxt = (cx + dx, cy + dy)
            if nxt not in parent and 0 <= nxt[0] < w and 0 <= nxt[1] < h \
                    and binary_map[nxt[1]][nxt[0]] == 255:
                parent[nxt] = (cx, cy)
                queue.append(nxt)
    if target_pos not in parent:
        return target_pos
    route = []
    node = target_pos
    while node is not None:
        route.append(node)
        node = parent[node]
    for node in route:
        if line_of_sight(binary_map, here, _Pt(*node)):
            return node
    return target_pos


def move_to_position(current_pos, target_pos, max_attempts=200, stall_limit=13,
                     progress_epsilon=1.5, on_tick=None, next_pos=None, binary_map=None):
    """移动到目标位置.

    next_pos / binary_map: 走路径时的下一个路点和可走图. 给了的话, "到了"(离目标
    <ARRIVE_RADIUS 或冲过头)还要满足 _can_turn_to_next, 否则继续往目标本身走.

    on_tick: 可选回调, 每个内循环 tick(moveTo 之后、sleep 之前)调一次, 传入当前
    minimap 坐标. 返回真值 → 立刻收手, move_to_position 把那个真值原样返回给调用方
    (约定用短字符串, 比如 "enemy"). 给 auto_farming 的 wander 腿用: 这函数是阻塞的,
    整段(max_attempts×0.05s)期间外层拿不回控制权、跑不了索敌, 快怪冲过来就撞死 ——
    钩子让 wander 途中也能触发一次索敌、需要接战/规避时中断这条腿. on_tick=None
    (execute_path / lazy_theta_pathing 那些纯赶路调用)时行为跟以前完全一样.

    跟原版(github.com/Shiny-Ladybug/florr-auto-pathing)的go_direction比对后, 补回了
    两条它有而我们这版"简化版本"漏掉的关键判定 —— 之前只看"到没到5px内", 不看有
    没有在朝目标靠近, 导致过头或者原地打转都要死等到max_attempts才认卡住:
      - 冲过头(这次比上次离目标还远) —— 已经很接近了, 直接算到达, 别死磕这一段.
      - 连续stall_limit次距离都没缩短(原地打转) —— 才真正判定为卡住, 不是简单数
        循环次数. max_attempts只是保底上限, 防止极端情况死循环, 平时基本不会撞到.

    原版(以及我们最早抄过来那版)这两条判定都是用"距离完全相等"(dist == last_dist)
    做比较 —— 实测位置检测本身有量化噪声, 连续两帧distance几乎不可能位级精确相等,
    导致卡在死角/洞里时stall_count永远攒不起来, "卡住"判定形同虚设, 角色能在原地
    干耗到天荒地老。改用progress_epsilon容差带: 只要没有明显缩短(缩短量小于
    progress_epsilon)就算一次停滞, 不再要求毫厘不差.
    """
    if current_pos is None or target_pos is None:
        return "stuck"

    last_dist = None
    best_dist = None
    stall_count = 0
    attempts = 0
    while attempts < max_attempts:
        if afk_watch.poll_afk_pause():
            overlay.update(state="AFK弹窗处理中", message="等待florr-auto-afk解题")
            time.sleep(0.2)
            # 暂停期间角色可能被上一次鼠标指令继续带着走(这游戏靠鼠标位置转向,
            # 不是靠按键状态) —— 暂停12秒后dist跟last_dist已经没有可比性了,
            # 不清零的话很容易被误判成"冲过头"直接算到达. 清成跟函数开头一样的
            # 初始状态, 让暂停后第一个真实tick当"刚开始移动"处理.
            last_dist = None
            best_dist = None
            stall_count = 0
            continue

        current_pos = get_player_position()
        if current_pos is None:
            # 以前这里不打印, 复盘日志里"移动到X"后面直接跟"卡住了", 分不清是
            # 位置检测丢失(跟外层lazy_theta_pathing重试打的是同一类问题)还是
            # 真的原地打转(stall_count超限, 见下面) —— 两种原因、两种修法.
            print(f"   ⚠️ 移动中丢失玩家位置(目标 {target_pos})")
            overlay.update(state="无法检测位置", message="移动中丢失玩家位置")
            return "stuck"

        # 计算方向
        dx = target_pos[0] - current_pos[0]
        dy = target_pos[1] - current_pos[1]
        dist = math.sqrt(dx**2 + dy**2)

        overlay.update(state="移动中", pos=current_pos, target=target_pos)

        can_turn = _can_turn_to_next(binary_map, current_pos, next_pos, dist)

        # 如果已到达目标
        if dist < ARRIVE_RADIUS and can_turn:
            reset_keyboard()
            return True

        if last_dist is not None:
            if (dist > last_dist + progress_epsilon and can_turn
                    and last_dist <= 2 * ARRIVE_RADIUS):
                # 明显冲过头了, 已经足够接近, 当作到达, 不继续死磕这一段. "足够接近"要真的
                # 近: 离目标还有十几格时读数一抖(卡墙角时被吸附到两个点上来回跳)也会"变远",
                # 那不是冲过头。
                reset_keyboard()
                return True
            elif dist < best_dist - progress_epsilon:
                # 比上次记进展时又近了一截 —— 真有进展, 停滞计数清零, 从这里重新量。
                best_dist = dist
                stall_count = 0
            else:
                # 没比上次记进展时近出一截 = 没有实质进展。原来跟"上一拍"比会被骗: 人卡在
                # 墙角不动, 读到的位置被吸附到两个可走点上来回跳, 距离一小一大, 每次"缩短"
                # 都清零 —— 2026-09-28 第四份录像里一段路这样走了 3 分钟。累计着比, 慢慢
                # 走(每拍不到 progress_epsilon)也能攒够, 不会被误判。
                stall_count += 1
        last_dist = dist
        if best_dist is None:
            best_dist = dist

        if stall_count > stall_limit:
            reset_keyboard()
            print(f"   ⚠️ 原地打转{stall_count}次, 判定卡住(dist={dist:.1f})")
            overlay.update(state="卡住", message=f"原地打转{stall_count}次")
            return "stuck"

        # 移动鼠标指向目标(看不到目标时指向绕行点, 见 _steer_point)
        aim = _steer_point(binary_map, current_pos, target_pos)
        adx, ady = aim[0] - current_pos[0], aim[1] - current_pos[1]
        aim_dist = math.hypot(adx, ady)
        extend = max(min(aim_dist * 45, 500), 50) * mouse_scale()
        if aim_dist > 0:
            extend_x = extend * adx / aim_dist
            extend_y = extend * ady / aim_dist
        else:
            extend_x = extend_y = 0

        mouse_pos = clamp_to_screen(SCREEN_WIDTH // 2 + extend_x, SCREEN_HEIGHT // 2 + extend_y)
        _move_mouse_safely(mouse_pos)

        # 检查游戏状态 —— 不用check_stage(): 它的in_game_dead/in_menu判定靠
        # 探测固定像素点是不是某个精确RGB, 连1920x1080参照分辨率下都从没真正
        # 触发过(见on_death_screen()的注释), 2026-08-26在1280x923上实测过更是
        # 直接把"正常游戏中"误判成"in_game_dead"(探测点landed在别的白色UI上)。
        # on_death_screen()/on_start_screen()是靠采样一块区域算颜色占比, 已经
        # 经过缩放+实机验证, 顶层重试循环(lazy_theta_pathing)一直用的就是这套,
        # 这里跟着统一, 不再有两条不一致的死亡检测逻辑.
        if on_death_screen():
            reset_keyboard()
            overlay.update(state="已死亡")
            return "in_game_dead"
        elif on_start_screen():
            reset_keyboard()
            overlay.update(state="菜单中")
            return "in_menu"

        if on_tick is not None:
            signal = on_tick(current_pos)
            if signal:
                release_keys()      # 交班(躲究极 / 进区了 / 超时), 不踩刹车, 见 release_keys
                return signal

        attempts += 1
        time.sleep(0.05)

    reset_keyboard()
    print(f"   ⚠️ {max_attempts}次尝试后仍未到达(保底上限, 平时不该撞到这条)")
    overlay.update(state="卡住", message=f"{max_attempts}次尝试后仍未到达")
    return "stuck"


def execute_path(path, stop_when=None, binary_map=None, enemy_watch=None, deadline=None):
    """执行路径。

    stop_when: (当前坐标) -> bool, 走的过程中**每个 tick** 都问一次"够了没", 回 True
        就立刻停、不走剩下的路(返回 True)。寻路回刷怪区用它: 进了区就停, 不用非得走到
        路径终点(用户 2026-09-25)。原来只在两段之间问 —— 出区一格的拉回路只有一段,
        照样走到底、往里多走 9 格(实机 2026-09-27)。挂在 move_to_position 的 on_tick
        上, 用它每 tick 本来就读的坐标, 不多截屏。
    binary_map: 规划用的可走图. 给了才会等看得到下一跳再转向(_can_turn_to_next)、
        看不到目标时顺着可走格子绕过去(_steer_point); 窄密道全靠这两条.
    enemy_watch: _PathingEnemyWatch. 给了就每个 tick 问一次"要不要躲究极", 要躲就停下
        返回 "enemy", 由 lazy_theta_pathing 躲开后重新规划。
    deadline: time.time() 口径的时间点, 过了就在这一段中途收手返回 "timeout" —— 原来
        只在两段之间查, 一段走不完就能超出预算 94 秒(第四份录像)。
    """
    if path is None or len(path) == 0:
        return "stuck"
    
    print(f"🗺️  执行路径，共 {len(path)} 个节点...")
    
    on_tick = None
    if stop_when is not None or enemy_watch is not None or deadline is not None:
        def on_tick(pos):
            if stop_when is not None and stop_when(pos):
                return "reached"
            if deadline is not None and time.time() >= deadline:
                return "timeout"
            if enemy_watch is not None and enemy_watch.should_flee():
                return "enemy"
            return None

    for i in range(len(path) - 1):
        current = path[i]
        next_point = path[i + 1]
        
        print(f"   [{i+1}/{len(path)-1}] 移动到 {next_point}")
        after = path[i + 2] if i + 2 < len(path) else None
        result = move_to_position(current, next_point, next_pos=after, binary_map=binary_map,
                                  on_tick=on_tick)
        if result == "reached":
            print("   ✅ 已进入目标区域, 剩下的路不走了")
            return True
        if result in ("enemy", "timeout"):
            return result
        if result == "stuck":
            print(f"   ⚠️ 在 {next_point} 卡住了")
            return "stuck"
        elif result in ["in_game_dead", "in_menu"]:
            return result
    
    print("✅ 路径执行完成")
    return True


PATH_FLEE_MAX_S = 10.0   # 寻路途中躲一次究极最多躲这么久, 之后照样回去重新规划


class _PathingEnemyWatch:
    """寻路途中的索敌: 跟刷怪同一套节流扫描(_maybe_scan_enemies), 只关心 flee。
    chase / swarm / wander 一律不管 —— 用户 2026-09-28: 路上只躲究极, 普通怪不管。"""

    def __init__(self):
        self.decision = ("wander", None)
        self.detections = []
        self.last_scan = 0.0

    def should_flee(self):
        self.decision, self.detections, self.last_scan, _ = _maybe_scan_enemies(
            True, time.time(), self.last_scan, self.decision, self.detections)
        return self.decision[0] == "flee"


def _describe_threats(watch):
    """日志用: 触发躲避的究极离多远、是不是冲过来的。第五份录像里路上的躲避比离线重放晚了
    1.5 秒(究极 88px 才躲), 日志里没有这几个数就查不出是没扫到还是没判成"冲过来"。"""
    try:
        center = enemy_detect.current_center()
        trig = {tuple(p) for p in watch.decision[1]}
        parts = []
        for d in getattr(watch, "detections", []):
            if tuple(d["screen_pos"]) in trig:
                dist = math.hypot(d["screen_pos"][0] - center[0], d["screen_pos"][1] - center[1])
                parts.append(f"{d['species']}({d['rarity']}) {dist:.0f}px"
                             + (" 冲过来" if d.get("approaching") else ""))
        return ", ".join(parts) or "?"
    except Exception:
        return "?"


def _flee_while_pathing(watch, max_s=None):
    """躲到 watch 不再说要躲(或最多 PATH_FLEE_MAX_S 秒)。躲法跟刷怪时一样:
    flee_mouse_target 背离究极 + _steer_clear_of_walls 别往墙里跑。"""
    if max_s is None:
        max_s = PATH_FLEE_MAX_S
    print(f"⚠️ 寻路途中有究极靠近, 先躲开 ({_describe_threats(watch)})")
    overlay.update(state="规避中", message="寻路途中躲究极")
    binary_map = load_binary_map()
    history = []
    end = time.time() + max_s
    try:
        while time.time() < end:
            if on_death_screen() or on_start_screen():
                return
            if not watch.should_flee():
                return
            pos = get_player_position()
            center = enemy_detect.current_center()
            target = _FLEE_PLAN.target(watch.decision[1], getattr(watch, "detections", []),
                                       center, pos, binary_map)
            # 读不到位置时别往卡住检测里塞 None
            _drive_and_check_stall(target, pos, history, "规避中", "寻路途中躲究极",
                                   center=center, track_stall=pos is not None)
    finally:
        release_keys()      # 躲完交回寻路, 不踩刹车(见 release_keys)


_WRONG_MAP_SEEN = None   # 这一轮寻路发现人被传到了别的图(区域名), run_worker 据此不记短局


def _on_wrong_map():
    """画布 HUD 区域名说人不在寻路图(utils.MAP)上 -> 那张图的名字, 并记进 _WRONG_MAP_SEEN;
    在 / 读不到 / 认不出 -> None(认不出的图不猜, 老行为)。"""
    global _WRONG_MAP_SEEN
    zone = canvas_zone_map()
    if zone is None or zone == utils.MAP:
        return None
    _WRONG_MAP_SEEN = zone
    return zone


def lazy_theta_pathing(location, area=[], deadline=None, enemy_watch=None):
    """寻路到目标区域. 检测不到位置、或者移动卡住, 都不放弃, 一直重试
    (脱困后重新规划路径)直到真的到达/玩家死亡/进了菜单为止。

    deadline: None(默认)= 上面那个"永不放弃", 跟以前一字不变 —— **刷怪那条路
        只能传 None**。这个无限重试是当年特意从上游恢复回来的行为(以前太容易
        放弃), 不能削。
        传了一个 time.time() 口径的时间戳, 则过了这个点就返回 False。只有
        _run_entry_route(进场路线)会传: 蚁穴的洞口被怪堵着 / 坐标标偏了 /
        花园的玩家标记色跟沙漠不一样时, 这里要是不放弃, worker 会永远待在花园
        里 —— 不超时、不记短局、也就永远不会换服重试。进场这一段放弃、重开一轮
        (甚至换台服务器)严格优于原地耗着, 这也正是设计里承诺的自愈路径。
    enemy_watch: _PathingEnemyWatch 或 None。给了就边走边看究极, 要躲就先躲开
        (_flee_while_pathing)再从新位置重新规划 —— 用户 2026-09-28: 路上也躲究极,
        普通怪不管。第二份录像 8 次死亡里 3 次死在去刷怪区的路上。
    """
    retry_count = 0
    no_progress = 0

    while True:
        if deadline is not None and time.time() >= deadline:
            print("⏰ 本段寻路的预算用完了, 交回上层")
            overlay.update(state="出错", message="寻路预算用完, 交回上层")
            return False

        if afk_watch.poll_afk_pause():
            overlay.update(state="AFK弹窗处理中", message="等待florr-auto-afk解题")
            time.sleep(0.2)
            continue

        # 死亡/开局画面的检查必须放在最前面、且不能只在"pos is None"分支里做 ——
        # 实机踩过坑: 死亡结算画面上凑巧有个像素跟玩家标记色对上了, 稳定测出一个
        # 假位置(不是None!), 导致下面那个"pos is None才查死亡画面"的分支永远
        # 进不去, 角色明明卡在死亡画面上, 脚本还在拿假坐标一遍遍重新规划路径。
        # 不管这轮测没测到位置, 每次循环开头都先确认没有落在这两个画面上.
        dead, at_menu = on_death_screen(), on_start_screen()
        if dead or at_menu:
            # 拆开打印是哪一个 —— 以前两条判断并成一句话, 复盘日志分不清是真死了
            # 还是"开局菜单"误判(比如花园这种整体偏绿的生态区, 固定屏幕坐标那块
            # 绿色占比检测更容易撞上世界渲染本身的颜色而不是真的开局按钮)。
            where = "死亡结算画面" if dead else "开局菜单"
            print(f"🔁 检测到落在{where}上, 交回上层处理")
            overlay.update(state="出错", message=f"落在{where}, 交回上层重开")
            return False

        # 人其实不在这张寻路图上(被传送门传走了)—— 拿这张图接着走只会一直"卡住"。
        # 第六份录像: 蚁穴复活后站进回花园的门被传走, 拿蚁穴的图在花园寻路 13 分钟。
        wrong = _on_wrong_map()
        if wrong is not None:
            print(f"🗺️ 画布区域名显示人在 {wrong}, 不在寻路图 {utils.MAP} 上 —— 交回上层重走进场路线")
            overlay.update(state="出错", message=f"人在 {wrong}, 不是 {utils.MAP}, 重走进场路线")
            return False

        pos = get_player_position()

        if pos is None:
            # 死亡/开局画面已经在循环开头查过了, 到这里还是None就是真的暂时没
            # 认出玩家标记(截图抖动之类), 单纯重试.
            retry_count += 1
            print(f"⚠️ 无法检测玩家位置，持续重试中 (第{retry_count}次)...")
            # 只在小状态框 + 控制台/GUI 日志里提示 —— 以前 retry_count>7 会弹一个
            # 屏幕正中央的大号黄色警告窗, 结果那个窗盖住了小地图, get_player_position()
            # 截图截到的是警告窗本身, 位置永远认不回来, 警告窗也就再也不消失. 死循环.
            hint = ("持续重试中 (第{}次)".format(retry_count) if retry_count <= 7
                    else "第{}次仍测不到 —— 检查地图是否被放大(M键) / 窗口是否全屏(F11)".format(retry_count))
            overlay.update(state="无法检测位置", message=hint)
            time.sleep(1)
            continue

        retry_count = 0
        if area and if_in_area(area, pos):
            # 已经在目标区域里 —— 不用非得走到 location 那个点(用户 2026-09-25: 实机
            # 每轮开局人本来就在刷怪区里, 却照样往配置点走, 走到了还判卡住)。
            print(f"✅ 已在目标区域内, 位置: {pos}\n")
            overlay.update(state="完成", pos=pos, message="已在目标区域内")
            return True
        print(f"\n📍 寻路: {pos} -> {location}")
        overlay.update(state="寻路中", pos=pos, target=location, message="规划路径...")
        time_now = time.time()

        binary_map = load_binary_map()
        if binary_map is None:
            print("❌ 地图加载失败")
            overlay.update(state="出错", message="地图加载失败")
            return False

        path = lazy_theta_star(binary_map, pos, location)
        print(f"⏱️  寻路耗时: {time.time() - time_now:.2f}秒")

        if path is None:
            print("❌ 路径规划失败")
            overlay.update(state="出错", message="路径规划失败")
            return False

        print(f"✅ 找到路径，共 {len(path)} 个点")
        overlay.update(message=f"找到路径, 共{len(path)}个点")
        def _inside(here):
            return bool(area) and here is not None and if_in_area(area, here)

        stat = execute_path(path, stop_when=_inside, binary_map=binary_map,
                            enemy_watch=enemy_watch, deadline=deadline)
        if stat == "enemy":
            _flee_while_pathing(enemy_watch)
            continue

        # 检查是否到达目标区域
        current_pos = get_player_position()
        if current_pos and if_in_area(area, current_pos):
            print(f"✅ 已到达目标区域！位置: {current_pos}\n")
            overlay.update(state="完成", pos=current_pos, message="已到达目标区域")
            return True

        if current_pos == location:
            print(f"✅ 已到达目标位置！\n")
            overlay.update(state="完成", pos=current_pos, message="已到达目标位置")
            return True

        # execute_path 说"跑完了"不等于真的走了路: 目标落在 ARRIVE_RADIUS 以内时,
        # move_to_position 一个 moveTo 都不发就 return True。那种情况下人既没进区域
        # 也没挪窝, 而下面的 stat 又不是 "stuck" —— 循环会原地重复同一次无效寻路,
        # 靠玩家自己飘进区域才出得来(实机: 90 次 / 118 秒)。"永不放弃"是不许 return
        # False, 不是不许换个招 —— 所以这里并进下面的脱困分支, 而不是退出。
        if current_pos is not None and math.dist(current_pos, pos) < NO_PROGRESS_EPSILON:
            no_progress += 1
        else:
            no_progress = 0

        if stat == "stuck" or no_progress >= NO_PROGRESS_LIMIT:
            # 这里是本函数唯一一条"无限循环"的路: 卡住 -> 脱困 -> 重新规划。带预算
            # 时在开一次新的脱困动作(它自己还要花好几秒)之前先看一眼钟, 别让一次
            # 脱困把预算拖过头。
            if deadline is not None and time.time() >= deadline:
                print("⏰ 本段寻路的预算用完了, 不再脱困重试, 交回上层")
                overlay.update(state="出错", message="寻路预算用完, 交回上层")
                return False
            if stat == "stuck":
                print("🔄 检测到卡住, 脱困后重新规划路径...")
                overlay.update(state="卡住", message="脱困中, 稍后重新寻路")
            else:
                print(f"🔄 连续{no_progress}次寻路都没挪窝(目标 {location} 可能太近), "
                      f"脱困后重新规划路径...")
                overlay.update(state="卡住", message="寻路没挪窝, 脱困中")
            no_progress = 0
            execute_anti_stuck()
            continue

        if on_death_screen():
            print("💀 玩家已死亡")
            overlay.update(state="已死亡")
            return False
        elif on_start_screen():
            print("📋 玩家在菜单中")
            overlay.update(state="菜单中")
            return False


def random_walkable_point(area, binary_map, max_tries=20):
    """在矩形区域内随机采样一个可走点(binary_map里=255的), 而不是纯瞎猜坐标.

    之前是在整个矩形里直接randint, 完全不管地图形状 —— 采样到墙里/区域外形状
    (不是每个刷怪区域都是实心矩形)的点很常见, 角色会直接顶着墙走不过去。
    这里改成拒绝采样: 采到墙就重来, max_tries次都不行就退回原来的随机点
    (兜底, 不会因为极端形状的区域卡死采样).
    """
    (x1, y1), (x2, y2) = area
    for _ in range(max_tries):
        x = random.randint(x1, x2)
        y = random.randint(y1, y2)
        if binary_map is not None and 0 <= y < binary_map.shape[0] and 0 <= x < binary_map.shape[1]:
            if binary_map[y, x] == 255:
                return x, y
    return random.randint(x1, x2), random.randint(y1, y2)


def _line_walkable(binary_map, a, b, step=0.5):
    """小地图上 a -> b 这条直线是不是全在可走像素上(每半格采一个点)。"""
    rows, cols = binary_map.shape[:2]
    (ax, ay), (bx, by) = a, b
    n = max(1, int(math.hypot(bx - ax, by - ay) / step))
    for i in range(n + 1):
        x = int(round(ax + (bx - ax) * i / n))
        y = int(round(ay + (by - ay) * i / n))
        if not (0 <= x < cols and 0 <= y < rows) or binary_map[y, x] != 255:
            return False
    return True


def _line_of_sight_reach(binary_map, area, map_pos):
    """给 select_action 的 can_reach: 300px 外的怪值不值得追 —— 它在刷怪区里, 而且小地图上
    从自己到它是一条没墙的直线。当初 300px 一刀切就是怕这两件事(追过去顶墙 / 追出刷怪带被
    拉回来)。这张图没实测小地图换算 / 没地图 / 不知道自己在哪 -> None(= 不远追, 老行为)。"""
    s = utils.MINIMAP_WORLD_SCALE.get(utils.MAP)
    if s is None or binary_map is None or map_pos is None:
        return None

    def can_reach(det):
        w = det.get("world")
        if w is None:
            return False
        mx, my = s * (w[0] + 1000.0), s * (w[1] + 1000.0)
        if not if_in_area([area], (mx, my)):
            return False
        return _line_walkable(binary_map, map_pos, (mx, my))
    return can_reach


def _in_farm_area(area):
    """给 select_action 的 in_area: 这只怪(世界坐标换成小地图坐标)在不在刷怪区里。没有世界
    坐标的不拦; 这张图没实测小地图换算 -> None(不筛, 老行为)。"""
    s = utils.MINIMAP_WORLD_SCALE.get(utils.MAP)
    if s is None or not area:
        return None

    def inside(det):
        w = det.get("world")
        if w is None:
            return True
        return bool(if_in_area([area], (s * (w[0] + 1000.0), s * (w[1] + 1000.0))))
    return inside


_APPROACH = enemy_detect.ApproachTracker()   # 跨扫描跟踪究极是不是在冲过来, 进程内一份

# 躲开了就多躲一会儿: 第三份录像(2026-09-28)里究极一退出 200px 决策就不躲了, 漫游/寻路又
# 走回它身边 -> 再躲 -> 再走回去, 来回四次把血磨光。开始躲之后, 要等 AVOID 怪都退到
# FLEE_RELEASE_PX 外才放开; 最后一次真躲之后最多这样多躲 FLEE_LATCH_MAX_S 秒, 免得被一只
# 不走的究极一直吊着。
FLEE_RELEASE_PX = 450
FLEE_LATCH_MAX_S = 6.0


class _FleeLatch:
    def __init__(self):
        self.last_flee = None

    def apply(self, decision, detections, center, now):
        if decision[0] == "flee":
            self.last_flee = now
            return decision
        if self.last_flee is not None and now - self.last_flee <= FLEE_LATCH_MAX_S:
            cx, cy = center
            near = [d["screen_pos"] for d in detections
                    if enemy_detect.classify_action(d["species"], d["rarity"]) == "AVOID"
                    and math.hypot(d["screen_pos"][0] - cx, d["screen_pos"][1] - cy)
                    <= FLEE_RELEASE_PX]
            if near:
                return ("flee", near)
        return decision


_FLEE_LATCH = _FleeLatch()


def _maybe_scan_enemies(enemy_ai_enabled, now, last_enemy_scan, prev_decision, prev_detections,
                        can_reach=None, in_area=None):
    """索敌节流 + 总开关. 返回 (decision, detections, last_enemy_scan, scanned).

    - enemy_ai_enabled=False: 永远返回漫游决策 + 空检测列表, 一次都不碰 enemy_detect.
    - 距上次扫描不到 ENEMY_SCAN_INTERVAL: 沿用上一轮的 decision 和 detections.
    - 到点了: 跑一次 scan_enemies + select_action; 任何异常 → 漫游 + 空列表.
    detections 单独回传是给 Mythic 近身锁定用的 (select_action 不看这个).
    scanned 只在真跑了一次 scan_enemies 的分支为 True (含扫描抛错 —— 尝试过一次
    观测就算数); 关掉索敌 / 节流跳过的 tick 是 False. 调用方靠这个只在新鲜扫描上
    推进 Mythic miss 计数, 别让节流 tick 拿同一份缓存检测重复扣数.
    """
    if not enemy_ai_enabled:
        return ("wander", None), [], last_enemy_scan, False
    if now - last_enemy_scan < ENEMY_SCAN_INTERVAL:
        return prev_decision, prev_detections, last_enemy_scan, False
    last_enemy_scan = now
    try:
        detections = enemy_detect.scan_enemies()
        # 给究极打"冲过来"标记(跨扫描跟踪), select_action 用它在 avoid_early_px 内提前躲。
        # 跟踪是附加的: 它出错只是这拍不提前躲, 不能把整次决策拖成漫游。
        try:
            _APPROACH.update(detections, enemy_detect.last_player_world(), now)
        except Exception:
            pass
        decision = enemy_detect.select_action(
            detections,
            avoid_trigger_px=enemy_detect.avoid_trigger_px_for(utils.MAP, AVOID_TRIGGER_PX),
            avoid_early_px=enemy_detect.avoid_early_px_for(utils.MAP),
            can_reach=can_reach, in_area=in_area,
            cautious_hold_px=CAUTIOUS_HOLD_PX,
            center=enemy_detect.current_center(),
            chase_min_conf=CHASE_MIN_CONF,
            target_policy=enemy_detect.target_policy_for(utils.MAP),
        )
        decision = _FLEE_LATCH.apply(decision, detections, enemy_detect.current_center(), now)
    except Exception as e:
        print(f"⚠️ 索敌出错, 本轮当漫游处理: {e}")
        decision, detections = ("wander", None), []
    return decision, detections, last_enemy_scan, True


def _update_mythic_latch(latched, misses, has_target, release_misses):
    """Mythic 近身锁定的状态机 (纯函数). has_target = 这次扫描有没有合格的近身
    Mythic. 有 → 锁定, misses 清零. 没有且已锁定 → misses+1, 攒够 release_misses
    就解锁 (迟滞, 扛检测闪烁). 返回 (latched, misses)."""
    if has_target:
        return True, 0
    if not latched:
        return False, 0
    misses += 1
    if misses >= release_misses:
        return False, 0
    return True, misses


def _drive_and_check_stall(mouse_target, current_pos, chase_pos_history, state, message,
                           center=None, track_stall=True):
    """chase / flee / 清青怪 三条分支共用的"卡住检测 + 出手"收尾.

    mouse_target == center 是"刻意停在这" (保持距离 / 合力抵消), 不算移动 —— 这种
    tick 不往 history 塞样本 (留给下一个真在动的 tick). 其余情况: 攒近期 minimap
    坐标, 时间窗内净位移不足 → execute_anti_stuck() 接管这一 tick、返回 "stuck";
    否则 overlay 更新 + moveTo + sleep, 返回 "moved".

    center 就是那几个 *_mouse_target 函数收到的同一个中心(玩家在画面上的位置) ——
    它们在"保持距离"时原样返回中心, 所以这里必须拿同一个值比, 不能比 SCREEN_CENTER:
    真实锚点在地图边界/非全屏时跟屏幕中心差上百像素, 比错了会把"刻意停住"当成
    "在移动", 于是卡住检测每隔几 tick 就误判一次脱困。

    track_stall=False(绕圈打蠕虫): 不记样本, 还清掉旧的。绕圈的净位移最多一个直径, 蚁穴
    里半径 60 屏幕像素只合 ~3 格小地图, 低于 chase_is_stalled 的 4 格 —— 喂进去必定误判。
    代价: 绕圈时真顶在墙上也不脱困, 由蠕虫定时钻地(目标消失)兜底。"""
    if center is None:
        center = enemy_detect.current_center()
    if not track_stall:
        chase_pos_history.clear()
    elif mouse_target != center:
        chase_pos_history.append(current_pos)
        if len(chase_pos_history) > enemy_detect.CHASE_STALL_WINDOW:
            chase_pos_history.pop(0)
        if enemy_detect.chase_is_stalled(chase_pos_history):
            print(f"⚠️ {state}途中卡住, 脱困一下...")
            overlay.update(state="卡住", message=f"{state}卡住, 脱困中")
            execute_anti_stuck()
            chase_pos_history.clear()
            return "stuck"
    overlay.update(state=state, pos=current_pos, message=message)
    with _NoPyautoguiPause():      # 追/躲/遛每拍一次, 别再白睡 0.1 秒(见 _NoPyautoguiPause)
        pyautogui.moveTo(clamp_to_screen(*mouse_target))
    time.sleep(0.05)
    return "moved"


# 躲怪时前方至少要有这么多格(地图像素)能走, 才算"这个方向跑得出去"。自己和兵蚁都是
# ~300 世界单位/秒, 蚁穴一格 ≈ 217 世界单位 -> 3 格 ≈ 2 秒的路。
FLEE_WALL_CLEARANCE = 3
_FLEE_TURN_STEPS_DEG = (0, 15, -15, 30, -30, 45, -45, 60, -60, 75, -75, 90, -90)


def _steer_clear_of_walls(mouse_target, center, map_pos, binary_map,
                          clearance=FLEE_WALL_CLEARANCE):
    """flee_mouse_target 只算"背离危险怪", 不看墙。2026-09-28 蚁穴录像: 往左下躲究极兵蚁,
    一头顶在刷怪带下沿(速度 304 -> 11), 被贴身打死。

    从理想方向开始左右各偏最多 90°(再多就是往怪那边跑了), 取第一个前方 clearance 格
    都能走的方向, 长度不变。屏幕和小地图同朝向(北在上), 方向可以直接套到地图上。
    都走不通 / 没地图没位置 / 本来就停着 -> 原样返回。"""
    if map_pos is None or binary_map is None or mouse_target == center:
        return mouse_target
    dx, dy = mouse_target[0] - center[0], mouse_target[1] - center[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return mouse_target
    base = math.atan2(dy, dx)
    rows, cols = binary_map.shape[:2]
    px, py = map_pos
    for deg in _FLEE_TURN_STEPS_DEG:
        a = base + math.radians(deg)
        ux, uy = math.cos(a), math.sin(a)
        clear = True
        for step in range(1, clearance + 1):
            x, y = int(round(px + ux * step)), int(round(py + uy * step))
            if not (0 <= x < cols and 0 <= y < rows) or binary_map[y, x] != 255:
                clear = False
                break
        if clear:
            if deg == 0:
                return mouse_target
            return (center[0] + ux * length, center[1] + uy * length)
    return mouse_target


FLEE_PLAN_PX = 450          # 屏幕像素: 这么近的究极算追兵、传奇以上的怪群算要绕开的(跟躲避滞回同半径)
FLEE_PLAN_FORGET_S = 1.5    # 这么久没躲, 上一次的目标就作废(下次躲是另一回事了)
FLEE_REPLAN_S = 0.1         # 规划一次 ~20ms, 两次之间沿用上一拍的方向(索敌本身也是 0.12 秒一扫)


class _FleePlan:
    """躲究极时往哪跑(flee_planner), 带上一拍的目标, 别每拍换一条路。

    老办法(背离究极 + 前方 3 格躲墙)留着当退路: 不知道自己的世界坐标 / 这张图没实测小地图
    换算 / 规划不出来时用它。"""

    def __init__(self):
        self.goal = None
        self.dir = None
        self.t = 0.0

    def reset(self):
        self.goal = None
        self.dir = None

    def target(self, avoid_positions, detections, center, map_pos, binary_map, now=None,
               me_world=None):
        """me_world: 自己的世界坐标; 不给就用最近一次索敌扫描的(analyze_recording 离线重放时给)。"""
        now = time.time() if now is None else now
        fallback = _steer_clear_of_walls(
            enemy_detect.flee_mouse_target(avoid_positions, center=center),
            center, map_pos, binary_map)
        s = utils.MINIMAP_WORLD_SCALE.get(utils.MAP)
        me = me_world if me_world is not None else enemy_detect.last_player_world()
        if s is None or me is None or binary_map is None:
            return fallback

        def to_map(w):
            return (s * (w[0] + 1000.0), s * (w[1] + 1000.0))

        def near(d):
            return math.hypot(d["screen_pos"][0] - center[0],
                              d["screen_pos"][1] - center[1]) <= FLEE_PLAN_PX

        trigger = {tuple(p) for p in avoid_positions}
        chasers, crowd = [], []
        for d in detections:
            if d.get("world") is None:
                continue
            if tuple(d["screen_pos"]) in trigger or (
                    near(d) and enemy_detect.classify_action(d["species"], d["rarity"]) == "AVOID"):
                chasers.append(to_map(d["world"]))
            elif near(d) and enemy_detect.is_flee_crowd(d):
                crowd.append(to_map(d["world"]))
        extend = 400 * mouse_scale()
        if now - self.t > FLEE_PLAN_FORGET_S:
            self.goal = self.dir = None
        elif self.dir is not None and now - self.t < FLEE_REPLAN_S:
            return (center[0] + self.dir[0] * extend, center[1] + self.dir[1] * extend)
        try:
            plan = flee_planner.plan_flee(binary_map, to_map(me), chasers, crowd, prefer=self.goal)
        except Exception as e:
            print(f"⚠️ 躲避规划出错, 这拍按老办法躲: {e}")
            plan = None
        self.t = now
        if plan is None:
            self.dir = None
            return fallback
        prev, self.goal, self.dir = self.goal, plan["goal"], plan["dir"]
        if prev is None or math.hypot(prev[0] - plan["goal"][0],
                                      prev[1] - plan["goal"][1]) > flee_planner.KEEP_R:
            how = f"早到 {plan['lead']:.1f} 格" if plan["safe"] else f"被堵住了, 亏得最少(差 {-plan['lead']:.1f} 格)"
            print(f"🏃 躲究极: 往 {plan['goal']} 跑 ({how}, 绕开怪群 {len(crowd)} 只)")
        ux, uy = plan["dir"]
        return (center[0] + ux * extend, center[1] + uy * extend)


_FLEE_PLAN = _FleePlan()


def _nearest_inside(farming_area, pos, binary_map, inset=ARRIVE_RADIUS + 3):
    """把一个区域外的位置夹回区域里, 取**离它最近**的那个可走点。

    实机(2026-09-24): 花朵飘到 (20, 62) —— 区域下边界(y=61)外**一格** —— 就被寻路
    拽回区域中心 (29, 32), 30 格的路走 20 秒, 一局里来回三次。回到最近的边内点只要
    走一两格。

    **inset 不能小于 ARRIVE_RADIUS**, 这是这个函数唯一不能凭直觉调的参数:
      - 比它小 -> 目标点落进 move_to_position 的到达半径里, 一个 moveTo 都不发就
        "到了"。人还在区外, 上层于是重新规划、再"到达"一次 …… 实机第一版 inset=2
        就是这样空转了 90 次/118 秒(2026-09-24)。
      - 等于它 -> 会动, 但 move_to_position 在离目标 ARRIVE_RADIUS 处收手, 收手点
        恰好落回边界上, 下一次飘动立刻又出区。多给 3 格当迟滞。

    区域窄到放不下 inset 时按轴退化成取中线(而不是夹出一个区外的点)。夹出来的点
    落在墙上(区域不一定是实心矩形)就回 None, 调用方退回配置的目标点。
    """
    (ax, ay), (bx, by) = farming_area
    x1, x2 = min(ax, bx), max(ax, bx)
    y1, y2 = min(ay, by), max(ay, by)
    ix = min(inset, (x2 - x1) // 2)
    iy = min(inset, (y2 - y1) // 2)
    x = int(min(max(pos[0], x1 + ix), x2 - ix))
    y = int(min(max(pos[1], y1 + iy), y2 - iy))
    if binary_map is None:
        return (x, y)
    if 0 <= y < binary_map.shape[0] and 0 <= x < binary_map.shape[1] \
            and binary_map[y][x] == 255:
        return (x, y)
    return None


FARM_SCREEN_CHECK_S = 0.5     # 刷怪时截图查死亡/开局画面的间隔(以前每拍两张截图)
FARM_TIMING_LOG_S = 30.0      # 刷怪主循环耗时分解, 隔这么久打一行


class _LoopClock:
    """刷怪主循环每拍花在哪 —— 第六份录像里鼠标 ~1 秒才动一次(究极冲到脸上才开始躲),
    光看代码估不出来是哪一段慢, 所以每 FARM_TIMING_LOG_S 秒在日志里打一行分解。
    漫游那一拍整条腿(move_to_position, 最多 20 小拍)算一拍, 单独计数, 不进中位数。"""

    def __init__(self, every=FARM_TIMING_LOG_S, now=None):
        self.every = every
        self._reset(time.time() if now is None else now)

    def _reset(self, now):
        self.t0 = now
        self.ticks = []
        self.wanders = 0
        self.parts = collections.defaultdict(float)
        self.n_parts = 0

    def part(self, name, dt):
        self.parts[name] += dt

    def tick(self, dt, wander=False, now=None):
        now = time.time() if now is None else now
        if wander:
            self.wanders += 1
        else:
            self.ticks.append(dt)
        self.n_parts += 1
        if now - self.t0 < self.every:
            return None
        line = None
        if self.ticks:
            xs = sorted(self.ticks)
            n = len(xs)
            parts = " / ".join(f"{k} {v / self.n_parts * 1000:.0f}"
                               for k, v in sorted(self.parts.items()))
            line = (f"⏱️ 刷怪循环(近 {now - self.t0:.0f} 秒): 反应拍 {n} 个, 每拍中位 "
                    f"{xs[n // 2] * 1000:.0f}ms / p90 {xs[int(n * 0.9)] * 1000:.0f}ms, "
                    f"漫游腿 {self.wanders} 条 | 平均每拍(ms): {parts}")
            print(line)
        self._reset(now)
        return line


def auto_farming(farming_area, duration=300, *, enemy_ai_enabled=True,
                 ):
    """自动刷怪逻辑（依赖一直攻击按钮）—— 在区域内连续走动, 不停下站桩.

    原来是走到一个随机点就停下等move_interval秒(靠站桩+一直攻击刷怪), 用户反馈
    应该在区域内持续走动而不是走走停停 —— 一到点立刻挑下一个可走点接着走, 全程
    不主动暂停, 靠外部"一直攻击"按钮在移动中持续输出.
    """
    x1, y1 = farming_area[0]
    x2, y2 = farming_area[1]

    min_x, max_x = min(x1, x2), max(x1, x2)
    min_y, max_y = min(y1, y2), max(y1, y2)
    farming_area = [(min_x, min_y), (max_x, max_y)]

    binary_map = load_binary_map()

    print(f"\n🎮 开始在区域 {farming_area} 进行自动刷怪...")
    print(f"⏱️  刷怪时长: {duration}秒（持续走动模式）\n")
    overlay.update(state="刷怪中", message=f"区域 {farming_area}")

    start_time = time.time()
    move_count = 0
    exit_reason = "timeout"
    last_enemy_scan = 0.0
    enemy_decision = ("wander", None)
    detections = []
    chase_pos_history = []   # 近期minimap坐标, 供enemy_detect.chase_is_stalled()看净位移
    mythic_latch = False
    mythic_misses = 0
    mythic_target_pos = None   # 上一 tick 锁定 Mythic 的屏幕坐标, 给 pick_mythic_target 做连续性

    def _wander_enemy_watch(_pos):
        """move_to_position 的 on_tick 钩子: wander 腿途中做一次(节流的)索敌, 需要
        规避/接战/锁 Mythic 时返回 "enemy" 中断这条腿, 外层下个 tick 就按刚更新的
        enemy_decision 处理. 只更新扫描状态、不推进 mythic miss 计数(那个归外层
        mythic 分支的 scanned 门管)."""
        nonlocal enemy_decision, detections, last_enemy_scan
        enemy_decision, detections, last_enemy_scan, _scanned = _maybe_scan_enemies(
            enemy_ai_enabled, time.time(), last_enemy_scan, enemy_decision, detections,
            can_reach=_line_of_sight_reach(binary_map, farming_area, _pos),
            in_area=_in_farm_area(farming_area))
        if enemy_decision[0] in ("flee", "chase", "swarm"):
            return "enemy"
        if (MYTHIC_LATCH_ENABLED and enemy_ai_enabled
                and enemy_detect.pick_mythic_target(
                    detections, center=enemy_detect.current_center(), latched=mythic_latch,
                    engage_px=MYTHIC_ENGAGE_PX, release_px=MYTHIC_RELEASE_PX,
                    chase_min_conf=CHASE_MIN_CONF, prev_pos=mythic_target_pos) is not None):
            return "enemy"
        return None

    clock = _LoopClock()
    tick_start = None
    tick_wander = False
    next_screen_check = 0.0
    while time.time() - start_time < duration:
        t_top = time.time()
        if tick_start is not None:
            clock.tick(t_top - tick_start, wander=tick_wander, now=t_top)
        tick_start, tick_wander = t_top, False
        if afk_watch.poll_afk_pause():
            overlay.update(state="AFK弹窗处理中", message="等待florr-auto-afk解题")
            time.sleep(0.2)
            # 暂停/丢位置/出区一圈回来后场景可能全变了 —— 别带着旧锁定用 600 释放
            # 半径, 让下一 tick 重新过 450 接战门槛.
            mythic_latch, mythic_misses, mythic_target_pos = False, 0, None
            continue

        # 死亡/开局画面检查放在循环最前面、不依赖"位置测不到" —— 死亡结算画面上
        # 曾经实测出过稳定的假位置(不是None), 只在current_pos is None分支里查
        # 会被这种假阳性绕过去, 角色明明已经死了脚本还在拿假坐标继续瞎刷.
        # 两张截图不便宜, 隔 FARM_SCREEN_CHECK_S 才查一次(死了晚半秒发现没关系)。
        if t_top >= next_screen_check:
            next_screen_check = t_top + FARM_SCREEN_CHECK_S
            t0 = time.time()
            on_screen = on_death_screen() or on_start_screen()
            clock.part("截图", time.time() - t0)
            if on_screen:
                print("🔁 检测到落在死亡/开局画面上, 交回上层处理")
                overlay.update(state="出错", message="落在死亡/开局画面, 交回上层重开")
                exit_reason = "break"
                break

        t0 = time.time()
        current_pos = get_player_position()
        clock.part("读位置", time.time() - t0)

        if current_pos is None:
            print("⚠️ 无法检测玩家位置")
            overlay.update(state="无法检测位置")
            time.sleep(1)
            mythic_latch, mythic_misses, mythic_target_pos = False, 0, None
            continue

        # 检查是否还在刷怪区域
        if not if_in_area([farming_area], current_pos):
            print(f"⚠️ 离开刷怪区域 (当前: {current_pos})，重新寻路回去")
            overlay.update(state="离开刷怪区域", pos=current_pos, message="重新寻路回去")
            # 回**最近的边内点**, 不是区域中心 —— 飘出一格也要横穿半张区域(用户
            # 2026-09-25)。落在墙上时退回中心。
            target = _nearest_inside(farming_area, current_pos, binary_map) or (
                (farming_area[0][0] + farming_area[1][0]) // 2,
                (farming_area[0][1] + farming_area[1][1]) // 2)
            if not lazy_theta_pathing(target, [farming_area],
                                      enemy_watch=_PathingEnemyWatch() if enemy_ai_enabled else None):
                print("❌ 无法回到刷怪区域")
                overlay.update(state="出错", message="无法回到刷怪区域")
                exit_reason = "break"
                break
            mythic_latch, mythic_misses, mythic_target_pos = False, 0, None
            continue

        # 索敌: 按ENEMY_SCAN_INTERVAL节流解码canvas帧(不是每tick都跑, 有开销).
        # 索敌是附加功能, 任何异常都退化成"漫游", 不能让它打断刷怪主循环.
        now = time.time()
        enemy_decision, detections, last_enemy_scan, scanned = _maybe_scan_enemies(
            enemy_ai_enabled, now, last_enemy_scan, enemy_decision, detections,
            can_reach=_line_of_sight_reach(binary_map, farming_area, current_pos),
            in_area=_in_farm_area(farming_area))
        clock.part("索敌", time.time() - now)
        enemy_action = enemy_decision[0]

        # 1) flee 最优先 —— 且立刻放掉 Mythic 锁定 (躲优先, 不为打 Mythic 送死).
        if enemy_action == "flee":
            mythic_latch, mythic_misses = False, 0
            center = enemy_detect.current_center()
            mouse_target = _FLEE_PLAN.target(enemy_decision[1], detections, center,
                                             current_pos, binary_map)
            _drive_and_check_stall(mouse_target, current_pos, chase_pos_history,
                                   "规避中", "附近有危险稀有怪, 拉开距离", center=center)
            continue

        # 2) Mythic 近身锁定 —— flee 之外, 贴脸有合格 Mythic 就锁定按物种走位磨掉.
        if MYTHIC_LATCH_ENABLED and enemy_ai_enabled:
            center = enemy_detect.current_center()
            mtarget = enemy_detect.pick_mythic_target(
                detections, center=enemy_detect.current_center(), latched=mythic_latch,
                engage_px=MYTHIC_ENGAGE_PX, release_px=MYTHIC_RELEASE_PX,
                chase_min_conf=CHASE_MIN_CONF, prev_pos=mythic_target_pos)
            # miss 计数只在真跑过扫描的 tick 推进 —— 节流 tick 拿的是同一份缓存
            # 检测, 再扣一次等于把同一帧证据数两遍, 3-miss 释放在快机器上缩成 ~2.
            if scanned:
                mythic_latch, mythic_misses = _update_mythic_latch(
                    mythic_latch, mythic_misses, mtarget is not None, MYTHIC_RELEASE_MISSES)
            if mythic_latch and mtarget is not None:
                mythic_target_pos = mtarget["screen_pos"]
                repel = enemy_decision[3] if enemy_action == "chase" else []
                mouse_target = enemy_detect.mythic_move_target(
                    mtarget, center,
                    strafe_radius=MYTHIC_STRAFE_RADIUS,
                    cactus_hold_px=MYTHIC_CACTUS_HOLD_PX,
                    repel_positions=repel, k_radial=MYTHIC_STRAFE_K_RADIAL)
                policy = enemy_detect.MYTHIC_KITE_SPECIES[mtarget["species"]]
                _drive_and_check_stall(mouse_target, current_pos, chase_pos_history,
                                       "清青怪", f"遛 {mtarget['species']}({policy})",
                                       center=center)
                continue
            # 没锁定 / 这 tick 没目标 —— 放掉连续性锚点, 别让下次锁定拿旧坐标.
            mythic_target_pos = None

        # 3) 蚁群 (蚁穴) —— 保持距离遛, 近了就退, 不冲进去.
        if enemy_action == "swarm":
            swarm, repel = enemy_decision[1], enemy_decision[2]
            center = enemy_detect.current_center()
            mouse_target = enemy_detect.swarm_move_target(swarm, center, repel_positions=repel)
            _drive_and_check_stall(mouse_target, current_pos, chase_pos_history,
                                   "打蚁群", f"蚁群 {swarm['count']} 只, 保持距离遛",
                                   center=center)
            continue

        # 4) 普通追击 —— 不 fleeing 也没锁定 Mythic. 走位按物种(蚁穴蠕虫绕圈不停, 其余到
        #    停步半径就停), 见 enemy_detect.chase_move_target。
        if enemy_action == "chase":
            target, hold_px, repel = enemy_decision[1], enemy_decision[2], enemy_decision[3]
            center = enemy_detect.current_center()
            mouse_target = enemy_detect.chase_move_target(
                target, hold_px, center, repel_positions=repel)
            circling = target["species"] in enemy_detect.ANTHELL_STRAFE_SPECIES
            verb = "绕圈打" if circling else "追击"
            _drive_and_check_stall(mouse_target, current_pos, chase_pos_history,
                                   "索敌中", f"{verb} {target['species']}({target['rarity']})",
                                   center=center, track_stall=not circling)
            continue

        # 5) enemy_action == "wander": 没有可打/需规避的目标, 随机漫游.
        tick_wander = True
        chase_pos_history.clear()
        random_x, random_y = random_walkable_point(farming_area, binary_map)

        # 移动到目标点 —— 到了立刻挑下一个点接着走, 不暂停.
        print(f"🚶 移动到 ({random_x}, {random_y})")
        overlay.update(state="刷怪中", pos=current_pos, target=(random_x, random_y), message=f"持续走动中 (第{move_count + 1}次)")
        # max_attempts=20 (≈1s worst case at time.sleep(0.05)每tick) 而不是默认的
        # 200(≈10s) —— 让外层循环更频繁拿回控制权重新索敌扫描, 见下面ENEMY_SCAN_INTERVAL
        # 的注释.
        move_result = move_to_position(current_pos, (random_x, random_y),
                                       max_attempts=20, on_tick=_wander_enemy_watch)

        if move_result == "enemy":
            # 路途中扫到怪(该 flee/chase/锁 Mythic) —— 立刻回外层, 下个 tick 用
            # _wander_enemy_watch 刚更新的 enemy_decision 处理, 不算走完一趟.
            continue
        if move_result == "stuck":
            print("⚠️ 移动受阻, 脱困一下...")
            overlay.update(state="卡住", message="脱困中")
            execute_anti_stuck()
        elif move_result in ["in_game_dead", "in_menu"]:
            print(f"⚠️ 游戏状态变化: {move_result}")
            exit_reason = "break"
            break
        else:
            # 只有真正走到点上才计入移动次数, "受阻"那次不算.
            move_count += 1

        # 检查游戏状态
        if on_death_screen():
            print("💀 玩家已死亡")
            overlay.update(state="已死亡")
            exit_reason = "break"
            break
        elif on_start_screen():
            print("📋 玩家在菜单中")
            overlay.update(state="菜单中")
            exit_reason = "break"
            break

    elapsed = time.time() - start_time
    print(f"\n" + "="*50)
    print(f"✅ 刷怪完成！")
    print(f"   实际耗时: {elapsed:.1f}秒")
    print(f"   移动次数: {move_count}")
    print(f"="*50)
    if exit_reason == "timeout":
        overlay.update(state="完成", message=f"刷怪结束, 共移动{move_count}次")
    else:
        overlay.update(message=f"刷怪结束, 共移动{move_count}次")

    # 是不是刷满了整个duration —— 给调用方(主循环)判断"这轮算不算刷够时长"用,
    # 不刷满(死亡/被踢/卡死放弃)的连续出现太多次, 说明这个服务器可能有问题
    # (比如刷怪区域被占、或者哪里持续卡关), 值得换个服务器而不是死磕.
    return exit_reason == "timeout"


def _apply_worker_config(cfg):
    """把 config.json 的值应用/摊平成 run_worker 主循环要用的局部值.
    v2: 读 cfg['active'](GUI 调度器在起 worker 前刷成"当前生效时块"的刷怪参数);
    老扁平文件 / 手写调试文件: 回退 cfg 本身; 再缺的键: 回退 app_config.DEFAULTS.
    apply_map() 必须在这里就调 —— utils 的 MAP 是模块级全局, load_binary_map()
    等一堆函数都读它."""
    src = cfg.get("active")
    if not isinstance(src, dict):
        src = cfg
    d = app_config.DEFAULTS
    map_name = src.get("map", d["map"])
    invert_attack = src.get("invert_attack", d["invert_attack"])
    invert_defense = src.get("invert_defense", d["invert_defense"])
    route = map_routes.route_for(map_name)
    # MAP 先设成刷怪那张图(单图路线就是它自己)。多阶段路线里 _run_entry_route()
    # 每一段会自己 apply_map() 覆盖, 这里只是给个合理的初值。
    apply_map(route.final_map)
    return {
        "map_name": map_name,
        "route": route,
        "location": tuple(src.get("location", d["location"])),
        "farming_area": [tuple(p) for p in src.get("farming_area", d["farming_area"])],
        "farming_duration": src.get("farming_duration", d["farming_duration"]),
        "short_round_limit": src.get("consecutive_short_round_limit",
                                    d["consecutive_short_round_limit"]),
        # 这张图没做索敌(MAP_SPECIES 里是空集)就强制关掉 —— priority_score() 对
        # 表外的 slug 会 KeyError, 不能指望 config 里不写 true。
        "enemy_ai_enabled": (src.get("enemy_ai_enabled", d["enemy_ai_enabled"])
                             and enemy_detect.species_supported(route.final_map)),
        "auto_switch_server": src.get("auto_switch_server", d["auto_switch_server"]),
        # 蚁穴的 server_biome 是 garden 不是 ant_hell —— 人是从花园服踩洞口进去的,
        # 标题页选生态区和换服务器都得对着花园来(见 map_routes.py)。
        "biome": route.server_biome,
        "enter_game_swap": src.get("enter_game_swap", d["enter_game_swap"]),
        "reach_area_swap": src.get("reach_area_swap", d["reach_area_swap"]),
        "invert_attack": invert_attack,
        "invert_defense": invert_defense,
    }


ENTRY_ROUTE_TIMEOUT = 180   # 秒。多阶段路线(蚁穴)从进场走到刷怪图的总预算。
                            # 洞口被怪堵着 / 坐标标偏了 / 传送没触发时兜底 ——
                            # 超时交回主循环, 按短局处理(攒够次数会自动换服务器)。

# 踩上传送点判定框(±PORTAL_TOLERANCE)之后, 给florr一点反应时间再复查一次是不是
# 真的传送了 —— 见 _run_entry_route 里的用法, 和实机复盘(2026-09-16): 用户肉眼
# 确认角色走到了判定框附近, 但没有精确踩在触发传送的那个点上, 传送压根没发生,
# 角色停在原地(花园)。lazy_theta_pathing 的判定框比florr真正触发传送要求的精度
# 松, 光靠"落在判定框里"不能当推进到下一段的依据。不是精确值, 只是给"传送
# 本身有没有反应延迟"留个缓冲.
PORTAL_SETTLE_SECONDS = 1

# 连续这么多次复查都判定"离开了"才真的推进 —— 实机复盘(2026-09-20): 单次
# 复查扛不住噪声(像素读数刚好越过 PORTAL_TOLERANCE 边界1个单位、canvas世界
# 距离读到一两千但其实人还在原地, 两种噪声都见过实例), 2 次独立确认能大幅
# 降低"同一次噪声骗过两条判据"的概率, 代价只是多等 1 个 PORTAL_SETTLE_SECONDS
# 才推进.
PORTAL_LEFT_CONFIRMATIONS = 2

# canvas 复查最多攒这么久的绘制记录再判断 —— 不用等一整秒, 有画面在动的话
# 零点几秒就能攒到至少2个不同帧号(拿次新帧解码, 跟 debug_canvas_portal.py 同款
# 套路), 没必要跟 PORTAL_SETTLE_SECONDS 那个"等florr反应"的用途混在一起.
CANVAS_VERIFY_DRAIN_SECONDS = 0.3


def _drain_second_newest_frame(drain_seconds=CANVAS_VERIFY_DRAIN_SECONDS):
    """canvas 钩子的公共部分: 注入钩子, drain够时间, 按帧号分组, 取次新帧
    (最新那帧可能没画完, 跟 debug_canvas_enemies.py / debug_canvas_portal.py
    同一个理由)的记录列表。钩子没装上 / 画面没在动 / 帧数不够, 任何一步失败
    都返回 None, 调用方自己决定怎么退化 —— 不猜一个结果出来."""
    try:
        cdp_bridge.inject_canvas_hook()
    except RuntimeError:
        return None
    buf = []
    deadline = time.time() + drain_seconds
    while time.time() < deadline:
        try:
            buf.extend(cdp_bridge.drain_canvas_log())
        except Exception:
            return None
        time.sleep(0.05)
    if not buf:
        return None
    frames = canvas_decode.group_by_frame(buf)
    keys = sorted(frames)
    if len(keys) < 2:
        return None
    return frames[keys[-2]]


def _canvas_zone_map():
    """画布 HUD 上的区域名 -> 寻路图名; 钩子没装上 / 读不到 / 认不出 -> None.

    走 utils.canvas_zone_map() 那条**只偷看不清空**的路(不是 _drain_second_newest_frame):
    清 __canvasLog 会把 scan_enemies / canvas_player_world 的帧偷走, 而且每次调用白等 0.3 秒 ——
    进场路线每圈都要问一次"人在哪张图", 这个代价是白花的。留这个薄包装只为给
    _run_entry_route 一个独立的打桩缝(_on_wrong_map 那条走 canvas_zone_map 本名)。"""
    return canvas_zone_map()


def _canvas_scale_zone(route):
    """画布小地图缩放比 -> 路线各段里唯一匹配的那张图; 读不到 / 分不出 -> None。

    认"人在哪张图"时区域名读不出来的兜底: 小地图缩放比只跟世界大小有关(utils.MINIMAP_SCALE_HINTS),
    对所有多阶段路线都读 —— 花园(0.004691) / 蚁穴(0.004617) / 下水道·工厂(0.006240)都是可分的
    (花园 vs 蚁穴差 1.6%, 容差 0.5%, 还要满足 map_for_minimap_scale 的"次优至少差 3 倍"边距)。
    这一层同时是"传送被误判成功"的纠错: 走门后区域名读不出(下水道/工厂的字样是猜的, 也可能
    正好赶上黑屏/切图这一瞬), 缩放比还说花园, 就该回花园那段重走, 别信乐观推进的阶段状态。

    跟 _canvas_zone_map 一样走 utils 的偷看路径, 不清 __canvasLog(理由见那边)。"""
    scale = utils.canvas_minimap_scale()
    if scale is None:
        return None
    ui_scale = utils.SCREEN_HEIGHT / utils._REF_HEIGHT
    return utils.map_for_minimap_scale(scale, [s.map_name for s in route.stages], ui_scale)


def _canvas_portal_world_offset(drain_seconds=CANVAS_VERIFY_DRAIN_SECONDS):
    """尝试用 canvas 钩子读一帧, 拿洞口光效相对玩家的世界坐标偏移量 —— 给
    _walk_toward_visible_portal 逐步逼近用, 也给 _run_entry_route 的 settle
    复查当"离开了没有"的独立判据(见那边的用法说明)。

    实机复盘(2026-09-19): 最早这里读的是小地图上玩家点的绝对世界坐标, 结果
    每次调用读到的距离在137~835之间大幅跳动, 而且几乎每次都直接被判成
    "冲过头"提前收手 —— 查出来是小地图那个点的绘制坐标本身是取整过的(挪1个
    小地图像素≈200多个世界单位), 跟玩家连续移动的真实位置对不上, 拿它做
    逐帧转向反馈只会来回抖。改成读洞口光效在主视图的渲染位置, 换算成相对
    玩家的世界坐标偏移量(不经过小地图那层量化, debug_canvas_portal.py 实机
    验证过精度到个位数), 才是真正"跟怪物检测一样实时定位"该走的路径。

    进一步实机复盘(2026-09-20): 这个"不经过小地图量化"的特性还有第二个用处
    ——像素判据(target_box)本身就是从小地图量化坐标算出来的, 如果只拿"小地图
    玩家点的世界坐标"当第二层复查(早期版本的做法), 两条判据其实共享同一个
    偏差源, 会一起被同一次系统性偏差骗过去(实测过两次隔1秒的独立复查读到
    一模一样的坐标和距离, 角色根本没动)。这个函数读的是主视图, 是真正独立
    于像素判据的数据源."""
    recs = _drain_second_newest_frame(drain_seconds)
    if recs is None:
        return None
    try:
        camera = canvas_decode.camera_from_frame(recs)
    except ValueError:
        return None
    return canvas_decode.portal_glow_world_offset(recs, camera)


# 实机(2026-09-18)拿 debug_canvas_portal.py 量出来的洞口视觉图案世界半径反推:
# 主视图那个带光效的同心圆, 最大 r=90(屏幕像素), 当时 zoom=0.9, 90/0.9≈100个
# 世界单位。60 留了足够余量, 保证真的踩进图案范围, 不是卡在边缘似进未进。
PORTAL_WORLD_ARRIVE_RADIUS = 60.0

# 刹车半径: 进这个范围之后不再维持全速追, 改成"轻推一下就收手, 等它真的
# 停下来再看" —— 实机复盘(2026-09-19)用户确认了触发条件是"精确站在原地不动
# 等一会", 而且明确说"我的程序每次都定位到了传送门位置, 但移动之后传送门
# 就不在原来位置了, 导致一直是跑过去了, 没进去" —— 全速追到最后一刻的
# 冲量会让角色直接冲穿这个小范围, 下一次读数已经在另一侧甚至更远, 永远
# 停不到点上。3倍 ARRIVE_RADIUS 留出足够刹车距离.
PORTAL_WORLD_BRAKE_RADIUS = 3 * PORTAL_WORLD_ARRIVE_RADIUS

# 给"直接朝洞口光效走"这一小段的预算 —— canvas 读数本身有 drain 延迟, 而且
# 这段没有寻路避障, 走不到就该早点认输交回上层重新跑一遍原来的像素路径规划,
# 不是在这里死磕.
PORTAL_WORLD_APPROACH_TIMEOUT = 6.0

# 刹车区里"轻推一下"这一下推多久、"收手等它停"这一下等多久 —— 用户实机反馈
# (2026-09-20): "能不能让他反应快一点，玩家到了之后立马就停，现在就是因为
# 反应不够导致直接略过去了"。原来是 0.05/0.1 秒, 加上每次重新定位光效本身的
# drain 延迟(见下面 drain_seconds), 刹车区一个完整"推一下→停→再读一次"的
# 周期动辄 0.25 秒以上 —— 这段时间里角色在惯性下还在移动, 足够冲穿这么小的
# 到达范围。收紧这两个数值 + drain_seconds, 缩短反应周期, 减少每次冲多远.
PORTAL_WORLD_BRAKE_TAP_SECONDS = 0.02
PORTAL_WORLD_BRAKE_SETTLE_SECONDS = 0.03

# 远处(还没进刹车区)推一下的力度/间隔 —— 比刹车区(固定50)大, 还是要往前赶
# 路; 但比老的"最多推满500、中途不收手"慢得多, 换成跟刹车区同样的"推一下
# 就收手"节奏, 不再维持贴着最高速冲的状态.
PORTAL_WORLD_FAR_STEP = 200.0
PORTAL_WORLD_FAR_TAP_SECONDS = 0.08
PORTAL_WORLD_FAR_SETTLE_SECONDS = 0.05


def _walk_toward_visible_portal(timeout=PORTAL_WORLD_APPROACH_TIMEOUT,
                                stall_limit=13, progress_epsilon=1.5,
                                drain_seconds=0.05):
    """绕开300x300小地图那层量化, 直接拿 canvas 读到的"洞口光效相对玩家的
    世界坐标偏移量"(见 _canvas_portal_world_offset), 一步步把鼠标指向洞口
    方向走, 直到真的进了 PORTAL_WORLD_ARRIVE_RADIUS 范围。不需要传入洞口的
    绝对坐标 —— 每一tick都直接从当前帧重新定位光效, 跟怪物检测同一个思路
    (实时读渲染位置, 不是死磕一个预先标定好的坐标)。

    远处(dist >= PORTAL_WORLD_BRAKE_RADIUS)也是"推一下就收手"(见
    PORTAL_WORLD_FAR_STEP), 跟 move_to_position 同一套"原地打转"停滞判定,
    只是位置来源换成 canvas 读到的偏移量。进了刹车半径之后收得更紧(见
    PORTAL_WORLD_BRAKE_RADIUS 的注释):
    不能再靠 move_to_position 那套"冲过头就当到了"的旧逻辑 —— 那是给
    像素级容差设计的, 这里冲过头往往是货真价实地冲穿了整个到达范围, 硬当
    "到了"处理只会让传送触发不了.

    这段没有寻路避障(canvas 直接读渲染位置, 不经过 binary_map) —— 只该在
    像素判据已经认为"到了"附近、路上不该有障碍之后才调用。光效一直没进画面 /
    一直没有进展 / 预算耗尽, 都老实返回 False(或者一开始就读不到时返回
    None), 交回上层退化成原来纯像素判据的行为, 不会比改之前更差.
    """
    last_dist = None
    stall_count = 0
    deadline = time.time() + timeout
    offset = _canvas_portal_world_offset(drain_seconds=drain_seconds)
    if offset is None:
        return None
    while time.time() < deadline:
        dx, dy = offset
        dist = math.hypot(dx, dy)

        if dist < PORTAL_WORLD_ARRIVE_RADIUS:
            reset_keyboard()
            return True

        # 冲过头(这次比上次还远)不再当"到了"处理 —— 刹车半径内的冲过头往往
        # 是货真价实地冲穿了到达范围, 只算一次"没进展", 靠 stall_count 兜底,
        # 不能硬当成功.
        if last_dist is not None and dist < last_dist - progress_epsilon:
            stall_count = 0
        elif last_dist is not None:
            stall_count += 1
        last_dist = dist

        if stall_count > stall_limit:
            reset_keyboard()
            return False

        if dist < PORTAL_WORLD_BRAKE_RADIUS:
            # 刹车区: 只给最小推力轻推一步就立刻收手, 给florr一点时间让角色
            # 速度真的降下来, 不维持全速冲 —— 不然下一次读数很容易已经在
            # 目标另一侧甚至更远.
            extend = 50.0 * mouse_scale()
            extend_x = extend * dx / dist
            extend_y = extend * dy / dist
            mouse_pos = clamp_to_screen(SCREEN_WIDTH // 2 + extend_x, SCREEN_HEIGHT // 2 + extend_y)
            _move_mouse_safely(mouse_pos)
            time.sleep(PORTAL_WORLD_BRAKE_TAP_SECONDS)
            reset_keyboard()
            time.sleep(PORTAL_WORLD_BRAKE_SETTLE_SECONDS)
        else:
            # 远处以前是推满500清一直不收手的"全速追" —— 用户反馈(2026-09-20)
            # 太快, 容易冲过刹车区反应不过来。改成跟刹车区同一套"推一下就
            # 收手"节奏, 只是推力比刹车区大(毕竟还没到刹车区, 不用那么保守),
            # 每一步之间留出反应时间, 不再维持"贴着最高速冲"的状态.
            extend = max(min(dist, PORTAL_WORLD_FAR_STEP), 50) * mouse_scale()
            extend_x = extend * dx / dist
            extend_y = extend * dy / dist
            mouse_pos = clamp_to_screen(SCREEN_WIDTH // 2 + extend_x, SCREEN_HEIGHT // 2 + extend_y)
            _move_mouse_safely(mouse_pos)
            time.sleep(PORTAL_WORLD_FAR_TAP_SECONDS)
            reset_keyboard()
            time.sleep(PORTAL_WORLD_FAR_SETTLE_SECONDS)

        offset = _canvas_portal_world_offset(drain_seconds=drain_seconds)
        if offset is None:
            reset_keyboard()
            return False
    reset_keyboard()
    return False


# _walk_toward_minimap_target 每一脚硬走多久 / 最多走几脚 —— 用户反馈
# (2026-09-20): 光效压根读不到时(_walk_toward_visible_portal 返回 None)
# 不该干等, 该退回小地图坐标硬走一段。跟 move_to_position 的 dist<5 快速
# 到达判据不一样: 这里目标点之间本来就常常没到5个单位, 按 dist<5 的话
# 还没真发指令就已经"到了", 一步没挪, 卡死. 所以这里不看目标距离, 只看
# "位置有没有真的变化"当到达判据, 至少强制走够以下这么多脚.
PORTAL_MINIMAP_FALLBACK_ATTEMPTS = 6
PORTAL_MINIMAP_FALLBACK_STEP_SECONDS = 0.15


def _walk_toward_minimap_target(target_pos, attempts=PORTAL_MINIMAP_FALLBACK_ATTEMPTS,
                                step_seconds=PORTAL_MINIMAP_FALLBACK_STEP_SECONDS):
    """_walk_toward_visible_portal 连光效起点都定位不到(返回 None)时的退路 ——
    改用小地图坐标(跟 move_to_position 同一个来源)硬走几脚, 但**不用**
    move_to_position 那套"欧氏距离<5就算到达"的判据: 这一段路线的目标点
    彼此之间经常本来就没到5个小地图像素, dist<5 在这里等于"一上来就判定
    到了", 从来不会真的发出移动指令。这里改成: 不管起始距离多近, 先真走
    step_seconds、松手看位置有没有变, 变了才算数, 没变就再走一脚, 走够
    attempts 脚还没变才认输。

    返回 True(真的挪动过) / False(目标读不到, 或走够了还是没挪动).
    """
    current = get_player_position()
    if current is None or target_pos is None:
        return False
    for _ in range(attempts):
        dx = target_pos[0] - current[0]
        dy = target_pos[1] - current[1]
        dist = math.hypot(dx, dy)
        if dist < 0.5:
            # 起点几乎就是终点本身, 连方向都算不出来, 硬走也没有意义.
            return False
        extend = max(min(dist * 45, 500), 50) * mouse_scale()
        extend_x = extend * dx / dist
        extend_y = extend * dy / dist
        mouse_pos = clamp_to_screen(SCREEN_WIDTH // 2 + extend_x, SCREEN_HEIGHT // 2 + extend_y)
        _move_mouse_safely(mouse_pos)
        time.sleep(step_seconds)
        reset_keyboard()
        new_pos = get_player_position()
        if new_pos is not None and new_pos != current:
            return True
        if new_pos is not None:
            current = new_pos
    return False


class _StageState:
    """读不到服务器号时, 对"人现在走到路线哪一段了"的猜测。

    读得到服务器号时这个猜测每圈都会被 florr_server.current_map_name() 覆盖;
    读不到(探针没装 / CDP 抖 / 反查接口挂了)时它就是唯一依据。
    转移规则来自实机确认过的事实: 从标题页进场 = 回到第一段; 踩过传送点 = 推进
    一段; 换服务器 = 回到第一段; **死亡重生不变**(蚁穴里死了还在蚁穴)。
    """

    def __init__(self, route):
        self._route = route
        self._index = 0

    @property
    def map_name(self):
        return self._route.stages[self._index].map_name

    def advance(self):
        self._index = min(self._index + 1, len(self._route.stages) - 1)

    def sync_to(self, map_name):
        """人实际在 map_name 上(画布区域名读出来的) —— 把猜测对齐过去. 不在路线上就不动."""
        for i, stage in enumerate(self._route.stages):
            if stage.map_name == map_name:
                self._index = i
                return

    def reset(self):
        self._index = 0


# 跟仓库一起发的寻路图(git ls-files maps/): 不是 capture_map.py 采出来的, 缺了要
# 从仓库恢复而不是重采 —— 重采会覆盖手工做的那张。jungle/sewers/factory 是 tmj_maps.py
# 按官方地图数据推导的, 花园图从 debug_garden_raw.png 生成 —— 缺了都要从仓库恢复, 重采会覆盖
# 推导出来的图。
_SHIPPED_MAPS = ("anthell", "desert", "ocean", "garden", "jungle", "sewers", "factory")


def _route_blocker(route):
    """这条路线跑不起来的原因; None = 能跑。

    在 worker 启动时一次性检查, 好过让 apply_map() 的 assert 在主循环里把 worker
    炸掉 —— 待标定的值缺了要给一句人看得懂的话, 不是 AssertionError。
    """
    if not route.is_calibrated():
        return (f"「{route.final_map}」进场路线里有传送门坐标还是 None: 未标定 "
                f"(map_routes 里对应的 *_PORTAL 常量)。"
                f"{map_routes.PORTAL_RECALIBRATION_RECIPE}")
    for stage in route.stages:
        path = os.path.join("./maps", f"{stage.map_name}.png")
        if not os.path.exists(path):
            if stage.map_name in _SHIPPED_MAPS:
                # 仓库自带的图别叫人去重采: capture_map.py 会把手工/推导做的那张覆盖掉,
                # 而且得先进得了这张图才采得到 —— 蚁穴恰恰是"进不去才缺图"的。
                return (f"缺寻路图 {path} —— 这张是仓库自带的图, "
                        f"用 `git checkout -- {path}` 恢复, 别跑 capture_map.py 重采"
                        "(会把推导/手工做的那张覆盖掉)。")
            return (f"缺寻路图 {path} —— 在该图局内跑 "
                    f"`python capture_map.py {stage.map_name}` 生成。")
    # 门坐标填了、但没在 PORTAL_OPENINGS 里登记 = 那扇门在寻路图上还是墙(所有门都是墙, 走门那
    # 一段运行时才挖, 见 map_routes.PORTAL_OPENINGS)。挖不开的后果是**静默**的: 目标点是墙,
    # lazy_theta_star 只报笼统的"路径规划失败", 一轮轮空转到超时, 谁也看不出是漏登记。
    # 重标定一个 *_PORTAL 却忘了改 PORTAL_OPENINGS 是最容易踩的一脚, 启动时就拦。
    for stage in route.stages:
        if stage.walk_to is None or stage.opening_rects():
            continue
        return (f"「{route.final_map}」进场路线要在 {stage.map_name} 上走到门 {stage.walk_to}, "
                f"但 map_routes.PORTAL_OPENINGS 里没有 ({stage.map_name!r}, {stage.walk_to}) "
                "这个键 —— 那扇门在寻路图里是墙, 挖不开就规划不出路径, 只会一轮轮报笼统的"
                "「路径规划失败」。改过 *_PORTAL 常量就要把新门的绿斑矩形一起登记进 "
                "map_routes.PORTAL_OPENINGS(键 = (地图名, walk_to))。")
    return None


PORTAL_SWITCH_TIMEOUT = 90.0   # 从蚁穴出生点走到回花园那个门的预算(出生点离它只有 ~6 格)
PORTAL_STAGING_MIN_PX = 3      # 先走到离门这么多格的可走点, 再按光效贴上去 —— 寻路走得太近
                               # 会在 lazy_theta_pathing 里就蹭上门传送走, 错过换服的时机


PORTAL_DIRECT_PX = 12          # 离门这么近(小地图格)就不寻路到集结点了, 直接朝门的世界坐标走


def _portal_switch_target(w):
    """这个时块换服时要不要走"传送门换服": 只有蚁穴(回花园的门)。其余图 -> None。"""
    if w.get("map_name") == "anthell":
        return map_routes.ANTHELL_TO_GARDEN_PORTAL
    return None


# 走进门: "点一下 -> 松手滑停 -> 量 -> 再点一下"。
# 门要人停在里面才传(花园那边离门心 14~34、慢下来 0.5~1 秒就传; 第六份录像被挤到离门心 57
# 半秒内就传; 冲过门心不传)。第五份录像一直朝门心推, 跑图装 ~1000/秒 冲过去来回摆; 第六、七份
# 换成按"松手会滑到哪"刹车, 每拍 75ms 了还是振荡 —— 速度只能拿相邻两次读位置差出来, CDP 延迟
# 抖 ±30ms, 估出来的速度噪声太大, 跑图装一推就是 1000/秒, 估错一次就冲过门心 300 单位。
# 这里每次都等停稳了才决定下一下往哪、推多久, 不用估速度; 推多久按上一下实际滑了多远现学。
PORTAL_HOLD_R = 60.0        # 世界单位: 停在离门心这么近 = 站进门了, 松手等传送(门的光圈半径 ~100)
PORTAL_PUSH_PX = 200.0      # 点一下时鼠标离中心多远(参照像素, 够满速)
PORTAL_PULSE_FIRST_S = 0.08 # 第一下推多久(还不知道这身装备多快, 往小了点)
PORTAL_PULSE_MIN_S = 0.02
PORTAL_PULSE_MAX_S = 0.6
PORTAL_PULSE_AIM = 0.75     # 按学到的速度只推剩下距离的这么多(宁短勿长, 冲过去再回来更费时)
PORTAL_REST_SPEED = 40.0    # 世界单位/秒: 近 0.2 秒挪得比这慢 = 停稳了
PORTAL_REST_WINDOW_S = 0.2
PORTAL_COAST_MAX_S = 2.5    # 松手后最多等这么久停稳, 等不到也照样量
PORTAL_DEATH_CHECK_S = 0.5  # 截图查死亡画面比读画布慢得多, 走门时隔这么久才查一次
PORTAL_NO_GAIN_PULSES = 4   # 连着这么多下都没靠近一截(卡住了/被怪顶着) -> 放弃, 交回直接换服
PORTAL_DWELL_MAX_S = 4.0    # 站在门里这么久还没传送 -> 走出去再进一次(刚落地的人站在门上不会被传回去,
                            # 说不定要"进门"这个动作)
PORTAL_LEAVE_S = 0.25       # 出去再进: 往外推这么久
PORTAL_REENTRY_MAX = 2


def _tick_stats(ticks):
    if not ticks:
        return ""
    xs = sorted(ticks)
    return f"(每拍中位 {xs[len(xs) // 2] * 1000:.0f}ms, 最慢 {xs[-1] * 1000:.0f}ms)"


class _PortalPulser:
    """纯状态机, 不碰屏幕: step(now, here) -> 鼠标偏移(世界方向, 参照像素) 或 None(松手);
    here 是自己的世界坐标。结果 "fail" 通过 .failed 给出。"""

    def __init__(self, target, now):
        self.target = target
        self.phase = "settle"          # settle(等停稳) / push / dwell / leave
        self.phase_t = now
        self.samples = []              # 近期 (t, pos)
        self.rate = None               # 学到的: 推 1 秒大约挪多远(世界单位), 含滑行
        self.push_from = None
        self.push_s = 0.0
        self.push_dir = (0.0, 0.0)
        self.best = None
        self.no_gain = 0
        self.reentries = 0
        self.failed = None
        self.log = []

    def _dist(self, p):
        return math.hypot(self.target[0] - p[0], self.target[1] - p[1])

    def _at_rest(self, now):
        # 只看松手之后的样本: 刚松手那一下拿推之前(还没动起来)的样本比, 会把正在加速的人当成
        # 停稳了, 马上又推一下, 学到的速度也是错的(仿真里就这样在门边来回点了 4 下)
        old = [s for s in self.samples
               if s[0] >= self.phase_t and now - s[0] >= PORTAL_REST_WINDOW_S]
        if not old:
            return False
        t0, p0 = old[-1]
        p1 = self.samples[-1][1]
        return math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / max(now - t0, 1e-3) < PORTAL_REST_SPEED

    def end_pulse(self, now):
        """调用方按计时推完 / 往外推完、已经松手了: 进入"等停稳"。"""
        if self.phase in ("push", "leave"):
            if self.phase == "leave":
                self.push_from = None
            self.phase, self.phase_t = "settle", now

    def pulse_s(self):
        """刚开始的这一下要推多久(push / leave 阶段), 不是就 None。"""
        if self.phase == "push":
            return self.push_s
        if self.phase == "leave":
            return PORTAL_LEAVE_S
        return None

    def step(self, now, here):
        self.samples = [s for s in self.samples if now - s[0] <= 1.0] + [(now, here)]
        d = self._dist(here)
        if self.phase == "push":
            if now - self.phase_t < self.push_s:
                return (self.push_dir[0] * PORTAL_PUSH_PX, self.push_dir[1] * PORTAL_PUSH_PX)
            self.phase, self.phase_t = "settle", now
            return None
        if self.phase == "leave":
            if now - self.phase_t < PORTAL_LEAVE_S:
                away = (here[0] - self.target[0], here[1] - self.target[1])
                n = math.hypot(*away) or 1.0
                return (away[0] / n * PORTAL_PUSH_PX, away[1] / n * PORTAL_PUSH_PX)
            self.phase, self.phase_t = "settle", now
            self.push_from = None
            return None
        if self.phase == "dwell":
            if d > PORTAL_HOLD_R + 30:
                self.phase, self.phase_t = "settle", now      # 被挤出去了, 重新量
                return None
            if now - self.phase_t > PORTAL_DWELL_MAX_S:
                if self.reentries >= PORTAL_REENTRY_MAX:
                    self.failed = f"在传送门里站了 {self.reentries + 1} 次都没传送"
                    return None
                self.reentries += 1
                self.log.append(f"⚠️ 在门里站了 {PORTAL_DWELL_MAX_S:.0f} 秒没传送, 出去再进一次({self.reentries})")
                self.phase, self.phase_t = "leave", now
            return None
        # settle: 松手等停稳, 停稳了再决定下一下
        if not self._at_rest(now) and now - self.phase_t < PORTAL_COAST_MAX_S:
            return None
        if self.push_from is not None and self.push_s > 0:
            moved = math.hypot(here[0] - self.push_from[0], here[1] - self.push_from[1])
            learned = moved / self.push_s
            self.rate = learned if self.rate is None else 0.5 * self.rate + 0.5 * learned
            self.push_from = None
        if d < PORTAL_HOLD_R:
            self.phase, self.phase_t = "dwell", now
            self.log.append(f"🚪 已站进传送门(离门心 {d:.0f}), 等传送")
            return None
        if self.best is None or d < self.best - 30.0:
            self.best, self.no_gain = d, 0
        else:
            self.no_gain += 1
            if self.no_gain >= PORTAL_NO_GAIN_PULSES:
                self.failed = f"朝传送门点了 {self.no_gain} 下没靠近(离门 {d:.0f})"
                return None
        if self.rate:
            push_s = PORTAL_PULSE_AIM * d / self.rate
        else:
            push_s = PORTAL_PULSE_FIRST_S
        self.push_s = min(max(push_s, PORTAL_PULSE_MIN_S), PORTAL_PULSE_MAX_S)
        self.push_dir = ((self.target[0] - here[0]) / d, (self.target[1] - here[1]) / d)
        self.push_from = here
        self.phase, self.phase_t = "push", now
        return (self.push_dir[0] * PORTAL_PUSH_PX, self.push_dir[1] * PORTAL_PUSH_PX)


def _walk_into_portal(portal_world, biome, deadline, progress_world=30.0):
    """走进门、停在里面, 一进传送(画布上自己的小地图点连着两次读不到)就换服。
    返回 True = 已经在传送途中换了服; False = 没走进去(卡住 / 超时 / 死了 / 站进去也不传)。

    不认"洞口光效": 第四份录像里它认成了别人身上转圈的金色特效, 朝错的方向顶墙。门的世界
    坐标是固定的(map_routes, 跟 florr 官方地图 ant_hell.tmj 的 to_garden 传送点一致), 自己的
    世界坐标画布每帧都有, 直接比就行。怎么走进去见 _PortalPulser。"""
    misses = 0
    pulser = None
    next_death_check = 0.0
    ticks = []
    prev_t = None
    try:
        while time.time() < deadline:
            if time.time() >= next_death_check:
                if on_death_screen():
                    return False
                next_death_check = time.time() + PORTAL_DEATH_CHECK_S
            here = canvas_player_world()
            now = time.time()
            if prev_t is not None:
                ticks.append(now - prev_t)
            prev_t = now
            if here is None:
                misses += 1
                if misses >= 2:
                    print("🌐 已进传送门, 现在换服")
                    switch_server(biome)
                    return True
                time.sleep(0.05)
                continue
            misses = 0
            if pulser is None:
                pulser = _PortalPulser(portal_world, now)
            off = pulser.step(now, here)
            for line in pulser.log:
                print(f"{line} {_tick_stats(ticks)}")
            pulser.log.clear()
            if pulser.failed:
                print(f"⚠️ {pulser.failed} {_tick_stats(ticks)}")
                return False
            cx, cy = enemy_detect.current_center()
            k = mouse_scale()
            ox, oy = off if off is not None else (0.0, 0.0)
            with _NoPyautoguiPause():
                _move_mouse_safely(clamp_to_screen(cx + ox * k, cy + oy * k))
            pulse = pulser.pulse_s() if off is not None else None
            if pulse is not None:
                # 这一下推多久按计时器掐, 不按读位置的节拍: 每拍读一次要 ~75ms, 推 0.03 秒会被
                # 拖成 0.075 秒以上, 跑图装一下就滑 180 单位, 比门还宽, 只能在门两边来回
                time.sleep(pulse)
                with _NoPyautoguiPause():
                    _move_mouse_safely(clamp_to_screen(cx, cy))
                pulser.end_pulse(time.time())
                continue
            time.sleep(0.02)
        print(f"⚠️ 朝传送门走, 预算用完 {_tick_stats(ticks)}")
        return False
    finally:
        reset_keyboard()


def _portal_staging_point(binary_map, portal, min_px=PORTAL_STAGING_MIN_PX, search=8):
    """门附近、主连通区里、离门至少 min_px 格的可走点里离门最近的那个。门本身在地图上是墙。
    附近找不到 -> 退回 calibrate_player(离门最近的可走点)。"""
    walkable = (binary_map == 255).astype(np.uint8)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(walkable, connectivity=8)
    if num <= 1:
        return calibrate_player(binary_map, portal)
    main_label = max(range(1, num), key=lambda i: stats[i, cv2.CC_STAT_AREA])
    px, py = portal
    rows, cols = binary_map.shape[:2]
    best = None
    for y in range(max(0, py - search), min(rows, py + search + 1)):
        for x in range(max(0, px - search), min(cols, px + search + 1)):
            d = math.hypot(x - px, y - py)
            if d >= min_px and labels[y, x] == main_label and (best is None or d < best[0]):
                best = (d, (x, y))
    return best[1] if best else calibrate_player(binary_map, portal)


def _switch_server_via_portal(biome, portal, enemy_watch=None, timeout=PORTAL_SWITCH_TIMEOUT,
                              want_defense=None):
    """走进蚁穴回花园的传送门, 传送(黑屏)那一下换服 —— 用户 2026-09-28 给的技巧: 这样
    新服务器里出生点还在蚁穴, 不用从花园重新走进来(录像里直接换服每次多走 2 分多钟)。

    want_defense: 时块的反转防御配置。开着时泡泡会把人推来推去(第三份录像: 两次都在门边
    晃、最近 ~130 世界单位没踩上), 跟花园进场贴洞口一样先关掉; 没换成就恢复, 换成了不用管
    (新服进局后 _reassert_florr_toggles 会按配置写回)。

    返回 True = 已经在传送途中换了服; False = 没走到 / 没传送上 / 死了, 调用方退回直接换服。
    最坏情况(时机没卡上)就是落回花园, 跟直接换服一样, 进场路线会接着走。"""
    deadline = time.time() + timeout
    binary_map = load_binary_map()
    if binary_map is None:
        return False
    staging = _portal_staging_point(binary_map, portal)
    print(f"🚪 蚁穴换服: 走进回花园的传送门 {portal}, 传送时换服(出生点不重置)")
    overlay.update(state="换服务器", target=portal, message="走到回花园的传送门, 传送时换服")
    defense_off = False
    switched = False
    try:
        if want_defense:
            florr_settings.ensure_flag(cdp_bridge.eval_js, florr_settings.INVERT_DEFENSE_ADDR, 0)
            defense_off = True
        here = get_player_position()
        if here is None or math.hypot(here[0] - portal[0], here[1] - portal[1]) > PORTAL_DIRECT_PX:
            # 离得远才寻路到门旁的集结点; 近了直接走 —— 出生点附近地图画的墙比游戏里多,
            # 寻路会绕一大圈(第四份录像: 离门 6 格, 规划出来先往下绕到 y=115)
            box = [(staging[0] - 2, staging[1] - 2), (staging[0] + 2, staging[1] + 2)]
            if not lazy_theta_pathing(staging, [box], deadline=deadline, enemy_watch=enemy_watch):
                return False
        if on_death_screen() or on_start_screen():
            return False
        portal_world = (map_routes.ANTHELL_TO_GARDEN_PORTAL_WORLD
                        if portal == map_routes.ANTHELL_TO_GARDEN_PORTAL else None)
        if portal_world is None:
            return False
        switched = _walk_into_portal(portal_world, biome, deadline)
        return switched
    finally:
        if defense_off and not switched:
            try:
                florr_settings.ensure_flag(cdp_bridge.eval_js,
                                           florr_settings.INVERT_DEFENSE_ADDR, 1)
            except Exception as e:
                print(f"⚠️ 恢复反转防御失败(下轮开头会重写): {e}")


# 进场后要核对"真的落在这张图上"的单图路线。海洋/丛林的标题页按钮坐标是从截图量的外框中心
# (utils._BIOME_BUTTON_POS), 没有在实机上悬停验证过 —— 点偏了就照旧落进花园, 而单图路线
# 本来不问"我在哪张图", 会拿着 ocean.png/jungle.png 在花园里刷。沙漠/花园的按钮是实机量过
# 的, 不在这里(沙漠那一轮读服务器号有害, 见 _run_entry_route 里的说明)。
_LANDING_CHECK_MAPS = ("ocean", "jungle")


def _run_entry_route(route, stage_state, timeout=ENTRY_ROUTE_TIMEOUT, want_defense=None):
    """进场路线: 从人当前所在的图, 一段段走到刷怪那张图。

    单图路线(沙漠 / 花园 / 海洋 / 丛林)第一圈就 "arrived", 整段空转 —— 沙漠/花园现有行为
    一字不变; 海洋/丛林只在进圈前多一次落地核对(_LANDING_CHECK_MAPS), 读得到服务器号且
    不是这张图就 "timeout", 读不到照旧 "arrived"。

    want_defense: 这个时块配置的反转防御键期望值(round 开始时 _reassert_florr_toggles
    已经按它设过一次)。用户反馈(2026-09-20): 反转防御开着时角色带着"泡泡"
    效果, 会让走位卡不到传送门的精确坐标上。

    这个开关的时机来回改了两次, 都是照用户实机复盘调的:
      第一版: 整个函数入口就关掉、函数返回才恢复 —— 太早了("还没到就直接
        关了": 人可能还在花园里老远的地方往传送点走, 根本没到会被"泡泡"卡
        精度的那一步).
      第二版: 收紧到只包住每一次 _walk_toward_visible_portal() 调用, 每次
        贴近尝试完(不管成没成功)立刻恢复 —— 又变成太晚了反过来太频繁: 一轮
        重试没贴近成功、下一轮重新贴近之前那个间隙里又被打开了, 用户反馈
        "没进传送门之前就不要开反转防御控制了"——还没真正进传送门这段时间,
        不该在重试之间来回开关.
      现在这版: defense_off 这个标记贴在整个函数调用的生命周期上, 不是单次
        贴近尝试上 —— 第一次真的要贴近传送门光效时才关(不是函数一开始就关,
        避免第一版的问题), 关了之后不管重试几轮都保持关着, 直到这个函数
        真的要返回(推进成功/被打断/超时, 任何一条路径)才在 finally 里恢复
        (不是每次贴近尝试后就恢复, 避免第二版的问题)。want_defense 是
        False/None(配置本来就不要反转防御, 或调用方没传)时整段不碰这个
        开关, 不给desert/ocean这类用不上的路线凭空加一条CDP依赖。

    返回:
      "arrived"     人已经在刷怪图上, 可以开始按 location/farming_area 寻路
      "interrupted" 路上死了 / 被踢回菜单, 交回主循环顶按现有分支处理
      "timeout"     预算用完还没走到 (或落在这条路线之外的图上)
    """
    deadline = time.time() + timeout
    if route.final_map in _LANDING_CHECK_MAPS:
        # 标题页按钮点偏 = 落进花园。服务器号读得到才判(读不到 = None, 照旧往下走, 不比
        # 以前更差); 落错了就别拿 ocean.png/jungle.png 在花园里刷, 交回上层跳过这一轮。
        landed = florr_server.current_map_name(cdp_bridge.eval_js)
        if landed is not None and landed != route.final_map:
            print(f"⚠️ 本时块要去 {route.final_map}, 但服务器号显示落在 {landed} —— "
                  "标题页生态区按钮多半点偏了(坐标是从截图量的, 没实机验证过), 跳过这一轮")
            overlay.update(state="出错",
                           message=f"落在 {landed}, 不是 {route.final_map}(标题页按钮点偏?)")
            return "timeout"
    defense_off = False   # 这次调用里关过反转防御了吗 —— 关了就不该在重试之间
                          # 来回开关, 只在这个函数真的要返回时才在 finally 里恢复.
    hopped = False   # 本次调用里刚确认传送走了(advance 过): 服务器号/区域名/缩放比都读不出时信阶段状态
    try:
        while time.time() < deadline:
            # 读服务器号是权威; 读不到(探针没装 / 换 build / CDP 抖)才退回自己的猜测。
            # **只有多阶段路线才读**(海洋/丛林那一次落地核对在循环外, 见 _LANDING_CHECK_MAPS):
            # 单图路线(花园/沙漠/海洋/丛林)压根没有"我在哪一段"的问题,
            # 那张图就是唯一那张图。读了反而有害 —— 沙漠那一轮只要读回来的号跟这张图
            # 对不上(别的标签页的号 / 上一台服务器的陈旧号 / 大厅号), stage_for() 就
            # 返回 None -> "timeout" -> 这一轮被跳过并记成短局 -> 攒够次数自动换服务器。
            # 那等于给**唯一在实机跑过的配置**凭空加一条中断路径。设计承诺的是"单图
            # 路线零影响"。
            current = (florr_server.current_map_name(cdp_bridge.eval_js)
                       if len(route.stages) > 1 else None) or stage_state.map_name
            if len(route.stages) > 1:
                # 画布区域名比服务器号 / 阶段猜测都可靠: 死在蚁穴后重生还在蚁穴, 但那两个
                # 都会说"花园"(蚁穴走花园那台服务器) —— 2026-09-27 实机, 3 轮各拿花园的
                # 路线去走蚁穴的墙, 白烧 180 秒。区域名读不出(下水道/工厂的中文名是猜的, 猜错就永远
                # 不匹配; 传送黑屏/切图那一瞬也读不到)就退到小地图缩放比(_canvas_scale_zone)。
                # 同样只对多阶段路线读, 单图路线零影响。
                zone = _canvas_zone_map()
                if zone is None:
                    zone = _canvas_scale_zone(route)
                if zone is not None:
                    hopped = False
                    # 读出来了就把阶段猜测对齐过去 —— 哪怕跟 current 一样。原来只在"不一样"时对齐,
                    # 结果乐观推进过的阶段状态会留着一个陈旧值(比如推进成了 sewers, 区域名和服务器号
                    # 都说 garden): 下一圈服务器号一旦读不到, current 就退回 stage_state.map_name,
                    # 又拿那个陈旧的 sewers 当依据。sync_to 对不在路线上的名字是空操作。
                    stage_state.sync_to(zone)
                    if zone != current and route.stage_for(zone) is not None:
                        print(f"🗺️ 画布区域名显示人在 {zone}, 不是 {current} —— 按 {zone} 走")
                        current = zone
                elif hopped:
                    # 同一台服务器里换图(花园 -> 蚁穴/下水道/工厂): 服务器号读出来还是花园, 区域名 /
                    # 缩放比又都读不出 —— 刚确认过传送走了, 信阶段状态, 别拿花园的路线去走新图的墙。
                    current = stage_state.map_name
                    hopped = False
            if current == route.final_map:
                apply_map(current)
                return "arrived"
            stage = route.stage_for(current)
            if stage is None:
                print(f"⚠️ 当前在 {current!r}, 不在本时块的进场路线上 —— 交回上层重开一轮")
                overlay.update(state="出错", message=f"落在路线外的图({current}), 重开一轮")
                return "timeout"
            print(f"🚪 进场路线: 在 {stage.map_name} 上寻路到传送点 {stage.walk_to}")
            overlay.update(state="进场路线", target=stage.walk_to,
                           message=f"{stage.map_name} → 传送点 {stage.walk_to}")
            apply_map(stage.map_name)
            open_map_rects(stage.opening_rects())
            # 把本次进场的总预算传下去: lazy_theta_pathing 默认是**永不返回**的
            # (卡住就脱困重来、测不到位置就 1Hz 无限重试), 上面 while 顶上的 deadline
            # 检查根本等不到它回来。洞口被堵/坐标标偏/花园玩家标记色不对时, 不传这个
            # 预算就是"worker 永远待在花园里", 连超时自愈都跑不到。
            lazy_theta_pathing(stage.walk_to, [stage.target_box()], deadline=deadline)
            # 返回值故意不看: 踩上传送点的那一刻人已经在下一张图了, 而
            # load_binary_map() 还读着上一张, calibrate_player 会把位置吸到错误的
            # 格子上 —— lazy_theta_pathing 多半返回 False。到没到, 只以下一圈重新问
            # "我现在在哪张图"为准。
            if on_death_screen() or on_start_screen():
                return "interrupted"

            # 实机复盘(2026-09-18): 用户人肉走到300x300像素容差框判定的"到了"那个
            # 点上, 传送依然没反应 —— debug_canvas_portal.py 量出来的CTM证实小地图
            # 挪1屏幕像素≈200多个世界单位, 比传送触发要求的精度粗了一整个量级,
            # 光靠像素容差框永远可能"像素上到了"但世界坐标还在几百个单位外。
            #
            # 第一版这里直接拿"canvas读到的玩家绝对世界坐标"跟硬编码的洞口坐标
            # 比 —— 实机复盘(2026-09-19)发现这条路径本身也不精确: 玩家那个世界
            # 坐标照样来自小地图玩家点, 跟像素容差框是同一层量化, 读数在137~835
            # 之间跳, 完全没帮上忙(见 _canvas_portal_world_offset 的说明)。改成
            # 直接实时定位洞口光效在主视图的渲染位置(不经过小地图量化), 才是
            # 真的绕开了那层精度天花板。stage.walk_to_world 是 None(没标定/单图
            # 路线)时跳过, 退化成改之前的纯像素判据行为; 光效找不到/卡住/超预算
            # 都老实放手, 交回下面原有的 settle+复查逻辑, 不会比改之前更差.
            if stage.walk_to_world is not None:
                # 反转防御的"泡泡"效果(用户反馈)会干扰走位精度 —— 第一次真的
                # 要贴近光效时才关, 关了之后不管重试几轮都保持关着(不在这里
                # 恢复), 到这个函数真的要返回时才在最外层 finally 里统一恢复.
                if bool(want_defense) and not defense_off:
                    florr_settings.ensure_flag(cdp_bridge.eval_js,
                                               florr_settings.INVERT_DEFENSE_ADDR, 0)
                    defense_off = True
                # 之前这一步完全静默 —— 实机复盘时分不清"这段真的没帮上忙"还是
                # "压根没跑起来"。打印结果(True/False/None), 下次实机复现就有
                # 数据看.
                walked = _walk_toward_visible_portal()
                print(f"🧲 朝洞口光效贴近: 结果={walked}")
                if walked is None:
                    # 光效压根没找到(相机解不出来 / 没进画面), 退回小地图坐标
                    # 硬走一段 —— 不看 dist<5(见 _walk_toward_minimap_target
                    # 的说明), 强制走到位置真的变化过为止.
                    walked = _walk_toward_minimap_target(stage.walk_to)
                    print(f"🗺️ 光效读不到, 改用小地图硬走贴近: 结果={walked}")

            # 落在判定框里 != 真的触发了传送 —— 实机复盘(2026-09-16)证实过这个落差:
            # lazy_theta_pathing 的判定框(±PORTAL_TOLERANCE)比florr真正触发传送要求
            # 的精度松, 角色蹭到框边就被判"到了", 但没有精确踩在触发传送的那个点上,
            # 传送压根没发生, 人还在原地(这一段的地图, stage.map_name)。
            #
            # 不能拿"这次测得到/测不到玩家位置"当验证信号: 就算传送成功、人已经在
            # 下一张图上, 屏幕上真实的小地图内容也换了, 但 calibrate_player 这会儿
            # 还在吸这一段的旧地图(stage.map_name 没变), 把新地图上随便一个黄色像素
            # 吸附到旧地图最近的可走点上, 照样能吸出一个"看似合理"的坐标, 不会是
            # None —— 那样"测到了就还没走"这条判断永远是假的, 永远不推进。
            #
            # 真正能判的是: 吸附出来的点还在不在**这一段自己的**判定框里。传送成功的
            # 话, 真实位置早就不在这一小块像素范围附近了, 吸附出来的坐标(不管吸到
            # 旧地图哪儿)几乎不可能凑巧又落回这个判定框; 没成功的话, 人确实
            # 还站在原地, 复查理所当然还是测在判定框里。
            #
            # 实机复盘(2026-09-20): 光靠单次复查还是会被噪声骗过去 —— 像素读数
            # (139,236)距目标(133,234)欧氏距离只有6.3, 但x轴上刚好越过 ±PORTAL_
            # TOLERANCE(5)的边界1个单位就被判"已离开"; canvas那边读到的世界距离
            # 1574.8, 在"真的已经离开"和"还在原地、只是这次读数噪声大"之间完全
            # 分不清(之前same-session的实机数据显示: 确认还在花园时canvas距离也
            # 经常读到几百到两千不等)。单次复查对这种噪声没有抵抗力, 两个判据
            # 都被同一次噪声一起骗过去的概率不算低。改成要求连续
            # PORTAL_LEFT_CONFIRMATIONS 次复查**都**判定"离开了"才真的推进 ——
            # 任何一次复查判"还在原地"就重新贴近, 连续确认次数清零重来。代价是
            # 真离开时要多等几个 PORTAL_SETTLE_SECONDS 才会推进, 换来的是单次
            # 噪声不再能直接骗过整个判定.
            confirmed_left = False
            for confirm_i in range(1, PORTAL_LEFT_CONFIRMATIONS + 1):
                time.sleep(PORTAL_SETTLE_SECONDS)
                still_here = get_player_position()
                pixel_says_still_here = (still_here is not None
                                         and if_in_area([stage.target_box()], still_here))

                # 2026-09-17 加的第二层复查, 跟上面这条像素判据**并存**不是替代。
                #
                # 实机复盘(2026-09-20): 第一版这里用的是 _canvas_world_distance_to
                # (读小地图玩家点的世界坐标) —— 结果发现它跟上面的像素判据其实
                # 不是两个独立信号: 两者都是从同一个小地图量化坐标衍生出来的
                # (像素直接读校准坐标, canvas读同一个小地图点解出的世界坐标),
                # 共享同一个偏差源。实测见过两次复查(隔1秒)读到一模一样的坐标
                # 和世界距离, 说明角色根本没动 —— 但这个位置本身系统性地落在
                # 判定框外, 不是随机噪声, 两条判据会一起被同一次偏差骗过去,
                # PORTAL_LEFT_CONFIRMATIONS 那层"多确认几次"对系统性偏差没用
                # (又读了一遍同一个有偏差的数据源, 不会因为多读就变准).
                #
                # 改用 _canvas_portal_world_offset() —— 跟 _walk_toward_visible_portal
                # 贴近传送门时用的是同一个函数, 直接读洞口光效在**主视图**的
                # 渲染位置, 完全不经过小地图那层量化, 是真正独立于像素判据的
                # 信号: 光效还在视野里、离玩家很近, 才说明人确实还没走; 小地图
                # 读数不管准不准, 都动不了这个结果。stage.walk_to_world 是 None
                # (这一段没有已标定的世界坐标)时这层直接跳过, 退化成改之前只用
                # 像素判据的行为, 不会更差.
                canvas_says_still_here = False
                wdist = None
                if stage.walk_to_world is not None:
                    glow_offset = _canvas_portal_world_offset()
                    if glow_offset is not None:
                        wdist = math.hypot(*glow_offset)
                        canvas_says_still_here = wdist < map_routes.PORTAL_WORLD_HOLD_BACK_RADIUS

                # 复查两条判据的原始值都打出来 —— 之前一旦"乐观推进"就完全没日志,
                # 真出现"没真的传送但还是被判定离开了"这种误判时无从查起。canvas
                # 一栏为"未启用"说明这段没标定世界坐标或钩子/相机解不出来, 不是
                # 出了故障。
                canvas_col = "未启用" if stage.walk_to_world is None else (
                    f"{'仍在原地' if canvas_says_still_here else '已离开'}"
                    + (f"(世界距离={wdist:.1f})" if wdist is not None else "(读不到)"))
                print(f"🔎 传送复查({confirm_i}/{PORTAL_LEFT_CONFIRMATIONS}): "
                      f"像素={'仍在原地' if pixel_says_still_here else '已离开'} "
                      f"({still_here}) canvas={canvas_col}")

                if pixel_says_still_here or canvas_says_still_here:
                    print(f"⚠️ 踩到 {stage.walk_to} 附近但没有真的传送(复查仍在原地), 重新贴近")
                    overlay.update(message=f"没真的传送, 重新贴近 {stage.walk_to}")
                    break
            else:
                confirmed_left = True

            if not confirmed_left:
                # 实机复盘(2026-09-20): 用户确认卡住时"鼠标停在屏幕中央, 完全
                # 不动", 直到手动碰一下鼠标才恢复 —— 排查下来不是FailSafe:
                # 屏幕中央正是 reset_keyboard() 停止移动时鼠标归位的地方。
                # 真正原因是 move_to_position() 的到达判据(离目标欧氏距离<5
                # 个小地图像素)在这个精度量级下形同虚设 —— target_box 里的
                # 点彼此之间往往就在这个5像素范围内(比如(133,238)到
                # (133,234)只差4), lazy_theta_star 规划出的每一跳还没真的
                # 发出移动指令就已经"到达"、调 reset_keyboard() 把鼠标归位,
                # 整条路径走完角色其实一步没挪; 这一轮如果 _walk_toward_
                # visible_portal 又没找到光效(walked 不是 True), 就没有任何
                # 一段代码真的尝试过移动角色 —— 卡死在原地, 直到外力(用户
                # 手动碰鼠标)打破这个僵局。
                #
                # 这里在退回上层重新规划之前, 补一脚 execute_anti_stuck() ——
                # 项目里现成的"脱困"机制, 花园没标定墙壁色(_MAP_BORDER_COLOR
                # 没有"garden"这一项)时会自动退化成随机方向硬闯几步, 足够
                # 打破"每一跳都trivially到达、全程零移动"这个僵局, 不需要
                # 知道正确方向 —— 跟用户手动碰一下鼠标是同一个效果.
                if stage.walk_to_world is not None and walked is not True:
                    print("🧭 贴近没有真的挪动位置, 补一脚脱困硬闯避免卡死")
                    execute_anti_stuck()
                continue

            # 乐观推进: 刚确认传送走了就先当人已经在下一段。下一圈里画布区域名、然后小地图缩放比
            # 会覆盖这个猜测(传送其实没成 -> 缩放比还说旧图 -> 回旧图那段重走); 只有这两个都读不
            # 出时才由阶段状态压过服务器号(同一台服务器里换图, 服务器号还是花园, 见 hopped)。
            # 读不到服务器号且猜错时, 下面的寻路会因为位置对不上而失败, 由 timeout / 主循环的
            # 短局逻辑兜住。
            stage_state.advance()
            hopped = True
        print(f"⏰ 进场路线 {timeout} 秒没走完 —— 交回上层重开一轮")
        overlay.update(state="出错", message="进场路线超时, 重开一轮")
        return "timeout"
    finally:
        # 走门那一段临时挖开的门, 出这个函数就关上。原来只靠下一次 apply_map() 换段时清 ——
        # 从任何一条非正常出口回去("timeout" / "interrupted" / 抛异常), 那扇门就一直是可走的,
        # 紧接着的刷怪寻路会把它当普通空地踩上去, 被传回花园(第六份录像那种卡法)。
        open_map_rects(())
        if defense_off:
            florr_settings.ensure_flag(cdp_bridge.eval_js, florr_settings.INVERT_DEFENSE_ADDR, 1)


def _reassert_florr_toggles(want_attack, want_defense):
    """按 config 把 florr 的反转攻击键 / 反转防御键字节写成 1(True)/0(False).
    florr 每次从菜单进局会从账号数据把这两个字节盖回 —— 所以 run_worker 启动时一次 +
    每轮进游戏后一次都要重写. 返回 {"attack": status, "defense": status}
    (status ∈ unchanged/changed/failed). unchanged 静默; changed / failed 才打日志;
    任一 failed 都不中断 worker."""
    # 顺便(重)装一次 florr_server 的服务器号探针 —— 每次(重)连接都要抓, 装一次
    # 不够(换服务器/进蚁穴那次重连会换一个新 WebSocket, 但探针本身是幂等的, 已装
    # 过就是立即返回的空操作, 不怕多调). 跟下面几行的 ensure_flag 同一个"每轮
    # 重申一次"的用法, 放在同一个函数里少一个调用点要记.
    florr_server.ensure_probe_installed(cdp_bridge.eval_js)
    out = {}
    for name, label, addr, want in (
        ("attack", "反转攻击键", florr_settings.INVERT_ATTACK_ADDR, 1 if want_attack else 0),
        ("defense", "反转防御键", florr_settings.INVERT_DEFENSE_ADDR, 1 if want_defense else 0),
    ):
        status, detail = florr_settings.ensure_flag(cdp_bridge.eval_js, addr, want)
        out[name] = status
        if status == "changed":
            print(f"✅ {label} 已(重新)设为 {'开' if want else '关'}")
        elif status == "failed":
            print(f"⚠️ {label} 未确认 ({detail}) —— 手动到 设置→控制 里勾/取消")
    return out


def _wait_for_start_menu(timeout=15, interval=0.5):
    """轮询等 florr 开局菜单("开始"按钮)出现/回来. 点生态区选择器可能触发一次重连、
    florr 短暂离开开局菜单 —— 选完等菜单回来再点开始, 别在重连空档里空点(那会让
    click_start_game 复查时以为已经进去了). 到点还没出现返回 False, 调用方
    (click_start_game) 自己还会重试. 已经在菜单上则立刻返回 True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if on_start_screen():
            return True
        time.sleep(interval)
    return False


def run_launch_chrome(cfg, alias=None, timeout=30):
    """非交互地把专用 Chrome 拉起来并等 florr.io 标签页出现. 返回进程退出码.

    无头服务器(Xvfb)上专用的入口: 交互式引导 cdp_bridge.launch_dedicated_chrome()
    会 input() 阻塞, GUI 那条路(gui_app._enter_block)要 X 起不来, 而 run_worker()
    在 Chrome 没就绪时直接 sys.exit(1) —— 所以得有这么一条零人工的路.

    alias=None 取 config 里第一个账号. 失败(账号不存在 / 没登录过 / 没装 Chrome /
    标签页没起来)都返回 1 并打印原因, 不抛 —— systemd 的 ExecStartPre 靠退出码
    决定要不要接着起 worker.
    """
    profiles = cfg.get("profiles") or []
    if alias is None:
        if not profiles:
            print("❌ config.json 里没有任何账号(profiles 为空).")
            return 1
        alias = profiles[0]["alias"]

    rel = app_config.profile_dir(cfg, alias)
    if rel is None:
        known = ", ".join(p["alias"] for p in profiles) or "(无)"
        print(f"❌ 账号『{alias}』不存在. 已有账号: {known}")
        return 1

    pdir = app_config.abs_profile_path(rel)
    if not os.path.isdir(pdir):
        # 跟 gui_app._enter_block 同一个判断: 目录不在 = 这个号没在本机登录过.
        # 硬拉起来只会停在 florr 的登录选择页, worker 之后白等一轮超时.
        print(f"❌ 账号『{alias}』还没登录过({pdir} 不存在). "
              f"先在有显示器的机器上用 GUI 登录, 再把这个目录拷过来.")
        return 1

    print(f"🚀 用账号『{alias}』启动专用 Chrome...")
    try:
        cdp_bridge.launch_chrome_for_profile(
            pdir, open_url="https://florr.io", fullscreen=True)
    except RuntimeError as e:
        print(f"❌ 起 Chrome 失败: {e}")
        return 1

    if cdp_bridge.wait_for_florr_tab(timeout) is None:
        print(f"❌ 等了 {timeout}s 没等到 florr.io 标签页. "
              f"确认 DISPLAY 指向跑着的 Xvfb, 且 Chrome 没有崩在启动阶段.")
        return 1

    print("✅ 专用 Chrome 就绪, 可以起 worker 了.")
    return 0


def run_worker(cfg):
    """刷怪 worker: 由 GUI 以 `main.py --worker` 子进程拉起. 掉线/死亡后自动点
    开始重来, 不主动停(沿用改造前 __main__ 的行为)."""
    global _WRONG_MAP_SEEN
    # 收尾闸装在最前面: 后面任何一步炸了、被 GUI 停掉、或者 Ctrl+C, 都要保证键是
    # 松的。实机现象是退出后整台机器一直按着 shift(见 install_shutdown_cleanup)。
    install_shutdown_cleanup()
    # 路线闸门放在**最前面**, 早于 Chrome 检查、早于悬浮窗、早于任何点击。
    # 待标定值缺着的蚁狱时块必然启动即退出, 而 GUI 把任何退出都当崩溃
    # (gui_app._on_worker_exit 清掉 _running_block_id), 调度器每 _TICK_MS(30 秒)
    # 就把这个时块重新拉起一次 —— 整个时段都在循环。闸门要是排在 create_overlay()
    # 后面, 那就是每 30 秒建一个 AppKit 悬浮窗再拆掉; 排在游客登录分支后面, 还会
    # 每 30 秒往屏幕上真点一下鼠标。这里只 print 不写悬浮窗: 进程紧接着就退出,
    # 悬浮窗上的字一眼都看不到(而且这会儿还没有悬浮窗)。
    # 路线只在 _apply_worker_config 里算这一次, 下面全用 w["route"], 不另起一份。
    w = _apply_worker_config(cfg)
    blocker = _route_blocker(w["route"])
    if blocker is not None:
        print(f"❌ 地图「{w['map_name']}」跑不起来: {blocker}")
        sys.exit(1)

    # 直接 python main.py --worker 调试时给个清楚的报错 —— 交互式 Chrome 引导
    # 已经搬进 GUI, 这条路不再自己拉 Chrome.
    if not cdp_bridge.is_dedicated_chrome_ready():
        print("❌ 专用 Chrome 未就绪. 请从 GUI 启动(GUI 会引导你准备 Chrome).")
        sys.exit(1)

    # florr-auto-afk 的生命周期整个归 GUI 管(gui_app._ensure_afk / _on_afk_toggle,
    # 两条路都先看 AFK 开关). worker 这边只 poll_afk_pause() 读它的日志, 绝不自己
    # 去拉起它 —— 以前无条件 ensure_florr_auto_afk_running() 有两个真实后果:
    # (1) exe 不在时它会走 input() 问要不要下载, 而 console=False 的打包 exe 里
    #     worker 的 stdin 是死的, 直接 RuntimeError 把 worker 撂倒;
    # (2) 用户刚在界面上关掉 AFK 开关(GUI 已 stop_florr_auto_afk), worker 一起来
    #     又给它拉回去.
    global overlay
    overlay = create_overlay()

    # bot 自己不按攻击键, 靠 florr 的「反转攻击键」持续输出;「反转防御键」是对称的
    # 可选项. florr 每次从菜单进局都会从账号数据重载设置、把这两个字节盖回原值 ——
    # 所以不能只在这写一次, 每轮进游戏后都要重写(_reassert_florr_toggles, 见主循环).
    # 这里先探一次给即时反馈: 地址没标定 / florr 更新导致地址失效时立刻在悬浮窗
    # 警告, 不用等第一轮.

    # 没登录过的 Chrome profile 停在 florr 的登录选择页(绿色「以游客身份游玩」
    # + Discord/Apple). 先点掉它, 让 florr 开始加载游戏 —— 否则下面的
    # _reassert_florr_toggles() 走 CDP 读 window.Module 时 WASM 还没就绪, 白报
    # 一次 failed. 登录过的号 on_guest_screen() 恒 False, 这段是 no-op.
    if on_guest_screen():
        print("👤 未登录标题页, 先点『以游客身份游玩』进正常标题页...")
        overlay.update(state="重新开始", message="点击游客登录...")
        click_play_as_guest()
        # 实机复盘(2026-09-16): 这里以前是死等 sleep(2), 赌标题页 2 秒内一定渲染完。
        # 赌输过一次: florr 还没切过去, 下面 round loop 顶部三个画面检测
        # (on_guest_screen/on_death_screen/on_start_screen)全部落空, 代码当成
        # "已经在游戏里"直接冲进 _run_entry_route 空转找位置, 一路重试到超时才
        # 靠 lazy_theta_pathing 内部的死亡/开局检测把这轮打断——比这里多等几秒
        # 贵得多(浪费一整轮), 还会误判成"这条命只撑了X秒"记一次短局。
        # 改成跟 select_biome_on_title 之后同款的轮询确认(_wait_for_start_menu),
        # 不是死等: 提前到就提前走, 15 秒还没到才继续(退化成原来的行为, 不会更差)。
        if not _wait_for_start_menu():
            print("⚠️ 点游客身份后15秒还没等到开局菜单, 继续往下走(靠主循环自己纠正)")

    stage_state = _StageState(w["route"])
    location = w["location"]
    farming_area = w["farming_area"]
    farming_duration = w["farming_duration"]
    CONSECUTIVE_SHORT_ROUND_LIMIT = w["short_round_limit"]


    # 反转攻击键 / 反转防御键的目标值来自当前时块 (active 切片, 见 _apply_worker_config).
    # 调度器换时块会重启 worker, 所以整个 worker 生命周期用同一份就够. florr 每次进局
    # 会从账号数据把这两个字节盖回 —— 每轮进游戏后重写一次(见主循环).
    want_attack = w["invert_attack"]
    want_defense = w["invert_defense"]
    if "failed" in _reassert_florr_toggles(want_attack, want_defense).values():
        overlay.update(message="⚠️ 反转键未全部确认, 见日志")

    print("🎮 开始自动寻路+刷怪 (掉线/死亡后自动点开始重来, 不主动停)\n")
    consecutive_short_rounds = 0
    round_count = 0
    # 生态区只在本次 worker 启动后的第一次进游戏时锁一次. florr 死亡/重生会留在
    # 同一台服务器 = 同生态区, 每次重生都 forceServerID 纯属多一次重连、拖慢重生.
    # 换服务器(连续短局那条)走 switch_server(w["biome"]) 自己保证生态区, 不影响这个标记.

    # switch_server() 之后紧跟着的下一次"死亡结算画面"是断线重连的过渡态, 不是
    # 真死亡 —— 点"继续"会把进度重置到检查点(用户实机确认). 用户: "如果检测到死亡
    # 结算画面之后就不要点了, 因为点了之后就会重置检查点". 只吞掉换服务器后的
    # 第一次死亡画面: 消费一次就清掉, 真死亡照常点(不会一直卡着不点).
    just_switched_server = False
    # 蚁穴要换服时不当场换(当场换 = 新服从花园出生点走过来), 先在蚁穴复活, 再走进回花园的
    # 传送门、传送那一下换 —— 见 _switch_server_via_portal。
    portal_switch_pending = False
    # 生态区选择器也一样: 用户实机确认"死了之后点沙漠坐标导致重置了检查点" ——
    # 点这个按钮本身(不只是死亡画面的『继续』)会触发同一种重置. 所以只在本次
    # worker 启动后第一次进开局菜单时点一次, 后续每次重生(回到开局菜单)都不再点
    # (florr 死亡/重生会留在同一台服务器 = 同生态区, 不点也不会跑去别的生态区).
    biome_selected = False
    while True:
        round_count += 1
        round_start_time = time.time()
        _WRONG_MAP_SEEN = None
        print(f"\n{'='*50}\n第 {round_count} 轮\n{'='*50}")

        entered_game = False
        if on_guest_screen():
            print("👤 检测到未登录标题页, 点击『以游客身份游玩』...")
            overlay.update(state="重新开始", message="点击游客登录...")
            click_play_as_guest()
            # 跟 run_worker() 启动时那处同一个理由: 轮询确认标题页真的到了, 别死等
            # 固定秒数——这里格外要紧, 因为等到了以后下面紧接着还有
            # on_death_screen()/on_start_screen() 两个 if, 轮询成功的话本轮就能
            # 顺势接上"点开始"那一支, 不用再多耗一整轮才被动发现.
            if not _wait_for_start_menu():
                print("⚠️ 点游客身份后15秒还没等到开局菜单, 继续往下走(靠本轮下面的检测纠正)")
        if on_death_screen():
            if just_switched_server:
                print("💀 换服务器后的死亡结算画面, 不点『继续』(点了会重置检查点), "
                      "等重连自己过去...")
                overlay.update(state="换服务器", message="重连过渡画面, 暂不点继续")
                just_switched_server = False   # 只吞这一次, 真死亡照常点
                time.sleep(2)
            else:
                print("💀 检测到死亡结算画面, 点击继续...")
                overlay.update(state="重新开始", message="死亡, 点击继续...")
                click_continue_after_death()
                entered_game = True
                time.sleep(2)
        if on_start_screen():
            print("🔁 检测到开局菜单, 点击开始按钮进入游戏...")
            overlay.update(state="重新开始", message="点击开始按钮...")
            just_switched_server = False   # 重连正常落到了标题页, 恢复正常死亡画面处理
            if w["route"].final_map in _LANDING_CHECK_MAPS:
                # 只给要做落地核对的那两张图(海洋/丛林)清: 探针记的服务器号活在页面里, 跨
                # worker 重启/时块切换都不会没 —— 花园时块切到海洋时, 核对会读到上一个时块那台
                # 花园服务器的号, 判"按钮点偏了"白跳一轮。清成 null = 读不到, 核对照旧往下走。
                # 别对其它路线清: 多阶段路线每圈都靠这个号分"还在花园 / 已经进蚁穴", 清了就瞎了。
                florr_server.clear_last_server_id(cdp_bridge.eval_js)
            if not biome_selected:
                # 只在本次 worker 启动后第一次进开局菜单时点一下生态区选择器 ——
                # florr 不记忆上次选的, 默认花园, 跟寻路用的地图对不上. (CDP
                # forceServerID 那条路试过反复不行, 只有像素点这个 canvas 按钮
                # 管用.) 但这个按钮点了会重置检查点(用户实机确认), 所以只点这一次,
                # 后续每次重生都跳过. 选生态区可能触发一次重连, 等"开始"按钮从
                # 空档回来再点.
                select_biome_on_title(w["biome"])
                _wait_for_start_menu()
                biome_selected = True
            click_start_game()
            # 鼠标还停在「开始」按钮上 = 一进局就朝右边走, 后面写开关、换装又要好几秒 ——
            # 第五份录像复活后朝那边走了 6 秒, 从传送门旁边走出去 1000 多单位。归中 = 原地等。
            keyup("w")
            entered_game = True
            stage_state.reset()   # 从标题页重新进场 = 回到路线第一段(蚁穴 -> 花园)
            time.sleep(3)

        # 进游戏了(或本来就在局内): florr 刚才可能从账号数据把「反转攻击键 /
        # 反转防御键」重置了, 每轮重写一次. unchanged 静默(常态), changed / failed
        # 才打日志.
        _reassert_florr_toggles(want_attack, want_defense)

        # 只在这一轮真的(重新)进了游戏、或首轮时才切 loadout —— 一命跑满
        # farming_duration 没死的下一轮不过上面两个分支, 玩家还在场上、florr 没重置
        # loadout, 再按一次 digits 这种盲切换会让非对称配置每轮漂移. press_swap
        # 内部 warn-only, 不打断轮次.
        swap_this_round = entered_game or round_count == 1
        if swap_this_round:
            loadout_swap.press_swap(w["enter_game_swap"])

        # 蚁穴这类"标题页选不到、得先进花园再踩洞口传送"的图: 先把进场路线跑完,
        # 人真的落在刷怪那张图上再开始寻路。单图路线(花园/沙漠/海洋/丛林)这里第一圈就
        # "arrived", 整段空转。
        entry = _run_entry_route(w["route"], stage_state, want_defense=want_defense)
        # 进场路线自己在花园里走着走着被洞口传进蚁穴, 也会被寻路当成"不在这张图"—— 那是正常
        # 进场, 不算。只有进场之后(去刷怪区 / 刷怪)再被传走才算。
        _WRONG_MAP_SEEN = None
        if entry != "arrived":
            print(f"❌ 进场路线未完成({entry}), 本轮跳过刷怪")
            overlay.update(message=f"进场路线未完成({entry})")
            reached_farm = False
            if portal_switch_pending and entry == "timeout":
                # 等着走传送门换服, 人却没进到蚁穴 —— 门用不上, 当场换
                portal_switch_pending = False
                print("⚠️ 人没进到蚁穴, 传送门换服用不上, 直接换")
                try:
                    switch_server(w["biome"])
                    stage_state.reset()
                    just_switched_server = True
                    consecutive_short_rounds = 0
                    time.sleep(2)
                    continue            # 刚换完服, 这一轮不再记短局(否则同一轮又换一次)
                except Exception as e:
                    print(f"⚠️ 换服务器失败, 先用当前服务器继续 (下轮再重试): {e}")
            time.sleep(1)
        else:
            if portal_switch_pending:
                portal_switch_pending = False
                switched = False
                try:
                    switched = _switch_server_via_portal(
                        w["biome"], _portal_switch_target(w),
                        enemy_watch=_PathingEnemyWatch() if w["enemy_ai_enabled"] else None,
                        want_defense=want_defense)
                except Exception as e:
                    print(f"⚠️ 传送门换服出错: {e}")
                if not switched:
                    print("⚠️ 没能在传送门上换服, 直接换")
                    try:
                        switch_server(w["biome"])
                        stage_state.reset()   # 直接换 = 新服从花园出生点重新进场
                        switched = True
                    except Exception as e:
                        print(f"⚠️ 换服务器失败, 先用当前服务器继续刷 (下轮再重试): {e}")
                if switched:
                    just_switched_server = True   # 下一次死亡画面是重连过渡态, 别点
                    time.sleep(2)
                    continue
            print(f"📍 目标区域: {farming_area}\n")
            # config 里配的 location 是个写死的点, 从来没在这张图的 binary_map
            # 上验证过是不是可走 —— 蚁穴这条路线在这次修复之前进场就没成功过,
            # 这个坑一直没暴露: 实机复盘(2026-09-20)第一次真的走到蚁穴, 配置的
            # location 落在墙上, lazy_theta_pathing 每轮都"路径规划失败", 一轮
            # 都刷不到。跟 random_walkable_point() 解决的是同一类问题(配置/随机
            # 点不保证落在可走像素上), 这里退回同一个函数在 farming_area 内重新
            # 采样一个可走点, 不是让每一轮都死在一个打错的坐标上.
            target_location = location
            binary_map = load_binary_map()
            if (binary_map is not None
                    and not (0 <= location[1] < binary_map.shape[0]
                             and 0 <= location[0] < binary_map.shape[1]
                             and binary_map[location[1], location[0]] == 255)):
                target_location = random_walkable_point(farming_area, binary_map)
                print(f"⚠️ 配置的目标点 {location} 不可走(墙/越界), "
                      f"改用区域内随机可走点 {target_location}")
            overlay.update(state="启动", target=target_location,
                           message=f"第{round_count}轮: 开始自动寻路到刷怪区域")
            # 路上也躲究极(用户 2026-09-28), 只在这张图开了索敌时
            reached_farm = lazy_theta_pathing(
                target_location, [farming_area],
                enemy_watch=_PathingEnemyWatch() if w["enemy_ai_enabled"] else None)
        if reached_farm:
            print("✅ 到达刷怪区域！")
            # 到刷怪区了: 按配置的键切到"输出" loadout. 跟 enter swap 同一道 gate ——
            # 存活续命轮 florr 没重置 loadout, 不重按.
            if swap_this_round:
                loadout_swap.press_swap(w["reach_area_swap"])
            auto_farming(farming_area, farming_duration,
                         enemy_ai_enabled=w["enemy_ai_enabled"],
                         )
        else:
            print("❌ 本轮未能到达目标区域")
            overlay.update(message="本轮未能到达目标区域")
            time.sleep(1)

        # entry == "interrupted": 进场路线被死亡/开局画面打断, 不是"烧满了预算"
        # —— 不管是不是这一轮才 entered_game(死后点继续+点开始重进也算
        # entered_game=True), 都不该记短局。实机复盘(2026-09-19)用户反馈:
        # "我只死了一次, 却算两局连续死亡" —— 死亡那一轮(entry 走到底、
        # 真烧了不少时间)算1次短局没问题, 但死后紧接着的下一轮如果重进场
        # 很快又被打断(entry=="interrupted"), 那只是**同一次死亡的重进尝试
        # 失败**, 不是又死了一次; 旧逻辑要求 entered_game 恒 False 才豁免
        # (只覆盖"压根没点过继续/开始"的开机空档), 死亡处理本身会把
        # entered_game 设成 True, 这条豁免反而用不上, 两次一凑就误触发
        # switch_server。用户明确要求: 只有 entry == "timeout"(真的烧满
        # ENTRY_ROUTE_TIMEOUT, 比如洞口永久进不去)才该记短局, 否则洞口卡死
        # 也永远攒不够次数去自愈换服 —— 这条自愈路径必须留着, 只是
        # "interrupted" 不再算数.
        if entry == "interrupted":
            print("↩️ 进场路线被打断(死亡/开局画面), 不计入短局, 重开一轮")
            overlay.update(state="重新开始", message="进场被打断, 重开一轮")
            time.sleep(2)
            continue

        # 寻路途中发现人被传到了别的图(传送门) —— 这一轮没法在这张图上刷, 但不是死了也不是
        # 洞口进不去, 不记短局。下一轮进场路线按画布区域名从那张图重新走(花园 -> 洞口)。
        if _WRONG_MAP_SEEN is not None:
            print(f"↩️ 人被传到了 {_WRONG_MAP_SEEN}, 不计入短局, 下一轮从那里重走进场路线")
            overlay.update(state="重新开始", message=f"人在 {_WRONG_MAP_SEEN}, 重走进场路线")
            stage_state.sync_to(_WRONG_MAP_SEEN)
            time.sleep(1)
            continue

        # 这轮既没在循环顶撞见死亡/开局/游客画面(entered_game 一直 False), 又没能
        # 寻路到刷怪区 —— 典型是换服/重连的空档: 开局菜单还没画出来, 循环顶
        # on_start_screen() 没抓到、没点开始, 等控制权到了 lazy_theta_pathing 菜单才
        # 渲染完被它的循环顶抓到并 return False。没进过场就没有"这条命", 不能算短局
        # (否则白 strike 攒够两次会误触发又一次 switch_server, 换服抖动). 直接重开
        # 一轮, 下一轮循环顶的 on_start_screen() 会把开始按钮点掉正常进场.
        # entry != "interrupted" 到这里只剩 "arrived"(entry=="timeout" 落进
        # 下面正常的短局统计) —— 单图路线(花园/沙漠/海洋/丛林)entry 恒 "arrived", 这里
        # 退化成原来那个比较, 行为不变; entered_game 是 True 时不豁免, 是因为
        # 那种情况下失败的是"刷怪区寻路"本身, 跟进场/传送无关, 该照常计短局.
        if not entered_game and not reached_farm and entry == "arrived":
            print("↩️ 本轮未进游戏(停在开局/死亡画面), 不计入短局, 重开一轮")
            overlay.update(state="重新开始", message="上轮卡在菜单, 重开一轮")
            time.sleep(2)
            continue

        round_elapsed = time.time() - round_start_time
        # 进场路线没走到的一轮一秒怪都没刷, 墙钟走了多久都不算"刷满" —— 别把这个
        # entry 判断"简化"掉: farming_duration 配得比 ENTRY_ROUTE_TIMEOUT(180秒)短的
        # 时块里(GUI 只校验正整数, 不设下限), 超时那一轮的 round_elapsed 反而
        # >= farming_duration, 光看时间会判成刷满 -> consecutive_short_rounds 清零 ->
        # 洞口永久进不去也永远攒不够短局, switch_server 再不会触发, 自愈就死了.
        # 单图路线(花园/沙漠/海洋/丛林)entry 恒 "arrived", 这里退化成原来那个比较, 行为不变.
        completed_full_duration = entry == "arrived" and round_elapsed >= farming_duration
        # 只要死了就算一次(用户 2026-09-25): 就地复活的那一轮照样跑满时长, 光看
        # "刷没刷满"的话死多少次都攒不够次数去换服。跑满而且一次没死才清零。
        revived = 0                 # 死了就重开一局的话, 这一轮不会"跑满"
        if completed_full_duration and not revived:
            consecutive_short_rounds = 0
        else:
            consecutive_short_rounds += revived or 1
            if completed_full_duration:
                print(f"⚠️ 这一轮死了 {revived} 次 (累计{consecutive_short_rounds}次)")
            else:
                print(f"⚠️ 这条命只撑了{round_elapsed:.0f}秒, 没到{farming_duration}秒 "
                      f"(连续{consecutive_short_rounds}次)")
            if (w["auto_switch_server"]
                    and consecutive_short_rounds >= CONSECUTIVE_SHORT_ROUND_LIMIT):
                # 只有这一轮人确实进了蚁穴(entry 到了)才走传送门; 卡在花园进不去(进场超时)
                # 的用不上蚁穴的门, 照旧当场换 —— 不然那条"洞口进不去就换服"的自愈链路就断了。
                if _portal_switch_target(w) is not None and entry == "arrived":
                    print(f"🌐 累计{consecutive_short_rounds}次(死亡/没刷满), 换个服务器 —— "
                          f"蚁穴: 先复活, 再走进回花园的传送门, 传送时换(出生点不重置)")
                    overlay.update(state="换服务器", message="复活后走传送门换服")
                    portal_switch_pending = True
                    consecutive_short_rounds = 0
                    continue
                print(f"🌐 累计{consecutive_short_rounds}次(死亡/没刷满), 换个服务器...")
                overlay.update(state="换服务器",
                               message=f"累计{consecutive_short_rounds}次(死亡/没刷满), 切换中")
                try:
                    switch_server(w["biome"])
                    stage_state.reset()   # 换到另一台花园服 = 又得重踩一次洞口
                    consecutive_short_rounds = 0
                    just_switched_server = True   # 下一次死亡画面是重连过渡态, 别点
                    time.sleep(2)
                except Exception as e:
                    print(f"⚠️ 换服务器失败, 先用当前服务器继续刷 (下轮再重试): {e}")
                    overlay.update(message=f"换服务器失败(下轮重试): {e}")


def _worker_graceful_exit(signum, frame):
    """GUI 点"停止"时给 worker 发的信号处理: 先把按住的方向键/空格松开, 再退出 ——
    直接 kill 的话这些键会一直是按下状态."""
    try:
        reset_keyboard()
    finally:
        sys.exit(0)


def _install_worker_signal_handlers():
    # POSIX 上 GUI 的"停止"会补一发 SIGTERM; Windows 上 SIGBREAK 只有从真控制台
    # (开发时 `python main.py --worker`)按 Ctrl+Break 才会来 —— 打包成
    # console=False 的 exe 之后两边都不保险, 真正的停止信号走 stdin EOF, 见
    # _install_worker_stdin_watcher().
    signal.signal(signal.SIGTERM, _worker_graceful_exit)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _worker_graceful_exit)


def _worker_stdin_watch():
    """阻塞读 stdin 直到对端关掉管道(EOF), 然后松开按住的键再退出."""
    try:
        sys.stdin.read()      # 阻塞到对端关闭管道 / 手动 Ctrl-D
    except Exception:
        pass
    try:
        reset_keyboard()
    finally:
        os._exit(0)


def _stdin_is_char_device():
    """stdin 是不是字符设备 —— tty 或 /dev/null. 这两种东西"被关掉"都不代表
    "GUI 请求停止", 不该拿 EOF 当停止信号.

    刻意判的是"是不是字符设备"而不是"是不是 FIFO": Windows 才是真正的生产环境,
    那边 GUI 用 subprocess.PIPE 拉 worker, 而 os.fstat 对 Windows 管道到底报什么
    st_mode 没在实机上验证过. 只认 FIFO 的话, 一旦它报的不是 FIFO, 停止按钮就
    退化成 3 秒后强杀 —— reset_keyboard() 来不及跑, 按住的键不松, 花接着乱跑.
    反过来写, 任何拿不准的 st_mode 都落到"装"这一边, 也就是改动前的老行为.

    拿不到真 fd (console=False 的 exe / pytest 替换过 sys.stdin) 返回 True =
    不装: 那种情况下 read() 会立刻抛异常, 老代码会顺着 os._exit(0) 把 worker
    当场干掉.
    """
    try:
        return stat.S_ISCHR(os.fstat(sys.stdin.fileno()).st_mode)
    except Exception:
        return True


def _install_worker_stdin_watcher():
    """GUI 关闭 worker 的 stdin 管道 = 请求停止. 起一个守护线程阻塞读 stdin, 读到
    EOF(管道被关)就先松开按住的键再退出 —— 打包成 console=False 的 exe 后
    CTRL_BREAK / SIGTERM 都不一定送得到, stdin EOF 是唯一跨平台可靠的信号.

    stdin 是字符设备(tty / /dev/null)时不装: systemd 默认把 stdin 接到
    /dev/null, 一读就是 EOF —— 无脑装的话 worker 在服务器上起来那一瞬间就自己
    os._exit(0) 了, 而且退出码是 0, systemd 看着像"正常跑完", 连重启都不会触发.
    终端里直接跑(stdin 是 tty)同理不装, 那种场景用 Ctrl-C 停.

    GUI 那条路(subprocess.PIPE)不是字符设备, 照旧装 —— 见 _stdin_is_char_device()
    里为什么是这个判断方向.
    """
    if sys.stdin is None or _stdin_is_char_device():
        return
    t = threading.Thread(target=_worker_stdin_watch, daemon=True)
    t.start()


def _force_utf8_stdio():
    """日志里有 ❌ ⚠️ 这类字符。中文 Windows 的 stdout 一旦不是控制台(管道 / 重定向 /
    IDE), Python 就按系统代码页 GBK 编码, 一 print 这些字符就 UnicodeEncodeError 崩掉。
    入口处统一改成 UTF-8, 编不出来的字符替换掉而不是抛异常。
    同时按行刷新: 打包版不认 GUI 给的 PYTHONUNBUFFERED, 管道上默认块缓冲, worker 日志
    要攒满 8KB 或进程退出才到面板。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass  # stream 为 None(无控制台的 pythonw)或不支持 reconfigure: 不影响运行


if __name__ == "__main__":
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="florr-auto-pathing")
    parser.add_argument("--worker", action="store_true",
                        help="内部用: 跑刷怪循环子进程(由 GUI 拉起, 不要手动加)")
    parser.add_argument("--launch-chrome", action="store_true",
                        help="无头服务器用: 非交互地起专用 Chrome 并等 florr.io 标签页")
    parser.add_argument("--profile", default=None,
                        help="配合 --launch-chrome: 用哪个账号(默认取 config 里第一个)")
    parser.add_argument("--tab-timeout", type=int, default=30,
                        help="配合 --launch-chrome: 等 florr.io 标签页的秒数(默认 30)")
    args = parser.parse_args()

    if args.launch_chrome:
        sys.exit(run_launch_chrome(app_config.load_config(),
                                   alias=args.profile, timeout=args.tab_timeout))
    elif args.worker:
        _install_worker_signal_handlers()
        _install_worker_stdin_watcher()
        run_worker(app_config.load_config())
    else:
        from gui_app import main as gui_main  # 惰性 import: 不让 `import main` 拖进 GUI 依赖
        gui_main()
