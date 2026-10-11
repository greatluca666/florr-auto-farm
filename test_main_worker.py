import inspect
import io
import math
import os

import numpy as np
import pytest

import main
import map_routes


@pytest.fixture(autouse=True)
def _fresh_enemy_state(monkeypatch):
    """_FLEE_LATCH / _APPROACH 是进程内共享的状态 —— 前一条用例"刚躲过"会让后一条用例的
    决策被滞回改写, 结果跟执行顺序有关。每条用例一份干净的。"""
    monkeypatch.setattr(main, "_FLEE_LATCH", main._FleeLatch())
    monkeypatch.setattr(main, "_APPROACH", main.enemy_detect.ApproachTracker())
    monkeypatch.setattr(main, "_FLEE_PLAN", main._FleePlan())
    # release_keys 直接调 pyautogui.keyUp —— 测试机上别真的发按键
    monkeypatch.setattr(main, "release_keys", lambda: None)
    # 画布区域名要连 Chrome; 默认读不到(= 不判"人不在这张图上"), 要测的用例自己换
    monkeypatch.setattr(main, "canvas_zone_map", lambda: None)
    monkeypatch.setattr(main, "_WRONG_MAP_SEEN", None)
    # utils.MAP_OPEN_RECTS 也是进程内共享的(哪扇传送门被运行时挖开了) —— 上一条用例走过
    # 进场路线、门还开着的话, 下一条用例的 load_binary_map() 读到的就是挖开过的图, 结果
    # 跟执行顺序有关。每条用例前后都归零。
    monkeypatch.setattr(main.utils, "MAP_OPEN_RECTS", ())


_REAL_RELEASE_KEYS = main.release_keys


def test_apply_worker_config_reads_active_slice(monkeypatch):
    applied = {}
    monkeypatch.setattr(main, "apply_map", lambda name: applied.setdefault("map", name))
    cfg = {"version": 2, "active": {
        "map": "ocean", "location": [11, 22], "farming_area": [[1, 2], [3, 4]],
        "farming_duration": 120, "consecutive_short_round_limit": 5,
        "enemy_ai_enabled": False, "auto_switch_server": False,
    }}
    w = main._apply_worker_config(cfg)
    assert applied["map"] == "ocean"
    assert w["location"] == (11, 22)
    assert w["farming_area"] == [(1, 2), (3, 4)]
    assert w["farming_duration"] == 120
    assert w["short_round_limit"] == 5
    assert w["enemy_ai_enabled"] is False
    assert w["auto_switch_server"] is False


def test_apply_worker_config_maps_config_map_to_the_routes_server_biome(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    # 蚁穴以前映射到 ant_hell(BIOME_INDEX 4) —— 那是错的: 标题页没有蚁穴格,
    # 人是从**花园服**跑到固定洞口踩进去的, 所以 select_biome_on_title /
    # switch_server 都得拿 garden(0). 换到 ant_hell 服等于换到一个进不去的地方.
    w = main._apply_worker_config({"version": 2, "active": {"map": "anthell"}})
    assert w["biome"] == "garden"
    assert w["map_name"] == "anthell"
    assert w["route"].final_map == "anthell"
    w2 = main._apply_worker_config({"version": 2, "active": {"map": "ocean"}})
    assert w2["biome"] == "ocean"
    w3 = main._apply_worker_config({"version": 2, "active": {"map": "desert"}})
    assert w3["biome"] == "desert"
    assert w3["route"].final_map == "desert"


def test_apply_worker_config_falls_back_to_flat_when_no_active(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    cfg = {"map": "anthell", "location": [1, 1], "farming_area": [[0, 0], [2, 2]],
           "farming_duration": 99, "consecutive_short_round_limit": 4,
           "enemy_ai_enabled": True, "auto_switch_server": True}
    w = main._apply_worker_config(cfg)
    assert w["farming_duration"] == 99
    assert w["short_round_limit"] == 4


def test_apply_worker_config_fills_missing_from_defaults(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    w = main._apply_worker_config({"version": 2, "active": {"map": "desert"}})
    import app_config
    assert w["farming_duration"] == app_config.DEFAULTS["farming_duration"]
    assert w["location"] == tuple(app_config.DEFAULTS["location"])


def test_apply_worker_config_reads_invert_from_active(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    w = main._apply_worker_config({"version": 2, "active": {
        "map": "desert", "invert_attack": False, "invert_defense": True}})
    assert w["invert_attack"] is False
    assert w["invert_defense"] is True


def test_apply_worker_config_invert_defaults_when_absent(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    w = main._apply_worker_config({"version": 2, "active": {"map": "desert"}})
    assert w["invert_attack"] is True
    assert w["invert_defense"] is False


def test_wait_for_start_menu_returns_true_when_menu_present(monkeypatch):
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    assert main._wait_for_start_menu(timeout=5) is True


def test_wait_for_start_menu_polls_until_menu_appears(monkeypatch):
    seq = iter([False, False, True])
    monkeypatch.setattr(main, "on_start_screen", lambda: next(seq, True))
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    assert main._wait_for_start_menu(timeout=5) is True


def test_wait_for_start_menu_times_out(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    monkeypatch.setattr(main.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    assert main._wait_for_start_menu(timeout=3, interval=0.5) is False
    assert clock[0] >= 3


def test_maybe_scan_enemies_disabled_never_touches_enemy_detect(monkeypatch):
    monkeypatch.setattr(main.enemy_detect, "scan_enemies",
                        lambda **k: (_ for _ in ()).throw(AssertionError("不该扫描")))
    decision, dets, last, scanned = main._maybe_scan_enemies(
        False, 1000.0, 0.0, ("chase", "x"), ["old"])
    assert decision == ("wander", None)
    assert dets == []
    assert last == 0.0
    assert scanned is False


def test_maybe_scan_enemies_throttled_returns_prev(monkeypatch):
    monkeypatch.setattr(main.enemy_detect, "scan_enemies",
                        lambda **k: (_ for _ in ()).throw(AssertionError("还没到扫描间隔")))
    prev, prev_dets = ("flee", [(1, 2)]), ["d1", "d2"]
    decision, dets, last, scanned = main._maybe_scan_enemies(True, 0.1, 0.0, prev, prev_dets)
    assert decision is prev
    assert dets is prev_dets
    assert last == 0.0
    assert scanned is False


def test_maybe_scan_enemies_scans_when_due(monkeypatch):
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: ["det"])
    monkeypatch.setattr(main.enemy_detect, "select_action",
                        lambda dets, **k: ("chase", "target", 250, []))
    now = main.ENEMY_SCAN_INTERVAL + 1.0
    decision, dets, last, scanned = main._maybe_scan_enemies(
        True, now, 0.0, ("wander", None), [])
    assert decision == ("chase", "target", 250, [])
    assert dets == ["det"]
    assert last == now
    assert scanned is True


def test_maybe_scan_enemies_scan_error_degrades_to_wander(monkeypatch):
    monkeypatch.setattr(main.enemy_detect, "scan_enemies",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("model missing")))
    now = main.ENEMY_SCAN_INTERVAL + 1.0
    decision, dets, last, scanned = main._maybe_scan_enemies(
        True, now, 0.0, ("wander", None), ["old"])
    assert decision == ("wander", None)
    assert dets == []
    assert last == now
    assert scanned is True   # 尝试过一次观测 —— 算一次 miss


def test_auto_farming_accepts_enemy_ai_enabled_kwarg():
    sig = inspect.signature(main.auto_farming)
    assert "enemy_ai_enabled" in sig.parameters
    assert sig.parameters["enemy_ai_enabled"].kind == inspect.Parameter.KEYWORD_ONLY


def test_worker_graceful_exit_resets_keyboard_then_exits(monkeypatch):
    called = []
    monkeypatch.setattr(main, "reset_keyboard", lambda: called.append("reset"))
    with pytest.raises(SystemExit):
        main._worker_graceful_exit(15, None)
    assert called == ["reset"]


class _StubOverlay:
    def update(self, **kw):
        pass

    def show_warning(self, *a, **k):
        pass

    def hide_warning(self):
        pass


def test_run_worker_does_not_start_florr_auto_afk(monkeypatch):
    """florr-auto-afk 的生命周期归 GUI. worker 一旦自己调
    ensure_florr_auto_afk_running(), 在 exe 缺失时它会走到 input() —— 而
    console=False 打包出来的 worker stdin 是死的, 那一下直接把 worker 撂倒
    (RuntimeError: lost sys.stdin), 第一轮都跑不到. 而且用户刚在界面上关掉
    AFK 开关, worker 又会把它拉回来.
    """
    monkeypatch.setattr(main.cdp_bridge, "is_dedicated_chrome_ready", lambda: True)
    monkeypatch.setattr(
        main.afk_watch, "ensure_florr_auto_afk_running",
        lambda *a, **k: pytest.fail("worker 不该自己去拉起 florr-auto-afk"))
    monkeypatch.setattr(main, "create_overlay", lambda *a, **k: _StubOverlay())
    monkeypatch.setattr(main, "overlay", None, raising=False)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2),
        "farming_area": [(0, 0), (9, 9)],
        "farming_duration": 300,
        "short_round_limit": 2,
        "enemy_ai_enabled": False,
        "auto_switch_server": False,
        "biome": "desert",
        "map_name": "desert",
        "route": map_routes.route_for("desert"),
        "invert_attack": True,
        "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "select_biome_on_title", lambda *a, **k: None)  # 本测跟生态区无关
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ej, addr, want: ("unchanged", ""))
    # 主循环体的第一个调用 —— 在这里掐断, 前面的 setup 已经全跑完了.
    monkeypatch.setattr(main, "on_death_screen",
                        lambda: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})


def test_worker_stdin_watcher_resets_keyboard_on_eof(monkeypatch):
    """GUI 关掉 worker 的 stdin 管道 = 停止请求. 打包成 console=False 之后
    CTRL_BREAK / SIGTERM 都不一定送得到, 这条 EOF 路径是唯一保证"停止"时
    space+WASD 会被松开的机制 —— 它坏了, 每次停止都把角色卡在按住状态.
    """
    order = []
    monkeypatch.setattr(main, "reset_keyboard", lambda: order.append("reset"))
    monkeypatch.setattr(main.os, "_exit",
                        lambda code: (order.append(("exit", code)),
                                      (_ for _ in ()).throw(SystemExit(code))))
    monkeypatch.setattr(main.sys, "stdin", io.StringIO(""))   # 立刻 EOF

    with pytest.raises(SystemExit):
        main._worker_stdin_watch()

    assert order == ["reset", ("exit", 0)]


def test_install_worker_stdin_watcher_noop_without_stdin(monkeypatch):
    """打包后的 GUI 直接双击 exe 跑 worker 调试时 sys.stdin 可能是 None ——
    那种情况下别起线程(读 None 会直接抛)."""
    started = []

    class _FakeThreading:
        @staticmethod
        def Thread(*a, **k):
            started.append(1)
            raise AssertionError("stdin 是 None 时不该起看门线程")

    monkeypatch.setattr(main.sys, "stdin", None)
    monkeypatch.setattr(main, "threading", _FakeThreading)
    main._install_worker_stdin_watcher()
    assert started == []


def test_update_mythic_latch_locks_on_target():
    assert main._update_mythic_latch(False, 0, True, 3) == (True, 0)
    assert main._update_mythic_latch(True, 2, True, 3) == (True, 0)   # miss counter resets


def test_update_mythic_latch_stays_off_without_target():
    assert main._update_mythic_latch(False, 0, False, 3) == (False, 0)


def test_update_mythic_latch_counts_misses_then_releases():
    latched, misses = True, 0
    latched, misses = main._update_mythic_latch(latched, misses, False, 3)
    assert (latched, misses) == (True, 1)
    latched, misses = main._update_mythic_latch(latched, misses, False, 3)
    assert (latched, misses) == (True, 2)
    latched, misses = main._update_mythic_latch(latched, misses, False, 3)
    assert (latched, misses) == (False, 0)


def test_main_exposes_mythic_wiring():
    assert hasattr(main, "_drive_and_check_stall")
    assert isinstance(main.MYTHIC_LATCH_ENABLED, bool)
    for name in ("MYTHIC_ENGAGE_PX", "MYTHIC_RELEASE_PX", "MYTHIC_RELEASE_MISSES",
                 "MYTHIC_STRAFE_RADIUS", "MYTHIC_CACTUS_HOLD_PX",
                 "MYTHIC_STRAFE_K_RADIAL"):
        assert isinstance(getattr(main, name), (int, float))


def test_mythic_miss_counter_only_advances_on_fresh_scan():
    """节流 tick (scanned=False) 不能推进 miss 计数 —— 循环里 mythic 分支每 tick
    都跑, 但只有真扫描过的 tick 才是一次新观测. 少了这道门, 3-miss 释放在快机器上
    会缩成 ~2 (节流 tick 拿同一份缓存检测重复扣数)."""
    latched, misses = True, 0

    def tick(scanned, has_target):
        nonlocal latched, misses
        if scanned:
            latched, misses = main._update_mythic_latch(latched, misses, has_target, 3)

    tick(scanned=True, has_target=False)      # 真扫描 miss 1
    assert (latched, misses) == (True, 1)
    tick(scanned=False, has_target=False)     # 节流 tick —— 不推进
    assert (latched, misses) == (True, 1)
    tick(scanned=True, has_target=False)      # 真扫描 miss 2
    assert (latched, misses) == (True, 2)
    tick(scanned=False, has_target=False)     # 节流 tick —— 不推进
    assert (latched, misses) == (True, 2)
    tick(scanned=True, has_target=False)      # 真扫描 miss 3 —— 解锁
    assert (latched, misses) == (False, 0)


# ── move_to_position 的 on_tick 钩子 (wander 腿途中让外层索敌) ──────────────

def _stub_move_env(monkeypatch, pos=(10, 10), dead=False, menu=False):
    """把 move_to_position 的所有实机依赖打桩掉, 只留纯逻辑."""
    import types
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: pos, raising=False)
    monkeypatch.setattr(main, "on_death_screen", lambda: dead, raising=False)
    monkeypatch.setattr(main, "on_start_screen", lambda: menu, raising=False)
    monkeypatch.setattr(main, "reset_keyboard", lambda: None, raising=False)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda *a, **k: None)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(main, "overlay",
                        types.SimpleNamespace(update=lambda **k: None), raising=False)


def test_move_to_position_on_tick_aborts_leg_with_its_signal(monkeypatch):
    # 玩家位置恒定 (永远到不了目标), on_tick 第 3 次返回 "enemy" —— 应在那一 tick
    # 立刻收手, 返回该信号, 早于 max_attempts 和 stall 判定.
    _stub_move_env(monkeypatch, pos=(10, 10))
    calls = []

    def on_tick(pos):
        calls.append(pos)
        return "enemy" if len(calls) >= 3 else None

    result = main.move_to_position((10, 10), (999, 999), max_attempts=50, on_tick=on_tick)
    assert result == "enemy"
    assert len(calls) == 3


def test_move_to_position_on_tick_falsy_does_not_abort(monkeypatch):
    # on_tick 从不返回信号 —— 腿正常按老逻辑走完 (位置恒定 → stall → "stuck"),
    # 钩子每 tick 都被调到.
    _stub_move_env(monkeypatch, pos=(10, 10))
    ticks = []
    result = main.move_to_position((10, 10), (999, 999), max_attempts=50,
                                   on_tick=lambda p: ticks.append(p))
    assert result == "stuck"
    assert len(ticks) >= 5


def test_move_to_position_without_on_tick_unchanged(monkeypatch):
    # 不传 on_tick (默认 None) —— 行为跟以前完全一样: 已在 5px 内 → 立刻到达.
    _stub_move_env(monkeypatch, pos=(500, 500))
    assert main.move_to_position((500, 500), (502, 501), max_attempts=5) is True


# ── move_to_position 的三条"卡住"路径各自打日志 ────────────────────────────
# 实机复盘(2026-09-15, 花园寻路): 以前三条路径共用外层execute_path()打印的
# 笼统一句"卡住了", 分不清是位置检测丢失(跟顶层重试同一类问题)还是真的原地
# 打转(地图/坐标本身有问题) —— 这里补上区分, 方便下次直接看日志断根因.

def test_move_to_position_prints_when_position_is_lost_mid_move(monkeypatch, capsys):
    # pos 序列: 第一次拿到, 后面全是 None —— 走到"移动中丢失玩家位置"分支.
    seq = iter([(10, 10), None, None])
    monkeypatch.setattr(main, "get_player_position",
                        lambda *a, **k: next(seq, None), raising=False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False, raising=False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False, raising=False)
    monkeypatch.setattr(main, "reset_keyboard", lambda: None, raising=False)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda *a, **k: None)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    import types
    monkeypatch.setattr(main, "overlay",
                        types.SimpleNamespace(update=lambda **k: None), raising=False)

    result = main.move_to_position((10, 10), (999, 999), max_attempts=50)
    assert result == "stuck"
    assert "移动中丢失玩家位置" in capsys.readouterr().out


def test_move_to_position_prints_when_progress_stalls(monkeypatch, capsys):
    # 位置恒定 (dist 永远不缩短) —— 走到 stall_count 超限那条分支, 不是位置丢失.
    _stub_move_env(monkeypatch, pos=(10, 10))
    result = main.move_to_position((10, 10), (999, 999), max_attempts=50, stall_limit=3)
    assert result == "stuck"
    out = capsys.readouterr().out
    assert "原地打转" in out
    assert "移动中丢失玩家位置" not in out   # 两条路径互斥, 日志不该把它们混在一起


# ── 走路径: 窄道/拐角里别提前转向、别顶着墙推 ─────────────────────────────
# 蚁穴密道 2 像素宽带弯, 路点间隔 1~5 格。老判据"离路点 <5 就算到"在弯道里还没拐
# 过去就直奔下一跳, 顶着墙原地打转判卡住 —— 出生点到刷怪区必经两条密道, 次次卡.

def _l_corridor():
    m = np.zeros((20, 20), np.uint8)
    m[5, 2:11] = 255        # 横: (2..10, 5)
    m[5:16, 10] = 255       # 竖: (10, 5..15)
    return m


def test_waypoint_is_not_left_while_the_next_hop_is_round_the_corner(monkeypatch):
    m = _l_corridor()
    _stub_move_env(monkeypatch, pos=(7, 5))
    # 不给地图 = 老判据: 离拐角 (10,5) 3 格就算到
    assert main.move_to_position((7, 5), (10, 5), max_attempts=5) is True
    # 从 (7,5) 看不到拐角那头的 (10,15): 继续往拐角走
    assert main.move_to_position((7, 5), (10, 5), max_attempts=5,
                                 next_pos=(10, 15), binary_map=m) == "stuck"
    # 下一跳看得到: 照常提前转向
    assert main.move_to_position((7, 5), (10, 5), max_attempts=5,
                                 next_pos=(2, 5), binary_map=m) is True
    # 差 1 格就到拐角(读数抖动), 从这格直线看不到 (10,15) 也算到
    _stub_move_env(monkeypatch, pos=(9, 5))
    assert main.move_to_position((9, 5), (10, 5), max_attempts=5,
                                 next_pos=(10, 15), binary_map=m) is True


def test_overshooting_a_corner_is_not_arriving_while_the_next_hop_is_hidden(monkeypatch):
    m = _l_corridor()
    _stub_move_env(monkeypatch)

    def walk(**kw):
        seq = iter([(4, 5), (2, 5), (2, 5)])
        monkeypatch.setattr(main, "get_player_position", lambda *a, **k: next(seq))
        return main.move_to_position((4, 5), (10, 5), max_attempts=3, **kw)

    assert walk() is True               # 老判据: 6 -> 8 离远了 = 冲过头算到
    assert walk(next_pos=(10, 15), binary_map=m) == "stuck"


def test_unseen_target_is_approached_along_the_corridor_not_through_the_wall(monkeypatch):
    m = _l_corridor()
    assert main._steer_point(m, (4, 5), (10, 5)) == (10, 5)
    assert main._steer_point(m, (4, 5), (10, 12)) == (9, 5)
    assert main._steer_point(None, (4, 5), (10, 12)) == (10, 12)

    _stub_move_env(monkeypatch, pos=(4, 5))
    mouse = []
    monkeypatch.setattr(main, "_move_mouse_safely", mouse.append)
    main.move_to_position((4, 5), (10, 12), max_attempts=1, binary_map=m)
    x, y = mouse[0]
    assert x > main.SCREEN_WIDTH // 2 and abs(y - main.SCREEN_HEIGHT // 2) <= 1


class _SlidingFlower:
    """离线物理: 朝鼠标方向走(鼠标离中心越远越快, 封顶 vmax 格/跳), 撞墙沿轴滑."""

    def __init__(self, walk, start, vmax):
        self.walk, self.p, self.vmax, self.mouse = walk, [float(start[0]), float(start[1])], vmax, None

    def _wall(self, x, y):
        xi, yi = int(round(x)), int(round(y))
        h, w = self.walk.shape
        return not (0 <= xi < w and 0 <= yi < h) or self.walk[yi, xi] == 0

    def position(self, *a, **k):
        if self.mouse is not None:
            ox = self.mouse[0] - main.SCREEN_WIDTH // 2
            oy = self.mouse[1] - main.SCREEN_HEIGHT // 2
            d = (ox * ox + oy * oy) ** 0.5
            if d >= 1:
                speed = self.vmax * min(d / (250 * main.mouse_scale()), 1.0)
                n = max(1, int(speed / 0.2) + 1)
                vx, vy = speed * ox / d / n, speed * oy / d / n
                for _ in range(n):
                    x, y = self.p
                    for tx, ty in ((x + vx, y + vy), (x + vx, y), (x, y + vy)):
                        if not self._wall(tx, ty):
                            self.p = [tx, ty]
                            break
        return (int(round(self.p[0])), int(round(self.p[1])))


@pytest.mark.parametrize("map_name, start, goal, vmax", [
    ("anthell", (117, 102), (23, 89), 3.0),    # 实机出生点 -> 刷怪区, 连穿两条密道
    ("anthell", (72, 91), (109, 110), 2.0),    # 反着穿回来
    ("garden", (62, 132), (31, 129), 3.0),     # 出生点左边那条
    ("desert", (194, 142), (167, 138), 3.0),
    ("ocean", (72, 268), (68, 243), 3.0),
])
def test_routes_through_the_shortcut_tunnels_get_walked(monkeypatch, map_name, start, goal, vmax):
    import cv2
    walk = cv2.imread(f"./maps/{map_name}.png", cv2.IMREAD_GRAYSCALE)
    _stub_move_env(monkeypatch)
    # 这条测的是 execute_path 的 binary_map 判据(老判据卡密道、新判据走得通), 用纯最短路(贴墙)的路径才能
    # 把"老判据卡住"这个前提立住; 带净空的规划(默认)另有断言: 同样走得通。
    path = main.lazy_theta_star(walk, start, goal, clearance=False)
    assert path is not None
    path_clear = main.lazy_theta_star(walk, start, goal)
    assert path_clear is not None

    def walk_it(binary_map, nodes=path):
        flower = _SlidingFlower(walk, start, vmax)
        monkeypatch.setattr(main, "get_player_position", flower.position)
        monkeypatch.setattr(main, "_move_mouse_safely", lambda p: setattr(flower, "mouse", p))
        monkeypatch.setattr(main, "reset_keyboard", lambda: setattr(flower, "mouse", None))
        return main.execute_path(nodes, binary_map=binary_map)

    assert walk_it(None) == "stuck"      # 老判据: 卡在密道里
    assert walk_it(walk) is True
    assert walk_it(walk, path_clear) is True          # 带净空的路径照样穿得过密道


# ── _move_mouse_safely: pyautogui FailSafe 恢复(2026-09-20实机复盘) ─────────

def test_move_mouse_safely_passes_through_on_the_normal_path(monkeypatch):
    calls = []
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda pos: calls.append(pos))
    main._move_mouse_safely((100, 200))
    assert calls == [(100, 200)]


def test_move_mouse_safely_recovers_from_failsafe_exception(monkeypatch, capsys):
    # 用户实机反馈: 角色和鼠标指针在传送点附近完全静止不动, 直到用户自己碰了
    # 一下鼠标才恢复; 同一份日志里 lazy_theta_pathing 反复"执行路径"却读回来
    # 的位置从没变过, 跟"鼠标指针压根没在真的移动"吻合。pyautogui 默认开着
    # FAILSAFE, 鼠标停在屏幕角落时任何调用都会先抛 FailSafeException, 而这条
    # 调用链之前完全没捕获过 —— 这条测的是抓到之后老实打印 + 挪回屏幕中央
    # 尝试恢复, 不是让异常就这么把整条调用链带崩.
    calls = []

    def fake_move_to(pos):
        calls.append(pos)
        if len(calls) == 1:
            raise main.pyautogui.FailSafeException("mouse in corner")

    monkeypatch.setattr(main.pyautogui, "moveTo", fake_move_to)
    monkeypatch.setattr(main, "SCREEN_WIDTH", 1920, raising=False)
    monkeypatch.setattr(main, "SCREEN_HEIGHT", 1080, raising=False)

    main._move_mouse_safely((5000, 5000))   # 第一次调用会触发 FailSafeException

    # 第一次是原本要去的目标(触发异常), 第二次是恢复动作(挪回屏幕中央).
    assert calls == [(5000, 5000), (960, 540)]
    assert "FailSafe" in capsys.readouterr().out


def test_move_mouse_safely_restores_failsafe_flag_after_recovering(monkeypatch):
    # 恢复动作本身得先临时关掉 FAILSAFE(不然挪回屏幕中央这个调用也会立刻再抛
    # 一次), 但用完必须恢复成 True —— 不能让后续所有 pyautogui 调用永久失去
    # FailSafe 保护.
    monkeypatch.setattr(main.pyautogui, "FAILSAFE", True, raising=False)
    calls = []

    def fake_move_to(pos):
        calls.append((pos, main.pyautogui.FAILSAFE))
        if len(calls) == 1:
            raise main.pyautogui.FailSafeException("mouse in corner")

    monkeypatch.setattr(main.pyautogui, "moveTo", fake_move_to)
    main._move_mouse_safely((100, 100))

    assert calls[1][1] is False   # 恢复那一步调用时 FAILSAFE 确实被临时关掉了
    assert main.pyautogui.FAILSAFE is True   # 结束后恢复成 True, 不是永久关闭


def test_move_mouse_safely_restores_failsafe_flag_even_if_recovery_itself_fails(monkeypatch):
    monkeypatch.setattr(main.pyautogui, "FAILSAFE", True, raising=False)
    calls = []

    def fake_move_to(pos):
        calls.append(pos)
        raise main.pyautogui.FailSafeException("still stuck")

    monkeypatch.setattr(main.pyautogui, "moveTo", fake_move_to)
    with pytest.raises(main.pyautogui.FailSafeException):
        main._move_mouse_safely((100, 100))

    assert main.pyautogui.FAILSAFE is True   # finally 保证恢复, 不管恢复动作本身成不成功


def test_lazy_theta_pathing_names_which_screen_it_landed_on(monkeypatch, capsys):
    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    assert main.lazy_theta_pathing((1, 2)) is False
    assert "开局菜单" in capsys.readouterr().out

    monkeypatch.setattr(main, "on_death_screen", lambda: True)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    assert main.lazy_theta_pathing((1, 2)) is False
    assert "死亡结算画面" in capsys.readouterr().out


def test_unreadable_position_diagnostics_fire_on_a_schedule(monkeypatch, capsys):
    """2026-10-08 第一份录像: 死后回到标题页, 位置读不出来连续 5 分钟(57 次重试 + 之后一轮轮脱困),
    日志里只有同一句"无法检测玩家位置", 看不出是截图黑了还是画面认不出。第 10 次起记一行屏幕体检 +
    页面焦点, 之后每 60 次再记一行(不能每次都记: 一次整屏截图 + 一次 CDP 往返)。"""
    monkeypatch.setattr(main.utils, "screen_health", lambda: ("屏幕截图亮度 3 起伏 1 —— 几乎全黑!", True))
    monkeypatch.setattr(main.cdp_bridge, "_eval_value", lambda js, **k: "true,visible")
    fired = []
    for n in range(1, 200):
        main._report_unreadable_position(n)
        if capsys.readouterr().out:
            fired.append(n)
    assert fired == [10, 60, 120, 180]
    main._report_unreadable_position(10)
    out = capsys.readouterr().out
    assert "几乎全黑" in out and "true,visible" in out


def test_unreadable_position_diagnostics_print_the_value_not_the_cdp_envelope(monkeypatch, capsys):
    monkeypatch.setattr(main.utils, "screen_health", lambda: ("屏幕截图亮度 90 起伏 40", False))
    # 真的 eval_js 返回整个 CDP 应答; 诊断行里不该出现 {'id': ..., 'result': ...}
    monkeypatch.setattr(main.cdp_bridge, "eval_js", lambda js, **k: {"id": 1, "result": {"result": {"value": "x"}}})
    monkeypatch.setattr(main.cdp_bridge, "_eval_value", lambda js, **k: "true,visible")
    main._report_unreadable_position(10)
    out = capsys.readouterr().out
    assert "true,visible" in out and "'result'" not in out and "'id'" not in out


def test_unreadable_position_diagnostics_survive_a_dead_cdp(monkeypatch, capsys):
    monkeypatch.setattr(main.utils, "screen_health", lambda: ("屏幕截图亮度 90 起伏 40", False))

    def boom(*a, **k):
        raise ConnectionError("cdp down")
    monkeypatch.setattr(main.cdp_bridge, "_eval_value", boom)
    main._report_unreadable_position(10)           # 不能抛: 这是诊断, 不能把寻路循环带崩
    out = capsys.readouterr().out
    assert "屏幕截图亮度 90" in out and "页面状态读不到" in out


def test_lazy_theta_pathing_runs_the_diagnostics_while_position_is_unreadable(monkeypatch, capsys):
    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "_on_wrong_map", lambda: None)
    monkeypatch.setattr(main, "get_player_position", lambda: None)
    monkeypatch.setattr(main.utils, "screen_health", lambda: ("屏幕截图亮度 0 起伏 0 —— 几乎全黑!", True))
    monkeypatch.setattr(main.cdp_bridge, "_eval_value", lambda js, **k: "false,hidden")
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 12:
            raise KeyboardInterrupt        # 永不放弃的循环, 测试里靠这个跳出来

    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        main.lazy_theta_pathing((1, 2))
    out = capsys.readouterr().out
    assert out.count("几乎全黑") == 1 and "false,hidden" in out    # 第 10 次那一拍, 只一次


def test_lazy_theta_pathing_installs_the_canvas_hook_before_reading_the_start_position(monkeypatch):
    """2026-10-10 录像: 每次进游戏后第一次读位置都读成同一个假点 (250,45) —— 那时 canvas hook 还没装(装钩子
    只在刷怪索敌扫描里), 退回截图认小地图金点, 小地图大小跟 300x300 的假设对不上, 读出个假坐标; 路径从假起点
    规划, 人往错的方向走了 10 秒才"原地打转"卡住、脱困后位置读对才重新规划。寻路开头先装钩子, 位置就读画布。"""
    _pathing_env(monkeypatch)
    order = []
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", lambda *a, **k: order.append("hook"))
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: order.append("pos") or (20, 40))
    assert main.lazy_theta_pathing((13, 68), [AREA]) is True      # 已经在区域里, 读一次位置就返回
    assert order[0] == "hook" and "pos" in order


def test_lazy_theta_pathing_survives_a_failing_hook_install(monkeypatch):
    _pathing_env(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("canvas hook 注入没生效")
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", boom)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (20, 40))
    assert main.lazy_theta_pathing((13, 68), [AREA]) is True      # 装不上就照旧(退回截图), 不能拦住寻路


def test_lazy_theta_pathing_stops_replanning_when_nothing_moves(monkeypatch, capsys):
    """实机 2026-09-24: 刷怪区外一格的花朵被拽向 3 格外的边内点 —— 比
    move_to_position 的到达半径还近, 于是每次寻路都"成功"、一个 moveTo 都不发。
    人没进区域、stat 也不是 "stuck", 循环就原地重复同一次无效寻路: 90 次 / 118 秒,
    最后靠玩家自己飘进区域才出来。

    "永不放弃"是不许 return False, 不是不许换个招 —— 没挪窝就该去脱困再规划。
    """
    import numpy as np

    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "load_binary_map",
                        lambda: np.full((70, 60), 255, dtype=np.uint8))
    monkeypatch.setattr(main, "lazy_theta_star", lambda m, a, b: [a, b])
    monkeypatch.setattr(main, "execute_path", lambda path, **kw: True)  # "跑完了", 但人没动

    seen = {"pos": 0, "unstick": 0}

    def position():
        seen["pos"] += 1
        # 空转的话这个上限会先炸 —— 测试要**失败**, 不能挂死(挂死的变异测试什么也
        # 证明不了)。脱困之后才给一个区内的位置, 让这一趟正常收尾。
        assert seen["pos"] <= 30, "原地重规划停不下来"
        return (16, 55) if seen["unstick"] else (16, 62)

    monkeypatch.setattr(main, "get_player_position", position)
    monkeypatch.setattr(main, "execute_anti_stuck",
                        lambda: seen.__setitem__("unstick", seen["unstick"] + 1))

    assert main.lazy_theta_pathing((16, 55), [[(6, 4), (52, 61)]]) is True
    assert seen["unstick"] == 1
    assert "没挪窝" in capsys.readouterr().out


def _pathing_env(monkeypatch):
    import numpy as np

    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "load_binary_map",
                        lambda: np.full((120, 120), 255, dtype=np.uint8))


AREA = [(2, 5), (44, 73)]


def test_lazy_theta_pathing_does_nothing_when_already_inside(monkeypatch):
    """用户(2026-09-25): 不用每次都走到一个特定的点。实机每一轮开局人本来就在刷怪区
    里, 却照样往配置的 (13,68) 走, 走到那儿还"原地打转14次, 判定卡住"。"""
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))
    monkeypatch.setattr(main, "lazy_theta_star",
                        lambda *a: pytest.fail("已经在区域里了还去规划路径"))
    assert main.lazy_theta_pathing((13, 68), [AREA]) is True


def test_path_walk_hands_each_hop_the_next_one_and_the_planning_map(monkeypatch):
    hops = []
    monkeypatch.setattr(main, "move_to_position",
                        lambda a, b, **kw: hops.append((b, kw["next_pos"], kw["binary_map"])) or True)
    marker = object()
    assert main.execute_path([(0, 0), (1, 1), (2, 2), (3, 3)], binary_map=marker) is True
    assert hops == [((1, 1), (2, 2), marker), ((2, 2), (3, 3), marker), ((3, 3), None, marker)]

    _pathing_env(monkeypatch)
    pos = {"p": (20, 80)}
    maps = []

    def execute(path, **kw):
        maps.append(kw.get("binary_map"))
        pos["p"] = (20, 40)
        return True

    monkeypatch.setattr(main, "execute_path", execute)
    monkeypatch.setattr(main, "get_player_position", lambda: pos["p"])
    monkeypatch.setattr(main, "lazy_theta_star", lambda m, a, b: [a, b])
    assert main.lazy_theta_pathing((13, 40), [AREA]) is True
    assert len(maps) == 1 and maps[0].shape == (120, 120)


def test_execute_path_stops_as_soon_as_the_goal_is_reached():
    moves = []
    orig = main.move_to_position

    def move(a, b, on_tick=None, **kw):
        moves.append(b)
        return (on_tick(b) if on_tick else None) or True

    main.move_to_position = move
    try:
        assert main.execute_path([(0, 0), (1, 1), (2, 2), (3, 3)],
                                 stop_when=lambda p: p == (1, 1)) is True
    finally:
        main.move_to_position = orig
    assert moves == [(1, 1)]


def test_a_single_leg_walk_back_stops_at_the_edge_not_the_waypoint(monkeypatch):
    """实机(2026-09-27): 从下边出区一格, 拉回的路只有一段 "(24,61) -> (24,51)" ——
    原来"进区就停"只在两段之间查, 一段的路照样走到底, 往里多走 9 格。现在走的过程中
    每个 tick 都查。"""
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "_move_mouse_safely", lambda p: None)
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    ys = iter(range(61, 40, -1))                   # 每读一次往北挪一格
    seen = []

    def position():
        y = next(ys)
        seen.append(y)
        return (24, y)

    monkeypatch.setattr(main, "get_player_position", position)
    assert main.execute_path([(24, 61), (24, 51)],
                             stop_when=lambda p: main.if_in_area([[(6, 8), (43, 59)]], p)) is True
    assert seen[-1] == 59                          # 一进区(y=59)就停, 没走到 51


def test_walking_back_stops_the_moment_the_flower_is_inside(monkeypatch):
    """出区之后回到**最近的**区内就行 —— 原来要把整条路走完(走到边内点/区域中心/
    配置点), 一边走一边还在区里乱窜。"""
    _pathing_env(monkeypatch)
    pos = {"p": (20, 80)}
    moves = []

    def move(a, b, on_tick=None, **kw):
        moves.append(b)
        pos["p"] = b
        return (on_tick(b) if on_tick else None) or True

    monkeypatch.setattr(main, "move_to_position", move)
    monkeypatch.setattr(main, "get_player_position", lambda: pos["p"])
    monkeypatch.setattr(main, "lazy_theta_star",
                        lambda m, a, b: [a, (20, 72), (20, 60), (13, 40)])
    assert main.lazy_theta_pathing((13, 40), [AREA]) is True
    # (20,72) 刚踩进边界(离上沿 1 格)还不够: 再往里 AREA_ENTRY_MARGIN 格才收手, 免得惯性一滑又出区;
    # 但也不走完整条路(没走到 (13,40))
    assert moves == [(20, 72), (20, 60)]


