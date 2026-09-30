import os

import cv2
import numpy as np
import pytest

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


@pytest.mark.parametrize("name, const", [
    ("garden", ("GARDEN_SHORTCUTS",)),
    ("anthell", ("ANTHELL_SHORTCUTS", "ANTHELL_PORTAL_CLEARINGS")),
    ("desert", ("DESERT_SHORTCUTS",)), ("ocean", ("OCEAN_SHORTCUTS",)),
])
def test_capture_opens_known_shortcuts_for_the_map_name(monkeypatch, tmp_path, name, const):
    # 按地图名自动查 _KNOWN_SHORTCUTS 打通, 不用手动传坐标. 背景全黑 —— 不然密道落在
    # 天然可走的半区里, 不接 open_shortcuts_at 断言也会巧合通过.
    all_dark = np.zeros((300, 300, 3), dtype=np.uint8)
    monkeypatch.setattr(capture_map.utils, "get_map", lambda: all_dark)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    binary_path, _ = capture_map.capture(name)

    out = cv2.imread(binary_path, cv2.IMREAD_GRAYSCALE)
    rects = [r for c in const for r in getattr(capture_map.map_routes, c)]
    opened = sum((x1 - x0 + 1) * (y1 - y0 + 1) for x0, y0, x1, y1 in rects)
    assert int((out == 255).sum()) == opened
    for x0, y0, x1, y1 in rects:
        assert (out[y0:y1 + 1, x0:x1 + 1] == 255).all(), f"已知密道 ({x0},{y0}) 没被打通"


def test_capture_does_not_open_shortcuts_for_an_unknown_map_name(monkeypatch, tmp_path):
    # 没在 _KNOWN_SHORTCUTS 登记的图不该凭空开洞 —— 传 None 时 preprocess_map 必须是原来的行为.
    all_dark = np.zeros((300, 300, 3), dtype=np.uint8)
    monkeypatch.setattr(capture_map.utils, "get_map", lambda: all_dark)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    binary_path, _ = capture_map.capture("nowhere")

    assert (cv2.imread(binary_path, cv2.IMREAD_GRAYSCALE) == 0).all()


def test_main_parses_name_and_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    assert capture_map.main(["garden", "--threshold", "100"]) == 0
    assert os.path.exists(os.path.join("./maps", "garden.png"))


def test_capture_from_a_raw_image_never_touches_the_screen(monkeypatch, tmp_path):
    # --raw: 拿已有的原色截图重新生成, 不用进游戏截屏 —— 花园图要重生成时用.
    def no_screenshot():
        raise AssertionError("给了 raw_image 就不该去截屏")
    monkeypatch.setattr(capture_map.utils, "get_map", no_screenshot)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()

    binary_path, raw_path = capture_map.capture("garden", raw_image=_fake_minimap())

    assert raw_path is None
    out = cv2.imread(binary_path, cv2.IMREAD_GRAYSCALE)
    assert out[10, 10] == 255 and out[290, 10] == 0
    assert not (tmp_path / "debug_garden_raw.png").exists()   # 不写"原色副本"覆盖别人的截图


def test_main_raw_option_reads_the_image_file(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map",
                        lambda: (_ for _ in ()).throw(AssertionError("不该截屏")))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    cv2.imwrite(str(tmp_path / "shot.png"), _fake_minimap())

    assert capture_map.main(["garden", "--raw", "shot.png"]) == 0
    assert cv2.imread("./maps/garden.png", cv2.IMREAD_GRAYSCALE)[10, 10] == 255


def test_main_raw_option_rejects_an_unreadable_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    with pytest.raises(SystemExit) as excinfo:
        capture_map.main(["garden", "--raw", "nope.png"])
    assert "nope.png" in str(excinfo.value)     # 是"读不出文件"退出的, 不是 argparse 不认识 --raw 的 exit(2)


def _published_upper_half_walkable(tmp_path, top_wall_rows=0):
    published = np.zeros((300, 300), dtype=np.uint8)
    published[0:150, :] = 255
    published[0:top_wall_rows, :] = 0
    (tmp_path / "maps").mkdir(exist_ok=True)
    cv2.imwrite(str(tmp_path / "maps" / "toy.png"), published)


def test_compare_reports_agreement_with_the_published_map(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)     # 上半亮
    monkeypatch.chdir(tmp_path)
    _published_upper_half_walkable(tmp_path)
    assert capture_map.compare_with_published("toy") == (1.0, 0.0, 0.0)
    _published_upper_half_walkable(tmp_path, top_wall_rows=30)           # 已发布图最上面 30 行是墙
    agree, live_only, published_only = capture_map.compare_with_published("toy")
    assert abs(agree - 0.9) < 1e-9
    assert abs(live_only - 0.1) < 1e-9 and published_only == 0.0


