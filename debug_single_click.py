"""最小化隔离测试: 单独测"点这一个坐标到底有没有生效", 不掺杂Esc/开下拉框
那些步骤 —— switch_server()换了3版点击方式/坐标都没能让"服务器"框的值变化,
需要把"点击本身有没有传进游戏"这一件事单独摘出来测, 排除掉其它步骤的干扰.

用法: 你自己手动先把设置面板打开、服务器下拉展开(眼睛能看见Juliett那一行),
然后跑这个脚本. 倒计时结束后它只做一件事: 在目标坐标点一下(默认(233, 305),
也可以`python debug_single_click.py X Y`传别的坐标进来测), 点前点后各截一张
图存下来. 加`--double`参数就点两下(跟click_continue_after_death()同款间隔:
moveTo→sleep(0.2)→click→sleep(0.1)→click), 测"第一下只是激活、第二下才真正
生效"这个假设.
"""
import sys
import time
import pyautogui


def main():
    args = [a for a in sys.argv[1:] if a != "--double"]
    double = "--double" in sys.argv[1:]
    x, y = (233, 305)
    if len(args) == 2:
        x, y = int(args[0]), int(args[1])

    print("🔍 单点点击隔离测试" + ("(双击模式)" if double else "(单击模式)"))
    print(f"   目标坐标: ({x}, {y})")
    print("\n⏳ 5秒后点击, 这段时间自己把设置面板+服务器下拉打开...\n")
    for i in range(5, 0, -1):
        print(f"   {i}...")
        time.sleep(1)

    before_img = pyautogui.screenshot()
    before_img.save("./debug_click_before.png")
    print(f"📸 点击前截图已保存: debug_click_before.png (鼠标当前 {pyautogui.position()})")

    pyautogui.moveTo(x, y)
    time.sleep(0.2)
    print(f"🖱️  moveTo后鼠标实际位置: {pyautogui.position()} (应该等于目标坐标)")
    pyautogui.click()
    if double:
        time.sleep(0.1)
        pyautogui.click()
        print("🖱️  已点第二下")
    time.sleep(0.5)

    after_img = pyautogui.screenshot()
    after_img.save("./debug_click_after.png")
    print("📸 点击后截图已保存: debug_click_after.png")
    print("\n对比两张图, 看'服务器'框显示的值变没变.")


if __name__ == "__main__":
    main()
