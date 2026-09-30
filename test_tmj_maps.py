import base64
import gzip
import json
import struct

import cv2
import numpy as np
import pytest

import tmj_maps

FLIP_H = tmj_maps.FLIP_H
FLIP_D = tmj_maps.FLIP_D


def _mask(fill):
    """32x32 的瓦片轮廓: fill 是 0~255 的均匀值, 或一个 callable(m) 原地改。"""
    m = np.zeros((32, 32), np.uint8)
    if callable(fill):
        fill(m)
    else:
        m[:, :] = fill
    return m


def _tmj(width, height, tile_layers, object_layers=()):
    layers = []
    for name, gids in tile_layers.items():
        raw = struct.pack("<%dI" % (width * height),
                          *np.asarray(gids, dtype=np.uint32).ravel())
        layers.append({"type": "tilelayer", "name": name, "compression": "gzip",
                       "encoding": "base64",
                       "data": base64.b64encode(gzip.compress(raw)).decode()})
    for name, objects in object_layers:
        layers.append({"type": "objectgroup", "name": name, "objects": objects})
    return {"width": width, "height": height, "layers": layers}


def _write_data(tmp_path, name, tile_layers, object_layers=()):
    """4x4 格的玩具地图. 瓦片: id0=整格墙 id1=左半格墙 id2=只盖 30% (gid = id+1)."""
    (tmp_path / "masks").mkdir(exist_ok=True)
    cv2.imwrite(str(tmp_path / "masks" / "wall_c_0.png"), _mask(255))
    cv2.imwrite(str(tmp_path / "masks" / "wall_l_0.png"),
                _mask(lambda m: m.__setitem__((slice(None), slice(0, 16)), 255)))
    cv2.imwrite(str(tmp_path / "masks" / "wall_q_0.png"), _mask(77))
    (tmp_path / "tileset.tsj").write_text(json.dumps({"tiles": [
        {"id": 0, "image": "wall_c_0.svg"}, {"id": 1, "image": "wall_l_0.svg"},
        {"id": 2, "image": "wall_q_0.svg"}]}), encoding="utf-8")
    (tmp_path / f"{name}.tmj").write_text(
        json.dumps(_tmj(4, 4, tile_layers, object_layers)), encoding="utf-8")
    return str(tmp_path)


def _grid(cells):
    """cells: {(col, row): gid} -> 4x4 gid 数组"""
    a = np.zeros((4, 4), np.uint32)
    for (c, r), g in cells.items():
        a[r, c] = g
    return a


@pytest.fixture
def toy_spec(monkeypatch):
    def install(**spec):
        monkeypatch.setitem(tmj_maps.MAP_SPECS, "toy", spec)
    return install


# 玩具图: 世界宽 4*512=2048 -> s=300/4048, 偏移 1000*s-0.5=73.6; 一格约 37.9 像素,
# 第 c 列覆盖像素 x∈[73.6+37.9c, 111.5+37.9c]. 下面挑的点都在格子正中间, 不贴边.
def _cell_center(col, row):
    s = 300.0 / (4 * 512 + 2000)
    off = 1000 * s - 0.5
    return (int(round(off + (col + 0.5) * 512 * s)), int(round(off + (row + 0.5) * 512 * s)))


def test_wall_tiles_are_walls_and_the_rest_is_walkable(tmp_path, toy_spec):
    toy_spec(walls=("walls",))
    d = _write_data(tmp_path, "toy", {"walls": _grid({(0, 0): 1, (0, 1): 1, (0, 2): 1, (0, 3): 1})})
    out = tmj_maps.derive(d, "toy")
    assert out.shape == (300, 300) and out.dtype == np.uint8
    x, y = _cell_center(0, 1)
    assert out[y, x] == 0                       # 墙列
    x, y = _cell_center(2, 1)
    assert out[y, x] == 255                     # 没有墙的格子可走
    assert out[5, 5] == 0                       # 地图外 = 墙(border=1)
    assert out[150, 290] == 0


