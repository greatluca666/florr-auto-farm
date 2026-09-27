"""打包版(Windows exe)的自动更新. 纯逻辑, 界面在 gui_update.py.

流程: check_latest() 查版本 → download() 下载并算 sha256 → verify() 验签名 →
stage() 解压到安装目录下的 .update/ → launch_swap() 起一个 PowerShell 脚本,
等本进程退出后替换文件并重启. 设计见
docs/superpowers/specs/2026-09-26-official-site-and-auto-update-design.md.

版本信息先查官网镜像 florrfarm.cc.cd(国内快), 查不到再查 GitHub; 下载也是先镜像后 GitHub.
真正保证安装包没被掉包的是 Ed25519 签名: 私钥只在公开仓库的 GitHub Actions secret 里,
镜像服务器被黑也伪造不了. sha256 只用来给「下载损坏」一个更清楚的报错.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import certifi

import version

PRODUCT = "florr-auto-farm"
SITE_URL = "https://florrfarm.cc.cd/"
MANIFEST_URL = "https://florrfarm.cc.cd/latest.json"
GITHUB_REPO = "greatluca666/florr-auto-farm"
GITHUB_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# 更新签名公钥(base64 的 32 字节 Ed25519 公钥), 由 deploy/release/gen_signing_key.py
# 生成(2026-09-27), 私钥只在公开仓库的 GitHub secret UPDATE_SIGNING_KEY 里.
# 为空时 verify_signature() 一律拒绝 —— 宁可不更新, 也不装没验过签名的包.
PUBLIC_KEY_B64 = "WvwOAbM4evOLXMgB717BAHzUz/hFtBlUXDxroCLpI54="

_USER_AGENT = f"{PRODUCT}-updater"
# 和 server_lookup.py 同样的原因: Windows 上 urllib 不读系统证书库, 显式用 certifi.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_JSON_LIMIT = 1024 * 1024        # latest.json / GitHub API 响应的上限, 防止被塞超大响应
_DOWNLOAD_LIMIT = 512 * 1024 * 1024   # 发布信息里没给大小时, 安装包最多下这么多
_CHUNK = 256 * 1024
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG_RE = re.compile(r"^v(\d+\.\d+\.\d+)$")


class UpdateError(Exception):
    """更新流程里要告诉用户的失败. 消息是中文, 直接显示在界面上."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    notes: str
    published_at: str
    urls: tuple              # 按顺序尝试的下载地址(镜像在前, GitHub 在后)
    sha256: str | None       # 清单 / GitHub 给的 sha256, 可能没有
    signature: str           # .sig 文件的内容(base64)
    size: int | None
    source: str              # "官网" 或 "GitHub"


def parse_version(s):
    m = _VERSION_RE.match(s or "")
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(candidate, current):
    """candidate 是否比 current 新. 只认 X.Y.Z; current 不是 X.Y.Z(比如源码运行的
    0.0.0+dev)时, 当成比任何正式版都旧."""
    cand = parse_version(candidate)
    if cand is None:
        return False
    cur = parse_version(current)
    return cur is None or cand > cur


def asset_name(ver):
    return f"{PRODUCT}-v{ver}-win64.zip"


def signed_message(ver, sha256_hex):
    """签名覆盖的消息. 版本号也签进去: 拿一个旧的合法包冒充新版本, 签名就对不上(防降级).
    deploy/release/sign_release.py 用的也是这个函数, 两边格式不会走样."""
    return f"{PRODUCT}\n{ver}\n{sha256_hex.lower()}\n".encode("utf-8")


def verify_signature(ver, sha256_hex, signature_b64, public_key_b64=None):
    key_b64 = PUBLIC_KEY_B64 if public_key_b64 is None else public_key_b64
    if not key_b64:
        raise UpdateError("程序里没有内置更新公钥, 没法验证更新包, 请到官网手动下载")
    # 用到才 import: 源码运行的人 git pull 之后还没装 cryptography 时, gui_app → gui_update →
    # updater 这条 import 链不能直接崩掉控制面板, 只让「一键更新」这一步报错
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as e:
        raise UpdateError("缺少 cryptography 库, 没法验证更新包签名"
                          "(源码运行请先 pip install -r requirements.txt)") from e
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64, validate=True))
        sig = base64.b64decode((signature_b64 or "").strip(), validate=True)
        key.verify(sig, signed_message(ver, sha256_hex))
    except (InvalidSignature, ValueError) as e:     # binascii.Error 是 ValueError 的子类
        raise UpdateError("更新包签名校验失败, 已取消更新(安装包可能被篡改)") from e


