import json

import florr_server as fsv


def _resp(payload):
    """把一个值包成 cdp_bridge.eval_js 那种返回结构(值会被 JSON.stringify).
    跟 test_florr_settings.py 的 _resp 同一套路。"""
    return {"result": {"result": {"type": "string", "value": json.dumps(payload)}}}


def _reset_biome_cache(monkeypatch):
    monkeypatch.setattr(fsv, "_biome_by_server_id", {})
    monkeypatch.setattr(fsv, "_biome_lookup_fetched_at", 0.0)


def test_ensure_probe_installed_evals_the_probe_js():
    calls = []
    assert fsv.ensure_probe_installed(lambda e: calls.append(e)) is True
    assert calls == [fsv._WS_PROBE_JS]


def test_ensure_probe_installed_returns_false_on_eval_exception():
    def boom(_expr):
        raise RuntimeError("no florr tab")
    assert fsv.ensure_probe_installed(boom) is False


def test_current_server_id_reads_string_through_json_stringify():
    seen = []

    def fake_eval(expr):
        seen.append(expr)
        return _resp("25jl")

    assert fsv.current_server_id(fake_eval) == "25jl"
    assert seen == ["JSON.stringify(window.__florrAutoLastServerId ?? null)"]


def test_current_server_id_returns_none_on_eval_exception():
    def boom(_expr):
        raise RuntimeError("no florr tab")
    assert fsv.current_server_id(boom) is None


def test_current_server_id_returns_none_on_null_or_non_string():
    assert fsv.current_server_id(lambda e: _resp(None)) is None
    assert fsv.current_server_id(lambda e: _resp(42)) is None
    assert fsv.current_server_id(lambda e: _resp({"a": 1})) is None


def test_current_server_id_returns_none_on_malformed_cdp_response():
    assert fsv.current_server_id(lambda e: None) is None
    assert fsv.current_server_id(lambda e: {}) is None
    assert fsv.current_server_id(lambda e: {"result": {"result": {"type": "object"}}}) is None
    assert fsv.current_server_id(
        lambda e: {"result": {"result": {"value": "not json{"}}}) is None
    # 真实的 cdp_bridge.eval_js 只会返回 dict 或抛异常, 不会返回下面这些类型 ——
    # 但本函数的不变量是"对任何可调用的 eval_js 都不抛", 不只是针对
    # cdp_bridge.eval_js 这一种实现, 所以对这种"看似合法"的 truthy 非 dict
    # 返回值也要防住(曾经在这里炸出过 AttributeError).
    assert fsv.current_server_id(lambda e: ["not", "a", "dict"]) is None
    assert fsv.current_server_id(lambda e: "not a dict either") is None
    assert fsv.current_server_id(lambda e: 42) is None


def test_biome_for_server_id_builds_reverse_lookup_from_fetch_server_ids(monkeypatch):
    # 服务器短码本身不含生态区信息(2026-09-20 实测确认, 见模块文档) —— 唯一
    # 能知道某个短码属于哪个生态区的办法就是反查 server_lookup.fetch_server_ids().
    _reset_biome_cache(monkeypatch)
    fake_pools = {
        "garden": ["25jk", "25jl"],
        "desert": ["25jn", "25jo"],
        "ocean": ["25jq"],
        "jungle": [],
        "ant_hell": ["25nz"],
        "hel": [],
        "sewers": [],
    }
    monkeypatch.setattr(fsv.server_lookup, "fetch_server_ids",
                         lambda biome, timeout=5: fake_pools[biome])
    assert fsv.biome_for_server_id("25jl") == "garden"
    assert fsv.biome_for_server_id("25jo") == "desert"
    assert fsv.biome_for_server_id("25nz") == "ant_hell"


def test_biome_for_server_id_unknown_id_returns_none(monkeypatch):
    _reset_biome_cache(monkeypatch)
    monkeypatch.setattr(fsv.server_lookup, "fetch_server_ids",
                         lambda biome, timeout=5: ["25jl"] if biome == "garden" else [])
    assert fsv.biome_for_server_id("nope") is None


def test_biome_for_server_id_returns_none_on_junk():
    for junk in (None, "", 4, {"id": "25jl"}):
        assert fsv.biome_for_server_id(junk) is None


def test_biome_for_server_id_one_biome_failing_does_not_clear_the_rest(monkeypatch):
    _reset_biome_cache(monkeypatch)

    def flaky_fetch(biome, timeout=5):
        if biome == "ocean":
            raise RuntimeError("endpoint down")
        return {"garden": ["25jl"], "desert": ["25jo"]}.get(biome, [])

    monkeypatch.setattr(fsv.server_lookup, "fetch_server_ids", flaky_fetch)
    assert fsv.biome_for_server_id("25jl") == "garden"
    assert fsv.biome_for_server_id("25jo") == "desert"


def test_biome_for_server_id_uses_cache_within_ttl(monkeypatch):
    _reset_biome_cache(monkeypatch)
    calls = []

    def counting_fetch(biome, timeout=5):
        calls.append(biome)
        return ["25jl"] if biome == "garden" else []

    monkeypatch.setattr(fsv.server_lookup, "fetch_server_ids", counting_fetch)
    assert fsv.biome_for_server_id("25jl") == "garden"
    first_round = len(calls)
    assert first_round > 0
    # 同一个短码, 缓存没过期 -> 不该再打一次接口.
    assert fsv.biome_for_server_id("25jl") == "garden"
    assert len(calls) == first_round


def test_current_map_name_end_to_end(monkeypatch):
    _reset_biome_cache(monkeypatch)
    fake_pools = {
        "garden": ["25jl"], "desert": ["25jo"], "ocean": [],
        "jungle": [], "ant_hell": ["25nz"], "hel": [], "sewers": [],
    }
    monkeypatch.setattr(fsv.server_lookup, "fetch_server_ids",
                         lambda biome, timeout=5: fake_pools[biome])

    assert fsv.current_map_name(lambda e: _resp("25jo")) == "desert"
    assert fsv.current_map_name(lambda e: _resp("25jl")) == "garden"
    # ant_hell 生态区 key 跟寻路图文件名不一致(接口 key 是 ant_hell, 图叫 anthell).
    assert fsv.current_map_name(lambda e: _resp("25nz")) == "anthell"


def test_current_map_name_is_none_for_biomes_without_a_pathing_map(monkeypatch):
    # jungle/hel/sewers 没有 maps/*.png, 读到也没法用 -> None, 调用方退回自己的
    # 阶段推断.
    _reset_biome_cache(monkeypatch)
    fake_pools = {
        "garden": [], "desert": [], "ocean": [],
        "jungle": ["25a1"], "ant_hell": [], "hel": ["25a2"], "sewers": ["25a3"],
    }
    monkeypatch.setattr(fsv.server_lookup, "fetch_server_ids",
                         lambda biome, timeout=5: fake_pools[biome])
    for sid in ("25a1", "25a2", "25a3"):
        assert fsv.current_map_name(lambda e, sid=sid: _resp(sid)) is None


def test_current_map_name_is_none_when_unreadable(monkeypatch):
    # 探针没装 / CDP 出错 —— 整条链路必须安静地返回 None, 不抛.
    _reset_biome_cache(monkeypatch)
    assert fsv.current_map_name(lambda e: (_ for _ in ()).throw(RuntimeError("x"))) is None
