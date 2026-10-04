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
        "enemy_rules": {"species": {}, "knobs": {}},
    }


def test_map_radio_state():
    for name in ("garden", "desert", "ocean", "jungle", "anthell", "sewers", "factory"):
        assert gs._map_radio_state(name) == "normal"
    assert gs._map_radio_state("hel") == "disabled"       # 冥界没有小地图


def test_enemy_switch_is_only_enabled_where_species_are_known(monkeypatch):
    # GUI 得跟 worker 一致: main._apply_worker_config 会在没做索敌的图上把
    # enemy_ai_enabled 强制关掉, 界面上就不该让人以为开了能生效.
    for name in ("garden", "desert", "ocean", "jungle", "anthell", "sewers", "factory"):
        assert enemy_detect.species_supported(name) is True, name

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
        assert ed._enemy.cget("state") == "normal"
        assert ed._enemy_hint.cget("text") == "本图支持索敌"

        ed._map.set("ocean")
        ed._on_map_change()
        assert ed._enemy.cget("state") == "normal"
        assert ed._enemy_hint.cget("text") == "本图支持索敌"

        # 没做索敌的图(合成一张空表): 开关置灰, 提示是不点名任何地图的通用文案。
        monkeypatch.setitem(enemy_detect.MAP_SPECIES, "jungle", frozenset())
        ed._map.set("jungle")
        ed._on_map_change()
        assert ed._enemy.cget("state") == "disabled"
        assert ed._enemy_hint.cget("text") == "本图暂不支持索敌"
    finally:
        root.destroy()


def test_anthell_calibration_hint_names_both_missing_pieces():
    # 蚁狱选中了也会被 main._route_blocker 拦停(路线没标定); GUI 提前提醒,
    # 这条提示必须点名两个缺的东西, 不然用户不知道该去补什么.
    hint = gs._anthell_calibration_hint()
    assert "garden.png" in hint
    assert "capture_map.py" in hint
    assert "ANTHELL_PORTAL" in hint
    # maps/garden.png 现在是仓库自带的(门全是墙那张) —— 别再叫人跑 `capture_map.py garden`
    # 去"生成"它, 那是当场覆盖。步骤跟 main._route_blocker 共用 map_routes 那一份。
    assert "capture_map.py garden`" not in hint and "capture_map.py garden " not in hint
    assert "git checkout -- maps/garden.png" in hint
    assert "PORTAL_OPENINGS" in hint


def test_validate_rejects_disabled_map():
    msg = gs.validate_block(_blk(map="hel"), [])
    assert msg is not None and "暂不可用" in msg


def test_every_map_has_a_chinese_label():
    import gui_theme
    assert [gui_theme.map_label(m) for m in ("garden", "desert", "ocean", "jungle", "anthell", "sewers", "factory")] \
        == ["花园", "沙漠", "海洋", "丛林", "蚁狱", "下水道", "工厂"]


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


# ── 第②轮: 索敌设置 ──────────────────────────────────────────────────────────

_NO_RULES = {"species": {}, "knobs": {}}


def test_block_to_active_carries_normalized_enemy_rules():
    blk = _blk(map="ocean", enemy_rules={
        "species": {"bubble": "ignore", "scorpion": "ignore", "shell": "dance"},
        "knobs": {"chase_max_px": 400, "swarm": "yes", "nope": 1}})
    assert gs.block_to_active(blk)["enemy_rules"] == {
        "species": {"bubble": "ignore"}, "knobs": {"chase_max_px": 400}}


def test_block_to_active_defaults_missing_enemy_rules():
    assert gs.block_to_active(_blk())["enemy_rules"] == _NO_RULES


def test_new_block_template_has_no_enemy_rules_key():
    assert "enemy_rules" not in gs.new_block_template({"profiles": [], "schedule": []})


@pytest.mark.parametrize("rules, want", [
    (None, "全部默认"), ({}, "全部默认"), (_NO_RULES, "全部默认"),
    ({"species": {"a": "ignore", "b": "avoid"}, "knobs": {"swarm": False}},
     "自定义 2 个物种, 1 项数值"),
    ({"species": {"a": "ignore"}}, "自定义 1 个物种"),
    ({"knobs": {"chase_max_px": 400, "swarm": True}}, "自定义 2 项数值"),
])
def test_rules_summary(rules, want):
    assert gs.rules_summary(rules) == want


def test_rules_for_map_drops_species_of_other_maps_and_keeps_knobs():
    rules = {"species": {"scorpion": "ignore", "beetle": "avoid"}, "knobs": {"chase_max_px": 400}}
    got = gs.rules_for_map(rules, "desert")
    assert got == rules and got is not rules and got["species"] is not rules["species"]
    assert gs.rules_for_map(rules, "garden") == {"species": {}, "knobs": {"chase_max_px": 400}}
    assert gs.rules_for_map(None, "garden") == _NO_RULES


def test_species_rows_cover_the_maps_species_with_names_and_kind_labels():
    for name in ("garden", "desert", "ocean", "jungle", "anthell", "sewers", "factory"):
        rows = gs.species_rows(name)
        assert [r[0] for r in rows] == sorted(enemy_detect.MAP_SPECIES[name])
        for slug, zh, en, kind in rows:
            assert zh and en and kind in ("主动", "被动", "不动", "绕圈")
            assert zh == enemy_detect.SPECIES_NAMES[slug]["zh"]
    kinds = {r[0]: r[3] for r in gs.species_rows("garden")}
    assert kinds["spider"] == "主动" and kinds["ladybug"] == "被动"
    assert kinds["rock"] == "不动" and kinds["hornet"] == "绕圈"
    assert gs.species_rows("atlantis") == []


