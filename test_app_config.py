import copy
import json

import pytest

import app_config


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(app_config, "CONFIG_PATH", str(p))
    return p


def test_gui_enabled_maps_has_desert_and_anthell_but_valid_maps_untouched():
    # 蚁穴解锁(进场路线见 map_routes.py); ocean 仍然只在 coerce 层认, GUI 不给选.
    assert app_config._GUI_ENABLED_MAPS == ("desert", "anthell")
    assert app_config._VALID_MAPS == ("desert", "ocean", "anthell")
    assert set(app_config._GUI_ENABLED_MAPS).issubset(app_config._VALID_MAPS)


def _v2_block(**kw):
    base = dict(id="blk-1", enabled=True, days=[0, 1, 2, 3, 4, 5, 6],
                start="00:00", end="00:00", profile="默认", map="desert",
                location=[22, 32], farming_area=[[9, 8], [51, 56]],
                farming_duration=300, consecutive_short_round_limit=2,
                enemy_ai_enabled=True, auto_switch_server=True)
    base.update(kw)
    return base


def _v2_cfg(**kw):
    c = copy.deepcopy(app_config.DEFAULTS_V2)
    c["schedule"] = [_v2_block()]
    c["profiles"] = [{"alias": "默认", "dir": "chrome-profiles/默认"}]
    c.update(kw)
    return c


class TestCoerceV2:
    def test_missing_file_returns_defaults_v2(self, cfg_path):
        assert app_config.load_config() == app_config.DEFAULTS_V2

    def test_v2_roundtrips(self, cfg_path):
        cfg = _v2_cfg()
        app_config.save_config(cfg)
        assert app_config.load_config() == app_config.load_config()  # 稳定
        got = app_config.load_config()
        assert got["schedule"][0]["id"] == "blk-1"
        assert got["profiles"] == [{"alias": "默认", "dir": "chrome-profiles/默认"}]

    def test_top_level_not_dict_falls_back(self, cfg_path):
        cfg_path.write_text("[1,2,3]", encoding="utf-8")
        assert app_config.load_config() == app_config.DEFAULTS_V2

    @pytest.mark.parametrize("bad_block", [
        {"days": []},
        {"days": [7]},
        {"start": "9am"},                     # normalize_time 解析不了 -> 丢块
        {"start": "10:00", "end": "10:00"},   # start==end 非全天
        {"map": "nonsense"},
        {"location": "12,32"},
        {"farming_area": [[1, 2], [3]]},
        {"farming_duration": 0},
        {"enemy_ai_enabled": "yes"},
    ])
    def test_bad_block_is_dropped_whole(self, cfg_path, bad_block):
        good = _v2_block(id="good")
        bad = _v2_block(id="bad", **bad_block)
        cfg_path.write_text(json.dumps(_v2_cfg(schedule=[bad, good])), encoding="utf-8")
        got = app_config.load_config()
        assert [b["id"] for b in got["schedule"]] == ["good"]

    def test_dangling_profile_disables_block_not_drops(self, cfg_path):
        blk = _v2_block(id="x", profile="不存在")
        cfg_path.write_text(json.dumps(_v2_cfg(schedule=[blk])), encoding="utf-8")
        got = app_config.load_config()
        assert [b["id"] for b in got["schedule"]] == ["x"]
        assert got["schedule"][0]["enabled"] is False

    def test_empty_profiles_gets_default(self, cfg_path):
        cfg_path.write_text(json.dumps(_v2_cfg(profiles=[])), encoding="utf-8")
        assert app_config.load_config()["profiles"] == [
            {"alias": "默认", "dir": "chrome-profiles/默认"}]

    def test_duplicate_alias_deduped(self, cfg_path):
        cfg_path.write_text(json.dumps(_v2_cfg(profiles=[
            {"alias": "a", "dir": "chrome-profiles/a"},
            {"alias": "a", "dir": "chrome-profiles/a2"},
        ], schedule=[])), encoding="utf-8")
        got = app_config.load_config()
        assert [p["alias"] for p in got["profiles"]] == ["a"]

    def test_days_sorted_deduped(self, cfg_path):
        blk = _v2_block(days=[4, 0, 0, 2])
        cfg_path.write_text(json.dumps(_v2_cfg(schedule=[blk])), encoding="utf-8")
        assert app_config.load_config()["schedule"][0]["days"] == [0, 2, 4]

    def test_active_defaults_from_first_block_when_absent(self, cfg_path):
        c = _v2_cfg(schedule=[_v2_block(map="ocean", location=[5, 5])])
        del c["active"]
        cfg_path.write_text(json.dumps(c), encoding="utf-8")
        got = app_config.load_config()
        assert got["active"]["map"] == "ocean"
        assert got["active"]["location"] == [5, 5]

    def test_coerce_block_normalizes_loose_time(self, cfg_path):
        blk = _v2_block(id="loose", start="9:00", end="1230")
        cfg_path.write_text(json.dumps(_v2_cfg(schedule=[blk])), encoding="utf-8")
        got = app_config.load_config()["schedule"]
        assert [b["id"] for b in got] == ["loose"]
        assert got[0]["start"] == "09:00" and got[0]["end"] == "12:30"

    def test_coerce_block_drops_unparseable_time(self, cfg_path):
        good = _v2_block(id="good")
        bad = _v2_block(id="bad", start="9am")
        cfg_path.write_text(json.dumps(_v2_cfg(schedule=[bad, good])), encoding="utf-8")
        assert [b["id"] for b in app_config.load_config()["schedule"]] == ["good"]


