"""托管结算奖励的测试。纯状态机, 不碰 CDP。

重点全在"挂账什么时候兑现、什么时候作废" —— 这是整套奖励里唯一有跨步状态的地方,
也是唯一能悄悄算错还看不出来的地方。
"""
import pytest

from rl_reward import RewardTracker, rarity_weight


def P(hp=1.0, x=0.0, y=0.0):
    return {"hp": hp, "x": x, "y": y}


def M(hp, rank=5, x=50.0, y=0.0, name="沙尘暴"):
    return {"hp": hp, "rarity_rank": rank, "x": x, "y": y, "name": name}


@pytest.fixture
def tr():
    return RewardTracker(survival=0.0, hp_weight=0.0, death=0.0,
                         kill_bonus=1.0, engage_weight=1.0, drop_weight=1.0)


# ── 权重 ─────────────────────────────────────────────────────────────────────

def test_common_has_a_nonzero_floor():
    """rank/9 会让普通怪恒为 0 分, 而刷怪区绝大多数是普通怪。"""
    assert rarity_weight(0) == 0.1
    assert rarity_weight(5) == 0.6
    assert rarity_weight(9) == 1.0
    assert rarity_weight(None) == 0.0
    assert rarity_weight(-1) == 0.0


def test_a_moving_mob_keeps_its_identity_across_grid_lines(tr):
    """怪每步都在动, 认不出是同一只 = 那一拍的掉血读不到。

    原来按 20 单位的格子取整当编号: 实测一只每拍走 6 单位的怪, 20 拍里编号换了
    6 次 —— 三成的拍掉血直接丢了, 而且一声不响。"""
    accrued = 0.0
    for i in range(10):
        _, info = tr.step(t=i * 0.1, player=P(),
                          mobs=[M(1.0 - 0.05 * i, x=25.0 + 6.0 * i)])
        accrued += info["escrow_accrued"]
    assert accrued == pytest.approx(0.45)      # 9 次掉血, 一次都不能丢


def test_a_different_mob_walking_into_the_same_spot_is_a_new_mob(tr):
    """甲虫刚被打死消失, 一只本来就残血的蝎子走到同一个位置 —— 那不是甲虫掉了 0.3 血。"""
    tr.step(t=0.0, player=P(), mobs=[M(1.0, x=50.0, name="甲虫")])
    _, info = tr.step(t=0.1, player=P(), mobs=[M(0.7, x=55.0, name="蝎子")])
    assert info["escrow_accrued"] == 0.0


def test_two_nearby_mobs_of_the_same_kind_stay_separate(tr):
    """同名的两只挨着走, 不能被认成一只 —— 那样两只的掉血会互相抵消/串账。"""
    tr.step(t=0.0, player=P(), mobs=[M(1.0, x=50.0), M(1.0, x=120.0)])
    for i in range(1, 4):
        tr.step(t=i * 0.1, player=P(),
                mobs=[M(1.0 - 0.1 * i, x=50.0), M(1.0, x=120.0)])
    r, info = tr.step(t=0.4, player=P(), mobs=[M(1.0, x=120.0)], kill_events=[(5, 1)])
    assert r == pytest.approx(0.6 * (0.3 + 1.0))
    assert info["escrow_open"] == 0            # 没挨打的那只从没挂过账


# ── 挂账 -> 兑现 ─────────────────────────────────────────────────────────────

def test_damage_pays_nothing_until_the_kill_lands(tr):
    r, info = tr.step(t=0.0, player=P(), mobs=[M(1.0)])
    assert r == 0.0
    r, info = tr.step(t=0.1, player=P(), mobs=[M(0.6)])
    assert r == 0.0                       # 打掉 40% 血, 一分不给
    assert info["escrow_accrued"] == pytest.approx(0.4)
    assert info["escrow_open"] == 1

    r, info = tr.step(t=0.2, player=P(), mobs=[], kill_events=[(5, 1)])
    # 神话权重 0.6 × (挂账 0.4 + kill_bonus 1.0)
    assert r == pytest.approx(0.6 * 1.4)
    assert info["escrow_settled"] == 1
    assert info["escrow_open"] == 0


