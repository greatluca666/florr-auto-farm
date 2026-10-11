"""真服后端 —— 环境跟真实 florr 之间唯一的一层。

rl_env.FlorrEnv 只认三个方法: reset() / send_action(a) / next_state()。这里把它们
接到真游戏上:

    读  cdp_bridge 拉 canvas 日志 -> rl_scene 解出自己/怪/其他玩家
        florr_wasm_probe.readChanges() 拿这一拍新增的击杀和掉落
    写  pyautogui 发按键(9 向 WASD + 空格攻击 + Shift 防御 + 点一下 R 换装)

## 三件在这里而不在别处做的事

**必须关掉「反转攻击键」。** bot 脚本靠它让花朵持续攻击, 自己一下攻击键都不按。可是
策略的攻击维要真的能控制攻击 —— 反转开着的话按不按都在打, 那一维就是个死维, 学不到
任何东西。防御同理。setup() 里强制关掉这两个字节。

**键是"按住"的状态, 不是"按一下"的事件。** 动作里的移动/攻击/防御是这一拍要保持的
状态, 所以发键前要跟上一拍比对: 先松掉不要的, 再按下新要的。顺序反了会出现一瞬间的
斜向(比如从「北」切到「东」时短暂变成「东北」)。

**掉线/掉基址/窗口失焦都不能当成正常样本。** 三种都会让"策略按了什么"和"世界发生了
什么"对不上:
  - CDP 断了 / 探针没就绪 -> next_state() 回 None, 这一段 rollout 到此为止
  - 击杀表基址漂了(hiNonZero) -> 同上。宁可断掉, 也不能拿错位的计数编出假奖励
  - 游戏窗口没焦点 -> control="unfocused", 环境截断。pyautogui 发的键进了别的窗口,
    这一拍的动作根本没落到游戏里

## 实机验证还没做

这个模块只能在装着 florr 的那台 Windows 上验。纯逻辑部分(键集合换算、事件聚合、节拍、
各种中断)都有测试; "按键真的让花朵动了"没有。
"""
import json
import math
import time

import cdp_bridge
import florr_settings
import rl_scene
from rl_actions import ATTACK_KEY, DEFENSE_KEY, MOVE_KEYS, SWAP_KEY

# florr_wasm_probe.js 里 MOB_RARITY_NAMES / PETAL_RARITY_NAMES 的顺序 —— 这两张表
# 必须跟那边一致, 否则奖励的稀有度权重整体错档。
MOB_RARITY_RANK = {"普通": 0, "罕见": 1, "稀有": 2, "史诗": 3,
                   "传说": 4, "神话": 5, "究极": 6, "超神": 7}
PETAL_RARITY_RANK = dict(MOB_RARITY_RANK, 独特=8)

_FOCUS_JS = "document.hasFocus()"

