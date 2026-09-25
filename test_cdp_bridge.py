import hashlib
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import URLError

import pytest

import cdp_bridge
from cdp_bridge import (
    find_florr_tab, eval_js, capture_screenshot,
    _find_windows_chrome, _quit_all_chrome, _launch_chrome_process, _poll_for_florr_tab,
)

# 不能依赖"这台机器9222端口有没有真Chrome在监听"这种外部状态 —— 之前这样写过,
# 开发机上真跑起了CDP-enabled Chrome后这些测试就全炸了(find_florr_tab意外真的
# 找到标签页, eval_js意外真的没报错). 全部mock掉urllib请求, 让测试结果只取决于
# 代码逻辑本身, 不取决于谁凑巧在跑什么.


def test_find_florr_tab_returns_none_when_cdp_port_not_listening():
    with patch("cdp_bridge.urllib.request.urlopen", side_effect=URLError("refused")):
        assert find_florr_tab() is None


def test_find_florr_tab_returns_none_when_no_florr_tab_open():
    other_tabs = json.dumps([{"url": "https://example.com/", "id": "1"}]).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = other_tabs
    with patch("cdp_bridge.urllib.request.urlopen", return_value=mock_resp):
        assert find_florr_tab() is None


def test_find_florr_tab_finds_florr_among_other_tabs():
    tabs = json.dumps([
        {"url": "https://example.com/", "id": "1"},
        {"url": "https://florr.io/", "id": "2", "webSocketDebuggerUrl": "ws://x"},
    ]).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = tabs
    with patch("cdp_bridge.urllib.request.urlopen", return_value=mock_resp):
        tab = find_florr_tab()
        assert tab is not None
        assert tab["id"] == "2"


def test_eval_js_raises_clear_error_when_no_tab_found():
    with patch("cdp_bridge.find_florr_tab", return_value=None):
        with pytest.raises(RuntimeError, match="florr.io标签页"):
            eval_js("1 + 1")


def test_capture_screenshot_raises_clear_error_when_no_tab_found():
    with patch("cdp_bridge.find_florr_tab", return_value=None):
        with pytest.raises(RuntimeError, match="florr.io标签页"):
            capture_screenshot()


def test_eval_js_raises_clear_error_when_wrong_websocket_package_installed():
    # 实机在Windows上复现过: 装的是PyPI上不相关的"websocket"包(不是
    # "websocket-client"), 两个包都叫`websocket`这个模块名, 装错了就没有
    # create_connection这个函数. 用一个没有create_connection属性的假模块
    # 模拟这种情况, 不用真的卸载/装错包来测.
    fake_tab = {"webSocketDebuggerUrl": "ws://x", "url": "https://florr.io/"}
    fake_websocket_module = MagicMock(spec=[])  # spec=[]: 除了标准MagicMock属性,
                                                 # 不允许访问任何没显式声明的属性,
                                                 # hasattr(..., 'create_connection')
                                                 # 会正确判False, 不会被MagicMock
                                                 # 的自动属性生成骗过去.
    with patch("cdp_bridge.find_florr_tab", return_value=fake_tab), \
         patch("cdp_bridge.websocket", fake_websocket_module):
        with pytest.raises(RuntimeError, match="websocket-client"):
            eval_js("1 + 1")


def test_find_windows_chrome_returns_none_when_no_candidate_exists():
    with patch("cdp_bridge.os.path.isfile", return_value=False):
        assert _find_windows_chrome() is None


def test_find_windows_chrome_returns_first_existing_candidate():
    existing = cdp_bridge._WINDOWS_CHROME_CANDIDATES[1]
    with patch("cdp_bridge.os.path.isfile", side_effect=lambda p: p == existing):
        assert _find_windows_chrome() == existing


