"""RL 刷怪 / 训练进度的聚合器 —— 喂给悬浮窗的「AI 训练」面板。

纯 Python, 不 import sb3 / tk / AppKit: 数据怎么攒、显示成什么字, 全在这里, 好单测。
画面由 overlay.show_training(view) 负责; 谁来喂数据见 rl_farm(训练走 sb3 回调,
推理走 run_round 的 on_step)。

跟终端日志(rl_train.progress_callback / main._rl_farming 末尾那张表)看的是同一批量,
只是换成一直挂在游戏画面上的形式 —— 实机用户问过"为什么看不到进展", 日志窗口在
全屏游戏后面根本看不到。
"""
import time

# 面板刷新节流: 策略 10Hz, 每拍都重画 tk / AppKit 没必要。
RENDER_INTERVAL_S = 0.5
HISTORY_ROUNDS = 20

# 这些量攒够多少拍才开始下"不对劲"的结论, 不然开局头几拍全是噪声。
_WARN_MIN_STEPS_PLAYER = 30
_WARN_MIN_STEPS_SWAP = 100

_PHASES = {
    # control 值 -> (显示文字, 等级). 等级沿用 overlay 的 ok / warn / bad / idle 配色.
    "policy": ("策略控制中", "ok"),
    "script": ("脚本接管中(拉回刷怪区 / 脱困)", "warn"),
    "afk": ("AFK 弹窗处理中", "warn"),
    "menu": ("在菜单, 等待重进", "warn"),
    "unfocused": ("窗口没焦点 —— 按键没进游戏", "bad"),
    "dead": ("死亡, 等待复活", "bad"),
    "lost": ("丢了自己的位置, 这局要结束", "bad"),
    "between": ("局间: 寻路 / 重开中", "idle"),
}


