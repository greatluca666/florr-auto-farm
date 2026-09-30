import pytest
import map_routes


def test_anthell_routes_through_the_garden_server():
    # 整个设计的支点: 蚁穴不是标题页生态区, 只能从花园服跑到固定洞口踩进去.
    # 所以 select_biome_on_title / switch_server 都要拿 garden(BIOME_INDEX 0),
    # 不是 ant_hell(4) —— 换到 ant_hell 服等于换到一个进不去的地方.
    assert map_routes.route_for("anthell").server_biome == "garden"


def test_anthell_has_two_stages_garden_then_anthell():
    r = map_routes.route_for("anthell")
    assert [s.map_name for s in r.stages] == ["garden", "anthell"]
    assert r.final_map == "anthell"
    assert r.stages[-1].walk_to is None      # 最后一段不用走, 到了就开始刷怪


def test_garden_stage_carries_the_canvas_verified_world_position():
    # 2026-09-17 用 debug_canvas_portal.py 反解 + 交叉验证过的世界坐标, 不是
    # 300x300小地图量化坐标 —— main._run_entry_route 拿它做比 target_box 更
    # 精确的到达复查. 最后一段(刷怪那张图)没有"要走到的点", 也就没有世界坐标.
    r = map_routes.route_for("anthell")
    assert r.stage_for("garden").walk_to_world == (27392.0, 48896.0)
    assert r.stage_for("anthell").walk_to_world is None


def test_stage_without_world_position_defaults_to_none():
    # 沙漠/海洋这种单图路线的 Stage 不传 walk_to_world —— 默认值就是 None,
    # main._run_entry_route 那边据此跳过世界坐标复查这一层, 不是漏传了个必填参数.
    r = map_routes.route_for("desert")
    assert r.stages[0].walk_to_world is None


def test_single_stage_maps_are_their_own_final_map_and_own_server():
    for name in ("garden", "desert", "ocean", "jungle"):
        r = map_routes.route_for(name)
        assert len(r.stages) == 1
        assert r.final_map == name
        assert r.server_biome == name


def test_unknown_map_falls_back_to_desert():
    # 跟 server_lookup.biome_key_for_map 同样的容错. 冥界(hel)没有小地图, 不支持.
    for bad in ("", None, "nope", "hel"):
        assert map_routes.route_for(bad).final_map == "desert"


def test_stage_for_returns_the_stage_on_that_map():
    r = map_routes.route_for("anthell")
    assert r.stage_for("garden").map_name == "garden"
    assert r.stage_for("anthell").walk_to is None
    assert r.stage_for("desert") is None      # 不在这条路线上


def test_is_calibrated_is_false_until_portal_is_filled(monkeypatch):
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", None)
    assert map_routes.route_for("anthell").is_calibrated() is False
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", (150, 40))
    assert map_routes.route_for("anthell").is_calibrated() is True


def test_single_stage_routes_are_always_calibrated():
    # 单图路线没有"要走到的点", 天然可跑.
    assert map_routes.route_for("desert").is_calibrated() is True


def test_target_box_is_a_tolerance_square_around_walk_to(monkeypatch):
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", (150, 40))
    stage = map_routes.route_for("anthell").stage_for("garden")
    assert stage.target_box(tolerance=2) == [(148, 38), (152, 42)]
    assert stage.target_box() == [(150 - map_routes.PORTAL_TOLERANCE,
                                   40 - map_routes.PORTAL_TOLERANCE),
                                  (150 + map_routes.PORTAL_TOLERANCE,
                                   40 + map_routes.PORTAL_TOLERANCE)]


def test_portal_is_the_calibrated_constant():
    # 2026-09-15 实机标定. 改这个数字等于改 bot 从花园走向哪个蚁穴入口.
    assert map_routes.ANTHELL_PORTAL == (133, 234)


def test_portal_tolerance_is_wide_enough_for_move_to_positions_arrival_radius():
    """PORTAL_TOLERANCE 不能比 5 还紧, 否则会跟 main.move_to_position() 自己的
    "到达"判据(离目标欧氏距离 < 5 个地图像素, 通用于所有寻路, 硬编码在那个
    函数里)打起来: move_to_position 可能在某个点上就宣布"到了"(比如目标
    (133,234), 落在 (134,231): 距离3.16<5), 但这个点某一个轴上的偏移量超过
    了更窄的 tolerance(这里 y 轴差 3, tolerance=2 时越界), 导致
    lazy_theta_pathing 外层 if_in_area(target_box) 判"没到", 两边判据永远
    对不上, 陷入"重新规划1步路径 -> move_to_position立刻宣布到了 -> 外层复查
    还是判没到"的死循环(实机复现 2026-09-19, 连下面的canvas复查/精确贴近
    逻辑都没机会跑起来)。tolerance=5 的正方形能完整包住那个半径5的圆
    (dist<5 隐含 |dx|<5 且 |dy|<5), 是消除这个死循环的数学下限.
    """
    assert map_routes.PORTAL_TOLERANCE >= 5


