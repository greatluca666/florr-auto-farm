"""隔离测试: 死亡结算画面"继续"按钮到底点不点得动.

跟main.py整套循环逻辑完全无关, 只测一件事: pyautogui在_CONTINUE_BUTTON_POS
这个坐标点两下, 画面到底有没有反应. 点前点后各截一张全屏图 + 打印按钮区域
的绿色占比(跟on_death_screen()用的是同一套判定), 方便直接对比.

用法: 死在游戏里, 停在死亡结算画面上(main.py先别跑, 或者停掉), 切到florr.io
标签页确保它在最前面, 然后跑这个脚本. 5秒倒计时期间去切换窗口.
"""
import time
import pyautogui
from utils import (
    _CONTINUE_BUTTON_POS,
    _green_button_ratio,
    on_death_screen,
    on_start_screen,
)


def main():
    print("🔍 死亡画面点击隔离测试")
    print(f"   屏幕尺寸: {pyautogui.size()}")
    print(f"   目标坐标: {_CONTINUE_BUTTON_POS}")
    print("\n⏳ 5秒后开始, 这段时间切到florr.io标签页, 确保停在死亡结算画面上...\n")
    for i in range(5, 0, -1):
        print(f"   {i}...")
        time.sleep(1)

    print(f"\n📸 点击前: 鼠标当前位置 {pyautogui.position()}")
    before_ratio = _green_button_ratio(_CONTINUE_BUTTON_POS)
    print(f"   按钮区域绿色占比: {before_ratio:.4f} (on_death_screen()判定阈值0.02)")
    print(f"   on_death_screen()={on_death_screen()}  on_start_screen()={on_start_screen()}")

    before_img = pyautogui.screenshot()
    before_img.save("./debug_before_click.png")
    print("   已保存 debug_before_click.png\n")

    print("🖱️  执行点击: moveTo -> sleep(0.2) -> click -> sleep(0.1) -> click")
    pyautogui.moveTo(_CONTINUE_BUTTON_POS)
    time.sleep(0.2)
    pyautogui.click()
    time.sleep(0.1)
    pyautogui.click()

    print(f"\n📸 点击后: 鼠标实际停在 {pyautogui.position()} (应该等于目标坐标, 不等就是moveTo没生效)")
    time.sleep(1)  # 给画面转场留点时间
    after_ratio = _green_button_ratio(_CONTINUE_BUTTON_POS)
    print(f"   同一位置绿色占比: {after_ratio:.4f}")
    print(f"   on_death_screen()={on_death_screen()}  on_start_screen()={on_start_screen()}")

    after_img = pyautogui.screenshot()
    after_img.save("./debug_after_click.png")
    print("   已保存 debug_after_click.png\n")

    print("=" * 60)
    if before_ratio > 0.02 and after_ratio <= 0.02:
        print("✅ 点击前判定为死亡画面, 点击后不再判定为死亡画面 —— 点击生效了")
    elif before_ratio > 0.02 and after_ratio > 0.02:
        print("❌ 点击前后死亡画面判定没变化 —— 点击没让画面发生转场")
    else:
        print("⚠️  点击前就没判定为死亡画面(before_ratio<=0.02) —— 先确认倒计时期间")
        print("    真的停在死亡结算画面上, 不是别的画面, 重跑一次")
    print("对比 debug_before_click.png 和 debug_after_click.png 这两张图, 一起发我")
    print("=" * 60)


if __name__ == "__main__":
    main()
