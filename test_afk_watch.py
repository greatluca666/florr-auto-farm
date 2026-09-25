import hashlib
import io
import json
import os
import time
import zipfile
from unittest.mock import MagicMock, patch

import afk_watch


def _reset(monkeypatch, log_path):
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(log_path))
    monkeypatch.setattr(afk_watch, "_last_offset", 0)
    monkeypatch.setattr(afk_watch, "_pause_until", 0.0)
    # 默认模拟"之前已经poll过, offset合法为0"这种常规状态(不是模块刚加载的
    # 首次poll) —— 首次poll跳到文件末尾的特殊逻辑单独用测试覆盖, 见下面
    # test_poll_afk_pause_ignores_preexisting_history_on_first_ever_poll.
    monkeypatch.setattr(afk_watch, "_initialized", True)
    monkeypatch.setattr(afk_watch, "_warned_unreadable", False)
    monkeypatch.setattr(afk_watch, "_pending_since", None)


def test_poll_afk_pause_false_when_log_file_missing(tmp_path, monkeypatch):
    _reset(monkeypatch, tmp_path / "does_not_exist.log")
    assert afk_watch.poll_afk_pause() is False


def test_poll_afk_pause_ignores_unrelated_lines(tmp_path, monkeypatch):
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:1> <afk_thread()> EVENT: something else\n"
    )
    _reset(monkeypatch, log_path)
    assert afk_watch.poll_afk_pause() is False


def test_poll_afk_pause_true_after_marker_line_written(tmp_path, monkeypatch):
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:127> <afk_thread()> EVENT: Found AFK window\n"
    )
    _reset(monkeypatch, log_path)
    assert afk_watch.poll_afk_pause() is True


def test_poll_afk_pause_expires_after_pause_seconds(tmp_path, monkeypatch):
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:127> <afk_thread()> EVENT: Found AFK window\n"
    )
    _reset(monkeypatch, log_path)
    monkeypatch.setattr(afk_watch, "PAUSE_SECONDS", 0.05)
    assert afk_watch.poll_afk_pause() is True
    time.sleep(0.1)
    assert afk_watch.poll_afk_pause() is False


def test_poll_afk_pause_escalates_to_full_pause_when_no_misdetect_signal_arrives(tmp_path, monkeypatch):
    # "Found" 单独出现、provisional窗口过了也没等到失败标记 —— 当真弹窗, 补足
    # 到完整 PAUSE_SECONDS(不是卡在短暂的provisional就结束).
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:65> <afk_thread()> EVENT: Found AFK window\n"
    )
    _reset(monkeypatch, log_path)
    monkeypatch.setattr(afk_watch, "PAUSE_SECONDS", 0.3)
    monkeypatch.setattr(afk_watch, "PROVISIONAL_PAUSE_SECONDS", 0.05)
    assert afk_watch.poll_afk_pause() is True    # provisional窗口
    time.sleep(0.1)                              # 过了provisional, 没等到失败标记
    assert afk_watch.poll_afk_pause() is True    # 补足到完整暂停, 还没到点
    time.sleep(0.25)
    assert afk_watch.poll_afk_pause() is False   # 完整暂停也到点了


def test_poll_afk_pause_misdetect_marker_prevents_escalation(tmp_path, monkeypatch):
    # "Found" 之后单独一次poll读到了失败标记(不同批次写入, 模拟florr-auto-afk
    # 隔了几秒才吐出这条) —— provisional窗口一到点就该结束, 不补足.
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:65> <afk_thread()> EVENT: Found AFK window\n"
    )
    _reset(monkeypatch, log_path)
    monkeypatch.setattr(afk_watch, "PAUSE_SECONDS", 0.3)
    monkeypatch.setattr(afk_watch, "PROVISIONAL_PAUSE_SECONDS", 0.05)
    assert afk_watch.poll_afk_pause() is True
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("[2026-08-11 00:00:04] <segment.py:98> <afk_thread()> ERROR: No masks found\n")
    assert afk_watch.poll_afk_pause() is True    # 还在provisional窗口里, 读到了失败标记
    time.sleep(0.1)
    assert afk_watch.poll_afk_pause() is False   # 没有被补足到完整PAUSE_SECONDS


def test_poll_afk_pause_misdetect_in_same_batch_as_found_still_cancels(tmp_path, monkeypatch):
    # 失败标记跟"Found"一起在同一批新行里读到(实测常见: 4~5秒内florr-auto-afk
    # 自己就吐完整套WARNING/ERROR, 我们poll频率够低的话一次就全读到了).
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:65> <afk_thread()> EVENT: Found AFK window\n"
        "[2026-08-11 00:00:04] <segment.py:90> <afk_thread()> WARNING: No start found\n"
        "[2026-08-11 00:00:04] <segment.py:95> <afk_thread()> WARNING: No end found, going for linear prediction\n"
        "[2026-08-11 00:00:04] <segment.py:98> <afk_thread()> ERROR: No masks found\n"
    )
    _reset(monkeypatch, log_path)
    monkeypatch.setattr(afk_watch, "PAUSE_SECONDS", 0.3)
    monkeypatch.setattr(afk_watch, "PROVISIONAL_PAUSE_SECONDS", 0.05)
    assert afk_watch.poll_afk_pause() is True
    time.sleep(0.1)
    assert afk_watch.poll_afk_pause() is False


def test_poll_afk_pause_misdetect_marker_without_pending_found_is_ignored(tmp_path, monkeypatch):
    # 没有对应的"Found"在等确认时, 单独出现的失败标记不该引发任何暂停/报错.
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:98> <afk_thread()> ERROR: No masks found\n"
    )
    _reset(monkeypatch, log_path)
    assert afk_watch.poll_afk_pause() is False


