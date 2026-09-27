import base64
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "deploy" / "release"))
import _stdio  # noqa: E402
import gen_signing_key  # noqa: E402
import sign_release  # noqa: E402

import updater  # noqa: E402


def _pkg(tmp_path, ver="1.2.0"):
    z = tmp_path / updater.asset_name(ver)
    z.write_bytes(b"package-bytes")
    return z


def test_make_keypair_is_consistent():
    seed, pub = gen_signing_key.make_keypair()
    assert len(base64.b64decode(seed)) == 32
    assert len(base64.b64decode(pub)) == 32
    assert sign_release.public_key_b64(seed) == pub


def test_sign_produces_a_signature_the_updater_accepts(tmp_path):
    seed, pub = gen_signing_key.make_keypair()
    z = _pkg(tmp_path)
    sig = sign_release.sign(z, "1.2.0", seed, expected_public_key_b64=pub)
    updater.verify_signature("1.2.0", sign_release.sha256_file(z), sig, pub)


def test_sign_refuses_a_key_that_does_not_match_the_expected_public_key(tmp_path):
    seed, _ = gen_signing_key.make_keypair()
    _, other_pub = gen_signing_key.make_keypair()
    with pytest.raises(updater.UpdateError):
        sign_release.sign(_pkg(tmp_path), "1.2.0", seed, expected_public_key_b64=other_pub)


def test_sign_checks_against_the_built_in_public_key_by_default(tmp_path, monkeypatch):
    seed, pub = gen_signing_key.make_keypair()
    z = _pkg(tmp_path)
    monkeypatch.setattr(updater, "PUBLIC_KEY_B64", pub)
    sign_release.sign(z, "1.2.0", seed)
    monkeypatch.setattr(updater, "PUBLIC_KEY_B64", "")
    with pytest.raises(updater.UpdateError):
        sign_release.sign(z, "1.2.0", seed)


def test_main_writes_the_sig_file(tmp_path, monkeypatch):
    seed, pub = gen_signing_key.make_keypair()
    monkeypatch.setattr(updater, "PUBLIC_KEY_B64", pub)
    monkeypatch.setenv("UPDATE_SIGNING_KEY", seed)
    z = _pkg(tmp_path)
    assert sign_release.main([str(z), "--version", "1.2.0"]) == 0
    sig = Path(str(z) + ".sig").read_text(encoding="ascii").strip()
    updater.verify_signature("1.2.0", sign_release.sha256_file(z), sig, pub)


def test_main_fails_without_the_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UPDATE_SIGNING_KEY", raising=False)
    z = _pkg(tmp_path)
    assert sign_release.main([str(z), "--version", "1.2.0"]) == 1
    assert "UPDATE_SIGNING_KEY" in capsys.readouterr().err
    assert not Path(str(z) + ".sig").exists()


def test_main_refuses_a_zip_named_for_another_version(tmp_path, monkeypatch):
    seed, pub = gen_signing_key.make_keypair()
    monkeypatch.setattr(updater, "PUBLIC_KEY_B64", pub)
    monkeypatch.setenv("UPDATE_SIGNING_KEY", seed)
    z = _pkg(tmp_path, "1.1.0")
    assert sign_release.main([str(z), "--version", "1.2.0"]) == 1
    assert not Path(str(z) + ".sig").exists()


def test_gen_signing_key_pipes_private_key_to_gh_and_prints_only_the_public_key(capsys):
    calls = []
    gen_signing_key.main(["--repo", "o/r"], run=lambda cmd, **kw: calls.append((cmd, kw)))
    (cmd, kw), = calls
    assert cmd == ["gh", "secret", "set", "UPDATE_SIGNING_KEY", "-R", "o/r"]
    assert kw["check"] is True
    seed = kw["input"]
    out = capsys.readouterr().out
    assert seed not in out
    assert out.strip().splitlines()[-1] == sign_release.public_key_b64(seed)


def _cp1252_stdout():
    """模拟 GitHub Actions windows-latest 上 pwsh 的 stdout: 不是交互式控制台, 按系统
    代码页 cp1252 编码 —— print() 里的中文字符本来会直接 UnicodeEncodeError."""
    buf = io.BytesIO()
    return buf, io.TextIOWrapper(buf, encoding="cp1252")


def test_stdio_force_utf8_tolerates_a_stream_without_reconfigure(monkeypatch):
    # pythonw(无控制台)下 sys.stdout/stderr 可能是 None; reconfigure() 也可能不存在.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", object())
    _stdio.force_utf8_stdio()  # 不应该抛异常


def test_sign_release_main_does_not_crash_on_a_cp1252_console(tmp_path, monkeypatch):
    # 2026-09-27 v1.0.0 发版时真的这样崩过 —— print(f"已签名: ...") 在这种 stdout 上直接
    # UnicodeEncodeError, 整个 GitHub Actions job 失败, 没有生成 Release.
    seed, pub = gen_signing_key.make_keypair()
    monkeypatch.setattr(updater, "PUBLIC_KEY_B64", pub)
    monkeypatch.setenv("UPDATE_SIGNING_KEY", seed)
    z = _pkg(tmp_path)
    buf, fake_stdout = _cp1252_stdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    assert sign_release.main([str(z), "--version", "1.2.0"]) == 0
    fake_stdout.flush()
    assert "已签名" in buf.getvalue().decode("utf-8")


def test_gen_signing_key_main_does_not_crash_on_a_cp1252_console(monkeypatch):
    buf, fake_stdout = _cp1252_stdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    assert gen_signing_key.main(["--repo", "o/r"], run=lambda cmd, **kw: None) == 0
    fake_stdout.flush()
    assert "私钥已存进" in buf.getvalue().decode("utf-8")
