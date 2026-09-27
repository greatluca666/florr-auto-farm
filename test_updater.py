import base64
import hashlib
import importlib.util
import io
import ntpath
import os
import sys
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import updater


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)
    return priv, base64.b64encode(pub).decode()


def _sign(priv, ver, sha):
    return base64.b64encode(priv.sign(updater.signed_message(ver, sha))).decode()


SHA = hashlib.sha256(b"zip-bytes").hexdigest()


@pytest.mark.parametrize("cand,cur,expected", [
    ("1.2.0", "1.1.9", True),
    ("1.2.0", "1.2.0", False),
    ("1.1.9", "1.2.0", False),
    ("1.10.0", "1.9.0", True),        # 按数字比, 不是按字符串
    ("1.0.0", "0.0.0+dev", True),     # 源码运行的版本比任何正式版都旧
    ("garbage", "1.0.0", False),
    ("1.0", "0.0.0+dev", False),
    ("v1.2.0", "1.0.0", False),       # 清单里的版本号不带 v
])
def test_is_newer(cand, cur, expected):
    assert updater.is_newer(cand, cur) is expected


def test_asset_name_and_signed_message_format():
    assert updater.asset_name("1.2.0") == "florr-auto-farm-v1.2.0-win64.zip"
    assert updater.signed_message("1.2.0", "ABCDEF") == b"florr-auto-farm\n1.2.0\nabcdef\n"


def test_verify_signature_accepts_good_signature():
    priv, pub = _keypair()
    updater.verify_signature("1.2.0", SHA, _sign(priv, "1.2.0", SHA), pub)


def test_verify_signature_rejects_other_key():
    priv, _ = _keypair()
    _, other_pub = _keypair()
    with pytest.raises(updater.UpdateError, match="签名"):
        updater.verify_signature("1.2.0", SHA, _sign(priv, "1.2.0", SHA), other_pub)


def test_verify_signature_rejects_old_package_presented_as_new_version():
    priv, pub = _keypair()
    with pytest.raises(updater.UpdateError):
        updater.verify_signature("1.2.0", SHA, _sign(priv, "1.0.0", SHA), pub)


def test_verify_signature_rejects_tampered_sha():
    priv, pub = _keypair()
    with pytest.raises(updater.UpdateError):
        updater.verify_signature("1.2.0", hashlib.sha256(b"evil").hexdigest(),
                                 _sign(priv, "1.2.0", SHA), pub)


def test_verify_signature_rejects_garbage_signature():
    _, pub = _keypair()
    with pytest.raises(updater.UpdateError):
        updater.verify_signature("1.2.0", SHA, "not base64!!", pub)


def test_verify_signature_refuses_when_no_key_is_built_in(monkeypatch):
    priv, _ = _keypair()
    monkeypatch.setattr(updater, "PUBLIC_KEY_B64", "")
    with pytest.raises(updater.UpdateError, match="公钥"):
        updater.verify_signature("1.2.0", SHA, _sign(priv, "1.2.0", SHA))


def test_verify_signature_uses_built_in_key_by_default(monkeypatch):
    priv, pub = _keypair()
    monkeypatch.setattr(updater, "PUBLIC_KEY_B64", pub)
    updater.verify_signature("1.2.0", SHA, _sign(priv, "1.2.0", SHA))


def test_updater_imports_without_cryptography_and_only_verify_complains(monkeypatch):
    # 源码运行 git pull 之后还没 pip install cryptography: 控制面板照样要能打开
    names = [n for n in sys.modules if n == "cryptography" or n.startswith("cryptography.")]
    for n in {*names, "cryptography", "cryptography.exceptions",
              "cryptography.hazmat.primitives.asymmetric.ed25519"}:
        monkeypatch.setitem(sys.modules, n, None)      # None = 这个模块 import 必定失败
    spec = importlib.util.spec_from_file_location("updater_without_cryptography",
                                                  updater.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)                      # 不应该抛 ModuleNotFoundError
    with pytest.raises(fresh.UpdateError, match="缺少 cryptography"):
        fresh.verify_signature("1.2.0", SHA, "c2ln", "AAAA")


def _info(**kw):
    base = dict(version="1.2.0", notes="", published_at="", urls=("https://m/x.zip",),
                sha256=None, signature="", size=None, source="官网")
    base.update(kw)
    return updater.UpdateInfo(**base)


