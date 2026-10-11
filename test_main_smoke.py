def test_main_module_imports_and_exposes_enemy_config():
    import main
    assert hasattr(main, "auto_farming")
    assert hasattr(main, "ENEMY_SCAN_INTERVAL")
    assert hasattr(main, "AVOID_TRIGGER_PX")
    assert hasattr(main, "CAUTIOUS_HOLD_PX")
    assert hasattr(main, "run_worker")
    assert hasattr(main, "_apply_worker_config")
    assert hasattr(main, "_maybe_scan_enemies")


# ── RL 战斗模式接线 ──────────────────────────────────────────────────────────

def test_rl_mode_does_not_force_the_inversions_off_for_the_whole_round():
    """寻路途中正是靠反转攻击键开着才能边走边打 —— 脚本自己从不按攻击键。整轮关掉
    等于让花朵裸奔穿过怪群。这两个键只在策略真正接管的那一段里关。"""
    import main
    w = main._apply_worker_config({"active": {"combat": "rl", "invert_attack": True,
                                              "invert_defense": True}})
    assert w["combat"] == "rl"
    assert w["invert_attack"] is True and w["invert_defense"] is True


def test_rules_mode_leaves_the_inversions_to_the_time_block():
    import main
    w = main._apply_worker_config({"active": {"combat": "rules", "invert_attack": True,
                                              "invert_defense": True}})
    assert w["invert_attack"] is True and w["invert_defense"] is True


def test_an_unknown_combat_value_falls_back_to_rules():
    """拼错一个词不该让这个时块整个作废, 更不该悄悄跑成 RL。"""
    import main
    w = main._apply_worker_config({"active": {"combat": "RL 模式", "invert_attack": True}})
    assert w["combat"] == "rules" and w["invert_attack"] is True


def test_auto_farming_hands_the_round_to_the_policy(monkeypatch):
    import main
    seen = {}

    def fake(area, duration, session, *, restore_toggles, route=None, swaps=(None, None)):
        seen["area"], seen["duration"], seen["session"] = area, duration, session
        seen["restore"] = restore_toggles
        return True

    monkeypatch.setattr(main, "_rl_farming", fake)
    sentinel = object()
    assert main.auto_farming([(51, 56), (9, 8)], 30, combat="rl", rl_session=sentinel,
                             restore_toggles=(True, False)) is True
    assert seen["restore"] == (True, False)      # 时块配的值要一路传到还原那一步
    # 区域必须是归一化之后的 —— 两种模式看到的区域得一样
    assert seen["area"] == [(9, 8), (51, 56)]
    assert seen["duration"] == 30 and seen["session"] is sentinel


def test_rl_round_failure_does_not_crash_the_worker(monkeypatch):
    """缺 sb3 / 没有训好的策略 / CDP 断了 —— 这一局算没刷满就行, 上层照常走它的
    短局和换服逻辑, 不能把 worker 带崩。"""
    import main
    class Boom:
        def run(self, deps, *, duration):
            raise RuntimeError("没有训好的策略")

        def teardown(self):
            self.torn = True

    # overlay 是 run_worker() 里才创建的全局(main.py 里其他地方也都这么用) ——
    # 单测里没跑过 worker, 得自己补一个。
    monkeypatch.setattr(main, "overlay", type("O", (), {"update": lambda s, **kw: None})(),
                        raising=False)
    b = Boom()
    assert main._rl_farming([(0, 0), (10, 10)], 5, b) is False
    assert b.torn is True


def test_rl_session_is_reused_across_rounds(monkeypatch):
    import main
    import rl_farm

    made = []
    monkeypatch.setattr(rl_farm, "FarmSession",
                        lambda **kw: made.append(kw) or type("S", (), {"teardown": lambda s: None})())
    main._RL_SESSION = main._RL_SESSION_KEY = None
    a = main._get_rl_session("runs/desert", False)
    b = main._get_rl_session("runs/desert", False)
    assert a is b and len(made) == 1
    main._get_rl_session("runs/other", False)     # 换了模型 -> 重建
    assert len(made) == 2
    main._RL_SESSION = main._RL_SESSION_KEY = None