def test_flip_flag_moves_the_wall_to_the_other_half(tmp_path, toy_spec):
    toy_spec(walls=("walls",))
    plain = _write_data(tmp_path, "toy", {"walls": _grid({(1, 1): 2})})     # gid 2 = wall_l_0, 左半墙
    out = tmj_maps.derive(plain, "toy")
    x0, y = _cell_center(1, 1)
    left, right = x0 - 12, x0 + 12
    assert out[y, left] == 0 and out[y, right] == 255
    flipped = _write_data(tmp_path, "toy", {"walls": _grid({(1, 1): 2 | FLIP_H})})
    out = tmj_maps.derive(flipped, "toy")
    assert out[y, left] == 255 and out[y, right] == 0


def test_orient_follows_the_tmx_order_transpose_then_flips():
    m = np.zeros((32, 32), np.float32)
    m[:, :16] = 1.0                                  # 左半
    top = np.zeros((32, 32), np.float32)
    top[:16, :] = 1.0
    assert (tmj_maps.orient(m, 2 | FLIP_D) == top).all()          # 对角翻转 = 转置: 左半 -> 上半
    assert (tmj_maps.orient(m, 2 | FLIP_H) == m[:, ::-1]).all()
    # 顺序是先转置、再水平、再垂直: "转置 + 垂直翻" 把上半翻到下半
    assert (tmj_maps.orient(m, 2 | FLIP_D | tmj_maps.FLIP_V) == top[::-1, :]).all()
    # 转置和水平翻只有同时出现才分得出先后: 先转置(左半 -> 上半)再水平翻 = 上半;
    # 反过来先水平翻(左半 -> 右半)再转置 = 下半
    assert (tmj_maps.orient(m, 2 | FLIP_D | FLIP_H) == top).all()


def test_affine_is_the_game_minimap_formula():
    a121 = tmj_maps.world_to_index_affine(121)
    assert abs(a121[0, 0] - 0.00469101826) < 1e-10        # 花园/沙漠的实测 CTM 缩放
    assert abs(tmj_maps.world_to_index_affine(123)[0, 0] - 0.00461708941) < 1e-10   # 蚁穴
    # 花园里蚁穴门 (27392, 48896) 在原色图上绿斑中心是 (132.8, 233.5)
    x = a121[0, 0] * 27392 + a121[0, 2]
    y = a121[1, 1] * 48896 + a121[1, 2]
    assert abs(x - 132.8) < 0.3 and abs(y - 233.5) < 0.3


def test_lower_threshold_makes_thicker_walls(tmp_path, toy_spec):
    toy_spec(walls=("walls",))
    d = _write_data(tmp_path, "toy", {"walls": _grid({(1, 1): 2, (2, 2): 1})})
    thin = tmj_maps.derive(d, "toy", threshold=0.5)
    thick = tmj_maps.derive(d, "toy", threshold=0.1)
    assert (thick == 255).sum() < (thin == 255).sum()
    assert ((thick == 255) & (thin == 0)).sum() == 0          # 只会多出墙, 不会多出路


def test_floor_layers_make_everything_else_a_wall(tmp_path, toy_spec):
    toy_spec(walls=(), floors=("floor",))
    d = _write_data(tmp_path, "toy", {"floor": _grid({(1, 1): 1, (1, 2): 1})})
    out = tmj_maps.derive(d, "toy")
    assert out[_cell_center(1, 1)[1], _cell_center(1, 1)[0]] == 255
    assert out[_cell_center(1, 2)[1], _cell_center(1, 2)[0]] == 255
    assert out[_cell_center(3, 1)[1], _cell_center(3, 1)[0]] == 0      # 地板层没盖到 = 墙
    assert out[_cell_center(1, 3)[1], _cell_center(1, 3)[0]] == 0


def test_collision_objects_are_walls(tmp_path, toy_spec):
    toy_spec(walls=("walls",), collision_objects="collision")
    obj = {"x": 512, "y": 512, "width": 512, "height": 512}      # 正好是 (1,1) 那一格
    d = _write_data(tmp_path, "toy", {"walls": _grid({})}, [("collision", [obj])])
    out = tmj_maps.derive(d, "toy")
    x, y = _cell_center(1, 1)
    assert out[y, x] == 0
    x, y = _cell_center(2, 1)
    assert out[y, x] == 255


