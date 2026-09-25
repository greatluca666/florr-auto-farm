"""DEV TOOL — 在游戏局内跑一次, 生成某张图的二值寻路模板。

    python capture_map.py garden

产出两个文件:

    maps/garden.png        寻路用的二值图 —— utils.load_binary_map() 读的就是它
    debug_garden_raw.png   同一次截图的**原色**版本, 给你肉眼找地标用
                           (二值图丢掉了颜色, 看不出哪个是蚁穴洞口)

两张图来自同一次 utils.get_map(), 所以坐标系完全一致: 你在原色图上量出来的
(x, y) 可以直接填进 map_routes.ANTHELL_PORTAL。

前提: florr 全屏跑着、人在局内、小地图没被 M 键放大(跟 worker 跑起来时一样)。

生成出来墙和地板糊在一起 -> 调 --threshold(默认 200)。
"""
import argparse
import os
import sys

import cv2

import map_routes
import utils

DEFAULT_THRESHOLD = 200

# 地图名 -> 已标定的密道矩形列表(见 map_routes 里对应的 *_SHORTCUTS 常量)。
# 按名字自动查, 不用每次手动传坐标 —— 密道跟传送点不一样, 一张图上有几条是
# 固定的, 不像 --portal 那样"这次要走进哪一个"每条路线可能不一样。
_KNOWN_SHORTCUTS = {
    "garden": map_routes.GARDEN_SHORTCUTS,
    "anthell": map_routes.ANTHELL_SHORTCUTS,
}


def parse_point(text):
    """"133,234" -> (133, 234)。给 --portal 用。"""
    try:
        x, y = (int(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"要写成 x,y 两个整数, 收到的是 {text!r}")
    return (x, y)


def capture(name, threshold=DEFAULT_THRESHOLD, map_dir="./maps", portal=None):
    """截一次当前小地图, 写 <map_dir>/<name>.png 和 debug_<name>_raw.png。
    portal=(x,y) 时只把那一个传送点绿斑打通成可走(见 utils._open_portal_blob)。
    返回 (binary_path, raw_path)。"""
    raw = utils.get_map()
    raw_path = f"debug_{name}_raw.png"
    cv2.imwrite(raw_path, raw)
    binary_path = os.path.join(map_dir, f"{name}.png")
    utils.preprocess_map(raw, out_path=binary_path, threshold=threshold,
                         open_portal_at=portal,
                         open_shortcuts_at=_KNOWN_SHORTCUTS.get(name))
    return binary_path, raw_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="地图名, 例如 garden —— 会写成 maps/<name>.png")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                        help=f"二值化阈值(默认 {DEFAULT_THRESHOLD}). "
                             "生成的图墙/地板分不开时调这个")
    parser.add_argument("--portal", type=parse_point, default=None, metavar="X,Y",
                        help="要走进去的那个传送点坐标(如 133,234)。传送点在小地图上"
                             "是亮绿色圆点, 灰度低于阈值会被当成墙 —— 目标点是墙的话"
                             "寻路直接规划失败。只打通这一个, 别的绿点留着当墙好让"
                             "寻路绕开(免得路上踩中别的传送点被拽走)。"
                             "花园图要传, 值就是 map_routes.ANTHELL_PORTAL")
    args = parser.parse_args(argv)
    binary_path, raw_path = capture(args.name, threshold=args.threshold,
                                    portal=args.portal)
    print(f"✅ 写好了:\n   {binary_path}\t(寻路二值图)\n   {raw_path}\t(原色, 肉眼找地标用)")
    print("   对着原色图找到目标点, 用 GUI 的地图选点器或 map_select.py 读出坐标。")
    if args.portal is None:
        print("   ⚠️ 没传 --portal: 图上的传送点绿圆点会是墙。要走进某个传送点的话, "
              "先读出它的坐标再带 --portal x,y 重跑一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
