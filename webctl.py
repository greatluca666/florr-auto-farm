"""florr-auto-pathing Web 控制面板后端. 跟 florr-bot 一样以非 root 用户跑,
部署方式见 deploy/ubuntu/florr-webctl.service + setup.sh.

架构(完整说明见 docs/superpowers/specs/2026-09-14-web-control-panel-design.md):
两台机器各跑一份完全相同的这份代码, 互相不知道对方存在, 只是各自知道"对方的
URL"(FLORR_WEBCTL_PEER_URL). 前端浏览器打开任意一台后, JS 同时向两台的
/api/* 发跨域请求聚合展示 —— 这里的 CORS 处理就是为了让那个跨域请求能成功.

鉴权: 单一共享密码本身就是 bearer token, 没有独立的登录端点/服务端会话状态.
"""
import datetime
import functools
import json
import os
import secrets
import subprocess
import time

from flask import Flask, request, jsonify, Response
from werkzeug.middleware.proxy_fix import ProxyFix

import app_config

app = Flask(__name__, static_folder="webctl_static", static_url_path="")
# 只信任一跳代理(Caddy, 跑在同一台机器上反代到这里) —— x_for=1 意味着只採信
# X-Forwarded-For 链条最后一段, 再往前的段可以被客户端随意伪造, 不能信.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

_PASSWORD_FILE = os.environ.get("FLORR_WEBCTL_PASSWORD_FILE", "/etc/florr-webctl/password")
_PEER_URL = os.environ.get("FLORR_WEBCTL_PEER_URL", "").rstrip("/")

_FAILED_ATTEMPTS = {}  # ip -> (连续失败次数, 锁定截止时间戳(0=未锁定))
_MAX_ATTEMPTS = 10
_LOCKOUT_SECONDS = 300


def _load_password():
    """读密码文件. 文件缺失/不可读时不能让异常冒到 Flask 顶层 —— 那会把每一个
    端点(包括压根没鉴权的)都炸成裸 HTML 500. Fail closed: 返回一个随机哨兵,
    真实密码(client 发来的 token)几乎不可能撞上它, 于是 _authenticate 正常走
    到"密码错误"分支, 返回干净的 401.

    空文件(0 字节, 或去空白后是空)是同一类问题的另一种形态, 不能走同一个
    except 分支接住: open()+read()+strip() 不会抛异常, 得到的就是空字符串 ——
    而没带 Authorization header 的请求, require_auth 里 token 也默认是空
    字符串, "" == "" 直接放行, 认证形同虚设. 两台机器间手动拷贝密码文件
    (README 里的操作步骤)传输中断就会留下这样一个空文件. 读到空字符串时
    同样返回哨兵, 不当成"密码就是空的"接受."""
    try:
        with open(_PASSWORD_FILE, "r", encoding="utf-8") as f:
            password = f.read().strip()
    except Exception:
        return secrets.token_hex(32)
    return password if password else secrets.token_hex(32)


def _rate_limited(ip):
    _count, locked_until = _FAILED_ATTEMPTS.get(ip, (0, 0))
    return time.time() < locked_until


def _record_failure(ip):
    count, _locked_until = _FAILED_ATTEMPTS.get(ip, (0, 0))
    count += 1
    locked_until = time.time() + _LOCKOUT_SECONDS if count >= _MAX_ATTEMPTS else 0
    _FAILED_ATTEMPTS[ip] = (count, locked_until)


def _record_success(ip):
    _FAILED_ATTEMPTS.pop(ip, None)


def _authenticate(ip, token):
    """限流+密码校验的共享核心. 通过返回 (True, None); 拒绝返回
    (False, (body_dict, status)), 调用方直接 jsonify(body), status 转成响应.

    两处鉴权入口共用这个函数, 只是 token 的取法不同(header vs query string,
    见 require_auth 和 Task 6 的 /api/logs/stream) —— "限流检查 → 校验密码 →
    记失败/成功"这段逻辑只写这一处, 不在两个入口各抄一遍."""
    if _rate_limited(ip):
        return False, ({"error": "失败次数过多, 请稍后再试"}, 429)
    if token != _load_password():
        _record_failure(ip)
        return False, ({"error": "密码错误"}, 401)
    _record_success(ip)
    return True, None