# 花园图上 7 个传送门绿斑的中心(2026-09-15 实测, debug_garden_raw.png)。
# 官方 garden.tmj 的 warps 里正好 7 个: to_desert / to_sewers / to_ant_hell / to_ocean /
# to_jungle / to_factory / to_crystal_room。
_GARDEN_DOORS = {"anthell": (133, 234), "sewers": (86, 185), "factory": (215, 150),
                 "desert": (156, 294), "ocean": (291, 158), "jungle": (294, 211),
                 "crystal_room": (152, 102)}


def test_every_garden_door_is_a_wall_on_the_shipped_map():
    """maps/garden.png 上 7 个传送门都必须是墙。

    花园现在既是刷怪图、也是去蚁穴/下水道/工厂的第一段。门要是烤成可走, 刷怪时寻路会
    大方地从上面穿过去, 踩上去人就被传走了 —— 所以去哪个门, 就在**那一段**里运行时
    只挖开那一个(map_routes.PORTAL_OPENINGS + utils.open_map_rects)。
    """
    import cv2

    binary = cv2.imread("./maps/garden.png", cv2.IMREAD_GRAYSCALE)
    assert binary is not None and binary.shape == (300, 300)
    for name, (x, y) in _GARDEN_DOORS.items():
        assert binary[y, x] == 0, f"花园的门 {name} ({x},{y}) 是可走的 —— garden.png 是不是带 --portal 重新生成的?"


_OPENED = [("anthell", 28), ("sewers", 25), ("factory", 25)]


@pytest.mark.parametrize("name, pixels", _OPENED)
def test_opening_rects_are_exactly_the_door_marker_blob(name, pixels):
    import cv2

    rects = map_routes.PORTAL_OPENINGS[("garden", _GARDEN_DOORS[name])]
    mask = _shortcut_mask(rects)
    assert int(mask.sum()) == pixels
    assert sum((x1 - x0 + 1) * (y1 - y0 + 1) for x0, y0, x1, y1 in rects) == pixels   # 矩形不重叠
    binary = cv2.imread("./maps/garden.png", cv2.IMREAD_GRAYSCALE)
    assert (binary[mask] == 0).all(), "要挖开的全是门标记(墙)"
    # 上面几条整体平移一格都还成立(门标记带粗黑描边, 平移后落的还是墙, 中心像素也还在矩形里),
    # 所以再对着原色小地图逐像素比: 矩形 == 门中心所在的那一整团绿斑(旧版 --portal 开的那团)。
    import numpy as np
    import utils

    raw = cv2.imread("./debug_garden_raw.png")
    assert raw is not None, "debug_garden_raw.png 不在仓库根目录"
    x, y = _GARDEN_DOORS[name]
    blob = utils._open_portal_blob(np.zeros((300, 300), np.uint8),
                                   cv2.cvtColor(raw, cv2.COLOR_BGR2HSV), (x, y)) == 255
    assert int(blob.sum()) == pixels
    assert (mask == blob).all(), f"{name} 的挖门矩形跟原色图上的绿斑对不上(平移/漏像素?)"


@pytest.mark.parametrize("name, pixels", _OPENED)
def test_opening_a_door_makes_only_that_door_walkable_and_reachable(name, pixels):
    import cv2
    import numpy as np
    import utils

    binary = cv2.imread("./maps/garden.png", cv2.IMREAD_GRAYSCALE)
    x, y = _GARDEN_DOORS[name]
    opened = utils._open_shortcut_rects(binary.copy(),
                                        map_routes.PORTAL_OPENINGS[("garden", (x, y))])
    assert opened[y, x] == 255
    count, labels, stats, _ = cv2.connectedComponentsWithStats((opened == 255).astype(np.uint8), 8)
    main = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA])
    assert labels[y, x] == main, "门开了但跟主迷宫不连通, bot 走不过去"
    for other, (ox, oy) in _GARDEN_DOORS.items():
        if other != name:
            assert opened[oy, ox] == 0, f"挖 {name} 的门把 {other} 的门也挖开了"


