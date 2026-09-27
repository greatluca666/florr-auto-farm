import os
import types

import gui_app
import gui_update
import updater


def test_version_label():
    assert gui_update.version_label("1.2.0") == "版本 v1.2.0"
    assert gui_update.version_label("0.0.0+dev") == "版本 开发版"


def test_describe_available():
    info = types.SimpleNamespace(version="1.2.0")
    assert gui_update.describe_available(info, True) == "发现新版本 v1.2.0"
    assert "git pull" in gui_update.describe_available(info, False)


def test_format_progress():
    mb = 1024 * 1024
    assert gui_update.format_progress(5 * mb, 10 * mb) == "正在下载 5.0 / 10.0 MB (50%)"
    assert gui_update.format_progress(5 * mb, 0) == "正在下载 5.0 MB"


class FakeRoot:
    """root.after(0, fn, *args) 直接同步调用 —— 测试里没有 Tk 主循环."""

    def after(self, _ms, fn, *args):
        fn(*args)


class FakeBanner:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, a))


def _controller(is_busy=lambda: False, is_closing=lambda: False):
    banner, log = FakeBanner(), []
    state = {"stopped": 0, "quit": 0}

    def bump(key):
        state[key] += 1

    c = gui_update.UpdateController(
        FakeRoot(), banner, log=log.append, is_busy=is_busy,
        stop_all=lambda: bump("stopped"), quit_app=lambda: bump("quit"),
        is_closing=is_closing, current="1.0.0")
    return c, banner, log, state


INFO = types.SimpleNamespace(version="1.2.0", notes="n")


def test_available_update_shows_banner_with_button_on_packaged_windows(monkeypatch):
    monkeypatch.setattr(updater, "enabled", lambda: True)
    c, banner, _log, _ = _controller()
    c._on_checked(False, INFO, None)
    assert banner.calls == [("show_available", ("发现新版本 v1.2.0", True))]


def test_manual_check_on_source_run_shows_banner_without_button(monkeypatch):
    monkeypatch.setattr(updater, "enabled", lambda: False)
    c, banner, _log, _ = _controller()
    c._on_checked(True, INFO, None)
    assert banner.calls == [("show_available", ("发现新版本 v1.2.0, 源码运行请用 git pull 更新", False))]


def test_startup_check_on_source_run_only_logs(monkeypatch):
    # 源码运行每次启动都弹横幅太烦: 只写日志, 手动点「检查更新」才显示
    monkeypatch.setattr(updater, "enabled", lambda: False)
    c, banner, log, _ = _controller()
    c._on_checked(False, INFO, None)
    assert banner.calls == []
    assert log == ["发现新版本 v1.2.0, 源码运行请用 git pull 更新\n"]


# ---- 上次更新没完成(update.log 里记着失败 / 超时 / 脚本中途退出) ----

def test_last_problem_is_appended_to_the_update_banner(monkeypatch):
    monkeypatch.setattr(updater, "enabled", lambda: True)
    c, banner, log, _ = _controller()
    c._on_checked(False, INFO, None, problem="文件被占用")
    assert banner.calls == [("show_available",
                             ("发现新版本 v1.2.0 —— 上次更新没完成: 文件被占用", True))]
    assert "上次更新没完成: 文件被占用\n" in log


def test_last_problem_without_new_version_shows_error(monkeypatch):
    monkeypatch.setattr(updater, "enabled", lambda: True)
    c, banner, log, _ = _controller()
    c._on_checked(False, None, None, problem="替换脚本中途退出")
    assert banner.calls == [("show_error", ("上次更新没完成: 替换脚本中途退出",))]
    assert "上次更新没完成: 替换脚本中途退出\n" in log


def test_last_problem_with_check_failure_shows_error(monkeypatch):
    monkeypatch.setattr(updater, "enabled", lambda: True)
    c, banner, log, _ = _controller()
    c._on_checked(False, None, updater.UpdateError("boom"), problem="timeout: x")
    assert banner.calls == [("show_error", ("上次更新没完成: timeout: x",))]
    assert any("boom" in line for line in log)


