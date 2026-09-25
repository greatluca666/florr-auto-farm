"""florr-auto-afk协同 —— 监听它写的latest.log, 检测到"发现AFK弹窗"事件后,
让本项目的寻路循环暂停一段时间, 别跟它的YOLO拖拽方案抢鼠标.

florr-auto-afk只在检测到弹窗那一刻写一条会落盘的日志(`log_ret`默认save=True):
    ... EVENT: Found AFK window
它清场后的"No AFK window found"硬编码save=False, 不管verbose开不开都不落盘,
没法拿来当"解除暂停"信号用 —— 这里只能是触发器, 不是起止对: 看到触发行就暂停
固定时长, 时间到自动恢复, 不去猜它到底解完没解完. 详见
docs/superpowers/specs/2026-08-11-afk-check-coexistence-design.md.

2026-09-20 用户反馈+贴出的 latest.log 加了一层refinement: "Found AFK window"
本身也会误报(YOLO认错游戏画面上的形状), 而且florr-auto-afk自己在误报后
4~5秒内会接着写 `ERROR: No masks found`(承认这次没能真正分割出弹窗内容,
这条也落盘) —— 见 poll_afk_pause() 的两段式暂停: 先按 PROVISIONAL_PAUSE_
SECONDS 暂停, 等到这条失败标记就不再往上补, 等不到才当真弹窗补足到
PAUSE_SECONDS. 跟上面那段"没有起止对"的结论不矛盾 —— 用的是不同的信号
(失败标记, 不是"No AFK window found"清场信号), 解决的是不同的问题(误报
拖慢寻路, 不是"提前解除暂停").

florr-auto-afk本身是完全独立的另一个程序(不是这个repo的一部分), 用户得自己
有一份能跑. ensure_florr_auto_afk_running()负责在Windows上自动确保它在跑
(已经在跑就不动, 没装就问要不要下, 装了没跑就静默打开并确认它真的开始检测);
LATEST_LOG_PATH跟着它实际的安装位置算出来, 不再是写死的个人路径. 详见
docs/superpowers/specs/2026-08-27-afk-auto-bootstrap-design.md.

"不用手点run"这件事官方原版做不到 —— _DOWNLOAD_URL钉的是我们自己那份打过补丁的
fork: greatluca666/florr-auto-afk(GPL-3.0, 从上游Shiny-Ladybug/florr-auto-afk
的v1.1.1开出来, 只加了一个runs.autoStart开关 —— GUI起来时读到它就自己调
toggle_segment_process()并把窗口最小化). 官方release没有这个键, 把_DOWNLOAD_URL
改回上游的话每次启动都会白等满_START_TIMEOUT_SECONDS秒, 然后回落到"请手动点run"
那条提示. 详见docs/superpowers/specs/2026-08-28-afk-autostart-design.md.
"""
import copy
import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
import zipfile

import certifi

# 这个目录名是我们自己挑的, 不用跟谁对齐: release zip里没有顶层目录(它的workflow
# 用`Compress-Archive -Path ./dist/segment/*`打的包), 所以_download_and_extract()
# 是解压到这个目录里, 而不是解压到它旁边 —— 别看着名字像"沿用发行包的目录名"就把
# extractall()改回_INSTALL_ROOT, 那会把4500个文件直接铺在main.py旁边.
#
# 末尾的-autostart是我们自己加的后缀(release tag和zip名里都没有这个词, 见下面
# _DOWNLOAD_URL): 已经装过旧版官方包的机器上_EXE_PATH是存在的, 不换目录名就永远
# 不会重新下载, 而那份exe没有autoStart, 每次启动都白等到超时.
_INSTALL_DIR_NAME = "florr-auto-afk-v1.1.1-autostart"
# 实测过release zip内部结构确认的真实可执行文件名 —— 不是"florr-auto-afk.exe"
# 这种直觉猜测的名字.
_EXE_NAME = "segment.exe"
# 我们那份fork的Actions产物(见模块docstring). tag(v1.1.1)和zip名跟官方原件一模
# 一样, 是故意的: 这俩都是从fork里constants.py的VERSION_INFO推出来的, 而同一个
# 常量还要喂给它自己的更新检查 —— parse_version()是
# `tuple(map(int, version.split('.')))`(segment_utils.py:171-175), 写成
# "1.1.1-autostart"那边直接ValueError. 所以区分官方原件靠的是URL里的账号名,
# 不是文件名.
_DOWNLOAD_URL = (
    "https://github.com/greatluca666/florr-auto-afk/releases/download/"
    "v1.1.1/florr-auto-afk-v1.1.1-auto.zip"
)
# 下完先核对再解压: 这是347MB的一包exe, 解压完我们直接Popen它. release asset能被
# 持有者用同一个tag覆盖掉(gh release upload --clobber), 不校验就等于无条件执行一个
# 远端随时能换内容的二进制. 重新构建/换release时这个值必须跟着更新, 否则所有人都
# 会卡在"下载校验失败".
_DOWNLOAD_SHA256 = "74488ef58966d123ace6d19ebb11c05d7ac8ee992abd949289714a8a866e7d74"

