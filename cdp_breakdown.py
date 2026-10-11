"""把一次 CDP 调用拆开量 —— 那 56ms 固定成本到底是握手, 还是主线程在排队?

两者的修法完全不同:
  * 握手贵   -> cdp_bridge 每次调用都 HTTP GET /json + 新开一条 WebSocket, 复用连接就行
  * 排队贵   -> 页面主线程在跑 60fps 的游戏, 我们的 eval 得等它让出空隙; 复用连接省不了

量五档, 每档都是同一件事只改一个变量:
  1. HTTP GET /json          找标签页(cdp_bridge 每次调用都做)
  2. 新开 WebSocket 再关掉    握手本身
  3. 复用连接上求值 `1`       纯往返 + 主线程排队, 零载荷
  4. 复用连接上跑一拍但只回条数  加上页面里那段 JS 的真实工作量, 仍然零载荷
  5. 复用连接上跑完整一拍      再加上序列化 + 传输真实载荷

    3 - (1+2) 的差 = 复用连接能省多少
    4 - 3      = 那段 JS 自己花了多久
    5 - 4      = 载荷花了多久

只读, 不发按键不动光标。
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request

import websocket

import cdp_bridge
import rl_backend


def ms(xs):
    xs = sorted(xs)
    return (statistics.median(xs), xs[int(len(xs) * 0.95)] if xs else 0.0, max(xs, default=0.0))


class Conn:
    """一条一直开着的 CDP 连接。id 每次递增, 按 id 对上号才算我们的响应 —— 页面事件
    会混在同一条连接上。"""

    def __init__(self, url, timeout=20):
        self.ws = websocket.create_connection(url, timeout=timeout)
        self.id = 0

    def evaluate(self, expression):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": "Runtime.evaluate",
                                 "params": {"expression": expression}}))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == self.id:
                return r

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="拆开量一次 CDP 调用的耗时构成")
    ap.add_argument("--n", type=int, default=30, help="每档量几次")
    ap.add_argument("--setup", action="store_true", help="先注入 canvas hook")
    args = ap.parse_args(argv)

    tab = cdp_bridge.find_florr_tab()
    if tab is None:
        print("❌ 找不到 florr.io 标签页(CDP)")
        return 2
    url = tab["webSocketDebuggerUrl"]

    if args.setup:
        print("注入 canvas hook…")
        cdp_bridge.inject_canvas_hook()
        time.sleep(1.0)

    def bench(fn, n=None):
        out = []
        for _ in range(n or args.n):
            t0 = time.perf_counter()
            fn()
            out.append((time.perf_counter() - t0) * 1000)
            time.sleep(0.05)
        return out

    print(f"每档 {args.n} 次…\n")

    http = bench(lambda: urllib.request.urlopen(
        f"http://{cdp_bridge.CDP_HOST}:{cdp_bridge.CDP_PORT}/json", timeout=3).read())

    def fresh_ws():
        w = websocket.create_connection(url, timeout=20)
        w.close()

    ws_handshake = bench(fresh_ws)

    conn = Conn(url)
    try:
        trivial = bench(lambda: conn.evaluate("1"))

        count_js = rl_backend._TICK_JS_TEMPLATE % "null"
        # 同一段 JS, 只把 recs 换成它的**条数** —— 页面里的活一模一样, 回传的载荷没了。
        count_only = count_js.replace("return {f: document.hasFocus(), c: null, r: recs};",
                                      "return {f: document.hasFocus(), c: null, r: recs.length};")
        assert count_only != count_js, "改写没生效, 这一档量的还是完整载荷"
        counts = bench(lambda: conn.evaluate(f"JSON.stringify({count_only})"))

        sizes = []

        def full():
            r = conn.evaluate(f"JSON.stringify({count_js})")
            v = r.get("result", {}).get("result", {}).get("value") or "{}"
            sizes.append(len(v))

        fulls = bench(full)
    finally:
        conn.close()

    # 训练每拍还要发一次鼠标移动 —— 诊断脚本(debug_rl_tick)把输入整个 stub 掉了, 所以
    # 它永远测不到这一段。pyautogui.PAUSE 默认 0.1, 是**每次调用之后**都睡。
    # 移到光标现在所在的位置 = 不动, 只量开销。
    pag_rows = []
    try:
        import pyautogui

        here = pyautogui.position()
        pag_rows.append(("6. pyautogui.moveTo(默认 PAUSE)",
                         bench(lambda: pyautogui.moveTo(here), n=min(args.n, 10))))
        zeroed = rl_backend.without_pause(pyautogui.moveTo, pyautogui)
        pag_rows.append(("7. pyautogui.moveTo(PAUSE 置零)",
                         bench(lambda: zeroed(here), n=min(args.n, 10))))
    except Exception as e:
        print(f"(跳过 pyautogui 那两档: {e})")

    rows = [("1. HTTP GET /json(找标签页)", http),
            ("2. 新开 WebSocket 再关掉", ws_handshake),
            ("3. 复用连接求值 `1`", trivial),
            ("4. 复用连接跑一拍(只回条数)", counts),
            ("5. 复用连接跑完整一拍", fulls)] + pag_rows
    print(f"{'':<30}{'中位':>9}{'p95':>9}{'最大':>9}   (毫秒)")
    for name, xs in rows:
        p50, p95, mx = ms(xs)
        print(f"{name:<30}{p50:>9.1f}{p95:>9.1f}{mx:>9.1f}")

    h = statistics.median(http) + statistics.median(ws_handshake)
    t = statistics.median(trivial)
    c = statistics.median(counts)
    f = statistics.median(fulls)
    print(f"\n现在每拍的固定开销 = 1+2+3 = {h + t:.1f}ms")
    print(f"复用连接能省掉的      = 1+2   = {h:.1f}ms")
    print(f"页面里那段 JS 自己花   = 4-3   = {c - t:.1f}ms")
    print(f"载荷(序列化+传输)花    = 5-4   = {f - c:.1f}ms")
    if sizes:
        print(f"每次回传 {statistics.median(sizes) / 1024:.0f} KB")
    pag = statistics.median(pag_rows[0][1]) if pag_rows else 0.0
    pag0 = statistics.median(pag_rows[1][1]) if pag_rows else 0.0
    if pag_rows:
        print(f"发一次鼠标移动省掉的  = 6-7   = {pag - pag0:.1f}ms")
    print(f"\n训练时一拍大约 = CDP {f:.1f} + 鼠标 {pag0:.1f} + 解码 ≈ "
          f"{f + pag0 + 5:.1f}ms -> {1000 / max(f + pag0 + 5, 1e-9):.1f}Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
