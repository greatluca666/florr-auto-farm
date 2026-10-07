import os
import re

import cv2
import numpy as np
import pytest
from PIL import Image

import utils
from utils import (if_in_area, _ensure_grayscale_2d, _pick_server_id, calc_anti_stuck,
                   get_player_location_on_map, calibrate_player)


def test_if_in_area_normal_corner_order():
    area = [(0, 0), (10, 10)]
    assert if_in_area([area], (5, 5)) is True
    assert if_in_area([area], (20, 20)) is False


def test_if_in_area_flipped_x_corner_order():
    # main.py的farming_area实际写的是[(20, 15), (9, 76)] —— 第一个角x比第二个角
    # x还大. 之前if_in_area直接假设area[0]是"左上角"、area[1]是"右下角", 对这种
    # 顺序会导致x区间变成 20<=x<=9 永远判不出True, 玩家哪怕站在区域正中间都会被
    # 判定"不在区域内".
    area = [(20, 15), (9, 76)]
    assert if_in_area([area], (16, 46)) is True
    assert if_in_area([area], (14, 45)) is True
    assert if_in_area([area], (0, 0)) is False
    assert if_in_area([area], (25, 50)) is False


def test_if_in_area_flipped_y_corner_order():
    area = [(0, 76), (10, 15)]
    assert if_in_area([area], (5, 45)) is True
    assert if_in_area([area], (5, 100)) is False


def test_if_in_area_checks_multiple_areas():
    areas = [[(0, 0), (5, 5)], [(20, 20), (25, 25)]]
    assert if_in_area(areas, (22, 22)) is True
    assert if_in_area(areas, (12, 12)) is False


def test_ensure_grayscale_2d_squeezes_trailing_channel_dim():
    # 复现实测撞到的场景: cv2.imread(path, cv2.IMREAD_GRAYSCALE)在某些
    # OpenCV/平台组合(Windows)下吐出(H,W,1)而不是纯(H,W), 导致下游
    # calibrate_player()的`rows, cols = map.shape`拆包报错.
    img_3d = np.zeros((10, 20, 1), dtype=np.uint8)
    img_3d[3, 5, 0] = 255
    result = _ensure_grayscale_2d(img_3d)
    assert result.shape == (10, 20)
    assert result[3, 5] == 255


def test_ensure_grayscale_2d_leaves_true_2d_untouched():
    img_2d = np.zeros((10, 20), dtype=np.uint8)
    img_2d[3, 5] = 255
    result = _ensure_grayscale_2d(img_2d)
    assert result.shape == (10, 20)
    assert result[3, 5] == 255


def test_ensure_grayscale_2d_passes_through_none():
    # cv2.imread在文件读不到时返回None(不是抛异常) —— 不能让形状归一化
    # 逻辑在这种情况下自己先炸了(None.ndim会AttributeError).
    assert _ensure_grayscale_2d(None) is None


def test_pick_server_id_excludes_ids_used_within_cooldown():
    ids = ["a", "b", "c"]
    now = 10_000
    last_used = {"a": now - 60}  # 1分钟前刚用过a, 还在30分钟冷却期内
    for _ in range(20):
        assert _pick_server_id(ids, last_used, now, cooldown_seconds=1800) != "a"


def test_pick_server_id_allows_id_once_cooldown_expires():
    ids = ["a", "b", "c"]
    now = 10_000
    last_used = {"a": now - 1801}  # 30分钟零1秒前用过, 刚好过了冷却期
    # a现在应该重新进入候选池了(不再总是被排除), 多跑几次应该能选到a.
    results = {_pick_server_id(ids, last_used, now, cooldown_seconds=1800) for _ in range(50)}
    assert "a" in results


def test_pick_server_id_falls_back_to_least_recently_used_when_all_on_cooldown():
    # 只有3台服务器, 全部都在冷却期内(换得比冷却期还频繁) —— 不能卡死不换,
    # 退化成挑最久没用过的那个(b, 5分钟前用的, 比a/c更久远).
    ids = ["a", "b", "c"]
    now = 10_000
    last_used = {"a": now - 60, "b": now - 300, "c": now - 120}
    assert _pick_server_id(ids, last_used, now, cooldown_seconds=1800) == "b"


def test_pick_server_id_never_used_before_is_always_eligible():
    # last_used里完全没有的id, 相当于"上次使用时间是很久很久以前", 天然不在
    # 冷却期内 —— 用.get(i, 0)兜底, 不能因为字典里没这个key就报KeyError.
    ids = ["a", "brand_new"]
    now = 10_000
    last_used = {"a": now - 60}
    result = _pick_server_id(ids, last_used, now, cooldown_seconds=1800)
    assert result == "brand_new"


def test_scale_x_and_scale_y_are_identity_at_reference_resolution(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 1920)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 1080)
    assert utils.scale_x(960) == 960
    assert utils.scale_y(540) == 540


def test_scale_point_scales_uniformly_on_larger_same_aspect_screen(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 2560)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 1440)
    # 2560/1920 == 1440/1080 == 4/3: same 16:9 aspect ratio, uniform scale-up.
    assert utils.scale_point(960, 540) == (1280, 720)


def test_scale_point_scales_axes_independently_on_non_16_9_screen(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 2560)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 1080)  # ultrawide: width changes, height doesn't
    x, y = utils.scale_point(1920, 1080)
    assert x == 2560  # scaled by width ratio (2560/1920)
    assert y == 1080  # scale_y ratio is 1, untouched


def test_scale_region_scales_position_and_size_independently(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 3840)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 2160)
    # get_map()'s reference crop region: [1600, 20, 300, 300] at 1920x1080.
    assert utils.scale_region(1600, 20, 300, 300) == [3200, 40, 600, 600]


def test_clamp_to_screen_keeps_point_inside_bounds_with_margin(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 1366)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 768)
    assert utils.clamp_to_screen(-50, 2000) == (2, 766)
    assert utils.clamp_to_screen(700, 400) == (700, 400)


def test_mouse_scale_matches_min_of_axis_ratios(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 960)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 1080)
    assert utils.mouse_scale() == 0.5  # min(960/1920, 1080/1080) == min(0.5, 1.0)
    assert round(10 * utils.mouse_scale()) == 5  # must work as a real float in arithmetic, like Task 2/4 will use it


def test_calc_anti_stuck_clips_to_actual_screen_bounds_not_1920x1080(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 800)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 600)
    monkeypatch.setattr(utils, "toggle_map", lambda: None)
    # A single "wall" pixel far to the left of screen center pushes the
    # suggested position hard to the right — enough to hit whatever the
    # x-clip upper bound is. At the old hardcoded bound (1920) this would
    # stay under it and the bug wouldn't show; at the new 800-wide bound
    # it must clip to 800.
    borders = [(-5000, 300)]
    x, y = calc_anti_stuck(borders, weight=10000.0)
    assert x == 800
    assert 0 <= y <= 600


# ── toggle_map: 花园之前从没触发过, 见 utils.toggle_map 的说明 ─────────────────

def test_toggle_map_presses_m_when_calibrated_position_hits_the_trigger(monkeypatch):
    # 沙漠已经实机验证过的老路径: 校准后坐标正好命中 (250,50).
    monkeypatch.setattr(utils, "get_player_position",
                        lambda precise=False: (250, 50) if not precise else (251.2, 49.6))
    keys = []
    monkeypatch.setattr(utils.pyautogui, "press", lambda k: keys.append(k))
    monkeypatch.setattr(utils.time, "sleep", lambda *a, **k: None)
    utils.toggle_map()
    assert keys == ["m"]


def test_toggle_map_presses_m_when_raw_position_hits_the_trigger_even_if_calibrated_differs(monkeypatch):
    # 花园这种场景: 校准后坐标(吸附到花园自己最近的可走点)对不上 (250,50)了,
    # 但没经过吸附的原始检测坐标还是命中的 —— 这条就是这次要补的路径.
    monkeypatch.setattr(utils, "get_player_position",
                        lambda precise=False: (77, 133) if not precise else (249.4, 50.9))
    keys = []
    monkeypatch.setattr(utils.pyautogui, "press", lambda k: keys.append(k))
    monkeypatch.setattr(utils.time, "sleep", lambda *a, **k: None)
    utils.toggle_map()
    assert keys == ["m"]


def test_toggle_map_does_nothing_when_neither_position_hits_the_trigger(monkeypatch):
    monkeypatch.setattr(utils, "get_player_position",
                        lambda precise=False: (77, 133) if not precise else (80.0, 135.0))
    keys = []
    monkeypatch.setattr(utils.pyautogui, "press", lambda k: keys.append(k))
    monkeypatch.setattr(utils.time, "sleep", lambda *a, **k: None)
    utils.toggle_map()
    assert keys == []


def test_toggle_map_raw_trigger_has_a_tolerance_not_pixel_exact(monkeypatch):
    # precise 坐标是浮点、带检测噪声 —— 差几个像素也该算命中, 不是非得刚好
    # 250.0/50.0.
    monkeypatch.setattr(utils, "get_player_position",
                        lambda precise=False: (77, 133) if not precise else (252.5, 47.7))
    keys = []
    monkeypatch.setattr(utils.pyautogui, "press", lambda k: keys.append(k))
    monkeypatch.setattr(utils.time, "sleep", lambda *a, **k: None)
    utils.toggle_map()
    assert keys == ["m"]


def test_toggle_map_rechecks_position_after_pressing_m(monkeypatch):
    # 用户反馈(2026-09-20): 按完M不能就假设生效了, 得再判断一次 —— 按M之前的
    # 判定用的是按M**之前**的画面, 不代表按完之后的真实状态. 这条测的是
    # toggle_map 真的在按完M之后又读了一次位置(不是只判定触发条件那两次).
    calls = {"n": 0}

    def fake_get_player_position(precise=False):
        calls["n"] += 1
        if precise:
            return (249.5, 49.5)
        return (250, 50) if calls["n"] == 1 else (12, 34)

    monkeypatch.setattr(utils, "get_player_position", fake_get_player_position)
    monkeypatch.setattr(utils.pyautogui, "press", lambda k: None)
    monkeypatch.setattr(utils.time, "sleep", lambda *a, **k: None)
    utils.toggle_map()
    # 判定用的 calibrated(1次) + raw(1次) + 按完M复查(1次) = 3次.
    assert calls["n"] == 3


def test_toggle_map_does_not_recheck_when_trigger_never_hits(monkeypatch):
    # 没触发按M的话, 后面那次复查也不该发生 —— 不是无条件多读一次位置.
    calls = {"n": 0}

    def fake_get_player_position(precise=False):
        calls["n"] += 1
        return (77, 133) if not precise else (80.0, 135.0)

    monkeypatch.setattr(utils, "get_player_position", fake_get_player_position)
    monkeypatch.setattr(utils.pyautogui, "press", lambda k: None)
    monkeypatch.setattr(utils.time, "sleep", lambda *a, **k: None)
    utils.toggle_map()
    assert calls["n"] == 2   # 只有判定用的 calibrated + raw, 没有第3次复查.


