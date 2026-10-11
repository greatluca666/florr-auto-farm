"""用策略刷一局 —— auto_farming 的 combat="rl" 分支落在这。

分工跟当初定的一样: 进图/走到刷怪区/死亡重开/换服/AFK 解题全归脚本, 策略只管刷怪区
**里面**的战斗。这个模块是两边的接缝。

## 跨局保活

worker 的主循环一局接一局地调 auto_farming。每局都重建后端的话, 每次都要重新注入
canvas hook、重新扫 WASM 探针(要好几秒), 训练时还会把模型和学到的东西整个丢掉。
所以 FarmSession 把后端/环境/模型留着跨局复用, 只有真出事(CDP 断了、换服后页面换了)
才重建 —— 那种时候本来就得重新注入。

## 两种模式

play:  加载训好的策略, 只推理。给日常刷怪用。
train: 在真刷怪的同时继续学。sb3 的 learn() 自己拥有循环, 所以一局就是一次
       learn(reset_num_timesteps=False) —— 靠一个到点返回 False 的回调把它停在这局
       结束的时刻。

       **n_steps 必须比一局短, 这是硬约束不是调参。** PPO 收满 n_steps 才做一次
       梯度更新; 收不满就被停掉的话, 那批数据**整个丢弃**, 一次更新都不会发生。
       实机(2026-09-24)踩到: n_steps=256 而实际每局只有 81 / 139 步 —— 跑了几十分钟
       模型一直是随机初始化, 那几个击杀纯属撞上。而且这事没有任何报错, 日志上
       "累计击杀 11 / 回报 +15.91" 看着还挺像在学。
       默认 64。实机 38 局的分布: 一半的局只有 15~180 步, 另一半 300~1800 步。
       按这个分布, 256 只用得上 65% 的步数、总共 21 次更新; 64 能用上 85%、109 次;
       32 能用上 93%。**没有选 32** 是因为 gamma=0.995 的半衰期是 138 步 —— 32 步的
       rollout 连一个"接近-攻击-击杀"的回合都跨不完, GAE 算出来的优势基本是噪声。
       64 是"更新次数"和"每次更新有多少真信息"之间的折中。
       每局结束都报这一局做了几次更新 —— 0 次单独警告, 这是唯一能看出"其实没在学"
       的地方。
"""
import time

import rl_backend
import rl_supervisor
from rl_env import FlorrEnv

# 环境自己判出的、**就地恢复不了**的理由。死亡不在里面 —— 仲裁器能就地复活的话这一局
# 该继续(见 rl_supervisor.control 里的长注释: 一结束, 没收满的 rollout 就被整个丢掉,
# 而那恰好是死得快的那些局)。复活不了的情况由 supervisor.stop_reason 报上来。
ROUND_OVER = ("no_state",)


def run_round(env, act, *, deadline, clock=time.monotonic, max_resets=50,
              progress_every=0, announce=print, should_stop=None, on_step=None):
    """推理模式跑一局。返回 (exit_reason, stats)。

    截断分两种, 处理方式完全不同:
      * 脚本插手(拉回刷怪区/AFK)   -> 重开一段接着打, 这局没结束
      * 死亡/掉线/菜单             -> 这局结束, 交回上层

    max_resets 是防打转的闸: 要是每一步都在截断(比如窗口一直没焦点), 这里会变成
    "reset-step-reset"空转到 deadline。计数超了就带着 "thrash" 退出 —— 白跑一局
    总比让人以为在刷怪好。
    """
    obs, info = env.reset()
    steps = resets = kills = swaps = 0
    total_reward = 0.0
    reason = "timeout"
    while clock() < deadline:
        obs, reward, terminated, truncated, info = env.step(act(obs))
        steps += 1
        kills += info.get("escrow_settled", 0)
        swaps += info.get("swap", 0)
        total_reward += float(reward)
        if on_step is not None:
            on_step(info, reward)
        if progress_every and steps % progress_every == 0:
            announce(f"   策略 {steps:5d} 步 | 解出自己 {'是' if info.get('has_player') else '否'}"
                     f" | 怪 {info.get('n_mobs', 0):2d} | 累计击杀 {kills}"
                     f" | 累计回报 {total_reward:+.2f}")
        if not (terminated or truncated):
            continue
        reason = info.get("reason") or "unknown"
        stop = should_stop() if should_stop else None
        if stop:
            reason = stop
            break
        if reason in ROUND_OVER:
            break
        resets += 1
        if resets > max_resets:
            reason = "thrash"
            break
        obs, info = env.reset()
        reason = "timeout"
    return reason, {"steps": steps, "resets": resets, "kills": kills, "swaps": swaps,
                    "reward": round(total_reward, 2)}