def _mmss(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class RLProgress:
    """mode: "train" | "play". n_steps: PPO 每收满多少步做一次梯度更新(训练模式才有意义)。
    render: callable(view) —— 节流后调用; None 就只攒数不画。"""

    def __init__(self, *, mode="train", n_steps=64, render=None, clock=time.monotonic,
                 render_interval=RENDER_INTERVAL_S):
        self.mode = mode
        self.n_steps = n_steps
        self._render = render
        self._clock = clock
        self._render_interval = render_interval
        self._last_render = None

        self.round_no = 0
        self.total_steps = 0
        self.total_kills = 0
        self.total_updates = 0
        self.active_seconds = 0.0     # 已结束各局的在区时长, 算"击杀/时"
        self.history = []             # 每局击杀
        self.metrics = {}
        self.last_round_updates = None
        self.last_reason = None
        self.rollout_fill = 0
        self.phase = "between"
        self._in_round = False
        self._reset_round()

    def _reset_round(self):
        self.steps = self.kills = self.swaps = self.updates = 0
        self.with_player = 0
        self.unfocused = 0
        self.reward = 0.0
        self.round_start = self._clock()
        self.round_deadline = None

    # ------------------------------------------------------------ 喂数据
    def start_round(self, duration):
        self.round_no += 1
        self._reset_round()
        self.round_deadline = self.round_start + float(duration)
        self._in_round = True
        self.phase = "policy"
        self.flush()

    def on_step(self, info, reward=0.0):
        info = info or {}
        self.steps += 1
        self.total_steps += 1
        k = int(info.get("escrow_settled", 0) or 0)
        self.kills += k
        self.total_kills += k
        self.swaps += int(info.get("swap", 0) or 0)
        self.reward += float(reward)
        self.with_player += bool(info.get("has_player"))
        control = info.get("control", "policy")
        self.unfocused += control == "unfocused"
        self.phase = control if control in _PHASES else "script"
        if self.mode == "train":
            self.rollout_fill = min(self.rollout_fill + 1, self.n_steps)
        self.tick()

    def on_update(self):
        """收满一批、做了一次梯度更新。"""
        self.updates += 1
        self.total_updates += 1
        self.rollout_fill = 0
        self.tick()

    def set_metrics(self, metrics):
        """sb3 logger 的 train/* 值(熵 / KL / 解释方差). 缺哪个就不显示哪个."""
        self.metrics = {k: v for k, v in (metrics or {}).items() if v is not None}

    def end_round(self, reason):
        if self._in_round:
            self.active_seconds += max(0.0, self._clock() - self.round_start)
            self.history = (self.history + [self.kills])[-HISTORY_ROUNDS:]
            self.last_round_updates = self.updates if self.mode == "train" else None
        self._in_round = False
        self.last_reason = reason
        self.phase = "between"
        self.flush()

    # ------------------------------------------------------------ 画
    def tick(self):
        now = self._clock()
        if self._last_render is not None and now - self._last_render < self._render_interval:
            return
        self.flush()

    def flush(self):
        self._last_render = self._clock()
        if self._render is not None:
            try:
                self._render(self.view())
            except Exception:
                # 悬浮窗出问题绝不能把训练带崩(overlay 自己也会锁死成空操作)。
                pass

    def kills_per_hour(self, now=None):
        now = self._clock() if now is None else now
        secs = self.active_seconds + (now - self.round_start if self._in_round else 0.0)
        if secs < 60:
            return None      # 不到一分钟的数没有意义, 开局会显示成几百杀/时
        return self.total_kills * 3600.0 / secs

    def warnings(self):
        out = []
        if self.steps >= _WARN_MIN_STEPS_PLAYER and self.with_player * 2 < self.steps:
            out.append(f"只有 {self.with_player * 100 // self.steps}% 的帧解出了自己 "
                       "—— 画面解码可能坏了, 这样学不到东西")
        if self.steps and self.unfocused * 2 > self.steps:
            out.append("大半的拍窗口没焦点 —— 点回游戏窗口")
        if self.steps >= _WARN_MIN_STEPS_SWAP and self.swaps * 5 > self.steps:
            out.append(f"按 R 占 {self.swaps * 100 // self.steps}% —— 还没学会少换装")
        if self.mode == "train" and self.last_round_updates == 0:
            out.append(f"上一局 0 次梯度更新 —— 局太短, 没收满 {self.n_steps} 步, "
                       "那批数据被丢掉了")
        return out

    def view(self, now=None):
        now = self._clock() if now is None else now
        text, level = _PHASES.get(self.phase, _PHASES["script"])
        if self._in_round and self.round_deadline is not None:
            remain = f"本局剩余 {_mmss(self.round_deadline - now)}"
        elif self.last_reason:
            remain = f"上局: {self.last_reason}"
        else:
            remain = ""
        kph = self.kills_per_hour(now)
        steps = self.steps or 1
        health = [f"解出自己 {self.with_player * 100 // steps}%" if self.steps else "解出自己 -",
                  f"按 R {self.swaps * 100 / steps:.1f}%" if self.steps else "按 R -"]
        m = self.metrics
        metrics = []
        if "entropy" in m:
            metrics.append(f"熵 {m['entropy']:.2f}")
        if "approx_kl" in m:
            metrics.append(f"KL {m['approx_kl']:.3f}")
        if "explained_variance" in m:
            metrics.append(f"解释方差 {m['explained_variance']:.2f}")
        train = self.mode == "train"
        return {
            "title": "AI 训练中" if train else "AI 刷怪(推理)",
            "round": f"第 {self.round_no} 局" if self.round_no else "等待开局",
            "phase": text,
            "level": level,
            "remain": remain,
            "round_line": f"本局  {self.steps} 步 · 击杀 {self.kills} · 回报 {self.reward:+.1f}",
            "total_line": (f"累计  {self.total_steps:,} 步 · 击杀 {self.total_kills}"
                           + (f" · {kph:.0f} 杀/时" if kph is not None else "")),
            # 训练模式才有: 离下一次梯度更新还差多少步 —— 能不能学到东西就看它会不会满。
            "fill": (self.rollout_fill / self.n_steps) if train and self.n_steps else None,
            "fill_text": (f"下次更新 {self.rollout_fill}/{self.n_steps} 步 · "
                          f"本局更新 {self.updates} · 累计 {self.total_updates}")
                         if train else "推理模式: 只用策略, 不更新",
            "health": " · ".join(health),
            "metrics": " · ".join(metrics),
            "history": list(self.history) + ([self.kills] if self._in_round else []),
            "history_live": self._in_round,
            "warning": (self.warnings() or [""])[0],
        }


def sb3_callback(progress):
    """把 RLProgress 接到 sb3 learn() 上的回调。类在函数里建 —— 没装 sb3 的机器也能
    import 本模块。"""
    from stable_baselines3.common.callbacks import BaseCallback

    class HudCallback(BaseCallback):
        def _on_step(self):
            infos = self.locals.get("infos", [])
            rewards = list(self.locals.get("rewards", []))
            for i, info in enumerate(infos):
                progress.on_step(info, float(rewards[i]) if i < len(rewards) else 0.0)
            return True

        def _on_rollout_start(self):
            # PPO 的顺序是: 收一批 -> dump 日志 -> train() 记下 train/* -> 下一批开始。
            # 所以这时候 logger 里放的正是上一次梯度更新的指标(还没被下一次 dump 清掉)。
            try:
                vals = self.model.logger.name_to_value
                ent = vals.get("train/entropy_loss")
                progress.set_metrics({
                    # sb3 记的是 entropy_loss = -熵; 显示成熵本身, 越学越小才直观。
                    "entropy": -ent if ent is not None else None,
                    "approx_kl": vals.get("train/approx_kl"),
                    "explained_variance": vals.get("train/explained_variance"),
                })
            except Exception:
                pass

        def _on_rollout_end(self):
            progress.on_update()

    return HudCallback()