def test_rules_farming_walks_back_to_the_nearest_edge_not_the_centre(monkeypatch):
    """规则刷怪出区原来一律回区域中心 —— 飘出一格也要横穿半张区域。"""
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 80))
    targets = []

    class Done(Exception):
        pass

    def pathing(target, areas, **kw):
        targets.append(target)
        raise Done

    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    with pytest.raises(Done):
        main.auto_farming(AREA, 30, enemy_ai_enabled=False)
    (x, y), = targets
    assert x == 20 and 60 <= y <= 73           # 正下方的边内点, 不是中心 (23, 39)


def test_lazy_theta_pathing_does_not_cry_stuck_while_it_is_actually_walking(monkeypatch):
    """"没挪窝"判据不能把正常长途赶路误判成卡住 —— 那会在每条长路上插脱困动作。"""
    import numpy as np

    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "load_binary_map",
                        lambda: np.full((70, 60), 255, dtype=np.uint8))
    monkeypatch.setattr(main, "lazy_theta_star", lambda m, a, b: [a, b])
    monkeypatch.setattr(main, "execute_path", lambda path, **kw: True)
    monkeypatch.setattr(main, "execute_anti_stuck",
                        lambda: pytest.fail("走得好好的却去脱困了"))

    walk = {"y": 62}

    def position():
        walk["y"] = max(8, walk["y"] - 6)      # 每次读都往北挪一大截
        return (16, walk["y"])

    monkeypatch.setattr(main, "get_player_position", position)
    assert main.lazy_theta_pathing((16, 8), [[(6, 4), (20, 10)]]) is True


def _stub_run_worker_env(monkeypatch, overlay=None):
    monkeypatch.setattr(main.cdp_bridge, "is_dedicated_chrome_ready", lambda: True)
    monkeypatch.setattr(main, "create_overlay",
                        lambda *a, **k: overlay or _StubOverlay())
    monkeypatch.setattr(main, "overlay", None, raising=False)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)], "farming_duration": 300,
        "short_round_limit": 2, "enemy_ai_enabled": False, "auto_switch_server": False,
        "biome": "desert",
        "map_name": "desert",
        "route": map_routes.route_for("desert"),
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    # 服务器号读取也得打桩: 不打桩的话每条用到本 fixture 的用例都会真的走到
    # cdp_bridge.eval_js —— 而真正的部署机是 Windows, 那台的 Chrome 就是带
    # --remote-debugging-port 起的, 等于让测试套往用户活着的 florr 标签页里执行 JS。
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "switch_server", lambda *a, **k: "stub-srv")
    # 生态区选择默认打桩成 no-op; 专门测它的用例自己再 re-stub.
    monkeypatch.setattr(main, "select_biome_on_title", lambda *a, **k: None)
    monkeypatch.setattr(main, "_wait_for_start_menu", lambda *a, **k: True)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "on_guest_screen", lambda: False)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)


def test_run_worker_reasserts_florr_toggles_at_startup_and_each_round(monkeypatch):
    _stub_run_worker_env(monkeypatch)
    calls = []
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ej, addr, want: calls.append((addr, want)) or ("unchanged", ""))
    # 掐在寻路 —— 它在"每轮重写"之后, 所以第 1 轮那次也算进去
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    # 启动 1 次 + 第 1 轮进游戏后 1 次 = _reassert_florr_toggles 调 2 次
    # 每次内部对 attack + defense 各调一次 ensure_flag = 4 次
    assert len(calls) == 4
    A, D = main.florr_settings.INVERT_ATTACK_ADDR, main.florr_settings.INVERT_DEFENSE_ADDR
    # _stub 的 _apply_worker_config 返回 invert_attack True → want 1; invert_defense False → want 0
    assert calls == [(A, 1), (D, 0), (A, 1), (D, 0)]


def test_run_worker_asks_windows_to_stay_awake(monkeypatch):
    # 无人值守挂机时显示器休眠 / 屏保会让输入和画面都到不了游戏(2026-10-08 第一份录像 5 分钟卡死)
    _stub_run_worker_env(monkeypatch)
    started = []
    monkeypatch.setattr(main.keep_awake, "start", lambda *a, **k: started.append(1))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert started == [1]


def test_run_worker_toggle_wants_come_from_active_slice(monkeypatch):
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)], "farming_duration": 300,
        "short_round_limit": 2, "enemy_ai_enabled": False, "auto_switch_server": False,
        "biome": "desert",
        "map_name": "desert",
        "route": map_routes.route_for("desert"),
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": False, "invert_defense": True,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    calls = []
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ej, addr, want: calls.append((addr, want)) or ("unchanged", ""))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    A, D = main.florr_settings.INVERT_ATTACK_ADDR, main.florr_settings.INVERT_DEFENSE_ADDR
    assert calls == [(A, 0), (D, 1), (A, 0), (D, 1)]


def test_run_worker_survives_toggle_failure(monkeypatch):
    """ensure_flag 返回 failed 时 worker 照常进主循环, 不 SystemExit, 悬浮窗警告."""
    ov = _StubOverlay()
    warned = []
    ov.update = lambda **kw: warned.append(kw.get("message"))
    _stub_run_worker_env(monkeypatch, overlay=ov)
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ej, addr, want: ("failed", "not-bool:9"))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):   # 到了主循环 = 没被 failed 掐死
        main.run_worker({})
    assert any(m and "反转" in m for m in warned)


def test_reassert_florr_toggles_returns_per_flag_status(monkeypatch):
    seen = []

    def fake(ej, addr, want):
        seen.append((addr, want))
        return ("changed", "") if addr == main.florr_settings.INVERT_ATTACK_ADDR else ("unchanged", "")

    monkeypatch.setattr(main.florr_settings, "ensure_flag", fake)
    out = main._reassert_florr_toggles(True, False)
    assert out == {"attack": "changed", "defense": "unchanged"}
    A, D = main.florr_settings.INVERT_ATTACK_ADDR, main.florr_settings.INVERT_DEFENSE_ADDR
    assert seen == [(A, 1), (D, 0)]


def test_run_worker_selects_biome_on_title_before_clicking_start(monkeypatch):
    """点生态区选择器必须在 click_start_game() 之前 —— 点"开始"进的是当时选中的
    生态区. 顺序: select_biome_on_title -> _wait_for_start_menu -> click_start_game."""
    _stub_run_worker_env(monkeypatch)
    events = []
    monkeypatch.setattr(main, "select_biome_on_title",
                        lambda b: events.append(("select", b)))
    monkeypatch.setattr(main, "_wait_for_start_menu",
                        lambda *a, **k: events.append("wait") or True)
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main, "click_start_game", lambda: events.append("click") or True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert events == [("select", "desert"), "wait", "click"]


def test_run_worker_selects_biome_only_once_across_respawns(monkeypatch):
    """用户实机确认: 死了之后再点一次生态区选择器会重置检查点 —— 只在本次 worker
    启动后第一次进开局菜单点一下, 后续每次重生(回到开局菜单)都不再点, 但照常点
    "开始"进游戏."""
    _stub_run_worker_env(monkeypatch)
    selects, clicks, waits = [], [], []
    monkeypatch.setattr(main, "select_biome_on_title", lambda b: selects.append(b))
    monkeypatch.setattr(main, "_wait_for_start_menu", lambda *a, **k: waits.append(1) or True)
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main, "click_start_game", lambda: clicks.append(1) or True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    n = {"i": 0}

    def pathing(*a, **k):
        n["i"] += 1
        if n["i"] >= 3:
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert selects == ["desert"]      # 3 轮都回开局菜单, 只点了第 1 轮那次
    assert len(waits) == 1            # 等菜单回来也只在那一次选择之后做
    assert len(clicks) == 3           # 每轮照常点开始, 不受影响


def _run_one_round_with_route(monkeypatch, map_name):
    """只跑一轮 run_worker(第一次寻路就 KeyboardInterrupt), 路线换成 map_name 那条.
    返回 florr_server.clear_last_server_id 收到的调用次数。"""
    _stub_run_worker_env(monkeypatch)
    base = main._apply_worker_config(None)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: dict(
        base, map_name=map_name, biome=map_routes.route_for(map_name).server_biome,
        route=map_routes.route_for(map_name)))
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main, "click_start_game", lambda: True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "_run_entry_route", lambda *a, **k: "arrived")
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "load_binary_map", lambda: None)
    cleared = []
    monkeypatch.setattr(main.florr_server, "clear_last_server_id",
                        lambda ej: cleared.append(1) or True)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    return len(cleared)


def test_run_worker_clears_the_stale_server_id_only_for_the_landing_checked_maps(monkeypatch):
    """探针记的服务器号活在页面里, worker 重启/换时块都不会没 —— 花园时块切到海洋的那一轮,
    _run_entry_route 的落地核对会读到上一个时块那台花园服务器的号, 判"按钮点偏了"白跳一轮。
    点「开始」之前清掉它。别给别的路线清: 多阶段路线每圈靠这个号分"还在花园/已经进蚁穴"。"""
    for name in ("ocean", "jungle"):
        assert _run_one_round_with_route(monkeypatch, name) == 1, name
    for name in ("desert", "garden", "anthell", "sewers", "factory"):
        assert _run_one_round_with_route(monkeypatch, name) == 0, name


def test_run_worker_does_not_select_biome_when_not_on_start_screen(monkeypatch):
    _stub_run_worker_env(monkeypatch)   # on_start_screen 恒 False
    selects = []
    monkeypatch.setattr(main, "select_biome_on_title", lambda b: selects.append(b))
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert selects == []      # 选生态区只在标题页(点开始前), 不在开局菜单就不点


def test_run_worker_switch_server_uses_configured_biome(monkeypatch):
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)], "farming_duration": 9999,
        "short_round_limit": 1, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "ocean",
        "map_name": "ocean",
        "route": map_routes.route_for("ocean"),
        "enter_game_swap": "none", "reach_area_swap": "none",
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    # 每轮都真进游戏(否则 d9592fd 后"没进游戏的轮"不计短局, 到不了换服分支)
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main, "click_start_game", lambda: True)
    monkeypatch.setattr(main, "select_biome_on_title", lambda *a, **k: None)
    monkeypatch.setattr(main, "_wait_for_start_menu", lambda *a, **k: True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda *a, **k: False)  # 没到区 -> 短局
    sw = []

    def rec(*a, **k):
        sw.append(a)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "switch_server", rec)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert sw == [("ocean",)]      # 换服分支的 switch_server 收到配置里的 biome


# ── switch_server() 后紧跟的死亡画面是重连过渡态, 不真点『继续』────────────
# 用户实机确认: 换服务器后如果检测到死亡结算画面就点『继续』, 会把进度重置到
# 检查点. 只吞换服后的第一次死亡画面, 之后照常点(不会一直卡着不点).

def test_run_worker_skips_death_click_right_after_switch_server(monkeypatch):
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)], "farming_duration": 9999,
        "short_round_limit": 1, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "desert",
        "map_name": "desert",
        "route": map_routes.route_for("desert"),
        "enter_game_swap": "none", "reach_area_swap": "none",
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "select_biome_on_title", lambda *a, **k: None)
    monkeypatch.setattr(main, "_wait_for_start_menu", lambda *a, **k: True)
    monkeypatch.setattr(main, "click_start_game", lambda: True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda *a, **k: False)

    # run_worker 在 while 循环前有一次独立的 on_guest_screen() 探测(游客页处理),
    # 会先把计数拨到 0 —— 从 -1 起步, 让循环体第 1 轮的计数正好落在 1, 对齐 round_count.
    rounds = {"n": -1}
    monkeypatch.setattr(main, "on_guest_screen",
                        lambda: rounds.__setitem__("n", rounds["n"] + 1) or False)
    # 轮1: 正常进游戏(短局 -> 触发换服). 轮2 起: 死亡画面 —— 轮2 该是换服的过渡态
    # (吞掉), 轮3 是真死亡(照常点).
    monkeypatch.setattr(main, "on_start_screen", lambda: rounds["n"] == 1)
    monkeypatch.setattr(main, "on_death_screen", lambda: rounds["n"] >= 2)

    calls = []

    def click_continue():
        calls.append(rounds["n"])
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "click_continue_after_death", click_continue)

    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert calls == [3]      # 轮2(换服刚发生)被吞掉, 轮3 才真的点了『继续』


def test_run_worker_clears_switch_flag_when_start_screen_seen_first(monkeypatch):
    """换服后如果重连直接落到标题页(没经过死亡画面), on_start_screen 分支也会清掉
    标记 —— 之后真正的死亡画面照常点, 不会被误吞."""
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)], "farming_duration": 100,
        "short_round_limit": 1, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "desert",
        "map_name": "desert",
        "route": map_routes.route_for("desert"),
        "enter_game_swap": "none", "reach_area_swap": "none",
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "select_biome_on_title", lambda *a, **k: None)
    monkeypatch.setattr(main, "_wait_for_start_menu", lambda *a, **k: True)
    monkeypatch.setattr(main, "click_start_game", lambda: True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda *a, **k: False)

    # 精心安排的 time.time() 序列, 每轮恰好 2 次调用(round_start_time / round_elapsed):
    # 轮1 elapsed=1(<100, 短局 -> 触发换服); 轮2 elapsed=1000(>=100, 算刷满 -> 不再
    # 换服, 只用来验证 on_start_screen 分支清标记); 轮3 只用到 round_start_time.
    seq = iter([0, 1, 2, 1002, 2000])
    monkeypatch.setattr(main.time, "time", lambda: next(seq, 999999))

    # run_worker 在 while 循环前有一次独立的 on_guest_screen() 探测(游客页处理),
    # 会先把计数拨到 0 —— 从 -1 起步, 让循环体第 1 轮的计数正好落在 1, 对齐 round_count.
    rounds = {"n": -1}
    monkeypatch.setattr(main, "on_guest_screen",
                        lambda: rounds.__setitem__("n", rounds["n"] + 1) or False)
    monkeypatch.setattr(main, "on_start_screen", lambda: rounds["n"] in (1, 2))
    monkeypatch.setattr(main, "on_death_screen", lambda: rounds["n"] == 3)

    calls = []

    def click_continue():
        calls.append(rounds["n"])
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "click_continue_after_death", click_continue)

    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert calls == [3]      # 轮2 的 on_start_screen 已经清过标记, 轮3 死亡正常点


# ── 未登录标题页: run_worker 自动点「以游客身份游玩」──────────────────────

def test_run_worker_clicks_play_as_guest_when_on_guest_screen(monkeypatch):
    """停在未登录登录选择页时, 启动阶段 + 第 1 轮各点一次「以游客身份游玩」."""
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ej, addr, want: ("unchanged", ""))
    monkeypatch.setattr(main, "on_guest_screen", lambda: True)
    clicks = []
    monkeypatch.setattr(main, "click_play_as_guest", lambda: clicks.append(1))
    # 掐在寻路 —— 启动那次 + 第 1 轮那次都已经跑过了
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert len(clicks) == 2                       # 启动前 1 次 + 第 1 轮顶部 1 次


def test_run_worker_waits_for_start_menu_after_guest_click_not_a_fixed_sleep(monkeypatch):
    """实机复盘(2026-09-16): 点游客身份后以前是死等 sleep(2), 标题页没渲染完的话
    round loop 顶部三个画面检测全部落空, 代码当成"已经在游戏里"直接冲进
    _run_entry_route 空转找位置, 一路重试到超时才发现自己还在开局菜单——白白
    浪费一整轮. 改成轮询确认(_wait_for_start_menu)之后: 游客点击 -> 确认到了
    开局菜单 -> 本轮顶部紧接着的 on_start_screen() 检测就该命中, 同一轮直接
    点"开始"进游戏, 不需要多等一整轮.
    """
    _stub_run_worker_env(monkeypatch)
    events = []
    monkeypatch.setattr(main, "on_guest_screen", lambda: True)
    monkeypatch.setattr(main, "click_play_as_guest",
                        lambda: events.append("click_guest"))
    monkeypatch.setattr(main, "_wait_for_start_menu",
                        lambda *a, **k: events.append("wait") or True)
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main, "click_start_game",
                        lambda: events.append("click_start") or True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    # 启动阶段那次(run_worker 里 while 循环前)先 click_guest+wait; 第 1 轮顶部
    # 因为 on_guest_screen 恒 True, 又点一次 click_guest+wait —— 到这里就已经确认
    # 到了开局菜单; 紧接着 on_start_screen() 命中, 走进"点击开始按钮"那支已有的
    # select_biome_on_title -> _wait_for_start_menu -> click_start_game 流程(那处
    # 的 wait 是既有逻辑, 不是这次改的), 同一轮就点了 click_start —— 关键是
    # "同一轮", 不是等到下一轮才点.
    assert events == ["click_guest", "wait", "click_guest", "wait", "wait", "click_start"]


def test_run_worker_guest_click_wait_timeout_does_not_crash(monkeypatch):
    # 15 秒还是没等到开局菜单(florr 这次是真的卡住/慢) —— 只打个警告, 继续往下走,
    # 靠主循环自己纠正, 不能让 worker 直接炸.
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "on_guest_screen", lambda: True)
    monkeypatch.setattr(main, "click_play_as_guest", lambda: None)
    monkeypatch.setattr(main, "_wait_for_start_menu", lambda *a, **k: False)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})   # 没有别的异常炸出来就算过


def test_run_worker_never_clicks_guest_when_not_on_guest_screen(monkeypatch):
    """登录过的 profile 的常态: on_guest_screen 恒 False, 一次都不点."""
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ej, addr, want: ("unchanged", ""))
    monkeypatch.setattr(main, "on_guest_screen", lambda: False)
    monkeypatch.setattr(main, "click_play_as_guest",
                        lambda: pytest.fail("不在游客页不该点「以游客身份游玩」"))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})


def test_apply_worker_config_reads_swap_keys(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    e = {"enabled": True, "mod": "k", "digit": "3"}
    r = {"enabled": True, "mod": "none", "digit": "7"}
    cfg = {"version": 2, "active": {"map": "desert",
                                    "enter_game_swap": e, "reach_area_swap": r}}
    w = main._apply_worker_config(cfg)
    assert w["enter_game_swap"] == e
    assert w["reach_area_swap"] == r


def test_apply_worker_config_swap_keys_default_disabled(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    off = {"enabled": False, "mod": "none", "digit": "1"}
    w = main._apply_worker_config({"version": 2, "active": {"map": "desert"}})
    assert w["enter_game_swap"] == off
    assert w["reach_area_swap"] == off


def _swap_env(monkeypatch, *, enter="k", reach="l"):
    """_stub_run_worker_env + 记录 press_swap 调用 + _apply_worker_config 带 swap 键.

    enter/reach 是不透明哨兵 —— main.loadout_swap.press_swap 被 stub 成"记下参数",
    真按键逻辑(和弦 / 对象形状)由 test_loadout_swap.py 覆盖. 这里只关心"哪个字段的
    值被传给了 press_swap、顺序、以及 swap_this_round 门控".
    """
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)], "farming_duration": 300,
        "short_round_limit": 2, "enemy_ai_enabled": False, "auto_switch_server": False,
        "biome": "desert",
        "map_name": "desert",
        "route": map_routes.route_for("desert"),
        "enter_game_swap": enter, "reach_area_swap": reach,
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ej, addr, want: ("unchanged", ""))
    seen = []
    monkeypatch.setattr(main.loadout_swap, "press_swap", lambda spec: seen.append(spec))
    return seen


def _location_check_env(monkeypatch, *, binary_map, location=(1, 2)):
    """_stub_run_worker_env + 指定 location 和 load_binary_map() 的返回值, 记录
    lazy_theta_pathing 实际收到的 target —— 专测"配置的 location 落在墙上时该
    退回 random_walkable_point()" 这条新逻辑, 不跟真实 maps/desert.png 的内容
    绑定(不然测试结果取决于那张图长什么样, 不是这条逻辑本身)."""
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": location, "farming_area": [(0, 0), (9, 9)], "farming_duration": 300,
        "short_round_limit": 2, "enemy_ai_enabled": False, "auto_switch_server": False,
        "biome": "desert",
        "map_name": "desert",
        "route": map_routes.route_for("desert"),
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "load_binary_map", lambda: binary_map, raising=False)
    targets = []

    def pathing(target, areas, **kw):
        targets.append(target)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    return targets


def test_run_worker_uses_configured_location_when_it_is_walkable(monkeypatch):
    binary_map = np.zeros((10, 10), dtype=np.uint8)
    binary_map[2, 1] = 255   # location=(1,2) 在 binary_map[y=2,x=1] 上是可走的
    targets = _location_check_env(monkeypatch, binary_map=binary_map)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert targets == [(1, 2)]


def test_run_worker_falls_back_to_random_walkable_point_when_location_is_a_wall(monkeypatch):
    """实机复盘(2026-09-20): 蚁穴这条路线第一次真的进场成功后, 配置的 location
    落在墙上, lazy_theta_pathing 每轮都"路径规划失败", 一轮都刷不到。这条锁定
    修复: 配置的点不可走时退回 random_walkable_point() 在 farming_area 内重新
    采样, 不是死磕一个打错的坐标.
    """
    binary_map = np.zeros((10, 10), dtype=np.uint8)   # 全墙, location=(1,2) 落在墙上
    fallback = (7, 7)
    calls = []

    def fake_random_point(area, bm):
        calls.append((area, bm))
        return fallback

    monkeypatch.setattr(main, "random_walkable_point", fake_random_point)
    targets = _location_check_env(monkeypatch, binary_map=binary_map)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert targets == [fallback]
    assert calls == [([(0, 0), (9, 9)], binary_map)]


def test_run_worker_skips_location_validation_when_binary_map_unavailable(monkeypatch):
    # 地图还没截(load_binary_map() 返回 None) —— 不该崩, 原样把配置的 location
    # 传下去(退回改之前的行为, 不会更差).
    targets = _location_check_env(monkeypatch, binary_map=None)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert targets == [(1, 2)]


def test_run_worker_presses_enter_swap_on_entry(monkeypatch):
    # 第 1 轮总会切 (round_count == 1); 这里第 1 轮就在寻路处掐断, 只证明
    # "进了游戏 → 按 enter swap", 没到区域 → reach 不按.
    seen = _swap_env(monkeypatch, enter="k", reach="l")
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == ["k"]                 # 进游戏切换按了; 没到区域, reach 没按


def test_run_worker_presses_reach_swap_on_arrival(monkeypatch):
    seen = _swap_env(monkeypatch, enter="k", reach="l")
    calls = {"n": 0}

    def fake_path(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return True
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "lazy_theta_pathing", fake_path)
    monkeypatch.setattr(main, "auto_farming",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == ["k", "l"]            # enter 先, 到区域后 reach


def test_run_worker_skips_reach_swap_when_pathing_fails(monkeypatch):
    seen = _swap_env(monkeypatch, enter="k", reach="l")
    calls = {"n": 0}

    def fake_path(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "lazy_theta_pathing", fake_path)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert "l" not in seen               # 没到区域 → reach 永不触发
    assert seen == ["k"]                 # 第 1 轮进游戏切一次; 第 2 轮没重进游戏
                                         # (_stub 里 on_start/on_death 恒 False) → 不切


def test_run_worker_puts_the_damage_loadout_on_when_a_later_round_finally_arrives(monkeypatch):
    # 第 1 轮进游戏穿上跑路装, 但寻路没到刷怪区(路径规划失败 / 被传去别的图 / 进场被打断,
    # 人都没死); 第 2 轮人还活着, entered_game 是假的 —— 原来的门只看"这一轮是不是刚进游戏",
    # 到区了也不按输出装, 花朵穿着跑路装刷到下次死。门要看"跑路装还穿着没换回来"。
    seen = _swap_env(monkeypatch, enter="k", reach="l")
    calls = {"n": 0}

    def fake_path(*a, **k):
        calls["n"] += 1
        return calls["n"] >= 2          # 第 1 轮没到, 第 2 轮到了

    monkeypatch.setattr(main, "lazy_theta_pathing", fake_path)
    monkeypatch.setattr(main, "auto_farming",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == ["k", "l"]           # 第 1 轮穿跑路装; 第 2 轮到区 -> 换回输出装


def test_run_worker_does_not_press_the_damage_loadout_twice_for_one_run_loadout(monkeypatch):
    # 配对: 一次跑路装只换回一次。到区换过之后, 续命轮(没死没重进)再到区不能重复按 ——
    # 非对称配置每轮漂移(见 test_run_worker_skips_swaps_on_survived_continuation_round)
    seen = _swap_env(monkeypatch, enter="k", reach="l")
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda *a, **k: True)
    calls = {"n": 0}

    def fake_farm(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(main, "auto_farming", fake_farm)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == ["k", "l"]           # 三轮, 只有第 1 轮一对


def test_run_worker_skips_swaps_on_survived_continuation_round(monkeypatch):
    # 一命跑满 farming_duration 没死: 下一轮不过 on_death/on_start 分支, florr 没
    # 重置 loadout, 不该再切. auto_farming 第 1 次正常返回, 第 2 次掐断.
    seen = _swap_env(monkeypatch, enter="k", reach="l")
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda *a, **k: True)
    calls = {"n": 0}

    def fake_farm(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "auto_farming", fake_farm)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == ["k", "l"]            # 只有第 1 轮切; 第 2 轮存活续命轮不切


def test_run_worker_swaps_again_after_real_respawn(monkeypatch):
    # 每轮都真的过 on_start_screen 分支 (= 真重进游戏) → 每轮都该切 enter swap,
    # 证明 gate 是"这轮真进了游戏"而不是"仅第 1 轮".
    seen = _swap_env(monkeypatch, enter="k", reach="l")
    monkeypatch.setattr(main, "click_start_game", lambda: None)
    # 锁生态区那段会额外反复轮询 on_start_screen —— stub 掉, 让计数器只被主循环
    # 体每轮那一次 on_start_screen() 推进.
    monkeypatch.setattr(main, "select_biome_on_title", lambda *a, **k: None)
    monkeypatch.setattr(main, "_wait_for_start_menu", lambda *a, **k: True)
    starts = {"n": 0}

    def fake_start_screen():
        starts["n"] += 1
        return starts["n"] <= 2          # 第 1、2 轮都在开局菜单

    monkeypatch.setattr(main, "on_start_screen", fake_start_screen)
    calls = {"n": 0}

    def fake_path(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "lazy_theta_pathing", fake_path)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == ["k", "k"]            # 两轮都真重进了游戏 → 两轮都切 enter


def test_run_worker_marks_a_loadout_baseline_on_every_real_reentry(monkeypatch):
    """florr 重进游戏会把 loadout 重置 —— 就算时块没配预设和弦、一个键都没按。RL 环境
    靠这个把 R 的奇偶归零; 漏了的话, 两局之间死过一次, 下一局的奇偶观测就是错的。"""
    _swap_env(monkeypatch, enter="k", reach="l")
    marks = []
    monkeypatch.setattr(main.loadout_swap, "mark_baseline", lambda: marks.append(1))
    monkeypatch.setattr(main, "click_start_game", lambda: None)
    monkeypatch.setattr(main, "select_biome_on_title", lambda *a, **k: None)
    monkeypatch.setattr(main, "_wait_for_start_menu", lambda *a, **k: True)
    starts = {"n": 0}

    def fake_start_screen():
        starts["n"] += 1
        return starts["n"] <= 2          # 第 1、2 轮都从开局菜单重进

    monkeypatch.setattr(main, "on_start_screen", fake_start_screen)
    calls = {"n": 0}

    def fake_path(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            return False
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "lazy_theta_pathing", fake_path)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert len(marks) == 2               # 第 3 轮没重进 -> 不算


class TestWorkerStdinWatcher:
    """GUI 靠"关掉 worker 的 stdin 管道"来请求停止, 所以 worker 读到 EOF 就退出.
    但 systemd 默认把 stdin 接到 /dev/null —— 一读就是 EOF, worker 起来瞬间自杀.

    判断方向很关键: 排除的是"字符设备"(tty / /dev/null), 而不是只认 FIFO. 因为
    Windows 才是真正的生产环境, 那边 GUI 用 subprocess.PIPE 拉 worker, 而
    os.fstat 对 Windows 管道具体报什么 st_mode 没在实机上验过 —— 只认 FIFO 的
    话一旦报的不是 FIFO, GUI 的停止按钮就退化成强杀, 按住的键不会松开. 反过来
    写"不是字符设备就装", 任何拿不准的情况都按老行为走, Windows 不受影响."""

    @pytest.fixture
    def started(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main.threading, "Thread",
                            lambda **kw: type("T", (), {"start": lambda s: calls.append(kw)})())
        return calls

    def test_not_installed_when_stdin_is_devnull(self, monkeypatch, started):
        """systemd StandardInput=null 就是这个 —— 原来的 bug 现场."""
        with open(os.devnull) as f:
            monkeypatch.setattr(main.sys, "stdin", f)
            main._install_worker_stdin_watcher()
        assert started == []

    def test_not_installed_when_stdin_is_a_tty(self, monkeypatch, started):
        """终端里直接 `python main.py --worker` 调试时用 Ctrl-C 停, 不该靠 EOF."""
        master, slave = os.openpty()
        try:
            with os.fdopen(slave) as f:
                monkeypatch.setattr(main.sys, "stdin", f)
                main._install_worker_stdin_watcher()
        finally:
            os.close(master)
        assert started == []

    def test_installed_when_stdin_is_a_pipe(self, monkeypatch, started):
        """GUI 拉 worker 的情况(subprocess.PIPE). 这条必须装, 否则停止按钮失灵."""
        r_fd, w_fd = os.pipe()
        try:
            with os.fdopen(r_fd) as f:
                monkeypatch.setattr(main.sys, "stdin", f)
                main._install_worker_stdin_watcher()
        finally:
            os.close(w_fd)
        assert len(started) == 1

    def test_installed_when_fstat_reports_something_unexpected(self, monkeypatch, started):
        """拿不准就按老行为装 —— 这条锁住"对 Windows 保守"这个方向. 只认 FIFO
        的写法会让这个用例变成"不装", 那正是 Windows 上可能踩的坑."""
        class Weird:
            def fileno(self):
                return 0

        monkeypatch.setattr(main.sys, "stdin", Weird())
        monkeypatch.setattr(main.os, "fstat",
                            lambda fd: os.stat_result((0,) * 10))   # st_mode = 0
        main._install_worker_stdin_watcher()
        assert len(started) == 1

    def test_not_installed_when_stdin_is_none(self, monkeypatch, started):
        monkeypatch.setattr(main.sys, "stdin", None)
        main._install_worker_stdin_watcher()
        assert started == []

    def test_not_installed_when_stdin_has_no_real_fd(self, monkeypatch, started):
        """打包成 console=False 的 exe 里 stdin 可能没有真 fd. 老代码这时会装,
        然后 read() 立刻抛异常 → os._exit(0), worker 起来就死. 不装才对."""
        class NoFd:
            def fileno(self):
                raise OSError("no fd")

        monkeypatch.setattr(main.sys, "stdin", NoFd())
        main._install_worker_stdin_watcher()
        assert started == []


class TestRunLaunchChrome:
    """`main.py --launch-chrome`: 无头服务器上唯一能把专用 Chrome 拉起来的入口.
    GUI 那条路(gui_app._enter_block)在没有 X 的机器上根本起不来, 而
    run_worker() 在 Chrome 没就绪时直接 sys.exit(1)."""

    @pytest.fixture
    def cfg(self):
        return {"profiles": [{"alias": "默认", "dir": "chrome-profiles/默认"},
                             {"alias": "小号2", "dir": "chrome-profiles/小号2"}]}

    def test_launches_named_profile_fullscreen_at_florr(self, monkeypatch, cfg):
        seen = {}
        monkeypatch.setattr(main.app_config, "abs_profile_path", lambda d: f"/srv/{d}")
        monkeypatch.setattr(main.os.path, "isdir", lambda p: True)
        monkeypatch.setattr(main.cdp_bridge, "launch_chrome_for_profile",
                            lambda d, **kw: seen.update(dir=d, **kw))
        monkeypatch.setattr(main.cdp_bridge, "wait_for_florr_tab",
                            lambda t: {"id": "1"})
        assert main.run_launch_chrome(cfg, alias="小号2") == 0
        assert seen["dir"] == "/srv/chrome-profiles/小号2"
        assert seen["fullscreen"] is True
        assert seen["open_url"] == "https://florr.io"

    def test_defaults_to_first_profile_when_no_alias_given(self, monkeypatch, cfg):
        seen = {}
        monkeypatch.setattr(main.app_config, "abs_profile_path", lambda d: f"/srv/{d}")
        monkeypatch.setattr(main.os.path, "isdir", lambda p: True)
        monkeypatch.setattr(main.cdp_bridge, "launch_chrome_for_profile",
                            lambda d, **kw: seen.update(dir=d))
        monkeypatch.setattr(main.cdp_bridge, "wait_for_florr_tab", lambda t: {"id": "1"})
        assert main.run_launch_chrome(cfg) == 0
        assert seen["dir"] == "/srv/chrome-profiles/默认"

    def test_unknown_alias_fails_without_launching(self, monkeypatch, cfg):
        launched = []
        monkeypatch.setattr(main.cdp_bridge, "launch_chrome_for_profile",
                            lambda *a, **k: launched.append(1))
        assert main.run_launch_chrome(cfg, alias="没有这个号") == 1
        assert launched == []

    def test_never_logged_in_profile_fails_without_launching(self, monkeypatch, cfg):
        """profile 目录不存在 = 这个号从没在本机登录过. 硬拉起来只会停在登录页,
        worker 随后白等 —— 提前报错, 跟 gui_app._enter_block 的判断一致."""
        launched = []
        monkeypatch.setattr(main.app_config, "abs_profile_path", lambda d: f"/srv/{d}")
        monkeypatch.setattr(main.os.path, "isdir", lambda p: False)
        monkeypatch.setattr(main.cdp_bridge, "launch_chrome_for_profile",
                            lambda *a, **k: launched.append(1))
        assert main.run_launch_chrome(cfg, alias="默认") == 1
        assert launched == []

    def test_chrome_missing_reports_failure(self, monkeypatch, cfg):
        monkeypatch.setattr(main.app_config, "abs_profile_path", lambda d: f"/srv/{d}")
        monkeypatch.setattr(main.os.path, "isdir", lambda p: True)

        def boom(*a, **k):
            raise RuntimeError("没找到 Chrome")

        monkeypatch.setattr(main.cdp_bridge, "launch_chrome_for_profile", boom)
        assert main.run_launch_chrome(cfg, alias="默认") == 1

    def test_florr_tab_never_appears_reports_failure(self, monkeypatch, cfg):
        monkeypatch.setattr(main.app_config, "abs_profile_path", lambda d: f"/srv/{d}")
        monkeypatch.setattr(main.os.path, "isdir", lambda p: True)
        monkeypatch.setattr(main.cdp_bridge, "launch_chrome_for_profile",
                            lambda *a, **k: None)
        monkeypatch.setattr(main.cdp_bridge, "wait_for_florr_tab", lambda t: None)
        assert main.run_launch_chrome(cfg, alias="默认") == 1

    def test_passes_timeout_through_to_tab_wait(self, monkeypatch, cfg):
        seen = {}
        monkeypatch.setattr(main.app_config, "abs_profile_path", lambda d: f"/srv/{d}")
        monkeypatch.setattr(main.os.path, "isdir", lambda p: True)
        monkeypatch.setattr(main.cdp_bridge, "launch_chrome_for_profile",
                            lambda *a, **k: None)
        monkeypatch.setattr(main.cdp_bridge, "wait_for_florr_tab",
                            lambda t: seen.setdefault("timeout", t) and {"id": "1"})
        main.run_launch_chrome(cfg, alias="默认", timeout=90)
        assert seen["timeout"] == 90
# ── lazy_theta_pathing 的可选预算 (deadline) ───────────────────────────────

def _stub_pathing_env(monkeypatch):
    """把 lazy_theta_pathing 的实机依赖全部打桩掉, 并换上一个假时钟:
    time.sleep(s) 直接把钟往前拨 s 秒, 所以"重试一整天"在测试里是瞬间的。
    返回那个钟(单元素列表), 用例可以读它/往前拨。"""
    clock = [1000.0]
    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    monkeypatch.setattr(main.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    return clock


def test_lazy_theta_pathing_without_a_deadline_never_gives_up(monkeypatch):
    """不传 deadline = 今天的行为一字不变: 测不到玩家位置就无限重试。

    刷怪那条路上的"永不放弃"是仓库主人当年特意从上游恢复回来的决定
    ([[stuck-retry-vs-upstream]]), 加 deadline 参数绝不能把它削掉。
    """
    clock = _stub_pathing_env(monkeypatch)
    tries = []

    def never_found(*a, **k):
        tries.append(clock[0])
        if len(tries) >= 30:        # 保险丝: 早就越过"要是有预算"的那个时刻了
            raise KeyboardInterrupt
        return None

    monkeypatch.setattr(main, "get_player_position", never_found, raising=False)
    with pytest.raises(KeyboardInterrupt):   # 只有保险丝能让它停 = 它真的不放弃
        main.lazy_theta_pathing((1, 2))
    assert clock[0] - 1000.0 >= 25           # 假时钟已经走过 25 秒还在重试


def test_lazy_theta_pathing_gives_up_once_the_deadline_has_passed(monkeypatch):
    # 传了 deadline 且已经过期 -> 立刻 False, 连一次位置检测都不做.
    clock = _stub_pathing_env(monkeypatch)
    monkeypatch.setattr(
        main, "get_player_position",
        lambda *a, **k: pytest.fail("预算已用完, 不该再去找玩家位置"), raising=False)
    assert main.lazy_theta_pathing((1, 2), deadline=clock[0] - 1) is False


def test_lazy_theta_pathing_does_not_start_another_anti_stuck_past_the_deadline(monkeypatch):
    # 卡住 -> 脱困 -> 重新规划 是这个函数里唯一一条"无限循环"的路; 预算用完之后
    # 不该再开一次脱困动作(execute_anti_stuck 本身还要花好几秒).
    clock = _stub_pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (5, 5), raising=False)
    monkeypatch.setattr(main, "load_binary_map", lambda: object(), raising=False)
    monkeypatch.setattr(main, "lazy_theta_star", lambda m, s, g: [(5, 5), (1, 2)])
    monkeypatch.setattr(main, "if_in_area", lambda area, pos: False, raising=False)
    escapes = []
    monkeypatch.setattr(main, "execute_anti_stuck",
                        lambda *a, **k: escapes.append(clock[0]), raising=False)

    def execute(path, **kw):
        clock[0] += 10          # 这一趟路就把预算走完了
        return "stuck"

    monkeypatch.setattr(main, "execute_path", execute)
    assert main.lazy_theta_pathing((1, 2), deadline=clock[0] + 5) is False
    assert escapes == []        # 预算用完了就不该再脱一次困


# ── 进场路线: 蚁穴要先进花园、跑到洞口、踩上去传送 ──────────────────────────

def _anthell_route(monkeypatch, portal=(150, 40), portal_world=None):
    """portal_world 默认 None(= 这条路线的 canvas 世界坐标复查层直接关掉) ——
    大多数用例测的是像素判据那一层, 不该被 canvas 层的行为(哪怕只是"读不到,
    退化成 None")悄悄影响; 想测 canvas 层的用例自己传具体坐标进来.
    """
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", portal)
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL_WORLD", portal_world)
    return map_routes.route_for("anthell")


def _entry_route_with_small_budget(monkeypatch, timeout=5):
    """run_worker 里跑的还是**真的** _run_entry_route, 只是把 180 秒的默认预算换成
    几秒 —— 回归时用例要立刻失败, 不能挂三分钟(挂死会被当成 CI 抽风)。
    run_worker 不传 timeout, 而默认值在 def 时就绑死了, 所以只能在这层包一下。"""
    real = main._run_entry_route
    monkeypatch.setattr(main, "_run_entry_route",
                        lambda route, st, timeout=timeout, **kw: real(route, st, timeout, **kw))


def _quiet_entry_env(monkeypatch):
    """_run_entry_route 会碰到的副作用全部打桩掉。

    get_player_position 默认 None(= 复查"测不到玩家") / time.sleep 默认 no-op:
    等价于旧的"无条件推进"语义 —— 不关心传送验证细节的用例不用额外打桩;
    专门测验证行为的用例(test_entry_route_holds_back_when_still_in_target_box
    那一类)自己再覆盖.

    execute_anti_stuck 也打桩成 no-op(2026-09-20新增用法: "还在原地重新贴近"
    这条路径现在会顺手补一脚 execute_anti_stuck 破僵局) —— 那是 utils.py
    自己的 time.sleep, 不受这里 main.time.sleep 那行打桩影响, 真跑起来会
    每次卡住都真等1.5秒外加真的截一次图, 拖慢一大批不关心这个机制细节的
    用例; 专门测这个机制接没接上的用例(test_entry_route_kicks_anti_stuck_
    那一类)自己再覆盖.
    """
    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(main, "execute_anti_stuck", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: None, raising=False)
    monkeypatch.setattr(main, "_canvas_scale_zone", lambda r: None, raising=False)


def test_maybe_scan_enemies_passes_target_policy_for_current_map(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: [])
    monkeypatch.setattr(main.enemy_detect, "select_action",
                        lambda dets, **k: seen.update(k) or ("wander", None))
    now = main.ENEMY_SCAN_INTERVAL + 1.0
    for map_name, want in (("anthell", "nearest"), ("desert", "priority")):
        monkeypatch.setattr(main.utils, "MAP", map_name)
        main._maybe_scan_enemies(True, now, 0.0, ("wander", None), [])
        assert seen["target_policy"] == want


def test_apply_worker_config_force_disables_enemy_ai_on_a_map_without_species(monkeypatch):
    # 现在七张图都有物种表; "没做索敌的图要强制关"这条机制靠合成一张空表图来测 ——
    # priority_score() 对表外 slug 会 KeyError, 不能指望 config 里不写 true.
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    monkeypatch.setitem(main.enemy_detect.MAP_SPECIES, "ocean", frozenset())
    w = main._apply_worker_config(
        {"version": 2, "active": {"map": "ocean", "enemy_ai_enabled": True}})
    assert w["enemy_ai_enabled"] is False


@pytest.mark.parametrize("name", ["garden", "desert", "ocean", "jungle", "anthell",
                                  "sewers", "factory"])
def test_apply_worker_config_keeps_enemy_ai_on_for_every_map_with_species(monkeypatch, name):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    on = main._apply_worker_config(
        {"version": 2, "active": {"map": name, "enemy_ai_enabled": True}})
    assert on["enemy_ai_enabled"] is True
    off = main._apply_worker_config(
        {"version": 2, "active": {"map": name, "enemy_ai_enabled": False}})
    assert off["enemy_ai_enabled"] is False


def test_route_blocker_names_the_route_when_a_portal_is_uncalibrated(monkeypatch):
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", None)
    msg = main._route_blocker(map_routes.route_for("anthell"))
    assert msg is not None and "未标定" in msg and "anthell" in msg


def test_route_blocker_is_none_for_every_shipped_map():
    for name in ("garden", "desert", "ocean", "jungle", "anthell", "sewers", "factory"):
        assert main._route_blocker(map_routes.route_for(name)) is None, name


def test_route_blocker_refuses_a_door_that_has_no_registered_opening(monkeypatch):
    """重标了 *_PORTAL 却忘了改 PORTAL_OPENINGS: 门在二值图里还是墙, lazy_theta_star 只会
    报笼统的"路径规划失败"一轮轮空转 —— 谁也看不出是漏登记。启动时就得拦住并点名那张表。"""
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", (120, 120))    # 不是 PORTAL_OPENINGS 的键
    msg = main._route_blocker(map_routes.route_for("anthell"))
    assert msg is not None and "PORTAL_OPENINGS" in msg and "(120, 120)" in msg


def test_route_blocker_recalibration_recipe_never_tells_you_to_overwrite_the_garden_map(
        monkeypatch):
    # `capture_map.py garden` 会把仓库自带的 maps/garden.png(门全是墙那张)覆盖掉 ——
    # 三处文案以前都这么写。现在统一用 map_routes 那一份步骤。
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", None)
    msg = main._route_blocker(map_routes.route_for("anthell"))
    assert "capture_map.py garden`" not in msg and "capture_map.py garden " not in msg
    assert "git checkout -- maps/garden.png" in msg
    assert "map_select.py" in msg and "PORTAL_OPENINGS" in msg


def test_route_blocker_tells_you_to_restore_a_missing_shipped_map(monkeypatch):
    real_exists = main.os.path.exists
    monkeypatch.setattr(main.os.path, "exists",
                        lambda p: False if str(p).endswith("jungle.png") else real_exists(p))
    msg = main._route_blocker(map_routes.route_for("jungle"))
    assert msg is not None and "git checkout" in msg


def test_route_blocker_reports_missing_map_png(monkeypatch, tmp_path):
    route = _anthell_route(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    (tmp_path / "maps" / "anthell.png").write_bytes(b"x")
    blocker = main._route_blocker(route)
    assert blocker is not None and "garden.png" in blocker


def test_route_blocker_does_not_tell_you_to_recapture_the_shipped_anthell_map(
        monkeypatch, tmp_path):
    # maps/anthell.png 是上游初始提交里手工做好的图, 不是 capture_map.py 采出来的。
    # 它要是没了, 照着"在该图局内跑 capture_map.py anthell"去做, 正好会把那张手工图
    # 覆盖掉 —— 而且还得先进得了蚁穴才能采, 是个死循环。
    route = _anthell_route(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    (tmp_path / "maps" / "garden.png").write_bytes(b"x")     # 只缺 anthell.png
    blocker = main._route_blocker(route)
    assert blocker is not None and "anthell.png" in blocker
    assert "python capture_map.py anthell" not in blocker    # 别叫人去重采
    assert "git checkout" in blocker                         # 指向"从仓库恢复"


def test_route_blocker_is_none_when_everything_is_there(monkeypatch, tmp_path):
    route = _anthell_route(monkeypatch)
    # 挪过洞口坐标就得连着登记这个门运行时要挖开的像素, 不然 _route_blocker 会(正确地)拦下来
    monkeypatch.setitem(map_routes.PORTAL_OPENINGS, ("garden", (150, 40)),
                        [(149, 39, 151, 41)])
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    for name in ("garden.png", "anthell.png"):
        (tmp_path / "maps" / name).write_bytes(b"x")
    assert main._route_blocker(route) is None


def test_run_worker_blocked_route_exits_before_any_screen_side_effect(monkeypatch, capsys):
    """待标定值没填的蚁狱时块 = worker 启动即退出, 而 GUI 把**任何**退出都当崩溃
    (gui_app._on_worker_exit 清掉 _running_block_id), 调度器每 _TICK_MS(30 秒)就把
    这个时块重新拉起一次, 整个时段都这么循环。

    所以这道闸门必须排在一切副作用**之前**: 排在 create_overlay() 后面就是每 30 秒
    建一个 AppKit 悬浮窗再拆掉; 排在游客登录分支后面还会每 30 秒往屏幕上真点一下
    鼠标。悬浮窗上写的那句话也没人看得见 —— 进程紧接着就退出了。
    """
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", None)   # 洞口未标定
    monkeypatch.setattr(main, "apply_map", lambda n: None)

    def forbidden(what):
        return lambda *a, **k: pytest.fail(f"{what} 不该在路线闸门之前跑")

    monkeypatch.setattr(main.cdp_bridge, "is_dedicated_chrome_ready",
                        forbidden("Chrome 就绪检查"))
    monkeypatch.setattr(main, "create_overlay", forbidden("create_overlay"))
    monkeypatch.setattr(main, "on_guest_screen", forbidden("on_guest_screen"))
    monkeypatch.setattr(main, "click_play_as_guest", forbidden("click_play_as_guest"))

    with pytest.raises(SystemExit) as excinfo:
        main.run_worker({"version": 2, "active": {"map": "anthell"}})
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "未标定" in out and "anthell" in out   # 明说缺哪个值、哪条路线


def test_stage_state_advances_and_resets(monkeypatch):
    route = _anthell_route(monkeypatch)
    st = main._StageState(route)
    assert st.map_name == "garden"
    st.advance()
    assert st.map_name == "anthell"
    st.advance()                       # 已经在最后一段, 不越界
    assert st.map_name == "anthell"
    st.reset()
    assert st.map_name == "garden"


def test_stage_state_past(monkeypatch):
    route = _anthell_route(monkeypatch)
    st = main._StageState(route)
    assert not st.past("garden")          # 还在第一段: 谁都不在"之后"
    st.advance()
    assert st.past("garden")              # 已经推进到蚁穴: 花园在身后
    assert not st.past("anthell")         # 当前段不算
    assert not st.past("desert")          # 不在路线上 -> False


def test_entry_route_trusts_stage_state_when_zone_unreadable_and_server_says_garden(monkeypatch):
    """工厂/蚁穴/下水道都跑在花园那台服务器上, 服务器号永远说"花园"。区域名读不出时(1024x768 下
    hook 把它丢了 90% 的帧), 人明明重生在工厂的检查点, 原来拿服务器号当依据 -> 在工厂里走花园的路线
    180 秒(2026-10-11 实机一次死亡丢 2-3 分钟)。阶段状态已经推进到工厂时, 该信它。"""
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("factory")
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    walked = []
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc))
    st = main._StageState(route)
    st.advance()                           # 上一命就在工厂里
    st.carry()                             # 死亡重生带过来的阶段
    assert main._run_entry_route(route, st, timeout=5) == "arrived"
    assert walked == []                    # 一步花园的路都不该走


def test_stage_state_carried_flag_lives_only_until_the_next_transition(monkeypatch):
    route = map_routes.route_for("factory")
    st = main._StageState(route)
    assert not st.carried
    st.advance()
    st.carry()
    assert st.carried
    st.sync_to("factory")                  # 区域名读到了 -> 以画面为准, 不再是"带过来的"
    assert not st.carried
    st.carry()
    st.reset()
    assert not st.carried
    st.advance()
    st.carry()
    st.advance()                           # 已经在最后一段, advance 也清掉带过来的标记
    assert not st.carried


def test_entry_route_does_not_trust_an_optimistic_stage_even_when_ahead_of_the_server_id(monkeypatch):
    """没带过来的阶段(乐观推进猜的)比服务器号靠前时, 服务器号说了算: 猜的可能是假的。"""
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("factory")
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    walked = []
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    st = main._StageState(route)
    st.advance()                           # 没 carry
    main._run_entry_route(route, st, timeout=0.05)
    assert walked and walked[0] == route.stage_for("garden").walk_to


def test_entry_route_still_walks_the_garden_when_state_is_at_the_first_stage(monkeypatch):
    """反过来: 阶段状态在第一段(冷启动/换服后 reset), 服务器号说花园, 区域名读不出 -> 照旧走花园
    那段到传送点。信阶段状态只在它比服务器号靠前时。"""
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("factory")
    reported = iter(["garden", "garden", "garden"])
    monkeypatch.setattr(main.florr_server, "current_map_name",
                        lambda ev: next(reported, "garden"))
    walked = []
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    # 走到传送点 -> 复查"已离开" -> 推进到工厂 -> arrived (走的是花园那段路)
    assert main._run_entry_route(route, main._StageState(route), timeout=5) == "arrived"
    assert walked and walked[0] == route.stage_for("garden").walk_to


def test_entry_route_zone_label_beats_stage_state(monkeypatch):
    """区域名读得到就以它为准: 阶段状态说工厂, 画面却说花园(被门传走了) -> 按花园走。"""
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("factory")
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: "garden")
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    walked = []
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    st = main._StageState(route)
    st.advance()
    assert main._run_entry_route(route, st, timeout=0.05) == "timeout"
    assert walked and walked[0] == route.stage_for("garden").walk_to