class TestMigrationV1:
    def _v1(self, **kw):
        base = dict(map="ocean", location=[7, 8], farming_area=[[1, 1], [5, 5]],
                    farming_duration=222, consecutive_short_round_limit=3,
                    enemy_ai_enabled=False, auto_switch_server=False, afk_enabled=True)
        base.update(kw)
        return base

    def test_v1_flat_migrates_to_single_all_week_block(self, cfg_path):
        cfg_path.write_text(json.dumps(self._v1()), encoding="utf-8")
        got = app_config.load_config()
        assert got["version"] == 2
        assert got["afk_enabled"] is True
        assert got["profiles"] == [{"alias": "默认", "dir": "chrome-profiles/默认"}]
        assert len(got["schedule"]) == 1
        blk = got["schedule"][0]
        assert blk["days"] == [0, 1, 2, 3, 4, 5, 6]
        assert blk["start"] == "00:00" and blk["end"] == "00:00"
        assert blk["profile"] == "默认"
        assert blk["map"] == "ocean" and blk["location"] == [7, 8]
        assert blk["farming_duration"] == 222
        assert got["active"]["map"] == "ocean"
        assert got["active"]["consecutive_short_round_limit"] == 3

    def test_migration_is_written_back_as_v2(self, cfg_path):
        cfg_path.write_text(json.dumps(self._v1()), encoding="utf-8")
        app_config.load_config()
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["version"] == 2
        assert "schedule" in on_disk

    def test_migration_tolerates_bad_v1_values(self, cfg_path):
        cfg_path.write_text(json.dumps(self._v1(map="bogus", farming_duration=-1)),
                            encoding="utf-8")
        blk = app_config.load_config()["schedule"][0]
        assert blk["map"] == app_config.DEFAULTS["map"]
        assert blk["farming_duration"] == app_config.DEFAULTS["farming_duration"]

    def test_rename_legacy_dir_best_effort(self, tmp_path, monkeypatch):
        root = tmp_path
        monkeypatch.setattr(app_config.sys, "argv", [str(root / "app.exe")])
        (root / "chrome-profile").mkdir()
        app_config._rename_legacy_profile_dir()
        assert (root / "chrome-profiles" / "默认").is_dir()
        assert not (root / "chrome-profile").exists()

    def test_rename_legacy_dir_noop_when_target_exists(self, tmp_path, monkeypatch):
        root = tmp_path
        monkeypatch.setattr(app_config.sys, "argv", [str(root / "app.exe")])
        (root / "chrome-profile").mkdir()
        (root / "chrome-profiles" / "默认").mkdir(parents=True)
        app_config._rename_legacy_profile_dir()   # 不该抛
        assert (root / "chrome-profile").exists()  # 保留原样


