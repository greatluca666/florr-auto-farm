"""真服后端的测试。假按键、假时钟、假 CDP —— 不碰游戏, 也不用装 pyautogui。

这里测不了的只有一件事: 发出去的按键真的让花朵动了。那个只能在那台 Windows 上验。
"""
import json
import re
from pathlib import Path

import pytest

import rl_backend
from rl_actions import ATTACK_KEY, DEFENSE_KEY, MOVE_KEYS, SWAP_KEY
from rl_backend import (
    MOB_RARITY_RANK, MOVE_UNIT, PETAL_RARITY_RANK, KeySender, LiveBackend, MouseMover,
    aggregate_events, ensure_manual_controls, keys_for_action,
)
from rl_scene import Scene

NOOP = (0, 0, 0, 0)


def make_mover():
    """记录用的鼠标通道。测试**绝不能**构造真的 MouseMover —— 那会去动这台机器上
    真实的光标。"""
    moved = []
    return MouseMover(radius=200.0, move_to=moved.append, screen_size=(1920, 1080)), moved


def make_sender():
    log = []
    s = KeySender(key_down=lambda k: log.append(("down", k)),
                  key_up=lambda k: log.append(("up", k)),
                  press=lambda k: log.append(("press", k)))
    return s, log


class FakeDecode:
    """吐预设场景; 吐完之后一直重复最后一个。记下每次收到的绘制记录。"""

    def __init__(self, scenes=None):
        self.scenes = list(scenes or [Scene(None, [], [], None, False)])
        self.seen = []

    def __call__(self, records):
        self.seen.append(records)
        return self.scenes.pop(0) if len(self.scenes) > 1 else self.scenes[0]


class FakeEval:
    def __init__(self, focus=True, changes=None, raises=None, fail_after=0, records=None):
        self.focus, self.raises, self.fail_after = focus, raises, fail_after
        self.changes = changes if changes is not None else {"first": False, "kills": [],
                                                            "drops": []}
        self.records = records if records is not None else [{"frame": 1}]
        self.calls = []

    def __call__(self, expr, timeout=15):
        self.calls.append(expr)
        if self.raises and len(self.calls) > self.fail_after:
            raise self.raises
        if expr == rl_backend._FOCUS_JS:
            return self.focus
        has_counters = rl_backend._READ_CHANGES in expr
        return {"f": self.focus, "c": self.changes if has_counters else None,
                "r": list(self.records)}


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += max(0.0, s)


def backend(**kw):
    ft = kw.pop("time", None) or FakeTime()
    sender = kw.pop("sender", None)
    if sender is None:
        sender, _ = make_sender()
    mover = kw.pop("mover", None)
    if mover is None:
        mover, _ = make_mover()
    b = LiveBackend(decode=kw.pop("decode", None) or FakeDecode(),
                    sender=sender, mover=mover,
                    evaluate=kw.pop("evaluate", None) or FakeEval(),
                    clock=ft.clock, sleep=ft.sleep, **kw)
    b._t = ft
    return b


ALIVE = Scene({"hp": 0.8, "hp_secondary": 0.0, "x": 10.0, "y": 20.0}, [], [], None, False)


# ── 动作 -> 按键 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("move", range(9))
def test_move_index_maps_to_the_same_keys_as_the_recorder(move):
    """录人类数据时是"按键 -> 下标", 这里是"下标 -> 按键"。两边错开一格的话, 学到的
    东西和发出去的动作就对不上了。"""
    held, _ = keys_for_action((move, 0, 0, 0))
    assert held == set(MOVE_KEYS[move])


def test_attack_and_defense_are_held_keys():
    held, taps = keys_for_action((0, 1, 1, 0))
    assert held == {ATTACK_KEY, DEFENSE_KEY} and taps == []


def test_swap_is_a_tap_not_a_hold():
    """按住 R 不会连续换装, 只会换一次 —— 所以它必须是点一下, 不能进按住集合。"""
    held, taps = keys_for_action((0, 0, 0, 1))
    assert taps == [SWAP_KEY] and SWAP_KEY not in held


def test_sender_releases_before_pressing():
    """从「北」切到「东」: 先按 d 再松 w 的话, 中间那一瞬间是「东北」。"""
    s, log = make_sender()
    s.apply((1, 0, 0, 0))          # 北 = w
    log.clear()
    s.apply((3, 0, 0, 0))          # 东 = d
    assert log == [("up", "w"), ("down", "d")]


def test_sender_sends_nothing_when_the_action_repeats():
    """同一个动作连着几拍很常见。每拍重发 keyDown 会被游戏当成连打。"""
    s, log = make_sender()
    s.apply((2, 1, 0, 0))
    log.clear()
    s.apply((2, 1, 0, 0))
    assert log == []


def test_swap_fires_every_time_even_when_the_rest_is_unchanged():
    s, log = make_sender()
    s.apply((0, 0, 0, 1))
    log.clear()
    s.apply((0, 0, 0, 1))
    assert log == [("press", SWAP_KEY)]


def test_release_all_clears_everything():
    s, log = make_sender()
    s.apply((2, 1, 1, 0))
    log.clear()
    s.release_all()
    assert sorted(k for _, k in log) == sorted({"w", "d", ATTACK_KEY, DEFENSE_KEY})
    assert s.held == set()
    log.clear()
    s.release_all()
    assert log == []               # 幂等


# ── 计数器增量 ───────────────────────────────────────────────────────────────

def test_same_rarity_is_merged():
    events, skipped = aggregate_events(
        [{"sid": "beetle", "rarity": "传说", "delta": 2},
         {"sid": "scorpion", "rarity": "传说", "delta": 1},
         {"sid": "sandstorm", "rarity": "神话", "delta": 1}], MOB_RARITY_RANK)
    assert events == [(4, 3), (5, 1)] and skipped == 0