def verify(info, sha256_hex, public_key_b64=None):
    if info.sha256 and info.sha256.lower() != sha256_hex.lower():
        raise UpdateError("下载的文件和发布信息里的 sha256 对不上, 可能下载损坏了, 请重试")
    verify_signature(info.version, sha256_hex, info.signature, public_key_b64)


def _is_https(url):
    # 只认 https: urllib 也能打开 file:// ftp:// 之类, 清单被篡改时别让它去读本地文件
    return isinstance(url, str) and url.startswith("https://")


def parse_manifest(data):
    """官网 latest.json(deploy/mirror/mirror_sync.py 生成)→ UpdateInfo. 格式不对抛 UpdateError."""
    try:
        if data.get("schema") != 1:
            raise UpdateError(f"latest.json 的 schema {data.get('schema')!r} 不认识")
        win = data["win64"]
        urls = tuple(u for u in (win.get("url"), win.get("github_url")) if _is_https(u))
        size = win.get("size")
        info = UpdateInfo(
            version=data["version"], notes=str(data.get("notes") or ""),
            published_at=str(data.get("published_at") or ""), urls=urls,
            sha256=win.get("sha256") or None, signature=win["signature"],
            size=size if isinstance(size, int) else None, source="官网")
    except UpdateError:
        raise
    except (AttributeError, KeyError, TypeError) as e:
        raise UpdateError(f"latest.json 格式不对: {e!r}") from e
    if parse_version(info.version) is None:
        raise UpdateError(f"latest.json 里的版本号 {info.version!r} 不是 X.Y.Z")
    if not info.urls:
        raise UpdateError("latest.json 里没有 https 下载地址")
    if not isinstance(info.signature, str) or not info.signature:
        raise UpdateError("latest.json 里没有签名")
    return info


def info_from_github_release(release, fetch_text):
    """GitHub API 的 releases/latest 响应 → UpdateInfo. 要求 tag 是 vX.Y.Z, 并且带
    florr-auto-farm-vX.Y.Z-win64.zip 和同名 .sig 两个附件; .sig 的内容用 fetch_text 取."""
    tag = release.get("tag_name") if isinstance(release, dict) else None
    m = _TAG_RE.match(tag or "")
    if not m:
        raise UpdateError(f"GitHub 最新 Release 的 tag {tag!r} 不是 vX.Y.Z")
    ver = m.group(1)
    name = asset_name(ver)
    assets = {a.get("name"): a for a in release.get("assets") or [] if isinstance(a, dict)}
    zip_asset, sig_asset = assets.get(name), assets.get(name + ".sig")
    if zip_asset is None or sig_asset is None:
        raise UpdateError(f"GitHub 最新 Release {tag} 里没有 {name} 和 {name}.sig")
    zip_url, sig_url = zip_asset.get("browser_download_url"), sig_asset.get("browser_download_url")
    if not _is_https(zip_url) or not _is_https(sig_url):
        raise UpdateError(f"GitHub 最新 Release {tag} 的下载地址不是 https")
    digest = zip_asset.get("digest") or ""
    size = zip_asset.get("size")
    return UpdateInfo(
        version=ver, notes=str(release.get("body") or ""),
        published_at=str(release.get("published_at") or ""),
        urls=(zip_url,),
        sha256=digest[len("sha256:"):] if digest.startswith("sha256:") else None,
        signature=fetch_text(sig_url).strip(),
        size=size if isinstance(size, int) else None, source="GitHub")


def _open(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT)


def fetch_bytes(url, limit=_JSON_LIMIT, timeout=15):
    with _open(url, timeout) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise UpdateError(f"{url} 的响应太大")
    return data


def fetch_json(url):
    return json.loads(fetch_bytes(url).decode("utf-8"))


def fetch_text(url):
    return fetch_bytes(url).decode("utf-8")


def check_latest(current=version.__version__, fetch_json=fetch_json, fetch_text=fetch_text):
    """有比 current 新的版本就返回 UpdateInfo, 没有返回 None. 官网能答复就以官网为准,
    不再问 GitHub; 两边都失败时抛 UpdateError, 消息里带两边各自的原因."""
    sources = (
        ("官网", lambda: parse_manifest(fetch_json(MANIFEST_URL))),
        ("GitHub", lambda: info_from_github_release(fetch_json(GITHUB_LATEST_API), fetch_text)),
    )
    errors = []
    for name, get in sources:
        try:
            info = get()
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        return info if is_newer(info.version, current) else None
    raise UpdateError("检查更新失败 —— " + "; ".join(errors))