def test_the_rl_session_sees_the_real_loadout_epoch(monkeypatch):
    """环境靠它判断"花瓣栏回到基准了没" —— 不传的话只有死亡会让 R 的奇偶归零,
    主循环按的预设和重进游戏它都看不见。"""
    import main
    import rl_farm

    made = []
    monkeypatch.setattr(rl_farm, "FarmSession",
                        lambda **kw: made.append(kw) or type("S", (), {"teardown": lambda s: None})())
    main._RL_SESSION = main._RL_SESSION_KEY = None
    main._get_rl_session("runs/desert", True)
    assert made[0]["loadout_epoch"] is main.loadout_swap.baseline_epoch
    main._RL_SESSION = main._RL_SESSION_KEY = None


def test_the_policy_segment_turns_the_inversions_off_then_restores_them(monkeypatch):
    """策略要自己控制攻击和防御这两维 —— 反转开着的话按不按都一样, 那两维是死的。
    但一出这一段就得立刻还原, 下一段路要靠它边走边打。"""
    import main
    import florr_settings

    wrote = []
    monkeypatch.setattr(florr_settings, "ensure_flag",
                        lambda ev, addr, want: wrote.append((addr, want)) or ("changed", ""))
    restored = []
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda a, d: restored.append((a, d)) or {})
    monkeypatch.setattr(main, "overlay", type("O", (), {"update": lambda s, **kw: None})(),
                        raising=False)

    class Session:
        def run(self, deps, *, duration):
            return "timeout", {"steps": 1, "resets": 0}

    main._rl_farming([(0, 0), (9, 9)], 1, Session(), restore_toggles=(True, False))
    assert wrote == [(florr_settings.INVERT_ATTACK_ADDR, 0),
                     (florr_settings.INVERT_DEFENSE_ADDR, 0)]
    assert restored == [(True, False)]


def test_the_inversions_are_restored_even_when_the_round_blows_up(monkeypatch):
    """出错那条路不还原的话, 接下来整段寻路花朵一下都不打 —— 而且没人会发现,
    只会看到"怎么路上死得这么频繁"。"""
    import main
    import florr_settings

    monkeypatch.setattr(florr_settings, "ensure_flag", lambda ev, addr, want: ("changed", ""))
    restored = []
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda a, d: restored.append((a, d)) or {})
    monkeypatch.setattr(main, "overlay", type("O", (), {"update": lambda s, **kw: None})(),
                        raising=False)

    class Boom:
        def run(self, deps, *, duration):
            raise RuntimeError("CDP 断了")

        def teardown(self):
            pass

    assert main._rl_farming([(0, 0), (9, 9)], 1, Boom(), restore_toggles=(True, True)) is False
    assert restored == [(True, True)]


def test_rl_falls_back_to_rules_after_repeated_failures(monkeypatch):
    """实机(2026-09-24): 存档路径配错, 每轮 2 秒失败一次, 连着转了十几轮 —— 寻路、
    进游戏、报错、重来, 一直烧时间却一只怪没刷。这类错重试多少次都一样, 而退回规则
    战斗至少还在刷怪。"""
    import main

    monkeypatch.setattr(main, "overlay", type("O", (), {"update": lambda s, **kw: None})(),
                        raising=False)
    monkeypatch.setattr(main, "_reassert_florr_toggles", lambda a, d: {})
    import florr_settings
    monkeypatch.setattr(florr_settings, "ensure_flag", lambda ev, addr, want: ("changed", ""))
    monkeypatch.setattr(main, "_RL_CONSECUTIVE_FAILURES", 0, raising=False)

    class Boom:
        def run(self, deps, *, duration):
            raise RuntimeError("没有存档")

        def teardown(self):
            pass

    for _ in range(main.RL_FAILURE_FALLBACK):
        main._rl_farming([(0, 0), (9, 9)], 1, Boom(), restore_toggles=(True, False))

    # 攒够了 -> auto_farming 不再走 RL 分支(走了就会再调一次 _rl_farming)
    called = []
    monkeypatch.setattr(main, "_rl_farming", lambda *a, **k: called.append(1) or False)
    monkeypatch.setattr(main, "load_binary_map", lambda: [[255]])
    monkeypatch.setattr(main, "on_death_screen", lambda: True)   # 立刻退出那个 while
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    main.auto_farming([(0, 0), (9, 9)], 1, combat="rl", rl_session=Boom())
    assert called == []
    main._RL_CONSECUTIVE_FAILURES = 0


