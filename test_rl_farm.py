"""刷一局的测试。桩环境 + 桩策略, 不碰游戏, 也不要求装 sb3。"""
import pytest

from rl_farm import ROUND_OVER, FarmSession, run_round


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.1          # 每次问时间都往前走一点, 免得测试里写死循环
        return self.t


class StubEnv:
    """按脚本回放 (terminated, truncated, reason)。"""

    def __init__(self, script):
        self.script = list(script)
        self.resets = 0
        self.steps = 0
        self.actions = []

    def reset(self, **kw):
        self.resets += 1
        return "obs", {}

    def step(self, action):
        self.actions.append(action)
        self.steps += 1
        term, trunc, reason = self.script.pop(0) if self.script else (False, False, None)
        return "obs", 0.0, term, trunc, {"reason": reason}


def act(obs):
    return (0, 0, 0, 0)


def run(script, *, deadline=5.0, **kw):
    """deadline 一定要**有限**: 无限的话, 一旦某个跳出条件被改坏, 测试不是变红而是
    挂死 —— 挂死的测试证明不了任何事, 还会把整条流水线卡住(变异验证时实际卡过)。"""
    env = StubEnv(script)
    reason, stats = run_round(env, act, deadline=deadline, clock=Clock(), **kw)
    return env, reason, stats


# ── 两种截断, 处理方式完全不同 ───────────────────────────────────────────────

def test_script_takeover_resets_and_keeps_farming():
    """脚本把人拉回刷怪区之后这局还没完 —— 重开一段接着打。"""
    env, reason, stats = run([(False, True, "script"), (False, False, None),
                              (False, False, None)] + [(False, False, None)] * 50,
                             deadline=2.0)
    assert stats["resets"] == 1
    assert env.resets == 2


@pytest.mark.parametrize("reason", ROUND_OVER)
def test_a_round_over_reason_stops_the_round(reason):
    """死亡/掉线/菜单要交回上层 —— 上层才会去点死亡结算、重连、换服。"""
    env, got, _ = run([(True, False, reason)])
    assert got == reason and env.resets == 1        # 没有重开


def test_timeout_when_the_deadline_passes():
    env, reason, stats = run([(False, False, None)] * 100, deadline=0.5)
    assert reason == "timeout" and stats["steps"] < 100


def test_thrashing_is_reported_not_hidden():
    """每一步都截断(比如窗口一直没焦点)时, 这里会变成 reset-step-reset 空转到时间用完。
    白跑一局总比让人以为在刷怪好。"""
    env, reason, stats = run([(False, True, "unfocused")] * 100, max_resets=3,
                             deadline=5.0)
    assert reason == "thrash" and stats["resets"] == 4


def test_the_policy_actually_drives():
    env, _, _ = run([(False, False, None)] * 3, deadline=0.35)
    assert env.actions and all(a == (0, 0, 0, 0) for a in env.actions)


# ── 跨局保活 ─────────────────────────────────────────────────────────────────

DEPS = dict(position=lambda: (5, 5), in_area=lambda p: True,
            path_back=lambda p: True, afk_paused=lambda: False,
            on_start_screen=lambda: False)


class FakeBackend:
    def __init__(self, control_hint):
        self.control_hint = control_hint
        self.closed = 0
        self.released = 0

    def release(self):
        self.released += 1

    def close(self):
        self.closed += 1


def session(script, **kw):
    made = []

    def backend_factory(hint):
        b = FakeBackend(hint)
        made.append(b)
        return b

    s = FarmSession(backend_factory=backend_factory,
                    model_factory=lambda env: _Model(), **kw)
    s._made = made
    s._script = script
    return s


class _Model:
    def predict(self, obs, deterministic=False):
        return (0, 0, 0, 0), None


def patched_round(monkeypatch, reason, steps=1):
    import rl_farm

    monkeypatch.setattr(rl_farm, "run_round",
                        lambda env, act, **kw: (reason, {"steps": steps, "resets": 0}))