# 一拍一次 CDP 往返, 回三样东西: 焦点、计数器增量、**一整帧**绘制记录。
#
# 为什么帧要在页面里挑好: 实机测出来 drain_canvas_log() 中位 185ms, 而同样走一次
# 握手的小 evaluate 只要 42ms —— 差的那 143ms 是载荷。canvas_hook 的页面缓冲留 5 帧
# (FRAME_RETENTION), 一次 drain 拖回 1478 条记录(31 只怪那一屏), 可我们每拍只用
# 其中一帧。在页面里先挑出次新的那一帧再回传; 跟计数器/焦点合并又省掉一次握手。
#
# slim(): 再把每条记录瘦一遍。实拍帧 108KB/451 条里, 光浮点数就占七成 —— m 矩阵 34KB、
# bbox 16KB、x/y 18KB, 全是十几位有效数字。canvas_decode 从没用过 n/lw/alpha(grep 为 0),
# 直接不发; 其余像素量留 4 位小数, **CTM 的缩放分量留 9 位有效数字**。
#
# 那个"有效数字"不是讲究, 是实测逼出来的。自身的绝对世界坐标是从小地图那个点反解的:
#     player_world = ((x - m[4]) / m[0], (y - m[5]) / m[3])
# 而小地图的 m[0] ≈ 0.0084 —— 除以它等于**乘 119**。按小数位留 4 位, 0.0084 只剩两位
# 有效数字, 0.15% 的相对误差落到 12000 量级的坐标上就是 18 个世界单位。
#
# 这不是"差不多就行": rl_reward 逐帧认怪只容许 MATCH_RADIUS(40 单位)的位移, 坐标
# 逐帧抖这么多会让挂账认不出是同一只怪, 打掉的血永远结算不了 —— 而且一声不响。
# 按有效数字给就跟量级无关了: 0.0084 和 798.016 各留 9 位有效数字, 误差都可忽略。
#
# 挑**次新**的那一帧而不是最新的: 最新那帧还在画一半, 用它会解出半张场景。
# 挑完把这一帧及更早的从页面缓冲里删掉 —— 下一拍不会再看到它。
# 整段是同步执行的, 中间不会有新的 draw call 插进来(JS 单线程), 所以读和清之间没有竞态。
_TICK_JS_TEMPLATE = """(() => {
  var log = window.__canvasLog || [];
  var recs = [], target = -1;
  if (log.length) {
    var newest = log[log.length - 1].frame;
    for (var i = log.length - 1; i >= 0; i--) {
      if (log[i].frame < newest) { target = log[i].frame; break; }
    }
    if (target >= 0) {
      var keep = [];
      for (var j = 0; j < log.length; j++) {
        if (log[j].frame === target) recs.push(slim(log[j]));
        else if (log[j].frame > target) keep.push(log[j]);
      }
      window.__canvasLog = keep;
    }
  }
  function r4(v) { return Math.round(v * 1e4) / 1e4; }
  function rp(v) { return v ? Number(v.toPrecision(9)) : v; }
  function slim(o) {
    var out = {frame: o.frame, op: o.op, x: r4(o.x), y: r4(o.y), r: o.r,
               fill: o.fill, stroke: o.stroke};
    if (o.bbox) out.bbox = [r4(o.bbox[0]), r4(o.bbox[1]), r4(o.bbox[2]), r4(o.bbox[3])];
    if (o.m) out.m = [rp(o.m[0]), rp(o.m[1]), rp(o.m[2]), rp(o.m[3]), r4(o.m[4]), r4(o.m[5])];
    if (o.text !== undefined) out.text = o.text;
    return out;
  }
  return {f: document.hasFocus(), c: %s, r: recs};
})()"""
_NO_COUNTERS = "null"
_READ_CHANGES = "window.__florrWasmProbe.readChanges()"


# ---------------------------------------------------------------- 动作 -> 按键
def keys_for_action(action, *, with_move=True):
    """(这一拍要按住的键集合, 要点一下的键列表)。

    移动/攻击/防御是持续状态, R 是一次性的 —— 按住 R 不会连续换装, 只会换一次。

    with_move=False: 移动交给鼠标那条通道(默认), 这里只管攻击/防御/换装。
    """
    move, attack, defense, swap = action
    held = set(MOVE_KEYS[move]) if with_move else set()
    if attack:
        held.add(ATTACK_KEY)
    if defense:
        held.add(DEFENSE_KEY)
    return held, ([SWAP_KEY] if swap else [])


class KeySender:
    """维护"现在按住了哪些键", 每拍只发差量。

    key_down/key_up/press 是注入点 —— 测试里换成记录器, 就不用真发按键, 也不用装
    pyautogui(那东西在无头机器上 import 都会炸)。
    """

    def __init__(self, *, key_down=None, key_up=None, press=None, with_move=True):
        self.with_move = with_move
        if key_down is None or key_up is None or press is None:
            import pyautogui       # 延迟导入: 纯逻辑部分在没有显示器的机器上也要能用
            key_down = key_down or without_pause(pyautogui.keyDown, pyautogui)
            key_up = key_up or without_pause(pyautogui.keyUp, pyautogui)
            press = press or without_pause(pyautogui.press, pyautogui)
        self._down, self._up, self._press = key_down, key_up, press
        self.held = set()

    def apply(self, action):
        """把按键状态切到 action 要的样子。返回 (按下的, 松开的, 点了的), 给测试和日志。"""
        want, taps = keys_for_action(action, with_move=self.with_move)
        # 先松后按: 反过来的话, 从「北」切到「东」会有一瞬间 w 和 d 同时按着 = 东北。
        release = self.held - want
        for k in sorted(release):
            self._up(k)
        pressed = want - self.held
        for k in sorted(pressed):
            self._down(k)
        self.held = set(want)
        for k in taps:
            self._press(k)
        return pressed, release, list(taps)

    def release_all(self):
        """松开所有键。断线/死亡/脚本接管/退出时必须调 —— 留着一个 d 按着, 花朵会一直
        贴着墙跑到下一次有人管它为止。"""
        for k in sorted(self.held):
            self._up(k)
        self.held = set()