def test_knob_defaults_are_the_engines_builtin_values():
    d = gs.knob_defaults("desert")
    assert d == {"hold_active_px": enemy_detect.ENGAGE_HOLD_PX,
                 "hold_passive_px": enemy_detect.PASSIVE_HOLD_PX,
                 "chase_max_px": enemy_detect.CHASE_MAX_PX,
                 "far_chase_px": enemy_detect.FAR_CHASE_PX,
                 "avoid_px": enemy_detect.DEFAULT_AVOID_TRIGGER_PX,
                 "cautious_px": enemy_detect.CAUTIOUS_HOLD_PX}
    assert gs.knob_defaults("anthell")["avoid_px"] == 200      # 蚁穴自己的躲避半径
    assert set(d) == set(gs.app_config.ENEMY_KNOB_RANGES)


def test_knob_applies_on_the_rarity_priority_map_only_the_avoid_and_cautious_knobs_count():
    # 躲避半径 / 躲避起点稀有度都喂给 flee 判定(classify_action → select_action 头部), 两种选目标策略都走;
    # 谨慎保持距离(cautious_px)两种策略的分支里都读(沙漠 Ultra 沙尘暴 / 仙人掌就是谨慎怪)。
    got = gs.knob_applies("desert")
    assert got == {"hold_active_px": False, "hold_passive_px": False, "chase_max_px": False,
                   "far_chase_px": False, "avoid_px": True, "cautious_px": True, "swarm": False,
                   "avoid_min_rarity": True}
    assert set(got) == set(gs.app_config.ENEMY_KNOB_KEYS)


@pytest.mark.parametrize("name", ["anthell", "garden", "ocean", "jungle", "sewers", "factory"])
def test_knob_applies_everything_on_the_nearest_policy_maps(name):
    assert gs.knob_applies(name) == dict.fromkeys(gs.app_config.ENEMY_KNOB_KEYS, True)


def test_the_knobs_knob_applies_greys_out_really_are_ignored_by_the_priority_policy():
    """knob_applies 说「沙漠上这几项没用」的依据: select_action 的 priority 分支不读它们。
    引擎哪天让 priority 分支也读了, 这条红 = 子窗该把它们放开。对照: nearest 分支读。"""
    dets = [{"species": "scorpion", "rarity": "Mythic", "screen_pos": (1100, 540), "confidence": 0.9},
            {"species": "beetle", "rarity": "Common", "screen_pos": (1000, 540), "confidence": 0.9}]
    kw = dict(center=(960, 540))
    base = enemy_detect.select_action(dets, target_policy="priority", **kw)
    assert base[0] == "chase"
    try:
        enemy_detect.set_rules({"knobs": {"hold_passive_px": 10, "hold_active_px": 10}})
        for extra in ({"engage_hold_px": 1}, {"chase_max_px": 1}, {"far_chase_px": 1},
                      {"swarm_enabled": False}, {}):
            assert enemy_detect.select_action(dets, target_policy="priority", **kw, **extra) == base
        # 对照: 最近优先的图上同一个旋钮会改变决策(甲虫在 40px 处: 默认追击上限内 -> 追; 调成 10 -> 漫游)
        near = enemy_detect.select_action(dets, target_policy="nearest", **kw)
        assert near[0] == "chase"
        assert enemy_detect.select_action(dets, target_policy="nearest", chase_max_px=10, **kw) \
            == ("wander", None)
    finally:
        enemy_detect.set_rules(None)


def test_rules_from_inputs_empty_means_defaults():
    rules, err = gs.rules_from_inputs({"rock": "", "bee": ""}, {"chase_max_px": "  "}, "")
    assert err is None and rules == _NO_RULES


def test_rules_from_inputs_builds_species_knobs_and_swarm():
    rules, err = gs.rules_from_inputs(
        {"rock": "ignore", "bee": "fight", "wasp": "avoid", "ladybug": ""},
        {"hold_active_px": " 150 ", "chase_max_px": "400", "avoid_px": ""}, "off")
    assert err is None
    assert rules == {"species": {"rock": "ignore", "bee": "fight", "wasp": "avoid"},
                     "knobs": {"hold_active_px": 150, "chase_max_px": 400, "swarm": False}}
    assert gs.rules_from_inputs({}, {}, "on")[0]["knobs"] == {"swarm": True}


@pytest.mark.parametrize("text", ["abc", "1.5", "-5", "1e3", "1_0", "9", "3001", "０５０"])
def test_rules_from_inputs_rejects_non_integers_and_out_of_range(text):
    rules, err = gs.rules_from_inputs({}, {"chase_max_px": text}, "")
    assert rules is None and "追击上限" in err


@pytest.mark.parametrize("text", ["1" * 7, "9" * 100, "1" * 4301], ids=["7-digits", "100-digits", "4301-digits"])
def test_rules_from_inputs_overlong_digit_strings_give_the_red_error_not_an_exception(text):
    # Python 3.11 的 int() 对 > 4300 位的字符串抛 ValueError; 全是数字所以过得了 _DIGITS_RE ——
    # 必须在 int() 之前按长度挡掉(合法最大值 3000 只有 4 位)。7 位是刚好越过 len > 6 门槛的一档。
    rules, err = gs.rules_from_inputs({}, {"chase_max_px": text}, "")
    assert rules is None
    assert "追击上限" in err and "10" in err and "3000" in err


