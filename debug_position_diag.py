"""诊断脚本: 精确复现get_player_location_on_map()内部逻辑, 不靠肉眼看图/看颜色排行榜猜.

跟debug.py的区别: debug.py列"出现次数最多的颜色" —— 玩家标记只是个小圆点, 在
300x300=90000像素里占比太小, 根本进不了前15名, 所以那份颜色表看不出标记在不在.
这份直接用跟get_player_location_on_map()一模一样的容差去数玩家标记颜色范围内的
像素有多少个、在哪, 不靠猜.

颜色/容差/半径阈值(PLAYER_MARKER_COLOR / PLAYER_MARKER_TOLERANCE /
PLAYER_MARKER_MIN_RADIUS)全部从 utils 直接 import, 不在这里另抄一份 —— 之前脚本
自己硬编码过一份`radius > 2`, utils.py 那边后来调成了`> 1`, 脚本没跟着改, 打出来
的"诊断结论"其实跟真代码判的不是一回事, 把人绕晕过一次(2026-08-26). 现在共用
同一份常量, 不会再分叉.

用法: python debug_position_diag.py
"""
import time
import cv2
import numpy as np
import pyautogui
from utils import (get_map, load_binary_map, MAP, minimap_capture_region,
                   SCREEN_WIDTH, SCREEN_HEIGHT, PLAYER_MARKER_COLOR,
                   PLAYER_MARKER_MIN_RADIUS, PLAYER_MARKER_TOLERANCE)