def test_stage_opening_rects_come_from_the_table():
    r = map_routes.route_for("anthell")
    assert r.stage_for("garden").opening_rects() == map_routes.PORTAL_OPENINGS[("garden", (133, 234))]
    assert not r.stage_for("anthell").opening_rects()        # 最后一段没有门要走


def test_stage_without_a_table_entry_has_no_opening(monkeypatch):
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", (150, 40))
    assert not map_routes.route_for("anthell").stage_for("garden").opening_rects()


# 蚁穴图上的 3 个传送点(2026-09-15 实测)。蚁穴里是去刷怪的, 一个都不该踩,
# 所以全部保持墙 —— maps/anthell.png 生成时不传 --portal。
_ANTHELL_PORTALS = [(121, 102), (95, 202), (204, 216)]


def test_anthell_map_is_one_connected_area():
    """蚁穴图的可走区必须几乎全在一个连通块里。

    蚁穴是"大房间 + 细走廊"的结构, 细走廊只有一两像素宽。默认阈值 200 会把走廊
    的抗锯齿过渡像素切掉, 图碎成 53 块、最大的一块只占 35% —— bot 只能在出生的
    那个房间里打转, 走不到刷怪区, 而且报的还是笼统的"路径规划失败"。

    重新生成要用:  python capture_map.py anthell --threshold 160
    (灰度直方图是干净的双峰 11 / 228; 160 是能连通的前提下最保守的阈值, 吃掉的
     墙最少。)
    """
    import cv2
    import numpy as np

    binary = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    assert binary is not None, "maps/anthell.png 不存在或读不出来"
    assert binary.shape == (300, 300)

    walkable = (binary == 255).astype(np.uint8)
    total = int(walkable.sum())
    count, labels, stats, _ = cv2.connectedComponentsWithStats(walkable, 4)
    largest = max(stats[i, cv2.CC_STAT_AREA] for i in range(1, count))
    assert largest / total > 0.99, (
        f"可走区碎了: 最大连通块只占 {100*largest/total:.1f}% —— "
        f"是不是用默认阈值重新生成的? 要 --threshold 160")


def test_anthell_portals_stay_walls():
    # 蚁穴里没有要走进去的传送点, 三个全该是墙让寻路绕开 —— 刷怪时踩中一个就被
    # 传出去了, 而且 bot 只会当成"位置对不上"傻等.
    import cv2

    binary = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    assert binary is not None
    for x, y in _ANTHELL_PORTALS:
        assert binary[y, x] == 0, f"蚁穴传送点 ({x},{y}) 是可走的 —— 刷怪会被传走"


def _bfs_steps(binary, start, goal):
    from collections import deque
    h, w = binary.shape
    seen = {start: 0}
    q = deque([start])
    while q:
        x, y = q.popleft()
        if (x, y) == goal:
            return seen[(x, y)]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                n = (x + dx, y + dy)
                if 0 <= n[0] < w and 0 <= n[1] < h and binary[n[1], n[0]] == 255 and n not in seen:
                    seen[n] = seen[(x, y)] + 1
                    q.append(n)
    return None


# ── 密道(推导见 map_routes.GARDEN_SHORTCUTS 上面那段注释) ──────────────────────
# 地图名: (常量名, 坐标指纹, 像素数, [(通道口a, 通道口b, 走密道最多几步, 不走密道至少几步)])
# 通道口 = 密道两头外侧紧挨着的可走像素; 步数是 8 邻接 BFS, 推导时在 maps/*.png 上量的
# (走密道 14/11/16... 步, 不走 106/147/137... 步), 这里各留一点余量.
_SHORTCUTS = {
    "garden": ("GARDEN_SHORTCUTS", "deefa96ad0c52e16", 240,
               [((178, 107), (203, 77), 35, 300), ((31, 129), (62, 132), 38, 330)]),
    "anthell": ("ANTHELL_SHORTCUTS", "aebf254d028a89d8", 107,
                [((82, 81), (95, 86), 16, 95), ((99, 106), (110, 109), 13, 130),
                 ((75, 177), (87, 175), 18, 120)]),
    "desert": ("DESERT_SHORTCUTS", "2e7b09afa2a187ae", 143,
               [((94, 105), (102, 92), 15, 20), ((167, 138), (194, 142), 30, 300)]),
    "ocean": ("OCEAN_SHORTCUTS", "e98ed2c7e1b614fd", 185,
              [((75, 76), (95, 90), 25, 150), ((170, 184), (192, 186), 24, 550),
               ((68, 243), (72, 268), 27, 420)]),
}


