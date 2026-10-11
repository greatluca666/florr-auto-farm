"""farm_baseline 的纯函数测试 —— 速率换算和报告。不碰 CDP。"""
import pytest

from farm_baseline import RARITY_NAMES, format_report, summarize, weight


def _samples(k0, k1, p0, p1, t1):
    return [{"t": 0.0, "kills": k0, "petals": p0},
            {"t": t1, "kills": k1, "petals": p1}]


def test_weight_gives_common_a_nonzero_floor():
    """(rank+1)/10 而不是 rank/9 —— 后者会让普通怪恒为 0 分, 而刷怪区绝大多数是普通怪,
    加权分就完全无视了它们。"""
    assert weight(0) == 0.1
    assert weight(5) == 0.6
    assert weight(9) == 1.0


def test_summarize_computes_rates_over_wall_clock():
    s = summarize(_samples([100, 0, 0, 0, 0, 0], [400, 0, 0, 0, 0, 0],
                           [0] * 10, [0] * 10, 1800.0), 1800.0)
    assert s["kills_total"] == 300
    assert s["kills_per_hour"] == 600.0          # 半小时 300 只 -> 600/小时
    assert s["hours"] == 0.5


def test_summarize_weights_rarity():
    """1 只神话(权重 0.6)该顶 6 只普通(0.1)。"""
    common = summarize(_samples([0] * 8, [6, 0, 0, 0, 0, 0, 0, 0], [0] * 10, [0] * 10, 3600.0), 3600.0)
    mythic = summarize(_samples([0] * 8, [0, 0, 0, 0, 0, 1, 0, 0], [0] * 10, [0] * 10, 3600.0), 3600.0)
    assert common["kills_weighted_per_hour"] == pytest.approx(0.6)
    assert mythic["kills_weighted_per_hour"] == pytest.approx(0.6)


def test_summarize_tracks_drops_separately_from_kills():
    s = summarize(_samples([0] * 6, [10] * 6,
                           [0] * 10, [0, 0, 5, 0, 0, 0, 0, 0, 0, 0], 3600.0), 3600.0)
    assert s["kills_total"] == 60 and s["drops_total"] == 5
    assert s["drops_by_rarity"][2] == 5


def test_summarize_needs_two_samples():
    with pytest.raises(ValueError):
        summarize([{"t": 0.0, "kills": [0] * 6, "petals": [0] * 10}], 0.0)


def test_summarize_survives_zero_elapsed():
    """采样间隔被压到 0(极端情况)时不能除零崩掉。"""
    s = summarize(_samples([0] * 6, [3] * 6, [0] * 10, [0] * 10, 0.0), 0.0)
    assert s["kills_total"] == 18
    assert s["kills_per_hour"] == 0.0


def test_report_mentions_every_rarity():
    s = summarize(_samples([0] * 8, [1, 2, 3, 4, 5, 6, 7, 8], [0] * 10, [1] * 10, 3600.0),
                  3600.0)
    out = format_report(s, "rules")
    for name in RARITY_NAMES[:8]:
        assert name in out
    assert "rules" in out


def test_report_handles_kills_shorter_than_drops():
    """击杀 8 档、掉落 10 档 —— 两张表布局本来就不一样, 报告不能因为长度不一致崩掉。"""
    s = summarize(_samples([0] * 8, [1] * 8, [0] * 10, [1] * 10, 3600.0), 3600.0)
    assert len(s["kills_by_rarity"]) == 8 and len(s["drops_by_rarity"]) == 10
    format_report(s, "rl")          # 不抛就算过
