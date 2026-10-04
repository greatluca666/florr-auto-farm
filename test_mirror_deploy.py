import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
MIRROR = ROOT / "deploy" / "mirror"
sys.path.insert(0, str(MIRROR))
import mirror_sync  # noqa: E402


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("bash") is None, reason="没有 bash")
@pytest.mark.parametrize("script", ["deploy/mirror/install.sh", "deploy/mirror/fetch_afk.sh", "deploy/ubuntu/setup.sh"])
def test_shell_scripts_parse(script):
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


def test_setup_sh_imports_sites_d_so_the_official_site_survives_reruns():
    text = _read("deploy/ubuntu/setup.sh")
    heredoc = text.split("cat > /etc/caddy/Caddyfile <<CADDYEOF", 1)[1].split("\nCADDYEOF", 1)[0]
    assert "import /etc/caddy/sites.d/*.caddy" in heredoc
    assert "install -d -m 755 /etc/caddy/sites.d" in text


def test_service_environment_matches_mirror_sync_defaults():
    env = {}
    for line in _read("deploy/mirror/florr-mirror.service").splitlines():
        if line.startswith("Environment="):
            k, v = line[len("Environment="):].split("=", 1)
            env[k] = v
    assert env["FLORR_MIRROR_REPO"] == mirror_sync.REPO
    assert env["FLORR_MIRROR_DATA"] == str(mirror_sync.DATA_DIR)
    assert env["FLORR_MIRROR_SITE_REPO"] == str(mirror_sync.SITE_REPO_DIR)
    assert env["FLORR_MIRROR_PUBLIC_BASE"] == mirror_sync.PUBLIC_BASE


def test_timer_runs_every_ten_minutes():
    timer = _read("deploy/mirror/florr-mirror.timer")
    assert "OnUnitActiveSec=10min" in timer
    assert "Unit=florr-mirror.service" in timer


def test_caddy_site_serves_mirror_paths_with_the_right_cache_rules():
    c = _read("deploy/mirror/florrfarm.caddy")
    body = c[c.index("florrfarm.cc.cd {"):]
    latest = body[body.index("handle /latest.json"):body.index("handle /download/*")]
    downloads = body[body.index("handle /download/*"):body.index("\thandle {")]
    site = body[body.index("\thandle {"):]
    assert f"root * {mirror_sync.DATA_DIR}" in latest and 'Cache-Control "no-cache"' in latest
    assert f"root * {mirror_sync.DATA_DIR}" in downloads
    assert f"root * {mirror_sync.SITE_REPO_DIR}/site" in site


def test_caddy_marks_only_existing_downloads_immutable():
    # 404 也带 immutable 的话, Cloudflare / 浏览器会把「还没同步到的包」的 404 缓存一年
    c = _read("deploy/mirror/florrfarm.caddy")
    body = c[c.index("florrfarm.cc.cd {"):]
    downloads = body[body.index("handle /download/*"):body.index("\thandle {")]
    assert "\t\t@found file\n" in downloads
    assert '\t\theader @found Cache-Control "public, max-age=31536000, immutable"\n' in downloads
    assert 'header Cache-Control "public, max-age=31536000, immutable"' not in c


def test_install_sh_wires_everything_up():
    s = _read("deploy/mirror/install.sh")
    for needle in ("systemctl enable --now florr-mirror.timer",
                   "/etc/caddy/sites.d/florrfarm.caddy",
                   "grep -qF 'import /etc/caddy/sites.d/*.caddy' /etc/caddy/Caddyfile",
                   "if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then",
                   "systemctl reload caddy"):
        assert needle in s, needle
    backup = s.index("cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-florrfarm")
    assert backup < s.index('install -m 644 "$HERE/florrfarm.caddy"')
    assert s.index("if ! caddy validate") < s.index("systemctl reload caddy")


