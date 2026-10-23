import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

import afk_solver as S

FIX = Path(__file__).parent / "test_frames" / "afk_popups"
MANIFEST = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
OK = [e for e in MANIFEST if e["status"] == "ok"]
FAIL = [e for e in MANIFEST if e["status"] != "ok"]


def _img(entry):
    im = cv2.imread(str(FIX / entry["file"]))
    assert im is not None, entry["file"]
    return im


def _length(route):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(route, route[1:]))


# ── 弹窗框的位置 ──────────────────────────────────────────────────────────────────

def test_popup_box_matches_the_real_geometry():
    # 10-15 工厂那次: 画布标题文字 (599.46, 267.77), 对上截图里框 ≈ (538,224)-(812,580)(1920 尺度)
    x0, y0, x1, y1 = S.popup_box((599.46, 267.77), 1.0)
    assert abs(x0 - 538) < 6 and abs(y0 - 224) < 6 and abs(x1 - 812) < 6 and abs(y1 - 580) < 6


def test_popup_box_scales_with_the_ui_scale():
    full = S.popup_box((1000.0, 500.0), 1.0)
    half = S.popup_box((1000.0, 500.0), 0.5)
    assert half[0] == pytest.approx(1000 + (full[0] - 1000) / 2)
    assert (half[2] - half[0]) == pytest.approx((full[2] - full[0]) / 2)


# ── 真实截图(2026-10-15 工厂 / 2026-10-18 蚁穴, 球有绿 / 青, 底有棕 / 灰, 管道有弯钩 / 螺旋 / 之字) ──

@pytest.mark.parametrize("e", OK, ids=[e["file"] for e in OK])
def test_real_popups_route_from_the_ball_to_the_ring_along_the_tube(e):
    out, why = S.solve_image(_img(e), tuple(e["roi"]))
    assert why == "ok" and out is not None
    bx, by, br = out["ball"]
    assert abs(bx - e["ball"][0]) < 2.5 and abs(by - e["ball"][1]) < 2.5
    ex, ey = out["end"]
    assert math.hypot(ex - e["end"][0], ey - e["end"][1]) < 3.0          # 金标: 目检过, 环心 ±0.4r
    assert out["rule"] == "hole"
    assert out["route"][0] == pytest.approx((bx, by))
    assert out["route"][-1] == pytest.approx((ex, ey))
    assert _length(out["route"]) == pytest.approx(e["length"], rel=0.08)
    mask = out["mask"]
    for x, y in out["route"]:                                            # 路线每个点都在管道里
        assert mask[int(round(y)), int(round(x))] > 0, (x, y)
    gaps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(out["route"], out["route"][1:])]
    assert max(gaps) <= 1.2 * br                                         # 点距够密, 球不会被甩出管道


@pytest.mark.parametrize("e", FAIL, ids=[e["file"] for e in FAIL])
def test_unreadable_real_popups_fail_safe_instead_of_dragging(e):
    out, why = S.solve_image(_img(e), tuple(e["roi"]))
    assert out is None and why == e["status"]


# ── 合成图: 把管道 / 球 / 环按弹窗的画法画出来, 逐条钉规则 ─────────────────────────────

def _synthetic(ring_a=True, ring_b=False, ball_at="a", w=140, h=180):
    """深棕框底 + 一条 L 形浅灰管道(宽 20): A 端在左上, B 端在右下; 环 = 管道端头圆心的暗色小圆 + 亮圈。"""
    img = np.full((h, w, 3), (30, 45, 70), np.uint8)                 # BGR 深棕
    a, corner, b = (30, 40), (30, 140), (110, 140)
    tube = (150, 160, 175)
    for p, q in ((a, corner), (corner, b)):
        cv2.line(img, p, q, tube, 20, cv2.LINE_AA)
    for p in (a, b):
        cv2.circle(img, p, 10, tube, -1, cv2.LINE_AA)
    for p, on in ((a, ring_a), (b, ring_b)):
        if on:
            cv2.circle(img, p, 7, (110, 120, 140), 1, cv2.LINE_AA)    # 亮圈
            cv2.circle(img, p, 5, (28, 40, 62), -1, cv2.LINE_AA)       # 暗内圈
    bp = {"a": a, "b": b, "mid": corner}[ball_at]
    cv2.circle(img, bp, 7, (110, 245, 60), -1, cv2.LINE_AA)           # 绿球
    return img