def test_quit_all_chrome_calls_taskkill_on_windows(monkeypatch):
    monkeypatch.setattr(cdp_bridge.sys, "platform", "win32")
    with patch("cdp_bridge.subprocess.run") as mock_run, \
         patch("cdp_bridge.time.sleep"):
        _quit_all_chrome()
    mock_run.assert_called_once_with(
        ["taskkill", "/IM", "chrome.exe", "/F"], capture_output=True
    )


def test_quit_all_chrome_calls_osascript_on_macos(monkeypatch):
    monkeypatch.setattr(cdp_bridge.sys, "platform", "darwin")
    with patch("cdp_bridge.subprocess.run") as mock_run, \
         patch("cdp_bridge.time.sleep"):
        _quit_all_chrome()
    mock_run.assert_called_once_with(
        ["osascript", "-e", 'quit app "Google Chrome"'], capture_output=True
    )


def test_launch_chrome_process_raises_when_windows_chrome_not_found(monkeypatch):
    monkeypatch.setattr(cdp_bridge.sys, "platform", "win32")
    with patch("cdp_bridge._find_windows_chrome", return_value=None):
        with pytest.raises(RuntimeError, match="没找到Chrome"):
            _launch_chrome_process()


def test_launch_chrome_process_windows_passes_correct_args(monkeypatch):
    monkeypatch.setattr(cdp_bridge.sys, "platform", "win32")
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    with patch("cdp_bridge._find_windows_chrome", return_value=chrome_path), \
         patch("cdp_bridge.subprocess.Popen") as mock_popen:
        _launch_chrome_process()
    called_args = mock_popen.call_args[0][0]
    assert called_args[0] == chrome_path
    assert f"--remote-debugging-port={cdp_bridge.CDP_PORT}" in called_args
    assert "--remote-allow-origins=*" in called_args
    assert any(a.startswith("--user-data-dir=") for a in called_args)
    assert "--no-first-run" in called_args
    assert "--no-default-browser-check" in called_args


def test_launch_chrome_process_macos_uses_open_dash_a(monkeypatch):
    monkeypatch.setattr(cdp_bridge.sys, "platform", "darwin")
    with patch("cdp_bridge.subprocess.Popen") as mock_popen:
        _launch_chrome_process()
    called_args = mock_popen.call_args[0][0]
    assert called_args[:4] == ["open", "-a", "Google Chrome", "--args"]
    assert f"--remote-debugging-port={cdp_bridge.CDP_PORT}" in called_args


def test_poll_for_florr_tab_returns_tab_immediately_when_found():
    fake_tab = {"url": "https://florr.io/", "id": "1"}
    with patch("cdp_bridge.find_florr_tab", return_value=fake_tab), \
         patch("cdp_bridge.time.sleep") as mock_sleep:
        result = _poll_for_florr_tab(timeout=5)
    assert result == fake_tab
    mock_sleep.assert_not_called()


def test_poll_for_florr_tab_returns_none_after_timeout():
    with patch("cdp_bridge.find_florr_tab", return_value=None), \
         patch("cdp_bridge.time.sleep"):
        result = _poll_for_florr_tab(timeout=0.05, interval=0.01)
    assert result is None


def test_poll_for_florr_tab_retries_until_found():
    with patch("cdp_bridge.find_florr_tab", side_effect=[None, None, {"url": "https://florr.io/"}]), \
         patch("cdp_bridge.time.sleep") as mock_sleep:
        result = _poll_for_florr_tab(timeout=5, interval=1)
    assert result is not None
    assert mock_sleep.call_count == 2


def test_launch_dedicated_chrome_happy_path_calls_everything_once():
    with patch("builtins.input", return_value="") as mock_input, \
         patch("cdp_bridge.is_dedicated_chrome_ready", return_value=False), \
         patch("cdp_bridge._quit_all_chrome") as mock_quit, \
         patch("cdp_bridge._launch_chrome_process") as mock_launch, \
         patch("cdp_bridge._poll_for_florr_tab", return_value={"url": "https://florr.io/"}) as mock_poll:
        cdp_bridge.launch_dedicated_chrome()
    mock_quit.assert_called_once()
    mock_launch.assert_called_once()
    mock_poll.assert_called_once_with(15, 1.0)
    assert mock_input.call_count == 2  # 一次关闭确认 + 一次"已打开florr.io"确认


