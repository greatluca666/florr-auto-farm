"""从 florr 客户端读"当前连的是哪台服务器", 进而知道"人现在在哪张图".

为什么需要: 蚁穴路线(map_routes.py)要区分"还在花园"和"已经进蚁穴了"。踩洞口
传送是游戏自己做的, 我们没下过任何指令, 除了服务器码没有别的信号。

机制(2026-09-20 在真实 florr.io 客户端上验证过, 取代了早前一版从没在活客户端上
验证过的设计):

  1. ensure_probe_installed() 往 florr.io 标签页装一次 WebSocket 构造拦截 ——
     每次(重)连接, 把连接目标 URL 的 hostname(形如 "25jl.s.m28n.net")记到
     window.__florrAutoLastServerId, 记的是那个点号前的短码("25jl")。
  2. current_server_id() 读这个全局变量。
  3. biome_for_server_id() 把这个短码翻成生态区 —— **短码本身不含生态区信息**
     (早前 SERVER_ID_EXPR/map_index_from_server_id 那版以为服务器码长得像
     "florrio-map-4-green-vultr-tokyo"、中间数字就是生态区 index, 这其实是把
     server_lookup.M28_ENDPOINT_TEMPLATE 那个**查询接口路径**的格式误当成了
     服务器*码*本身的格式 —— 实测这两者完全不是一回事, 真实短码是数据中心分配
     的不透明code、如"25jl"/"25oa", 不含任何生态区数字。导致那一版无论
     SERVER_ID_EXPR 标不标定, 识别链路在真机上都必然全程返回 None, 这也是
     2026-09-20 用户反馈"无法正确识别到目前处于那个生态区"的根因)。
     唯一能知道某个短码属于哪个生态区的办法就是反过来查一遍
     server_lookup.fetch_server_ids() —— 查一次, 建张 id -> biome 的表, 缓存
     _BIOME_LOOKUP_TTL 秒(服务器池不是秒级变化的东西, 不用每次都打接口)。

本模块的所有函数**任何情况下都不抛异常**, 读不到一律 None(或 False) —— 调用方
(main 的进场路线)有自己的阶段状态变量兜底, 不能因为读个服务器号把 worker 撂倒。

不 import cdp_bridge / main: 只通过参数拿 eval_js, 跟 florr_settings.py 一样。
"""
import json
import time

import server_lookup

# 往 window.WebSocket 上装一次构造拦截, 记下每次(重)连接目标的服务器短码。
# 为什么拦 WebSocket 构造而不是找一个现成的可读属性: 探过 —— cp6(已知的写口,
# switch_server() 在用)只暴露 forceServerID/disconnect/simulateContextLoss 这
# 3 个方法, 没有能读当前服务器码的属性; 对 window 做过深度受限的对象图扫描
# 也没找到(游戏本体的连接对象在打包器闭包作用域里, 不挂在任何全局对象上, 没法
# 从 window 沿属性链摸到)。服务器码唯一确认过的出口就是 `new WebSocket(url)`
# 这个 url 参数本身。用 Proxy 拦 construct 陷阱, 不改 WebSocket 的 prototype
# 链, instanceof WebSocket 之类的判断不受影响。幂等(装过一次就短路), 每轮重装
# 一次是安全的, 跟 florr_settings.ensure_flag() 同一个"每轮重申一次"的用法 ——
# 也正因为不依赖 florr 具体 build 里的某个对象属性名, 比原来那套"靠
# server_id_finder.js 在活客户端上人工探一个属性路径填常量"的方案更抗 florr
# 换 build(florr 换 build 不会不用 WebSocket 连服务器)。
_WS_PROBE_JS = r"""(function() {
  if (window.__florrAutoWsPatched) return true;
  window.__florrAutoWsPatched = true;
  window.__florrAutoLastServerId = window.__florrAutoLastServerId || null;
  const OrigWS = window.WebSocket;
  window.WebSocket = new Proxy(OrigWS, {
    construct(target, args) {
      try {
        const host = new URL(String(args[0]), location.href).hostname;
        const id = host.split(".")[0];
        if (id) window.__florrAutoLastServerId = id;
      } catch (e) {}
      return new target(...args);
    }
  });
  return true;
})()"""

# current_server_id() 读的表达式 —— 固定值, 不再需要像旧版 SERVER_ID_EXPR 那样
# 靠人在活客户端上探一遍填常量(见模块文档)。测试可以传 expr= 覆盖。
SERVER_ID_EXPR = "window.__florrAutoLastServerId"