def without_pause(fn, module):
    """包一层, 让这次 pyautogui 调用不吃那 0.1 秒的全局 PAUSE。

    pyautogui.PAUSE 默认 0.1, 是**每次调用之后**都睡 —— 训练时一拍要 moveTo 一次,
    按键有变化时还要 keyDown/keyUp, 一拍光这个就 100~300ms。10Hz 根本无从谈起, 而
    CDP 那边省下来的十几毫秒会被它整个盖掉。

    **不直接改全局**: main.py 的寻路也在用同一个 pyautogui, 它的节奏(反卡死的时间窗、
    每 tick 的 moveTo)是按现在这个 PAUSE 调出来的。RL 接进 auto_farming 之后两边同进程,
    改全局等于顺手改了寻路的行为, 而那个没法在这台机器上验。所以只在自己这几次调用
    周围临时置零, 用完立刻还回去。
    """
    def wrapped(*a, **kw):
        prev = module.PAUSE
        module.PAUSE = 0
        try:
            return fn(*a, **kw)
        finally:
            module.PAUSE = prev
    return wrapped


# 9 向 -> 屏幕方向单位向量。跟 rl_actions.move_index_from_mouse 是一对逆运算:
# 那边 atan2(dx, -dy) 把光标偏移折成下标, 这边把下标折回偏移。正北 = 下标 1, 顺时针。
# (y 轴向下增长, 所以正北是 dy 为负。)
MOVE_UNIT = [(0.0, 0.0)] + [
    (math.sin(math.radians(i * 45)), -math.cos(math.radians(i * 45))) for i in range(8)
]


class MouseMover:
    """用鼠标发移动 —— florr 的花朵持续朝光标走, 这也是寻路脚本一直在用的通道。

    **为什么不用 WASD**, 两条独立的理由:

    1. florr 的「键盘移动」默认是**关**的(用户实机确认)。关着时 WASD 一点反应都没有,
       也不报错 —— 花朵原地不动, 策略却以为自己在走。走键盘就得先去改用户 florr 账号
       里的那个设置, 而那个地址还没标定。
    2. 就算打开了, 两条通道会打架: 脚本寻路全靠鼠标(main.py 里一次都没按过 wasd),
       交接给策略时光标停在它最后瞄的地方**没人动过**, 而花朵持续朝光标走。策略用
       键盘的每一步都在跟那个陈旧光标拔河, 同样不报错。

    走鼠标两个问题都没有, 而且用的是这台机器上已经验过能动的那条通道。

    "不动"就是把光标压在花朵身上 —— main.py 的 _drive_and_check_stall 一直是这么判
    "刻意停在这"的, 说明 florr 确实把光标贴身当成不移动。

    radius: 光标离花朵多远。florr 只看方向不看距离, 但太近会落进"停住"的判定里。
    margin: 离屏幕边缘至少留这么多像素。**这个不是讲究, 是必须的** —— pyautogui 的
            FAILSAFE_POINTS 就是屏幕四角 [(0,0),(0,h-1),(w-1,0),(w-1,h-1)], 光标停在
            任何一个角上, **下一次** pyautogui 调用就抛 FailSafeException。
            实机(2026-09-24)炸的是脚本的 click_continue_after_death, 整个 worker
            退出码 1 —— 光标是策略推到角上的, 抛的却是别人。
    """

    def __init__(self, *, radius=220.0, margin=4.0, move_to=None, screen_size=None,
                 center=None):
        if move_to is None:
            import pyautogui
            move_to = without_pause(pyautogui.moveTo, pyautogui)
            screen_size = screen_size or tuple(pyautogui.size())
        self._move_to = move_to
        self.screen_size = screen_size
        self.center = center or ((screen_size[0] / 2, screen_size[1] / 2)
                                 if screen_size else (960.0, 540.0))
        self.radius = radius
        self.margin = margin
        self.last_target = None

    def _clamp(self, x, y):
        """夹进屏幕, 但**离边缘留 margin** —— 夹到 0 或 w-1 就正好是 pyautogui 的
        失效保护点(屏幕四角), 下一次调用会抛 FailSafeException。"""
        if not self.screen_size:
            return x, y
        w, h = self.screen_size
        m = self.margin
        return (min(max(x, m), w - 1.0 - m), min(max(y, m), h - 1.0 - m))

    def apply(self, action, anchor=None):
        """把光标放到"从花朵出发、朝 action 那个方向"的位置。返回放哪了。

        anchor 是花朵在画面上的位置。解不出来(相机近似/那一帧没画自己)时退回屏幕
        中心 —— 实测这两者在地图边界能差上百像素, 所以能拿到真锚点就一定要用。
        """
        ax, ay = anchor if anchor else self.center
        ux, uy = MOVE_UNIT[action[0]]
        target = self._clamp(ax + ux * self.radius, ay + uy * self.radius)
        self.last_target = target
        self._move_to(target)
        return target

    def screen_corners(self):
        """给测试用: 这块屏幕上那几个会触发 pyautogui 失效保护的点。"""
        if not self.screen_size:
            return []
        w, h = self.screen_size
        return [(0, 0), (0, h - 1), (w - 1, 0), (w - 1, h - 1)]

    def park(self, anchor=None):
        """把光标压回花朵身上 = 停住。死亡/脚本接管/断线时必须调 —— 光标留在外面,
        花朵会自己一直朝那边走。"""
        return self.apply((0, 0, 0, 0), anchor)