def _fake_check_env(monkeypatch, tmp_path, problem=None, calls=None, cleanup=None):
    calls = [] if calls is None else calls
    monkeypatch.setattr(updater, "enabled", lambda: True)
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "last_update_problem",
                        lambda inst: calls.append(("problem", inst)) or problem)
    monkeypatch.setattr(updater, "acknowledge_update_problem",
                        lambda inst: calls.append(("ack", inst)))
    monkeypatch.setattr(updater, "cleanup_leftovers",
                        cleanup or (lambda inst: calls.append(("cleanup", inst))))
    monkeypatch.setattr(updater, "check_latest",
                        lambda cur: calls.append(("check", cur)) or INFO)
    return calls


def test_check_worker_reads_problem_then_cleans_up_then_checks(monkeypatch, tmp_path):
    calls = _fake_check_env(monkeypatch, tmp_path, problem="文件被占用")
    c, banner, _log, _ = _controller()
    c._check_worker(False)
    assert calls == [("problem", tmp_path), ("ack", tmp_path), ("cleanup", tmp_path),
                     ("check", "1.0.0")]
    assert banner.calls == [("show_available",
                             ("发现新版本 v1.2.0 —— 上次更新没完成: 文件被占用", True))]


def test_check_worker_without_problem_does_not_acknowledge(monkeypatch, tmp_path):
    calls = _fake_check_env(monkeypatch, tmp_path, problem=None)
    c, _banner, _log, _ = _controller()
    c._check_worker(False)
    assert [name for name, _ in calls] == ["problem", "cleanup", "check"]


def test_check_worker_cleanup_error_does_not_block_the_check(monkeypatch, tmp_path):
    def broken_cleanup(inst):
        raise OSError("disk gone")

    calls = _fake_check_env(monkeypatch, tmp_path, cleanup=broken_cleanup)
    c, banner, _log, _ = _controller()
    c._check_worker(False)
    assert ("check", "1.0.0") in calls
    assert c._checking is False
    assert banner.calls[-1][0] == "show_available"


def test_check_worker_on_source_run_neither_cleans_nor_reads_the_log(monkeypatch, tmp_path):
    calls = _fake_check_env(monkeypatch, tmp_path, problem="x")
    monkeypatch.setattr(updater, "enabled", lambda: False)
    c, _banner, _log, _ = _controller()
    c._check_worker(True)
    assert [name for name, _ in calls] == ["check"]


def test_check_is_refused_while_updating(monkeypatch):
    started = _no_threads(monkeypatch)
    c, _banner, _log, _ = _controller()
    c._updating = True
    c.check()
    assert started == []            # 更新中不检查, 也就不会去清理正在解压的 .update/


def test_gui_app_no_longer_schedules_its_own_cleanup():
    # 清理只在检查线程里做(那时一定不在更新), 启动 15 秒后另起线程清理会和 stage() 撞上
    import inspect
    assert "cleanup_leftovers" not in inspect.getsource(gui_app)


def test_startup_check_failure_only_logs():
    c, banner, log, _ = _controller()
    c._on_checked(False, None, updater.UpdateError("boom"))
    assert banner.calls == []
    assert "boom" in log[0]


def test_manual_check_failure_shows_error():
    c, banner, _log, _ = _controller()
    c._on_checked(True, None, updater.UpdateError("boom"))
    assert banner.calls[0][0] == "show_error"


def test_manual_check_up_to_date_says_so():
    c, banner, _log, _ = _controller()
    c._on_checked(True, None, None)
    assert banner.calls == [("show_message", ("已是最新版本",))]