# 跟cdp_bridge.py的_CHROME_PROFILE_DIR同一个套路: 打包成exe后是exe自己所在
# 目录, 脚本模式下是main.py所在目录 —— 两种场景下"跟可执行文件同级"语义一致,
# 不用sys.executable(脚本模式下那是python解释器路径, 跟main.py不在同一目录).
_INSTALL_ROOT = os.path.dirname(os.path.abspath(sys.argv[0]))
_INSTALL_DIR = os.path.join(_INSTALL_ROOT, _INSTALL_DIR_NAME)
_EXE_PATH = os.path.join(_INSTALL_DIR, _EXE_NAME)

# florr-auto-afk.exe(现在自动下载安装到_INSTALL_DIR了)双击时CWD是它自己所在
# 目录, latest.log就落在这个目录下.
LATEST_LOG_PATH = os.path.join(_INSTALL_DIR, "latest.log")
# 覆盖YOLO检测+分割+拖拽执行的时间; 若在florr-auto-afk配置里关掉moveAfterAFK可以调低.
PAUSE_SECONDS = 12
# "Found AFK window"之后, 等这么久看它自己会不会吐出_MISDETECT_MARKER(误检
# 信号) —— 实测(2026-09-20用户贴的latest.log)Found到No masks found大约
# 4~5秒, 6秒留了余量. 见 poll_afk_pause() 的两段式暂停设计.
PROVISIONAL_PAUSE_SECONDS = 6

_FOUND_MARKER = "EVENT: Found AFK window"
# florr-auto-afk自己承认这次"Found AFK window"没能真正分割出弹窗内容 —— 挑
# ERROR级别这条(而不是它前面两条WARNING: No start found / No end found)当
# 判据, 因为实测里它总是这个失败序列里最后落盘的一条, 出现即代表这次彻底放弃了.
_MISDETECT_MARKER = "ERROR: No masks found"

# florr-auto-afk真正进入检测状态时写的日志(log_ret的save默认True会落盘).
# 挑的是**子进程**写的那条, 不是父进程那条 —— v1.1.1点一次run会分出两层进程:
#   父: start_segment_process()在segment_process.start()之后立刻写
#       "Segment process started"(segment.py:201-202, toggle那处:234还带个句号),
#       此时子进程连YOLO都还没开始加载 —— 模型坏了它照样写这句, 拿它当"已开始
#       检测"是假阳性, 我们会打✅然后一整局都没人处理AFK弹窗.
#   子: run_segment()先加载afk-seg.pt和afk-det.pt(segment.py:158-159), 失败就
#       删掉这两个模型和models/version然后直接return, 压根不会spawn afk_thread;
#       只有加载成功才起afk_thread(), 它跑完test_environment()、再在
#       runningCountDown == -1时写"Running indefinitely"(segment.py:36-41).
# 所以这句落盘 = 模型真加载上了、环境自检跑完了、检测循环马上开始转.
# 代价是它耦合runs.runningCountDown == -1(否则那个分支写的是"Running for X
# minutes"), 所以那个键也进了_REQUIRED_CONFIG.
_STARTED_MARKER = "Running indefinitely"
# 等它起来的上限: PyInstaller解包 + 模型加载, 它自己FAQ说初始化
# 要10秒以上, 慢机器上留足余量. 上面那条marker还要再多等一个test_environment()
# (一次YOLO预测 + 一次duration=1的pyautogui.moveTo, 只有首次运行会跑 —— 它跑完
# 会把advanced.environment写成true), 所以余量比字面上看起来小一些.
# 超时只是少一句确认, 不影响主程序继续跑.
_START_TIMEOUT_SECONDS = 90

