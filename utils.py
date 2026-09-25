import re
import json
import sys
import pyautogui
import time
import math
import os
import cv2
import traceback
import heapq
import random
import numpy as np

import cdp_bridge
import server_lookup

if sys.platform == "win32":
    # 没有这行, Windows下显示缩放不是100%时, PyAutoGUI截图/点击用的坐标系会被
    # 系统偷偷做DPI虚拟化映射, 跟真实物理像素对不上 —— 全屏时最明显(实测:
    # 关掉浏览器全屏反而能点对, 但一离开全屏, 屏幕中心就不等于游戏画布中心了,
    # 靠鼠标相对屏幕中心转向的移动逻辑跟着报废, 全屏/点得准不能兼得, 必须从根上
    # 让这个进程本身声明自己是DPI-aware的). florr-auto-afk(同作者同类项目)的
    # main.py末尾就有这行, 说明这个坑已经被踩过验证过.
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

try:
    SCREEN_WIDTH, SCREEN_HEIGHT = pyautogui.size()
except Exception:
    # 没有真实显示器(比如headless CI)时pyautogui.size()可能取不到 —— 退化成
    # 原来硬编码的1920x1080, 跟这个改动之前的行为完全一致, 不让import本身崩掉.
    SCREEN_WIDTH, SCREEN_HEIGHT = 1920, 1080

_REF_WIDTH, _REF_HEIGHT = 1920, 1080  # 下面这些函数里所有写死的坐标常量都是照这个分辨率量出来的


def scale_x(value):
    """把一个照1920宽量出来的x坐标/宽度, 换算成实际屏幕宽度下的等效值."""
    return round(value * SCREEN_WIDTH / _REF_WIDTH)


def scale_y(value):
    """同scale_x, 换算y坐标/高度(照1080高量出来的)."""
    return round(value * SCREEN_HEIGHT / _REF_HEIGHT)


def scale_point(x, y):
    return (scale_x(x), scale_y(y))


def scale_region(x, y, w, h):
    """把pyautogui.screenshot(region=[x,y,w,h])用的截图区域(照1920x1080量出来的)
    换算成实际分辨率下的区域. 宽高各自按对应轴单独换算(不是简单乘同一个比例),
    这样非16:9分辨率(宽高比跟1920x1080不一样)也不用另外分支处理 —— 跟
    scale_x/scale_y是同一套"每根轴独立缩放"逻辑.

    位置和宽高分开算两次scale_x/scale_y再相减(而不是直接scale_x(w)),是为了让
    四舍五入的误差不累积: right-left的差值比"起点+独立换算的宽度"更贴近实际
    截到的物理像素范围.
    """
    left = scale_x(x)
    top = scale_y(y)
    right = scale_x(x + w)
    bottom = scale_y(y + h)
    return [left, top, right - left, bottom - top]


def clamp_to_screen(x, y, margin=2):
    """把一个鼠标目标位置钳制在屏幕范围内(留一点margin) —— 防止小分辨率屏幕上
    算出来的转向偏移量把pyautogui.moveTo()的目标坐标推到屏幕外报错."""
    return (
        min(max(x, margin), SCREEN_WIDTH - margin),
        min(max(y, margin), SCREEN_HEIGHT - margin),
    )


def mouse_scale():
    """鼠标转向距离(不是绝对坐标)该乘的缩放系数. 写成函数(不是模块级常量), 跟
    scale_x/scale_y同一套模式 —— 每次调用都从当前SCREEN_WIDTH/SCREEN_HEIGHT
    重新算, 这样测试里monkeypatch这两个全局变量后, 调用方(不管是utils.py内部裸
    调用还是外部utils.mouse_scale())拿到的都是按monkeypatch后的值算出来的结果.
    """
    return min(SCREEN_WIDTH / _REF_WIDTH, SCREEN_HEIGHT / _REF_HEIGHT)

MAP = ""


def apply_map(name):
    global MAP
    assert name in [path.removesuffix(".png") for path in os.listdir("./maps")]
    MAP = name


def if_in_area(areas: list[tuple[tuple[int, int], tuple[int, int]]], point: tuple[int, int]):
    """检查point在不在areas任意一个矩形里.

    不假设area[0]一定是"左上角"、area[1]一定是"右下角" —— 之前就假设了这个顺序,
    main.py里farming_area=[(20,15),(9,76)]第一个角x比第二个角x还大, 导致
    `20 <= x <= 9`这种区间永远判不出True, 玩家哪怕站在区域正中间都判定"不在区域
    内"。这里对每个轴分别取min/max再判断, 不管两个角怎么给都能判对。
    """
    for area in areas:
        (x1, y1), (x2, y2) = area
        min_x, max_x = min(x1, x2), max(x1, x2)
        min_y, max_y = min(y1, y2), max(y1, y2)
        if min_x <= point[0] <= max_x and min_y <= point[1] <= max_y:
            return True
    return False


def distance(pos1, pos2):
    return math.sqrt((pos1[0] - pos2[0])**2 + (pos1[1] - pos2[1])**2)


# 脱困时用来找墙的墙壁色(十六进制 RGB, 匹配容差 ±3, 见下面的 inRange).
# 只列实测标定过的图. 没标定的(garden / anthell)故意不填 —— check_map_border()
# 会返回空列表, execute_anti_stuck() 本来就有"排斥力太弱就随机方向硬闯"那条
# 分支(它的注释写明这个墙色实测经常一个像素都匹配不上), 空列表精确落进那条
# 分支. 以后实测出蚁穴/花园的墙色, 填进来就自动升级成定向脱困.
_MAP_BORDER_COLOR = {
    "ocean": "4c4950",
    "desert": "4f3422",
}


def check_map_border(opencv_img):
    """按当前地图的墙壁色在全屏截图里描出墙的轮廓点.

    这张图的墙色没标定(或 MAP 还是初始空串)-> 返回 [], 不抛 —— 改之前是
    if/elif 没有 else, 这种情况下 target_color 未绑定, 直接 UnboundLocalError
    把 execute_anti_stuck() 撂倒.
    """
    target_color = _MAP_BORDER_COLOR.get(MAP)
    if target_color is None:
        return []
    target_color_bgr = tuple(int(target_color[i:i+2], 16) for i in (4, 2, 0))
    lower_bound = np.array([max(0, target_color_bgr[0] - 3), max(0,
                           target_color_bgr[1] - 3), max(0, target_color_bgr[2] - 3)])
    upper_bound = np.array([min(255, target_color_bgr[0] + 3), min(255,
                           target_color_bgr[1] + 3), min(255, target_color_bgr[2] + 3)])

    mask = cv2.inRange(opencv_img, lower_bound, upper_bound)

    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)

    points = []
    for contour in contours:
        for i in range(0, len(contour), 5):
            cv2.circle(opencv_img, tuple(contour[i][0]), 1, (0, 255, 0), -1)
            points.append(tuple(contour[i][0]))

    return points


