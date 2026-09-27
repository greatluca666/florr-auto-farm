"""swap.ps1 的真机测试. 要 PowerShell 和 Windows 的文件锁语义, 其它平台整份 skip.
私有仓库的 .github/workflows/windows-updater-test.yml 和公开仓库的 release.yml 都在
windows-latest 上跑它."""
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

import updater

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="swap.ps1 只在 Windows 上跑")

# 假 exe: hostname.exe 的拷贝, 启动后马上退出. PE 文件末尾追加数据不影响运行,
# 用追加的版本标记区分新旧 exe.
_HOSTNAME = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "hostname.exe"

# swap.ps1 的处理顺序: _internal 最先, exe 最后, 其余按名字
_ORDER = ["_internal", "LICENSE", "maps", "README.md", updater.EXE_NAME]
_FILES = ("_internal/lib.txt", "maps/desert.png", "README.md", "LICENSE")


def _make_app(root, tag):
    root.mkdir(parents=True, exist_ok=True)
    (root / updater.EXE_NAME).write_bytes(_HOSTNAME.read_bytes() + tag.encode())
    (root / "_internal").mkdir()
    (root / "_internal" / "lib.txt").write_text(tag)
    (root / "maps").mkdir()
    (root / "maps" / "desert.png").write_text(tag)
    (root / "README.md").write_text(tag)
    (root / "LICENSE").write_text(tag)


def _add_user_data(install):
    (install / "config.json").write_text('{"user": true}', encoding="utf-8")
    (install / "chrome-profiles" / "a").mkdir(parents=True)
    (install / "chrome-profiles" / "a" / "Cookies").write_bytes(b"secret-cookie")


def _assert_user_data_untouched(install):
    assert (install / "config.json").read_text(encoding="utf-8") == '{"user": true}'
    assert (install / "chrome-profiles" / "a" / "Cookies").read_bytes() == b"secret-cookie"


def _assert_app_is(install, tag):
    assert (install / updater.EXE_NAME).read_bytes().endswith(tag.encode())
    for rel in _FILES:
        assert (install / rel).read_text() == tag, rel


def _run_swap(install, staged, pid, timeout_sec=60):
    script = updater.write_swap_script(install)
    return subprocess.run(updater.swap_command(script, pid, install, staged, timeout_sec),
                          capture_output=True, text=True, timeout=180)


def _log(install):
    return (install / "update.log").read_text(encoding="utf-8-sig")


def _placed(log):
    return [ln.split(" placed ", 1)[1].strip() for ln in log.splitlines() if " placed " in ln]


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _package(tmp_path, tag):
    """打一个和发版一样结构的 zip: 顶层只有 florr-auto-pathing/."""
    build = tmp_path / "build" / updater.APP_DIR_NAME
    _make_app(build, tag)
    pkg = tmp_path / "pkg.zip"
    with zipfile.ZipFile(pkg, "w") as zf:
        for p in build.rglob("*"):
            zf.write(p, p.relative_to(build.parent).as_posix())
    return pkg


@pytest.fixture
def app(tmp_path):
    install = tmp_path / "app"
    _make_app(install, "old")
    _add_user_data(install)
    staged = install / updater.UPDATE_DIR / updater.APP_DIR_NAME
    _make_app(staged, "new")
    return install, staged


def test_swap_waits_for_the_app_then_replaces_it_and_keeps_user_data(app):
    install, staged = app
    waiter = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    t0 = time.monotonic()
    r = _run_swap(install, staged, waiter.pid)
    elapsed = time.monotonic() - t0
    waiter.wait()
    log = _log(install)
    assert r.returncode == 0, r.stdout + r.stderr + log
    assert elapsed >= 4, f"没等程序退出就动手了 ({elapsed:.1f}s)"
    _assert_app_is(install, "new")
    _assert_user_data_untouched(install)
    assert "launched" in log
    assert _placed(log) == _ORDER, log
    assert updater.last_update_problem(install) is None
    # 旧版本留着不删, 等新版本真正起来以后由 cleanup_leftovers() 删
    assert sorted(p.name for p in install.glob("*" + updater.OLD_SUFFIX)) == sorted(
        n + updater.OLD_SUFFIX for n in _ORDER)
    updater.cleanup_leftovers(install)
    assert not list(install.glob("*" + updater.OLD_SUFFIX))
    assert not (install / updater.UPDATE_DIR).exists()
    _assert_app_is(install, "new")
    _assert_user_data_untouched(install)