# server_lookup.py的_SSL_CONTEXT/_USER_AGENT同款写法 —— Windows上urllib默认
# 不读系统证书链, 显式传certifi的证书链才不会CERTIFICATE_VERIFY_FAILED.
# 两个都是模块私有常量, 不跨模块
# import, 沿用这个repo"平台/职责专属模块各自小段重复"的既有约定.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_USER_AGENT = "florr-auto-pathing (github.com/greatluca666/florr-auto-farm)"

_last_offset = 0
_pause_until = 0.0
# 上一条还没确认是真是假的"Found AFK window"的时间戳; None = 没有待确认的.
_pending_since = None
# 还没做过第一次真实读取 —— 用来区分"模块刚加载, 从没poll过"和"poll过, offset
# 合法为0"这两种情况, 好让下面的截断重置逻辑(size < _last_offset)只在后一种
# 情况下生效. 参见_read_new_lines()里的用法.
_initialized = False
# 日志读取失败(路径没配对/文件不存在/是目录/权限不够)只在第一次发生时打印一次
# 警告, 别每次poll(一秒好几次)都刷屏.
_warned_unreadable = False


def _read_new_lines():
    """读取上次读到的位置之后新增的行. 文件比上次记录的offset还小(轮转/程序
    重启)就当成新文件, 从头重读.

    模块刚加载、还从没poll过的第一次调用是特例: 不从文件开头读, 直接跳到当前
    文件末尾. florr-auto-afk可能已经跑了一段时间, 从0读会把老早以前就处理完的
    "Found AFK window"历史事件当成新事件, 触发一次没必要的暂停. 代价是main.py
    启动前几毫秒内刚好写的标记可能被跳过, 比回放整段历史划得来.
    """
    global _last_offset, _initialized, _warned_unreadable
    try:
        size = os.path.getsize(LATEST_LOG_PATH)
        if not _initialized:
            _last_offset = size
            _initialized = True
        elif size < _last_offset:
            _last_offset = 0
        with open(LATEST_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(_last_offset)
            lines = f.readlines()
            _last_offset = f.tell()
            if lines and not lines[-1].endswith("\n"):
                # 最后一行是对方进程正在写、还没写完的残行(我们刚好在它两次write
                # 之间poll到了). 扔掉它, offset退回到这行开头 —— 不然下次它写完
                # 剩下的部分时, 两半永远拼不到一起, 里面的标记行就永久漏检了.
                partial = lines.pop()
                _last_offset -= len(partial.encode("utf-8"))
        return lines
    except Exception:
        if not _warned_unreadable:
            _warned_unreadable = True
            print(
                f"⚠️ 读取florr-auto-afk日志失败(LATEST_LOG_PATH配置可能有误): {LATEST_LOG_PATH}"
            )
        return []


def poll_afk_pause():
    """轮询一次. 发现新的"Found AFK window"事件就开一段暂停窗口; 返回当前是否
    还在暂停中. 日志文件不存在(florr-auto-afk还没启动, 或者LATEST_LOG_PATH没配对)
    时永远返回False, 不抛异常 —— 这个探测器绝不能把主程序带崩.

    用户实机反馈(2026-09-20): 蚁穴那一段路一直被"检测到florr-auto-afk发现AFK
    弹窗"打断, 而且贴过来的florr-auto-afk自己的日志显示"Found AFK window"后面
    紧跟着 `WARNING: No start found` / `WARNING: No end found, going for linear
    prediction` / `ERROR: No masks found` —— 它自己在4~5秒内就承认这次识别
    没能真正分割出弹窗的内容, 大概率是YOLO在游戏画面上认错了形状的误检, 不是
    真弹窗。而且这类误检还会反复触发(实测6秒内连续两次), 每次都按旧逻辑重新
    计满 PAUSE_SECONDS, 暂停窗口被不断顶上去, 寻路被拖得很久。

    改成两段式, 不再见"Found"就无条件暂停满PAUSE_SECONDS: 先按
    PROVISIONAL_PAUSE_SECONDS(封顶PAUSE_SECONDS, 别比它还长)暂停, 这段时间里
    等 _MISDETECT_MARKER 出现 —— 出现了就是误检, 不再往上补, 让这段短暂停按
    原计划到点结束; 等满这段时间还没等到失败信号, 才当作真弹窗, 补足到完整的
    PAUSE_SECONDS(从最初检测到的那一刻算起, 不是从"确认为真"这一刻算起, 保证
    实际覆盖时长不缩水)。
    """
    global _pause_until, _pending_since
    now = time.time()
    provisional = min(PROVISIONAL_PAUSE_SECONDS, PAUSE_SECONDS)
    for line in _read_new_lines():
        if _MISDETECT_MARKER in line and _pending_since is not None:
            print("✅ florr-auto-afk这次是误检(未能分割出弹窗内容), 不再延长暂停")
            _pending_since = None
        elif _FOUND_MARKER in line:
            _pending_since = now
            _pause_until = max(_pause_until, now + provisional)
            print(f"⏸️  检测到florr-auto-afk发现AFK弹窗, 先暂停{provisional}秒待确认...")

    if _pending_since is not None and now - _pending_since >= provisional:
        _pause_until = max(_pause_until, _pending_since + PAUSE_SECONDS)
        print(f"⏸️  florr-auto-afk这次没等到误检信号, 按真弹窗补足到{PAUSE_SECONDS}秒")
        _pending_since = None

    return time.time() < _pause_until


def _prompt_download_confirm():
    """问用户要不要下载florr-auto-afk. 回车/任何不是'n'的输入都算同意;
    输入n(不分大小写, 允许前后空白)算跳过."""
    answer = input(
        f"\n🤖 没检测到florr-auto-afk(AFK弹窗自动处理用). 现在下载吗?\n"
        f"   来源: {_DOWNLOAD_URL}\n"
        f"   大小: 约350MB, 解压到: {_INSTALL_DIR}\n"
        f"   (回车=下载, 输入n=跳过, 之后AFK弹窗不会自动处理): "
    )
    return answer.strip().lower() != "n"


def _download_and_extract():
    """流式下载到临时文件 → 核对SHA-256 → zipfile解压, 完了删掉临时zip. 网络失败/
    校验不过/zip损坏都不抛异常出去 —— 返回False, 让调用方
    (ensure_florr_auto_afk_running())决定怎么继续, 主程序不受影响."""
    tmp_path = os.path.join(_INSTALL_ROOT, f"{_INSTALL_DIR_NAME}.zip.download")
    try:
        req = urllib.request.Request(_DOWNLOAD_URL, headers={"User-Agent": _USER_AGENT})
        # 边下边算摘要, 不下完再回头读一遍文件: 347MB多走一趟完整磁盘IO纯属白费.
        digest = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)  # 1MB一块
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    if total:
                        print(
                            f"\r   下载中... {downloaded / 1e6:.0f}MB / {total / 1e6:.0f}MB",
                            end="",
                        )
            print()  # 结束下载进度那行, 换行

        actual_sha256 = digest.hexdigest()
        # 常量侧先normalize再比: 重新构建之后来刷_DOWNLOAD_SHA256的人是在Windows上,
        # 最可能直接粘`certutil -hashfile ... SHA256`的输出 —— 大写、还按每两字节插
        # 空格. 不normalize的话这种粘贴会报"校验失败", 而那条提示跟"asset真被人换了"
        # 长得一模一样, 白查半天. 只放宽大小写和空白(split()顺带吃掉换行/制表),
        # 校验本身一点没松.
        expected_sha256 = "".join(_DOWNLOAD_SHA256.split()).lower()
        if actual_sha256 != expected_sha256:
            # 校验不过就当下载失败, 一个文件都不解出来: 这包里是我们下一步要Popen的
            # exe, asset内容跟钉的摘要不一样时唯一安全的做法是停在这儿(finally会把
            # 临时zip删掉). 最常见的原因不是被人换包, 而是release重新构建了而
            # _DOWNLOAD_SHA256没跟着改 —— 所以两个摘要都印出来, 好一眼对比.
            print(
                "⚠️ florr-auto-afk下载校验失败(SHA-256不符, 已丢弃, 之后AFK弹窗不会自动处理):\n"
                f"   期望: {expected_sha256}\n"
                f"   实际: {actual_sha256}"
            )
            return False

        with zipfile.ZipFile(tmp_path) as zf:
            # 解压到_INSTALL_DIR而不是_INSTALL_ROOT: 官方release zip没有顶层
            # 目录(它的workflow用`Compress-Archive -Path ./dist/segment/*`),
            # 解压到ROOT会把4500个文件直接铺在main.py旁边, _EXE_PATH永远不存在.
            zf.extractall(_INSTALL_DIR)

        if not os.path.isfile(_EXE_PATH):
            # 上游换了打包布局(比如哪天套了层顶层目录)时不能假装装好了 —— 这里
            # 报出来, 比拖到Popen那步炸掉容易看懂.
            print(
                f"⚠️ florr-auto-afk解压完没找到{_EXE_NAME}(zip结构跟预期不符), "
                f"之后AFK弹窗不会自动处理: {_INSTALL_DIR}"
            )
            return False

        print(f"✅ florr-auto-afk已下载解压到 {_INSTALL_DIR}")
        return True
    except Exception as e:
        print(f"⚠️ 下载/解压florr-auto-afk失败(不影响主程序, 之后AFK弹窗不会自动处理): {e}")
        return False
    finally:
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)