def download(info, dest, progress_cb=None, opener=None):
    """按 info.urls 的顺序下载到 dest, 一个失败换下一个. 返回文件的 sha256(hex).
    progress_cb(已下载字节, 总字节) —— 总字节未知时是 0."""
    opener = opener or (lambda url: _open(url, 30))
    errors = []
    for url in info.urls:
        try:
            return _download_one(opener, url, dest, info.size, progress_cb)
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise UpdateError("下载失败 —— " + "; ".join(errors))


def _download_one(opener, url, dest, expected_size, progress_cb):
    # 先写 .part, 完整下完再改名 —— 半截文件不会冒充成完整的包
    part = f"{dest}.part"
    h = hashlib.sha256()
    done = 0
    # 比发布信息里的大小还多就不用再下了(肯定不是那个包); 没给大小时也不能无限下, 把磁盘写满
    limit = expected_size or _DOWNLOAD_LIMIT
    try:
        with opener(url) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0) or expected_size or 0
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                if done + len(chunk) > limit:
                    raise UpdateError(f"下载的数据超过了 {limit} 字节, 已中止")
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)
        if total and done != total:
            raise UpdateError(f"下载不完整: {done}/{total} 字节")
        os.replace(part, dest)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    return h.hexdigest()


# ---- 安装: 解压 → PowerShell 替换 → 重启 ----

APP_DIR_NAME = "florr-auto-pathing"     # zip 里唯一的顶层目录, 也是 exe 的名字
EXE_NAME = APP_DIR_NAME + ".exe"
UPDATE_DIR = ".update"                  # 安装目录下的暂存目录(和安装目录同一个磁盘, 改名是原子的)
DOWNLOAD_NAME = ".update-download.zip"
OLD_SUFFIX = ".old-update"              # swap.ps1 里写死了同样的后缀
FAILED_SUFFIX = ".failed-update"        # 同上: 回滚时删不掉的新版本文件改成这个名字让位
LOG_NAME = "update.log"                 # 同上: swap.ps1 写在安装目录里的日志

# 不能用 DETACHED_PROCESS: 没有控制台的 Windows PowerShell 5.1 一行脚本都不跑就 exit 0
# (Windows CI 实测 2026-09-27, v1.0.1 实机每次"更新脚本没能启动"就是它)。
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