def _respawn_env(monkeypatch):
    """跑 run_worker 的工厂路线, 进场路线换成探针: 第 1 轮把阶段状态推进到工厂(= 上一命在工厂里),
    之后每轮记下进场路线开始时阶段状态在哪一段。"""
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)], "farming_duration": 9999,
        "short_round_limit": 99, "enemy_ai_enabled": False, "auto_switch_server": False,
        "biome": "garden", "map_name": "factory",
        "route": map_routes.route_for("factory"),
        "enter_game_swap": "none", "reach_area_swap": "none",
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "select_biome_on_title", lambda *a, **k: None)
    monkeypatch.setattr(main, "_wait_for_start_menu", lambda *a, **k: True)
    monkeypatch.setattr(main, "click_start_game", lambda: True)
    monkeypatch.setattr(main, "click_continue_after_death", lambda: True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    seen = []

    def probe(route, stage_state, **kw):
        seen.append((stage_state.map_name, stage_state.carried))
        if len(seen) == 1:
            stage_state.advance()          # 第 1 轮: 走到工厂了
            return "interrupted"
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "_run_entry_route", probe)
    return seen


def test_run_worker_keeps_stage_when_respawning_after_a_real_death(monkeypatch):
    """死了 -> 点继续 -> 点开始: 重生在工厂的检查点, 进场路线要从"工厂"这一段起步,
    不是 reset 回花园(区域名读不出时 reset 回去就拿花园的图在工厂里走满 180 秒)。"""
    seen = _respawn_env(monkeypatch)
    rounds = {"n": -1}
    monkeypatch.setattr(main, "on_guest_screen",
                        lambda: rounds.__setitem__("n", rounds["n"] + 1) or False)
    monkeypatch.setattr(main, "on_start_screen", lambda: rounds["n"] >= 1)
    monkeypatch.setattr(main, "on_death_screen", lambda: rounds["n"] >= 2)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == [("garden", False), ("factory", True)]    # 带过来的, 不是乐观推进猜的


def test_run_worker_carries_the_stage_into_the_next_round_after_reaching_the_farm(monkeypatch):
    """没死、一轮刷满接着下一轮: 人还在工厂里。到过刷怪区的那一轮要把阶段标成"带过来的",
    区域名读不出时下一轮的进场路线才不会拿服务器号(永远说花园)去走花园的路。"""
    seen = _respawn_env(monkeypatch)
    rounds = {"n": -1}
    monkeypatch.setattr(main, "on_guest_screen",
                        lambda: rounds.__setitem__("n", rounds["n"] + 1) or False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)

    def probe(route, stage_state, **kw):
        seen.append((stage_state.map_name, stage_state.carried))
        if len(seen) == 1:
            stage_state.advance()            # 第 1 轮走到工厂, 到达刷怪区
            return "arrived"
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "_run_entry_route", probe)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda *a, **k: True)
    monkeypatch.setattr(main, "auto_farming", lambda *a, **k: None)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == [("garden", False), ("factory", True)]


def test_run_worker_resets_stage_on_a_cold_start_without_death(monkeypatch):
    """没死(掉线 / 冷启动 / 换服)直接落到标题页: 新进场 = 从花园出生点走, 阶段状态回第一段。"""
    seen = _respawn_env(monkeypatch)
    rounds = {"n": -1}
    monkeypatch.setattr(main, "on_guest_screen",
                        lambda: rounds.__setitem__("n", rounds["n"] + 1) or False)
    monkeypatch.setattr(main, "on_start_screen", lambda: rounds["n"] >= 1)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == [("garden", False), ("garden", False)]


def test_entry_route_is_a_noop_on_single_stage_maps(monkeypatch):
    _quiet_entry_env(monkeypatch)
    walked = []
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    route = map_routes.route_for("desert")
    assert main._run_entry_route(route, main._StageState(route)) == "arrived"
    assert walked == []                # 沙漠不该走任何进场寻路


def test_entry_route_never_reads_the_server_id_on_single_stage_routes(monkeypatch):
    """单图路线(沙漠/花园)一次都不该去问"我现在在哪张图"(海洋/丛林只有一次落地核对,
    见下面 test_entry_route_skips_the_round_when_ocean_button_missed...).

    探针装上、真读得到服务器号之后这条才真正咬人: 沙漠那一轮只要读回来的号跟
    stage_state 对不上(读到别的标签页的号 / 上一台服务器的陈旧号 / 大厅号),
    stage_for() 返回 None -> _run_entry_route 返回 "timeout" -> 这一轮被跳过 ->
    记成短局 -> 攒够 short_round_limit 就自动换服务器。那是给**唯一在实机跑过的
    配置**凭空加了一条中断路径, 而且触发它的值在这台机器上没法验证。
    设计里承诺的是相反的:"单图路线(沙漠/ocean)下这套恒等于「就是那一张图」,
    零影响"。
    """
    _quiet_entry_env(monkeypatch)
    reads, walked = [], []
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc))
    # 读回来的是一张对不上的图 —— 沙漠轮必须完全不受影响.
    monkeypatch.setattr(main.florr_server, "current_map_name",
                        lambda ev: reads.append(ev) or "ocean")
    route = map_routes.route_for("desert")
    assert main._run_entry_route(route, main._StageState(route),
                                 timeout=5) == "arrived"
    assert walked == []
    assert reads == []                 # 单图路线连 CDP 都不该去打扰


def _landing_env(monkeypatch, landed):
    """海洋/丛林落地核对用的打桩: current_map_name 返回 landed, 记下 apply_map 调用。"""
    _quiet_entry_env(monkeypatch)
    applied = []
    monkeypatch.setattr(main, "apply_map", lambda n: applied.append(n))
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: False)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: landed)
    return applied


def test_entry_route_skips_the_round_when_ocean_button_missed_and_we_landed_in_garden(monkeypatch):
    # 海洋按钮坐标是从截图量的, 点偏 = 落进花园。单图路线以前完全不核对, 会拿 ocean.png
    # 在花园里刷 —— 现在读得到服务器号且不是海洋就 "timeout"(上层跳过这一轮)。
    applied = _landing_env(monkeypatch, "garden")
    route = map_routes.route_for("ocean")
    assert main._run_entry_route(route, main._StageState(route), timeout=5) == "timeout"
    assert applied == []               # 一张图都没套上


def test_entry_route_arrives_on_jungle_when_the_server_says_jungle(monkeypatch):
    applied = _landing_env(monkeypatch, "jungle")
    route = map_routes.route_for("jungle")
    assert main._run_entry_route(route, main._StageState(route), timeout=5) == "arrived"
    assert applied == ["jungle"]


def test_entry_route_proceeds_on_ocean_when_the_server_id_is_unreadable(monkeypatch):
    # 探针没装 / 读不到 -> None -> 跟改之前一样往下走, 不比以前更差。
    applied = _landing_env(monkeypatch, None)
    route = map_routes.route_for("ocean")
    assert main._run_entry_route(route, main._StageState(route), timeout=5) == "arrived"
    assert applied == ["ocean"]


def test_entry_route_never_reads_florr_server_on_desert(monkeypatch):
    # 沙漠是唯一在实机跑过的配置, 读服务器号对它有害(陈旧号 / 别的标签页的号会让这一轮
    # 被跳过) —— 落地核对只给 _LANDING_CHECK_MAPS, 沙漠一次都不许碰 florr_server。
    _quiet_entry_env(monkeypatch)
    monkeypatch.setattr(main, "apply_map", lambda n: None)

    def forbidden(ev):
        raise AssertionError("沙漠不该读服务器号")

    monkeypatch.setattr(main.florr_server, "current_map_name", forbidden)
    route = map_routes.route_for("desert")
    assert main._run_entry_route(route, main._StageState(route), timeout=5) == "arrived"


def test_entry_route_walks_to_portal_then_arrives_when_server_id_flips(monkeypatch):
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    reported = iter(["garden", "anthell"])
    monkeypatch.setattr(main.florr_server, "current_map_name",
                        lambda ev: next(reported))
    applied, walked = [], []
    monkeypatch.setattr(main, "apply_map", lambda n: applied.append(n))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append((loc, area)) or False)

    st = main._StageState(route)
    assert main._run_entry_route(route, st) == "arrived"
    tol = map_routes.PORTAL_TOLERANCE
    assert walked == [((150, 40), [[(150 - tol, 40 - tol), (150 + tol, 40 + tol)]])]
    assert applied == ["garden", "anthell"]


def test_entry_route_falls_back_to_stage_state_when_server_id_unreadable(monkeypatch):
    # 探针没装 / 读不到 -> current_map_name 恒 None -> 只能靠状态变量乐观推进.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: False)
    # timeout 显式给小值: 用默认的 180 秒时, advance() 这类回归会让本用例**挂**
    # 三分钟而不是立刻失败(失败要快, 挂死会被当成 CI 抽风).
    assert main._run_entry_route(route, main._StageState(route),
                                 timeout=5) == "arrived"


def test_entry_route_advances_even_when_pathing_returns_false(monkeypatch):
    # 踩上传送点那一刻人已经在下一张图, 而 load_binary_map() 还读着上一张 ——
    # lazy_theta_pathing 多半返回 False. "返回 True 才推进"会让状态变量永远卡住.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: False)
    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=5)   # 同上: 别用 180 秒的默认预算
    assert st.map_name == "anthell"


# ── 踩到判定框 != 真的传送了(2026-09-16 实机复盘) ───────────────────────────

def test_entry_route_holds_back_when_still_detected_in_the_portal_box(monkeypatch):
    """复查发现人还在判定框里(传送没真的触发)—— 不该推进, 该重新贴近同一个点."""
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    # 复查复位的坐标就落在判定框里(±map_routes.PORTAL_TOLERANCE) —— 模拟
    # "蹭到框边但没精确踩中触发点, 传送没发生, 人还站在原地"这个实机现象.
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (151, 41))

    st = main._StageState(route)
    result = main._run_entry_route(route, st, timeout=0.05)

    assert result == "timeout"           # 预算耗尽前一直原地重试, 从没真正推进过
    assert st.map_name == "garden"       # 没推进: 复查一直判"还在原地"


def test_entry_route_advances_when_no_longer_detected_in_the_portal_box(monkeypatch):
    """复查发现人不在判定框里了(不管测没测到位置)—— 当作真的传送了, 该推进."""
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    # 复查测到一个坐标, 但离判定框老远 —— 真传送后, 新地图上随便一个像素被旧地图
    # 吸附出来的坐标, 几乎不可能凑巧落回这个判定框里.
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=5)

    assert st.map_name == "anthell"      # 推进了


def test_entry_route_retry_uses_fresh_position_each_time_not_a_hang(monkeypatch):
    # "重新贴近"不是原地死循环打转 —— 每次 continue 都会重新调用 lazy_theta_pathing,
    # 给它一个改进的机会(哪怕这条用例里贴桩的调用不真的改坐标, 至少确认它真的
    # 被反复调用了, 不是卡在验证步骤本身出不来).
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    calls = []
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: calls.append(loc) or True)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (151, 41))

    main._run_entry_route(route, main._StageState(route), timeout=0.05)

    assert len(calls) >= 2               # 至少重试过一次, 不是只打了一发就躺平
    assert all(c == (150, 40) for c in calls)   # 每次都还是往同一个洞口的方向贴


# ── canvas 世界坐标复查(2026-09-17): 跟像素判据并存, 不是替代 ──────────────

def _stub_canvas_hook(monkeypatch, distance):
    """让 settle 复查那层的 canvas 判据"读到"一个固定距离 —— 不用真的拼一份
    合法的 canvas 记录.

    2026-09-20: settle 复查的 canvas 判据从 _canvas_world_distance_to(小地图
    玩家点, 跟像素判据共享同一个量化偏差源, 实机复盘证实过两者会一起被同一次
    偏差骗过去)换成了 _canvas_portal_world_offset(主视图洞口光效, 独立信号)
    —— 直接打桩这个函数, 让它返回一个欧氏距离等于 distance 的偏移量, 用这个
    helper 的用例不用关心 _run_entry_route 内部具体调的是哪个函数.

    _walk_toward_visible_portal 也顺手打桩成 no-op(返回 None) —— 用这个
    helper 的用例测的是 settle 复查那一层, 不是"朝洞口光效贴近"那一步(那步
    有自己专门的测试), 而它内部同样会调 _canvas_portal_world_offset, 不需要
    在这里也跑起来.
    """
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", lambda: None)
    monkeypatch.setattr(main.cdp_bridge, "drain_canvas_log",
                        lambda: [{"frame": 0}, {"frame": 1}])   # 凑够2个不同帧号
    monkeypatch.setattr(main.canvas_decode, "player_world_distance",
                        lambda recs, target: distance)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: (distance, 0.0))
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(main, "_walk_toward_visible_portal", lambda **kw: None)


def test_entry_route_holds_back_when_canvas_says_still_close_even_if_pixel_disagrees(monkeypatch):
    """这条测的正是这次改动的核心价值: 像素判据(target_box)已经判定"离开了"
    (旧代码在这一刻就会推进 stage_state, 这正是实机复盘那次的真实 bug), 但
    canvas 复查说世界坐标距离还很近 —— 两者取"任一个说还在原地就不推进",
    canvas 这层必须能单独拦住一次原本会被像素判据误判为"已传送"的推进。
    """
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    # 像素判据: 复查测到的点不在判定框里(旧逻辑会认为"已经离开", 推进) ——
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))
    # canvas判据: 世界坐标距离远小于 PORTAL_WORLD_HOLD_BACK_RADIUS(150) ——
    _stub_canvas_hook(monkeypatch, distance=5.0)

    st = main._StageState(route)
    result = main._run_entry_route(route, st, timeout=0.05)

    assert result == "timeout"     # 一直被 canvas 那层拦住, 没能推进
    assert st.map_name == "garden"


def test_entry_route_advances_when_both_pixel_and_canvas_agree_it_left(monkeypatch):
    # 两个判据都说"不在原地了"才真的推进 —— 对照组, 确认 canvas 这层加进来
    # 没有反过来卡死正常应该推进的情况.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))
    _stub_canvas_hook(monkeypatch, distance=9999.0)   # 远得多, 判"不在原地"

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=0.05)

    assert st.map_name == "anthell"


def test_entry_route_resets_confirmation_count_on_a_single_flaky_still_here_reading(monkeypatch):
    """单次复查说"离开了"不该立刻推进 —— 实机复盘(2026-09-20): 单次复查的
    像素/canvas读数都可能被噪声骗到(比如刚好越过容差框边界1个单位), 得连续
    PORTAL_LEFT_CONFIRMATIONS 次都判"离开了"才真的推进。这条测的是: 第一轮
    两次复查里, 第二次读到"还在原地"(哪怕第一次读到"离开了"), 也不该推进,
    确认次数清零重来 —— 直到某一轮连续两次都读到"离开了"才真的推进.
    """
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=None)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    # 第1轮: 复查1"离开"(10,10), 复查2却又判"还在原地"(150,40) —— 单次噪声,
    # 不该推进. 第2轮: 复查1/复查2都"离开"(10,10) —— 这次才真的推进.
    positions = [(10, 10), (150, 40), (10, 10), (10, 10)]
    calls = []

    def fake_get_player_position(*a, **k):
        calls.append(1)
        return positions[len(calls) - 1]

    monkeypatch.setattr(main, "get_player_position", fake_get_player_position)

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=5)

    assert st.map_name == "anthell"
    # 真的经过两轮×两次复查(4次)才推进 —— 不是第一次读到"离开"就走了(那样
    # 只会调用1次, PORTAL_LEFT_CONFIRMATIONS 不起作用也照样能凑出这个结果,
    # 这个调用次数才是这条测试真正要盯的东西).
    assert len(calls) == 4


def test_entry_route_kicks_anti_stuck_when_still_here_and_fine_approach_did_not_move(monkeypatch):
    """用户实机反馈(2026-09-20): 鼠标卡在屏幕中央完全静止, 直到用户自己碰了
    一下才恢复 —— 排查发现根因不是外部干扰: move_to_position 的到达判据
    (离目标欧氏距离<5个小地图像素)在这个精度量级下形同虚设, target_box
    附近的点彼此往往就在5像素以内(比如(133,238)到(133,234)只差4), 每一跳
    "trivially到达"、根本没真的发出移动指令就直接 reset_keyboard() 把鼠标
    归位 —— 如果这一轮 _walk_toward_visible_portal 又没找到光效
    (walked 不是 True), 整条链路里没有任何一段代码真的尝试移动过角色,
    彻底卡死。这条测的是: 确认"还在原地"要重新贴近前, 这种情况下会补一脚
    execute_anti_stuck() 打破僵局(项目里现成的脱困机制, 跟用户手动碰一下
    鼠标是同一个效果).
    """
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    rounds = {"n": 0}

    def pathing(loc, area, **kw):
        rounds["n"] += 1
        if rounds["n"] > 3:
            raise KeyboardInterrupt
        return True

    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (150, 40))   # 始终"还在原地"
    monkeypatch.setattr(main, "_walk_toward_visible_portal", lambda **kw: False)  # 贴近没成功
    anti_stuck_calls = []
    monkeypatch.setattr(main, "execute_anti_stuck", lambda *a, **k: anti_stuck_calls.append(1))

    st = main._StageState(route)
    with pytest.raises(KeyboardInterrupt):
        main._run_entry_route(route, st, timeout=5)

    assert len(anti_stuck_calls) == 3   # 每一轮"还在原地"都补了一脚, 不是只补一次


def test_entry_route_skips_anti_stuck_when_fine_approach_actually_moved(monkeypatch):
    # walked 是 True(_walk_toward_visible_portal 真的贴近成功了)时不该再补
    # 脱困硬闯 —— 这一轮已经有真实移动发生, 不是卡死的场景.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (150, 40))   # 始终"还在原地"
    monkeypatch.setattr(main, "_walk_toward_visible_portal", lambda **kw: True)   # 贴近成功了
    anti_stuck_calls = []
    monkeypatch.setattr(main, "execute_anti_stuck", lambda *a, **k: anti_stuck_calls.append(1))

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=0.05)

    assert anti_stuck_calls == []


def test_walk_toward_minimap_target_ignores_the_dist_under_5_shortcut(monkeypatch):
    """move_to_position 的"欧氏距离<5就算到达"判据在这张图上形同虚设 ——
    target_box 附近的点彼此经常本来就没到5个单位(比如(133,238)到
    (133,234)只差4), 按那条判据一上来就"到了", 一步没挪。这条退路函数
    专门不信这条判据: 哪怕起始距离只有4, 也该先真走一步、拿位置有没有
    真的变化说话, 不是拿距离说话.
    """
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    moves = []
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda pos: moves.append(pos))
    positions = iter([(133, 238), (133, 234)])   # 第二次读到位置已经变了
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: next(positions))

    assert main._walk_toward_minimap_target((133, 234)) is True
    assert len(moves) == 1   # 只走了一脚就检测到真挪动了, 没有多走


def test_walk_toward_minimap_target_gives_up_after_exhausting_attempts_if_stuck(monkeypatch):
    # 位置死活不变(真卡住了, 不是判据的锅) —— 走够预算才认输, 不是试一次
    # 就放弃, 也不是死等到天荒地老.
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    moves = []
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda pos: moves.append(pos))
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (133, 238))   # 永远不变

    assert main._walk_toward_minimap_target((133, 234), attempts=4) is False
    assert len(moves) == 4


def test_walk_toward_minimap_target_returns_false_when_position_unreadable(monkeypatch):
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    moves = []
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda pos: moves.append(pos))
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: None)

    assert main._walk_toward_minimap_target((133, 234)) is False
    assert moves == []


def test_walk_toward_minimap_target_gives_up_when_already_on_top_of_target(monkeypatch):
    # 起点跟终点几乎完全重合, 连方向都算不出来, 硬走没有意义, 不该崩.
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    moves = []
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda pos: moves.append(pos))
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (133, 234))

    assert main._walk_toward_minimap_target((133, 234)) is False
    assert moves == []


def test_entry_route_falls_back_to_minimap_walk_when_glow_never_found(monkeypatch):
    """用户反馈(2026-09-20): "光效都不到的话就尝试使用小地图贴近，并忽距离
    <5的规则" —— _walk_toward_visible_portal 返回 None(压根没定位到光效起点,
    不是追近过程中卡住)时不该干等, 该退回小地图坐标硬走一段。这条测接线:
    None 时真的调了 _walk_toward_minimap_target(拿 stage.walk_to 当目标),
    它成功(True)的话也不该再额外补脱困硬闯.
    """
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    rounds = {"n": 0}

    def pathing(loc, area, **kw):
        rounds["n"] += 1
        if rounds["n"] > 3:
            raise KeyboardInterrupt
        return True

    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (150, 40))
    monkeypatch.setattr(main, "_walk_toward_visible_portal", lambda **kw: None)
    fallback_calls = []
    monkeypatch.setattr(main, "_walk_toward_minimap_target",
                        lambda target, **kw: fallback_calls.append(target) or True)
    anti_stuck_calls = []
    monkeypatch.setattr(main, "execute_anti_stuck", lambda *a, **k: anti_stuck_calls.append(1))

    st = main._StageState(route)
    with pytest.raises(KeyboardInterrupt):
        main._run_entry_route(route, st, timeout=5)

    assert fallback_calls == [(150, 40)] * 3   # 每一轮都退回小地图硬走了一次
    assert anti_stuck_calls == []   # 小地图硬走成功了(True), 不该再补脱困


def test_entry_route_does_not_fall_back_to_minimap_walk_when_glow_search_just_stalled(monkeypatch):
    # walked=False(光效定位到过, 追近过程中冲过头/卡住)时不该退回小地图硬走
    # —— 那条退路是专给"光效压根没定位到"(None)准备的, False 已经真的尝试过
    # 追近了, 该走原来的脱困硬闯路径, 不是再叠一层小地图硬走.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (150, 40))
    monkeypatch.setattr(main, "_walk_toward_visible_portal", lambda **kw: False)
    fallback_calls = []
    monkeypatch.setattr(main, "_walk_toward_minimap_target",
                        lambda target, **kw: fallback_calls.append(target) or True)
    monkeypatch.setattr(main, "execute_anti_stuck", lambda *a, **k: None)

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=0.05)

    assert fallback_calls == []