TOGGLE_MAP_TRIGGER = (250, 50)   # 沙漠实机标定的误检像素点, 见下方说明.
TOGGLE_MAP_TRIGGER_TOLERANCE = 3.0   # precise 原始坐标是浮点、带一点检测噪声,
                                      # 留个小容差, 比 calibrate_player 那种
                                      # "吸附到最近可走点"的整数格子松一些但
                                      # 远小于地图上两个可走点之间的典型间距.


def toggle_map():
    """小地图卡在"没展开"的状态、玩家标记读不出来时按 M 展开地图.

    2026-09-19 用户反馈: 这条在花园里从来没触发过(desert 实机验证过有效,
    但从没在贴的日志里见过 anthell 路线走花园这段打出过这行)。旧代码拿
    get_player_position() 的**校准后**坐标比 (250,50) —— 那个坐标要先经过
    calibrate_player() "吸附到当前这张地图最近的可走像素"这一步, 而每张地图
    可走区域形状不一样, 同一个原始误检像素在两张地图上可能被吸附到不同的
    格子上, (250,50) 这个具体数字很可能只是沙漠地图自己的吸附结果, 不是
    误检本身的位置 —— 换到花园就对不上了.

    改用 get_player_position(precise=True) 的**原始**检测坐标(不经过
    calibrate_player 那层地图相关的吸附) —— 这个误检大概率来自小地图控件
    本身的某个固定UI元素(展开图标之类), 跟具体在哪张地图上无关, 理论上
    该是地图无关的常量。同时保留旧的"校准后坐标"判据(OR 关系, 不是替代)
    ——不确定这个理论是否完全对, 保留旧判据至少不会让沙漠现有的已验证行为
    退化.
    """
    calibrated = get_player_position()
    raw = get_player_position(precise=True)
    hit_calibrated = calibrated == TOGGLE_MAP_TRIGGER
    hit_raw = (raw is not None
              and abs(raw[0] - TOGGLE_MAP_TRIGGER[0]) <= TOGGLE_MAP_TRIGGER_TOLERANCE
              and abs(raw[1] - TOGGLE_MAP_TRIGGER[1]) <= TOGGLE_MAP_TRIGGER_TOLERANCE)
    if hit_calibrated or hit_raw:
        print(f"🗺️ toggle_map: 位置命中{TOGGLE_MAP_TRIGGER}(校准={calibrated} "
              f"原始={raw}), 按M切换地图")
        pyautogui.press('m')
        time.sleep(1)
        # 按完M不能就假设生效了 —— 用户反馈: 得再判断一次。按之前的判定用的是
        # 按M**之前**截的图, 不能代表按完之后的真实状态。重新读一次位置, 好歹
        # 打个日志能看出M到底帮没帮上忙(是不是真的能测到玩家了), 不在这里改
        # calc_anti_stuck 后续的脱困方向计算 —— 那是它自己的决定, 这里只负责
        # 如实报告"按完之后现在测到的是什么".
        after = get_player_position()
        print(f"🗺️ toggle_map: 按M之后复查位置={after}")


def calc_anti_stuck(borders, weight=1.0):
    screen_center = np.array([SCREEN_WIDTH / 2, SCREEN_HEIGHT / 2])
    total_force = np.array([0.0, 0.0])

    for point in borders:
        point_vector = np.array(point)
        distance = np.linalg.norm(screen_center - point_vector)
        if distance == 0:
            continue
        force_vector = (screen_center - point_vector) / distance
        total_force += force_vector

    final_position = screen_center + total_force * weight
    final_position[0] = np.clip(final_position[0], 0, SCREEN_WIDTH)
    final_position[1] = np.clip(final_position[1], 0, SCREEN_HEIGHT)
    toggle_map()
    return final_position[0], final_position[1]


# 地图感知脱困的搜索参数(小地图像素). 蚁穴这类"大房间 + 一两像素宽细走廊"的图
# 没标定墙壁色, execute_anti_stuck 原来只能随机蒙一个方向硬闯 1.5 秒 —— 卡在走廊
# 拐角时 8 个方向里大半是继续顶墙。改成读 maps/<图>.png 的可走图: 沿可走路径找
# 附近"净空更大"(离墙更远)的地方, 朝路径上的下一小段走, 走的是通道不是穿墙直线.
MAP_ESCAPE_SEARCH_STEPS = 25     # BFS 最多沿路径往外找这么多步
MAP_ESCAPE_LOOKAHEAD = 4         # 朝路径上第几步的点转向(太远会在拐角处抄近路顶墙)
MAP_ESCAPE_STEP_PENALTY = 0.15   # 每多走一步扣多少净空分, 越大越倾向就近脱身
MAP_ESCAPE_MIN_GAIN = 0.5        # 比原地净空至少好这么多才值得动, 否则算"已经在最宽处"
MAP_ESCAPE_TIE_BAND = 1.0        # 得分在最优这个范围内的候选随机挑一个 —— 别每次都
                                 # 选同一个点, 不然被怪/别的障碍挡住时会原地死循环


def map_escape_step(binary_map, pos):
    """从 pos 出发沿可走路径找一个净空明显更大的地方, 返回路径上前 MAP_ESCAPE_LOOKAHEAD
    步的那个点 (x, y); pos 不在可走像素上 / 附近没有更宽的地方 -> None.

    净空 = 到最近墙的欧氏距离(cv2.distanceTransform). 卡住的角色往往贴着墙、
    净空很小; 往净空大的地方走 = 离开墙、回到通道中线. 用 BFS 沿路径搜(8 邻域,
    跟 lazy_theta_star 的邻居定义一致), 不是直线距离 —— 细走廊里直线目标常常隔着墙.
    """
    if binary_map is None:
        return None
    h, w = binary_map.shape[:2]
    x, y = int(round(pos[0])), int(round(pos[1]))
    if not (0 <= x < w and 0 <= y < h) or binary_map[y, x] != 255:
        return None

    # 外面补一圈墙: distanceTransform 不把图像边界当墙, 一张没有任何墙像素的图
    # 会得到 float 上限当净空; 补一圈之后"图边 = 墙"也更符合游戏里的事实.
    walkable = np.pad((binary_map == 255).astype(np.uint8), 1)
    clearance = cv2.distanceTransform(walkable, cv2.DIST_L2, 3)[1:-1, 1:-1]
    base = float(clearance[y, x])

    parent = {(x, y): None}
    steps = {(x, y): 0}
    queue = [(x, y)]
    head = 0
    candidates = []
    while head < len(queue):
        cx, cy = queue[head]
        head += 1
        s = steps[(cx, cy)]
        if s > 0:
            score = float(clearance[cy, cx]) - MAP_ESCAPE_STEP_PENALTY * s
            if score >= base + MAP_ESCAPE_MIN_GAIN:
                candidates.append((score, (cx, cy)))
        if s >= MAP_ESCAPE_SEARCH_STEPS:
            continue
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = cx + dx, cy + dy
                if (0 <= nx < w and 0 <= ny < h and binary_map[ny, nx] == 255
                        and (nx, ny) not in steps):
                    steps[(nx, ny)] = s + 1
                    parent[(nx, ny)] = (cx, cy)
                    queue.append((nx, ny))

    if not candidates:
        return None
    best = max(score for score, _ in candidates)
    pool = [pt for score, pt in candidates if score >= best - MAP_ESCAPE_TIE_BAND]
    target = random.choice(pool)

    path = []
    node = target
    while node is not None:
        path.append(node)
        node = parent[node]
    path.reverse()          # path[0] 就是 pos 本身
    return path[min(MAP_ESCAPE_LOOKAHEAD, len(path) - 1)]