def test_the_backend_survives_across_rounds(monkeypatch):
    """每局重建后端要重新注入 canvas hook、重扫 WASM 探针(好几秒), 训练时还会把模型
    整个丢掉。"""
    patched_round(monkeypatch, "timeout")
    s = session(None)
    s.run(DEPS, duration=1.0)
    s.run(DEPS, duration=1.0)
    assert len(s._made) == 1 and s.rounds == 2


def test_the_env_watches_the_loadout_epoch_it_was_given(monkeypatch):
    """奇偶只在花瓣栏真回到基准时归零 —— 环境得拿到 main 那边的纪元计数。"""
    patched_round(monkeypatch, "timeout")
    s = session(None, loadout_epoch=lambda: 7)
    s.run(DEPS, duration=1.0)
    assert s.env._loadout_epoch() == 7


def test_the_live_backend_saves_missed_frames_under_rl_debug(monkeypatch):
    """GUI 这条路是无人值守的 —— 证据得自己留, 等人盯着抓帧对不上时机。"""
    import rl_backend

    seen = {}

    class Fake(FakeBackend):
        def __init__(self, **kw):
            seen.update(kw)
            super().__init__(kw.get("control_hint"))

        def setup(self):
            pass

    monkeypatch.setattr(rl_backend, "LiveBackend", Fake)
    patched_round(monkeypatch, "timeout")
    s = FarmSession(model_factory=lambda env: _Model())
    s.run(DEPS, duration=1.0)
    assert seen["dump_dir"] == "rl_debug"


def test_a_dead_page_forces_a_rebuild(monkeypatch):
    """no_state = 页面没了。之前注入的钩子和计数器基准都不在了, 留着这个后端只会一直
    失败。"""
    patched_round(monkeypatch, "no_state")
    s = session(None)
    s.run(DEPS, duration=1.0)
    assert s.backend is None
    patched_round(monkeypatch, "timeout")
    s.run(DEPS, duration=1.0)
    assert len(s._made) == 2


def test_each_round_gets_a_fresh_supervisor(monkeypatch):
    """节流基准和连续失败计数都该归零 —— 上一局最后一次截屏是几分钟前的事了。"""
    patched_round(monkeypatch, "timeout")
    s = session(None)
    s.run(DEPS, duration=1.0)
    first = s.supervisor
    s.run(DEPS, duration=1.0)
    assert s.supervisor is not first
    assert s.backend.control_hint == s.supervisor.control   # 钩子换成了这一局的


def test_the_backend_is_released_at_the_end_of_every_round(monkeypatch):
    """这局结束时键还按着 / 光标还指在外面的话, 接下来寻路那几秒花朵会一边被脚本拽
    一边自己往外走。"""
    patched_round(monkeypatch, "timeout")
    s = session(None)
    s.run(DEPS, duration=1.0)
    assert s._made[0].closed == 1


def test_the_backend_is_released_even_when_the_round_blows_up(monkeypatch):
    import rl_farm

    def boom(env, act, **kw):
        raise RuntimeError("解码炸了")

    monkeypatch.setattr(rl_farm, "run_round", boom)
    s = session(None)
    with pytest.raises(RuntimeError):
        s.run(DEPS, duration=1.0)
    assert s._made[0].closed == 1


def test_play_mode_without_a_model_says_what_to_do():
    # FarmSession._new_model 在检查存档之前就 import stable_baselines3, 没装它时先炸 ModuleNotFoundError。
    pytest.importorskip("stable_baselines3")
    s = FarmSession(model_path="runs/nope", train=False,
                    backend_factory=lambda hint: FakeBackend(hint))
    with pytest.raises(RuntimeError, match="改回规则"):
        s.run(DEPS, duration=1.0)


# ── 存档路径 ─────────────────────────────────────────────────────────────────

