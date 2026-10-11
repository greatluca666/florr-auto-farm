"""环境层的测试。用桩后端, 不碰 CDP、不装 gymnasium。"""
import numpy as np
import pytest

import rl_obs

from rl_env import ACTION_NVEC, OBS_DIM, FlorrEnv
from rl_reward import RewardTracker


def S(t=0.0, hp=1.0, x=0.0, y=0.0, mobs=(), others=(), control="policy", dt=0.1,
      kill_events=(), drop_events=(), approx=False, player=True):
    return {
        "t": t, "dt": dt, "control": control, "camera_approx": approx,
        "player": {"hp": hp, "hp_secondary": 0.0, "x": x, "y": y} if player else None,
        "mobs": list(mobs), "others": list(others),
        "kill_events": list(kill_events), "drop_events": list(drop_events),
    }


def M(dx, hp=1.0, rank=5, x0=0.0):
    return {"name": "沙尘暴", "rarity_rank": rank, "hp": hp, "x": x0 + dx, "y": 0.0}


class Stub:
    """按脚本回放 state 列表; 发过的动作都记下来。"""

    def __init__(self, states):
        self.states = list(states)
        self.sent = []
        self.i = 0

    def reset(self):
        self.i = 0
        return self.states[0]

    def send_action(self, action):
        self.sent.append(tuple(action))

    def next_state(self):
        self.i += 1
        return self.states[self.i] if self.i < len(self.states) else None


NOOP = (0, 0, 0, 0)


# ── 基本契约 ─────────────────────────────────────────────────────────────────

def test_reset_returns_obs_and_info():
    env = FlorrEnv(Stub([S()]))
    obs, info = env.reset()
    assert obs.shape == (OBS_DIM,) and obs.dtype == np.float32
    assert info["reset"] is True


def test_step_returns_the_gym_five_tuple():
    env = FlorrEnv(Stub([S(), S(t=0.1)]))
    env.reset()
    obs, reward, terminated, truncated, info = env.step(NOOP)
    assert obs.shape == (OBS_DIM,)
    assert isinstance(reward, float)
    assert terminated is False and truncated is False


def test_action_is_forwarded_to_the_backend():
    b = Stub([S(), S(t=0.1)])
    env = FlorrEnv(b)
    env.reset()
    env.step((3, 1, 0, 1))
    assert b.sent == [(3, 1, 0, 1)]


@pytest.mark.parametrize("bad", [(0, 0, 0), (0, 0, 0, 0, 0)])
def test_wrong_action_arity_raises(bad):
    env = FlorrEnv(Stub([S(), S()]))
    env.reset()
    with pytest.raises(ValueError):
        env.step(bad)


@pytest.mark.parametrize("bad", [(9, 0, 0, 0), (0, 2, 0, 0), (-1, 0, 0, 0)])
def test_out_of_range_action_raises(bad):
    """越界动作要当场炸 —— 悄悄裁剪的话, 训练时动作空间配错了永远发现不了。"""
    env = FlorrEnv(Stub([S(), S()]))
    env.reset()
    with pytest.raises(ValueError):
        env.step(bad)


# ── 终止 / 截断 ──────────────────────────────────────────────────────────────

def test_death_terminates():
    env = FlorrEnv(Stub([S(hp=1.0), S(t=0.1, hp=0.0)]))
    env.reset()
    _, reward, terminated, truncated, info = env.step(NOOP)
    assert terminated is True and truncated is False
    assert info["reason"] == "died"
    assert reward < 0


def test_script_control_truncates_without_reward():
    """脚本接管(拉回刷怪区/脱困/死亡结算)的那些 tick 不是策略干的, 不能进 rollout。"""
    env = FlorrEnv(Stub([S(), S(t=0.1, hp=0.2, control="script")]),
                   reward=RewardTracker(survival=0.01, hp_weight=1.0))
    env.reset()
    _, reward, terminated, truncated, info = env.step(NOOP)
    assert (terminated, truncated) == (False, True)
    assert reward == 0.0                 # 存活/掉血都不算
    assert info["reason"] == "script"