def test_the_round_summary_reports_the_r_press_rate(monkeypatch, capsys):
    """每局结尾报按 R 占多少拍 —— 判断"练没练会少按"就看这个数往下走没有。"""
    import main
    import florr_settings

    monkeypatch.setattr(main, "overlay", type("O", (), {"update": lambda s, **kw: None})(),
                        raising=False)
    monkeypatch.setattr(main, "_reassert_florr_toggles", lambda a, d: {})
    monkeypatch.setattr(florr_settings, "ensure_flag", lambda ev, addr, want: ("changed", ""))

    class Ok:
        def run(self, deps, *, duration):
            return "timeout", {"steps": 200, "resets": 0, "swaps": 10}

    main._rl_farming([(0, 0), (9, 9)], 1, Ok(), restore_toggles=(True, False))
    assert "按 R: 10 次 / 200 步 (5.0%)" in capsys.readouterr().out


def test_a_successful_rl_round_clears_the_failure_streak(monkeypatch):
    import main
    import florr_settings

    monkeypatch.setattr(main, "overlay", type("O", (), {"update": lambda s, **kw: None})(),
                        raising=False)
    monkeypatch.setattr(main, "_reassert_florr_toggles", lambda a, d: {})
    monkeypatch.setattr(florr_settings, "ensure_flag", lambda ev, addr, want: ("changed", ""))
    main._RL_CONSECUTIVE_FAILURES = 2

    class Ok:
        def run(self, deps, *, duration):
            return "timeout", {"steps": 5, "resets": 0}

    assert main._rl_farming([(0, 0), (9, 9)], 1, Ok(), restore_toggles=(True, False)) is True
    assert main._RL_CONSECUTIVE_FAILURES == 0


# ── 退出时把键松开 ───────────────────────────────────────────────────────────

def test_reset_keyboard_releases_shift():
    """实机(2026-09-24): GUI 点"停止"之后整台机器一直按着 shift, 打字全变大写。
    RL 用 shift 当防御键, 而这里原来只松 space/wasd。"""
    import main
    import pyautogui

    released = []
    orig_up, orig_keyup = pyautogui.keyUp, main.keyup
    pyautogui.keyUp = released.append
    main.keyup = released.append
    try:
        main.reset_keyboard()
    finally:
        pyautogui.keyUp, main.keyup = orig_up, orig_keyup
    assert "shift" in released and "space" in released
    assert {"w", "a", "s", "d"} <= set(released)


def test_shutdown_cleanup_hooks_atexit_and_the_signals():
    import main
    import signal

    registered, signals = [], []
    main.install_shutdown_cleanup(register=registered.append,
                                  on_signal=lambda sig, h: signals.append(sig),
                                  stdin_eof_exit=False)
    assert registered == [main._release_everything]
    assert signal.SIGTERM in signals and signal.SIGINT in signals


def test_the_stdin_watcher_only_runs_when_the_gui_asks():
    """GUI 停 worker 的第一步就是关 stdin。但无条件监听的话,
    `nohup python main.py < /dev/null`(Ubuntu 无头部署)会在启动瞬间读到 EOF 就退出。"""
    import main

    assert main.install_shutdown_cleanup(register=lambda f: None,
                                         on_signal=lambda s, h: None,
                                         stdin_eof_exit=False) is None
    ran = []
    t = main.install_shutdown_cleanup(register=lambda f: None,
                                      on_signal=lambda s, h: None,
                                      watch_stdin=lambda: ran.append(1),
                                      stdin_eof_exit=True)
    t.join(timeout=2)
    assert ran == [1]


def test_release_is_idempotent():
    """三道闸可能同时触发(GUI 关 stdin + 补一发 SIGTERM)。"""
    import main

    calls = []
    orig = main.reset_keyboard
    main.reset_keyboard = lambda: calls.append(1)
    main._cleanup_done = False
    try:
        main._release_everything()
        main._release_everything()
    finally:
        main.reset_keyboard = orig
        main._cleanup_done = False
    assert calls == [1]