def test_poll_afk_pause_repeated_misdetected_founds_never_accumulate_a_full_pause(tmp_path, monkeypatch):
    # 复现用户实机场景: "Found"反复出现、每次都紧跟着失败标记(连续误检刷屏) ——
    # 不该有任何一次被补足成完整暂停, 寻路不该被拖住.
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:65> <afk_thread()> EVENT: Found AFK window\n"
        "[2026-08-11 00:00:04] <segment.py:98> <afk_thread()> ERROR: No masks found\n"
    )
    _reset(monkeypatch, log_path)
    monkeypatch.setattr(afk_watch, "PAUSE_SECONDS", 0.3)
    monkeypatch.setattr(afk_watch, "PROVISIONAL_PAUSE_SECONDS", 0.05)
    assert afk_watch.poll_afk_pause() is True
    time.sleep(0.1)
    assert afk_watch.poll_afk_pause() is False
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("[2026-08-11 00:00:10] <segment.py:65> <afk_thread()> EVENT: Found AFK window\n"
                "[2026-08-11 00:00:14] <segment.py:98> <afk_thread()> ERROR: No masks found\n")
    assert afk_watch.poll_afk_pause() is True
    time.sleep(0.1)
    assert afk_watch.poll_afk_pause() is False


def test_poll_afk_pause_does_not_retrigger_from_already_read_line(tmp_path, monkeypatch):
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:127> <afk_thread()> EVENT: Found AFK window\n"
    )
    _reset(monkeypatch, log_path)
    assert afk_watch.poll_afk_pause() is True
    # 暂停窗口手动过期(模拟时间流逝), 日志文件没有新行 —— 不该重新触发.
    monkeypatch.setattr(afk_watch, "_pause_until", 0.0)
    assert afk_watch.poll_afk_pause() is False


def test_poll_afk_pause_rereads_from_start_after_truncation(tmp_path, monkeypatch):
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:1> <afk_thread()> EVENT: " + ("x" * 200) + "\n"
    )
    _reset(monkeypatch, log_path)
    assert afk_watch.poll_afk_pause() is False
    # 模拟florr-auto-afk重启/日志轮转: 文件被换成更短的新内容.
    log_path.write_text(
        "[2026-08-11 00:00:01] <segment.py:127> <afk_thread()> EVENT: Found AFK window\n"
    )
    assert afk_watch.poll_afk_pause() is True


def test_poll_afk_pause_false_when_path_is_directory(tmp_path, monkeypatch):
    # 模拟LATEST_LOG_PATH指向目录而非文件(权限错误或误配置).
    # open()会抛IsADirectoryError, 应被捕获且返回False.
    _reset(monkeypatch, tmp_path)
    assert afk_watch.poll_afk_pause() is False


def test_poll_afk_pause_detects_marker_appended_after_a_poll(tmp_path, monkeypatch):
    # 复现真实运行方式: 先poll一次(什么都没有), 文件被外部程序追加内容,
    # 再poll一次才检测到 —— 不是像其它测试那样在第一次poll前就把完整内容写好.
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:1> <afk_thread()> EVENT: something else\n"
    )
    _reset(monkeypatch, log_path)
    assert afk_watch.poll_afk_pause() is False
    with open(log_path, "a") as f:
        f.write(
            "[2026-08-11 00:00:01] <segment.py:127> <afk_thread()> EVENT: Found AFK window\n"
        )
    assert afk_watch.poll_afk_pause() is True


def test_poll_afk_pause_detects_marker_split_across_two_writes(tmp_path, monkeypatch):
    # 模拟florr-auto-afk的write()调用被我们的poll撞到一半: 第一次poll时文件里
    # 只有这一行的前半部分(还没写完, 没有结尾换行符). 没有finding 1的修复,
    # _last_offset会越过这半行, 后半部分写完后也永远拼不回去, 标记就漏检了.
    log_path = tmp_path / "latest.log"
    prefix = "[2026-08-11 00:00:00] <segment.py:127> <afk_thread()> EVENT: Found AFK "
    log_path.write_text(prefix)
    _reset(monkeypatch, log_path)
    assert afk_watch.poll_afk_pause() is False
    with open(log_path, "a") as f:
        f.write("window\n")
    assert afk_watch.poll_afk_pause() is True


def test_poll_afk_pause_ignores_preexisting_history_on_first_ever_poll(tmp_path, monkeypatch):
    # 模拟main.py启动前florr-auto-afk已经跑了一段时间, 日志里躺着一条老早以前
    # 就处理完的标记行. 模块从没poll过时的第一次调用不该把这段历史当成新事件,
    # 得直接跳到文件末尾. 之后真的新写一条, 才应该触发.
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[2026-08-11 00:00:00] <segment.py:127> <afk_thread()> EVENT: Found AFK window\n"
    )
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(log_path))
    monkeypatch.setattr(afk_watch, "_last_offset", 0)
    monkeypatch.setattr(afk_watch, "_pause_until", 0.0)
    monkeypatch.setattr(afk_watch, "_initialized", False)
    monkeypatch.setattr(afk_watch, "_warned_unreadable", False)

    assert afk_watch.poll_afk_pause() is False

    with open(log_path, "a") as f:
        f.write(
            "[2026-08-11 00:00:01] <segment.py:127> <afk_thread()> EVENT: Found AFK window\n"
        )
    assert afk_watch.poll_afk_pause() is True


def test_poll_afk_pause_warns_once_when_log_unreadable(tmp_path, monkeypatch, capsys):
    _reset(monkeypatch, tmp_path / "does_not_exist.log")
    assert afk_watch.poll_afk_pause() is False
    assert afk_watch.poll_afk_pause() is False
    assert afk_watch.poll_afk_pause() is False
    captured = capsys.readouterr()
    assert captured.out.count("⚠️") == 1