def test_a_directory_is_not_a_model(tmp_path):
    """实机(2026-09-24)踩到的: rl_train 的 CheckpointCallback 会按存档前缀建一个同名
    **目录**。os.path.exists 对目录也回 True, 于是 PPO.load 去把目录当文件打开,
    Windows 上炸 PermissionError, 而且每一轮都炸。"""
    import rl_farm

    (tmp_path / "desert").mkdir()
    assert rl_farm._exists(str(tmp_path / "desert")) is False


def test_a_real_file_counts(tmp_path):
    import rl_farm

    (tmp_path / "desert.zip").write_bytes(b"x")
    assert rl_farm._exists(str(tmp_path / "desert")) is True
    (tmp_path / "plain").write_bytes(b"x")
    assert rl_farm._exists(str(tmp_path / "plain")) is True


def test_the_missing_model_error_says_how_to_make_one(tmp_path):
    pytest.importorskip("stable_baselines3")                # 同上: _new_model 先 import 再查存档
    s = FarmSession(model_path=str(tmp_path / "nope"), train=False,
                    backend_factory=lambda hint: FakeBackend(hint))
    with pytest.raises(RuntimeError, match="rl_train.py"):
        s.run(DEPS, duration=1.0)


# ── 开工前的自检 ─────────────────────────────────────────────────────────────

def test_preflight_passes_when_training_from_scratch(tmp_path):
    """从零开始学不需要存档 —— 这正是"第一次跑 RL"的正常姿势。"""
    import rl_farm

    ok, why = rl_farm.preflight(str(tmp_path / "nope"), True)
    assert ok is True and why == ""


def test_preflight_passes_with_a_real_model(tmp_path):
    import rl_farm

    (tmp_path / "m.zip").write_bytes(b"x")
    assert rl_farm.preflight(str(tmp_path / "m"), False)[0] is True


def test_preflight_catches_play_mode_without_a_model(tmp_path):
    """实机(2026-09-24): 连着四轮"寻路、到区、报错、重来"才看清是配置问题。每发现
    一次都要先白跑一整段寻路 —— 而这个判断在 worker 启动那一刻就做得了。"""
    import rl_farm

    ok, why = rl_farm.preflight(str(tmp_path / "nope"), False)
    assert ok is False
    assert "边刷边继续训练" in why and "rl_train.py" in why


def test_preflight_does_not_accept_a_directory(tmp_path):
    import rl_farm

    (tmp_path / "m").mkdir()
    assert rl_farm.preflight(str(tmp_path / "m"), False)[0] is False


# ── 看得见进展 ───────────────────────────────────────────────────────────────

def test_run_round_reports_progress_and_counts_kills(capsys):
    """实机(2026-09-24)用户的原话: "为什么看不到进展"。sb3 自己要收满一整批才打第一张
    表, 10Hz 下几十秒一个字都没有; 而推理模式压根没有 sb3 的日志。"""
    class KillEnv(StubEnv):
        def step(self, action):
            out = super().step(action)
            return out[:4] + ({**out[4], "escrow_settled": 1, "n_mobs": 3,
                               "has_player": True},)

    env = KillEnv([(False, False, None)] * 100)
    reason, stats = run_round(env, act, deadline=5.0, clock=Clock(), progress_every=5)
    out = capsys.readouterr().out
    assert "累计击杀" in out and "策略" in out
    assert stats["kills"] == stats["steps"] and stats["steps"] > 0


def test_run_round_stats_carry_kills_even_without_printing():
    env = StubEnv([(False, False, None)] * 100)
    _, stats = run_round(env, act, deadline=1.0, clock=Clock())
    assert "kills" in stats and "reward" in stats


# ── 训练模式那条路(之前一条测试都没有) ───────────────────────────────────────