def test_release_never_raises():
    """收尾路径上再抛异常, 连退出都退不干净。"""
    import main

    orig = main.reset_keyboard
    main.reset_keyboard = lambda: (_ for _ in ()).throw(RuntimeError("显示器没了"))
    main._cleanup_done = False
    try:
        main._release_everything()
    finally:
        main.reset_keyboard = orig
        main._cleanup_done = False


def test_the_gui_tells_the_worker_that_closing_stdin_means_quit():
    import inspect

    import gui_app

    src = inspect.getsource(gui_app.App._spawn_worker)
    assert "FLORR_WORKER_STDIN_EOF_EXIT" in src


# ── 就地复活 ─────────────────────────────────────────────────────────────────

def test_revive_is_not_offered_on_multi_stage_routes():
    """蚁穴那种(先进花园再踩洞口)复活要跑整条进场路线, 是主循环的活。这里不重复实现
    一遍, 那种图上退回原样: 死亡 = 这一局结束。"""
    import main
    import map_routes

    back = lambda pos, **kw: True
    assert main._make_revive([(0, 0), (9, 9)], map_routes.route_for("desert"), back)
    assert main._make_revive([(0, 0), (9, 9)], map_routes.route_for("anthell"), back) is None


def test_revive_walks_back_when_it_lands_outside_the_area(monkeypatch):
    import main
    import map_routes

    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "get_player_position", lambda: (99, 99))
    paths = []
    revive = main._make_revive([(0, 0), (9, 9)], map_routes.route_for("desert"),
                               lambda pos, **kw: paths.append(pos) or True)
    assert revive() is True and paths == [(99, 99)]     # 回区用的是同一份 path_back


def test_revive_does_not_walk_when_it_is_already_inside(monkeypatch):
    """复活点常常就在刷怪区里 —— 白跑一趟寻路要好几秒, 而那几秒策略是停的。"""
    import main
    import map_routes

    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "get_player_position", lambda: (5, 5))
    revive = main._make_revive([(0, 0), (9, 9)], map_routes.route_for("desert"),
                               lambda pos: pytest.fail("已经在区里了还去寻路"))
    assert revive() is True


def _swap_spy(monkeypatch):
    """录下 press_swap 收到的 cfg 顺序。"""
    import main

    pressed = []
    monkeypatch.setattr(main.loadout_swap, "press_swap", lambda cfg: pressed.append(cfg))
    return pressed


_ENTER = {"enabled": True, "mod": "k", "digit": "2"}    # 跑路装
_REACH = {"enabled": True, "mod": "k", "digit": "1"}    # 输出装


def test_revive_tells_path_back_that_the_loadout_was_reset(monkeypatch):
    """死了重进 florr 会把 loadout 重置 —— 跟主循环 entered_game 那道 gate 同一个
    前提。不告诉 path_back 的话, 回区的路要是短过 SWAP_WALK_UNITS 它就不换装,
    花朵会穿着重置后的默认装开打。用户实机指出(2026-09-24)。"""
    import main
    import map_routes

    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "get_player_position", lambda: (99, 99))

    seen = {}
    revive = main._make_revive([(0, 0), (9, 9)], map_routes.route_for("desert"),
                               lambda pos, **kw: seen.update(kw) or True,
                               swaps=(_ENTER, _REACH))
    assert revive() is True
    assert seen == {"reset_loadout": True}


def test_revive_inside_the_area_goes_straight_to_the_damage_loadout(monkeypatch):
    """没有路要跑, 但 florr 刚把 loadout 重置了 —— 输出装还是得按回来。"""
    import main
    import map_routes

    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "get_player_position", lambda: (5, 5))
    pressed = _swap_spy(monkeypatch)

    revive = main._make_revive([(0, 0), (9, 9)], map_routes.route_for("desert"),
                               lambda pos, **kw: pytest.fail("已经在区里了还去寻路"),
                               swaps=(_ENTER, _REACH))
    assert revive() is True
    assert pressed == [_REACH]