def test_launch_dedicated_chrome_retries_when_tab_not_found_yet():
    with patch("builtins.input", return_value="") as mock_input, \
         patch("cdp_bridge.is_dedicated_chrome_ready", return_value=False), \
         patch("cdp_bridge._quit_all_chrome"), \
         patch("cdp_bridge._launch_chrome_process"), \
         patch("cdp_bridge._poll_for_florr_tab", side_effect=[None, {"url": "https://florr.io/"}]) as mock_poll:
        cdp_bridge.launch_dedicated_chrome()
    assert mock_poll.call_count == 2
    assert mock_input.call_count == 3  # 关闭确认 + 2次"已打开florr.io"确认(第一次没找到, 重试一次)


def test_is_cdp_port_reachable_is_public():
    """gui_chrome_flow 也要靠它区分"Chrome 没起来"和"florr.io 没开", 所以它是
    公有 API —— 别再留一个下划线版本让调用方去够私有名字."""
    assert callable(cdp_bridge.is_cdp_port_reachable)
    assert not hasattr(cdp_bridge, "_is_cdp_port_reachable")


def test_is_cdp_port_reachable_returns_false_when_port_not_listening():
    with patch("cdp_bridge.urllib.request.urlopen", side_effect=URLError("refused")):
        assert cdp_bridge.is_cdp_port_reachable() is False


def test_is_cdp_port_reachable_returns_true_when_port_listening():
    mock_resp = MagicMock()
    with patch("cdp_bridge.urllib.request.urlopen", return_value=mock_resp):
        assert cdp_bridge.is_cdp_port_reachable() is True


def test_launch_dedicated_chrome_blames_user_when_port_reachable_but_no_florr_tab(capsys):
    with patch("builtins.input", return_value="") as mock_input, \
         patch("cdp_bridge.is_dedicated_chrome_ready", return_value=False), \
         patch("cdp_bridge._quit_all_chrome"), \
         patch("cdp_bridge._launch_chrome_process"), \
         patch("cdp_bridge._poll_for_florr_tab", side_effect=[None, {"url": "https://florr.io/"}]), \
         patch("cdp_bridge.is_cdp_port_reachable", return_value=True):
        cdp_bridge.launch_dedicated_chrome()
    assert mock_input.call_count == 3
    out = capsys.readouterr().out
    assert "还没检测到florr.io标签页" in out
    assert "CDP端口连不上" not in out


def test_launch_dedicated_chrome_blames_chrome_when_port_unreachable(capsys):
    with patch("builtins.input", return_value="") as mock_input, \
         patch("cdp_bridge.is_dedicated_chrome_ready", return_value=False), \
         patch("cdp_bridge._quit_all_chrome"), \
         patch("cdp_bridge._launch_chrome_process"), \
         patch("cdp_bridge._poll_for_florr_tab", side_effect=[None, {"url": "https://florr.io/"}]), \
         patch("cdp_bridge.is_cdp_port_reachable", return_value=False):
        cdp_bridge.launch_dedicated_chrome()
    assert mock_input.call_count == 3
    out = capsys.readouterr().out
    assert "CDP端口连不上" in out
    assert "还没检测到florr.io标签页" not in out


def test_is_dedicated_chrome_ready_returns_true_when_a_cdp_command_round_trips():
    with patch("cdp_bridge.eval_js", return_value={"result": {"result": {"value": 1}}}):
        assert cdp_bridge.is_dedicated_chrome_ready() is True


def test_is_dedicated_chrome_ready_returns_false_when_no_florr_tab():
    with patch("cdp_bridge.eval_js", side_effect=RuntimeError("找不到florr.io标签页(CDP)")):
        assert cdp_bridge.is_dedicated_chrome_ready() is False