class TestScheduleMath:
    def _blk(self, **kw):
        base = dict(id="b", enabled=True, days=[0], start="09:00", end="12:00",
                    profile="默认", map="desert", location=[1, 2],
                    farming_area=[[0, 0], [9, 9]], farming_duration=300,
                    consecutive_short_round_limit=2, enemy_ai_enabled=True,
                    auto_switch_server=True)
        base.update(kw)
        return base

    def test_hhmm_to_min(self):
        assert app_config._hhmm_to_min("00:00") == 0
        assert app_config._hhmm_to_min("09:30") == 570
        assert app_config._hhmm_to_min("23:59") == 1439

    @pytest.mark.parametrize("s, ok", [
        ("09:00", True), ("00:00", True), ("23:59", True),
        ("9:00", False), ("24:00", False), ("12:60", False), ("", False), (None, False),
    ])
    def test_valid_time(self, s, ok):
        assert app_config._valid_time(s) is ok

    def test_expand_plain(self):
        assert app_config.expand_block_days(self._blk(days=[0, 3])) == [
            (0, 540, 720), (3, 540, 720)]

    def test_expand_all_day(self):
        assert app_config.expand_block_days(
            self._blk(days=[2], start="00:00", end="00:00")) == [(2, 0, 1440)]

    def test_expand_cross_midnight(self):
        assert app_config.expand_block_days(
            self._blk(days=[0], start="22:00", end="02:00")) == [
            (0, 1320, 1440), (1, 0, 120)]

    def test_overlap_same_day_intersect(self):
        assert app_config.blocks_overlap(
            self._blk(start="09:00", end="12:00"),
            self._blk(start="11:00", end="13:00")) is True

    def test_overlap_different_day(self):
        assert app_config.blocks_overlap(
            self._blk(days=[0]), self._blk(days=[1])) is False

    def test_overlap_touching_edges_not_overlap(self):
        assert app_config.blocks_overlap(
            self._blk(start="09:00", end="12:00"),
            self._blk(start="12:00", end="15:00")) is False

    def test_overlap_cross_midnight_spills_into_next_day(self):
        assert app_config.blocks_overlap(
            self._blk(days=[0], start="22:00", end="02:00"),
            self._blk(days=[1], start="01:00", end="03:00")) is True

    def test_active_block_hit_and_miss(self):
        sched = [self._blk(id="x", days=[0], start="09:00", end="12:00")]
        assert app_config.active_block(sched, 0, "10:00")["id"] == "x"
        assert app_config.active_block(sched, 0, "12:00") is None   # 半开区间
        assert app_config.active_block(sched, 1, "10:00") is None

    def test_active_block_skips_disabled(self):
        sched = [self._blk(id="x", enabled=False, days=[0], start="09:00", end="12:00")]
        assert app_config.active_block(sched, 0, "10:00") is None

    def test_active_block_cross_midnight_belongs_to_next_day(self):
        sched = [self._blk(id="x", days=[0], start="22:00", end="02:00")]
        assert app_config.active_block(sched, 1, "01:00")["id"] == "x"

    def test_next_start_same_day(self):
        sched = [self._blk(days=[0], start="18:00", end="20:00")]
        assert app_config.next_start(sched, 0, "09:00") == (0, "18:00")

    def test_next_start_wraps_week(self):
        sched = [self._blk(days=[2], start="09:00", end="12:00")]
        assert app_config.next_start(sched, 3, "10:00") == (2, "09:00")

    def test_next_start_none_when_empty(self):
        assert app_config.next_start([], 0, "09:00") is None


class TestNormalizeTime:
    _OK = [
        ("09:00", "09:00"),
        (" 9:00 ", "09:00"),
        ("9:5", "09:05"),
        ("09：00", "09:00"),          # 全角冒号
        ("０９:００", "09:00"),        # 全角数字
        ("　9:00　", "09:00"),        # 全角空格
        ("9.00", "09:00"),
        ("9-30", "09:30"),
        ("9", "09:00"),
        ("18", "18:00"),
        ("930", "09:30"),
        ("0930", "09:30"),
        ("1830", "18:30"),
        ("0", "00:00"),
        ("23:59", "23:59"),
    ]
    _BAD = ["2400", "25:00", "9:70", "12:00:00", "9am", "", "   ", "abc",
            "99999", ":30", "9:", "1:2:3"]

    @pytest.mark.parametrize("raw, out", _OK)
    def test_normalizes(self, raw, out):
        assert app_config.normalize_time(raw) == out

    @pytest.mark.parametrize("raw", _BAD)
    def test_rejects(self, raw):
        assert app_config.normalize_time(raw) is None

    @pytest.mark.parametrize("raw", [None, 123, 9.0, ["09:00"]])
    def test_non_str_is_none(self, raw):
        assert app_config.normalize_time(raw) is None

    @pytest.mark.parametrize("_raw, out", _OK)
    def test_idempotent(self, _raw, out):
        assert app_config.normalize_time(out) == out


_SWAP_OFF = {"enabled": False, "mod": "none", "digit": "1"}