def test_a_kill_settles_everything_that_mob_took_not_just_one_hit(tr):
    """一只怪通常要挨好几拍才死。原来每一拍的掉血各挂一笔, 击杀只兑现最大的那**一笔**:
    实测神话怪挨 9 拍、每拍 0.1, 死时只结算 0.1 而不是 0.9, 剩下 8 笔挂着 5 秒内会被
    下一只同稀有度的击杀冒领。"""
    for i in range(10):
        tr.step(t=i * 0.1, player=P(), mobs=[M(1.0 - 0.1 * i)])
    r, info = tr.step(t=1.0, player=P(), mobs=[], kill_events=[(5, 1)])
    assert r == pytest.approx(0.6 * (0.9 + 1.0))
    assert info["escrow_open"] == 0


def test_escrow_expiry_counts_from_the_last_hit_not_the_first(tr):
    """打一只怪打了 8 秒: 头几拍的掉血不能因为"5 秒前挂的"就先作废 —— 这只怪一直在挨打。"""
    for i in range(81):
        tr.step(t=i * 0.1, player=P(), mobs=[M(1.0 - 0.01 * i)])
    r, _ = tr.step(t=8.1, player=P(), mobs=[], kill_events=[(5, 1)])
    assert r == pytest.approx(0.6 * (0.8 + 1.0))


def test_a_stalled_frame_between_hits_keeps_the_mob_and_its_escrow(tr):
    """卡顿那一拍的掉血不算(跨了多久不知道), 但怪还是那只怪 —— 下一拍接着记,
    不能把挂账劈成两笔。卡顿每秒都可能有一次(仲裁器截屏), 劈开就等于又回到每刀一笔。"""
    tr.step(t=0.0, player=P(), mobs=[M(1.0)])
    tr.step(t=0.1, player=P(), mobs=[M(0.8)])
    tr.step(t=0.5, player=P(), mobs=[M(0.7)], stalled=True)     # 这 0.1 不算
    tr.step(t=0.6, player=P(), mobs=[M(0.6)])
    r, info = tr.step(t=0.7, player=P(), mobs=[], kill_events=[(5, 1)])
    assert r == pytest.approx(0.6 * (0.3 + 1.0))
    assert info["escrow_open"] == 0


def test_damage_is_voided_when_nobody_credits_the_kill(tr):
    """别人补刀 / 怪跑了 —— 你砍的那几刀一分不得。"""
    tr.step(t=0.0, player=P(), mobs=[M(1.0)])
    tr.step(t=0.1, player=P(), mobs=[M(0.6)])   # 掉 0.4, 在 hp_clip(0.5) 之内
    total, expired = 0.0, 0.0
    for i in range(80):                   # 一直没有击杀事件, 熬过 escrow_timeout_s
        r, info = tr.step(t=0.2 + i * 0.1, player=P(), mobs=[])
        total += r
        expired += info["escrow_expired"]   # 作废发生在中途某一步, 不是最后一步
    assert total == 0.0
    assert info["escrow_open"] == 0
    assert expired == pytest.approx(0.4)    # 当初挂上去的那 0.4 原样作废


def test_kill_without_any_escrow_still_pays_the_bonus(tr):
    """计数器涨了就是你杀的(游戏自己判的), 比我们的挂账可靠 —— 补最后一刀也算。"""
    r, _ = tr.step(t=0.0, player=P(), mobs=[], kill_events=[(4, 1)])
    assert r == pytest.approx(0.5 * 1.0)


def test_multiple_kills_settle_the_biggest_escrows_first(tr):
    """同稀有度有多笔挂账时, 打得最狠的那只最可能就是刚死的那只。"""
    tr.step(t=0.0, player=P(), mobs=[M(1.0, x=50.0), M(1.0, x=80.0)])
    tr.step(t=0.1, player=P(), mobs=[M(0.9, x=50.0), M(0.5, x=80.0)])   # 0.1 和 0.5
    r, info = tr.step(t=0.2, player=P(), mobs=[], kill_events=[(5, 1)])
    assert r == pytest.approx(0.6 * (0.5 + 1.0))       # 先兑现 0.5 那笔
    assert info["escrow_open"] == 1