def test_synthetic_ring_at_the_far_end_is_the_target():
    img = _synthetic(ring_a=False, ring_b=True, ball_at="a")
    out, why = S.solve_image(img, (0, 0, 140, 180))
    assert why == "ok"
    assert math.hypot(out["end"][0] - 110, out["end"][1] - 140) < 3.5
    assert out["route"][0] == pytest.approx(out["ball"][:2])


def test_synthetic_ring_end_wins_even_when_the_ball_sits_at_the_ring_end_side():
    # 环在 A 端, 球在 B 端(拖到一半的情形): 终点必须是 A, 不能是"离球最远"以外的别的什么
    img = _synthetic(ring_a=True, ring_b=False, ball_at="b")
    out, why = S.solve_image(img, (0, 0, 140, 180))
    assert why == "ok"
    assert math.hypot(out["end"][0] - 30, out["end"][1] - 40) < 3.5


def test_synthetic_no_ring_means_no_drag():
    img = _synthetic(ring_a=False, ring_b=False)
    out, why = S.solve_image(img, (0, 0, 140, 180))
    assert out is None and "no_ring" in why


def test_synthetic_rings_at_both_ends_is_ambiguous_not_a_guess():
    img = _synthetic(ring_a=True, ring_b=True, ball_at="mid")     # 球在管道中间, 两头都像有环: 不猜
    out, why = S.solve_image(img, (0, 0, 140, 180))
    assert out is None and "ambiguous" in why


def test_synthetic_ball_in_the_middle_with_one_ring_still_finds_the_ring_end():
    img = _synthetic(ring_a=True, ring_b=False, ball_at="mid")
    out, why = S.solve_image(img, (0, 0, 140, 180))
    assert why == "ok" and math.hypot(out["end"][0] - 30, out["end"][1] - 40) < 3.5


def test_a_tube_coloured_region_touching_the_search_box_edge_is_a_leak_not_a_tube():
    # 框外有一大片跟管道同色的亮区(浅色地面)并且跟管道连着: 长出来的区域会碰到 ROI 边缘 = 漏了, 不能当管道拖
    img = _synthetic(ring_b=True, ball_at="a")
    ball = S.find_ball(img)
    cv2.rectangle(img, (0, 52), (20, 62), (150, 160, 175), -1)         # 一条同色带从管道 A 端下方一直连到左边缘
    assert S.tube_mask(img, ball, (0, 0, 140, 180)) is None
    assert S.tube_mask(_synthetic(ring_b=True, ball_at="a"), ball, (0, 0, 140, 180)) is not None   # 没有带子就是好的


def test_find_ball_ignores_green_outside_the_box():
    img = _synthetic(ring_b=True)
    cv2.circle(img, (5, 5), 8, (110, 245, 60), -1)                    # 框外也有一块绿(草地)
    ball = S.find_ball(img, (10, 10, 140, 180))
    assert ball is not None and abs(ball[0] - 30) < 2 and abs(ball[1] - 40) < 2


