"""发版脚本共用: 强制 stdout/stderr 用 UTF-8.

2026-09-27 v1.0.0 发版时, `sign_release.py` 的 `print(f"已签名: ...")` 在 GitHub Actions
的 windows-latest 上炸了: 该 runner 跑 pwsh, Python 的 stdout 不是交互式控制台, 就按系统代码页
cp1252 编码, 一遇到中文字符就 UnicodeEncodeError 崩溃, 整个发版流水线跟着失败。main.py 的
`_force_utf8_stdio()` 已经在正式程序里修过同一个问题, 这里给两个发版脚本(sign_release.py /
gen_signing_key.py)抽一份共用的, 避免抄两遍。
"""
import sys


def force_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # stream 为 None 或不支持 reconfigure: 不影响运行