def _run_caddy_section(tmp_path, validate_ok, caddyfile="{\n}\n"):
    """把 install.sh 里配 Caddy 的那一段拿出来, /etc/caddy 换成临时目录、caddy / systemctl
    换成记录调用的假命令, 真跑一遍."""
    s = _read("deploy/mirror/install.sh")
    section = s[s.index('echo "==> 配 Caddy 站点'):s.index('echo "==> 先同步一次"')]
    etc = tmp_path / "etc-caddy"
    etc.mkdir()
    (etc / "Caddyfile").write_text(caddyfile, encoding="utf-8")
    calls = tmp_path / "calls.log"
    script = "\n".join([
        "set -euo pipefail",
        f'HERE="{MIRROR}"',
        f'caddy() {{ echo "caddy $*" >> "{calls}"; [ {1 if validate_ok else 0} -eq 1 ]; }}',
        f'systemctl() {{ echo "systemctl $*" >> "{calls}"; }}',
        section.replace("/etc/caddy", str(etc)),
    ])
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r, etc, calls.read_text() if calls.exists() else ""


@pytest.mark.skipif(shutil.which("bash") is None, reason="没有 bash")
def test_install_sh_rolls_back_caddy_config_when_validation_fails(tmp_path):
    r, etc, calls = _run_caddy_section(tmp_path, validate_ok=False)
    assert r.returncode == 1, r.stdout + r.stderr
    assert not (etc / "sites.d" / "florrfarm.caddy").exists()
    assert (etc / "Caddyfile").read_text(encoding="utf-8") == "{\n}\n"   # import 行也撤掉了
    assert "caddy validate" in calls
    assert "systemctl reload caddy" not in calls
    assert "还原" in r.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="没有 bash")
