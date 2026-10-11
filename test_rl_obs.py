"""观测编码的测试。纯函数, 不碰 CDP。"""
import json
from pathlib import Path

import numpy as np
import pytest

import canvas_decode as cd
import rl_obs
from rl_obs import (
    K_MOBS, K_PLAYERS, MOB_DIM, OBS_DIM, PLAYER_DIM, SPECIES, UNKNOWN_SPECIES,
    danger_level, encode, rarity_norm, species_index,
)

FRAMES = Path(__file__).with_name("test_frames")


def P(x=1000.0, y=2000.0, hp=0.75, sec=0.0):
    return {"hp": hp, "hp_secondary": sec, "x": x, "y": y}


def M(dx, dy=0.0, hp=1.0, rank=5, name="沙尘暴", me=None):
    me = me or P()
    return {"name": name, "rarity_rank": rank, "hp": hp,
            "x": me["x"] + dx, "y": me["y"] + dy}


def mob_slot(obs, i):
    base = PLAYER_DIM + i * MOB_DIM
    return obs[base:base + MOB_DIM]


# ── 形状 / 兜底 ──────────────────────────────────────────────────────────────

def test_shape_and_dtype():
    obs = encode(P(), [], [])
    assert obs.shape == (OBS_DIM,) and obs.dtype == np.float32


def test_no_player_gives_zeros_but_keeps_the_approx_flag():
    """"这一帧什么都看不见"本身就是策略该能分辨的状态, 不是让调用方去猜。"""
    obs = encode(None, [M(10.0)], [], camera_approx=True)
    assert obs[rl_obs.APPROX] == 1.0
    assert np.count_nonzero(obs) == 1


def test_unused_slots_stay_zero_with_present_cleared():
    obs = encode(P(), [M(10.0)], [])
    assert mob_slot(obs, 0)[0] == 1.0
    for i in range(1, K_MOBS):
        assert np.all(mob_slot(obs, i) == 0.0)


# ── 以自己为原点 ─────────────────────────────────────────────────────────────

def test_mobs_are_relative_not_absolute():
    """同样的相对布局, 站在图的哪一角都该编出一样的怪槽 —— 否则换个刷怪区就得重学。"""
    a = encode(P(x=1000.0, y=2000.0), [M(100.0, 50.0, me=P(1000.0, 2000.0))], [])
    b = encode(P(x=9000.0, y=500.0), [M(100.0, 50.0, me=P(9000.0, 500.0))], [])
    assert np.allclose(mob_slot(a, 0), mob_slot(b, 0))


def test_mobs_sorted_nearest_first():
    me = P()
    mobs = [M(900.0, me=me), M(30.0, me=me), M(300.0, me=me)]
    obs = encode(me, mobs, [])
    dists = [mob_slot(obs, i)[3] for i in range(3)]
    assert dists == sorted(dists)


def test_mob_list_is_capped_at_k():
    me = P()
    obs = encode(me, [M(float(i + 1) * 10, me=me) for i in range(50)], [])
    assert mob_slot(obs, K_MOBS - 1)[0] == 1.0
    assert PLAYER_DIM + K_MOBS * MOB_DIM <= OBS_DIM


# ── 物种 / 稀有度 ────────────────────────────────────────────────────────────

def test_species_one_hot_is_exactly_one():
    obs = encode(P(), [M(10.0, name="仙人掌")], [])
    onehot = mob_slot(obs, 0)[6:]
    assert onehot.sum() == 1.0
    assert onehot[species_index("仙人掌")] == 1.0


def test_unknown_species_falls_into_its_own_slot():
    """别的生态的怪 / 客户端换语言 —— 要落进"不认识"那一格, 不能悄悄算成某个已知物种。"""
    obs = encode(P(), [M(10.0, name="某种没见过的怪")], [])
    assert mob_slot(obs, 0)[6 + UNKNOWN_SPECIES] == 1.0


