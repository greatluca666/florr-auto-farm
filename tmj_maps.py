"""DEV TOOL — 按 florr 官方地图数据(.tmj)推导寻路二值图 maps/<name>.png。

    python tmj_maps.py --data DIR build jungle sewers factory           # 写 ./maps/<name>.png
    python tmj_maps.py --data DIR compare garden desert ocean ant_hell   # 跟已发布的实机图对拍

DIR 里要有(都是 florr.io 官方静态资源, 不进仓库; 官方站点对 curl 回 403, 要走浏览器抓):
    <name>.tmj        https://florr.io/static/maps/<name>.tmj   Tiled 地图
    tileset.tsj       Tiled tileset(瓦片 id -> 图片名)
    masks/<tile>.png  每种瓦片 /static/tiles/<tile>.svg 渲染出的 alpha 轮廓(灰度 PNG, 边长随意,
                      读进来统一缩到每格 32x32)。下水道的 grate 是带缝隙的格栅, 要先做闭运算
                      填成整块轮廓再存。

推导规则(2026-09-29 用花园/沙漠/海洋/蚁穴四张实机图标定过, 像素吻合率 98.4%~99.1%):
  - 墙 = MAP_SPECS[name]["walls"] 里那些瓦片层的轮廓(按 TMX 翻转标志摆好); "floors" 里
    列的层是地板, 地板没盖到的地方也是墙; "collision_objects" 是矩形碰撞物件;
  - 世界坐标 -> 小地图像素索引: s = 300/(世界宽+2000), 索引 = s*(world+1000) - 0.5。
    跟游戏小地图的 CTM [s,0,0,s,box+1000s,...] 是同一条(s 在蚁穴/花园上精确到 8 位);
  - 一个像素里官方墙覆盖 > WALL_COVERAGE_THRESHOLD(0.1) 就算墙。小地图的墙描边比官方轮廓
    粗, 阈值取 0.5 时吻合率只有 95.8%~97.5%, 而且错的全是"我说能走、实机是墙"这个危险方向;
  - shortcut 层(通风管/密道)盖着、官方墙覆盖 <50% 的像素强制打通 —— 窄路会被上面的阈值吞掉;
  - 传送门(warps 里 type=warp)周围 DOOR_CLOSE_RADIUS_PX 内强制是墙 —— 刷怪时踩上去会被传走。
"""
import argparse
import base64
import gzip
import json
import os
import struct
import sys

import cv2
import numpy as np

TILE = 512.0                    # 一格 = 512 世界单位
MASK_PX = 32                    # 每格栅格化成 32x32
MAP_SIZE = 300                  # 小地图 300x300
SUBSAMPLE = 8                   # 渲染时每个小地图像素细分成 8x8 再平均
WALL_COVERAGE_THRESHOLD = 0.1
SHORTCUT_COVERAGE = 0.05
DOOR_CLOSE_RADIUS_PX = 1.5
FLIP_H, FLIP_V, FLIP_D = 0x80000000, 0x40000000, 0x20000000
GID_MASK = 0x1FFFFFFF

# tmj 文件名 -> maps/ 下的文件名
MAP_FILE_NAMES = {"ant_hell": "anthell"}

# 每张图哪些层算墙 / 地板。判断依据:
#   花园/沙漠/海洋/蚁穴: 2026-09-26 上一轮做密道时拿实机图对过。
#   丛林: water/bush/dirt/hut 全是 tileset 里 "墙系" 的 wang 颜色; 怪物刷新多边形只有 9.3%
#     压在 bush 上(bush 占全图 27.8%), 跟花园的 dirt(7.2%)一个量级 —— 多边形绕着 bush 走。
#   下水道: 怪物刷新区 100% 压在 grate 上、0% 压在 dirt / bg 上 —— grate 是能走的格栅地板,
#     dirt 是墙, 格栅外面的污水(bg)保守当墙。
#   工厂: 只有 walls 一层(shortcut 层是通风管, collisions=false)。
MAP_SPECS = {
    "garden": {"walls": ("dirt", "castle", "water")},
    "desert": {"walls": ("dirt", "water"), "collision_objects": "collision"},
    "ocean": {"walls": ("dirt", "castle")},
    "ant_hell": {"walls": ("dirt",)},
    "jungle": {"walls": ("water", "bush", "dirt", "hut")},
    "sewers": {"walls": ("dirt",), "floors": ("grate",)},
    "factory": {"walls": ("walls",), "shortcut_layer": "shortcut"},
}