def test_is_dedicated_chrome_ready_returns_false_when_websocket_handshake_rejected():
    # 端口通、标签页也在列表里, 但启动参数漏了--remote-allow-origins=* —— 只有
    # 真的发一条CDP命令时才会露出403. 这种"看起来好了其实不能用"的Chrome必须
    # 判False, 不然下面launch_dedicated_chrome()会跳过重启, 用户永远修不好.
    with patch("cdp_bridge.eval_js", side_effect=Exception("Handshake status 403 Forbidden")):
        assert cdp_bridge.is_dedicated_chrome_ready() is False


def test_launch_dedicated_chrome_leaves_running_chrome_alone_when_already_ready(capsys):
    # 用户报的bug: 已经有一个满足条件的专用Chrome在跑时(main.py上次跑崩了重开、
    # 或者同时开了两份), 照样无条件杀掉所有Chrome重开 —— 用户白迁移一次账号.
    with patch("cdp_bridge.is_dedicated_chrome_ready", return_value=True), \
         patch("builtins.input") as mock_input, \
         patch("cdp_bridge._quit_all_chrome") as mock_quit, \
         patch("cdp_bridge._launch_chrome_process") as mock_launch, \
         patch("cdp_bridge._poll_for_florr_tab") as mock_poll:
        cdp_bridge.launch_dedicated_chrome()
    mock_input.assert_not_called()   # 不该弹"即将关闭所有Chrome窗口"那句确认
    mock_quit.assert_not_called()
    mock_launch.assert_not_called()
    mock_poll.assert_not_called()    # 已经确认标签页能用了, 不用再等
    assert "跳过" in capsys.readouterr().out


def test_is_dedicated_chrome_ready_true_when_eval_succeeds():
    with patch("cdp_bridge.eval_js", return_value={"result": {}}):
        assert cdp_bridge.is_dedicated_chrome_ready() is True


def test_is_dedicated_chrome_ready_false_when_eval_raises():
    with patch("cdp_bridge.eval_js", side_effect=RuntimeError("403")):
        assert cdp_bridge.is_dedicated_chrome_ready() is False


def test_quit_and_launch_chrome_quits_then_launches():
    calls = []
    with patch("cdp_bridge._quit_all_chrome", side_effect=lambda: calls.append("quit")), \
         patch("cdp_bridge._launch_chrome_process", side_effect=lambda: calls.append("launch")):
        cdp_bridge.quit_and_launch_chrome()
    assert calls == ["quit", "launch"]


def test_wait_for_florr_tab_returns_tab_when_found():
    tab = {"url": "https://florr.io/", "webSocketDebuggerUrl": "ws://x"}
    with patch("cdp_bridge.find_florr_tab", return_value=tab):
        assert cdp_bridge.wait_for_florr_tab(5) == tab


def test_wait_for_florr_tab_returns_none_on_timeout():
    with patch("cdp_bridge.find_florr_tab", return_value=None):
        assert cdp_bridge.wait_for_florr_tab(0) is None


# ── inject_canvas_hook / drain_canvas_log (canvas 绘制调用解码取代 YOLO+像素) ──
#
# 这几个测试全程 mock 掉 _send_cdp_command, 让结果只取决于 inject/drain 自己的
# 拆包/幂等/reload 分支逻辑, 不碰真 Chrome. fake_send 按 expression 里的子串分流,
# 子串跟 cdp_bridge.py 里实际写的 JS 字符串必须对得上(改一处要一起改).

_HOOK_SRC = (Path(cdp_bridge.__file__).parent / "canvas_hook.js").read_text(encoding="utf-8")
_HOOK_VER = hashlib.sha256(_HOOK_SRC.encode()).hexdigest()[:16]