def _no_threads(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            started.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(gui_update.threading, "Thread", FakeThread)
    return started


def test_start_update_does_nothing_if_user_declines_stopping_the_schedule(monkeypatch):
    monkeypatch.setattr(updater, "enabled", lambda: True)
    monkeypatch.setattr(gui_update.messagebox, "askyesno", lambda *a, **k: False)
    started = _no_threads(monkeypatch)
    c, _banner, _log, state = _controller(is_busy=lambda: True)
    c._info = INFO
    c.start_update()
    assert state["stopped"] == 0
    assert started == []


def test_start_update_stops_the_schedule_then_starts_worker(monkeypatch):
    monkeypatch.setattr(updater, "enabled", lambda: True)
    monkeypatch.setattr(gui_update.messagebox, "askyesno", lambda *a, **k: True)
    started = _no_threads(monkeypatch)
    c, _banner, _log, state = _controller(is_busy=lambda: True)
    c._info = INFO
    c.start_update()
    assert state["stopped"] == 1
    assert started == [(c._update_worker, (INFO,))]


def test_update_worker_success_launches_swap_then_quits(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path)
    calls = []

    def fake_download(info, dest, cb):
        cb(50, 100)
        cb(100, 100)
        dest.write_bytes(b"zip")
        return "abc"

    monkeypatch.setattr(updater, "download", fake_download)
    monkeypatch.setattr(updater, "verify", lambda info, sha: calls.append(("verify", sha)))
    monkeypatch.setattr(updater, "stage",
                        lambda z, inst: calls.append(("stage", z, inst)) or tmp_path / "staged")
    monkeypatch.setattr(updater, "launch_swap",
                        lambda inst, staged, pid: calls.append(("swap", inst, staged, pid)))
    monkeypatch.setattr(updater, "wait_for_swap_start",
                        lambda inst, pid: calls.append(("wait", inst, pid)) or True)
    c, banner, _log, state = _controller()
    c._update_worker(INFO)
    assert calls == [("verify", "abc"),
                     ("stage", tmp_path / updater.DOWNLOAD_NAME, tmp_path),
                     ("swap", tmp_path, tmp_path / "staged", os.getpid()),
                     ("wait", tmp_path, os.getpid())]
    assert state["quit"] == 1
    assert not (tmp_path / updater.DOWNLOAD_NAME).exists()
    assert ("show_progress", ("正在下载 0.0 / 0.0 MB (50%)", 0.5)) in banner.calls


def test_update_worker_stays_open_when_swap_script_never_starts(monkeypatch, tmp_path):
    # 脚本被杀毒软件拦了: 这时退出程序就没人把它重新打开了, 必须留着并报错
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "download",
                        lambda info, dest, cb: dest.write_bytes(b"zip") or "abc")
    monkeypatch.setattr(updater, "verify", lambda info, sha: None)
    monkeypatch.setattr(updater, "stage", lambda z, inst: tmp_path / "staged")
    monkeypatch.setattr(updater, "launch_swap", lambda inst, staged, pid: None)
    monkeypatch.setattr(updater, "wait_for_swap_start", lambda inst, pid: False)
    c, banner, log, state = _controller()
    c._updating = True
    c._update_worker(INFO)
    assert state["quit"] == 0
    assert c._updating is False
    assert banner.calls[-1] == (
        "show_error", ("更新失败: 更新脚本没能启动(可能被杀毒软件拦截了), 请到官网手动下载新版本",))
    assert "杀毒软件" in log[-1]


def test_update_worker_failure_shows_error_and_does_not_quit(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path)

    def bad_download(info, dest, cb):
        raise updater.UpdateError("下载失败 —— x")

    monkeypatch.setattr(updater, "download", bad_download)
    c, banner, log, state = _controller()
    c._updating = True
    c._update_worker(INFO)
    assert state["quit"] == 0
    assert banner.calls[-1][0] == "show_error"
    assert c._updating is False
    assert "下载失败" in log[-1]


def test_stop_for_update_stops_a_running_schedule():
    calls = []
    fake = types.SimpleNamespace(_sched_running=True,
                                 _on_start_stop=lambda: calls.append("sched"),
                                 _stop_worker_sync=lambda: calls.append("worker"))
    gui_app.App._stop_for_update(fake)
    assert calls == ["sched"]


