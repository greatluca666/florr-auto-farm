"""诊断: canvas 解码到底从画面里读出了什么怪.

用途 —— "识别不到 msandstorm / usandstorm" 这类问题, 先跑这个看 canvas_decode
到底给了什么, 再决定改 _species_from_name / _tier_from_color / 还是 hook。

跑法 (florr.io 已在那个带 --remote-debugging-port=9222 的 Chrome 里打开, 画面里
有目标怪):

    python debug_canvas_enemies.py

它会连续 drain 几秒, 取最新一帧完整记录, 打印:
  1. 这一帧的原始统计 (记录数 / 帧号跨度 / 血条锚点数 / 文本记录数)
  2. 每个血条块 (nameplate block): anchor / hp / 所有文本 + 每条文本的 fill 色
  3. mobs_from_frame 的结果: name / rarity / rarity_color / hp / 屏幕坐标
  4. 每个 mob 过 _species_from_name / _tier_from_color 之后变成什么
  5. scan_enemies() 最终返回的检测列表
  6. 找自己花身的证据 (候选花身 / HUD tint / NNN级 牌 / 裸血条周围的圆) —— 相机认错自己时看
不改游戏, 只读。
"""
import sys
import time

import cdp_bridge
import canvas_decode
import enemy_detect
import utils

# e81b404 给 _species_from_name 加了按图分流(MAP_SPECIES[utils.MAP]) —— main.py
# 跑起来时 apply_map() 会设好 utils.MAP, 但这个脚本是独立跑的, 不设就一直是
# utils.MAP == ""(默认值), MAP_SPECIES.get("", frozenset()) 是空集合, 结果不管
# 画面上是什么怪, _species_from_name 一律返回 None —— 连甲虫蝎子这些一直没问题
# 的怪都会显示"检测不到", 是这个脚本没跟上那次改动, 不是解码坏了。
# 用法: python debug_canvas_enemies.py [地图名, 默认 desert] [--dump 文件名]
#   --dump 会把这一帧的原始绘制记录存成 JSON, 可以拿去离线复现(解码出问题时必备)
_MAP = "desert"
_DUMP = None
_args = sys.argv[1:]
if "--dump" in _args:
    i = _args.index("--dump")
    _DUMP = _args[i + 1] if i + 1 < len(_args) else "canvas_frame_dump.json"
    del _args[i:i + 2]
if _args:
    _MAP = _args[0]


def _drain_for(seconds=3.0):
    """反复 drain, 把这段时间内的所有绘制记录攒起来。"""
    buf = []
    try:
        cdp_bridge.inject_canvas_hook()
    except RuntimeError as e:
        print(f"⚠️ inject_canvas_hook 抛错: {e}")
        print("   (hook 没装上 —— 后面基本会是空的; 先重进游戏再跑)")
    end = time.time() + seconds
    while time.time() < end:
        try:
            buf.extend(cdp_bridge.drain_canvas_log())
        except Exception as e:  # noqa: BLE001  诊断脚本, 什么都想看见
            print(f"⚠️ drain_canvas_log 抛错: {type(e).__name__}: {e}")
            break
        time.sleep(0.1)
    return buf


