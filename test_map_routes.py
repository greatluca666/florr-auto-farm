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
    for name in ("desert", "ocean"):
        r = map_routes.route_for(name)
        assert len(r.stages) == 1
        assert r.final_map == name
        assert r.server_biome == name


def test_unknown_map_falls_back_to_desert():
    # 跟 server_lookup.biome_key_for_map 同样的容错. "garden" 也走这条 —— 它是
    # 蚁穴路线的内部阶段名, 不是 config 里能选的 map.
    for bad in ("garden", "", None, "jungle"):
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


def test_portal_is_walkable_on_the_shipped_garden_map():
    """洞口在 maps/garden.png 上必须是可走像素, 而且连在主迷宫上。

    这条盯的是一个会静默坏掉的组合: 洞口在小地图上是亮绿色圆点, 灰度 179, 低于
    preprocess_map 的 200 阈值 —— 没有 utils._PORTAL_GREEN_HSV_* 那条补救 mask
    的话它就是一堵墙。而寻路目标是墙时 lazy_theta_star 只会报笼统的"路径规划
    失败", 看不出是地图的问题, bot 会一轮一轮空转到超时。
    所以只要有人用旧逻辑重新生成 garden.png, 这条就得红。
    """
    import cv2
    import numpy as np

    binary = cv2.imread("./maps/garden.png", cv2.IMREAD_GRAYSCALE)
    assert binary is not None, "maps/garden.png 不存在或读不出来"
    assert binary.shape == (300, 300)

    x, y = map_routes.ANTHELL_PORTAL
    assert binary[y, x] == 255, "洞口落在墙上 —— garden.png 是不是用旧的阈值逻辑生成的?"

    walkable = (binary == 255).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(walkable, 4)
    main = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA])
    assert labels[y, x] == main, "洞口可走但跟主迷宫不连通, bot 走不过去"


# 花园图上另外 6 个传送点绿斑(2026-09-15 实测). 只有 ANTHELL_PORTAL 是蚁穴入口,
# 这几个通向别处 —— 用户确认过。
_OTHER_PORTALS = [(152, 102), (215, 150), (291, 158), (86, 185), (294, 211), (156, 294)]


def test_garden_shortcuts_are_the_calibrated_rects():
    # 改这两个数字等于改密道打通的位置 —— 跟 test_portal_is_the_calibrated_
    # constant 同一个把关思路, 锁死具体数值, 不满足于"随便什么矩形都行".
    assert map_routes.GARDEN_SHORTCUTS == [
        (28, 127, 68, 139),
        (179, 82, 211, 115),
    ]


def test_garden_shortcuts_are_valid_rects():
    # (x0,y0,x1,y1) 都得在 300x300 地图坐标系里, 而且是正经矩形(x0<x1, y0<y1),
    # 不然 utils._open_shortcut_rects 的切片会静默切出空区域或者反着切.
    assert len(map_routes.GARDEN_SHORTCUTS) == 2
    for x0, y0, x1, y1 in map_routes.GARDEN_SHORTCUTS:
        assert 0 <= x0 < x1 < 300
        assert 0 <= y0 < y1 < 300


def test_garden_shortcuts_are_walkable_and_connected_on_the_shipped_map():
    """密道矩形在 maps/garden.png 上必须是可走的, 而且连在主迷宫上 —— 跟
    test_portal_is_walkable_on_the_shipped_garden_map 是同一类回归: 只要有人
    用没带 open_shortcuts_at 的旧逻辑重新生成 garden.png, 这条就得红。
    """
    import cv2
    import numpy as np

    binary = cv2.imread("./maps/garden.png", cv2.IMREAD_GRAYSCALE)
    assert binary is not None
    walkable = (binary == 255).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(walkable, 4)
    main = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA])

    for x0, y0, x1, y1 in map_routes.GARDEN_SHORTCUTS:
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        assert binary[cy, cx] == 255, f"密道 ({cx},{cy}) 落在墙上"
        assert labels[cy, cx] == main, f"密道 ({cx},{cy}) 可走但跟主迷宫不连通"