def test_toggle_map_raw_trigger_tolerance_does_not_swallow_the_whole_map(monkeypatch):
    # 容差不能松到把真的离目标老远的点也算命中.
    monkeypatch.setattr(utils, "get_player_position",
                        lambda precise=False: (77, 133) if not precise else (260.0, 50.0))
    keys = []
    monkeypatch.setattr(utils.pyautogui, "press", lambda k: keys.append(k))
    monkeypatch.setattr(utils.time, "sleep", lambda *a, **k: None)
    utils.toggle_map()
    assert keys == []


def test_get_map_resizes_scaled_capture_back_to_300x300(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 3840)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 2160)
    captured = {}

    def fake_screenshot(region):
        captured["region"] = region
        w, h = region[2], region[3]
        return Image.new("RGB", (w, h), color=(10, 20, 30))

    monkeypatch.setattr(utils.pyautogui, "screenshot", fake_screenshot)

    image = utils.get_map()

    # At 4K (2x the 1920x1080 reference), the scaled minimap region should
    # be captured at 600x600 (2x the reference 300x300)...
    assert captured["region"] == [3200, 40, 600, 600]
    # ...but get_map() must hand back exactly 300x300 regardless, since
    # maps/*.png templates and every downstream map-space consumer assume
    # that fixed pixel space.
    assert image.shape[:2] == (300, 300)


def test_get_map_is_a_no_op_resize_at_reference_resolution(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 1920)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 1080)
    captured = {}

    def fake_screenshot(region):
        captured["region"] = region
        w, h = region[2], region[3]
        return Image.new("RGB", (w, h), color=(10, 20, 30))

    monkeypatch.setattr(utils.pyautogui, "screenshot", fake_screenshot)

    image = utils.get_map()

    # Unchanged from the pre-this-plan behavior: region is already 300x300.
    assert captured["region"] == [1600, 20, 300, 300]
    assert image.shape[:2] == (300, 300)


def test_get_map_uses_height_based_uniform_scale_for_non_16_9(monkeypatch):
    """实机验证(见2026-08-26的调试会话): 1024x768下小地图真实边界实测约
    左上(797,20)~右下(1009,227) —— 用debug_screen_pos.py量鼠标点 + 用
    debug_position_diag.py对着get_map()的截图跑f8de60颜色匹配确认过, 独立轴
    scale_region()那套公式(算出来是[853,14,160,214])截歪了: 宽度偏窄、外边框
    整个漏在截图外, 匹配像素数是0. 换成"整体按SCREEN_HEIGHT/1080统一缩放、
    保持正方形、贴右上角"的公式跟实测数据吻合(右/下边几乎分毫不差, 左边基本对上)。
    """
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 1024)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 768)
    captured = {}

    def fake_screenshot(region):
        captured["region"] = region
        w, h = region[2], region[3]
        return Image.new("RGB", (w, h), color=(10, 20, 30))

    monkeypatch.setattr(utils.pyautogui, "screenshot", fake_screenshot)

    image = utils.get_map()

    assert captured["region"] == [797, 14, 213, 213]
    assert image.shape[:2] == (300, 300)


def test_get_player_location_on_map_accepts_a_shrunk_marker_blob():
    """实机(1024x768)调试确认过: 非参照分辨率下get_map()截到的原始区域比300x300小,
    resize放大回300x300后, 真实玩家标记的像素footprint会缩水 —— 抓到的失败瞬间现场
    量出来radius=1.90(debug_position_diag.py+debug_position_diag_marked.png肉眼确认
    过是真标记不是噪声), 被原来radius>2的阈值当噪声滤掉. 这里在300x300画布上画一个
    对角十字形的7像素小blob(minEnclosingCircle算出来radius≈1.41, 复现"比参照分辨率
    下的标记小, 但不是孤立噪声"这个情况), 用precise=True跳过calibrate_player(不需要
    真实binary_map), 直接验证这个尺寸的标记能被找到.
    """
    image = np.zeros((300, 300, 3), dtype=np.uint8)
    bgr = (0x60, 0xde, 0xf8)  # f8de60的BGR顺序 (cv2图像是BGR, 不是RGB)
    for (px, py) in [(149, 150), (150, 150), (151, 150), (150, 149), (150, 151), (149, 149), (151, 151)]:
        image[py, px] = bgr

    position = get_player_location_on_map(image, "f8de60", map=None, precise=True)

    assert position is not None
    x, y = position
    assert abs(x - 150) < 1.5
    assert abs(y - 150) < 1.5


def test_player_marker_constants_match_the_calibrated_values():
    # 这几个值是从 get_player_location_on_map()/get_player_position() 内联硬编码
    # 里拆出来的模块级常量(2026-09-16), 就是为了给 debug_position_diag.py 这类
    # 诊断脚本 import —— 改这几个数直接改这里, 别再让诊断脚本自己抄一份.
    assert utils.PLAYER_MARKER_MIN_RADIUS == 1
    assert utils.PLAYER_MARKER_COLOR == "f8de60"
    assert utils.PLAYER_MARKER_TOLERANCE == 30


def test_get_player_location_on_map_finds_the_marker_in_two_real_garden_captures():
    """实机复盘(2026-09-16): 用户反馈花园里测不到玩家位置, debug_position_diag.py
    的全屏红框工具排出了"截图区域没对准"这条; 逐像素量两张独立的花园实机截图
    (calibration 时截的 debug_garden_raw.png, 和这次复盘用户现截的
    debug_position_diag_raw.png)才发现: 玩家标记的真实颜色 R 通道系统性地偏了
    一截, 分别要 ±27 / ±22 容差才测得到, 原来的 ±20 一个像素都测不到 —— 不是
    分辨率/噪声问题(那是 PLAYER_MARKER_MIN_RADIUS 修的另一件事), 是容差本身卡线.

    这条测试用真实截图文件回归, 不是合成的假图: 拿旧容差跑会 0 匹配, 拿新容差
    (utils.PLAYER_MARKER_TOLERANCE=30)必须两张都测到.
    """
    for name in ("debug_garden_raw.png", "debug_position_diag_raw.png"):
        path = os.path.join(os.path.dirname(__file__), name)
        if not os.path.exists(path):
            pytest.skip(f"{name} 不在仓库里(应该已经入库, 但本地检出可能缺失)")
        image = cv2.imread(path)
        position = get_player_location_on_map(image, utils.PLAYER_MARKER_COLOR,
                                              map=None, precise=True)
        assert position is not None, f"{name}: 玩家标记测不到了 —— 容差是不是又改窄了?"


def test_get_player_location_on_map_still_clean_on_a_different_biomes_palette():
    # 加宽容差最怕的副作用是误伤别的地图上凑巧接近这个颜色的墙/地板像素。蚁穴的
    # debug_anthell_raw.png 调门户坐标时截的, 色板跟花园完全不同——测到的应该
    # 还是玩家标记自己那一个点, 不是别的什么东西被误判成了玩家.
    path = os.path.join(os.path.dirname(__file__), "debug_anthell_raw.png")
    if not os.path.exists(path):
        pytest.skip("debug_anthell_raw.png 不在仓库里")
    image = cv2.imread(path)
    position = get_player_location_on_map(image, utils.PLAYER_MARKER_COLOR,
                                          map=None, precise=True)
    assert position is not None


# ── calibrate_player: 别把玩家吸附到孤立的小碎块上 ──────────────────────────

def _map_with_island(main_blob, island, shape=(30, 30)):
    """造一张假地图: main_blob 和 island 都是可走像素坐标的列表, 两者之间用一圈
    黑像素隔开(不共享/不相邻), 复现"主区域 + 一撮不连通的碎块"这个形状."""
    m = np.zeros(shape, dtype=np.uint8)
    for x, y in main_blob:
        m[y, x] = 255
    for x, y in island:
        m[y, x] = 255
    return m


def test_calibrate_player_does_not_snap_onto_a_disconnected_island():
    """实机复盘(2026-09-16, 蚁穴): 玩家检测到站在(121,98), 恰好是把附近一个传送点
    强制挖成墙时顺手切出来的31像素孤岛, 跟30168像素的主区域完全不通(4/8连通都
    验证过, 8连通是 lazy_theta_star 自己的邻居定义)。旧逻辑无差别在全图找最近
    可走点, 吸附到孤岛上——从孤岛出发, 地图上真的没有路到刷怪区,
    lazy_theta_star 秒回 None, 报"路径规划失败", 一轮一轮空转.

    这里用一张缩小的合成图复现同样的形状: 一大块主区域, 旁边隔着黑格贴一小撮
    "孤岛", 原始检测点离孤岛比离主区域近——旧逻辑会snap到孤岛, 新逻辑必须
    忽略孤岛、snap到主区域.
    """
    main_blob = [(x, y) for x in range(2, 10) for y in range(2, 10)]   # 8x8=64像素主区域
    island = [(15, 5), (16, 5), (15, 6)]                               # 3像素孤岛, 离主区域有黑格隔开
    m = _map_with_island(main_blob, island)

    raw_detected = (15, 5)   # 就落在孤岛正中间
    result = calibrate_player(m, raw_detected)

    assert result not in island, f"吸附到孤岛上了: {result}"
    assert result in main_blob, f"没有吸附到主区域: {result}"


def test_calibrate_player_leaves_a_position_already_on_the_main_component_unchanged():
    # 正常情况(检测点本来就在主区域上)不该受影响——孤岛过滤不能误伤这条最常见的路径.
    main_blob = [(x, y) for x in range(2, 10) for y in range(2, 10)]
    island = [(15, 5)]
    m = _map_with_island(main_blob, island)

    result = calibrate_player(m, (5, 5))
    assert result == (5, 5)


def test_calibrate_player_returns_input_unchanged_when_map_is_entirely_walls():
    # 极端情况: 整张图一个可走像素都没有(地图文件坏了/读错了) —— 没有"主区域"
    # 可言, 原样吃老行为返回输入本身, 不在这里硬造一个假坐标.
    m = np.zeros((30, 30), dtype=np.uint8)
    assert calibrate_player(m, (5, 5)) == (5, 5)