def test_kills_polled_on_the_takeover_tick_are_still_paid():
    """后端是**先**读计数器、**再**问仲裁器谁在控 —— 所以接管那一拍读到的击杀发生在
    交出控制权之前, 是策略打的。原来这一拍整个作废, 那些击杀一分不给; 接管之后脚本
    打的那些会在 reset() 里被丢掉, 不会混进来。"""
    env = FlorrEnv(Stub([S(), S(t=0.1, control="script", kill_events=[(5, 3)],
                               drop_events=[(4, 1)])]),
                   reward=RewardTracker(survival=0.0, hp_weight=0.0, kill_bonus=1.0))
    env.reset()
    _, reward, _, truncated, info = env.step(NOOP)
    assert truncated is True
    assert reward == pytest.approx(0.6 * 3 + 0.5)
    assert info["escrow_settled"] == 3   # 进度行的"累计击杀"数的就是这个


def test_kills_polled_on_the_death_tick_are_paid_on_top_of_the_penalty():
    env = FlorrEnv(Stub([S(), S(t=0.1, player=False, control="dead",
                               kill_events=[(5, 1)])]),
                   reward=RewardTracker(death=-5.0, kill_bonus=1.0))
    env.reset()
    _, reward, terminated, _, info = env.step(NOOP)
    assert terminated is True
    assert reward == pytest.approx(-5.0 + 0.6)
    assert info["escrow_settled"] == 1


def test_backend_running_out_truncates_on_the_last_real_obs():
    """拿不到下一帧时不能编一个零向量: PPO 会拿它 bootstrap 价值估计, 等于用一个不存在
    的状态训练。"""
    env = FlorrEnv(Stub([S(hp=0.5, mobs=[M(50.0)])]))
    obs0, _ = env.reset()
    obs1, reward, terminated, truncated, info = env.step(NOOP)
    assert (terminated, truncated) == (False, True)
    assert info["reason"] == "no_state"
    assert np.array_equal(obs1, obs0)    # 用的是上一份真实观测


def test_max_steps_truncates():
    env = FlorrEnv(Stub([S(t=i * 0.1) for i in range(10)]), max_steps=3)
    env.reset()
    for _ in range(2):
        _, _, _, truncated, _ = env.step(NOOP)
        assert truncated is False
    _, _, terminated, truncated, info = env.step(NOOP)
    assert (terminated, truncated) == (False, True)
    assert info["reason"] == "max_steps"


# ── 跨步状态 ─────────────────────────────────────────────────────────────────

def test_loadout_parity_flips_on_r_and_shows_up_in_the_obs():
    """R 是相对翻转, 绝对 loadout 读不到 —— 只能记按了几次的奇偶。"""
    env = FlorrEnv(Stub([S(t=i * 0.1) for i in range(4)]))
    env.reset()
    obs, *_ = env.step((0, 0, 0, 1))
    assert obs[rl_obs.PARITY] == 1.0
    obs, *_ = env.step((0, 0, 0, 1))
    assert obs[rl_obs.PARITY] == 0.0
    obs, *_ = env.step((0, 0, 0, 0))
    assert obs[rl_obs.PARITY] == 0.0


class Epoch:
    """装备基准纪元的桩 —— 真的那个是 loadout_swap.baseline_epoch。"""

    def __init__(self):
        self.n = 0

    def __call__(self):
        return self.n


def test_a_reset_that_did_not_touch_the_loadout_keeps_the_parity():
    """AFK / 步数上限 / 短途拉回 / 窗口失焦都会 reset, 但都没按过装备预设, 游戏里的
    花瓣栏还是翻过去的那样。原来 reset 一律归零 —— 观测在撒谎, 策略就只能学到"一律
    少按", 学不到"什么时候该按"(残血换回血那排之类), 而那是保留这一维唯一的理由。"""
    env = FlorrEnv(Stub([S(t=i * 0.1) for i in range(3)]), loadout_epoch=Epoch())
    env.reset()
    env.step((0, 0, 0, 1))
    obs, _ = env.reset()
    assert obs[rl_obs.PARITY] == 1.0


def test_a_preset_pressed_between_segments_zeroes_the_parity():
    epoch = Epoch()
    env = FlorrEnv(Stub([S(t=i * 0.1) for i in range(3)]), loadout_epoch=epoch)
    env.reset()
    env.step((0, 0, 0, 1))
    epoch.n += 1                          # 脚本拉回刷怪区时换了跑路装/输出装
    obs, _ = env.reset()
    assert obs[rl_obs.PARITY] == 0.0