def test_other_portals_stay_walls_on_the_shipped_garden_map():
    """别的传送点必须还是墙。

    它们要是也被打通成可走, 寻路就会大方地从上面穿过去 —— 而踩上传送点是会把人
    传走的, bot 去蚁穴的半路上就被拽到别的地方了。留成墙, 寻路自然绕开。
    utils.preprocess_map 默认一个都不开, 只有 open_portal_at 点名的那个才打通。
    """
    import cv2

    binary = cv2.imread("./maps/garden.png", cv2.IMREAD_GRAYSCALE)
    assert binary is not None
    for x, y in _OTHER_PORTALS:
        assert binary[y, x] == 0, f"传送点 ({x},{y}) 被打通了 —— bot 会在半路被传走"


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


def test_anthell_shortcuts_are_the_calibrated_rects():
    # 每个元素是同一行的一段像素, 90 个像素拼出 3 条 2~3 像素宽的弯通道 —— 按
    # florr 自己的 ant_hell.tmj 瓦片形状算出来的, 推导见 map_routes.ANTHELL_SHORTCUTS
    # 上面的注释. 改这些数字等于改密道打通的位置/形状.
    assert map_routes.ANTHELL_SHORTCUTS == [
        (83, 84, 87, 84), (83, 85, 87, 85), (86, 86, 93, 86), (86, 87, 93, 87),
        (89, 88, 89, 88), (91, 88, 93, 88), (91, 89, 93, 89), (91, 90, 93, 90),
        (100, 107, 100, 107), (100, 108, 104, 108), (100, 109, 104, 109),
        (103, 110, 109, 110), (103, 111, 109, 111),
        (85, 178, 86, 178), (84, 179, 86, 179), (76, 180, 76, 180), (84, 180, 86, 180),
        (76, 181, 85, 181), (76, 182, 85, 182),
    ]


def test_anthell_shortcuts_are_valid_rects():
    total = 0
    for x0, y0, x1, y1 in map_routes.ANTHELL_SHORTCUTS:
        assert 0 <= x0 <= x1 < 300
        assert 0 <= y0 <= y1 < 300
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    # 上一版按"整格 + 取整外扩"打通了 274 个像素, 其中 169 个按 florr 地图数据是真墙.
    assert total == 90


# 3 条密道各自两头的通道口(maps/anthell.png 坐标, 密道外侧紧挨着的可走像素).
_ANTHELL_SHORTCUT_MOUTHS = [((82, 83), (94, 91)), ((99, 106), (110, 112)), ((87, 177), (75, 183))]


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


def test_anthell_shortcuts_are_real_short_passages_on_the_shipped_map():
    """每条密道都得是一条真的能穿过去的近路: 打通后两头之间十几步就到, 不打通
    要绕 100 多步. 密道形状要是算歪了(没接上某一头、或者中间断了), 打通后的距离
    会跟没打通一样长. "不打通"= 把已发布地图上的密道像素抹回墙 —— 这些像素在小地图
    截图里本来就是墙(推导时就是按"小地图画成墙"筛的), 不用依赖调试截图."""
    import cv2

    shipped = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    minimap_only = shipped.copy()
    for x0, y0, x1, y1 in map_routes.ANTHELL_SHORTCUTS:
        minimap_only[y0:y1 + 1, x0:x1 + 1] = 0
    for a, b in _ANTHELL_SHORTCUT_MOUTHS:
        assert shipped[a[1], a[0]] == 255 and shipped[b[1], b[0]] == 255
        assert _bfs_steps(shipped, a, b) <= 15, f"密道 {a}<->{b} 没打通"
        assert _bfs_steps(minimap_only, a, b) >= 100, f"{a}<->{b} 本来就近, 不是密道"


def test_anthell_shortcuts_are_walkable_and_connected_on_the_shipped_map():
    """密道矩形在 maps/anthell.png 上必须是可走的, 而且连在主迷宫上 —— 跟花园那条
    同一类回归: 只要有人用没带 open_shortcuts_at 的旧逻辑重新生成 anthell.png,
    这条就得红。"""
    import cv2
    import numpy as np

    binary = cv2.imread("./maps/anthell.png", cv2.IMREAD_GRAYSCALE)
    assert binary is not None
    walkable = (binary == 255).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(walkable, 4)
    main = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA])

    for x0, y0, x1, y1 in map_routes.ANTHELL_SHORTCUTS:
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        assert binary[cy, cx] == 255, f"密道 ({cx},{cy}) 落在墙上"
        assert labels[cy, cx] == main, f"密道 ({cx},{cy}) 可走但跟主迷宫不连通"
