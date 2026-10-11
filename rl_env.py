"""刷怪环境 —— 把观测编码、奖励、终止条件串起来。

**故意不继承 gymnasium.Env**: 那样这个文件在没装 gymnasium 的机器上就导入不了, 连测试
都跑不了。接口本身是 Gym 那一套(reset/step 的签名和返回一致), sb3 要的那层包装放在
rl_train.py —— 那里本来就非装 gymnasium 不可。

环境只管"策略在刷怪区里的这一段"。进图、走到刷怪区、死亡重开、换服、AFK 解题这些仍然
归现有脚本；脚本接管的那些 tick **不进 rollout** —— 不是策略干的, 不能让它背锅。后端
通过 state["control"] 告诉环境现在谁在控, 不是 "policy" 就截断这一段。

后端协议(rl_backend.py 实现真服版, 测试用桩):
    reset()        -> state
    send_action(a) -> None
    next_state()   -> state | None      None = 没有下一帧(连接断/录像放完)

state:
    t             单调秒
    player        {"hp","x","y","hp_secondary"} 或 None(菜单/死亡结算/解码失败)
    mobs          [{"x","y","hp","rarity_rank","name"}, ...]
    others        [{"x","y","level"}, ...]
    camera_approx 这一帧的自身读数是不是只是近似
    kill_events   [(稀有度档位, 个数), ...]
    drop_events   同上
    dt            距上次动作的真实间隔(秒)
    control       "policy" | "script" | "dead" | "menu"
"""
from collections import deque

import numpy as np

import rl_obs
from rl_reward import RewardTracker

# (移动 9 向含不动, 攻击, 防御, 按 R)
ACTION_NVEC = (9, 2, 2, 2)
OBS_DIM = rl_obs.OBS_DIM

# 一步跨过这么久就认为时间不可信(梯度更新的停顿/CDP 卡死) —— 奖励里存活/掉血/挂账全不算,
# 击杀/掉落/死亡照结。
#
# 原来 0.25: 实机(2026-09-24)"卡顿"稳定在每 50 步 6~9 次 ≈ 每秒一次, 正是仲裁器每秒
# 截三次屏的那一拍(节拍 ~0.14s + 截屏)。那一拍花朵一直按着策略上一拍的动作在走,
# 时间并不"不可信", 却每秒扔掉一拍的掉血和挂账。0.6 放过截屏拍, 仍拦得住以秒计的停顿。
MAX_STEP_DT_S = 0.6

HP_HISTORY = rl_obs.DANGER_WINDOW + 1


