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


# ── 装备基准纪元: RL 的"按了几次 R"奇偶靠它归零 ─────────────────────────────

def test_an_actual_press_starts_a_new_loadout_epoch():
    """按下一套预设 = 花瓣栏回到那套存档的样子 —— R 翻过几次都作废。RL 环境看到纪元
    变了就把奇偶归零; 不看这个的话, 它只能在每次 reset 时一律归零, 而 AFK/步数上限/
    短途拉回这些 reset 根本没碰过装备, 奇偶就错了。"""
    _, io = _rig()
    before = loadout_swap.baseline_epoch()
    loadout_swap.press_swap({"enabled": True, "mod": "k", "digit": "2"}, **io)
    assert loadout_swap.baseline_epoch() == before + 1


@pytest.mark.parametrize("cfg", [
    None,
    {"enabled": False, "mod": "k", "digit": "3"},
    {"enabled": True, "mod": "none", "digit": "x"},
])
def test_nothing_pressed_means_the_epoch_does_not_move(cfg):
    _, io = _rig()
    before = loadout_swap.baseline_epoch()
    loadout_swap.press_swap(cfg, **io)
    assert loadout_swap.baseline_epoch() == before


def test_a_failed_press_does_not_claim_a_new_epoch():
    """键没按出去, 花瓣栏就没变 —— 归零的话奇偶反而错。"""
    def boom(_k):
        raise RuntimeError("no focus")
    before = loadout_swap.baseline_epoch()
    loadout_swap.press_swap({"enabled": True, "mod": "none", "digit": "1"},
                            press=boom, key_down=lambda k: None, key_up=lambda k: None)
    assert loadout_swap.baseline_epoch() == before


def test_reentering_the_game_starts_a_new_epoch():
    """死了重进 / 从标题页进 florr 会把 loadout 重置(用户实机确认) —— 就算时块没配
    预设和弦、一个键都没按, 花瓣栏也回到了基准。"""
    before = loadout_swap.baseline_epoch()
    loadout_swap.mark_baseline()
    assert loadout_swap.baseline_epoch() == before + 1