class TestLoadoutSwapKeys:
    def test_defaults_are_disabled_chord_objects(self):
        assert app_config.DEFAULTS["enter_game_swap"] == _SWAP_OFF
        assert app_config.DEFAULTS["reach_area_swap"] == _SWAP_OFF

    def test_active_keys_include_swaps(self):
        assert "enter_game_swap" in app_config._ACTIVE_KEYS
        assert "reach_area_swap" in app_config._ACTIVE_KEYS

    def test_defaults_v2_active_slice_has_swaps(self):
        assert app_config.DEFAULTS_V2["active"]["enter_game_swap"] == _SWAP_OFF
        assert app_config.DEFAULTS_V2["active"]["reach_area_swap"] == _SWAP_OFF

    def test_valid_chord_roundtrip(self, cfg_path):
        cfg = _v2_cfg()
        cfg["schedule"] = [_v2_block(
            enter_game_swap={"enabled": True, "mod": "k", "digit": "3"},
            reach_area_swap={"enabled": True, "mod": "none", "digit": "0"})]
        app_config.save_config(cfg)
        blk = app_config.load_config()["schedule"][0]
        assert blk["enter_game_swap"] == {"enabled": True, "mod": "k", "digit": "3"}
        assert blk["reach_area_swap"] == {"enabled": True, "mod": "none", "digit": "0"}

    def test_bad_fields_normalized(self, cfg_path):
        cfg = _v2_cfg()
        cfg["schedule"] = [_v2_block(
            enter_game_swap={"enabled": 1, "mod": "ctrl", "digit": "99"},
            reach_area_swap="k")]   # 旧字符串形式 → 默认禁用对象
        app_config.save_config(cfg)
        blk = app_config.load_config()["schedule"][0]
        # enabled 非严格 True → False; mod 非法 → none; digit 非法 → "1"
        assert blk["enter_game_swap"] == _SWAP_OFF
        assert blk["reach_area_swap"] == _SWAP_OFF

    def test_partial_chord_fills_missing(self, cfg_path):
        cfg = _v2_cfg()
        cfg["schedule"] = [_v2_block(enter_game_swap={"enabled": True, "mod": "l"})]
        app_config.save_config(cfg)
        blk = app_config.load_config()["schedule"][0]
        assert blk["enter_game_swap"] == {"enabled": True, "mod": "l", "digit": "1"}

    def test_old_block_without_keys_still_valid(self, cfg_path):
        # 旧 config.json: 时块 dict 里根本没有这两个键 —— 不能被整块丢
        cfg = _v2_cfg()
        blk = _v2_block()
        blk.pop("enter_game_swap", None)
        blk.pop("reach_area_swap", None)
        cfg["schedule"] = [blk]
        app_config.save_config(cfg)
        got = app_config.load_config()
        assert len(got["schedule"]) == 1
        assert got["schedule"][0]["enter_game_swap"] == _SWAP_OFF
        assert got["schedule"][0]["reach_area_swap"] == _SWAP_OFF

    def test_v1_migration_adds_defaults(self, cfg_path):
        cfg_path.write_text(json.dumps({
            "map": "desert", "location": [1, 2], "farming_area": [[0, 0], [3, 3]],
            "farming_duration": 100, "consecutive_short_round_limit": 1,
            "enemy_ai_enabled": False, "auto_switch_server": True,
        }), encoding="utf-8")
        got = app_config.load_config()
        assert got["schedule"][0]["enter_game_swap"] == _SWAP_OFF
        assert got["schedule"][0]["reach_area_swap"] == _SWAP_OFF
        assert got["active"]["enter_game_swap"] == _SWAP_OFF

    def test_coerce_swap_obj_unit(self):
        f = app_config._coerce_swap_obj
        assert f(None) == _SWAP_OFF
        assert f("digits") == _SWAP_OFF          # 旧字符串形式
        assert f({"enabled": True, "mod": "k", "digit": "5"}) == {
            "enabled": True, "mod": "k", "digit": "5"}
        assert f({"enabled": True, "mod": "l", "digit": 5})["digit"] == "1"  # int 非法
        assert f({"enabled": "yes"})["enabled"] is False   # 非严格 True


