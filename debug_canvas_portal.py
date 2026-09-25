"""诊断: canvas 绘制记录里, 蚁穴洞口到底是哪一条, 世界坐标是多少.

动机 —— 现在寻路/进场靠的是"300x300 缩小小地图 + 像素容差框", 精度天生有限
(map_routes.PORTAL_TOLERANCE=±2 这个容差框比florr真正触发传送要求的精度松,
实机复盘过角色蹭到框边被判"到了"但传送根本没发生)。canvas_decode.py 已经会
从绘制记录里反解出玩家的**绝对世界坐标**(camera_from_frame/screen_to_world,
洞口只要是画出来的东西, 也能同样反解出一个绝对世界坐标 —— 不经过300x300那层
缩放/量化, 精度是florr自己内部用的浮点数。洞口是静态的, 一旦反解出来就能直接
硬编码使用, 不用每次都重新识别.

跑法(florr.io 已在带 --remote-debugging-port=9222 的 Chrome 里打开, **站在
花园里洞口附近、洞口在屏幕上可见**):

    python debug_canvas_portal.py

它会连续 drain 几秒, 从最新一帧完整记录里:
  1. 解出这一帧的相机信息(camera_from_frame) —— 玩家绝对世界坐标 + 屏幕锚点 + 缩放
  2. 列出所有"候选"绘制记录 —— 排除掉已知是玩家本体/怪物血条/UI文字的, 按跟屏幕
     中心的距离从近到远排, 每条打印: 屏幕锚点、半径、填充色、是不是小地图绘制、
     换算出来的世界坐标(小地图绘制直接用小地图公式; 主视图绘制用 screen_to_world)
  3. 提示怎么从列表里挑出洞口那条: 找 fill 色接近之前标定过的传送点绿
     (HSV大约 59,135,228, 对应 utils._PORTAL_GREEN_HSV_LO/HI 那个范围), 且屏幕
     锚点大致落在肉眼看到洞口的位置附近的那条.
不改游戏, 只读.
"""
import math
import time

import cv2
import numpy as np

import cdp_bridge
import canvas_decode
import utils


def _drain_for(seconds=3.0):
    buf = []
    try:
        cdp_bridge.inject_canvas_hook()
    except RuntimeError as e:
        print(f"⚠️ inject_canvas_hook 抛错: {e}")
        print("   (hook 没装上 —— 后面基本会是空的; 先重进游戏再跑)")
    end = time.time() + seconds
    while time.time() < end:
        try:
            buf.extend(cdp_bridge.drain_canvas_log())
        except Exception as e:  # noqa: BLE001  诊断脚本, 什么都想看见
            print(f"⚠️ drain_canvas_log 抛错: {type(e).__name__}: {e}")
            break
        time.sleep(0.1)
    return buf


# 已知不是洞口的东西, 排除掉别让候选列表被刷屏怪物/玩家本体的记录淹没.
_KNOWN_PLAYER_FILLS = {"#CFBB50", "#FFE763", "#111111"}
_KNOWN_HEALTHBAR_STROKES = {"#222222", "#DD3434", "#42E3F5"}


def is_greenish(fill_hex):
    """粗略判断一个 CSS 颜色字符串是不是"传送点绿"附近的色相 —— 用来在候选列表里
    highlight, 不是精确判定(精确判定用 utils._PORTAL_GREEN_HSV_LO/HI, 那个是在
    300x300小地图截图上标定的, 跟这里 canvas fill 色字符串不是同一个数值空间,
    不能直接套用同一对边界)。

    实机复盘(2026-09-17): fillStyle 不一定是字符串 —— florr 有的绘制用
    CanvasGradient/CanvasPattern 对象(比如带光效的圆), CDP returnByValue 序列化
    出来是个空 dict(那类对象没有可枚举的自有属性, 序列化不出内容)。这种情况下
    "是不是绿的"这个问题本身没有答案(色值没传下来), 不是"不绿", 是"不知道"——
    两者都返回 False, 但下面 main() 里会分开标注, 别让人以为真的判过了。
    """
    if not isinstance(fill_hex, str):
        return False
    if not fill_hex or not fill_hex.startswith("#") or len(fill_hex) != 7:
        return False
    try:
        r, g, b = (int(fill_hex[i:i+2], 16) for i in (1, 3, 5))
    except ValueError:
        return False   # 不是合法十六进制(比如 "#zzzzzz") —— 当成"不是绿的", 不崩
    bgr = np.uint8([[[b, g, r]]])
    h, s, v = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[0, 0]
    # 显式转成 Python bool —— h/s/v 是 numpy 标量, 比较结果是 numpy.bool_, 跟
    # 调用方拿 `is True`/`is False` 做身份比较会假失败(值对但不是同一个类型/对象).
    return bool(40 <= h <= 80 and s >= 40 and v >= 40)