@pytest.mark.parametrize("hue", [0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165])
def test_the_ball_can_be_any_colour(hue):
    # 2026-10-18 夜: 球是黄色, 按"绿到青"写死的找球三次都 no_ball —— 球的颜色每个弹窗不一样, 什么色相都要认得出
    img = _synthetic(ring_b=True, ball_at="a")
    bgr = cv2.cvtColor(np.uint8([[[hue, 235, 250]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
    cv2.circle(img, (30, 40), 7, bgr, -1, cv2.LINE_AA)
    out, why = S.solve_image(img, (0, 0, 140, 180))
    assert why == "ok", (hue, why)
    assert math.hypot(out["ball"][0] - 30, out["ball"][1] - 40) < 2
    assert math.hypot(out["end"][0] - 110, out["end"][1] - 140) < 3.5


def test_a_grey_blob_is_not_a_ball():
    img = _synthetic(ring_b=True, ball_at="a")
    cv2.circle(img, (30, 40), 7, (150, 160, 175), -1)                # 把球涂成管道色 = 没有球
    assert S.find_ball(img, (0, 0, 140, 180)) is None


def test_ball_radius_hint_rejects_blobs_of_the_wrong_size():
    img = np.full((120, 160, 3), (30, 45, 70), np.uint8)
    cv2.circle(img, (40, 60), 4, (60, 230, 255), -1)                 # 太小(受击数字的碎片之类)
    cv2.circle(img, (110, 60), 14, (60, 230, 255), -1)               # 大小对得上
    ball = S.find_ball(img, None, r_hint=14)
    assert ball is not None and abs(ball[0] - 110) < 2
    assert S.find_ball(img, None, r_hint=40) is None                 # 提示 40: 两个都不对


def test_ball_search_prefers_the_round_blob_over_a_ragged_one():
    img = np.full((120, 160, 3), (30, 45, 70), np.uint8)
    cv2.circle(img, (110, 60), 10, (60, 230, 255), -1)               # 圆
    cv2.rectangle(img, (20, 20), (80, 30), (60, 230, 255), -1)       # 又长又大的亮条(伤害数字 / 光效): 不圆
    ball = S.find_ball(img)
    assert ball is not None and abs(ball[0] - 110) < 2


def test_a_lone_ragged_bright_streak_is_not_a_ball():
    img = np.full((120, 160, 3), (30, 45, 70), np.uint8)
    cv2.rectangle(img, (20, 20), (80, 26), (60, 230, 255), -1)       # 又细又长: 伤害数字 / 光效, 不是球
    assert S.find_ball(img) is None


def test_plan_from_page_gives_the_solver_a_ball_radius_that_follows_the_ui_scale(monkeypatch):
    seen = {}
    monkeypatch.setattr(S, "solve_image", lambda img, roi=None, r_hint=None: seen.update(roi=roi, r=r_hint) or (None, "x"))
    deps = FakeDeps(["present"], _img(_E0))
    deps.read_state = lambda: {"title": [500.0, 300.0, 0.711], "frames": 3, "cjk": True, "dpr": 1.0}
    S.plan_from_page(deps)
    assert seen["r"] == pytest.approx(S.BALL_R_BASE * 0.711)
    assert seen["roi"] == pytest.approx(S.popup_box((500.0, 300.0), 0.711))


def test_find_ball_none_without_a_ball():
    img = np.full((60, 60, 3), (30, 45, 70), np.uint8)
    assert S.find_ball(img) is None


def test_the_ball_size_sets_every_length_so_a_doubled_screenshot_gives_the_same_route():
    small = _synthetic(ring_b=True, ball_at="a")
    big = cv2.resize(small, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    a, _ = S.solve_image(small, (0, 0, 140, 180))
    b, _ = S.solve_image(big, (0, 0, 280, 360))
    assert a is not None and b is not None
    assert _length(b["route"]) == pytest.approx(2 * _length(a["route"]), rel=0.1)
    assert math.hypot(b["end"][0] / 2 - a["end"][0], b["end"][1] / 2 - a["end"][1]) < 3.0


# ── 拖动事件 ───────────────────────────────────────────────────────────────────────

ROUTE = [(100.0, 100.0), (110.0, 100.0), (120.0, 100.0), (120.0, 110.0)]


def test_drag_events_press_on_the_ball_walk_the_route_then_release_on_the_ring():
    ev = S.drag_events(ROUTE, dpr=1.0)
    kinds = [k for k, *_ in ev]
    assert kinds[0] == "move" and kinds[1] == "down" and kinds[-1] == "up"
    assert kinds.count("down") == 1 and kinds.count("up") == 1
    assert (ev[1][1], ev[1][2]) == (100.0, 100.0)                      # 在球上按下
    assert (ev[-1][1], ev[-1][2]) == (120.0, 110.0)                    # 在终点松手
    moves = [(x, y) for k, x, y, _ in ev if k == "move"][1:]
    assert moves == ROUTE[1:]                                          # 每个路线点都走到


def test_drag_events_pace_the_drag_by_distance():
    ev = S.drag_events(ROUTE, dpr=1.0, speed=100.0)
    step_delays = [d for k, x, y, d in ev[2:-2]]
    assert step_delays == pytest.approx([0.1, 0.1])                    # 前两段各 10px / 100 px/s
    assert ev[-2][3] == pytest.approx(0.1 + S.RELEASE_PAUSE)           # 最后一步 10px, 走完再停一下才松手
    assert ev[-1][3] == 0.0


def test_drag_events_divide_by_the_device_pixel_ratio():
    ev = S.drag_events(ROUTE, dpr=2.0, speed=100.0)
    assert (ev[1][1], ev[1][2]) == (50.0, 50.0)                        # CSS 像素 = 截图像素 / dpr
    assert ev[2][3] == pytest.approx(0.1)                              # 步长(截图像素)不变, 速度按截图像素算


def test_run_drag_sends_every_event_and_sleeps_between():
    sent, slept = [], []
    S.run_drag(S.drag_events(ROUTE, speed=100.0), lambda *a: sent.append(a), slept.append)
    assert [k for k, *_ in sent] == ["move", "down", "move", "move", "move", "up"]
    assert sum(slept) == pytest.approx(S.PRESS_PAUSE * 2 + 0.1 + 0.1 + 0.1 + S.RELEASE_PAUSE, rel=0.01)


def test_run_drag_releases_the_button_if_sending_fails_midway():
    sent = []

    def send(kind, x, y):
        sent.append(kind)
        if len(sent) == 4:
            raise RuntimeError("ws closed")

    with pytest.raises(RuntimeError):
        S.run_drag(S.drag_events(ROUTE), send, lambda s: None)
    assert sent[-1] == "up"                                            # 别让鼠标键卡在按下状态


def test_run_drag_does_not_release_a_button_it_never_pressed():
    sent = []

    def send(kind, x, y):
        sent.append(kind)
        raise RuntimeError("never connected")

    with pytest.raises(RuntimeError):
        S.run_drag(S.drag_events(ROUTE), send, lambda s: None)
    assert sent == ["move"]


class _FakeWs:
    def __init__(self):
        self.sent = []
        self.queue = []

    def send(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        self.queue.append(json.dumps({"method": "Page.noise"}))        # 混进一条无关事件
        self.queue.append(json.dumps({"id": msg["id"], "result": {}}))

    def recv(self):
        return self.queue.pop(0)

    def close(self):
        self.closed = True


def test_cdp_mouse_events_carry_the_button_state():
    ws = _FakeWs()
    with S.CdpMouse(ws) as m:
        m.event("move", 10, 20)
        m.event("down", 10, 20)
        m.event("move", 15, 25)
        m.event("up", 15, 25)
        m.event("move", 30, 30)
    sent = [x for x in ws.sent if x["method"].startswith("Input.")]
    p = [x["params"] for x in sent]
    assert all(x["method"] == "Input.dispatchMouseEvent" for x in sent)
    assert (p[0]["type"], p[0]["buttons"]) == ("mouseMoved", 0)
    assert (p[1]["type"], p[1]["button"], p[1]["buttons"], p[1]["clickCount"]) == ("mousePressed", "left", 1, 1)
    assert (p[2]["type"], p[2]["button"], p[2]["buttons"]) == ("mouseMoved", "left", 1)      # 按住时的移动
    assert (p[3]["type"], p[3]["buttons"]) == ("mouseReleased", 0)
    assert (p[4]["type"], p[4]["buttons"]) == ("mouseMoved", 0)                              # 松手后不再算按住
    assert (p[2]["x"], p[2]["y"]) == (15.0, 25.0)


def test_cdp_mouse_turns_on_focus_emulation_first_and_survives_it_failing():
    ws = _FakeWs()
    with S.CdpMouse(ws) as m:
        m.event("move", 1, 1)
    assert ws.sent[0]["method"] == "Emulation.setFocusEmulationEnabled"
    assert ws.sent[0]["params"] == {"enabled": True}                  # 页面失焦时有的游戏会忽略鼠标输入
    assert ws.sent[1]["method"] == "Input.dispatchMouseEvent"

    ws2 = _FakeWs()
    real_send = ws2.send

    def send(data):
        if json.loads(data)["method"].startswith("Emulation."):          # 旧 Chrome 没这个命令
            ws2.queue.append(json.dumps({"id": json.loads(data)["id"], "error": {"message": "not found"}}))
        else:
            real_send(data)

    ws2.send = send
    with S.CdpMouse(ws2) as m:
        m.event("move", 1, 1)                                         # 焦点模拟失败不能拦住拖动
    assert ws2.sent[0]["method"] == "Input.dispatchMouseEvent"


def test_cdp_mouse_raises_on_a_cdp_error():
    ws = _FakeWs()
    ws.send = lambda data: ws.queue.append(json.dumps({"id": json.loads(data)["id"], "error": {"message": "x"}}))
    with S.CdpMouse(ws) as m:
        with pytest.raises(RuntimeError):
            m.event("move", 1, 1)


# ── 页面坐标 -> 屏幕坐标(系统鼠标兜底) ────────────────────────────────────────────

def test_page_to_screen_is_identity_when_fullscreen():
    info = {"fs": True, "sx": 0, "sy": 0, "ow": 1920, "oh": 1080, "iw": 1920, "ih": 1080, "sw": 1920, "sh": 1080}
    assert S.page_to_screen((300.0, 200.0), info, 1920) == (300.0, 200.0)


def test_page_to_screen_adds_the_window_offset_and_browser_chrome_when_windowed():
    info = {"fs": False, "sx": 100, "sy": 50, "ow": 1000, "oh": 700, "iw": 984, "ih": 640, "sw": 1920, "sh": 1080}
    x, y = S.page_to_screen((300.0, 200.0), info, 1920)
    assert x == pytest.approx(100 + 8 + 300)                          # 左边框 (1000-984)/2
    assert y == pytest.approx(50 + (700 - 640 - 8) + 200)             # 顶栏 = 总差 - 底边框


def test_page_to_screen_scales_to_pyautoguis_coordinate_system():
    info = {"fs": True, "sx": 0, "sy": 0, "ow": 1536, "oh": 864, "iw": 1536, "ih": 864, "sw": 1536, "sh": 864}
    x, y = S.page_to_screen((300.0, 200.0), info, 1920)               # DPI 感知: pyautogui 是物理像素 1920 宽
    assert (x, y) == pytest.approx((375.0, 250.0))


# ── 三态 ──────────────────────────────────────────────────────────────────────────

def test_popup_state_distinguishes_present_gone_and_unknown():
    assert S.popup_state({"title": [1, 2, 1.0], "frames": 3, "cjk": True}) == "present"
    assert S.popup_state({"title": None, "frames": 3, "cjk": True}) == "gone"
    assert S.popup_state({"title": None, "frames": 0, "cjk": False}) == "unknown"      # 缓冲是空的 != 弹窗没了
    assert S.popup_state({"title": None, "frames": 3, "cjk": False}) == "unknown"      # 没有中文字 = 不是中文客户端 / 没画
    assert S.popup_state(None) == "unknown"


# ── 编排 ──────────────────────────────────────────────────────────────────────────

class FakeDeps:
    """脚本化的页面: states 是每次 read_state 依次返回的三态; 拖动记录下来。"""

    def __init__(self, states, shot):
        self.states = list(states)
        self.shot = shot
        self.reads = 0
        self.drags = []
        self.slept = []

    def _info(self, st):
        base = {"dpr": 1.0, "sx": 0, "sy": 0, "ow": 1920, "oh": 1080, "iw": 1920, "ih": 1080,
                "sw": 1920, "sh": 1080, "fs": True, "frames": 3, "cjk": True}
        if st == "present":
            base["title"] = ENTRY_TITLE
        elif st == "gone":
            base["title"] = None
        else:
            base.update(title=None, frames=0, cjk=False)
        return base

    def read_state(self):
        self.reads += 1
        st = self.states[min(self.reads - 1, len(self.states) - 1)]
        return self._info(st)

    def screenshot(self):
        return self.shot

    def drag_cdp(self, events):
        self.drags.append(("cdp", events))

    def drag_os(self, events, info):
        self.drags.append(("os", events))

    def sleep(self, s):
        self.slept.append(s)


# 夹具里第一张图的框 (339,284)-(476,462)(半尺寸截图); 合成一个对得上的"标题文字位置":
# popup_box((tx,ty),1.0) = (tx-62, ty-44, tx+212, ty+312) —— 反推成让框正好是 ROI 的 ENTRY_TITLE。
_E0 = OK[0]
ENTRY_TITLE = [_E0["roi"][0] + 62.0, _E0["roi"][1] + 44.0, 0.5]       # 夹具是半尺寸截图: UI 缩放 0.5 => 球半径提示 7px


def _deps(states):
    deps = FakeDeps(states, _img(_E0))
    # 夹具是半尺寸截图, roi 直接给: 绕开 popup_box 的 1920 偏移
    S_orig = S.popup_box
    return deps, S_orig


def test_solve_popup_succeeds_when_the_popup_is_gone_after_the_first_drag(monkeypatch):
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(_E0["roi"]))
    deps = FakeDeps(["present", "gone"], _img(_E0))
    logs = []
    assert S.solve_popup(deps, log=logs.append) is True
    assert [m for m, _ in deps.drags] == ["cdp"]
    assert any("解开了" in l for l in logs)


def test_solve_popup_falls_back_to_the_os_mouse_on_the_second_attempt(monkeypatch):
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(_E0["roi"]))
    deps = FakeDeps(["present", "present", "present", "gone"], _img(_E0))
    assert S.solve_popup(deps, log=lambda m: None) is True
    assert [m for m, _ in deps.drags] == ["cdp", "os"]                 # 先 CDP, 没解开再换系统鼠标


def test_solve_popup_gives_up_after_max_attempts_and_hands_over(monkeypatch):
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(_E0["roi"]))
    deps = FakeDeps(["present"], _img(_E0))
    logs = []
    assert S.solve_popup(deps, max_attempts=3, log=logs.append) is False
    assert [m for m, _ in deps.drags] == ["cdp", "os", "cdp"]
    assert any("florr-auto-afk 接手" in l for l in logs)


def test_solve_popup_returns_true_without_dragging_when_the_popup_is_already_gone():
    deps = FakeDeps(["gone"], _img(_E0))
    assert S.solve_popup(deps, log=lambda m: None) is True
    assert deps.drags == []


def test_the_default_attempt_budget_is_three_and_it_is_what_solve_popup_uses(monkeypatch):
    assert S.MAX_ATTEMPTS == 3
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(_E0["roi"]))
    deps = FakeDeps(["present"], _img(_E0))
    S.solve_popup(deps, log=lambda m: None)                            # 不传 max_attempts
    assert len(deps.drags) == 3


def test_solve_popup_never_drags_on_an_unknown_canvas_state():
    deps = FakeDeps(["unknown"], _img(_E0))
    assert S.solve_popup(deps, max_attempts=2, log=lambda m: None) is False
    assert deps.drags == []                                            # 读不到画布 != 弹窗没了, 也不能乱拖


def test_solve_popup_does_not_drag_when_the_picture_is_unreadable(monkeypatch):
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(_E0["roi"]))
    blank = np.full_like(_img(_E0), 40)
    deps = FakeDeps(["present"], blank)
    assert S.solve_popup(deps, max_attempts=2, log=lambda m: None) is False
    assert deps.drags == []


def test_solve_popup_survives_a_failing_drag_and_tries_again(monkeypatch):
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(_E0["roi"]))

    class Flaky(FakeDeps):
        def drag_cdp(self, events):
            raise RuntimeError("ws down")

    deps = Flaky(["present", "present", "gone"], _img(_E0))
    assert S.solve_popup(deps, log=lambda m: None) is True
    assert [m for m, _ in deps.drags] == ["os"]                        # CDP 炸了, 系统鼠标那次解开了