def _capture_path_back(monkeypatch, area, *, pathing):
    """跑一趟 _rl_farming, 把它交给仲裁器的那个 path_back 抓出来。"""
    import main

    monkeypatch.setattr(main, "overlay",
                        type("O", (), {"update": lambda s, **kw: None})(), raising=False)
    monkeypatch.setattr(main, "_reassert_florr_toggles", lambda a, d: {})
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ev, addr, want: ("unchanged", ""))
    monkeypatch.setattr(main, "load_binary_map", lambda: None)
    monkeypatch.setattr(main, "lazy_theta_pathing", pathing)

    grabbed = {}

    class Session:
        def run(self, deps, *, duration):
            grabbed["back"] = deps["path_back"]
            return "timeout", {"steps": 1, "resets": 0}

    main._rl_farming(area, 1, Session(), restore_toggles=(True, False),
                     swaps=(_ENTER, _REACH))
    return grabbed["back"]


def test_a_long_walk_back_puts_the_travel_loadout_on(monkeypatch):
    """实机(2026-09-24)第 3 轮: 同一段走廊, 进场 7 段一次没卡; 打了一段怪之后原路
    回去 16 段**段段**"原地打转14次", 每次挪 5~8 格、210 秒才走完。差别只有身上的装
    —— 进场穿跑路装, 回区穿的是到区时换上的输出装, 慢到每 tick 挪不满
    move_to_position 的 progress_epsilon。"""
    import main

    pressed = _swap_spy(monkeypatch)
    back = _capture_path_back(monkeypatch, [(0, 0), (99, 99)],
                              pathing=lambda *a, **k: True)
    assert back((200, 200)) is True
    assert pressed == [_ENTER, _REACH]      # 出门换跑路装, 回来换输出装


def test_a_short_drift_back_is_not_worth_two_chords(monkeypatch):
    """出区飘一两格一局里有好多次 —— 两次和弦 + florr 的换装动画比省下来的路还贵。"""
    import main

    pressed = _swap_spy(monkeypatch)
    back = _capture_path_back(monkeypatch, [(0, 0), (99, 99)],
                              pathing=lambda *a, **k: True)
    assert back((100, 50)) is True          # 夹回边内点只有几格
    assert pressed == []


def test_the_damage_loadout_comes_back_even_when_the_walk_fails(monkeypatch):
    """换出去没换回来的话, 主循环下一轮的 swap gate 在"人没死、续命一轮"时是假的,
    不会替我们纠正 —— 花朵会穿着跑路装刷一整轮。"""
    import main

    pressed = _swap_spy(monkeypatch)
    back = _capture_path_back(monkeypatch, [(0, 0), (99, 99)],
                              pathing=lambda *a, **k: False)
    assert back((200, 200)) is False
    assert pressed == [_ENTER, _REACH]


def _capture_rl_deps(monkeypatch, area, events, *, pathing=None, pos=(200, 200)):
    """跑一趟 _rl_farming, 抓出交给仲裁器的 deps; 反转开关的每次写入和寻路/进局都记进 events。"""
    import main
    import map_routes

    monkeypatch.setattr(main, "overlay",
                        type("O", (), {"update": lambda s, **kw: None})(), raising=False)
    monkeypatch.setattr(main, "_reassert_florr_toggles",
                        lambda a, d: events.append(("script", a, d)) or {})
    monkeypatch.setattr(main.florr_settings, "ensure_flag",
                        lambda ev, addr, want: events.append(("flag", addr, want)) or ("changed", ""))
    monkeypatch.setattr(main, "load_binary_map", lambda: None)
    monkeypatch.setattr(main, "lazy_theta_pathing",
                        pathing or (lambda *a, **k: events.append("walk") or True))
    monkeypatch.setattr(main.loadout_swap, "press_swap", lambda cfg: None)
    monkeypatch.setattr(main, "on_death_screen", lambda: False)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    monkeypatch.setattr(main, "get_player_position", lambda: pos)

    grabbed = {}

    class Session:
        def run(self, deps, *, duration):
            grabbed.update(deps)
            events.clear()                  # 只看这一局**里面**的写入
            return "timeout", {"steps": 1, "resets": 0}

    main._rl_farming(area, 1, Session(), restore_toggles=(True, False),
                     route=map_routes.route_for("desert"), swaps=(_ENTER, _REACH))
    return grabbed


