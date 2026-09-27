import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "deploy" / "mirror"))
import mirror_sync  # noqa: E402

import updater  # noqa: E402


def _data(ver):
    return f"zip-{ver}".encode()


def _sha(ver):
    return hashlib.sha256(_data(ver)).hexdigest()


def _rel(ver, *, draft=False, pre=False, tag=None, names=None, digest=True):
    name = f"florr-auto-farm-v{ver}-win64.zip"
    names = names if names is not None else [name, name + ".sig"]
    assets = [{"name": n, "browser_download_url": f"https://gh/{n}", "size": 1,
               "digest": (f"sha256:{_sha(ver)}" if digest else None) if n.endswith(".zip")
               else None} for n in names]
    return {"tag_name": tag or f"v{ver}", "draft": draft, "prerelease": pre,
            "published_at": f"2026-10-01T00:00:{ver.split('.')[-1].zfill(2)}Z",
            "body": f"notes {ver}", "assets": assets}


class FakeGitHub:
    def __init__(self, releases):
        self.releases = releases
        self.downloads = []

    def fetch_json(self, url):
        assert url == "https://api.github.com/repos/o/r/releases?per_page=10"
        return self.releases

    def download(self, url, dest):
        self.downloads.append(url)
        ver = url.split("-v")[1].split("-win64")[0]
        Path(dest).write_bytes(_data(ver))
        return _sha(ver)

    def fetch_sig(self, url):
        return "sig-" + url.rsplit("/", 1)[1] + "\n"


def _run(tmp_path, gh, site=lambda: None):
    return mirror_sync.run_once(fetch=gh.fetch_json, download=gh.download,
                                fetch_sig=gh.fetch_sig, site=site, data_dir=tmp_path,
                                repo="o/r", public_base="https://site")


def test_select_releases_filters_and_sorts_numerically():
    releases = [
        _rel("1.9.0"), _rel("1.10.0"), _rel("1.2.0"), _rel("1.1.0"),
        _rel("2.0.0", draft=True), _rel("3.0.0", pre=True),
        _rel("1.0.0", tag="1"),                                    # 手动传的那种 tag
        _rel("4.0.0", names=["florr-auto-farm-v4.0.0-win64.zip"]),  # 没有 .sig
        _rel("5.0.0", names=["florr-auto-pathing.zip"]),
    ]
    assert [r["version"] for r in mirror_sync.select_releases(releases)] == [
        "1.10.0", "1.9.0", "1.2.0"]


def test_run_once_mirrors_latest_three_and_writes_manifest(tmp_path):
    gh = FakeGitHub([_rel("1.0.0"), _rel("1.3.0"), _rel("1.1.0"), _rel("1.2.0")])
    assert _run(tmp_path, gh) == 0
    m = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert m["schema"] == 1 and m["version"] == "1.3.0"
    w = m["win64"]
    assert w["file"] == "florr-auto-farm-v1.3.0-win64.zip"
    assert w["url"] == "https://site/download/florr-auto-farm-v1.3.0-win64.zip"
    assert w["github_url"] == "https://gh/florr-auto-farm-v1.3.0-win64.zip"
    assert w["sha256"] == _sha("1.3.0")
    assert w["size"] == len(_data("1.3.0"))
    assert w["signature"] == "sig-florr-auto-farm-v1.3.0-win64.zip.sig"
    assert [h["version"] for h in m["history"]] == ["1.3.0", "1.2.0", "1.1.0"]
    assert m["history"][0]["notes"] == "notes 1.3.0"
    zips = sorted(p.name for p in (tmp_path / "download").glob("*.zip"))
    assert zips == [f"florr-auto-farm-v{v}-win64.zip" for v in ("1.1.0", "1.2.0", "1.3.0")]


def test_manifest_is_what_the_updater_expects(tmp_path):
    _run(tmp_path, FakeGitHub([_rel("1.3.0")]))
    info = updater.parse_manifest(
        json.loads((tmp_path / "latest.json").read_text(encoding="utf-8")))
    assert info.version == "1.3.0"
    assert info.urls[0] == "https://site/download/florr-auto-farm-v1.3.0-win64.zip"


def test_second_run_reuses_verified_files(tmp_path):
    gh = FakeGitHub([_rel("1.3.0"), _rel("1.2.0")])
    sigs = []
    fetch_sig = gh.fetch_sig
    gh.fetch_sig = lambda url: sigs.append(url) or fetch_sig(url)
    _run(tmp_path, gh)
    _run(tmp_path, gh)
    assert len(gh.downloads) == 2
    assert len(sigs) == 2            # 包没重新下, 签名也不用重新取


def test_redownloaded_zip_also_refetches_its_signature(tmp_path):
    # 同一个版本的附件在 GitHub 上被换掉了(digest 变了): zip 会重新下, .sig 也必须跟着换,
    # 不能让旧签名配着新包发出去
    rel = _rel("1.3.0")
    gh = FakeGitHub([rel])
    assert _run(tmp_path, gh) == 0
    sig_file = tmp_path / "download" / "florr-auto-farm-v1.3.0-win64.zip.sig"
    assert sig_file.read_text(encoding="utf-8").strip() == "sig-florr-auto-farm-v1.3.0-win64.zip.sig"

    new_data = b"rebuilt 1.3.0"
    rel["assets"][0]["digest"] = "sha256:" + hashlib.sha256(new_data).hexdigest()

    def download_new(url, dest):
        gh.downloads.append(url)
        Path(dest).write_bytes(new_data)
        return hashlib.sha256(new_data).hexdigest()

    sigs = []
    gh.download = download_new
    gh.fetch_sig = lambda url: sigs.append(url) or "sig-rebuilt\n"
    assert _run(tmp_path, gh) == 0
    assert len(gh.downloads) == 2
    assert sigs == ["https://gh/florr-auto-farm-v1.3.0-win64.zip.sig"]
    assert sig_file.read_text(encoding="utf-8").strip() == "sig-rebuilt"
    m = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert m["win64"]["signature"] == "sig-rebuilt"
    assert m["win64"]["sha256"] == hashlib.sha256(new_data).hexdigest()