def _dump_self_evidence(recs):
    """相机认错自己(2026-09-27 蚁穴: 严格模式返回了另一个玩家的位置)时用的证据:
    camera_from_frame 找自己花身时看到的候选、HUD 头像色、每个候选近旁有没有别人的
    "NNN级" 牌, 以及没有名牌的裸血条(自己的血条)周围到底画着什么圆。"""
    zoom = next((r["m"][0] for r in recs
                 if r["op"] == "stroke" and r.get("stroke") == canvas_decode.HEALTHBAR_BG
                 and not canvas_decode._is_minimap(r)), None)
    tint = canvas_decode.hud_self_colour(recs)
    print(f"  zoom={zoom}  HUD 头像色 tint={tint!r}")

    lvl = [canvas_decode._anchor(r) for r in recs
           if r["op"] == "text"
           and canvas_decode.PLAYER_RARITY_PATTERN.match(str(r.get("text", "")))]
    print(f"  屏幕上的 NNN级 牌锚点 ({len(lvl)}): "
          f"{[(round(x), round(y)) for x, y in lvl]}")

    bare = [b for b in canvas_decode._bar_blocks(recs) if not b["texts"]]
    print(f"  无名牌文字的裸血条 ({len(bare)}): "
          f"{[(round(b['anchor'][0]), round(b['anchor'][1]), 'shield' if b['secondary'] else '-') for b in bare]}")

    if zoom is None:
        print("  没有 zoom, 后面的候选筛选跑不了")
        return
    print("  候选花身 (跟 camera_from_frame 同一套筛选: 金色/同 tint 的圆, 缩放==zoom):")
    n = 0
    for r in recs:
        if not (r["op"] == "fill" and r.get("r") is not None
                and (r.get("fill") == tint or canvas_decode._is_self_body_color(r.get("fill")))
                and not canvas_decode._is_minimap(r)
                and (r.get("fill") == tint or not canvas_decode._is_rotated(r))
                and abs(canvas_decode._scale(r) - zoom) < 1e-6):
            continue
        n += 1
        ax, ay = canvas_decode._anchor(r)
        near = [round(((lx - ax) ** 2 + (ly - ay) ** 2) ** 0.5)
                for lx, ly in lvl
                if ((lx - ax) ** 2 + (ly - ay) ** 2) ** 0.5 <= canvas_decode.SELF_DISAMBIGUATION_RADIUS]
        print(f"    #{n}: anchor=({ax:.0f},{ay:.0f}) r={r['r']:.1f} fill={r.get('fill')!r} "
              f"同tint={r.get('fill') == tint}  近旁NNN级牌距离={near or '无'}")
    if n == 0:
        print("    (一个都没有)")

    # 自己的裸血条在画布中心附近 (窗口化 y=472.5, 全屏 y=540), 优先看离中心最近的几条。
    def _center_dist(b):
        return min(((b["anchor"][0] - 960) ** 2 + (b["anchor"][1] - cy) ** 2) ** 0.5
                   for cy in (472.5, 540.0))
    for b in sorted(bare, key=_center_dist)[:3]:
        bx, by = b["anchor"]
        print(f"  裸血条 ({bx:.0f},{by:.0f}) 60px 内的圆形填充:")
        seen = 0
        for r in recs:
            if r["op"] != "fill" or r.get("r") is None or canvas_decode._is_minimap(r):
                continue
            ax, ay = canvas_decode._anchor(r)
            if ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 > 60:
                continue
            seen += 1
            if seen > 12:
                print("      ...")
                break
            print(f"      fill={r.get('fill')!r:10} r={r['r']:.1f} scale={canvas_decode._scale(r):.4f} "
                  f"rotated={canvas_decode._is_rotated(r)}  anchor=({ax:.0f},{ay:.0f})")


