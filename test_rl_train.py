"""训练入口的测试。要装了 stable-baselines3 才跑 —— 没装就整份跳过, 别让别的机器
(比如只跑脚本不训练的部署机)因为缺这个依赖而全红。
"""
import numpy as np
import pytest

pytest.importorskip("stable_baselines3")

import rl_train
from rl_env import ACTION_NVEC, OBS_DIM
from rl_train import SyntheticBackend, make_gym_env, release_keys_callback


class CountingBackend(SyntheticBackend):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.closed = 0

    def close(self):
        self.closed += 1


# ── gym 壳 ───────────────────────────────────────────────────────────────────

def test_spaces_match_the_agreed_action_and_obs():
    env = make_gym_env(SyntheticBackend())
    assert tuple(env.action_space.nvec) == ACTION_NVEC
    assert env.observation_space.shape == (OBS_DIM,)
    assert env.observation_space.dtype == np.float32


def test_sb3_accepts_the_env():
    """check_env 会挨个核对 reset/step 的返回形状、dtype、边界 —— 手写壳最容易在
    这些地方跟 Gym 的契约差一点点, 而 PPO 跑起来之后再发现要浪费几小时。"""
    from stable_baselines3.common.env_checker import check_env

    check_env(make_gym_env(SyntheticBackend()), warn=True, skip_render_check=True)


def test_observations_stay_inside_the_declared_box():
    env = make_gym_env(SyntheticBackend())
    obs, _ = env.reset()
    assert env.observation_space.contains(obs)
    for _ in range(50):
        obs, _, term, trunc, _ = env.step(env.action_space.sample())
        assert env.observation_space.contains(obs)
        if term or trunc:
            obs, _ = env.reset()


# ── 更新期间要松键 ───────────────────────────────────────────────────────────

def test_keys_are_released_when_a_rollout_ends():
    """PPO 收满一批就停下来做梯度更新, 那十几秒没人发动作。键是**按住**的状态 ——
    不松的话花朵会一直朝最后那个方向怼, 撞墙或者站在怪堆里被打死, 而这段时间里
    没有任何动作能救它。"""
    from stable_baselines3 import PPO
    from stable_baselines3.common.monitor import Monitor

    backend = CountingBackend()
    model = PPO("MlpPolicy", Monitor(make_gym_env(backend)), n_steps=64, batch_size=32,
                n_epochs=1, verbose=0, seed=0)
    model.learn(total_timesteps=128, callback=release_keys_callback())
    assert backend.closed >= 2          # 两批各一次(外加训练结束那次)


# ── 合成后端本身不能是空转 ───────────────────────────────────────────────────

def test_the_synthetic_backend_actually_produces_kills():
    """冒烟用的后端如果奖励恒为零, 那条 --dry-run 就什么都没验到。"""
    b = SyntheticBackend(seed=1)
    b.reset()
    kills = 0
    for _ in range(400):
        b.send_action((3, 1, 0, 0))     # 一路往东, 一直攻击
        st = b.next_state()
        kills += sum(n for _, n in st["kill_events"])
    assert kills > 0


def test_the_synthetic_backend_hurts_you_for_standing_in_the_pile():
    b = SyntheticBackend(seed=2)
    b.reset()
    for _ in range(200):
        b.send_action((0, 0, 0, 0))
        st = b.next_state()
    assert st["player"]["hp"] < 1.0


def test_a_short_ppo_run_completes():
    from stable_baselines3 import PPO
    from stable_baselines3.common.monitor import Monitor

    model = PPO("MlpPolicy", Monitor(make_gym_env(SyntheticBackend())),
                n_steps=64, batch_size=32, n_epochs=1, verbose=0, seed=0)
    model.learn(total_timesteps=128)
    action, _ = model.predict(model.env.reset(), deterministic=True)
    assert action.shape[-1] == len(ACTION_NVEC)


def test_defaults_are_sane_for_a_ten_hz_real_time_env():
    """10Hz 下 gamma 的半衰期要够覆盖"接近-攻击-击杀"这一串; 从零学 + 稀疏奖励时
    熵系数不能压太低, 否则会收敛到"站着不动吃存活分"。"""
    import math

    half_life = math.log(0.5) / math.log(rl_train.DEFAULTS["gamma"])
    assert 80 <= half_life <= 400          # 8~40 秒
    assert rl_train.DEFAULTS["ent_coef"] >= 0.005