def test_rules_from_inputs_writes_the_rarity_tier_only_when_picked():
    assert gs.rules_from_inputs({}, {}, "")[0] == _NO_RULES
    assert gs.rules_from_inputs({}, {}, "", "")[0] == _NO_RULES            # "" = 默认, 不写键
    rules, err = gs.rules_from_inputs({"rock": "ignore"}, {"avoid_px": "300"}, "off", "Legendary")
    assert err is None
    assert rules == {"species": {"rock": "ignore"},
                     "knobs": {"avoid_px": 300, "swarm": False, "avoid_min_rarity": "Legendary"}}
    assert gs.rules_from_inputs({}, {}, "", "never")[0]["knobs"] == {"avoid_min_rarity": "never"}


@pytest.mark.parametrize("bad", ["Common", "Rare", "ultra", "默认", None, 5])
def test_rules_from_inputs_rejects_a_tier_the_config_would_drop(bad):
    rules, err = gs.rules_from_inputs({}, {}, "", bad)
    assert rules is None and "躲避起点稀有度" in err


def test_tier_menu_labels_cover_every_config_choice_exactly_once_and_roundtrip():
    import enemy_species
    assert set(gs._TIER_LABELS) == {""} | set(enemy_species.AVOID_MIN_RARITY_CHOICES)
    assert len(set(gs._TIER_LABELS.values())) == len(gs._TIER_LABELS)       # 显示名不重复, 才能反查
    assert all(gs._TIER_FROM_LABEL[label] == key for key, label in gs._TIER_LABELS.items())
    assert "Ultra" in gs._TIER_LABELS[""]                                    # 默认档写在「默认」后面


def test_rules_from_inputs_range_edges_are_inclusive():
    rules, err = gs.rules_from_inputs({}, {"hold_passive_px": "10", "far_chase_px": "3000"}, "")
    assert err is None and rules["knobs"] == {"hold_passive_px": 10, "far_chase_px": 3000}


def test_enemy_rules_editor_and_dialog_wiring(monkeypatch):
    """真造 Tk root(跟上面 enemy_switch 那条一样需要显示): 按钮布线、换图过滤、_collect、子窗确认 / 报错。"""
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        rules = {"species": {"scorpion": "avoid", "beetle": "ignore"}, "knobs": {"chase_max_px": 400}}
        ed = gs.TimeBlockEditor(root, block=_blk(map="desert", enemy_rules=rules), others=[],
                                profiles=[{"alias": "默认", "dir": "d"}], on_save=lambda b: None)
        assert ed._rules == rules
        assert ed._rules_summary.cget("text") == "自定义 2 个物种, 1 项数值"
        assert ed._rules_btn.cget("state") == "normal"

        # 索敌开关关掉 -> 按钮置灰; 恢复 -> 可点
        ed._enemy.deselect()
        ed._sync_rules_button()
        assert ed._rules_btn.cget("state") == "disabled"
        ed._enemy.select()
        ed._sync_rules_button()
        assert ed._rules_btn.cget("state") == "normal"

        # _collect: 非空写进时块, 空则不写键
        assert ed._collect()["enemy_rules"] == rules
        ed._set_rules(gs.empty_rules())
        assert ed._rules_summary.cget("text") == "全部默认"
        assert "enemy_rules" not in ed._collect()
        ed._set_rules({"species": {"scorpion": "ignore"}, "knobs": {"chase_max_px": 400}})

        # 换图: 沙漠的蝎子不在花园, 丢掉; 旋钮保留; 汇总跟着变
        ed._map.set("garden")
        ed._on_map_change()
        assert ed._rules == {"species": {}, "knobs": {"chase_max_px": 400}}
        assert ed._rules_summary.cget("text") == "自定义 1 项数值"

        # 本图不支持索敌(合成空表) -> 按钮置灰
        monkeypatch.setitem(enemy_detect.MAP_SPECIES, "jungle", frozenset())
        ed._map.set("jungle")
        ed._on_map_change()
        assert ed._rules_btn.cget("state") == "disabled"

        # 子窗: 预填、确认回调、报错不关窗
        got = []
        dlg = gs.EnemyRulesDialog(
            root, map_name="ocean", on_ok=got.append,
            rules={"species": {"bubble": "ignore"}, "knobs": {"avoid_px": 250}})
        assert dlg._cell_get("bubble", "*") == "ignore"
        assert dlg._cell_get("shell", "*") == ""
        assert dlg._knob_entries["avoid_px"].get() == "250"
        dlg._set_cell("shell", "*", "avoid")
        dlg._ok()
        assert got == [{"species": {"bubble": "ignore", "shell": "avoid"},
                        "knobs": {"avoid_px": 250}}]

        dlg2 = gs.EnemyRulesDialog(root, map_name="ocean", rules=None, on_ok=got.append)
        dlg2._knob_entries["chase_max_px"].insert(0, "abc")
        dlg2._ok()
        assert "追击上限" in dlg2._err.cget("text") and len(got) == 1
        dlg2._reset()
        assert dlg2._err.cget("text") == "" and dlg2._knob_entries["chase_max_px"].get() == ""
        dlg2._ok()
        assert got[-1] == _NO_RULES
    finally:
        root.destroy()