def test_negative_delta_is_dropped():
    """计数器只会涨。掉下去 = 换号/重连后整张表重建了, 不是"负的击杀"。"""
    events, skipped = aggregate_events(
        [{"rarity": "传说", "delta": -900}], MOB_RARITY_RANK)
    assert events == [] and skipped == 1


def test_unknown_rarity_word_is_dropped():
    events, skipped = aggregate_events([{"rarity": "Legendary", "delta": 1}],
                                       MOB_RARITY_RANK)
    assert events == [] and skipped == 1


def test_petal_table_has_the_extra_unique_tier():
    assert PETAL_RARITY_RANK["独特"] == 8 and "独特" not in MOB_RARITY_RANK


def test_rarity_tables_match_the_probe_js():
    """探针那边是按下标发稀有度词的。两张表错一格, 奖励权重就整体错档 —— 而那种错法
    不会报错, 只会让策略学偏。"""
    probe = Path(__file__).with_name("florr_wasm_probe.js")
    if not probe.exists():
        pytest.skip("florr_wasm_probe.js 公开树不带")
    src = probe.read_text(encoding="utf-8")

    def table(name):
        m = re.search(r"var %s = (\[[^\]]*\]);" % name, src)
        return json.loads(m.group(1))

    assert table("MOB_RARITY_NAMES") == [w for w, _ in
                                         sorted(MOB_RARITY_RANK.items(), key=lambda kv: kv[1])]
    assert table("PETAL_RARITY_NAMES") == [w for w, _ in
                                           sorted(PETAL_RARITY_RANK.items(),
                                                  key=lambda kv: kv[1])]


# ── 反转开关 ─────────────────────────────────────────────────────────────────

def test_manual_controls_turn_both_inversions_off():
    """反转攻击键开着时, 花朵一直自动打 —— 策略的攻击维按不按都一样, 那一维是死的。"""
    seen = []

    def fake_eval(js, timeout=5):
        seen.append(js)
        return {"result": {"result": {"value": json.dumps({"ok": True, "before": 1,
                                                           "after": 0})}}}

    report = ensure_manual_controls(fake_eval)
    assert report["attack"][0] == "changed" and report["defense"][0] == "changed"
    assert len(seen) == 2
    assert all("const W = 0;" in js for js in seen)


# ── 后端协议 ─────────────────────────────────────────────────────────────────

def counter_reads(ev):
    return [c for c in ev.calls if rl_backend._READ_CHANGES in c]


def test_reset_primes_the_change_snapshot():
    """第一次 readChanges 只建快照, 结果扔掉。不扔的话, 开局会把这个账号有史以来的
    击杀一次性当成"刚杀的"发出去 —— 那是几万分的假奖励。"""
    ev = FakeEval(changes={"first": False, "kills": [{"rarity": "神话", "delta": 9999}],
                           "drops": []})
    b = backend(evaluate=ev, decode=FakeDecode([ALIVE]))
    st = b.reset()
    assert counter_reads(ev)             # 确实读了(建快照)
    assert st["kill_events"] == []       # 但一分都没发出去


class AccruingEval(FakeEval):
    """readChanges 返回"上次读之后攒下的", 读完清空 —— 跟真探针一样。"""

    def __init__(self):
        super().__init__()
        self.pending = []

    def __call__(self, expr, timeout=15):
        out = super().__call__(expr, timeout)
        if isinstance(out, dict) and out.get("c") is not None:
            out["c"] = {"first": False, "kills": self.pending, "drops": []}
            self.pending = []
        return out


def test_what_the_script_kills_during_reset_never_reaches_the_policy():
    """实机(2026-09-24, 反转在脚本段打开之后): 一局 337 步报了 162 杀, 一个进度窗口里
    击杀从 10 跳到 82 —— 恰好跨着一次复活。reset() 原来是**先**丢计数器快照、**后**
    问仲裁器; 仲裁器的节流检查要是落在 reset 里, 就在这里跑复活/拉回, 脚本一路砍死
    的几十只怪全算到下一拍的策略头上。每死一次白送 +70, 比 -5 的死亡惩罚大得多 ——
    等于在奖励它去死。"""
    ev = AccruingEval()

    def hint():
        ev.pending.append({"rarity": "神话", "delta": 70})    # 复活走回去一路砍的
        return "dead"

    b = backend(evaluate=ev, decode=FakeDecode([ALIVE]), control_hint=hint)
    b.reset()
    b.control_hint = lambda: "policy"
    b.send_action((0, 0, 0, 0))
    assert b.next_state()["kill_events"] == []


def test_reset_releases_keys_and_clears_the_frame_buffer():
    s, log = make_sender()
    s.apply((3, 1, 0, 0))
    log.clear()
    d = FakeDecode([ALIVE])
    b = backend(sender=s, decode=d)
    b.reset()
    assert s.held == set() and d.seen


def test_reset_waits_for_a_frame_with_a_player():
    """刚进图/刚重连时头几帧解不出自己。直接返回一个没有玩家的观测, 等于用一个空状态
    开局。"""
    blank = Scene(None, [], [], None, False)
    b = backend(decode=FakeDecode([blank, blank, ALIVE]))
    assert b.reset()["player"] is not None


def test_reset_gives_up_after_the_retries():
    b = backend(decode=FakeDecode([Scene(None, [], [], None, False)]), reset_retries=3)
    assert b.reset()["player"] is None


def test_next_state_carries_the_scene_and_events():
    ev = FakeEval(changes={"first": False, "kills": [{"rarity": "神话", "delta": 1}],
                           "drops": [{"rarity": "稀有", "delta": 2}]})
    b = backend(evaluate=ev, decode=FakeDecode([ALIVE]))
    b.reset()
    st = b.next_state()
    assert st["control"] == "policy"
    assert st["kill_events"] == [(5, 1)] and st["drop_events"] == [(2, 2)]
    assert st["player"]["hp"] == 0.8


def test_control_hint_overrides():
    b = backend(decode=FakeDecode([ALIVE]), control_hint=lambda: "script")
    b.reset()
    assert b.next_state()["control"] == "script"