def test_install_sh_reloads_caddy_only_after_validation_passes(tmp_path):
    r, etc, calls = _run_caddy_section(tmp_path, validate_ok=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (etc / "sites.d" / "florrfarm.caddy").read_text(encoding="utf-8") == _read(
        "deploy/mirror/florrfarm.caddy")
    assert f"import {etc}/sites.d/*.caddy" in (etc / "Caddyfile").read_text(encoding="utf-8")
    lines = calls.splitlines()
    assert lines[-1] == "systemctl reload caddy"
    assert lines[-2].startswith("caddy validate")


def test_service_umask_keeps_published_dirs_readable_by_caddy():
    # mirror_sync.py 建目录(download/、site-repo/ 的 git 检出)不单独 chmod, 靠这里的 umask 让
    # Caddy(另一个用户)能进目录读文件; 改严了网站和下载会 403.
    assert "UMask=0022" in _read("deploy/mirror/florr-mirror.service").splitlines()


@pytest.mark.skipif(shutil.which("bash") is None, reason="没有 bash")
def test_install_sh_puts_a_deny_all_admin_placeholder_until_stats_sets_a_password(tmp_path):
    # florrfarm.caddy 的 /admin import 这个文件: 没有它 Caddy 校验不过; 有了也只能是 403, 不能放行
    r, etc, _ = _run_caddy_section(tmp_path, validate_ok=True)
    assert r.returncode == 0, r.stdout + r.stderr
    auth = (etc / "florrfarm-admin.auth").read_text(encoding="utf-8")
    assert auth.startswith("respond ") and auth.rstrip().endswith("403")


@pytest.mark.skipif(shutil.which("bash") is None, reason="没有 bash")
def test_install_sh_keeps_an_existing_admin_password_file(tmp_path):
    etc = tmp_path / "etc-caddy"
    real = "basic_auth {\n\tadmin $2a$14$hash\n}\n"

    def run():
        s = _read("deploy/mirror/install.sh")
        section = s[s.index('echo "==> 配 Caddy 站点'):s.index('echo "==> 先同步一次"')]
        script = "\n".join(["set -euo pipefail", f'HERE="{MIRROR}"', "caddy() { :; }",
                            "systemctl() { :; }", section.replace("/etc/caddy", str(etc))])
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    etc.mkdir()
    (etc / "Caddyfile").write_text("{\n}\n", encoding="utf-8")
    (etc / "florrfarm-admin.auth").write_text(real, encoding="utf-8")
    r = run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert (etc / "florrfarm-admin.auth").read_text(encoding="utf-8") == real


# ---- florr-auto-afk 固定版本的加速下载 ----

AFK_SHA256 = "74488ef58966d123ace6d19ebb11c05d7ac8ee992abd949289714a8a866e7d74"


def test_caddy_serves_afk_package_outside_the_pruned_download_dir():
    # download/ 里不属于 florr-auto-farm 的文件会被 mirror_sync.prune 删掉, afk 包必须在别处
    c = _read("deploy/mirror/florrfarm.caddy")
    body = c[c.index("florrfarm.cc.cd {"):]
    afk = body[body.index("handle /afk/*"):body.index("handle /latest.json")]
    assert f"root * {mirror_sync.DATA_DIR}" in afk
    assert "\t\t@found file\n" in afk
    assert '\t\theader @found Cache-Control "public, max-age=31536000, immutable"\n' in afk
    assert "file_server" in afk
    assert "/var/lib/florrfarm/afk" in _read("deploy/mirror/fetch_afk.sh")


def test_install_sh_creates_afk_dir_and_fetches_the_package():
    s = _read("deploy/mirror/install.sh")
    assert "/var/lib/florrfarm/afk" in s
    assert 'bash "$HERE/fetch_afk.sh"' in s


def test_fetch_afk_pins_the_sha256_of_the_github_asset():
    s = _read("deploy/mirror/fetch_afk.sh")
    assert f'SHA256="{AFK_SHA256}"' in s
    assert 'FILE="florr-auto-afk-v1.1.1-auto.zip"' in s
    assert 'URL="https://github.com/greatluca666/florr-auto-afk/releases/download/v1.1.1/${FILE}"' in s


def _fetch_afk_env(tmp_path, curl_body):
    """假 curl(把 curl_body 写进 -o 指定的文件) + 在没有 sha256sum 的机器(macOS)上用 shasum 顶替."""
    stub = tmp_path / "bin"
    stub.mkdir()
    curl = stub / "curl"
    curl.write_text('#!/usr/bin/env bash\nwhile [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
                    f'printf %s {curl_body!r} > "$out"\n', encoding="utf-8")
    curl.chmod(0o755)
    if shutil.which("sha256sum") is None:
        sha = stub / "sha256sum"
        sha.write_text('#!/usr/bin/env bash\nexec shasum -a 256 "$@"\n', encoding="utf-8")
        sha.chmod(0o755)
    import os
    env = dict(os.environ, PATH=f"{stub}{os.pathsep}{os.environ['PATH']}",
               FLORR_AFK_DIR=str(tmp_path / "afk"))
    return env


@pytest.mark.skipif(shutil.which("bash") is None, reason="没有 bash")
def test_fetch_afk_refuses_a_download_with_the_wrong_hash(tmp_path):
    env = _fetch_afk_env(tmp_path, "not the real zip")
    r = subprocess.run(["bash", str(MIRROR / "fetch_afk.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "SHA-256" in r.stderr
    assert list((tmp_path / "afk").iterdir()) == []      # 坏包和 .part 都不留


@pytest.mark.skipif(shutil.which("bash") is None, reason="没有 bash")
def test_fetch_afk_keeps_a_verified_package_without_downloading_again(tmp_path):
    # 把 SHA256 换成假内容的哈希来跑通成功路径; 同时确认第二次不再调用 curl
    import hashlib
    body = "pretend zip"
    script = _read("deploy/mirror/fetch_afk.sh").replace(
        AFK_SHA256, hashlib.sha256(body.encode()).hexdigest())
    patched = tmp_path / "fetch_afk.sh"
    patched.write_text(script, encoding="utf-8")
    env = _fetch_afk_env(tmp_path, body)
    r = subprocess.run(["bash", str(patched)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    pkg = tmp_path / "afk" / "florr-auto-afk-v1.1.1-auto.zip"
    assert pkg.read_text(encoding="utf-8") == body
    assert not (tmp_path / "afk" / "florr-auto-afk-v1.1.1-auto.zip.part").exists()
    (tmp_path / "bin" / "curl").write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
    r2 = subprocess.run(["bash", str(patched)], capture_output=True, text=True, env=env)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "不用再拉" in r2.stdout