def test_verify_reports_sha_mismatch_before_checking_signature():
    priv, pub = _keypair()
    info = _info(sha256="0" * 64, signature=_sign(priv, "1.2.0", SHA))
    with pytest.raises(updater.UpdateError, match="sha256"):
        updater.verify(info, SHA, pub)


def test_verify_passes_with_matching_sha_and_signature():
    priv, pub = _keypair()
    updater.verify(_info(sha256=SHA.upper(), signature=_sign(priv, "1.2.0", SHA)), SHA, pub)


def _manifest(**over):
    m = {
        "schema": 1, "version": "1.2.0", "published_at": "2026-10-01T00:00:00Z",
        "notes": "修了 bug",
        "win64": {
            "file": "florr-auto-farm-v1.2.0-win64.zip",
            "url": "https://florrfarm.cc.cd/download/florr-auto-farm-v1.2.0-win64.zip",
            "github_url": "https://github.com/x/y/releases/download/v1.2.0/"
                          "florr-auto-farm-v1.2.0-win64.zip",
            "size": 123, "sha256": SHA, "signature": "c2ln"},
        "history": [],
    }
    m.update(over)
    return m


def test_parse_manifest_mirror_first_then_github():
    info = updater.parse_manifest(_manifest())
    assert info.version == "1.2.0"
    assert info.urls == (
        "https://florrfarm.cc.cd/download/florr-auto-farm-v1.2.0-win64.zip",
        "https://github.com/x/y/releases/download/v1.2.0/florr-auto-farm-v1.2.0-win64.zip")
    assert (info.sha256, info.signature, info.size, info.notes, info.source) == (
        SHA, "c2ln", 123, "修了 bug", "官网")


@pytest.mark.parametrize("bad", [
    {"schema": 2},
    {"version": "1.2"},
    {"win64": None},
    {"win64": {"url": "", "github_url": "", "signature": "c2ln"}},   # 没有下载地址
    {"win64": {"url": "https://m/x.zip"}},                           # 没签名
    {"win64": {"url": "https://m/x.zip", "signature": ""}},
])
def test_parse_manifest_rejects_bad_manifests(bad):
    with pytest.raises(updater.UpdateError):
        updater.parse_manifest(_manifest(**bad))


def test_parse_manifest_rejects_non_dict():
    with pytest.raises(updater.UpdateError):
        updater.parse_manifest(["not", "a", "dict"])


def test_parse_manifest_drops_non_https_urls():
    m = _manifest()
    m["win64"]["url"] = "http://florrfarm.cc.cd/download/x.zip"
    info = updater.parse_manifest(m)
    assert info.urls == (m["win64"]["github_url"],)


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "ftp://evil/x.zip",
                                 "http://florrfarm.cc.cd/x.zip", "//evil/x.zip"])
def test_parse_manifest_rejects_when_no_https_url_is_left(url):
    m = _manifest()
    m["win64"]["url"] = url
    m["win64"]["github_url"] = url
    with pytest.raises(updater.UpdateError, match="https"):
        updater.parse_manifest(m)


def _release(tag="v1.2.0", names=None, digest="sha256:" + SHA):
    if names is None:
        names = ["florr-auto-farm-v1.2.0-win64.zip", "florr-auto-farm-v1.2.0-win64.zip.sig"]
    assets = [{"name": n, "browser_download_url": f"https://gh/{n}", "size": 99,
               "digest": digest if n.endswith(".zip") else None} for n in names]
    return {"tag_name": tag, "body": "notes", "published_at": "2026-10-01T00:00:00Z",
            "assets": assets}


def test_info_from_github_release():
    fetched = []
    info = updater.info_from_github_release(
        _release(), lambda url: fetched.append(url) or "c2ln\n")
    assert info.version == "1.2.0"
    assert info.urls == ("https://gh/florr-auto-farm-v1.2.0-win64.zip",)
    assert info.signature == "c2ln"
    assert info.sha256 == SHA
    assert info.size == 99
    assert info.source == "GitHub"
    assert fetched == ["https://gh/florr-auto-farm-v1.2.0-win64.zip.sig"]