def test_canvas_hook_js_is_read_as_utf8():
    """canvas_hook.js 里有非 ASCII 字符(一个 em-dash + 中文注释). 必须显式按
    UTF-8 读 —— Windows 上 Path.read_text() 不带 encoding 会用 locale 码
    (cp936/gbk) 解, 撞上 UTF-8 字节序列直接 UnicodeDecodeError(ValueError 子类),
    被 scan_enemies 吞掉后索敌永远返回 [] 且一声不吭(同 commit d4a7843 的坑).
    这里钉死"显式 encoding='utf-8' 能读通且内容确实含非 ASCII"这个意图."""
    text = cdp_bridge._CANVAS_HOOK_PATH.read_text(encoding="utf-8")
    assert any(ord(ch) > 127 for ch in text)


def _eval_result(value):
    """伪造 Runtime.evaluate + returnByValue 的双层 result 响应."""
    return {"id": 1, "result": {"result": {"value": value}}}


def test_drain_canvas_log_unwraps_and_clears():
    sent = []

    def fake_send(method, params=None, timeout=5):
        sent.append((method, params))
        return _eval_result([{"frame": 1, "op": "fill"}])

    with patch("cdp_bridge._send_cdp_command", side_effect=fake_send):
        out = cdp_bridge.drain_canvas_log()
    assert out == [{"frame": 1, "op": "fill"}]
    assert sent[0][0] == "Runtime.evaluate"
    assert "__canvasLog" in sent[0][1]["expression"]


def test_drain_canvas_log_returns_empty_on_bad_shape():
    with patch("cdp_bridge._send_cdp_command", return_value={"id": 1, "result": {}}):
        assert cdp_bridge.drain_canvas_log() == []


def test_inject_canvas_hook_noop_when_matching_version_installed():
    calls = []

    def fake_send(method, params=None, timeout=5):
        calls.append((method, (params or {}).get("expression", "")))
        expr = (params or {}).get("expression", "")
        if "__canvasHookInstalled" in expr and "Version" not in expr:
            return _eval_result(True)                      # already installed
        if "__canvasHookInstalledVersion" in expr:
            return _eval_result(_HOOK_VER)                 # same version
        return _eval_result(None)

    with patch("cdp_bridge._send_cdp_command", side_effect=fake_send):
        cdp_bridge.inject_canvas_hook()
    methods = [m for m, _ in calls]
    # matching version installed -> must NOT reload and NOT raise
    assert "Page.reload" not in methods
    # ...and genuinely idempotent: only the two version-probe reads go out, the hook
    # source is never re-eval'd (no expression carries the assignment + src).
    assert methods == ["Runtime.evaluate", "Runtime.evaluate"]
    assert not any("__canvasHookInstalledVersion = " in e for _, e in calls)


def test_inject_canvas_hook_reloads_and_raises_on_stale_version(monkeypatch):
    monkeypatch.setattr(cdp_bridge, "_last_reload_ts", 0.0)
    calls = []

    def fake_send(method, params=None, timeout=5):
        calls.append(method)
        expr = (params or {}).get("expression", "")
        if "__canvasHookInstalled" in expr and "Version" not in expr:
            return _eval_result(True)
        if "__canvasHookInstalledVersion" in expr:
            return _eval_result("deadbeefdeadbeef")        # different (non-None) version
        return _eval_result(None)

    with patch("cdp_bridge._send_cdp_command", side_effect=fake_send):
        with pytest.raises(RuntimeError, match="版本不一致|旧版"):
            cdp_bridge.inject_canvas_hook()
    assert "Page.reload" in calls
    # stale path must NOT re-eval the hook source before reloading — it can't hot-swap.
    assert "Runtime.evaluate" in calls and calls.count("Runtime.evaluate") == 2


