"""从 canvas 日志读出"这一帧场上有什么" —— 录制和在线训练共用的一层。

原来这段逻辑在 rl_record.py 里, 带一个**模块级**的帧缓冲。在线训练要和录制跑同样的
解码, 但两边共享一个全局缓冲会互相吃帧 —— 所以抽成类, 每个使用者一份自己的缓冲。

只读: 不发任何按键/点击。
"""
from collections import namedtuple

import canvas_decode
import cdp_bridge

# canvas 里怪物稀有度是按颜色画的, 这张表把颜色还原成档位。
# 0..7 = 普通/罕见/稀有/史诗/传说/神话/究极/超神; 9 = 独特(灰)。
RANK_BY_COLOR = {"#7EEF6D": 0, "#FFE65D": 1, "#4D52E3": 2, "#861FDE": 3, "#DE1F1F": 4,
                 "#1FDBDE": 5, "#FF2B75": 6, "#2BFFA3": 7, "#555555": 9}

FRAME_BUFFER_CAP = 20000

# player_screen: 自己在画面上的锚点(像素), 鼠标方向换算要用; approx: 相机只是近似,
# 这一帧的自身血量/世界坐标不可信。
# miss: 自己缺了什么 —— "no_frame" / "camera:<缺的项>"(连位置都没有) / "no_hp"(位置有,
# 血量两处都读不到) / "player_error:<异常类>"; 全齐是 None。
Scene = namedtuple("Scene", "player mobs others player_screen approx miss",
                   defaults=(None,))
EMPTY = Scene(None, [], [], None, None, "no_frame")


class FrameReader:
    """按需从 CDP 拉 canvas 日志, 解出一帧的场景。

    drain: 注入点, 默认 cdp_bridge.drain_canvas_log。测试里换成假的就不用起 Chrome。
    """

    def __init__(self, *, drain=None, cap=FRAME_BUFFER_CAP):
        self._drain = drain or cdp_bridge.drain_canvas_log
        self._cap = cap
        self._buf = []

    def clear(self):
        self._buf.clear()

    def latest_frame(self):
        """次新的一帧 —— 最新那帧可能还在画一半, 用它会解出半张场景。

        取完就把这一帧之前的记录全丢掉: 缓冲只用来跨 drain 拼接一帧, 不是历史。
        """
        self._buf.extend(self._drain() or [])
        if len(self._buf) > self._cap:
            del self._buf[:-self._cap]
        frames = canvas_decode.group_by_frame(self._buf)
        if len(frames) < 2:
            return None
        keys = sorted(frames)
        recs = frames[keys[-2]]
        self._buf[:] = [r for r in self._buf if r.get("frame", -1) >= keys[-1]]
        return recs

    def scene(self):
        return decode(self.latest_frame())


def _self(recs, cam):
    """自己: 位置跟相机走, 血量/护甲先读左上角头像卡, 再退回脚下那条血条。返回 (player, miss)。

    原来"自己"= 脚下那条血条: 读不到(挨打闪色 / 被怪挤住 / 相机只能近似)就整个 None,
    观测连怪带人清零。可位置本来就在相机里 —— 怪的世界坐标就是拿同一个相机换算的,
    相对几何始终自洽。真正只能从自己身上读的只有血量, 而左上角就有(用户 2026-09-24)。
    两样都读不到: 位置照给, 血量标 None, miss="no_hp"。
    """
    hp = secondary = None
    miss = None
    try:
        hud = canvas_decode.hud_self_from_frame(recs)
    except Exception:
        hud = None
    if hud is not None:
        hp, secondary = hud["hp"], hud["hp_secondary"]
    else:
        try:
            bar = canvas_decode.player_from_frame(recs, cam)
        except Exception as e:
            bar, miss = None, f"player_error:{type(e).__name__}"
        if bar is not None:
            hp, secondary = bar["hp"], bar["hp_secondary"]
        elif miss is None:
            miss = "no_hp"
    world = cam.get("player_world")
    if world is None:
        return None, miss or "no_position"
    return {"hp": hp, "hp_secondary": secondary, "x": world[0], "y": world[1]}, miss


def decode(recs):
    """一帧的绘制记录 -> Scene。纯函数。

    解不出来返回 EMPTY, 不抛 —— 掉帧/切菜单/刚重连都会解不出, 那是常态不是异常,
    调用方该看到"这一帧什么都没有"而不是要包 try。

    四个解码器各自 try: 自己解不出来不该连带把怪也丢掉。
    """
    if not recs:
        return EMPTY
    try:
        cam = canvas_decode.camera_from_frame(recs, best_effort=True)
    except Exception as e:
        msg = str(e)
        what = msg.split("missing:")[-1].replace(" ", "") if "missing:" in msg else ""
        return EMPTY._replace(miss=f"camera:{what}" if what else "camera")
    me, miss = _self(recs, cam)
    try:
        mobs = [dict(m, rarity_rank=RANK_BY_COLOR.get(m.get("rarity_color"), -1))
                for m in canvas_decode.mobs_from_frame(recs, cam)]
    except Exception:
        mobs = []
    try:
        others = canvas_decode.other_players_from_frame(recs, cam)
    except Exception:
        others = []
    return Scene(me, mobs, others, cam.get("player_screen"), bool(cam.get("approx")), miss)