def test_solve_popup_survives_a_failing_screenshot(monkeypatch):
    class Broken(FakeDeps):
        def screenshot(self):
            raise RuntimeError("cdp down")

    deps = Broken(["present"], None)
    assert S.solve_popup(deps, max_attempts=2, log=lambda m: None) is False


# ── 系统鼠标兜底(pyautogui): 用假 pyautogui 验证事件序列 / 坐标折算 / 全局设置还原 ───────────────────

class _FakePyauto:
    PAUSE = 0.1
    FAILSAFE = True

    def __init__(self, width=1920):
        self.calls = []
        self._width = width

    def size(self):
        class _S:
            pass
        s = _S()
        s.width = self._width
        return s

    def moveTo(self, x, y):
        self.calls.append(("moveTo", round(x, 1), round(y, 1)))

    def mouseDown(self):
        self.calls.append(("down",))

    def mouseUp(self):
        self.calls.append(("up",))


def test_os_mouse_fallback_presses_walks_and_releases_and_restores_pyautogui_settings(monkeypatch):
    import sys
    fake = _FakePyauto(width=1920)
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    seen = {}
    real_sleep = S.time.sleep
    monkeypatch.setattr(S.time, "sleep", lambda s: seen.setdefault("pause_during", fake.PAUSE))
    info = {"fs": True, "sx": 0, "sy": 0, "ow": 1920, "oh": 1080, "iw": 1920, "ih": 1080, "sw": 1920, "sh": 1080}
    S.Deps().drag_os(S.drag_events(ROUTE, speed=100.0), info)
    kinds = [c[0] for c in fake.calls]
    assert kinds[0] == "moveTo" and "down" in kinds and kinds[-1] == "up"
    assert kinds.index("down") < kinds.index("up")
    assert fake.calls[kinds.index("down") - 1][1:] == (100.0, 100.0)          # 在球上按下(全屏: 页面坐标 = 屏幕坐标)
    assert fake.calls[-2][1:] == (120.0, 110.0)                               # 终点松手前移到终点
    assert seen["pause_during"] == 0                                          # 拖动期间 pyautogui 不睡 0.1 秒
    assert fake.PAUSE == 0.1 and fake.FAILSAFE is True                        # 全局设置还原
    monkeypatch.setattr(S.time, "sleep", real_sleep)