class FakeLearner:
    """假模型, 按 sb3 的回调协议驱动一遍。够跑通 _run_train 里那几个回调。

    为什么值得有: 实机(2026-09-24)一局跑完在 stats 那行炸 AttributeError —— 回调上
    压根没有 kills 属性, 我那个补丁的缩进锚点对不上, str.replace 静默跳过了。全套
    1234 条测试全绿, 因为**这条路一条测试都没有**。
    """

    def __init__(self, steps=5, info_extra=None, n_steps=2):
        self.n_steps = n_steps
        self.info_extra = info_extra or {}
        self.poll = None          # 真后端每拍会调一次 control_hint; 这里模拟那一下
        self.num_timesteps = 0
        self.logger = None
        self.steps = steps
        self.saved = []
        self.released = 0
        # sb3 的 BaseCallback.init_callback 会断言 get_env() 非空; release_keys_callback
        # 还要遍历 .envs 去松键。
        outer = self

        class _Env:
            def close(self_inner):
                outer.released += 1

            unwrapped = property(lambda self_inner: self_inner)

        class _Vec:
            envs = [_Env()]

        self._vec = _Vec()

    def get_env(self):
        return self._vec

    def learn(self, total_timesteps, reset_num_timesteps=True, callback=None):
        callback.init_callback(self)
        callback.on_training_start({}, {})
        collected = 0
        for _ in range(self.steps):
            self.num_timesteps += 1
            if self.poll is not None:
                self.poll()
            info = {"escrow_settled": 1, "n_mobs": 2, "has_player": True,
                    "control": "policy"}
            info.update(self.info_extra)
            callback.update_locals({"infos": [info], "rewards": [0.5]})
            if not callback.on_step():
                break
            # sb3 收满 n_steps 才 on_rollout_end 一次(= 做一次梯度更新); 收不满就
            # 被停掉的话那批数据整个丢弃, 一次都不会调。
            collected += 1
            if collected % self.n_steps == 0:
                callback.on_rollout_end()
        callback.on_training_end()

    def save(self, path):
        self.saved.append(path)


def train_session(**kw):
    pytest.importorskip("stable_baselines3")
    learner = FakeLearner(**kw.pop("learner", {}))
    s = FarmSession(train=True, progress_every=2,
                    backend_factory=lambda hint: FakeBackend(hint),
                    model_factory=lambda env: learner, **kw)
    # 真后端每拍都会调一次 control_hint —— 仲裁器就是这么被驱动的。
    learner.poll = lambda: s.backend.control_hint()
    s._learner = learner
    return s


def test_train_round_reports_kills_and_reward():
    s = train_session()
    reason, stats = s.run(DEPS, duration=1e6)
    assert stats["kills"] == 5                    # 每步一个击杀事件
    assert stats["reward"] == pytest.approx(2.5)  # 每步 +0.5
    assert stats["steps"] == 5 and reason == "timeout"


def test_train_round_counts_r_presses():
    s = train_session(learner={"steps": 4, "info_extra": {"swap": 1}})
    _, stats = s.run(DEPS, duration=1e6)
    assert stats["swaps"] == 4


def test_play_round_counts_r_presses():
    class Swaps(StubEnv):
        def step(self, action):
            obs, r, term, trunc, info = super().step(action)
            info["swap"] = 1 if self.steps <= 2 else 0
            return obs, r, term, trunc, info

    env = Swaps([(False, False, None)] * 50)
    _, stats = run_round(env, act, deadline=0.55, clock=Clock())
    assert stats["swaps"] == 2 and stats["steps"] > 2


def test_train_round_stops_at_the_deadline():
    t = [0.0]

    def clock():
        t[0] += 1.0
        return t[0]

    s = train_session(learner={"steps": 100})
    reason, stats = s.run(DEPS, duration=3.0, clock=clock)
    assert reason == "timeout" and stats["steps"] < 100


