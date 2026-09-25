"""在一张图上点一下, 读出它的像素坐标.

    python map_select.py                      # 默认开 maps/desert.png
    python map_select.py anthell              # maps/anthell.png
    python map_select.py debug_garden_raw.png # 任意图片路径

参数既可以是 maps/ 下的地图名(不带 .png), 也可以是任意一张图的路径 —— 后者是
标定蚁穴洞口时要用的: capture_map.py 会同时输出 maps/garden.png(二值寻路图) 和
debug_garden_raw.png(同一次截图的原色版). 二值图把颜色丢掉了, 洞口肉眼看不出来,
所以要在**原色**那张上点. 两张图来自同一次 utils.get_map(), 坐标系完全一致, 在
原色图上量出来的 (x, y) 可以直接填进 map_routes.ANTHELL_PORTAL.

左键点一下就在终端打印坐标, 同时在图上画个红点标出来(点错了再点一次, 旧的会消失).
按任意键关窗口.
"""
import argparse
import os
import sys

import cv2

_MAP_DIR = "./maps"


def resolve_image_path(name):
    """参数既可以是地图名(anthell)也可以是图片路径(debug_garden_raw.png).
    先按路径试, 找不到再当成 maps/<name>.png."""
    if os.path.isfile(name):
        return name
    candidate = os.path.join(_MAP_DIR, f"{name}.png")
    if os.path.isfile(candidate):
        return candidate
    raise SystemExit(
        f"❌ 找不到 {name!r}: 既不是一个文件, {candidate} 也不存在.\n"
        f"   maps/ 下现有: {sorted(p.removesuffix('.png') for p in os.listdir(_MAP_DIR))}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", nargs="?", default="desert",
                        help="maps/ 下的地图名(如 anthell), 或任意图片路径"
                             "(如 debug_garden_raw.png). 默认 desert")
    args = parser.parse_args(argv)

    path = resolve_image_path(args.name)
    image = cv2.imread(path)
    if image is None:
        raise SystemExit(f"❌ {path} 读不出来(不是图片, 或者文件坏了)")
    print(f"🖼️  {path}  ({image.shape[1]}x{image.shape[0]})")
    print("   左键点目标点 -> 终端打印坐标; 按任意键关窗口")

    original = image.copy()
    state = {"shown": image}

    def on_mouse_click(event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        print(f"Map position: ({x}, {y})")
        marked = original.copy()
        cv2.circle(marked, (x, y), radius=2, color=(0, 0, 255), thickness=-1)
        cv2.putText(marked, f"({x}, {y})", (x + 5, y - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (4, 250, 4), 1)
        state["shown"] = marked
        cv2.imshow("Map", marked)

    cv2.imshow("Map", image)
    cv2.setMouseCallback("Map", on_mouse_click)
    cv2.waitKey(0)
    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
