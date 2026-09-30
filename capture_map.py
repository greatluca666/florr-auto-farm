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

    python capture_map.py jungle --compare   # 局内核对推导出来的图, 不写图

--compare 不生成任何图: 把当前小地图跟已发布的 maps/<name>.png 对拍, 打印吻合率并
写 debug_<name>_compare.png(白=都可走 黑=都是墙 红=实机可走但已发布是墙 蓝=反过来)。
丛林/下水道/工厂没有实机截图(tmj_maps.py 按官方数据推导), 进局内用它核对。

--portal X,Y 是遗留选项, 花园图不再需要: 传送门在 maps/garden.png 里一律是墙,
走门那一段由运行时按 map_routes.PORTAL_OPENINGS 只挖目标那一扇。带上它会把那一扇门
烤进二值图, 别给花园图这么干。
"""
import argparse
import os
import sys

import cv2
import numpy as np

import map_routes
import utils

DEFAULT_THRESHOLD = 200

# 地图名 -> 已标定的密道矩形列表(见 map_routes 里对应的 *_SHORTCUTS 常量)。
# 按名字自动查, 不用每次手动传坐标 —— 密道跟传送点不一样, 一张图上有几条是
# 固定的, 不像 --portal 那样"这次要走进哪一个"每条路线可能不一样。
_KNOWN_SHORTCUTS = {
    "garden": map_routes.GARDEN_SHORTCUTS,
    "anthell": map_routes.ANTHELL_SHORTCUTS + map_routes.ANTHELL_PORTAL_CLEARINGS,
    "desert": map_routes.DESERT_SHORTCUTS,
    "ocean": map_routes.OCEAN_SHORTCUTS,
}


def parse_point(text):
    """"133,234" -> (133, 234)。给 --portal 用。"""
    try:
        x, y = (int(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"要写成 x,y 两个整数, 收到的是 {text!r}")
    return (x, y)


def capture(name, threshold=DEFAULT_THRESHOLD, map_dir="./maps", portal=None, raw_image=None):
    """截一次当前小地图, 写 <map_dir>/<name>.png 和 debug_<name>_raw.png。
    portal=(x,y) 时只把那一个传送点绿斑打通成可走(见 utils._open_portal_blob)。
    raw_image 给了就用它、不截屏也不写原色副本(用已有的原色截图重新生成)。
    返回 (binary_path, raw_path); 用 raw_image 时 raw_path 是 None。"""
    if raw_image is None:
        raw = utils.get_map()
        raw_path = f"debug_{name}_raw.png"
        cv2.imwrite(raw_path, raw)
    else:
        raw, raw_path = raw_image, None
    binary_path = os.path.join(map_dir, f"{name}.png")
    utils.preprocess_map(raw, out_path=binary_path, threshold=threshold,
                         open_portal_at=portal,
                         open_shortcuts_at=_KNOWN_SHORTCUTS.get(name))
    return binary_path, raw_path


def compare_with_published(name, threshold=DEFAULT_THRESHOLD, map_dir="./maps", raw_image=None):
    """把当前小地图二值化后跟已发布的 <map_dir>/<name>.png 对拍(不改任何地图)。

    给 tmj_maps.py 推导出来的图(丛林/下水道/工厂)在局内核对用 —— 它们没有实机截图。
    返回 (吻合率, 实机可走/已发布是墙的占比, 实机是墙/已发布可走的占比), 同时写
    debug_<name>_compare.png: 白=都可走 黑=都是墙 红=实机可走但已发布是墙 蓝=实机是墙但已发布可走。
    玩家标记 / 传送点绿斑会算成几个不一致的像素, 正常。
    已知密道矩形(_KNOWN_SHORTCUTS)照 capture() 的做法打通后再比, 跟已发布图同口径。
    """
    path = os.path.join(map_dir, f"{name}.png")
    # 先读已发布图再去截屏: 图不存在就立刻报错, 别白白截一次屏。
    # _ensure_grayscale_2d: Windows 上 IMREAD_GRAYSCALE 实测吐过 (H,W,1), --compare 恰恰要在
    # 那台机器上跑, 不锁形状的话 live & published 会广播成 (300,300,300) 然后 IndexError。
    published_img = utils._ensure_grayscale_2d(cv2.imread(path, cv2.IMREAD_GRAYSCALE))
    if published_img is None:
        raise SystemExit(f"❌ 读不出已发布的 {path}")
    published = published_img == 255
    raw = utils.get_map() if raw_image is None else raw_image
    # open_shortcuts_at 跟 capture() 保持一致: 已发布图里已经烤进了密道矩形(花园/蚁穴/沙漠/海洋),
    # 不开的话那些矩形会被当成"已发布可走/实机是墙"的假差异。
    live = utils.preprocess_map(raw, out_path=None, threshold=threshold,
                                open_shortcuts_at=_KNOWN_SHORTCUTS.get(name)) == 255
    if live.shape != published.shape:
        raise SystemExit(f"❌ 形状对不上: 实机小地图 {live.shape}, 已发布的 {path} {published.shape}")
    diff = np.zeros(live.shape + (3,), dtype=np.uint8)
    diff[live & published] = (255, 255, 255)
    diff[live & ~published] = (0, 0, 255)
    diff[~live & published] = (255, 0, 0)
    cv2.imwrite(f"debug_{name}_compare.png", diff)
    return (float((live == published).mean()),
            float((live & ~published).mean()),
            float((~live & published).mean()))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="地图名, 例如 garden —— 会写成 maps/<name>.png")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                        help=f"二值化阈值(默认 {DEFAULT_THRESHOLD}). "
                             "生成的图墙/地板分不开时调这个")
    parser.add_argument("--portal", type=parse_point, default=None, metavar="X,Y",
                        help="(遗留选项, 花园图不要传)把这个坐标(如 133,234)上的传送点绿斑"
                             "烤成可走。花园图不再需要: 门在图里一律是墙, 走门时运行时只挖"
                             "目标那一扇(map_routes.PORTAL_OPENINGS)")
    parser.add_argument("--raw", default=None, metavar="FILE",
                        help="用这张已有的原色小地图截图重新生成(不截屏), 例如 debug_garden_raw.png")
    parser.add_argument("--compare", action="store_true",
                        help="不写图, 只把当前小地图跟已发布的 maps/<name>.png 对拍并写 "
                             "debug_<name>_compare.png")
    args = parser.parse_args(argv)
    raw_image = None
    if args.raw is not None:
        raw_image = cv2.imread(args.raw)
        if raw_image is None:
            raise SystemExit(f"❌ 读不出 {args.raw}")
    if args.compare:
        agree, live_only, published_only = compare_with_published(
            args.name, threshold=args.threshold, raw_image=raw_image)
        print(f"{args.name}: 吻合率 {100 * agree:.2f}%  "
              f"(实机可走/已发布是墙 {100 * live_only:.2f}%, 实机是墙/已发布可走 {100 * published_only:.2f}%)")
        print(f"   差异图 debug_{args.name}_compare.png (红=实机可走但已发布是墙, 蓝=反过来)")
        return 0
    binary_path, raw_path = capture(args.name, threshold=args.threshold,
                                    portal=args.portal, raw_image=raw_image)
    lines = ["✅ 写好了:", f"   {binary_path}\t(寻路二值图)"]
    if raw_path is not None:
        lines.append(f"   {raw_path}\t(原色, 肉眼找地标用)")
    print("\n".join(lines))
    if raw_path is not None:
        print("   对着原色图找到目标点, 用 GUI 的地图选点器或 map_select.py 读出坐标。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