def test_inject_canvas_hook_reloads_and_raises_when_eval_did_not_take(monkeypatch):
    # fresh page (not installed), eval the hook, then drains stay empty -> reload + raise
    monkeypatch.setattr(cdp_bridge, "_last_reload_ts", 0.0)
    calls = []

    def fake_send(method, params=None, timeout=5):
        calls.append(method)
        expr = (params or {}).get("expression", "")
        if "__canvasHookInstalled" in expr and "Version" not in expr:
            return _eval_result(False)                     # not installed yet
        if "__canvasHookInstalledVersion" in expr:
            return _eval_result(None)
        if "__canvasLog" in expr:
            return _eval_result([])                        # drains never grow
        return _eval_result(None)

    monkeypatch.setattr(cdp_bridge.time, "sleep", lambda *a: None)
    with patch("cdp_bridge._send_cdp_command", side_effect=fake_send):
        with pytest.raises(RuntimeError, match="没生效|帧号没推进"):
            cdp_bridge.inject_canvas_hook()
    assert "Page.reload" in calls


def test_inject_canvas_hook_reloads_and_raises_when_frame_number_stuck(monkeypatch):
    # patch landed and records DO flow, but every record is tagged frame 0 (florr bound
    # its own pre-hook requestAnimationFrame, __canvasFrame never advances). A non-empty
    # drain is not enough — the check requires >=2 distinct frame values.
    monkeypatch.setattr(cdp_bridge, "_last_reload_ts", 0.0)
    calls = []

    def fake_send(method, params=None, timeout=5):
        calls.append(method)
        expr = (params or {}).get("expression", "")
        if "__canvasHookInstalled" in expr and "Version" not in expr:
            return _eval_result(False)
        if "__canvasHookInstalledVersion" in expr:
            return _eval_result(None)
        if "__canvasLog" in expr:
            return _eval_result([{"frame": 0, "op": "fill"}, {"frame": 0, "op": "stroke"}])
        return _eval_result(None)

    monkeypatch.setattr(cdp_bridge.time, "sleep", lambda *a: None)
    with patch("cdp_bridge._send_cdp_command", side_effect=fake_send):
        with pytest.raises(RuntimeError, match="帧号没推进"):
            cdp_bridge.inject_canvas_hook()
    assert "Page.reload" in calls


def test_inject_canvas_hook_throttles_reload_within_min_interval(monkeypatch):
    # inject_canvas_hook is called ~8x/second; two failing calls inside
    # _RELOAD_MIN_INTERVAL must issue only ONE Page.reload (else the user's game
    # reloads 8x/s forever).
    monkeypatch.setattr(cdp_bridge, "_last_reload_ts", 0.0)
    clock = [10_000.0]
    monkeypatch.setattr(cdp_bridge.time, "sleep", lambda *a: None)
    monkeypatch.setattr(cdp_bridge.time, "time", lambda: clock[0])
    calls = []

    def fake_send(method, params=None, timeout=5):
        calls.append(method)
        expr = (params or {}).get("expression", "")
        if "__canvasHookInstalled" in expr and "Version" not in expr:
            return _eval_result(False)
        if "__canvasHookInstalledVersion" in expr:
            return _eval_result(None)
        if "__canvasLog" in expr:
            return _eval_result([])                        # empty -> <2 distinct frames
        return _eval_result(None)

    with patch("cdp_bridge._send_cdp_command", side_effect=fake_send):
        with pytest.raises(RuntimeError):
            cdp_bridge.inject_canvas_hook()
        clock[0] += 5.0                                    # still < _RELOAD_MIN_INTERVAL (30s)
        with pytest.raises(RuntimeError):
            cdp_bridge.inject_canvas_hook()
    assert calls.count("Page.reload") == 1

    # once the interval elapses, the next failing call reloads again
    clock[0] += cdp_bridge._RELOAD_MIN_INTERVAL + 1.0
    with patch("cdp_bridge._send_cdp_command", side_effect=fake_send):
        with pytest.raises(RuntimeError):
            cdp_bridge.inject_canvas_hook()
    assert calls.count("Page.reload") == 2


