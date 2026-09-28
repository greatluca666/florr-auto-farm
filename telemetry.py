"""匿名使用统计. 控制面板启动时报一次; 刷怪进程在跑的时候每 5 分钟报一次当前时块的
地图和刷怪区. 发到 florrfarm.cc.cd/api/t, 服务器按来源 IP 查出国家/省/市后就把 IP 丢掉.

上报内容: 随机生成的安装 ID(每台电脑每个系统用户一个, 存在用户目录里, 同一台电脑上的
打包版、源码版、新旧版本文件夹共用)、版本号、系统、地图、刷怪区坐标. 不报账号名、不报 Chrome 里的任何东西.

全部在后台线程里发, 5 秒超时, 失败就算了、不重试 —— 统计丢几条无所谓, 绝不能反过来
卡住或弄崩控制面板. FLORR_TELEMETRY=0 关掉(给测试和开发机用).
"""
import json
import os
import platform
import ssl
import sys
import threading
import urllib.request
import uuid

import certifi

import app_config
import version

ENDPOINT = "https://florrfarm.cc.cd/api/t"
HEARTBEAT_S = 300              # 和服务器 stats_store.HEARTBEAT_MINUTES 对应
TIMEOUT_S = 5


def user_data_dir(plat=None):
    """这个系统用户的程序数据目录(不跟着程序文件夹走)."""
    plat = plat or sys.platform
    if plat.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    elif plat == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "florr-auto-farm")


ID_PATH = os.path.join(user_data_dir(), "install_id")
# v1.0.3 把 ID 存在程序文件夹里(config.json 旁边): 换个文件夹解压、或者源码和打包版各跑一份,
# 同一台电脑就成了好几台. 现在只在用户目录存不了时才退回这里; 读到老 ID 就接着用.
LEGACY_ID_PATH = os.path.join(os.path.dirname(app_config.CONFIG_PATH), "install_id")
# 和 updater.py 同样的原因: Windows 上 urllib 不读系统证书库, 显式用 certifi.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def enabled():
    return os.environ.get("FLORR_TELEMETRY", "1") != "0"


def _read_id(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            iid = f.read().strip()
    except (OSError, ValueError):
        return None
    return iid if len(iid) == 32 and all(c in "0123456789abcdef" for c in iid) else None


def _write_id(path, iid):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(iid + "\n")
        return True
    except (OSError, ValueError):
        return False


def install_id():
    """这台电脑的安装 ID. 用户目录里有就用它; 没有就接过程序文件夹里老版本留下的, 再没有
    才新生成, 然后存进用户目录. 用户目录存不了退回程序文件夹; 哪都存不了也照样返回一个,
    只是下次启动会换 —— 多算一台电脑, 不影响程序."""
    iid = _read_id(ID_PATH)
    if iid:
        return iid
    iid = _read_id(LEGACY_ID_PATH) or uuid.uuid4().hex
    if not _write_id(ID_PATH, iid):
        _write_id(LEGACY_ID_PATH, iid)
    return iid


def _base(kind):
    return {"v": 1, "type": kind, "id": install_id(), "ver": version.__version__}


def start_event():
    return {**_base("start"), "os": f"{platform.system()} {platform.release()}"[:64]}


def heartbeat_event(block):
    """block: 时间表里正在跑的时块(app_config 的 schedule 条目)."""
    area = block.get("farming_area")
    if area is not None:
        area = [[int(area[0][0]), int(area[0][1])], [int(area[1][0]), int(area[1][1])]]
    return {**_base("hb"), "map": block.get("map"), "area": area}


def _post(payload):
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": f"florr-auto-farm/{version.__version__}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_SSL_CONTEXT) as resp:
        resp.read()


def _quiet(post, payload):
    try:
        post(payload)
    except Exception:
        pass


def send(make_payload, post=_post):
    """make_payload: 生成事件的函数 —— 读/写安装 ID 也放进后台线程, 不占界面线程.
    关着的时候返回 None, 否则返回已启动的线程."""
    if not enabled():
        return None

    def run():
        try:
            payload = make_payload()
        except Exception:
            return
        _quiet(post, payload)

    t = threading.Thread(target=run, name="telemetry", daemon=True)
    t.start()
    return t
