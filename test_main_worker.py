import inspect
import io
import os

import numpy as np
import pytest

import main
import map_routes


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


@pytest.mark.parametrize("start, goal, vmax", [
    ((117, 102), (23, 89), 3.0),    # 实机出生点 -> 刷怪区, 连穿两条密道
    ((72, 91), (109, 110), 2.0),    # 反着穿回来
])
def test_anthell_routes_through_the_shortcut_tunnels_get_walked(monkeypatch, start, goal, vmax):
    import cv2
    walk = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    _stub_move_env(monkeypatch)
    path = main.lazy_theta_star(walk, start, goal)
    assert path is not None

    def walk_it(binary_map):
        flower = _SlidingFlower(walk, start, vmax)
        monkeypatch.setattr(main, "get_player_position", flower.position)
        monkeypatch.setattr(main, "_move_mouse_safely", lambda p: setattr(flower, "mouse", p))
        monkeypatch.setattr(main, "reset_keyboard", lambda: setattr(flower, "mouse", None))
        return main.execute_path(path, binary_map=binary_map)

    assert walk_it(None) == "stuck"      # 老判据: 卡在密道里
    assert walk_it(walk) is True


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


def test_path_walk_hands_each_hop_the_next_one_and_the_planning_map(monkeypatch):
    hops = []
    monkeypatch.setattr(main, "move_to_position",
                        lambda a, b, **kw: hops.append((b, kw["next_pos"], kw["binary_map"])) or True)
    marker = object()
    assert main.execute_path([(0, 0), (1, 1), (2, 2), (3, 3)], binary_map=marker) is True
    assert hops == [((1, 1), (2, 2), marker), ((2, 2), (3, 3), marker), ((3, 3), None, marker)]

    monkeypatch.setattr(main, "overlay", _StubOverlay(), raising=False)
    monkeypatch.setattr(main.afk_watch, "poll_afk_pause", lambda: False)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "load_binary_map",
                        lambda: np.full((120, 120), 255, dtype=np.uint8))
    pos = {"p": (20, 80)}
    maps = []

    def execute(path, **kw):
        maps.append(kw.get("binary_map"))
        pos["p"] = (20, 40)
        return True

    monkeypatch.setattr(main, "execute_path", execute)
    monkeypatch.setattr(main, "get_player_position", lambda: pos["p"])
    monkeypatch.setattr(main, "lazy_theta_star", lambda m, a, b: [a, b])
    assert main.lazy_theta_pathing((13, 40), [[(2, 5), (44, 73)]]) is True
    assert len(maps) == 1 and maps[0].shape == (120, 120)


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


def test_apply_worker_config_force_disables_enemy_ai_on_unsupported_map(monkeypatch):
    # 海洋没做索敌(MAP_SPECIES["ocean"] 是空集), config 里写 true 也得关掉 ——
    # priority_score() 对表外 slug 会 KeyError.
    monkeypatch.setattr(main, "apply_map", lambda name: None)
    w = main._apply_worker_config(
        {"version": 2, "active": {"map": "ocean", "enemy_ai_enabled": True}})
    assert w["enemy_ai_enabled"] is False
    w2 = main._apply_worker_config(
        {"version": 2, "active": {"map": "desert", "enemy_ai_enabled": True}})
    assert w2["enemy_ai_enabled"] is True


def test_route_blocker_reports_uncalibrated_portal(monkeypatch):
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", None)
    blocker = main._route_blocker(map_routes.route_for("anthell"))
    assert blocker is not None and "ANTHELL_PORTAL" in blocker


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
    assert "ANTHELL_PORTAL" in capsys.readouterr().out   # 明说缺哪个值


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
    """单图路线(沙漠/海洋)一次都不该去问"我现在在哪张图".

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