def main():
    print(f"检测到的分辨率: {SCREEN_WIDTH}x{SCREEN_HEIGHT}")
    print(f"当前MAP变量: {MAP!r} (空字符串说明还没调用过apply_map(), load_binary_map会失败, 不影响这次诊断)")

    region = minimap_capture_region()
    raw_w, raw_h = region[2], region[3]
    print(f"get_map()理论截图区域(minimap_capture_region()): {region}")
    if raw_w < 300 or raw_h < 300:
        shrink = min(raw_w, raw_h) / 300
        print(f"⚠️  这块区域比 300x300 小(截到 {raw_w}x{raw_h}), get_map() 会把它放大"
              f"回 300x300 —— 玩家标记的像素footprint会跟着缩水到约 {shrink:.2f} 倍,"
              f" 半径卡在 PLAYER_MARKER_MIN_RADIUS 附近时容易帧与帧之间时测得到时测"
              f"不到. 2026-08-26 在 1024x768 上复现过一模一样的现象(区域约209x212,"
              f" 标记半径量出来1.90px, 刚好卡线).")
    print("\n⏳ 5秒后截屏, 这段时间切到游戏窗口, 保持全屏、角色在场景里能看见小地图...\n")
    for i in range(5, 0, -1):
        print(f"   {i}...")
        time.sleep(1)

    # 先截一张全屏, 把"理论截图区域该在哪"画成红框——比让人盯着300x300小图猜"对没对准"
    # 直接得多: 红框没框住小地图, 一眼就看出来是区域算错了; 框住了但小地图里没有那个
    # 黄点, 那就是另一类问题(缩放/UI遮挡/小地图当时没渲染标记), 两种情况肉眼一眼分清,
    # 不用再靠"我觉得截图没问题"这种主观判断.
    full = np.array(pyautogui.screenshot())
    full = cv2.cvtColor(full, cv2.COLOR_RGB2BGR)
    fx, fy, fw, fh = region
    overlay = full.copy()
    cv2.rectangle(overlay, (fx, fy), (fx + fw, fy + fh), (0, 0, 255), 3)
    cv2.putText(overlay, f"minimap_capture_region() = {region}", (max(fx - 300, 10), max(fy - 15, 25)),
               cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    cv2.imwrite("./debug_position_diag_fullscreen.png", overlay)
    print("✅ 已保存 debug_position_diag_fullscreen.png (全屏截图, 红框=理论截图区域"
          "——红框应该正好框住小地图; 没框住说明区域算错了, 这张图请务必发出来看)")

    image = get_map()  # 已经resize回300x300了
    h, w = image.shape[:2]
    print(f"\n✅ get_map()返回图像尺寸: {w}x{h} (应该恒为300x300, 不管什么分辨率)")
    cv2.imwrite("./debug_position_diag_raw.png", image)
    print("✅ 已保存 debug_position_diag_raw.png (get_map()截到的原始画面, 可以自己打开看是不是小地图)")

    target_bgr = tuple(int(PLAYER_MARKER_COLOR[i:i + 2], 16) for i in (4, 2, 0))
    t = PLAYER_MARKER_TOLERANCE
    lower = np.array([max(0, c - t) for c in target_bgr])
    upper = np.array([min(255, c + t) for c in target_bgr])
    mask = cv2.inRange(image, lower, upper)
    match_count = int(np.count_nonzero(mask))
    print(f"\n🎯 跟玩家标记颜色{PLAYER_MARKER_COLOR}(±{t}容差)匹配的像素数: {match_count} / {w*h}")

    if match_count == 0:
        print("❌ 一个匹配的像素都没有 —— 说明get_map()截到的画面里根本没有这个黄色标记.")
        print("   先看 debug_position_diag_fullscreen.png: 红框有没有框住小地图?")
        print("   - 没框住 -> 截图区域算错了。Windows 上常见元凶: 系统显示缩放不是100%")
        print("     (设置->系统->显示->缩放), 会让 pyautogui 截图/点击坐标跟真实像素错位,")
        print("     跟分辨率本身对不对没关系——先去确认一下这个缩放比例是多少.")
        print("   - 框住了但框里没有黄点 -> 再看 debug_position_diag_raw.png, 小地图本身是不是")
        print("     被放大了(M键)/被弹窗挡住了/角色那一刻真的死了或在菜单上")
    else:
        contours, _ = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        print(f"   匹配像素聚成了 {len(contours)} 个连通块, 逐个看半径"
              f"(get_player_location_on_map要求radius>{PLAYER_MARKER_MIN_RADIUS}才算数):")
        found_valid = False
        marked = image.copy()
        for c in contours:
            (x, y), radius = cv2.minEnclosingCircle(c)
            ok = radius > PLAYER_MARKER_MIN_RADIUS
            flag = f"✅ 达标(radius>{PLAYER_MARKER_MIN_RADIUS})" if ok else \
                   f"❌ 太小(radius<={PLAYER_MARKER_MIN_RADIUS}, 会被判定为噪声忽略)"
            if ok:
                found_valid = True
            print(f"   - 位置({x:.1f}, {y:.1f}), 半径{radius:.2f}px  {flag}")
            cv2.circle(marked, (int(x), int(y)), max(3, int(radius) + 2), (0, 0, 255), 1)
        cv2.imwrite("./debug_position_diag_marked.png", marked)
        print("✅ 已保存 debug_position_diag_marked.png (红圈标出了每个候选点)")
        if found_valid:
            print(f"\n✅ 至少有一个候选点半径>{PLAYER_MARKER_MIN_RADIUS}, 理论上"
                  f"get_player_position()应该能返回结果 —— 如果实机还是卡住, 问题可能在"
                  f"calibrate_player或更上游, 需要再往下查.")
        else:
            print(f"\n❌ 匹配到像素了, 但每个连通块半径都<={PLAYER_MARKER_MIN_RADIUS} —— "
                  f"标记点太小太碎, 被当成噪声过滤掉了. 这种情况通常是截图区域比300x300小"
                  f"(见上面的分辨率警告)导致标记缩水, 是真实的检测极限问题——多截几次跑这个"
                  f"脚本, 如果半径经常卡在临界值附近(1~2px)反复横跳, 基本能实锤.")


if __name__ == "__main__":
    main()