class TestInvertToggles:
    def test_flat_defaults_unchanged(self):
        assert app_config.DEFAULTS["invert_attack"] is True
        assert app_config.DEFAULTS["invert_defense"] is False

    def test_now_in_active_keys(self):
        assert "invert_attack" in app_config._ACTIVE_KEYS
        assert "invert_defense" in app_config._ACTIVE_KEYS

    def test_not_top_level_in_defaults_v2(self):
        assert "invert_attack" not in app_config.DEFAULTS_V2
        assert "invert_defense" not in app_config.DEFAULTS_V2
        assert app_config.DEFAULTS_V2["active"]["invert_attack"] is True
        assert app_config.DEFAULTS_V2["active"]["invert_defense"] is False

    def test_load_has_them_per_block_not_top_level(self, cfg_path):
        cfg = _v2_cfg()
        cfg["schedule"] = [_v2_block()]
        app_config.save_config(cfg)
        got = app_config.load_config()
        assert "invert_attack" not in got            # gone from top level
        blk = got["schedule"][0]
        assert blk["invert_attack"] is True
        assert blk["invert_defense"] is False
        assert got["active"]["invert_attack"] is True

    def test_block_explicit_values_roundtrip(self, cfg_path):
        cfg = _v2_cfg()
        cfg["schedule"] = [_v2_block(invert_attack=False, invert_defense=True)]
        app_config.save_config(cfg)
        blk = app_config.load_config()["schedule"][0]
        assert blk["invert_attack"] is False
        assert blk["invert_defense"] is True

    def test_block_non_bool_falls_back_to_default(self, cfg_path):
        cfg = _v2_cfg()
        cfg["schedule"] = [_v2_block(invert_attack="yes", invert_defense=1)]
        app_config.save_config(cfg)
        blk = app_config.load_config()["schedule"][0]
        assert blk["invert_attack"] is True
        assert blk["invert_defense"] is False

    def test_old_block_without_keys_kept_with_defaults(self, cfg_path):
        cfg = _v2_cfg()
        b = _v2_block()
        b.pop("invert_attack", None)
        b.pop("invert_defense", None)
        cfg["schedule"] = [b]
        app_config.save_config(cfg)
        got = app_config.load_config()
        assert len(got["schedule"]) == 1            # not dropped
        assert got["schedule"][0]["invert_attack"] is True
        assert got["schedule"][0]["invert_defense"] is False

    def test_v1_migration_per_block_and_active_not_top_level(self, cfg_path):
        cfg_path.write_text(json.dumps({
            "map": "desert", "location": [1, 2], "farming_area": [[0, 0], [3, 3]],
            "farming_duration": 100, "consecutive_short_round_limit": 1,
            "enemy_ai_enabled": False, "auto_switch_server": True,
        }), encoding="utf-8")
        got = app_config.load_config()
        assert "invert_attack" not in got
        assert got["schedule"][0]["invert_attack"] is True
        assert got["schedule"][0]["invert_defense"] is False
        assert got["active"]["invert_attack"] is True
        assert got["active"]["invert_defense"] is False


class TestProfilePaths:
    """profile 目录解析. 原先住在 gui_accounts.py, 但那个模块 import customtkinter
    —— 无头服务器上没有 X, import 就炸, 而 `main.py --launch-chrome` 恰恰需要这
    两个函数. 纯数据逻辑, 搬到这里."""

    def test_profile_dir_returns_configured_dir_for_alias(self):
        cfg = {"profiles": [{"alias": "默认", "dir": "chrome-profiles/默认"},
                            {"alias": "小号2", "dir": "chrome-profiles/小号2"}]}
        assert app_config.profile_dir(cfg, "小号2") == "chrome-profiles/小号2"

    def test_profile_dir_returns_none_for_unknown_alias(self):
        cfg = {"profiles": [{"alias": "默认", "dir": "chrome-profiles/默认"}]}
        assert app_config.profile_dir(cfg, "不存在") is None

    def test_profile_dir_returns_none_when_no_profiles_key(self):
        assert app_config.profile_dir({}, "默认") is None

    def test_abs_profile_path_joins_relative_dir_to_exe_root(self, monkeypatch):
        monkeypatch.setattr(app_config.sys, "argv", ["/srv/florr/main.py"])
        assert app_config.abs_profile_path("chrome-profiles/默认") == (
            "/srv/florr/chrome-profiles/默认")

    def test_abs_profile_path_passes_absolute_through_unchanged(self):
        assert app_config.abs_profile_path("/var/lib/florr/p") == "/var/lib/florr/p"


