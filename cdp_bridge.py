"""通过Chrome DevTools Protocol(CDP)直接在florr.io标签页里跑JS —— 不用装
Tampermonkey之类的浏览器扩展, 也不用靠pyautogui点游戏画布上的像素(那条路在
换服务器这个功能上反复卡住, 见switch_server()的历史).

前提: Chrome得用三个参数一起启动(实机踩过坑确认的, 少一个都不行, 默认全部
不开是出于安全考虑). 正常双击图标打开的Chrome不满足这些条件, 得完全退出
Chrome后从命令行重新起:

    macOS:
        osascript -e 'quit app "Google Chrome"'   # 先完全退出, 不然新参数不生效
                                                     # (复用已有进程, 忽略--args)
        open -a "Google Chrome" --args --remote-debugging-port=9222 \
            "--remote-allow-origins=*" --user-data-dir="/tmp/chrome-debug-profile"
    Windows:
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
            --remote-debugging-port=9222 --remote-allow-origins=* ^
            --user-data-dir="C:\\chrome-debug-profile"

三个参数各自的坑:
  --remote-debugging-port=9222   光加这个不够, Chrome不会真的监听这个端口 ——
                                   必须配合下面的--user-data-dir一起给(单独给会
                                   静默失败, 端口测起来是Connection refused,
                                   没有任何报错提示为什么).
  --user-data-dir=...            指向一个独立的用户目录(不是默认Profile) ——
                                   这意味着开出来的是全新的Chrome窗口, 没有你
                                   原来的标签页/登录状态, 得在这个新窗口里重新
                                   打开florr.io.
  --remote-allow-origins=*       没有这个, CDP的WebSocket握手会被403拒绝
                                   (Chrome自己的跨源保护, 报错信息里会直接把
                                   这个参数名写出来).
Shell里`*`记得加引号, 不然会被当成通配符展开(zsh下`--remote-allow-origins=*`
不加引号会报"no matches found").

这个模块只负责"找到florr.io那个标签页 + 往里面扔一段JS执行", 不掺杂具体要跑
哪段JS(那是调用方, 比如switch_server(), 该关心的事).
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import websocket

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222

_CHROME_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(sys.argv[0])), "chrome-profiles", "默认"
)
# 阶段1 是单个写死的 chrome-profile/; 阶段2 每个账号一个 chrome-profiles/<别名>/,
# 这个常量指向迁移出来的『默认』账号(命令行版 / 旧引导仍用它).
# sys.argv[0]: 打包成exe后是exe自己的路径, 脚本模式下是main.py的路径 —— 两种
# 情况都想要"跟可执行文件同级". 不用sys.executable(那是python解释器本身的
# 路径, 脚本模式下跟main.py不在同一目录, 只有frozen模式才等于exe路径, 两种
# 场景表现不一致), sys.argv[0]在两种场景下语义更一致.

_WINDOWS_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


_LINUX_CHROME_NAMES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]


def _find_windows_chrome():
    """按几个常见安装路径挨个试, 都没有就返回None(调用方负责报错文案)."""
    for path in _WINDOWS_CHROME_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _find_linux_chrome():
    """按PATH找Chrome可执行文件, 都没有就返回None(同_find_windows_chrome的约定).

    Linux没有macOS那种`open -a`按应用名找的机制, 跟Windows一样得自己定位可执行
    文件 —— 只是发行版装出来的名字不统一(.deb装的是google-chrome / apt装的是
    chromium), 所以走shutil.which挨个试而不是写死绝对路径.
    """
    for name in _LINUX_CHROME_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _linux_extra_flags():
    """无头服务器(Xvfb)上跑Chrome必须补的flag.

    --disable-gpu        Xvfb里没有GPU. florr.io是Canvas 2D(见canvas_hook.js打
                         的是CanvasRenderingContext2D.prototype), 纯CPU软光栅就
                         能画, 不给这个flag只会白等一轮GPU进程起不来的重试.
    --window-size        Xvfb默认不带窗口管理器, 窗口尺寸完全不确定. utils.py
                         里所有坐标常量都是照1920x1080量的(_REF_WIDTH), 尺寸对不
                         上时scale_point()会把点击全推偏, 所以必须写死.
    --no-sandbox         只在root下给. root跑Chrome时沙箱起不来会直接退出; 非
                         root时不该给 —— 无脑常开等于白白关掉渲染器沙箱.
    """
    flags = ["--disable-gpu", "--window-size=1920,1080"]
    if getattr(os, "geteuid", lambda: -1)() == 0:
        flags.append("--no-sandbox")
    return flags


def _quit_all_chrome():
    """强制退出所有Chrome进程 —— 专用CDP实例要求Chrome完全重启后新参数才生效
    (见模块文档). 本来就没在跑不算失败, 静默吞掉非零退出码."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", "chrome.exe", "/F"], capture_output=True)
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'quit app "Google Chrome"'], capture_output=True)
    elif sys.platform.startswith("linux"):
        # 只按进程名配, 绝不加-f: -f拿整条命令行去配, 而`python main.py
        # --launch-chrome`这条命令行里就带着"chrome"这个词 —— 加了-f第一件事
        # 就是把我们自己杀掉.
        subprocess.run(["pkill", "^(chrome|chromium)$"], capture_output=True)
    time.sleep(1)  # 给进程真正退出、释放profile锁一点时间, 避免新实例抢锁失败