def load_map(data_dir, name):
    """-> (tmj dict, {瓦片层名: gid 数组(H,W)}, {物件层名: [物件]})"""
    with open(os.path.join(data_dir, f"{name}.tmj"), encoding="utf-8") as f:
        j = json.load(f)
    count = j["width"] * j["height"]
    layers = {}
    for layer in j["layers"]:
        if layer["type"] != "tilelayer":
            continue
        assert layer.get("encoding") == "base64", f"{name}/{layer['name']}: 只认 base64 编码"
        raw = base64.b64decode(layer["data"])
        if layer.get("compression") == "gzip":
            raw = gzip.decompress(raw)
        gids = np.array(struct.unpack("<%dI" % count, raw), dtype=np.uint64)
        layers[layer["name"]] = gids.reshape(j["height"], j["width"])
    objects = {layer["name"]: layer.get("objects", [])
               for layer in j["layers"] if layer["type"] == "objectgroup"}
    return j, layers, objects


def load_tileset(data_dir):
    """tileset.tsj -> {瓦片 id: 图片文件名}"""
    with open(os.path.join(data_dir, "tileset.tsj"), encoding="utf-8") as f:
        return {t["id"]: t["image"] for t in json.load(f)["tiles"]}


_mask_cache = {}


def tile_mask(data_dir, image, size=MASK_PX):
    """瓦片轮廓 -> size x size 的 float32 [0,1]"""
    key = (data_dir, image, size)
    if key not in _mask_cache:
        path = os.path.join(data_dir, "masks", image.removesuffix(".svg") + ".png")
        m = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if m is None:
            raise FileNotFoundError(f"缺瓦片轮廓 {path} —— 见模块文档里 masks/ 的说明")
        _mask_cache[key] = cv2.resize(m.astype(np.float32) / 255.0, (size, size),
                                      interpolation=cv2.INTER_AREA)
    return _mask_cache[key]


def orient(mask, gid):
    """按 gid 高位的 TMX 翻转标志摆瓦片: 先对角(转置), 再水平, 再垂直。"""
    if gid & FLIP_D:
        mask = mask.T
    if gid & FLIP_H:
        mask = mask[:, ::-1]
    if gid & FLIP_V:
        mask = mask[::-1, :]
    return mask


def raster_layer(gids, id2img, data_dir, size=MASK_PX):
    """一个瓦片层 -> (H*size, W*size) 的覆盖栅格, 同一格重叠取最大。"""
    height, width = gids.shape
    out = np.zeros((height * size, width * size), np.float32)
    for y, x in zip(*np.nonzero(gids)):
        gid = int(gids[y, x])
        image = id2img[(gid & GID_MASK) - 1]
        mask = orient(tile_mask(data_dir, image, size), gid)
        block = out[y * size:(y + 1) * size, x * size:(x + 1) * size]
        np.maximum(block, mask, out=block)
    return out


def world_to_index_affine(width_tiles):
    """世界坐标 -> 300x300 小地图像素索引 的 2x3 仿射矩阵。"""
    s = MAP_SIZE / (width_tiles * TILE + 2000.0)
    offset = 1000.0 * s - 0.5
    return np.array([[s, 0.0, offset], [0.0, s, offset]])


def render_coverage(raster, affine, border=1.0):
    """世界栅格 -> 每个小地图像素被它盖住的比例 [0,1]; 地图外按 border(1.0 = 墙)。"""
    s = TILE / MASK_PX
    to_world = np.array([[s, 0, 0.5 * s], [0, s, 0.5 * s], [0, 0, 1]])
    a3 = np.vstack([affine, [0, 0, 1]])
    to_hires = np.array([[SUBSAMPLE, 0, 0.5 * SUBSAMPLE - 0.5],
                         [0, SUBSAMPLE, 0.5 * SUBSAMPLE - 0.5], [0, 0, 1]])
    matrix = (to_hires @ a3 @ to_world)[:2]
    hires = cv2.warpAffine(raster, matrix, (MAP_SIZE * SUBSAMPLE, MAP_SIZE * SUBSAMPLE),
                           flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                           borderValue=border)
    return cv2.resize(hires, (MAP_SIZE, MAP_SIZE), interpolation=cv2.INTER_AREA)