def test_unfocused_window_is_not_policy_control():
    """pyautogui 的键发给**当前焦点窗口**。人一切走, 这一拍的动作根本没进游戏 ——
    照样算进 rollout 的话, 就是在拿"按了但没生效"的样本训练。"""
    b = backend(evaluate=FakeEval(focus=False), decode=FakeDecode([ALIVE]))
    b.reset()
    assert b.next_state()["control"] == "unfocused"


def test_focus_check_can_be_turned_off():
    b = backend(evaluate=FakeEval(focus=False), decode=FakeDecode([ALIVE]),
                require_focus=False)
    b.reset()
    assert b.next_state()["control"] == "policy"


def test_script_control_releases_the_keys():
    """脚本要把人拉回刷怪区, 结果策略还按着 d —— 两边打架。"""
    s, _ = make_sender()
    b = backend(sender=s, decode=FakeDecode([ALIVE]), control_hint=lambda: "script")
    b.reset()
    b.send_action((3, 1, 0, 0))
    assert s.held
    b.next_state()
    assert s.held == set()


def test_death_releases_the_keys():
    dead = Scene({"hp": 0.0, "hp_secondary": 0.0, "x": 1.0, "y": 2.0}, [], [], None, False)
    s, _ = make_sender()
    b = backend(sender=s, decode=FakeDecode([ALIVE, dead]))
    b.reset()
    b.send_action((3, 1, 0, 0))
    b.next_state()
    assert s.held == set()


def test_cdp_failure_ends_the_episode_and_releases_keys():
    s, _ = make_sender()
    b = backend(sender=s, evaluate=FakeEval(raises=RuntimeError("socket closed")),
                decode=FakeDecode([ALIVE]))
    b._ticks = 0
    b.send_action((3, 0, 0, 0))
    assert b.next_state() is None
    assert s.held == set() and "socket closed" in b.last_error


def test_counter_base_drift_ends_the_episode():
    """基址漂了还继续跑, 会把错位的数字当击杀发奖励 —— 比没有奖励糟得多: 策略会追着
    一个跟游戏无关的信号学。"""
    ev = FakeEval(changes={"first": False, "kills": [], "drops": [], "hiNonZero": 3})
    b = backend(evaluate=ev, decode=FakeDecode([ALIVE]))
    b.reset()
    assert b.next_state() is None
    assert "hiNonZero=3" in b.last_error


def test_probe_not_ready_ends_the_episode():
    ev = FakeEval(changes={"error": "not-ready:scanning"})
    b = backend(evaluate=ev, decode=FakeDecode([ALIVE]))
    b.reset()
    assert b.next_state() is None
    assert "not-ready" in b.last_error


def test_counters_can_be_skipped_on_some_ticks():
    """每拍一次 readChanges 是一个额外的 CDP 往返。挂账超时 5 秒, 攒几拍再结算不影响
    归因。"""
    ev = FakeEval(changes={"first": False, "kills": [{"rarity": "神话", "delta": 1}],
                           "drops": []})
    b = backend(evaluate=ev, decode=FakeDecode([ALIVE]), counter_every=3)
    b.reset()
    before = len(counter_reads(ev))
    got = [b.next_state()["kill_events"] for _ in range(3)]
    assert got == [[], [], [(5, 1)]]
    assert len(counter_reads(ev)) - before == 1     # 三拍只读了一次计数器


def test_no_counters_mode_never_touches_the_probe():
    ev = FakeEval()
    b = backend(evaluate=ev, decode=FakeDecode([ALIVE]), use_counters=False)
    b.reset()
    b.next_state()
    assert counter_reads(ev) == []


# ── 节拍 ─────────────────────────────────────────────────────────────────────

def test_it_sleeps_to_the_tick_boundary():
    ft = FakeTime()
    b = backend(time=ft, decode=FakeDecode([ALIVE]), hz=10.0)
    b.reset()
    b.next_state()
    assert ft.slept == [pytest.approx(0.1)]


def test_a_late_tick_resyncs_instead_of_spinning():
    """解码慢了一拍时硬追(补睡 0)会把后面每一拍都变成零睡眠, 越追越糟。重新对齐,
    让 dt 如实反映真跨了多久 —— 奖励那边自会判 stalled 把这一步作废。"""
    ft = FakeTime()
    b = backend(time=ft, decode=FakeDecode([ALIVE]), hz=10.0)
    b.reset()
    ft.now += 1.0                  # 这一拍卡了 1 秒
    ft.slept.clear()
    b.next_state()
    assert ft.slept == []          # 已经晚了, 不睡
    ft.slept.clear()
    b.next_state()
    assert ft.slept == [pytest.approx(0.1)]   # 下一拍恢复正常节奏, 不是补睡 0


def test_dt_reports_the_real_gap():
    ft = FakeTime()
    b = backend(time=ft, decode=FakeDecode([ALIVE]), hz=10.0)
    b.reset()
    b.next_state()
    ft.now += 0.9
    assert b.next_state()["dt"] == pytest.approx(0.9)


def test_close_releases_the_keys():
    s, _ = make_sender()
    b = backend(sender=s, decode=FakeDecode([ALIVE]))
    b.send_action((2, 1, 1, 0))
    b.close()
    assert s.held == set()


# ── 跟环境对接 ───────────────────────────────────────────────────────────────