def test_swap_rolls_back_everything_when_a_file_is_locked(app):
    install, staged = app
    # Python 在 Windows 上 open() 不带 FILE_SHARE_DELETE: 打开着的文件改不了名 —— 模拟杀毒软件 /
    # 残留进程占着文件. README.md 排在 _internal、LICENSE、maps 后面, 这三项先放进去了才失败.
    with open(install / "README.md", "rb"):
        r = _run_swap(install, staged, _dead_pid())
    log = _log(install)
    assert r.returncode == 1, r.stdout + r.stderr + log
    assert "rolling back" in log
    assert _placed(log) == ["_internal", "LICENSE", "maps"], log
    _assert_app_is(install, "old")
    _assert_user_data_untouched(install)
    assert not list(install.glob("*" + updater.OLD_SUFFIX)), log
    assert not list(install.glob("*" + updater.FAILED_SUFFIX)), log
    assert "launched" in log            # 回滚后把旧版本启动起来
    problem = updater.last_update_problem(install)
    assert problem and "rolling back" not in problem, log


def test_swap_gives_up_without_touching_anything_if_the_app_never_exits(app):
    install, staged = app
    waiter = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        r = _run_swap(install, staged, waiter.pid, timeout_sec=2)
    finally:
        waiter.kill()
    assert r.returncode == 2, r.stdout + r.stderr + _log(install)
    assert (install / "README.md").read_text() == "old"
    assert (staged / "README.md").read_text() == "new"
    assert updater.last_update_problem(install).startswith("timeout")


def test_swap_with_missing_staged_dir_relaunches_the_old_version(app):
    install, staged = app
    shutil.rmtree(staged)
    r = _run_swap(install, staged, _dead_pid())
    log = _log(install)
    assert r.returncode == 1, r.stdout + r.stderr + log
    assert "swap failed" in log and "launched" in log
    _assert_app_is(install, "old")
    _assert_user_data_untouched(install)


def test_swap_still_works_when_the_log_cannot_be_written(app):
    install, staged = app
    (install / "update.log").mkdir()            # Add-Content 往目录里写必然失败
    r = _run_swap(install, staged, _dead_pid())
    assert r.returncode == 0, r.stdout + r.stderr
    _assert_app_is(install, "new")
    _assert_user_data_untouched(install)
    assert (install / "update.log").is_dir()
    assert updater.last_update_problem(install) is None


def test_stage_then_swap_end_to_end(tmp_path):
    install = tmp_path / "app"
    _make_app(install, "old")
    _add_user_data(install)
    staged = updater.stage(_package(tmp_path, "new"), install)
    r = _run_swap(install, staged, _dead_pid())
    assert r.returncode == 0, r.stdout + r.stderr + _log(install)
    _assert_app_is(install, "new")
    _assert_user_data_untouched(install)


def test_stage_then_swap_in_a_path_with_spaces_and_chinese(tmp_path):
    install = tmp_path / "florr 自动 更新" / "app"
    _make_app(install, "old")
    _add_user_data(install)
    staged = updater.stage(_package(tmp_path, "new"), install)
    r = _run_swap(install, staged, _dead_pid())
    log = _log(install)
    assert r.returncode == 0, r.stdout + r.stderr + log
    _assert_app_is(install, "new")
    _assert_user_data_untouched(install)
    assert "swap ok" in log and "launched" in log