# ---------------------------------------------------------------- 计数器增量
def aggregate_events(entries, rank_by_word):
    """readChanges 的明细 -> [(稀有度档位, 个数), ...], 同档合并。

    返回 (events, skipped)。skipped 是丢掉的条数:
      - delta <= 0: 计数器只会涨。掉下去说明换号/重连后整张表重建了, 那不是"负的击杀"
      - 稀有度词认不出: 客户端换了语言或探针的表过期了
    两种都不该悄悄算进奖励里。
    """
    totals = {}
    skipped = 0
    for e in entries or []:
        delta = e.get("delta") or 0
        rank = rank_by_word.get(e.get("rarity"))
        if delta <= 0 or rank is None:
            skipped += 1
            continue
        totals[rank] = totals.get(rank, 0) + delta
    return sorted(totals.items()), skipped


def ensure_keyboard_movement(eval_js=None):
    """打开 florr 的「键盘移动」。返回 (status, detail), 同 florr_settings.ensure_flag。

    florr 默认**关**着这个。关着时 WASD 一点反应都没有, 也不报错 —— 花朵就是不动,
    而策略会以为自己在走。只有 move_channel="keys" 才需要它。
    """
    eval_js = eval_js or cdp_bridge.eval_js
    return florr_settings.ensure_flag(eval_js, florr_settings.KEYBOARD_MOVE_ADDR, 1)


def ensure_manual_controls(eval_js=None):
    """把「反转攻击键」「反转防御键」都关掉, 让策略的攻击/防御维真的能控制花朵。

    返回 {"attack": (status, detail), "defense": (...)}。写不进去不抛 —— 调用方
    (setup) 负责决定是硬停还是只警告。
    """
    eval_js = eval_js or cdp_bridge.eval_js
    return {
        "attack": florr_settings.ensure_flag(eval_js, florr_settings.INVERT_ATTACK_ADDR, 0),
        "defense": florr_settings.ensure_flag(eval_js, florr_settings.INVERT_DEFENSE_ADDR, 0),
    }


def wait_for_focus(evaluate=None, *, timeout=120.0, poll=1.0,
                   sleep=time.sleep, clock=time.monotonic, announce=print):
    """挡在训练/诊断前面: 等游戏窗口拿到焦点再开始。返回拿到没有。

    pyautogui 的按键发给**当前焦点窗口**。从终端敲命令启动训练, 焦点就在终端上 ——
    策略发的每一个键都进了终端而不是游戏。环境会把这些拍标成 unfocused 全部截断,
    于是训练看起来在跑, 实际一步有效样本都没有。

    实机第一次诊断: 100 拍里 100 拍都是 unfocused。没有这道闸就是白跑一晚上。
    """
    evaluate = evaluate or eval_value
    if evaluate(_FOCUS_JS):
        return True
    announce("⏳ 游戏窗口没有焦点 —— 现在点一下 florr 那个窗口。"
             "\n   (按键是发给当前焦点窗口的; 焦点在终端上的话, 策略的动作一个都进不了游戏)")
    end = clock() + timeout
    while clock() < end:
        sleep(poll)
        if evaluate(_FOCUS_JS):
            announce("✅ 拿到焦点, 开始。")
            return True
    announce(f"❌ 等了 {timeout:.0f} 秒还是没有焦点。")
    return False