def test_the_env_can_drive_this_backend_end_to_end():
    """后端协议和 FlorrEnv 的契约对不上是最容易悄悄发生的错 —— 两边各自的测试都用桩,
    谁也测不到对接。这条把真后端(假 CDP)塞进真环境跑一轮。"""
    from rl_env import OBS_DIM, FlorrEnv

    mob = {"name": "沙尘暴", "rarity_rank": 5, "hp": 1.0, "x": 60.0, "y": 20.0}
    scene = Scene({"hp": 0.9, "hp_secondary": 0.0, "x": 10.0, "y": 20.0},
                  [mob], [{"x": 200.0, "y": 20.0, "level": 118}], None, False)
    ev = FakeEval(changes={"first": False, "kills": [{"rarity": "神话", "delta": 1}],
                           "drops": []})
    env = FlorrEnv(backend(evaluate=ev, decode=FakeDecode([scene])))

    obs, info = env.reset()
    assert obs.shape == (OBS_DIM,) and info["n_mobs"] == 1
    obs, reward, terminated, truncated, info = env.step((3, 1, 0, 0))
    assert not terminated and not truncated
    assert info["parts"]["kill"] > 0          # 神话击杀一路走到了奖励
    assert reward > 0


def test_a_dead_backend_truncates_the_episode():
    from rl_env import FlorrEnv

    # 掉线发生在 reset 之后 —— reset 时就连不上是"训练根本起不来", 那该当场抛,
    # 不是安静地跑一个空 rollout。
    env = FlorrEnv(backend(evaluate=FakeEval(raises=RuntimeError("closed"), fail_after=2),
                           decode=FakeDecode([ALIVE])))
    env.reset()
    _, reward, terminated, truncated, info = env.step(NOOP)
    assert (terminated, truncated) == (False, True)
    assert info["reason"] == "no_state" and reward == 0.0


# ── 一拍那条 JS 的真实行为(拿 node 跑) ───────────────────────────────────────