def test_enemy_rules_dialog_lifecycle_inside_the_editor(monkeypatch):
    """上面那条直接调 _sync_rules_button / 单独造子窗, 测不出这几处布线: 索敌开关自己的 command、
    「索敌设置…」只开一个子窗、确认后规则回到编辑器并带进下次预填、换图时关掉按旧地图建的子窗。"""
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        ed = gs.TimeBlockEditor(root, block=_blk(map="desert"), others=[],
                                profiles=[{"alias": "默认", "dir": "d"}], on_save=lambda b: None)
        assert ed._rules == _NO_RULES and ed._rules_summary.cget("text") == "全部默认"

        # select()/deselect() 不触发 command, toggle()(= 用户点开关)才触发 —— 按钮要跟着开关走
        assert ed._rules_btn.cget("state") == "normal"
        ed._enemy.toggle()
        assert ed._rules_btn.cget("state") == "disabled"
        ed._enemy.toggle()
        assert ed._rules_btn.cget("state") == "normal"

        # 再点一次「索敌设置…」不会新开第二个子窗
        ed._open_rules()
        dlg = ed._rules_dlg
        assert dlg is not None and dlg.winfo_exists()
        ed._open_rules()
        assert ed._rules_dlg is dlg

        # 确认: 规则回到编辑器、汇总更新、_collect 带上
        dlg._set_cell("scorpion", "*", "ignore")
        dlg._swarm.set("关")
        dlg._ok()
        assert not dlg.winfo_exists()
        want = {"species": {"scorpion": "ignore"}, "knobs": {"swarm": False}}
        assert ed._rules == want
        assert ed._rules_summary.cget("text") == "自定义 1 个物种, 1 项数值"
        assert ed._collect()["enemy_rules"] == want

        # 子窗关掉后再点会重新开一个, 预填的是编辑器里现有的规则(含蚁群)
        ed._open_rules()
        dlg = ed._rules_dlg
        assert dlg.winfo_exists()
        assert dlg._cell_get("scorpion", "*") == "ignore" and dlg._swarm.get() == "关"

        # 换图时关掉按旧地图建的子窗
        ed._map.set("garden")
        ed._on_map_change()
        assert not dlg.winfo_exists() and ed._rules_dlg is None
        assert ed._rules == {"species": {}, "knobs": {"swarm": False}}
    finally:
        root.destroy()


def test_the_rules_dialog_closes_when_the_button_greys_out_so_it_cannot_stay_confirmable(monkeypatch):
    """按钮置灰(索敌关)时, 已经开着的子窗不能留着 —— 否则界面上按钮是灰的、子窗却还能点「确定」,
    把一份「不生效」的规则写回编辑器。换图那条路径早就会关(见上一条), 这里补另外两条。"""
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        ed = gs.TimeBlockEditor(root, block=_blk(map="ocean"), others=[],
                                profiles=[{"alias": "默认", "dir": "d"}], on_save=lambda b: None)

        # 1) 索敌开关关掉(toggle = 用户点它, 会触发 command)
        ed._open_rules()
        dlg = ed._rules_dlg
        assert dlg is not None and dlg.winfo_exists()
        ed._enemy.toggle()
        assert ed._rules_btn.cget("state") == "disabled"
        assert not dlg.winfo_exists() and ed._rules_dlg is None

        # 2) 索敌开着时又能开; 按钮本来就可点、子窗开着时再同步一次, 不能把子窗误关
        ed._enemy.toggle()
        assert ed._rules_btn.cget("state") == "normal"
        ed._open_rules()
        dlg = ed._rules_dlg
        assert dlg.winfo_exists()
        ed._sync_rules_button()
        assert ed._rules_btn.cget("state") == "normal"
        assert dlg.winfo_exists() and ed._rules_dlg is dlg
    finally:
        root.destroy()


def test_enemy_rules_dialog_shows_the_species_hint_with_the_risk_wording(monkeypatch):
    """test_main_worker 守的是 _HINT_SPECIES 这个常量的措辞; 这条守它真的被渲染在子窗物种卡片上
    (有物种的图和没物种表的图都是 —— 说明跟着卡片走, 不跟着行数走)。"""
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        for map_name in ("desert", "ocean"):
            dlg = gs.EnemyRulesDialog(root, map_name=map_name, rules=None, on_ok=lambda r: None)
            text = dlg._species_hint.cget("text")
            assert text == gs.EnemyRulesDialog._HINT_SPECIES
            assert "后果自负" in text and "一直追" not in text
    finally:
        root.destroy()


def test_enemy_rules_dialog_rarity_tier_menu_roundtrips_and_is_live_on_every_map(monkeypatch):
    """「躲避起点稀有度」下拉: 预填已存的值、确认交回、「全部恢复默认」清回默认; 沙漠(按稀有度追击)上也可用。"""
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        for map_name in ("desert", "ocean"):
            got = []
            dlg = gs.EnemyRulesDialog(root, map_name=map_name, rules=None, on_ok=got.append)
            assert dlg._tier.get() == gs._TIER_LABELS[""]
            assert dlg._tier.cget("state") == "normal"
            dlg._ok()
            assert got == [_NO_RULES]                                        # 默认不写键

            saved = {"species": {}, "knobs": {"avoid_min_rarity": "Legendary"}}
            dlg = gs.EnemyRulesDialog(root, map_name=map_name, rules=saved, on_ok=got.append)
            assert dlg._tier.get() == gs._TIER_LABELS["Legendary"]
            dlg._tier.set(gs._TIER_LABELS["never"])
            dlg._ok()
            assert got[-1] == {"species": {}, "knobs": {"avoid_min_rarity": "never"}}

            dlg = gs.EnemyRulesDialog(root, map_name=map_name, rules=saved, on_ok=got.append)
            dlg._reset()
            assert dlg._tier.get() == gs._TIER_LABELS[""]
            dlg._ok()
            assert got[-1] == _NO_RULES
    finally:
        root.destroy()


# ── 第③轮: 按装备推荐 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("style, petal", [(None, "Epic"), ("", "Epic"), ("tank", "Epic"),
                                          ("melee", None), ("melee", ""), ("melee", "Eternal")])
