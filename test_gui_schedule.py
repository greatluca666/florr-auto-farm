import os

import pytest

import enemy_detect
import gui_map_picker
import gui_schedule as gs


def _blk(**kw):
    base = dict(id="b", enabled=True, days=[0], start="09:00", end="12:00",
               profile="默认", map="desert", location=[1, 2],
               farming_area=[[0, 0], [9, 9]], farming_duration=300,
               consecutive_short_round_limit=2, enemy_ai_enabled=True,
               auto_switch_server=True)
    base.update(kw)
    return base


@pytest.mark.parametrize("raw, out", [
    ("小号2", "小号2"),
    ("main account", "main_account"),
    ("a/b\\c", "a_b_c"),
    ("  x  ", "x"),
    ("***", ""),
])
def test_safe_dirname(raw, out):
    assert gs._safe_dirname(raw) == out


def test_block_to_active_shapes():
    a = gs.block_to_active(_blk(map="ocean", location=(5, 6),
                               farming_area=[(1, 1), (2, 2)], farming_duration="120"))
    _off = {"enabled": False, "mod": "none", "digit": "1"}
    assert a == {
        "map": "ocean", "location": [5, 6], "farming_area": [[1, 1], [2, 2]],
        "farming_duration": 120, "consecutive_short_round_limit": 2,
        "enemy_ai_enabled": True, "auto_switch_server": True,
        "invert_attack": True, "invert_defense": False,
        "enter_game_swap": _off, "reach_area_swap": _off,
    }


def test_map_radio_state():
    # 蚁狱已解锁(本支的目的), 海洋继续置灰 —— 这次没有任何理由去验证它.
    assert gs._map_radio_state("desert") == "normal"
    assert gs._map_radio_state("ocean") == "disabled"
    assert gs._map_radio_state("anthell") == "normal"


def test_enemy_switch_is_only_enabled_where_species_are_known(monkeypatch):
    # GUI 得跟 worker 一致: main._apply_worker_config 会在没做索敌的图上把
    # enemy_ai_enabled 强制关掉, 界面上就不该让人以为开了能生效.
    assert enemy_detect.species_supported("desert") is True
    assert enemy_detect.species_supported("anthell") is False

    # 光测 species_supported 测不出 _sync_enemy_enabled 布线断没断(开关真
    # configure(state=...) 了吗? _on_map_change 真调它了吗?) —— 这里真造一个
    # TimeBlockEditor, 盯着 self._enemy 的 tk state 和提示文字看.
    # gui_map_picker._MAP_DIR 按 sys.argv[0] 算(平时是主程序同级 maps/); pytest
    # 跑起来 argv[0] 是 pytest 自己的路径, 这里 patch 成仓库真正的 maps/, 不然
    # TimeBlockEditor._build() 里 MapPicker 加载缩略图会 FileNotFoundError.
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))

    # 全仓库只有这一条用例真造 Tk root —— 所以 pytest 需要一个可用的显示
    # (macOS/Windows 桌面上直接跑没问题; 无头 CI / ssh 里会在这一行报
    # "no display name and no $DISPLAY environment variable" 之类, 那不是回归).
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        ed = gs.TimeBlockEditor(root, block=_blk(map="desert"), others=[],
                                profiles=[{"alias": "默认", "dir": "d"}],
                                on_save=lambda b: None)
        assert ed._enemy.cget("state") == "normal"
        assert ed._enemy_hint.cget("text") == "本图支持索敌"

        ed._map.set("anthell")
        ed._on_map_change()
        assert ed._enemy.cget("state") == "disabled"
        assert ed._enemy_hint.cget("text").startswith("本图暂不支持索敌")
    finally:
        root.destroy()


def test_anthell_calibration_hint_names_both_missing_pieces():
    # 蚁狱选中了也会被 main._route_blocker 拦停(路线没标定); GUI 提前提醒,
    # 这条提示必须点名两个缺的东西, 不然用户不知道该去补什么.
    hint = gs._anthell_calibration_hint()
    assert "garden.png" in hint
    assert "capture_map.py" in hint
    assert "ANTHELL_PORTAL" in hint


def test_validate_rejects_disabled_map():
    msg = gs.validate_block(_blk(map="ocean"), [])
    assert msg is not None and "暂不可用" in msg


def test_validate_accepts_desert_map():
    # _blk() 默认 map="desert" —— 已被 test_validate_ok 覆盖, 这里显式再钉一次
    assert gs.validate_block(_blk(map="desert"), []) is None


def test_validate_ok():
    assert gs.validate_block(_blk(), []) is None


def test_validate_no_days():
    assert "星期" in gs.validate_block(_blk(days=[]), [])