def test_a_preset_pressed_during_a_tick_wins_over_that_ticks_r():
    """仲裁器在 next_state 里面跑寻路/复活, 期间按的预设发生在这一拍的 R **之后**。"""
    epoch = Epoch()

    class Bumps(Stub):
        def next_state(self):
            epoch.n += 1
            return super().next_state()

    env = FlorrEnv(Bumps([S(t=i * 0.1) for i in range(3)]), loadout_epoch=epoch)
    env.reset()
    obs, *_ = env.step((0, 0, 0, 1))
    assert obs[rl_obs.PARITY] == 0.0


def test_death_zeroes_the_parity():
    """死了重进 florr 会把 loadout 重置 —— 就算时块没配预设、一个键都没按。"""
    env = FlorrEnv(Stub([S(), S(t=0.1), S(t=0.2, player=False, control="dead")]),
                   loadout_epoch=Epoch())
    env.reset()
    env.step((0, 0, 0, 1))
    obs, *_ = env.step(NOOP)
    assert obs[rl_obs.PARITY] == 0.0
    obs, _ = env.reset()
    assert obs[rl_obs.PARITY] == 0.0


def test_death_read_from_the_hp_bar_zeroes_the_parity_too():
    env = FlorrEnv(Stub([S(), S(t=0.1), S(t=0.2, hp=0.0)]), loadout_epoch=Epoch())
    env.reset()
    env.step((0, 0, 0, 1))
    obs, _, terminated, *_ = env.step(NOOP)
    assert terminated is True and obs[rl_obs.PARITY] == 0.0


def test_info_says_whether_r_was_pressed_this_step():
    """按 R 的频率是判断"它学没学会少按"的唯一读数 —— 随机初始化时每拍 50%。"""
    env = FlorrEnv(Stub([S(t=i * 0.1) for i in range(3)]))
    env.reset()
    *_, info = env.step((0, 0, 0, 1))
    assert info["swap"] == 1
    *_, info = env.step(NOOP)
    assert info["swap"] == 0


def test_danger_level_accumulates_across_steps():
    """近几步的净掉血 —— 当前血量看不出"正在挨打"。"""
    states = [S(t=i * 0.1, hp=1.0 - i * 0.05) for i in range(8)]
    env = FlorrEnv(Stub(states))
    env.reset()
    obs = None
    for _ in range(6):
        obs, *_ = env.step(NOOP)
    assert obs[rl_obs.DANGER] > 0


def test_reset_clears_cross_step_state():
    """新一局不能带上一局的残留 —— 危险度(近几拍掉血)尤其: 上一局最后在挨打, 新一局
    第一拍不该一上来就报"正在挨打"。"""
    env = FlorrEnv(Stub([S(t=i * 0.1, hp=1.0 - i * 0.1) for i in range(8)]))
    env.reset()
    for _ in range(6):
        obs, *_ = env.step(NOOP)
    assert obs[rl_obs.DANGER] > 0
    env.backend.states = [S(hp=0.3), S(t=0.1, hp=0.3)]
    obs, _ = env.reset()
    assert obs[rl_obs.DANGER] == 0.0


# ── 奖励接线 ─────────────────────────────────────────────────────────────────

def test_kill_events_reach_the_reward():
    env = FlorrEnv(Stub([S(), S(t=0.1, kill_events=[(5, 1)])]),
                   reward=RewardTracker(survival=0.0, hp_weight=0.0, death=0.0,
                                        kill_bonus=1.0))
    env.reset()
    _, reward, _, _, info = env.step(NOOP)
    assert reward == pytest.approx(0.6)
    assert info["parts"]["kill"] == pytest.approx(0.6)


def test_stalled_step_is_flagged_and_zeroed():
    """dt 太大 = 这一步跨的时间不可信, 存活/掉血/挂账都不算。"""
    env = FlorrEnv(Stub([S(hp=1.0), S(t=5.0, hp=0.5, dt=5.0)]),
                   reward=RewardTracker(survival=0.01, hp_weight=1.0, death=0.0))
    env.reset()
    _, reward, _, _, info = env.step(NOOP)
    assert info["stalled"] is True and reward == 0.0