def test_unknown_rarity_reads_low_not_high():
    """宁可低估不可高估: 高估会让策略去碰一只实际上很危险的怪。"""
    assert rarity_norm(None) == 0.0
    assert rarity_norm(-1) == 0.0
    assert rarity_norm(5) == pytest.approx(5 / 9)
    assert rarity_norm(99) == 1.0


# ── 危险度 ───────────────────────────────────────────────────────────────────

def test_danger_level_measures_recent_hp_loss():
    """当前血量看不出这个: 80% 血在掉和 80% 血在回, 该做的事完全不一样。"""
    assert danger_level([1.0, 0.95, 0.9, 0.85, 0.8, 0.7]) == pytest.approx(0.3)
    assert danger_level([0.7, 0.75, 0.8, 0.85, 0.9, 1.0]) == 0.0   # 在回血 -> 不危险


def test_danger_level_needs_enough_history():
    assert danger_level([1.0, 0.5]) == 0.0


def test_the_self_block_is_exactly_these_eight():
    """用户(2026-09-24)删掉了速度(原第 4、5 维)和"附近有没有玩家"(原第 6 维):
    - 速度: 每拍位移 ÷ 1500 ≈ 0.005, 跟别的输入比几乎看不见, 还随拍长抖
    - 附近有人: 其他玩家第一个槽的 present 位已经是同一个信息
    自身块只剩这 8 个, 按这个顺序。"""
    obs = encode(P(x=3000.0, y=6000.0, hp=0.75, sec=0.25), [M(10.0)] * 4,
                 [{"x": 1010.0, "y": 2000.0, "level": 50}],
                 hp_history=[1.0, 1.0, 1.0, 1.0, 1.0, 0.9], loadout_parity=1,
                 camera_approx=True)
    assert PLAYER_DIM == 8
    assert list(obs[:PLAYER_DIM]) == pytest.approx(
        [0.75, 0.25, 3000.0 / rl_obs.MAP_NORM, 6000.0 / rl_obs.MAP_NORM,
         0.1, 1.0, 1.0, 4 / rl_obs.MOB_COUNT_NORM])
    assert (rl_obs.HP, rl_obs.SHIELD, rl_obs.POS_X, rl_obs.POS_Y, rl_obs.DANGER,
            rl_obs.PARITY, rl_obs.APPROX, rl_obs.MOB_COUNT) == tuple(range(8))


# ── 其他玩家 ─────────────────────────────────────────────────────────────────

def test_other_players_carry_level_and_are_capped():
    me = P()
    others = [{"x": me["x"] + (i + 1) * 10.0, "y": me["y"], "level": 100 + i}
              for i in range(9)]
    obs = encode(me, [], others)
    base = PLAYER_DIM + K_MOBS * MOB_DIM
    assert obs[base] == 1.0                  # 第一个槽 present
    assert obs[base + 4] > 0                 # 等级归一
    assert obs[base + K_PLAYERS * 5 - 5] == 1.0      # 最后一个槽也填满了


def test_no_others_clears_the_has_company_bit():
    assert encode(P(), [], [])[6] == 0.0


def test_missing_level_does_not_crash():
    me = P()
    obs = encode(me, [], [{"x": me["x"] + 10.0, "y": me["y"], "level": None}])
    base = PLAYER_DIM + K_MOBS * MOB_DIM
    assert obs[base + 4] == 0.0


# ── 真实帧 ───────────────────────────────────────────────────────────────────