def test_recommendation_for_needs_both_a_known_style_and_a_known_petal_rarity(style, petal):
    values, err = gs.recommendation_for(style, petal, "ocean")
    assert values is None and "流派" in err and "花瓣" in err


def test_recommendation_for_only_fills_the_knobs_the_map_really_reads():
    # 沙漠按稀有度追击: 停步半径不生效, 往里填一个没用的数只会骗人; 躲避起点稀有度到处生效
    values, err = gs.recommendation_for("ranged", "Legendary", "desert")
    assert err is None and values == {"avoid_min_rarity": "Mythic"}
    values, err = gs.recommendation_for("ranged", "Legendary", "garden")
    assert err is None and set(values) == {"avoid_min_rarity", "hold_active_px", "hold_passive_px"}
    assert values["avoid_min_rarity"] == "Mythic" and values["hold_active_px"] > 120


def test_recommendation_for_melee_clears_the_stop_radii_back_to_default():
    # None = 清回默认: 先套远程、再套近战, 近战不该留着远程的停步半径
    values, _ = gs.recommendation_for("melee", "Epic", "garden")
    assert values == {"avoid_min_rarity": "Legendary", "hold_active_px": None, "hold_passive_px": None}


def test_recommendation_note_lists_what_was_filled_and_says_nothing_is_saved_yet():
    values, _ = gs.recommendation_for("ranged", "Legendary", "garden")
    note = gs.recommendation_note(values)
    assert "躲避起点稀有度 Mythic" in note
    assert f"主动怪停步 {values['hold_active_px']}" in note
    assert f"被动/不动怪停步 {values['hold_passive_px']}" in note
    assert "确定" in note and "才保存" in note
    cleared = gs.recommendation_note(gs.recommendation_for("melee", "Legendary", "garden")[0])
    assert "主动怪停步 默认" in cleared and "被动/不动怪停步 默认" in cleared


def test_recommendation_note_keeps_the_placeholder_caveat_only_when_stop_radii_were_filled():
    # 套用后这行提示会顶掉子窗里原来的静态说明, 占位数值的提醒不能跟着消失
    ranged = gs.recommendation_note(gs.recommendation_for("ranged", "Epic", "garden")[0])
    assert "占位" in ranged and "没有实机数据" in ranged
    assert "占位" not in gs.recommendation_note(gs.recommendation_for("melee", "Epic", "garden")[0])
    assert "占位" not in gs.recommendation_note(gs.recommendation_for("ranged", "Epic", "desert")[0])


def test_recommendation_note_says_when_the_map_ignores_the_stop_radii():
    values, _ = gs.recommendation_for("ranged", "Legendary", "desert")
    note = gs.recommendation_note(values)
    assert "躲避起点稀有度 Mythic" in note
    assert "本图" in note and "停步半径" in note and "没填" in note
    assert "主动怪停步" not in note                                # 没填的就不列


def test_recommendation_note_warns_when_the_tier_would_fight_ultra_as_ordinary_mobs():
    # 花瓣 Ultra -> 起点 Super: Ultra 当普通怪打, 连内置躲的 Ultra 蝎子 / 甲虫也一起放掉(README 写明的后果)
    hot = gs.recommendation_note(gs.recommendation_for("melee", "Ultra", "garden")[0])
    assert "⚠" in hot and "Ultra" in hot and "蝎子" in hot
    for petal in ("Rare", "Epic", "Legendary", "Mythic"):         # 起点 <= Ultra: 没有这条警告
        cool = gs.recommendation_note(gs.recommendation_for("melee", petal, "garden")[0])
        assert "⚠" not in cool, petal


def test_enemy_rules_dialog_recommendation_fills_the_controls_and_is_only_saved_by_ok(monkeypatch):
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        got = []
        saved = {"species": {"bubble": "ignore"}, "knobs": {"chase_max_px": 350}}
        dlg = gs.EnemyRulesDialog(root, map_name="ocean", rules=saved, on_ok=got.append)
        assert dlg._rec_style.get() == gs._REC_PICK and dlg._rec_petal.get() == gs._REC_PICK

        # 没选全就点套用: 红字、什么都不动
        dlg._apply_recommendation()
        assert "流派" in dlg._err.cget("text")
        assert dlg._tier.get() == gs._TIER_LABELS[""] and dlg._knob_entries["hold_active_px"].get() == ""
        dlg._rec_style.set(gs._STYLE_LABELS["ranged"])
        dlg._apply_recommendation()                                 # 还缺花瓣
        assert dlg._err.cget("text") and dlg._tier.get() == gs._TIER_LABELS[""]

        # 选全: 填进下面的控件, 提示行写明填了什么; 物种选择和别的数值不动; 红字清掉
        dlg._rec_petal.set("Legendary")
        dlg._apply_recommendation()
        assert dlg._err.cget("text") == ""
        assert dlg._tier.get() == "Mythic"
        want, _ = gs.recommendation_for("ranged", "Legendary", "ocean")
        assert dlg._knob_entries["hold_active_px"].get() == str(want["hold_active_px"])
        assert dlg._knob_entries["hold_passive_px"].get() == str(want["hold_passive_px"])
        assert dlg._knob_entries["chase_max_px"].get() == "350"
        assert dlg._cell_get("bubble", "*") == "ignore"
        assert "躲避起点稀有度 Mythic" in dlg._rec_hint.cget("text")
        assert got == []                                            # 套用不落盘
        dlg._ok()
        assert got == [{"species": {"bubble": "ignore"},
                        "knobs": {"chase_max_px": 350, "avoid_min_rarity": "Mythic", **{
                            k: want[k] for k in ("hold_active_px", "hold_passive_px")}}}]

        # 换成近战 + Epic: 停步半径清回默认, 起点跟着变
        dlg2 = gs.EnemyRulesDialog(
            root, map_name="ocean", on_ok=got.append,
            rules={"species": {}, "knobs": {"hold_active_px": 222, "hold_passive_px": 77}})
        dlg2._rec_style.set(gs._STYLE_LABELS["melee"])
        dlg2._rec_petal.set("Epic")
        dlg2._apply_recommendation()
        assert dlg2._tier.get() == "Legendary"
        assert dlg2._knob_entries["hold_active_px"].get() == ""
        assert dlg2._knob_entries["hold_passive_px"].get() == ""

        # 全部恢复默认: 推荐的两个下拉和提示行一并回到初始
        dlg2._reset()
        assert dlg2._rec_style.get() == gs._REC_PICK and dlg2._rec_petal.get() == gs._REC_PICK
        assert dlg2._rec_hint.cget("text") == gs.EnemyRulesDialog._HINT_RECOMMEND
    finally:
        root.destroy()