def test_train_round_stops_when_the_supervisor_calls_it_over():
    """死亡/菜单要立刻交回上层 —— 不停的话 learn() 会在死亡结算画面上继续"训练",
    喂进去的全是一屏静止的结算界面。"""
    s = train_session(learner={"steps": 100})
    reason, stats = s.run(dict(DEPS, on_death_screen=lambda: True), duration=1e6)
    assert reason == "dead" and stats["steps"] < 100


def test_train_round_stops_on_a_round_over_reason_from_the_env():
    """环境自己判出的终止(死亡/掉线)同样要让 learn() 停下来。"""
    s = train_session(learner={"steps": 100, "info_extra": {"reason": "no_state"}})
    reason, stats = s.run(DEPS, duration=1e6)
    assert reason == "no_state" and stats["steps"] == 1


def test_train_round_saves_the_model_when_a_path_is_configured(tmp_path):
    s = train_session(model_path=str(tmp_path / "m"))
    s.run(DEPS, duration=1e6)
    assert s._learner.saved == [str(tmp_path / "m")]


# ── 一局有没有真的学到东西 ───────────────────────────────────────────────────

def test_a_round_reports_how_many_gradient_updates_happened():
    """PPO 收满 n_steps 才更新一次; 收不满就被停掉的话那批数据**整个丢弃**。
    实机(2026-09-24): n_steps=256 而每局只有 81/139 步 —— 跑了几十分钟一次更新都没有,
    模型一直是随机初始化。而且没有任何报错, 日志上"累计击杀 11 / 回报 +15.91"看着
    还挺像在学。这个计数是唯一能看出来的地方。"""
    s = train_session(learner={"steps": 7, "n_steps": 2})
    _, stats = s.run(DEPS, duration=1e6)
    assert stats["updates"] == 3               # 7 步 / 每 2 步一批
    assert stats["n_steps"] == s._learner.n_steps   # 报模型真在用的, 不是配置值


def test_a_round_too_short_to_fill_a_rollout_reports_zero_updates():
    s = train_session(learner={"steps": 3, "n_steps": 10})
    _, stats = s.run(DEPS, duration=1e6)
    assert stats["updates"] == 0 and stats["steps"] == 3


def test_the_default_rollout_fits_inside_a_real_round():
    """实机几十局: 短的只有 15~60 步(策略刚被丢进怪堆、还没学会攻击, 几秒就死),
    长的 595 步。默认 n_steps 必须小到连短局也能产出一次更新, 否则大半的局白跑。"""
    assert FarmSession().n_steps <= 64


# ── 存档会把超参一起带回来 ───────────────────────────────────────────────────

def tiny_env():
    pytest.importorskip("stable_baselines3")
    import rl_train

    return rl_train.make_gym_env(rl_train.SyntheticBackend())


def test_loading_a_model_forces_the_configured_rollout_size(tmp_path):
    """实机(2026-09-24): 存档是最早用 n_steps=256 建的。之后把默认改成 64、再改成 32,
    **一点用都没有** —— PPO.load 把存档里的 256 一起恢复了。对账才发现:
    1805 步只有 7 次更新(258 步/次), 1386 步只有 5 次(277 步/次)。
    而每局末尾那条警告打的是配置值, 所以它一直在说"要收满 64 步"。"""
    pytest.importorskip("stable_baselines3")
    from stable_baselines3 import PPO
    import rl_train

    path = str(tmp_path / "m")
    PPO("MlpPolicy", tiny_env(), n_steps=256, batch_size=64, verbose=0).save(path)

    s = FarmSession(model_path=path, train=True, n_steps=64,
                    backend_factory=lambda hint: FakeBackend(hint))
    s.supervisor = None
    import rl_env
    import rl_train

    model = s._new_model(rl_env.FlorrEnv(rl_train.SyntheticBackend()))
    assert model.n_steps == 64
    assert model.batch_size <= 64
    assert model.rollout_buffer.buffer_size == 64      # 缓冲区真的按新值建的