def test_latest_log_path_is_computed_from_install_dir():
    assert afk_watch.LATEST_LOG_PATH == os.path.join(afk_watch._INSTALL_DIR, "latest.log")


def test_install_dir_is_our_own_chosen_folder_name():
    # 名字里的-autostart后缀是我们自己加的(release tag和zip名里都没有这个词):
    # 已经装过旧版官方包的机器上_EXE_PATH是存在的, 不换目录名就永远不会重新下载,
    # 而那份exe没有autoStart, 每次启动都白等到超时.
    assert afk_watch._INSTALL_DIR == os.path.join(
        afk_watch._INSTALL_ROOT, "florr-auto-afk-v1.1.1-autostart"
    )


def test_exe_path_uses_the_real_executable_name_not_florr_auto_afk_exe():
    # 实测过release zip内部结构确认的真实文件名 —— 不是"florr-auto-afk.exe"
    # 这种直觉猜测的名字, 写死一个测试防止以后被改错.
    assert afk_watch._EXE_PATH == os.path.join(afk_watch._INSTALL_DIR, "segment.exe")


def test_prompt_download_confirm_returns_true_on_enter(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert afk_watch._prompt_download_confirm() is True


def test_prompt_download_confirm_returns_false_on_n(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert afk_watch._prompt_download_confirm() is False


def test_prompt_download_confirm_returns_false_on_n_case_insensitive_with_whitespace(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "  N  ")
    assert afk_watch._prompt_download_confirm() is False


def test_prompt_download_confirm_prints_url_size_and_destination(monkeypatch):
    # input()的提示文本是作为参数传给input(...)的, 不是print()出来的 —— capsys
    # 抓不到. 换个把prompt参数记下来的假input, 直接断言参数内容.
    captured_prompt = {}

    def fake_input(prompt):
        captured_prompt["text"] = prompt
        return ""

    monkeypatch.setattr("builtins.input", fake_input)
    afk_watch._prompt_download_confirm()
    assert afk_watch._DOWNLOAD_URL in captured_prompt["text"]
    assert afk_watch._INSTALL_DIR in captured_prompt["text"]
    # 体积得跟_DOWNLOAD_URL钉的那个asset对得上(347,277,698字节) —— 换release时
    # 这句会提醒把提示里的数字一起改, 别让用户按旧数字估等待时间.
    assert "350MB" in captured_prompt["text"]


def _patch_install_paths(monkeypatch, tmp_path):
    """把三个安装路径常量都指到tmp_path下 —— _EXE_PATH也要patch, 不然
    _download_and_extract()新增的"解压完exe在不在"检查会去看真实机器上的路径.

    目录名从_INSTALL_DIR_NAME推出来, 不写字面量: 那个常量改名时(比如这次加
    -autostart后缀)下面断言"临时zip已删掉"的路径才不会悄悄指向一个压根不可能
    存在的地方, 变成永远通过的假断言."""
    install_dir = tmp_path / afk_watch._INSTALL_DIR_NAME
    monkeypatch.setattr(afk_watch, "_INSTALL_ROOT", str(tmp_path))
    monkeypatch.setattr(afk_watch, "_INSTALL_DIR", str(install_dir))
    monkeypatch.setattr(afk_watch, "_EXE_PATH", str(install_dir / "segment.exe"))
    return install_dir


def _tmp_zip_path(tmp_path):
    """_download_and_extract()下载时用的临时文件路径(下完就该被删掉)."""
    return tmp_path / f"{afk_watch._INSTALL_DIR_NAME}.zip.download"


def _fake_zip_bytes():
    """跟官方release zip一致的结构: 条目直接在根, 没有顶层目录(它v1.1.1的
    workflow用`Compress-Archive -Path ./dist/segment/*`打的包). 这个fixture
    早先假设有顶层目录, 正好把"解压到_INSTALL_ROOT会把4500个文件铺在main.py
    旁边"这个bug盖住了.

    真zip里的分隔符是反斜杠(Compress-Archive的产物): zipfile在Windows上会把它
    当目录分隔符正确展开(os.sep就是反斜杠), 在POSIX上则会变成一个带反斜杠的平
    文件名. 测试用正斜杠, 断言的是"没有顶层目录"这个真正要紧的性质."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("segment.exe", b"fake exe content")
        zf.writestr("models/afk-det.pt", b"fake model")
    return buf.getvalue()


def _fake_zip_without_exe_bytes():
    """结构对但少了segment.exe —— 上游改了打包布局时该被当成失败, 不能返回True
    让调用方以为装好了."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("models/afk-det.pt", b"fake model")
    return buf.getvalue()


def _fake_response(body):
    """假的urlopen返回值: 支持with语句, 第一次read()给全部内容, 第二次给空
    表示读完."""
    resp = MagicMock()
    resp.headers = {"Content-Length": str(len(body))}
    resp.read.side_effect = [body, b""]
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda self, *a: False
    return resp


def _patch_expected_digest(monkeypatch, payload):
    """把_DOWNLOAD_SHA256改成这份假zip自己的摘要.

    生产常量钉的是真release那347MB asset的摘要, 假zip当然对不上 —— 不patch的话
    下面这些测试全都停在校验那一步, 断言的"解压/结构检查"行为压根跑不到.
    现算而不是抄一个字面量摘要: fixture内容一改就自动跟着走, 也不会有人手抖把
    假摘要粘回生产常量里.
    """
    monkeypatch.setattr(
        afk_watch, "_DOWNLOAD_SHA256", hashlib.sha256(payload).hexdigest()
    )


def test_download_and_extract_success(tmp_path, monkeypatch):
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    payload = _fake_zip_bytes()
    _patch_expected_digest(monkeypatch, payload)

    with patch("afk_watch.urllib.request.urlopen", return_value=_fake_response(payload)):
        result = afk_watch._download_and_extract()

    assert result is True
    assert (install_dir / "segment.exe").read_bytes() == b"fake exe content"
    # 临时zip用完就删, 不该留在目标目录里.
    assert not _tmp_zip_path(tmp_path).exists()


def test_download_and_extract_returns_false_and_cleans_up_on_network_error(tmp_path, monkeypatch):
    _patch_install_paths(monkeypatch, tmp_path)

    with patch("afk_watch.urllib.request.urlopen", side_effect=OSError("network unreachable")):
        result = afk_watch._download_and_extract()

    assert result is False
    assert not _tmp_zip_path(tmp_path).exists()


def test_download_and_extract_returns_false_when_sha256_does_not_match(
    tmp_path, monkeypatch, capsys
):
    # release asset能被持有者用同一个tag覆盖(gh release upload --clobber), 而这个
    # zip里就是我们下一步要Popen的exe —— 摘要对不上必须停在解压之前, 一个文件都
    # 不能落地. 故意不patch _DOWNLOAD_SHA256: 真常量对不上这份假zip, 正是要测的情况.
    _patch_install_paths(monkeypatch, tmp_path)
    payload = _fake_zip_bytes()

    with patch("afk_watch.urllib.request.urlopen", return_value=_fake_response(payload)):
        result = afk_watch._download_and_extract()

    assert result is False
    # 解压目录压根不该被建出来, 临时zip也得清掉.
    assert not (tmp_path / afk_watch._INSTALL_DIR_NAME).exists()
    assert not _tmp_zip_path(tmp_path).exists()
    out = capsys.readouterr().out
    assert "⚠️" in out
    # 两个摘要都要印: 正常原因是release重新构建了而常量没跟着改, 得让人能一眼对比.
    assert afk_watch._DOWNLOAD_SHA256 in out
    assert hashlib.sha256(payload).hexdigest() in out


def test_download_and_extract_accepts_a_digest_pasted_in_certutil_form(tmp_path, monkeypatch):
    # 重新构建之后刷_DOWNLOAD_SHA256的人是在Windows上, 最可能直接从
    # `certutil -hashfile ... SHA256`的输出粘过来 —— 那玩意儿是大写、还按每两字节
    # 插空格. 这种写法不能被当成"校验失败": 那条提示跟"asset真被人换了"长得一模一样,
    # 会让人往完全错的方向查.
    _patch_install_paths(monkeypatch, tmp_path)
    payload = _fake_zip_bytes()
    real = hashlib.sha256(payload).hexdigest()
    certutil_style = " ".join(real[i:i + 2] for i in range(0, len(real), 2)).upper()
    monkeypatch.setattr(afk_watch, "_DOWNLOAD_SHA256", f"  {certutil_style}\n")

    with patch("afk_watch.urllib.request.urlopen", return_value=_fake_response(payload)):
        result = afk_watch._download_and_extract()

    assert result is True


def test_download_and_extract_returns_false_on_corrupt_zip(tmp_path, monkeypatch):
    _patch_install_paths(monkeypatch, tmp_path)
    payload = b"this is not a zip file"
    _patch_expected_digest(monkeypatch, payload)

    with patch(
        "afk_watch.urllib.request.urlopen",
        return_value=_fake_response(payload),
    ):
        result = afk_watch._download_and_extract()

    assert result is False
    assert not _tmp_zip_path(tmp_path).exists()


def test_download_and_extract_returns_false_when_zip_has_no_exe(tmp_path, monkeypatch):
    _patch_install_paths(monkeypatch, tmp_path)
    payload = _fake_zip_without_exe_bytes()
    _patch_expected_digest(monkeypatch, payload)

    with patch(
        "afk_watch.urllib.request.urlopen",
        return_value=_fake_response(payload),
    ):
        result = afk_watch._download_and_extract()
    assert result is False


def test_ensure_florr_auto_afk_running_skips_entirely_on_non_windows(monkeypatch):
    monkeypatch.setattr(afk_watch.sys, "platform", "darwin")
    with patch("builtins.input") as mock_input, \
         patch("afk_watch.subprocess.Popen") as mock_popen, \
         patch("afk_watch.urllib.request.urlopen") as mock_urlopen:
        afk_watch.ensure_florr_auto_afk_running()
    mock_input.assert_not_called()
    mock_popen.assert_not_called()
    mock_urlopen.assert_not_called()


def test_ensure_florr_auto_afk_running_opens_directly_when_already_installed(monkeypatch):
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    with patch("afk_watch.os.path.isfile", return_value=True), \
         patch("afk_watch._is_florr_auto_afk_running", return_value=False), \
         patch("afk_watch._write_afk_config"), \
         patch("afk_watch._wait_for_segment_started", return_value=True), \
         patch("builtins.input") as mock_input, \
         patch("afk_watch.subprocess.Popen") as mock_popen:
        afk_watch.ensure_florr_auto_afk_running()
    mock_input.assert_not_called()  # 已经装了, 不该问下载
    # cwd必须是它自己的安装目录 —— segment.exe内部读的是相对路径'./config.json',
    # 它写的latest.log也落在CWD; 继承我们的CWD会让它一启动就FileNotFoundError.
    mock_popen.assert_called_once_with([afk_watch._EXE_PATH], cwd=afk_watch._INSTALL_DIR)


def test_ensure_florr_auto_afk_running_skips_when_user_declines_download(monkeypatch):
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    with patch("afk_watch.os.path.isfile", return_value=False), \
         patch("afk_watch._is_florr_auto_afk_running", return_value=False), \
         patch("afk_watch._prompt_download_confirm", return_value=False), \
         patch("afk_watch._download_and_extract") as mock_download, \
         patch("afk_watch.subprocess.Popen") as mock_popen:
        afk_watch.ensure_florr_auto_afk_running()
    mock_download.assert_not_called()
    mock_popen.assert_not_called()


def test_ensure_florr_auto_afk_running_downloads_then_opens_when_confirmed(monkeypatch):
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    with patch("afk_watch.os.path.isfile", return_value=False), \
         patch("afk_watch._is_florr_auto_afk_running", return_value=False), \
         patch("afk_watch._prompt_download_confirm", return_value=True), \
         patch("afk_watch._download_and_extract", return_value=True) as mock_download, \
         patch("afk_watch._write_afk_config"), \
         patch("afk_watch._wait_for_segment_started", return_value=True), \
         patch("afk_watch.subprocess.Popen") as mock_popen:
        afk_watch.ensure_florr_auto_afk_running()
    mock_download.assert_called_once()
    mock_popen.assert_called_once_with([afk_watch._EXE_PATH], cwd=afk_watch._INSTALL_DIR)


def test_ensure_florr_auto_afk_running_does_not_open_when_download_fails(monkeypatch):
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    with patch("afk_watch.os.path.isfile", return_value=False), \
         patch("afk_watch._is_florr_auto_afk_running", return_value=False), \
         patch("afk_watch._prompt_download_confirm", return_value=True), \
         patch("afk_watch._download_and_extract", return_value=False), \
         patch("afk_watch.subprocess.Popen") as mock_popen:
        afk_watch.ensure_florr_auto_afk_running()
    mock_popen.assert_not_called()


def test_ensure_florr_auto_afk_running_does_not_crash_when_popen_raises(monkeypatch):
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    with patch("afk_watch.os.path.isfile", return_value=True), \
         patch("afk_watch._is_florr_auto_afk_running", return_value=False), \
         patch("afk_watch._write_afk_config"), \
         patch("afk_watch._wait_for_segment_started") as mock_wait, \
         patch("afk_watch.subprocess.Popen", side_effect=OSError("no permission")):
        afk_watch.ensure_florr_auto_afk_running()  # 不该抛异常
    mock_wait.assert_not_called()  # 压根没启动成功, 没什么可等的


def _fake_tasklist_result(stdout):
    result = MagicMock()
    result.stdout = stdout
    return result


def test_is_florr_auto_afk_running_true_when_tasklist_lists_the_exe():
    listed = (
        "segment.exe                   9128 Console                    1    271,204 K\n"
    )
    with patch("afk_watch.subprocess.run", return_value=_fake_tasklist_result(listed)):
        assert afk_watch._is_florr_auto_afk_running() is True


def test_is_florr_auto_afk_running_false_when_tasklist_reports_no_tasks():
    # tasklist找不到匹配进程时退出码照样是0, 只是往stdout印这句 —— 不能靠
    # returncode判断, 只能看输出里有没有那个进程名.
    no_tasks = 'INFO: No tasks are running which match the specified criteria.\n'
    with patch("afk_watch.subprocess.run", return_value=_fake_tasklist_result(no_tasks)):
        assert afk_watch._is_florr_auto_afk_running() is False


def test_is_florr_auto_afk_running_false_when_tasklist_unavailable():
    with patch("afk_watch.subprocess.run", side_effect=OSError("tasklist not found")):
        assert afk_watch._is_florr_auto_afk_running() is False


def test_ensure_florr_auto_afk_running_does_not_open_second_instance_when_already_running(
    monkeypatch, capsys
):
    # 用户报的bug: 不检测就无条件Popen —— florr-auto-afk已经在跑时会开出第二个
    # 实例, 两个都在做YOLO拖拽, 互相抢鼠标.
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    with patch("afk_watch._is_florr_auto_afk_running", return_value=True), \
         patch("builtins.input") as mock_input, \
         patch("afk_watch.subprocess.Popen") as mock_popen, \
         patch("afk_watch._download_and_extract") as mock_download:
        afk_watch.ensure_florr_auto_afk_running()
    mock_popen.assert_not_called()
    mock_input.assert_not_called()    # 在跑就说明装过了, 更不该问下载
    mock_download.assert_not_called()
    assert "已经在跑" in capsys.readouterr().out


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_write_afk_config_overwrites_our_keys_and_keeps_the_rest(tmp_path, monkeypatch):
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    (install_dir / "config.json").write_text(json.dumps({
        "runs": {"autoStart": False, "moveAfterAFK": True, "idleTimeThreshold": 10},
        "advanced": {"verbose": False, "mouseSpeed": 250},
        "yoloConfig": {"segModel": "./models/afk-seg.pt"},
    }))

    assert afk_watch._write_afk_config() is True

    config = _read_json(install_dir / "config.json")
    assert config["runs"]["autoStart"] is True
    assert config["runs"]["autoTakeOverWhenIdle"] is False
    assert config["runs"]["moveAfterAFK"] is False
    # 同一个section里用户自己调过的键不能被冲掉 —— 我们只负责自己依赖的那几个.
    assert config["runs"]["idleTimeThreshold"] == 10
    # 我们压根不碰的section原样留着(连verbose这种我们曾经强制过的键也一样).
    assert config["advanced"] == {"verbose": False, "mouseSpeed": 250}
    assert config["yoloConfig"] == {"segModel": "./models/afk-seg.pt"}


def test_write_afk_config_leaves_advanced_section_entirely_to_the_user(tmp_path, monkeypatch):
    # 曾经强制过advanced.verbose和advanced.skipUpdate, 两个都撤了, 别再加回来:
    # - verbose只管那些硬编码save=False的控制台行, 我们等的
    #   log_ret("Found AFK window", ...)默认save=True无条件落盘, 强制它没有任何收益,
    #   纯粹白覆盖用户自己的选择(见2026-08-11那份design)
    # - skipUpdate强制成True会把上游唯一的模型自修复永久关掉: run_segment
    #   (segment.py:158-165)在模型加载失败时删掉models/afk-seg.pt、afk-det.pt和
    #   models/version, 就等着下次update_models()重下, 而那次重下只由这个键把门
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    (install_dir / "config.json").write_text(json.dumps({
        "advanced": {"verbose": False, "skipUpdate": False, "mouseSpeed": 250},
    }))

    assert afk_watch._write_afk_config() is True

    advanced = _read_json(install_dir / "config.json")["advanced"]
    assert advanced == {"verbose": False, "skipUpdate": False, "mouseSpeed": 250}


def test_write_afk_config_rebuilds_from_shipped_defaults_when_file_missing(tmp_path, monkeypatch):
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()

    assert afk_watch._write_afk_config() is True

    config = _read_json(install_dir / "config.json")
    assert config["runs"]["autoStart"] is True
    # 重建出来的必须是"发行版默认配置 + 我们那几个键", 不能是只含我们那几个键的
    # 最小config: 上游get_config()对缺键没有任何默认值兜底, 少一个segment.exe就
    # KeyError死在启动路上. 断言几个我们压根不覆盖的键 —— 它们只可能来自默认配置.
    assert config["advanced"]["mouseSpeed"] == 100
    assert config["advanced"]["skipUpdate"] is False  # 发行版默认值, 我们不动它
    assert config["gui"]["theme"] == "auto"
    assert config["yoloConfig"]["segModel"] == "./models/afk-seg.pt"
    assert config["advanced"]["windowSizeRatio"] == [0.787, 1]


def test_write_afk_config_rebuilds_when_json_is_corrupt(tmp_path, monkeypatch):
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    (install_dir / "config.json").write_text("{this is not json")

    assert afk_watch._write_afk_config() is True

    config = _read_json(install_dir / "config.json")
    assert config["runs"]["autoStart"] is True
    # 读不出来这条路径跟"文件不存在"一样要以发行版默认配置为底, 不然重建出来的
    # config照样启动不了segment.exe.
    assert config["advanced"]["mouseSpeed"] == 100
    assert config["gui"]["theme"] == "auto"
    assert config["yoloConfig"]["segModel"] == "./models/afk-seg.pt"


def test_write_afk_config_rebuilds_when_top_level_is_not_an_object(tmp_path, monkeypatch):
    # 整份配置被写坏成数组/标量: 拿不来当底, 走跟"读不出来"同一条重建+备份路径.
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    (install_dir / "config.json").write_text("[1, 2, 3]")

    assert afk_watch._write_afk_config() is True

    assert _read_json(install_dir / "config.json")["advanced"]["mouseSpeed"] == 100
    assert (install_dir / "config.json.bak").read_text() == "[1, 2, 3]"


def test_write_afk_config_replaces_section_that_is_not_a_dict(tmp_path, monkeypatch):
    # 真见过配置被写坏成标量的情况; .update()会在这种值上炸AttributeError.
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    (install_dir / "config.json").write_text(json.dumps({"runs": 5}))

    assert afk_watch._write_afk_config() is True
    runs = _read_json(install_dir / "config.json")["runs"]
    assert runs["autoStart"] is True
    # 补回来的section也得是完整的 —— 只塞我们那三个键的话, 上游读
    # runs.idleTimeThreshold(test_idle_thread要用)时就KeyError了.
    assert runs["idleTimeThreshold"] == 10
    assert runs["runningCountDown"] == -1


def test_write_afk_config_returns_false_without_raising_when_dir_missing(tmp_path, monkeypatch, capsys):
    # install目录压根不存在(用户手动删了/解压失败): 写不进去, 但不能把主程序带崩.
    _patch_install_paths(monkeypatch, tmp_path / "nope")

    assert afk_watch._write_afk_config() is False
    assert "config.json" in capsys.readouterr().out


def test_write_afk_config_warns_when_existing_config_cannot_be_read(tmp_path, monkeypatch, capsys):
    # 文件在、但读不出来: 下面的重建用不上用户自己调过的键(mouseSpeed/yoloConfig
    # 之类), 不能一声不响就干了, 得说清楚原文件被挪到哪去了. 这里用真实的触发方式
    # 复现 —— 中文Windows上有人拿记事本按ANSI(GBK)存过它, 我们按utf-8读直接
    # UnicodeDecodeError; 不是"JSON语法坏了"那种一眼能看出来的情况.
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    (install_dir / "config.json").write_bytes(
        '{"advanced": {"note": "中文"}}'.encode("gbk")
    )

    assert afk_watch._write_afk_config() is True

    out = capsys.readouterr().out
    assert "⚠️" in out
    assert "config.json.bak" in out
    # 照样重建出能用的config —— 读失败不等于撂挑子不写.
    assert _read_json(install_dir / "config.json")["runs"]["autoStart"] is True


def test_write_afk_config_keeps_the_unreadable_original_as_a_bak_file(tmp_path, monkeypatch):
    # 读不出来就重建, 但绝不能顺手把用户那份冲掉 —— 里面是他手调过的全部设置,
    # 原封不动挪成.bak, 至少还能自己捞回来.
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    original = '{"advanced": {"note": "中文", "mouseSpeed": 250}}'.encode("gbk")
    (install_dir / "config.json").write_bytes(original)

    assert afk_watch._write_afk_config() is True

    assert (install_dir / "config.json.bak").read_bytes() == original


def test_write_afk_config_bak_holds_the_latest_unreadable_original(tmp_path, monkeypatch):
    # 已经有一份.bak就直接盖掉: 留最近那份够用, 不攒一堆带时间戳的垃圾在人家目录里.
    # (Windows上os.rename()目标已存在会报错, 必须是os.replace()。)
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    (install_dir / "config.json.bak").write_text("上一次的备份")
    (install_dir / "config.json").write_text("{this is not json")

    assert afk_watch._write_afk_config() is True
    assert (install_dir / "config.json.bak").read_text() == "{this is not json"


def test_write_afk_config_still_writes_when_backing_up_the_original_fails(
    tmp_path, monkeypatch, capsys
):
    # 改名失败(文件被别的进程占着/权限不够)不能让这个函数抛异常或者不写配置 ——
    # 照样重建, 只是提示里得说清楚原文件这次真没保住.
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    (install_dir / "config.json").write_text("{this is not json")

    with patch("afk_watch.os.replace", side_effect=OSError("文件被占用")):
        assert afk_watch._write_afk_config() is True

    assert _read_json(install_dir / "config.json")["runs"]["autoStart"] is True
    assert "⚠️" in capsys.readouterr().out


def test_write_afk_config_forces_running_count_down_to_minus_one(tmp_path, monkeypatch):
    # 两个理由都要它是-1: (1) _STARTED_MARKER等的"Running indefinitely"只在这个值
    # 是-1时才写(否则写的是"Running for X minutes", segment.py:36-46), 用户调过它
    # 我们就永远确认不到启动; (2) 非-1意味着它跑满X分钟自己退出
    # (segment.py:61 "Countdown Ends, program exiting"), 挂机中途AFK处理静默失效.
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()
    (install_dir / "config.json").write_text(json.dumps({
        "runs": {"runningCountDown": 30, "idleTimeThreshold": 10},
    }))

    assert afk_watch._write_afk_config() is True

    config = _read_json(install_dir / "config.json")
    assert config["runs"]["runningCountDown"] == -1
    assert config["runs"]["idleTimeThreshold"] == 10  # 同section里的其它键照旧不动


def test_write_afk_config_does_not_mutate_the_default_config_constant(tmp_path, monkeypatch):
    # 合并是就地.update()的 —— 不深拷贝的话第一次重建就把模块级默认值改成了我们的
    # 强制值, 同一次运行里第二次重建拿到的底就不是"出厂设置"了.
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()

    afk_watch._write_afk_config()

    assert afk_watch._DEFAULT_CONFIG["runs"]["moveAfterAFK"] is True
    assert afk_watch._DEFAULT_CONFIG["runs"]["autoTakeOverWhenIdle"] is True
    # fork发行包自带的config.json里autoStart默认是false(单独跑它时行为跟官方一致),
    # _DEFAULT_CONFIG是那份文件的抄本 —— 键要在, 值不能被我们的强制值改成True.
    assert afk_watch._DEFAULT_CONFIG["runs"]["autoStart"] is False


def test_write_afk_config_forced_keys_win_over_the_shipped_defaults(tmp_path, monkeypatch):
    # 发行版默认这两个都是true, 我们要的正好相反 —— 合并顺序写反(默认值盖在强制值
    # 上面)这条就红.
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()

    assert afk_watch._write_afk_config() is True

    config = _read_json(install_dir / "config.json")
    assert config["runs"]["autoStart"] is True
    assert config["runs"]["autoTakeOverWhenIdle"] is False
    assert config["runs"]["moveAfterAFK"] is False


def test_write_afk_config_stays_quiet_when_config_file_does_not_exist_yet(
    tmp_path, monkeypatch, capsys
):
    # 首次运行(刚解压完, 还没有config.json)是正常路径, 别拿警告吓用户.
    install_dir = _patch_install_paths(monkeypatch, tmp_path)
    install_dir.mkdir()

    assert afk_watch._write_afk_config() is True
    assert capsys.readouterr().out == ""


def test_current_log_size_returns_zero_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(tmp_path / "nope.log"))
    assert afk_watch._current_log_size() == 0


def test_current_log_size_returns_real_size(tmp_path, monkeypatch):
    log_path = tmp_path / "latest.log"
    log_path.write_text("hello")
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(log_path))
    assert afk_watch._current_log_size() == 5