def test_enemy_rules_dialog_recommendation_on_desert_only_moves_the_tier_and_spares_greyed_values(monkeypatch):
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        got = []
        saved = {"species": {}, "knobs": {"hold_active_px": 222, "chase_max_px": 350}}
        dlg = gs.EnemyRulesDialog(root, map_name="desert", rules=saved, on_ok=got.append)
        dlg._rec_style.set(gs._STYLE_LABELS["ranged"])
        dlg._rec_petal.set("Mythic")
        dlg._apply_recommendation()
        assert dlg._tier.get() == "Ultra"
        assert dlg._knob_entries["hold_active_px"].get() == "222"        # 灰的框里已存的值没被推荐碰
        assert dlg._knob_entries["hold_active_px"].cget("state") == "disabled"
        assert "本图" in dlg._rec_hint.cget("text") and "没填" in dlg._rec_hint.cget("text")
        dlg._ok()
        assert got == [{"species": {}, "knobs": {"hold_active_px": 222, "chase_max_px": 350,
                                                 "avoid_min_rarity": "Ultra"}}]
    finally:
        root.destroy()


def test_enemy_rules_dialog_greys_out_the_knobs_the_map_ignores_but_keeps_their_values(monkeypatch):
    """沙漠(按稀有度追击)上只有躲避半径管用: 其余四个数值框 + 蚁群开关置灰, 子窗里有一句说明;
    但已存的值不能被「确定」吃掉。最近优先的图(海洋)上全部可用、没有说明。"""
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        saved = {"species": {"scorpion": "avoid"},
                 "knobs": {"chase_max_px": 400, "avoid_px": 250, "swarm": False}}
        got = []
        dlg = gs.EnemyRulesDialog(root, map_name="desert", rules=saved, on_ok=got.append)
        for key in ("hold_active_px", "hold_passive_px", "chase_max_px", "far_chase_px"):
            assert dlg._knob_entries[key].cget("state") == "disabled", key
        assert dlg._knob_entries["avoid_px"].cget("state") == "normal"
        assert dlg._knob_entries["cautious_px"].cget("state") == "normal"
        assert dlg._swarm.cget("state") == "disabled"
        assert dlg._knob_hint is not None
        assert "只有「躲避半径」「谨慎保持距离」和「躲避起点稀有度」管用" in dlg._knob_hint.cget("text")
        # 灰的框里原值还在, 确认后原样交回去(含灰掉的 chase_max_px / swarm)
        assert dlg._knob_entries["chase_max_px"].get() == "400"
        dlg._ok()
        assert got == [saved]
        # 「全部恢复默认」连灰的也清, 清完仍是灰的(确认会销毁子窗, 所以另开一个)
        dlg_r = gs.EnemyRulesDialog(root, map_name="desert", rules=saved, on_ok=got.append)
        dlg_r._reset()
        assert dlg_r._knob_entries["chase_max_px"].get() == "" and dlg_r._swarm.get() == "默认"
        assert dlg_r._knob_entries["chase_max_px"].cget("state") == "disabled"
        assert dlg_r._swarm.cget("state") == "disabled"
        dlg_r._ok()
        assert got[-1] == _NO_RULES

        dlg2 = gs.EnemyRulesDialog(root, map_name="ocean", rules=None, on_ok=got.append)
        for key in ("hold_active_px", "hold_passive_px", "chase_max_px", "far_chase_px", "avoid_px",
                    "cautious_px"):
            assert dlg2._knob_entries[key].cget("state") == "normal", key
        assert dlg2._swarm.cget("state") == "normal"
        assert dlg2._knob_hint is None
    finally:
        root.destroy()


# ── 逐物种 × 逐稀有度: 子窗里的矩阵 ─────────────────────────────────────────────

import app_config
import enemy_species


def test_the_cell_cycle_goes_default_fight_cautious_avoid_ignore_and_wraps_both_ways():
    assert gs.RULE_CYCLE == ("", "fight", "cautious", "avoid", "ignore")
    assert set(gs.RULE_CYCLE) - {""} == set(app_config._ENEMY_RULE_VALUES)         # 跟配置层认的打法一一对应
    seen, cur = [], ""
    for _ in range(6):
        cur = gs.next_rule(cur)
        seen.append(cur)
    assert seen == ["fight", "cautious", "avoid", "ignore", "", "fight"]
    back, cur = [], ""
    for _ in range(6):
        cur = gs.next_rule(cur, -1)
        back.append(cur)
    assert back == ["ignore", "avoid", "cautious", "fight", "", "ignore"]
    assert gs.next_rule("garbage") == "fight"                                  # 不认识的状态当默认


