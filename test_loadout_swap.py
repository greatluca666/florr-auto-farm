import pytest

import loadout_swap


def _rig():
    """记录 press / key_down / key_up 的调用顺序到一个共享列表."""
    ev = []
    return ev, {
        "press": lambda k: ev.append(("press", k)),
        "key_down": lambda k: ev.append(("down", k)),
        "key_up": lambda k: ev.append(("up", k)),
    }


@pytest.mark.parametrize("cfg", [
    None, "k", 3, [],
    {},                                           # 缺 enabled
    {"enabled": False, "mod": "k", "digit": "3"}, # 关
    {"enabled": 0, "mod": "none", "digit": "1"},  # falsy enabled
])
def test_disabled_or_junk_does_nothing(cfg):
    ev, io = _rig()
    loadout_swap.press_swap(cfg, **io)
    assert ev == []


def test_enabled_no_mod_presses_digit_only():
    ev, io = _rig()
    loadout_swap.press_swap({"enabled": True, "mod": "none", "digit": "5"}, **io)
    assert ev == [("press", "5")]


@pytest.mark.parametrize("mod", ["k", "l"])
def test_enabled_with_mod_is_a_chord(mod):
    ev, io = _rig()
    loadout_swap.press_swap({"enabled": True, "mod": mod, "digit": "3"}, **io)
    assert ev == [("down", mod), ("press", "3"), ("up", mod)]


def test_mod_key_released_even_if_press_raises():
    ev = []

    def boom(_k):
        ev.append(("press", _k))
        raise RuntimeError("no focus")

    loadout_swap.press_swap(
        {"enabled": True, "mod": "k", "digit": "7"},
        press=boom,
        key_down=lambda k: ev.append(("down", k)),
        key_up=lambda k: ev.append(("up", k)),
    )   # 不外抛
    assert ev == [("down", "k"), ("press", "7"), ("up", "k")]


@pytest.mark.parametrize("digit", ["x", "", "12", None, 3])
def test_invalid_digit_is_noop_with_warning(digit, capsys):
    ev, io = _rig()
    loadout_swap.press_swap({"enabled": True, "mod": "none", "digit": digit}, **io)
    assert ev == []
    out = capsys.readouterr().out
    assert out.startswith("⚠️")
    assert "数字" in out


def test_unknown_mod_treated_as_none():
    ev, io = _rig()
    loadout_swap.press_swap({"enabled": True, "mod": "ctrl", "digit": "9"}, **io)
    assert ev == [("press", "9")]


def test_press_exception_is_swallowed(capsys):
    def boom(_k):
        raise RuntimeError("no focus")
    loadout_swap.press_swap({"enabled": True, "mod": "none", "digit": "1"},
                            press=boom, key_down=lambda k: None, key_up=lambda k: None)
    out = capsys.readouterr().out
    assert "装备切换按键失败" in out
    assert out.startswith("⚠️")


def test_digit_zero_is_valid():
    ev, io = _rig()
    loadout_swap.press_swap({"enabled": True, "mod": "l", "digit": "0"}, **io)
    assert ev == [("down", "l"), ("press", "0"), ("up", "l")]