def test_calibrate_player_fixes_the_real_anthell_portal_island_end_to_end():
    """端到端复现用户实机那次失败, 直接用真实的 maps/anthell.png:

        检测到 (121, 98) -> calibrate_player 吸附 -> lazy_theta_star 规划到刷怪点

    改之前这条链路在 (121,98) 上会 100% 复现"路径规划失败"(吸附到31像素孤岛,
    孤岛到主区域无路可通); 改之后吸附点必须落在主连通区域上, 且从那里出发必须
    真的能规划出一条路.
    """
    import main as m

    path_to_map = os.path.join(os.path.dirname(__file__), "maps", "anthell.png")
    if not os.path.exists(path_to_map):
        pytest.skip("maps/anthell.png 不在仓库里")
    binary_map = cv2.imread(path_to_map, cv2.IMREAD_GRAYSCALE)

    raw_detected = (121, 98)          # 用户实机日志里报出来的检测点
    farming_target = (45, 178)        # 同一次日志里配置的刷怪目标点

    calibrated = calibrate_player(binary_map, raw_detected)
    assert binary_map[calibrated[1], calibrated[0]] == 255

    path = m.lazy_theta_star(binary_map, calibrated, farming_target)
    assert path is not None, f"从吸附后的 {calibrated} 到 {farming_target} 仍然规划失败"


def test_get_player_location_on_map_reads_the_min_radius_constant_live(monkeypatch):
    # 证明判定真的读的是模块常量, 不是函数里另外埋了一份字面量 —— 调高常量后,
    # 上面那个 radius≈1.41 的缩水标记就该被拒了(以前"radius>2"卡过同一种情况).
    image = np.zeros((300, 300, 3), dtype=np.uint8)
    bgr = (0x60, 0xde, 0xf8)
    for (px, py) in [(149, 150), (150, 150), (151, 150), (150, 149), (150, 151), (149, 149), (151, 151)]:
        image[py, px] = bgr

    monkeypatch.setattr(utils, "PLAYER_MARKER_MIN_RADIUS", 5)
    assert get_player_location_on_map(image, "f8de60", map=None, precise=True) is None


def test_debug_position_diag_imports_the_shared_constants_not_its_own_copy():
    # 2026-08-26 踩过的坑: 诊断脚本自己硬编码了一份 radius>2, utils.py 那边调成
    # >1 之后脚本没跟着改, 打出来的"诊断结论"其实跟真代码判的不是一回事. 这条锁住
    # "脚本 import 的就是 utils 那个对象本身", 不是复制了一份同名同值的常量.
    import debug_position_diag as diag
    assert diag.PLAYER_MARKER_MIN_RADIUS is utils.PLAYER_MARKER_MIN_RADIUS
    assert diag.PLAYER_MARKER_COLOR is utils.PLAYER_MARKER_COLOR
    assert diag.PLAYER_MARKER_TOLERANCE is utils.PLAYER_MARKER_TOLERANCE


class _ClickSpy:
    def __init__(self):
        self.clicks = 0

    def moveTo(self, *_a, **_kw):
        pass

    def click(self, *_a, **_kw):
        self.clicks += 1


def _patch_click(monkeypatch, on_screen_sequence, screen_attr="on_start_screen"):
    """on_screen_sequence: 每次复查画面时依次返回的值; 用完后保持最后一个值.
    screen_attr: _click_button_until_gone 传进来的复查函数名 (on_start_screen /
    on_death_screen / on_guest_screen)."""
    spy = _ClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    seq = list(on_screen_sequence)
    calls = {"n": 0}

    def fake_screen_check():
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    monkeypatch.setattr(utils, screen_attr, fake_screen_check)
    return spy


def test_click_start_game_stops_after_menu_gone_on_first_try(monkeypatch):
    # 点一下菜单就消失了 —— 只该点这一轮(两下connect click), 返回True.
    spy = _patch_click(monkeypatch, [False])
    assert utils.click_start_game() is True
    assert spy.clicks == 2


def test_click_start_game_retries_until_menu_gone(monkeypatch):
    # 前两轮点了菜单还在, 第三轮才进去 —— 该重试到进去为止.
    spy = _patch_click(monkeypatch, [True, True, False])
    assert utils.click_start_game() is True
    assert spy.clicks == 2 * 3


def test_click_start_game_gives_up_after_max_attempts_without_crashing(monkeypatch):
    # 菜单怎么点都不消失(标签页卡死之类) —— 试满次数后返回False, 交给主循环下轮再试,
    # 不无限卡在这里, 也不抛异常.
    spy = _patch_click(monkeypatch, [True])
    assert utils.click_start_game() is False
    assert spy.clicks == 2 * utils._CONFIRM_CLICK_MAX_ATTEMPTS


class _MoveClickSpy:
    def __init__(self):
        self.moves = []
        self.clicks = 0

    def moveTo(self, pos=None, *_a, **_kw):
        self.moves.append(tuple(pos) if pos is not None else None)

    def click(self, *_a, **_kw):
        self.clicks += 1