def test_signature_fetch_failure_keeps_the_old_package_and_retries(tmp_path):
    # 新包下好了但签名没取到: 旧包、旧签名、旧 latest.json 都不动, 下一轮重新来
    rel = _rel("1.3.0")
    gh = FakeGitHub([rel])
    _run(tmp_path, gh)
    d = tmp_path / "download"
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    manifest_before = (tmp_path / "latest.json").read_bytes()

    new_data = b"rebuilt 1.3.0"
    rel["assets"][0]["digest"] = "sha256:" + hashlib.sha256(new_data).hexdigest()

    def download_new(url, dest):
        gh.downloads.append(url)
        Path(dest).write_bytes(new_data)
        return hashlib.sha256(new_data).hexdigest()

    def broken_sig(url):
        raise OSError("github down")

    gh.download, gh.fetch_sig = download_new, broken_sig
    assert _run(tmp_path, gh) == 1
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before
    assert (tmp_path / "latest.json").read_bytes() == manifest_before

    gh.fetch_sig = lambda url: "sig-rebuilt\n"
    assert _run(tmp_path, gh) == 0
    assert (d / "florr-auto-farm-v1.3.0-win64.zip").read_bytes() == new_data
    assert (d / "florr-auto-farm-v1.3.0-win64.zip.sig").read_text().strip() == "sig-rebuilt"


def test_digest_mismatch_publishes_nothing(tmp_path):
    (tmp_path / "latest.json").write_text('{"old": true}')
    gh = FakeGitHub([_rel("1.3.0")])

    def evil(url, dest):
        Path(dest).write_bytes(b"evil")
        return hashlib.sha256(b"evil").hexdigest()

    gh.download = evil
    assert _run(tmp_path, gh) == 1
    assert (tmp_path / "latest.json").read_text() == '{"old": true}'
    assert list((tmp_path / "download").iterdir()) == []


def test_release_without_digest_publishes_nothing(tmp_path):
    (tmp_path / "latest.json").write_text('{"old": true}')
    assert _run(tmp_path, FakeGitHub([_rel("1.3.0", digest=False)])) == 1
    assert (tmp_path / "latest.json").read_text() == '{"old": true}'


def test_no_usable_release_keeps_everything(tmp_path):
    assert _run(tmp_path, FakeGitHub([_rel("1.0.0", tag="1")])) == 0
    assert not (tmp_path / "latest.json").exists()


def test_old_versions_and_temp_files_are_pruned(tmp_path):
    d = tmp_path / "download"
    d.mkdir()
    for suffix in ("", ".sig", ".sha256"):
        (d / f"florr-auto-farm-v0.9.0-win64.zip{suffix}").write_text("old")
    (d / ".tmp-florr-auto-farm-v1.3.0-win64.zip").write_text("half")
    _run(tmp_path, FakeGitHub([_rel("1.3.0")]))
    assert sorted(p.name for p in d.iterdir()) == [
        "florr-auto-farm-v1.3.0-win64.zip",
        "florr-auto-farm-v1.3.0-win64.zip.sha256",
        "florr-auto-farm-v1.3.0-win64.zip.sig"]


def test_site_failure_is_reported_but_packages_still_publish(tmp_path):
    def broken_site():
        raise OSError("github down")

    assert _run(tmp_path, FakeGitHub([_rel("1.3.0")]), site=broken_site) == 1
    assert (tmp_path / "latest.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="文件权限只在 POSIX 上有意义")
def test_published_files_are_world_readable(tmp_path):
    _run(tmp_path, FakeGitHub([_rel("1.3.0")]))
    for p in [tmp_path / "latest.json", *(tmp_path / "download").iterdir()]:
        assert p.stat().st_mode & 0o044 == 0o044, p     # Caddy 以别的用户身份读这些文件


def test_update_site_clones_sparse_the_first_time(tmp_path):
    cmds = []
    mirror_sync.update_site(tmp_path / "repo", "o/r", run=lambda cmd, **kw: cmds.append(cmd))
    assert cmds[0][:2] == ["git", "clone"]
    assert "--sparse" in cmds[0]
    assert cmds[0][-2:] == ["https://github.com/o/r.git", str(tmp_path / "repo")]
    assert cmds[1] == ["git", "-C", str(tmp_path / "repo"), "sparse-checkout", "set", "site"]


def test_update_site_fetches_and_resets_later(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cmds = []
    mirror_sync.update_site(repo, "o/r", run=lambda cmd, **kw: cmds.append(cmd))
    assert cmds == [["git", "-C", str(repo), "fetch", "--depth", "1", "origin", "main"],
                    ["git", "-C", str(repo), "reset", "--hard", "FETCH_HEAD"]]
