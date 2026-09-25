"""worker 启动 / 每轮按 config 把 florr.io 的两个「反转」控制键强制成 1(开)或 0(关):
  设置→控制→反转攻击键  (Invert attack button)  —— 字节 INVERT_ATTACK_ADDR
  设置→控制→反转防御键  (Invert defend button)  —— 字节 INVERT_DEFENSE_ADDR

bot 自己不按攻击键, 靠「反转攻击键」让 florr 持续攻击 —— 关着的话 bot 到位、绕圈、
寻路全正常, 但一点伤害都不出. 「反转防御键」是可选的对称项.

机制: florr 是 Emscripten/WASM, 这两个设置在运行时各是静态数据段里一个 bool 字节,
游戏每帧读它. 同一个 florr build 里地址稳定; florr 发新 build 才漂移. 地址靠仓库根
的 settings_finder.js 找出来填到下面的常量.

不 import 任何 GUI 库、不 import main. 只通过参数拿 eval_js (= cdp_bridge.eval_js).
"""
import json

# settings_finder.js 找出来的地址 (INVERT_ATTACK_ADDR: 2026-09-02 中文客户端上标定;
# INVERT_DEFENSE_ADDR: 用户提供, 未二次核对). florr 大版本更新后可能失效 —— worker
# 日志报 addr-out-of-range / not-bool 时重跑 settings_finder.js 求新值填这里.
# 设回 None = 该项静默降级成"只警告".
INVERT_ATTACK_ADDR = 0x53430E
INVERT_DEFENSE_ADDR = 0x534310


_JS_TEMPLATE = r"""JSON.stringify((() => {{
  const M = window.Module;
  const mem = M && (
    (M.asm && M.asm.memory && M.asm.memory.buffer) ||
    (M.wasmMemory && M.wasmMemory.buffer) ||
    (M.HEAPU8 && M.HEAPU8.buffer) ||
    (M.asm && M.asm.Mf && M.asm.Mf.buffer)
  );
  if (!mem) return {{ok: false, reason: "no-wasm-memory"}};
  const u8 = new Uint8Array(mem);
  const A = {addr};
  const W = {want};
  if (A < 0 || A >= u8.length) return {{ok: false, reason: "addr-out-of-range"}};
  const before = u8[A];
  if (before > 1) return {{ok: false, reason: "not-bool:" + before}};
  if (before !== W) u8[A] = W;
  return {{ok: true, before: before, after: u8[A]}};
}})())"""


def _js(addr, want):
    return _JS_TEMPLATE.format(addr=addr, want=want)


def ensure_flag(eval_js, addr, want):
    """把 florr 某个 bool 字节强制成 want(0/1). 返回 (status, detail):
      "unchanged" —— 字节本来就等于 want
      "changed"   —— 本来不等, 已写成 want
      "failed"    —— 没能确认(detail = 原因), 调用方该警告但别中断 worker

    eval_js: cdp_bridge.eval_js 那种签名的函数(expression -> CDP Runtime.evaluate
             原始返回 dict). addr: None 时直接 failed, 不调 eval_js. want: 0 或 1.
    """
    if addr is None:
        return ("failed", "addr-not-calibrated")
    try:
        resp = eval_js(_js(addr, want))
    except Exception as e:
        return ("failed", f"cdp-error:{e}")
    inner = (resp or {}).get("result", {}).get("result", {})
    if "value" not in inner:
        return ("failed", "no-value")
    try:
        data = json.loads(inner["value"])
    except (ValueError, TypeError):
        return ("failed", "bad-json")
    if not data.get("ok"):
        return ("failed", data.get("reason", "unknown"))
    return ("unchanged" if data.get("before") == want else "changed", "")
