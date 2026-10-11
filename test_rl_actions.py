"""人类操作 -> 动作 的换算测试。纯函数, 不碰 CDP。"""
import pytest

from rl_actions import (
    ATTACK_KEY, DEFENSE_KEY, MOVE_NONE, action_from_state, apply_events,
    count_swap_presses, move_index_from_keys, move_index_from_mouse, normalize_key,
)


def down(key="", code=""):
    return {"type": "down", "key": key, "code": code}


def up(key="", code=""):
    return {"type": "up", "key": key, "code": code}


# ── 键名归一 ─────────────────────────────────────────────────────────────────

def test_code_wins_over_key():
    """key 在中文输入法/异布局下不可靠, code 是物理键位。"""
    assert normalize_key(" w", "KeyW") == "w"
    assert normalize_key("不相干", "KeyD") == "d"


def test_space_and_shift_normalise():
    assert normalize_key(" ", "Space") == ATTACK_KEY
    assert normalize_key("Shift", "ShiftLeft") == DEFENSE_KEY
    assert normalize_key("Shift", "ShiftRight") == DEFENSE_KEY


def test_falls_back_to_key_when_code_missing():
    assert normalize_key("a", "") == "a"
    assert normalize_key(" ", "") == ATTACK_KEY


# ── 按住状态重建 ─────────────────────────────────────────────────────────────

def test_held_keys_survive_a_tick_with_no_events():
    """按住不放跨 tick 时这一 tick 一个事件都没有 —— 不能因此判成松开了。"""
    held = apply_events(set(), [down(code="KeyW")])
    assert held == {"w"}
    assert apply_events(held, []) == {"w"}


def test_keyup_clears():
    held = apply_events({"w", "d"}, [up(code="KeyD")])
    assert held == {"w"}


def test_repeat_downs_are_harmless():
    held = apply_events(set(), [down(code="KeyW"),
                                dict(down(code="KeyW"), repeat=True),
                                dict(down(code="KeyW"), repeat=True)])
    assert held == {"w"}


# ── 方向 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("keys,want", [
    (set(), 0),
    ({"w"}, 1),
    ({"w", "d"}, 2),
    ({"d"}, 3),
    ({"s", "d"}, 4),
    ({"s"}, 5),
    ({"s", "a"}, 6),
    ({"a"}, 7),
    ({"w", "a"}, 8),
])
def test_move_index_from_keys(keys, want):
    assert move_index_from_keys(keys) == want


def test_opposite_keys_cancel():
    """同时按 a 和 d 在 florr 里就是不动, 不该解成某个方向。"""
    assert move_index_from_keys({"a", "d"}) == MOVE_NONE
    assert move_index_from_keys({"w", "s"}) == MOVE_NONE
    assert move_index_from_keys({"a", "d", "w"}) == 1      # 左右抵消, 只剩北


def test_non_direction_keys_ignored():
    assert move_index_from_keys({ATTACK_KEY, DEFENSE_KEY, "r"}) == MOVE_NONE


@pytest.mark.parametrize("dx,dy,want", [
    (0, -100, 1),     # 北 (y 向下增长)
    (100, -100, 2),
    (100, 0, 3),
    (100, 100, 4),
    (0, 100, 5),
    (-100, 100, 6),
    (-100, 0, 7),
    (-100, -100, 8),
])
def test_move_index_from_mouse(dx, dy, want):
    player = (960.0, 540.0)
    mouse = {"x": player[0] + dx, "y": player[1] + dy}
    assert move_index_from_mouse(mouse, player) == want


def test_mouse_deadzone_means_stand_still():
    """光标压在花朵身上时 florr 基本不动 —— 硬解一个方向等于给策略喂噪声。"""
    assert move_index_from_mouse({"x": 962.0, "y": 543.0}, (960.0, 540.0)) == MOVE_NONE


def test_mouse_without_player_anchor_is_none():
    assert move_index_from_mouse({"x": 1.0, "y": 2.0}, None) == MOVE_NONE
    assert move_index_from_mouse(None, (960.0, 540.0)) == MOVE_NONE


# ── 完整动作 ─────────────────────────────────────────────────────────────────

def test_keys_take_priority_over_mouse():
    """人一旦用 WASD, 鼠标可能还停在别处 —— 那不是他这一刻的意图。"""
    a = action_from_state({"a"}, {"x": 1500.0, "y": 540.0}, (960.0, 540.0))
    assert a["move"] == 7          # 按键说西, 鼠标说东


def test_mouse_used_when_no_direction_keys():
    a = action_from_state({ATTACK_KEY}, {"x": 1500.0, "y": 540.0}, (960.0, 540.0))
    assert a["move"] == 3          # 东
    assert a["attack"] == 1


def test_attack_and_defense_are_held_state_not_events():
    a = action_from_state({ATTACK_KEY, DEFENSE_KEY}, None, None)
    assert (a["attack"], a["defense"]) == (1, 1)
    b = action_from_state(set(), None, None)
    assert (b["attack"], b["defense"]) == (0, 0)


def test_swap_is_a_press_not_a_hold():
    assert action_from_state(set(), None, None, swap_presses=0)["swap"] == 0
    assert action_from_state(set(), None, None, swap_presses=1)["swap"] == 1
    # 10Hz 下一个 tick 内连按两次 R 不可能是有意的, 仍然只记 1
    assert action_from_state(set(), None, None, swap_presses=3)["swap"] == 1


def test_count_swap_presses_ignores_repeat_and_other_keys():
    events = [down(code="KeyR"),
              dict(down(code="KeyR"), repeat=True),
              down(code="KeyW"),
              up(code="KeyR"),
              down(code="KeyR")]
    assert count_swap_presses(events) == 2
