import rl_hud


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _p(mode="train", **kw):
    clock = Clock()
    views = []
    p = rl_hud.RLProgress(mode=mode, n_steps=4, render=views.append, clock=clock,
                          render_interval=0.5, **kw)
    return p, clock, views


def test_round_and_total_counters():
    p, clock, _ = _p()
    p.start_round(300)
    for _ in range(3):
        p.on_step({"escrow_settled": 1, "has_player": True, "control": "policy"}, 0.5)
    v = p.view()
    assert v["round"] == "第 1 局"
    assert v["round_line"] == "本局  3 步 · 击杀 3 · 回报 +1.5"
    assert v["total_line"].startswith("累计  3 步 · 击杀 3")
    assert v["remain"] == "本局剩余 05:00"
    assert v["level"] == "ok"


def test_rollout_fill_resets_on_update_and_counts_updates():
    p, _, _ = _p()
    p.start_round(60)
    for _ in range(3):
        p.on_step({"control": "policy"})
    assert p.view()["fill"] == 0.75
    p.on_step({"control": "policy"})
    p.on_update()
    v = p.view()
    assert v["fill"] == 0.0
    assert "本局更新 1 · 累计 1" in v["fill_text"]


def test_play_mode_has_no_fill_bar():
    p, _, _ = _p(mode="play")
    p.start_round(60)
    p.on_step({"control": "policy"})
    v = p.view()
    assert v["fill"] is None
    assert v["title"] == "AI 刷怪(推理)"


def test_history_keeps_finished_rounds_and_shows_live_round_last():
    p, _, _ = _p()
    for kills in (2, 0, 5):
        p.start_round(60)
        for _ in range(kills):
            p.on_step({"escrow_settled": 1, "control": "policy"})
        p.end_round("timeout")
    assert p.view()["history"] == [2, 0, 5]
    p.start_round(60)
    p.on_step({"escrow_settled": 1, "control": "policy"})
    v = p.view()
    assert v["history"] == [2, 0, 5, 1] and v["history_live"]


def test_history_is_capped():
    p, _, _ = _p()
    for _ in range(rl_hud.HISTORY_ROUNDS + 5):
        p.start_round(1)
        p.end_round("timeout")
    assert len(p.history) == rl_hud.HISTORY_ROUNDS


def test_kills_per_hour_waits_for_a_minute_of_data():
    p, clock, _ = _p()
    p.start_round(3600)
    p.on_step({"escrow_settled": 10, "control": "policy"})
    clock.t = 30
    assert "杀/时" not in p.view()["total_line"]
    clock.t = 600
    assert p.view()["total_line"].endswith("60 杀/时")


def test_phase_follows_control():
    p, _, _ = _p()
    p.start_round(60)
    p.on_step({"control": "dead"})
    assert p.view()["level"] == "bad"
    p.on_step({"control": "script"})
    assert p.view()["level"] == "warn"
    p.end_round("timeout")
    v = p.view()
    assert v["level"] == "idle" and v["remain"] == "上局: timeout"


def test_render_is_throttled_but_round_boundaries_always_render():
    p, clock, views = _p()
    p.start_round(60)                 # 强制画一次
    n = len(views)
    for _ in range(10):
        p.on_step({"control": "policy"})   # 时钟没动 -> 全被节流
    assert len(views) == n
    clock.t = 1.0
    p.on_step({"control": "policy"})
    assert len(views) == n + 1
    p.end_round("timeout")            # 局结束不节流
    assert len(views) == n + 2


def test_render_errors_never_propagate():
    def boom(view):
        raise RuntimeError("overlay gone")
    p = rl_hud.RLProgress(render=boom, clock=Clock())
    p.start_round(60)
    p.on_step({"control": "policy"})
    p.end_round("timeout")


def test_warning_when_player_is_rarely_decoded():
    p, _, _ = _p()
    p.start_round(60)
    for i in range(40):
        p.on_step({"has_player": i < 5, "control": "policy"})
    assert "解出了自己" in p.view()["warning"]


def test_warning_when_last_train_round_had_zero_updates():
    p, _, _ = _p()
    p.start_round(60)
    p.on_step({"has_player": True, "control": "policy"})
    p.end_round("died")
    assert "0 次梯度更新" in p.view()["warning"]


def test_no_warning_on_a_healthy_round():
    p, _, _ = _p()
    p.start_round(60)
    for _ in range(200):
        p.on_step({"has_player": True, "control": "policy"})
    assert p.view()["warning"] == ""


def test_metrics_formatting():
    p, _, _ = _p()
    p.set_metrics({"entropy": 5.21, "approx_kl": 0.0123, "explained_variance": None})
    assert p.view()["metrics"] == "熵 5.21 · KL 0.012"
