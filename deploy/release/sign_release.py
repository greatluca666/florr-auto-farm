"""发版 CI 用: 给打好的安装包签名, 在旁边写出 <zip>.sig(一行 base64).

    UPDATE_SIGNING_KEY=<base64 私钥种子> python deploy/release/sign_release.py \\
        florr-auto-farm-v1.2.0-win64.zip --version 1.2.0

私钥只从环境变量读(GitHub Actions secret). 签完用程序里内置的公钥(updater.PUBLIC_KEY_B64)
验一遍: 验不过就失败 —— 用错了密钥发出去的版本, 用户的程序认不出来, 等于发了一个没法自动更新的版本.
签名的消息格式直接复用 updater.signed_message, 两边不会走样.
"""
import argparse
import base64
import hashlib
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import updater  # noqa: E402


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _private_key(seed_b64):
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64, validate=True))


def public_key_b64(seed_b64):
    raw = _private_key(seed_b64).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def sign(zip_path, ver, private_seed_b64, expected_public_key_b64=None):
    """返回签名(base64). expected_public_key_b64 为 None 时用程序内置的公钥自检."""
    digest = sha256_file(zip_path)
    sig = _private_key(private_seed_b64).sign(updater.signed_message(ver, digest))
    sig_b64 = base64.b64encode(sig).decode("ascii")
    updater.verify_signature(ver, digest, sig_b64, expected_public_key_b64)
    return sig_b64


def main(argv=None):
    p = argparse.ArgumentParser(description="给安装包签名, 写出 <zip>.sig")
    p.add_argument("zip")
    p.add_argument("--version", required=True, help="X.Y.Z, 不带 v")
    a = p.parse_args(argv)
    if updater.parse_version(a.version) is None:
        print(f"版本号 {a.version!r} 不是 X.Y.Z", file=sys.stderr)
        return 1
    if Path(a.zip).name != updater.asset_name(a.version):
        print(f"文件名应当是 {updater.asset_name(a.version)}, 实际是 {Path(a.zip).name}",
              file=sys.stderr)
        return 1
    key = os.environ.get("UPDATE_SIGNING_KEY", "").strip()
    if not key:
        print("缺少环境变量 UPDATE_SIGNING_KEY(GitHub secret 没配?)", file=sys.stderr)
        return 1
    sig = sign(a.zip, a.version, key)
    Path(a.zip + ".sig").write_text(sig + "\n", encoding="ascii")
    print(f"已签名: {a.zip}.sig")
    return 0


if __name__ == "__main__":
    sys.exit(main())
