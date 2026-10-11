"""把"人这一刻按了什么"换算成策略的动作 —— 纯函数, 不碰 CDP。

动作空间(设计里定的): (移动 9 向含不动, 攻击, 防御, 按 R)。

两个麻烦事在这里处理:

1. **人可能用鼠标走, 也可能用 WASD 走。** florr 两种都支持, bot 自己用的是鼠标。
   录人类数据时不能假设人用哪种 —— 所以两边都收: 有按方向键就用按键, 没有就看鼠标
   相对玩家屏幕锚点的方向。换算出来的都是同一个 9 向下标, 下游分不出来也不需要分。

2. **按住不放时 keydown 会连发。** 所以不能数事件个数, 得重建"这一刻哪些键是按下的"
   这个状态, 跨 tick 维护。
"""
import math

# 9 向: 0 不动, 1..8 从正北开始顺时针。跟 florragent 的 MOVEMENT_KEYS 同序, 免得
# 以后接它的 checkpoint 时还要再转一道。
MOVE_NONE = 0
MOVE_KEYS = (
    (),                 # 0 不动
    ("w",),             # 1 北
    ("w", "d"),         # 2 东北
    ("d",),             # 3 东
    ("s", "d"),         # 4 东南
    ("s",),             # 5 南
    ("s", "a"),         # 6 西南
    ("a",),             # 7 西
    ("w", "a"),         # 8 西北
)
_MOVE_INDEX_BY_KEYS = {frozenset(k): i for i, k in enumerate(MOVE_KEYS)}

DIRECTION_KEYS = frozenset("wasd")
ATTACK_KEY = "space"
DEFENSE_KEY = "shift"
SWAP_KEY = "r"

# 鼠标离玩家锚点不到这么多像素, 当成"没有明确方向" —— florr 里光标压在身上时花朵基本
# 不动, 硬解出一个方向等于给策略喂噪声。
MOUSE_DEADZONE_PX = 12.0


def normalize_key(key, code=""):
    """浏览器的 key/code 归一成本模块用的小写名。

    key 在不同布局/输入法下不可靠(比如中文输入法下可能是别的值), code 是物理键位,
    更稳 —— 所以优先 code。
    """
    code = str(code or "")
    if code.startswith("Key") and len(code) == 4:
        return code[3].lower()
    if code == "Space":
        return ATTACK_KEY
    if code in ("ShiftLeft", "ShiftRight"):
        return DEFENSE_KEY
    k = str(key or "").lower()
    if k == " ":
        return ATTACK_KEY
    return k


def apply_events(held, events):
    """把一批 keydown/keyup 事件叠加到"当前按住的键"集合上, 返回新集合。

    held 是上一 tick 结束时的状态 —— 按住不放跨 tick 时, 这一 tick 可能一个事件都没有,
    但键仍然是按着的。只数事件个数会把这种情况判成"松开了"。

    repeat 的 keydown 不改变状态(本来就已经按着), 但也无害。
    """
    out = set(held)
    for e in events:
        name = normalize_key(e.get("key"), e.get("code"))
        if not name:
            continue
        if e.get("type") == "down":
            out.add(name)
        elif e.get("type") == "up":
            out.discard(name)
    return out


def move_index_from_keys(held):
    """按住的方向键 -> 9 向下标。对角线认, 互相抵消(同时按 a 和 d)当不动。"""
    dirs = frozenset(k for k in held if k in DIRECTION_KEYS)
    if "w" in dirs and "s" in dirs:
        dirs = dirs - {"w", "s"}
    if "a" in dirs and "d" in dirs:
        dirs = dirs - {"a", "d"}
    return _MOVE_INDEX_BY_KEYS.get(frozenset(dirs), MOVE_NONE)


def move_index_from_mouse(mouse, player_screen, deadzone=MOUSE_DEADZONE_PX):
    """鼠标位置 -> 9 向下标(相对玩家在画面上的锚点)。

    没有鼠标记录 / 缺玩家锚点 / 在死区内 -> 0(不动)。
    y 轴向下增长, 所以正北是 dy 为负。
    """
    if not mouse or not player_screen:
        return MOVE_NONE
    dx = mouse["x"] - player_screen[0]
    dy = mouse["y"] - player_screen[1]
    if math.hypot(dx, dy) < deadzone:
        return MOVE_NONE
    # 正北 = 0 度, 顺时针。atan2(dx, -dy) 正好给出这个。
    angle = math.degrees(math.atan2(dx, -dy)) % 360
    return int(round(angle / 45)) % 8 + 1


def action_from_state(held, mouse, player_screen, *, swap_presses=0,
                      deadzone=MOUSE_DEADZONE_PX):
    """一个 tick 的完整动作。

    swap_presses: 这一 tick 里 R 被**按下**了几次(按住不放不算多次, 调用方数 down 事件
    且排除 repeat)。动作维只有 0/1 —— 一个 tick 内连按两次 R 在 10Hz 下不可能是有意的。

    移动优先看按键: 人一旦用 WASD, 鼠标可能还停在别处, 那不是他这一刻的意图。
    """
    move = move_index_from_keys(held)
    if move == MOVE_NONE:
        move = move_index_from_mouse(mouse, player_screen, deadzone)
    return {
        "move": move,
        "attack": 1 if ATTACK_KEY in held else 0,
        "defense": 1 if DEFENSE_KEY in held else 0,
        "swap": 1 if swap_presses > 0 else 0,
    }


def count_swap_presses(events):
    """这批事件里 R 被真正按下了几次(repeat 不算)。"""
    n = 0
    for e in events:
        if e.get("type") != "down" or e.get("repeat"):
            continue
        if normalize_key(e.get("key"), e.get("code")) == SWAP_KEY:
            n += 1
    return n