def main():
    utils.apply_map(_MAP)
    print(f"utils.MAP = {utils.MAP!r}  (物种表: {sorted(enemy_detect.MAP_SPECIES.get(utils.MAP, ()))})")

    raw = _drain_for(3.0)
    print(f"\n=== 原始 drain: {len(raw)} 条记录 ===")
    if not raw:
        print("空。hook 没生效, 或者画面里什么都没画 (florr 标签页在后台?)。")
        return

    frames = canvas_decode.group_by_frame(raw)
    keys = sorted(frames)
    print(f"帧号: {len(keys)} 个不同值, 范围 {keys[0]}..{keys[-1]}")
    if len(keys) < 2:
        print("⚠️ 少于 2 个不同帧号 —— __canvasFrame 没在推进 (florr 抓着 patch 之前的")
        print("   requestAnimationFrame 引用)。canvas 解码这时候永远是空的。重进游戏。")
        return

    recs = frames[keys[-2]]  # 最新那帧可能没画完, 取次新

    if _DUMP:
        # 把这一帧的原始绘制记录原样存下来 —— 离线复现/回归用。解码出问题时(比如
        # "神话怪识别不到"), 光看打印出来的结论没法重跑, 有了原始帧就能在 Mac 上反复
        # 调试到根因, 不用一次次占着真实客户端试。
        import json
        with open(_DUMP, "w", encoding="utf-8") as f:
            json.dump({"map": _MAP, "frame": keys[-2], "raw": recs}, f, ensure_ascii=False)
        print(f"\n💾 已把这一帧 {len(recs)} 条原始记录写到 {_DUMP}")
    ops = {}
    texts = []
    for r in recs:
        ops[r.get("op")] = ops.get(r.get("op"), 0) + 1
        if r.get("op") == "text":
            texts.append((r.get("text"), r.get("fill")))
    print(f"\n=== 次新帧 (frame {keys[-2]}): {len(recs)} 条 ===")
    print(f"op 分布: {ops}")

    # 原始交错序列: 想看 florr 到底怎么排一个 nameplate 的 血条 vs 名字/稀有度,
    # 中间夹没夹别的 (fill 粒子 / 别的怪的血条). _bar_blocks 靠"血条后面紧跟文本"
    # 这个假设取名字 —— 中间夹一条 fill 就丢整块名字。这段就是拿证据的。
    print("\n=== 原始交错序列 (只截前 120 条; s=stroke t=text f=fill, xy=CTM锚点) ===")
    HB = {"#222222", "#DD3434", "#42E3F5"}
    for k, r in enumerate(recs[:120]):
        op = r.get("op")
        ax, ay = (r.get("m") or [0, 0, 0, 0, 0, 0])[4:6]
        if op == "text":
            print(f"  {k:3} t  ({ax:6.0f},{ay:6.0f})  {r.get('text')!r:20} fill={r.get('fill')!r}")
        elif op == "stroke":
            col = r.get("stroke")
            tag = "HP-BAR" if col in HB else "value/other"
            print(f"  {k:3} s  ({ax:6.0f},{ay:6.0f})  stroke={col!r:10} {tag}")
        else:
            print(f"  {k:3} f  ({ax:6.0f},{ay:6.0f})  fill={r.get('fill')!r:10} r={r.get('r')}")

    print(f"\n文本记录 {len(texts)} 条:")
    for t, fill in texts:
        print(f"    text={t!r:24}  fill={fill!r}")

    print("\n=== _bar_blocks (nameplate 块) ===")
    blocks = canvas_decode._bar_blocks(recs)
    if not blocks:
        print("一个都没有。画面里没有带 #222222 血条的怪 —— 或者 sandstorm 根本不画")
        print("标准 nameplate (那样 canvas 解码结构上就看不到它, 得走 body 特征那条路)。")
    for i, b in enumerate(blocks):
        print(f"  块#{i}: anchor=({b['anchor'][0]:.0f},{b['anchor'][1]:.0f}) hp={b['hp']}")
        for t, c in zip(b["texts"], b["text_colors"]):
            print(f"        text={t!r:24}  fill={c!r}")

    print("\n=== camera_from_frame ===")
    strict_ok = False
    try:
        cam = canvas_decode.camera_from_frame(recs)
        strict_ok = True
        print(f"  严格: zoom={cam['zoom']:.4f}  player_world={cam['player_world']}  "
              f"player_screen={cam['player_screen']}")
    except ValueError as e:
        print(f"  严格: ⚠️ {e}")
        menu_hits = [t for t, _ in texts if t in ("设置", "图像", "控制", "致谢",
                                                  "反转攻击控制", "使用键盘移动")]
        if "player_screen" in str(e) and menu_hits:
            print("  ★ 画面里有设置菜单文本", menu_hits, "—— 设置面板打开时 florr 不画花本体。关掉再跑。")

    print("\n=== 找自己花身的证据 ===")
    _dump_self_evidence(recs)

    # scan_enemies 走的是 best_effort —— 严格解不出也能兜底. 用这个跑后面的映射.
    try:
        cam = canvas_decode.camera_from_frame(recs, best_effort=True)
        print(f"  best_effort: player_screen={cam['player_screen']}  approx={cam.get('approx')}")
    except ValueError as e:
        print(f"  best_effort: ⚠️ 也解不出: {e}  —— 这一帧 scan_enemies 会返回 []")
        return

    print("\n=== mobs_from_frame -> 映射 ===")
    mobs = canvas_decode.mobs_from_frame(recs, cam)
    if not mobs:
        print("  空。有 nameplate 块但都被当成玩家自己 / 别的玩家过滤掉了, 或者没有块。")
    for m in mobs:
        sp = enemy_detect._species_from_name(m.get("name"))
        tier = enemy_detect._tier_from_color(m.get("rarity_color"))
        print(f"  name={m.get('name')!r:20} rarity_word={m.get('rarity')!r:12} "
              f"rarity_color={m.get('rarity_color')!r:10} hp={m.get('hp')}")
        print(f"      -> _species_from_name={sp!r}   _tier_from_color={tier!r}   "
              f"screen=({m['sx']:.0f},{m['sy']:.0f})")
        if sp is None:
            print("      ✗ species=None -> 这个 mob 被 scan_enemies 丢掉")
        elif tier == "Common" and m.get("rarity_color") not in (None, "#7EEF6D"):
            print(f"      ✗ rarity_color {m.get('rarity_color')!r} 不在 _RANK_BY_RARITY_COLOR 里 "
                  f"-> 当成 Common")

    print("\n=== enemy_detect.scan_enemies() 最终结果 ===")
    enemy_detect._frame_buffer[:] = []
    dets = enemy_detect.scan_enemies()
    if not dets:
        print("  []  (scan_enemies 没返回任何检测)")
    for d in dets:
        print(f"  {d}")


if __name__ == "__main__":
    main()