class TestLaunchChromeForProfile:
    def test_windows_args_include_profile_and_url(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.sys, "platform", "win32")
        monkeypatch.setattr(cdp_bridge, "_quit_all_chrome", lambda: None)
        with patch("cdp_bridge._find_windows_chrome", return_value=r"C:\chrome.exe"), \
             patch("cdp_bridge.subprocess.Popen") as mock_popen:
            cdp_bridge.launch_chrome_for_profile(r"D:\profiles\小号2",
                                                open_url="https://florr.io", fullscreen=True)
        argv = mock_popen.call_args[0][0]
        assert argv[0] == r"C:\chrome.exe"
        assert r"--user-data-dir=D:\profiles\小号2" in argv
        assert "--start-fullscreen" in argv
        assert "--remote-debugging-port=9222" in argv
        assert "--remote-allow-origins=*" in argv
        assert "--hide-crash-restore-bubble" in argv
        assert "--disable-session-crashed-bubble" in argv
        assert argv[-1] == "https://florr.io"

    def test_fullscreen_false_omits_flag(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.sys, "platform", "win32")
        monkeypatch.setattr(cdp_bridge, "_quit_all_chrome", lambda: None)
        with patch("cdp_bridge._find_windows_chrome", return_value=r"C:\chrome.exe"), \
             patch("cdp_bridge.subprocess.Popen") as mock_popen:
            cdp_bridge.launch_chrome_for_profile(r"D:\p", fullscreen=False)
        assert "--start-fullscreen" not in mock_popen.call_args[0][0]

    def test_macos_uses_open_dash_a_with_args(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.sys, "platform", "darwin")
        monkeypatch.setattr(cdp_bridge, "_quit_all_chrome", lambda: None)
        with patch("cdp_bridge.subprocess.Popen") as mock_popen:
            cdp_bridge.launch_chrome_for_profile("/tmp/p", open_url="https://florr.io")
        argv = mock_popen.call_args[0][0]
        assert argv[:4] == ["open", "-a", "Google Chrome", "--args"]
        assert "--user-data-dir=/tmp/p" in argv
        assert argv[-1] == "https://florr.io"

    def test_windows_missing_chrome_raises(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.sys, "platform", "win32")
        monkeypatch.setattr(cdp_bridge, "_quit_all_chrome", lambda: None)
        with patch("cdp_bridge._find_windows_chrome", return_value=None):
            with pytest.raises(RuntimeError):
                cdp_bridge.launch_chrome_for_profile(r"D:\p")

    def test_default_profile_dir_is_under_chrome_profiles(self):
        assert cdp_bridge._CHROME_PROFILE_DIR.endswith(
            __import__("os").path.join("chrome-profiles", "默认"))


