"""量一拍到底花在哪 —— RL 后端撑不撑得住 10Hz, 靠这个回答, 不靠猜。

实机第一版(2026-09-23)量出来: 目标 10Hz, 实际 4.25Hz。
    CDP evaluate(计数+焦点)   中位  42.5ms
    CDP drain(画面日志)       中位 185.5ms   <- 大头
两次走的是同一套握手, 差的 143ms 是**载荷**: canvas_hook 的页面缓冲留 5 帧
(FRAME_RETENTION), 一次 drain 拖回 490 条记录, 而每拍只用得上其中一帧。

现在改成在页面里先挑出那一帧再回传, 并跟计数器、焦点合成一次调用。--compare 会把
老路子(整段 drain)也量一遍, 直接看两者差多少。

默认**不发任何按键**(动作恒为"不动"), 跑的时候花朵不会乱动, 可以一边看画面。

    python debug_rl_tick.py --ticks 100 --setup
    python debug_rl_tick.py --ticks 100 --setup --compare
    python debug_rl_tick.py --ticks 60 --dump-frame frame.json   # 存一帧原始记录
"""
import argparse
import json
import statistics
import sys
import time
from collections import Counter

import cdp_bridge
import rl_backend
import rl_scene


class Stopwatch:
    """记每一段的耗时。分位数比平均值有用得多 —— 卡的是尾巴不是平均。"""

    def __init__(self):
        self.samples = {}

    def time(self, name, fn, *a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            self.samples.setdefault(name, []).append((time.perf_counter() - t0) * 1000)

    def report(self):
        rows = []
        for name, xs in self.samples.items():
            xs = sorted(xs)
            rows.append((name, len(xs), xs[len(xs) // 2], xs[int(len(xs) * 0.95)], xs[-1],
                         statistics.fmean(xs)))
        return rows


class NoInput:
    """诊断时既不发按键也不动光标 —— 花朵不该因为你在量耗时就乱跑, 光标也不该被抢走。"""

    held = set()

    def apply(self, action, anchor=None):
        return set(), set(), []

    def release_all(self):
        pass

    def park(self, anchor=None):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="量 RL 后端一拍的耗时构成")
    ap.add_argument("--ticks", type=int, default=100)
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--no-counters", action="store_true", help="不读击杀/掉落计数")
    ap.add_argument("--setup", action="store_true",
                    help="也做一次 setup(注入钩子/探针, 关反转开关)")
    ap.add_argument("--compare", action="store_true",
                    help="顺便量一遍老路子(整段 drain), 对比载荷差多少")
    ap.add_argument("--dump-frame", default=None,
                    help="把第一帧解得出怪的原始记录存成 JSON(解不出怪时存最后一帧)")
    ap.add_argument("--no-focus-gate", action="store_true", help="不等焦点直接开始")
    args = ap.parse_args(argv)

    if not cdp_bridge.is_cdp_port_reachable():
        print("❌ CDP 端口不通 —— Chrome 要用那三个启动参数起")
        return 2

    sw = Stopwatch()
    session = cdp_bridge.CdpSession()
    raw_eval = session.eval_value
    frame_sizes = []

    def timed_eval(expr, timeout=15):
        return sw.time("CDP 一拍(焦点+计数+一帧, 常驻连接)", raw_eval, expr, timeout)

    scenes = []
    dumped = []

    def timed_decode(records):
        frame_sizes.append(len(records or []))
        if args.dump_frame is not None:
            dumped.append(list(records or []))
        sc = sw.time("解码(纯 CPU)", rl_scene.decode, records)
        scenes.append(sc)
        return sc

    backend = rl_backend.LiveBackend(
        hz=args.hz, use_counters=not args.no_counters,
        decode=timed_decode, sender=NoInput(), mover=NoInput(), evaluate=timed_eval)

    if args.setup:
        print("setup: 注入钩子 + 探针, 关反转开关…")
        for name, (status, detail) in backend.setup(strict=False).items():
            print(f"   反转{name}: {status} {detail}")

    if not args.no_focus_gate and not rl_backend.wait_for_focus(timeout=90.0):
        print("   (继续量, 但 control 会全是 unfocused)")

    print(f"跑 {args.ticks} 拍 @ {args.hz:g}Hz(不发按键)…")
    t0 = time.perf_counter()
    backend.reset()

    control = Counter()
    n_player = n_mobs_seen = 0
    mob_counts = []
    kills = drops = 0
    for i in range(args.ticks):
        backend.send_action((0, 0, 0, 0))
        st = sw.time("一整拍(含等节拍)", backend.next_state)
        if st is None:
            print(f"\n❌ 第 {i + 1} 拍后端断了: {backend.last_error}")
            break
        control[st["control"]] += 1
        n_player += st["player"] is not None
        mob_counts.append(len(st["mobs"]))
        n_mobs_seen += bool(st["mobs"])
        kills += sum(n for _, n in st["kill_events"])
        drops += sum(n for _, n in st["drop_events"])
    elapsed = time.perf_counter() - t0
    done = sum(control.values())

    # 对比必须放在主循环**之后**单独跑: drain_canvas_log 会把页面缓冲整个清空, 跟
    # 主路径抢帧 —— 混在一起量的话, 主路径的"解出自己 %"会被这个动作压低, 量出来的
    # 就不是它真实的样子了。
    if args.compare:
        print("\n对比: 单独量老路子(整段 drain)…")
        legacy_sizes = []
        for _ in range(min(args.ticks, 30)):
            recs = sw.time("(对比)老路子整段 drain", cdp_bridge.drain_canvas_log)
            legacy_sizes.append(len(recs or []))
            time.sleep(1.0 / args.hz)
        if legacy_sizes:
            print(f"   老路子每次拖回 {statistics.median(legacy_sizes):.0f} 条(中位)")

    print(f"\n跑了 {elapsed:.1f} 秒, {done} 拍 -> 实际 {done / max(elapsed, 1e-9):.2f} Hz"
          f" (目标 {args.hz:g})")
    print(f"\n{'':<28}{'次数':>6}{'中位':>9}{'p95':>9}{'最大':>9}{'均值':>9}   (毫秒)")
    for name, n, p50, p95, mx, avg in sw.report():
        print(f"{name:<28}{n:>6}{p50:>9.1f}{p95:>9.1f}{mx:>9.1f}{avg:>9.1f}")

    if done:
        print(f"\n解出自己  {n_player * 100 // done}%    有怪  {n_mobs_seen * 100 // done}%"
              f"    一屏怪数中位 {statistics.median(mob_counts):.0f}"
              f"    最多 {max(mob_counts)}")
        print("control:  " + "  ".join(f"{k}={v}" for k, v in control.most_common()))
        print(f"这段里击杀 {kills}  掉落 {drops}")
    if frame_sizes:
        print(f"每拍回传的绘制记录数: 中位 {statistics.median(frame_sizes):.0f}"
              f"  最大 {max(frame_sizes)}   (改之前是一次 490 条)")

    if args.dump_frame and dumped:
        # 优先存一帧**解得出怪**的; 一帧都解不出就存记录最多的那帧 —— 那才是有东西
        # 可看的, 存个空帧没意义。
        best = None
        for recs, sc in zip(dumped, scenes):
            if sc.mobs:
                best = recs
                break
        if best is None:
            best = max(dumped, key=len)
        with open(args.dump_frame, "w", encoding="utf-8") as f:
            json.dump({"raw": best, "had_mobs": bool(best is not None and any(
                s.mobs for s in scenes))}, f, ensure_ascii=False)
        print(f"\n存了一帧原始记录({len(best)} 条) -> {args.dump_frame}")

    if done and done / max(elapsed, 1e-9) < args.hz * 0.8:
        print("\n⚠️  实际频率明显低于目标 —— 看上面哪一段占大头。"
              "\n   CDP 那一行大 = 往返/载荷; 解码那一行大 = 纯 CPU, 那是另一回事。")
    if done and control.get("unfocused"):
        print(f"\n⚠️  有 {control['unfocused']} 拍窗口没焦点 —— 训练时这些拍全会被截断, "
              "策略的按键根本没进游戏。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
