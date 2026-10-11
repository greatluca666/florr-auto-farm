"""录人类刷怪数据 —— 行为克隆的专家数据来自你自己打的那些局。

每 0.1 秒记一条 `(这一刻的画面, 你这一刻按了什么)`。画面走 canvas_hook.js 解码,
操作走 input_hook.js 的页面按键钩子 —— 两边都用 performance.now(), 天然同一条时间轴。

存的是**解码出来的原始状态**, 不是编码好的观测向量: 观测编码还在设计中(维度、归一化、
物种 one-hot 都可能改), 而从原始状态重新编码是离线免费的。存编码结果等于把当下这版
设计焊死在数据里。

跑法(你照常玩, 这个在旁边录):

    python rl_record.py --out demos/desert_01.jsonl.gz --minutes 30

Ctrl+C 随时停。停的时候会打一张动作分布表 —— 如果里面全是"不动", 说明没录到你的操作,
别等录完 30 分钟才发现。

只读: 不发任何按键/点击, 钩子全是 capture 阶段的被动监听, 不 preventDefault。
"""
import argparse
import gzip
import json
import sys
import time
from collections import Counter
from pathlib import Path

import cdp_bridge
import rl_actions
from rl_scene import RANK_BY_COLOR, FrameReader

K_MOBS = 16          # 每帧最多记多少只怪(按距离由近到远)
K_PLAYERS = 4        # 最多记多少个其他玩家
COUNTER_EVERY = 10   # 每几 tick 读一次击杀/掉落计数(10Hz 下 = 1Hz)

MOVE_NAMES = ["不动", "北", "东北", "东", "东南", "南", "西南", "西", "西北"]


def val(expression, timeout=15):
    resp = cdp_bridge.eval_js(f"JSON.stringify({expression})", timeout=timeout)
    inner = (resp or {}).get("result", {}).get("result", {})
    if "value" not in inner:
        raise RuntimeError(f"JS 没返回值: {inner!r}")
    raw = inner["value"]
    return None if raw is None else json.loads(raw)


def inject(name):
    cdp_bridge.eval_js(Path(__file__).with_name(name).read_text(encoding="utf-8"), timeout=10)


def _near(items, me, k):
    if me is None:
        return []
    out = []
    for it in items:
        dx, dy = it["x"] - me["x"], it["y"] - me["y"]
        out.append((dx * dx + dy * dy, dx, dy, it))
    out.sort(key=lambda t: t[0])
    return out[:k]