def test_stop_for_update_stops_a_lone_worker():
    calls = []
    fake = types.SimpleNamespace(_sched_running=False,
                                 _on_start_stop=lambda: calls.append("sched"),
                                 _stop_worker_sync=lambda: calls.append("worker"))
    gui_app.App._stop_for_update(fake)
    assert calls == ["worker"]


# ---- 关窗口时的收尾: 正在关的话后台线程不再往主线程发回调 ----

def test_post_does_nothing_when_closing():
    c, _banner, _log, _state = _controller(is_closing=lambda: True)
    calls = []
    c._post(calls.append, "x")
    assert calls == []


def test_post_swallows_runtime_error_from_destroyed_root():
    class BrokenRoot:
        def after(self, *_a, **_k):
            raise RuntimeError("main thread is not in main loop")

    c = gui_update.UpdateController(
        BrokenRoot(), FakeBanner(), log=lambda *_: None, is_busy=lambda: False,
        stop_all=lambda: None, quit_app=lambda: None, current="1.0.0")
    c._post(lambda: None)  # 不应该抛出


def test_update_worker_success_clears_updating_before_quitting(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "download",
                        lambda info, dest, cb: dest.write_bytes(b"zip") or "abc")
    monkeypatch.setattr(updater, "verify", lambda info, sha: None)
    monkeypatch.setattr(updater, "stage", lambda z, inst: tmp_path / "staged")
    monkeypatch.setattr(updater, "launch_swap", lambda inst, staged, pid: None)
    monkeypatch.setattr(updater, "wait_for_swap_start", lambda inst, pid: True)

    captured = {}
    c = gui_update.UpdateController(
        FakeRoot(), FakeBanner(), log=lambda *_: None, is_busy=lambda: False,
        stop_all=lambda: None,
        quit_app=lambda: captured.setdefault("updating_at_quit", c._updating),
        current="1.0.0")
    c._updating = True
    c._update_worker(INFO)
    assert captured["updating_at_quit"] is False


def test_on_closing_prompts_and_cancels_if_user_declines(monkeypatch):
    monkeypatch.setattr(gui_app.messagebox, "askyesno", lambda *a, **k: False)
    calls = []
    fake = types.SimpleNamespace(
        _updates=types.SimpleNamespace(is_updating=lambda: True),
        _closing=False, _tick_job=None,
        after_cancel=lambda *_: calls.append("after_cancel"),
        _stop_worker_sync=lambda: calls.append("worker"),
        destroy=lambda: calls.append("destroy"))
    gui_app.App.on_closing(fake)
    assert calls == []
    assert fake._closing is False


def test_on_closing_proceeds_if_user_confirms(monkeypatch):
    monkeypatch.setattr(gui_app.messagebox, "askyesno", lambda *a, **k: True)
    calls = []
    fake = types.SimpleNamespace(
        _updates=types.SimpleNamespace(is_updating=lambda: True),
        _closing=False, _tick_job=None,
        after_cancel=lambda *_: calls.append("after_cancel"),
        _stop_worker_sync=lambda: calls.append("worker"),
        destroy=lambda: calls.append("destroy"))
    gui_app.App.on_closing(fake)
    assert calls == ["worker", "destroy"]
    assert fake._closing is True


def test_on_closing_skips_prompt_when_not_updating(monkeypatch):
    asked = []
    monkeypatch.setattr(gui_app.messagebox, "askyesno",
                        lambda *a, **k: asked.append(1) or True)
    calls = []
    fake = types.SimpleNamespace(
        _updates=types.SimpleNamespace(is_updating=lambda: False),
        _closing=False, _tick_job=None,
        after_cancel=lambda *_: calls.append("after_cancel"),
        _stop_worker_sync=lambda: calls.append("worker"),
        destroy=lambda: calls.append("destroy"))
    gui_app.App.on_closing(fake)
    assert asked == []
    assert calls == ["worker", "destroy"]
    assert fake._closing is True
