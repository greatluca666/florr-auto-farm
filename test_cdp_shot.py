"""录像截图(cdp_bridge.screenshot_jpeg_small): 不能带 clip。

2026-10-08 用户反馈「录制的时候一直白屏、屏幕整体缩小」。实测根因: Page.captureScreenshot 带 clip + scale=0.5 时, Chrome
会临时按缩小后的尺寸重排合成(无界面实测合成帧变成一半大小; 有界面窗口里截图期间出画帧少六成, 真实屏幕上抓到过红块缩成
一半的帧), 每 5 秒一次 —— 就是那个周期性的一闪。不带 clip、或 clip.scale=1 都没有这个现象。所以: 整幅截下来, 缩小留到本地做。
"""
import base64
import io
import re
from pathlib import Path

import cv2
import numpy as np
import pytest

import cdp_bridge

ROOT = Path(__file__).resolve().parent


def _jpeg(w, h):
    img = np.zeros((h, w, 3), np.uint8)
    img[:, : w // 2] = (30, 60, 200)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _fake_cdp(data, calls):
    def cdp(method, params=None, timeout=5):
        calls.append((method, params, timeout))
        return {"result": {"data": base64.b64encode(data).decode()}}
    return cdp


def _size(jpeg_bytes):
    img = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
    return img.shape[1], img.shape[0]


def test_it_never_asks_chrome_for_a_clip_or_a_scale():
    calls = []
    cdp_bridge.screenshot_jpeg_small(_fake_cdp(_jpeg(400, 200), calls))
    (method, params, _), = calls
    assert method == "Page.captureScreenshot" and params["format"] == "jpeg"
    assert "clip" not in params and "scale" not in params


def test_the_shrinking_happens_locally_to_half_size_by_default():
    out = cdp_bridge.screenshot_jpeg_small(_fake_cdp(_jpeg(400, 200), []))
    assert _size(out) == (200, 100)


def test_another_scale_and_the_quality_is_for_the_final_file():
    calls = []
    out = cdp_bridge.screenshot_jpeg_small(_fake_cdp(_jpeg(400, 200), calls), scale=0.25, quality=40)
    assert _size(out) == (100, 50)
    big = cdp_bridge.screenshot_jpeg_small(_fake_cdp(_jpeg(400, 200), []), scale=0.5, quality=95)
    small = cdp_bridge.screenshot_jpeg_small(_fake_cdp(_jpeg(400, 200), []), scale=0.5, quality=10)
    assert len(small) < len(big)                     # 最终那次编码才吃 quality 参数


def test_scale_one_hands_back_the_capture_untouched():
    data = _jpeg(400, 200)
    out = cdp_bridge.screenshot_jpeg_small(_fake_cdp(data, []), scale=1)
    assert out == data


def test_something_that_cannot_be_decoded_is_returned_as_is_not_lost():
    out = cdp_bridge.screenshot_jpeg_small(_fake_cdp(b"\xff\xd8fake-jpeg", []))
    assert out == b"\xff\xd8fake-jpeg"


def test_it_uses_the_real_cdp_helper_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(cdp_bridge, "_send_cdp_command", _fake_cdp(_jpeg(100, 100), calls))
    assert _size(cdp_bridge.screenshot_jpeg_small()) == (50, 50)
    assert calls and calls[0][0] == "Page.captureScreenshot"


@pytest.mark.parametrize("name", ["blackbox.py"])
def test_no_recorder_asks_chrome_for_a_clip_again(name):
    """三处录像截图(黑匣子 / 命令行录像 / 自动录像)用的是同一个写法, 都踩过这个坑 —— 谁再写回 clip 就拦下。"""
    src = (ROOT / name).read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())      # 注释里提到不算
    assert not re.search(r"""["']clip["']""", code), f"{name} 又在给 Page.captureScreenshot 传 clip 了"