def test_small_rollouts_are_not_trained_on_over_and_over():
    """GUI 那条路一批只有 64 步(≈ 8.6 秒), 按实机每局 ~23 杀 / 300 秒算平均只有
    0.66 次击杀 —— 约一半的批次一次击杀都没有。那些批里优势基本是价值误差, 优势
    归一化再把它放大成跟正常批一样大的更新; 原来还要在同一批上反复训 10 轮。
    轮数压下来, 再加 target_kl 让它漂远了就提前停。"""
    assert rl_train.DEFAULTS["n_epochs"] <= 4
    assert rl_train.DEFAULTS["target_kl"] is not None


def test_loading_a_model_puts_the_current_hyperparameters_back(tmp_path):
    """PPO.load 会把存档里的超参**全部**一起恢复。实机踩过 n_steps 那一回; 同一个坑
    对每个超参都成立 —— 改了 DEFAULTS, 加载回来的旧存档照样用旧值, 而且不报错。"""
    from stable_baselines3 import PPO

    path = str(tmp_path / "m")
    PPO("MlpPolicy", make_gym_env(SyntheticBackend()), n_steps=256, batch_size=64,
        n_epochs=10, gamma=0.9, gae_lambda=0.8, ent_coef=0.0, learning_rate=1e-3,
        clip_range=0.3, target_kl=None, verbose=0).save(path)

    model = rl_train.load_ppo(path, make_gym_env(SyntheticBackend()), n_steps=64)
    d = rl_train.DEFAULTS
    assert model.n_steps == 64 and model.rollout_buffer.buffer_size == 64
    assert model.batch_size <= 64
    for k in ("n_epochs", "gamma", "gae_lambda", "ent_coef", "target_kl"):
        assert getattr(model, k) == d[k], k
    assert model.clip_range(1.0) == d["clip_range"]
    assert model.lr_schedule(1.0) == d["learning_rate"]   # train() 每次按它重设 lr


def test_the_cli_resume_goes_through_the_same_loader(tmp_path, monkeypatch):
    """--resume 原来直接 PPO.load(path, env=env) —— --n-steps 和改过的 DEFAULTS 全部
    被存档里的旧值盖掉。"""
    seen = {}

    class Stop(Exception):
        pass

    def spy(path, env, n_steps):
        seen.update(path=path, n_steps=n_steps)
        raise Stop

    monkeypatch.setattr(rl_train, "load_ppo", spy)
    with pytest.raises(Stop):
        rl_train.main(["--dry-run", "--resume", "old.zip", "--n-steps", "32",
                       "--steps", "8", "--progress-every", "0"])
    assert seen == {"path": "old.zip", "n_steps": 32}


# ── 进度输出 ─────────────────────────────────────────────────────────────────

def test_progress_prints_something_before_the_first_gradient_update(capsys):
    """sb3 自己要收满一整批(默认 1024 步 ≈ 102 秒游戏时间)才打第一张表。实机第一次
    跑就栽在这: 3.9 分钟零输出, 分不出"在跑只是慢"和"卡死了", 只能手动停掉。"""
    from stable_baselines3 import PPO
    from stable_baselines3.common.monitor import Monitor
    from rl_train import progress_callback

    model = PPO("MlpPolicy", Monitor(make_gym_env(SyntheticBackend())),
                n_steps=1024, batch_size=32, n_epochs=1, verbose=0, seed=0)
    model.learn(total_timesteps=60, callback=progress_callback(every=20))
    out = capsys.readouterr().out
    assert "步" in out and "Hz" in out
    assert "解出自己 100%" in out        # 合成后端每帧都有玩家


def test_progress_reports_how_often_r_was_pressed(capsys):
    """随机初始化时每拍 50% 按 R —— 每按一次花瓣都要重新装填。练没练会"少按",
    只能看这个比例往下走没有; 熵奖励会一直把它往 50% 拉, 停在哪一档事先算不出。"""
    from rl_train import progress_callback

    class Model:
        num_timesteps = 0
        logger = None

        def get_env(self):
            return object()

    cb = progress_callback(every=4)
    cb.init_callback(Model())
    cb.on_training_start({}, {})
    for swap in (1, 0, 0, 0):
        cb.update_locals({"infos": [{"swap": swap, "control": "policy"}], "rewards": [0.0]})
        cb.on_step()
    assert "按R 25%" in capsys.readouterr().out