def test_info_from_github_release_without_digest_leaves_sha_empty():
    info = updater.info_from_github_release(_release(digest=None), lambda url: "c2ln")
    assert info.sha256 is None


@pytest.mark.parametrize("release", [
    _release(tag="1"),                                        # 现在手动传的那个 Release
    _release(names=["florr-auto-farm-v1.2.0-win64.zip"]),     # 缺 .sig
    _release(names=["florr-auto-pathing.zip"]),
])
def test_info_from_github_release_rejects_unusable_release(release):
    with pytest.raises(updater.UpdateError):
        updater.info_from_github_release(release, lambda url: "c2ln")


@pytest.mark.parametrize("which", ["zip", "sig"])
@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "ftp://evil/x"])
def test_info_from_github_release_rejects_non_https_urls(which, url):
    release = _release()
    asset = release["assets"][0 if which == "zip" else 1]
    asset["browser_download_url"] = url
    fetched = []
    with pytest.raises(updater.UpdateError, match="https"):
        updater.info_from_github_release(release, lambda u: fetched.append(u) or "c2ln")
    assert fetched == []            # 不是 https 的地址一次都不去访问


def test_check_latest_uses_mirror_and_skips_github_when_mirror_answers():
    calls = []

    def fetch_json(url):
        calls.append(url)
        return _manifest()

    info = updater.check_latest("1.1.0", fetch_json=fetch_json, fetch_text=lambda u: "")
    assert info.version == "1.2.0" and info.source == "官网"
    assert calls == [updater.MANIFEST_URL]


def test_check_latest_returns_none_when_not_newer_without_asking_github():
    calls = []

    def fetch_json(url):
        calls.append(url)
        return _manifest()

    assert updater.check_latest("1.2.0", fetch_json=fetch_json,
                                fetch_text=lambda u: "") is None
    assert calls == [updater.MANIFEST_URL]


def test_check_latest_falls_back_to_github():
    def fetch_json(url):
        if url == updater.MANIFEST_URL:
            raise OSError("mirror down")
        assert url == updater.GITHUB_LATEST_API
        return _release()

    info = updater.check_latest("1.1.0", fetch_json=fetch_json, fetch_text=lambda u: "c2ln")
    assert info.source == "GitHub"


def test_check_latest_reports_both_failures():
    def fetch_json(url):
        raise OSError("down")

    with pytest.raises(updater.UpdateError) as e:
        updater.check_latest("1.1.0", fetch_json=fetch_json, fetch_text=lambda u: "")
    assert "官网" in str(e.value) and "GitHub" in str(e.value)


class _Resp(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}


def test_download_falls_back_to_next_url_and_hashes(tmp_path):
    data = b"x" * 600_000
    progress = []

    def opener(url):
        if "mirror" in url:
            raise OSError("mirror broken")
        return _Resp(data)

    info = _info(urls=("https://mirror/x.zip", "https://github/x.zip"))
    dest = tmp_path / "pkg.zip"
    digest = updater.download(info, dest, lambda d, t: progress.append((d, t)), opener=opener)
    assert digest == hashlib.sha256(data).hexdigest()
    assert dest.read_bytes() == data
    assert progress[-1] == (len(data), len(data))
    assert not (tmp_path / "pkg.zip.part").exists()


def test_download_rejects_truncated_body_and_leaves_nothing(tmp_path):
    dest = tmp_path / "pkg.zip"
    with pytest.raises(updater.UpdateError, match="下载失败"):
        updater.download(_info(urls=("https://mirror/x.zip",)), dest,
                         opener=lambda url: _Resp(b"abc", length=10))
    assert not dest.exists()
    assert not (tmp_path / "pkg.zip.part").exists()


def test_download_uses_manifest_size_when_no_content_length(tmp_path):
    class NoLength(_Resp):
        def __init__(self, data):
            super().__init__(data)
            self.headers = {}

    totals = []
    updater.download(_info(urls=("https://m/x.zip",), size=5), tmp_path / "p.zip",
                     lambda d, t: totals.append(t), opener=lambda u: NoLength(b"12345"))
    assert totals[-1] == 5


class _Endless:
    """不给 Content-Length、一直往外吐数据的响应(最多 chunks 块), 记下被读了几次."""

    def __init__(self, chunks=100):
        self.headers = {}
        self.left = chunks
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        self.reads += 1
        if self.left <= 0:
            return b""
        self.left -= 1
        return b"x" * n