def test_the_matrix_columns_are_the_star_then_every_rarity_with_a_short_header_each():
    assert gs.MATRIX_COLS == ("*",) + tuple(enemy_species.RARITY_ORDER)
    assert set(gs._COL_HEADERS) == set(gs.MATRIX_COLS)
    assert len(set(gs._COL_HEADERS.values())) == len(gs.MATRIX_COLS)           # 表头不重名
    assert gs._COL_HEADERS["*"] == "全部"


@pytest.mark.parametrize("rule, cells", [
    (None, {}), ("", {}), ("ignore", {"*": "ignore"}),
    ({"Epic": "avoid", "*": "fight"}, {"Epic": "avoid", "*": "fight"}),
    ({"Epic": "", "*": "fight"}, {"*": "fight"}),                              # 字典里的空格子不算
    ({}, {}),
])
def test_species_cells_expands_a_rule_into_per_column_states(rule, cells):
    assert gs.species_cells(rule) == cells


@pytest.mark.parametrize("cells, rule", [
    ({}, None), ({"*": ""}, None), ({"Epic": "", "Ultra": ""}, None),
    ({"*": "ignore"}, "ignore"),                                              # 只有「全部」 = 老写法
    ({"Epic": "avoid"}, {"Epic": "avoid"}),
    ({"Ultra": "avoid", "*": "fight", "Common": "", "Epic": "cautious"},
     {"*": "fight", "Epic": "cautious", "Ultra": "avoid"}),                   # 空格子不写, 键按稀有度排
])
def test_species_rule_from_cells_collapses_back_to_the_smallest_form(cells, rule):
    got = gs.species_rule_from_cells(cells)
    assert got == rule
    if isinstance(rule, dict):
        assert list(got) == list(rule)


def test_cells_and_rules_round_trip():
    for rule in ("fight", "cautious", {"*": "avoid", "Common": "fight"}, {"Legendary": "ignore", "Unique": "avoid"}):
        assert gs.species_rule_from_cells(gs.species_cells(rule)) == rule


def test_rules_from_inputs_takes_cell_dicts_and_still_takes_plain_strings():
    rules, err = gs.rules_from_inputs(
        {"bee": {"*": "", "Epic": "avoid", "Ultra": "cautious"}, "rock": {"*": "ignore"},
         "ladybug": "fight", "wasp": "", "spider": {}, "ant_egg": {"Epic": ""}}, {}, "")
    assert err is None
    assert rules["species"] == {"bee": {"Epic": "avoid", "Ultra": "cautious"}, "rock": "ignore", "ladybug": "fight"}


@pytest.mark.parametrize("bad", [{"epic": "avoid"}, {"Epic": "dance"}, {"Epic": None}, {"Foo": "fight"}, "dance"])
def test_rules_from_inputs_refuses_what_the_config_layer_would_drop(bad):
    rules, err = gs.rules_from_inputs({"bee": bad}, {}, "")
    assert rules is None and "bee" in err


def test_the_gui_produces_only_rules_the_config_layer_keeps_unchanged():
    rules, _ = gs.rules_from_inputs(
        {"bee": {"*": "fight", "Common": "cautious", "Mythic": "avoid", "Unique": "ignore"}, "rock": {"*": "avoid"}},
        {}, "")
    assert app_config._coerce_enemy_rules(rules, "garden") == rules
    assert app_config._validate_enemy_rules(rules, "garden", "x") == []


def test_rules_for_map_keeps_per_rarity_rules_for_species_on_the_map_and_copies_them():
    rules = {"species": {"scorpion": {"Epic": "avoid"}, "beetle": "avoid", "rock": {"Ultra": "fight"}}, "knobs": {}}
    got = gs.rules_for_map(rules, "desert")
    assert got["species"] == {"scorpion": {"Epic": "avoid"}, "beetle": "avoid"}
    got["species"]["scorpion"]["Epic"] = "fight"
    assert rules["species"]["scorpion"] == {"Epic": "avoid"}                  # 不是同一个字典
    assert "rock" in gs.rules_for_map(rules, "garden")["species"] and "scorpion" not in gs.rules_for_map(rules, "garden")["species"]


def test_rules_summary_counts_a_per_rarity_species_once():
    assert gs.rules_summary({"species": {"a": {"Epic": "avoid", "Ultra": "fight"}, "b": "ignore"}}) == "自定义 2 个物种"


def test_the_species_hint_explains_the_matrix_and_keeps_its_risk_wording():
    hint = gs.EnemyRulesDialog._HINT_SPECIES
    assert "全部" in hint and "优先" in hint and "谨慎" in hint and "右键" in hint
    assert "一直追" not in hint
    assert "沙漠" in hint and "不会让低稀有度怪被专门追" in hint
    assert "Ultra" in hint and "后果自负" in hint and "忽略" in hint


def _matrix_dialog(root, map_name="garden", rules=None, on_ok=None):
    return gs.EnemyRulesDialog(root, map_name=map_name, rules=rules, on_ok=on_ok or (lambda r: None))