def test_click_start_game_parks_the_mouse_right_after_clicking(monkeypatch):
    # 鼠标停在「开始」上 = 一进局就朝它走; 第六份录像蚁穴复活后这样滑到回花园的门边被传走
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils, "on_start_screen", lambda: False)
    assert utils.click_start_game() is True
    assert spy.moves == [tuple(utils._START_BUTTON_POS),
                         (utils.SCREEN_WIDTH // 2, utils.SCREEN_HEIGHT // 2)]


# ── 开局菜单的「开始」按钮: 按颜色找, 不靠固定坐标 ──────────────────────────────
# 2026-10-04 有粉丝 2K 屏上认不出开局菜单。「开始」按钮左边是玩家名, 名字 + 按钮这一组是水平居中的,
# 所以按钮离屏幕中心多远取决于名字多长(再乘上 UI 缩放); 固定坐标 (1059,527) 只在名字长度跟量坐标那个号
# 差不多、而且屏幕是 16:9 时才对得上。现在: 先试老的固定点(快路, 行为不变), 不中再在屏幕中间一条横带里
# 找一块「确认绿」的按钮形色块, 点击也点它的中心。下面的按钮像素是 2026-10-04 真录像截图里抠的。
from pathlib import Path as _Path

_TITLE_BG = (28, 168, 95)          # 标题页背景绿(同一张截图里量的)
_START_CROP = cv2.cvtColor(
    cv2.imread(str(_Path(__file__).parent / "test_frames" / "start_button_1080p_half.png")),
    cv2.COLOR_BGR2RGB)            # 半分辨率截图里抠的「开始」按钮, 72x48, 按钮约在正中


class _ScreenStub:
    """pyautogui 的替身: screenshot(region) 从一张预先画好的整屏图里截。"""

    def __init__(self, frame):
        self.frame = frame
        self.shots = []

    def screenshot(self, region=None):
        x, y, w, h = region
        self.shots.append((x, y, w, h))
        return Image.fromarray(self.frame[y:y + h, x:x + w].copy())


def _screen(monkeypatch, w, h, frame):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", w)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", h)
    # 这两个固定点是 import 时按真屏幕算的常量; 换了屏幕大小就得跟着换, 不然采样框落在屏幕外
    monkeypatch.setattr(utils, "_START_BUTTON_POS", utils.scale_point(1059, 527))
    monkeypatch.setattr(utils, "_CONTINUE_BUTTON_POS", utils._ui_point(959, 634))
    stub = _ScreenStub(frame)
    monkeypatch.setattr(utils, "pyautogui", stub)
    return stub


def _title_frame(w, h, ui, offset_ref=99, y_ref=527):
    """w x h 的标题页: 背景绿 + 真「开始」按钮(按 UI 缩放 ui 放大), 按钮中心在屏幕中心 +
    (offset_ref, y_ref-540)*ui —— 1080p 下默认值就是量坐标那个号(1059, 527)。返回 (整屏图, 按钮中心)。"""
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = _TITLE_BG
    k = 2.0 * ui
    btn = cv2.resize(_START_CROP, None, fx=k, fy=k, interpolation=cv2.INTER_LINEAR)
    bh, bw = btn.shape[:2]
    cx, cy = round(w / 2 + offset_ref * ui), round(h / 2 + (y_ref - 540) * ui)
    x0, y0 = cx - bw // 2, cy - bh // 2
    frame[y0:y0 + bh, x0:x0 + bw] = btn
    return frame, (cx, cy)


_SCREENS = [   # (宽, 高, UI 缩放)。超宽屏两种缩放都测: florr 按高还是按宽缩放没实机量过
    (1920, 1080, 1.0), (2560, 1440, 4 / 3), (3840, 2160, 2.0), (1366, 768, 768 / 1080),
    (1024, 768, 768 / 1080), (3440, 1440, 4 / 3), (3440, 1440, 3440 / 1920),
]


@pytest.mark.parametrize("w,h,ui", _SCREENS)
@pytest.mark.parametrize("offset_ref", [-60, 20, 99, 220])      # 玩家名长短不同 -> 按钮离屏幕中心的距离不同
def test_start_button_is_found_wherever_the_layout_puts_it(monkeypatch, w, h, ui, offset_ref):
    frame, (cx, cy) = _title_frame(w, h, ui, offset_ref)
    _screen(monkeypatch, w, h, frame)
    monkeypatch.setattr(utils, "on_death_screen", lambda: False)
    found = utils.find_start_button()
    assert found is not None
    assert abs(found[0] - cx) <= 4 * ui and abs(found[1] - cy) <= 4 * ui
    assert utils.on_start_screen() is True


def test_the_old_fixed_point_still_works_and_skips_the_search(monkeypatch):
    # 名字长度跟量坐标那个号差不多、16:9 的人行为不变: 固定点命中就不再截那条带
    frame = np.zeros((1080, 1920, 3), np.uint8)
    frame[:] = _TITLE_BG
    frame[527 - 24:527 + 24, 1059 - 57:1059 + 57] = utils._BUTTON_GREEN_RGB     # 固定点正落在纯绿底上
    stub = _screen(monkeypatch, 1920, 1080, frame)
    monkeypatch.setattr(utils, "on_death_screen", lambda: False)
    monkeypatch.setattr(utils, "find_start_button", lambda: pytest.fail("固定点命中就不该再找"))
    assert utils.on_start_screen() is True
    assert len(stub.shots) == 1


def _gameplay_frame(w, h):
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = _TITLE_BG                                  # 花园游戏里的背景跟标题页同一种绿
    rng = np.random.default_rng(7)
    for _ in range(40):                                   # 零星的怪 / 花瓣 / 血条(颜色各异, 都不是确认绿)
        x, y = int(rng.integers(0, w - 30)), int(rng.integers(0, h - 30))
        frame[y:y + 14, x:x + 24] = rng.integers(0, 255, 3)
    return frame


@pytest.mark.parametrize("w,h", [(1920, 1080), (2560, 1440), (3440, 1440)])
def test_gameplay_is_not_the_start_menu(monkeypatch, w, h):
    _screen(monkeypatch, w, h, _gameplay_frame(w, h))
    monkeypatch.setattr(utils, "on_death_screen", lambda: False)
    assert utils.find_start_button() is None
    assert utils.on_start_screen() is False


def test_green_blobs_that_are_not_button_shaped_are_not_the_start_button(monkeypatch):
    w, h = 2560, 1440
    ui = h / 1080

    def blob(frame, cx, cy, bw, bh):
        frame[cy - bh // 2:cy + bh // 2, cx - bw // 2:cx + bw // 2] = utils._BUTTON_GREEN_RGB

    for label, (bw, bh) in {"小圆点": (8, 8), "游客页那种又宽又扁的按钮": (round(207 * ui), round(32 * ui)),
                            "竖条": (round(20 * ui), round(120 * ui)),
                            "整面绿墙": (round(600 * ui), round(240 * ui))}.items():
        frame = _gameplay_frame(w, h)
        blob(frame, w // 2 + 100, h // 2 - 20, bw, bh)
        _screen(monkeypatch, w, h, frame)
        monkeypatch.setattr(utils, "on_death_screen", lambda: False)
        assert utils.find_start_button() is None, label


def test_text_and_arrow_inside_the_button_do_not_split_it_into_pieces(monkeypatch):
    # 高分辨率下按钮里的字 / 箭头笔画更粗更利, 会把绿底割开; 闭运算要能把它们合回一块
    w, h, ui = 2560, 1440, 4 / 3
    frame, (cx, cy) = _title_frame(w, h, ui)
    stripe = round(3 * ui)
    frame[cy - 40:cy + 40, cx - stripe // 2:cx - stripe // 2 + stripe] = (255, 255, 255)   # 贯穿整个按钮的白竖线
    _screen(monkeypatch, w, h, frame)
    found = utils.find_start_button()
    assert found is not None and abs(found[1] - cy) <= 4 * ui


@pytest.mark.parametrize("bw,bh,why", [
    (60, 45, "太窄(只有宽度不够)"), (120, 30, "太矮(只有高度不够)"),
    (400, 110, "太宽(只有宽度超标)"), (300, 150, "太高(只有高度超标)"),
])
def test_each_size_limit_of_the_button_matters_on_its_own(monkeypatch, bw, bh, why):
    # 2560x1440 下(ui=4/3)按钮合理范围: 宽 93~346, 高 37~120, 宽高比 1.3~4.2 —— 这几个尺寸各只踩破一条尺寸线
    w, h = 2560, 1440
    frame = _gameplay_frame(w, h)
    cx, cy = w // 2 + 100, 705
    frame[cy - bh // 2:cy - bh // 2 + bh, cx - bw // 2:cx - bw // 2 + bw] = utils._BUTTON_GREEN_RGB
    _screen(monkeypatch, w, h, frame)
    assert utils.find_start_button() is None, why
    ok_w, ok_h = 150, 60                                   # 同一个位置换成合格尺寸就找得到
    frame[cy - bh // 2:cy - bh // 2 + bh, cx - bw // 2:cx - bw // 2 + bw] = _TITLE_BG
    frame[cy - ok_h // 2:cy + ok_h // 2, cx - ok_w // 2:cx + ok_w // 2] = utils._BUTTON_GREEN_RGB
    _screen(monkeypatch, w, h, frame)
    assert utils.find_start_button() is not None


def test_when_several_blobs_look_like_the_button_the_biggest_one_wins(monkeypatch):
    w, h = 1920, 1080
    frame = _gameplay_frame(w, h)
    frame[500:500 + 34, 700:700 + 80] = utils._BUTTON_GREEN_RGB          # 小的, 先被扫到(靠上靠左)
    frame[520:520 + 52, 1000:1000 + 120] = utils._BUTTON_GREEN_RGB       # 大的
    _screen(monkeypatch, w, h, frame)
    x, y = utils.find_start_button()
    assert abs(x - 1060) <= 2 and abs(y - 546) <= 2


def test_a_hollow_green_outline_is_not_a_button(monkeypatch):
    w, h = 1920, 1080
    frame = _gameplay_frame(w, h)
    x0, y0, bw, bh, t = 1000, 503, 115, 48, 3
    frame[y0:y0 + bh, x0:x0 + bw] = utils._BUTTON_GREEN_RGB
    frame[y0 + t:y0 + bh - t, x0 + t:x0 + bw - t] = _TITLE_BG        # 只剩 3px 的框: 填充率远低于按钮
    _screen(monkeypatch, w, h, frame)
    assert utils.find_start_button() is None


def test_a_button_outside_the_title_row_is_ignored(monkeypatch):
    # 死亡页的「继续」在 (959,634), 比「开始」那一行低一截 —— 不在找按钮的那条带里
    w, h = 1920, 1080
    frame = _gameplay_frame(w, h)
    frame[634 - 19:634 + 19, 959 - 50:959 + 50] = utils._BUTTON_GREEN_RGB
    _screen(monkeypatch, w, h, frame)
    monkeypatch.setattr(utils, "on_death_screen", lambda: False)
    assert utils.find_start_button() is None


def test_the_death_screen_is_never_reported_as_the_start_menu(monkeypatch):
    frame, _ = _title_frame(2560, 1440, 4 / 3, offset_ref=220)     # 固定点够不着, 走找按钮那条路
    _screen(monkeypatch, 2560, 1440, frame)
    monkeypatch.setattr(utils, "on_death_screen", lambda: True)
    assert utils.find_start_button() is not None
    assert utils.on_start_screen() is False


def test_finding_the_start_button_never_raises(monkeypatch):
    class Boom:
        def screenshot(self, region=None):
            raise OSError("截屏失败")

    monkeypatch.setattr(utils, "pyautogui", Boom())
    assert utils.find_start_button() is None


def test_click_start_game_clicks_the_button_it_found(monkeypatch):
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils, "on_start_screen", lambda: False)
    monkeypatch.setattr(utils, "find_start_button", lambda: (1852, 702))
    assert utils.click_start_game() is True
    assert spy.moves[0] == (1852, 702)


def test_click_start_game_falls_back_to_the_fixed_point_and_researches_every_attempt(monkeypatch):
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    shown = iter([True, False])
    monkeypatch.setattr(utils, "on_start_screen", lambda: next(shown))
    found = iter([None, (1500, 700)])                  # 第一次没找到 -> 固定点; 第二次找到了 -> 点它
    monkeypatch.setattr(utils, "find_start_button", lambda: next(found))
    assert utils.click_start_game() is True
    clicked = [m for m in spy.moves if m != (utils.SCREEN_WIDTH // 2, utils.SCREEN_HEIGHT // 2)]
    assert clicked == [tuple(utils._START_BUTTON_POS), (1500, 700)]


# ── 死亡页「继续」: 检测点按 florr 真实的界面缩放算, 点的时候按颜色找按钮 ──────────────────────
# 2026-10-07 粉丝反馈死亡时没点到正确位置。2026-10-07 在浏览器里实测 florr 标题页: 界面缩放 =
# max(屏宽/1920, 屏高/1080)(1800x600 下游客按钮宽 192、离中心 -39, 正好是 207 / -42 的 0.9375 倍), 而
# 老代码按 scale_point 横纵各自缩放 —— 只在 16:9 及更窄的屏上碰巧一样。比 16:9 宽的(带鱼屏 / 浏览器没全屏、
# 视口被工具栏压扁)上「继续」实际更靠下, 检测框还能擦到按钮上沿, 点的却是框中心 = 按钮上面。
# 另外死亡页跟着死掉的花走、有滑入动画(录像 rec4 一帧里按钮在 y=519, 停稳后在 634)。
# 下面的死亡面板是用户 1080p 真录像(rec4, 死于工蚁)半分辨率截图里抠的, 「继续」中心在 (44,121)。
_DEATH_PANEL = cv2.cvtColor(
    cv2.imread(str(_Path(__file__).parent / "test_frames" / "death_panel_1080p_half.png")),
    cv2.COLOR_BGR2RGB)            # 88x154: 「你死于 工蚁」/ 死掉的花 / 「继续」/「关闭」
_DEATH_PANEL_BTN = (44, 121)
# 用户 Windows 1920x1080 真标题页(带生态区选择格)的中间一块, 原图 x 560~1360 / y 440~840, 缩成半分辨率
_TITLE_MID = cv2.cvtColor(
    cv2.imread(str(_Path(__file__).parent / "test_frames" / "title_with_biomes_1080p_half.png")),
    cv2.COLOR_BGR2RGB)


def _florr_scale(w, h):
    return max(w / 1920, h / 1080)


def _death_frame(w, h, s, center=None):
    """w x h 的游戏画面上贴一块真死亡面板(按界面缩放 s 放大)。「继续」中心默认放在花停在屏幕中心时它该在的地方
    (中心 + (-1, 94)*s); 浏览器没全屏之类的情况传 center。返回 (整屏图, 按钮中心)。"""
    frame = _gameplay_frame(w, h)
    k = 2.0 * s
    panel = cv2.resize(_DEATH_PANEL, None, fx=k, fy=k, interpolation=cv2.INTER_LINEAR)
    if center is None:
        center = (round(w / 2 - s), round(h / 2 + 94 * s))
    x0, y0 = center[0] - round(_DEATH_PANEL_BTN[0] * k), center[1] - round(_DEATH_PANEL_BTN[1] * k)
    ph, pw = panel.shape[:2]
    frame[y0:y0 + ph, x0:x0 + pw] = panel
    return frame, center


_DEATH_SCREENS = [   # (宽, 高): 16:9 / 16:10 / 4:3 / 5:4, 再加三种比 16:9 宽的
    (1920, 1080), (2560, 1440), (3840, 2160), (1366, 768), (1024, 768), (1920, 1200), (1280, 1024),
    (2560, 1080), (3440, 1440), (5120, 1440),
]


@pytest.mark.parametrize("w,h,want", [
    (1920, 1080, 1.0), (2560, 1440, 4 / 3), (1024, 768, 768 / 1080), (1920, 1200, 1200 / 1080),
    (3440, 1440, 3440 / 1920), (1800, 600, 1800 / 1920), (1920, 950, 1.0),
])
def test_florr_ui_scale_is_the_larger_of_the_two_axis_ratios(monkeypatch, w, h, want):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", w)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", h)
    assert utils.florr_ui_scale() == pytest.approx(want)


@pytest.mark.parametrize("w,h", [(1920, 1080), (2560, 1440), (3840, 2160), (1366, 768), (1024, 768),
                                 (1920, 1200), (1280, 1024)])
def test_continue_check_point_is_unchanged_on_16_9_and_narrower_screens(monkeypatch, w, h):
    # 这些屏上 florr 按高缩放, 跟老的 scale_point 一样 —— 已经能用的人行为不变
    monkeypatch.setattr(utils, "SCREEN_WIDTH", w)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", h)
    new, old = utils._ui_point(959, 634), utils.scale_point(959, 634)
    assert abs(new[0] - old[0]) <= 1 and abs(new[1] - old[1]) <= 1


def test_continue_check_point_follows_florr_scale_on_an_ultrawide(monkeypatch):
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 3440)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 1440)
    assert utils._ui_point(959, 634) == (1718, 888)          # 老的 scale_point 是 (1718, 845), 偏上 43px


def _calibrated_death_frame(w, h, s):
    """给 on_death_screen 的绿占比用。上面那块死亡面板是半分辨率 JPEG, 放大回来绿底边缘被糊进字里, 1080p 下
    30x16 采样框只量到 0.15; 真全分辨率截图量的是 0.376(utils 里 _DEATH_SCREEN_* 上面的注释)。这里画一颗
    同比例的按钮: 76x40 绿底 + 正中 50x24 白块当字 —— 1080p 下采样框里绿占 0.375, 按 s 一起放大。"""
    frame = _gameplay_frame(w, h)
    cx, cy = round(w / 2 - s), round(h / 2 + 94 * s)
    bw, bh, tw, th = round(76 * s), round(40 * s), round(50 * s), round(24 * s)
    frame[cy - bh // 2:cy - bh // 2 + bh, cx - bw // 2:cx - bw // 2 + bw] = utils._BUTTON_GREEN_RGB
    frame[cy - th // 2:cy - th // 2 + th, cx - tw // 2:cx - tw // 2 + tw] = (255, 255, 255)
    return frame


def test_the_calibrated_button_measures_like_the_real_screenshot(monkeypatch):
    _screen(monkeypatch, 1920, 1080, _calibrated_death_frame(1920, 1080, 1.0))
    ratio = utils._green_button_ratio(utils._CONTINUE_BUTTON_POS, utils._DEATH_SCREEN_SAMPLE_HALF_W,
                                      utils._DEATH_SCREEN_SAMPLE_HALF_H)
    assert ratio == pytest.approx(0.376, abs=0.01)


@pytest.mark.parametrize("w,h", _DEATH_SCREENS)
def test_the_death_screen_is_recognised_on_every_screen_shape(monkeypatch, w, h):
    _screen(monkeypatch, w, h, _calibrated_death_frame(w, h, _florr_scale(w, h)))
    assert utils.on_death_screen()


@pytest.mark.parametrize("w,h", _DEATH_SCREENS)
def test_the_death_check_sees_the_button_like_it_did_when_the_threshold_was_set(monkeypatch, w, h):
    # 阈值 0.15 是 1080p 下拿 30x16 框量出来的; 框要跟按钮一起按 florr 缩放, 不然宽屏上框变扁、只框到字
    _screen(monkeypatch, w, h, _calibrated_death_frame(w, h, _florr_scale(w, h)))
    spy = []
    real = utils._green_button_ratio
    monkeypatch.setattr(utils, "_green_button_ratio", lambda *a, **kw: spy.append(real(*a, **kw)) or spy[-1])
    utils.on_death_screen()
    assert spy[0] == pytest.approx(0.376, abs=0.05)          # 小屏上按钮 / 框取整会差一点


@pytest.mark.parametrize("w,h", _DEATH_SCREENS)
def test_continue_button_is_found_on_every_screen_shape(monkeypatch, w, h):
    s = _florr_scale(w, h)
    frame, (cx, cy) = _death_frame(w, h, s)
    _screen(monkeypatch, w, h, frame)
    found = utils.find_continue_button()
    assert found is not None
    assert abs(found[0] - cx) <= 4 * s and abs(found[1] - cy) <= 4 * s


@pytest.mark.parametrize("w,h,top,vh", [(1920, 1080, 90, 950), (2560, 1440, 110, 1290)])
def test_continue_button_is_found_when_the_browser_is_not_fullscreen(monkeypatch, w, h, top, vh):
    # 浏览器最大化但没全屏: 画布从工具栏下面开始、比屏幕矮, 视口比 16:9 宽 -> florr 按视口宽缩放
    s = max(w / 1920, vh / 1080)
    frame, (cx, cy) = _death_frame(w, h, s, center=(round(w / 2 - s), round(top + vh / 2 + 94 * s)))
    _screen(monkeypatch, w, h, frame)
    found = utils.find_continue_button()
    assert found is not None
    assert abs(found[0] - cx) <= 4 * s and abs(found[1] - cy) <= 4 * s


@pytest.mark.parametrize("w,h", [(1920, 1080), (2560, 1440), (3440, 1440)])
def test_the_real_title_screen_is_not_a_death_screen(monkeypatch, w, h):
    # 「开始」按钮 / 生态区格子(花园、丛林也是绿的)都不能被认成「继续」
    s = _florr_scale(w, h)
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = _TITLE_BG
    mid = cv2.resize(_TITLE_MID, None, fx=2 * s, fy=2 * s, interpolation=cv2.INTER_LINEAR)
    mh, mw = mid.shape[:2]
    x0, y0 = round(w / 2 - 400 * s), round(h / 2 - 100 * s)       # 原图 (560,440) 离 1080p 中心 (-400,-100)
    frame[y0:y0 + mh, x0:x0 + mw] = mid
    _screen(monkeypatch, w, h, frame)
    assert utils.find_continue_button() is None
    assert not utils.on_death_screen()


@pytest.mark.parametrize("w,h", [(1920, 1080), (2560, 1440), (3440, 1440)])
def test_gameplay_has_no_continue_button(monkeypatch, w, h):
    _screen(monkeypatch, w, h, _gameplay_frame(w, h))
    assert utils.find_continue_button() is None


@pytest.mark.parametrize("bw,bh,why", [   # 每个只踩一条线(1080p: 宽 50~120, 高 26~64, 宽高比 1.4~2.4)
    (110, 44, "「开始」那种更扁的按钮(宽高比 2.5)"), (60, 56, "差不多是方的(圆怪 / 叶子)"),
    (44, 30, "太窄"), (60, 22, "太矮"), (130, 60, "太宽"), (105, 70, "太高"),
])
def test_green_blobs_that_are_not_the_continue_button_are_ignored(monkeypatch, bw, bh, why):
    w, h = 1920, 1080
    frame = _gameplay_frame(w, h)
    cx, cy = 960, 660
    frame[cy - bh // 2:cy - bh // 2 + bh, cx - bw // 2:cx - bw // 2 + bw] = utils._BUTTON_GREEN_RGB
    _screen(monkeypatch, w, h, frame)
    assert utils.find_continue_button() is None, why
    frame[cy - bh // 2:cy - bh // 2 + bh, cx - bw // 2:cx - bw // 2 + bw] = _TITLE_BG
    frame[cy - 20:cy + 20, cx - 38:cx + 38] = utils._BUTTON_GREEN_RGB        # 同一个位置换成「继续」的尺寸就找得到
    _screen(monkeypatch, w, h, frame)
    assert utils.find_continue_button() is not None


@pytest.mark.parametrize("cy,why", [(560, "在屏幕中心那一行(「开始」的位置)"), (1000, "在最底下(花瓣栏的位置)")])
def test_a_continue_shaped_blob_outside_the_band_below_center_is_ignored(monkeypatch, cy, why):
    frame = _gameplay_frame(1920, 1080)
    frame[cy - 20:cy + 20, 960 - 38:960 + 38] = utils._BUTTON_GREEN_RGB
    _screen(monkeypatch, 1920, 1080, frame)
    assert utils.find_continue_button() is None, why


def test_finding_the_continue_button_never_raises(monkeypatch):
    class Boom:
        def screenshot(self, region=None):
            raise OSError("截屏失败")

    monkeypatch.setattr(utils, "pyautogui", Boom())
    assert utils.find_continue_button() is None


class _ScreenClickStub(_ScreenStub):
    """既能截屏又记点击; 点一下之后画面换成 after(按钮消失)。"""

    def __init__(self, frame, after):
        super().__init__(frame)
        self.after = after
        self.moves = []
        self.clicks = 0

    def moveTo(self, pos=None, *_a, **_kw):
        self.moves.append(tuple(pos))

    def click(self, *_a, **_kw):
        self.clicks += 1
        self.frame = self.after


@pytest.mark.parametrize("w,h,top,vh", [
    (1920, 1080, 0, 1080), (2560, 1080, 0, 1080), (3440, 1440, 0, 1440), (5120, 1440, 0, 1440),
    (1920, 1080, 90, 950), (2560, 1440, 110, 1290),          # 浏览器没全屏: 按钮不在算出来的检测点上
])
def test_click_continue_after_death_hits_the_real_button(monkeypatch, w, h, top, vh):
    s = max(w / 1920, vh / 1080)
    frame, (cx, cy) = _death_frame(w, h, s, center=(round(w / 2 - s), round(top + vh / 2 + 94 * s)))
    _screen(monkeypatch, w, h, frame)
    stub = _ScreenClickStub(frame, _gameplay_frame(w, h))
    monkeypatch.setattr(utils, "pyautogui", stub)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    assert utils.click_continue_after_death() is True
    x, y = stub.moves[0]
    assert abs(x - cx) <= 30 * s and abs(y - cy) <= 12 * s       # 按钮约 76x40(1080p), 落在按钮里面


def test_click_continue_waits_for_the_sliding_panel_to_stop(monkeypatch):
    # 死亡页滑进来的时候按钮还在动: 连续两次找到的位置一致了才点
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils, "on_death_screen", lambda: False)
    seen = iter([(960, 519), (960, 590), (960, 633), (960, 634), (960, 634)])
    monkeypatch.setattr(utils, "find_continue_button", lambda: next(seen))
    assert utils.click_continue_after_death() is True
    assert spy.moves == [(960, 634)]


def test_click_continue_falls_back_to_the_check_point_when_nothing_is_found(monkeypatch):
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(utils, "on_death_screen", lambda: False)
    monkeypatch.setattr(utils, "find_continue_button", lambda: None)
    assert utils.click_continue_after_death() is True
    assert spy.moves == [tuple(utils._CONTINUE_BUTTON_POS)]


def test_select_biome_on_title_clicks_desert_button(monkeypatch):
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    utils.select_biome_on_title("desert")
    assert spy.moves == [tuple(utils._BIOME_BUTTON_POS["desert"])]
    assert spy.clicks == 2          # 先点一下抢焦点、再点一下真命中


def test_select_biome_on_title_clicks_garden_button(monkeypatch):
    # 蚁穴走花园进场, 所以蚁穴时块传进来的 biome 是 "garden" 而不是 "anthell".
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    utils.select_biome_on_title("garden")
    assert spy.moves == [tuple(utils._BIOME_BUTTON_POS["garden"])]
    assert spy.clicks == 2


def test_biome_button_positions_are_calibrated_constants():
    # 2026-09-15 实机标定值. 花园跟沙漠同一行、在它左边, y 基本齐平 —— 标题页那个
    # 格子网格的几何关系, 哪天有人手滑改错一个数字这条会拦住.
    assert utils._BIOME_BUTTON_POS["desert"] == utils.scale_point(977, 596)
    assert utils._BIOME_BUTTON_POS["garden"] == utils.scale_point(861, 599)
    gx, gy = utils._BIOME_BUTTON_POS["garden"]
    dx, dy = utils._BIOME_BUTTON_POS["desert"]
    assert gx < dx and abs(gy - dy) <= utils.scale_y(10)


# 1920x1080 标题页截图(debug_marked_pos.png)上量的按钮外框: (x0, x1, y0, y1)
_TITLE_GRID_BOXES = {
    "garden": (814, 904, 590, 617), "desert": (913, 1007, 590, 617),
    "ocean": (1013, 1107, 590, 617), "jungle": (864, 956, 624, 651),
}


@pytest.mark.parametrize("biome", sorted(_TITLE_GRID_BOXES))
def test_every_biome_button_point_lies_inside_its_measured_box(biome):
    x0, x1, y0, y1 = _TITLE_GRID_BOXES[biome]
    lo = utils.scale_point(x0, y0)
    hi = utils.scale_point(x1, y1)
    x, y = utils._BIOME_BUTTON_POS[biome]
    assert lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1]


def test_ocean_and_jungle_button_points_are_the_box_centres():
    assert utils._BIOME_BUTTON_POS["ocean"] == utils.scale_point(1060, 603)
    assert utils._BIOME_BUTTON_POS["jungle"] == utils.scale_point(910, 637)
    ox, oy = utils._BIOME_BUTTON_POS["ocean"]
    jx, jy = utils._BIOME_BUTTON_POS["jungle"]
    assert jy > oy and jx < ox            # 丛林在第二行, 靠左


@pytest.mark.parametrize("biome", ["ocean", "jungle"])
def test_select_biome_on_title_clicks_the_new_buttons(monkeypatch, biome):
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    utils.select_biome_on_title(biome)
    assert spy.moves == [tuple(utils._BIOME_BUTTON_POS[biome])]
    assert spy.clicks == 2


def test_ocean_and_jungle_share_the_garden_minimap_scale():
    # 世界一样大(121 格 = 61952), 小地图缩放 300/(61952+2000) 就一样
    s = utils.MINIMAP_WORLD_SCALE
    assert s["ocean"] == s["jungle"] == s["garden"]
    assert abs(s["ocean"] - 300.0 / (61952 + 2000)) < 1e-9


def test_select_biome_on_title_noop_for_unmapped_biome(monkeypatch):
    # "anthell" 也在这里: 它永远不该被当成生态区 key 传进来(蚁穴传的是 "garden"),
    # 万一传了也只是不点, 不会乱点别的格子.
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    for biome in ("ant_hell", "anthell", "", None):
        utils.select_biome_on_title(biome)
    assert spy.moves == [] and spy.clicks == 0


# ── on_guest_screen: 未登录标题页的「以游客身份游玩」绿按钮检测 ──────────────

def _stub_guest_ratio(monkeypatch, value):
    """把 _green_button_ratio 打桩成定值, 只测 on_guest_screen 的阈值判定."""
    seen = {}

    def fake_ratio(pos, half_w=15, half_h=10):
        seen["pos"] = pos
        seen["half_w"] = half_w
        seen["half_h"] = half_h
        return value

    monkeypatch.setattr(utils, "_green_button_ratio", fake_ratio)
    return seen


def test_on_guest_screen_true_when_green_ratio_above_threshold(monkeypatch):
    seen = _stub_guest_ratio(monkeypatch, 0.5)
    assert utils.on_guest_screen() is True
    # 采样的是游客按钮坐标, 用的是宽框(不是 _green_button_ratio 的默认 15x10)
    assert seen["pos"] == utils._PLAY_AS_GUEST_POS
    assert seen["half_w"] == utils._GUEST_SCREEN_SAMPLE_HALF_W
    assert seen["half_h"] == utils._GUEST_SCREEN_SAMPLE_HALF_H


def test_on_guest_screen_false_at_exact_threshold(monkeypatch):
    _stub_guest_ratio(monkeypatch, utils._GUEST_SCREEN_GREEN_THRESHOLD)
    assert utils.on_guest_screen() is False          # 严格 >


def test_on_guest_screen_false_when_mostly_background(monkeypatch):
    _stub_guest_ratio(monkeypatch, 0.05)
    assert utils.on_guest_screen() is False


def test_play_as_guest_pos_is_scaled_from_reference():
    # _PLAY_AS_GUEST_POS 在 import 时按 1920x1080 参照缩放到实际分辨率. 用
    # scale_point 比较而非写死元组 —— 非参照分辨率的开发/CI 机器上也成立, 同时
    # 仍钉住 960/498 这两个字面量.
    assert utils._PLAY_AS_GUEST_POS == utils.scale_point(960, 498)


# ── click_play_as_guest: 点掉登录选择页 ──────────────────────────────────

def test_click_play_as_guest_stops_after_page_gone_on_first_try(monkeypatch):
    spy = _patch_click(monkeypatch, [False], screen_attr="on_guest_screen")
    assert utils.click_play_as_guest() is True
    assert spy.clicks == 2                       # 一轮 = connect click + 真点


def test_click_play_as_guest_retries_until_page_gone(monkeypatch):
    spy = _patch_click(monkeypatch, [True, True, False], screen_attr="on_guest_screen")
    assert utils.click_play_as_guest() is True
    assert spy.clicks == 2 * 3


def test_click_play_as_guest_gives_up_after_max_attempts_without_crashing(monkeypatch):
    spy = _patch_click(monkeypatch, [True], screen_attr="on_guest_screen")
    assert utils.click_play_as_guest() is False
    assert spy.clicks == 2 * utils._CONFIRM_CLICK_MAX_ATTEMPTS


def test_check_map_border_returns_empty_on_uncalibrated_map(monkeypatch):
    # 改之前: MAP 不是 desert/ocean 时 if/elif 两个分支都不进, target_color 未绑定,
    # 下一行直接 UnboundLocalError —— 蚁穴/花园上 execute_anti_stuck() 必崩.
    monkeypatch.setattr(utils, "MAP", "anthell")
    img = np.zeros((20, 20, 3), dtype=np.uint8)
    assert utils.check_map_border(img) == []


def test_check_map_border_returns_empty_when_map_unset(monkeypatch):
    # utils.MAP 的初值就是空串(apply_map 还没被调用过), 同样不能崩.
    monkeypatch.setattr(utils, "MAP", "")
    assert utils.check_map_border(np.zeros((20, 20, 3), dtype=np.uint8)) == []


def test_check_map_border_still_finds_desert_wall_pixels(monkeypatch):
    # 已标定的图行为不变: 画一块沙漠墙色(4f3422)的方块, 应该能描出轮廓点.
    monkeypatch.setattr(utils, "MAP", "desert")
    img = np.zeros((60, 60, 3), dtype=np.uint8)
    img[15:45, 15:45] = (0x22, 0x34, 0x4f)   # BGR
    assert len(utils.check_map_border(img)) > 0


def test_check_map_border_color_table_covers_the_two_calibrated_maps():
    assert utils._MAP_BORDER_COLOR == {"ocean": "4c4950", "desert": "4f3422"}


def test_preprocess_map_does_not_write_anything_without_out_path(monkeypatch):
    # 改之前这里硬编码 cv2.imwrite('./maps/anthell.png', binary) —— 任何调用都会
    # 悄悄覆盖蚁穴地图.
    writes = []
    monkeypatch.setattr(utils.cv2, "imwrite", lambda p, i: writes.append(p))
    utils.preprocess_map(np.zeros((300, 300, 3), dtype=np.uint8))
    assert writes == []


def test_preprocess_map_writes_to_the_given_path(monkeypatch):
    writes = []
    monkeypatch.setattr(utils.cv2, "imwrite", lambda p, i: writes.append(p))
    utils.preprocess_map(np.zeros((300, 300, 3), dtype=np.uint8),
                         out_path="maps/garden.png")
    assert writes == ["maps/garden.png"]


def test_preprocess_map_threshold_decides_walkable():
    mid_grey = np.full((10, 10, 3), 150, dtype=np.uint8)
    assert (utils.preprocess_map(mid_grey, threshold=200) == 0).all()     # 线以下 = 墙
    assert (utils.preprocess_map(mid_grey, threshold=100) == 255).all()   # 线以上 = 可走


_PORTAL_BGR = (107, 228, 111)   # 传送点绿的实测色 (HSV 59,135,228), 灰度 179


def _two_portals():
    """一张背景全黑、放了两个传送点绿斑的假小地图: 目标那个在 (5,5), 另一个在 (15,15)."""
    img = np.zeros((20, 20, 3), dtype=np.uint8)
    img[4:7, 4:7] = _PORTAL_BGR
    img[14:17, 14:17] = _PORTAL_BGR
    return img


def test_preprocess_map_leaves_portal_green_as_wall_by_default():
    # 不传 open_portal_at 就一个都不开: 传送点是"踩上去会把你传走"的东西, 默认
    # 当墙让寻路绕开才安全.
    out = utils.preprocess_map(_two_portals())
    assert out[5, 5] == 0 and out[15, 15] == 0


def test_preprocess_map_keeps_portals_walls_even_below_the_threshold():
    """传送点是不是墙, 不能由亮度阈值说了算。

    它的灰度是 179。默认阈值 200 下面本来就是墙, 所以这条在默认值上看不出差别 ——
    但蚁穴图必须用 threshold=160 才能把细走廊接上(见 test_map_routes 那条), 而
    179 > 160, 不强制的话三个传送点会跟着悄悄变可走, bot 刷怪时踩上去就被传走。
    """
    out = utils.preprocess_map(_two_portals(), threshold=100)
    assert out[5, 5] == 0 and out[15, 15] == 0, "阈值一低传送点就漏成可走了"
    # 同一张图上真正的亮地面在这个阈值下该是可走的 —— 证明低阈值本身生效了,
    # 上面两个 0 是"被强制成墙", 不是"整张图都没过阈值".
    img = _two_portals()
    img[0:3, 0:3] = (180, 180, 180)
    assert (utils.preprocess_map(img, threshold=100)[0:3, 0:3] == 255).all()


def test_preprocess_map_opens_only_the_requested_portal():
    # 花园里这样的绿点有 7 个, 只有一个是蚁穴入口. 全开的话 bot 可能在去蚁穴的
    # 路上踩中别的传送点被拽走 —— 所以只开点名的那一个, 其余留着当墙.
    out = utils.preprocess_map(_two_portals(), open_portal_at=(5, 5))
    assert (out[4:7, 4:7] == 255).all(), "点名的传送点没被打通"
    assert (out[14:17, 14:17] == 0).all(), "别的传送点被一起打通了 —— 会被误传走"
    assert out[0, 0] == 0, "背景不该被带成可走"


def test_preprocess_map_open_portal_at_a_non_portal_point_changes_nothing():
    # 坐标写歪了(没落在绿斑上)时不猜、不就近吸附 —— 原样返回, 让
    # test_portal_is_walkable_on_the_shipped_garden_map 那条去发现.
    img = _two_portals()
    assert (utils.preprocess_map(img, open_portal_at=(0, 0))
            == utils.preprocess_map(img)).all()


def test_preprocess_map_portal_mask_leaves_dark_background_alone():
    # 花园背景是暗绿(HSV V=24) —— 跟传送点同色相, 只有亮度区分得开. mask 的 V 下限
    # 就是干这个的; 收窄/写错的话整张图会糊成一片"可走".
    dark_green = np.zeros((10, 10, 3), dtype=np.uint8)
    dark_green[:, :] = (14, 24, 4)
    assert (utils.preprocess_map(dark_green, open_portal_at=(5, 5)) == 0).all()


def test_preprocess_map_leaves_shortcuts_as_wall_by_default():
    # 不传 open_shortcuts_at 就不动 —— 密道静态截图里本来就长得跟真墙一模一样,
    # 默认行为该跟没有这个机制之前完全一致.
    all_dark = np.zeros((20, 20, 3), dtype=np.uint8)
    assert (utils.preprocess_map(all_dark) == 0).all()


def test_preprocess_map_opens_only_the_declared_shortcut_rect():
    # 密道矩形跟传送点斑不一样, 精确到标定范围, 矩形外一个像素都不该被带着变可走
    # (密道所在的墙经常跟外墙连成一片, 按连通块开的话会把不该开的墙一起打穿).
    all_dark = np.zeros((20, 20, 3), dtype=np.uint8)
    out = utils.preprocess_map(all_dark, open_shortcuts_at=[(4, 4, 8, 6)])
    assert (out[4:7, 4:9] == 255).all(), "标定范围内没被打通"
    out[4:7, 4:9] = 0   # 清掉矩形本身, 剩下的必须全是原来的墙(0)
    assert (out == 0).all(), "矩形外的墙被一起带开了"


def test_preprocess_map_opens_multiple_shortcut_rects_independently():
    all_dark = np.zeros((20, 20, 3), dtype=np.uint8)
    out = utils.preprocess_map(all_dark, open_shortcuts_at=[(0, 0, 1, 1), (10, 10, 12, 12)])
    assert (out[0:2, 0:2] == 255).all()
    assert (out[10:13, 10:13] == 255).all()
    assert out[5, 5] == 0   # 两个矩形之间没被带着开


# ---- 地图感知脱困(没标定墙壁色的图: 蚁穴/花园) ----

def _room_and_corridor():
    """一条 2 像素宽的横走廊(y=10..11, x=2..19)通向右边一个大房间(x=20..44, y=2..20)."""
    m = np.zeros((25, 50), dtype=np.uint8)
    m[10:12, 2:20] = 255
    m[2:21, 20:45] = 255
    return m


def test_map_escape_step_moves_away_from_the_wall_toward_open_space():
    # 贴着大房间左墙站着(x=21), 净空最小 —— 该往房间里面(x 变大)走, 不是往墙里.
    m = np.zeros((25, 50), dtype=np.uint8)
    m[2:21, 20:45] = 255
    utils.random.seed(0)
    step = utils.map_escape_step(m, (20, 11))
    assert step is not None and step[0] > 20


def test_map_escape_step_follows_the_corridor_not_a_straight_line_through_walls():
    # 走廊里的角色, 净空更大的地方(房间)在右边 —— 第一步必须还在走廊里(可走),
    # 不能是穿墙直线上的墙像素.
    m = _room_and_corridor()
    utils.random.seed(0)
    step = utils.map_escape_step(m, (4, 10))
    assert step is not None
    assert m[step[1], step[0]] == 255
    assert step[0] > 4


def test_map_escape_step_returns_none_when_already_at_the_widest_spot():
    # 全图都是同样宽的空地, 挑不出"明显更好"的地方 -> None(调用方退回随机).
    m = np.full((11, 11), 255, dtype=np.uint8)
    assert utils.map_escape_step(m, (5, 5)) is None


def test_map_escape_step_returns_none_for_wall_or_out_of_bounds_position():
    m = _room_and_corridor()
    assert utils.map_escape_step(m, (0, 0)) is None        # 墙
    assert utils.map_escape_step(m, (999, 999)) is None    # 越界
    assert utils.map_escape_step(None, (5, 5)) is None     # 没地图


def test_map_escape_step_picks_among_near_equal_candidates_not_always_the_same():
    # 走廊中间两头各有一个一样大的房间 —— 多次调用不该永远选同一头(被挡住时会死循环).
    m = np.zeros((25, 62), dtype=np.uint8)
    m[10:12, 20:42] = 255
    m[2:21, 2:20] = 255
    m[2:21, 42:60] = 255
    seen = set()
    for seed in range(20):
        utils.random.seed(seed)
        step = utils.map_escape_step(m, (30, 10))
        seen.add(step[0] < 30)
    assert seen == {True, False}


def _stub_escape_env(monkeypatch, *, map_name, binary_map, pos):
    monkeypatch.setattr(utils, "MAP", map_name)
    monkeypatch.setattr(utils.pyautogui, "screenshot",
                        lambda **k: Image.new("RGB", (64, 64)))
    monkeypatch.setattr(utils, "SCREEN_WIDTH", 64)
    monkeypatch.setattr(utils, "SCREEN_HEIGHT", 64)
    monkeypatch.setattr(utils, "get_player_position", lambda *a, **k: pos)
    monkeypatch.setattr(utils, "load_binary_map", lambda: binary_map)
    monkeypatch.setattr(utils.time, "sleep", lambda *a, **k: None)
    moves = []
    monkeypatch.setattr(utils.pyautogui, "moveTo", lambda *a, **k: moves.append(a))
    randoms = []
    monkeypatch.setattr(utils, "keydown", lambda d, *a, **k: randoms.append(d))
    monkeypatch.setattr(utils, "keyup", lambda d, *a, **k: None)
    return moves, randoms


def test_execute_anti_stuck_uses_the_map_on_an_uncalibrated_map(monkeypatch):
    # 蚁穴没标定墙壁色 -> borders 空 -> 以前恒随机蒙方向; 现在走地图脱困.
    moves, randoms = _stub_escape_env(monkeypatch, map_name="anthell",
                                      binary_map=_room_and_corridor(), pos=(4, 10))
    utils.random.seed(0)
    utils.execute_anti_stuck(duration=0.5)
    assert randoms == []          # 没走随机方向
    assert len(moves) >= 1        # 真的转向推了


def test_execute_anti_stuck_falls_back_to_random_when_the_map_is_unavailable(monkeypatch):
    moves, randoms = _stub_escape_env(monkeypatch, map_name="anthell",
                                      binary_map=None, pos=(4, 10))
    utils.execute_anti_stuck(duration=0.5)
    assert len(randoms) == 1      # 读不到地图 -> 退回原来的随机方向


def test_execute_anti_stuck_leaves_calibrated_maps_on_the_old_path(monkeypatch):
    # 沙漠有标定墙壁色: 行为不变, 就算力度弱落进随机分支也不去碰地图脱困.
    moves, randoms = _stub_escape_env(monkeypatch, map_name="desert",
                                      binary_map=_room_and_corridor(), pos=(4, 10))
    monkeypatch.setattr(utils, "_map_aware_escape",
                        lambda d: pytest.fail("沙漠不该走地图脱困"))
    utils.execute_anti_stuck(duration=0.5)
    assert len(randoms) == 1


# ── 位置先读画布里的小地图点 (2026-09-28 蚁穴录像) ─────────────────────────────
# 截图认小地图金点 11 分钟里读丢 40 次(每次: 寻路判卡住乱冲 / 刷怪整轮停 1 秒不躲),
# 而同一时刻画布里那个点一直都在; 两者读数差 1px 以内。

def test_canvas_position_converts_minimap_world_to_map_pixels(monkeypatch):
    # 录像实测: 蚁穴世界 (7661, 19176) 的那一帧, 小地图点在 (40.0, 93.2)
    monkeypatch.setattr(utils, "MAP", "anthell")
    monkeypatch.setattr(utils.cdp_bridge, "_eval_value", lambda js, timeout=5: [7661.0, 19176.0])
    x, y = utils.canvas_player_position(precise=True)
    assert x == pytest.approx(40.0, abs=0.1) and y == pytest.approx(93.2, abs=0.1)


def test_canvas_position_is_calibrated_like_the_pixel_one(monkeypatch):
    monkeypatch.setattr(utils, "MAP", "anthell")
    monkeypatch.setattr(utils.cdp_bridge, "_eval_value", lambda js, timeout=5: [7661.0, 19176.0])
    seen = {}

    def calib(m, pos):
        seen["pos"] = pos
        return (1, 2)
    monkeypatch.setattr(utils, "calibrate_player", calib)
    monkeypatch.setattr(utils, "load_binary_map", lambda: "map")
    assert utils.canvas_player_position() == (1, 2) and seen["pos"] == (40, 93)


@pytest.mark.parametrize("value", [None, [], [1e9, 5.0], "boom"])
def test_canvas_position_gives_up_quietly(monkeypatch, value):
    monkeypatch.setattr(utils, "MAP", "anthell")

    def ev(js, timeout=5):
        if value == "boom":
            raise ConnectionError("没开 CDP")
        return value
    monkeypatch.setattr(utils.cdp_bridge, "_eval_value", ev)
    assert utils.canvas_player_position(precise=True) is None


# 工厂(2026-10-04 录像): 画布里小地图的 CTM 缩放实测 0.0062396, 跟预测 300/(46080+2000) 在 7 位上一样。
# 之前它不在 MINIMAP_WORLD_SCALE 里 -> 位置只能走截图认金点, 12 分钟里读丢 46 次(每次寻路当"卡住"乱冲脱困、
# 刷怪整轮停 1 秒), 而这 46 次丢的当口画布里那个点都在。下面是同一份录像里 worker 日志(截图读的位置)
# 和同一刻画布世界坐标的对照, 差都在 1.5 像素以内。
@pytest.mark.parametrize("world,screenshot_pos", [
    ((15167.6, 40999.7), (101, 262)),
    ((26270.0, 25576.9), (170, 165)),
    ((25368.1, 14906.9), (164, 99)),
    ((21047.5, 9642.6), (137, 66)),
    ((25750.8, 8990.4), (167, 62)),
    ((22048.4, 9914.8), (144, 68)),
    ((20287.8, 12304.8), (132, 82)),
])
def test_factory_canvas_position_matches_what_the_screenshot_path_read_in_the_real_recording(
        monkeypatch, world, screenshot_pos):
    monkeypatch.setattr(utils, "MAP", "factory")
    monkeypatch.setattr(utils.cdp_bridge, "_eval_value", lambda js, timeout=5: list(world))
    x, y = utils.canvas_player_position(precise=True)
    assert abs(x - screenshot_pos[0]) <= 1.5 and abs(y - screenshot_pos[1]) <= 1.5


def test_factory_scale_is_the_measured_one():
    assert utils.MINIMAP_WORLD_SCALE["factory"] == pytest.approx(300.0 / (90 * 512 + 2000), rel=1e-9)
    assert utils.MINIMAP_WORLD_SCALE["factory"] == pytest.approx(0.0062396, abs=1e-7)   # 录像里画布的值


def test_factory_position_goes_through_the_canvas_not_the_screenshot(monkeypatch):
    monkeypatch.setattr(utils, "MAP", "factory")
    monkeypatch.setattr(utils.cdp_bridge, "_eval_value", lambda js, timeout=5: [26270.0, 25576.9])
    monkeypatch.setattr(utils, "load_binary_map", lambda: "map")
    monkeypatch.setattr(utils, "calibrate_player", lambda m, pos: pos)
    monkeypatch.setattr(utils, "get_map", lambda: pytest.fail("画布有就不截图"))
    assert utils.get_player_position() == (170, 166)


def test_canvas_position_needs_a_measured_scale_for_the_map(monkeypatch):
    monkeypatch.setattr(utils, "MAP", "sewers")       # 没实测过缩放的图 -> 不猜(海洋/丛林现在有了)
    monkeypatch.setattr(utils.cdp_bridge, "_eval_value",
                        lambda js, timeout=5: pytest.fail("不该去读"))
    assert utils.canvas_player_position() is None


def test_get_player_position_prefers_the_canvas(monkeypatch):
    monkeypatch.setattr(utils, "canvas_player_position", lambda precise=False: (7, 8))
    monkeypatch.setattr(utils, "get_map", lambda: pytest.fail("画布有就不截图"))
    assert utils.get_player_position() == (7, 8)


def test_get_player_position_falls_back_to_the_screenshot(monkeypatch):
    monkeypatch.setattr(utils, "canvas_player_position", lambda precise=False: None)
    monkeypatch.setattr(utils, "get_map", lambda: "img")
    monkeypatch.setattr(utils, "load_binary_map", lambda: "map")
    monkeypatch.setattr(utils, "get_player_location_on_map",
                        lambda img, color, m, precise: (3, 4) if img == "img" else None)
    assert utils.get_player_position() == (3, 4)


# ── calibrate_player 提速: 结果必须跟原来逐像素找的一模一样 (2026-09-28) ─────────
# 原实现每次读位置都 Python 遍历 9 万个像素(Mac 上 155ms), 寻路/刷怪每 tick 都读, 第三份
# 录像里躲究极的反应慢了 1.6 秒, 这是大头之一。

def _calibrate_bruteforce(m, pos):
    walkable = (m == 255).astype(np.uint8)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(walkable, connectivity=8)
    if num <= 1:
        return pos
    main = max(range(1, num), key=lambda i: stats[i, cv2.CC_STAT_AREA])
    best, best_d = pos, float("inf")
    for y in range(m.shape[0]):
        for x in range(m.shape[1]):
            if labels[y, x] == main:
                d = ((x - pos[0]) ** 2 + (y - pos[1]) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = (x, y), d
    return best


def test_fast_calibrate_matches_the_bruteforce_one_including_ties():
    rng = np.random.default_rng(7)
    for trial in range(6):
        m = np.where(rng.random((40, 50)) < 0.55, 255, 0).astype(np.uint8)
        for pos in [(0, 0), (49, 39), (25, 20), (3, 37), (-5, 10), (60, 60)]:
            assert utils.calibrate_player(m, pos) == _calibrate_bruteforce(m, pos), (trial, pos)


def test_fast_calibrate_on_the_real_anthell_map():
    import cv2 as _cv2
    m = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    for pos in [(47, 99), (121, 102), (18, 91), (200, 5)]:
        got = utils.calibrate_player(m, pos)
        assert got == _calibrate_bruteforce(m, pos)
        assert isinstance(got[0], int) and isinstance(got[1], int)


def test_fast_calibrate_is_fast():
    import time
    import cv2 as _cv2
    m = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    utils.calibrate_player(m, (47, 99))               # 第一次建缓存
    t = time.time()
    for _ in range(50):
        utils.calibrate_player(m.copy(), (47, 99))    # 每次都是新数组(load_binary_map 就是这样)
    assert (time.time() - t) / 50 < 0.01


def test_open_map_rects_carves_the_loaded_map_and_apply_map_clears_it(monkeypatch, tmp_path):
    import cv2
    import numpy as np
    (tmp_path / "maps").mkdir()
    cv2.imwrite(str(tmp_path / "maps" / "toy.png"), np.zeros((300, 300), np.uint8))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(utils, "MAP", "")
    monkeypatch.setattr(utils, "MAP_OPEN_RECTS", ())

    utils.apply_map("toy")
    assert (utils.load_binary_map() == 0).all()
    utils.open_map_rects([(10, 20, 12, 21)])
    carved = utils.load_binary_map()
    assert int((carved == 255).sum()) == 6 and (carved[20:22, 10:13] == 255).all()
    utils.apply_map("toy")                       # 重新 apply = 换段了, 门关上
    assert (utils.load_binary_map() == 0).all()


def test_open_map_rects_with_nothing_is_a_no_op(monkeypatch):
    monkeypatch.setattr(utils, "MAP_OPEN_RECTS", ((1, 1, 2, 2),))
    utils.open_map_rects(())
    assert utils.MAP_OPEN_RECTS == ()


def test_minimap_scale_hints_agree_with_the_measured_scales():
    for name in ("garden", "anthell"):
        assert abs(utils.MINIMAP_SCALE_HINTS[name] - utils.MINIMAP_WORLD_SCALE[name]) < 1e-9
    # 工厂 2026-10-04 实测过了(下面那几条); 下水道只是按同一条公式预测的, 没实测 —— 只能用来分类,
    # 不能进"位置读数信画布"的那张表
    assert abs(utils.MINIMAP_SCALE_HINTS["factory"] - utils.MINIMAP_WORLD_SCALE["factory"]) < 1e-9
    assert "sewers" not in utils.MINIMAP_WORLD_SCALE
    assert abs(utils.minimap_scale_for_world(90) - 300.0 / (46080 + 2000)) < 1e-12


def test_map_for_minimap_scale_picks_the_unique_candidate():
    h = utils.MINIMAP_SCALE_HINTS
    assert utils.map_for_minimap_scale(h["sewers"], ["garden", "sewers"]) == "sewers"
    assert utils.map_for_minimap_scale(h["garden"], ["garden", "sewers"]) == "garden"
    assert utils.map_for_minimap_scale(h["anthell"], ["garden", "anthell"]) == "anthell"   # 只差 1.6%
    assert utils.map_for_minimap_scale(h["sewers"], ["garden", "factory"]) == "factory"
    assert utils.map_for_minimap_scale(h["sewers"], ["sewers", "factory"]) is None   # 一样大: 分不出
    assert utils.map_for_minimap_scale(0.1, ["garden", "sewers"]) is None
    assert utils.map_for_minimap_scale(None, ["garden"]) is None
    assert utils.map_for_minimap_scale(h["sewers"], ["garden", "nope"]) is None


def test_map_for_minimap_scale_normalises_the_ui_scale():
    h = utils.MINIMAP_SCALE_HINTS
    assert utils.map_for_minimap_scale(h["sewers"] * 0.5, ["garden", "sewers"], ui_scale=0.5) == "sewers"
    assert utils.map_for_minimap_scale(h["sewers"] * 0.5, ["garden", "sewers"], ui_scale=1.0) is None


def test_map_for_minimap_scale_needs_a_margin_over_the_runner_up():
    # 花园和蚁穴的提示值只差 1.58%, 容差 0.5% —— 读数偏了 0.4% 就已经"离花园近、离蚁穴也不算远",
    # 而认错的代价是拿蚁穴的图在花园里刷(第六份录像卡了 13 分钟)。要求次优至少差 3 倍才认。
    h = utils.MINIMAP_SCALE_HINTS
    # 读数偏小就是往蚁穴那个值靠: 偏 0.4% 时离蚁穴只剩 1.20%(不到 3 倍) -> 不认
    assert utils.map_for_minimap_scale(h["garden"] * 0.996, ["garden", "anthell"]) is None
    assert utils.map_for_minimap_scale(h["garden"] * 0.997, ["garden", "anthell"]) == "garden"
    assert utils.map_for_minimap_scale(h["garden"] * 1.003, ["garden", "anthell"]) == "garden"
    assert utils.map_for_minimap_scale(h["garden"] * 1.01, ["garden", "anthell"]) is None
    # 边距只对"分不开的一对"起作用: 花园 vs 下水道差 33%, 容差内的读数照旧认得出
    assert utils.map_for_minimap_scale(h["garden"] * 0.996, ["garden", "sewers"]) == "garden"
    # 提示值一模一样的两张(下水道/工厂, 世界都是 90 格): 次优跟最优一样近 = 永远分不出
    assert utils.map_for_minimap_scale(h["sewers"], ["sewers", "factory"]) is None


def test_canvas_minimap_scale_peeks_and_never_clears_the_log(monkeypatch):
    # 清 __canvasLog 会把 scan_enemies / canvas_player_world 的帧偷走 —— 读缩放比只能偷看,
    # 跟 canvas_zone_map / canvas_player_world 走同一条路(main._canvas_scale_zone 原来走 drain)。
    seen = {}

    def ev(js, timeout=5):
        seen["js"] = js
        return 0.00624
    monkeypatch.setattr(utils.cdp_bridge, "_eval_value", ev)
    assert utils.canvas_minimap_scale() == 0.00624
    assert "__canvasLog" in seen["js"]
    assert not re.search(r"__canvasLog\s*=", seen["js"])    # 只读, 不赋值 = 不清空


@pytest.mark.parametrize("value", [None, 0, "boom", "nope"])
def test_canvas_minimap_scale_gives_up_quietly(monkeypatch, value):
    def ev(js, timeout=5):
        if value == "boom":
            raise ConnectionError("没开 CDP")
        return value
    monkeypatch.setattr(utils.cdp_bridge, "_eval_value", ev)
    assert utils.canvas_minimap_scale() is None
