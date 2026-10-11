"""PPO 训练入口 —— 唯一一个需要 gymnasium / stable-baselines3 的文件。

rl_env.FlorrEnv 故意没继承 gymnasium.Env(那样它在没装 gym 的机器上连导入都不行,
测试也跑不了)。这里补上那层壳: 声明观测/动作空间, 转发 reset/step, 剩下的交给 sb3。

    冒烟(不碰游戏, 用合成后端把整条链路跑通):
        python rl_train.py --dry-run --steps 3000

    真跑(florr 开着, Chrome 带那三个启动参数, 人已经在刷怪区里):
        python rl_train.py --steps 300000 --save runs/desert

## ⚠️ 这条命令行**不能无人值守**

这里的后端没有 control_hint(见 live_backend), 也就是没有 rl_supervisor 那一层。
花朵一死, **没人去点"继续"** —— 策略会对着一屏静止的死亡结算界面继续"训练",
第一次死亡之后收集的全是垃圾, 而且不会有任何报错。

要无人值守就走 GUI 那条路(时块里打开"刷怪区内用 AI 战斗"): 那边 auto_farming 负责
死亡重开、寻路回区、换服、AFK, rl_supervisor 每拍仲裁谁在控。
这条命令行留着干两件事: --dry-run 验管线, 以及人在旁边盯着的短时实验。

## 两个真实时环境特有的坑, 都处理了

**PPO 收满一批就会停下来做梯度更新, 那期间没人发动作。** 键是按住的状态 ——
更新那十几秒里花朵会一直朝最后那个方向怼, 撞墙或者站在怪堆里被打死。所以挂了个
回调, 每批收完先把键全松开。

**更新完继续收数据时, 第一步的 dt 是那十几秒。** 环境本来就判 stalled 把这一步的
奖励清零, 不用额外处理 —— 记在这里是因为看日志时会发现每批第一步都是 stalled,
那是正常的。

## 从零开始学, 所以熵系数不低

用户选了不做行为克隆冷启动、直接在线学。奖励又是稀疏的(只有击杀那一刻结算),
初期策略基本靠瞎试撞上第一次击杀 —— ent_coef 压太低会让它过早收敛到"站着不动"
(存活分是白给的)。
"""
import argparse
import random
import sys
import time

import numpy as np

import rl_obs
from rl_env import ACTION_NVEC, OBS_DIM, FlorrEnv

# 观测各维都归一过, 实测绝对值不超过个位数。给一个远大于此的有限界: 既不会触发 sb3
# 的"无界观测空间"警告, 又能在真出 NaN/量级爆炸时被检查器抓到。
OBS_LIMIT = 1e3

DEFAULTS = dict(
    n_steps=1024,        # 10Hz 下约 102 秒一批; 再大的话每次更新的停顿太久
    batch_size=256,
    # GUI 那条路一批只有 64 步(见 rl_farm), 约一半的批一次击杀都没有 —— 那些批的
    # 优势基本是价值误差, 归一化后照样是满幅更新。同一批反复训 10 轮等于把噪声
    # 学进去; 压到 4 轮, 再用 target_kl 让漂远了的那一轮提前停。
    n_epochs=4,
    target_kl=0.02,
    gamma=0.995,         # 10Hz; 半衰期约 138 步 ≈ 14 秒, 够覆盖"接近-攻击-击杀"
    gae_lambda=0.95,
    ent_coef=0.01,       # 从零学 + 稀疏奖励, 压低会过早收敛到"站着不动吃存活分"
    learning_rate=3e-4,
    clip_range=0.2,
)


def _gym():
    try:
        import gymnasium as gym
    except ImportError:  # pragma: no cover - 装了才跑得到这
        raise SystemExit("缺 gymnasium: pip install 'stable-baselines3>=2.3,<3'")
    return gym


def load_ppo(path, env, n_steps):
    """加载存档, 但超参一律用**现在的** DEFAULTS(外加这次的 n_steps)。

    PPO.load 会把存档里的超参**全部**一起恢复。实机(2026-09-24)踩过 n_steps 那一回
    —— 存档是 256 建的, 之后改成 64、32 都没用。同一个坑对每个超参都成立: 改了
    DEFAULTS, 加载回来的旧存档照样用旧值, 而且不报错。GUI 和 --resume 都走这里。
    """
    from stable_baselines3 import PPO

    params = dict(DEFAULTS, n_steps=n_steps)
    params["batch_size"] = min(params["batch_size"], n_steps)
    try:
        return PPO.load(path, env=env, custom_objects=params)
    except ValueError as e:
        if "Observation spaces do not match" not in str(e):
            raise
        # 观测格式改过(比如 2026-09-24 自身块 11 -> 8 维)。sb3 只报一句空间不匹配, GUI 那条
        # 路上会连败三轮再静默退回规则战斗 —— 直接说清楚是怎么回事、怎么办。
        import re
        dims = re.findall(r"\((\d+),\)", str(e))
        old = dims[0] if dims else "?"
        raise RuntimeError(
            f"{path!r} 是旧格式的存档: 它的观测是 {old} 维, 现在是 {OBS_DIM} 维"
            "(观测格式改过)。旧存档用不了 —— 把它挪走/删掉让「边刷边继续训练」从零开始, "
            "或者在时块里换一个 rl_model 路径。") from e