def test_compare_writes_a_colour_coded_diff_image(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    _published_upper_half_walkable(tmp_path, top_wall_rows=30)
    capture_map.compare_with_published("toy")
    diff = cv2.imread("debug_toy_compare.png")
    assert diff.shape == (300, 300, 3)
    assert tuple(diff[5, 5]) == (0, 0, 255)          # 实机可走、已发布是墙 = 红
    assert tuple(diff[100, 5]) == (255, 255, 255)    # 都可走 = 白
    assert tuple(diff[250, 5]) == (0, 0, 0)          # 都是墙 = 黑
    published = np.zeros((300, 300), dtype=np.uint8)
    published[100:200, :] = 255                      # 已发布图: 100~199 行可走
    cv2.imwrite("./maps/toy.png", published)
    agree, live_only, published_only = capture_map.compare_with_published("toy")
    diff = cv2.imread("debug_toy_compare.png")
    assert tuple(diff[170, 5]) == (255, 0, 0)        # 实机是墙(实机 150 行以下是墙)、已发布可走 = 蓝
    # 实机 0~149 可走、已发布 100~199 可走: 都可走 100~149, 都是墙 200~299(共 150 行吻合),
    # 实机独有 0~99(100 行), 已发布独有 150~199(50 行)
    assert abs(agree - 150 / 300) < 1e-9
    assert abs(live_only - 100 / 300) < 1e-9
    assert abs(published_only - 50 / 300) < 1e-9


def test_main_compare_never_overwrites_the_published_map(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    _published_upper_half_walkable(tmp_path, top_wall_rows=30)
    before = (tmp_path / "maps" / "toy.png").read_bytes()
    assert capture_map.main(["toy", "--compare"]) == 0
    assert (tmp_path / "maps" / "toy.png").read_bytes() == before
    assert "90.00%" in capsys.readouterr().out


def test_compare_with_a_missing_published_map_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    with pytest.raises(SystemExit) as excinfo:
        capture_map.main(["nowhere", "--compare"])
    assert "nowhere.png" in str(excinfo.value)   # 是"读不出已发布图"退出的, 不是 argparse 不认识 --compare 的 exit(2)


def test_main_without_portal_no_longer_nags_about_portal(monkeypatch, tmp_path, capsys):
    # 花园图现在把每扇传送门都留作墙、走门时运行时才挖(map_routes.PORTAL_OPENINGS),
    # 所以不该再提示"带 --portal 重跑" —— 那会把人引去把门烤回图里。
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    assert capture_map.main(["garden"]) == 0
    assert "--portal" not in capsys.readouterr().out


def test_compare_survives_a_published_map_read_back_as_h_w_1(monkeypatch, tmp_path):
    # Windows 上 cv2.imread(IMREAD_GRAYSCALE) 实测吐过 (H,W,1)(见 utils._ensure_grayscale_2d);
    # --compare 恰恰要在那台机器上跑, 形状不锁的话 live & published 会广播成 (300,300,300)。
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    _published_upper_half_walkable(tmp_path, top_wall_rows=30)
    real_imread = cv2.imread

    def imread_with_trailing_channel(path, flags=cv2.IMREAD_COLOR):
        img = real_imread(path, flags)
        if flags == cv2.IMREAD_GRAYSCALE and str(path).endswith("toy.png"):
            return img[:, :, None]
        return img
    monkeypatch.setattr(capture_map.cv2, "imread", imread_with_trailing_channel)

    agree, live_only, published_only = capture_map.compare_with_published("toy")
    assert abs(agree - 0.9) < 1e-9
    assert abs(live_only - 0.1) < 1e-9 and published_only == 0.0
    monkeypatch.undo()
    assert cv2.imread(str(tmp_path / "debug_toy_compare.png")).shape == (300, 300, 3)


def test_compare_with_a_published_map_of_another_size_names_both_shapes(monkeypatch, tmp_path):
    monkeypatch.setattr(capture_map.utils, "get_map", _fake_minimap)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    cv2.imwrite(str(tmp_path / "maps" / "toy.png"), np.full((100, 100), 255, dtype=np.uint8))
    with pytest.raises(SystemExit) as excinfo:
        capture_map.compare_with_published("toy")
    message = str(excinfo.value)
    assert "(300, 300)" in message and "(100, 100)" in message


def test_compare_opens_the_known_shortcuts_like_capture_does(monkeypatch, tmp_path):
    # garden.png 里密道矩形是烤进去的。实机全黑、已发布 = 只有密道矩形可走: 口径一致时应该
    # 100% 吻合; 不给实机那一侧开密道的话, 这些矩形会全变成"已发布可走/实机是墙"的假差异。
    monkeypatch.setattr(capture_map.utils, "get_map",
                        lambda: np.zeros((300, 300, 3), dtype=np.uint8))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    published = np.zeros((300, 300), dtype=np.uint8)
    for x0, y0, x1, y1 in capture_map.map_routes.GARDEN_SHORTCUTS:
        published[y0:y1 + 1, x0:x1 + 1] = 255
    assert published.any()
    cv2.imwrite(str(tmp_path / "maps" / "garden.png"), published)

    assert capture_map.compare_with_published("garden") == (1.0, 0.0, 0.0)
    # 没登记密道的图不凭空开洞: 同一张已发布图拿去当 "nowhere" 比, 会出现蓝色差异
    cv2.imwrite(str(tmp_path / "maps" / "nowhere.png"), published)
    agree, _, published_only = capture_map.compare_with_published("nowhere")
    assert agree < 1.0 and published_only > 0.0


def test_compare_checks_the_published_map_before_grabbing_the_screen(monkeypatch, tmp_path):
    # 已发布图不存在就该立刻报错 —— 别先截一次屏(局内跑的, 截屏有成本)才发现。
    def no_screenshot():
        raise AssertionError("已发布图都读不出来, 不该去截屏")
    monkeypatch.setattr(capture_map.utils, "get_map", no_screenshot)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "maps").mkdir()
    with pytest.raises(SystemExit) as excinfo:
        capture_map.compare_with_published("nowhere")
    assert "nowhere.png" in str(excinfo.value)