# 程序退出后由它替换文件. 只能有 ASCII: Windows PowerShell 5.1 按系统代码页读没有 BOM 的脚本.
# 失败(任何一步)就回滚并启动旧版; 等不到程序退出就什么都不动.
# 处理顺序固定: _internal 最先、exe 最后、其余按名字 —— exe 没换之前出错, 旧 exe 总还在原处.
# 成功时 *.old-update 留着, 新版本真正起来后由 cleanup_leftovers() 删.
# update.log 里的 "update start: pid=" / "swap ok" / "swap failed:" / "timeout:" 几行由
# wait_for_swap_start() 和 last_update_problem() 解析, 改措辞要两边一起改.
SWAP_PS1 = r"""
param(
    [Parameter(Mandatory = $true)][int]$WaitPid,
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$StagedDir,
    [Parameter(Mandatory = $true)][string]$ExeName,
    [int]$TimeoutSec = 60
)
# Written by updater.py (florr-auto-farm). Keep this script pure ASCII.
$ErrorActionPreference = 'Stop'
$log = Join-Path $InstallDir 'update.log'

# Logging must never stop the swap (log locked, read-only, not a file, ...).
function Log([string]$msg) {
    try {
        $line = '{0} {1}' -f (Get-Date -Format 's'), $msg
        Add-Content -LiteralPath $log -Encoding UTF8 -Value $line
    } catch { }
}

function Retry([scriptblock]$action) {
    for ($i = 1; $i -le 10; $i++) {
        try { & $action; return }
        catch {
            if ($i -eq 10) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}

function Launch {
    $exe = Join-Path $InstallDir $ExeName
    try {
        Start-Process -FilePath $exe -WorkingDirectory $InstallDir | Out-Null
        Log "launched $exe"
    } catch {
        Log "launch failed: $($_.Exception.Message)"
    }
}

Log "update start: pid=$WaitPid staged=$StagedDir"
$proc = Get-Process -Id $WaitPid -ErrorAction SilentlyContinue
if ($proc -and -not $proc.WaitForExit($TimeoutSec * 1000)) {
    Log "timeout: pid $WaitPid still running after $TimeoutSec s, nothing changed"
    exit 2
}

$moved = @()
$placed = @()
try {
    if (-not (Test-Path -LiteralPath $StagedDir -PathType Container)) {
        throw "staged dir not found: $StagedDir"
    }
    $names = @(Get-ChildItem -LiteralPath $StagedDir -Force | ForEach-Object { $_.Name })
    if ($names -notcontains $ExeName) { throw "staged dir has no $ExeName" }
    # Fixed order: _internal first, the exe last, everything else by name.
    $items = @()
    if ($names -contains '_internal') { $items += '_internal' }
    $items += @($names | Where-Object { $_ -ne '_internal' -and $_ -ne $ExeName } | Sort-Object)
    $items += $ExeName

    foreach ($name in $items) {
        $target = Join-Path $InstallDir $name
        $backup = "$target.old-update"
        if (Test-Path -LiteralPath $backup) { Retry { Remove-Item -LiteralPath $backup -Recurse -Force } }
        if (Test-Path -LiteralPath $target) {
            Retry { Rename-Item -LiteralPath $target -NewName "$name.old-update" }
            $moved += $name
        }
        Retry { Move-Item -LiteralPath (Join-Path $StagedDir $name) -Destination $target }
        $placed += $name
        Log "placed $name"
    }
} catch {
    Log "swap failed: $($_.Exception.Message); rolling back"
    foreach ($name in $placed) {
        $target = Join-Path $InstallDir $name
        try {
            Retry { Remove-Item -LiteralPath $target -Recurse -Force }
        } catch {
            # Could not delete the new item: move it aside so the old one can come back.
            try {
                $aside = "$target.failed-update"
                if (Test-Path -LiteralPath $aside) { Retry { Remove-Item -LiteralPath $aside -Recurse -Force } }
                Retry { Rename-Item -LiteralPath $target -NewName "$name.failed-update" }
                Log "rollback: could not remove new $name, renamed it to $name.failed-update"
            } catch {
                Log "rollback: could not remove new $name"
            }
        }
    }
    foreach ($name in $moved) {
        try { Retry { Rename-Item -LiteralPath (Join-Path $InstallDir "$name.old-update") -NewName $name } }
        catch { Log "rollback: could not restore $name" }
    }
    Launch
    exit 1
}

Log "swap ok"
Launch
exit 0
"""


def enabled():
    """只有 Windows 打包版能自己更新; 源码运行用 git pull."""
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def install_dir():
    """打包版 exe 所在的目录. 源码运行时返回本文件所在目录(只用于显示和测试)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _safe_parts(name):
    parts = PurePosixPath(name.replace("\\", "/")).parts
    # 任何一段带冒号都不行: 开头是盘符, 中间是盘符相对路径(C:evil)或 NTFS 备用数据流(a:stream)
    if not parts or parts[0] == "/" or ".." in parts or any(":" in p for p in parts):
        raise UpdateError(f"更新包里有不安全的路径: {name!r}")
    return parts


def stage(zip_path, install):
    """把更新包解压到 install/.update/, 返回新版本的目录 install/.update/florr-auto-pathing.
    自己逐个条目解压(不用 extractall): 反斜杠和正斜杠都当目录分隔符, 并且先把所有路径
    检查一遍, 有 .. / 绝对路径 / 盘符就整包拒绝, 一个文件都不写."""
    install = Path(install)
    root = install / UPDATE_DIR
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise UpdateError("更新包不是有效的 zip 文件") from e
    with zf:
        entries = [(info, _safe_parts(info.filename)) for info in zf.infolist()]
        tops = {parts[0] for _info, parts in entries}
        if tops != {APP_DIR_NAME}:
            raise UpdateError(f"更新包结构不对: 顶层应当只有 {APP_DIR_NAME}/, 实际是 {sorted(tops)}")
        for info, parts in entries:
            target = root.joinpath(*parts)
            if info.is_dir() or info.filename.endswith(("/", "\\")):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        # 全部写完再逐个核对: 磁盘满、杀毒软件截断或隔离了某个文件, 都不能带着缺损的新版本去替换
        for info, parts in entries:
            if info.is_dir() or info.filename.endswith(("/", "\\")):
                continue
            try:
                ok = root.joinpath(*parts).stat().st_size == info.file_size
            except OSError:
                ok = False
            if not ok:
                raise UpdateError(f"更新包解压不完整: {info.filename}")
    staged = root / APP_DIR_NAME
    if not (staged / EXE_NAME).is_file() or not (staged / "_internal").is_dir():
        raise UpdateError(f"更新包里缺少 {EXE_NAME} 或 _internal/")
    return staged


def write_swap_script(install):
    script = Path(install) / UPDATE_DIR / "swap.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig 带 BOM: 脚本本身只有 ASCII, BOM 是给 PowerShell 5.1 的双保险
    script.write_text(SWAP_PS1, encoding="utf-8-sig")
    return script


def _powershell_exe():
    # 绝对路径, 不靠 PATH 搜索: 当前目录 / PATH 里别的 powershell.exe 冒充不了
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def swap_command(script, pid, install, staged, timeout_sec=60):
    return [_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(script),
            "-WaitPid", str(pid), "-InstallDir", str(install), "-StagedDir", str(staged),
            "-ExeName", EXE_NAME, "-TimeoutSec", str(timeout_sec)]


def _swap_env():
    """替换脚本(以及它最后启动的新 exe)的环境. PyInstaller 引导程序往环境里放的 _PYI_* /
    _MEIPASS2 要去掉, 否则新 exe 会以为自己是旧进程的子进程, 去用旧进程的目录."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("_PYI") and k.upper() != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def launch_swap(install, staged, pid):
    """起替换脚本(单独进程组 + 隐藏控制台, 不开窗口). 调用方随后要退出程序, 脚本等到 pid 退出才动手."""
    script = write_swap_script(install)
    subprocess.Popen(swap_command(script, pid, install, staged),
                     creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, cwd=str(install),
                     env=_swap_env())


