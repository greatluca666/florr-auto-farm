"""隔离测试: switch_server()到底管不管用.

跟main.py整套循环逻辑完全无关, 只测一件事: 查一次官方实时服务器列表
(server_lookup.py)、挑一个、通过CDP执行cp6.forceServerID(...)这个动作, 在
真实游戏里到底会发生什么 —— 有没有真的触发重连、重连后卡在哪个画面(开局菜单/
直接进局/别的中间状态)。上main.py无人值守主循环前务必先跑一遍这个亲眼确认,
别让没验证过的新动作直接进自动重试逻辑(比照这项目里toggle_map那次误触发的
教训 —— 没验证的假设很容易在实机上跟预期不一样, 而且不容易从日志倒推出哪
一步错了).

用法: Chrome得用cdp_bridge.py模块文档里那三个参数启动, florr.io标签页开着.
不需要切到最前台 —— 前后两张截图走CDP的Page.captureScreenshot, 截的是标签页
真实内容, 不依赖窗口焦点(main.py实际跑起来时用户很可能在看别的窗口, 这也是
为什么不用pyautogui.screenshot()).
"""
import time
import pyautogui
import cdp_bridge
from utils import (
    apply_map,
    switch_server,
    on_death_screen,
    on_start_screen,
    get_player_position,
)


def main():
    apply_map("desert")  # MAP全局变量不设就是空字符串, get_player_position()内部
                          # 拼路径会变成'./maps/.png', 读不到文件.
    print("🔍 换服务器隔离测试")
    print(f"   屏幕尺寸: {pyautogui.size()}")
    print("\n⏳ 5秒后开始, 这段时间切到florr.io标签页...\n")
    for i in range(5, 0, -1):
        print(f"   {i}...")
        time.sleep(1)

    print(f"\n📸 切换前: 鼠标当前位置 {pyautogui.position()}")
    before_pos = get_player_position()
    print(f"   切换前玩家位置(小地图坐标): {before_pos}")
    with open("./debug_before_switch.png", "wb") as f:
        f.write(cdp_bridge.capture_screenshot())
    print("   已保存 debug_before_switch.png (CDP截图, 不依赖窗口焦点)\n")

    print("🌐 执行 switch_server()...")
    server_id = switch_server()
    print(f"   切换到服务器码: {server_id}\n")

    time.sleep(2)  # 给画面转场留点时间
    print(f"📸 切换后: 鼠标停在 {pyautogui.position()}")
    after_pos = get_player_position()
    print(f"   切换后玩家位置(小地图坐标): {after_pos}")
    print(f"   on_death_screen()={on_death_screen()}  on_start_screen()={on_start_screen()}")

    with open("./debug_after_switch.png", "wb") as f:
        f.write(cdp_bridge.capture_screenshot())
    print("   已保存 debug_after_switch.png (CDP截图, 不依赖窗口焦点)\n")

    print("=" * 60)
    print("对比 debug_before_switch.png 和 debug_after_switch.png 这两张图, 确认:")
    print("  1. 画面确实变了(出现'连接中...../登录中...'的重连画面, 或者重新")
    print("     进了一局/回到开局菜单) —— 没变说明forceServerID没真的执行")
    print("  2. on_start_screen()或on_death_screen()有没有命中, 命中了main.py现有")
    print("     逻辑就能接得住; 都没命中但玩家位置也测不到, 说明卡在了某个中间")
    print("     状态, 需要另外处理")
    print("一起把这两张图和上面几行日志发我")
    print("=" * 60)


if __name__ == "__main__":
    main()