def test_entry_route_canvas_layer_uses_the_glow_offset_not_the_minimap_distance(monkeypatch):
    """实机复盘(2026-09-20): 像素判据和旧版canvas判据(读小地图玩家点)其实
    共享同一个量化偏差源(两次独立复查读到过一模一样的坐标和世界距离, 角色
    根本没动过) —— 两个"独立"确认会一起被同一次系统性偏差骗过去,
    PORTAL_LEFT_CONFIRMATIONS 那层"多确认几次"对这种偏差没用。这条测的是:
    复查用的确实是 _canvas_portal_world_offset(主视图洞口光效, 真正独立于
    像素判据的信号), 不是 player_world_distance(小地图, 跟像素共享偏差源)
    —— 就算 player_world_distance 说"很近"(该拦), 只要
    _canvas_portal_world_offset 说"很远", canvas 判据也该说"已离开", 不能
    被前者拦住.
    """
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))  # 像素判据: 已离开
    monkeypatch.setattr(main, "_walk_toward_visible_portal", lambda **kw: None)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", lambda: None)
    monkeypatch.setattr(main.cdp_bridge, "drain_canvas_log",
                        lambda: [{"frame": 0}, {"frame": 1}])
    # 旧信号(小地图)说"很近"5.0(< PORTAL_WORLD_HOLD_BACK_RADIUS, 该拦) ——
    monkeypatch.setattr(main.canvas_decode, "player_world_distance",
                        lambda recs, target: 5.0)
    # 新信号(主视图光效)说"很远"9999.0(不该拦) ——
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: (9999.0, 0.0))

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=5)

    assert st.map_name == "anthell"   # 用的是新信号, 没被旧信号的"很近"拦住


def test_entry_route_skips_canvas_layer_when_stage_has_no_world_position(monkeypatch):
    # 沙漠/海洋这种单图路线第一圈就 "arrived"(stage_state.map_name 一开始
    # 就是 final_map), 连 lazy_theta_pathing/复查那一段都碰不到 —— 这条盯的
    # 是 inject_canvas_hook 压根不该被调用(不是"发生了但读到None", 是
    # "根本没试着读"), 免得给单图路线凭空添一条实机没验证过的 CDP 依赖。
    _quiet_entry_env(monkeypatch)
    calls = []
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", lambda: calls.append(1))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: (_ for _ in ()).throw(AssertionError("不该走到寻路这步")))
    route = map_routes.route_for("desert")
    result = main._run_entry_route(route, main._StageState(route))
    assert result == "arrived"
    assert calls == []


def test_canvas_portal_world_offset_returns_none_when_hook_unavailable(monkeypatch):
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook",
                        lambda: (_ for _ in ()).throw(RuntimeError("no chrome")))
    assert main._canvas_portal_world_offset() is None


def test_canvas_portal_world_offset_returns_none_when_drain_raises(monkeypatch):
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", lambda: None)
    monkeypatch.setattr(main.cdp_bridge, "drain_canvas_log",
                        lambda: (_ for _ in ()).throw(ConnectionError("closed")))
    assert main._canvas_portal_world_offset() is None


def test_canvas_portal_world_offset_returns_none_with_fewer_than_two_frames(monkeypatch):
    # 只有一个不同帧号 = __canvasFrame 没在推进(画面没在动/hook装的时机不对) ——
    # 跟 debug_canvas_enemies.py 那条诊断逻辑一样, 这种情况下不硬解, 老实认输.
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", lambda: None)
    monkeypatch.setattr(main.cdp_bridge, "drain_canvas_log", lambda: [{"frame": 0}])
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    assert main._canvas_portal_world_offset(drain_seconds=0.01) is None


def test_canvas_portal_world_offset_returns_none_when_camera_undetermined(monkeypatch):
    # camera_from_frame 需要 zoom/player_world/player_screen 三样齐全, 缺一
    # 就抛 ValueError —— 这一帧解不出相机时老实认输, 不猜一个偏移量出来,
    # 也不该带着一个坏掉的 camera 继续往下调 portal_glow_world_offset(会拿
    # None 去下标, 真机上会崩掉 —— 这里直接断言它压根没被调, 不只是看最终
    # 返回值是不是 None, 免得"两步都恰好返回None"把这条测试的意义抵消掉).
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", lambda: None)
    monkeypatch.setattr(main.cdp_bridge, "drain_canvas_log",
                        lambda: [{"frame": 0}, {"frame": 1}])
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(main.canvas_decode, "camera_from_frame",
                        lambda recs: (_ for _ in ()).throw(ValueError("camera undetermined")))
    calls = []
    monkeypatch.setattr(main.canvas_decode, "portal_glow_world_offset",
                        lambda recs, camera: calls.append(1))
    assert main._canvas_portal_world_offset(drain_seconds=0.01) is None
    assert calls == []


def test_canvas_portal_world_offset_delegates_to_canvas_decode(monkeypatch):
    # 真正的解码逻辑归 canvas_decode.camera_from_frame / portal_glow_world_offset
    # —— 这里只验证 _canvas_portal_world_offset 把"次新帧"这个正确的记录集合
    # 传给它们, 不是随手传了最新帧或者全部记录混在一起.
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", lambda: None)
    # time.sleep 打桩成空转之后, drain 循环在 0.01 秒预算里会跑很多轮 —— 只在
    # 第一次真的吐数据, 后面吐空列表, 不然 buf 里会攒出同一帧的好几份拷贝。
    drained = []

    def _drain_once():
        if drained:
            return []
        drained.append(1)
        return [{"frame": 0, "tag": "old"}, {"frame": 1, "tag": "second_newest"},
                {"frame": 2, "tag": "newest"}]

    monkeypatch.setattr(main.cdp_bridge, "drain_canvas_log", _drain_once)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    seen_camera = []
    seen_offset = []
    fake_camera = {"zoom": 1.0, "player_world": (0.0, 0.0), "player_screen": (0.0, 0.0)}
    monkeypatch.setattr(main.canvas_decode, "camera_from_frame",
                        lambda recs: seen_camera.append(recs) or fake_camera)
    monkeypatch.setattr(main.canvas_decode, "portal_glow_world_offset",
                        lambda recs, camera: seen_offset.append((recs, camera)) or (3.0, 4.0))

    result = main._canvas_portal_world_offset(drain_seconds=0.01)

    assert result == (3.0, 4.0)
    assert [r["tag"] for r in seen_camera[0]] == ["second_newest"]   # 次新帧, 不是最新/全部
    recs, camera = seen_offset[0]
    assert [r["tag"] for r in recs] == ["second_newest"]
    assert camera == fake_camera


# ── _walk_toward_visible_portal: 绕开300x300量化, 实时朝洞口光效贴近 ──────────
#
# 第一版拿"canvas读到的玩家绝对世界坐标"跟硬编码目标比, 实机(2026-09-19)发现
# 那条路径本身也没绕开小地图量化(玩家世界坐标照样来自小地图玩家点)。现在
# 改成每tick直接从当前帧读洞口光效相对玩家的世界坐标偏移量, 不需要预先给
# 一个绝对目标坐标.

def _stub_walk_env(monkeypatch):
    # reset_keyboard() 内部会调 keyup(w/a/s/d), 每个都无条件把鼠标挪回屏幕
    # 中心(utils.keyup 的既有实现, 跟方向参数无关) —— 跟这里要盯的"朝目标转向"
    # 那次 moveTo 混在一起会让调用次数断言失真, 打桩成 no-op, 只留意图明确的
    # 转向调用.
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda pos: calls.append(pos))
    return calls


def test_walk_toward_visible_portal_returns_none_when_canvas_unavailable(monkeypatch):
    calls = _stub_walk_env(monkeypatch)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: None)
    assert main._walk_toward_visible_portal() is None
    assert calls == []


def test_walk_toward_visible_portal_returns_true_when_already_close_enough(monkeypatch):
    calls = _stub_walk_env(monkeypatch)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: (10.0, 10.0))
    assert main._walk_toward_visible_portal() is True
    assert calls == []   # 已经在 PORTAL_WORLD_ARRIVE_RADIUS 内, 不该多走一步


def test_walk_toward_visible_portal_steers_closer_each_tick_until_arrived(monkeypatch):
    # 每次读到的偏移量都比上次更靠近0(超过progress_epsilon), 不该被误判成
    # "原地打转" —— dist依次150,110,70,50, 最后一步<60(默认ARRIVE_RADIUS)
    # 触发到达, 到达前的3次都该转向走过去.
    offsets = iter([(0.0, 150.0), (0.0, 110.0), (0.0, 70.0), (0.0, 50.0)])
    calls = _stub_walk_env(monkeypatch)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: next(offsets))
    assert main._walk_toward_visible_portal() is True
    assert len(calls) == 3


def test_walk_toward_visible_portal_gives_up_when_no_progress(monkeypatch):
    # 偏移量死活不变(卡在障碍物上, 或者光效/玩家双双没挪窝) —— stall_count
    # 攒够就该认输, 不能死等到 timeout 预算耗尽才反应.
    calls = _stub_walk_env(monkeypatch)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: (500.0, 500.0))
    assert main._walk_toward_visible_portal(stall_limit=3) is False
    assert len(calls) == 4   # stall_limit=3: 第4次判定卡住之前已经转向走了4次


def test_walk_toward_visible_portal_gives_up_when_offset_lost_mid_walk(monkeypatch):
    # 一开始还读得到, 走着走着canvas突然读不到了(光效画出画面外/相机解不出
    # 来了) —— 不能装作到达, 老实认输交回上层.
    offsets = iter([(0.0, 1000.0), None])
    calls = _stub_walk_env(monkeypatch)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: next(offsets))
    assert main._walk_toward_visible_portal() is False
    assert len(calls) == 1


def test_walk_toward_visible_portal_overshoot_does_not_count_as_arrival(monkeypatch):
    # 实机复盘(2026-09-19): 用户确认了角色一直"跑过去"传送门, 没真的进去 ——
    # 查出来是旧逻辑把"这次比上次更远"直接当成"到了"处理, 骗过了后续判定,
    # 传送从来没真的触发过。这条测的是冲过头(dist越读越大, 但从没真的进过
    # ARRIVE_RADIUS)不该被当成到达, 只该老实计进 stall_count.
    offsets = iter([(0.0, 100.0), (0.0, 150.0), (0.0, 160.0)])
    calls = _stub_walk_env(monkeypatch)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: next(offsets))
    result = main._walk_toward_visible_portal(stall_limit=1)
    assert result is False   # 不是 True —— 冲过头不算数
    assert len(calls) == 2   # 两次都在刹车区轻推了一步, 第3次判定卡住没再动


def test_walk_toward_visible_portal_brake_zone_uses_the_tightened_reaction_timing(monkeypatch):
    # 用户反馈(2026-09-20): "能不能让他反应快一点，玩家到了之后立马就停，
    # 现在就是因为反应不够导致直接略过去了" —— 这条锁定刹车区"轻推"和
    # "收手等它停"用的是收紧过的常量, 不是老的0.05/0.1秒, 免得以后手滑
    # 改回慢的数值也测不出来.
    offsets = iter([(0.0, 100.0), (0.0, 50.0)])   # 第2次读数已经<ARRIVE_RADIUS
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    sleeps = []
    monkeypatch.setattr(main.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda pos: None)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: next(offsets))

    assert main._walk_toward_visible_portal() is True

    assert sleeps == [main.PORTAL_WORLD_BRAKE_TAP_SECONDS,
                      main.PORTAL_WORLD_BRAKE_SETTLE_SECONDS]
    assert 0.05 not in sleeps and 0.1 not in sleeps   # 老的慢数值不该再出现


def test_walk_toward_visible_portal_far_zone_no_longer_sprints_at_full_speed(monkeypatch):
    # 用户反馈(2026-09-20): "远处的时候不要全速贴近，慢一点" —— 老逻辑远处
    # 推力最多拉到500、全程不收手、只 sleep(0.05) 就再读一次, 相当于一路
    # 贴着最高速冲到刹车区。这条测两件事: (1) 现在跟刹车区一样是"推一下就
    # 收手"(一个tick里两次sleep, 中间夹一次reset_keyboard), 不是老的单次
    # sleep(0.05); (2) 推力上限是 PORTAL_WORLD_FAR_STEP(200), 不是老的500.
    offsets = iter([(0.0, 1000.0), (0.0, 50.0)])   # 第2次读数已经<ARRIVE_RADIUS
    resets = []
    monkeypatch.setattr(main, "reset_keyboard", lambda: resets.append(1))
    sleeps = []
    monkeypatch.setattr(main.time, "sleep", lambda s: sleeps.append(s))
    moves = []
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda pos: moves.append(pos))
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda **kw: next(offsets))

    assert main._walk_toward_visible_portal() is True

    # 远处那一脚推完收手一次, 到达那一步再收手一次 —— 老逻辑远处那一脚
    # 完全不调 reset_keyboard, 只有到达时才有这一次.
    assert len(resets) == 2
    assert sleeps == [main.PORTAL_WORLD_FAR_TAP_SECONDS,
                      main.PORTAL_WORLD_FAR_SETTLE_SECONDS]

    extend = main.PORTAL_WORLD_FAR_STEP * main.mouse_scale()
    expected = main.clamp_to_screen(main.SCREEN_WIDTH // 2, main.SCREEN_HEIGHT // 2 + extend)
    assert moves == [expected]   # 推力封顶在 FAR_STEP, 不是老的500


def test_entry_route_calls_walk_toward_visible_portal_when_stage_has_one(monkeypatch):
    """光靠 canvas 复查(只判定"到没到")拦不住"永远到不了"这个问题本身 ——
    实机复盘(2026-09-18): 人肉走到像素判据认定的点上传送依然没反应, 300x300
    小地图那层量化本身就比传送触发精度粗了一个量级。这条测的是 _run_entry_route
    真的多走了那一步(调 _walk_toward_visible_portal), 不是只加了一层"判定"
    而没有真的"纠正"。
    """
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))
    _stub_canvas_hook(monkeypatch, distance=9999.0)   # 两条判据都同意"离开了", 一轮就推进
    calls = []
    monkeypatch.setattr(main, "_walk_toward_visible_portal",
                        lambda **kw: calls.append(1) or True)

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=0.05)

    assert calls == [1]


def test_entry_route_skips_walk_toward_visible_portal_when_stage_has_no_world_position(monkeypatch):
    # 蚁穴这段没标定世界坐标(portal_world=None)时, _walk_toward_visible_portal
    # 压根不该被调 —— 跟 canvas 复查那层同一道闸门, 单图路线/未标定路线不该
    # 凭空多一条实机没验证过的 CDP 依赖.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=None)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))
    calls = []
    monkeypatch.setattr(main, "_walk_toward_visible_portal",
                        lambda **kw: calls.append(1) or True)

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=0.05)

    assert calls == []


# ── want_defense: 贴近传送门这段临时关反转防御, 离开时恢复(2026-09-20) ─────────
# 用户反馈: 反转防御开着时角色带"泡泡"效果, 卡不到传送门精确坐标上.

def _stub_ensure_flag_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda eval_js, addr, want: calls.append((addr, want))
                        or ("unchanged", ""))
    return calls


def test_entry_route_toggles_defense_off_then_on_around_portal_approach(monkeypatch):
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))
    _stub_canvas_hook(monkeypatch, distance=9999.0)   # 一轮就推进, 用例跑得快
    calls = _stub_ensure_flag_calls(monkeypatch)

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=0.05, want_defense=True)

    D = main.florr_settings.INVERT_DEFENSE_ADDR
    assert calls == [(D, 0), (D, 1)]   # 先关, 结束时(不管怎么结束)恢复成开


def test_entry_route_never_toggles_defense_when_interrupted_before_the_portal_approach(monkeypatch):
    # 实机反馈(2026-09-20): 第一版把开关窗口摆在了整个进场函数外层, 结果人
    # 还在花园里老远的地方走, 还没到真正贴近传送门光效那一步, 反转防御就先
    # 关了 —— "还没到就直接关了"。收紧到只包住 _walk_toward_visible_portal()
    # 那次调用之后, 在到达那一步之前就被打断(死亡/开局画面)的这种情况,
    # 压根不该碰这个开关.
    _quiet_entry_env(monkeypatch)
    monkeypatch.setattr(main, "on_start_screen", lambda: True)   # 立刻 interrupted
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    calls = _stub_ensure_flag_calls(monkeypatch)

    st = main._StageState(route)
    result = main._run_entry_route(route, st, timeout=0.05, want_defense=True)

    assert result == "interrupted"
    assert calls == []


def test_entry_route_keeps_defense_off_across_retries_until_it_truly_returns(monkeypatch):
    # 用户实机反馈(2026-09-20): "没进传送门之前就不要开反转防御控制了" ——
    # 一轮贴近没成功、马上要重新贴近的这个间隙里不该被打开. 这条模拟"一直
    # 没能推进, 反复重新贴近很多轮"(像素判据一直判'还在原地'), 确认反转
    # 防御全程只关了一次, 直到整个 _run_entry_route 真的要返回(这里是
    # timeout)才恢复一次 —— 不是每轮贴近尝试各自开关一次.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    # 像素判据: 还在判定框里, 一直重试, 不推进 ——
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (150, 40))
    calls = _stub_ensure_flag_calls(monkeypatch)

    st = main._StageState(route)
    result = main._run_entry_route(route, st, timeout=0.05, want_defense=True)

    D = main.florr_settings.INVERT_DEFENSE_ADDR
    assert result == "timeout"           # 一直没推进, 预算耗尽交回上层
    # 不管重试了多少轮(这个场景里 timeout=0.05 秒配合无操作的假环境, 实际会
    # 循环很多轮), 全程只有开头一次"关"、结尾一次"开" —— 不是每轮各自一对.
    assert calls == [(D, 0), (D, 1)]


def test_entry_route_does_not_toggle_defense_when_config_does_not_want_it(monkeypatch):
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(150, 40), portal_world=(1000.0, 2000.0))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: True)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))
    _stub_canvas_hook(monkeypatch, distance=9999.0)
    calls = _stub_ensure_flag_calls(monkeypatch)

    st = main._StageState(route)
    main._run_entry_route(route, st, timeout=0.05, want_defense=False)

    assert calls == []


def test_entry_route_does_not_toggle_defense_for_route_without_world_position(monkeypatch):
    # 沙漠/海洋这种单图路线, 或者蚁穴没标定世界坐标的时候: 就算 want_defense=True
    # 也不该碰这个开关 —— 没有"贴近传送门"这一步, 没理由多一条CDP依赖.
    _quiet_entry_env(monkeypatch)
    calls = _stub_ensure_flag_calls(monkeypatch)
    route = map_routes.route_for("desert")
    main._run_entry_route(route, main._StageState(route), want_defense=True)
    assert calls == []


def test_entry_route_interrupted_on_death_screen(monkeypatch):
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: False)
    monkeypatch.setattr(main, "on_death_screen", lambda: True)
    assert main._run_entry_route(route, main._StageState(route)) == "interrupted"


def test_entry_route_interrupted_on_start_screen(monkeypatch):
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    assert main._run_entry_route(route, main._StageState(route)) == "interrupted"


def test_entry_route_times_out(monkeypatch):
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: False)
    assert main._run_entry_route(route, main._StageState(route),
                                 timeout=0) == "timeout"


def test_entry_route_budget_bounds_pathing_that_would_never_return(monkeypatch):
    """ENTRY_ROUTE_TIMEOUT 必须真的兜得住, 哪怕寻路本身永不返回。

    设计里 风险 1 写的是"洞口被怪/玩家堵住 -> lazy_theta_pathing 走不到 ->
    ENTRY_ROUTE_TIMEOUT 兜住 -> 重开一轮 … 自愈, 不卡死" —— 可 lazy_theta_pathing
    恰恰是**设计成永不放弃**的(卡住就脱困重来, 测不到位置就 1Hz 无限重试), 而
    deadline 只在 while 的顶上查。那 worker 就会永远待在花园里: 不超时、不记短局、
    不换服。这里跑的是**真的** lazy_theta_pathing(只打桩它的实机依赖), 玩家位置
    恒 None = 花园的玩家标记色跟沙漠不一样时的样子(设计 风险 4)。
    """
    clock = _stub_pathing_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    tries = []

    def never_found(*a, **k):
        tries.append(clock[0])
        if len(tries) >= 200:   # 保险丝: 兜不住时让用例**失败**, 而不是挂死
            pytest.fail("deadline 没传进 lazy_theta_pathing —— 进场路线永远回不来")
        return None

    monkeypatch.setattr(main, "get_player_position", never_found, raising=False)
    assert main._run_entry_route(route, main._StageState(route),
                                 timeout=5) == "timeout"
    assert clock[0] - 1000.0 < 30    # 预算是 5 秒, 不该拖出个没边的数


def test_entry_route_gives_up_when_on_a_map_outside_the_route(monkeypatch):
    # 掉到 jungle / 别的图上: 不在这条路线里, 没法一段段走过去, 交回上层重开.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "ocean")
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda loc, area, **kw: False)
    assert main._run_entry_route(route, main._StageState(route)) == "timeout"


def test_run_worker_entry_route_timeout_never_counts_as_a_full_round(monkeypatch):
    """进场路线没走到的一轮, 一秒怪都没刷 —— 墙钟走了多久都不算"刷满".

    时块把 farming_duration 配得比 ENTRY_ROUTE_TIMEOUT(180s)短时(GUI 只校验
    "正整数", 不设下限), 超时那一轮的 round_elapsed 反而 >= farming_duration:
    光看时间会把这一轮判成刷满 -> consecutive_short_rounds 清零 -> 洞口永久进不去
    也永远攒不够短局, switch_server 再也不会触发, 自愈链路直接死掉。
    """
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)],
        "farming_duration": 60,          # < ENTRY_ROUTE_TIMEOUT(180)
        "short_round_limit": 2, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "garden",
        "map_name": "anthell",
        "route": map_routes.route_for("anthell"),
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    # 洞口坐标还没标定、maps/garden.png 还没采 —— 启动闸门不是这条用例要测的东西.
    monkeypatch.setattr(main, "_route_blocker", lambda route: None)
    # 每轮都真进游戏, 否则"没进游戏的轮"会 continue 掉, 根本到不了短局统计.
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main, "click_start_game", lambda: True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    # 假时钟: 进场路线把整个预算耗完才返回, 于是 round_elapsed(180) > farming_duration(60).
    clock = [1000.0]
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    rounds = {"n": 0}

    def entry_times_out(route, stage_state, timeout=main.ENTRY_ROUTE_TIMEOUT, **kw):
        rounds["n"] += 1
        clock[0] += timeout
        if rounds["n"] > 5:              # 保险丝: 不换服也别把用例挂成死循环
            raise KeyboardInterrupt
        return "timeout"

    monkeypatch.setattr(main, "_run_entry_route", entry_times_out)
    sw = []

    def rec(*a, **k):
        sw.append(a)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "switch_server", rec)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert sw == [("garden",)]   # 攒够 2 次短局 -> 换一台花园服重踩洞口
    assert rounds["n"] == 2      # 第 2 轮就换, 不是跑满保险丝


def test_run_worker_counts_entry_timeout_even_when_the_round_never_re_entered(monkeypatch):
    """洞口进不去的稳定态: 人活着待在花园里, 死亡/开局画面都不出现.

    这时 entered_game 恒 False, 于是 `not entered_game and not reached_farm` 那道
    "没进过场不算短局"的闸门会把整轮 continue 掉、跳过短局统计 —— 短局计数永远
    停在 1, 到不了 short_round_limit, switch_server 一辈子不触发, 180 秒的进场路线
    无限重复。那道闸门是给换服/重连空档那种**瞬态**留的; 进场路线超时是**持续**
    故障, 而且实打实烧掉了 ENTRY_ROUTE_TIMEOUT, 必须记账。
    """
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)],
        "farming_duration": 9999,        # 怎么都刷不满 -> 每轮都是短局
        "short_round_limit": 2, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "garden",
        "map_name": "anthell",
        "route": map_routes.route_for("anthell"),
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "_route_blocker", lambda route: None)
    # _stub_run_worker_env 里 on_death_screen / on_start_screen / on_guest_screen 全恒 False
    # —— 正是"人还活着站在花园里"的第 2 轮之后的稳定态, entered_game 一直是 False.
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    rounds = {"n": 0}

    def entry_times_out(route, stage_state, timeout=main.ENTRY_ROUTE_TIMEOUT, **kw):
        rounds["n"] += 1
        if rounds["n"] > 5:              # 保险丝: 不换服也别把用例挂成死循环
            raise KeyboardInterrupt
        return "timeout"

    monkeypatch.setattr(main, "_run_entry_route", entry_times_out)
    sw = []

    def rec(*a, **k):
        sw.append(a)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "switch_server", rec)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert sw == [("garden",)]
    assert rounds["n"] == 2      # 第 2 轮就换, 不是跑满保险丝


def test_run_worker_entry_route_interrupted_near_boot_never_counts_as_a_short_round(monkeypatch):
    """刚开worker/换账号重开Chrome那一刻, 标题页还没渲染完。

    entered_game 全程 False(循环顶 on_start_screen 还没抓到) 时,
    _run_entry_route 内层的 lazy_theta_pathing 撞见开局菜单会几乎瞬间
    return "interrupted"(不是烧满 ENTRY_ROUTE_TIMEOUT 的 "timeout")——
    实机复盘(2026-09-18): 这种10秒不到的一轮被当成短局计了一次, 连续两轮
    撞上就真的触发了 switch_server, 白白换了服, 根本没死过, 也没真的进过场。
    "没进过场不算短局"那道闸门本该吞掉这种瞬态, 之前的条件写成
    entry == "arrived" 只覆盖得了单图路线自己的边界情况, 把 "interrupted"
    漏在外面了。
    """
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)],
        "farming_duration": 300,
        "short_round_limit": 2, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "garden",
        "map_name": "anthell",
        "route": map_routes.route_for("anthell"),
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "_route_blocker", lambda route: None)
    # _stub_run_worker_env 里 on_death_screen/on_start_screen/on_guest_screen
    # 全恒 False —— entered_game 全程 False, 正是"标题页还没渲染完"那个瞬态.
    rounds = {"n": 0}

    def entry_interrupted(route, stage_state, timeout=main.ENTRY_ROUTE_TIMEOUT, **kw):
        rounds["n"] += 1
        if rounds["n"] > 5:      # 保险丝: 不换服也别把用例挂成死循环
            raise KeyboardInterrupt
        return "interrupted"     # 几乎瞬间返回, 没碰 ENTRY_ROUTE_TIMEOUT

    monkeypatch.setattr(main, "_run_entry_route", entry_interrupted)
    sw = []
    monkeypatch.setattr(main, "switch_server", lambda *a, **k: sw.append(a))

    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})

    assert sw == []           # 一次都不该被判成短局, switch_server 不该被叫到
    assert rounds["n"] == 6   # 跑满保险丝退出, 不是提前因为别的分支短路


def test_run_worker_entry_route_interrupted_after_death_reentry_never_counts_as_a_short_round(monkeypatch):
    """死亡后点继续+点开始重新进场, 这次进场很快又被打断(entry=="interrupted")
    —— 不该算独立的一次短局。

    实机反馈(2026-09-19): 用户只死了一次, 但记录显示"连续2次"短局、真的
    触发了 switch_server。查出来: 死亡那一轮本身算1次短局没问题, 但死后
    处理(点继续+点开始)会把 entered_game 设成 True, 上一条豁免("没进过场
    不算短局")要求 entered_game 恒 False, 这时候就用不上了 —— 死后重进场
    很快又被打断这件事, 本质是"同一次死亡的重进尝试失败", 不是又死了一次,
    却照样被记成第2次短局, 两次一凑就误触发换服务器。
    """
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)],
        "farming_duration": 300,
        "short_round_limit": 2, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "garden",
        "map_name": "anthell",
        "route": map_routes.route_for("anthell"),
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "_route_blocker", lambda route: None)
    # 每轮循环顶都撞见死亡画面 -> entered_game 恒 True(模拟"死后一直在重进"
    # 这个稳定态, 不是真的每轮都死了一次).
    monkeypatch.setattr(main, "on_death_screen", lambda: True)
    monkeypatch.setattr(main, "click_continue_after_death", lambda: None)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    rounds = {"n": 0}

    def entry_interrupted(route, stage_state, timeout=main.ENTRY_ROUTE_TIMEOUT, **kw):
        rounds["n"] += 1
        if rounds["n"] > 5:
            raise KeyboardInterrupt
        return "interrupted"

    monkeypatch.setattr(main, "_run_entry_route", entry_interrupted)
    sw = []
    monkeypatch.setattr(main, "switch_server", lambda *a, **k: sw.append(a))

    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})

    assert sw == []
    assert rounds["n"] == 6


def test_run_worker_resets_stage_state_after_switching_servers(monkeypatch):
    """换服 = 落回另一台花园服的出生点, 下一轮必须重新走一遍洞口.

    读不到服务器号时 _StageState 是"人在哪张图"的唯一依据: switch_server 后
    不 reset(), 下标会一直停在蚁穴那一段, 下一轮 _run_entry_route 第一圈就
    "arrived", 人明明站在花园里, 却直接按蚁穴坐标系的 location 寻路。
    这里跑的是**真的** _run_entry_route + _StageState, 只打桩它们的副作用。
    """
    _stub_run_worker_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)],
        "farming_duration": 9999,        # 怎么都刷不满 -> 每轮都是短局
        "short_round_limit": 1, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "garden",
        "map_name": "anthell",
        "route": route,
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "_route_blocker", lambda r: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)      # maps/garden.png 还没采
    # 2026-09-20新增: 到刷怪区前会读一次 load_binary_map() 校验配置的 location
    # 是不是可走 —— 跟这条测的东西(_StageState reset)无关, 不打桩的话会读到
    # 别的用例真跑过 apply_map() 留下的全局 MAP、读到一张真图, (1,2) 落在墙上
    # 会被悄悄换成别的点, walked 断言就对不上了.
    monkeypatch.setattr(main, "load_binary_map", lambda: None, raising=False)
    # 读不到 -> 恒 None -> _StageState 是唯一依据, 正是 reset() 要保护的那个场景.
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    # 第 1 轮在开局菜单(点了开始就不在了 -> entered_game=True, 走得到短局统计);
    # 换服走的是 CDP 重连, 人直接落在新那台花园服里, **不回标题页** —— 这正是
    # switch_server 后面那句 reset() 唯一起作用的情形: 真回了标题页的话, 循环顶
    # click_start_game 分支里那句 reset() 顺手就把它覆盖了, 测不出差别.
    at_menu = {"yes": True}
    monkeypatch.setattr(main, "on_start_screen", lambda: at_menu["yes"])
    monkeypatch.setattr(main, "click_start_game",
                        lambda: at_menu.__setitem__("yes", False) or True)

    walked = []

    def pathing(target, areas, **kw):
        if len(walked) >= 4:             # 保险丝: 两轮该有的 4 次寻路走完就收工
            raise KeyboardInterrupt
        walked.append(target)
        return False

    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    _entry_route_with_small_budget(monkeypatch)
    switched = []
    monkeypatch.setattr(main, "switch_server", lambda biome: switched.append(biome))
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    # 每轮: 先在花园寻路到洞口(150,40), 传送到蚁穴后再按时块的 location(1,2) 寻路.
    # 换服后的第 2 轮**又从洞口重走一遍**; 少了 reset() 的话第 2 轮的 _StageState
    # 还停在蚁穴那段, 会直接 "arrived" 跳过洞口, walked 里就只剩 (1,2) 了.
    assert walked == [(150, 40), (1, 2), (150, 40), (1, 2)]
    assert switched == ["garden"]        # 第 1 轮攒够 1 次短局就换了一次服


def test_run_worker_resets_stage_state_when_re_entering_from_the_title_screen(monkeypatch):
    """从标题页重新进场 = 回到路线第一段, 下一轮必须重新走一遍洞口。

    上面那条测的是换服后的 reset(); 这条测的是**循环顶 click_start_game 分支里**
    的那一句 —— 在蚁穴里被踢回标题页(掉线/被踢/手动退出)时, 人重新进场是落在
    **花园**, 而 _StageState 还停在蚁穴那一段。读不到服务器号的当下,
    _StageState 是"人在哪张图"的唯一依据, 所以少了这句 reset(),
    _run_entry_route 第一圈就 "arrived", 人站在花园里却直接按蚁穴坐标系寻路。

    **这一轮的触发必须是标题页, 不能是 switch_server** —— 换服后面那句 reset()
    会把这里要测的东西盖掉, 用例看着绿其实什么都没测(这个坑之前真踩过)。
    所以本用例 auto_switch_server=False, 并且 switch_server 一旦被调到就直接 fail。
    """
    _stub_run_worker_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)],
        "farming_duration": 9999,        # 怎么都刷不满 -> 每轮都是短局
        "short_round_limit": 1,
        "enemy_ai_enabled": False,
        "auto_switch_server": False,     # 换服这条路整个关掉, 见 docstring
        "biome": "garden",
        "map_name": "anthell",
        "route": route,
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "_route_blocker", lambda r: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)      # maps/garden.png 还没采
    # 见上一条测试同名的这行: 跟 _StageState reset 无关, 不打桩会读到别的用例
    # 留下的全局 MAP、悄悄把 (1,2) 换成别的点.
    monkeypatch.setattr(main, "load_binary_map", lambda: None, raising=False)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "switch_server",
                        lambda *a, **k: pytest.fail("本用例不该换服 —— 换服的 reset() "
                                                    "会盖住标题页那一句, 测了等于没测"))

    # 每轮开头都在标题页(= 上一轮末被踢回去了), 点了开始就进局.
    at_menu = {"yes": True}
    monkeypatch.setattr(main, "on_start_screen", lambda: at_menu["yes"])
    monkeypatch.setattr(main, "click_start_game",
                        lambda: at_menu.__setitem__("yes", False) or True)

    walked = []

    def pathing(target, areas, **kw):
        if len(walked) >= 4:             # 保险丝: 两轮该有的 4 次寻路走完就收工
            raise KeyboardInterrupt
        walked.append(target)
        if len(walked) % 2 == 0:         # 刷怪那次寻路之后 = 又被踢回标题页
            at_menu["yes"] = True
        return False

    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    _entry_route_with_small_budget(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    # 两轮都: 先在花园寻路到洞口(150,40), 再按时块的 location(1,2) 寻路.
    # 少了 click_start_game 分支里那句 reset(), 第 2 轮的 _StageState 还停在蚁穴,
    # 会直接 "arrived" 跳过洞口 -> walked 第 3 项变成 (1,2).
    assert walked == [(150, 40), (1, 2), (150, 40), (1, 2)]


# ── 距离/停住判定跟着真实玩家锚点走, 不跟屏幕中心 (2026-09-22) ────────────────

def _stub_drive_env(monkeypatch):
    """把 _drive_and_check_stall 的实机依赖打桩, 只留"算不算停住"这条逻辑.

    main.overlay 是 run_worker 运行时才 global 赋上的, 测试里根本不存在 —— 所以用
    raising=False 直接塞一个空壳进去。"""
    import types
    moved = []
    monkeypatch.setattr(main, "overlay", types.SimpleNamespace(update=lambda **k: None),
                        raising=False)
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda pos: moved.append(pos))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "clamp_to_screen", lambda x, y: (x, y))
    monkeypatch.setattr(main, "execute_anti_stuck", lambda *a, **k: None)
    return moved


def test_stay_put_is_measured_against_the_real_anchor(monkeypatch):
    """aim/flee/mythic 那几个函数在"保持距离"时原样返回收到的中心, 所以卡住检测必须
    拿同一个中心比。比 SCREEN_CENTER 的话, 真实锚点一偏(地图边界/非全屏), "刻意停住"
    会被当成"在移动", 卡住检测每隔几 tick 误判一次脱困。"""
    _stub_drive_env(monkeypatch)
    center = (798.0, 472.5)
    history = []
    main._drive_and_check_stall(center, (5, 5), history, "清青怪", "保持距离", center=center)
    assert history == []            # 刻意停住 -> 不往卡住检测里塞样本


def test_moving_to_the_old_screen_centre_counts_as_movement(monkeypatch):
    """真实锚点不是屏幕中心时, 走向屏幕中心是一次真实移动, 不是"停住"."""
    import enemy_detect
    _stub_drive_env(monkeypatch)
    center = (798.0, 472.5)
    history = []
    main._drive_and_check_stall(enemy_detect.SCREEN_CENTER, (5, 5), history,
                                "索敌中", "追击", center=center)
    assert history == [(5, 5)]


def test_drive_defaults_to_enemy_detect_current_center(monkeypatch):
    """不显式传 center 时退回 enemy_detect.current_center(), 不是 SCREEN_CENTER."""
    import enemy_detect
    _stub_drive_env(monkeypatch)
    monkeypatch.setattr(enemy_detect, "_last_center", (100.0, 200.0))
    history = []
    main._drive_and_check_stall((100.0, 200.0), (5, 5), history, "清青怪", "保持距离")
    assert history == []


def test_circling_does_not_feed_the_stall_detector(monkeypatch):
    """绕圈(蠕虫)时净位移最多一个圈的直径: 半径 60 屏幕像素在蚁穴(zoom 0.315, 一格小地图
    约 37 像素)只有 ~3 格, 低于 chase_is_stalled 的 4 格门槛 —— 喂进去就必定被判卡住、
    触发脱困乱跳。绕圈 tick 不记样本, 还要清掉之前的旧样本(否则绕回原处时拿旧样本一比
    又是"没动")。"""
    moved = _stub_drive_env(monkeypatch)
    monkeypatch.setattr(main, "execute_anti_stuck",
                        lambda *a, **k: pytest.fail("绕圈被当成卡住"))
    history = [(5, 5)] * main.enemy_detect.CHASE_STALL_WINDOW
    got = main._drive_and_check_stall((1000.0, 540.0), (5, 5), history, "索敌中", "绕圈打",
                                      center=(960.0, 540.0), track_stall=False)
    assert got == "moved" and moved == [(1000.0, 540.0)]
    assert history == []