def test_the_dialog_has_one_cell_per_species_and_column_and_starts_from_the_saved_rules(monkeypatch):
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        saved = {"species": {"bee": {"*": "fight", "Epic": "avoid", "Ultra": "cautious"}, "rock": "ignore"}, "knobs": {}}
        dlg = _matrix_dialog(root, rules=saved)
        slugs = [r[0] for r in gs.species_rows("garden")]
        assert set(dlg._cell_widgets) == set(slugs)
        for slug in slugs:
            assert list(dlg._cell_widgets[slug]) == list(gs.MATRIX_COLS)
        assert dlg._cell_get("bee", "*") == "fight" and dlg._cell_get("bee", "Epic") == "avoid"
        assert dlg._cell_get("bee", "Ultra") == "cautious" and dlg._cell_get("bee", "Common") == ""
        assert dlg._cell_get("rock", "*") == "ignore" and dlg._cell_get("rock", "Epic") == ""
        assert dlg._cell_get("ladybug", "*") == ""
        got = []
        dlg._on_ok = got.append
        dlg._ok()
        assert got == [saved]                                                  # 不动就原样交回去(含键顺序)
    finally:
        root.destroy()


def test_clicking_a_cell_cycles_it_and_right_click_goes_back_and_each_cell_is_independent(monkeypatch):
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        got = []
        dlg = _matrix_dialog(root, on_ok=got.append)
        w = dlg._cell_widgets["bee"]["Epic"]
        assert w.cget("text") == gs._CELL_TEXT[""]
        w.event_generate("<Button-1>")
        root.update()
        assert dlg._cell_get("bee", "Epic") == "fight" and w.cget("text") == gs._CELL_TEXT["fight"]
        w.event_generate("<Button-1>")
        root.update()
        assert dlg._cell_get("bee", "Epic") == "cautious"
        w.event_generate("<Button-3>")
        root.update()
        assert dlg._cell_get("bee", "Epic") == "fight"                         # 右键: 反向
        assert dlg._cell_get("bee", "Common") == "" and dlg._cell_get("rock", "Epic") == ""   # 别的格子不动
        dlg._cycle_cell("bee", "Ultra", -1)                                    # 从默认往回: 忽略
        assert dlg._cell_get("bee", "Ultra") == "ignore"
        dlg._ok()
        assert got == [{"species": {"bee": {"Epic": "fight", "Ultra": "ignore"}}, "knobs": {}}]
    finally:
        root.destroy()


def test_every_state_has_its_own_look_so_the_matrix_reads_at_a_glance(monkeypatch):
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        dlg = _matrix_dialog(root)
        texts, backgrounds = [], []
        for state in gs.RULE_CYCLE:
            dlg._set_cell("bee", "Epic", state)
            w = dlg._cell_widgets["bee"]["Epic"]
            texts.append(w.cget("text"))
            backgrounds.append(w.cget("bg"))
        assert len(set(texts)) == len(gs.RULE_CYCLE)                         # 色盲 / 打印出来也分得清
        assert len(set(backgrounds)) == len(gs.RULE_CYCLE)                   # 扫一眼颜色也分得清
    finally:
        root.destroy()


def test_reset_clears_every_cell_not_only_the_star_column(monkeypatch):
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        got = []
        dlg = _matrix_dialog(root, rules={"species": {"bee": {"*": "fight", "Epic": "avoid"}, "rock": "ignore"},
                                          "knobs": {"chase_max_px": 400}}, on_ok=got.append)
        dlg._reset()
        for slug, cols in dlg._cell_widgets.items():
            assert all(dlg._cell_get(slug, c) == "" for c in cols)
            assert all(w.cget("text") == gs._CELL_TEXT[""] for w in cols.values())
        dlg._ok()
        assert got == [_NO_RULES]
    finally:
        root.destroy()


def test_per_rarity_rules_of_species_not_on_the_chosen_map_are_dropped_on_open(monkeypatch):
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        got = []
        dlg = _matrix_dialog(root, map_name="ocean", on_ok=got.append,
                             rules={"species": {"scorpion": {"Epic": "avoid"}, "bubble": {"Epic": "avoid"}}, "knobs": {}})
        dlg._ok()
        assert got == [{"species": {"bubble": {"Epic": "avoid"}}, "knobs": {}}]
    finally:
        root.destroy()


def test_a_map_without_a_species_table_shows_no_cells(monkeypatch):
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    monkeypatch.setitem(enemy_detect.MAP_SPECIES, "jungle", frozenset())
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        dlg = _matrix_dialog(root, map_name="jungle")
        assert dlg._cell_widgets == {}
    finally:
        root.destroy()


def test_column_headers_and_cells_share_the_same_grid_columns_so_they_cannot_drift(monkeypatch):
    # 以前表头和格子分开 pack, 字号不同换算成像素的宽度就不同, 表头越往右越歪; 现在放进同一个 grid
    monkeypatch.setattr(gui_map_picker, "_MAP_DIR",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
    root = gs.ctk.CTk()
    root.withdraw()
    try:
        dlg = _matrix_dialog(root)
        root.update()
        assert list(dlg._col_headers) == list(gs.MATRIX_COLS)
        for col, header in dlg._col_headers.items():
            head_col = int(header.grid_info()["column"])
            for slug, cols in dlg._cell_widgets.items():
                assert int(cols[col].grid_info()["column"]) == head_col, (col, slug)
                assert cols[col].master is header.master                       # 同一个 grid 容器
        # 列号随稀有度递增, 没有两列挤在一起
        assert [int(dlg._col_headers[c].grid_info()["column"]) for c in gs.MATRIX_COLS] == list(range(2, 2 + len(gs.MATRIX_COLS)))
        # 像素位置也对得上(同一列的表头和格子, 中心线差不多)
        slug = next(iter(dlg._cell_widgets))
        for col in gs.MATRIX_COLS:
            h, c = dlg._col_headers[col], dlg._cell_widgets[slug][col]
            assert abs((h.winfo_x() + h.winfo_width() / 2) - (c.winfo_x() + c.winfo_width() / 2)) <= 2, col
    finally:
        root.destroy()