def _read_log(install):
    """update.log 的全文; 不存在或读不了(被占用、是个目录...)返回 None."""
    try:
        return (Path(install) / LOG_NAME).read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, ValueError):
        return None


def wait_for_swap_start(install, pid, timeout=10.0, poll=0.2, sleep=time.sleep,
                        clock=time.monotonic):
    """launch_swap() 之后等替换脚本真的跑起来(它的第一件事是往 update.log 写
    "update start: pid=<pid>"). 等到返回 True; timeout 秒还没有返回 False —— 多半是
    powershell 被杀毒软件拦了, 这时程序不能退出, 否则就没人把它重新打开了."""
    pattern = re.compile(rf"update start: pid={int(pid)}(?!\d)")
    deadline = clock() + timeout
    while True:
        text = _read_log(install)
        if text and pattern.search(text):
            return True
        if clock() >= deadline:
            return False
        sleep(poll)


_LOG_TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\s+")
_DIED = "替换脚本中途退出"


def last_update_problem(install):
    """上一次更新没成功的话返回原因(给用户看), 成功了 / 没更新过 / 已经告诉过用户了返回 None.
    只看 update.log 里最后一个 "update start:" 之后的行."""
    text = _read_log(install)
    if text is None:
        return None
    lines = [_LOG_TIME_RE.sub("", ln.replace("\ufeff", "").strip())
             for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    # 去掉时间戳后按行首判断 —— "placed timeout.dll" 这种文件名不能被当成超时
    starts = [i for i, ln in enumerate(lines) if ln.startswith("update start:")]
    if not starts:
        return None
    after = lines[starts[-1] + 1:]
    if any(ln.startswith("acknowledged") for ln in after):
        return None
    for ln in after:
        if ln.startswith("swap failed"):
            msg = ln[len("swap failed"):].lstrip(": ").removesuffix("; rolling back").strip()
            return msg or "替换失败, 已回滚到旧版本"
        if ln.startswith("timeout"):
            return ln
        if ln.startswith("swap ok"):
            return None
    return _DIED             # 只有开头没有结尾: 脚本被杀了 / 机器断电了


def acknowledge_update_problem(install):
    """在 update.log 末尾记一笔「已经告诉过用户了」, 下次启动不再重复提示. 写不了就算了."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} acknowledged\r\n"
    try:
        with open(Path(install) / LOG_NAME, "a", encoding="utf-8", newline="") as f:
            f.write(line)
    except OSError:
        pass


def cleanup_leftovers(install):
    """删掉上次更新留下的东西: *.old-update、*.failed-update、.update/、下载了一半或没删掉的
    zip. 删不掉就算了(可能还被占用), 下次启动再删. update.log 留着, 它是更新的记录."""
    install = Path(install)
    targets = [*install.glob("*" + OLD_SUFFIX), *install.glob("*" + FAILED_SUFFIX),
               install / UPDATE_DIR, install / DOWNLOAD_NAME, install / (DOWNLOAD_NAME + ".part")]
    for p in targets:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            try:
                p.unlink()
            except OSError:
                pass