class FarmSession:
    """跨局活着的那一份东西。worker 整个生命周期共用一个。

    deps 是脚本那边的函数(位置/在不在区内/寻路回去/AFK/菜单), 由 main 传进来 ——
    这个模块不 import main, 否则就循环依赖了。
    """

    def __init__(self, *, model_path=None, train=False, hz=10.0, counter_every=1,
                 max_steps=2000, check_interval=1.0, n_steps=64, progress_every=50,
                 loadout_epoch=None, backend_factory=None, model_factory=None, hud=None):
        self.model_path = model_path
        self.train = train
        self.hz = hz
        self.counter_every = counter_every
        self.max_steps = max_steps
        self.check_interval = check_interval
        self.n_steps = n_steps
        self.progress_every = progress_every
        # 花瓣栏回到基准的纪元(loadout_swap.baseline_epoch), 交给环境判断 R 的奇偶何时归零。
        # 由 main 传进来 —— 这个模块不 import loadout_swap(它顶上就 import pyautogui)。
        self.loadout_epoch = loadout_epoch
        self._backend_factory = backend_factory
        self._model_factory = model_factory
        # hud: callable(view) —— 悬浮窗「AI 训练」面板(overlay.show_training). 进度对象
        # 跨局活着(累计步数/击杀/每局趋势), 模型建好之后才知道真正的 n_steps, 所以懒建。
        self._hud = hud
        self.progress = None
        self.backend = None
        self.env = None
        self.model = None
        self.supervisor = None
        self.rounds = 0

    # ---------------------------------------------------------------- 组装
    def _new_backend(self, control_hint):
        if self._backend_factory is not None:
            return self._backend_factory(control_hint)
        # dump_dir: 策略在控时没解出自己就把那一帧存下来(节流 + 上限, 见 LiveBackend)。
        # 这条路无人值守, 证据得自己留 —— 等人盯着日志手动抓帧对不上时机。
        backend = rl_backend.LiveBackend(hz=self.hz, counter_every=self.counter_every,
                                         control_hint=control_hint, dump_dir="rl_debug")
        backend.setup()
        return backend

    def _new_model(self, env):
        if self._model_factory is not None:
            return self._model_factory(env)
        from stable_baselines3 import PPO

        import rl_train

        gym_env = rl_train.make_gym_env_from(env)
        params = dict(rl_train.DEFAULTS, n_steps=self.n_steps)
        params["batch_size"] = min(params["batch_size"], self.n_steps)
        if self.model_path and _exists(self.model_path):
            # **不能直接 PPO.load**: 它会把存档里的超参一起恢复, 包括 n_steps。实机
            # (2026-09-24)踩到 —— 存档是最早用 n_steps=256 建的, 之后不管把默认改成
            # 64 还是 32, 加载回来的模型永远用 256, 改了等于没改。而每局末尾那条警告
            # 打的是配置值, 所以它一直在骗人。load_ppo 把全部 DEFAULTS 强制套回去。
            return rl_train.load_ppo(self.model_path, gym_env, self.n_steps)
        if not self.train:
            raise RuntimeError(
                f"play 模式要一个训好的策略, 但 {self.model_path!r} 下没有存档文件"
                f"({self.model_path}.zip)。\n"
                f"   先练一个:  python rl_train.py --steps 20000 --save {self.model_path}\n"
                "   或者把这个时块的「边刷边继续训练」打开(从零开始学),"
                "或者把战斗模式改回规则。")
        return PPO("MlpPolicy", gym_env, verbose=0,
                   policy_kwargs=dict(net_arch=[256, 256]), **params)

    def teardown(self):
        """把后端放掉(松键、光标压回花朵、关连接)。页面换了之后必须重建。"""
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:
                pass
        self.backend = self.env = self.model = None

    # ---------------------------------------------------------------- 一局
    def run(self, deps, *, duration, clock=time.monotonic):
        """刷一局。deps: position/in_area/path_back/afk_paused/on_start_screen。

        返回 (exit_reason, stats)。exit_reason == "timeout" 才算刷满了这一局。
        """
        self.supervisor = rl_supervisor.FarmingSupervisor(
            check_interval=self.check_interval, **deps)
        if self.backend is None:
            self.backend = self._new_backend(self.supervisor.control)
            self.env = FlorrEnv(self.backend, max_steps=self.max_steps,
                                loadout_epoch=self.loadout_epoch)
        else:
            # 后端复用, 但仲裁器是每局新建的(节流基准/连续失败计数都该归零) ——
            # 所以要把它的钩子换成这一局的。
            self.backend.control_hint = self.supervisor.control
        # 仲裁器阻塞前要能交出输入 —— 后端是在它之后才有的, 所以这里回填。
        self.supervisor.yield_input = self.backend.release

        if self.model is None:
            self.model = self._new_model(self.env)

        self.rounds += 1
        deadline = clock() + duration
        progress = self._progress()
        progress.start_round(duration)
        reason = "error"
        try:
            if self.train:
                reason, stats = self._run_train(deadline, clock)
            else:
                reason, stats = run_round(
                    self.env, lambda o: self.model.predict(o, deterministic=False)[0],
                    deadline=deadline, clock=clock,
                    progress_every=self.progress_every,
                    should_stop=lambda: self.supervisor.stop_reason,
                    on_step=progress.on_step)
        finally:
            progress.end_round(reason)
            # 这一局结束时键还按着 / 光标还指在外面的话, 接下来寻路那几秒花朵会一边
            # 被脚本拽一边自己往外走。
            if self.backend is not None:
                self.backend.close()
        if reason in ("no_state",):
            # 页面没了 —— 钩子和计数器基准都得重来, 留着这个后端只会一直失败。
            self.teardown()
        stats["supervisor"] = self.supervisor.stats()
        if self.train and self.model_path:
            self.save()
        return reason, stats

    def _progress(self):
        import rl_hud

        if self.progress is None:
            self.progress = rl_hud.RLProgress(
                mode="train" if self.train else "play",
                n_steps=getattr(self.model, "n_steps", None) or self.n_steps,
                render=self._hud)
        return self.progress

    def _run_train(self, deadline, clock):
        from stable_baselines3.common.callbacks import CallbackList

        import rl_train

        stop = _deadline_callback(deadline, clock, self.supervisor)
        import rl_hud

        cbs = [rl_train.release_keys_callback(), stop,
               rl_hud.sb3_callback(self._progress())]
        if self.progress_every:
            # **必须挂**: sb3 自己要收满一整批(n_steps)才打第一张表, 10Hz 下就是几十秒
            # 一个字都没有。实机(2026-09-24)用户的原话是"为什么看不到进展" —— 当时
            # 这个回调只接在 rl_train.py 那条命令行路径上, GUI 这条路一行都没有。
            cbs.append(rl_train.progress_callback(self.progress_every))
        # reset_num_timesteps=False: 跨局接着数步数, 否则学习率调度和日志每局都归零。
        # **但它有个副作用**: sb3 在已有上一步观测时会跳过 env.reset()。两局之间脚本
        # 刚跑过寻路/换服/复活, 上一局结尾那份观测早就不是现在的画面了 —— 速度是跨
        # 几分钟算出来的, 装备奇偶/血量历史/计数器快照全是上一局的。清掉它, 逼 learn()
        # 开局先 reset 一次。
        self.model._last_obs = None
        self.model.learn(total_timesteps=10 ** 9, reset_num_timesteps=False,
                         callback=CallbackList(cbs))
        return stop.reason, {"steps": stop.steps, "resets": 0, "kills": stop.kills,
                             "swaps": stop.swaps,
                             "reward": round(stop.total_reward, 2),
                             "updates": stop.rollouts,
                             # 报**模型真正在用的**那个值, 不是配置值 —— 两者曾经
                             # 长期对不上(见 _new_model 里 custom_objects 的说明)。
                             "n_steps": getattr(self.model, "n_steps", self.n_steps)}

    def save(self):
        if self.model is not None and self.model_path:
            self.model.save(self.model_path)