def require_auth(fn):
    """路由装饰器: 从 Authorization: Bearer <密码> 取 token 交给 _authenticate.
    EventSource(浏览器原生 SSE 客户端)发不了自定义 header, 所以 /api/logs/stream
    不用这个装饰器, 自己在函数体里从 query string 读 token(见 Task 6), 但一样
    调 _authenticate 做实际校验."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ip = request.remote_addr
        auth = request.headers.get("Authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        ok, rejection = _authenticate(ip, token)
        if not ok:
            body, status = rejection
            return jsonify(body), status
        return fn(*args, **kwargs)
    return wrapper


@app.after_request
def _add_cors_headers(response):
    """允许配置好的对端 origin 跨域读这台机器的 /api/*. 没配对端时不加任何
    CORS 头 —— 默认关闭跨域, 不是默认放开."""
    if _PEER_URL:
        response.headers["Access-Control-Allow-Origin"] = _PEER_URL
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


_UNITS = ("florr-xvfb", "florr-wm", "florr-bot", "florr-vnc")
_MIB = 1024 * 1024


def _unit_status(unit):
    try:
        r = subprocess.run(
            ["systemctl", "is-active", f"{unit}.service"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception as e:
        return f"unknown: {e}"


def _memory_summary():
    try:
        r = subprocess.run(["free", "-b"], capture_output=True, text=True, timeout=5)
        mem = r.stdout.splitlines()[1].split()
        swap = r.stdout.splitlines()[2].split()
        return {
            "mem_total_mb": int(mem[1]) // _MIB,
            "mem_used_mb": int(mem[2]) // _MIB,
            "mem_available_mb": int(mem[6]) // _MIB,
            "swap_total_mb": int(swap[1]) // _MIB,
            "swap_used_mb": int(swap[2]) // _MIB,
        }
    except Exception as e:
        return {"error": str(e)}


def _current_active_block():
    cfg = app_config.load_config()
    now = datetime.datetime.now()
    return app_config.active_block(cfg["schedule"], now.weekday(), now.strftime("%H:%M"))


@app.route("/")
def index():
    """静态前端入口. static_url_path="" 只让 Flask 在 /<filename> 下自动提供
    webctl_static/ 里的文件(比如 /index.html), 裸路径 "/" 并不会自动映射到
    index.html(这是 Flask 的实际行为, 不是"static_folder 自动提供"能替代的) ——
    这里显式补上这一条, 页面本身不需要密码(密码校验在前端 JS 请求 /api/* 时
    才发生, 见 webctl_static/index.html), 所以不加 @require_auth."""
    return app.send_static_file("index.html")


@app.route("/api/status", methods=["GET"])
@require_auth
def get_status():
    return jsonify({
        "units": {u: _unit_status(u) for u in _UNITS},
        "memory": _memory_summary(),
        "active_block": _current_active_block(),
        "peer_url": _PEER_URL,
    })


@app.route("/api/config", methods=["GET"])
@require_auth
def get_config():
    try:
        with open(app_config.CONFIG_PATH, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = json.dumps(app_config.DEFAULTS_V2, ensure_ascii=False)
    return Response(text, mimetype="application/json")


@app.route("/api/config", methods=["POST"])
@require_auth
def post_config():
    raw_text = request.get_data(as_text=True)
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return jsonify({"errors": [f"不是合法 JSON: {e}"]}), 400
    errors = app_config.validate_config(raw)
    if errors:
        return jsonify({"errors": errors}), 400
    # 原子写: 先写临时文件再 os.replace 改名, 不直接 open(CONFIG_PATH, "w") 截断
    # 原文件 —— florr-bot 随时可能在并发读这份配置(编辑时块的整个意义就是"边跑
    # 边改"), plain write 会有一个"文件已截断但新内容还没写完"的窗口, 这时候
    # load_config() 读到的要么是半份 JSON(解析失败, 整体落回 DEFAULTS_V2)要么是
    # 空 schedule —— 静默地在跑一份用户完全没写过的配置。os.replace 在同一文件系
    # 统内是原子的, 并发读者只会看到完整的旧文件或完整的新文件, 不会看到中间态。
    tmp_path = app_config.CONFIG_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(raw_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, app_config.CONFIG_PATH)
    except Exception as e:
        return jsonify({"errors": [str(e)]}), 500
    return jsonify({"ok": True})


_ALLOWED_ACTIONS = ("start", "stop", "restart")


@app.route("/api/control/<action>", methods=["POST"])
@require_auth
def control(action):
    if action not in _ALLOWED_ACTIONS:
        return jsonify({"error": f"未知操作: {action}"}), 400
    try:
        r = subprocess.run(
            ["sudo", "systemctl", action, "florr-bot.service"],
            capture_output=True, text=True, timeout=15)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if r.returncode != 0:
        return jsonify({"error": r.stderr.strip() or r.stdout.strip()}), 500
    return jsonify({"ok": True})


@app.route("/api/logs/stream", methods=["GET"])
def stream_logs():
    """SSE 推 florr-bot 的实时日志. 浏览器原生 EventSource 发不了自定义 header,
    令牌只能走 query string —— 跟其它端点走 Authorization header 刻意不同, 但
    实际校验逻辑复用 _authenticate(Task 2), 不重复写限流/密码检查那段."""
    ip = request.remote_addr
    token = request.args.get("token", "")
    ok, rejection = _authenticate(ip, token)
    if not ok:
        body, status = rejection
        return jsonify(body), status

    def generate():
        proc = subprocess.Popen(
            ["journalctl", "-u", "florr-bot.service", "-f", "--output=cat", "-n", "50"],
            stdout=subprocess.PIPE, text=True, bufsize=1)
        try:
            for line in proc.stdout:
                yield f"data: {line.rstrip()}\n\n"
        finally:
            proc.terminate()

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    _port = int(os.environ.get("FLORR_WEBCTL_PORT", "8765"))
    app.run(host="127.0.0.1", port=_port)
