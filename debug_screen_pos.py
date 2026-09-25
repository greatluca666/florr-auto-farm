"""倒计时结束时截一张全屏图, 在鼠标当前位置画红十字标出来, 存成
debug_marked_pos.png —— 用来精确量真实UI元素(按钮/下拉框)的坐标.

之前那版是终端里实时刷坐标数字, 靠人读出来再打字发过来 —— 来回传错好几次
(终端转义符混进去、多个点粘一起分不清、量的点跟实际想量的对不上), 而且数字
本身看不出跟目标像素对得准不准。这版直接给一张标了红十字的截图, 准不准一眼
看得出来, 不用再靠文字描述.

用法: 把鼠标移到目标位置(比如下拉框某一行), 倒计时结束前别再移动. 结束后
把debug_marked_pos.png发过来就行, 想量下一个点就重跑一次(会覆盖上一张).
"""
import time
import pyautogui
from PIL import ImageDraw


def main():
    print("🖱️  屏幕坐标标记工具")
    print("\n⏳ 5秒后截图, 这段时间把鼠标移到目标位置停住别再动...\n")
    for i in range(5, 0, -1):
        print(f"   {i}...")
        time.sleep(1)

    x, y = pyautogui.position()
    print(f"\n📍 捕获坐标: ({x}, {y})")

    img = pyautogui.screenshot()
    draw = ImageDraw.Draw(img)
    radius = 8
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline="red", width=3)
    draw.line((x - 15, y, x + 15, y), fill="red", width=2)
    draw.line((x, y - 15, x, y + 15), fill="red", width=2)
    img.save("./debug_marked_pos.png")
    print("💾 已保存 debug_marked_pos.png (红十字标出捕获点, 一眼看准不准)")


if __name__ == "__main__":
    main()
