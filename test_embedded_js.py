"""把仓库里所有会被送进浏览器的 JS 过一遍 `node --check`。

起因(2026-09-22 实机): 有一段 JS 写在普通三引号字符串里,
`.split("\\n")` 的 `\\n` 被 **Python** 先解释成了一个真的换行字符, 于是送到浏览器的
JS 字符串字面量里真的断了行 -> `SyntaxError: Invalid or unexpected token`。

这类错误在 Python 侧一点征兆都没有: 语法检查过、导入正常、测试全绿, 一直到真机上
CDP 把它喂给浏览器才炸。而且只有那一条命令行分支会走到, 平时跑不到。

所以这里拿真 node 去 parse。node 不在就整体跳过(Windows / CI 上可能没有), 但在
开发机上它是这类 bug 的唯一防线。

覆盖: 仓库根的 .js 文件, 以及 Python 里抽成模块级常量的 JS。**内联在函数体里的 JS
字符串覆盖不到** —— 要被这条守住, 就把它抽成模块级常量。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="没装 node, 跳过 JS 语法检查")


def _check(js_source, label, tmp_path):
    """把一段 JS 丢给 node --check。表达式(不是语句)要包一层才合法。"""
    f = tmp_path / "snippet.js"
    f.write_text(js_source, encoding="utf-8")
    r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
    if r.returncode != 0:
        # 表达式形式(比如一个 IIFE 前面没有语句上下文)包一层再试
        f.write_text("void (" + js_source + ");", encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, f"{label} 不是合法 JS:\n{r.stderr}"


@pytest.mark.parametrize("name", sorted(p.name for p in ROOT.glob("*.js")))
def test_js_files_parse(name, tmp_path):
    _check((ROOT / name).read_text(encoding="utf-8"), name, tmp_path)


def test_ws_probe_js_parses(tmp_path):
    import florr_server
    _check(florr_server._WS_PROBE_JS, "florr_server._WS_PROBE_JS", tmp_path)


def test_settings_js_template_parses(tmp_path):
    """_JS_TEMPLATE 带 {addr}/{want} 占位符, 填完才是合法 JS。"""
    import florr_settings
    _check(florr_settings._js(0x53430E, 1), "florr_settings._JS_TEMPLATE", tmp_path)
