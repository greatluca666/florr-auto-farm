"""丛林 / 下水道 / 工厂三张寻路图是按官方 .tmj 推导的(tmj_maps.py), 没有实机截图。
这里盯的是"推导结果至少是一张能走的图"。它们跟实机小地图差多少, 用
`python capture_map.py <name> --compare` 在局内核对。"""
import cv2
import numpy as np
import pytest

# 世界宽(格数): 丛林 121, 下水道/工厂 90
_TILES = {"jungle": 121, "sewers": 90, "factory": 90}


def _px(name, x, y):
    """官方世界坐标 -> maps/<name>.png 像素。故意把公式在这里再写一遍(不 import tmj_maps),
    这样 tmj_maps 里的公式被改歪时这里会红。"""
    s = 300.0 / (_TILES[name] * 512 + 2000.0)
    return (int(round(s * (x + 1000) - 0.5)), int(round(s * (y + 1000) - 0.5)))


def _load(name):
    binary = cv2.imread(f"./maps/{name}.png", cv2.IMREAD_GRAYSCALE)
    assert binary is not None, f"maps/{name}.png 不存在或读不出来"
    return binary


# 可走占比: 推导出来的数(±0.02)。变太多 = 规则或图层被改过。
# 实测: 丛林 0.5639 / 下水道 0.3496 / 工厂 0.4883
_SHARE = {"jungle": (0.544, 0.584), "sewers": (0.330, 0.370), "factory": (0.468, 0.508)}

# 出生点(官方 respawn_area / checkpoint 矩形: x, y, 宽, 高, 世界单位)
_SPAWN_RECTS = {
    "jungle": (18704, 12560, 2560, 2560),
    "sewers": (11856, 43520, 872, 592),
    "factory": (7008, 44736, 1296, 924),
}

# 通向别处的传送门(官方 warps 里 type=warp, 世界坐标)。刷怪时踩上去会被传走, 必须是墙。
_DOORS = {
    "jungle": [(100, 3072), (100, 38400), (22016, 512), (17664, 60672)],
    "sewers": [(12288, 44024)],
    "factory": [(7680, 45460)],
}


@pytest.mark.parametrize("name", sorted(_TILES))
def test_map_is_a_300x300_binary_image(name):
    binary = _load(name)
    assert binary.shape == (300, 300)
    assert set(np.unique(binary)) <= {0, 255}


@pytest.mark.parametrize("name", sorted(_TILES))
def test_walkable_share_is_in_the_derived_range(name):
    lo, hi = _SHARE[name]
    share = float((_load(name) == 255).mean())
    assert lo <= share <= hi, f"{name} 可走占比 {share:.3f} 不在 [{lo}, {hi}]"


def _main_component(binary):
    count, labels, stats, _ = cv2.connectedComponentsWithStats((binary == 255).astype(np.uint8), 8)
    main = max(range(1, count), key=lambda i: stats[i, cv2.CC_STAT_AREA])
    return labels == main, int(stats[main, cv2.CC_STAT_AREA])


@pytest.mark.parametrize("name", sorted(_TILES))
def test_walkable_area_is_one_connected_piece(name):
    binary = _load(name)
    _, largest = _main_component(binary)
    assert largest / int((binary == 255).sum()) > 0.99, "可走区碎了 —— bot 只能在出生的那块里打转"


@pytest.mark.parametrize("name", sorted(_TILES))
def test_the_spawn_area_touches_the_main_walkable_area(name):
    binary = _load(name)
    main, _ = _main_component(binary)
    x, y, w, h = _SPAWN_RECTS[name]
    x0, y0 = _px(name, x, y)
    x1, y1 = _px(name, x + w, y + h)
    assert main[y0:y1 + 1, x0:x1 + 1].any(), f"{name} 出生区里没有一个像素连在主可走区上"


@pytest.mark.parametrize("name", sorted(_TILES))
def test_doors_are_walls(name):
    binary = _load(name)
    for wx, wy in _DOORS[name]:
        x, y = _px(name, wx, wy)
        assert binary[y, x] == 0, f"{name} 的传送门 ({wx},{wy}) -> 像素 ({x},{y}) 是可走的 —— 刷怪会被传走"