def test_os_mouse_fallback_restores_settings_even_when_the_drag_fails(monkeypatch):
    import sys
    fake = _FakePyauto()

    def boom(x, y):
        raise RuntimeError("no display")

    fake.moveTo = boom
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    info = {"fs": True, "ow": 1920, "oh": 1080, "iw": 1920, "ih": 1080, "sw": 1920, "sh": 1080}
    with pytest.raises(RuntimeError):
        S.Deps().drag_os(S.drag_events(ROUTE), info)
    assert fake.PAUSE == 0.1 and fake.FAILSAFE is True


# ── 诊断: 每次尝试留证据(日志 + 带标注的图 + 拖完球动没动) ──────────────────────────────────────

def test_annotate_crops_around_the_box_and_draws_the_plan():
    e = OK[0]
    img = _img(e)
    out, _ = S.solve_image(img, tuple(e["roi"]))
    pic = S.annotate(img, tuple(e["roi"]), out)
    x0, y0, x1, y1 = e["roi"]
    assert pic.shape[1] <= (x1 - x0) + 2 * S.DEBUG_MARGIN + 1 and pic.shape[0] <= (y1 - y0) + 2 * S.DEBUG_MARGIN + 1
    assert pic.shape[:2] != img.shape[:2]                              # 裁过了, 不是整幅
    assert (pic != img[y0 - S.DEBUG_MARGIN:y1 + S.DEBUG_MARGIN, x0 - S.DEBUG_MARGIN:x1 + S.DEBUG_MARGIN]).any()   # 画了东西