# id -> biome 反查表的缓存时长。服务器池是按小时/天级别变化的东西(不是每次连接
# 都换), 缓存这么久足够新鲜, 不用每次读服务器号都去打 server_lookup 的接口。
_BIOME_LOOKUP_TTL = 300.0

_biome_by_server_id = {}
_biome_lookup_fetched_at = 0.0


def ensure_probe_installed(eval_js):
    """确保 _WS_PROBE_JS 已经装到当前 florr.io 标签页上. 幂等, 可以每轮都调.

    返回 True/False(装没装成功), 不抛异常 —— eval_js 出错(没有 florr.io 标签页
    / CDP 掉线)一律吞掉返回 False, 调用方(main 每轮的重申逻辑)不该因为这个把
    worker 撂倒。
    """
    try:
        eval_js(_WS_PROBE_JS)
        return True
    except Exception:
        return False


def current_server_id(eval_js, expr=None):
    """当前连的服务器短码(如 "25jl"). 探针没装 / CDP 出错 / 拿到的不是字符串
    -> None(不抛)。

    eval_js: cdp_bridge.eval_js 那种签名(expression -> CDP Runtime.evaluate 的
        原始返回 dict)。走 JSON.stringify 再在 Python 侧 json.loads, 免得去猜
        CDP 对各种 JS 类型的序列化差异 —— 跟 florr_settings 同一个套路。
    expr: 覆盖 SERVER_ID_EXPR(测试用)。
    """
    expr = expr if expr is not None else SERVER_ID_EXPR
    if not expr:
        return None
    try:
        resp = eval_js(f"JSON.stringify({expr} ?? null)")
    except Exception:
        return None
    # 这里的不变量是"对任何可调用的 eval_js 都不抛"——不只是针对
    # cdp_bridge.eval_js(它的约定是要么抛异常要么返回 dict), 还包括测试/未来
    # 调用方传进来的、返回值类型对不上的实现。逐层 isinstance 检查, 不能假设
    # resp / 它的 "result" 字段一定是 dict, 否则一个类型不对的返回值会在这里
    # 把 AttributeError 捅出去。
    result = resp.get("result", {}) if isinstance(resp, dict) else {}
    inner = result.get("result", {}) if isinstance(result, dict) else {}
    if not isinstance(inner, dict) or "value" not in inner:
        return None
    try:
        value = json.loads(inner["value"])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _refresh_biome_lookup(timeout=5):
    """查一遍 server_lookup.fetch_server_ids() 的每个生态区, 重建 id -> biome
    反查表。单个生态区查询失败(网络问题/接口挂了)不连累其它生态区 —— 能查到
    几个算几个, 不因为一个查询失败就把整张表清空。不抛异常。"""
    global _biome_lookup_fetched_at
    table = {}
    for biome in server_lookup.BIOME_INDEX:
        try:
            for server_id in server_lookup.fetch_server_ids(biome, timeout=timeout):
                table[server_id] = biome
        except Exception:
            continue
    _biome_by_server_id.clear()
    _biome_by_server_id.update(table)
    _biome_lookup_fetched_at = time.time()


def biome_for_server_id(server_id, timeout=5):
    """服务器短码 -> 生态区 key。查不到 -> None(不抛)。

    缓存里没有这个短码, 或者缓存已经超过 _BIOME_LOOKUP_TTL 秒没刷新过, 就重查
    一遍 _refresh_biome_lookup(); 刷新后仍查不到就是真查不到, 直接 None ——
    不会因为一个陌生短码就无限重查接口(刷新时间戳只要刷新过就更新, 不管这次
    刷新有没有命中这个短码)。
    """
    if not isinstance(server_id, str) or not server_id:
        return None
    stale = (time.time() - _biome_lookup_fetched_at) > _BIOME_LOOKUP_TTL
    if stale or server_id not in _biome_by_server_id:
        _refresh_biome_lookup(timeout=timeout)
    return _biome_by_server_id.get(server_id)


def current_map_name(eval_js):
    """人现在在 maps/<name>.png 的哪一张。读不出来 -> None, 调用方自己兜底。"""
    biome = biome_for_server_id(current_server_id(eval_js))
    if biome is None:
        return None
    return server_lookup.map_name_for_biome(biome)