# _DOWNLOAD_URL钉的那个fork发行包自带的config.json原文, 一个键都没删没改没猜
# (fork只在runs最前面多了个autoStart, 其余跟`git show v1.1.1:config.json`一致).
# 换_DOWNLOAD_URL时这份要跟着核对 —— 没有任何自动检查能抓到这俩漂移.
#
# 重建配置时必须以它为底, 不能从{}开始: 上游的get_config()就是
# `load(open("./config.json"))`(segment_utils.py:27-29), 对缺键没有任何默认值兜底,
# 全repo有几十处直接`get_config()[...]`取下标. 最狠的一处是import时就求值的默认
# 参数 —— segment_utils.py:266
# `def apply_mouse_movement(points, speed=get_config()["advanced"]["mouseSpeed"])`
# —— 少一个键segment.exe连GUI都来不及画就KeyError死掉, 用户只会看到我们打印的
# "🪟 已在后台打开florr-auto-afk", 然后被叫去点一个根本不存在的窗口里的run.
#
# autoStart在这儿是false(fork发行包的默认值, 单独跑它时行为跟官方原版一致),
# 我们要的true由_REQUIRED_CONFIG盖上去.
_DEFAULT_CONFIG = {
    "runs": {
        "autoStart": False,
        "autoTakeOverWhenIdle": True,
        "runningCountDown": -1,
        "moveAfterAFK": True,
        "idleTimeThreshold": 10,
        "idleDetInterval": 3,
        "idleDetIntervalMax": 10,
    },
    "exposure": {
        "enable": True,
        "duration": 3,
    },
    "gui": {
        "language": "en-us",
        "theme": "auto",
    },
    "advanced": {
        "showLogger": False,
        "moveMouse": True,
        "useOBS": False,
        "verbose": True,
        "epochInterval": 8,
        "optimizeQuantization": 1,
        "rdpEpsilon": 5,
        "extendLength": 30,
        "mouseSpeed": 100,
        "skipUpdate": False,
        "environment": False,
        "windowSizeTolerance": 0.1,
        "windowSizeRatio": [0.787, 1],
    },
    "executeBinary": {
        "runBeforeAFK": "",
        "runAfterAFK": "",
    },
    "yoloConfig": {
        "segModel": "./models/afk-seg.pt",
        "detModel": "./models/afk-det.pt",
    },
}