def test_shortcut_layer_reopens_thin_walls_that_the_threshold_swallowed(tmp_path, toy_spec):
    # wall_q 只盖 30%: 官方说它多半是路(<50%), 但 >10% 的阈值会把它当墙 —— 通风管/密道
    # 这种盖着贴图的窄路就是这么被吞掉的. shortcut 层盖着它就该重新打通.
    toy_spec(walls=("walls",), shortcut_layer="shortcut")
    layers = {"walls": _grid({(1, 1): 3}), "shortcut": _grid({(1, 1): 1})}
    with_shortcut = tmj_maps.derive(_write_data(tmp_path, "toy", layers), "toy")
    toy_spec(walls=("walls",))
    without = tmj_maps.derive(_write_data(tmp_path, "toy", layers), "toy")
    x, y = _cell_center(1, 1)
    assert without[y, x] == 0 and with_shortcut[y, x] == 255


def test_doors_are_closed_with_a_small_disc(tmp_path, toy_spec):
    # 传送门(warps 里 type=warp)周围强制是墙 —— 刷怪时踩上去会被传走.
    toy_spec(walls=("walls",))
    door = {"type": "warp", "name": "to_x", "x": 1024, "y": 1024, "width": 0, "height": 0}
    respawn = {"type": "respawn_area", "name": "", "x": 0, "y": 0, "width": 512, "height": 512}
    d = _write_data(tmp_path, "toy", {"walls": _grid({})}, [("warps", [door, respawn])])
    out = tmj_maps.derive(d, "toy")
    s = 300.0 / (4 * 512 + 2000)
    cx = cy = int(round(s * (1024 + 1000) - 0.5))
    assert out[cy, cx] == 0                                   # 门心是墙
    assert out[cy, cx + 5] == 255                             # 1.5 像素之外照旧可走
    x, y = _cell_center(0, 0)
    assert out[y, x] == 255                                   # respawn_area 不是门


def test_agreement_is_the_share_of_equal_pixels():
    a = np.zeros((300, 300), np.uint8)
    b = a.copy()
    b[:30, :] = 255
    assert tmj_maps.agreement(a, a) == 1.0
    assert abs(tmj_maps.agreement(a, b) - 0.9) < 1e-9


def test_build_writes_the_binary_map_under_the_maps_file_name(tmp_path, toy_spec, capsys):
    toy_spec(walls=("walls",))
    d = _write_data(tmp_path, "toy", {"walls": _grid({(0, 0): 1})})
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    assert tmj_maps.main(["--data", d, "build", "toy", "--out", str(out_dir)]) == 0
    img = cv2.imread(str(out_dir / "toy.png"), cv2.IMREAD_GRAYSCALE)
    assert img.shape == (300, 300) and set(np.unique(img)) <= {0, 255}
    assert "toy" in capsys.readouterr().out


def test_ant_hell_is_written_as_anthell(tmp_path, monkeypatch):
    monkeypatch.setitem(tmj_maps.MAP_SPECS, "ant_hell", {"walls": ("walls",)})
    d = _write_data(tmp_path, "ant_hell", {"walls": _grid({})})
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    tmj_maps.main(["--data", d, "build", "ant_hell", "--out", str(out_dir)])
    assert (out_dir / "anthell.png").exists()


def test_compare_reports_the_agreement_with_the_published_map(tmp_path, toy_spec, capsys):
    toy_spec(walls=("walls",))
    d = _write_data(tmp_path, "toy", {"walls": _grid({(0, 0): 1})})
    maps_dir = tmp_path / "maps"
    maps_dir.mkdir()
    tmj_maps.main(["--data", d, "build", "toy", "--out", str(maps_dir)])
    capsys.readouterr()
    assert tmj_maps.main(["--data", d, "compare", "toy", "--maps", str(maps_dir)]) == 0
    assert "100.00%" in capsys.readouterr().out