class FlorrEnv:
    def __init__(self, backend, *, max_steps=2000, max_step_dt_s=MAX_STEP_DT_S,
                 reward=None, terminate_on_death=True, loadout_epoch=None):
        """loadout_epoch: () -> int, 花瓣栏每回到一次已知基准(按了预设 / 重进游戏)就变。
        真服传 loadout_swap.baseline_epoch; 不给就只有死亡会让奇偶归零。"""
        self.backend = backend
        self.max_steps = max_steps
        self.max_step_dt_s = max_step_dt_s
        self.reward = reward if reward is not None else RewardTracker()
        self.terminate_on_death = terminate_on_death
        self._steps = 0
        self._hp_history = deque(maxlen=HP_HISTORY)
        self._loadout_parity = 0
        self._loadout_epoch = loadout_epoch or (lambda: 0)
        self._seen_epoch = self._loadout_epoch()
        self._swap = 0
        self._last_obs = np.zeros(OBS_DIM, dtype=np.float32)

    def _sync_loadout(self):
        """花瓣栏回到过基准 -> 奇偶归零。**只**在这时归零 —— 见 loadout_swap._epoch。"""
        epoch = self._loadout_epoch()
        if epoch != self._seen_epoch:
            self._seen_epoch = epoch
            self._loadout_parity = 0

    # ------------------------------------------------------------------ 内部
    def _encode(self, state):
        player = state.get("player")
        if player is not None:
            if player.get("hp") is not None:
                self._hp_history.append(player["hp"])
        if player is not None and player.get("hp") is None and self._hp_history:
            # 血量两处都没读到。观测里写 0 = "快死了", 策略会被假警报牵着走; 几拍之内
            # 血量变不了多少, 沿用上一次读到的。(奖励那边读不到就不算这一项, 不受影响。)
            player = dict(player, hp=self._hp_history[-1])
        return rl_obs.encode(player, state.get("mobs") or [], state.get("others") or [],
                             hp_history=self._hp_history,
                             loadout_parity=self._loadout_parity,
                             camera_approx=bool(state.get("camera_approx")))

    def _info(self, state, extra=None):
        info = {
            "control": state.get("control", "policy"),
            # 这一帧到底有没有解出自己。全程 0% = 画面解码坏了, 而那种坏法不会报错,
            # 只会让每个观测都是零向量, 策略照样"训练"几小时什么也学不到。
            "has_player": state.get("player") is not None,
            # 没解出自己时卡在哪一层(见 rl_scene.Scene.miss); 进度行按它分类计数。
            "self_miss": state.get("self_miss"),
            "n_mobs": len(state.get("mobs") or []),
            "loadout_parity": self._loadout_parity,
            # 这一拍按没按 R。随机初始化时每拍 50%; 练没练会"少按", 只能看这个的频率。
            "swap": self._swap,
            "steps": self._steps,
        }
        if extra:
            info.update(extra)
        return info

    # ------------------------------------------------------------------ Gym 接口
    def reset(self, seed=None, options=None):
        self._steps = 0
        self._hp_history.clear()
        self._swap = 0
        # 奇偶**不**在这里归零: AFK / 步数上限 / 短途拉回 / 窗口失焦都会 reset, 但都没
        # 碰过装备, 游戏里的花瓣栏还是翻过去的那样。
        self.reward.reset()
        state = self.backend.reset()
        self._sync_loadout()
        self._last_obs = self._encode(state)
        return self._last_obs, self._info(state, {"reset": True})

    def step(self, action):
        action = tuple(int(x) for x in action)
        if len(action) != len(ACTION_NVEC):
            raise ValueError(f"动作要 {len(ACTION_NVEC)} 维, 收到 {len(action)}")
        for i, (a, n) in enumerate(zip(action, ACTION_NVEC)):
            if not 0 <= a < n:
                raise ValueError(f"动作第 {i} 维越界: {a} 不在 [0,{n})")

        self.backend.send_action(action)
        # R 是相对翻转, 所以记的是"按了几次"的奇偶 —— 绝对状态读不到(见 rl_obs 的说明)。
        # 按下就算, 不管这一步后面发生了什么: 键已经发出去了, 我们对 loadout 的认知必须
        # 跟着**请求**走, 不是跟着这一步的结果走。
        self._swap = action[3]
        if action[3]:
            self._loadout_parity ^= 1

        state = self.backend.next_state()
        # 仲裁器在 next_state 里跑寻路/复活, 期间按的预设发生在这一拍的 R 之后。
        self._sync_loadout()
        if state is None:
            # 拿不到下一帧: 连接断 / 录像放完。用上一份真实观测截断, 不要编一个零向量 ——
            # PPO 会拿它去 bootstrap 价值估计, 喂零等于拿一个不存在的状态训练。
            return self._last_obs, 0.0, False, True, {"reason": "no_state"}

        self._steps += 1
        control = state.get("control", "policy")
        if control != "policy":
            # 这一拍读到的击杀/掉落照结: 后端是先读计数器、再问仲裁器谁在控, 所以它们
            # 发生在交出控制权之前, 是策略打的。脚本接管之后打的那些会在 reset() 里被
            # 丢掉(后端那边重建计数器快照), 不会混进来。
            reward, rinfo = self.reward.settle_counters(
                state.get("kill_events") or (), state.get("drop_events") or ())
            if control == "dead":
                # 这条是**唯一**能把死亡惩罚发出去的路: 死亡结算画面上血条没了, 画面解不出
                # 玩家, 奖励里 hp<=0 那个判据永远不成立(实机验证过)。
                # 先结算再罚 —— death_penalty 会把没兑现的挂账全部作废。
                death = self.reward.death_penalty()
                rinfo["parts"]["death"] = death
                reward += death
                rinfo["reward"] = reward
                self._loadout_parity = 0      # 死了重进 florr 会把 loadout 重置
                self._last_obs = self._encode(state)
                return (self._last_obs, float(reward), True, False,
                        self._info(state, {**rinfo, "reason": "died", "died": True}))
            # 脚本接管(拉回刷怪区 / 脱困 / 菜单) —— 这一段到此为止, 后面那些 tick 不是
            # 策略干的, 不能进 rollout。
            self._last_obs = self._encode(state)
            return (self._last_obs, float(reward), False, True,
                    self._info(state, {**rinfo, "reason": control}))

        dt = float(state.get("dt") or 0.0)
        stalled = dt > self.max_step_dt_s
        reward, rinfo = self.reward.step(
            t=state["t"], player=state.get("player"), mobs=state.get("mobs") or [],
            kill_events=state.get("kill_events") or (),
            drop_events=state.get("drop_events") or (),
            stalled=stalled)

        died = rinfo["died"]
        if died:
            self._loadout_parity = 0
        terminated = bool(died and self.terminate_on_death)
        truncated = (not terminated) and (died or self._steps >= self.max_steps)

        self._last_obs = self._encode(state)
        info = self._info(state, {"dt": dt, "stalled": stalled, **rinfo})
        if truncated and not died:
            info["reason"] = "max_steps"
        elif died:
            info["reason"] = "died"
        return self._last_obs, float(reward), terminated, truncated, info