def test_app_config_imports_without_any_gui_toolkit():
    """无头服务器上的硬约束: worker / --launch-chrome 这条路上任何模块都不许把
    customtkinter 或 tkinter 拖进来 —— 没有 X 时它们 import 就抛 TclError.
    起独立解释器查, 因为本进程里别的测试早就把这些模块 import 进 sys.modules 了."""
    import subprocess
    import sys as _sys
    r = subprocess.run(
        [_sys.executable, "-c",
         "import app_config, sys; "
         "bad = [m for m in ('customtkinter', 'tkinter') if m in sys.modules]; "
         "print(','.join(bad))"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", f"app_config 拖进了 GUI 依赖: {r.stdout.strip()}"


class TestValidateConfig:
    def _valid(self):
        return {
            "version": 2,
            "afk_enabled": False,
            "profiles": [{"alias": "默认", "dir": "chrome-profiles/默认"}],
            "schedule": [{
                "id": "blk-1", "enabled": True, "days": [0, 1, 2, 3, 4, 5, 6],
                "start": "00:00", "end": "00:00", "profile": "默认", "map": "desert",
                "location": [22, 32], "farming_area": [[9, 8], [51, 56]],
                "farming_duration": 300, "consecutive_short_round_limit": 2,
                "enemy_ai_enabled": True, "auto_switch_server": True,
                "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
                "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
                "invert_attack": True, "invert_defense": False,
            }],
            "active": {
                "map": "desert", "location": [22, 32], "farming_area": [[9, 8], [51, 56]],
                "farming_duration": 300, "consecutive_short_round_limit": 2,
                "enemy_ai_enabled": True, "auto_switch_server": True,
                "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
                "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
                "invert_attack": True, "invert_defense": False,
            },
        }

    def test_valid_config_has_no_errors(self):
        assert app_config.validate_config(self._valid()) == []

    def test_non_dict_top_level_rejected(self):
        assert app_config.validate_config([1, 2, 3]) == [
            "顶层必须是 JSON 对象, 实际是 list"]

    def test_wrong_version_rejected(self):
        cfg = self._valid()
        cfg["version"] = 1
        assert any("version" in e for e in app_config.validate_config(cfg))

    def test_empty_profiles_rejected(self):
        cfg = self._valid()
        cfg["profiles"] = []
        assert any("profiles" in e for e in app_config.validate_config(cfg))

    def test_duplicate_profile_alias_rejected(self):
        cfg = self._valid()
        cfg["profiles"] = [
            {"alias": "a", "dir": "chrome-profiles/a"},
            {"alias": "a", "dir": "chrome-profiles/a2"},
        ]
        assert any("重复的账号别名" in e for e in app_config.validate_config(cfg))

    def test_block_referencing_unknown_profile_rejected(self):
        cfg = self._valid()
        cfg["schedule"][0]["profile"] = "不存在"
        assert any("引用了不存在的账号" in e for e in app_config.validate_config(cfg))

    def test_block_bad_map_rejected(self):
        cfg = self._valid()
        cfg["schedule"][0]["map"] = "nonsense"
        assert any("schedule[0].map" in e for e in app_config.validate_config(cfg))

    def test_block_bad_time_rejected(self):
        cfg = self._valid()
        cfg["schedule"][0]["start"] = "9am"
        assert any("schedule[0].start" in e for e in app_config.validate_config(cfg))

    def test_block_start_equals_end_not_midnight_rejected(self):
        cfg = self._valid()
        cfg["schedule"][0]["start"] = "10:00"
        cfg["schedule"][0]["end"] = "10:00"
        errs = app_config.validate_config(cfg)
        assert any("schedule[0]" in e and "空的" in e for e in errs)

    def test_block_bad_farming_duration_rejected(self):
        cfg = self._valid()
        cfg["schedule"][0]["farming_duration"] = 0
        assert any("farming_duration" in e for e in app_config.validate_config(cfg))

    def test_block_bad_swap_object_rejected(self):
        cfg = self._valid()
        cfg["schedule"][0]["enter_game_swap"] = "not-an-object"
        assert any("enter_game_swap" in e for e in app_config.validate_config(cfg))

    def test_block_non_bool_enabled_rejected(self):
        cfg = self._valid()
        cfg["schedule"][0]["enabled"] = 0
        assert any("schedule[0].enabled" in e for e in app_config.validate_config(cfg))

    def test_active_bad_map_rejected(self):
        cfg = self._valid()
        cfg["active"]["map"] = "nonsense"
        assert any("active.map" in e for e in app_config.validate_config(cfg))

    def test_multiple_errors_all_reported_not_just_first(self):
        cfg = self._valid()
        cfg["profiles"] = []
        cfg["schedule"][0]["map"] = "nonsense"
        assert len(app_config.validate_config(cfg)) >= 2
