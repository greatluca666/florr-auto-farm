"""匿名使用统计. 控制面板启动时报一次; 刷怪进程在跑的时候每 5 分钟报一次当前时块的
地图和刷怪区. 发到 florrfarm.cc.cd/api/t, 服务器按来源 IP 查出国家/省/市后就把 IP 丢掉.

上报内容: 随机生成的安装 ID(每台电脑一个, 存在 config.json 旁边)、版本号、系统、地图、
刷怪区坐标. 不报账号名、不报 Chrome 里的任何东西.

全部在后台线程里发, 5 秒超时, 失败就算了、不重试 —— 统计丢几条无所谓, 绝不能反过来
卡住或弄崩控制面板. FLORR_TELEMETRY=0 关掉(给测试和开发机用).
"""
import json
import os
import platform
import ssl
import threading
import urllib.request
import uuid

import certifi

import app_config
import version

ENDPOINT = "https://florrfarm.cc.cd/api/t"
HEARTBEAT_S = 300              # 和服务器 stats_store.HEARTBEAT_MINUTES 对应
TIMEOUT_S = 5
ID_PATH = os.path.join(os.path.dirname(app_config.CONFIG_PATH), "install_id")
# 和 updater.py 同样的原因: Windows 上 urllib 不读系统证书库, 显式用 certifi.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def enabled():
    return os.environ.get("FLORR_TELEMETRY", "1") != "0"


def install_id(path=None):
    """读这台电脑的安装 ID, 没有就生成一个存下来. 存不下来(目录只读等)也照样返回一个,
    只是下次启动会换 —— 多算一台电脑, 不影响程序."""
    path = path or ID_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            iid = f.read().strip()
        if len(iid) == 32 and all(c in "0123456789abcdef" for c in iid):
            return iid
    except OSError:
        pass
    iid = uuid.uuid4().hex
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(iid + "\n")
    except OSError:
        pass
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