def _map_aware_escape(duration):
    """没标定墙壁色的图上的脱困: 读可走图找出路, 朝那个点转向走 duration 秒.
    真的推了一下返回 True; 读不到地图 / 玩家位置 / 找不到更好的地方 -> False, 调用方
    退回随机方向."""
    binary_map = load_binary_map()
    pos = get_player_position()
    if binary_map is None or pos is None:
        return False
    target = map_escape_step(binary_map, pos)
    if target is None:
        return False
    print(f"🧭 地图脱困: {pos} 沿通道朝 {target} 走(找净空更大的地方)")
    deadline = time.time() + duration
    pushed = False
    while time.time() < deadline:
        pos = get_player_position()
        if pos is None:
            break
        dx, dy = target[0] - pos[0], target[1] - pos[1]
        dist = math.hypot(dx, dy)
        if dist == 0 or (pushed and dist < 1.5):   # 至少推一下, 再谈"到了"
            break
        pushed = True
        extend = round(500 * mouse_scale())
        cx, cy = SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2
        pyautogui.moveTo(*clamp_to_screen(cx + extend * dx / dist, cy + extend * dy / dist))
        time.sleep(0.1)
    keyup("w")   # keyup 不看方向, 就是把鼠标归位到屏幕中心 = 停下
    return pushed


def execute_anti_stuck(duration=1.5):
    """卡住脱困: 优先用画面里的墙壁色算排斥方向, 排斥力太弱/没有就退化成随机方向硬闯.

    check_map_border靠一个写死的墙壁RGB(容差±3)在全屏截图里找墙 —— 实测这颜色
    经常一个像素都匹配不上(游戏画面是带纹理阴影的贴图, 不是纯色小地图符号,
    单一颜色+窄容差很容易全军覆没), 一旦borders是空的, 排斥力算出来就是零向量.

    光判"完全等于0"不够: 实测过好几次borders不是空的, 但只匹配到零星几个孤立
    像素, 算出来的力delta只有1像素左右(比如(0.26,-0.97)) —— 这点力换算成
    keydown按键时长约等于0, 角色压根没挪窝, 下一轮截图又是同一批孤立像素,
    算出来还是同一个delta, 陷入"看起来在脱困、实际原地不动"的死循环(实机验证过:
    连续多轮"卡住→脱困→还是卡住"打印的坐标一模一样)。改成力小于阈值(以屏幕
    像素为单位, 5px)就当没找到有效方向, 退化成随机方向硬闯.
    """
    pyautogui_img = pyautogui.screenshot(region=[0, 0, SCREEN_WIDTH, SCREEN_HEIGHT])
    opencv_img = cv2.cvtColor(np.array(pyautogui_img), cv2.COLOR_RGB2BGR)
    borders = check_map_border(opencv_img)
    suggested_position = calc_anti_stuck(borders)
    print(f"🧭 脱困: 朝 {suggested_position} 移动...")
    screen_center = np.array([SCREEN_WIDTH / 2, SCREEN_HEIGHT / 2])
    delta = suggested_position - screen_center
    max_delta = np.max(np.abs(delta))
    if max_delta < 5:
        # 没标定墙壁色的图(蚁穴/花园): 先试地图感知脱困, 动不了才随机蒙. 标定过的
        # 图(沙漠/海洋)行为不变 —— 它们是开阔地图, 随机蒙也能蒙出来.
        if MAP not in _MAP_BORDER_COLOR and _map_aware_escape(duration):
            return
        direction = random.choice(["w", "a", "s", "d", "wa", "wd", "sa", "sd"])
        print(f"⚠️ 附近没找到足够强的墙壁排斥力(力度{max_delta:.1f}), 退化成随机方向脱困: {direction}")
        keydown(direction)
        time.sleep(duration)
        keyup(direction)
        return
    duration_x = duration * abs(delta[0]) / max_delta
    duration_y = duration * abs(delta[1]) / max_delta
    if delta[0] > 0:
        keydown("d")
        time.sleep(duration_x)
        keyup("d")
    else:
        keydown("a")
        time.sleep(duration_x)
        keyup("a")

    if delta[1] > 0:
        keydown("s")
        time.sleep(duration_y)
        keyup("s")
    else:
        keydown("w")
        time.sleep(duration_y)
        keyup("w")


def keydown(direction, delta=500):
    delta = round(delta * mouse_scale())
    cx, cy = SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2
    if direction == "w":
        pyautogui.moveTo(*clamp_to_screen(cx, cy - delta))
    if direction == "s":
        pyautogui.moveTo(*clamp_to_screen(cx, cy + delta))
    if direction == "a":
        pyautogui.moveTo(*clamp_to_screen(cx - delta, cy))
    if direction == "d":
        pyautogui.moveTo(*clamp_to_screen(cx + delta, cy))
    if direction == "wa":
        pyautogui.moveTo(*clamp_to_screen(cx - delta, cy - delta))
    if direction == "wd":
        pyautogui.moveTo(*clamp_to_screen(cx + delta, cy - delta))
    if direction == "sa":
        pyautogui.moveTo(*clamp_to_screen(cx - delta, cy + delta))
    if direction == "sd":
        pyautogui.moveTo(*clamp_to_screen(cx + delta, cy + delta))