def test_annotate_without_a_plan_still_marks_the_search_box():
    e = FAIL[0]
    img = _img(e)
    pic = S.annotate(img, tuple(e["roi"]))
    ref = img[max(0, e["roi"][1] - S.DEBUG_MARGIN):e["roi"][3] + S.DEBUG_MARGIN,
              max(0, e["roi"][0] - S.DEBUG_MARGIN):e["roi"][2] + S.DEBUG_MARGIN]
    assert (pic != ref).any()


def test_save_debug_writes_the_image_and_keeps_only_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "debug_dir", lambda: str(tmp_path / "afk"))
    monkeypatch.setattr(S, "DEBUG_KEEP", 3)
    img = np.zeros((8, 8, 3), np.uint8)
    for i in range(6):
        S.save_debug(f"afk_{i:02d}.png", img)
    names = sorted(p.name for p in (tmp_path / "afk").iterdir())
    assert names == ["afk_03.png", "afk_04.png", "afk_05.png"]


def test_save_debug_never_raises(monkeypatch):
    monkeypatch.setattr(S, "debug_dir", lambda: (_ for _ in ()).throw(OSError("read-only")))
    S.save_debug("x.png", np.zeros((2, 2, 3), np.uint8))               # 不抛就算过


def test_describe_ball_motion_tells_the_three_cases_apart():
    end = (200.0, 100.0)
    assert "找不到球" in S.describe_ball_motion((100, 100, 7), None, end, 7)
    still = S.describe_ball_motion((100, 100, 7), (101, 100, 7), end, 7)
    assert "纹丝没动" in still and "没接受" in still                      # 游戏没接受拖动
    moved = S.describe_ball_motion((100, 100, 7), (150, 100, 7), end, 7)
    assert "纹丝没动" not in moved and "位移 50px" in moved and "离终点还有 50px" in moved