# ── 换服计数: 只要死了就算一次 ──────────────────────────────────────────────
#
# 用户(2026-09-25): 自动换服不好使 —— "只要死了就算一次死亡"。换服只看"连续 N 轮没刷
# 满时长"; RL 模式死了是就地复活, 那一轮照样跑满 300 秒, 于是永远攒不够次数。

def _switch_env(monkeypatch, deaths_per_round, *, limit=2):
    """每轮都"刷满"(farming_duration=0), 但 RL 那一段报告就地复活了几次。"""
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)], "farming_duration": 0,
        "short_round_limit": limit, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "desert", "map_name": "desert", "route": map_routes.route_for("desert"),
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main.florr_settings, "ensure_flag", lambda *a: ("unchanged", ""))
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda *a, **k: True)
    rounds = iter(deaths_per_round)

    def farm(*a, **k):
        try:
            main._RL_ROUND_DEATHS = next(rounds)
        except StopIteration:
            raise KeyboardInterrupt
        return True

    monkeypatch.setattr(main, "auto_farming", farm)
    switched = []
    monkeypatch.setattr(main, "switch_server", lambda *a, **k: switched.append(1))
    return switched


def test_deaths_inside_a_full_rl_round_count_towards_switching(monkeypatch):
    switched = _switch_env(monkeypatch, [2])
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert switched == [1]                  # 一轮里死两次 = 攒够 2


def test_single_deaths_add_up_across_rounds(monkeypatch):
    switched = _switch_env(monkeypatch, [1, 1])
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert switched == [1]


def test_a_clean_full_round_clears_the_count(monkeypatch):
    """跑满而且一次没死 = 这个服务器没问题, 计数清零。"""
    switched = _switch_env(monkeypatch, [1, 0, 1])
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert switched == []


# ── 死在蚁穴 -> 重生还在蚁穴, 但阶段猜测回到花园 (2026-09-27 实机: 3 轮各白烧 180 秒) ──

def test_stage_state_sync_to():
    route = map_routes.route_for("anthell")
    st = main._StageState(route)
    st.sync_to("anthell")
    assert st.map_name == "anthell"
    st.sync_to("garden")
    assert st.map_name == "garden"
    st.sync_to("desert")               # 不在路线上 -> 不动
    assert st.map_name == "garden"


def test_entry_route_opens_the_stage_door_before_pathing(monkeypatch):
    # 花园图的门全是墙; 走门这一段要先把这一段的门挖开, 再寻路 —— 否则目标是墙, 规划直接失败.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch, portal=(133, 234))
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    calls = []
    monkeypatch.setattr(main, "apply_map", lambda n: calls.append(("apply", n)))
    monkeypatch.setattr(main, "open_map_rects", lambda rects: calls.append(("open", tuple(rects))))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: calls.append(("walk", loc)) or False)
    main._run_entry_route(route, main._StageState(route), timeout=5)
    assert calls[:3] == [
        ("apply", "garden"),
        ("open", tuple(map_routes.PORTAL_OPENINGS[("garden", (133, 234))])),
        ("walk", (133, 234)),
    ]


def test_entry_route_does_not_open_anything_on_arrival(monkeypatch):
    _quiet_entry_env(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    opened = []
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "open_map_rects", lambda rects: opened.append(rects))
    route = map_routes.route_for("desert")
    assert main._run_entry_route(route, main._StageState(route)) == "arrived"
    # 唯一一次调用是退出时那道"全部关上"(finally 里的 open_map_rects(())) —— 没有任何门被挖开
    assert opened == [()]


def test_entry_route_trusts_canvas_zone_label_over_stage_guess(monkeypatch):
    """人其实在蚁穴、状态变量却在花园: 不能拿花园的路线去走蚁穴的墙."""
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: "anthell")
    walked, applied = [], []
    monkeypatch.setattr(main, "apply_map", lambda n: applied.append(n))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    st = main._StageState(route)
    assert main._run_entry_route(route, st, timeout=5) == "arrived"
    assert walked == []                # 一步花园路线都不该走
    assert applied == ["anthell"]
    assert st.map_name == "anthell"


def test_entry_route_canvas_zone_label_beats_a_garden_server_id(monkeypatch):
    # 服务器号(读得到时是"权威")说花园, 画布区域名说蚁穴 —— 以画布为准.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: "anthell")
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    walked = []
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    assert main._run_entry_route(route, main._StageState(route), timeout=5) == "arrived"
    assert walked == []


def test_entry_route_still_walks_when_canvas_zone_is_unknown(monkeypatch):
    # 读不到区域名(None) = 行为跟以前一字不差: 按阶段猜测走花园路线.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    walked = []
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    main._run_entry_route(route, main._StageState(route), timeout=5)
    assert len(walked) == 1


def test_entry_route_uses_the_minimap_scale_when_the_zone_label_is_unknown(monkeypatch):
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("sewers")
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")   # 同一台服务器
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: None)
    monkeypatch.setattr(main, "_canvas_scale_zone", lambda r: "sewers")
    applied, walked = [], []
    monkeypatch.setattr(main, "apply_map", lambda n: applied.append(n))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    st = main._StageState(route)
    assert main._run_entry_route(route, st, timeout=5) == "arrived"
    assert walked == []                       # 一步花园路线都不该走
    assert applied == ["sewers"] and st.map_name == "sewers"


def test_entry_route_uses_the_minimap_scale_on_the_anthell_route_too(monkeypatch):
    # 区域名读不出 + 服务器号读不出: 缩放比(花园 0.004691 / 蚁穴 0.004617, 都是实测值)分得开,
    # 说在蚁穴就直接判 arrived, 一步花园路线都不走.
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: None)
    monkeypatch.setattr(main, "_canvas_scale_zone", lambda r: "anthell")
    applied, walked = [], []
    monkeypatch.setattr(main, "apply_map", lambda n: applied.append(n))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    st = main._StageState(route)
    assert main._run_entry_route(route, st, timeout=5) == "arrived"
    assert walked == []
    assert applied == ["anthell"] and st.map_name == "anthell"


def _false_hop_env(monkeypatch, route):
    """走门那一段"传送被误判成功"(连续复查都判已离开, 其实人还在花园): 服务器号说花园, 区域名读不出,
    缩放比说人还在花园 —— 返回 (walked, applied)."""
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: None)
    monkeypatch.setattr(main, "_canvas_scale_zone", lambda r: "garden")
    monkeypatch.setattr(main, "_walk_toward_visible_portal", lambda *a, **k: True)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda *a, **k: None)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))   # 离目标框远 = 已离开
    walked, applied = [], []
    monkeypatch.setattr(main, "apply_map", lambda n: applied.append(n))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    return walked, applied


def test_entry_route_false_hop_on_the_anthell_route_walks_the_garden_again(monkeypatch):
    """传送被误判成功(advance 过 + hopped) —— 下一圈缩放比还说花园, 就不能因为 hopped 判 arrived,
    要回花园那段重走; 从头到尾没有 apply_map("anthell")."""
    _quiet_entry_env(monkeypatch)
    route = _anthell_route(monkeypatch)
    walked, applied = _false_hop_env(monkeypatch, route)
    st = main._StageState(route)
    assert main._run_entry_route(route, st, timeout=0.05) == "timeout"
    garden_target = route.stages[0].walk_to
    assert walked.count(garden_target) >= 2 and set(walked) == {garden_target}
    assert "anthell" not in applied


def test_entry_route_false_hop_on_the_sewers_route_walks_the_garden_again(monkeypatch):
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("sewers")
    walked, applied = _false_hop_env(monkeypatch, route)
    assert main._run_entry_route(route, main._StageState(route), timeout=0.05) == "timeout"
    garden_target = route.stages[0].walk_to
    assert walked.count(garden_target) >= 2 and set(walked) == {garden_target}
    assert "sewers" not in applied


def test_entry_route_trusts_the_stage_state_right_after_a_confirmed_hop(monkeypatch):
    """走门传送成功(连续复查都判"已离开")后: 服务器号还说花园(同一台服务器), 区域名和缩放比也
    读不出 —— 该信阶段状态, 不该再拿花园的路线去走下水道的墙。"""
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("sewers")
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    monkeypatch.setattr(main, "_canvas_scale_zone", lambda r: None)
    monkeypatch.setattr(main, "_walk_toward_visible_portal", lambda *a, **k: True)
    monkeypatch.setattr(main, "_canvas_portal_world_offset", lambda *a, **k: None)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, 10))   # 离目标框远 = 已离开
    walked, applied = [], []
    monkeypatch.setattr(main, "apply_map", lambda n: applied.append(n))
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    st = main._StageState(route)
    assert main._run_entry_route(route, st, timeout=5) == "arrived"
    assert walked == [(86, 185)]              # 花园那一段只走了一次
    assert applied[-1] == "sewers" and st.map_name == "sewers"


def test_entry_route_without_a_hop_still_believes_the_server_id(monkeypatch):
    # 没有"刚传送过": 服务器号说花园就是花园(死亡重生还在同一张图上时靠区域名/缩放比纠正, 不靠这条)
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("sewers")
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    monkeypatch.setattr(main, "_canvas_scale_zone", lambda r: None)
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    walked = []
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: walked.append(loc) or False)
    st = main._StageState(route)
    st.advance()                               # 阶段状态说"已经在下水道了"
    main._run_entry_route(route, st, timeout=5)
    assert walked                              # 服务器号说花园 -> 照旧走花园那段(不是 hop 后第一圈)


def test_entry_route_syncs_the_stage_state_even_when_the_zone_agrees_with_the_server_id(
        monkeypatch):
    """乐观推进成了下水道, 区域名和服务器号都说花园 —— 两个说的一样也得把阶段猜测拽回花园。
    留着那个陈旧值的话, 下一圈服务器号一读不到, current 就退回 stage_state.map_name 又变成下水道。"""
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("sewers")
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: "garden")
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda loc, area, **kw: (_ for _ in ()).throw(KeyboardInterrupt))
    st = main._StageState(route)
    st.advance()                               # 阶段状态说"已经在下水道了"
    with pytest.raises(KeyboardInterrupt):
        main._run_entry_route(route, st, timeout=5)
    assert st.map_name == "garden"


def test_entry_route_closes_the_runtime_door_on_every_exit(monkeypatch):
    """走门那一段挖开的门, 出 _run_entry_route 就得关上 —— 不然超时/被打断回去之后, 紧接着的
    刷怪寻路会把那扇门当普通空地踩上去被传走。"""
    _quiet_entry_env(monkeypatch)
    route = map_routes.route_for("anthell")
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: "garden")
    monkeypatch.setattr(main, "apply_map", lambda n: None)     # 别让 apply_map 顺手清掉
    seen = []

    def pathing(loc, area, **kw):
        seen.append(tuple(main.utils.MAP_OPEN_RECTS))
        raise KeyboardInterrupt
    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    with pytest.raises(KeyboardInterrupt):
        main._run_entry_route(route, main._StageState(route), timeout=5)
    assert seen and seen[0]                     # 走这一段时门是挖开的
    assert main.utils.MAP_OPEN_RECTS == ()      # 回去时关上了


def test_entry_route_never_reads_the_canvas_on_single_stage_routes(monkeypatch):
    # 单图路线(沙漠/海洋)压根没有"我在哪一段"的问题 —— 连画布都不该碰.
    _quiet_entry_env(monkeypatch)
    reads = []
    monkeypatch.setattr(main, "_canvas_zone_map", lambda: reads.append(1) or "anthell")
    monkeypatch.setattr(main, "_canvas_scale_zone", lambda r: reads.append(1) or "anthell")
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main.florr_server, "current_map_name", lambda ev: None)
    route = map_routes.route_for("desert")
    assert main._run_entry_route(route, main._StageState(route)) == "arrived"
    assert reads == []


def test_canvas_zone_and_scale_reads_never_drain_the_canvas_log(monkeypatch):
    """认图的两次读都只能偷看 __canvasLog: drain 会清空日志(把 scan_enemies /
    canvas_player_world 的帧偷走), 而且每次白等 0.3 秒 —— 进场路线每圈都要读一次。
    清空还会互相打断: 先 drain 一遍再读缩放比, 读的就是刚被清空的那份日志。"""
    monkeypatch.setattr(main, "_drain_second_newest_frame",
                        lambda *a, **k: pytest.fail("认图不该清空画布日志"))
    monkeypatch.setattr(main, "canvas_zone_map", lambda: "anthell")
    assert main._canvas_zone_map() == "anthell"

    monkeypatch.setattr(main.utils, "SCREEN_HEIGHT", main.utils._REF_HEIGHT)
    monkeypatch.setattr(main.utils, "canvas_minimap_scale",
                        lambda: main.utils.MINIMAP_SCALE_HINTS["sewers"])
    assert main._canvas_scale_zone(map_routes.route_for("sewers")) == "sewers"
    monkeypatch.setattr(main.utils, "canvas_minimap_scale", lambda: None)
    assert main._canvas_scale_zone(map_routes.route_for("sewers")) is None


def test_force_utf8_stdio_lets_a_gbk_stream_print_emoji(monkeypatch):
    # 中文 Windows 上 stdout 是管道时按 GBK 编码, print("❌ ...") 直接 UnicodeEncodeError。
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="gbk")
    monkeypatch.setattr(main.sys, "stdout", out)
    monkeypatch.setattr(main.sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="gbk"))
    main._force_utf8_stdio()
    print("❌ 失败", file=out)
    out.flush()
    assert raw.getvalue() == "❌ 失败\n".encode("utf-8")


def test_force_utf8_stdio_makes_each_line_reach_the_gui_right_away(monkeypatch):
    # 打包版(PyInstaller)不认 GUI 给的 PYTHONUNBUFFERED: 管道上的 stdout 是块缓冲, worker
    # 的日志攒满 8KB 或进程退出才出来, 面板上一行都看不到(v1.0.2 实机 2026-09-27)。
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="gbk")
    monkeypatch.setattr(main.sys, "stdout", out)
    monkeypatch.setattr(main.sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="gbk"))
    main._force_utf8_stdio()
    print("🎮 开始自动寻路", file=out)          # 不手动 flush
    assert raw.getvalue() == "🎮 开始自动寻路\n".encode("utf-8")


def test_force_utf8_stdio_survives_missing_streams(monkeypatch):
    # pythonw 没有控制台时 sys.stdout 是 None: 不能因此启动失败
    monkeypatch.setattr(main.sys, "stdout", None)
    monkeypatch.setattr(main.sys, "stderr", None)
    main._force_utf8_stdio()


# ── 蚁群 + 蚁穴 U 怪规避半径 (用户 2026-09-27) ────────────────────────────────

def test_maybe_scan_enemies_uses_per_map_avoid_trigger(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: [])
    monkeypatch.setattr(main.enemy_detect, "select_action",
                        lambda dets, **k: seen.update(k) or ("wander", None))
    now = main.ENEMY_SCAN_INTERVAL + 1.0
    for map_name, want in (("anthell", 200), ("desert", main.AVOID_TRIGGER_PX)):
        monkeypatch.setattr(main.utils, "MAP", map_name)
        main._maybe_scan_enemies(True, now, 0.0, ("wander", None), [])
        assert seen["avoid_trigger_px"] == want


def test_auto_farming_kites_a_swarm(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))   # 区内
    swarm = {"center": (1300, 540), "nearest": (1260, 540), "count": 6}
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("swarm", swarm, [(1, 2)]), [], 0.0, True))
    monkeypatch.setattr(main, "MYTHIC_LATCH_ENABLED", False)
    moved = {}

    def swarm_move(sw, center, **kw):
        moved["swarm"], moved["repel"] = sw, kw.get("repel_positions")
        return (1234.0, 567.0)

    class Done(Exception):
        pass

    def drive(mouse_target, *a, **kw):
        moved["target"] = mouse_target
        raise Done

    monkeypatch.setattr(main.enemy_detect, "swarm_move_target", swarm_move)
    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    monkeypatch.setattr(main, "random_walkable_point",
                        lambda *a, **k: pytest.fail("有蚁群却交回了漫游"))
    with pytest.raises(Done):
        main.auto_farming(AREA, 30, enemy_ai_enabled=True)
    assert moved == {"swarm": swarm, "repel": [(1, 2)], "target": (1234.0, 567.0)}


def test_auto_farming_screenshots_for_the_death_screen_only_every_half_second(monkeypatch):
    # 第六份录像: 刷怪主循环每拍近 1 秒, 每拍两张截图查死亡/开局画面是其中一块
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("flee", [(1100.0, 540.0)]), [], 0.0, True))
    monkeypatch.setattr(main, "MYTHIC_LATCH_ENABLED", False)
    clock = {"t": 1000.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    shots = []
    monkeypatch.setattr(main, "on_death_screen", lambda: shots.append(clock["t"]) or False)

    class Done(Exception):
        pass
    drives = []

    def drive(*a, **kw):
        drives.append(1)
        clock["t"] += 0.1
        if len(drives) >= 20:
            raise Done
    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    with pytest.raises(Done):
        main.auto_farming(AREA, 300, enemy_ai_enabled=True)
    assert 3 <= len(shots) <= 5                     # 20 拍 × 0.1 秒 = 2 秒, 半秒一张


def test_loop_clock_reports_where_the_time_goes(capsys):
    c = main._LoopClock(every=30.0, now=0.0)
    for i in range(10):
        c.part("读位置", 0.05)
        c.part("索敌", 0.2)
        c.tick(0.3 if i % 2 else 0.5, now=float(i))
    c.tick(2.0, wander=True, now=10.0)
    assert capsys.readouterr().out == ""            # 不到 30 秒不打
    line = c.tick(0.4, now=31.0)
    assert "反应拍 11 个" in line and "每拍中位 400ms" in line and "漫游腿 1 条" in line
    assert "读位置 42" in line and "索敌 167" in line   # 按拍数平均(12 拍)


def test_auto_farming_chase_moves_by_species(monkeypatch):
    # 追击分支走 enemy_detect.chase_move_target(蠕虫绕圈、其余到停步半径就停), 不再直接调 aim.
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))   # 区内
    target = {"species": "worm", "rarity": "Mythic", "screen_pos": (1000, 540)}
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("chase", target, 60, [(1, 2)]), [], 0.0, True))
    monkeypatch.setattr(main, "MYTHIC_LATCH_ENABLED", False)
    moved = {}

    def chase_move(tgt, hold_px, center, **kw):
        moved["args"] = (tgt, hold_px, kw.get("repel_positions"))
        return (1234.0, 567.0)

    class Done(Exception):
        pass

    def drive(mouse_target, *a, **kw):
        moved["target"] = mouse_target
        raise Done

    monkeypatch.setattr(main.enemy_detect, "chase_move_target", chase_move)
    monkeypatch.setattr(main.enemy_detect, "aim_mouse_target",
                        lambda *a, **k: pytest.fail("追击绕过了按物种的走位"))
    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    with pytest.raises(Done):
        main.auto_farming(AREA, 30, enemy_ai_enabled=True)
    assert moved == {"args": (target, 60, [(1, 2)]), "target": (1234.0, 567.0)}


@pytest.mark.parametrize("species, want_track", [
    ("worm", False),                                            # 蠕虫: stall_exempt, 不记卡住样本
    ("hornet", True), ("wasp", True), ("mantis", True),         # 射手绕圈照样记: 绕进墙角要能脱困
    ("soldier_ant", True), ("spider", True), ("rock", True)])
def test_auto_farming_only_stall_exempt_species_skip_stall_tracking(monkeypatch, species, want_track):
    # 是否记卡住样本看特性表的 stall_exempt(目前只有蠕虫), 不看 tactic: 射手绕圈也要能脱困。
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))
    target = {"species": species, "rarity": "Mythic", "screen_pos": (1000, 540)}
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("chase", target, 60, []), [], 0.0, True))
    monkeypatch.setattr(main, "MYTHIC_LATCH_ENABLED", False)
    seen = {}

    class Done(Exception):
        pass

    def drive(mouse_target, *a, **kw):
        seen["track"] = kw.get("track_stall", True)
        raise Done

    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    with pytest.raises(Done):
        main.auto_farming(AREA, 30, enemy_ai_enabled=True)
    assert seen["track"] is want_track


# ── 躲怪别往墙里跑 (2026-09-28 蚁穴录像: 往左下躲究极兵蚁, 一头顶在刷怪带下沿, 速度
# 304 -> 11, 被贴身打死) ─────────────────────────────────────────────────────

def _corridor(y0=85, y1=96, size=300):
    import numpy as np
    m = np.zeros((size, size), dtype=np.uint8)
    m[y0:y1 + 1, 5:200] = 255
    return m


def test_flee_into_a_wall_is_turned_along_the_corridor():
    # 贴着刷怪带下沿(y=95), 理想方向左下 -> 下面是墙, 转成沿走廊往左
    center = (960.0, 540.0)
    want = (960.0 - 283.0, 540.0 + 283.0)                 # 左下 45°
    got = main._steer_clear_of_walls(want, center, (60, 95), _corridor())
    dx, dy = got[0] - center[0], got[1] - center[1]
    assert dx < 0 and abs(dy) < abs(dx)                    # 主要往左
    assert math.hypot(dx, dy) == pytest.approx(math.hypot(283.0, 283.0))


def test_flee_with_open_space_is_left_alone():
    center = (960.0, 540.0)
    want = (660.0, 540.0)                                   # 正左, 走廊里一路通
    assert main._steer_clear_of_walls(want, center, (60, 90), _corridor()) == want


def test_flee_never_turns_back_toward_the_threat():
    # 死胡同: 除了来路(右边)全是墙 -> 不掉头, 保持原方向
    import numpy as np
    m = np.zeros((300, 300), dtype=np.uint8)
    m[90, 60:200] = 255                                     # 只剩往右一条路
    center = (960.0, 540.0)
    want = (660.0, 540.0)                                   # 往左躲(怪在右边)
    assert main._steer_clear_of_walls(want, center, (60, 90), m) == want


def test_flee_steering_is_skipped_without_a_map_or_position():
    center = (960.0, 540.0)
    assert main._steer_clear_of_walls((700.0, 540.0), center, None, _corridor()) == (700.0, 540.0)
    assert main._steer_clear_of_walls((700.0, 540.0), center, (60, 90), None) == (700.0, 540.0)
    assert main._steer_clear_of_walls(center, center, (60, 90), _corridor()) == center


def test_auto_farming_flee_is_steered_clear_of_walls(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("flee", [(1100, 540)]), [], 0.0, True))
    monkeypatch.setattr(main, "MYTHIC_LATCH_ENABLED", False)
    seen = {}

    class Done(Exception):
        pass

    def steer(mouse_target, center, pos, binary_map):
        seen["args"] = (pos, binary_map is not None)
        return (1.0, 2.0)

    def drive(mouse_target, *a, **kw):
        seen["target"] = mouse_target
        raise Done

    monkeypatch.setattr(main, "_steer_clear_of_walls", steer)
    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    with pytest.raises(Done):
        main.auto_farming(AREA, 30, enemy_ai_enabled=True)
    assert seen == {"args": ((20, 40), True), "target": (1.0, 2.0)}


def test_maybe_scan_enemies_tracks_charging_ultras_and_passes_the_early_radius(monkeypatch):
    seen = {}
    dets = [{"species": "soldier_ant", "rarity": "Ultra", "screen_pos": (1300, 540),
             "world": (900.0, 0.0)}]
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: dets)
    monkeypatch.setattr(main.enemy_detect, "last_player_world", lambda: (0.0, 0.0))
    monkeypatch.setattr(main.enemy_detect, "select_action",
                        lambda d, **k: seen.update(k, marked=d[0].get("approaching"))
                        or ("wander", None))
    monkeypatch.setattr(main, "_APPROACH", main.enemy_detect.ApproachTracker())
    for map_name, want in (("anthell", 400), ("desert", None)):
        monkeypatch.setattr(main.utils, "MAP", map_name)
        main._maybe_scan_enemies(True, main.ENEMY_SCAN_INTERVAL + 1.0, 0.0, ("wander", None), [])
        assert seen["avoid_early_px"] == want
        assert seen["marked"] is False            # 第一眼, 还算不出速度


