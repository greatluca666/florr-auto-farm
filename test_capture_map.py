import os

import cv2
import numpy as np

import capture_map


def _fake_minimap():
    """上半亮(可走) 下半暗(墙)的假小地图, 300x300 BGR —— 跟 utils.get_map() 一样."""
    img = np.zeros((300, 300, 3), dtype=np.uint8)
    img[0:150, :] = 255
    return img


def test_capture_writes_binary_and_raw_side_by_side(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    binary_path, raw_path = capture_map.capture("garden")

    assert binary_path == os.path.join("./maps", "garden.png")
    assert raw_path == "debug_garden_raw.png"
    assert os.path.exists(binary_path) and os.path.exists(raw_path)


def test_capture_binary_is_300x300_walkable_white(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    binary_path, _ = capture_map.capture("garden")

    out = cv2.imread(binary_path, cv2.IMREAD_GRAYSCALE)
    assert out.shape == (300, 300)      # 寻路整条链路都假设 300x300
    assert out[10, 10] == 255           # 亮的那半 = 可走
    assert out[290, 10] == 0            # 暗的那半 = 墙


def test_capture_raw_keeps_colour_for_eyeballing(monkeypatch, tmp_path):
    # 原色图是给人肉眼找蚁穴洞口用的 —— 二值图丢掉颜色就看不出哪个是洞.
    coloured = np.zeros((300, 300, 3), dtype=np.uint8)
    coloured[:, :] = (10, 20, 200)                    # BGR: 红
    monkeypatch.setattr(capture_map.utils, "get_map", lambda: coloured)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    _, raw_path = capture_map.capture("garden")

    back = cv2.imread(raw_path)
    assert tuple(back[0, 0]) == (10, 20, 200)


def test_capture_passes_threshold_through(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map",
                        lambda: np.full((300, 300, 3), 150, dtype=np.uint8))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    binary_path, _ = capture_map.capture("garden", threshold=100)

    assert (cv2.imread(binary_path, cv2.IMREAD_GRAYSCALE) == 255).all()


def test_capture_opens_known_shortcuts_for_the_map_name(monkeypatch, tmp_path):
    # "garden" 在 capture_map._KNOWN_SHORTCUTS 里有登记 —— 不用手动传坐标,
    # 按地图名自动查表打通。背景全黑(不是 _fake_minimap 那张上半天然可走的图,
    # GARDEN_SHORTCUTS 的矩形正好落在它的可走半区里, 不传 open_shortcuts_at
    # 断言也会巧合通过, 测不出接线到底有没有打通) —— 这样断言只有真接上
    # open_shortcuts_at 才会过.
    all_dark = np.zeros((300, 300, 3), dtype=np.uint8)
    monkeypatch.setattr(capture_map.utils, "get_map", lambda: all_dark)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    binary_path, _ = capture_map.capture("garden")

    out = cv2.imread(binary_path, cv2.IMREAD_GRAYSCALE)
    for x0, y0, x1, y1 in capture_map.map_routes.GARDEN_SHORTCUTS:
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        assert out[cy, cx] == 255, f"已知密道 ({cx},{cy}) 没被打通"


def test_capture_does_not_open_shortcuts_for_an_unknown_map_name(monkeypatch, tmp_path):
    # 没在 _KNOWN_SHORTCUTS 登记的图(比如沙漠/海洋, 还没标定)不该凭空开洞 ——
    # get_shortcuts_at 传 None 时 preprocess_map 必须是原来的行为.
    all_dark = np.zeros((300, 300, 3), dtype=np.uint8)
    monkeypatch.setattr(capture_map.utils, "get_map", lambda: all_dark)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    binary_path, _ = capture_map.capture("desert")

    assert (cv2.imread(binary_path, cv2.IMREAD_GRAYSCALE) == 0).all()


def test_capture_opens_known_shortcuts_for_anthell(monkeypatch, tmp_path):
    # 蚁穴也在 _KNOWN_SHORTCUTS 里登记了(florr.io 自己的 ant_hell.tmj 反解出来的
    # 3 条), 跟花园同一条接线, 单独锁一条防止以后改 _KNOWN_SHORTCUTS 漏掉它.
    all_dark = np.zeros((300, 300, 3), dtype=np.uint8)
    monkeypatch.setattr(capture_map.utils, "get_map", lambda: all_dark)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    binary_path, _ = capture_map.capture("anthell")

    out = cv2.imread(binary_path, cv2.IMREAD_GRAYSCALE)
    for x0, y0, x1, y1 in capture_map.map_routes.ANTHELL_SHORTCUTS:
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        assert out[cy, cx] == 255, f"已知密道 ({cx},{cy}) 没被打通"


def test_main_parses_name_and_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    assert capture_map.main(["garden", "--threshold", "100"]) == 0
    assert os.path.exists(os.path.join("./maps", "garden.png"))
