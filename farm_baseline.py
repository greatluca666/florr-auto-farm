"""量一段时间内的真实刷怪收益 —— kills/hour 和 drops/hour, 按稀有度分。

这是判断"RL 到底有没有比现有规则机器人强"的唯一客观锚点。没有它, "训出来了"只是感觉。

用法: 自己照常把 worker 跑起来(规则模式), 然后在**另一个终端**里:

    python farm_baseline.py --minutes 120 --label rules --json baseline_rules.json

跑完(或 Ctrl+C 提前结束)出一张表。之后 RL 模式再跑一次同样时长, 两个 JSON 一比就知道。

只读: 不碰 worker, 不发按键, 只每隔一会儿读一次游戏自己的计数器。worker 死亡/重开/
换服都不影响 —— 那是账号级累计数, 不会被重置。

时长用墙钟, **不扣除走路/换服/复活的时间** —— 那些开销本来就是吞吐的一部分, 正是
要比较的东西。
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import cdp_bridge

# 稀有度权重: 跟 RL 奖励设计里用的一致 —— (rank+1)/10。不用 rank/9, 否则普通怪恒为 0 分,
# 而刷怪区绝大多数是普通怪, 加权分会完全无视它们。
RARITY_NAMES = ["普通", "罕见", "稀有", "史诗", "传说", "神话", "究极", "超神", "独特", "永恒"]


def weight(rank):
    return (rank + 1) / 10.0


def val(expression, timeout=20):
    resp = cdp_bridge.eval_js(f"JSON.stringify({expression})", timeout=timeout)
    inner = (resp or {}).get("result", {}).get("result", {})
    if "value" not in inner:
        raise RuntimeError(f"JS 没返回值: {inner!r}")
    raw = inner["value"]
    return None if raw is None else json.loads(raw)


def summarize(samples, elapsed_s):
    """把采样序列汇总成速率。纯函数, 好测。

    samples: [{"t": 秒, "petals": [...], "kills": [...]}, ...] 至少两个
    返回 {elapsed_s, hours, kills_total/hour, drops_total/hour, 按档明细, 加权分/hour}
    """
    if len(samples) < 2:
        raise ValueError("至少要两次采样才算得出速率")
    first, last = samples[0], samples[-1]
    hours = elapsed_s / 3600.0 if elapsed_s > 0 else 0.0

    def diff(key):
        a, b = first[key], last[key]
        return [y - x for x, y in zip(a, b)]

    dk, dp = diff("kills"), diff("petals")

    def per_hour(total):
        return round(total / hours, 1) if hours > 0 else 0.0

    kills_weighted = sum(weight(i) * v for i, v in enumerate(dk))
    drops_weighted = sum(weight(i) * v for i, v in enumerate(dp))

    return {
        "elapsed_s": round(elapsed_s, 1),
        "hours": round(hours, 3),
        "samples": len(samples),
        "kills_by_rarity": dk,
        "drops_by_rarity": dp,
        "kills_total": sum(dk),
        "drops_total": sum(dp),
        "kills_per_hour": per_hour(sum(dk)),
        "drops_per_hour": per_hour(sum(dp)),
        "kills_weighted_per_hour": round(kills_weighted / hours, 2) if hours > 0 else 0.0,
        "drops_weighted_per_hour": round(drops_weighted / hours, 2) if hours > 0 else 0.0,
        "kills_by_rarity_per_hour": [per_hour(v) for v in dk],
        "drops_by_rarity_per_hour": [per_hour(v) for v in dp],
    }


def format_report(summary, label):
    lines = []
    lines.append("=" * 68)
    lines.append(f" 刷怪基线 [{label}]   {summary['elapsed_s'] / 60:.1f} 分钟   "
                 f"{summary['samples']} 次采样")
    lines.append("=" * 68)
    lines.append(f" 击杀 {summary['kills_total']:>8}  ->  {summary['kills_per_hour']:>9}/小时"
                 f"   加权 {summary['kills_weighted_per_hour']}/小时")
    lines.append(f" 掉落 {summary['drops_total']:>8}  ->  {summary['drops_per_hour']:>9}/小时"
                 f"   加权 {summary['drops_weighted_per_hour']}/小时")
    lines.append("")
    lines.append(f" {'稀有度':<8}{'击杀':>10}{'/小时':>12}{'掉落':>10}{'/小时':>12}")
    for i, name in enumerate(RARITY_NAMES):
        k = summary["kills_by_rarity"][i] if i < len(summary["kills_by_rarity"]) else "—"
        kh = (summary["kills_by_rarity_per_hour"][i]
              if i < len(summary["kills_by_rarity_per_hour"]) else "—")
        d = summary["drops_by_rarity"][i] if i < len(summary["drops_by_rarity"]) else "—"
        dh = (summary["drops_by_rarity_per_hour"][i]
              if i < len(summary["drops_by_rarity_per_hour"]) else "—")
        lines.append(f" {name:<8}{k:>10}{kh:>12}{d:>10}{dh:>12}")
    lines.append("")
    lines.append(" 注: 击杀 8 档(普通..超神), 掉落 10 档 —— 两张表布局本来就不一样;")
    lines.append("     加权用 (档位+1)/10, 跟 RL 奖励设计一致。")
    lines.append("=" * 68)
    return "\n".join(lines)


def setup(scan_timeout):
    src = Path(__file__).with_name("florr_wasm_probe.js").read_text(encoding="utf-8")
    cdp_bridge.eval_js(src, timeout=10)
    val("window.__florrWasmProbe.scan()")
    deadline = time.time() + scan_timeout
    while time.time() < deadline:
        st = val("window.__florrWasmProbe")
        if (st or {}).get("status") == "ready":
            return st
        if (st or {}).get("status") == "error":
            raise RuntimeError(f"探针扫描失败: {st.get('reason')}")
        time.sleep(0.5)
    raise RuntimeError("探针扫描超时")


def main(argv=None):
    ap = argparse.ArgumentParser(description="量刷怪吞吐: kills/hour + drops/hour")
    ap.add_argument("--minutes", type=float, default=120, help="跑多久, 默认 120 分钟")
    ap.add_argument("--interval", type=float, default=60, help="几秒采一次, 默认 60")
    ap.add_argument("--label", default="rules", help="这次跑的是什么模式(rules / rl / ...)")
    ap.add_argument("--json", metavar="PATH", help="把采样和汇总写成 JSON")
    ap.add_argument("--scan-timeout", type=int, default=90)
    args = ap.parse_args(argv)

    if not cdp_bridge.is_cdp_port_reachable():
        print("❌ CDP 端口不通 —— Chrome 要用那三个启动参数起")
        return 2

    print("注入探针并扫描 client.wasm…")
    st = setup(args.scan_timeout)
    print(f"✅ 就绪。每 {args.interval:.0f} 秒采一次, 计划跑 {args.minutes:.0f} 分钟。"
          f"Ctrl+C 可随时结束并出报告。\n")

    samples = []
    started = time.time()
    deadline = started + args.minutes * 60
    try:
        while True:
            t = val("window.__florrWasmProbe.readTotals()", timeout=30)
            if not t or t.get("error"):
                print(f"⚠️  读计数失败: {(t or {}).get('error')} —— 跳过这次采样")
            elif t.get("hiNonZero"):
                print(f"❌ u64 高位非零 {t['hiNonZero']} 个 —— 击杀表基址漂了, 数据无意义。"
                      f"florr 多半换了 build, 重跑 debug_wasm_counters.py 重新定位。")
                return 1
            else:
                samples.append({"t": round(time.time() - started, 1),
                                "petals": t["petals"], "kills": t["kills"]})
                if len(samples) >= 2:
                    s = summarize(samples, samples[-1]["t"])
                    print(f"   [{datetime.now():%H:%M:%S}] "
                          f"{samples[-1]['t'] / 60:6.1f} 分钟   "
                          f"击杀 {s['kills_total']:>6} ({s['kills_per_hour']}/h)   "
                          f"掉落 {s['drops_total']:>6} ({s['drops_per_hour']}/h)")
                else:
                    print(f"   [{datetime.now():%H:%M:%S}] 起点已记录")
            if time.time() >= deadline:
                break
            time.sleep(min(args.interval, max(0, deadline - time.time())))
    except KeyboardInterrupt:
        print("\n(手动结束)")

    if len(samples) < 2:
        print("❌ 采样不足两次, 算不出速率")
        return 1

    summary = summarize(samples, samples[-1]["t"])
    print("\n" + format_report(summary, args.label))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"label": args.label, "started": started,
                       "summary": summary, "samples": samples},
                      f, ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