def build_record(t, me, mobs, others, action, focus, approx, counters):
    """一条样本。数值都压到 1 位小数 —— 亚像素精度对策略没有意义, 但体积差一倍。"""
    rec = {
        "t": round(t, 2),
        "a": action,
        "f": 1 if focus else 0,
    }
    if approx:
        rec["approx"] = 1        # 相机只是近似, 自身血量/世界坐标不可信, 训练时可丢
    if me is not None:
        rec["p"] = [round(me.get("hp") or 0.0, 3), round(me.get("hp_secondary") or 0.0, 3),
                    round(me["x"], 1), round(me["y"], 1)]
    rec["m"] = [[round(dx, 1), round(dy, 1),
                 round(mob.get("hp") or 0.0, 3),
                 RANK_BY_COLOR.get(mob.get("rarity_color"), -1),
                 mob.get("name") or ""]
                for _, dx, dy, mob in _near(mobs, me, K_MOBS)]
    rec["o"] = [[round(dx, 1), round(dy, 1), p.get("level") or -1]
                for _, dx, dy, p in _near(others, me, K_PLAYERS)]
    if counters is not None:
        rec["c"] = counters
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description="录人类刷怪数据(行为克隆用)")
    ap.add_argument("--out", required=True, help="输出文件, 建议 .jsonl.gz")
    ap.add_argument("--minutes", type=float, default=30, help="录多久, 默认 30 分钟")
    ap.add_argument("--hz", type=float, default=10.0, help="每秒记几条, 默认 10")
    ap.add_argument("--no-counters", action="store_true", help="不读击杀/掉落计数")
    args = ap.parse_args(argv)

    if not cdp_bridge.is_cdp_port_reachable():
        print("❌ CDP 端口不通 —— Chrome 要用那三个启动参数起")
        return 2

    print("注入 canvas hook…")
    try:
        cdp_bridge.inject_canvas_hook()
    except Exception as e:
        print(f"❌ canvas hook 注入失败: {e}")
        return 2
    print("注入输入钩子…")
    inject("input_hook.js")

    use_counters = not args.no_counters
    if use_counters:
        print("注入 WASM 探针(读击杀/掉落计数)…")
        try:
            inject("florr_wasm_probe.js")
            val("window.__florrWasmProbe.scan()")
            for _ in range(180):
                st = val("window.__florrWasmProbe")
                if (st or {}).get("status") == "ready":
                    break
                if (st or {}).get("status") == "error":
                    raise RuntimeError(st.get("reason"))
                time.sleep(0.5)
            else:
                raise RuntimeError("扫描超时")
        except Exception as e:
            print(f"⚠️  计数器不可用({e}) —— 只录画面和操作")
            use_counters = False

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if out_path.suffix == ".gz" else open

    period = 1.0 / args.hz
    deadline = time.time() + args.minutes * 60
    started = time.time()
    held = set()
    moves = Counter()
    reader = FrameReader()
    n, n_focus, n_scene = 0, 0, 0

    print(f"\n🔴 开始录制 -> {out_path}   {args.hz:.0f}Hz, 计划 {args.minutes:.0f} 分钟。"
          f"Ctrl+C 随时停。\n   现在切回游戏正常打就行。\n")
    try:
        with opener(out_path, "wt", encoding="utf-8") as f:
            f.write(json.dumps({"_meta": {"hz": args.hz, "k_mobs": K_MOBS,
                                          "k_players": K_PLAYERS,
                                          "started": started}}, ensure_ascii=False) + "\n")
            while time.time() < deadline:
                tick_start = time.time()
                me, mobs, others, player_screen, approx = reader.scene()

                inp = val("window.__florrInputHook.drain()") or {}
                events = inp.get("events") or []
                held = rl_actions.apply_events(held, events)
                action = rl_actions.action_from_state(
                    held, inp.get("mouse"), player_screen,
                    swap_presses=rl_actions.count_swap_presses(events))
                focus = bool(inp.get("focus", True))

                counters = None
                if use_counters and n % COUNTER_EVERY == 0:
                    t = val("window.__florrWasmProbe.readTotals()", timeout=20)
                    if t and not t.get("error") and not t.get("hiNonZero"):
                        counters = {"k": t["kills"], "p": t["petals"]}

                f.write(json.dumps(
                    build_record(tick_start - started, me, mobs, others, action,
                                 focus, approx, counters), ensure_ascii=False) + "\n")
                n += 1
                n_focus += focus
                n_scene += me is not None
                moves[action["move"]] += 1

                if n % (int(args.hz) * 30) == 0:
                    mins = (time.time() - started) / 60
                    print(f"   {mins:5.1f} 分钟  {n:6d} 条  "
                          f"有画面 {n_scene * 100 // max(1, n)}%  "
                          f"有焦点 {n_focus * 100 // max(1, n)}%")
                time.sleep(max(0.0, period - (time.time() - tick_start)))
    except KeyboardInterrupt:
        print("\n(手动停止)")

    print(f"\n共 {n} 条, {(time.time() - started) / 60:.1f} 分钟 -> {out_path}")
    if n:
        print(f"有画面 {n_scene * 100 // n}%   有焦点 {n_focus * 100 // n}%")
        print("动作分布:")
        for idx, cnt in moves.most_common():
            print(f"   {MOVE_NAMES[idx]:<5} {cnt:6d}  {cnt * 100 // n:3d}%")
        if moves.get(0, 0) == n:
            print("\n⚠️  全是「不动」—— 没录到你的操作。检查: 录制时游戏标签页是不是前台?"
                  "\n   input_hook 挂在页面上, 切到别的窗口就收不到按键了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