def make_gym_env(backend, **env_kwargs):
    """后端 -> gymnasium.Env(自带一个新的 FlorrEnv)。"""
    return make_gym_env_from(FlorrEnv(backend, **env_kwargs))


def make_gym_env_from(inner):
    """把**已有的** FlorrEnv 包成 gymnasium.Env。类在函数里建 —— 模块级的话没装 gym
    就导入失败。

    rl_farm 跨局复用同一个环境和后端(重建要重新注入钩子、重扫 WASM 探针, 好几秒),
    所以它需要"包一个现成的", 而不是"顺便造一个新的"。
    """
    gym = _gym()

    class GymFlorrEnv(gym.Env):
        metadata = {"render_modes": []}

        def __init__(self):
            super().__init__()
            self.inner = inner
            self.observation_space = gym.spaces.Box(
                low=-OBS_LIMIT, high=OBS_LIMIT, shape=(OBS_DIM,), dtype=np.float32)
            self.action_space = gym.spaces.MultiDiscrete(list(ACTION_NVEC))

        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            return self.inner.reset(seed=seed, options=options)

        def step(self, action):
            return self.inner.step(action)

        def close(self):
            close = getattr(self.inner.backend, "close", None)
            if close:
                close()

    return GymFlorrEnv()


def progress_callback(every=50):
    """每 every 步打一行"现在到底在发生什么"。

    sb3 自己的日志要等收满一整批(默认 1024 步 ≈ 102 秒)才打第一张表 —— 在真游戏上
    那意味着好几分钟一个字都没有, 分不出"在跑只是慢"和"卡死了"。实机第一次跑就栽在
    这上面: 3.9 分钟零输出, 只能手动停掉。

    打的是能判断死活的量: 实际频率(掉到 10Hz 以下就是 CDP 撑不住)、解出自己的比例
    (0% = 画面解码坏了 —— 那种坏法不报错, 只是每个观测都是零向量)、control 分布
    (全是 unfocused = 键根本没进游戏)、累计击杀(一直 0 = 奖励没有任何信号)、
    按 R 的比例(随机初始化是 50%, 每按一次花瓣都要重新装填; 练会了应该掉到 1% 以下 ——
    熵奖励会一直把它往 50% 拉, 卡在百分之几不动就说明"按 R 的坏处"信号太弱)。
    """
    from stable_baselines3.common.callbacks import BaseCallback

    class Progress(BaseCallback):
        def __init__(self):
            super().__init__()
            self.every = every

        def _on_training_start(self):
            self.t0 = time.time()
            self._window()
            self.kills = 0

        def _window(self):
            self.n = self.with_player = self.stalled = self.swaps = 0
            self.mobs = []
            self.control = {}
            self.misses = {}
            self.reward = 0.0
            self.mark = time.time()

        def _on_step(self):
            for info in self.locals.get("infos", []):
                self.n += 1
                self.control[info.get("control", "?")] = \
                    self.control.get(info.get("control", "?"), 0) + 1
                self.with_player += bool(info.get("has_player"))
                if info.get("self_miss"):
                    m = info["self_miss"]
                    self.misses[m] = self.misses.get(m, 0) + 1
                self.mobs.append(info.get("n_mobs", 0))
                self.stalled += bool(info.get("stalled"))
                self.swaps += info.get("swap", 0)
                self.kills += info.get("escrow_settled", 0)
            for r in np.atleast_1d(self.locals.get("rewards", [])):
                self.reward += float(r)
            if self.n >= self.every:
                self._print()
                self._window()
            return True

        def _print(self):
            dt = max(time.time() - self.mark, 1e-9)
            mobs = sorted(self.mobs)[len(self.mobs) // 2] if self.mobs else 0
            ctrl = " ".join(f"{k}={v}" for k, v in
                            sorted(self.control.items(), key=lambda kv: -kv[1]))
            print(f"   {self.num_timesteps:>7} 步  {time.time() - self.t0:6.0f}s  "
                  f"{self.n / dt:4.1f}Hz | 解出自己 {self.with_player * 100 // self.n}% "
                  f"卡顿 {self.stalled} | 按R {self.swaps * 100 // self.n}% | "
                  f"怪中位 {mobs:2d} | {ctrl} | "
                  f"累计击杀 {self.kills} | 本段回报 {self.reward:+.2f}", flush=True)
            if self.misses:
                why = " ".join(f"{k}×{v}" for k, v in
                               sorted(self.misses.items(), key=lambda kv: -kv[1]))
                print(f"      没解出自己: {why}", flush=True)
            if self.control.get("unfocused", 0) > self.n // 2:
                print("      ⚠️  这一段大半的拍窗口没焦点 —— 按键进的是别的窗口, "
                      "这些拍全被截断, 等于没在训练。点回游戏窗口。", flush=True)

    return Progress()


def release_keys_callback():
    """每批 rollout 收完(= 要开始做梯度更新了)就把键全松开。

    不松的话, 更新那十几秒里花朵会一直按着最后那个方向键 —— 撞墙, 或者站在怪堆里
    被打死, 而这段时间没有任何动作能救它。
    """
    from stable_baselines3.common.callbacks import BaseCallback

    class ReleaseKeys(BaseCallback):
        def _on_step(self):
            return True

        def _on_rollout_end(self):
            self._release()

        def _on_training_end(self):
            self._release()

        def _release(self):
            for env in self.training_env.envs:
                env.unwrapped.close()

    return ReleaseKeys()


# ---------------------------------------------------------------- 合成后端
class SyntheticBackend:
    """不碰游戏的假后端 —— 把整条链路(观测编码/奖励/PPO/存档)跑通用。

    行为刻意简单: 怪随机分布, 攻击时近处的怪掉血, 掉光了发一个击杀事件, 站在怪旁边
    不动会掉血。够让奖励不恒为零, 从而能看出管线是通的; **不是**游戏的模拟器,
    在它上面学出来的策略拿到真服没有任何意义。
    """

    def __init__(self, *, seed=0, n_mobs=6, hz=50.0):
        self.rng = random.Random(seed)
        self.n_mobs = n_mobs
        self.period = 1.0 / hz
        self.reset()

    def reset(self):
        self.t = 0.0
        self.hp = 1.0
        self.x, self.y = 5000.0, 5000.0
        self.mobs = [self._spawn() for _ in range(self.n_mobs)]
        self.action = (0, 0, 0, 0)
        return self._state([], [])

    def _spawn(self):
        return {"name": self.rng.choice(rl_obs.SPECIES[:7]),
                "rarity_rank": self.rng.choice([0, 0, 0, 1, 2, 4, 5]),
                "hp": 1.0,
                "x": self.x + self.rng.uniform(-600, 600),
                "y": self.y + self.rng.uniform(-600, 600)}

    def send_action(self, action):
        self.action = tuple(int(a) for a in action)

    def next_state(self):
        from rl_actions import MOVE_KEYS

        self.t += self.period
        move, attack, defense, _ = self.action
        dx = ("d" in MOVE_KEYS[move]) - ("a" in MOVE_KEYS[move])
        dy = ("s" in MOVE_KEYS[move]) - ("w" in MOVE_KEYS[move])
        self.x += dx * 12.0
        self.y += dy * 12.0

        kills = {}
        for mob in self.mobs:
            d = ((mob["x"] - self.x) ** 2 + (mob["y"] - self.y) ** 2) ** 0.5
            if d > 200.0:
                continue
            if attack:
                mob["hp"] -= 0.08
            if not defense:
                self.hp -= 0.004
        for i, mob in enumerate(self.mobs):
            if mob["hp"] <= 0.0:
                rank = mob["rarity_rank"]
                kills[rank] = kills.get(rank, 0) + 1
                self.mobs[i] = self._spawn()
        drops = [(r, n) for r, n in kills.items() if self.rng.random() < 0.3]
        return self._state(sorted(kills.items()), drops)

    def _state(self, kill_events, drop_events):
        return {"t": self.t, "dt": self.period, "control": "policy",
                "player": {"hp": max(0.0, self.hp), "hp_secondary": 0.0,
                           "x": self.x, "y": self.y},
                "mobs": self.mobs, "others": [], "camera_approx": False,
                "kill_events": kill_events, "drop_events": drop_events}

    def close(self):
        pass


# ---------------------------------------------------------------- 真后端
def live_backend(args):
    import rl_backend

    backend = rl_backend.LiveBackend(hz=args.hz, counter_every=args.counter_every,
                                     move_channel=args.move_channel)
    print("注入钩子 + 探针, 关掉反转攻击/防御键…")
    report = backend.setup()
    for name, (status, detail) in report.items():
        print(f"   反转{name}: {status} {detail}")
    return backend


def main(argv=None):
    ap = argparse.ArgumentParser(description="在 florr 上用 PPO 学刷怪")
    ap.add_argument("--steps", type=int, default=100000, help="总步数")
    ap.add_argument("--hz", type=float, default=10.0, help="每秒几拍")
    ap.add_argument("--counter-every", type=int, default=1, help="每几拍读一次击杀计数")
    ap.add_argument("--move-channel", choices=("mouse", "keys"), default="mouse",
                    help="移动走哪条通道。默认鼠标 —— 跟寻路脚本同一条, 见 "
                         "rl_backend.MouseMover 的说明")
    ap.add_argument("--max-steps", type=int, default=2000, help="一局最多几步")
    ap.add_argument("--n-steps", type=int, default=DEFAULTS["n_steps"],
                    help="收满几步做一次梯度更新; 第一次实机跑建议调小(如 256)看得快")
    ap.add_argument("--progress-every", type=int, default=50,
                    help="每几步打一行进度; 0 = 不打")
    ap.add_argument("--save", default=None, help="存档前缀, 如 runs/desert")
    ap.add_argument("--resume", default=None, help="从这个 .zip 接着练")
    ap.add_argument("--save-every", type=int, default=10000, help="每几步存一次")
    ap.add_argument("--dry-run", action="store_true",
                    help="用合成后端跑, 不碰游戏 —— 验管线用")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-focus-gate", action="store_true",
                    help="不等游戏窗口拿到焦点就开始(基本只在 --dry-run 外调试时用)")
    args = ap.parse_args(argv)

    from stable_baselines3 import PPO
    from stable_baselines3.common.callbacks import CallbackList, CheckpointCallback
    from stable_baselines3.common.monitor import Monitor

    if args.dry_run:
        backend = SyntheticBackend(seed=args.seed, hz=args.hz)
    else:
        import cdp_bridge
        if not cdp_bridge.is_cdp_port_reachable():
            print("❌ CDP 端口不通 —— Chrome 要用那三个启动参数起")
            return 2
        backend = live_backend(args)
        # 从终端敲命令启动, 焦点就在终端上 —— 策略发的每个键都进终端, 环境把这些拍
        # 全标成 unfocused 截断掉, 训练看着在跑实际一步有效样本都没有。实机第一次
        # 诊断 100 拍全是 unfocused。
        import rl_backend
        if not args.no_focus_gate and not rl_backend.wait_for_focus(timeout=120.0):
            print("❌ 游戏窗口始终没有焦点, 不开训练 —— 这样跑等于白跑。"
                  "\n   (真要跑加 --no-focus-gate)")
            return 2

    env = Monitor(make_gym_env(backend, max_steps=args.max_steps))

    params = dict(DEFAULTS, n_steps=args.n_steps)
    params["batch_size"] = min(params["batch_size"], args.n_steps)
    if args.resume:
        print(f"接着 {args.resume} 练")
        model = load_ppo(args.resume, env, args.n_steps)
    else:
        model = PPO("MlpPolicy", env, verbose=1, seed=args.seed,
                    policy_kwargs=dict(net_arch=[256, 256]), **params)

    if not args.dry_run:
        secs = args.n_steps / args.hz
        print(f"每 {args.n_steps} 步做一次梯度更新 ≈ {secs:.0f} 秒游戏时间; "
              f"在那之前 sb3 自己不会打任何日志, 看下面的进度行。")

    callbacks = [release_keys_callback()]
    if args.progress_every:
        callbacks.append(progress_callback(args.progress_every))
    if args.save:
        # 检查点放 <前缀>_ckpt/, **不能**用前缀本身 —— sb3 会把它建成目录, 而
        # 最终存档是 <前缀>.zip。两者重名的话 rl_farm 那边 PPO.load(前缀) 会去打开
        # 一个目录, Windows 上炸 PermissionError(实机踩到过)。
        callbacks.append(CheckpointCallback(
            save_freq=args.save_every, save_path=f"{args.save}_ckpt", name_prefix="ppo"))

    started = time.time()
    try:
        model.learn(total_timesteps=args.steps, callback=CallbackList(callbacks),
                    progress_bar=False)
    except KeyboardInterrupt:
        print("\n(手动停止)")
    finally:
        env.close()
        if args.save:
            # 存成 <前缀>.zip —— 这正是 config 里 rl_model 填的那个前缀,
            # PPO.load(前缀) 直接就能找到。别再加 _final 后缀, 两边对不上。
            model.save(args.save)
            print(f"存档 -> {args.save}.zip")
    print(f"跑了 {(time.time() - started) / 60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
