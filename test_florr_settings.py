import json

import pytest

import florr_settings as fs


def _resp(payload):
    """把一个 dict 包成 cdp_bridge.eval_js 那种返回结构(payload 会被 JSON.stringify)."""
    return {"result": {"result": {"type": "string", "value": json.dumps(payload)}}}


def test_addresses_are_calibrated_constants():
    assert fs.INVERT_ATTACK_ADDR == 0x53430E
    assert fs.INVERT_DEFENSE_ADDR == 0x534310


def test_addr_none_does_not_call_eval():
    calls = []
    assert fs.ensure_flag(lambda e: calls.append(e), None, 1) == ("failed", "addr-not-calibrated")
    assert calls == []


def test_changed_when_before_differs_from_want():
    # want=1, before=0 -> 写 1
    resp = _resp({"ok": True, "before": 0, "after": 1})
    assert fs.ensure_flag(lambda e: resp, 0xAD1234, 1) == ("changed", "")
    # want=0, before=1 -> 写 0
    resp0 = _resp({"ok": True, "before": 1, "after": 0})
    assert fs.ensure_flag(lambda e: resp0, 0xAD1234, 0) == ("changed", "")


def test_unchanged_when_before_equals_want():
    assert fs.ensure_flag(lambda e: _resp({"ok": True, "before": 1, "after": 1}), 1, 1) == ("unchanged", "")
    assert fs.ensure_flag(lambda e: _resp({"ok": True, "before": 0, "after": 0}), 1, 0) == ("unchanged", "")


@pytest.mark.parametrize("reason", ["no-wasm-memory", "addr-out-of-range", "not-bool:7"])
def test_failed_passes_through_js_reason(reason):
    assert fs.ensure_flag(lambda e: _resp({"ok": False, "reason": reason}), 1, 1) == ("failed", reason)


def test_failed_on_eval_exception():
    def boom(_e):
        raise RuntimeError("no florr tab")
    assert fs.ensure_flag(boom, 1, 1) == ("failed", "cdp-error:no florr tab")


def test_failed_when_no_value_in_response():
    resp = {"result": {"result": {"type": "object", "className": "Object"}}}
    assert fs.ensure_flag(lambda e: resp, 1, 1) == ("failed", "no-value")


def test_failed_on_bad_json():
    resp = {"result": {"result": {"type": "string", "value": "not json{"}}}
    assert fs.ensure_flag(lambda e: resp, 1, 1) == ("failed", "bad-json")


def test_failed_on_none_response():
    assert fs.ensure_flag(lambda e: None, 1, 1) == ("failed", "no-value")


def test_js_embeds_decimal_addr_and_want_and_is_stringify_iife():
    js = fs._js(0x1234, 0)
    assert "const A = 4660" in js
    assert "const W = 0" in js
    assert js.startswith("JSON.stringify((() => {")
    assert js.rstrip().endswith("})())")
    js1 = fs._js(0x1234, 1)
    assert "const W = 1" in js1
