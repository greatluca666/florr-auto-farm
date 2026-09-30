import math

import numpy as np
import pytest

import flee_planner as fp


def _map(rows):
    """'.' = 可走, '#' = 墙。返回 255/0 的二值图(跟 maps/*.png 一样)。"""
    h, w = len(rows), len(rows[0])
    m = np.zeros((h, w), dtype=np.uint8)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == ".":
                m[y, x] = 255
    return m


def _deg(v):
    return math.degrees(math.atan2(v[1], v[0]))


def test_runs_straight_away_down_an_open_corridor():
    m = _map(["#" * 60] + ["#" + "." * 58 + "#"] * 3 + ["#" * 60])
    plan = fp.plan_flee(m, (20.0, 2.0), [(17.0, 2.0)])
    assert plan["safe"]
    assert abs(_deg(plan["dir"])) < 30          # 追兵在左 -> 往右跑


def test_prefers_the_way_out_over_a_dead_end_that_looks_further_from_the_chaser():
    # 第五份录像: 究极从左下来, 老办法一直往右上跑, 扎进刷怪区右上角的死胡同
    # 这张图: 人在 T 字路口, 右边是封死的湾, 下边是通到窗口外的长走廊
    rows = ["#" * 70 for _ in range(60)]
    grid = [list(r) for r in rows]
    for y in range(20, 26):                     # 横着的房间 x 5..40
        for x in range(5, 41):
            grid[y][x] = "."
    for y in range(26, 59):                     # 往下的走廊, 一直通到窗口外
        for x in range(20, 23):
            grid[y][x] = "."
    m = _map(["".join(r) for r in grid])
    me, chaser = (20.0, 22.0), (15.0, 22.0)
    plan = fp.plan_flee(m, me, [chaser])
    assert plan["safe"]
    assert plan["goal"][1] > 26                 # 目标在下边的走廊里, 不在右边的死湾
    assert 45 < _deg(plan["dir"]) < 135         # 往下


def test_does_not_run_into_a_crowd():
    # 第五份录像: 背离究极的方向上正好 12~14 只传奇兵蚁
    m = _map(["." * 60 for _ in range(60)])
    me, chaser = (30.0, 30.0), (27.0, 30.0)
    crowd = [(33.0 + dx * 0.7, 30.0 + dy * 0.7) for dx in range(3) for dy in (-1, 0, 1)]
    plan = fp.plan_flee(m, me, [chaser], crowd)
    assert plan["safe"]
    assert abs(_deg(plan["dir"])) > 30          # 不是正右边(怪群那边)
    assert abs(abs(_deg(plan["dir"])) - 180) > 60   # 也不是回头冲究极


def test_squeezes_through_the_crowd_when_it_surrounds_you():
    m = _map(["." * 40 for _ in range(40)])
    crowd = [(20.0 + dx, 20.0 + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]
    plan = fp.plan_flee(m, (20.0, 20.0), [(16.0, 20.0)], crowd)
    assert plan is not None                     # 身边一圈都被当成墙也得给个方向, 不能站着


def test_no_cutting_diagonally_through_a_wall_corner():
    m = _map(["....#",
              "....#",
              "##..#",
              "#...#",
              "#####"])
    dist, _ = fp._dijkstra(m, [(0.0, (1, 1))], (0, 0, 4, 4))
    # (1,1) -> (0,2)? 墙。 (2,2) 可以直接斜走到 (1,3) 吗? (1,2) 是墙 -> 不行, 得绕 (2,3)
    assert dist[(1, 3)] == pytest.approx(dist[(2, 2)] + 2.0)


def test_keeps_the_previous_route_when_two_are_equally_good():
    m = _map(["." * 61 for _ in range(61)])
    me, chaser = (30.0, 30.0), (27.0, 30.0)
    up = fp.plan_flee(m, me, [chaser], prefer=(50, 10))
    down = fp.plan_flee(m, me, [chaser], prefer=(50, 50))
    assert up["dir"][1] < 0 < down["dir"][1]


def test_window_edge_cutting_through_a_closed_bay_is_not_a_way_out():
    # 窗口边正好切在一片封闭的湾中间: 湾里的格子也在窗口边上, 但出不去
    m = _map(["#" * 40] + ["#" + "." * 30 + "#" * 9] * 5 + ["#" * 40])
    box = (0, 0, 28, 6)
    assert not fp._escapes_beyond(m, (28, 3), box)
    m2 = _map(["#" * 60] + ["#" + "." * 58 + "#"] * 5 + ["#" * 60])
    assert fp._escapes_beyond(m2, (28, 3), box)


def test_cornered_still_picks_the_least_bad_cell():
    m = _map(["#####",
              "#...#",
              "#...#",
              "#...#",
              "#####"])
    # 人贴着右墙, 究极就在身边: 哪一格都不比它先到 1 格
    plan = fp.plan_flee(m, (3.0, 2.0), [(2.0, 2.0)])
    assert plan is not None and plan["safe"] is False
    assert plan["goal"][0] == 3                 # 亏得最少的是贴墙那一列(离究极最远), 不是往它身上撞


def test_nothing_to_plan_without_a_chaser_or_off_the_map():
    m = _map(["." * 10 for _ in range(10)])
    assert fp.plan_flee(m, (5.0, 5.0), []) is None
    assert fp.plan_flee(m, (50.0, 50.0), [(4.0, 5.0)]) is None


def test_standing_on_a_thickened_wall_cell_still_looks_far():
    # 地图墙比游戏里厚一格, 人常"站在墙格上"(第五份录像: 走廊里 y=101.9 取整到墙上)
    rows = ["#" * 30, "#" * 30, "#" + "." * 28 + "#", "#" * 30]
    m = _map(rows)
    plan = fp.plan_flee(m, (10.0, 2.9), [(6.0, 2.0)])
    assert abs(_deg(plan["dir"])) < 20          # 顺着走廊往右, 不是斜着顶上边的墙