def candidate_fills(records, camera=None):
    """从一帧记录里挑出"可能是洞口"的圆弧填充: 排除已知的玩家本体相关颜色,
    只留 op=fill 且有半径的记录。按跟玩家屏幕锚点的距离升序排 —— 玩家是专门
    走近洞口去看的, 离玩家近的候选比远处的场景装饰更可能是洞口。camera 解不出
    时(比如设置面板挡住了玩家本体)退化成按跟画布原点的距离排, 好歹给个确定
    顺序, 不比完全不排序更差.

    实机复盘(2026-09-17): fill 字段不一定是字符串(CanvasGradient/CanvasPattern
    序列化出来是个 dict, 不能拿去跟字符串常量集合做 `in` 成员判断 —— 这本身
    会直接抛 TypeError: unhashable type)。渐变/图案填充恰恰可能就是带光效的
    洞口本体, 不能被这条过滤误伤掉: 非字符串的 fill 一律当"不是已知玩家色",
    照样收进候选列表.
    """
    if camera is not None:
        px, py = camera["player_screen"]
    else:
        px, py = 0.0, 0.0
    out = []
    for r in records:
        if r.get("op") != "fill" or r.get("r") is None:
            continue
        fill = r.get("fill")
        if isinstance(fill, str) and fill in _KNOWN_PLAYER_FILLS:
            continue
        out.append(r)
    out.sort(key=lambda r: (r["x"] - px) ** 2 + (r["y"] - py) ** 2)
    return out


def world_position_of(rec, camera):
    """反解一条绘制记录的绝对世界坐标。小地图绘制直接用小地图公式(不需要相机);
    主视图绘制需要 camera(screen_to_world) —— camera 为 None 时主视图记录解不出,
    返回 None."""
    if canvas_decode._is_minimap(rec):
        m = rec["m"]
        return ((rec["x"] - m[4]) / m[0], (rec["y"] - m[5]) / m[3])
    if camera is None:
        return None
    return canvas_decode.screen_to_world(rec["x"], rec["y"], camera)