# 我们依赖的配置键 —— 只覆盖这四个, config.json里其余键(用户自己调的mouseSpeed
# 之类)原样保留. autoStart是fork里加的开关(见
# docs/superpowers/specs/2026-08-28-afk-autostart-design.md第一部分),
# autoTakeOverWhenIdle和moveAfterAFK的理由见
# docs/superpowers/specs/2026-08-11-afk-check-coexistence-design.md,
# runningCountDown的理由写在下面那几行注释里.
#
# 曾经还强制过advanced里的两个键, 都撤了, 别再加回来:
# - verbose: 加它的理由是"保证事件真的落进latest.log", 而这是错的 ——
#   log_ret("Found AFK window", ...)的save默认True, 无条件落盘; verbose只管那些
#   硬编码save=False的控制台行(2026-08-11那份design第32行早就写明了). 强制它没有
#   任何收益, 纯粹白覆盖用户自己的选择.
# - skipUpdate: 强制成True会把上游唯一的模型自修复永久关掉. run_segment
#   (segment.py:158-165)在YOLO模型加载失败时会删掉models/afk-seg.pt、afk-det.pt
#   和models/version, 就等着下次update_models()重新下回来, 而那次重下只由
#   advanced.skipUpdate一个键把门(segment.py:382-383) —— 关了它, 模型一坏就永久坏.
_REQUIRED_CONFIG = {
    "runs": {
        "autoStart": True,              # 启动即开始检测, 不用手点"run"
        "autoTakeOverWhenIdle": False,  # 我们一直在动鼠标, 它的idle门永远不触发
        "moveAfterAFK": False,          # 它解完题的WASD乱走会跟我们的移动打架
        # 两个理由都指向-1: (1) _STARTED_MARKER等的那句"Running indefinitely"只在
        # 这个值是-1时才写(否则走的是"Running for X minutes"那支, segment.py:41-46),
        # 用户自己调过它我们就永远确认不到启动, 白等满超时再叫他手点run;
        # (2) 非-1意味着它跑满X分钟就自己退出(segment.py:61 "Countdown Ends,
        # program exiting"), 长时间挂机时AFK处理会中途静默消失 —— 正好是这套东西
        # 最不能出的故障.
        "runningCountDown": -1,
    },
}