def eval_value(expression, timeout=15):
    """在页面里求值并把结果 JSON 解回来。跟 rl_record.val 同一套。"""
    resp = cdp_bridge.eval_js(f"JSON.stringify({expression})", timeout=timeout)
    inner = (resp or {}).get("result", {}).get("result", {})
    if "value" not in inner:
        raise RuntimeError(f"JS 没返回值: {inner!r}")
    raw = inner["value"]
    return None if raw is None else json.loads(raw)


# ---------------------------------------------------------------- 后端
class LiveBackend:
    """把真游戏包成 rl_env.FlorrEnv 要的后端协议。

    hz:            每秒几拍。10Hz = 每拍 100ms, 跟录制用的一致。
    counter_every: 每几拍读一次击杀/掉落计数。每拍一次是一个额外的 CDP 往返;
                   挂账超时是 5 秒, 所以攒几拍再结算不影响归因。
    control_hint:  外面的脚本用它告诉后端"现在不是策略在控"(拉回刷怪区/脱困/死亡
                   结算/菜单)。返回 "policy" 以外的任何值, 环境都会把这一段截断。
    require_focus: 游戏窗口没焦点时把这一拍标成 unfocused。pyautogui 的键是发给
                   **当前焦点窗口**的 —— 人一切走, 动作就全丢了, 那些拍不能进 rollout。
    dump_dir:      策略在控时没解出自己, 就把那一帧的绘制记录存到这里(json, 跟
                   test_frames/ 同格式), 供离线查是哪一层掉的。None = 不存。
                   同一个原因每 dump_every_s 秒最多一帧, 总共最多 dump_cap 帧。
    move_channel:  "mouse"(默认) 用鼠标发移动, 跟寻路脚本同一条通道; "keys" 用 WASD。
                   见 MouseMover 的说明: 两条通道同时活着时, 脚本留下的那个光标会一直
                   把花朵往旧位置拽, 而这事不报错。
    """

    def __init__(self, *, hz=10.0, counter_every=1, use_counters=True,
                 control_hint=None, require_focus=True, move_channel="mouse",
                 decode=None, sender=None, mover=None, evaluate=None,
                 clock=time.monotonic, sleep=time.sleep, reset_retries=10,
                 dump_dir=None, dump_every_s=60.0, dump_cap=30):
        self.period = 1.0 / hz
        self.counter_every = max(1, int(counter_every))
        self.use_counters = use_counters
        self.control_hint = control_hint or (lambda: "policy")
        self.require_focus = require_focus
        self.decode = decode or rl_scene.decode
        self.move_channel = move_channel
        by_mouse = move_channel == "mouse"
        self.sender = sender if sender is not None else KeySender(with_move=not by_mouse)
        if mover is not None:
            self.mover = mover
        else:
            self.mover = MouseMover() if by_mouse else None
        self._anchor = None
        # 默认走常驻连接: 实机量出来每次新建连接(HTTP GET /json 找标签页 + WebSocket
        # 握手)白花 17.2ms, 10Hz 下是一拍预算的六分之一。
        self._session = None if evaluate is not None else cdp_bridge.CdpSession()
        self.evaluate = evaluate or self._session.eval_value
        self.clock = clock
        self.sleep = sleep
        self.reset_retries = reset_retries
        self.last_error = None
        self._ticks = 0
        self._prev_t = None
        self._deadline = 0.0
        # 上一拍不是策略在控(AFK 弹窗 / 脚本把人拉回刷怪区) —— 这时再发动作是在跟
        # 脚本抢鼠标。键已经松了、光标已经压回花朵身上, 别再动它。
        self.paused = False
        self.dump_dir = dump_dir
        self.dump_every_s = dump_every_s
        self.dump_cap = dump_cap
        self._dumped = 0
        self._last_dump = {}

    # ------------------------------------------------------------ 一次性准备
    def setup(self, *, strict=True):
        """注入钩子和探针, 关掉两个反转开关。一次训练开始前调一次。

        strict=True 时反转开关写不进去就抛 —— 攻击维是死的这件事必须在训练开始前发现,
        不能等跑完几万步才回头看为什么策略从不攻击。
        """
        cdp_bridge.inject_canvas_hook()
        if self.use_counters:
            cdp_bridge.eval_js(_probe_source(), timeout=10)
            self.evaluate("window.__florrWasmProbe.scan()")
            self._wait_probe_ready()
        report = ensure_manual_controls()
        bad = [name for name, (status, _) in report.items() if status == "failed"]
        if bad and strict:
            raise RuntimeError(
                f"没能关掉反转开关 {bad}: {report} —— 反转开着时策略的攻击/防御维按不按"
                "都一样, 学不到任何东西")
        if self.move_channel == "keys":
            report["keyboard_move"] = ensure_keyboard_movement()
            if report["keyboard_move"][0] == "failed" and strict:
                raise RuntimeError(
                    f"没能打开「键盘移动」({report['keyboard_move'][1]}) —— florr 默认"
                    "关着这个, 关着时 WASD 一点反应都没有而且不报错, 花朵原地不动。"
                    "\n   要么在 florr 设置→控制里手动打开, 要么用 settings_finder.js "
                    "标定 florr_settings.KEYBOARD_MOVE_ADDR。"
                    "\n   (或者直接用默认的 move_channel=\"mouse\", 那条路不需要这个设置)")
        return report

    # 只取这两个字段, **不要**把整个探针对象拖回来: 它上面挂着花瓣/怪物目录, 而且
    # snapRange() 用过之后还挂着一个最多 200 万项的 Uint32Array —— JSON.stringify 那玩意
    # 会吐几十 MB。轮询一次拖一次, 完全没必要。
    _PROBE_STATUS_JS = ("(() => { var p = window.__florrWasmProbe; "
                        "return p ? {status: p.status, reason: p.reason} : null; })()")

    def _wait_probe_ready(self, timeout=90.0):
        end = self.clock() + timeout
        while self.clock() < end:
            st = self.evaluate(self._PROBE_STATUS_JS) or {}
            if st.get("status") == "ready":
                return
            if st.get("status") == "error":
                raise RuntimeError(f"WASM 探针出错: {st.get('reason')}")
            self.sleep(0.5)
        raise RuntimeError("WASM 探针扫描超时")

    # ------------------------------------------------------------ 后端协议
    def reset(self):
        self._stop_moving()
        self.last_error = None
        self.paused = False
        self._ticks = 0
        self._prev_t = None
        # **先**问仲裁器, **再**建计数器快照。仲裁器的节流检查可能正好落在这里, 在里面
        # 跑复活/拉回(几十秒, 脚本开着反转一路砍怪)。顺序反过来的话那些击杀全进下一拍
        # 的奖励 —— 实机(2026-09-24)一次复活白送 +70, 比死亡惩罚大得多, 等于奖励它去死。
        control = self.control_hint() or "policy"
        if self.use_counters:
            # 第一次 readChanges 只建快照。不丢掉的话, 这之前累积的全部历史会被当成
            # "刚刚发生的击杀"一次性发出去。
            self.evaluate(self._tick_js())
        scene = rl_scene.EMPTY
        for _ in range(self.reset_retries):
            _, _, recs = self._poll()
            scene = self.decode(recs)
            if scene.player_screen:
                self._anchor = scene.player_screen
            if scene.player is not None:
                break
            self.sleep(self.period)
        now = self.clock()
        self._prev_t = now
        self._deadline = now + self.period
        return self._state(scene, control=control, kill_events=[], drop_events=[],
                           t=now, dt=0.0)

    def send_action(self, action):
        if self.paused:
            return
        self.sender.apply(action)
        if self.mover is not None:
            self.mover.apply(action, self._anchor)

    def next_state(self):
        self._wait_for_tick()
        self._ticks += 1
        try:
            focus, changes, recs = self._poll()
        except Exception as e:
            return self._abort(f"cdp:{e}")

        kill_events, drop_events = [], []
        if changes is not None:
            if changes.get("error"):
                return self._abort(f"probe:{changes['error']}")
            if changes.get("hiNonZero"):
                # 击杀表基址漂了。继续跑会把错位的数字当成击杀发奖励 —— 那比没有奖励
                # 糟得多, 策略会追着一个跟游戏无关的信号学。
                return self._abort(f"counter-base-drift:hiNonZero={changes['hiNonZero']}")
            if not changes.get("first"):
                kill_events, k_skip = aggregate_events(changes.get("kills"), MOB_RARITY_RANK)
                drop_events, d_skip = aggregate_events(changes.get("drops"), PETAL_RARITY_RANK)
                if k_skip or d_skip:
                    self.last_error = f"skipped-events:kills={k_skip},drops={d_skip}"

        scene = self.decode(recs)
        if scene.player_screen:
            self._anchor = scene.player_screen
        control = self.control_hint() or "policy"
        if control == "policy" and self.require_focus and focus is False:
            control = "unfocused"

        now = self.clock()
        dt = self.period if self._prev_t is None else now - self._prev_t
        self._prev_t = now

        if scene.miss and control == "policy":
            self._dump_miss(scene.miss, recs)

        # hp 为 None = 两处都没读到, **不是** 0。原来 `(hp or 0.0) <= 0` 把读不到当成死亡。
        hp = None if scene.player is None else scene.player.get("hp")
        died = hp is not None and hp <= 0.0
        self.paused = control != "policy"
        if control != "policy" or died:
            # 这一段到此为止。键留着按住 / 光标留在外面的话, 死亡读秒和脚本拉人的
            # 那几秒里花朵会一直朝某个方向怼。
            self._stop_moving()

        return self._state(scene, control=control, kill_events=kill_events,
                           drop_events=drop_events, t=now, dt=dt)

    def release(self):
        """松开所有键并把光标压回花朵身上。

        仲裁器在做任何**阻塞**的事(等 AFK 弹窗、寻路回刷怪区、就地复活)之前必须调它:
        那些事都以秒计, 而策略上一拍按下的键还按着。实机(2026-09-24)踩到 —— 防御键
        shift 在整段寻路期间一直按着, florr 里防御会大幅减速, 于是一路"原地打转14次,
        判定卡住", 272 秒的一局里 200 秒耗在挪不动的寻路上。
        """
        self._stop_moving()

    def close(self):
        self._stop_moving()
        if self._session is not None:
            self._session.close()

    def _stop_moving(self):
        self.sender.release_all()
        if self.mover is not None:
            self.mover.park(self._anchor)

    # ------------------------------------------------------------ 内部
    def _wait_for_tick(self):
        wait = self._deadline - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._deadline += self.period
        now = self.clock()
        if self._deadline <= now:
            # 落后超过一整拍(解码慢了/CDP 卡了)。硬追会陷进零睡眠, 越追越慢 ——
            # 直接重新对齐, 让 dt 如实反映这一拍真的跨了多久, 奖励那边自会判 stalled。
            self._deadline = now + self.period

    def _tick_js(self, with_counters=True):
        return _TICK_JS_TEMPLATE % (_READ_CHANGES if (with_counters and self.use_counters)
                                    else _NO_COUNTERS)

    def _poll(self):
        """(窗口有没有焦点, readChanges 的结果或 None, 一帧的绘制记录)。

        全部合成一条 JS 表达式 —— 实机测出来一次 CDP 往返光握手就 42ms, 10Hz 下
        每省一个往返都是实打实的。
        """
        want = self.use_counters and self._ticks % self.counter_every == 0
        data = self.evaluate(self._tick_js(want)) or {}
        return data.get("f"), data.get("c"), data.get("r") or []

    def _dump_miss(self, reason, recs):
        """策略在控时自己缺了点什么(没位置 / 没血量) -> 把这一帧存下来。按原因节流,
        有总上限, 写失败不抛。"""
        if not self.dump_dir or self._dumped >= self.dump_cap:
            return
        reason = reason or "unknown"
        now = self.clock()
        last = self._last_dump.get(reason)
        if last is not None and now - last < self.dump_every_s:
            return
        self._last_dump[reason] = now
        self._dumped += 1
        try:
            from pathlib import Path
            d = Path(self.dump_dir)
            d.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() else "_" for c in reason)
            name = f"self_miss_{time.strftime('%Y%m%d-%H%M%S')}_{self._dumped:02d}_{safe}.json"
            (d / name).write_text(json.dumps({"reason": reason, "raw": recs},
                                             ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self.last_error = f"dump:{e}"

    def _abort(self, reason):
        self.last_error = reason
        self._stop_moving()
        return None

    def _state(self, scene, *, control, kill_events, drop_events, t, dt):
        return {
            "t": t,
            "dt": dt,
            "control": control,
            "player": scene.player,
            "mobs": scene.mobs,
            "others": scene.others,
            "camera_approx": bool(scene.approx),
            "self_miss": scene.miss,
            "kill_events": kill_events,
            "drop_events": drop_events,
        }


def _probe_source():
    from pathlib import Path
    return Path(__file__).with_name("florr_wasm_probe.js").read_text(encoding="utf-8")