def test_progress_breaks_down_why_self_was_missing(capsys):
    """"解出自己 26%" 查不下去 —— 要知道是相机缺了哪项、还是近似、还是没血条。"""
    from rl_train import progress_callback

    class Model:
        num_timesteps = 0
        logger = None

        def get_env(self):
            return object()

    cb = progress_callback(every=4)
    cb.init_callback(Model())
    cb.on_training_start({}, {})
    for miss in ("camera:zoom", "camera:zoom", "approx", None):
        cb.update_locals({"infos": [{"has_player": miss is None, "self_miss": miss}],
                          "rewards": [0.0]})
        cb.on_step()
    out = capsys.readouterr().out
    assert "解出自己 25%" in out
    assert "camera:zoom×2" in out and "approx×1" in out


def test_progress_counts_a_missing_hp_even_when_the_position_is_known(capsys):
    from rl_train import progress_callback

    class Model:
        num_timesteps = 0
        logger = None

        def get_env(self):
            return object()

    cb = progress_callback(every=2)
    cb.init_callback(Model())
    cb.on_training_start({}, {})
    for miss in ("no_hp", None):
        cb.update_locals({"infos": [{"has_player": True, "self_miss": miss}], "rewards": [0.0]})
        cb.on_step()
    assert "no_hp×1" in capsys.readouterr().out


def test_progress_reports_a_broken_decoder_as_zero_percent(capsys):
    """画面解码坏了不会报错 —— 只是每个观测都是零向量, 策略照样"训练"几小时什么也
    学不到。这一行是唯一能看出来的地方。"""
    from stable_baselines3 import PPO
    from stable_baselines3.common.monitor import Monitor
    from rl_train import progress_callback

    class Blind(SyntheticBackend):
        def _state(self, kill_events, drop_events):
            st = super()._state(kill_events, drop_events)
            st["player"] = None
            return st

    model = PPO("MlpPolicy", Monitor(make_gym_env(Blind())), n_steps=1024,
                batch_size=32, n_epochs=1, verbose=0, seed=0)
    model.learn(total_timesteps=40, callback=progress_callback(every=20))
    assert "解出自己 0%" in capsys.readouterr().out


def test_progress_warns_when_most_ticks_were_unfocused(capsys):
    """按键发给当前焦点窗口。焦点在终端上时策略的动作一个都进不了游戏, 而这件事
    在别的地方一点声音都没有 —— 环境只是安静地把这些拍截断。"""
    from stable_baselines3 import PPO
    from stable_baselines3.common.monitor import Monitor
    from rl_train import progress_callback

    class Unfocused(SyntheticBackend):
        def _state(self, kill_events, drop_events):
            st = super()._state(kill_events, drop_events)
            st["control"] = "unfocused"
            return st

    model = PPO("MlpPolicy", Monitor(make_gym_env(Unfocused())), n_steps=1024,
                batch_size=32, n_epochs=1, verbose=0, seed=0)
    model.learn(total_timesteps=40, callback=progress_callback(every=20))
    assert "没焦点" in capsys.readouterr().out


def test_the_cli_warns_that_it_cannot_run_unattended():
    """rl_train 的后端没有 control_hint —— 花朵一死没人点"继续", 策略会对着静止的
    死亡结算界面继续"训练", 而且不报错。这个限制只在文档里, 所以钉一下。"""
    import inspect

    assert "不能无人值守" in inspect.getdoc(rl_train)
    assert "control_hint" not in inspect.getsource(rl_train.live_backend)


def test_an_old_format_checkpoint_says_what_to_do(tmp_path):
    """观测格式改过(2026-09-24 自身块 11 -> 8), 旧存档就加载不了。sb3 只报一句
    "Observation spaces do not match", 在 GUI 那条路上连败三轮后静默退回规则战斗 ——
    得直接说清楚: 这是旧格式的存档, 要从零练或换一个存档路径。"""
    import gymnasium as gym
    from stable_baselines3 import PPO

    class Old(gym.Env):
        observation_space = gym.spaces.Box(-1e3, 1e3, (367,), np.float32)
        action_space = gym.spaces.MultiDiscrete(list(ACTION_NVEC))

        def reset(self, seed=None, options=None):
            return np.zeros(367, np.float32), {}

        def step(self, a):
            return np.zeros(367, np.float32), 0.0, False, False, {}

    path = str(tmp_path / "old")
    PPO("MlpPolicy", Old(), n_steps=64, batch_size=64).save(path)
    with pytest.raises(RuntimeError, match="旧格式") as e:
        rl_train.load_ppo(path, make_gym_env(SyntheticBackend()), n_steps=64)
    assert "367" in str(e.value) and str(OBS_DIM) in str(e.value)
