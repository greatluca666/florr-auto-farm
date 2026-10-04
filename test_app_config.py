import copy
import json

import pytest

import app_config


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(app_config, "CONFIG_PATH", str(p))
    return p


_ALL_MAPS = ("garden", "desert", "ocean", "jungle", "anthell", "sewers", "factory")


def test_every_supported_map_is_valid_and_selectable_in_the_gui():
    assert app_config._VALID_MAPS == _ALL_MAPS
    assert app_config._GUI_ENABLED_MAPS == _ALL_MAPS
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


# ── 第②轮: enemy_rules ────────────────────────────────────────────────────────

_EMPTY_RULES = {"species": {}, "knobs": {}}


class TestEnemyRulesCoerce:
    def test_defaults_and_active_keys(self):
        assert app_config.DEFAULTS["enemy_rules"] == _EMPTY_RULES
        assert "enemy_rules" in app_config._ACTIVE_KEYS
        assert app_config.DEFAULTS_V2["active"]["enemy_rules"] == _EMPTY_RULES

    def test_knob_tables(self):
        assert set(app_config.ENEMY_KNOB_RANGES) == {
            "hold_active_px", "hold_passive_px", "chase_max_px", "far_chase_px", "avoid_px",
            "cautious_px"}
        assert all(r == (10, 3000) for r in app_config.ENEMY_KNOB_RANGES.values())
        # swarm 是布尔、avoid_min_rarity 是档名(第③轮), 都不在整数范围表里
        assert app_config.ENEMY_KNOB_KEYS == tuple(app_config.ENEMY_KNOB_RANGES) + (
            "swarm", "avoid_min_rarity")
        assert app_config._ENEMY_RULE_VALUES == ("fight", "cautious", "avoid", "ignore")
        import enemy_species
        assert app_config._ENEMY_RARITY_KEYS == ("*",) + tuple(enemy_species.RARITY_ORDER)

    @pytest.mark.parametrize("junk", [None, 5, "x", [], [1]])
    def test_non_dict_gives_empty(self, junk):
        assert app_config._coerce_enemy_rules(junk, "ocean") == _EMPTY_RULES

    @pytest.mark.parametrize("junk", [5, "x", [], [1], True])
    def test_non_dict_enemy_rules_warns_and_names_the_bad_type(self, junk, capsys):
        # 以前这里悄悄换成空, 手改坏了 config.json 的人不知道自己的规则被丢了。
        assert app_config._coerce_enemy_rules(junk, "ocean") == _EMPTY_RULES
        out = capsys.readouterr().out
        assert "enemy_rules" in out and type(junk).__name__ in out

    @pytest.mark.parametrize("key", ["species", "knobs"])
    @pytest.mark.parametrize("junk", [5, "x", [], [1], True])
    def test_present_but_non_dict_species_or_knobs_warns(self, key, junk, capsys):
        got = app_config._coerce_enemy_rules({key: junk}, "ocean")
        assert got == _EMPTY_RULES
        out = capsys.readouterr().out
        assert f"enemy_rules.{key}" in out and type(junk).__name__ in out

    def test_one_bad_subkey_does_not_lose_the_other(self, capsys):
        got = app_config._coerce_enemy_rules(
            {"species": 3, "knobs": {"swarm": False}}, "ocean")
        assert got == {"species": {}, "knobs": {"swarm": False}}
        assert "enemy_rules.species" in capsys.readouterr().out

    @pytest.mark.parametrize("absent", [
        None, {}, {"species": None}, {"knobs": None}, {"species": None, "knobs": None},
        {"species": {}, "knobs": {}},
    ])
    def test_absent_none_or_empty_enemy_rules_stay_silent(self, absent, capsys):
        # 旧配置没有这个键(load 路径传 None) —— 不能每次启动都报一堆警告。
        assert app_config._coerce_enemy_rules(absent, "ocean") == _EMPTY_RULES
        assert capsys.readouterr().out == ""

    def test_valid_rules_pass_through(self):
        rules = {"species": {"bubble": "ignore", "shell": "avoid", "crab": "fight"},
                 "knobs": {"hold_active_px": 100, "chase_max_px": 3000, "avoid_px": 10,
                           "cautious_px": 300, "swarm": False}}
        assert app_config._coerce_enemy_rules(rules, "ocean") == rules

    def test_result_is_a_copy(self):
        rules = {"species": {"bubble": "ignore"}, "knobs": {"swarm": True}}
        got = app_config._coerce_enemy_rules(rules, "ocean")
        got["species"]["bubble"] = "fight"
        assert rules["species"]["bubble"] == "ignore"

    def test_unknown_species_and_other_maps_species_are_dropped_with_a_warning(self, capsys):
        got = app_config._coerce_enemy_rules(
            {"species": {"bubble": "ignore", "nonsense": "ignore", "scorpion": "ignore"}}, "ocean")
        assert got["species"] == {"bubble": "ignore"}
        out = capsys.readouterr().out
        assert "nonsense" in out and "scorpion" in out

    def test_bad_rule_values_are_dropped(self, capsys):
        got = app_config._coerce_enemy_rules(
            {"species": {"bubble": "dance", "shell": 3, "crab": None, "leech": ["ignore"],
                         "starfish": "avoid"}}, "ocean")
        assert got["species"] == {"starfish": "avoid"}
        assert "bubble" in capsys.readouterr().out

    def test_bad_knobs_are_dropped(self, capsys):
        got = app_config._coerce_enemy_rules({"knobs": {
            "hold_active_px": 9,          # 低于下限
            "hold_passive_px": 3001,      # 高于上限
            "chase_max_px": True,         # bool 不是整数
            "far_chase_px": 600.0,        # float 不是整数
            "avoid_px": "400",            # 字符串
            "cautious_px": 5,             # 低于下限
            "swarm": 1,                   # 不是 bool
            "mystery": 5,                 # 未知键
        }}, "ocean")
        assert got["knobs"] == {}
        out = capsys.readouterr().out
        assert "hold_active_px" in out and "mystery" in out

    @pytest.mark.parametrize("tier", ["Epic", "Legendary", "Mythic", "Ultra", "Super", "Eternal",
                                      "Unique", "never"])
    def test_avoid_min_rarity_accepts_every_listed_tier(self, tier):
        got = app_config._coerce_enemy_rules({"knobs": {"avoid_min_rarity": tier}}, "ocean")
        assert got["knobs"] == {"avoid_min_rarity": tier}

    @pytest.mark.parametrize("junk", ["Common", "Unusual", "Rare", "ultra", "NEVER", "", "Foo",
                                      None, 5, True, ["Epic"], {"a": 1}])
    def test_avoid_min_rarity_drops_anything_else_and_says_so(self, junk, capsys):
        # 下限是 Epic: Common..Rare 等于每只怪都躲, 当手滑拦掉; 大小写敏感(跟 config 里别的枚举一样)。
        got = app_config._coerce_enemy_rules({"knobs": {"avoid_min_rarity": junk}}, "ocean")
        assert got["knobs"] == {}
        assert "avoid_min_rarity" in capsys.readouterr().out

    def test_the_tier_names_come_from_the_pure_data_module_and_are_real_rarities(self):
        import enemy_species
        assert app_config.enemy_species is enemy_species
        assert set(enemy_species.AVOID_MIN_RARITY_CHOICES) - {"never"} <= set(enemy_species.RARITY_ORDER)

    def test_bool_is_never_an_integer_knob_even_if_the_range_would_admit_it(self, monkeypatch, capsys):
        # True == 1 / False == 0: 默认下限是 10, 光靠范围检查就把 bool 挡掉了, 那条 `not isinstance(val, bool)`
        # 守卫看不出有没有 —— 把下限放到 0, 逼出守卫本身。
        monkeypatch.setitem(app_config.ENEMY_KNOB_RANGES, "chase_max_px", (0, 3000))
        assert app_config._enemy_knob_ok("chase_max_px", 1) is True
        assert app_config._enemy_knob_ok("chase_max_px", 0) is True
        for b in (True, False):
            assert app_config._enemy_knob_ok("chase_max_px", b) is False
            got = app_config._coerce_enemy_rules({"knobs": {"chase_max_px": b}}, "ocean")
            assert got["knobs"] == {}
        assert "chase_max_px" in capsys.readouterr().out

    def test_species_not_checked_against_a_bad_map(self):
        assert app_config._coerce_enemy_rules({"species": {"bubble": "ignore"}}, None)["species"] == {}

    def test_missing_subkeys_are_fine(self):
        assert app_config._coerce_enemy_rules({}, "ocean") == _EMPTY_RULES
        assert app_config._coerce_enemy_rules({"knobs": {"swarm": False}}, "ocean") == {
            "species": {}, "knobs": {"swarm": False}}

    def test_extra_top_level_keys_are_ignored_on_read(self):
        got = app_config._coerce_enemy_rules({"species": {}, "knobs": {}, "zzz": 1}, "ocean")
        assert got == _EMPTY_RULES

    def test_no_species_means_enemy_detect_is_not_imported(self, monkeypatch):
        # app_config 在 GUI 启动路径上, 不能为了一个空规则去拉 cv2 / cdp_bridge
        import builtins
        real = builtins.__import__

        def guard(name, *a, **k):
            if name == "enemy_detect":
                raise AssertionError("不该导入 enemy_detect")
            return real(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", guard)
        assert app_config._coerce_enemy_rules({"knobs": {"swarm": True}}, "ocean") == {
            "species": {}, "knobs": {"swarm": True}}
        assert app_config._coerce_enemy_rules(None, "ocean") == _EMPTY_RULES

    def test_importing_app_config_does_not_import_enemy_detect(self):
        # 上一条只守「调用时」; 这条守「import 时」—— 模块顶部写一句 import enemy_detect 就会让每个
        # 只想读配置的进程(GUI 启动、webctl)背上 cv2 / cdp_bridge。起独立解释器, 理由同上面那条 tkinter 测试。
        import subprocess
        import sys as _sys
        r = subprocess.run(
            [_sys.executable, "-c",
             "import app_config, sys; "
             "print(','.join(m for m in ('enemy_detect', 'cv2', 'cdp_bridge') if m in sys.modules))"],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "", f"app_config 在 import 时拉进了: {r.stdout.strip()}"

    def test_coercing_and_validating_species_never_imports_the_heavy_modules(self, tmp_path):
        # Task 2 先用了函数内延迟导入, 但 load_config() 读到带物种条目的时块、validate_config()
        # 校验带物种条目的配置照样会触发它(GUI 启动路径 / 无显示器的网页控制台进程)。
        # 所以子进程里走**真实入口**: load_config 读一份真 config.json, validate_config 校验好坏两份,
        # 再查 sys.modules。起独立解释器 —— 本进程里别的测试早把 enemy_detect / cv2 拉进来了。
        import json as _json
        import os
        import subprocess
        import sys as _sys

        on_disk = _v2_cfg()
        on_disk["schedule"] = [_v2_block(
            map="ocean", enemy_rules={"species": {"bubble": "ignore"}})]
        p = tmp_path / "config.json"
        p.write_text(_json.dumps(on_disk, ensure_ascii=False), encoding="utf-8")

        good = TestValidateConfig()._valid()
        good["schedule"][0]["map"] = good["active"]["map"] = "ocean"
        good["schedule"][0]["enemy_rules"] = {"species": {"bubble": "ignore", "shell": "avoid"}}
        good["active"]["enemy_rules"] = {"species": {"crab": "fight"}}
        bad = copy.deepcopy(good)
        bad["schedule"][0]["enemy_rules"] = {"species": {"scorpion": "ignore"}}

        # 每走完一个入口就查一次 sys.modules(并点名是哪一步拖进来的): 前面的步骤都干净才会走到后面,
        # 所以哪个入口单独退化都能被抓到, 不会被前一步先拖进来的模块掩盖。
        code = (
            "import sys, json, app_config\n"
            "cfg_file, good_s, bad_s = sys.argv[1:4]\n"
            "HEAVY = {'enemy_detect', 'cv2', 'cdp_bridge', 'pyautogui', 'utils'}\n"
            "def clean(step):\n"
            "    bad = HEAVY & set(sys.modules)\n"
            "    assert not bad, f'{step} 拖进了 {sorted(bad)}'\n"
            "clean('import app_config')\n"
            # 1) 真实入口 load_config: 带物种条目的时块, 物种要原样活下来
            "app_config.CONFIG_PATH = cfg_file\n"
            "loaded = app_config.load_config()\n"
            "assert loaded['schedule'][0]['enemy_rules']['species'] == {'bubble': 'ignore'}, loaded\n"
            "clean('load_config')\n"
            # 2) 真实入口 validate_config: 好的配置 0 错, 坏 slug 要报带路径的错
            "assert app_config.validate_config(json.loads(good_s)) == []\n"
            "clean('validate_config(合法配置)')\n"
            "errs = app_config.validate_config(json.loads(bad_s))\n"
            "assert any('schedule[0].enemy_rules.species.scorpion' in e for e in errs), errs\n"
            "clean('validate_config(坏 slug)')\n"
            # 3) 直接调内部函数
            "got = app_config._coerce_enemy_rules({'species': {'bubble': 'ignore'}}, 'ocean')\n"
            "assert got['species'] == {'bubble': 'ignore'}, got\n"
            "clean('_coerce_enemy_rules')\n"
            "errs = app_config._validate_enemy_rules({'species': {'scorpion': 'ignore'}}, 'ocean', 'x')\n"
            "assert errs, errs\n"
            "clean('_validate_enemy_rules')\n"
        )
        repo_root = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run(
            [_sys.executable, "-c", code, str(p), _json.dumps(good), _json.dumps(bad)],
            capture_output=True, text=True, cwd=repo_root)
        assert r.returncode == 0, r.stderr


class TestEnemyRulesInBlocks:
    def test_block_without_the_key_gets_empty_rules_and_is_kept(self, cfg_path):
        cfg = _v2_cfg()
        app_config.save_config(cfg)
        got = app_config.load_config()
        assert got["schedule"][0]["enemy_rules"] == _EMPTY_RULES
        assert got["active"]["enemy_rules"] == _EMPTY_RULES

    def test_loading_a_config_without_the_key_prints_nothing(self, cfg_path, capsys):
        # 所有现存配置都没有 enemy_rules —— 新加的「类型不对」警告不能在它们身上响。
        app_config.save_config(_v2_cfg())
        capsys.readouterr()
        app_config.load_config()
        assert capsys.readouterr().out == ""

    def test_block_with_garbage_rules_is_not_thrown_away(self, cfg_path):
        for junk in (5, "x", [1], {"species": 3, "knobs": "y"}):
            cfg = _v2_cfg()
            cfg["schedule"] = [_v2_block(enemy_rules=junk)]
            app_config.save_config(cfg)
            got = app_config.load_config()
            assert len(got["schedule"]) == 1
            assert got["schedule"][0]["enemy_rules"] == _EMPTY_RULES

    def test_loading_a_hand_edited_file_with_garbage_rules_warns_and_keeps_the_block(
            self, cfg_path, capsys):
        # save_config 写之前会先规整, 所以要走「读」这条路径就得直接写 JSON(手改 config.json 的场景)。
        for junk, needle in ((5, "enemy_rules"), ({"species": 3}, "enemy_rules.species"),
                             ({"knobs": "y"}, "enemy_rules.knobs")):
            cfg = _v2_cfg()
            cfg["schedule"] = [_v2_block(enemy_rules=junk)]
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            capsys.readouterr()
            got = app_config.load_config()
            assert len(got["schedule"]) == 1
            assert got["schedule"][0]["enemy_rules"] == _EMPTY_RULES
            assert needle in capsys.readouterr().out

    def test_rules_roundtrip_per_block_and_use_the_blocks_own_map(self, cfg_path):
        cfg = _v2_cfg()
        cfg["schedule"] = [
            _v2_block(id="a", map="ocean", days=[0], start="00:00", end="12:00",
                      enemy_rules={"species": {"bubble": "ignore", "scorpion": "ignore"},
                                   "knobs": {"chase_max_px": 400}}),
            _v2_block(id="b", map="desert", days=[0], start="12:00", end="23:59",
                      enemy_rules={"species": {"scorpion": "avoid"}}),
        ]
        app_config.save_config(cfg)
        got = app_config.load_config()
        a, b = got["schedule"]
        assert a["enemy_rules"] == {"species": {"bubble": "ignore"},     # scorpion 不属于 ocean
                                    "knobs": {"chase_max_px": 400}}
        assert b["enemy_rules"] == {"species": {"scorpion": "avoid"}, "knobs": {}}

    def test_flat_v1_config_with_valid_rules_is_kept_and_does_not_warn(self, capsys):
        got = app_config._coerce_v1({"map": "ocean", "enemy_rules": {
            "species": {"bubble": "ignore"}, "knobs": {"swarm": False}}})
        assert got["enemy_rules"] == {"species": {"bubble": "ignore"}, "knobs": {"swarm": False}}
        assert capsys.readouterr().out == ""        # 不能掉进 "else: 当布尔" 那个分支报「值不合法」

    def test_flat_v1_config_with_garbage_rules_falls_back_to_empty(self):
        assert app_config._coerce_v1({"enemy_rules": "x"})["enemy_rules"] == _EMPTY_RULES
        assert app_config._coerce_v1({})["enemy_rules"] == _EMPTY_RULES

    def test_migrate_v1_carries_rules_into_block_and_active(self, cfg_path):
        cfg_path.write_text(json.dumps({
            "map": "ocean", "location": [1, 2], "farming_area": [[0, 0], [3, 3]],
            "farming_duration": 100, "consecutive_short_round_limit": 1,
            "enemy_ai_enabled": True, "auto_switch_server": True,
            "enemy_rules": {"species": {"bubble": "ignore"}},
        }), encoding="utf-8")
        got = app_config.load_config()
        want = {"species": {"bubble": "ignore"}, "knobs": {}}
        assert got["schedule"][0]["enemy_rules"] == want
        assert got["active"]["enemy_rules"] == want

    def test_active_slice_rules_are_normalized_with_the_actives_map(self):
        cfg = app_config._coerce({
            "version": 2, "afk_enabled": False,
            "profiles": [{"alias": "默认", "dir": "chrome-profiles/默认"}],
            "schedule": [],
            "active": {"map": "ocean", "enemy_rules": {"species": {"bubble": "ignore",
                                                                    "scorpion": "ignore"}}},
        })
        assert cfg["active"]["enemy_rules"] == {"species": {"bubble": "ignore"}, "knobs": {}}


class TestEnemyRulesValidate:
    def _cfg(self, rules="__absent__", active_rules="__absent__", map_name="ocean"):
        base = TestValidateConfig()._valid()
        base["schedule"][0]["map"] = map_name
        base["active"]["map"] = map_name
        if rules != "__absent__":
            base["schedule"][0]["enemy_rules"] = rules
        if active_rules != "__absent__":
            base["active"]["enemy_rules"] = active_rules
        return base

    def test_absent_is_valid(self):
        assert app_config.validate_config(self._cfg()) == []

    def test_empty_and_full_valid_rules(self):
        assert app_config.validate_config(self._cfg(rules={})) == []
        assert app_config.validate_config(self._cfg(rules=_EMPTY_RULES)) == []
        full = {"species": {"bubble": "ignore", "shell": "avoid", "crab": "fight"},
                "knobs": {"hold_active_px": 10, "hold_passive_px": 3000, "chase_max_px": 300,
                          "far_chase_px": 600, "avoid_px": 400, "cautious_px": 500, "swarm": True}}
        assert app_config.validate_config(self._cfg(rules=full, active_rules=full)) == []

    def test_defaults_v2_with_empty_active_rules_validates(self):
        # webctl 的 "POST DEFAULTS_V2" 测试依赖这条
        assert app_config.validate_config(app_config.DEFAULTS_V2) == []

    @pytest.mark.parametrize("bad", [5, "x", [], [1]])
    def test_non_object_rejected(self, bad):
        errs = app_config.validate_config(self._cfg(rules=bad))
        assert errs == ["schedule[0].enemy_rules 必须是对象 {species, knobs}"]

    def test_extra_keys_rejected(self):
        errs = app_config.validate_config(self._cfg(rules={"species": {}, "zzz": 1}))
        assert errs == ["schedule[0].enemy_rules 多出了不认识的键: zzz"]

    def test_species_must_be_an_object(self):
        errs = app_config.validate_config(self._cfg(rules={"species": ["bubble"]}))
        assert any("schedule[0].enemy_rules.species 必须是对象" in e for e in errs)

    def test_unknown_species_and_other_maps_species_rejected_with_paths(self):
        errs = app_config.validate_config(self._cfg(
            rules={"species": {"bubble": "ignore", "scorpion": "ignore", "nonsense": "avoid"}}))
        assert len(errs) == 2
        assert any("schedule[0].enemy_rules.species.scorpion" in e for e in errs)
        assert any("schedule[0].enemy_rules.species.nonsense" in e for e in errs)

    def test_bad_rule_value_rejected(self):
        errs = app_config.validate_config(self._cfg(rules={"species": {"bubble": "dance"}}))
        assert len(errs) == 1
        assert "schedule[0].enemy_rules.species.bubble" in errs[0] and "'dance'" in errs[0]

    def test_knobs_must_be_an_object(self):
        errs = app_config.validate_config(self._cfg(rules={"knobs": [1]}))
        assert any("schedule[0].enemy_rules.knobs 必须是对象" in e for e in errs)

    @pytest.mark.parametrize("key, val", [
        ("hold_active_px", 9), ("hold_passive_px", 3001), ("chase_max_px", True),
        ("far_chase_px", 6.5), ("avoid_px", "400"), ("cautious_px", 9), ("cautious_px", 3001),
        ("cautious_px", "500"), ("swarm", 1), ("swarm", "yes"),
        ("mystery", 5),
    ])
    def test_bad_knob_rejected_with_path(self, key, val):
        errs = app_config.validate_config(self._cfg(rules={"knobs": {key: val}}))
        assert len(errs) == 1
        assert f"schedule[0].enemy_rules.knobs.{key}" in errs[0]

    @pytest.mark.parametrize("tier", ["Epic", "Ultra", "Unique", "never"])
    def test_avoid_min_rarity_accepts_listed_tiers(self, tier):
        assert app_config.validate_config(self._cfg(rules={"knobs": {"avoid_min_rarity": tier}})) == []

    @pytest.mark.parametrize("val", ["Common", "Rare", "ultra", "", 5, True, None, ["Epic"]])
    def test_bad_avoid_min_rarity_rejected_with_path_and_the_choices(self, val):
        errs = app_config.validate_config(self._cfg(rules={"knobs": {"avoid_min_rarity": val}}))
        assert len(errs) == 1
        assert "schedule[0].enemy_rules.knobs.avoid_min_rarity" in errs[0]
        assert "Epic" in errs[0] and "Unique" in errs[0] and "never" in errs[0]

    def test_bool_knob_rejected_even_if_the_range_would_admit_it(self, monkeypatch):
        monkeypatch.setitem(app_config.ENEMY_KNOB_RANGES, "chase_max_px", (0, 3000))
        for b in (True, False):
            errs = app_config.validate_config(self._cfg(rules={"knobs": {"chase_max_px": b}}))
            assert len(errs) == 1
            assert "schedule[0].enemy_rules.knobs.chase_max_px" in errs[0]

    def test_active_slice_is_validated_too_with_its_own_prefix(self):
        errs = app_config.validate_config(self._cfg(
            active_rules={"species": {"scorpion": "ignore"}, "knobs": {"swarm": 1}}))
        assert any("active.enemy_rules.species.scorpion" in e for e in errs)
        assert any("active.enemy_rules.knobs.swarm" in e for e in errs)

    def test_a_bad_map_does_not_cascade_into_species_errors(self):
        errs = app_config.validate_config(self._cfg(
            rules={"species": {"bubble": "ignore"}}, map_name="atlantis"))
        assert not any("enemy_rules" in e for e in errs)       # 地图那条错误已经报了
        assert any(".map 必须是" in e for e in errs)


# ── 逐物种 × 逐稀有度: species[slug] 可以是 {稀有度: 打法}("*" = 没单独配的稀有度的默认) ─────────────

class TestPerRarityRules:
    def _coerce(self, species, map_name="garden"):
        return app_config._coerce_enemy_rules({"species": species}, map_name)["species"]

    def test_cautious_is_a_valid_action(self):
        assert self._coerce({"bee": "cautious"}) == {"bee": "cautious"}

    def test_a_per_rarity_dict_is_kept_in_rarity_order(self):
        got = self._coerce({"bee": {"Ultra": "avoid", "*": "fight", "Common": "cautious", "Epic": "ignore"}})
        assert got == {"bee": {"*": "fight", "Common": "cautious", "Epic": "ignore", "Ultra": "avoid"}}
        assert list(got["bee"]) == ["*", "Common", "Epic", "Ultra"]               # 存盘顺序固定

    def test_every_rarity_name_and_the_star_are_accepted(self):
        import enemy_species
        every = {k: "avoid" for k in ("*",) + tuple(enemy_species.RARITY_ORDER)}
        assert self._coerce({"bee": every}) == {"bee": every}

    def test_a_dict_with_only_the_star_collapses_to_the_plain_string(self):
        assert self._coerce({"bee": {"*": "ignore"}}) == {"bee": "ignore"}        # 老写法和新写法是同一个意思

    @pytest.mark.parametrize("bad_key", ["epic", "EPIC", "Foo", "", "all", 5, None])
    def test_unknown_rarity_keys_are_dropped_with_a_warning_and_the_rest_survives(self, bad_key, capsys):
        got = self._coerce({"bee": {bad_key: "avoid", "Epic": "fight"}})
        assert got == {"bee": {"Epic": "fight"}}
        out = capsys.readouterr().out
        assert "bee" in out and repr(bad_key) in out

    @pytest.mark.parametrize("bad", ["dance", "", "FIGHT", None, 5, True, ["avoid"], {"a": 1}])
    def test_bad_actions_inside_the_dict_are_dropped_with_a_warning(self, bad, capsys):
        got = self._coerce({"bee": {"Epic": bad, "Rare": "avoid"}})
        assert got == {"bee": {"Rare": "avoid"}}
        assert "bee.Epic" in capsys.readouterr().out

    def test_a_dict_that_ends_up_empty_drops_the_species(self, capsys):
        assert self._coerce({"bee": {"Epic": "dance"}, "ladybug": {}}) == {}
        out = capsys.readouterr().out
        assert "bee.Epic" in out and "ladybug" in out

    def test_only_the_bad_species_is_dropped_the_others_stay(self):
        got = self._coerce({"bee": {"Epic": "dance"}, "ladybug": {"Epic": "avoid"}, "rock": "ignore"})
        assert got == {"ladybug": {"Epic": "avoid"}, "rock": "ignore"}

    @pytest.mark.parametrize("junk", [5, None, [], ["avoid"], True, 1.5])
    def test_a_value_that_is_neither_string_nor_dict_is_dropped(self, junk, capsys):
        assert self._coerce({"bee": junk}) == {}
        assert "bee" in capsys.readouterr().out

    def test_a_per_rarity_dict_for_a_species_not_on_the_map_is_dropped(self, capsys):
        assert self._coerce({"scorpion": {"Epic": "avoid"}}, "garden") == {}
        assert "scorpion" in capsys.readouterr().out

    def test_coercion_is_idempotent_and_never_aliases_the_input(self):
        src = {"bee": {"Epic": "avoid", "*": "fight"}}
        once = self._coerce(src)
        assert app_config._coerce_enemy_rules({"species": once}, "garden")["species"] == once
        once["bee"]["Epic"] = "ignore"
        assert src == {"bee": {"Epic": "avoid", "*": "fight"}}

    def test_per_rarity_rules_survive_load_save_and_both_slices(self, cfg_path):
        rules = {"species": {"bee": {"Epic": "cautious", "Ultra": "avoid"}, "rock": "ignore"}, "knobs": {}}
        raw = _v2_cfg(schedule=[_v2_block(map="garden", enemy_rules=rules)])
        raw["active"] = {"map": "garden", "enemy_rules": rules}
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        got = app_config.load_config()
        assert got["schedule"][0]["enemy_rules"] == rules and got["active"]["enemy_rules"] == rules
        app_config.save_config(got)
        again = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert again["schedule"][0]["enemy_rules"] == rules


class TestPerRarityValidation:
    def _errs(self, species, map_name="garden"):
        cfg = _v2_cfg(schedule=[_v2_block(map=map_name, enemy_rules={"species": species})])
        return app_config.validate_config(cfg)

    def test_valid_dicts_and_cautious_pass(self):
        assert self._errs({"bee": {"*": "fight", "Epic": "cautious", "Ultra": "avoid", "Unique": "ignore"},
                           "rock": "cautious"}) == []

    @pytest.mark.parametrize("key", ["epic", "Foo", "", "all"])
    def test_an_unknown_rarity_key_is_reported_with_its_path_and_the_choices(self, key):
        errs = self._errs({"bee": {key: "avoid"}})
        assert len(errs) == 1
        assert f"schedule[0].enemy_rules.species.bee.{key}" in errs[0]
        assert "Common" in errs[0] and "Unique" in errs[0] and "*" in errs[0]

    @pytest.mark.parametrize("bad", ["dance", "", None, 5, True, ["avoid"]])
    def test_a_bad_action_is_reported_with_the_rarity_in_the_path(self, bad):
        errs = self._errs({"bee": {"Epic": bad}})
        assert len(errs) == 1 and "schedule[0].enemy_rules.species.bee.Epic" in errs[0]
        assert "fight" in errs[0] and "cautious" in errs[0] and "avoid" in errs[0] and "ignore" in errs[0]

    def test_an_empty_dict_is_an_error_not_silently_nothing(self):
        errs = self._errs({"bee": {}})
        assert len(errs) == 1 and "schedule[0].enemy_rules.species.bee" in errs[0]

    @pytest.mark.parametrize("junk", [5, None, [], ["avoid"], True])
    def test_a_value_of_the_wrong_type_is_reported(self, junk):
        errs = self._errs({"bee": junk})
        assert len(errs) == 1 and "schedule[0].enemy_rules.species.bee" in errs[0]

    def test_a_species_not_on_the_map_is_reported_even_with_a_dict(self):
        errs = self._errs({"scorpion": {"Epic": "avoid"}})
        assert len(errs) == 1 and "scorpion" in errs[0] and "garden" in errs[0]

    def test_the_active_slice_is_validated_too(self):
        cfg = _v2_cfg()
        cfg["active"] = {"map": "garden", "enemy_rules": {"species": {"bee": {"Epic": "dance"}}}}
        assert any("active.enemy_rules.species.bee.Epic" in e for e in app_config.validate_config(cfg))