class LoggingSynthetic:
    """合成后端 + 记下 reset/step 的先后, 外加 FarmSession 要的 release/close。"""

    def __init__(self, control_hint):
        import rl_train

        self.control_hint = control_hint
        self.inner = rl_train.SyntheticBackend()
        self.log = []

    def reset(self):
        self.log.append("reset")
        return self.inner.reset()

    def send_action(self, action):
        self.inner.send_action(action)

    def next_state(self):
        self.log.append("step")
        return self.inner.next_state()

    def release(self):
        pass

    def close(self):
        pass


def test_every_training_round_starts_from_a_reset():
    """sb3 的 learn(reset_num_timesteps=False) 在已有上一步观测时**跳过 env.reset()**。
    跨局复用模型, 于是从第 2 局起: 第一拍的观测是上一局结尾(几分钟前、可能在地图另
    一头)的, 速度是跨几分钟算出来的, 装备奇偶/血量历史沿用上一局, 计数器快照也没
    重建 —— 只是碰巧被卡顿判定挡掉了奖励那部分。"""
    pytest.importorskip("stable_baselines3")
    made = []

    def factory(hint):
        made.append(LoggingSynthetic(hint))
        return made[-1]

    t = [0.0]

    def clock():
        t[0] += 1.0
        return t[0]

    s = FarmSession(train=True, n_steps=8, progress_every=0, backend_factory=factory)
    s.run(DEPS, duration=20.0, clock=clock)
    (backend,) = made
    backend.log.clear()
    s.run(DEPS, duration=20.0, clock=clock)
    assert backend.log[0] == "reset"


def test_the_round_reports_the_models_real_rollout_size(tmp_path):
    """报配置值的话, 存档里是别的数时那行警告就是假的。"""
    s = train_session()
    s._learner.n_steps = 999                 # 假模型自称用 999
    _, stats = s.run(DEPS, duration=1e6)
    assert stats["n_steps"] == 999


def test_the_supervisor_can_hand_the_input_back_to_the_script(monkeypatch):
    """仲裁器要做阻塞的事(等 AFK、寻路回区、就地复活)之前必须能松键。实机
    (2026-09-24): 防御键 shift 在整段寻路期间一直按着, florr 里防御会大幅减速 ——
    272 秒的一局里 200 秒耗在挪不动的寻路上, 策略只跑了 390 步。"""
    patched_round(monkeypatch, "timeout")
    s = session(None)
    s.run(DEPS, duration=1.0)
    s.supervisor.yield_input()
    assert s._made[0].released == 1


def test_play_round_feeds_the_hud_panel(monkeypatch):
    import rl_farm

    def fake_round(env, act, **kw):
        for _ in range(3):
            kw["on_step"]({"escrow_settled": 1, "control": "policy"}, 1.0)
        return "timeout", {"steps": 3, "resets": 0}

    monkeypatch.setattr(rl_farm, "run_round", fake_round)
    views = []
    s = session(None, hud=views.append)
    s.run(DEPS, duration=1.0)
    assert views, "面板一次都没画"
    assert views[0]["round"] == "第 1 局" and views[0]["title"] == "AI 刷怪(推理)"
    assert views[-1]["history"] == [3]
    assert views[-1]["remain"] == "上局: timeout"


def test_run_round_calls_on_step_every_step():
    seen = []
    _env, reason, stats = run([(False, False, None)] * 2 + [(False, True, "no_state")],
                              on_step=lambda info, r: seen.append(r))
    assert reason == "no_state"
    assert len(seen) == stats["steps"] == 3


def test_train_round_feeds_the_hud_panel_with_updates():
    views = []
    s = train_session(hud=views.append, learner={"steps": 8, "n_steps": 4})
    s.run(DEPS, duration=1e6)
    assert s.progress.total_updates == 2
    assert s.progress.total_kills == 8
    assert views[-1]["history"] == [8]
