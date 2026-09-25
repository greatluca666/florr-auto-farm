"""诊断脚本: 精确复现on_start_screen()/on_death_screen()/check_stage()内部逻辑,
在一张全屏截图上标出这些函数实际在看屏幕的哪个点/哪块区域 —— 不用肉眼猜button坐标
对不对.

用法: 保持florr.io全屏, 想诊断哪个画面就先手动切到那个画面(比如故意去点一下M菜单
外的地方回到开局菜单, 或者等一次死亡结算画面出现), 然后跑:
    python debug_stage_diag.py
"""
import time

import cv2
import numpy as np
import pyautogui

from utils import (
    SCREEN_WIDTH, SCREEN_HEIGHT,
    _START_BUTTON_POS, _CONTINUE_BUTTON_POS,
    _DEATH_SCREEN_SAMPLE_HALF_W, _DEATH_SCREEN_SAMPLE_HALF_H,
    _green_button_ratio, on_start_screen, on_death_screen, check_stage,
    scale_point,
)


def main():
    print(f"检测到的分辨率: {SCREEN_WIDTH}x{SCREEN_HEIGHT}")
    print(f"_START_BUTTON_POS (开始按钮, 参照1059,527缩放后): {_START_BUTTON_POS}")
    print(f"_CONTINUE_BUTTON_POS (继续按钮, 参照959,634缩放后): {_CONTINUE_BUTTON_POS}")
    print("\n⏳ 5秒后截屏, 这段时间别动游戏画面(想测哪个界面提前切好)...\n")
    for i in range(5, 0, -1):
        print(f"   {i}...")
        time.sleep(1)

    start_ratio = _green_button_ratio(_START_BUTTON_POS)
    death_ratio = _green_button_ratio(
        _CONTINUE_BUTTON_POS,
        half_w=_DEATH_SCREEN_SAMPLE_HALF_W,
        half_h=_DEATH_SCREEN_SAMPLE_HALF_H,
    )
    is_start = on_start_screen()
    is_death = on_death_screen()
    stage = check_stage()

    print(f"\non_start_screen(): {is_start}  (绿色占比={start_ratio:.4f}, 阈值>0.1)")
    print(f"on_death_screen(): {is_death}  (绿色占比={death_ratio:.4f}, 阈值>0.15)")
    print(f"check_stage(): {stage!r}")

    p1 = scale_point(316, 32)
    p2 = scale_point(156, 35)
    full = pyautogui.screenshot(region=[0, 0, SCREEN_WIDTH, SCREEN_HEIGHT])
    color1 = full.getpixel(p1)
    color2 = full.getpixel(p2)
    print(f"  check_stage()第一个探测点 {p1} 的颜色: {color1} (in_game判定要求(187,85,85), in_game_dead要求(255,255,255))")
    print(f"  check_stage()第二个探测点(仅第一个不匹配时才看) {p2} 的颜色: {color2} (in_menu判定要求(155,181,107))")

    img = cv2.cvtColor(np.array(full), cv2.COLOR_RGB2BGR)

    def mark(pos, color, label, half_w=None, half_h=None):
        x, y = int(pos[0]), int(pos[1])
        cv2.drawMarker(img, (x, y), color, markerType=cv2.MARKER_CROSS, markerSize=16, thickness=2)
        cv2.putText(img, label, (x + 10, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        if half_w is not None:
            from utils import scale_x, scale_y
            hw = max(1, scale_x(half_w))
            hh = max(1, scale_y(half_h))
            cv2.rectangle(img, (x - hw, y - hh), (x + hw, y + hh), color, 1)

    mark(_START_BUTTON_POS, (0, 255, 0), "START", half_w=15, half_h=10)
    mark(_CONTINUE_BUTTON_POS, (0, 165, 255), "CONTINUE", half_w=_DEATH_SCREEN_SAMPLE_HALF_W, half_h=_DEATH_SCREEN_SAMPLE_HALF_H)
    mark(p1, (0, 0, 255), "check_stage#1")
    mark(p2, (255, 0, 255), "check_stage#2")

    cv2.imwrite("./debug_stage_diag_marked.png", img)
    print("\n✅ 已保存 debug_stage_diag_marked.png —— 绿框=开始按钮采样区, 橙框=继续按钮采样区, 红/品红十字=check_stage探测点. 发这张图 + 上面打印的内容过来.")


if __name__ == "__main__":
    main()