def test_escrow_is_keyed_by_rarity_not_species(tr):
    """按稀有度归类 —— 免掉"canvas 的中文怪名 -> 计数器的英文 sid"那层映射。"""
    tr.step(t=0.0, player=P(), mobs=[M(1.0, rank=5, name="沙尘暴")])
    tr.step(t=0.1, player=P(), mobs=[M(0.7, rank=5, name="沙尘暴")])
    r, _ = tr.step(t=0.2, player=P(), mobs=[], kill_events=[(5, 1)])
    assert r == pytest.approx(0.6 * 1.3)               # 名字对不上也照样兑现


def test_far_away_damage_is_not_escrowed(tr):
    """攻击距离外的怪掉血不可能是你打的。"""
    tr.step(t=0.0, player=P(), mobs=[M(1.0, x=5000.0)])
    _, info = tr.step(t=0.1, player=P(), mobs=[M(0.2, x=5000.0)])
    assert info["escrow_accrued"] == 0.0


def test_implausible_hp_jump_is_dropped_not_escrowed(tr):
    """一步掉 90% 血是解码读错, 不是真打中。"""
    tr.step(t=0.0, player=P(), mobs=[M(1.0)])
    _, info = tr.step(t=0.1, player=P(), mobs=[M(0.05)])
    assert info["escrow_accrued"] == 0.0
    assert info["escrow_open"] == 0


# ── 掉落 / 生存 ──────────────────────────────────────────────────────────────

def test_drops_pay_immediately(tr):
    """掉落是击杀的结果, 计数精确, 不需要托管。"""
    r, _ = tr.step(t=0.0, player=P(), mobs=[], drop_events=[(4, 2), (0, 3)])
    assert r == pytest.approx(0.5 * 2 + 0.1 * 3)


# 实机反推的一次击杀值(2026-09-24, 满局: 84.4/67、50.0/46、70.3/57 -> ~1.2)。
# 一局 300 秒 × 实测 7.4Hz。
KILL_WORTH = 1.2
ROUND_STEPS = 300 * 7.4


def test_doing_nothing_a_whole_round_must_not_outearn_killing():
    """默认值唯一真正要守的性质。

    原来 survival=0.01: 跑满一局白拿 +22 = 18 次击杀, 零风险。策略真的学会了这一手
    —— 第 30 轮 2120 步(全场最多)只有 3 杀、总回报 +22.21(≈2120×0.01), 中间 250 秒
    `怪中位 0~1`; 第 38 轮击杀从 76 秒起冻在 46 不动, 一冻 222 秒。而这种局梯度更新
    最多(33/34 次), 死得快的局只有 0~3 次。29 局的击杀均值从 23.2 掉到 14.1。

    这里不写死 `survival == 0` —— 要守的是**比例**: 什么都不干拿到的, 不能比动手
    赚得多。
    """
    t = RewardTracker()
    assert t.survival * ROUND_STEPS <= KILL_WORTH


def test_dying_must_cost_more_than_a_few_kills():
    """原来 death=-1.0 比一次击杀(+1.2)还便宜, 死亡在账面上几乎不算事。"""
    t = RewardTracker()
    assert t.death <= -3 * KILL_WORTH


def test_hp_loss_and_death():
    t = RewardTracker(survival=0.01, hp_weight=1.0, death=-1.0)
    t.step(t=0.0, player=P(hp=1.0), mobs=[])
    r, info = t.step(t=0.1, player=P(hp=0.9), mobs=[])
    assert r == pytest.approx(0.01 - 0.1)
    r, info = t.step(t=0.2, player=P(hp=0.0), mobs=[])
    assert info["died"] is True
    assert info["parts"]["death"] == -1.0