def test_wait_for_segment_started_true_when_marker_written_after_offset(tmp_path, monkeypatch):
    log_path = tmp_path / "latest.log"
    log_path.write_text("[old] EVENT: something else\n")
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(log_path))
    start_offset = log_path.stat().st_size
    with open(log_path, "a") as f:
        f.write("[new] <segment.py:41> <afk_thread()> INFO: Running indefinitely\n")

    with patch("afk_watch.time.sleep"):
        assert afk_watch._wait_for_segment_started(start_offset, timeout=0.05, interval=0.01) is True


def test_wait_for_segment_started_ignores_marker_from_a_previous_run(tmp_path, monkeypatch):
    # 这条是这个函数存在的理由: 日志是追加的, 上次运行的那条还在文件里,
    # 整文件搜就会立刻误判成功, 于是"其实没起来"永远不会被发现.
    log_path = tmp_path / "latest.log"
    log_path.write_text("[old run] <segment.py:41> <afk_thread()> INFO: Running indefinitely\n")
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(log_path))
    start_offset = log_path.stat().st_size

    with patch("afk_watch.time.sleep"):
        assert afk_watch._wait_for_segment_started(start_offset, timeout=0.05, interval=0.01) is False


def test_wait_for_segment_started_ignores_the_parent_process_start_line(tmp_path, monkeypatch):
    # "Segment process started"是父进程在segment_process.start()之后立刻写的
    # (segment.py:201-202), 那会儿子进程连YOLO模型都还没开始加载 —— 模型坏了
    # 它照样会写这句. 拿它当"已开始检测"是假阳性, 所以我们等的是子进程里的
    # "Running indefinitely"(模型加载成功 + test_environment()跑完之后才写).
    log_path = tmp_path / "latest.log"
    log_path.write_text(
        "[new] <segment.py:202> <start_segment_process()> INFO: Segment process started\n"
        "[new] <segment.py:234> <toggle_segment_process()> INFO: Segment process started.\n"
    )
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(log_path))

    with patch("afk_watch.time.sleep"):
        assert afk_watch._wait_for_segment_started(0, timeout=0.05, interval=0.01) is False