def test_validate_bad_time():
    assert gs.validate_block(_blk(start="9am"), [])


def test_validate_equal_times():
    assert gs.validate_block(_blk(start="09:00", end="09:00"), [])


def test_validate_all_day_equal_times_ok():
    assert gs.validate_block(_blk(start="00:00", end="00:00"), []) is None


def test_validate_no_point_no_area():
    assert gs.validate_block(_blk(location=None, farming_area=None), [])


def test_validate_bad_numbers():
    assert gs.validate_block(_blk(farming_duration=0), [])
    assert gs.validate_block(_blk(consecutive_short_round_limit=-1), [])


def test_validate_overlap_reports_other_id():
    other = _blk(id="blk-9", start="11:00", end="13:00")
    msg = gs.validate_block(_blk(id="mine"), [other])
    assert "blk-9" in msg


def test_validate_ignores_self_in_others():
    me = _blk(id="mine")
    assert gs.validate_block(me, [me]) is None


_OFF = {"enabled": False, "mod": "none", "digit": "1"}


class TestLoadoutSwapInGuiSchedule:
    def test_block_to_active_carries_chord(self):
        blk = _blk(enter_game_swap={"enabled": True, "mod": "k", "digit": "3"},
                   reach_area_swap={"enabled": True, "mod": "none", "digit": "7"})
        act = gs.block_to_active(blk)
        assert act["enter_game_swap"] == {"enabled": True, "mod": "k", "digit": "3"}
        assert act["reach_area_swap"] == {"enabled": True, "mod": "none", "digit": "7"}

    def test_block_to_active_defaults_missing_swaps(self):
        blk = _blk()
        blk.pop("enter_game_swap", None)
        blk.pop("reach_area_swap", None)
        act = gs.block_to_active(blk)
        assert act["enter_game_swap"] == _OFF
        assert act["reach_area_swap"] == _OFF

    def test_block_to_active_normalizes_bad_chord(self):
        blk = _blk(enter_game_swap={"enabled": 1, "mod": "ctrl", "digit": "x"})
        assert gs.block_to_active(blk)["enter_game_swap"] == _OFF

    def test_new_block_template_swaps_disabled(self):
        cfg = {"profiles": [{"alias": "默认", "dir": "d"}], "schedule": []}
        tpl = gs.new_block_template(cfg)
        assert tpl["enter_game_swap"] == _OFF
        assert tpl["reach_area_swap"] == _OFF

    def test_mod_label_maps_are_inverse(self):
        for k in ("none", "k", "l"):
            assert gs._SWAP_MOD_FROM_LABEL[gs._SWAP_MOD_LABELS[k]] == k

    def test_digit_values_are_1_through_0(self):
        assert gs._SWAP_DIGIT_VALUES == list("1234567890")


class TestInvertTogglesInGuiSchedule:
    def test_block_to_active_carries_invert(self):
        blk = _blk(invert_attack=False, invert_defense=True)
        act = gs.block_to_active(blk)
        assert act["invert_attack"] is False
        assert act["invert_defense"] is True

    def test_block_to_active_invert_defaults_when_missing(self):
        blk = _blk()
        blk.pop("invert_attack", None)
        blk.pop("invert_defense", None)
        act = gs.block_to_active(blk)
        assert act["invert_attack"] is True
        assert act["invert_defense"] is False

    def test_new_block_template_invert_defaults(self):
        cfg = {"profiles": [{"alias": "默认", "dir": "d"}], "schedule": []}
        tpl = gs.new_block_template(cfg)
        assert tpl["invert_attack"] is True
        assert tpl["invert_defense"] is False



@pytest.mark.parametrize("days, expected", [
    (list(range(7)), "每天"),
    ([4, 3, 2, 1, 0], "工作日"),
    ([6, 5], "周末"),
    ([], "未选星期"),
    ([0, 2, 4], "周一 三 五"),
])
def test_weekday_text(days, expected):
    assert gs.weekday_text(days) == expected


def test_anthell_blocker_none_when_calibrated(monkeypatch):
    import gui_map_picker
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    assert gs._anthell_blocker() is None


def test_anthell_blocker_warns_when_portal_missing(monkeypatch):
    import gui_map_picker
    import map_routes
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    monkeypatch.setattr(map_routes, "ANTHELL_PORTAL", None)
    assert gs._anthell_blocker() == gs._anthell_calibration_hint()


def test_anthell_blocker_warns_when_map_image_missing(monkeypatch, tmp_path):
    import gui_map_picker
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR", str(tmp_path))
    assert gs._anthell_blocker() == gs._anthell_calibration_hint()