def _launch_chrome_process():
    """带三个CDP参数 + 持久独立profile拉起一个全新空白Chrome窗口. 找不到Chrome
    可执行文件(仅Windows需要按路径找; macOS靠`open -a`按应用名找, 找不到时
    `open`自己会报错, 不用额外检测)时抛RuntimeError, 带清楚的安装引导."""
    args = [
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={_CHROME_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if sys.platform == "win32":
        chrome_path = _find_windows_chrome()
        if chrome_path is None:
            raise RuntimeError("没找到Chrome, 请先安装: https://www.google.com/chrome/")
        subprocess.Popen([chrome_path] + args)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Google Chrome", "--args"] + args)
    elif sys.platform.startswith("linux"):
        chrome_path = _find_linux_chrome()
        if chrome_path is None:
            raise RuntimeError(
                "没找到Chrome, 请先安装: sudo apt install ./google-chrome-stable_current_amd64.deb")
        subprocess.Popen([chrome_path] + args + _linux_extra_flags())
    else:
        raise RuntimeError(f"不支持的平台: {sys.platform}")


def _poll_for_florr_tab(timeout, interval=1):
    """每隔interval秒查一次find_florr_tab(), 直到找到或超时(超时返回None,
    不抛异常 —— 调用方决定要不要重试)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tab = find_florr_tab()
        if tab is not None:
            return tab
        time.sleep(interval)
    return None


def is_cdp_port_reachable():
    """轻量探一下CDP端口是不是真的在监听(不关心florr.io标签页在不在) ——
    区分"Chrome压根没启动成功"和"Chrome好好的, 就是florr.io还没打开"这两种
    超时原因, 跟find_florr_tab()同一套try/except模式, 只是不解析标签页列表.

    公有: 命令行版(launch_dedicated_chrome)和GUI版(gui_chrome_flow)都要靠它
    区分这两种超时原因, 给出的重试提示才不会指错方向."""
    try:
        urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json", timeout=3)
        return True
    except (urllib.error.URLError, ConnectionRefusedError, OSError):
        return False


def is_dedicated_chrome_ready():
    """端到端探一下"专用Chrome已经就绪": CDP端口通 + florr.io标签页开着 +
    WebSocket握手没被403拒(--remote-allow-origins=* 确实给了). 三条全过才算就绪.

    任何异常都吞掉返回False —— 这只是个"能不能省掉重启"的乐观探测, 探不通就
    走完整的杀掉重启流程, 不该由它决定调用方能不能继续.
    """
    try:
        eval_js("1", timeout=3)
        return True
    except Exception:
        return False


def launch_chrome_for_profile(profile_dir, *, open_url="https://florr.io", fullscreen=False):
    """杀掉所有 Chrome, 用指定 profile 目录拉起专用实例, 顺带打开 open_url.
    调度器换账号时用(fullscreen=True 让 florr.io canvas 直接铺满); 登录引导用
    (fullscreen=False, 普通窗口方便登录). 找不到 chrome.exe(仅 Windows 需按路径
    找)时抛 RuntimeError。"""
    _quit_all_chrome()
    args = [
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # _quit_all_chrome() 用 taskkill /F 硬杀, 下次启动 Chrome 会当成上次崩溃
        # 弹"是否恢复页面"横幅 —— 这两个 flag 把它压掉(前者是新版专用, 后者兼容旧版).
        "--hide-crash-restore-bubble",
        "--disable-session-crashed-bubble",
    ]
    if fullscreen:
        args.append("--start-fullscreen")
    if sys.platform.startswith("linux"):
        # 必须插在 open_url 前面 —— Chrome 把第一个非 flag 参数当要打开的 URL,
        # flag 排到它后面会被当成第二个 URL, 静默失效.
        args += _linux_extra_flags()
    args.append(open_url)
    if sys.platform == "win32":
        chrome_path = _find_windows_chrome()
        if chrome_path is None:
            raise RuntimeError("没找到 Chrome, 请先安装: https://www.google.com/chrome/")
        subprocess.Popen([chrome_path] + args)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Google Chrome", "--args"] + args)
    elif sys.platform.startswith("linux"):
        chrome_path = _find_linux_chrome()
        if chrome_path is None:
            raise RuntimeError(
                "没找到 Chrome, 请先安装: sudo apt install ./google-chrome-stable_current_amd64.deb")
        subprocess.Popen([chrome_path] + args)
    else:
        raise RuntimeError(f"不支持的平台: {sys.platform}")


def quit_and_launch_chrome():
    """非交互版: 杀掉所有Chrome + 带三个CDP参数拉起专用实例(『默认』profile, 不开
    florr.io, 不全屏). 命令行版 launch_dedicated_chrome() 用它. GUI 调度器换账号
    走 launch_chrome_for_profile()(带 open_url + fullscreen), 不走这条."""
    _quit_all_chrome()
    _launch_chrome_process()


def wait_for_florr_tab(timeout, interval=1.0):
    """轮询find_florr_tab()直到找到florr.io标签页或超时. 超时返回None, 不抛 ——
    调用方(GUI)决定要不要提示重试."""
    return _poll_for_florr_tab(timeout, interval)


def launch_dedicated_chrome():
    """整条"准备专用Chrome"引导链路, main.py启动时调用一次. 面向不懂命令行的
    用户, 全程只需要回车 —— 没有任何一步要求手动敲参数.

    已经有一个就绪的专用Chrome在跑就直接返回, 不碰它 —— main.py上次跑崩了重开、
    或者同时开两份的时候, 无条件杀掉重启会让用户白迁移一次账号(专用profile是
    独立目录, 每次新建都是空的登录状态)."""
    if is_dedicated_chrome_ready():
        print("✅ 检测到专用Chrome已经就绪(CDP可用 + florr.io标签页开着), 跳过重启.")
        return

    input(
        "⚠️ 即将关闭所有Chrome窗口以启动专用实例(未保存的标签页/内容会丢失).\n"
        "   按回车继续, Ctrl+C取消: "
    )
    quit_and_launch_chrome()

    while True:
        input(
            "\n🌐 专用Chrome已启动. 请在这个新窗口里把你的florr账号迁移过来,"
            "\n   迁移完成后打开florr.io, 回到这里按回车继续: "
        )
        if wait_for_florr_tab(15) is not None:
            return
        if is_cdp_port_reachable():
            print("   还没检测到florr.io标签页, 确认已经在那个新Chrome窗口里打开florr.io了? 重试一次.")
        else:
            print("   专用Chrome好像没有正常启动(CDP端口连不上), 请检查Chrome是不是还开着.")


def find_florr_tab():
    """在CDP暴露的标签页列表里找florr.io那个, 返回它的调试信息字典(含
    webSocketDebuggerUrl). 找不到(Chrome没开调试端口/florr.io标签页没开着)
    返回None, 不抛异常 —— 让调用方自己决定报错文案, 别在这里印一堆没上下文
    的traceback."""
    try:
        resp = urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json", timeout=3)
    except (urllib.error.URLError, ConnectionRefusedError, OSError):
        return None
    tabs = json.loads(resp.read())
    for tab in tabs:
        if "florr.io" in tab.get("url", ""):
            return tab
    return None


def _send_cdp_command(method, params=None, timeout=5):
    """开一条WebSocket连接, 发一条CDP命令, 等到id对上号的响应, 关连接, 返回
    结果. eval_js()/capture_screenshot()共用这一套收发逻辑, 不重复写.

    找不到标签页时抛RuntimeError, 带清楚的中文原因, 不静默失败 —— 调用方
    (switch_server()等)必须知道到底有没有真的执行成功."""
    tab = find_florr_tab()
    if tab is None:
        raise RuntimeError(
            "找不到florr.io标签页(CDP). 确认: 1) Chrome是不是用本模块文档里"
            "那三个参数(--remote-debugging-port=9222 + --remote-allow-origins=* "
            "+ --user-data-dir=...)一起启动的(正常双击图标打开的不算, 少一个"
            "参数也不行); 2) florr.io标签页是不是在这个新窗口里打开的."
        )
    ws_url = tab["webSocketDebuggerUrl"]
    if not hasattr(websocket, "create_connection"):
        # PyPI上"websocket"和"websocket-client"是两个不相关的包, 都导入成
        # `websocket`这个模块名, 装错/装重会互相覆盖 —— 实机在Windows上复现过
        # (报错是"module 'websocket' has no attribute 'create_connection'",
        # 不容易联想到"装错包了", 这里把原因和修法直接说清楚). 放在实际调用这里
        # 检查, 不放在模块导入时 —— 导入时就抛的话main.py会直接启动不了, 换服务
        # 器这个附加功能坏了不该连累寻路/刷怪这些正常功能, 让这条错误照样走
        # switch_server()调用方那边已有的try/except, 不阻塞主循环.
        raise RuntimeError(
            "装的是错的'websocket'包 —— PyPI上有两个不同的包都叫这个模块名, "
            "这个项目要的是'websocket-client', 不是单独的'websocket'. 修法:\n"
            "    pip uninstall -y websocket websocket-client\n"
            "    pip install websocket-client"
        )
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        request_id = 1
        ws.send(json.dumps({
            "id": request_id,
            "method": method,
            "params": params or {},
        }))
        # CDP这条连接上可能同时有别的事件消息(跟我们这次调用无关的通知)混进来,
        # 不能假设recv()第一条就是我们要的响应 —— 按id对上号才是我们的.
        while True:
            result = json.loads(ws.recv())
            if result.get("id") == request_id:
                break
    finally:
        ws.close()
    if "error" in result:
        raise RuntimeError(f"CDP执行出错: {result['error']}")
    return result


def eval_js(expression, timeout=5):
    """在florr.io标签页里执行一段JS表达式, 返回CDP Runtime.evaluate的原始
    返回字典."""
    return _send_cdp_command(
        "Runtime.evaluate", {"expression": expression}, timeout=timeout)


def capture_screenshot(timeout=5):
    """截florr.io标签页当前内容, 返回PNG原始字节. 走CDP, 不是pyautogui —— 不
    依赖那个标签页是不是在前台/有没有窗口焦点(main.py跑着的时候用户很可能在看
    别的窗口, pyautogui.screenshot()这时候截到的是别的东西, 不是游戏画面)."""
    result = _send_cdp_command(
        "Page.captureScreenshot", {"format": "png"}, timeout=timeout)
    return base64.b64decode(result["result"]["data"])


# 未打包: 跟本模块同目录. PyInstaller 打包后: main.spec 把它作为 datas 塞进
# _internal/ (onedir 下就是 sys._MEIPASS), 冻结模块的 __file__ 不一定指到真实
# 路径, 所以 frozen 时改从 _MEIPASS 取.
_CANVAS_HOOK_PATH = (
    Path(sys._MEIPASS) / "canvas_hook.js"
    if getattr(sys, "frozen", False)
    else Path(__file__).with_name("canvas_hook.js")
)


def _eval_value(expression, timeout=5):
    """Runtime.evaluate + returnByValue, 拆成里面那个原始值/JSON 值. 拿不到 → None.

    Runtime.evaluate 的响应是双层 result: {"result": {"result": {"value": ...}}}
    —— returnByValue=True 时最里那层 value 才是真值(以前 scroll_wheel 少剥一层
    直接 KeyError). 这个 helper 把这层拆包收在一处, drain/inject 都走它."""
    resp = _send_cdp_command(
        "Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout=timeout)
    return resp.get("result", {}).get("result", {}).get("value")


def drain_canvas_log(timeout=5):
    """读空 window.__canvasLog(canvas_hook.js 往里塞每帧的绘制记录), 返回记录列表.
    一次 Runtime.evaluate 里读 + 清, 中间不会漏帧. 拿不到 → []."""
    v = _eval_value(
        "(()=>{const l=window.__canvasLog||[];window.__canvasLog=[];return l;})()", timeout)
    return v if isinstance(v, list) else []


_RELOAD_MIN_INTERVAL = 30.0   # Page.reload 两次之间的最短间隔(秒)
_last_reload_ts = 0.0         # 上一次真的发出 Page.reload 的时刻(time.time())


def _maybe_reload(timeout=5):
    """限流版 Page.reload. inject_canvas_hook 的两条失败分支都要 reload 页面, 而
    enemy_detect.scan_enemies 每 ~0.12s 就调一次 inject_canvas_hook —— 坏状态不
    限流的话, 用户的游戏会被每秒 reload 八次, 永远回不到能玩的状态. 距上次真的
    reload 不到 _RELOAD_MIN_INTERVAL 秒就跳过. 真发了返回 True, 被限流跳过返回
    False(调用方无论哪种都照样抛 RuntimeError, 这一 tick 退化成 [])."""
    global _last_reload_ts
    if time.time() - _last_reload_ts < _RELOAD_MIN_INTERVAL:
        return False
    _send_cdp_command("Page.reload", {}, timeout=timeout)
    _last_reload_ts = time.time()
    return True


def inject_canvas_hook(timeout=5):
    """把 canvas_hook.js 注进 florr.io 标签页(patch CanvasRenderingContext2D 记录绘制调用).
    幂等: 同版本已装 → 直接返回. 移植自 florragent 的 _inject_canvas_hook, 换成裸 CDP.

    只走一条注入路径: Runtime.evaluate 里跑 hook 源码, 顺带盖上版本戳
    window.__canvasHookInstalledVersion. 不用 Page.addScriptToEvaluateOnNewDocument
    —— _send_cdp_command 每次开一条全新 WebSocket、发完就关, 用它注册的脚本绑在那条
    转瞬即逝的 DevTools 会话上, 活不到下次页面加载. reload 恢复靠的是 scan_enemies
    每 tick 都调本函数: florr 自己 reload 后 window.__canvasHookInstalled 没了 → 下一
    tick 走全新注入路径经 Runtime.evaluate 重注.

    版本指纹(sha256[:16]): 页面上装的是别的版本 → reload + 抛 RuntimeError
    (patchProto 的 per-prototype guard 不能热替).
    注完 drain 一次 + sleep(0.5) + 再 drain: 排出的记录里 frame 号不足 2 个不同值
    → patch 没落地, 或 florr 在 patch 落地前就绑死了 ctx 方法/自己那份
    requestAnimationFrame 引用(__canvasFrame 推不动, 每条记录都是 frame 0, 光看
    "非空"检测不出来)→ reload + 抛 RuntimeError.
    两处 reload 都过 _maybe_reload 限流(本函数每秒被调 ~8 次). 抛 RuntimeError 时
    调用方(enemy_detect.scan_enemies)当"本次没检测到"退化成 wander, 下次再重试(幂等)."""
    src = _CANVAS_HOOK_PATH.read_text(encoding="utf-8")
    version = hashlib.sha256(src.encode()).hexdigest()[:16]
    installed = _eval_value("!!window.__canvasHookInstalled", timeout)
    installed_ver = _eval_value("window.__canvasHookInstalledVersion || null", timeout)
    if installed and installed_ver == version:
        return                                     # 同版本已装 → 无操作
    if installed and installed_ver is not None:     # 真·版本不一致(页面上是别的版本)
        _maybe_reload(timeout)
        raise RuntimeError("canvas hook 版本不一致(页面上是旧版) —— 已 reload, 请重进游戏后重试")
    # 没装, 或装了但没盖版本戳(理论上到不了 —— 只有本函数会设 __canvasHookInstalled)
    # → 都走全新注入路径.
    _eval_value(f"window.__canvasHookInstalledVersion = {version!r};\n" + src, timeout)
    drain_canvas_log(timeout)                       # discard the injection's own output
    time.sleep(0.5)
    recs = drain_canvas_log(timeout)
    if len({r.get("frame") for r in recs}) < 2:
        _maybe_reload(timeout)
        raise RuntimeError("canvas hook 注入没生效 (帧号没推进) —— 已 reload, 请重进游戏后重试")