def main():
    raw = _drain_for(3.0)
    print(f"\n=== 原始 drain: {len(raw)} 条记录 ===")
    if not raw:
        print("空。hook 没生效, 或者画面里什么都没画(florr 标签页在后台?)。")
        return

    frames = canvas_decode.group_by_frame(raw)
    keys = sorted(frames)
    if len(keys) < 2:
        print("⚠️ 少于 2 个不同帧号 —— __canvasFrame 没在推进, canvas 解码这时候永远是空的。重进游戏。")
        return
    recs = frames[keys[-2]]   # 最新那帧可能没画完, 取次新(跟 debug_canvas_enemies.py 一致)
    print(f"次新帧(frame {keys[-2]}): {len(recs)} 条记录")

    camera = None
    try:
        camera = canvas_decode.camera_from_frame(recs)
        print(f"\n=== camera_from_frame ===")
        print(f"  zoom={camera['zoom']:.4f}  player_world={camera['player_world']}  "
              f"player_screen={camera['player_screen']}")
    except ValueError as e:
        print(f"\n=== camera_from_frame ===\n  ⚠️ {e}")
        print("  (解不出相机的话, 下面主视图记录的世界坐标那一栏会是 None ——")
        print("   小地图记录不受影响, 仍然能直接算出世界坐标)")

    # 实机复盘(2026-09-17): 头几十条几乎全是玩家自己的花瓣(florr核心玩法, 花瓣
    # 绕着玩家转, 每帧都在重绘, 颜色是灰/白色系, 紧贴玩家屏幕位置) —— 之前这里
    # 硬截了个只打印最近40条, 洞口这种远处静止的地形物按"离玩家距离"排, 大概率
    # 被花瓣挤到40条以外, 直接被截断没打出来。现在把上限提到200(94条实测样本
    # 全部展示都不算多), 并且把"离玩家距离"直接打出来 —— 花瓣紧贴玩家、洞口是
    # 玩家走过去才会出现在附近, 从列表里能直接看到"距离突然变大"的分界线在哪.
    cands = candidate_fills(recs, camera)
    print(f"\n=== 候选圆弧填充记录: {len(cands)} 条(已排除玩家本体已知色) ===")
    print("找洞口: 找 fill 色接近传送点绿(标 ★)、或者标 ?(渐变/图案填充, 拿不到颜色但")
    print("不代表不是它——带光效的洞口很可能就是这种), 且屏幕锚点大致在肉眼看到")
    print("洞口位置附近的那条。开头一大片灰白色、紧贴玩家、半径个位数的通常是")
    print("花瓣, 不是洞口 —— 往下翻, 找 dist 突然变大、或者形状/颜色明显不一样的。\n")
    px, py = (camera["player_screen"] if camera is not None else (0.0, 0.0))
    DISPLAY_CAP = 200
    for i, r in enumerate(cands[:DISPLAY_CAP]):
        wpos = world_position_of(r, camera)
        fill = r.get("fill")
        # fill 不一定是字符串 —— CanvasGradient/CanvasPattern 序列化出来是个空
        # dict, 拿不到颜色信息(不是"确认不是绿的", 是"没法判断", 两者要分清楚,
        # 不然人看着"没打★"会误以为已经排除掉了).
        if isinstance(fill, str):
            mark = "★" if is_greenish(fill) else " "
            fill_s = fill
        else:
            mark = "?"
            fill_s = "<渐变/图案, 拿不到颜色>"
        minimap_tag = "小地图" if canvas_decode._is_minimap(r) else "主视图"
        wpos_s = f"world=({wpos[0]:.1f},{wpos[1]:.1f})" if wpos else "world=? (camera未解出)"
        dist = ((r["x"] - px) ** 2 + (r["y"] - py) ** 2) ** 0.5
        # 世界坐标空间里离玩家真实位置有多远 —— 跟上面那个屏幕像素距离(dist)是
        # 两个不同的尺度: 小地图记录哪怕屏幕上就差1像素, 换算到世界坐标可能也是
        # 几百个单位(300x300那层压缩/量化的代价); 主视图记录不经过那层压缩,
        # 这个数字才是真正判断"这条候选是不是玩家脚下那个洞口"该看的指标.
        if wpos is not None and camera is not None:
            wdist = math.hypot(wpos[0] - camera["player_world"][0],
                               wpos[1] - camera["player_world"][1])
            wdist_s = f"世界距玩家={wdist:7.1f}"
        else:
            wdist_s = "世界距玩家=?"
        print(f"  {mark} #{i:3d} dist={dist:6.1f} 屏幕=({r['x']:7.1f},{r['y']:7.1f}) r={r['r']:.1f} "
              f"fill={fill_s:22} [{minimap_tag}]  {wpos_s}  {wdist_s}")

    if len(cands) > DISPLAY_CAP:
        print(f"  ...(还有 {len(cands)-DISPLAY_CAP} 条, 只打了最近的 {DISPLAY_CAP} 条)")

    print("\n把上面标★/?、或者 dist 突然变大、或者屏幕坐标跟洞口位置对得上的那一行")
    print("发过来, 世界坐标定下来就能直接硬编码进 map_routes.py, 不用再靠300x300")
    print("小地图容差框判断到没到了。")


if __name__ == "__main__":
    main()