def _policy_flags():
    import florr_settings
    return [("flag", florr_settings.INVERT_ATTACK_ADDR, 0),
            ("flag", florr_settings.INVERT_DEFENSE_ADDR, 0)]


def test_walking_back_to_the_area_attacks_on_the_way(monkeypatch):
    """出区拉回是**脚本**在走, 脚本从不按攻击键, 全靠反转攻击键开着才能边走边打。
    原来整段 RL 局里反转一直是关的 —— 拉回那段花朵一下都不打, 穿过怪群挨揍。
    走完再关回去, 把攻击/防御交还给策略。"""
    events = []
    deps = _capture_rl_deps(monkeypatch, [(0, 0), (99, 99)], events)
    events.clear()
    assert deps["path_back"]((200, 200)) is True
    assert events == [("script", True, False), "walk"] + _policy_flags()


def test_the_policy_gets_its_controls_back_even_when_the_walk_fails(monkeypatch):
    events = []
    deps = _capture_rl_deps(monkeypatch, [(0, 0), (99, 99)], events,
                            pathing=lambda *a, **k: events.append("walk") or False)
    events.clear()
    assert deps["path_back"]((200, 200)) is False
    assert events[-2:] == _policy_flags()


def test_revive_turns_the_inversions_back_off_after_reentering(monkeypatch):
    """florr 每次从菜单进局都会用**账号里存的值**把这两个开关盖回去。就地复活要点
    继续+开始 = 重新进局, 之后没人再写一次的话, 账号里要是开着反转攻击, 策略这一局剩下
    的时间攻击维全是死的(按不按都在打) —— 而且不报错。"""
    import main

    events = []
    deps = _capture_rl_deps(monkeypatch, [(0, 0), (99, 99)], events, pos=(5, 5))
    starts = iter([True, False, False])
    monkeypatch.setattr(main, "on_start_screen", lambda: next(starts))
    monkeypatch.setattr(main, "click_start_game", lambda: events.append("enter"))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    events.clear()
    assert deps["revive"]() is True
    assert events.index("enter") < len(events) - 2
    assert events[-2:] == _policy_flags()


def test_auto_farming_hands_the_time_block_swaps_to_the_rl_leg(monkeypatch):
    """时块配的那一对和弦得真的到得了 _make_revive —— 中间断一节就是静默失效。"""
    import main

    seen = {}
    monkeypatch.setattr(main, "_rl_farming",
                        lambda *a, **k: seen.update(k) or True)
    main.auto_farming([(0, 0), (9, 9)], 1, combat="rl", rl_session=object(),
                      rl_route=object(), rl_swaps=(_ENTER, _REACH))
    assert seen["swaps"] == (_ENTER, _REACH)


def test_the_worker_passes_the_configured_swaps_into_rl_farming():
    """run_worker -> auto_farming 那一跳同样不能漏(整条链只有一处调用点)。"""
    import inspect

    import main

    src = inspect.getsource(main.run_worker)
    assert 'rl_swaps=(w["enter_game_swap"], w["reach_area_swap"])' in src


def test_revive_gives_up_when_the_screens_will_not_go_away(monkeypatch):
    """点不进去就交回上层 —— 它有完整的重进逻辑(游客登录、生态区、多次重试)。"""
    import main
    import map_routes

    monkeypatch.setattr(main, "on_death_screen", lambda: True)
    monkeypatch.setattr(main, "click_continue_after_death", lambda: None)
    monkeypatch.setattr(main, "on_start_screen", lambda: False)
    revive = main._make_revive([(0, 0), (9, 9)], map_routes.route_for("desert"),
                               lambda pos, **kw: True)
    assert revive() is False