class _DebugDeps(FakeDeps):
    def __init__(self, states, shots):
        super().__init__(states, shots[0])
        self.shots = list(shots)
        self.n = 0
        self.saved = []

    def screenshot(self):
        i = min(self.n, len(self.shots) - 1)
        self.n += 1
        return self.shots[i]

    def save_debug(self, name, img):
        self.saved.append((name, img.shape))


def _two_shots():
    # 同一个弹窗的两张真截图: 球在起点 / 球在管道中途(解题器拖了一半)
    a = next(e for e in OK if e["file"].endswith("1792310618449.jpg"))
    b = next(e for e in OK if e["file"].endswith("1792310661545.jpg"))
    return a, _img(a), _img(b)


def test_a_drag_attempt_logs_the_plan_the_ball_motion_and_saves_pictures(monkeypatch):
    a, first, second = _two_shots()
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(a["roi"]))
    deps = _DebugDeps(["present", "present"], [first, second])         # 第 1 张用来识别, 第 2 张是拖完之后
    deps.read_state = lambda: {"title": [0.0, 0.0, 0.5], "frames": 3, "cjk": True, "dpr": 1.0}
    logs = []
    S.solve_popup(deps, max_attempts=1, log=logs.append)
    text = "\n".join(logs)
    assert "ROI=(" in text and "UI缩放 0.500" in text and "规则 hole" in text and "dpr 1" in text
    assert "球从 (" in text and "位移" in text                           # 对比了拖完球的位置(两张图球不在同一处)
    names = [n for n, _ in deps.saved]
    assert any(n.endswith("_a1_plan.png") for n in names) and any(n.endswith("_a1_after.png") for n in names)