def _deadline_callback(deadline, clock, supervisor):
    """sb3 回调: 到点、或这一局该结束了, 就让 learn() 停下来。

    类在函数里建 —— BaseCallback 只有装了 sb3 才有, 而这个模块要在没装的机器上也能
    import(测试、只跑脚本不训练的部署机)。
    """
    from stable_baselines3.common.callbacks import BaseCallback

    class Deadline(BaseCallback):
        def __init__(self):
            super().__init__()
            self.reason = "timeout"
            self.steps = 0
            self.kills = 0
            self.swaps = 0
            self.total_reward = 0.0
            # 收满一批 = 做了一次梯度更新。0 次就是这一局白跑了 —— 收不满的那批
            # sb3 会整个丢掉。
            self.rollouts = 0

        def _on_rollout_end(self):
            self.rollouts += 1

        def _on_step(self):
            self.steps += 1
            for r in self.locals.get("rewards", []):
                self.total_reward += float(r)
            for info in self.locals.get("infos", []):
                self.kills += info.get("escrow_settled", 0)
                self.swaps += info.get("swap", 0)
                r = info.get("reason")
                if r in ROUND_OVER:
                    self.reason = r
                    return False
            if supervisor.stop_reason:
                self.reason = supervisor.stop_reason
                return False
            return clock() < deadline

    return Deadline()