def _backup_broken_config(config_path):
    """把读不出来的config.json改名成config.json.bak, 返回一句能塞进警告里的说明.

    直接盖掉上一份.bak: 保住"最近一次读不出来的原文件"就够了, 攒一堆带时间戳的
    备份只会在人家目录里越积越多. 必须是os.replace()不能是os.rename() ——
    Windows上后者在目标已存在时直接报错.

    改名失败也只是返回一句话, 不抛 —— 调用方承诺绝不抛异常, 备份不了顶多是原文件
    这次真丢了, 不能因此连配置都不写.
    """
    try:
        os.replace(config_path, config_path + ".bak")
        return "原文件已改名保留成config.json.bak"
    except Exception as e:
        return f"原文件没能改名成config.json.bak, 内容会丢: {e}"


def _write_afk_config():
    """把我们依赖的几个键写进florr-auto-afk自己的config.json, 其它键原样保留.
    文件不存在/读不出来就以_DEFAULT_CONFIG(发行包自带的那份)为底重建, 不能从空的
    开始 —— 理由见那个常量上面的注释. 读不出来的原文件先改名成config.json.bak
    留着, 绝不直接冲掉. 失败只打印警告返回False, 不抛 —— 配置没写上最多是"还得
    手点run", 不该拦住寻路/刷怪."""
    config_path = os.path.join(_INSTALL_DIR, "config.json")
    # 读之前先记下文件在不在(isfile放在try里面: 这个函数承诺绝不抛, 连探路都不例外).
    config_existed = False
    try:
        config_existed = os.path.isfile(config_path)
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            # 整份配置被写坏成数组/标量: 当不了底, 跟"读不出来"走同一条重建路径.
            raise TypeError(f"顶层不是JSON对象, 是{type(config).__name__}")
    except Exception as e:
        # deepcopy: 下面的合并是就地.update()的, 直接拿常量当底会把模块级默认值
        # 改成我们的强制值, 同一次运行里第二次重建拿到的就不是出厂设置了.
        config = copy.deepcopy(_DEFAULT_CONFIG)
        if config_existed:
            # 文件不存在是正常首次运行, 不吭声; 但"文件在、却读不出来"时用户自己
            # 调过的键(mouseSpeed/yoloConfig之类)这次都用不上了, 必须把原因说出来,
            # 并且把原文件挪成.bak让他能捞回来 —— 不能静悄悄地删人东西. 最可能的
            # 触发不是JSON语法坏了, 而是中文Windows上有人拿记事本按ANSI(GBK)存过
            # 它, 我们按utf-8读直接UnicodeDecodeError.
            backup_note = _backup_broken_config(config_path)
            print(
                f"⚠️ florr-auto-afk原有的config.json读不出来({backup_note}), "
                f"已按发行版默认值重建(自定义设置不会生效): {e}"
            )

    for section, values in _REQUIRED_CONFIG.items():
        section_config = config.get(section)
        if not isinstance(section_config, dict):
            # 单个section被写坏成标量/数组: 补回发行版默认的那一份, 同样不能用{} ——
            # 只塞我们那几个键的话, 上游读同一section里的其它键时照样KeyError.
            section_config = copy.deepcopy(_DEFAULT_CONFIG.get(section, {}))
        section_config.update(values)
        config[section] = section_config

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"⚠️ 写florr-auto-afk的config.json失败(可能还需要手动点run): {e}")
        return False