def test_wait_for_segment_started_false_without_raising_when_log_never_appears(tmp_path, monkeypatch):
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(tmp_path / "never.log"))
    with patch("afk_watch.time.sleep"):
        assert afk_watch._wait_for_segment_started(0, timeout=0.05, interval=0.01) is False


def test_wait_for_segment_started_rereads_from_start_when_log_shrank(tmp_path, monkeypatch):
    # 它启动时若发现latest.log不存在会新建(segment_utils.py:113), 用户也可能手动
    # 删掉 —— 文件比划线时还小就说明不是同一份, 从头读.
    log_path = tmp_path / "latest.log"
    log_path.write_text("INFO: Running indefinitely\n")
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(log_path))

    with patch("afk_watch.time.sleep"):
        assert afk_watch._wait_for_segment_started(9999, timeout=0.05, interval=0.01) is True


def test_wait_for_segment_started_does_not_touch_poll_module_state(tmp_path, monkeypatch):
    # poll_afk_pause()的offset状态是另一套账 —— 确认这个函数没顺手改掉它, 否则
    # 主循环第一次poll的"跳到文件末尾"行为会变.
    log_path = tmp_path / "latest.log"
    log_path.write_text("INFO: Running indefinitely\n")
    monkeypatch.setattr(afk_watch, "LATEST_LOG_PATH", str(log_path))
    monkeypatch.setattr(afk_watch, "_last_offset", 0)
    monkeypatch.setattr(afk_watch, "_initialized", False)

    with patch("afk_watch.time.sleep"):
        afk_watch._wait_for_segment_started(0, timeout=0.05, interval=0.01)

    assert afk_watch._last_offset == 0
    assert afk_watch._initialized is False