def run_tick_js(log, node_out="JSON.stringify(out)"):
    """在真 node 里执行那条一拍 JS, 回 (返回值, 执行后页面缓冲里还剩什么)。

    这段是整个后端里唯一一块跑在浏览器里的逻辑, 而且直接决定"解码器看到的是哪一帧"
    —— 挑错帧不会报错, 只会让场景莫名其妙地空一半。Python 侧怎么测都碰不到它。
    """
    import json as _json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node")
    js = rl_backend._TICK_JS_TEMPLATE % "null"
    src = ("var window = {__canvasLog: %s};\n"
           "var document = {hasFocus: function () { return true; }};\n"
           "var out = %s;\n"
           "console.log(JSON.stringify({out: out, left: window.__canvasLog}));\n"
           % (_json.dumps(log), js))
    r = subprocess.run([node, "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = _json.loads(r.stdout)
    return data["out"], data["left"]


def frame_log(*frames):
    return [{"frame": f, "tag": f"{f}.{i}"} for f, n in frames for i in range(n)]


def test_tick_js_returns_the_second_newest_frame():
    """最新那帧还在画一半 —— 用它会解出半张场景。"""
    out, _ = run_tick_js(frame_log((7, 2), (8, 3), (9, 1)))
    assert [r["frame"] for r in out["r"]] == [8, 8, 8]


def test_tick_js_drops_everything_up_to_that_frame():
    """取过的帧留在页面缓冲里的话, 下一拍还会再看到一遍同一帧。"""
    _, left = run_tick_js(frame_log((7, 2), (8, 3), (9, 1)))
    assert [r["frame"] for r in left] == [9]


def test_tick_js_returns_nothing_when_only_one_frame_is_buffered():
    out, left = run_tick_js(frame_log((9, 4)))
    assert out["r"] == [] and len(left) == 4      # 不动缓冲, 等下一帧画出来


def test_tick_js_on_an_empty_buffer():
    out, left = run_tick_js([])
    assert out["r"] == [] and left == []


def test_tick_js_ships_one_frame_not_the_whole_buffer():
    """实机瓶颈就在这: canvas_hook 页面缓冲留 5 帧, 一次 drain 拖回 490 条记录,
    而我们每拍只用一帧。185ms 的中位耗时里绝大部分是这些用不上的载荷。"""
    out, _ = run_tick_js(frame_log((1, 98), (2, 98), (3, 98), (4, 98), (5, 98)))
    assert len(out["r"]) == 98


def test_the_records_from_the_tick_reach_the_decoder():
    d = FakeDecode([ALIVE])
    b = backend(evaluate=FakeEval(records=[{"frame": 4, "tag": "x"}]), decode=d)
    b.reset()
    b.next_state()
    assert d.seen[-1] == [{"frame": 4, "tag": "x"}]


# ── 焦点闸 ───────────────────────────────────────────────────────────────────

def test_focus_gate_passes_straight_through_when_already_focused():
    calls = []
    assert rl_backend.wait_for_focus(lambda e, timeout=15: calls.append(e) or True,
                                     announce=lambda *a: None) is True
    assert len(calls) == 1


def test_focus_gate_waits_until_the_window_is_focused():
    """从终端敲命令启动训练时焦点在终端上 —— 策略发的每个键都进了终端。实机第一次
    诊断 100 拍全是 unfocused, 没有这道闸就是白跑一晚上。

    时钟必须**会走**: 用冻结时钟的话, 万一"拿到焦点就返回"那段逻辑坏了, 这条测试
    不是变红而是**挂死**(超时永远到不了)。挂死的测试什么也证明不了, 还会把整条
    测试流水线卡住 —— 实际就这么卡过一次。
    """
    answers = [False, False, True]
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    ok = rl_backend.wait_for_focus(lambda e, timeout=15: answers.pop(0), timeout=30.0,
                                   sleep=sleep, clock=lambda: t[0],
                                   announce=lambda *a: None)
    assert ok is True and len(slept) == 2


def test_focus_gate_gives_up_and_says_so():
    t = [0.0]

    def clock():
        return t[0]

    def sleep(_):
        t[0] += 10.0

    said = []
    ok = rl_backend.wait_for_focus(lambda e, timeout=15: False, timeout=30.0,
                                   sleep=sleep, clock=clock, announce=said.append)
    assert ok is False and any("没有焦点" in s for s in said)


# ── 鼠标那条移动通道 ─────────────────────────────────────────────────────────

def test_stand_still_parks_the_cursor_on_the_flower():
    """main.py 的 _drive_and_check_stall 一直拿"光标 == 锚点"当"刻意停在这" —— 说明
    florr 就是这么判不移动的。"""
    m, moved = make_mover()
    m.apply((0, 0, 0, 0), (800.0, 400.0))
    assert moved == [(800.0, 400.0)]


@pytest.mark.parametrize("idx", range(1, 9))
def test_direction_round_trips_through_the_recorder(idx):
    """录人类数据时是"光标偏移 -> 下标", 这里是"下标 -> 光标偏移"。两边不互逆的话,
    策略学到的方向和它实际走的方向会差一格, 而且没有任何地方会报错。"""
    from rl_actions import move_index_from_mouse

    m, moved = make_mover()
    anchor = (900.0, 500.0)
    m.apply((idx, 0, 0, 0), anchor)
    x, y = moved[-1]
    assert move_index_from_mouse({"x": x, "y": y}, anchor) == idx


def test_cursor_is_clamped_to_the_screen():
    """花朵贴着屏幕边缘时, 朝外那个方向算出来的坐标会跑到屏幕外。夹进来, 但**留边距**
    —— 夹到 0 就正好是 pyautogui 的失效保护点。"""
    m, moved = make_mover()
    m.apply((1, 0, 0, 0), (50.0, 30.0))       # 正北, 但上面只剩 30 像素
    assert moved[-1] == (50.0, m.margin)


def test_mouse_channel_does_not_press_movement_keys():
    """两条通道同时活着 = 花朵同时被键盘和光标拉。这正是要避免的那个 bug。"""
    s, log = make_sender()
    s.with_move = False
    s.apply((3, 1, 0, 0))                     # 东 + 攻击
    assert log == [("down", ATTACK_KEY)]      # 只有攻击, 没有 d


def test_backend_drives_the_cursor_from_the_decoded_anchor():
    """屏幕中心和真实锚点在地图边界能差上百像素 —— 拿中心当锚点会让每个方向都偏。"""
    scene = Scene({"hp": 0.9, "hp_secondary": 0.0, "x": 1.0, "y": 2.0}, [], [],
                  (700.0, 300.0), False)
    m, moved = make_mover()
    b = backend(decode=FakeDecode([scene]), mover=m)
    b.reset()
    moved.clear()
    b.send_action((3, 0, 0, 0))               # 东
    assert moved[-1] == (900.0, 300.0)        # 锚点 + 半径, 不是屏幕中心


def test_losing_control_parks_the_cursor():
    """脚本要把人拉回刷怪区, 光标却还指在外面 —— 花朵会一边被脚本拽一边自己往那边走。"""
    scene = Scene({"hp": 0.9, "hp_secondary": 0.0, "x": 1.0, "y": 2.0}, [], [],
                  (700.0, 300.0), False)
    m, moved = make_mover()
    b = backend(decode=FakeDecode([scene]), mover=m, control_hint=lambda: "script")
    b.reset()
    b.send_action((3, 0, 0, 0))
    moved.clear()
    b.next_state()
    assert moved[-1] == (700.0, 300.0)        # 压回花朵身上


def test_move_unit_table_has_all_nine():
    assert len(MOVE_UNIT) == 9 and MOVE_UNIT[0] == (0.0, 0.0)


def test_cursor_follows_the_anchor_as_it_moves():
    """花朵在画面上的位置不是固定的 —— 靠近地图边界时相机顶住不动, 花朵自己在屏幕上
    跑, 实测能偏出上百像素。锚点不跟着更新的话, 每一步的方向都会歪, 越靠边越歪。
    """
    def at(px, py):
        return Scene({"hp": 0.9, "hp_secondary": 0.0, "x": 1.0, "y": 2.0}, [], [],
                     (px, py), False)

    m, moved = make_mover()
    b = backend(decode=FakeDecode([at(700.0, 300.0), at(1100.0, 640.0)]), mover=m)
    b.reset()
    b.send_action((0, 0, 0, 0))
    b.next_state()                            # 这一帧花朵挪到了 (1100, 640)
    moved.clear()
    b.send_action((3, 0, 0, 0))               # 东
    assert moved[-1] == (1300.0, 640.0)       # 用的是新锚点, 不是 reset 时那个


# ── 键盘移动那个设置 ─────────────────────────────────────────────────────────

def setup_backend(monkeypatch, move_channel, *, flag=("unchanged", "")):
    import cdp_bridge
    import florr_settings

    monkeypatch.setattr(cdp_bridge, "inject_canvas_hook", lambda *a, **k: None)
    monkeypatch.setattr(rl_backend, "ensure_manual_controls",
                        lambda *a, **k: {"attack": ("unchanged", ""),
                                         "defense": ("unchanged", "")})
    seen = []

    def fake_flag(eval_js, addr, want):
        seen.append((addr, want))
        return flag

    monkeypatch.setattr(florr_settings, "ensure_flag", fake_flag)
    m, _ = make_mover()
    s, _ = make_sender()
    b = LiveBackend(move_channel=move_channel, use_counters=False, decode=FakeDecode(),
                    sender=s, mover=m, evaluate=FakeEval())
    return b, seen


def test_keys_channel_needs_the_keyboard_movement_setting(monkeypatch):
    """florr 默认**关**着「键盘移动」。关着时 WASD 一点反应都没有, 而且不报错 ——
    花朵原地不动, 策略却以为自己在走。这种坏法只能在启动时挡, 跑起来看不出来。"""
    b, _ = setup_backend(monkeypatch, "keys", flag=("failed", "addr-not-calibrated"))
    with pytest.raises(RuntimeError, match="键盘移动"):
        b.setup()


def test_keys_channel_turns_the_setting_on(monkeypatch):
    import florr_settings

    b, seen = setup_backend(monkeypatch, "keys", flag=("changed", ""))
    report = b.setup()
    assert (florr_settings.KEYBOARD_MOVE_ADDR, 1) in seen
    assert report["keyboard_move"] == ("changed", "")


def test_mouse_channel_does_not_touch_that_setting(monkeypatch):
    """走鼠标就不需要这个设置 —— 也不该去改用户 florr 账号里用不着的东西。"""
    b, seen = setup_backend(monkeypatch, "mouse")
    assert "keyboard_move" not in b.setup()
    assert seen == []


# ── 瘦身后解码结果必须不变(拿实拍帧 + 真 node 对拍) ──────────────────────────

FRAMES = __import__("pathlib").Path(__file__).with_name("test_frames")


def slim_via_node(records):
    """把实拍帧真的送进页面那段 JS, 拿回它瘦身后的结果。

    瘦身是省流量的手段, 不是可以牺牲正确性的地方 —— 少一位精度让一只怪从名牌上掉下来,
    不会报任何错, 只会让观测里少一只怪。所以直接拿实拍帧对拍解码结果。
    """
    import json as _json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node")
    # 加一条更新的帧, 否则"次新的那一帧"不存在
    log = list(records) + [{"frame": max(r["frame"] for r in records) + 1, "op": "fill",
                            "x": 0.0, "y": 0.0, "r": None, "fill": None, "stroke": None,
                            "m": [1, 0, 0, 1, 0, 0]}]
    js = rl_backend._TICK_JS_TEMPLATE % "null"
    src = ("var window = {__canvasLog: %s};\n"
           "var document = {hasFocus: function () { return true; }};\n"
           "console.log(JSON.stringify((%s).r));\n" % (_json.dumps(log), js))
    r = subprocess.run([node, "-e", src], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return _json.loads(r.stdout)


def decode_both(name):
    import json as _json

    import rl_scene

    raw = _json.loads((FRAMES / f"{name}.json").read_text(encoding="utf-8"))["raw"]
    return rl_scene.decode(raw), rl_scene.decode(slim_via_node(raw)), raw


@pytest.mark.parametrize("name", ["desert_ultra_44mobs", "desert_mythic_26mobs"])
def test_slimming_does_not_change_what_gets_decoded(name):
    full, slim, _ = decode_both(name)

    def key(m):
        return (m.get("name") or "", m.get("rarity_color") or "")

    assert len(slim.mobs) == len(full.mobs)
    assert sorted(map(key, slim.mobs)) == sorted(map(key, full.mobs))
    # 按最近邻配对, **不能**两边各自按坐标排序再 zip: 位置只要动一点点排序就换位,
    # 于是拿 A 去跟 B 比, 量出来的"误差"是两只不同怪之间的距离 —— 精度调高时这个
    # 数字反而会变大, 一度让我以为是代码坏了。
    left = list(slim.mobs)
    worst = 0.0
    for a in full.mobs:
        b = min(left, key=lambda m: (m["x"] - a["x"]) ** 2 + (m["y"] - a["y"]) ** 2)
        left.remove(b)
        worst = max(worst, ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5)
        assert (a.get("hp") or 0) == pytest.approx(b.get("hp") or 0, abs=0.01)
        assert key(a) == key(b)
    assert worst < 0.5, f"最远的怪被瘦身挪了 {worst:.2f} 个世界单位"
    assert (full.player is None) == (slim.player is None)
    if full.player:
        assert abs(full.player["x"] - slim.player["x"]) < 1.0
        assert full.player["hp"] == pytest.approx(slim.player["hp"], abs=0.01)
    assert len(slim.others) == len(full.others)


def test_slimming_actually_shrinks_the_payload():
    """省不下来的话这套复杂度就白加了。实拍帧实测省 38%。"""
    import json as _json

    _, _, raw = decode_both("desert_ultra_44mobs")
    before = len(_json.dumps(raw, separators=(",", ":")))
    after = len(_json.dumps(slim_via_node(raw), separators=(",", ":")))
    assert after < before * 0.8


def test_slimming_drops_the_fields_nobody_reads():
    """canvas_decode 从没读过 n/lw/alpha(grep 为 0)。它们合起来占 7KB/帧。"""
    _, _, raw = decode_both("desert_ultra_44mobs")
    assert any("alpha" in r for r in raw)          # 原始记录里确实有
    slim = slim_via_node(raw)
    assert not any(k in r for r in slim for k in ("n", "lw", "alpha"))


def test_scale_components_keep_enough_precision():
    """位数必须按**有效数字**给, 不是小数位。

    自身的绝对世界坐标是 (x - m[4]) / m[0] 反解的, 而小地图的 m[0] ≈ 0.0084 —— 除以它
    等于乘 119。按小数位留 4 位的话 0.0084 只剩两位有效数字, 0.15% 的相对误差落到
    12000 量级的坐标上就是 18 个世界单位, 足够让 rl_reward 的 20 单位格子认不出同一
    只怪, 于是掉血永远结算不了。
    """
    minimap_scale = 0.00841234567
    raw = [{"frame": 1, "op": "fill", "x": 812.3456789, "y": 407.6543211, "r": 3.0,
            "fill": "#FFE763", "stroke": None, "n": 2, "lw": 3.0, "alpha": 1,
            "bbox": [1.11111111, 2.22222222, 3.33333333, 4.44444444],
            "m": [minimap_scale, 0.0, 0.0, minimap_scale, 111.11111111, 222.22222222]}]
    got = slim_via_node(raw)[0]
    assert abs(got["m"][0] / minimap_scale - 1.0) < 1e-8      # 相对误差, 不是绝对
    assert got["m"][4] == pytest.approx(111.1111, abs=1e-9)   # 平移是像素, 4 位小数够
    assert got["x"] == pytest.approx(812.3457, abs=1e-9)


def test_a_minimap_scale_record_survives_the_round_trip():
    """直接钉住后果: 拿真实量级的小地图记录反解世界坐标, 瘦身前后必须几乎一样。"""
    import canvas_decode

    m = [0.00841234567, 0.0, 0.0, 0.00841234567, 111.11111111, 222.22222222]
    rec = {"frame": 1, "op": "fill", "x": 812.3456789, "y": 407.6543211, "r": 3.0,
           "fill": canvas_decode.PLAYER_BODY_COLOR, "stroke": None, "m": m}

    def world(r):
        return ((r["x"] - r["m"][4]) / r["m"][0], (r["y"] - r["m"][5]) / r["m"][3])

    a, b = world(rec), world(slim_via_node([rec])[0])
    assert abs(a[0] - b[0]) < 0.5 and abs(a[1] - b[1]) < 0.5


def test_backend_uses_a_persistent_connection_by_default():
    """每拍新建一条 CDP 连接白花 17.2ms(实机: HTTP GET /json 10.3 + 握手 6.9), 10Hz
    下是一拍预算的六分之一。"""
    import cdp_bridge

    b = LiveBackend(decode=FakeDecode(), sender=make_sender()[0], mover=make_mover()[0])
    assert isinstance(b._session, cdp_bridge.CdpSession)
    b._session.close = lambda: closed.append(1)
    closed = []
    b.close()
    assert closed == [1]


def test_injected_evaluate_means_no_session_of_our_own():
    b = backend()
    assert b._session is None
    b.close()                                   # 不该因为没有 session 就炸


# ── pyautogui 的全局 PAUSE ───────────────────────────────────────────────────

class FakeAutoGui:
    PAUSE = 0.1

    def __init__(self):
        self.seen = []

    def act(self, *a):
        self.seen.append(self.PAUSE)      # 记下调用**当时**的 PAUSE


def test_input_calls_do_not_pay_the_global_pause():
    """pyautogui.PAUSE 默认 0.1, 每次调用**之后**都睡。训练时一拍要 moveTo 一次, 按键
    有变化时还要 keyDown/keyUp —— 一拍光这个就 100~300ms, 10Hz 无从谈起。诊断脚本永远
    测不到这个, 因为它把输入整个 stub 掉了。"""
    g = FakeAutoGui()
    rl_backend.without_pause(g.act, g)("x")
    assert g.seen == [0]


def test_the_pause_is_put_back_afterwards():
    """main.py 的寻路用同一个 pyautogui, 它的节奏是按现在这个 PAUSE 调出来的。RL 接进
    auto_farming 之后两边同进程 —— 顺手改掉全局等于改了寻路的行为。"""
    g = FakeAutoGui()
    rl_backend.without_pause(g.act, g)("x")
    assert g.PAUSE == 0.1


def test_the_pause_is_put_back_even_when_the_call_blows_up():
    """FailSafeException 就是会抛的 —— 光标停在屏幕角落时 pyautogui 每次调用都抛。
    在那之后把 PAUSE 永久留成 0, 寻路的节奏会被悄悄改掉。"""
    g = FakeAutoGui()

    def boom():
        raise RuntimeError("failsafe")

    with pytest.raises(RuntimeError):
        rl_backend.without_pause(boom, g)()
    assert g.PAUSE == 0.1


# ── 等探针就绪 ───────────────────────────────────────────────────────────────

class ProbeEval:
    def __init__(self, states):
        self.states = list(states)
        self.asked = []

    def __call__(self, expr, timeout=15):
        self.asked.append(expr)
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def waiter(states):
    b = backend()
    b.evaluate = ProbeEval(states)
    # 名字别用 _t —— backend() 那个 helper 已经把 FakeTime 挂在 b._t 上了
    b._probe_clock = 0.0
    b.sleep = lambda s: setattr(b, "_probe_clock", b._probe_clock + s)
    b.clock = lambda: b._probe_clock
    return b


def test_probe_wait_only_asks_for_the_status():
    """别把整个探针对象拖回来: 它挂着花瓣/怪物目录, snapRange() 用过之后还挂着一个
    最多 200 万项的 Uint32Array —— JSON.stringify 那玩意会吐几十 MB, 而且是每轮一次。"""
    b = waiter([{"status": "ready"}])
    b._wait_probe_ready()
    assert b.evaluate.asked and all("status" in e for e in b.evaluate.asked)
    assert not any(e.strip() == "window.__florrWasmProbe" for e in b.evaluate.asked)


def test_probe_wait_returns_once_ready():
    b = waiter([{"status": "scanning"}, {"status": "scanning"}, {"status": "ready"}])
    b._wait_probe_ready()
    assert len(b.evaluate.asked) == 3


def test_probe_error_is_raised_with_its_reason():
    b = waiter([{"status": "error", "reason": "no-heap"}])
    with pytest.raises(RuntimeError, match="no-heap"):
        b._wait_probe_ready()


def test_probe_wait_times_out():
    b = waiter([{"status": "scanning"}])
    with pytest.raises(RuntimeError, match="超时"):
        b._wait_probe_ready(timeout=2.0)


@pytest.mark.parametrize("anchor", [(0.0, 0.0), (1919.0, 0.0), (0.0, 1079.0),
                                    (1919.0, 1079.0), (3.0, 2.0), (1917.0, 1077.0)])
@pytest.mark.parametrize("move", range(1, 9))
def test_the_cursor_never_lands_on_a_failsafe_corner(anchor, move):
    """pyautogui 的 FAILSAFE_POINTS 就是屏幕四角。光标停在任何一个角上, **下一次**
    pyautogui 调用就抛 FailSafeException —— 实机炸的是脚本的 click_continue_after_death,
    整个 worker 退出码 1。推光标的是策略, 抛的却是别人, 排查起来完全对不上。
    """
    m, moved = make_mover()
    m.apply((move, 0, 0, 0), anchor)
    assert tuple(moved[-1]) not in set(m.screen_corners())


def test_the_cursor_stays_inside_the_screen():
    m, moved = make_mover()
    for anchor in ((0.0, 0.0), (1919.0, 1079.0), (960.0, 540.0)):
        for move in range(9):
            m.apply((move, 0, 0, 0), anchor)
            x, y = moved[-1]
            assert 0 <= x <= 1919 and 0 <= y <= 1079


def test_parking_near_a_corner_also_avoids_it():
    """"不动"是把光标压在花朵身上 —— 花朵自己就在角上时, 那个位置同样是失效保护点。"""
    m, moved = make_mover()
    m.park((0.0, 0.0))
    assert tuple(moved[-1]) not in set(m.screen_corners())


# ── 脚本在控时别发动作 ───────────────────────────────────────────────────────

def test_no_actions_are_sent_while_the_script_is_in_control():
    """AFK 弹窗期间策略还在每拍推鼠标的话, 正好跟解 AFK 的那个工具抢光标。脚本把人
    拉回刷怪区时同理 —— 两边一起拽花朵。"""
    s, log = make_sender()
    m, moved = make_mover()
    b = backend(sender=s, mover=m, decode=FakeDecode([ALIVE]),
                control_hint=lambda: "script")
    b.reset()
    b.send_action((3, 1, 0, 0))
    b.next_state()                      # 这一拍之后知道了"脚本在控"
    log.clear()
    moved.clear()
    b.send_action((3, 1, 0, 0))
    assert log == [] and moved == []


def test_actions_resume_once_the_policy_gets_control_back():
    control = ["script"]
    s, log = make_sender()
    b = backend(sender=s, decode=FakeDecode([ALIVE]), control_hint=lambda: control[0])
    b.reset()
    b.next_state()
    control[0] = "policy"
    b.next_state()
    log.clear()
    b.send_action((3, 0, 0, 0))
    assert log == [("down", "d")]


def test_reset_clears_the_pause():
    """新一局开局就该能动 —— 上一局是被脚本接管着结束的很常见(死亡/拉回区)。"""
    b = backend(decode=FakeDecode([ALIVE]), control_hint=lambda: "script")
    b.reset()
    b.next_state()
    assert b.paused is True
    b.control_hint = lambda: "policy"
    b.reset()
    assert b.paused is False


# ── 没解出自己: 带上原因, 并把那一帧存下来离线查 ─────────────────────────────
#
# 实机(2026-09-24)修完挨打闪色后仍有几段"解出自己"只有 8~44%, 怪中位 0~4。
# 光看百分比查不下去; 等人盯着日志手动抓帧又对不上时机 —— 让后端自己留证据。

def missing(reason):
    return Scene(None, [], [], None, None, reason)


def test_the_state_says_why_self_was_missing():
    b = backend(decode=FakeDecode([missing("approx")]))
    b.reset()
    assert b.next_state()["self_miss"] == "approx"


def dumps(path):
    import json
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(path.glob("*.json"))]


def test_a_missed_frame_is_saved_for_offline_diagnosis(tmp_path):
    ev = FakeEval(records=[{"frame": 7, "op": "fill"}])
    b = backend(evaluate=ev, decode=FakeDecode([ALIVE, missing("camera:zoom")]),
                dump_dir=str(tmp_path))
    b.reset()
    b.next_state()
    (d,) = dumps(tmp_path)
    assert d["reason"] == "camera:zoom" and d["raw"] == [{"frame": 7, "op": "fill"}]


def test_dumps_are_rate_limited_per_reason(tmp_path):
    b = backend(decode=FakeDecode([ALIVE, missing("approx")]), dump_dir=str(tmp_path),
                dump_every_s=60.0)
    b.reset()
    b.next_state()
    b.next_state()                        # 同一个原因, 紧接着 —— 不再存
    assert len(dumps(tmp_path)) == 1
    b._t.now += 61.0
    b.next_state()
    assert len(dumps(tmp_path)) == 2


def test_a_new_reason_is_saved_straight_away(tmp_path):
    b = backend(decode=FakeDecode([ALIVE, missing("approx"), missing("no_bar")]),
                dump_dir=str(tmp_path))
    b.reset()
    b.next_state()
    b.next_state()
    assert sorted(d["reason"] for d in dumps(tmp_path)) == ["approx", "no_bar"]


def test_dumps_have_a_hard_cap(tmp_path):
    b = backend(decode=FakeDecode([ALIVE, missing("approx")]), dump_dir=str(tmp_path),
                dump_every_s=0.0, dump_cap=2)
    b.reset()
    for _ in range(5):
        b.next_state()
    assert len(dumps(tmp_path)) == 2


def test_nothing_is_saved_while_the_script_is_in_control(tmp_path):
    """死亡结算/寻路那些拍解不出自己是正常的, 存下来只是噪声。"""
    b = backend(decode=FakeDecode([ALIVE, missing("no_bar")]), dump_dir=str(tmp_path),
                control_hint=lambda: "script")
    b.reset()
    b.next_state()
    assert dumps(tmp_path) == []


def test_nothing_is_saved_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = backend(decode=FakeDecode([ALIVE, missing("approx")]))
    b.reset()
    b.next_state()
    assert list(tmp_path.rglob("*.json")) == []


NO_HP = Scene({"hp": None, "hp_secondary": None, "x": 10.0, "y": 20.0}, [], [],
              (960.0, 540.0), False, "no_hp")


def test_an_unknown_hp_is_not_a_death():
    """血量两处都没读到(None)不等于 0。原来 `(hp or 0.0) <= 0` —— 读不到就当死了,
    松键、光标压回花朵, 策略这一拍白发。"""
    s, log = make_sender()
    b = backend(decode=FakeDecode([ALIVE, NO_HP]), sender=s)
    b.reset()
    b.send_action((3, 1, 0, 0))
    log.clear()
    b.next_state()
    assert s.held                          # 键还按着 —— 没被当成死亡松掉


def test_a_frame_with_position_but_no_hp_is_saved_too(tmp_path):
    b = backend(decode=FakeDecode([ALIVE, NO_HP]), dump_dir=str(tmp_path))
    b.reset()
    b.next_state()
    assert [d["reason"] for d in dumps(tmp_path)] == ["no_hp"]
