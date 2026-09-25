import gui_accounts as ga


def _cfg():
    return {
        "version": 2, "afk_enabled": False,
        "profiles": [{"alias": "默认", "dir": "chrome-profiles/默认"},
                     {"alias": "小号2", "dir": "chrome-profiles/小号2"}],
        "schedule": [
            {"id": "blk-1", "enabled": True, "profile": "默认"},
            {"id": "blk-2", "enabled": True, "profile": "小号2"},
            {"id": "blk-3", "enabled": False, "profile": "小号2"},
        ],
        "active": {},
    }


def test_add_profile_ok():
    cfg, err = ga.add_profile(_cfg(), "新号")
    assert err is None
    assert cfg["profiles"][-1] == {"alias": "新号", "dir": "chrome-profiles/新号"}


def test_add_profile_rejects_blank():
    _, err = ga.add_profile(_cfg(), "   ")
    assert err


def test_add_profile_rejects_pure_symbols():
    _, err = ga.add_profile(_cfg(), "***")
    assert err


def test_add_profile_rejects_duplicate():
    _, err = ga.add_profile(_cfg(), "小号2")
    assert err


def test_rename_profile_updates_schedule_refs():
    cfg, err = ga.rename_profile(_cfg(), "小号2", "big")
    assert err is None
    aliases = [p["alias"] for p in cfg["profiles"]]
    assert "big" in aliases and "小号2" not in aliases
    assert [b["profile"] for b in cfg["schedule"]] == ["默认", "big", "big"]
    assert cfg["profiles"][1]["dir"] == "chrome-profiles/big"


def test_rename_profile_rejects_duplicate_target():
    _, err = ga.rename_profile(_cfg(), "小号2", "默认")
    assert err


def test_delete_profile_blocked_by_references():
    cfg, err = ga.delete_profile(_cfg(), "小号2")
    assert "blk-2" in err and "blk-3" in err
    assert len(cfg["profiles"]) == 2   # 没删


def test_delete_profile_ok_when_unreferenced():
    c = _cfg()
    c["schedule"] = [{"id": "blk-1", "enabled": True, "profile": "默认"}]
    cfg, err = ga.delete_profile(c, "小号2")
    assert err is None
    assert [p["alias"] for p in cfg["profiles"]] == ["默认"]


def test_profile_dir_lookup():
    assert ga.profile_dir(_cfg(), "小号2") == "chrome-profiles/小号2"
    assert ga.profile_dir(_cfg(), "没有") is None