def _exists(path):
    """存档在不在。**必须是文件** —— os.path.exists 对目录也回 True, 而
    rl_train 的 CheckpointCallback 会按存档前缀建一个同名目录, 于是 PPO.load()
    去把目录当文件打开, 在 Windows 上炸 PermissionError(实机踩到过)。"""
    import os

    return os.path.isfile(path) or os.path.isfile(f"{path}.zip")


def preflight(model_path, train):
    """开工前就能判定的那部分: 这个时块的 RL 配置能不能跑起来。

    返回 (能不能, 不能的话为什么)。

    为什么值得单独有这么个函数: 缺存档这种事, 等到"寻路到刷怪区之后第一次交接"才
    发现, 意味着每发现一次都要先白跑一整段寻路。实机踩过 —— 连着四轮"寻路、到区、
    报错、重来"才看清是配置问题。而这个判断在 worker 启动那一刻就做得了。
    """
    if train:
        return True, ""      # 从零开始学不需要存档
    if _exists(model_path):
        return True, ""
    return False, (
        f"战斗模式是 RL, 但 {model_path!r} 下没有存档文件({model_path}.zip), "
        "而且「边刷边继续训练」是关的 —— 没有可用的策略。\n"
        "   三条出路: 打开这个时块的「边刷边继续训练」(从零开始学) / "
        f"先 python rl_train.py --steps 20000 --save {model_path} 练一个 / "
        "把战斗模式改回规则。")
