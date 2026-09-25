import os

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


def test_select_biome_on_title_noop_for_unmapped_biome(monkeypatch):
    # "anthell" 也在这里: 它永远不该被当成生态区 key 传进来(蚁穴传的是 "garden"),
    # 万一传了也只是不点, 不会乱点别的格子.
    spy = _MoveClickSpy()
    monkeypatch.setattr(utils, "pyautogui", spy)
    monkeypatch.setattr(utils.time, "sleep", lambda *_a, **_kw: None)
    for biome in ("ocean", "ant_hell", "anthell", "", None):
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
