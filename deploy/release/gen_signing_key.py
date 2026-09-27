"""一次性: 生成更新签名用的 Ed25519 密钥对.

私钥(32 字节种子的 base64)经 stdin 直接交给 `gh secret set`, 不打印、不写盘;
屏幕上只输出公钥 —— 把它填进 updater.py 的 PUBLIC_KEY_B64.

    python deploy/release/gen_signing_key.py        # 默认写到公开仓库 greatluca666/florr-auto-farm

私钥丢了/泄露了: 重新跑一遍(会覆盖 secret), 把新公钥填进 updater.py 发一个新版本;
旧版本的程序认不出新签名, 用户需要手动下载这一版.
"""
import argparse
import base64
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import updater  # noqa: E402
from _stdio import force_utf8_stdio  # noqa: E402


def make_keypair():
    priv = Ed25519PrivateKey.generate()
    seed = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                              serialization.NoEncryption())
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)
    return base64.b64encode(seed).decode("ascii"), base64.b64encode(pub).decode("ascii")


def main(argv=None, run=subprocess.run):
    force_utf8_stdio()
    p = argparse.ArgumentParser(description="生成更新签名密钥, 私钥直接存进 GitHub secret")
    p.add_argument("--repo", default=updater.GITHUB_REPO)
    a = p.parse_args(argv)
    seed_b64, pub_b64 = make_keypair()
    run(["gh", "secret", "set", "UPDATE_SIGNING_KEY", "-R", a.repo],
        input=seed_b64, text=True, check=True)
    print(f"私钥已存进 {a.repo} 的 secret UPDATE_SIGNING_KEY. 公钥(填进 updater.PUBLIC_KEY_B64):")
    print(pub_b64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