def test_info_carries_the_reward_breakdown():
    """从零开始学时"学不动"和"步数不够"必须分得开 —— 分项和挂账统计要一路传到训练日志。"""
    env = FlorrEnv(Stub([S(), S(t=0.1, mobs=[M(50.0, hp=0.9)])]))
    env.reset()
    _, _, _, _, info = env.step(NOOP)
    assert set(info["parts"]) == {"survival", "hp", "death", "kill", "drop", "idle"}
    for key in ("escrow_accrued", "escrow_expired", "escrow_settled", "escrow_open"):
        assert key in info


def test_missing_player_frames_do_not_crash():
    env = FlorrEnv(Stub([S(), S(t=0.1, player=False), S(t=0.2)]))
    env.reset()
    obs, reward, terminated, truncated, _ = env.step(NOOP)
    assert obs.shape == (OBS_DIM,) and not terminated
    env.step(NOOP)


def test_action_nvec_matches_the_agreed_action_space():
    assert ACTION_NVEC == (9, 2, 2, 2)


# ── 死亡结算画面(画面里解不出玩家的那种死) ───────────────────────────────────

def test_the_death_screen_terminates_with_the_death_penalty():
    """实机(2026-09-24): 死了一轮又一轮, 一次死亡惩罚都没发出去。死亡结算画面上血条
    已经没了, canvas 解不出玩家, 于是奖励里 `hp <= 0` 那个判据**永远不成立** ——
    "死亡有惩罚"整条设计等于没实现。脚本截屏看到死亡画面时走的就是这条路。"""
    env = FlorrEnv(Stub([S(), S(t=0.1, player=False, control="dead")]),
                   reward=RewardTracker(death=-1.0))
    env.reset()
    _, reward, terminated, truncated, info = env.step(NOOP)
    assert (terminated, truncated) == (True, False)
    assert reward == pytest.approx(-1.0)
    assert info["reason"] == "died"


def test_the_death_screen_path_does_not_depend_on_reading_hp():
    """这一帧解不出玩家也照样罚 —— 那正是死亡结算画面的样子。"""
    env = FlorrEnv(Stub([S(), S(t=0.1, player=False, control="dead")]),
                   reward=RewardTracker(death=-2.5))
    env.reset()
    _, reward, *_ = env.step(NOOP)
    assert reward == pytest.approx(-2.5)


def test_info_carries_why_self_was_missing():
    st = S(t=0.1, player=False)
    st["self_miss"] = "camera:zoom"
    env = FlorrEnv(Stub([S(), st]))
    env.reset()
    *_, info = env.step(NOOP)
    assert info["self_miss"] == "camera:zoom"


def test_the_supervisors_screenshot_tick_is_not_a_stall():
    """实机(2026-09-24)进度行"卡顿"稳定在每 50 步 6~9 次 ≈ 每秒一次 = 仲裁器每秒截三次屏
    那一拍。原来门限 0.25s, 那一拍必超, 每秒扔一拍的掉血和挂账 —— 可那一拍花朵一直按着
    策略上一拍的动作在走, 时间并不"不可信"。真正该扔的是梯度更新那种以秒计的停顿。"""
    env = FlorrEnv(Stub([S(hp=1.0), S(t=0.1, hp=1.0), S(t=0.45, hp=0.9, dt=0.35),
                         S(t=2.55, hp=0.8, dt=2.1)]),
                   reward=RewardTracker(survival=0.0, hp_weight=1.0, death=0.0))
    env.reset()
    env.step(NOOP)                        # 先建血量基线
    _, r, *_, info = env.step(NOOP)
    assert info["stalled"] is False and r == pytest.approx(-0.1)
    *_, info = env.step(NOOP)
    assert info["stalled"] is True


def test_an_unknown_hp_carries_the_last_reading_into_the_obs():
    """血量读不到(None)时观测里写 0 = "快死了", 策略会被一个假警报牵着走。几拍之内血量
    变不了多少, 沿用上一次读到的。"""
    st = S(t=0.1)
    st["player"]["hp"] = None
    env = FlorrEnv(Stub([S(hp=0.7), st]))
    env.reset()
    obs, *_ = env.step(NOOP)
    assert obs[rl_obs.HP] == pytest.approx(0.7)