class TestLinuxChrome:
    """Linux(无头服务器 + Xvfb)分支. 跟 Windows 一样要按路径找可执行文件 ——
    macOS 那条 `open -a` 按应用名找的路子 Linux 没有."""

    def test_find_linux_chrome_returns_first_name_on_path(self):
        found = {"google-chrome-stable": "/usr/bin/google-chrome-stable"}
        with patch("cdp_bridge.shutil.which", side_effect=found.get):
            assert cdp_bridge._find_linux_chrome() == "/usr/bin/google-chrome-stable"

    def test_find_linux_chrome_prefers_google_chrome_over_chromium(self):
        found = {"google-chrome": "/usr/bin/google-chrome",
                 "chromium": "/usr/bin/chromium"}
        with patch("cdp_bridge.shutil.which", side_effect=found.get):
            assert cdp_bridge._find_linux_chrome() == "/usr/bin/google-chrome"

    def test_find_linux_chrome_returns_none_when_nothing_installed(self):
        with patch("cdp_bridge.shutil.which", return_value=None):
            assert cdp_bridge._find_linux_chrome() is None

    def test_quit_all_chrome_uses_pkill_on_linux(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.sys, "platform", "linux")
        with patch("cdp_bridge.subprocess.run") as mock_run, \
             patch("cdp_bridge.time.sleep"):
            _quit_all_chrome()
        mock_run.assert_called_once_with(
            ["pkill", "^(chrome|chromium)$"], capture_output=True
        )

    def test_quit_all_chrome_never_matches_full_cmdline(self, monkeypatch):
        """pkill -f 会拿整条命令行去配 —— `python main.py --launch-chrome` 里带着
        'chrome' 这个词, 用 -f 就会把我们自己杀掉. 必须只配进程名."""
        monkeypatch.setattr(cdp_bridge.sys, "platform", "linux")
        with patch("cdp_bridge.subprocess.run") as mock_run, \
             patch("cdp_bridge.time.sleep"):
            _quit_all_chrome()
        assert "-f" not in mock_run.call_args[0][0]

    def test_launch_chrome_process_raises_when_linux_chrome_not_found(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.sys, "platform", "linux")
        with patch("cdp_bridge._find_linux_chrome", return_value=None):
            with pytest.raises(RuntimeError, match="没找到Chrome"):
                _launch_chrome_process()

    def test_launch_chrome_process_linux_passes_cdp_args(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.sys, "platform", "linux")
        with patch("cdp_bridge._find_linux_chrome", return_value="/usr/bin/google-chrome"), \
             patch("cdp_bridge.subprocess.Popen") as mock_popen:
            _launch_chrome_process()
        argv = mock_popen.call_args[0][0]
        assert argv[0] == "/usr/bin/google-chrome"
        assert f"--remote-debugging-port={cdp_bridge.CDP_PORT}" in argv
        assert "--remote-allow-origins=*" in argv
        assert any(a.startswith("--user-data-dir=") for a in argv)

    def test_linux_flags_include_disable_gpu_and_window_size(self, monkeypatch):
        """Xvfb 里没有 GPU, 也没有窗口管理器 —— 显式给窗口尺寸, 不然窗口大小
        不确定, utils.py 那套照 1920x1080 标的坐标全错位."""
        monkeypatch.setattr(cdp_bridge.os, "geteuid", lambda: 1000)
        flags = cdp_bridge._linux_extra_flags()
        assert "--disable-gpu" in flags
        assert "--window-size=1920,1080" in flags

    def test_linux_flags_omit_no_sandbox_for_normal_user(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.os, "geteuid", lambda: 1000)
        assert "--no-sandbox" not in cdp_bridge._linux_extra_flags()

    def test_linux_flags_add_no_sandbox_only_as_root(self, monkeypatch):
        """root 跑 Chrome 时沙箱起不来, 不给这个 flag 会直接退出. 非 root 时不给 ——
        无脑常开等于白白关掉渲染器沙箱."""
        monkeypatch.setattr(cdp_bridge.os, "geteuid", lambda: 0)
        assert "--no-sandbox" in cdp_bridge._linux_extra_flags()

    def test_launch_chrome_for_profile_linux_args(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.sys, "platform", "linux")
        monkeypatch.setattr(cdp_bridge, "_quit_all_chrome", lambda: None)
        monkeypatch.setattr(cdp_bridge.os, "geteuid", lambda: 1000)
        with patch("cdp_bridge._find_linux_chrome", return_value="/usr/bin/google-chrome"), \
             patch("cdp_bridge.subprocess.Popen") as mock_popen:
            cdp_bridge.launch_chrome_for_profile(
                "/srv/florr/chrome-profiles/默认",
                open_url="https://florr.io", fullscreen=True)
        argv = mock_popen.call_args[0][0]
        assert argv[0] == "/usr/bin/google-chrome"
        assert "--user-data-dir=/srv/florr/chrome-profiles/默认" in argv
        assert "--start-fullscreen" in argv
        assert "--disable-gpu" in argv
        assert argv[-1] == "https://florr.io"

    def test_launch_chrome_for_profile_linux_missing_chrome_raises(self, monkeypatch):
        monkeypatch.setattr(cdp_bridge.sys, "platform", "linux")
        monkeypatch.setattr(cdp_bridge, "_quit_all_chrome", lambda: None)
        with patch("cdp_bridge._find_linux_chrome", return_value=None):
            with pytest.raises(RuntimeError, match="没找到 Chrome"):
                cdp_bridge.launch_chrome_for_profile("/srv/p")