def _shortcut_mask(rects):
    import numpy as np
    mask = np.zeros((300, 300), bool)
    for x0, y0, x1, y1 in rects:
        mask[y0:y1 + 1, x0:x1 + 1] = True
    return mask


@pytest.mark.parametrize("name", sorted(_SHORTCUTS))
def test_shortcut_coordinates_are_the_derived_ones(name):
    # 改坐标等于改密道打通的位置/形状 —— 按 map_routes 那段注释重新推导过, 再改这里的指纹.
    import hashlib
    const, fingerprint, _, _ = _SHORTCUTS[name]
    rects = getattr(map_routes, const)
    assert hashlib.sha256(repr(rects).encode()).hexdigest()[:16] == fingerprint


@pytest.mark.parametrize("name", sorted(_SHORTCUTS))
def test_shortcut_rects_are_in_bounds_and_do_not_overlap(name):
    const, _, pixels, _ = _SHORTCUTS[name]
    rects = getattr(map_routes, const)
    for x0, y0, x1, y1 in rects:
        assert 0 <= x0 <= x1 < 300 and 0 <= y0 <= y1 < 300
    area = sum((x1 - x0 + 1) * (y1 - y0 + 1) for x0, y0, x1, y1 in rects)
    assert area == pixels == int(_shortcut_mask(rects).sum())


@pytest.mark.parametrize("name", sorted(_SHORTCUTS))
def test_shortcuts_are_real_short_passages_on_the_shipped_map(name):
    """每条密道都得是真的能穿过去的近路: 打通后两头十几二十步, 不打通要绕远(沙漠金字塔
    旁那条本来就只绕 22 步). 形状算歪了(没接上某一头 / 中间断了), 打通后的距离会跟没打通
    一样长. "不打通" = 把已发布地图上的密道像素抹回墙 —— 它们在小地图截图里本来就是墙."""
    import cv2

    const, _, _, passages = _SHORTCUTS[name]
    shipped = cv2.imread(f"./maps/{name}.png", cv2.IMREAD_GRAYSCALE)
    mask = _shortcut_mask(getattr(map_routes, const))
    assert (shipped[mask] == 255).all(), "密道像素在 maps/*.png 上不可走 —— 生成地图时没打通"
    minimap_only = shipped.copy()
    minimap_only[mask] = 0
    for a, b, max_with, min_without in passages:
        assert shipped[a[1], a[0]] == 255 and shipped[b[1], b[0]] == 255
        with_ = _bfs_steps(shipped, a, b)
        without = _bfs_steps(minimap_only, a, b)
        assert with_ is not None and with_ <= max_with, f"密道 {a}<->{b} 没打通 ({with_} 步)"
        assert without is None or without >= min_without, f"{a}<->{b} 本来就近, 不是密道"


@pytest.mark.parametrize("name", sorted(_SHORTCUTS))
def test_shortcuts_are_part_of_the_main_walkable_area(name):
    import cv2
    import numpy as np

    const, _, _, _ = _SHORTCUTS[name]
    binary = cv2.imread(f"./maps/{name}.png", cv2.IMREAD_GRAYSCALE)
    count, labels, stats, _ = cv2.connectedComponentsWithStats((binary == 255).astype(np.uint8), 8)
    main = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA])
    mask = _shortcut_mask(getattr(map_routes, const))
    assert (labels[mask] == main).all(), "有密道像素跟主迷宫不连通"


# ── 蚁穴传送门周围被门标记盖掉的空地 (用户 2026-09-29: "那个小部分管道是密道") ──────

def _clearing_mask():
    return _shortcut_mask(map_routes.ANTHELL_PORTAL_CLEARINGS)


def test_portal_clearings_are_open_on_the_shipped_anthell_map():
    import cv2
    shipped = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    assert (shipped[_clearing_mask()] == 255).all(), "门口空地在 maps/anthell.png 上还是墙 —— 地图没重新生成"


def test_portal_clearings_never_open_a_portal_itself():
    mask = _clearing_mask()
    for x, y in _ANTHELL_PORTALS:
        assert not mask[y, x], f"门口空地把传送门 ({x},{y}) 本身也打通了 —— 寻路会踩进门被传走"