def test_ensure_florr_auto_afk_running_marks_log_offset_before_launching(monkeypatch):
    # 划线必须在Popen之前: 它启动瞬间就写marker, 划线晚了那条就落在线左边, 永远等不到.
    calls = []
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    with patch("afk_watch.os.path.isfile", return_value=True), \
         patch("afk_watch._is_florr_auto_afk_running", return_value=False), \
         patch("afk_watch._write_afk_config", side_effect=lambda: calls.append("config")), \
         patch("afk_watch._current_log_size", side_effect=lambda: (calls.append("mark"), 123)[1]), \
         patch("afk_watch.subprocess.Popen", side_effect=lambda *a, **k: calls.append("popen")), \
         patch("afk_watch._wait_for_segment_started", return_value=True) as mock_wait:
        afk_watch.ensure_florr_auto_afk_running()
    assert calls == ["config", "mark", "popen"]
    mock_wait.assert_called_once_with(123)


def test_ensure_florr_auto_afk_running_reports_success_without_asking_for_a_click(monkeypatch, capsys):
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    with patch("afk_watch.os.path.isfile", return_value=True), \
         patch("afk_watch._is_florr_auto_afk_running", return_value=False), \
         patch("afk_watch._write_afk_config"), \
         patch("afk_watch._current_log_size", return_value=0), \
         patch("afk_watch.subprocess.Popen"), \
         patch("afk_watch._wait_for_segment_started", return_value=True):
        afk_watch.ensure_florr_auto_afk_running()
    out = capsys.readouterr().out
    assert "已开启" in out
    assert "run" not in out  # 确认成功时不再要求用户点run