def test_a_static_picture_after_the_drag_is_reported_as_the_game_ignoring_it(monkeypatch):
    a, first, _ = _two_shots()
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(a["roi"]))
    deps = _DebugDeps(["present", "present"], [first, first])          # 拖完图一模一样
    deps.read_state = lambda: {"title": [0.0, 0.0, 0.5], "frames": 3, "cjk": True, "dpr": 1.0}
    logs = []
    S.solve_popup(deps, max_attempts=1, log=logs.append)
    assert any("纹丝没动" in l for l in logs)


def test_a_failed_recognition_saves_the_picture_with_the_reason_in_the_name(monkeypatch):
    f = FAIL[0]
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(f["roi"]))
    deps = _DebugDeps(["present"], [_img(f)])
    deps.read_state = lambda: {"title": [0.0, 0.0, 0.5], "frames": 3, "cjk": True, "dpr": 1.0}
    logs = []
    S.solve_popup(deps, max_attempts=1, log=logs.append)
    assert any("认不出" in l and "ROI=(" in l for l in logs)
    assert any("_fail-" in n for n, _ in deps.saved)


def test_diagnostics_never_break_the_solve(monkeypatch):
    a, first, second = _two_shots()
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(a["roi"]))

    class Hostile(_DebugDeps):
        def save_debug(self, name, img):
            raise OSError("disk full")

    deps = Hostile(["present", "gone"], [first, second])
    seq = iter(["present", "gone"])

    def read_state():
        st = next(seq, "gone")
        return {"title": None if st == "gone" else [0.0, 0.0, 0.5], "frames": 3, "cjk": True, "dpr": 1.0}

    deps.read_state = read_state
    assert S.solve_popup(deps, max_attempts=1, log=lambda m: None) is True


def test_deps_without_a_save_debug_are_fine(monkeypatch):
    monkeypatch.setattr(S, "popup_box", lambda t, s=1.0: tuple(_E0["roi"]))
    deps = FakeDeps(["present", "gone"], _img(_E0))                    # FakeDeps 没有 save_debug
    assert S.solve_popup(deps, log=lambda m: None) is True
