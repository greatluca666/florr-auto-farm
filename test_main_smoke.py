def test_main_module_imports_and_exposes_enemy_config():
    import main
    assert hasattr(main, "auto_farming")
    assert hasattr(main, "ENEMY_SCAN_INTERVAL")
    assert hasattr(main, "AVOID_TRIGGER_PX")
    assert hasattr(main, "CAUTIOUS_HOLD_PX")
    assert hasattr(main, "run_worker")
    assert hasattr(main, "_apply_worker_config")
    assert hasattr(main, "_maybe_scan_enemies")


# ── 退出时把键松开 ───────────────────────────────────────────────────────────

def test_reset_keyboard_releases_shift():
    """实机(2026-09-24): GUI 点"停止"之后整台机器一直按着 shift, 打字全变大写。
    防御键会按住 shift, 而这里原来只松 space/wasd。"""
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
