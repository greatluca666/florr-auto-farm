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
@pytest.mark.parametrize("script", ["deploy/mirror/install.sh", "deploy/ubuntu/setup.sh"])
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
