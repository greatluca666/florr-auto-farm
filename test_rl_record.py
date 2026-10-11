"""rl_record 的纯函数测试: 一条样本长什么样。不碰 CDP。"""
import json

import pytest

from rl_record import K_MOBS, K_PLAYERS, build_record


def _me(x=1000.0, y=2000.0, hp=0.75):
    return {"hp": hp, "hp_secondary": 0.0, "x": x, "y": y, "sx": 960.0, "sy": 540.0}


def _mob(dx, dy, name="沙尘暴", color="#1FDBDE", hp=1.0, me=None):
    me = me or _me()
    return {"name": name, "rarity_color": color, "hp": hp,
            "x": me["x"] + dx, "y": me["y"] + dy, "sx": 0.0, "sy": 0.0}


ACTION = {"move": 3, "attack": 1, "defense": 0, "swap": 0}


def test_record_is_one_json_line():
    rec = build_record(1.0, _me(), [], [], ACTION, True, False, None)
    line = json.dumps(rec, ensure_ascii=False)
    assert "\n" not in line
    assert json.loads(line)["a"] == ACTION


def test_mobs_are_relative_to_the_player_and_sorted_by_distance():
    """存相对偏移而不是绝对坐标 —— 策略不该去学地图坐标系。"""
    me = _me()
    mobs = [_mob(500.0, 0.0, me=me), _mob(50.0, 0.0, me=me), _mob(200.0, 0.0, me=me)]
    rec = build_record(0.0, me, mobs, [], ACTION, True, False, None)
    assert [m[0] for m in rec["m"]] == [50.0, 200.0, 500.0]


def test_mob_list_is_capped():
    me = _me()
    mobs = [_mob(float(i * 10 + 10), 0.0, me=me) for i in range(60)]
    rec = build_record(0.0, me, mobs, [], ACTION, True, False, None)
    assert len(rec["m"]) == K_MOBS
    assert rec["m"][0][0] == 10.0        # 留下的是最近的那些


def test_rarity_stored_as_rank_unknown_is_minus_one():
    me = _me()
    rec = build_record(0.0, me,
                       [_mob(10.0, 0.0, color="#1FDBDE", me=me),
                        _mob(20.0, 0.0, color="#BADBAD", me=me)],
                       [], ACTION, True, False, None)
    assert rec["m"][0][3] == 5           # 神话
    assert rec["m"][1][3] == -1          # 认不出的颜色不能悄悄当成普通(0)


def test_other_players_capped_and_carry_level():
    me = _me()
    others = [{"x": me["x"] + i * 10 + 10, "y": me["y"], "level": 30 + i} for i in range(9)]
    rec = build_record(0.0, me, [], others, ACTION, True, False, None)
    assert len(rec["o"]) == K_PLAYERS
    assert rec["o"][0][2] == 30


def test_missing_player_still_records_the_action():
    """解不出画面的 tick 也要落盘 —— 丢掉的话时间轴就断了, 而且那正是"人在菜单里"
    这种需要被学到的状态。"""
    rec = build_record(0.0, None, [], [], ACTION, True, False, None)
    assert "p" not in rec and rec["m"] == [] and rec["a"] == ACTION


def test_approx_camera_is_flagged():
    """近似相机下自身血量和世界坐标不可信 —— 打标记, 训练时可以整条丢掉。"""
    assert "approx" not in build_record(0.0, _me(), [], [], ACTION, True, False, None)
    assert build_record(0.0, _me(), [], [], ACTION, True, True, None)["approx"] == 1


def test_focus_is_recorded():
    """人切出去时"没按键"不代表"选择不动" —— 那是无效样本。"""
    assert build_record(0.0, _me(), [], [], ACTION, True, False, None)["f"] == 1
    assert build_record(0.0, _me(), [], [], ACTION, False, False, None)["f"] == 0


def test_counters_attached_only_when_given():
    assert "c" not in build_record(0.0, _me(), [], [], ACTION, True, False, None)
    c = {"k": [1] * 8, "p": [2] * 10}
    assert build_record(0.0, _me(), [], [], ACTION, True, False, c)["c"] == c


def test_hp_none_does_not_crash():
    me = dict(_me(), hp=None, hp_secondary=None)
    rec = build_record(0.0, me, [_mob(10.0, 0.0, hp=None)], [], ACTION, True, False, None)
    assert rec["p"][0] == 0.0
    assert rec["m"][0][2] == 0.0