def _current_log_size():
    """latest.log当前字节数, 文件不存在算0. 用来在启动前划一条线 —— 它是用'a'
    追加打开的(segment_utils.py), 跨次运行不清空, 整文件搜marker会把上次运行的
    记录误判成本次启动成功."""
    try:
        return os.path.getsize(LATEST_LOG_PATH)
    except OSError:
        return 0


def _wait_for_segment_started(start_offset, timeout=_START_TIMEOUT_SECONDS, interval=1.0):
    """轮询latest.log在start_offset之后的新增内容, 等它写出"检测已启动"那条.
    找到返回True, 超时返回False —— 不抛异常, 调用方只是打印不同的提示.

    每轮都从start_offset重读一遍尾巴, 不做增量offset推进: 这段日志很小, 重读几十
    次的代价远小于维护offset的复杂度, 也天然没有"marker刚好被切在两次读中间"的漏检.
    """
    deadline = time.time() + timeout
    while True:
        try:
            size = os.path.getsize(LATEST_LOG_PATH)
            with open(LATEST_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                # 比划线时还小说明不是同一份文件了(被删/被换), 从头读.
                f.seek(0 if size < start_offset else start_offset)
                if _STARTED_MARKER in f.read():
                    return True
        except Exception:
            pass  # 文件还没出现: 它还在初始化, 继续等
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def _is_florr_auto_afk_running():
    """查segment.exe是不是已经有实例在跑(只在Windows上有意义, 调用方保证).

    tasklist找不到匹配进程时退出码照样是0, 只是往stdout印一句"INFO: No tasks
    are running which match the specified criteria." —— 不能靠returncode判断,
    只能看输出里有没有那个进程名. 查不了(tasklist不存在/输出解码失败)就当"没在
    跑", 保持加这个检测之前的行为: 顶多多开一个, 不会因为探测失败就不开.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {_EXE_NAME}", "/NH"],
            capture_output=True,
            text=True,
            errors="ignore",  # 中文Windows的tasklist输出不是UTF-8, 别因为解码炸掉
        )
        return _EXE_NAME.lower() in (result.stdout or "").lower()
    except Exception:
        return False


def is_florr_auto_afk_running():
    """查 segment.exe 是不是已经有实例在跑(只在 Windows 上有意义). GUI 的 AFK
    开关用它决定"要不要再开一个"."""
    return _is_florr_auto_afk_running()


def stop_florr_auto_afk():
    """taskkill 掉 segment.exe(AFK 开关关掉时用). 非 win32 直接返回. 本来就没在
    跑不算失败 —— 吞掉非零退出码和异常, 跟 _is_florr_auto_afk_running() 一个态度."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(["taskkill", "/IM", _EXE_NAME, "/F"], capture_output=True)
    except Exception as e:
        print(f"⚠️ 停止 florr-auto-afk 失败(忽略): {e}")


def download_florr_auto_afk():
    """下载+校验+解压 florr-auto-afk, 不问确认 —— 确认对话框由 GUI 负责弹.
    命令行那条路(ensure_florr_auto_afk_running)仍然走 _prompt_download_confirm()."""
    return _download_and_extract()


def ensure_florr_auto_afk_running():
    """确保florr-auto-afk在跑 —— 已经在跑就不动, 没装就问要不要下, 装了没跑就写好
    config并静默打开它, 然后读它的日志确认真的进入了检测状态. 用户不需要点任何按钮
    (自动启动靠config里的runs.autoStart, 见docs/superpowers/specs/2026-08-28-afk-autostart-design.md
    第一部分那个fork补丁). 只在Windows上做, 其余平台整段跳过. 全程不阻塞主流程 ——
    这是可选增强, 任何一步失败/用户跳过都只打印一句提示, main.py照常往下走."""
    if sys.platform != "win32":
        return

    # 在跑就直接返回, 连"装没装"都不用查(能跑起来说明装过了 —— 哪怕是用户自己
    # 另一份拷贝, 也不该再开一个). 多开两个实例会各自做YOLO拖拽, 互相抢鼠标.
    if _is_florr_auto_afk_running():
        print("✅ florr-auto-afk已经在跑, 不重复打开.")
        return

    if not os.path.isfile(_EXE_PATH):
        if not _prompt_download_confirm():
            print("   跳过florr-auto-afk, 之后AFK弹窗不会自动处理.")
            return
        if not _download_and_extract():
            return  # 失败原因已经在_download_and_extract()里打印过了

    _write_afk_config()
    # 划线必须在Popen之前 —— 它启动瞬间就写marker, 晚划就落在线左边, 永远等不到.
    start_offset = _current_log_size()
    try:
        # cwd=_INSTALL_DIR是必须的, 不能让它继承我们的CWD: segment.exe内部读的是
        # 相对路径'./config.json'(get_config()), 继承我们的CWD时它一启动就
        # FileNotFoundError: './config.json'挂掉; 而且它写的latest.log也落在CWD,
        # 不指定的话LATEST_LOG_PATH指向的位置永远不会有文件, AFK检测静默失效.
        subprocess.Popen([_EXE_PATH], cwd=_INSTALL_DIR)
    except Exception as e:
        print(f"⚠️ 打开florr-auto-afk失败(不影响主程序): {e}")
        return

    print(f"🪟 已在后台打开florr-auto-afk, 确认它开始检测(最多等{_START_TIMEOUT_SECONDS}秒)...")
    if _wait_for_segment_started(start_offset):
        print("✅ AFK弹窗自动处理已开启")
    else:
        print(
            "⚠️ 没能确认florr-auto-afk已开始检测(autoStart没生效 / 初始化特别慢 / "
            "YOLO模型损坏 —— 最后这种它会自己删掉模型, 下次启动时重下). "
            "需要的话去它窗口里手动点\"run\"(不点也不影响寻路/刷怪, 只是AFK弹窗不会自动处理)."
        )