def test_implausible_player_hp_jump_is_ignored():
    """自身血量一步跳 90% 是读错, 不能当成真掉血罚下去。"""
    t = RewardTracker(survival=0.0, hp_weight=1.0, death=0.0)
    t.step(t=0.0, player=P(hp=1.0), mobs=[])
    r, _ = t.step(t=0.1, player=P(hp=0.05), mobs=[])
    assert r == 0.0


def test_stalled_step_zeroes_everything_except_death():
    """跨的时间不明时, 存活/掉血/挂账都不可信 —— 但死了就是死了。"""
    t = RewardTracker(survival=0.01, hp_weight=1.0, death=-1.0)
    t.step(t=0.0, player=P(hp=1.0), mobs=[])
    r, info = t.step(t=5.0, player=P(hp=0.5), mobs=[], stalled=True)
    assert r == 0.0 and info["stalled"] is True
    r, info = t.step(t=9.0, player=P(hp=0.0), mobs=[], stalled=True)
    assert r == -1.0


def test_stalled_step_still_pays_the_kill_counter(tr):
    """击杀/掉落是游戏计数器给的事实, 跟这一拍跨了多久无关。原来卡顿拍整个作废:
    每批 rollout 的第一拍(刚做完梯度更新)必卡, 仲裁器每秒截屏那一拍也可能卡 ——
    落在这些拍上的击杀一分不给, 挂账还留着等 5 秒后被别的击杀冒领。"""
    tr.step(t=0.0, player=P(), mobs=[M(1.0)])
    tr.step(t=0.1, player=P(), mobs=[M(0.6)])
    r, info = tr.step(t=3.0, player=P(), mobs=[], stalled=True,
                      kill_events=[(5, 1)], drop_events=[(4, 1)])
    assert r == pytest.approx(0.6 * (0.4 + 1.0) + 0.5)
    assert info["escrow_settled"] == 1 and info["escrow_open"] == 0


def test_missing_player_does_not_crash(tr):
    """解不出玩家的帧(菜单/死亡结算)照样要能算, 只是不挂账。"""
    r, info = tr.step(t=0.0, player=None, mobs=[M(0.5)])
    assert r == 0.0 and info["escrow_accrued"] == 0.0


# ── 诊断 ─────────────────────────────────────────────────────────────────────

def test_info_breaks_the_reward_into_parts(tr):
    """从零开始学时, "学不动"和"步数不够"必须分得开 —— 光看总回报分不出来。"""
    _, info = tr.step(t=0.0, player=P(), mobs=[], kill_events=[(5, 1)],
                      drop_events=[(2, 1)])
    assert set(info["parts"]) == {"survival", "hp", "death", "kill", "drop", "idle"}
    assert info["parts"]["kill"] > 0 and info["parts"]["drop"] > 0
    assert sum(info["parts"].values()) == pytest.approx(info["reward"])


def test_reset_clears_escrow(tr):
    tr.step(t=0.0, player=P(), mobs=[M(1.0)])
    tr.step(t=0.1, player=P(), mobs=[M(0.5)])
    tr.reset()
    r, info = tr.step(t=0.2, player=P(), mobs=[], kill_events=[(5, 1)])
    assert info["escrow_open"] == 0
    assert r == pytest.approx(0.6 * 1.0)      # 只剩 kill_bonus, 上一局的挂账不带过来


def test_death_penalty_voids_the_open_escrow():
    """人都死了, 打掉的那些血不会再有击杀去兑现它们 —— 留着的话下一局开局第一个击杀
    会把上一局的账一起结了。"""
    t = RewardTracker(death=-1.0, attack_range=1e9)
    me = {"hp": 1.0, "x": 0.0, "y": 0.0}
    mobs = [{"name": "甲虫", "rarity_rank": 4, "hp": 1.0, "x": 10.0, "y": 0.0}]
    t.step(t=0.0, player=me, mobs=mobs)
    t.step(t=0.1, player=me, mobs=[dict(mobs[0], hp=0.7)])
    assert t._info({}, 0.0)["escrow_open"] == 1
    assert t.death_penalty() == pytest.approx(-1.0)
    assert t._info({}, 0.0)["escrow_open"] == 0