def door_mask(warps, affine, radius=DOOR_CLOSE_RADIUS_PX):
    """传送门周围 radius 个小地图像素内 -> True。"""
    yy, xx = np.mgrid[0:MAP_SIZE, 0:MAP_SIZE]
    mask = np.zeros((MAP_SIZE, MAP_SIZE), bool)
    for o in warps:
        if o.get("type") != "warp":
            continue
        cx = affine[0, 0] * o["x"] + affine[0, 2]
        cy = affine[1, 1] * o["y"] + affine[1, 2]
        mask |= (xx - cx) ** 2 + (yy - cy) ** 2 <= radius ** 2
    return mask


def derive(data_dir, name, threshold=WALL_COVERAGE_THRESHOLD):
    """官方 .tmj -> 300x300 uint8 二值图(255 = 可走)。"""
    spec = MAP_SPECS[name]
    j, layers, objects = load_map(data_dir, name)
    id2img = load_tileset(data_dir)

    def union(names):
        acc = np.zeros((j["height"] * MASK_PX, j["width"] * MASK_PX), np.float32)
        for layer_name in names:
            np.maximum(acc, raster_layer(layers[layer_name], id2img, data_dir), out=acc)
        return acc

    walls = union(spec.get("walls", ()))
    if spec.get("floors"):
        walls = np.maximum(walls, 1.0 - union(spec["floors"]))
    cell = TILE / MASK_PX
    for o in objects.get(spec.get("collision_objects", ""), []):
        walls[int(o["y"] / cell):int((o["y"] + o["height"]) / cell),
              int(o["x"] / cell):int((o["x"] + o["width"]) / cell)] = 1.0

    affine = world_to_index_affine(j["width"])
    coverage = render_coverage(walls, affine)
    walkable = coverage <= threshold
    if spec.get("shortcut_layer"):
        shortcut = render_coverage(union([spec["shortcut_layer"]]), affine, border=0.0)
        walkable |= (shortcut > SHORTCUT_COVERAGE) & (coverage < 0.5)
    walkable &= ~door_mask(objects.get("warps", []), affine)
    return np.where(walkable, 255, 0).astype(np.uint8)


def agreement(a, b):
    """两张二值图相同像素的占比。"""
    return float((a == b).mean())


def _file_name(name):
    return MAP_FILE_NAMES.get(name, name)


def _summary(binary):
    walkable = (binary == 255).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(walkable, connectivity=8)
    areas = [int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, count)]
    total = sum(areas) or 1
    return (f"可走 {100 * walkable.mean():.1f}%, {len(areas)} 块, "
            f"最大块占 {100 * max(areas, default=0) / total:.2f}%")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True, help="官方地图数据目录(见模块文档)")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="推导并写 <out>/<name>.png")
    build.add_argument("names", nargs="+", choices=sorted(MAP_SPECS))
    build.add_argument("--out", default="./maps")
    build.add_argument("--threshold", type=float, default=WALL_COVERAGE_THRESHOLD)
    compare = sub.add_parser("compare", help="跟已发布的 maps/<name>.png 对拍, 只打印")
    compare.add_argument("names", nargs="+", choices=sorted(MAP_SPECS))
    compare.add_argument("--maps", default="./maps")
    compare.add_argument("--threshold", type=float, default=WALL_COVERAGE_THRESHOLD)
    args = parser.parse_args(argv)

    for name in args.names:
        derived = derive(args.data, name, args.threshold)
        if args.command == "build":
            path = os.path.join(args.out, _file_name(name) + ".png")
            cv2.imwrite(path, derived)
            print(f"✅ {name} -> {path}  ({_summary(derived)})")
        else:
            path = os.path.join(args.maps, _file_name(name) + ".png")
            real = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if real is None:
                print(f"❌ {name}: 读不出 {path}")
                return 1
            false_walk = float(((derived == 255) & (real != 255)).mean())
            false_wall = float(((derived != 255) & (real == 255)).mean())
            print(f"{name}: 吻合率 {100 * agreement(derived, real):.2f}%  "
                  f"(推导可走/实机是墙 {100 * false_walk:.2f}%, "
                  f"推导是墙/实机可走 {100 * false_wall:.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