def test_portal_clearings_are_part_of_the_main_walkable_area():
    import cv2
    import numpy as np
    binary = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    count, labels, stats, _ = cv2.connectedComponentsWithStats((binary == 255).astype(np.uint8), 8)
    main = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA])
    assert (labels[_clearing_mask()] == main).all()


def test_respawn_point_walks_straight_past_the_garden_portal():
    # 第四份录像: 复活点(~128,101)在门东边, 旧图上那是墙 —— 位置被吸附到 (134,103), 往门/往西
    # 走要先往下绕到 y=115, 在墙角卡了 3 分钟。打通后几步就到。
    import cv2
    shipped = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    assert shipped[101, 127] == 255
    steps = _bfs_steps(shipped, (127, 101), (116, 104))
    assert steps is not None and steps <= 14
    closed = shipped.copy()
    closed[_clearing_mask()] = 0
    without = _bfs_steps(closed, (134, 103), (116, 104))
    assert without is None or without >= 25


def test_portal_clearings_do_not_overlap_the_shortcuts():
    assert not (_clearing_mask() & _shortcut_mask(map_routes.ANTHELL_SHORTCUTS)).any()


def _garden_px(x, y):
    """花园(121 格 = 61952 世界单位)里世界坐标 -> 小地图像素(浮点)。"""
    s = 300.0 / (121 * 512 + 2000.0)
    return (s * (x + 1000) - 0.5, s * (y + 1000) - 0.5)


@pytest.mark.parametrize("name, portal, world", [
    ("anthell", (133, 234), (27392.0, 48896.0)),
    ("sewers", (86, 185), (17408.0, 38600.0)),
    ("factory", (215, 150), (44987.8787878788, 30975.7575757576)),
])
def test_door_routes_go_through_the_garden_to_the_official_door(name, portal, world):
    # 官方 garden.tmj 的 warps: to_ant_hell / to_sewers / to_factory. 下水道和工厂在标题页
    # 没有格子, 只有这一条路. walk_to 是 debug_garden_raw.png 上绿斑的中心, 跟官方世界坐标
    # 换算出来的像素差不到 1 格。
    r = map_routes.route_for(name)
    assert [s.map_name for s in r.stages] == ["garden", name]
    assert r.server_biome == "garden" and r.final_map == name
    stage = r.stage_for("garden")
    assert stage.walk_to == portal and stage.walk_to_world == world
    px, py = _garden_px(*world)
    assert abs(px - portal[0]) <= 1.0 and abs(py - portal[1]) <= 1.0
    assert r.is_calibrated() is True
    assert r.stages[-1].walk_to is None


def test_ocean_and_jungle_are_entered_from_their_own_title_button():
    # 用户 2026-09-29 定: 海洋、丛林按标题页按钮进(不走花园门).
    for name in ("ocean", "jungle"):
        r = map_routes.route_for(name)
        assert r.server_biome == name and len(r.stages) == 1
        assert r.stages[0].walk_to is None


def test_every_route_server_biome_has_a_title_screen_button():
    """server_biome 是给 utils.select_biome_on_title() / switch_server() 的 key —— 没有对应的
    标题页按钮坐标, select_biome_on_title 是静默 no-op(pos is None 就 return), 那一轮照旧落进
    florr 默认的花园, 而路线拿的是别的图。单图路线靠按钮进; 两段路线一律先进花园再走门。"""
    import utils
    for name, route in map_routes._routes().items():
        assert route.server_biome in utils._BIOME_BUTTON_POS, name
        if len(route.stages) == 1:
            # 单图路线 = 标题页格子里选得到的那四张, biome 就是它自己
            assert route.server_biome == name, name
            assert name in ("garden", "desert", "ocean", "jungle"), name
        else:
            # 蚁穴/下水道/工厂在标题页没有格子, 只有"先进花园再踩门"这一条路
            assert route.server_biome == "garden", name
            assert route.stages[0].map_name == "garden", name


def test_every_walk_to_stage_has_a_portal_opening():
    # 每个要走去踩门的阶段, 运行时都得能把那个门挖开(utils.open_map_rects); 漏登记 =
    # 那个门在二值图上是墙, 寻路永远到不了 —— 静默失败, 所以盯住键对得上。
    checked = 0
    for name, route in map_routes._routes().items():
        for stage in route.stages:
            if stage.walk_to is None:
                continue
            checked += 1
            assert (stage.map_name, stage.walk_to) in map_routes.PORTAL_OPENINGS, name
            assert stage.opening_rects(), name
    assert checked >= 3       # anthell / sewers / factory