def test_maybe_scan_enemies_cached_ticks_do_not_feed_the_tracker(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_APPROACH", type("T", (), {"update": lambda self, *a: calls.append(a)})())
    main._maybe_scan_enemies(True, 1.0, 0.99, ("wander", None), [])     # 节流, 不扫
    assert calls == []


# ── 寻路路上也躲究极 (用户 2026-09-28; 第二份录像 8 次死亡里 3 次死在去刷怪区的路上) ──

class _FakeWatch:
    """_PathingEnemyWatch 的替身: 按脚本依次回答"现在要不要躲"。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.decision = ("flee", [(1100, 540)])

    def should_flee(self):
        return self.answers.pop(0) if self.answers else False


def test_execute_path_hands_back_when_an_ultra_needs_dodging():
    moves = []
    orig = main.move_to_position

    def move(a, b, on_tick=None, **kw):
        moves.append(b)
        return (on_tick(b) if on_tick else None) or True

    main.move_to_position = move
    try:
        got = main.execute_path([(0, 0), (1, 1), (2, 2)], enemy_watch=_FakeWatch([False, True]))
    finally:
        main.move_to_position = orig
    assert got == "enemy" and moves == [(1, 1), (2, 2)]


def test_execute_path_reaching_the_goal_wins_over_dodging():
    orig = main.move_to_position
    main.move_to_position = lambda a, b, on_tick=None, **kw: on_tick(b) or True
    try:
        got = main.execute_path([(0, 0), (1, 1)], stop_when=lambda p: True,
                                enemy_watch=_FakeWatch([True]))
    finally:
        main.move_to_position = orig
    assert got is True


def test_lazy_theta_pathing_dodges_then_replans(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "lazy_theta_star", lambda m, a, b: [a, b])
    positions = iter([(30, 30), (30, 30), (40, 40), (40, 40)])
    monkeypatch.setattr(main, "get_player_position", lambda: next(positions))
    results = iter(["enemy", True])
    monkeypatch.setattr(main, "execute_path", lambda path, **kw: next(results))
    fled = []
    monkeypatch.setattr(main, "_flee_while_pathing", lambda watch: fled.append(watch))
    watch = _FakeWatch([])
    assert main.lazy_theta_pathing((40, 40), [[(35, 35), (45, 45)]], enemy_watch=watch) is True
    assert fled == [watch]


def test_flee_while_pathing_runs_until_clear(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960.0, 540.0))
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    drives = []
    monkeypatch.setattr(main, "_drive_and_check_stall",
                        lambda target, pos, hist, *a, **kw: drives.append(target))
    main._flee_while_pathing(_FakeWatch([True, True, False]))
    assert len(drives) == 2
    assert all(t[0] < 960 for t in drives)          # 背离右边的究极


def test_flee_while_pathing_gives_up_after_a_while(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    monkeypatch.setattr(main, "_drive_and_check_stall", lambda *a, **kw: None)
    clock = {"t": 100.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]
    monkeypatch.setattr(main.time, "time", tick)
    main._flee_while_pathing(_FakeWatch([True] * 1000))
    assert clock["t"] - 100.0 <= main.PATH_FLEE_MAX_S + 3


def test_flee_while_pathing_stops_on_death(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "on_death_screen", lambda: True)
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    monkeypatch.setattr(main, "_drive_and_check_stall", lambda *a, **kw: pytest.fail("死了还在躲"))
    main._flee_while_pathing(_FakeWatch([True] * 5))


def test_flee_while_pathing_does_not_track_stalls_without_a_position(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: None)
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    seen = []
    monkeypatch.setattr(main, "_drive_and_check_stall",
                        lambda target, pos, hist, *a, **kw: seen.append(kw.get("track_stall", True)))
    main._flee_while_pathing(_FakeWatch([True, False]))
    assert seen == [False]


# ── 躲究极往哪跑: flee_planner 接进刷怪 / 寻路两处躲避(第五份录像两次死亡) ──────────

_ZOOM = 0.45


def _flee_plan_env(monkeypatch, me_world=(31488.0, 31488.0)):
    """me_world 默认落在小地图 (150, 150) 附近; 整张图都能走。"""
    import numpy as np
    monkeypatch.setattr(main.utils, "MAP", "anthell")
    monkeypatch.setattr(main.enemy_detect, "last_player_world", lambda: me_world)
    monkeypatch.setattr(main, "mouse_scale", lambda: 1.0)
    return np.full((300, 300), 255, dtype=np.uint8)


def _det_at(species, rarity, screen, me_world=(31488.0, 31488.0)):
    world = (me_world[0] + (screen[0] - 960.0) / _ZOOM, me_world[1] + (screen[1] - 540.0) / _ZOOM)
    return {"species": species, "rarity": rarity, "screen_pos": screen, "world": world,
            "bbox": (0, 0, 0, 0), "confidence": 1.0}


def test_flee_goes_around_a_crowd_instead_of_into_it(monkeypatch):
    bm = _flee_plan_env(monkeypatch)
    ultra = _det_at("soldier_ant", "Ultra", (800.0, 540.0))
    crowd = [_det_at("soldier_ant", "Legendary", (1100.0 + 60 * i, 540.0 + 60 * j))
             for i in range(3) for j in (-1, 0, 1)]
    tx, ty = main._FLEE_PLAN.target([ultra["screen_pos"]], [ultra] + crowd, (960.0, 540.0),
                                    (150, 150), bm, now=10.0)
    assert abs(ty - 540) > abs(tx - 960) * 0.5      # 往上或往下绕, 不是正右边撞进怪群
    assert tx > 960 - 150                            # 也不是回头冲究极
    # 老办法就是正右边: 这条测试就是为它写的
    old = main.enemy_detect.flee_mouse_target([ultra["screen_pos"]], center=(960.0, 540.0))
    assert abs(old[1] - 540) < 1


def test_flee_falls_back_to_the_old_way_without_a_world_position(monkeypatch):
    bm = _flee_plan_env(monkeypatch, me_world=None)
    monkeypatch.setattr(main.enemy_detect, "last_player_world", lambda: None)
    ultra = _det_at("soldier_ant", "Ultra", (800.0, 540.0))
    got = main._FLEE_PLAN.target([ultra["screen_pos"]], [ultra], (960.0, 540.0), (150, 150), bm)
    want = main._steer_clear_of_walls(
        main.enemy_detect.flee_mouse_target([ultra["screen_pos"]], center=(960.0, 540.0)),
        (960.0, 540.0), (150, 150), bm)
    assert got == want


def test_flee_falls_back_when_the_planner_blows_up(monkeypatch, capsys):
    bm = _flee_plan_env(monkeypatch)
    monkeypatch.setattr(main.flee_planner, "plan_flee",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("坏了")))
    ultra = _det_at("soldier_ant", "Ultra", (800.0, 540.0))
    tx, ty = main._FLEE_PLAN.target([ultra["screen_pos"]], [ultra], (960.0, 540.0), (150, 150), bm)
    assert tx > 960                                  # 老办法: 背离左边的究极
    assert "躲避规划出错" in capsys.readouterr().out


def test_flee_plan_keeps_its_goal_between_ticks_and_forgets_it_later(monkeypatch):
    bm = _flee_plan_env(monkeypatch)
    calls = []

    def fake(bm_, me, chasers, crowd, prefer=None):
        calls.append(prefer)
        return {"dir": (0.0, 1.0), "goal": (150, 170), "lead": 3.0, "safe": True}
    monkeypatch.setattr(main.flee_planner, "plan_flee", fake)
    ultra = _det_at("soldier_ant", "Ultra", (800.0, 540.0))
    args = ([ultra["screen_pos"]], [ultra], (960.0, 540.0), (150, 150), bm)
    first = main._FLEE_PLAN.target(*args, now=10.0)
    again = main._FLEE_PLAN.target(*args, now=10.05)        # 不到 FLEE_REPLAN_S: 沿用, 不重算
    main._FLEE_PLAN.target(*args, now=10.2)
    main._FLEE_PLAN.target(*args, now=10.2 + main.FLEE_PLAN_FORGET_S + 0.5)
    assert first == again == (960.0, 940.0)
    assert calls == [None, (150, 170), None]


def test_flee_plan_takes_an_explicit_world_position(monkeypatch):
    # analyze_recording 离线重放时没有"最近一次索敌扫描", 自己把每帧的世界坐标递进来
    bm = _flee_plan_env(monkeypatch)
    monkeypatch.setattr(main.enemy_detect, "last_player_world", lambda: None)
    seen = {}
    monkeypatch.setattr(main.flee_planner, "plan_flee",
                        lambda bm_, me, *a, **k: seen.setdefault("me", me) and None)
    ultra = _det_at("soldier_ant", "Ultra", (800.0, 540.0))
    main._FLEE_PLAN.target([ultra["screen_pos"]], [ultra], (960.0, 540.0), (150, 150), bm,
                           me_world=(31488.0, 31488.0))
    assert seen["me"] == pytest.approx((150.0, 150.0), abs=0.1)


def test_flee_counts_every_nearby_ultra_as_a_chaser(monkeypatch):
    bm = _flee_plan_env(monkeypatch)
    seen = {}

    def fake(bm_, me, chasers, crowd, prefer=None):
        seen["chasers"], seen["crowd"] = chasers, crowd
        return None
    monkeypatch.setattr(main.flee_planner, "plan_flee", fake)
    trig = _det_at("soldier_ant", "Ultra", (800.0, 540.0))
    other = _det_at("worm", "Ultra", (960.0, 900.0))              # 没触发躲避, 但 450px 内
    far = _det_at("soldier_ant", "Ultra", (960.0 + 600, 540.0))   # 太远
    baby = _det_at("baby_ant", "Mythic", (1000.0, 540.0))         # 被动的不算怪群
    leg = _det_at("soldier_ant", "Legendary", (1000.0, 600.0))
    main._FLEE_PLAN.target([trig["screen_pos"]], [trig, other, far, baby, leg],
                           (960.0, 540.0), (150, 150), bm)
    assert len(seen["chasers"]) == 2 and len(seen["crowd"]) == 1


def test_move_to_position_hands_over_without_braking(monkeypatch):
    # 交班(躲究极 / 进区了)不踩刹车: reset_keyboard 会把鼠标挪回屏幕中心
    _stub_move_env(monkeypatch, pos=(10, 10))
    calls = []
    monkeypatch.setattr(main, "reset_keyboard", lambda: calls.append("reset"))
    monkeypatch.setattr(main, "release_keys", lambda: calls.append("release"))
    assert main.move_to_position((10, 10), (999, 999), on_tick=lambda p: "enemy") == "enemy"
    assert calls == ["release"]


def test_flee_while_pathing_hands_back_without_braking(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))
    calls = []
    monkeypatch.setattr(main, "reset_keyboard", lambda: calls.append("reset"))
    monkeypatch.setattr(main, "release_keys", lambda: calls.append("release"))
    monkeypatch.setattr(main, "_drive_and_check_stall", lambda *a, **kw: None)
    main._flee_while_pathing(_FakeWatch([True, False]))
    assert calls == ["release"]


def test_release_keys_lets_go_of_attack_and_defense_but_leaves_the_mouse(monkeypatch):
    up, moved = [], []
    monkeypatch.setattr(main.pyautogui, "keyUp", up.append)
    monkeypatch.setattr(main.pyautogui, "moveTo", lambda *a, **k: moved.append(a))
    _REAL_RELEASE_KEYS()
    assert sorted(up) == ["shift", "space"] and moved == []


def test_pathing_flee_log_names_the_ultra(monkeypatch):
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960.0, 540.0))
    w = _FakeWatch([])
    d = _det_at("soldier_ant", "Ultra", (960.0, 390.0))
    d["approaching"] = True
    w.decision, w.detections = ("flee", [d["screen_pos"]]), [d]
    assert main._describe_threats(w) == "soldier_ant(Ultra) 150px 冲过来"
    assert main._describe_threats(_FakeWatch([])) == "?"          # 替身没有 detections 也不能炸


# ── 人被传送门传到别的图: 第六份录像拿蚁穴的图在花园里寻路卡了 13 分钟 ─────────────

def test_pathing_hands_back_when_the_player_is_on_another_map(monkeypatch, capsys):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main.utils, "MAP", "anthell")
    monkeypatch.setattr(main, "canvas_zone_map", lambda: "garden")
    monkeypatch.setattr(main, "get_player_position",
                        lambda: pytest.fail("人不在这张图上, 不该再按这张图读位置"))
    assert main.lazy_theta_pathing((18, 91), [[(7, 85), (61, 96)]]) is False
    assert main._WRONG_MAP_SEEN == "garden"
    assert "人在 garden" in capsys.readouterr().out


@pytest.mark.parametrize("zone", ["anthell", None])
def test_pathing_goes_on_when_on_the_right_map_or_the_zone_is_unknown(monkeypatch, zone):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main.utils, "MAP", "anthell")
    monkeypatch.setattr(main, "canvas_zone_map", lambda: zone)
    monkeypatch.setattr(main, "get_player_position", lambda: (40, 90))
    assert main.lazy_theta_pathing((18, 91), [[(7, 85), (61, 96)]]) is True
    assert main._WRONG_MAP_SEEN is None


def test_pathing_watch_only_cares_about_flee(monkeypatch):
    decisions = iter([(("chase", "x", 1, []), [], 1.0, True), (("flee", [(1, 2)]), [], 2.0, True)])
    monkeypatch.setattr(main, "_maybe_scan_enemies", lambda *a, **k: next(decisions))
    w = main._PathingEnemyWatch()
    assert w.should_flee() is False and w.should_flee() is True
    assert w.decision == ("flee", [(1, 2)])


@pytest.mark.parametrize("ai, want", [(True, True), (False, False)])
def test_walking_back_into_the_area_dodges_ultras_only_with_enemy_ai(monkeypatch, ai, want):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (90, 90))      # 区外
    seen = {}

    class Done(Exception):
        pass

    def pathing(target, area, **kw):
        seen["watch"] = kw.get("enemy_watch")
        raise Done
    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("wander", None), [], 0.0, False))
    with pytest.raises(Done):
        main.auto_farming(AREA, 30, enemy_ai_enabled=ai)
    assert isinstance(seen["watch"], main._PathingEnemyWatch) is want


@pytest.mark.parametrize("ai", [True, False])
def test_run_worker_walks_to_the_farm_dodging_ultras_only_with_enemy_ai(monkeypatch, ai):
    _stub_run_worker_env(monkeypatch)
    cfg = main._apply_worker_config({})
    monkeypatch.setattr(main, "_apply_worker_config", lambda c: dict(cfg, enemy_ai_enabled=ai))
    monkeypatch.setattr(main, "_reassert_florr_toggles", lambda *a, **k: {})
    seen = {}

    def pathing(target, area, **kw):
        seen["watch"] = kw.get("enemy_watch")
        raise KeyboardInterrupt
    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert isinstance(seen["watch"], main._PathingEnemyWatch) is ai


# ── 蚁穴: 走回花园的传送门, 传送时换服, 出生点不重置 (用户 2026-09-28) ──────────
# 直接换服 = 新服从花园出生点走过来(录像里每次 2 分多钟); 用户给的技巧: 蚁穴里走进回花园
# 的门, 传送(黑屏)那一下换服, 新服里还是蚁穴出生点。

def test_portal_staging_point_is_walkable_and_off_the_portal():
    import numpy as np
    m = np.zeros((300, 300), dtype=np.uint8)
    m[95:110, 110:140] = 255
    m[102, 121] = 0                                   # 门本身是墙
    x, y = main._portal_staging_point(m, (121, 102))
    assert m[y, x] == 255
    assert 3 <= math.hypot(x - 121, y - 102) < 4


_PW = None   # map_routes.ANTHELL_TO_GARDEN_PORTAL_WORLD, 在 _portal_env 里取


def _portal_env(monkeypatch, *, pathing=True, worlds=(), dead=False, here=(128, 102)):
    """worlds: 依次读到的自己世界坐标(None = 这一拍读不到小地图点 = 传送黑屏)。读完之后
    一直停在最后一个值(没有就当一直在门外 2000 单位)。here: 开局读到的小地图坐标。"""
    import numpy as np
    m = np.zeros((300, 300), dtype=np.uint8)
    m[95:110, 110:140] = 255
    monkeypatch.setattr(main, "load_binary_map", lambda: m)
    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)
    seen = {"pathing": [], "switch": [], "mouse": []}
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        lambda target, area, **kw: seen["pathing"].append((target, area)) or pathing)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: here)
    pw = map_routes.ANTHELL_TO_GARDEN_PORTAL_WORLD
    seq = list(worlds) or [(pw[0] + 2000.0, pw[1])]
    state = {"i": 0}

    def world():
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v
    monkeypatch.setattr(main, "canvas_player_world", world)
    monkeypatch.setattr(main, "on_death_screen", lambda: dead)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "reset_keyboard", lambda: None)
    monkeypatch.setattr(main, "_move_mouse_safely", seen["mouse"].append)
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960.0, 540.0))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "switch_server", lambda b: seen["switch"].append(b) or "srv")
    return seen


def _toward_portal(steps=6, start=2000.0, step=300.0):
    pw = map_routes.ANTHELL_TO_GARDEN_PORTAL_WORLD
    return [(pw[0] + start - i * step, pw[1]) for i in range(steps)]


def test_portal_switch_happens_once_the_teleport_starts(monkeypatch):
    seen = _portal_env(monkeypatch, worlds=_toward_portal() + [None, None])
    assert main._switch_server_via_portal("garden", (121, 102)) is True
    assert seen["switch"] == ["garden"]


def test_portal_pulser_waits_for_rest_then_pushes_toward_the_portal():
    target = (0.0, 0.0)
    p = main._PortalPulser(target, now=0.0)
    assert p.step(0.0, (500.0, 0.0)) is None                  # 先松手等停稳
    assert p.step(0.1, (500.0, 0.0)) is None
    ox, oy = p.step(0.3, (500.0, 0.0))                        # 停稳 0.2 秒以上 -> 朝门点一下
    assert ox < 0 and abs(oy) < 1e-9
    assert p.push_s == main.PORTAL_PULSE_FIRST_S


def test_portal_pulser_does_not_mistake_a_fresh_push_for_rest():
    # 刚推完那一下人才开始动: 拿推之前的样本比会误判"停稳了"又推一下(仿真里在门边来回点)
    p = main._PortalPulser((0.0, 0.0), now=0.0)
    p.step(0.0, (500.0, 0.0))
    p.step(0.3, (500.0, 0.0))                                 # 开推
    assert p.step(0.3 + p.push_s + 0.01, (495.0, 0.0)) is None   # 推完 -> 松手
    assert p.step(0.3 + p.push_s + 0.05, (480.0, 0.0)) is None   # 还在滑, 别再推
    assert p.phase == "settle"


def test_portal_switch_needs_two_misses_in_a_row(monkeypatch):
    # 单独一次读不到 = 刚好撞上 scan_enemies drain 画布日志, 不是传送
    pw = map_routes.ANTHELL_TO_GARDEN_PORTAL_WORLD
    worlds = [(pw[0] + 900, pw[1]), None, (pw[0] + 600, pw[1]), None, (pw[0] + 300, pw[1]),
              None, None]
    seen = _portal_env(monkeypatch, worlds=worlds)
    assert main._switch_server_via_portal("garden", (121, 102)) is True
    assert len(seen["switch"]) == 1
    assert len(seen["mouse"]) == 3        # 三个读得到的拍子都在朝门走, 没被单个 None 骗去换服


def test_portal_switch_gives_up_when_it_stops_getting_closer(monkeypatch):
    pw = map_routes.ANTHELL_TO_GARDEN_PORTAL_WORLD
    seen = _portal_env(monkeypatch, worlds=[(pw[0] + 1100, pw[1])])     # 顶住了, 一动不动
    clock = {"t": 0.0}
    monkeypatch.setattr(main.time, "time", lambda: clock.__setitem__("t", clock["t"] + 0.25) or clock["t"])
    assert main._switch_server_via_portal("garden", (121, 102)) is False
    assert seen["switch"] == []
    assert clock["t"] < main.PORTAL_SWITCH_TIMEOUT / 2      # 顶住几秒就放弃, 不耗光整个预算


def test_portal_switch_walks_straight_when_already_near_the_portal(monkeypatch):
    # 第四份录像: 离门 6 格, 寻路到集结点却先往下绕到 y=115, 在墙角卡了 3 分钟
    seen = _portal_env(monkeypatch, worlds=_toward_portal() + [None, None], here=(127, 103))
    main._switch_server_via_portal("garden", (121, 102))
    assert seen["pathing"] == []


def test_portal_switch_paths_over_first_when_far_away(monkeypatch):
    seen = _portal_env(monkeypatch, worlds=_toward_portal() + [None, None], here=(60, 95))
    main._switch_server_via_portal("garden", (121, 102))
    assert len(seen["pathing"]) == 1


def test_portal_switch_does_not_switch_on_the_death_screen(monkeypatch):
    seen = _portal_env(monkeypatch, worlds=[None, None], dead=True)
    assert main._switch_server_via_portal("garden", (121, 102)) is False
    assert seen["switch"] == []


# ── 走进门: 第五~七份录像跑图装 ~1000/秒 反复冲过门心, 门要人停在里面才传 ─────────

class _PortalSim:
    """带惯性的花 + 要停在里面才传送的门。按录像量的: 松手后速度时间常数 ~0.55 秒, 跑图装
    ~1000/秒; 花园那边离门心 ~30 以内、慢下来 0.5~1 秒才传。第七份录像按住推的时候比松手
    滑行加速得快(accel_tau), 读位置有 CDP 延迟(read_s: 每读一次时钟走这么久)、读到的是
    lag_s 之前的位置、还带 noise 的抖动 —— 上一版控制就是被这些弄得来回振荡。鼠标离中心
    sat_px 参照像素就满速。time.sleep 推进仿真时钟。"""

    def __init__(self, start, vel=(0.0, 0.0), vmax=1000.0, tau=0.55, sat_px=50.0,
                 teleports=True, dwell_s=0.6, accel_tau=None, read_s=0.0, lag_s=0.0,
                 noise=0.0, seed=0):
        import random
        pw = map_routes.ANTHELL_TO_GARDEN_PORTAL_WORLD
        self.target = pw
        self.p = [pw[0] + start[0], pw[1] + start[1]]
        self.v = list(vel)
        self.vmax, self.tau, self.sat = vmax, tau, sat_px
        self.accel_tau = accel_tau or tau
        self.read_s, self.lag_s, self.noise = read_s, lag_s, noise
        self.rnd = random.Random(seed)
        self.cmd = (0.0, 0.0)
        self.t = 1000.0
        self.teleports, self.dwell_s = teleports, dwell_s
        self.inside = 0.0
        self.gone = False
        self.hist = [(self.t, tuple(self.p))]

    def time(self):
        return self.t

    def sleep(self, dt):
        steps = max(1, int(round(dt / 0.005)))
        for _ in range(steps):
            h = 0.005
            m = math.hypot(*self.cmd)
            u = min(1.0, m / self.sat) if m > 1e-9 else 0.0
            ux, uy = (self.cmd[0] / m * u, self.cmd[1] / m * u) if m > 1e-9 else (0.0, 0.0)
            tau = self.accel_tau if u > 0 else self.tau
            self.v[0] += (ux * self.vmax - self.v[0]) / tau * h
            self.v[1] += (uy * self.vmax - self.v[1]) / tau * h
            self.p[0] += self.v[0] * h
            self.p[1] += self.v[1] * h
            self.t += h
            self.hist.append((self.t, (self.p[0], self.p[1])))
            d = math.hypot(self.p[0] - self.target[0], self.p[1] - self.target[1])
            if d < 50 and math.hypot(*self.v) < 150:
                self.inside += h
            else:
                self.inside = 0.0
            if self.teleports and self.inside >= self.dwell_s:
                self.gone = True
        self.hist = self.hist[-400:]

    def world(self):
        if self.read_s:
            self.sleep(self.read_s)
        if self.gone:
            return None
        want = self.t - self.lag_s
        pos = next((p for t, p in reversed(self.hist) if t <= want), self.hist[0][1])
        return (pos[0] + self.rnd.gauss(0, self.noise), pos[1] + self.rnd.gauss(0, self.noise))

    def mouse(self, pos):
        self.cmd = (pos[0] - 960.0, pos[1] - 540.0)


def _portal_sim_env(monkeypatch, sim):
    seen = _portal_env(monkeypatch, here=(121, 103))
    monkeypatch.setattr(main.time, "time", sim.time)
    monkeypatch.setattr(main.time, "sleep", sim.sleep)
    monkeypatch.setattr(main, "canvas_player_world", sim.world)
    monkeypatch.setattr(main, "_move_mouse_safely", lambda pos: (seen["mouse"].append(pos), sim.mouse(pos)))
    monkeypatch.setattr(main, "clamp_to_screen", lambda x, y: (x, y))
    monkeypatch.setattr(main, "mouse_scale", lambda: 1.0)
    return seen


_REAL_READS = dict(read_s=0.055, lag_s=0.03, noise=15.0)   # 第七份录像: 每拍 ~75ms


@pytest.mark.parametrize("start, vel, kw", [
    ((-381.0, 188.0), (0.0, 0.0), {}),                         # 第五份录像: 复活在门左下 ~420
    ((1000.0, -100.0), (0.0, 0.0), {}),
    ((-300.0, 0.0), (1000.0, 0.0), {}),                        # 正以跑图速度冲向门
    ((-381.0, 188.0), (0.0, 0.0), {"sat_px": 150.0}),          # 鼠标要推很远才满速
    ((-381.0, 188.0), (0.0, 0.0), {"sat_px": 20.0}),           # 鼠标一推就满速
    ((-381.0, 188.0), (0.0, 0.0), {"vmax": 300.0}),            # 刷怪装
    # 第七份录像的样子: 按住加速快、读数慢半拍还抖 —— 上一版在这里来回冲过门心
    ((-470.0, 0.0), (0.0, 0.0), dict(accel_tau=0.2, **_REAL_READS)),
    ((-470.0, 0.0), (0.0, 0.0), dict(accel_tau=0.2, sat_px=20.0, **_REAL_READS)),
    ((200.0, -150.0), (0.0, 0.0), dict(accel_tau=0.2, tau=0.8, **_REAL_READS)),
    ((-381.0, 188.0), (0.0, 0.0), dict(accel_tau=0.3, vmax=1300.0, **_REAL_READS)),
    # 推多久要按计时器掐: 按读位置的节拍(~75ms)掐, 加速快的跑图装最小一下就滑 ~180, 比门还宽
    ((0.0, -219.0), (0.0, 0.0), dict(tau=0.4, accel_tau=0.15, sat_px=20.0, read_s=0.055)),
    ((1000.0, -100.0), (0.0, 0.0), dict(tau=0.4, accel_tau=0.15, sat_px=20.0, read_s=0.055)),
])
def test_portal_switch_stops_inside_the_portal_and_switches(monkeypatch, start, vel, kw):
    sim = _PortalSim(start, vel, **kw)
    seen = _portal_sim_env(monkeypatch, sim)
    t0 = sim.t
    assert main._switch_server_via_portal("garden", (121, 102)) is True
    assert seen["switch"] == ["garden"]
    assert sim.t - t0 < 20.0


def test_portal_walk_loop_skips_the_pyautogui_pause_and_rarely_screenshots(monkeypatch):
    # 第六份录像: 每拍 ~0.2 秒(moveTo 后白睡 0.1 秒 + 每拍截图查死亡), 刹车来回振荡
    sim = _PortalSim((-381.0, 188.0))
    seen = _portal_sim_env(monkeypatch, sim)
    pauses, deaths = [], []
    monkeypatch.setattr(main.pyautogui, "PAUSE", 0.1)
    monkeypatch.setattr(main, "_move_mouse_safely",
                        lambda pos: (pauses.append(main.pyautogui.PAUSE), sim.mouse(pos)))
    monkeypatch.setattr(main, "on_death_screen", lambda: deaths.append(1) or False)
    assert main._switch_server_via_portal("garden", (121, 102)) is True
    assert pauses and set(pauses) == {0}
    assert main.pyautogui.PAUSE == 0.1                  # 用完还回去, 寻路那边的节奏不变
    assert len(deaths) < len(pauses) / 5


def test_reset_keyboard_does_not_sleep_between_keys(monkeypatch):
    # 6 次 pyautogui 调用 × 0.1 秒 = 每次交班 / 每段路结束原地停 0.6 秒
    seen = []
    monkeypatch.setattr(main.pyautogui, "PAUSE", 0.1)
    monkeypatch.setattr(main.pyautogui, "keyUp", lambda k: seen.append(main.pyautogui.PAUSE))
    monkeypatch.setattr(main, "keyup", lambda d: seen.append(main.pyautogui.PAUSE))
    main.reset_keyboard()
    assert seen == [0] * 6 and main.pyautogui.PAUSE == 0.1


def test_portal_switch_steps_out_and_back_in_then_gives_up(monkeypatch):
    # 站在门里就是不传送(比如刚落地时那样): 出去再进两次, 然后交回调用方直接换服
    sim = _PortalSim((-381.0, 188.0), teleports=False)
    seen = _portal_sim_env(monkeypatch, sim)
    t0 = sim.t
    assert main._switch_server_via_portal("garden", (121, 102)) is False
    assert seen["switch"] == []
    spent = sim.t - t0
    assert spent >= (main.PORTAL_REENTRY_MAX + 1) * main.PORTAL_DWELL_MAX_S
    assert spent < main.PORTAL_SWITCH_TIMEOUT


def test_portal_switch_is_for_anthell_and_factory_only():
    # 用户 2026-10-10: "工厂换服没有走传送门, 改掉, 蚁穴也是要走传送门的"
    assert main._portal_switch_target({"map_name": "anthell"}) == map_routes.ANTHELL_TO_GARDEN_PORTAL
    assert main._portal_switch_target({"map_name": "factory"}) == map_routes.FACTORY_TO_GARDEN_PORTAL
    for other in ("desert", "garden", "ocean", "jungle", "sewers"):
        assert main._portal_switch_target({"map_name": other}) is None


def test_every_switch_door_has_a_world_coordinate_and_a_budget():
    assert set(map_routes.SWITCH_DOORS) == {"anthell", "factory"}
    for name, door in map_routes.SWITCH_DOORS.items():
        assert map_routes.door_for_cell(door.cell) is door
        assert len(door.world) == 2 and door.timeout >= 60
    assert map_routes.door_for_cell((1, 1)) is None
    # 工厂复活点可能离门很远, 预算比蚁穴宽
    assert map_routes.SWITCH_DOORS["factory"].timeout > map_routes.SWITCH_DOORS["anthell"].timeout


def test_factory_door_world_coordinate_matches_its_minimap_cell():
    # 世界坐标是 2026-10-10 工厂录像里小地图绿点反解的; 小地图格是按同一条换算(跟 maps/factory.png 一个坐标系)
    import utils
    door = map_routes.SWITCH_DOORS["factory"]
    s = utils.MINIMAP_WORLD_SCALE["factory"]
    cx, cy = s * (door.world[0] + 1000.0), s * (door.world[1] + 1000.0)
    assert abs(cx - door.cell[0]) <= 1.0 and abs(cy - door.cell[1]) <= 1.0


def test_factory_door_is_a_wall_with_a_walkable_staging_point_beside_it():
    # 门那一格在 maps/factory.png 上要是墙(寻路不踩进去被传走), 旁边要有主连通区里的可走点当集结点
    import cv2
    m = cv2.imread("./maps/factory.png", cv2.IMREAD_GRAYSCALE)
    x, y = map_routes.FACTORY_TO_GARDEN_PORTAL
    assert m[y, x] == 0
    staging = main._portal_staging_point(m, map_routes.FACTORY_TO_GARDEN_PORTAL)
    assert m[staging[1], staging[0]] == 255
    assert math.hypot(staging[0] - x, staging[1] - y) <= 10


def test_factory_portal_switch_uses_the_factory_door_and_its_budget(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "load_binary_map", lambda: np.full((300, 300), 255, dtype=np.uint8))
    monkeypatch.setattr(main, "get_player_position", lambda: (54, 285))      # 离门 5 格: 直接走
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)

    def walk(world, biome, deadline):
        seen["world"], seen["biome"] = world, biome
        seen["budget"] = deadline - main.time.time()
        return True
    monkeypatch.setattr(main, "_walk_into_portal", walk)
    assert main._switch_server_via_portal("garden", map_routes.FACTORY_TO_GARDEN_PORTAL) is True
    assert seen["world"] == map_routes.FACTORY_TO_GARDEN_PORTAL_WORLD and seen["biome"] == "garden"
    assert 170 < seen["budget"] <= 180


def _anthell_switch_worker(monkeypatch, portal_result, map_name="anthell"):
    _stub_run_worker_env(monkeypatch)
    cfg = main._apply_worker_config({})
    monkeypatch.setattr(main, "_apply_worker_config", lambda c: dict(
        cfg, map_name=map_name, biome="garden", auto_switch_server=True, short_round_limit=1))
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main, "click_start_game", lambda: True)
    monkeypatch.setattr(main, "_reassert_florr_toggles", lambda *a, **k: {})
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda *a, **k: False)   # 短局
    log = []
    monkeypatch.setattr(main, "switch_server", lambda b: log.append(("direct", b)) or "srv")

    def via_portal(biome, portal, **kw):
        log.append(("portal", biome, portal))
        if len([e for e in log if e[0] == "portal"]) >= 1 and len(log) >= 3:
            raise KeyboardInterrupt
        return portal_result
    monkeypatch.setattr(main, "_switch_server_via_portal", via_portal)
    return log


def test_anthell_switch_waits_for_the_portal_instead_of_switching_on_the_spot(monkeypatch):
    log = _anthell_switch_worker(monkeypatch, portal_result=True)
    rounds = {"n": 0}
    orig = main._run_entry_route

    def entry(*a, **k):
        rounds["n"] += 1
        if rounds["n"] > 3:
            raise KeyboardInterrupt
        return orig(*a, **k)
    monkeypatch.setattr(main, "_run_entry_route", entry)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert log[0] == ("portal", "garden", map_routes.ANTHELL_TO_GARDEN_PORTAL)
    assert ("direct", "garden") not in log


def test_anthell_switch_falls_back_to_a_direct_switch(monkeypatch):
    log = _anthell_switch_worker(monkeypatch, portal_result=False)
    rounds = {"n": 0}
    orig = main._run_entry_route

    def entry(*a, **k):
        rounds["n"] += 1
        if rounds["n"] > 2:
            raise KeyboardInterrupt
        return orig(*a, **k)
    monkeypatch.setattr(main, "_run_entry_route", entry)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert log[:2] == [("portal", "garden", map_routes.ANTHELL_TO_GARDEN_PORTAL),
                       ("direct", "garden")]


def test_factory_switches_servers_through_the_portal_too(monkeypatch):
    log = _anthell_switch_worker(monkeypatch, portal_result=True, map_name="factory")
    rounds = {"n": 0}

    def entry(*a, **k):
        rounds["n"] += 1
        if rounds["n"] > 2:
            raise KeyboardInterrupt
        return "arrived"
    monkeypatch.setattr(main, "_run_entry_route", entry)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert log[0] == ("portal", "garden", map_routes.FACTORY_TO_GARDEN_PORTAL)


def test_factory_falls_back_to_a_direct_switch_when_the_portal_walk_fails(monkeypatch):
    log = _anthell_switch_worker(monkeypatch, portal_result=False, map_name="factory")
    rounds = {"n": 0}

    def entry(*a, **k):
        rounds["n"] += 1
        if rounds["n"] > 2:
            raise KeyboardInterrupt
        return "arrived"
    monkeypatch.setattr(main, "_run_entry_route", entry)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert log[:2] == [("portal", "garden", map_routes.FACTORY_TO_GARDEN_PORTAL), ("direct", "garden")]


def test_anthell_portal_switch_pending_but_stuck_outside_switches_directly(monkeypatch):
    log = _anthell_switch_worker(monkeypatch, portal_result=True)
    results = iter(["arrived", "timeout"])

    def entry(*a, **k):
        try:
            return next(results)
        except StopIteration:
            raise KeyboardInterrupt
    monkeypatch.setattr(main, "_run_entry_route", entry)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    # 第 1 轮进了蚁穴、没到刷怪区 -> 记下"走门换服"; 第 2 轮人卡在外面 -> 当场直接换
    assert log == [("direct", "garden")]


def _teleported_rounds(monkeypatch, entries_see_garden):
    log = _anthell_switch_worker(monkeypatch, portal_result=True)

    def pathing(*a, **k):
        main._WRONG_MAP_SEEN = "garden"          # 去刷怪区的路上发现人在花园
        return False
    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)
    rounds = {"n": 0}

    def entry(route, stage_state, **k):
        rounds["n"] += 1
        if rounds["n"] > 3:
            raise KeyboardInterrupt
        if entries_see_garden:
            main._WRONG_MAP_SEEN = "anthell"     # 进场本身在花园走着被洞口传进蚁穴 —— 正常进场
        return "arrived"
    monkeypatch.setattr(main, "_run_entry_route", entry)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    return log


def test_a_round_ended_by_a_teleport_is_not_a_short_round(monkeypatch):
    # 第六份录像: 蚁穴复活后站进回花园的门被传走。不是死了也不是洞口进不去, 不该攒换服次数
    assert _teleported_rounds(monkeypatch, entries_see_garden=False) == []


def test_the_entry_route_walking_through_the_hole_does_not_count_as_a_teleport(monkeypatch):
    # 只有进场之后再被传走才算: 进场路线自己被洞口传进蚁穴时寻路也会说"不在这张图"
    orig = main.lazy_theta_pathing
    log = _anthell_switch_worker(monkeypatch, portal_result=True)
    rounds = {"n": 0}

    def entry(route, stage_state, **k):
        rounds["n"] += 1
        if rounds["n"] > 3:
            raise KeyboardInterrupt
        main._WRONG_MAP_SEEN = "anthell"
        return "arrived"
    monkeypatch.setattr(main, "_run_entry_route", entry)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert log and log[0][0] == "portal"          # 短局照常攒, 攒够了照常走门换服


def test_portal_switch_turns_off_invert_defense_and_restores_it_when_it_fails(monkeypatch):
    # 第三份录像: 开着反转防御, 泡泡把人推来推去, 两次都在门边晃、没踩上(最近 ~130 世界单位)。
    # 花园进场贴洞口时就会先关掉它, 这里同理。
    seen = _portal_env(monkeypatch, dead=True)             # 没走进去
    flags = []
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ev, addr, want: flags.append((addr, want)) or ("changed", ""))
    assert main._switch_server_via_portal("garden", (121, 102), want_defense=True) is False
    addr = main.florr_settings.INVERT_DEFENSE_ADDR
    assert flags == [(addr, 0), (addr, 1)]


def test_portal_switch_leaves_defense_off_once_it_switched(monkeypatch):
    # 换成了就不用恢复: 新服进局后每轮开头 _reassert_florr_toggles 会按时块配置写回
    _portal_env(monkeypatch, worlds=[None, None])
    flags = []
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ev, addr, want: flags.append(want) or ("changed", ""))
    assert main._switch_server_via_portal("garden", (121, 102), want_defense=True) is True
    assert flags == [0]


def test_portal_switch_does_not_touch_defense_when_it_is_not_inverted(monkeypatch):
    _portal_env(monkeypatch, worlds=[None, None])
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda *a: pytest.fail("没开反转防御就别碰它"))
    assert main._switch_server_via_portal("garden", (121, 102), want_defense=False) is True


def test_portal_switch_staging_box_is_not_pixel_tight(monkeypatch):
    # 第三份录像: ±1 格的框让它在集结点附近来回重规划了 16 秒
    _portal_env(monkeypatch, worlds=[None, None], here=(60, 95))
    boxes = []
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda target, area, **kw: boxes.append(area) or True)
    main._switch_server_via_portal("garden", (121, 102))
    (x0, y0), (x1, y1) = boxes[0][0]
    assert x1 - x0 >= 4 and y1 - y0 >= 4


# ── 躲开了就多躲一会儿 (第三份录像: 究极一退出 200px 就不躲了, 漫游/寻路又走回它身边,
#    躲 -> 走回去 -> 躲, 来回四次把血磨光) ────────────────────────────────────

def _ultra_det(x, y=540):
    return {"species": "soldier_ant", "rarity": "Ultra", "screen_pos": (x, y)}


def test_flee_latch_keeps_fleeing_until_the_ultra_is_far_enough():
    latch = main._FleeLatch()
    c = (960.0, 540.0)
    assert latch.apply(("flee", [(1100, 540)]), [_ultra_det(1100)], c, 10.0)[0] == "flee"
    # 退到 300px: 原决策已经不躲了, 滞回接着躲
    got = latch.apply(("wander", None), [_ultra_det(1260)], c, 10.5)
    assert got == ("flee", [(1260, 540)])
    # 退到 FLEE_RELEASE_PX 外: 放开
    far = 960 + main.FLEE_RELEASE_PX + 20
    assert latch.apply(("chase", "x", 1, []), [_ultra_det(far)], c, 11.0) == ("chase", "x", 1, [])


def test_flee_latch_gives_up_after_a_while():
    latch = main._FleeLatch()
    c = (960.0, 540.0)
    latch.apply(("flee", [(1100, 540)]), [_ultra_det(1100)], c, 10.0)
    later = 10.0 + main.FLEE_LATCH_MAX_S + 0.1
    assert latch.apply(("wander", None), [_ultra_det(1260)], c, later) == ("wander", None)


def test_flee_latch_without_a_recent_flee_changes_nothing():
    latch = main._FleeLatch()
    assert latch.apply(("wander", None), [_ultra_det(1100)], (960.0, 540.0), 10.0) == ("wander", None)


def test_flee_latch_ignores_non_avoid_mobs():
    latch = main._FleeLatch()
    c = (960.0, 540.0)
    latch.apply(("flee", [(1100, 540)]), [_ultra_det(1100)], c, 10.0)
    mythic = {"species": "soldier_ant", "rarity": "Mythic", "screen_pos": (1100, 540)}
    assert latch.apply(("chase", mythic, 120, []), [mythic], c, 10.5)[0] == "chase"


def test_maybe_scan_enemies_applies_the_flee_latch(monkeypatch):
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: [_ultra_det(1260)])
    monkeypatch.setattr(main.enemy_detect, "select_action", lambda d, **k: ("wander", None))
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960.0, 540.0))
    latch = main._FleeLatch()
    latch.last_flee = 99.9
    monkeypatch.setattr(main, "_FLEE_LATCH", latch)
    monkeypatch.setattr(main.time, "time", lambda: 100.0)
    decision = main._maybe_scan_enemies(True, 100.0, 0.0, ("wander", None), [])[0]
    assert decision == ("flee", [(1260, 540)])


def _band(y0=85, y1=96):
    m = np.zeros((300, 300), dtype=np.uint8)
    m[y0:y1 + 1, 5:200] = 255
    return m


def test_line_walkable_sees_a_clear_corridor():
    assert main._line_walkable(_band(), (20, 90), (60, 92)) is True


def test_line_walkable_is_blocked_by_a_wall():
    m = _band()
    m[85:97, 40] = 0                                    # 一堵竖墙
    assert main._line_walkable(m, (20, 90), (60, 92)) is False


def test_line_of_sight_reach_checks_area_and_walls(monkeypatch):
    monkeypatch.setattr(main.utils, "MAP", "anthell")
    s = main.utils.MINIMAP_WORLD_SCALE["anthell"]

    def at(mx, my):
        return {"world": (mx / s - 1000, my / s - 1000)}

    reach = main._line_of_sight_reach(_band(), [(7, 85), (61, 96)], (20, 90))
    assert reach(at(50, 92)) is True                    # 区里、直线可走
    assert reach(at(80, 92)) is False                   # 出了刷怪区
    m = _band()
    m[85:97, 40] = 0
    walled = main._line_of_sight_reach(m, [(7, 85), (61, 96)], (20, 90))
    assert walled(at(50, 92)) is False                  # 隔着墙
    assert reach({}) is False                           # 没有世界坐标


def test_line_of_sight_reach_needs_a_measured_map(monkeypatch):
    monkeypatch.setattr(main.utils, "MAP", "sewers")     # 没实测过缩放的图(海洋/丛林现在有了)
    assert main._line_of_sight_reach(_band(), [(7, 85), (61, 96)], (20, 90)) is None
    monkeypatch.setattr(main.utils, "MAP", "anthell")
    assert main._line_of_sight_reach(_band(), [(7, 85), (61, 96)], None) is None


def test_maybe_scan_enemies_passes_can_reach_through(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: [])
    monkeypatch.setattr(main.enemy_detect, "select_action",
                        lambda d, **k: seen.update(k) or ("wander", None))
    probe = lambda det: True
    main._maybe_scan_enemies(True, main.ENEMY_SCAN_INTERVAL + 1.0, 0.0, ("wander", None), [],
                             can_reach=probe)
    assert seen["can_reach"] is probe


def test_auto_farming_scans_with_a_reach_check(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main.utils, "MAP", "anthell")
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))
    seen = {}

    class Done(Exception):
        pass

    def scan(*a, **k):
        seen["can_reach"] = k.get("can_reach")
        raise Done
    monkeypatch.setattr(main, "_maybe_scan_enemies", scan)
    with pytest.raises(Done):
        main.auto_farming(AREA, 30, enemy_ai_enabled=True)
    assert callable(seen["can_reach"])


# ── 位置在两点之间来回跳, 也要判卡住 (第四份录像: 人卡在墙角 3 分钟没动, 读到的位置被
#    吸附到两个不同的可走点上来回跳, 离目标的距离一会儿大一会儿小, 老判据每次"缩短"都
#    把停滞计数清零, 这一段路一直走不完) ──────────────────────────────────────────

def test_move_to_position_calls_a_jittering_standstill_stuck(monkeypatch):
    _stub_move_env(monkeypatch)
    seq = iter([(125, 103), (129, 101)] * 200)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: next(seq))
    result = main.move_to_position((127, 103), (123, 115), max_attempts=400, stall_limit=13)
    assert result == "stuck"


def test_move_to_position_real_progress_is_not_stuck(monkeypatch):
    _stub_move_env(monkeypatch)
    ys = iter(range(100, 0, -1))
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, next(ys)))
    assert main.move_to_position((10, 100), (10, 20), max_attempts=400, stall_limit=13) is True


def test_execute_path_honours_the_deadline_inside_a_leg():
    orig = main.move_to_position
    clock = {"t": 100.0}
    ticks = []

    def move(a, b, on_tick=None, **kw):
        for _ in range(1000):                      # 一段路走很久
            clock["t"] += 1.0
            ticks.append(1)
            sig = on_tick(b) if on_tick else None
            if sig:
                return sig
        return True

    main.move_to_position = move
    orig_time = main.time.time
    main.time.time = lambda: clock["t"]
    try:
        got = main.execute_path([(0, 0), (1, 1)], deadline=110.0)
    finally:
        main.move_to_position = orig
        main.time.time = orig_time
    assert got == "timeout" and len(ticks) <= 11


def test_lazy_theta_pathing_passes_its_deadline_down(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "lazy_theta_star", lambda m, a, b: [a, b])
    monkeypatch.setattr(main, "get_player_position", lambda: (30, 30))
    seen = {}
    monkeypatch.setattr(main, "execute_path",
                        lambda path, **kw: seen.update(kw) or "timeout")
    deadline = main.time.time() + 1000
    calls = {"n": 0}
    orig_time = main.time.time

    def fake_time():
        calls["n"] += 1
        return orig_time() + (0 if calls["n"] < 5 else 2000)
    monkeypatch.setattr(main.time, "time", fake_time)
    assert main.lazy_theta_pathing((40, 40), [[(35, 35), (45, 45)]], deadline=deadline) is False
    assert seen["deadline"] == deadline


def test_in_farm_area_predicate(monkeypatch):
    monkeypatch.setattr(main.utils, "MAP", "anthell")
    s = main.utils.MINIMAP_WORLD_SCALE["anthell"]
    inside = main._in_farm_area([(7, 85), (61, 96)])
    assert inside({"world": (40 / s - 1000, 90 / s - 1000)}) is True
    assert inside({"world": (40 / s - 1000, 84 / s - 1000)}) is False
    assert inside({}) is True                      # 不知道在哪的不拦
    monkeypatch.setattr(main.utils, "MAP", "sewers")     # 没实测过缩放的图(海洋/丛林现在有了)
    assert main._in_farm_area([(7, 85), (61, 96)]) is None


def test_auto_farming_passes_the_area_filter(monkeypatch):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main.utils, "MAP", "anthell")
    monkeypatch.setattr(main, "get_player_position", lambda: (20, 40))
    seen = {}

    class Done(Exception):
        pass

    def scan(*a, **k):
        seen["in_area"] = k.get("in_area")
        raise Done
    monkeypatch.setattr(main, "_maybe_scan_enemies", scan)
    with pytest.raises(Done):
        main.auto_farming(AREA, 30, enemy_ai_enabled=True)
    assert callable(seen["in_area"])



def test_move_to_position_progress_then_standstill_is_stuck(monkeypatch):
    # 先真走了一段(80 -> 40), 再顶住不动: 参照距离得跟着进展往下挪, 不然永远"比开头近"
    _stub_move_env(monkeypatch)
    ys = list(range(100, 60, -2)) + [60] * 100
    it = iter(ys)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: (10, next(it)))
    assert main.move_to_position((10, 100), (10, 20), max_attempts=200, stall_limit=13) == "stuck"


# ── 统计的阶段标记: worker 在哪些调用点套了 phase_marks.phase(...) ─────────────

def _phase_round_env(monkeypatch):
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main, "click_start_game", lambda: True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "apply_map", lambda n: None)
    monkeypatch.setattr(main, "load_binary_map", lambda: None)


def test_run_worker_marks_entry_and_pathing_as_travel_and_farming_as_farm(monkeypatch):
    import phase_marks
    phase_marks._reset_for_tests()
    _phase_round_env(monkeypatch)
    seen = []

    def entry(*a, **k):
        seen.append(("entry", phase_marks.current()))
        return "arrived"

    def path(*a, **k):
        seen.append(("path", phase_marks.current()))
        return True

    def farm(*a, **k):
        seen.append(("farm", phase_marks.current()))
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "_run_entry_route", entry)
    monkeypatch.setattr(main, "lazy_theta_pathing", path)
    monkeypatch.setattr(main, "auto_farming", farm)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == [("entry", "travel"), ("path", "travel"), ("farm", "farm")]
    assert phase_marks.current() == "other"            # 异常冒出来也恢复了


def test_run_worker_marks_the_short_round_server_switch_as_travel(monkeypatch):
    import phase_marks
    phase_marks._reset_for_tests()
    _stub_run_worker_env(monkeypatch)
    monkeypatch.setattr(main, "_apply_worker_config", lambda cfg: {
        "location": (1, 2), "farming_area": [(0, 0), (9, 9)], "farming_duration": 9999,
        "short_round_limit": 1, "enemy_ai_enabled": False, "auto_switch_server": True,
        "biome": "ocean", "map_name": "ocean", "route": map_routes.route_for("ocean"),
        "enter_game_swap": "none", "reach_area_swap": "none",
        "invert_attack": True, "invert_defense": False,
        "combat": "rules", "rl_model": "runs/desert", "rl_train": False,
    })
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    monkeypatch.setattr(main, "click_start_game", lambda: True)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda *a, **k: {"attack": "unchanged", "defense": "unchanged"})
    monkeypatch.setattr(main, "lazy_theta_pathing", lambda *a, **k: False)   # 没到区 -> 短局
    seen = []

    def rec(*a, **k):
        seen.append(phase_marks.current())
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "switch_server", rec)
    with pytest.raises(KeyboardInterrupt):
        main.run_worker({})
    assert seen == ["travel"]


def test_every_server_switch_in_run_worker_is_marked_travel():
    """四处换服(传送门换服 + 三处直接换)都得在 with phase_marks.phase("travel") 里 ——
    传送门那条要蚁穴路线才走得到, 行为测试够不着, 所以直接查源码."""
    import re
    lines = inspect.getsource(main.run_worker).splitlines()
    hits = [i for i, ln in enumerate(lines)
            if re.search(r'\bswitch_server\(w\["biome"\]\)|_switch_server_via_portal\(', ln)
            and not ln.lstrip().startswith("#")]
    assert len(hits) == 4
    for i in hits:
        assert lines[i - 1].strip() == 'with phase_marks.phase("travel"):', lines[i - 1]


# ── 第②轮: 用户自配索敌规则 ────────────────────────────────────────────────────

_NO_RULES = {"species": {}, "knobs": {}}


def test_avoid_trigger_px_is_the_shared_default():
    assert main.AVOID_TRIGGER_PX == main.enemy_detect.DEFAULT_AVOID_TRIGGER_PX == 400


def test_apply_worker_config_installs_enemy_rules(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    rules = {"species": {"bubble": "ignore", "shell": "avoid"},
             "knobs": {"chase_max_px": 420, "swarm": False}}
    w = main._apply_worker_config(
        {"version": 2, "active": {"map": "ocean", "enemy_rules": rules}})
    assert w["enemy_rules"] == rules
    assert main.enemy_detect.get_rules() == rules


def test_apply_worker_config_without_rules_clears_the_previous_ones(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    main.enemy_detect.set_rules({"species": {"bubble": "ignore"}})
    w = main._apply_worker_config({"version": 2, "active": {"map": "ocean"}})
    assert w["enemy_rules"] == _NO_RULES
    assert main.enemy_detect.get_rules() == _NO_RULES


def test_apply_worker_config_drops_rules_for_species_not_on_the_map(monkeypatch, capsys):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    w = main._apply_worker_config({"version": 2, "active": {"map": "ocean", "enemy_rules": {
        "species": {"scorpion": "ignore", "bubble": "ignore"}}}})
    assert w["enemy_rules"]["species"] == {"bubble": "ignore"}
    assert main.enemy_detect.rule_for("scorpion") is None
    assert "scorpion" in capsys.readouterr().out


def test_apply_worker_config_reads_rules_from_a_flat_config_too(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    w = main._apply_worker_config({"map": "ocean", "enemy_rules": {"knobs": {"avoid_px": 250}}})
    assert w["enemy_rules"] == {"species": {}, "knobs": {"avoid_px": 250}}


def _scan_dets():
    return [{"species": "rock", "rarity": "Ultra", "screen_pos": (1000, 540), "confidence": 0.9},
            {"species": "bee", "rarity": "Common", "screen_pos": (1100, 540), "confidence": 0.9}]


def test_maybe_scan_enemies_drops_ignored_species_everywhere_downstream(monkeypatch):
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: _scan_dets())
    seen = {}
    monkeypatch.setattr(main.enemy_detect, "select_action",
                        lambda ds, **k: seen.update(ds=ds) or ("wander", None))
    main.enemy_detect.set_rules({"species": {"rock": "ignore"}})
    now = main.ENEMY_SCAN_INTERVAL + 1.0
    _decision, dets, _last, scanned = main._maybe_scan_enemies(
        True, now, 0.0, ("wander", None), [])
    assert scanned is True
    assert [d["species"] for d in dets] == ["bee"]            # 回传给 Mythic 锁定 / flee 规划的那份
    assert [d["species"] for d in seen["ds"]] == ["bee"]      # select_action 看到的那份


def test_maybe_scan_enemies_without_rules_keeps_every_detection(monkeypatch):
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: _scan_dets())
    monkeypatch.setattr(main.enemy_detect, "select_action", lambda ds, **k: ("wander", None))
    now = main.ENEMY_SCAN_INTERVAL + 1.0
    _d, dets, _l, _s = main._maybe_scan_enemies(True, now, 0.0, ("wander", None), [])
    assert [d["species"] for d in dets] == ["rock", "bee"]


def test_maybe_scan_enemies_passes_builtin_values_when_no_knobs_are_set(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: [])
    monkeypatch.setattr(main.enemy_detect, "select_action",
                        lambda ds, **k: seen.update(k) or ("wander", None))
    now = main.ENEMY_SCAN_INTERVAL + 1.0
    for map_name, want_avoid in (("anthell", 200), ("desert", main.AVOID_TRIGGER_PX)):
        monkeypatch.setattr(main.utils, "MAP", map_name)
        main._maybe_scan_enemies(True, now, 0.0, ("wander", None), [])
        assert seen["avoid_trigger_px"] == want_avoid
        assert seen["engage_hold_px"] == main.enemy_detect.ENGAGE_HOLD_PX
        assert seen["chase_max_px"] == main.enemy_detect.CHASE_MAX_PX
        assert seen["far_chase_px"] == main.enemy_detect.FAR_CHASE_PX
        assert seen["cautious_hold_px"] == main.CAUTIOUS_HOLD_PX == main.enemy_detect.CAUTIOUS_HOLD_PX
        assert seen["swarm_enabled"] is True


def test_maybe_scan_enemies_passes_user_knobs_to_select_action(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: [])
    monkeypatch.setattr(main.enemy_detect, "select_action",
                        lambda ds, **k: seen.update(k) or ("wander", None))
    monkeypatch.setattr(main.utils, "MAP", "anthell")
    main.enemy_detect.set_rules({"knobs": {
        "hold_active_px": 111, "chase_max_px": 222, "far_chase_px": 333,
        "avoid_px": 444, "cautious_px": 555, "swarm": False}})
    now = main.ENEMY_SCAN_INTERVAL + 1.0
    main._maybe_scan_enemies(True, now, 0.0, ("wander", None), [])
    assert (seen["engage_hold_px"], seen["chase_max_px"], seen["far_chase_px"],
            seen["avoid_trigger_px"], seen["cautious_hold_px"]) == (111, 222, 333, 444, 555)
    assert seen["swarm_enabled"] is False


def test_maybe_scan_enemies_disabled_ignores_rules_entirely(monkeypatch):
    monkeypatch.setattr(main.enemy_detect, "scan_enemies",
                        lambda **k: (_ for _ in ()).throw(AssertionError("不该扫描")))
    main.enemy_detect.set_rules({"species": {"rock": "ignore"}})
    decision, dets, _l, scanned = main._maybe_scan_enemies(
        False, 1000.0, 0.0, ("chase", "x"), ["old"])
    assert decision == ("wander", None) and dets == [] and scanned is False


def test_an_ignored_ultra_species_does_not_make_the_bot_flee_but_an_unignored_one_does(monkeypatch):
    # 静止 Ultra 怪是永久逃跑源(第①轮的已知问题); 「忽略」就是它的出路 —— 真 select_action + 真 _FLEE_LATCH。
    rock = {"species": "rock", "rarity": "Ultra", "screen_pos": (1000, 540), "confidence": 0.9}
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: [dict(rock)])
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960, 540))
    monkeypatch.setattr(main.utils, "MAP", "garden")
    now = main.ENEMY_SCAN_INTERVAL + 1.0

    decision, _dets, _last, _scanned = main._maybe_scan_enemies(True, now, 0.0, ("wander", None), [])
    assert decision[0] == "flee"                                   # 没规则: 照旧逃

    # 故意不重置 _FLEE_LATCH: 上面那次 flee 已经让它挂上了(last_flee = now), 下面这次扫描
    # 在 FLEE_LATCH_MAX_S(6 秒)内。滞回此刻会拿「这次的检测」去找还没走远的 AVOID 怪来续躲 ——
    # 只有喂给它的也是剔除过「忽略」物种的那一份, 才会放开。要是只在 select_action 的入参上
    # 剔除、滞回拿的是原始检测, 这里就会被续成 flee。
    later = now + 1.0
    assert main._FLEE_LATCH.last_flee == now and later - now <= main.FLEE_LATCH_MAX_S
    main.enemy_detect.set_rules({"species": {"rock": "ignore"}})
    decision, dets, _last, _scanned = main._maybe_scan_enemies(True, later, 0.0, ("wander", None), [])
    assert decision == ("wander", None) and dets == []             # 忽略: 不逃, 检测里也没有它


# ── 第③轮: 躲避起点稀有度, 配置 → worker → 真 flee 决策 ─────────────────────────────

def _scan_one(monkeypatch, det, map_name="garden"):
    monkeypatch.setattr(main.enemy_detect, "scan_enemies", lambda **k: [dict(det)])
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960, 540))
    monkeypatch.setattr(main.utils, "MAP", map_name)
    monkeypatch.setattr(main._FLEE_LATCH, "last_flee", None)     # 每次扫描互相独立, 不吃上一次的滞回
    return main._maybe_scan_enemies(True, main.ENEMY_SCAN_INTERVAL + 1.0, 0.0, ("wander", None), [])[0]


def test_the_rarity_threshold_set_in_config_decides_whether_the_worker_flees(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    rock = {"species": "rock", "rarity": "Ultra", "screen_pos": (1000, 540), "confidence": 0.9}
    legendary = dict(rock, rarity="Legendary")

    main._apply_worker_config({"version": 2, "active": {"map": "garden"}})
    assert _scan_one(monkeypatch, rock)[0] == "flee"                  # 没设: Ultra 躲(第②轮及以前)
    assert _scan_one(monkeypatch, legendary)[0] != "flee"             # 没设: 传奇不躲

    cfg = lambda tier: {"version": 2, "active": {"map": "garden", "enemy_rules": {
        "knobs": {"avoid_min_rarity": tier}}}}
    main._apply_worker_config(cfg("Super"))                           # 调高: Ultra 当普通怪
    assert _scan_one(monkeypatch, rock)[0] != "flee"
    main._apply_worker_config(cfg("Legendary"))                       # 调低: 传奇也躲
    assert _scan_one(monkeypatch, legendary)[0] == "flee"
    main._apply_worker_config(cfg("never"))
    assert _scan_one(monkeypatch, dict(rock, rarity="Unique"))[0] != "flee"

    # 换回没有规则的配置: 上一份的起点不能漏到下一轮
    main._apply_worker_config({"version": 2, "active": {"map": "garden"}})
    assert _scan_one(monkeypatch, rock)[0] == "flee"


def test_per_rarity_rules_set_in_config_decide_each_rarity_separately_in_the_worker(monkeypatch):
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    rock = {"species": "rock", "rarity": "Ultra", "screen_pos": (1000, 540), "confidence": 0.9}
    cfg = lambda species: {"version": 2, "active": {"map": "garden", "enemy_rules": {"species": species}}}

    main._apply_worker_config({"version": 2, "active": {"map": "garden"}})
    assert _scan_one(monkeypatch, rock)[0] == "flee"                           # 没规则: 静止 Ultra 是逃跑源
    assert _scan_one(monkeypatch, dict(rock, rarity="Common"))[0] == "chase"   # Common 照打

    main._apply_worker_config(cfg({"rock": {"Ultra": "fight", "Common": "ignore"}}))
    assert _scan_one(monkeypatch, rock)[0] == "chase"                          # 只有 Ultra 改成打
    assert _scan_one(monkeypatch, dict(rock, rarity="Common"))[0] == "wander"  # Common 被忽略: 当它不存在
    assert _scan_one(monkeypatch, dict(rock, rarity="Super"))[0] == "flee"     # 没配的稀有度: 内置兜底

    main._apply_worker_config(cfg({"ladybug": {"Common": "avoid"}}))
    ladybug = {"species": "ladybug", "rarity": "Common", "screen_pos": (1000, 540), "confidence": 0.9}
    assert _scan_one(monkeypatch, ladybug)[0] == "flee"
    assert _scan_one(monkeypatch, dict(ladybug, rarity="Epic"))[0] == "chase"  # 同一物种别的稀有度照打

    main._apply_worker_config(cfg({"bee": {"Common": "cautious"}}))
    bee = {"species": "bee", "rarity": "Common", "screen_pos": (1240, 540), "confidence": 0.9}
    got = _scan_one(monkeypatch, bee)
    assert got[0] == "chase" and got[2] == main.CAUTIOUS_HOLD_PX               # 谨慎: 在谨慎距离外停下
    plain = _scan_one(monkeypatch, dict(bee, rarity="Epic"))
    assert plain[0] == "chase" and plain[2] != main.CAUTIOUS_HOLD_PX

    main._apply_worker_config({"version": 2, "active": {"map": "garden"}})     # 换回没规则的配置: 不留尾巴
    assert _scan_one(monkeypatch, rock)[0] == "flee"


def test_tier_tooltip_default_matches_the_engine_default():
    import gui_schedule
    import enemy_species
    tip = gui_schedule.EnemyRulesDialog._TIP_TIER
    assert f"默认 {enemy_species.AVOID_MIN_RARITY_DEFAULT}" in tip
    assert main.enemy_detect.AVOID_MIN_RARITY_DEFAULT == enemy_species.AVOID_MIN_RARITY_DEFAULT


# ── 面向用户的文案别跟引擎数值漂移(gui_schedule 不导入 enemy_detect, 数字是手写进文案的) ──────

def test_avoid_px_tooltip_numbers_match_the_engine():
    import gui_schedule
    tip = gui_schedule.EnemyRulesDialog._TIPS["avoid_px"]
    ed = main.enemy_detect
    default = ed.DEFAULT_AVOID_TRIGGER_PX
    anthell = ed.AVOID_TRIGGER_PX_BY_MAP["anthell"]
    early = ed.AVOID_EARLY_PX_BY_MAP["anthell"]
    release = main.FLEE_RELEASE_PX
    # 带上下文的子串, 不是裸数字: 400 在文案里出现两次(其余图的默认值 / 蚁穴提前躲), 裸 `"400" in tip`
    # 在默认值改了、提前躲半径没改时仍会过。
    assert f"蚁穴内置 {anthell}" in tip
    assert f"其余图 {default}" in tip
    assert f"{release}px 滞回" in tip
    assert f"{early}px 的提前躲" in tip
    for n in (default, anthell, early, release):
        assert str(n) in tip


def test_species_hint_copy_keeps_its_caveats_and_never_promises_always_chase():
    import gui_schedule
    hint = gui_schedule.EnemyRulesDialog._HINT_SPECIES
    # spec §5: 「打」只改分类, 沙漠按稀有度追击, 不能写成「打 = 一直追」
    assert "一直追" not in hint
    assert "沙漠" in hint and "不会让低稀有度怪被专门追" in hint
    # spec §2.1 / §5: 把平时要躲的 Ultra+ 设成「打」后果自负; 「忽略」的怪贴脸也不躲
    assert "Ultra" in hint and "后果自负" in hint
    assert "忽略" in hint and "贴脸" in hint


# ── 被吞掉的异常要留下 traceback(bug 自动上报靠日志里的 traceback 认问题), 但不能每拍刷屏 ──────

def test_print_exc_once_prints_a_traceback_the_first_time_per_tag_and_exception_type(monkeypatch, capsys):
    monkeypatch.setattr(main, "_TB_PRINTED", set())

    def blow(exc):
        try:
            raise exc
        except Exception:
            main._print_exc_once("scan")

    for _ in range(3):
        blow(ValueError("a"))
    assert capsys.readouterr().err.count("Traceback (most recent call last)") == 1
    blow(KeyError("b"))                              # 换了异常类型: 再打一次
    assert capsys.readouterr().err.count("Traceback (most recent call last)") == 1
    try:
        raise ValueError("c")
    except ValueError:
        main._print_exc_once("plan")                 # 换了位置标签: 再打一次
    assert capsys.readouterr().err.count("Traceback (most recent call last)") == 1


def test_a_swallowed_scan_error_leaves_one_traceback_in_the_worker_output(monkeypatch, capsys):
    monkeypatch.setattr(main, "_TB_PRINTED", set())
    monkeypatch.setattr(main.enemy_detect, "scan_enemies",
                        lambda **k: (_ for _ in ()).throw(ValueError("扫描炸了")))
    now = main.ENEMY_SCAN_INTERVAL + 1.0
    for i in range(3):
        decision, dets, _last, scanned = main._maybe_scan_enemies(True, now + i * 10, 0.0, ("wander", None), [])
        assert decision == ("wander", None) and dets == [] and scanned is True     # 行为照旧: 本轮当漫游
    out = capsys.readouterr()
    assert out.out.count("索敌出错") == 3                                            # 每拍一行提示照旧
    assert out.err.count("Traceback (most recent call last)") == 1 and "ValueError: 扫描炸了" in out.err


def test_a_swallowed_flee_planner_error_leaves_one_traceback_too(monkeypatch, capsys):
    monkeypatch.setattr(main, "_TB_PRINTED", set())
    bm = _flee_plan_env(monkeypatch)
    monkeypatch.setattr(main.flee_planner, "plan_flee",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("坏了")))
    ultra = _det_at("soldier_ant", "Ultra", (800.0, 540.0))
    for _ in range(2):
        main._FLEE_PLAN.goal = main._FLEE_PLAN.dir = None      # 每次都重新规划, 不吃上一拍的缓存
        main._FLEE_PLAN.target([ultra["screen_pos"]], [ultra], (960.0, 540.0), (150, 150), bm)
    out = capsys.readouterr()
    assert out.out.count("躲避规划出错") == 2
    assert out.err.count("Traceback (most recent call last)") == 1 and "ValueError: 坏了" in out.err



def test_cautious_px_tooltip_numbers_match_the_engine():
    import gui_schedule
    tip = gui_schedule.EnemyRulesDialog._TIPS["cautious_px"]
    ed = main.enemy_detect
    assert f"默认 {ed.CAUTIOUS_HOLD_PX}" in tip
    assert f"{round((1 - ed.CAUTIOUS_RETREAT_BAND) * 100)}%" in tip


# ── 谨慎怪太近 → 后退: 后退也别往墙里退(跟躲究极同一个坑: 贴墙站着不动被打死) ───────────────────

def _drive_one_chase_tick(monkeypatch, target, hold_px, *, center=(960.0, 540.0), pos=(20, 40)):
    """跑一拍追击分支, 返回 (交给 _drive_and_check_stall 的鼠标位置, 它的状态/文案, 避墙函数收到的参数)。"""
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: pos)
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("chase", target, hold_px, []), [], 0.0, True))
    monkeypatch.setattr(main, "MYTHIC_LATCH_ENABLED", False)
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: center)
    seen = {"steer": []}

    class Done(Exception):
        pass

    def steer(mouse_target, c, p, binary_map):
        seen["steer"].append((mouse_target, c, p, binary_map is not None))
        return (11.0, 22.0)

    def drive(mouse_target, current_pos, hist, state, message, **kw):
        seen["drive"] = (mouse_target, state, message)
        raise Done

    monkeypatch.setattr(main, "_steer_clear_of_walls", steer)
    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    with pytest.raises(Done):
        main.auto_farming(AREA, 30, enemy_ai_enabled=True)
    return seen


def test_auto_farming_cautious_retreat_steers_clear_of_walls_and_says_so(monkeypatch):
    main.enemy_detect.set_rules({"species": {"spider": "cautious"}})
    target = {"species": "spider", "rarity": "Mythic", "screen_pos": (1060, 540)}     # 100px: 比 300×0.85 近
    seen = _drive_one_chase_tick(monkeypatch, target, 300)
    assert len(seen["steer"]) == 1
    raw, center, pos, has_map = seen["steer"][0]
    assert raw[0] < center[0] and pos == (20, 40) and has_map       # 避墙吃的是引擎算出的后退方向
    mouse_target, state, message = seen["drive"]
    assert mouse_target == (11.0, 22.0)                              # 交给卡住检测 / moveTo 的是避墙后的
    assert state == "索敌中" and "后退" in message and "spider" in message


def test_auto_farming_cautious_target_inside_the_stand_band_is_not_steered(monkeypatch):
    main.enemy_detect.set_rules({"species": {"spider": "cautious"}})
    target = {"species": "spider", "rarity": "Mythic", "screen_pos": (1230, 540)}     # 270px: 站桩带
    seen = _drive_one_chase_tick(monkeypatch, target, 300)
    assert seen["steer"] == []
    assert seen["drive"][0] == (960.0, 540.0) and "后退" not in seen["drive"][2]


def test_auto_farming_a_fight_target_close_by_is_still_not_steered(monkeypatch):
    target = {"species": "soldier_ant", "rarity": "Mythic", "screen_pos": (970, 540)}
    seen = _drive_one_chase_tick(monkeypatch, target, 120)
    assert seen["steer"] == [] and seen["drive"][0] == (960.0, 540.0)
    assert "后退" not in seen["drive"][2] and "追击" in seen["drive"][2]


# ── 迷宫图(工厂)寻怪: 直线隔墙看得见的怪沿可走路径绕过去; 漫游目标选路径够得着的并走到为止 ────────
# 2026-10-09 三份工厂录像: 刷怪区里 98% 的拍子屏幕上都有怪, 却有一半时间在随机漫游 —— 其中 2/3 是最近
# 的可打怪在 300~600px、小地图上直线隔着墙(can_reach 说走不到, select_action 就不追)。漫游目标又是整个
# 161x71 区域里的随机可走点(三间房, 里面还有 U 形墙), 直线走过去顶墙, 一局 15 次"移动受阻/地图脱困"。

import numpy as _np


def _wall_map():
    """60x60 全可走, 中间一道竖墙 x=30(y=10..49), 上下各留一个口: 绕路得走到 y<10 或 y>49。"""
    m = _np.full((60, 60), 255, dtype=_np.uint8)
    m[10:50, 30] = 0
    return m


def _world_of(cell, scale):
    return (cell[0] / scale - 1000.0, cell[1] / scale - 1000.0)


def _det(species, rarity, cell, scale, center=(960, 540), px=(300.0, 0.0), conf=1.0):
    return {"species": species, "rarity": rarity, "confidence": conf, "world": _world_of(cell, scale),
            "screen_pos": (center[0] + px[0], center[1] + px[1]), "bbox": (0, 0, 0, 0)}


@pytest.fixture
def factory_scale(monkeypatch):
    monkeypatch.setattr(main.utils, "MAP", "factory")
    return main.utils.MINIMAP_WORLD_SCALE["factory"]


def test_bfs_reach_goes_around_a_wall_and_respects_the_limit():
    m = _wall_map()
    dist, parent = main._bfs_reach(m, (25, 30), max_cells=60)
    assert dist[(25, 30)] == 0
    assert dist[(35, 30)] > 10 + 5      # 直线 10 格, 隔墙绕要走到口子再回来
    dist_small, _ = main._bfs_reach(m, (25, 30), max_cells=8)
    assert (35, 30) not in dist_small and max(dist_small.values()) <= 8


AREA_WALL = [(5, 5), (55, 55)]


def test_path_approach_walks_around_the_wall_not_into_it(factory_scale):
    m = _wall_map()
    here = (28, 45)                                 # 离下面的口子(y=50)近, 目标在墙另一侧
    det = _det("spider", "Legendary", (33, 45), factory_scale)
    plan = main._plan_path_approach([det], here, m, AREA_WALL, max_cells=40, max_px=2000)
    assert plan is not None and plan["target"] is det
    wx, wy = plan["waypoint"]
    assert main.line_of_sight(m, main._Pt(*here), main._Pt(wx, wy))   # 朝它推不会顶墙
    assert wy > here[1]                              # 先往下绕, 不是直直朝 +x 顶墙
    assert plan["cells"] >= 8                        # 直线才 5 格, 绕口子要 10 格


def test_path_approach_gives_up_when_the_detour_is_too_long(factory_scale):
    m = _wall_map()
    det = _det("spider", "Legendary", (33, 30), factory_scale)   # 离口子 20 格, 绕一圈 ~40
    assert main._plan_path_approach([det], (28, 30), m, AREA_WALL, max_cells=20, max_px=2000) is None


def test_path_approach_ignores_what_it_should_not_chase(factory_scale):
    m = _wall_map()
    here = (28, 45)
    kw = dict(max_cells=40, max_px=2000)
    cell = (33, 45)
    assert main._plan_path_approach([_det("spider", "Ultra", cell, factory_scale)], here, m, AREA_WALL, **kw) is None
    assert main._plan_path_approach([_det("spider", "Legendary", cell, factory_scale, conf=0.3)],
                                    here, m, AREA_WALL, **kw) is None
    assert main._plan_path_approach([_det("spider", "Legendary", (33, 58), factory_scale)],
                                    here, m, AREA_WALL, **kw) is None          # 区外
    assert main._plan_path_approach([_det("spider", "Legendary", cell, factory_scale, px=(3000.0, 0.0))],
                                    here, m, AREA_WALL, **kw) is None          # 屏幕上太远
    assert main._plan_path_approach([], here, m, AREA_WALL, **kw) is None
    assert main._plan_path_approach([_det("spider", "Legendary", cell, factory_scale)], None, m, AREA_WALL, **kw) is None


def test_path_approach_prefers_the_cheaper_species_at_similar_distance(factory_scale):
    m = _wall_map()
    here = (28, 45)
    barrel = _det("barrel", "Legendary", (33, 44), factory_scale)      # cost 3.0, 最后才打
    spider = _det("spider", "Legendary", (33, 46), factory_scale)
    plan = main._plan_path_approach([barrel, spider], here, m, AREA_WALL, max_cells=40, max_px=2000)
    assert plan["target"] is spider


def test_path_approach_on_a_line_of_sight_target_aims_straight_at_it(factory_scale):
    m = _wall_map()
    det = _det("crab", "Legendary", (20, 20), factory_scale)
    plan = main._plan_path_approach([det], (10, 20), m, AREA_WALL, max_cells=40, max_px=2000)
    assert plan["waypoint"] == (20, 20)


def test_aim_mouse_points_from_the_player_toward_the_waypoint(monkeypatch):
    monkeypatch.setattr(main.utils, "SCREEN_WIDTH", 1920)
    monkeypatch.setattr(main.utils, "SCREEN_HEIGHT", 1080)
    x, y = main._aim_mouse((960, 540), (10, 10), (20, 10))
    assert y == 540 and x > 960
    x, y = main._aim_mouse((960, 540), (10, 10), (10, 0))
    assert x == 960 and y < 540
    assert main._aim_mouse((960, 540), (10, 10), (10, 10)) == (960, 540)


def test_wander_goal_is_reachable_inside_the_area_and_not_too_close():
    m = _wall_map()
    import random
    for seed in range(30):
        goal = main._pick_wander_goal(m, (20, 20), AREA_WALL, rng=random.Random(seed))
        assert goal is not None
        dist, _ = main._bfs_reach(m, (20, 20), main.WANDER_GOAL_MAX_CELLS)
        assert main.WANDER_GOAL_MIN_CELLS <= dist[goal] <= main.WANDER_GOAL_MAX_CELLS
        assert 5 <= goal[0] <= 55 and 5 <= goal[1] <= 55
        assert m[goal[1], goal[0]] == 255


def test_wander_goal_is_none_when_nothing_qualifies():
    tiny = _np.full((60, 60), 0, dtype=_np.uint8)
    tiny[20:23, 20:23] = 255                         # 一个 3x3 的小窝, 走不出 MIN 格
    assert main._pick_wander_goal(tiny, (21, 21), AREA_WALL) is None


def _farming_env(monkeypatch, m=None):
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main.utils, "MAP", "factory")
    monkeypatch.setattr(main, "load_binary_map", lambda: m if m is not None else _wall_map())
    monkeypatch.setattr(main, "MYTHIC_LATCH_ENABLED", False)


class _Done(Exception):
    pass


def test_auto_farming_in_a_maze_approaches_a_walled_off_mob_instead_of_wandering(monkeypatch, factory_scale):
    _farming_env(monkeypatch)
    monkeypatch.setattr(main, "get_player_position", lambda: (28, 45))
    det = _det("spider", "Legendary", (33, 45), factory_scale)
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("wander", None), [det], 0.0, True))
    moved = {}

    def drive(mouse_target, current_pos, hist, state, message, **kw):
        moved.update(target=mouse_target, state=state, message=message)
        raise _Done

    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    monkeypatch.setattr(main, "move_to_position",
                        lambda *a, **k: pytest.fail("隔墙有怪却去随机漫游了"))
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960, 540))
    with pytest.raises(_Done):
        main.auto_farming(AREA_WALL, 30, enemy_ai_enabled=True)
    assert moved["state"] == "索敌中" and "spider" in moved["message"]
    assert moved["target"][1] > 540                  # 先往下绕


def test_auto_farming_wanders_the_old_way_on_open_maps(monkeypatch, factory_scale):
    # 只有迷宫图(工厂)和沙漠里的走廊形刷怪区走新逻辑; 开阔的沙漠区 / 蚁穴等照旧
    _farming_env(monkeypatch, _np.full((60, 60), 255, dtype=_np.uint8))
    monkeypatch.setattr(main.utils, "MAP", "desert")
    monkeypatch.setattr(main, "get_player_position", lambda: (28, 45))
    det = _det("spider", "Legendary", (33, 45), factory_scale)
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("wander", None), [det], 0.0, True))
    monkeypatch.setattr(main, "_plan_path_approach",
                        lambda *a, **k: pytest.fail("非迷宫图不该绕路追"))
    monkeypatch.setattr(main, "random_walkable_point", lambda *a, **k: (40, 40))

    def move(*a, **k):
        raise _Done
    monkeypatch.setattr(main, "move_to_position", move)
    with pytest.raises(_Done):
        main.auto_farming(AREA_WALL, 30, enemy_ai_enabled=True)


# ── 沙漠里的走廊形刷怪区也要走迷宫寻怪(2026-10-10 苏州用户日志) ───────────────────────────────
# 那份日志: 沙漠, 刷怪区是一条贴着墙拐来拐去的南北向走廊(184,138)-(218,292), 刷怪时长 4.2 万秒里「原地打转
# 判定卡住」2289 次、「20次尝试后仍未到达」264 次, 77% 的漫游腿以卡住收场; 人几乎全程待在区域最上头的路口
# (进区位置 175 次是 (184,144), 出区 128 次都在 x=219 / y 在 137~151)。老漫游在整块矩形里随机挑可走点、
# 直线走过去(move_to_position 不带地图, 不会绕墙), 走廊一拐弯就顶墙。

_USER_DESERT_AREA = [(184, 138), (218, 292)]


def _real_map(name):
    import cv2
    return cv2.imread(f"maps/{name}.png", cv2.IMREAD_GRAYSCALE)


def test_the_real_desert_corridor_from_the_user_log_counts_as_corridor_shaped():
    assert main._area_is_corridor(_real_map("desert"), _USER_DESERT_AREA) is True


@pytest.mark.parametrize("area", [[(20, 120), (100, 190)], [(225, 185), (290, 285)]])
def test_open_blobs_of_the_real_desert_do_not_count_as_corridors(area):
    assert main._area_is_corridor(_real_map("desert"), area) is False


def test_corridor_check_is_deterministic():
    m = _real_map("desert")
    assert len({main._area_is_corridor(m, _USER_DESERT_AREA) for _ in range(5)}) == 1


def test_corridor_check_copes_with_degenerate_input():
    assert main._area_is_corridor(None, _USER_DESERT_AREA) is False
    assert main._area_is_corridor(_real_map("desert"), None) is False
    solid = _np.zeros((60, 60), dtype=_np.uint8)                   # 区里一个可走点都没有
    assert main._area_is_corridor(solid, AREA_WALL) is False
    assert main._area_is_corridor(_np.full((60, 60), 255, dtype=_np.uint8), [(500, 500), (600, 600)]) is False
    # 不是 ndarray 的地图(测试替身 / 别处塞了个 list)也不能抛 —— 这个检查绝不能把刷怪循环带崩
    assert main._area_is_corridor([[255]], [(0, 0), (9, 9)]) is False
    assert main._area_is_corridor(_np.full((60, 60), 255, dtype=_np.uint8), "垃圾") is False


@pytest.mark.parametrize("map_name,m,area,ai,expected", [
    ("factory", _wall_map(), AREA_WALL, True, True),                       # 迷宫图: 一直开
    ("factory", _np.full((60, 60), 255, dtype=_np.uint8), AREA_WALL, True, True),
    ("desert", _wall_map(), AREA_WALL, True, True),                        # 沙漠 + 走廊形区域
    ("desert", _np.full((60, 60), 255, dtype=_np.uint8), AREA_WALL, True, False),   # 沙漠 + 开阔区域
    ("ocean", _wall_map(), AREA_WALL, True, False),                        # 别的图没证据, 不动
    ("garden", _wall_map(), AREA_WALL, True, False),
    ("desert", _wall_map(), AREA_WALL, False, False),                      # 索敌关着不走
    ("factory", None, AREA_WALL, True, False),                             # 没有地图图
])
def test_which_farming_runs_use_the_maze_style_seek(monkeypatch, map_name, m, area, ai, expected):
    monkeypatch.setattr(main.utils, "MAP", map_name)
    assert main._use_maze_seek(m, area, ai) is expected


def test_auto_farming_wanders_through_the_corridor_by_path_on_a_desert_corridor_area(monkeypatch):
    _farming_env(monkeypatch, _wall_map())
    monkeypatch.setattr(main.utils, "MAP", "desert")
    monkeypatch.setattr(main, "get_player_position", lambda: (28, 45))
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("wander", None), [], 0.0, True))
    monkeypatch.setattr(main, "_pick_wander_goal", lambda *a, **k: (35, 45))       # 墙对面
    monkeypatch.setattr(main, "random_walkable_point",
                        lambda *a, **k: pytest.fail("走廊形区域不该再瞎挑直线目标"))
    monkeypatch.setattr(main, "move_to_position",
                        lambda *a, **k: pytest.fail("走廊形区域不该再用 move_to_position 直线走"))
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960, 540))
    seen = {}

    def drive(mouse_target, current_pos, hist, state, message, **kw):
        seen.update(target=mouse_target, state=state)
        raise _Done
    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    with pytest.raises(_Done):
        main.auto_farming(AREA_WALL, 30, enemy_ai_enabled=True)
    assert seen["target"][1] > 540                   # 目标在墙另一侧: 先往下绕口子


def test_auto_farming_in_a_maze_wanders_by_steering_the_mouse_not_by_hopping(monkeypatch, factory_scale):
    """2026-10-10 实机: 漫游这一腿的目标取"路径上直线看得见的最远点", 走廊一拐弯它就落在 ARRIVE_RADIUS(5 格)
    以内, move_to_position 认为到了、一个 moveTo 都不发 —— 日志连续几十行 `移动到 (112, 85), 先到 (114, 95)`,
    人站在原地没动, 每轮开头白站 30~100 秒。迷宫漫游改成每拍朝路径点推鼠标(跟绕路同一套), 不再逐段 hop。"""
    m = _wall_map()
    _farming_env(monkeypatch, m)
    monkeypatch.setattr(main, "get_player_position", lambda: (28, 45))
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("wander", None), [], 0.0, True))
    monkeypatch.setattr(main, "_pick_wander_goal", lambda *a, **k: (35, 45))   # 墙对面
    monkeypatch.setattr(main, "random_walkable_point",
                        lambda *a, **k: pytest.fail("迷宫图漫游不该再瞎挑目标"))
    monkeypatch.setattr(main, "move_to_position",
                        lambda *a, **k: pytest.fail("迷宫图漫游不该再用 move_to_position 逐段 hop"))
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960, 540))
    seen = {}

    def drive(mouse_target, current_pos, hist, state, message, **kw):
        seen.update(target=mouse_target, state=state)
        raise _Done
    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    with pytest.raises(_Done):
        main.auto_farming(AREA_WALL, 30, enemy_ai_enabled=True)
    assert seen["target"] != (960, 540)              # 真的在推鼠标, 不是原地不动
    assert seen["target"][1] > 540                   # 目标在墙另一侧: 先往下绕口子


def test_auto_farming_in_a_maze_keeps_the_same_wander_goal_until_reached(monkeypatch, factory_scale):
    m = _wall_map()
    _farming_env(monkeypatch, m)
    pos = {"p": (10, 10)}
    monkeypatch.setattr(main, "get_player_position", lambda: pos["p"])
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("wander", None), [], 0.0, True))
    picks = []
    monkeypatch.setattr(main, "_pick_wander_goal", lambda *a, **k: picks.append(1) or (20, 10))
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960, 540))
    ticks = []

    def drive(mouse_target, current_pos, *a, **kw):
        ticks.append(current_pos)
        if len(ticks) == 1:
            pos["p"] = (14, 10)                      # 走了一截, 没到
        elif len(ticks) == 2:
            pos["p"] = (20, 10)                      # 到了
        elif len(ticks) == 3:
            raise _Done
        return "moved"
    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    with pytest.raises(_Done):
        main.auto_farming(AREA_WALL, 30, enemy_ai_enabled=True)
    assert len(picks) == 2         # 第 1、2 拍共用一个目标; 到了之后第 3 拍才重新挑


def test_auto_farming_in_a_maze_drops_the_goal_after_a_stall(monkeypatch, factory_scale):
    m = _wall_map()
    _farming_env(monkeypatch, m)
    monkeypatch.setattr(main, "get_player_position", lambda: (10, 10))
    monkeypatch.setattr(main, "_maybe_scan_enemies",
                        lambda *a, **k: (("wander", None), [], 0.0, True))
    picks = []
    monkeypatch.setattr(main, "_pick_wander_goal", lambda *a, **k: picks.append(1) or (20, 10))
    monkeypatch.setattr(main.enemy_detect, "current_center", lambda: (960, 540))
    n = []

    def drive(*a, **kw):
        n.append(1)
        if len(n) == 2:
            raise _Done
        return "stuck"
    monkeypatch.setattr(main, "_drive_and_check_stall", drive)
    with pytest.raises(_Done):
        main.auto_farming(AREA_WALL, 30, enemy_ai_enabled=True)
    assert len(picks) == 2         # 卡住(已脱困)后换个目标


# ── 路径只在刷怪区里走: 绕路 / 漫游的 BFS 带区域边界 ─────────────────────────────────────
# 2026-10-10 实机: 工厂刷怪区下沿 y=98, 区外一格 y=99 是条走廊, 绕路 / 漫游的路径从那儿穿出去(143,99) ->
# 「离开刷怪区域」-> 回区

def test_bfs_reach_stays_inside_the_bounds():
    m = _np.full((30, 30), 255, dtype=_np.uint8)
    dist, _ = main._bfs_reach(m, (10, 10), 20, bounds=(5, 5, 15, 15))
    assert dist and all(5 <= x <= 15 and 5 <= y <= 15 for x, y in dist)
    # 起点在边界外一格(人刚飘出去)也能走回来, 只是不往外扩
    dist2, _ = main._bfs_reach(m, (10, 16), 20, bounds=(5, 5, 15, 15))
    assert (10, 15) in dist2 and not any(y > 16 for _, y in dist2)


def test_path_approach_never_routes_outside_the_area(factory_scale):
    m = _wall_map()
    # 墙上下口子在 y<10 和 y>49; 区域只到 y<=45 -> 绕不过去, 不该规划出一条出区的路
    area = [(5, 5), (55, 45)]
    det = _det("spider", "Legendary", (33, 30), factory_scale)
    assert main._plan_path_approach([det], (28, 30), m, area, max_cells=80, max_px=2000) is not None  # 走上口子(y<10)可以
    area_low = [(5, 20), (55, 45)]                    # 上口子也被切掉
    assert main._plan_path_approach([det], (28, 30), m, area_low, max_cells=80, max_px=2000) is None


def test_wander_goal_only_uses_paths_inside_the_area():
    m = _wall_map()
    area = [(5, 20), (55, 45)]                        # 把两个口子都切在区外
    import random
    for seed in range(20):
        g = main._pick_wander_goal(m, (20, 30), area, rng=random.Random(seed))
        assert g is not None and g[0] < 30            # 墙另一侧走不到


def test_wander_waypoint_is_none_when_the_only_path_leaves_the_area():
    m = _wall_map()
    area_low = [(5, 20), (55, 45)]                    # 墙的两个口子都在区外
    assert main._wander_waypoint(m, (28, 30), (33, 30), area_low) is None
    # 区域放开(口子在区内)就有路, 路点从这里直线看得到
    wp = main._wander_waypoint(m, (28, 30), (33, 30), [(5, 5), (55, 55)])
    assert wp is not None and main.line_of_sight(m, main._Pt(28, 30), main._Pt(*wp))


# ── 离开刷怪区域后回哪儿: 最近的边内点是墙时不能退回区域中心(中心也可能是墙) ──────────────
# 2026-10-10 实机: 区域 [(46,25),(193,98)], 人在 (143,99): 夹出来的边内点 (143,90) 是墙 -> None -> 退回区域中心
# (119,61), 那也是墙且从这儿走不到 -> 寻路 18 秒后「路径规划失败」-> 「无法回到刷怪区域」整轮报废。

def test_nearest_reachable_inside_finds_a_walkable_cell_by_path():
    m = _np.zeros((300, 300), dtype=_np.uint8)
    m[95:100, 100:150] = 255                          # 一条 y=95..99 的走廊, 区域下沿 y=98
    area = [(46, 25), (193, 98)]
    assert main._nearest_inside(area, (143, 99), m) is None          # 老办法: 夹出来的点是墙
    got = main._nearest_reachable_inside(area, (143, 99), m)
    assert got is not None and m[got[1], got[0]] == 255
    assert 46 <= got[0] <= 193 and 25 <= got[1] <= 98
    assert main.if_in_area([area], got)
    assert math.hypot(got[0] - 143, got[1] - 99) > main.ARRIVE_RADIUS   # 不能落在到达半径里: 一个 moveTo 都不发


def test_nearest_reachable_inside_is_none_when_nothing_inside_is_reachable():
    m = _np.zeros((300, 300), dtype=_np.uint8)
    m[100:104, 100:110] = 255                          # 区外的一小块, 够不着区内
    assert main._nearest_reachable_inside([(46, 25), (193, 98)], (105, 101), m) is None


def test_auto_farming_returns_to_a_reachable_cell_not_a_wall_center(monkeypatch):
    m = _np.zeros((300, 300), dtype=_np.uint8)
    m[95:100, 100:150] = 255
    _pathing_env(monkeypatch)
    monkeypatch.setattr(main, "load_binary_map", lambda: m)
    monkeypatch.setattr(main, "get_player_position", lambda: (143, 99))    # 区外一格
    area = [(46, 25), (193, 98)]
    seen = {}

    def path(target, areas, **kw):
        seen["target"] = target
        raise _Done
    monkeypatch.setattr(main, "lazy_theta_pathing", path)
    with pytest.raises(_Done):
        main.auto_farming(area, 30, enemy_ai_enabled=False)
    tx, ty = seen["target"]
    assert m[ty, tx] == 255                          # 不是区域中心(119,61)那堵墙


# ── 路径离墙要有净空(2026-10-10 录像: 规划出的每一跳离墙都只有 0.95 格, 贴着墙走) ──────────────────
# 工厂 (180,145) 那个走廊转角在 8 份录像里卡了 9 次: Lazy Theta* 的任意角直线只看"没穿墙", 最短路天然贴着
# 内侧墙角, 小地图 ±1 格的误差 + 角色有体积 + 鼠标驱动的滑动 = 蹭墙卡住。规划时给"贴墙"的格子加代价,
# 路径自然走通道中线; 实在只有窄通道(密道)可走的地方照样走, 只是更贵。

def _min_clearance(m, nodes):
    import cv2
    walk = (m == 255).astype(np.uint8)
    cl = cv2.distanceTransform(cv2.copyMakeBorder(walk, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0),
                               cv2.DIST_L2, 3)[1:-1, 1:-1]
    lo = 99.0
    for a, b in zip(nodes, nodes[1:]):
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) * 2))
        for i in range(n + 1):
            x = int(round(a[0] + (b[0] - a[0]) * i / n))
            y = int(round(a[1] + (b[1] - a[1]) * i / n))
            lo = min(lo, float(cl[y, x]))
    return lo


def _wide_l_corridor(width=5):
    """从左下往右上拐一个直角弯的走廊(宽 width), 外面全是墙。"""
    m = np.zeros((60, 60), dtype=np.uint8)
    m[40:40 + width, 5:50] = 255          # 横段
    m[5:40 + width, 45:45 + width] = 255  # 竖段
    return m


def test_planned_path_keeps_off_the_walls_where_the_corridor_allows(monkeypatch):
    m = _wide_l_corridor(5)
    start, goal = (6, 42), (47, 6)
    nodes = main.lazy_theta_star(m, start, goal)
    assert nodes is not None and nodes[0] == start and nodes[-1] == goal
    # 起点终点各贴在走廊头尾(净空小, 躲不掉); 中间的路点要在通道中线附近
    inner = nodes[1:-1] or nodes
    for x, y in inner:
        assert _min_clearance(m, [(x, y), (x, y)]) >= 1.9, (x, y)
    assert _min_clearance(m, nodes[1:-1] if len(nodes) > 2 else nodes) >= 1.9
    # 旧行为(不带净空)在同一张图上贴着墙角, 净空 < 1.5 —— 守住"这条测试真的在测净空"
    plain = main.lazy_theta_star(m, start, goal, clearance=False)
    assert _min_clearance(m, plain[1:-1] if len(plain) > 2 else plain) < 1.5


def test_planned_path_still_threads_a_one_wide_tunnel(monkeypatch):
    m = np.zeros((40, 60), dtype=np.uint8)
    m[5:35, 5:15] = 255                              # 左厅
    m[5:35, 45:55] = 255                             # 右厅
    m[20, 15:45] = 255                               # 一格宽的密道
    nodes = main.lazy_theta_star(m, (10, 20), (50, 20))
    assert nodes is not None and nodes[-1] == (50, 20)
    assert all(m[y, x] == 255 for x, y in nodes)


def test_planned_path_prefers_the_short_tunnel_over_a_long_wide_detour():
    # 密道 14 格 vs 绕大宽走廊 ~100 格: 窄通道再贵也该走密道(净空惩罚不能大到把密道判死刑)
    m = np.zeros((70, 60), dtype=np.uint8)
    m[5:15, 5:15] = 255
    m[5:15, 45:55] = 255
    m[10, 15:45] = 255                               # 一格宽的密道(30 格长)
    m[10:60, 5:15] = 255                             # 左厅往下接一条宽走廊
    m[55:65, 5:55] = 255
    m[10:65, 45:55] = 255                            # 绕一大圈回右厅
    nodes = main.lazy_theta_star(m, (10, 10), (50, 10))
    assert nodes is not None
    assert all(y <= 20 for _, y in nodes), "该走密道, 却绕了下面的大圈"     # 任意角路径: 密道是一条直线段, 没有中间路点


def test_clearance_planning_can_be_switched_off():
    m = _wide_l_corridor(5)
    a = main.lazy_theta_star(m, (6, 42), (47, 6), clearance=False)
    assert a is not None


# ── 规划结果缓存: 工厂每次死亡都从同一个出生点 (58,285) 规划到同一个刷怪点, Windows 上一次 9~12 秒 ──────────
# 2026-10-10 三小时录像: 56 轮里 25 轮从 (58,285) 出发走同一条长路线, 光规划就 ~4 分钟。同一张图 + 同一起点 + 同一
# 终点, Lazy Theta* 的结果是确定的 —— 命中就直接给, 不重算。键里带地图内容指纹(开了传送门缺口的图是另一张图)。

def test_lazy_theta_star_caches_identical_requests(monkeypatch):
    main._PLAN_CACHE.clear()
    m = _wide_l_corridor(5)
    calls = []
    real_pen = main._wall_penalty
    monkeypatch.setattr(main, "_wall_penalty", lambda mm: calls.append(1) or real_pen(mm))
    a = main.lazy_theta_star(m, (6, 42), (47, 6))
    n_first = len(calls)
    b = main.lazy_theta_star(m, (6, 42), (47, 6))
    assert a == b and a is not None
    assert len(calls) == n_first, "第二次相同请求不该重新规划"
    a.append((0, 0))                                     # 调用方改了拿到的列表, 缓存里的不能跟着变
    assert main.lazy_theta_star(m, (6, 42), (47, 6)) == b


def test_plan_cache_is_keyed_on_map_start_goal_and_clearance():
    main._PLAN_CACHE.clear()
    m = _wide_l_corridor(5)
    p1 = main.lazy_theta_star(m, (6, 42), (47, 6))
    assert main.lazy_theta_star(m, (7, 42), (47, 6)) is not None          # 起点不同 -> 另一条
    m2 = m.copy()
    m2[40:45, 25:30] = 0                                                  # 把走廊整段封死(地图内容变了) -> 不能命中旧路径
    assert main.lazy_theta_star(m2, (6, 42), (47, 6)) is None
    plain = main.lazy_theta_star(m, (6, 42), (47, 6), clearance=False)    # 带不带净空是两个键
    assert plain is not None and plain != p1
    assert main.lazy_theta_star(m, (6, 42), (47, 6)) == p1                # 原图原请求仍命中老结果


def test_plan_cache_does_not_remember_failures_forever():
    main._PLAN_CACHE.clear()
    m = np.zeros((20, 20), dtype=np.uint8)
    m[2, 2] = 255
    m[15, 15] = 255
    assert main.lazy_theta_star(m, (2, 2), (15, 15)) is None
    m[2:16, 2:16] = 255                                                   # 同一个数组被改成通的(地图内容变了)
    assert main.lazy_theta_star(m, (2, 2), (15, 15)) is not None


# ── 纯追踪走路径(工厂) ───────────────────────────────────────────────────────────
# 2026-10-10 工厂录像 + 官方 factory.tmj: 反复卡住的 (151,188) / (180,145) 都在官方 shortcut(通风管)层的瓦片上
# —— 密道, 实际可走宽度恰好 1 个瓦片(512 世界单位 ≈ 3.2 个小地图格, 小地图画成 4 格); 跑图装的速度 1000~3000 u/s
# (12 格/秒), 而 move_to_position 一拍要 ~0.35 秒(每拍两次抓屏查死亡), 一拍跑 4 格, 逐点"追节点 + 比例鼠标偏移"
# 的闭环在这种延迟下会振荡: 录像里在竖井口沿墙来回荡 ±7 格, 14 拍后判卡住, 脱困 + 重规划又十几秒。
# 纯追踪: 瞄**路径上前方 PURSUIT_LOOKAHEAD 格**的点(永远在前面, 不存在"冲过节点"), 鼠标偏移满量(不做比例
# 增益), 只有临近终点才减速; 查死亡画面降到每 0.5 秒一次。

class _PursuitFlower:
    """离线物理: 速度朝鼠标方向走(满量偏移=vmax 格/秒), 撞墙沿轴滑; 位置每次读取推进 dt 秒。
    坐标跟实机读数一致: 像素下标 i 的中心在读数 i+0.5(像素 = floor(读数)); start 给像素下标, 站在它的中心。
    身体有半径 RADIUS(实机 ~0.2 格, 这里取 0.3 偏保守): 四个角任何一个进墙就算撞上 —— 贴着拐角内侧擦过去会被顶住。"""

    RADIUS = 0.3

    def __init__(self, walk, start, vmax=6.0, dt=0.12):
        self.walk, self.vmax, self.dt = walk, vmax, dt
        self.p = [float(start[0]) + 0.5, float(start[1]) + 0.5]
        self.mouse = None

    def _wall(self, x, y):
        h, w = self.walk.shape
        for ox in (-self.RADIUS, self.RADIUS):
            for oy in (-self.RADIUS, self.RADIUS):
                xi, yi = int(math.floor(x + ox)), int(math.floor(y + oy))
                if not (0 <= xi < w and 0 <= yi < h) or self.walk[yi, xi] == 0:
                    return True
        return False

    def read(self, precise=False):
        if self.mouse is not None:
            ox = self.mouse[0] - main.SCREEN_WIDTH // 2
            oy = self.mouse[1] - main.SCREEN_HEIGHT // 2
            d = (ox * ox + oy * oy) ** 0.5
            if d >= 1:
                sp = self.vmax * min(d / (250 * main.mouse_scale()), 1.0)
                for _ in range(6):
                    for tx, ty in ((self.p[0] + sp * ox / d * self.dt / 6, self.p[1] + sp * oy / d * self.dt / 6),
                                   (self.p[0] + sp * ox / d * self.dt / 6, self.p[1]),
                                   (self.p[0], self.p[1] + sp * oy / d * self.dt / 6)):
                        if not self._wall(tx, ty):
                            self.p = [tx, ty]
                            break
        return (self.p[0], self.p[1]) if precise else (int(math.floor(self.p[0])), int(math.floor(self.p[1])))


def _pursuit_env(monkeypatch, walk, start, **kw):
    _stub_move_env(monkeypatch)
    flower = _PursuitFlower(walk, start, **kw)
    monkeypatch.setattr(main, "get_player_position", lambda precise=False, **k: flower.read(precise))
    monkeypatch.setattr(main, "_move_mouse_safely", lambda p: setattr(flower, "mouse", p))
    monkeypatch.setattr(main, "reset_keyboard", lambda: setattr(flower, "mouse", None))
    monkeypatch.setattr(main, "release_keys", lambda: None)
    return flower


def _zigzag_map():
    """宽 60 的图里一条 4 格宽、带三个直角弯的走廊(像工厂的密道)。"""
    m = np.zeros((60, 60), dtype=np.uint8)
    m[8:12, 5:40] = 255      # 横 1
    m[8:30, 36:40] = 255     # 竖 1
    m[26:30, 20:40] = 255    # 横 2 (向左)
    m[26:52, 20:24] = 255    # 竖 2
    return m


def _vent_map():
    """工厂 (180,161) 井口的几何: 4 格宽的横走廊拐进 4 格宽、往北的竖井(像素 180..183)。"""
    m = np.zeros((200, 200), dtype=np.uint8)
    m[161:165, 150:190] = 255        # 横走廊(4 格宽)
    m[140:161, 180:184] = 255        # 竖井(4 格宽, 可走 c.x ∈ [180, 184))
    return m


def test_follow_path_does_not_cut_the_corner_into_the_vent_mouth(monkeypatch):
    """2026-10-11 录像: 横走廊拐进竖井, 前瞻点落在拐角后面, 瞄点连线从内侧墙角擦过 -> 人贴着井口左墙
    (c.x≈180.0)往北顶, 4 秒进度不涨判卡住, 同一个地方 4 次。瞄点要离墙边够远, 拐角前瞄拐角本身。"""
    m = _vent_map()
    start, goal = (152, 162), (181, 142)
    path = main.lazy_theta_star(m, start, goal)
    assert path is not None
    flower = _pursuit_env(monkeypatch, m, start, vmax=12.0, dt=0.15)
    clock = {"t": 0.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    monkeypatch.setattr(main.time, "sleep", lambda s: clock.update(t=clock["t"] + max(s, 0.05)))
    real_read = main.get_player_position
    monkeypatch.setattr(main, "get_player_position",
                        lambda precise=False, **k: (clock.update(t=clock["t"] + 0.15), real_read(precise))[1])
    assert main.follow_path(path, binary_map=m) is True
    assert flower.p[1] < 148                                    # 真的钻进井里往北走了(到终点 5 格内就算到)


def test_inset_areas_shrinks_farm_areas_but_not_portal_boxes():
    farm = [[(46, 25), (193, 98)]]
    assert main._inset_areas(farm, 4) == [((50, 29), (189, 94))]
    assert main._inset_areas([[(193, 98), (46, 25)]], 4) == [((50, 29), (189, 94))]     # 两个角顺序不限
    portal = [[(145, 35), (155, 45)]]                                  # 传送点到达框 10x10: 原样
    assert main._inset_areas(portal, 4) == [((145, 35), (155, 45))]


def test_lazy_pathing_only_stops_a_few_cells_inside_the_farm_area(monkeypatch):
    """进区就收手贴着边界, 惯性一滑就出区, 「离开刷怪区域 -> 重新寻路回去」每 25 秒一轮(2026-10-11)。"""
    _stub_move_env(monkeypatch)
    m = np.full((120, 220), 255, dtype=np.uint8)
    monkeypatch.setattr(main, "load_binary_map", lambda: m)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "_on_wrong_map", lambda: None)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main.cdp_bridge, "inject_canvas_hook", lambda *a, **k: None, raising=False)
    seen = {}
    pos = {"p": (170, 120 - 5)}

    def fake_exec(path, stop_when=None, **kw):
        seen["stop"] = stop_when
        pos["p"] = (170, 60)
        return True

    monkeypatch.setattr(main, "execute_path", fake_exec)
    monkeypatch.setattr(main, "get_player_position", lambda *a, **k: pos["p"])
    monkeypatch.setattr(main, "_use_pursuit", lambda: False)
    area = [[(46, 25), (193, 98)]]
    main.lazy_theta_pathing((145, 72), area)
    assert seen["stop"]((170, 98)) is False          # 刚踩进边界: 还要往里走
    assert seen["stop"]((170, 96)) is False
    assert seen["stop"]((170, 93)) is True           # 离边 5 格: 够了


def test_pursuit_aim_stops_short_of_a_corner_that_would_graze_the_wall():
    m = _vent_map()
    clear = main._clearance_map(m)
    # 路径: 沿横走廊中线往东到拐角 (182.0, 163.0), 再往北进井
    pts = [(152.0, 163.0), (182.0, 163.0), (182.0, 141.0)]
    cum = [0.0, 30.0, 52.0]
    pos = (178.0, 163.0)                                        # 离拐角 4 格, 前瞻 6 格会越过拐角
    raw = main._path_point(pts, cum, 28.0 + main.PURSUIT_LOOKAHEAD)
    assert raw == (182.0, 159.0)
    assert not main._clear_los(clear, pos, raw)                 # 直奔它要从井口左墙角(180,161)上擦过去
    aim = main._pursuit_aim(pts, cum, 28.0, pos, m, clear)
    assert aim is not None and aim != raw
    assert main._clear_los(clear, pos, aim)                     # 选的瞄点连线离墙边够远
    assert aim[0] > pos[0]                                      # 朝拐角方向, 没往墙里削


def test_pursuit_aim_pulls_a_wall_hugging_path_point_to_the_corridor_middle():
    """工厂 (181,161)->(180,143) 那条路径: 节点是像素下标, 竖井里它走在最左一列(c.x∈[180,181), 离墙边 0.5 格),
    人瞄着它稍有横向误差就蹭墙。瞄点要先挪到井的中线(c.x≈182)。"""
    m = _vent_map()
    clear = main._clearance_map(m)
    hug = (180.5, 150.5)                                         # 井里最左一列的中心
    assert main._edge_dist(clear, *hug) < 0.6
    mid = main._centered(m, clear, hug)
    assert 181.4 <= mid[0] <= 182.6 and abs(mid[1] - hug[1]) <= 1.0
    assert main._edge_dist(clear, *mid) >= 1.3
    # 本来就在中间的点不动
    assert main._centered(m, clear, (182.0, 150.5)) == (182.0, 150.5)


def test_centering_never_jumps_through_a_wall_into_the_next_corridor(monkeypatch):
    monkeypatch.setattr(main, "PURSUIT_CENTER_RADIUS", 4.0)
    m = np.zeros((40, 60), dtype=np.uint8)
    m[10:12, 5:50] = 255                    # 上面一条 2 格宽的走廊, 隔 1 格墙
    m[13:17, 5:50] = 255                    # 下面一条 4 格宽, 中线(y=15)净空更大, 在挪动半径内, 诱惑挪过去
    clear = main._clearance_map(m)
    assert main._edge_dist(clear, 20.5, 15.0) > main._edge_dist(clear, 20.5, 11.0) + 0.5
    q = main._centered(m, clear, (20.5, 11.0))
    assert 10.0 <= q[1] < 12.0              # 还在上面那条里


def test_pursuit_aim_uses_the_centered_point_when_the_path_hugs_the_wall():
    m = _vent_map()
    clear = main._clearance_map(m)
    pts = [(180.5, 160.5), (180.5, 141.5)]                   # 路径走在竖井最左一列
    cum = [0.0, 19.0]
    pos = (182.0, 158.0)
    aim = main._pursuit_aim(pts, cum, 2.5, pos, m, clear)
    assert aim is not None and aim[0] >= 181.4               # 瞄的是井中线, 不是贴墙的那一列


def test_pursuit_aim_from_a_wall_hugging_start_heads_along_the_corridor_not_into_the_corner():
    """录像里的停滞点: 人贴着走廊上沿(c.y=161.4)走到 c.x=180.0, 井口就在前面。瞄点不能再往北削拐角
    (人身子已经压着井口左墙角, 往北直接顶墙); 要沿走廊往东、往中间走, 过了井口再进井。"""
    m = _vent_map()
    clear = main._clearance_map(m)
    path = [(152, 163), (170, 163), (181, 161), (181, 143)]
    off = main.PURSUIT_NODE_OFFSET
    pts = [(x + off, y + off) for x, y in path]
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    pos = (180.0, 161.4)
    s = main._path_progress(pts, cum, pos, 0.0)
    aim = main._pursuit_aim(pts, cum, s, pos, m, clear)
    assert aim is not None
    assert aim[0] - pos[0] > abs(aim[1] - pos[1])               # 往东为主, 不是往北


def _pillar_gap_scene():
    """工厂 (125,246) 的几何缩小版: 沿墙垛左面往北的长直路, 拐进墙垛之间的口子往东, 出口再往北。
    人满速冲过口子往北多跑了一截(overshoot)。"""
    m = np.zeros((60, 40), dtype=np.uint8)
    m[:, 0:6] = 255                  # 左边的长走廊
    m[19:24, 0:12] = 255             # 口子(5 格宽)通到右边
    m[0:24, 9:12] = 255              # 口子出口往北的走廊
    path = [(4, 50), (4, 21), (10, 21), (10, 2)]
    off = main.PURSUIT_NODE_OFFSET
    pts = [(x + off, y + off) for x, y in path]
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return m, pts, cum


def test_path_progress_does_not_jump_across_a_wall_to_a_later_leg():
    m, pts, cum = _pillar_gap_scene()
    pos = (4.5, 12.0)                                    # 冲过口子往北 9 格
    s0 = 29.0                                            # 进度停在拐角
    assert main._path_progress(pts, cum, pos, s0) > s0 + 8       # 几何上最近的点在墙那边的后一段
    # 不许跳: 那一点从人这里看不见(隔着墙垛), 只推进到看得见的那几格(口子入口), 不是跳 8 格过墙
    assert main._advance_progress(pts, cum, pos, s0, m) <= s0 + main.PURSUIT_ADVANCE_MAX / 2
    assert main._advance_progress(pts, cum, pos, s0, None) == s0 + main.PURSUIT_ADVANCE_MAX   # 没地图只封顶
    # 正常往前走(同一条走廊里, 看得见)照常推进, 但一次不超过上限
    pos2 = (4.5, 40.0)
    assert main._advance_progress(pts, cum, pos2, 2.0, m) > 2.0
    assert main._advance_progress(pts, cum, pos2, 2.0, m) <= 2.0 + main.PURSUIT_ADVANCE_MAX


def test_pursuit_aim_goes_back_to_the_path_when_every_point_ahead_is_behind_a_wall():
    m, pts, cum = _pillar_gap_scene()
    clear = main._clearance_map(m)
    pos = (4.5, 12.0)
    s0 = 33.0                                           # 进度已经在口子里面, 前面的点全在墙垛后面
    assert not any(main._visible(m, pos, main._path_point(pts, cum, s0 + d)) for d in (0.6, 1.5, 3, 6))
    aim = main._pursuit_aim(pts, cum, s0, pos, m, clear)
    assert aim is not None                              # 不再返回 None 让调用方瞎绕
    assert main._visible(m, pos, aim)                   # 瞄的是看得见的点(先回到路径上)
    assert aim[1] > pos[1]                              # 往南回到口子那一层, 不是继续往北


def test_edge_dist_is_the_distance_to_the_wall_edge():
    m = np.zeros((20, 20), dtype=np.uint8)
    m[5:15, 5:15] = 255                                         # 10x10 可走, 外面是墙
    clear = main._clearance_map(m)
    assert main._edge_dist(clear, 10.0, 10.0) == pytest.approx(4.5, abs=0.6)   # 中心 ~5 格到边
    assert main._edge_dist(clear, 5.0, 10.0) == pytest.approx(0.0, abs=0.05)   # 贴在墙边上
    assert main._edge_dist(clear, 5.5, 10.0) == pytest.approx(0.5, abs=0.05)
    assert main._edge_dist(clear, 2.0, 2.0) < 0                                # 墙里是负的


def test_follow_path_walks_a_zigzag_corridor_at_speed(monkeypatch):
    m = _zigzag_map()
    start, goal = (6, 10), (22, 50)
    path = main.lazy_theta_star(m, start, goal)
    flower = _pursuit_env(monkeypatch, m, start, vmax=14.0)        # 跑图装速度(一拍 ~1.7 格)
    assert main.follow_path(path, binary_map=m) is True
    assert math.hypot(flower.p[0] - goal[0] - 0.5, flower.p[1] - goal[1] - 0.5) < main.ARRIVE_RADIUS + 0.5


def test_follow_path_aims_ahead_on_the_path_not_at_the_node_it_just_passed(monkeypatch):
    m = np.full((40, 80), 255, dtype=np.uint8)
    flower = _pursuit_env(monkeypatch, m, (10, 20), vmax=6.0)
    aims = []
    orig = main._move_mouse_safely
    monkeypatch.setattr(main, "_move_mouse_safely", lambda p: (aims.append(p), orig(p))[1])
    assert main.follow_path([(10, 20), (50, 20), (50, 5)], binary_map=m) is True
    # 过了第二个节点之后鼠标朝上(往 (50,5) 去), 不会回头朝 (50,20)
    assert any(a[1] < main.SCREEN_HEIGHT // 2 - 50 for a in aims)
    assert not any(a[0] < main.SCREEN_WIDTH // 2 - 50 for a in aims)     # 全程没往回(左)推
    # 还在第一段上(离拐角远于前瞻)时瞄的是这一段上前方的点, 不是直奔终点(终点在右上, 直奔会往上偏)
    early = aims[:6]
    assert early and all(abs(a[1] - main.SCREEN_HEIGHT // 2) < 25 for a in early)


def test_follow_path_slows_down_near_the_end_only(monkeypatch):
    m = np.full((40, 120), 255, dtype=np.uint8)
    flower = _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0)
    offsets = []
    orig = main._move_mouse_safely
    monkeypatch.setattr(main, "_move_mouse_safely",
                        lambda p: (offsets.append((flower.p[0], abs(p[0] - main.SCREEN_WIDTH // 2))), orig(p))[1])
    assert main.follow_path([(5, 20), (100, 20)], binary_map=m) is True
    far = [o for x, o in offsets if x < 70]
    near = [o for x, o in offsets if x > 94]
    assert min(far) >= 499 * main.mouse_scale() - 1                         # 路上满量, 不做比例增益
    assert near and min(near) < max(far) * 0.6                              # 临近终点才收


def test_follow_path_brakes_before_a_sharp_turn_but_not_on_a_straight_run(monkeypatch):
    # 满量偏移 + 控制延迟 = 速度大时冲过拐角撞墙: 离急转弯不到 PURSUIT_BRAKE_DIST 格才收偏移, 直路满量
    m = np.full((80, 80), 255, dtype=np.uint8)
    flower = _pursuit_env(monkeypatch, m, (5, 60), vmax=6.0)
    log = []
    orig = main._move_mouse_safely

    def spy(p):
        d = math.hypot(p[0] - main.SCREEN_WIDTH // 2, p[1] - main.SCREEN_HEIGHT // 2)
        log.append((flower.p[0], flower.p[1], d))
        orig(p)
    monkeypatch.setattr(main, "_move_mouse_safely", spy)
    # 先沿 y=60 往右走 60 格, 在 (65,60) 直角拐上去走 40 格
    assert main.follow_path([(5, 60), (65, 60), (65, 20)], binary_map=m) is True
    full = 500 * main.mouse_scale()
    straight = [d for x, y, d in log if y > 59 and x < 65 - main.PURSUIT_BRAKE_DIST - 3]
    corner = [d for x, y, d in log if y > 59 and 65 - main.PURSUIT_BRAKE_DIST / 2 < x < 64.5]
    assert straight and min(straight) >= full - 1                         # 直路满量
    assert corner and max(corner) < full * 0.9                            # 拐角前收了
    assert min(d for _, _, d in log if _ > 0) >= main.PURSUIT_BRAKE_FLOOR * full * 0.9   # 收也有下限


def test_follow_path_hands_over_on_stop_when_deadline_and_enemy(monkeypatch):
    m = np.full((40, 120), 255, dtype=np.uint8)
    flower = _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0)
    assert main.follow_path([(5, 20), (100, 20)], binary_map=m, stop_when=lambda p: p[0] > 30) is True
    assert flower.p[0] < 45                                                  # 进区就收手, 没走完
    flower = _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0)
    clock = {"t": 0.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    monkeypatch.setattr(main.time, "sleep", lambda s: clock.update(t=clock["t"] + max(s, 0.1)))
    assert main.follow_path([(5, 20), (100, 20)], binary_map=m, deadline=3.0) == "timeout"

    class W:
        def should_flee(self):
            return True
    flower = _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0)
    assert main.follow_path([(5, 20), (100, 20)], binary_map=m, enemy_watch=W()) == "enemy"


def test_follow_path_calls_a_wall_pinned_player_stuck(monkeypatch):
    m = np.full((40, 120), 255, dtype=np.uint8)
    m[:, 40:44] = 0                                      # 路径穿墙(用错的图规划): 人顶着墙进不去
    flower = _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0)
    clock = {"t": 0.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    monkeypatch.setattr(main.time, "sleep", lambda s: clock.update(t=clock["t"] + max(s, 0.1)))
    assert main.follow_path([(5, 20), (100, 20)], binary_map=None) == "stuck"


def test_follow_path_waits_out_a_brief_position_loss_instead_of_calling_it_stuck(monkeypatch):
    """传送刚落地 / 画布这一瞬没画小地图: 位置读不到几次再回来。原来第一次读不到就返回 stuck, 紧跟着的
    脱困在出生点旁边乱走进了回花园的门(2026-10-11 录像)。"""
    m = np.full((40, 120), 255, dtype=np.uint8)
    flower = _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0)
    clock = {"t": 0.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    monkeypatch.setattr(main.time, "sleep", lambda s: clock.update(t=clock["t"] + max(s, 0.05)))
    real_read = main.get_player_position
    calls = {"n": 0}

    def read(precise=False, **k):
        calls["n"] += 1
        clock["t"] += 0.1
        if calls["n"] <= 5:                    # 前 5 次读不到
            return None
        return real_read(precise)

    monkeypatch.setattr(main, "get_player_position", read)
    assert main.follow_path([(5, 20), (60, 20)], binary_map=m) is True


def test_follow_path_gives_up_when_the_position_never_comes_back(monkeypatch):
    m = np.full((40, 120), 255, dtype=np.uint8)
    _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0)
    clock = {"t": 0.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    monkeypatch.setattr(main.time, "sleep", lambda s: clock.update(t=clock["t"] + max(s, 0.05)))
    monkeypatch.setattr(main, "get_player_position",
                        lambda precise=False, **k: (clock.update(t=clock["t"] + 0.1), None)[1])
    assert main.follow_path([(5, 20), (60, 20)], binary_map=m) == "stuck"
    assert clock["t"] >= main.PURSUIT_POS_LOST_S              # 等满了才放弃, 不是第一次读不到就走


def test_follow_path_reports_death_and_menu(monkeypatch):
    m = np.full((40, 120), 255, dtype=np.uint8)
    _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0)
    monkeypatch.setattr(main, "on_death_screen", lambda: True)
    assert main.follow_path([(5, 20), (100, 20)], binary_map=m) == "in_game_dead"
    _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: True)
    assert main.follow_path([(5, 20), (100, 20)], binary_map=m) == "in_menu"


def test_follow_path_checks_the_death_screen_only_twice_a_second(monkeypatch):
    m = np.full((40, 200), 255, dtype=np.uint8)
    _pursuit_env(monkeypatch, m, (5, 20), vmax=6.0, dt=0.12)
    clock = {"t": 0.0}
    monkeypatch.setattr(main.time, "time", lambda: clock["t"])
    monkeypatch.setattr(main.time, "sleep", lambda s: clock.update(t=clock["t"] + s))
    checks = []
    monkeypatch.setattr(main, "on_death_screen", lambda: checks.append(clock["t"]) or False)
    real_read = main.get_player_position
    monkeypatch.setattr(main, "get_player_position",
                        lambda precise=False, **k: (clock.update(t=clock["t"] + 0.12), real_read(precise))[1])
    assert main.follow_path([(5, 20), (150, 20)], binary_map=m) is True
    gaps = [b - a for a, b in zip(checks, checks[1:])]
    assert len(checks) >= 3 and min(gaps) >= main.PURSUIT_SCREEN_CHECK_S - 1e-9


def test_follow_path_with_a_single_node_is_already_there(monkeypatch):
    m = np.full((20, 20), 255, dtype=np.uint8)
    _pursuit_env(monkeypatch, m, (5, 5))
    assert main.follow_path([(5, 5)], binary_map=m) is True
    assert main.follow_path([], binary_map=m) == "stuck"
    assert main.follow_path(None, binary_map=m) == "stuck"


def test_pathing_uses_the_pursuit_executor_only_on_pursuit_maps(monkeypatch):
    for map_name, want in (("factory", True), ("desert", False), ("anthell", False)):
        monkeypatch.setattr(main.utils, "MAP", map_name)
        assert main._use_pursuit() is want, map_name
    monkeypatch.setattr(main, "PURSUIT_ENABLED", False)
    monkeypatch.setattr(main.utils, "MAP", "factory")
    assert main._use_pursuit() is False                                      # 总开关