def test_nearest_inside_clamps_to_the_closest_edge():
    """实机(2026-09-24): 花朵飘到 (20,62) —— 区域下边界 y=61 外**一格** —— 就被拽回
    区域中心 (29,32), 30 格的路走 20 秒, 一局里来回三次。"""
    import main

    area = [(6, 4), (52, 61)]
    assert main._nearest_inside(area, (20, 62), None, inset=2) == (20, 59)
    assert main._nearest_inside(area, (100, 100), None, inset=2) == (50, 59)
    assert main._nearest_inside(area, (20, 30), None, inset=2) == (20, 30)   # 本来就在里面


def test_nearest_inside_refuses_a_wall():
    """刷怪区不一定是实心矩形 —— 夹到边上可能正好是墙, 那就退回配置的目标点。"""
    import main
    import numpy as np

    walls = np.zeros((70, 60), dtype=np.uint8)      # 全是墙
    assert main._nearest_inside([(6, 4), (52, 61)], (20, 62), walls) is None
    inside = main._nearest_inside([(6, 4), (52, 61)], (20, 62), None)
    walls[inside[1]][inside[0]] = 255
    assert main._nearest_inside([(6, 4), (52, 61)], (20, 62), walls) == inside


def test_nearest_inside_target_clears_the_arrive_radius():
    """实机 2026-09-24 的 118 秒空转: inset=2 把目标点放在了 3 格外, 而
    move_to_position 认为 ARRIVE_RADIUS(5) 以内就算"到了" —— 一个 moveTo 都不发就
    返回成功。人没动、还在区外, 上层于是重新规划…… 循环 90 次。

    两条不变式:
      1. 目标必须比 ARRIVE_RADIUS 远, 否则这一趟根本不会挪窝;
      2. move_to_position 是在离目标 ARRIVE_RADIUS 处收手的, 收手点也得还在区内,
         不然人刚被拉回来就又在区外, 下一 tick 立刻再拉一次。
    """
    import math
    import main

    area = [(6, 4), (52, 61)]
    r = main.ARRIVE_RADIUS
    for pos in [(20, 62), (20, 3), (5, 30), (53, 30), (100, 100), (0, 0), (53, 62)]:
        target = main._nearest_inside(area, pos, None)
        assert math.dist(target, pos) > r, f"{pos} -> {target} 落在到达半径里, 不会挪窝"
        for dx, dy in [(r, 0), (-r, 0), (0, r), (0, -r)]:
            stop = (target[0] + dx, target[1] + dy)
            assert main.if_in_area([area], stop), f"{pos} -> {target}, 收手点 {stop} 在区外"


def test_nearest_inside_survives_a_region_narrower_than_the_inset():
    """区域比 inset 还窄时, 按轴退化成取中线 —— 不能夹出一个区外的点。"""
    import main

    tiny = [(0, 0), (4, 4)]
    assert main._nearest_inside(tiny, (10, 10), None) == (2, 2)
    assert main.if_in_area([tiny], main._nearest_inside(tiny, (-9, -9), None))


def test_nearest_inside_does_not_assume_which_corner_is_which():
    """if_in_area 对两个角的顺序不敏感(utils 里专门修过), 这里也不能敏感。"""
    import main

    assert (main._nearest_inside([(52, 61), (6, 4)], (20, 62), None)
            == main._nearest_inside([(6, 4), (52, 61)], (20, 62), None))


def test_the_rl_leg_reports_how_many_times_it_revived(monkeypatch):
    """主循环靠它把就地复活算进换服计数(用户 2026-09-25: 只要死了就算一次)。"""
    import main
    import florr_settings

    monkeypatch.setattr(main, "overlay", type("O", (), {"update": lambda s, **kw: None})(),
                        raising=False)
    monkeypatch.setattr(main, "_reassert_florr_toggles", lambda a, d: {})
    monkeypatch.setattr(florr_settings, "ensure_flag", lambda ev, addr, want: ("changed", ""))

    class Ok:
        def run(self, deps, *, duration):
            return "timeout", {"steps": 5, "resets": 0, "supervisor": {"deaths": 3}}

    main._rl_farming([(0, 0), (9, 9)], 1, Ok(), restore_toggles=(True, False))
    assert main._RL_ROUND_DEATHS == 3
    main._RL_ROUND_DEATHS = 0