def test_ensure_florr_auto_afk_running_falls_back_to_manual_hint_on_timeout(monkeypatch, capsys):
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    with patch("afk_watch.os.path.isfile", return_value=True), \
         patch("afk_watch._is_florr_auto_afk_running", return_value=False), \
         patch("afk_watch._write_afk_config"), \
         patch("afk_watch._current_log_size", return_value=0), \
         patch("afk_watch.subprocess.Popen"), \
         patch("afk_watch._wait_for_segment_started", return_value=False):
        afk_watch.ensure_florr_auto_afk_running()  # 不该抛异常
    assert "run" in capsys.readouterr().out


def test_is_florr_auto_afk_running_delegates(monkeypatch):
    monkeypatch.setattr(afk_watch, "_is_florr_auto_afk_running", lambda: True)
    assert afk_watch.is_florr_auto_afk_running() is True


def test_stop_florr_auto_afk_noop_off_windows(monkeypatch):
    monkeypatch.setattr(afk_watch.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(afk_watch.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    afk_watch.stop_florr_auto_afk()
    assert calls == []


def test_stop_florr_auto_afk_taskkills_on_windows(monkeypatch):
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    seen = {}
    monkeypatch.setattr(afk_watch.subprocess, "run",
                        lambda args, **k: seen.setdefault("args", args))
    afk_watch.stop_florr_auto_afk()
    assert seen["args"] == ["taskkill", "/IM", "segment.exe", "/F"]


def test_stop_florr_auto_afk_swallows_errors(monkeypatch):
    monkeypatch.setattr(afk_watch.sys, "platform", "win32")
    monkeypatch.setattr(afk_watch.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    afk_watch.stop_florr_auto_afk()  # 不抛即通过


def test_download_florr_auto_afk_delegates_without_prompting(monkeypatch):
    monkeypatch.setattr(afk_watch, "_download_and_extract", lambda: True)
    monkeypatch.setattr(afk_watch, "_prompt_download_confirm",
                        lambda: (_ for _ in ()).throw(AssertionError("不该问确认")))
    assert afk_watch.download_florr_auto_afk() is True