def test_encodes_a_real_desert_frame():
    """实拍帧(44 只怪, 含究极)能端到端编出来, 且数值都在合理范围。"""
    raw = json.loads((FRAMES / "desert_ultra_44mobs.json").read_text(encoding="utf-8"))["raw"]
    cam = cd.camera_from_frame(raw, best_effort=True)
    me = cd.player_from_frame(raw, cam)
    rank_by_color = {"#7EEF6D": 0, "#FFE65D": 1, "#4D52E3": 2, "#861FDE": 3,
                     "#DE1F1F": 4, "#1FDBDE": 5, "#FF2B75": 6, "#2BFFA3": 7}
    mobs = [dict(m, rarity_rank=rank_by_color.get(m.get("rarity_color"), -1))
            for m in cd.mobs_from_frame(raw, cam)]
    obs = encode(me, mobs, cd.other_players_from_frame(raw, cam))

    assert obs.shape == (OBS_DIM,)
    assert np.isfinite(obs).all()
    assert np.abs(obs).max() < 50.0          # 归一化没漏, 不会有量级爆炸的维度
    assert all(mob_slot(obs, i)[0] == 1.0 for i in range(K_MOBS))   # 44 只, 槽位填满
    # 那一屏里有究极和神话, 编出来的稀有度必须有高的
    assert max(mob_slot(obs, i)[5] for i in range(K_MOBS)) >= 5 / 9


def test_species_table_covers_the_real_desert_names():
    raw = json.loads((FRAMES / "desert_ultra_44mobs.json").read_text(encoding="utf-8"))["raw"]
    cam = cd.camera_from_frame(raw, best_effort=True)
    names = {m["name"] for m in cd.mobs_from_frame(raw, cam) if m.get("name")}
    unknown = {n for n in names if species_index(n) == UNKNOWN_SPECIES}
    assert not unknown, f"实拍帧里这些名字不在物种表里: {unknown}"


# ── 高稀有度不能被近处的普通怪挤出观测 ───────────────────────────────────────

def test_distant_mythic_keeps_a_slot():
    """实拍帧 desert_ultra_44mobs.json: 一屏 44 只怪, 2 神话 + 1 究极全都比最近的 16 只
    更远。纯按距离取前 16 会把它们整个挤掉 —— 而奖励正是按稀有度加权的, 最值钱的目标
    反而不在观测里。"""
    me = P()
    crowd = [M(float(i + 1) * 10, rank=0, name="甲虫", me=me) for i in range(40)]
    far_mythic = M(1200.0, rank=5, name="沙尘暴", me=me)
    obs = encode(me, crowd + [far_mythic], [])
    ranks = [mob_slot(obs, i)[5] for i in range(K_MOBS)]
    assert max(ranks) == pytest.approx(5 / 9)


def test_slots_stay_sorted_by_distance_even_after_reserving():
    """给高稀有度留槽之后, 槽位"由近到远"这个不变量仍然成立 —— 网络可以依赖它。"""
    me = P()
    mobs = [M(float(i + 1) * 10, rank=0, me=me) for i in range(40)] + \
           [M(1200.0, rank=6, me=me), M(800.0, rank=5, me=me)]
    obs = encode(me, mobs, [])
    dists = [mob_slot(obs, i)[3] for i in range(K_MOBS)]
    assert dists == sorted(dists)


def test_real_frame_shows_its_high_rarity_mobs():
    raw = json.loads((FRAMES / "desert_ultra_44mobs.json").read_text(encoding="utf-8"))["raw"]
    cam = cd.camera_from_frame(raw, best_effort=True)
    me = cd.player_from_frame(raw, cam)
    rank_by_color = {"#7EEF6D": 0, "#FFE65D": 1, "#4D52E3": 2, "#861FDE": 3,
                     "#DE1F1F": 4, "#1FDBDE": 5, "#FF2B75": 6, "#2BFFA3": 7}
    mobs = [dict(m, rarity_rank=rank_by_color.get(m.get("rarity_color"), -1))
            for m in cd.mobs_from_frame(raw, cam)]
    obs = encode(me, mobs, [])
    # 那一屏有 2 神话 + 1 究极, 编出来必须都在
    high = sorted(mob_slot(obs, i)[5] for i in range(K_MOBS))[-3:]
    assert high == pytest.approx(sorted([5 / 9, 5 / 9, 6 / 9]))