def keyup(direction):
    pyautogui.moveTo(SCREEN_WIDTH // 2, SCREEN_HEIGHT // 2)


PLAYER_MARKER_COLOR = "f8de60"   # 玩家小地图标记的真实颜色(实测验证稳定)。
# 拆成模块级常量, 不再各处各写一份 —— debug_position_diag.py 这类诊断脚本要用
# 同一个值来复现真实判定逻辑, 之前它自己抄了一份(还抄错过阈值, 见下面
# PLAYER_MARKER_MIN_RADIUS 的注释), 打出来的"诊断结论"其实跟真代码判的不是一回事,
# 会把人绕晕.


def get_player_position(precise=False):
    image = get_map()
    binary_map = load_binary_map()
    # 旧列表里那10种颜色其实是迷宫墙壁/地板色 (~95%像素覆盖),
    # 谁先匹配到就返回, 导致"位置"实际是墙壁轮廓噪声, 每帧乱跳。
    for color in [PLAYER_MARKER_COLOR]:
        position = get_player_location_on_map(
            image, color, binary_map, precise)
        if position != None:
            return position


def abandon_game():
    pyautogui.moveTo(*scale_point(307, 32))
    pyautogui.doubleClick()
    pyautogui.doubleClick()
    pyautogui.doubleClick()


# 换服务器: 试过三种更麻烦的路子, 都被这个方案替代掉了——
#   1) 点游戏内"设置"面板下拉框: 坐标拿debug_screen_pos.py标记截图逐个确认过、
#      确认落在正确的行上, 但pyautogui的点击就是不被那个画布控件识别(单击/双击
#      都试过, debug_single_click.py隔离测试过, 排除了坐标和点击次数两个变量,
#      依然选不中), 真人手动点完全正常 —— 是pyautogui合成的点击事件对这个
#      canvas控件不管用, 不是坐标或点击次数的问题.
#   2) cp6.forceServerID("码") + 手动抄的静态码表: 靠CDP执行确认过真的能触发
#      重连, 但码是从第三方florr.io服务器码追踪站(ashish.top、craft.darkmax.top)
#      人工抄的, 会不定期失效 —— 得人时不时去核对更新, 不是配一次就一直管用.
#   3) cp6.disconnect()不指定服务器: 不用管码过期, 但交给游戏自己重新分配,
#      没法保证真的换到了不同服务器(可能原地重连回同一个).
# 现在这版: forceServerID(码)还是要用(能确保真的指向一个不同服务器), 但码不再
# 手动抄 —— 直接调server_lookup.py查的官方(M28 Games)实时接口, 每次调用拿到的
# 都是当下最新数据, 天然不存在"码过期"问题.
SERVER_COOLDOWN_SECONDS = 30 * 60  # 同一个服务器码30分钟内不再选回去

_server_last_used = {}  # server_id -> 上次切到它的time.time()时间戳, 进程内存, 不落盘


def _pick_server_id(ids, last_used, now, cooldown_seconds=SERVER_COOLDOWN_SECONDS):
    """从ids里随机挑一个不在冷却期内的服务器码.

    last_used是{server_id: 上次切到它的时间戳}. cooldown_seconds内用过的码
    被排除掉, 剩下的候选里随机选一个(不是固定选第一个, 3台服务器轮着用不然
    永远只在同两个之间跳). 排除完一个候选都不剩时(比如可选服务器总共就3个,
    换得比冷却期还频繁, 全在冷却里), 退化成挑最久没用过的那个 —— 好歹是当下
    真实存在的服务器, 不能因为"非要挑没冷却过的"卡死不换.
    """
    candidates = [i for i in ids if now - last_used.get(i, 0) >= cooldown_seconds]
    if candidates:
        return random.choice(candidates)
    return min(ids, key=lambda i: last_used.get(i, 0))


def switch_server(biome="desert"):
    """查一次官方实时服务器列表(server_lookup.fetch_server_ids), 用
    _pick_server_id()挑一个30分钟内没选过的, 通过CDP在florr.io标签页里跑
    cp6.forceServerID(...)切过去.

    需要Chrome用cdp_bridge.py模块文档里那三个参数启动. 网络请求失败/找不到
    florr.io标签页时原样抛出异常(urllib的异常/cdp_bridge.eval_js()的
    RuntimeError), 不在这里吞掉 —— 换服务器失败main.py那边应该能看到报错,
    不是静默啥也没发生.
    """
    ids = server_lookup.fetch_server_ids(biome)
    now = time.time()
    server_id = _pick_server_id(ids, _server_last_used, now)
    _server_last_used[server_id] = now

    print(f"🌐 切换服务器: {server_id}")
    cdp_bridge.eval_js(f'cp6.forceServerID("{server_id}")')
    return server_id


_BUTTON_GREEN_RGB = (27, 203, 37)  # florr.io确认类按钮统一用这个绿色底(开始/继续都是)
_START_BUTTON_POS = scale_point(1059, 527)     # 开局菜单"开始"按钮(还没进过局/或已经回到开局菜单), 1920x1080下量出来的
_CONTINUE_BUTTON_POS = scale_point(959, 634)   # 死亡结算画面"继续"按钮(注意: 跟开局菜单是两个完全不同的界面!), 同样是1920x1080下量出来的
# 未登录的 Chrome profile 打开 florr.io 时, 标题页先是个登录选择页 —— 绿色
# 「以游客身份游玩」按钮 + Discord/Apple 登录. 得先点掉游客按钮才到正常的
# 「开始」菜单. 这个页面是否出现取决于账号(登录过的直接跳过). 坐标/颜色:
# 2026-09-02 浏览器 florr.io 登出态实测, canvas 内部 1920x1080 下量的
# (docs/superpowers/specs/2026-09-02-play-as-guest-click-design.md).
_PLAY_AS_GUEST_POS = scale_point(960, 498)


def _green_button_ratio(pos, half_w=15, half_h=10):
    """采样按钮周围一小块区域, 算绿色像素占比 —— 不能只采一个点.

    按钮上的文字/图标带黑色描边, 单点坐标很容易正好落在描边或图标上而不是纯色
    背景上, 只有采样一整块区域看绿色占比才稳. 实测按钮区域里文字+图标占比不小,
    纯绿色背景经常只剩10%~20%, 别把阈值定太高.

    half_w/half_h默认值是1920x1080下量出来的采样半径, 换算到实际分辨率(至少
    留1px, 否则超小分辨率下可能四舍五入成0导致采样区域是空的).
    """
    x, y = pos
    half_w = max(1, scale_x(half_w))
    half_h = max(1, scale_y(half_h))
    region = pyautogui.screenshot(region=[x - half_w, y - half_h, half_w * 2, half_h * 2])
    arr = np.array(region)[:, :, :3]
    match = np.all(np.abs(arr.astype(int) - np.array(_BUTTON_GREEN_RGB)) <= 25, axis=-1)
    return match.sum() / match.size


def on_start_screen():
    """检测屏幕上是不是正显示着开局菜单的绿色"开始"按钮(还没进局, 或已经从
    死亡画面点"继续"回到了这里).

    check_stage()那套单像素精确匹配是给别的画面校准的, 跟开局菜单对不上号(实测
    这个画面check_stage()只会返回"unknown")。与其猜另一个精确像素签名, 不如直接
    去测"开始"按钮那块是不是真是绿的 —— 检测的就是马上要点的那个东西.
    """
    return _green_button_ratio(_START_BUTTON_POS) > 0.1


# "继续"按钮比"开始"按钮小, "继续"两个字相对占比更大 —— 用_green_button_ratio()
# 默认的15x10采样半径时, 那两个字几乎能把整个采样框填满, 纯绿色背景被挤没了.
# 实机截图验证过(debug_death_click.py存的debug_before_click.png, 真死亡画面):
#   15x10框: ratio=0.0033 (被文字占满, 远低于旧阈值0.02, 检测直接判False)
#   30x16框: ratio=0.3760 (放大采样范围, 纯绿色背景占比回归正常)
# 阈值和采样半径都得跟着放大的框重新定, 不是单独调阈值就能补救.
_DEATH_SCREEN_GREEN_THRESHOLD = 0.15
_DEATH_SCREEN_SAMPLE_HALF_W = 30
_DEATH_SCREEN_SAMPLE_HALF_H = 16


def on_death_screen():
    """检测屏幕上是不是正显示着死亡结算画面("你死于XX" + 绿色"继续"按钮).

    这是跟开局菜单完全不同的一个界面(死于XX的文字、花瓣战利品面板、"继续"/"关闭"
    两个按钮, 位置和文案都不一样), check_stage()原来那套in_game_dead判定
    (探测像素(316,32)是不是纯白255,255,255)在实机上从没真正触发过 —— 同样是
    没验证过的硬编码签名。这里直接测"继续"按钮那块是不是绿的.
    """
    ratio = _green_button_ratio(
        _CONTINUE_BUTTON_POS,
        half_w=_DEATH_SCREEN_SAMPLE_HALF_W,
        half_h=_DEATH_SCREEN_SAMPLE_HALF_H,
    )
    return ratio > _DEATH_SCREEN_GREEN_THRESHOLD


# 「以游客身份游玩」按钮 ~207x32, 中间横一条白字. 跟死亡页「继续」一样, 默认
# 15x10 采样框会正好卡在白字上、测不出纯绿 —— 用宽框(半宽 60 / 半高 16)把整个
# 按钮高度 + 两侧纯绿都框进来, 白字只占中间一小块, 绿占比回到 0.4~0.65.
_GUEST_SCREEN_SAMPLE_HALF_W = 60
_GUEST_SCREEN_SAMPLE_HALF_H = 16
_GUEST_SCREEN_GREEN_THRESHOLD = 0.25
# 未验证: florr 登录后的标题页也有一块绿色「开始」按钮. _START_BUTTON_POS
# (1059,527) 跟这里的 (960,498) 采样框重叠约 5%(远低于阈值), 但只在浏览器
# 登出态量过, 登录态标题页没采样过. 若那颗「开始」按钮明显更高, 重叠可能
# 冲到阈值以上 —— on_guest_screen() 会在登录过的号上每轮误触发, 白跑
# click_play_as_guest() 的 10 次重试(~17s)后 on_start_screen() 才接手
# (会自愈, 不卡死). 上 Windows 机后截一张登录态标题页, 跑
# _green_button_ratio(_PLAY_AS_GUEST_POS, 60, 16) 确认 < 0.25, 把数值补进这里.


def on_guest_screen():
    """检测屏幕上是不是正显示着未登录标题页的绿色「以游客身份游玩」按钮.

    只有没登录过的 Chrome profile 才会卡在这一页(登录过的直接进正常标题页).
    正常标题页 / 游戏内 / 死亡结算页在 _PLAY_AS_GUEST_POS 处都不是这个亮绿,
    返回 False —— 不会误点. 点掉这一页之后由 on_start_screen() / click_start_game()
    接手真正进游戏.
    """
    ratio = _green_button_ratio(
        _PLAY_AS_GUEST_POS,
        half_w=_GUEST_SCREEN_SAMPLE_HALF_W,
        half_h=_GUEST_SCREEN_SAMPLE_HALF_H,
    )
    return ratio > _GUEST_SCREEN_GREEN_THRESHOLD


def click_continue_after_death():
    """确认死亡结算画面, 回到开局菜单(还需要再调click_start_game才能真正进下一局).

    之前两版都试过回车(先纯回车, 再补"点一下抢焦点+回车"), 实机截图拿到手才
    发现方向从一开始就错了: florr.io里回车是开聊天框的快捷键(截图左下角写着
    "按下[ENTER]或点击这里聊天"), 根本不会触发这个"继续"按钮, 跟焦点没关系.
    (959,634)这个坐标本身是准的, 对着真实1920x1080截图量过, 正落在按钮范围内.

    改回纯点击, 但沿用这个项目自己在abandon_game()里已经踩过坑验证过的套路:
    单次click()第一下常常只把窗口/标签页激活, 点击事件没能真正传进游戏画布,
    要连点几次才可靠命中.

    实测坐标+点击机制本身都没问题(debug_death_click.py隔离测过, 点击成功) ——
    main.py主循环里失败, 是因为on_death_screen()一测到颜色达标就立刻点, 而
    死亡画面很可能还在渐入动画里, 颜色刚过检测阈值那一瞬间按钮还没真正可交互.
    隔离测试之所以每次都成功, 是因为脚本给了5秒倒计时, 画面早就稳定了才点 ——
    人手速也从没快到能踩中这个窗口, 所以感觉不到"冷却", 但紧循环里的脚本能.
    加一点等待, 让画面先稳定下来.
    """
    time.sleep(0.5)
    _click_button_until_gone(_CONTINUE_BUTTON_POS, on_death_screen, "继续")


# 点"开始"/"继续"这类确认按钮: 单发盲点经常不生效 —— 第一下click()常常只把浏览器
# 窗口/标签页抢到前台, 点击事件没真进游戏画布; 加上按钮淡入动画、页面还在加载,
# 颜色刚过检测阈值那一刻按钮还不可交互. 以前start路径点一次就往下走, 没中的话这轮
# 啥也没刷: lazy_theta_pathing()对着菜单立刻返回, round_elapsed极小, 主循环把它当
# "没刷满5分钟"给consecutive_short_rounds+1, 连着两次误触发switch_server() —— 可
# 服务器根本没问题, 是自己没点进去. 改成点完回采一次屏幕确认目标画面真的消失了,
# 没消失就再点, 试满上限还在就返回False交主循环下轮重试(跟本项目"卡住不放弃"
# 的无限重试风格一致, 不在这里硬卡死也不抛异常).
_CONFIRM_CLICK_MAX_ATTEMPTS = 10
_CONFIRM_CLICK_SETTLE_SECONDS = 1.5


def _click_button_until_gone(button_pos, still_showing, label):
    """鼠标移到button_pos连点两下(第一下常只抢焦点), 等画面稳定, 再用still_showing()
    复查是不是真离开了该画面. 没离开就重试, 最多_CONFIRM_CLICK_MAX_ATTEMPTS次.
    返回True=确认已离开, False=试满还在."""
    for attempt in range(1, _CONFIRM_CLICK_MAX_ATTEMPTS + 1):
        pyautogui.moveTo(button_pos)
        time.sleep(0.2)
        pyautogui.click()
        time.sleep(0.1)
        pyautogui.click()
        time.sleep(_CONFIRM_CLICK_SETTLE_SECONDS)
        if not still_showing():
            return True
        print(f"⚠️ 点[{label}]第{attempt}次没生效, 画面还在, 重试...")
    print(f"❌ 连点{_CONFIRM_CLICK_MAX_ATTEMPTS}次[{label}]都没进去, 本轮先放过, 下轮再试")
    return False


def click_start_game():
    """确认开局菜单, 真正进入游戏. 连点两下 + 复查on_start_screen()确认菜单消失,
    没消失就重试(理由见_click_button_until_gone上面的注释)."""
    return _click_button_until_gone(_START_BUTTON_POS, on_start_screen, "开始")


def click_play_as_guest():
    """点「以游客身份游玩」, 让 florr 从未登录的登录选择页进到正常标题页.
    连点两下 + 复查 on_guest_screen() 确认这一页消失, 没消失就重试(理由见
    _click_button_until_gone 上面的注释). 之后由 on_start_screen() /
    click_start_game() 接手真正进游戏.
    """
    return _click_button_until_gone(_PLAY_AS_GUEST_POS, on_guest_screen, "以游客身份游玩")


# florr 标题页(绿色"开始"按钮那屏)下方的生态区选择器 —— 一小格按钮:
#     Garden   Desert   Ocean
#        Jungle   Hel
# 选中的那个有浅色高亮. florr 不记忆上次选的, 每次(重新)开都默认 Garden ——
# 不点一下目标生态区, click_start_game() 就进花园, 跟寻路用的地图对不上.
# CDP cp6.forceServerID 那条路试过反复不行(切别的生态区会掉回花园 / 被踢回标题
# 页), 只有像素点这个 canvas 按钮管用 —— 跟"开始"/"继续"/"以游客身份游玩"同一类
# (canvas 标题页按钮吃 pyautogui 合成点击; 游戏内控件才不吃).
#
# 坐标: 实机 debug_screen_pos.py 量的, 1920x1080 全屏(跟 _START_BUTTON_POS 等一致).
# florr 改标题页布局 / 换了分辨率没对上 → 点空 → 照旧进花园(自愈, 不卡死),
# 重新跑 debug_screen_pos.py 量 Desert 格中心, 换算到 1920x1080 基准
# (ref_x = 实测x * 1920 / 屏宽) 填这里. 键 = server_lookup 的生态区 key
# (main._apply_worker_config 传进来的 w["biome"]). 现在只有 desert 量过;
# ocean GUI 禁用着没去量, garden 待标定(见下面表里那条注释) —— 蚁穴走 garden
# 进场, 所以蚁穴时块传进来的是 "garden" 而不是 "anthell", 见 map_routes.py.
_BIOME_BUTTON_POS = {
    "desert": scale_point(977, 596),
    # 2026-09-15 实机标定(1920x1080, debug_screen_pos.py 悬停花园格)。蚁穴走花园
    # 进场(见 map_routes.py), 所以蚁穴时块拿到的 biome 就是 "garden"。
    # 跟 desert 同一行、在它左边 116px, y 基本齐平(599 vs 596) —— 跟标题页那个
    # 「花园/沙漠/海洋 换行 丛林/冥界」的格子网格对得上, 可以互相印证。
    "garden": scale_point(861, 599),
}


def select_biome_on_title(biome):
    """在标题页生态区选择器里点一下目标生态区, 保证点"开始"后进这个生态区.
    biome 没有对应坐标(ocean 暂未量, 或未知值)→ 直接返回不点.
    ("anthell" 不会被传进来: 蚁穴走花园进场, 时块拿到的 biome 就是 "garden",
     见 map_routes.py。)
    已经选中目标生态区时再点一下也无害(幂等). 沿用确认类按钮的"先点一下抢焦点、
    再点一下真命中"套路, 但这个选择器点完不会消失、没有"画面已离开"的复查信号,
    所以不走 _click_button_until_gone, 就连点两下.
    """
    pos = _BIOME_BUTTON_POS.get(biome)
    if pos is None:
        return
    pyautogui.moveTo(pos)
    time.sleep(0.2)
    pyautogui.click()
    time.sleep(0.1)
    pyautogui.click()


def check_stage():
    full_screen = [0, 0, SCREEN_WIDTH, SCREEN_HEIGHT]
    color = pyautogui.screenshot(region=full_screen).getpixel(scale_point(316, 32))
    if color == (187, 85, 85):
        return "in_game"
    elif color == (255, 255, 255):
        return "in_game_dead"
    else:
        color = pyautogui.screenshot(region=full_screen).getpixel(scale_point(156, 35))
        if color == (155, 181, 107):
            return "in_menu"
        else:
            return "unknown"


def minimap_capture_region():
    """算小地图该截屏幕哪块区域. 不能直接套scale_region()的"宽高各自独立缩放" ——
    2026-08-26实机(1024x768)调试确认过, florr.io小地图控件不是那样缩放的: 用户拿
    debug_screen_pos.py量出真实外框≈左上(797,20)~右下(1009,227), 跟
    scale_region(1600,20,300,300)算出来的[853,14,160,214]对不上(偏窄, 外边框
    整个漏在截图外, 拿debug_position_diag.py对着截图跑f8de60颜色匹配, 命中
    像素数是0, 实锤截歪了)。

    实测数据吻合的是: 控件整体按SCREEN_HEIGHT/1080统一缩放(不分宽高轴), 保持
    正方形, 贴着屏幕右上角, 右边距/上边距也按同一个比例缩放 —— 右边距、下边界
    跟实测几乎分毫不差, 左边界也基本对上. 1920x1080参照分辨率下这套公式退化成
    跟原来完全一样的[1600,20,300,300], 不影响已验证过的16:9场景(1920x1080/
    2560x1440/3840x2160这类等比分辨率, scale_x==scale_y==这里的scale, 结果
    等价于按老公式算)。

    单独拆成一个函数(不是内联在get_map()里), 是因为debug_position_diag.py这类
    诊断脚本也需要打印"理论截图区域该是多少"来跟实机现象对比 —— 之前诊断脚本里
    自己写了一份`scale_region(1600,20,300,300)`, get_map()的算法改了以后诊断脚本
    那份没跟着改, 打印出来的区域是假的, 容易把人绕晕(实际问题跟这处不一致时看着
    像还是没修好). 只留一份实现, 两边一起用, 不会再分叉.
    """
    scale = SCREEN_HEIGHT / _REF_HEIGHT
    size = round(300 * scale)
    right_margin = round(20 * scale)
    top_margin = round(20 * scale)
    right = SCREEN_WIDTH - right_margin
    left = right - size
    top = top_margin
    return [left, top, size, size]


def get_map():
    region = minimap_capture_region()
    image = pyautogui.screenshot(region=region)
    image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    if image.shape[:2] != (300, 300):
        # maps/anthell.png、maps/desert.png、maps/ocean.png都是固定300x300模板,
        # 寻路/玩家定位那一整条链路(get_player_location_on_map、calibrate_player、
        # lazy_theta_star)全部假设坐标就活在这个300x300像素空间里 —— 分辨率一变,
        # scale_region()算出来的截图区域尺寸就不再是300x300了(比如4K下大概是
        # 600x600), 截完必须resize回300x300, 不然玩家位置检测/寻路全错位.
        image = cv2.resize(image, (300, 300), interpolation=cv2.INTER_AREA)
    return image


# 小地图上的传送点画成一个亮绿色圆点。实测色 HSV (59,135,228), 灰度只有 179 ——
# 落在 preprocess_map 的 200 阈值下面, 所以默认会被二值化成墙。
# V 下限 160 把暗背景挡在外面(花园背景 V=24); 实测整张 300x300 花园图只命中 7 个
# 圆斑共 183 像素, 背景一个都没误伤。
_PORTAL_GREEN_HSV_LO = np.array([45, 70, 160])
_PORTAL_GREEN_HSV_HI = np.array([75, 200, 255])


def _open_portal_blob(binary, hsv, point):
    """把 point 所在的那**一个**传送点绿斑改成可走, 其余绿斑一律不动。

    为什么只开一个: 花园里这样的绿点有 7 个, 只有一个是蚁穴入口(2026-09-15 用户
    确认)。其余 6 个通向别处 —— 它们留在"墙"的状态才是对的: 寻路会自动绕开, bot
    就不会在去蚁穴的路上踩中别的传送点被拽走。
    为什么这一个非开不可: 寻路的目标点要是墙, lazy_theta_star 直接规划失败, 报的
    还是笼统的"路径规划失败", 根本看不出是地图的问题, bot 会一轮轮空转到超时。

    point 不在任何绿斑上 -> 原样返回(不猜、不就近吸附), 由调用方的回归用例去发现。
    """
    x, y = point
    mask = cv2.inRange(hsv, _PORTAL_GREEN_HSV_LO, _PORTAL_GREEN_HSV_HI)
    count, labels = cv2.connectedComponents((mask > 0).astype(np.uint8), connectivity=8)
    label = labels[y, x]
    if label == 0:          # 0 = 背景, 即这个点压根不是绿斑
        return binary
    binary[labels == label] = 255
    return binary


def _open_shortcut_rects(binary, rects):
    """把 rects(每个 (x0,y0,x1,y1), 含边界)里的像素强制标记成可走。

    跟 _open_portal_blob 那种"点名一个连通斑, 整块开放"不一样: 密道
    (2025-12-15 加入游戏)在静态截图里就是一段普通墙 —— 玩家靠近才会显形,
    截图截不到这个动态效果, preprocess_map 二值化时跟真墙没有任何区别, 也
    没有像传送点那样独立成一个小连通块可以整体打通(密道所在的墙往往跟外墙
    连成一大片, 对着连通块开的话会把一整片不该开的墙一起打穿)。所以密道用
    的是精确到标定范围的矩形硬写, 不看连通性、不看颜色, 只信标定出来的坐标。
    """
    for x0, y0, x1, y1 in rects:
        binary[y0:y1 + 1, x0:x1 + 1] = 255
    return binary


def preprocess_map(image, out_path=None, threshold=200, open_portal_at=None,
                   open_shortcuts_at=None):
    """把小地图截图二值化成寻路用的图: 亮像素 = 可走(255), 其余 = 墙(0), 另外把
    黄色(小地图上的路/高亮)也算成可走。

    out_path 给了就顺便写盘(capture_map.py 用它生成 maps/<name>.png), 不给就只
    返回 —— 改之前这里硬编码 cv2.imwrite('./maps/anthell.png', binary), 谁调它
    都会悄悄把蚁穴地图覆盖掉。
    threshold: 二值化阈值。不同生态区的小地图明暗对比不一样, 生成出来墙和地板
    分不开时调它(capture_map.py --threshold)。
    open_portal_at: (x, y) —— 只把这个点所在的那一个传送点绿斑打通成可走, 别的
    绿斑照旧当墙(理由见 _open_portal_blob)。花园图要传 map_routes.ANTHELL_PORTAL;
    别的图不用传。
    open_shortcuts_at: 密道矩形范围列表, 每个 (x0,y0,x1,y1) —— 见 map_routes 里
    以 _SHORTCUTS 结尾的常量(比如 map_routes.GARDEN_SHORTCUTS)。capture_map.py
    按地图名自动查表传进来, 调这个函数的人一般不用自己传.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_yellow = np.array([20, 100, 100])
    upper_yellow = np.array([30, 255, 255])
    yellow_mask = cv2.inRange(hsv, lower_yellow, upper_yellow)
    binary[yellow_mask > 0] = 255
    # 传送点一律先按墙处理, **不让亮度阈值说了算**。它的灰度是 179: 默认阈值 200
    # 下面它本来就是墙, 可是哪天有人为了把细走廊接上而把 threshold 调到 179 以下
    # (蚁穴就需要), 三个传送点会跟着悄悄变成可走, bot 刷怪时踩上去就被传走 ——
    # 而且一点提示都没有。要不要放行只能由 open_portal_at 点名, 跟阈值无关。
    binary[cv2.inRange(hsv, _PORTAL_GREEN_HSV_LO, _PORTAL_GREEN_HSV_HI) > 0] = 0
    if open_portal_at is not None:
        binary = _open_portal_blob(binary, hsv, open_portal_at)
    if open_shortcuts_at:
        binary = _open_shortcut_rects(binary, open_shortcuts_at)
    if out_path is not None:
        cv2.imwrite(out_path, binary)
    return binary


def _ensure_grayscale_2d(img):
    """cv2.imread(path, cv2.IMREAD_GRAYSCALE)理论上强制转成纯(H,W), 但实测在
    Windows上撞到过它吐出带一条多余尾随通道维的(H,W,1)(Mac上没复现, 是OpenCV/
    平台组合的已知行为差异). 这个二义性会让所有假设'map是2D数组'的下游代码
    (calibrate_player的rows,cols=map.shape、lazy_theta_star的map[y][x]、
    random_walkable_point的binary_map[y,x]等)全部遭殃 —— 与其挨个打补丁, 不如
    在唯一的加载入口把形状锁死."""
    if img is not None and img.ndim == 3:
        img = img[:, :, 0]
    return img


def load_binary_map():
    return _ensure_grayscale_2d(cv2.imread(f'./maps/{MAP}.png', cv2.IMREAD_GRAYSCALE))


# radius>2这个阈值是照1920x1080实测调出来的(那分辨率下get_map()截图区域正好
# 是300x300, 不需要放大, 标记原始像素footprint最大最清晰). 2026-08-26实机
# (1024x768)调试确认过: 非参照分辨率下get_map()截到的原始区域比300x300小
# (这个例子里约209x212), resize放大回300x300时标记的像素footprint会跟着
# 缩水, 真实玩家标记量出来radius=1.90(debug_position_diag.py截到的失败瞬间
# 现场, 图里肉眼确认过是真标记, 不是噪声), 卡在>2这条线下面被当噪声滤掉 ——
# 帧与帧之间因为抗锯齿有±零点几像素抖动, 导致"有时候检测得到有时候检测不到"。
# 下调到>1, 给这种非参照分辨率下缩水的标记留出余量; 真正的噪声(孤立1像素)
# minEnclosingCircle算出来radius≈0.5, 仍然会被这条线滤掉.
#
# 2026-09-16 复盘: debug_position_diag.py 当年靠这个阈值改动出的诊断数据(上面
# 那段radius=1.90的现场), 但脚本自己把判定条件硬编码抄了一份`radius > 2`,
# 阈值改了脚本没跟着改——跟 minimap_capture_region() 拆出来之前踩的是同一个坑
# (见那个函数的 docstring)。以后诊断脚本一律 import 这个常量, 不再各写一份。
PLAYER_MARKER_MIN_RADIUS = 1

# 玩家标记颜色的逐通道容差(BGR各通道独立±这么多都算数)。
# 2026-09-16实机复盘(用户反馈花园里测不到位置, 拿 debug_position_diag.py 的全屏
# 红框工具排除了"截图区域没对准"): 两张独立的花园实机截图(隔一天拍的, 一张是
# calibration用的debug_garden_raw.png, 一张是这次复盘现截的)逐像素量出来的玩家
# 标记真实颜色, R通道都往下偏了一截——分别要 ±27 / ±22 才能匹配上, 原来的 ±20
# 卡线之外, 一个像素都测不到。两次独立测量方向一致(不是某一帧的随机抖动),
# 说明花园这张图上标记的渲染色本身就比标定用的参照色暗一点, 不是分辨率/噪声
# 问题(那个已经在 PLAYER_MARKER_MIN_RADIUS 那条修过, 是另一件事)。
# 选 ±30: 覆盖两次实测的最小需要值(27/22)且留出余量; 拿这两张花园图 + 蚁穴的
# debug_anthell_raw.png(调门户坐标时截的, 颜色跟花园完全不同的一张地图)在
# ±20~±35 区间反复测过误伤, 三张图全程只匹配到玩家标记自己那一个连通块,
# 没有多出来的假阳性.
PLAYER_MARKER_TOLERANCE = 30


def get_player_location_on_map(opencv_img, target_color, map, precise=False):
    target_color_bgr = tuple(int(target_color[i:i+2], 16) for i in (4, 2, 0))
    t = PLAYER_MARKER_TOLERANCE
    lower_bound = np.array([max(0, target_color_bgr[0] - t), max(0,
                           target_color_bgr[1] - t), max(0, target_color_bgr[2] - t)])
    upper_bound = np.array([min(255, target_color_bgr[0] + t), min(255,
                           target_color_bgr[1] + t), min(255, target_color_bgr[2] + t)])

    mask = cv2.inRange(opencv_img, lower_bound, upper_bound)

    contours, _ = cv2.findContours(
        mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    for contour in contours:
        ((x, y), radius) = cv2.minEnclosingCircle(contour)
        if radius > PLAYER_MARKER_MIN_RADIUS:
            if precise:
                position = (x, y)
                return position
            else:
                position = (round(x), round(y))
                return calibrate_player(map, position)
    return None


def calibrate_player(map, player_position):
    """把检测到的原始像素吸附到最近的可走点——但只在**主连通区域**里找, 不吸附到
    孤立的小碎块上.

    实机复盘(2026-09-16, 蚁穴): 玩家路过一个传送点(门户强制当墙, 见
    map_routes._ANTHELL_PORTAL 那类), 检测到站在(121,98) —— 可走像素没错,
    但这一像素恰好落在把门户挖成墙时顺手切下来的一撮31像素孤岛上, 跟主可走区域
    (30168像素)完全不连通(4/8连通两种算法都验证过, 8连通是 lazy_theta_star 自己
    的邻居定义). 旧逻辑不管这些, 无差别在全图找最近可走点, 吸附到这撮孤岛上;
    lazy_theta_star 从孤岛出发, 地图上真的没有路能到主区域, 秒回 None ——
    "路径规划失败"不是寻路算法的 bug, 是标定阶段把玩家钉在了一个哪儿都去不了
    的地方.

    这类孤岛不只蚁穴有(anthell.png 实测还有另外3撮各1~2像素的), 也不只因为传送点
    ——任何生成地图时留下的抗锯齿残渣都是同一个坑. 通用修法: 先按连通性把可走
    像素分组, 只在**最大的那一组**里找最近点, 小碎块一律当"跟没有可走像素一样"
    忽略掉.
    """
    walkable = (map == 255).astype(np.uint8)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(walkable, connectivity=8)
    if num_labels <= 1:
        # 整张图一个可走像素都没有(地图没生成好/读错了文件) —— 没有"主区域"可言,
        # 原样吃老行为, 让上游(lazy_theta_star 规划失败/get_player_position 继续
        # 重试)去处理这种彻底异常的情况, 不在这里硬造一个假坐标出来.
        return player_position
    main_label = max(range(1, num_labels), key=lambda i: stats[i, cv2.CC_STAT_AREA])

    rows, cols = map.shape
    min_distance = float('inf')
    nearest_walkable_position = player_position
    for y in range(rows):
        for x in range(cols):
            if labels[y, x] == main_label:
                distance = math.sqrt(
                    (x - player_position[0])**2 + (y - player_position[1])**2)
                if distance < min_distance:
                    min_distance = distance
                    nearest_walkable_position = (x, y)
    return nearest_walkable_position