# ── 空转惩罚: 15 秒没打到怪 ─────────────────────────────────────────────────
#
# 用户(2026-09-25): 周围没怪时策略就停在原地。视野里没怪时所有输入都一样, 它学不到
# "该往哪走"; 而且停着不挨打、不掉血, 在账面上零成本。15 秒没打到怪(没造成伤害、
# 也没击杀)就开始按秒扣分, 逼它动起来找怪。

def idle_tracker(**kw):
    return RewardTracker(survival=0.0, hp_weight=0.0, death=0.0, **kw)


def run_idle(t, seconds, step=0.1, mobs=()):
    total = 0.0
    n = int(round(seconds / step))
    for i in range(n):
        r, _ = t.step(t=i * step, player=P(), mobs=list(mobs))
        total += r
    return total


def test_no_penalty_inside_the_grace_period():
    assert run_idle(idle_tracker(), 15.0) == 0.0


def test_standing_around_past_15_seconds_costs_by_the_second():
    t = idle_tracker()
    total = run_idle(t, 25.0)                    # 15 秒宽限 + 10 秒空转
    assert total == pytest.approx(-t.idle_per_s * 10.0, rel=0.05)
    assert t.idle_per_s > 0


def test_hitting_a_mob_restarts_the_clock():
    t = idle_tracker()
    for i in range(140):                         # 14 秒没打到
        t.step(t=i * 0.1, player=P(), mobs=[])
    t.step(t=14.0, player=P(), mobs=[M(1.0)])
    t.step(t=14.1, player=P(), mobs=[M(0.9)])    # 打到了
    for i in range(1, 140):                      # 又 14 秒
        r, info = t.step(t=14.1 + i * 0.1, player=P(), mobs=[])
        assert r == 0.0 and info["parts"]["idle"] == 0.0


def test_a_kill_restarts_the_clock_too():
    t = idle_tracker()
    for i in range(140):
        t.step(t=i * 0.1, player=P(), mobs=[])
    t.step(t=14.0, player=P(), mobs=[], kill_events=[(0, 1)])
    for i in range(1, 140):
        _, info = t.step(t=14.0 + i * 0.1, player=P(), mobs=[])
        assert info["parts"]["idle"] == 0.0


def test_a_new_segment_gets_a_fresh_grace_period():
    """脚本拉回/复活之后重新开一段 —— 走路那几十秒不是策略在空转。"""
    t = idle_tracker()
    run_idle(t, 20.0)
    t.reset()
    for i in range(100):
        _, info = t.step(t=100.0 + i * 0.1, player=P(), mobs=[])
        assert info["parts"]["idle"] == 0.0


def test_a_stalled_step_is_not_charged_as_idle():
    """卡顿拍跨了多久不可信(梯度更新的停顿)。"""
    t = idle_tracker()
    run_idle(t, 20.0)
    _, info = t.step(t=40.0, player=P(), mobs=[], stalled=True)
    assert info["parts"]["idle"] == 0.0


def test_the_step_after_a_stall_is_charged_only_for_itself():
    """卡顿那几秒(比如梯度更新)不能在下一拍被补扣回来。"""
    t = idle_tracker()
    run_idle(t, 20.0)
    t.step(t=40.0, player=P(), mobs=[], stalled=True)
    _, info = t.step(t=40.1, player=P(), mobs=[])
    assert info["parts"]["idle"] == pytest.approx(-t.idle_per_s * 0.1)


def test_only_the_time_past_the_grace_period_is_charged():
    t = idle_tracker()
    t.step(t=0.0, player=P(), mobs=[])
    _, info = t.step(t=16.0, player=P(), mobs=[])        # 一步跨过 15 秒线
    assert info["parts"]["idle"] == pytest.approx(-t.idle_per_s * 1.0)
