#!/usr/bin/env python3
"""florrfarm.cc.cd 的安装包镜像同步. 只用标准库(服务器上没装 cryptography, 所以不 import
updater.py). 在 140 上由 florr-mirror.timer 每 10 分钟跑一次, 装法见 install.sh.

1. 从 GitHub API 取最近的 Release, 挑出最近 3 个合格的正式版: 不是草稿/预发布, tag 是
   vX.Y.Z, 同时带 florr-auto-farm-vX.Y.Z-win64.zip 和 .zip.sig 两个附件;
2. 本地没有的包下载下来, 按 GitHub 给的 sha256 digest 校验 —— 没有 digest 或对不上
   都算失败, 整轮不发布;
3. 原子写 latest.json(先写临时文件再改名), 然后删掉不再引用的旧包;
4. 把公开仓库的 site/ 拉到最新(稀疏检出), Caddy 直接把它当网站根目录.

任何一步失败都以非零退出码结束; 已经对外的 latest.json 和包保持原样.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = os.environ.get("FLORR_MIRROR_REPO", "greatluca666/florr-auto-farm")
DATA_DIR = Path(os.environ.get("FLORR_MIRROR_DATA", "/var/lib/florrfarm"))
SITE_REPO_DIR = Path(os.environ.get("FLORR_MIRROR_SITE_REPO", "/var/lib/florrfarm/site-repo"))
PUBLIC_BASE = os.environ.get("FLORR_MIRROR_PUBLIC_BASE", "https://florrfarm.cc.cd").rstrip("/")
KEEP = 3
PRODUCT = "florr-auto-farm"      # 和 updater.PRODUCT / updater.asset_name 必须一致
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_UA = "florrfarm-mirror"


class MirrorError(Exception):
    pass


def log(msg):
    print(msg, flush=True)       # systemd 把 stdout 收进 journal


def asset_name(ver):
    return f"{PRODUCT}-v{ver}-win64.zip"


def _get(url, timeout=60, accept=None):
    headers = {"User-Agent": _UA}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def fetch_json(url):
    with _get(url, accept="application/vnd.github+json") as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_text(url):
    with _get(url) as resp:
        return resp.read().decode("utf-8")


def download_file(url, dest):
    """下载到 dest, 返回 sha256 hex."""
    h = hashlib.sha256()
    with _get(url, timeout=120) as resp, open(dest, "wb") as f:
        for chunk in iter(lambda: resp.read(1024 * 1024), b""):
            f.write(chunk)
            h.update(chunk)
    return h.hexdigest()


def select_releases(releases, keep=KEEP):
    chosen = []
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        m = _TAG_RE.match(rel.get("tag_name") or "")
        if not m:
            continue
        ver = ".".join(m.groups())
        assets = {a.get("name"): a for a in rel.get("assets") or [] if isinstance(a, dict)}
        zip_asset = assets.get(asset_name(ver))
        sig_asset = assets.get(asset_name(ver) + ".sig")
        if zip_asset is None or sig_asset is None:
            continue
        chosen.append({"version": ver, "key": tuple(int(x) for x in m.groups()),
                       "published_at": rel.get("published_at") or "",
                       "notes": rel.get("body") or "", "zip": zip_asset, "sig": sig_asset})
    chosen.sort(key=lambda r: r["key"], reverse=True)
    return chosen[:keep]


def write_text_atomic(path, text):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)     # mkstemp 建出来是 0600, Caddy 以别的用户身份读不了
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path, data):
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def sync_packages(chosen, download_dir, download=download_file, fetch_sig=fetch_text):
    """确保 chosen 里每个版本的 zip 都在 download_dir 且校验过, 返回写 latest.json 用的条目
    (从新到旧). 任何一个失败都抛 MirrorError —— 宁可这一轮什么都不发布."""
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for rel in chosen:
        name = asset_name(rel["version"])
        digest = rel["zip"].get("digest") or ""
        if not digest.startswith("sha256:"):
            raise MirrorError(f"{name}: GitHub 没给 sha256 digest, 不发布")
        expected = digest[len("sha256:"):].lower()
        final = download_dir / name
        stamp = download_dir / (name + ".sha256")
        sig_path = download_dir / (name + ".sig")
        verified = (final.is_file() and stamp.is_file()
                    and stamp.read_text(encoding="utf-8").strip() == expected)
        if not verified:
            tmp = download_dir / f".tmp-{name}"
            try:
                got = download(rel["zip"]["browser_download_url"], tmp).lower()
                if got != expected:
                    raise MirrorError(f"{name}: sha256 对不上(GitHub {expected}, 下载到 {got})")
                # zip 重新下了, 签名也一定重新取(同名附件在 GitHub 上被换过, 旧 .sig 就对不上了).
                # 先取到签名再替换 zip: 取不到就整轮失败, 旧包和旧签名原样留着
                sig = fetch_sig(rel["sig"]["browser_download_url"]).strip()
                os.chmod(tmp, 0o644)
                os.replace(tmp, final)
            finally:
                if tmp.exists():
                    tmp.unlink()
            write_text_atomic(sig_path, sig + "\n")
            # .sha256 最后写: 它是「zip 和 .sig 都齐了」的标记, 中途断掉下一轮会整个重来
            write_text_atomic(stamp, expected + "\n")
            log(f"已镜像 {name}")
        elif not sig_path.is_file():
            write_text_atomic(sig_path, fetch_sig(rel["sig"]["browser_download_url"]).strip() + "\n")
        entries.append({**rel, "file": name, "sha256": expected, "size": final.stat().st_size,
                        "signature": sig_path.read_text(encoding="utf-8").strip()})
    return entries


def _win64_of(entry, public_base):
    return {
        "file": entry["file"],
        "url": f"{public_base}/download/{entry['file']}",
        "github_url": entry["zip"]["browser_download_url"],
        "size": entry["size"],
        "sha256": entry["sha256"],
        "signature": entry["signature"],
    }


def build_manifest(entries, public_base=PUBLIC_BASE):
    cur = entries[0]
    return {
        "schema": 1,
        "version": cur["version"],
        "published_at": cur["published_at"],
        "notes": cur["notes"],
        "win64": _win64_of(cur, public_base),
        # 每条历史都带自己的 win64(不只是最新版), 网站的更新记录才能给每个版本自己的
        # 一键下载链接, 跟 GitHub Release 页面每条 Release 自带下载链接一样.
        "history": [{"version": e["version"], "published_at": e["published_at"],
                     "notes": e["notes"], "win64": _win64_of(e, public_base)} for e in entries],
    }


def prune(download_dir, keep_names):
    keep = set()
    for n in keep_names:
        keep |= {n, n + ".sig", n + ".sha256"}
    for p in Path(download_dir).iterdir():
        if p.is_file() and p.name not in keep:
            p.unlink()
            log(f"删掉 {p.name}")


def update_site(repo_dir=SITE_REPO_DIR, repo=REPO, run=subprocess.run):
    """公开仓库只检出 site/(外加根目录的文件, git 稀疏检出的 cone 模式总会带上它们,
    但 Caddy 只把 site/ 当网站根目录, 根目录的文件不会被访问到)."""
    repo_dir = Path(repo_dir)
    if not (repo_dir / ".git").is_dir():
        run(["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
             f"https://github.com/{repo}.git", str(repo_dir)], check=True)
        run(["git", "-C", str(repo_dir), "sparse-checkout", "set", "site"], check=True)
        return
    # 这个目录只归本脚本管: 直接对齐到远端, 不管公开仓库有没有改写过历史
    run(["git", "-C", str(repo_dir), "fetch", "--depth", "1", "origin", "main"], check=True)
    run(["git", "-C", str(repo_dir), "reset", "--hard", "FETCH_HEAD"], check=True)


def run_once(fetch=fetch_json, download=download_file, fetch_sig=fetch_text, site=update_site,
             data_dir=DATA_DIR, repo=REPO, public_base=PUBLIC_BASE):
    data_dir = Path(data_dir)
    ok = True
    try:
        chosen = select_releases(fetch(f"https://api.github.com/repos/{repo}/releases?per_page=10"))
        if not chosen:
            log("没有合格的正式版 Release(tag vX.Y.Z + zip + sig), latest.json 保持不变")
        else:
            entries = sync_packages(chosen, data_dir / "download", download, fetch_sig)
            write_json_atomic(data_dir / "latest.json", build_manifest(entries, public_base))
            prune(data_dir / "download", [e["file"] for e in entries])
            log(f"latest.json → v{entries[0]['version']}")
    except Exception as e:
        log(f"同步安装包失败: {e}")
        ok = False
    try:
        site()
    except Exception as e:
        log(f"更新网站失败: {e}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run_once())