def test_download_aborts_once_body_exceeds_the_published_size(tmp_path):
    resp = _Endless()
    dest = tmp_path / "pkg.zip"
    size = 3 * updater._CHUNK
    with pytest.raises(updater.UpdateError, match="超过"):
        updater.download(_info(urls=("https://m/x.zip",), size=size), dest, opener=lambda u: resp)
    assert resp.reads <= 4          # 超出的那一块读到就停, 不会把 100 块都读完
    assert not dest.exists()
    assert not (tmp_path / "pkg.zip.part").exists()


def test_download_without_known_size_is_capped(tmp_path, monkeypatch):
    assert updater._DOWNLOAD_LIMIT == 512 * 1024 * 1024
    monkeypatch.setattr(updater, "_DOWNLOAD_LIMIT", 3 * updater._CHUNK)
    resp = _Endless()
    dest = tmp_path / "pkg.zip"
    with pytest.raises(updater.UpdateError, match="超过"):
        updater.download(_info(urls=("https://m/x.zip",), size=None), dest, opener=lambda u: resp)
    assert resp.reads <= 4
    assert not dest.exists()
    assert not (tmp_path / "pkg.zip.part").exists()


def _zip(path, entries):
    """entries: {zip 里的文件名: 内容}. 文件名原样写入(可以带反斜杠, 模拟 Windows 上的
    Compress-Archive 打出来的包)."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(zipfile.ZipInfo(name), data)
    return path


GOOD = {
    "florr-auto-pathing/florr-auto-pathing.exe": b"exe",
    "florr-auto-pathing/_internal/base_library.zip": b"lib",
    "florr-auto-pathing/maps/desert.png": b"png",
    "florr-auto-pathing/README.md": b"readme",
}


def _install(tmp_path):
    d = tmp_path / "app"
    d.mkdir()
    return d


def test_stage_extracts_into_update_dir(tmp_path):
    install = _install(tmp_path)
    staged = updater.stage(_zip(tmp_path / "u.zip", GOOD), install)
    assert staged == install / ".update" / "florr-auto-pathing"
    assert (staged / "florr-auto-pathing.exe").read_bytes() == b"exe"
    assert (staged / "_internal" / "base_library.zip").read_bytes() == b"lib"
    assert (staged / "maps" / "desert.png").read_bytes() == b"png"


def test_stage_handles_backslash_names(tmp_path):
    install = _install(tmp_path)
    entries = {k.replace("/", "\\"): v for k, v in GOOD.items()}
    staged = updater.stage(_zip(tmp_path / "u.zip", entries), install)
    assert (staged / "_internal" / "base_library.zip").read_bytes() == b"lib"


def test_stage_replaces_leftover_update_dir(tmp_path):
    install = _install(tmp_path)
    (install / ".update" / "junk").mkdir(parents=True)
    updater.stage(_zip(tmp_path / "u.zip", GOOD), install)
    assert not (install / ".update" / "junk").exists()


@pytest.mark.parametrize("evil", [
    "florr-auto-pathing/../../evil.txt",
    "/etc/evil",
    "C:/evil.txt",
    "florr-auto-pathing\\..\\..\\evil.txt",
    "florr-auto-pathing/C:evil.dll",        # 盘符相对路径
    "florr-auto-pathing/a/b:stream",        # NTFS 备用数据流
])
def test_stage_rejects_path_traversal(tmp_path, evil):
    install = _install(tmp_path)
    with pytest.raises(updater.UpdateError, match="不安全"):
        updater.stage(_zip(tmp_path / "u.zip", {**GOOD, evil: b"x"}), install)
    assert not (tmp_path / "evil.txt").exists()
    assert not (install / ".update" / "florr-auto-pathing" / "a").exists()   # 一个文件都没写


def test_stage_rejects_truncated_extraction(tmp_path, monkeypatch):
    # 模拟解压时磁盘满 / 杀毒软件把文件截断: 解出来的大小和 zip 里记的不一样
    real = updater.shutil.copyfileobj

    def short_copy(src, dst, *a, **k):
        if getattr(dst, "name", "").endswith("desert.png"):
            dst.write(src.read()[:-1])
        else:
            real(src, dst, *a, **k)

    monkeypatch.setattr(updater.shutil, "copyfileobj", short_copy)
    install = _install(tmp_path)
    with pytest.raises(updater.UpdateError, match="解压不完整.*desert.png"):
        updater.stage(_zip(tmp_path / "u.zip", GOOD), install)


def test_stage_rejects_file_missing_after_extraction(tmp_path, monkeypatch):
    real = updater.shutil.copyfileobj

    def vanishing_copy(src, dst, *a, **k):
        real(src, dst, *a, **k)
        if getattr(dst, "name", "").endswith("README.md"):
            dst.close()
            os.remove(dst.name)          # 写完就被删掉(杀毒软件隔离)

    monkeypatch.setattr(updater.shutil, "copyfileobj", vanishing_copy)
    install = _install(tmp_path)
    with pytest.raises(updater.UpdateError, match="解压不完整.*README.md"):
        updater.stage(_zip(tmp_path / "u.zip", GOOD), install)


def test_stage_rejects_second_top_level_dir(tmp_path):
    install = _install(tmp_path)
    with pytest.raises(updater.UpdateError, match="结构"):
        updater.stage(_zip(tmp_path / "u.zip", {**GOOD, "other/x": b"x"}), install)


def test_stage_rejects_package_without_exe(tmp_path):
    install = _install(tmp_path)
    entries = {k: v for k, v in GOOD.items() if not k.endswith(".exe")}
    with pytest.raises(updater.UpdateError, match="缺少"):
        updater.stage(_zip(tmp_path / "u.zip", entries), install)


def test_stage_rejects_non_zip(tmp_path):
    install = _install(tmp_path)
    bad = tmp_path / "u.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(updater.UpdateError, match="zip"):
        updater.stage(bad, install)


def test_swap_script_is_ascii_and_written_with_bom(tmp_path):
    assert updater.SWAP_PS1.isascii()
    script = updater.write_swap_script(tmp_path)
    assert script == tmp_path / ".update" / "swap.ps1"
    assert script.read_bytes().startswith(b"\xef\xbb\xbf")


# swap.ps1 的行为只能在 Windows 上真跑(test_updater_windows.py). 这里只在 Mac 上也能挡住
# 几个结构性的倒退: 这些位置写错了, Windows CI 要等推上去才会发现.
def test_swap_script_log_never_throws():
    log_fn = updater.SWAP_PS1.split("function Log")[1].split("function Retry")[0]
    assert "try" in log_fn and "catch" in log_fn and "Add-Content" in log_fn


def test_swap_script_lists_staged_items_inside_the_try():
    before, body = updater.SWAP_PS1.split("\ntry {", 1)
    assert "Get-ChildItem" not in before
    assert "Get-ChildItem" in body.split("\n} catch {", 1)[0]


def test_swap_script_keeps_old_items_on_success_and_moves_stuck_ones_aside():
    ok_tail = updater.SWAP_PS1.split('Log "swap ok"', 1)[1]
    assert "Launch" in ok_tail and "Remove-Item" not in ok_tail   # *.old-update 留给下次启动删
    rollback = updater.SWAP_PS1.split("\n} catch {", 1)[1].split('Log "swap ok"', 1)[0]
    # 回滚的每一步都要重试: 删新文件、删不掉就改名让位、把旧文件改回原名
    for step in ("Retry { Remove-Item -LiteralPath $target -Recurse -Force }",
                 'Retry { Rename-Item -LiteralPath $target -NewName "$name.failed-update" }',
                 'Retry { Rename-Item -LiteralPath (Join-Path $InstallDir "$name.old-update")'
                 " -NewName $name }"):
        assert step in rollback, step


def test_swap_command_passes_all_parameters(tmp_path):
    staged = tmp_path / "app" / ".update" / "florr-auto-pathing"
    cmd = updater.swap_command(tmp_path / "s.ps1", 4242, tmp_path / "app", staged, 60)
    assert cmd[0].lower().endswith("powershell.exe")
    assert cmd[cmd.index("-ExecutionPolicy") + 1] == "Bypass"
    assert cmd[cmd.index("-File") + 1] == str(tmp_path / "s.ps1")
    assert cmd[cmd.index("-WaitPid") + 1] == "4242"
    assert cmd[cmd.index("-InstallDir") + 1] == str(tmp_path / "app")
    assert cmd[cmd.index("-StagedDir") + 1] == str(staged)
    assert cmd[cmd.index("-ExeName") + 1] == "florr-auto-pathing.exe"
    assert cmd[cmd.index("-TimeoutSec") + 1] == "60"


@pytest.mark.parametrize("system_root,expected", [
    (r"D:\Win", "D:\\Win\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"),
    (None, "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"),
])
def test_swap_command_uses_absolute_powershell_path(tmp_path, monkeypatch, system_root, expected):
    # 不走 PATH 搜索: 安装目录 / 当前目录里的 powershell.exe 冒充不了
    if system_root is None:
        monkeypatch.delenv("SystemRoot", raising=False)
    else:
        monkeypatch.setenv("SystemRoot", system_root)
    cmd = updater.swap_command(tmp_path / "s.ps1", 1, tmp_path, tmp_path / "s", 60)
    assert ntpath.isabs(cmd[0])
    assert ntpath.normpath(cmd[0]).lower() == expected.lower()


def _capture_popen(monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw

    monkeypatch.setattr(updater.subprocess, "Popen", FakePopen)
    return seen


def test_launch_swap_starts_detached_powershell(tmp_path, monkeypatch):
    seen = _capture_popen(monkeypatch)
    updater.launch_swap(tmp_path, tmp_path / ".update" / "florr-auto-pathing", 99)
    assert seen["cmd"][seen["cmd"].index("-WaitPid") + 1] == "99"
    assert seen["kw"]["creationflags"] & 0x00000008        # DETACHED_PROCESS
    assert (tmp_path / ".update" / "swap.ps1").is_file()


def test_launch_swap_drops_pyinstaller_internal_env(tmp_path, monkeypatch):
    # 替换脚本最后启动的新 exe 继承它的环境; 带着旧进程的 _PYI_* / _MEIPASS2, 新 exe 的
    # 引导程序会以为自己是旧进程的子进程, 去用已经被删掉的旧解压目录
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\old\_internal")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("_MEIPASS2", r"C:\old\_internal")
    monkeypatch.setenv("FLORR_KEEP_ME", "yes")
    seen = _capture_popen(monkeypatch)
    updater.launch_swap(tmp_path, tmp_path / ".update" / "florr-auto-pathing", 99)
    env = seen["kw"]["env"]
    assert not [k for k in env if k.upper().startswith("_PYI") or k.upper() == "_MEIPASS2"]
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert env["FLORR_KEEP_ME"] == "yes"
    assert "_PYI_APPLICATION_HOME_DIR" in os.environ          # 只改传给子进程的副本


def test_cleanup_leftovers_removes_only_update_debris(tmp_path):
    (tmp_path / "_internal.old-update").mkdir()
    (tmp_path / "florr-auto-pathing.exe.old-update").write_bytes(b"x")
    (tmp_path / "maps.failed-update").mkdir()
    (tmp_path / "README.md.failed-update").write_bytes(b"x")
    (tmp_path / ".update" / "florr-auto-pathing").mkdir(parents=True)
    (tmp_path / ".update-download.zip").write_bytes(b"x")
    (tmp_path / ".update-download.zip.part").write_bytes(b"x")
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "chrome-profiles").mkdir()
    (tmp_path / "update.log").write_text("x")
    updater.cleanup_leftovers(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "chrome-profiles", "config.json", "update.log"]


# ---- update.log: 替换脚本有没有起来 / 上次更新有没有完成 ----

def _write_log(install, *lines, bom=True):
    text = "".join(f"2026-09-27T10:00:0{i % 10} {line}\r\n" for i, line in enumerate(lines))
    (install / "update.log").write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def test_wait_for_swap_start_sees_the_start_line(tmp_path):
    clock = _Clock()

    def sleep(s):
        clock.sleep(s)
        if len(clock.sleeps) == 3:          # 第 3 次轮询前脚本才写出第一行
            _write_log(tmp_path, "update start: pid=4242 staged=C:\\x")

    assert updater.wait_for_swap_start(tmp_path, 4242, timeout=10, poll=0.2,
                                       sleep=sleep, clock=clock) is True
    assert len(clock.sleeps) == 3


def test_wait_for_swap_start_times_out(tmp_path):
    clock = _Clock()
    _write_log(tmp_path, "update start: pid=42421 staged=C:\\x")    # 别的 pid, 前缀相同也不算
    assert updater.wait_for_swap_start(tmp_path, 4242, timeout=1.0, poll=0.25,
                                       sleep=clock.sleep, clock=clock) is False
    assert clock.now >= 1.0
    assert sum(clock.sleeps) <= 1.25


def test_wait_for_swap_start_treats_unreadable_log_as_not_yet(tmp_path):
    (tmp_path / "update.log").mkdir()
    clock = _Clock()
    assert updater.wait_for_swap_start(tmp_path, 1, timeout=0.5, poll=0.25,
                                       sleep=clock.sleep, clock=clock) is False


@pytest.mark.parametrize("lines,expected", [
    ([], None),
    (["update start: pid=1 staged=x", "placed _internal", "swap ok", "launched x"], None),
    (["update start: pid=1 staged=x", "placed _internal",
      "swap failed: The file is in use; rolling back", "launched x"], "The file is in use"),
    (["update start: pid=1 staged=x",
      "timeout: pid 1 still running after 60 s, nothing changed"],
     "timeout: pid 1 still running after 60 s, nothing changed"),
    (["update start: pid=1 staged=x"], "替换脚本中途退出"),
    (["update start: pid=1 staged=x", "placed _internal"], "替换脚本中途退出"),
    (["update start: pid=1 staged=x", "placed timeout.dll"], "替换脚本中途退出"),
    # 只看最后一次: 以前失败过、这次成功了 → 没问题
    (["update start: pid=1 staged=x", "swap failed: old; rolling back",
      "update start: pid=2 staged=x", "swap ok"], None),
    # 以前成功过、这次失败了 → 报这次的
    (["update start: pid=1 staged=x", "swap ok",
      "update start: pid=2 staged=x", "swap failed: new; rolling back"], "new"),
    # 已经告诉过用户了
    (["update start: pid=1 staged=x", "swap failed: x; rolling back", "acknowledged"], None),
])
def test_last_update_problem(tmp_path, lines, expected):
    if lines:
        _write_log(tmp_path, *lines)
    assert updater.last_update_problem(tmp_path) == expected


def test_last_update_problem_unreadable_log_is_no_problem(tmp_path):
    (tmp_path / "update.log").mkdir()
    assert updater.last_update_problem(tmp_path) is None


def test_acknowledge_update_problem_silences_it(tmp_path):
    _write_log(tmp_path, "update start: pid=1 staged=x", "swap failed: boom; rolling back")
    assert updater.last_update_problem(tmp_path) == "boom"
    updater.acknowledge_update_problem(tmp_path)
    assert updater.last_update_problem(tmp_path) is None
    # 下一次更新又失败, 照样要报
    with open(tmp_path / "update.log", "a", encoding="utf-8") as f:
        f.write("2026-09-28T10:00:00 update start: pid=2 staged=x\r\n")
    assert updater.last_update_problem(tmp_path) == "替换脚本中途退出"


def test_acknowledge_update_problem_swallows_errors(tmp_path):
    (tmp_path / "update.log").mkdir()
    updater.acknowledge_update_problem(tmp_path)              # 不应该抛
    updater.acknowledge_update_problem(tmp_path / "missing")  # 目录都不存在也不抛


@pytest.mark.parametrize("frozen,platform,expected", [
    (True, "win32", True), (False, "win32", False), (True, "darwin", False)])
def test_enabled_only_for_frozen_windows(monkeypatch, frozen, platform, expected):
    monkeypatch.setattr(updater.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(updater.sys, "platform", platform)
    assert updater.enabled() is expected


def test_install_dir_is_exe_folder_when_frozen(monkeypatch, tmp_path):
    exe = tmp_path / "florr-auto-pathing.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    assert updater.install_dir() == tmp_path.resolve()


def test_built_in_public_key_is_a_valid_ed25519_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = base64.b64decode(updater.PUBLIC_KEY_B64, validate=True)
    assert len(raw) == 32
    Ed25519PublicKey.from_public_bytes(raw)
