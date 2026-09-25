"""GUI 与 worker 共用的配置读写. main.py 以前把这些值硬编码在 __main__ 里,
现在集中到 exe 同级的 config.json —— GUI 写, worker(`main.py --worker`)读.

设计要点跟 afk_watch._write_afk_config() 一致: 读不出来/键坏了不抛, 回落到
DEFAULTS(= 以前硬编码那套) + print 一句警告. 这里只读不写用户那份坏文件,
所以不需要像 afk_watch 那样备份 .bak.
"""
import copy
import json
import os
import re
import sys

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(sys.argv[0])), "config.json"
)

# 每个值 = 改造前 main.py __main__ 里的硬编码值.
DEFAULTS = {
    "map": "desert",
    "location": [22, 32],
    "farming_area": [[9, 8], [51, 56]],
    "farming_duration": 300,
    "consecutive_short_round_limit": 2,
    # 这份扁平 DEFAULTS 现在只当两件事的兜底: (1) v1 迁移取值 (2) 空 schedule 时
    # 的 active 占位. 索敌这里留 False 是保守占位 —— 空 schedule 时 worker 根本不
    # 跑. 用户"新建时块"的默认是开 (gui_schedule.new_block_template / 编辑器).
    # 索敌早已不需要模型文件, 改成解码 canvas 绘制调用了.
    "enemy_ai_enabled": False,
    "auto_switch_server": True,
    # florr 反转攻击键 / 反转防御键: 每个时块单独配 (在 _ACTIVE_KEYS 里). worker 每轮
    # 把对应 WASM 字节写成 (1 if True else 0). 这里的扁平默认给 _ACTIVE_KEYS 迁移 +
    # 空 schedule 时 active 兜底用; 新建时块的默认在 gui_schedule 里.
    "invert_attack": True,
    "invert_defense": False,
    # 进游戏 / 寻路到刷怪区时按一次和弦切换 florr loadout.
    # {enabled: 开关, mod: 修饰键 "none"/"k"/"l", digit: 数字键 "1".."0"}.
    # enabled 打开时: 按住 mod (若非 none) → 按 digit → 松开 mod (像 Ctrl+C).
    "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
    "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
    "afk_enabled": False,
}

# maps/ 下的 3 个 png(去扩展名). 新增地图要同步这里 —— 跟 utils.check_map_border()
# 那种"就地写死一小张表"的仓库既有做法一致, 不在 import 时去 listdir.
_VALID_MAPS = ("desert", "ocean", "anthell")

# GUI 时块编辑器里实际可选的地图。不在这里的(ocean)在界面上置灰、标「暂不可用」。
# anthell 2026-09-11 解锁 —— 它走的是"先进花园再踩洞口传送"的多阶段进场路线
# (见 map_routes.py), 不是标题页直接选生态区。ocean 仍不放开: 没有任何人验证过
# 它的寻路图和墙壁色。coerce 层(_VALID_MAPS)不受影响, 手写 config.json / 旧时块
# 里的 ocean 仍能跑。
_GUI_ENABLED_MAPS = ("desert", "anthell")

# 一个时块 / active 切片里的刷怪参数键(不含 afk_enabled —— 那是 GUI 全局的).
_ACTIVE_KEYS = (
    "map", "location", "farming_area", "farming_duration",
    "consecutive_short_round_limit", "enemy_ai_enabled", "auto_switch_server",
    "enter_game_swap", "reach_area_swap",
    "invert_attack", "invert_defense",
)
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# loadout 切换和弦: 修饰键 + 数字键. tuple 不是 str —— `"12" in "1234567890"` 会
# 子串匹配误判成合法, tuple 成员判定才对.
_SWAP_MODS = ("none", "k", "l")
_SWAP_DIGITS = tuple("1234567890")


def _coerce_swap_obj(v):
    """把任意值规范成 loadout 切换和弦对象 {enabled, mod, digit}.

    非 dict / 键坏一律回落默认 {enabled: False, mod: "none", digit: "1"} —— 刚 ship
    的旧字符串形式 ("none"/"digits"/"k"/"l") 也走这条, 不猜 (合并才几分钟, 没有真实
    用户 config). enabled 非严格 True 一律当 False.
    """
    if not isinstance(v, dict):
        return {"enabled": False, "mod": "none", "digit": "1"}
    mod = v.get("mod")
    digit = v.get("digit")
    return {
        "enabled": v.get("enabled") is True,
        "mod": mod if mod in _SWAP_MODS else "none",
        "digit": digit if isinstance(digit, str) and digit in _SWAP_DIGITS else "1",
    }


def _is_int_pair(v):
    return (
        isinstance(v, (list, tuple))
        and len(v) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) for n in v)
    )


def _coerce_v1(raw):
    """阶段1 的扁平校验: 拿 DEFAULTS 当底, 逐键把 raw 里合法的值覆盖上去; 不合法
    的键留默认 + 警告. 未知键直接丢掉. 现在只用于迁移旧文件 + 规整 active 切片."""
    cfg = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        print(f"⚠️ config.json 顶层不是对象(是 {type(raw).__name__}), 全部用默认值")
        return cfg

    for key, default in DEFAULTS.items():
        if key not in raw:
            continue
        val = raw[key]
        if key in ("enter_game_swap", "reach_area_swap"):
            cfg[key] = _coerce_swap_obj(val)
            continue
        ok = False
        if key == "map":
            ok = isinstance(val, str) and val in _VALID_MAPS
        elif key == "location":
            ok = _is_int_pair(val)
            if ok:
                val = [int(val[0]), int(val[1])]
        elif key == "farming_area":
            ok = (
                isinstance(val, (list, tuple))
                and len(val) == 2
                and all(_is_int_pair(corner) for corner in val)
            )
            if ok:
                val = [[int(c[0]), int(c[1])] for c in val]
        elif key == "farming_duration":
            ok = isinstance(val, int) and not isinstance(val, bool) and val > 0
        elif key == "consecutive_short_round_limit":
            ok = isinstance(val, int) and not isinstance(val, bool) and val >= 1
        else:  # 其余全是 bool 开关
            ok = isinstance(val, bool)

        if ok:
            cfg[key] = val
        else:
            print(f"⚠️ config.json 的 {key} 值不合法({val!r}), 用默认 {default!r}")

    return cfg


# ─────────────────────────── v2 schema ───────────────────────────
# 顶层: version / afk_enabled / profiles / schedule / active.
# 阶段2 新增: 按星期几的时块调度 + 独立 Chrome profile.

DEFAULTS_V2 = {
    "version": 2,
    "afk_enabled": False,
    "profiles": [{"alias": "默认", "dir": "chrome-profiles/默认"}],
    # 全新装是空的 —— 用户自己加时块. 空 schedule 时 worker 不会真跑.
    "schedule": [],
    # 占位: 只在"全新装 + 空 schedule"时当 active 兜底. 索敌默认值沿用 DEFAULTS
    # 里的 False —— 迁移不该强翻用户没开的功能; "默认开索敌"体现在 GUI 新建时块
    # 的默认值上(gui_schedule._new_block_template / 编辑器), 不在这里.
    "active": {k: copy.deepcopy(DEFAULTS[k]) for k in _ACTIVE_KEYS},
}


def _coerce_profiles(v):
    out, seen = [], set()
    if isinstance(v, list):
        for item in v:
            if not isinstance(item, dict):
                continue
            alias = item.get("alias")
            if not (isinstance(alias, str) and alias.strip()):
                continue
            d = item.get("dir")
            if not (isinstance(d, str) and d):
                d = f"chrome-profiles/{alias}"
            if alias in seen:
                print(f"⚠️ config.json 重复的账号别名 {alias!r}, 丢弃后一个")
                continue
            seen.add(alias)
            out.append({"alias": alias, "dir": d})
    if not out:
        out = [{"alias": "默认", "dir": "chrome-profiles/默认"}]
    return out


def _coerce_block(raw, aliases, n):
    """一个时块: 全合法才返回规整后的 dict, 否则 None(调用方整块丢弃 + 警告)."""
    if not isinstance(raw, dict):
        return None
    bid = raw["id"] if isinstance(raw.get("id"), str) and raw["id"] else f"blk-{n}"
    days = raw.get("days")
    if not (isinstance(days, list) and days and all(
            isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days)):
        return None
    days = sorted(set(days))
    start = normalize_time(raw.get("start"))
    end = normalize_time(raw.get("end"))
    if start is None or end is None:
        return None
    if start == end and start != "00:00":
        return None
    profile = raw.get("profile")
    if not isinstance(profile, str):
        return None
    if raw.get("map") not in _VALID_MAPS:
        return None
    loc = raw.get("location")
    if not _is_int_pair(loc):
        return None
    area = raw.get("farming_area")
    if not (isinstance(area, (list, tuple)) and len(area) == 2
            and all(_is_int_pair(c) for c in area)):
        return None
    dur = raw.get("farming_duration")
    if not (isinstance(dur, int) and not isinstance(dur, bool) and dur > 0):
        return None
    lim = raw.get("consecutive_short_round_limit")
    if not (isinstance(lim, int) and not isinstance(lim, bool) and lim >= 1):
        return None
    eai, asw = raw.get("enemy_ai_enabled"), raw.get("auto_switch_server")
    if not isinstance(eai, bool) or not isinstance(asw, bool):
        return None
    enabled = raw.get("enabled")
    enabled = True if not isinstance(enabled, bool) else enabled
    if profile not in aliases:
        print(f"⚠️ config.json 时块 {bid} 引用的账号 {profile!r} 不存在, 已禁用该时块")
        enabled = False

    # 反转攻击键 / 反转防御键: 缺键或非 bool 一律回落 DEFAULTS, 绝不 return None ——
    # 旧 v2 时块没这两个键, 不能因此被整块丢. enemy_ai_enabled 那种严格 return None
    # 是给一直都有的键用的; 这两个是后加的, 故意宽松.
    def _bool_or(key, dflt):
        v = raw.get(key, dflt)
        return v if isinstance(v, bool) else dflt

    return {
        "id": bid, "enabled": enabled, "days": days, "start": start, "end": end,
        "profile": profile, "map": raw["map"],
        "location": [int(loc[0]), int(loc[1])],
        "farming_area": [[int(area[0][0]), int(area[0][1])],
                         [int(area[1][0]), int(area[1][1])]],
        "farming_duration": dur, "consecutive_short_round_limit": lim,
        "enemy_ai_enabled": eai, "auto_switch_server": asw,
        # 旧时块没这两个键 —— _coerce_swap_obj(raw.get(...)) 收 None 也回落默认对象,
        # 绝不 raw[key] (KeyError 会让整块被丢).
        "enter_game_swap": _coerce_swap_obj(raw.get("enter_game_swap")),
        "reach_area_swap": _coerce_swap_obj(raw.get("reach_area_swap")),
        "invert_attack": _bool_or("invert_attack", DEFAULTS["invert_attack"]),
        "invert_defense": _bool_or("invert_defense", DEFAULTS["invert_defense"]),
    }


def _coerce_schedule(v, aliases):
    if not isinstance(v, list):
        return []
    out = []
    for i, raw_blk in enumerate(v, 1):
        blk = _coerce_block(raw_blk, aliases, i)
        if blk is None:
            print(f"⚠️ config.json 第 {i} 个时块不合法, 整块丢弃")
        else:
            out.append(blk)
    for a in range(len(out)):
        for b in range(a + 1, len(out)):
            if out[a]["enabled"] and out[b]["enabled"] and blocks_overlap(out[a], out[b]):
                print(f"⚠️ config.json 时块 {out[a]['id']} 与 {out[b]['id']} 时间重叠")
    return out


def _coerce_active(v, schedule):
    if isinstance(v, dict):
        got = _coerce_v1(v)
        return {k: got[k] for k in _ACTIVE_KEYS}
    if schedule:
        return {k: copy.deepcopy(schedule[0][k]) for k in _ACTIVE_KEYS}
    return {k: copy.deepcopy(DEFAULTS[k]) for k in _ACTIVE_KEYS}


def _is_valid_swap_obj(v):
    return (
        isinstance(v, dict)
        and isinstance(v.get("enabled"), bool)
        and v.get("mod") in _SWAP_MODS
        and isinstance(v.get("digit"), str) and v.get("digit") in _SWAP_DIGITS
    )


def _validate_shared_fields(raw, prefix):
    """校验 map/location/farming_area/farming_duration/
    consecutive_short_round_limit/enemy_ai_enabled/auto_switch_server 这 7 个
    时块和 active 切片共用的字段, 错误信息前缀用 prefix(比如 "schedule[0]" 或
    "active"). _validate_block 和 _validate_active 都调它, 各自再补自己独有的
    字段(时块还有 days/start/end/profile; 两者对 swap/invert 字段的必填程度
    不同, 也留在各自函数里处理)."""
    errs = []
    if raw.get("map") not in _VALID_MAPS:
        errs.append(f"{prefix}.map 必须是 {_VALID_MAPS} 之一, 实际是 {raw.get('map')!r}")
    if not _is_int_pair(raw.get("location")):
        errs.append(f"{prefix}.location 必须是 [整数, 整数]")
    area = raw.get("farming_area")
    if not (isinstance(area, (list, tuple)) and len(area) == 2
            and all(_is_int_pair(c) for c in area)):
        errs.append(f"{prefix}.farming_area 必须是 [[整数,整数],[整数,整数]]")
    dur = raw.get("farming_duration")
    if not (isinstance(dur, int) and not isinstance(dur, bool) and dur > 0):
        errs.append(f"{prefix}.farming_duration 必须是正整数")
    lim = raw.get("consecutive_short_round_limit")
    if not (isinstance(lim, int) and not isinstance(lim, bool) and lim >= 1):
        errs.append(f"{prefix}.consecutive_short_round_limit 必须是 >=1 的整数")
    if not isinstance(raw.get("enemy_ai_enabled"), bool):
        errs.append(f"{prefix}.enemy_ai_enabled 必须是布尔值")
    if not isinstance(raw.get("auto_switch_server"), bool):
        errs.append(f"{prefix}.auto_switch_server 必须是布尔值")
    return errs


def _validate_block(raw, aliases, i):
    """校验 schedule[i] 一个时块, 返回该块的错误信息列表(带 schedule[i] 前缀).
    跟 _coerce_block 校验同一批规则, 但不做静默回落 —— 每条都要能报给用户看."""
    p = f"schedule[{i}]"
    if not isinstance(raw, dict):
        return [f"{p} 必须是对象"]
    errs = []
    en = raw.get("enabled")
    if en is not None and not isinstance(en, bool):
        errs.append(f"{p}.enabled 必须是布尔值")
    days = raw.get("days")
    if not (isinstance(days, list) and days and all(
            isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days)):
        errs.append(f"{p}.days 必须是非空整数数组, 每个值在 0-6 之间(0=周一)")
    start = normalize_time(raw.get("start"))
    end = normalize_time(raw.get("end"))
    if start is None:
        errs.append(f"{p}.start 不是合法时间: {raw.get('start')!r}")
    if end is None:
        errs.append(f"{p}.end 不是合法时间: {raw.get('end')!r}")
    if start is not None and end is not None and start == end and start != "00:00":
        errs.append(f"{p}: start 和 end 相同({start})但不是 00:00(全天), 这个区间是空的")
    profile = raw.get("profile")
    if not isinstance(profile, str):
        errs.append(f"{p}.profile 必须是字符串")
    elif profile not in aliases:
        errs.append(f"{p}.profile 引用了不存在的账号: {profile!r}")
    errs.extend(_validate_shared_fields(raw, p))
    for key in ("enter_game_swap", "reach_area_swap"):
        v = raw.get(key)
        if v is not None and not _is_valid_swap_obj(v):
            errs.append(f"{p}.{key} 必须是 {{enabled: bool, mod: 'none'/'k'/'l', digit: '0'-'9'}}")
    for key in ("invert_attack", "invert_defense"):
        v = raw.get(key)
        if v is not None and not isinstance(v, bool):
            errs.append(f"{p}.{key} 必须是布尔值")
    return errs


def _validate_active(raw):
    """校验 active 切片: 必须是对象, 且 _ACTIVE_KEYS 里每个键都合法(跟时块同一批
    规则, 但没有 profile/days/start/end 这些调度专属字段)."""
    if not isinstance(raw, dict):
        return ["active 必须是对象"]
    errs = []
    errs.extend(_validate_shared_fields(raw, "active"))
    for key in ("enter_game_swap", "reach_area_swap"):
        if not _is_valid_swap_obj(raw.get(key)):
            errs.append(f"active.{key} 必须是 {{enabled: bool, mod: 'none'/'k'/'l', digit: '0'-'9'}}")
    for key in ("invert_attack", "invert_defense"):
        if not isinstance(raw.get(key), bool):
            errs.append(f"active.{key} 必须是布尔值")
    return errs


def validate_config(raw):
    """报错式校验一份 v2 配置候选(已解析成 dict/list/等 JSON 值). 返回错误信息
    列表(空=合法). 跟 load_config()/_coerce() 那套"坏值静默回落默认"刻意不同:
    这里任何一条不合法都要原样报出来, 不做任何纠正 —— 调用方(webctl.py 的
    POST /api/config)靠这个决定要不要真的把用户写的东西落盘, 不能让用户以为
    保存的是自己写的那份, 实际存的是被偷偷改过的另一份.
    """
    if not isinstance(raw, dict):
        return [f"顶层必须是 JSON 对象, 实际是 {type(raw).__name__}"]

    errs = []
    if raw.get("version") != 2:
        errs.append(f"version 必须是整数 2, 实际是 {raw.get('version')!r}")

    afk = raw.get("afk_enabled", False)
    if not isinstance(afk, bool):
        errs.append(f"afk_enabled 必须是布尔值, 实际是 {afk!r}")

    profiles = raw.get("profiles")
    aliases = set()
    if not isinstance(profiles, list) or not profiles:
        errs.append("profiles 必须是非空数组")
    else:
        for i, p in enumerate(profiles):
            if not isinstance(p, dict):
                errs.append(f"profiles[{i}] 必须是对象")
                continue
            alias = p.get("alias")
            if not (isinstance(alias, str) and alias.strip()):
                errs.append(f"profiles[{i}].alias 必须是非空字符串")
                continue
            if alias in aliases:
                errs.append(f"profiles[{i}].alias 重复的账号别名: {alias!r}")
                continue
            d = p.get("dir")
            if not (isinstance(d, str) and d):
                errs.append(f"profiles[{i}].dir 必须是非空字符串")
                continue
            aliases.add(alias)

    schedule = raw.get("schedule")
    if not isinstance(schedule, list):
        errs.append("schedule 必须是数组")
        schedule = []
    for i, blk in enumerate(schedule):
        errs.extend(_validate_block(blk, aliases, i))

    if "active" in raw:
        errs.extend(_validate_active(raw["active"]))

    return errs


def _coerce(raw):
    """v2 顶层校验. 坏时块整块丢; profile 悬空 → 该块禁用(不丢); profiles 空 → 补默认."""
    if not isinstance(raw, dict):
        print(f"⚠️ config.json 顶层不是对象(是 {type(raw).__name__}), 全部用默认值")
        return copy.deepcopy(DEFAULTS_V2)
    cfg = {"version": 2}
    cfg["afk_enabled"] = raw["afk_enabled"] if isinstance(raw.get("afk_enabled"), bool) else False
    cfg["profiles"] = _coerce_profiles(raw.get("profiles"))
    aliases = {p["alias"] for p in cfg["profiles"]}
    cfg["schedule"] = _coerce_schedule(raw.get("schedule"), aliases)
    cfg["active"] = _coerce_active(raw.get("active"), cfg["schedule"])
    return cfg


def profile_dir(cfg, alias):
    """config 里 alias 对应的 profile 目录(相对路径), 没这个账号返回 None."""
    for p in cfg.get("profiles", []):
        if p["alias"] == alias:
            return p["dir"]
    return None


def abs_profile_path(rel_dir):
    """config 里 profile 的 dir 是相对 exe 同级的 (chrome-profiles/<别名>).
    绝对路径按 sys.argv[0] 定位, 跟 CONFIG_PATH 一套语义."""
    if os.path.isabs(rel_dir):
        return rel_dir
    root = os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.path.join(root, rel_dir)


def _rename_legacy_profile_dir():
    """阶段1 的 chrome-profile/ → 阶段2 的 chrome-profiles/默认/. best-effort:
    目标已存在 / 改名失败(目录被占用)都只 print 一句, 不抛 —— 用户下次用
    『默认』账号时会走登录引导补上。"""
    root = os.path.dirname(os.path.abspath(sys.argv[0]))
    old = os.path.join(root, "chrome-profile")
    new = os.path.join(root, "chrome-profiles", "默认")
    if not os.path.isdir(old) or os.path.exists(new):
        return
    try:
        os.makedirs(os.path.join(root, "chrome-profiles"), exist_ok=True)
        os.rename(old, new)
    except OSError as e:
        print(f"⚠️ 旧 Chrome profile 目录改名失败({e}); 用『默认』账号时会要求重新登录")


def migrate_v1(raw):
    """v1 扁平配置 → v2: 单『默认』profile + 一个全周全天时块 + active 切片。"""
    flat = _coerce_v1(raw if isinstance(raw, dict) else {})
    _rename_legacy_profile_dir()
    block = {
        "id": "blk-1", "enabled": True, "days": [0, 1, 2, 3, 4, 5, 6],
        "start": "00:00", "end": "00:00", "profile": "默认",
    }
    for k in _ACTIVE_KEYS:
        block[k] = copy.deepcopy(flat[k])
    return {
        "version": 2,
        "afk_enabled": flat["afk_enabled"],
        "profiles": [{"alias": "默认", "dir": "chrome-profiles/默认"}],
        "schedule": [block],
        "active": {k: copy.deepcopy(flat[k]) for k in _ACTIVE_KEYS},
    }


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(DEFAULTS_V2)
    except Exception as e:
        print(f"⚠️ 读 config.json 失败, 全部用默认值: {e}")
        return copy.deepcopy(DEFAULTS_V2)
    if not isinstance(raw, dict):
        print(f"⚠️ config.json 顶层不是对象(是 {type(raw).__name__}), 全部用默认值")
        return copy.deepcopy(DEFAULTS_V2)
    if raw.get("version") != 2:
        cfg = _coerce(migrate_v1(raw))
        try:
            _write(cfg)
        except OSError as e:
            print(f"⚠️ 迁移后写回 config.json 失败: {e}")
        return cfg
    return _coerce(raw)


def _write(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def save_config(cfg):
    """写之前先 _coerce, GUI 传进来的东西也不例外 —— 别让界面 bug 写出一份坏配置."""
    _write(_coerce(cfg))


# ─────────────────────────── 调度时间数学(纯函数) ───────────────────────────
# 星期编号 0=周一 … 6=周日. 时间 "HH:MM" 24h. 区间半开 [start, end).

def _hhmm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _valid_time(s):
    return isinstance(s, str) and _TIME_RE.match(s) is not None


# 宽松时间输入 -> 规范 "HH:MM". 校验层(_valid_time / gui_schedule.validate_block)
# 保持严格只判定; 这是独立的变换层, 跑在校验之前. 规则见
# docs/superpowers/specs/2026-09-02-time-input-tolerance-design.md.
_TIME_TRANSLATE = {ord("："): ":", ord("．"): ".", ord("－"): "-", ord("　"): None}
_TIME_TRANSLATE.update({ord("０") + _i: str(_i) for _i in range(10)})


def _ascii_digits(x):
    return x != "" and all("0" <= c <= "9" for c in x)


def normalize_time(s):
    """把宽松写法('9:00' / '09：00' / '930' / '9' / '9.00' / 全角数字)规整成规范
    'HH:MM'. 无法解析 / 越界返回 None(调用方落回原有失败路径: GUI 红字, coerce 丢块)."""
    if not isinstance(s, str):
        return None
    s = s.translate(_TIME_TRANSLATE).strip()
    if not s:
        return None
    s = s.replace(".", ":").replace("-", ":")
    if s.count(":") > 1:
        return None
    if ":" in s:
        h_str, m_str = s.split(":")
        if not (_ascii_digits(h_str) and _ascii_digits(m_str)):
            return None
        if not (1 <= len(h_str) <= 2 and 1 <= len(m_str) <= 2):
            return None
        h, m = int(h_str), int(m_str)
    elif _ascii_digits(s):
        if len(s) <= 2:              # "9" / "18" -> 整点
            h, m = int(s), 0
        elif len(s) == 3:            # "930" -> 9:30
            h, m = int(s[0]), int(s[1:])
        elif len(s) == 4:           # "1830" -> 18:30
            h, m = int(s[:2]), int(s[2:])
        else:
            return None
    else:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return "%02d:%02d" % (h, m)


def expand_block_days(block):
    """把一个时块摊平成 [(weekday, start_min, end_min)]. 半开区间 [start, end).
    00:00–00:00 = 全天; start >= end(且非全天)= 跨午夜, 拆成当天尾段 + 次日头段."""
    s = _hhmm_to_min(block["start"])
    e = _hhmm_to_min(block["end"])
    out = []
    for d in block["days"]:
        if s == 0 and e == 0:
            out.append((d, 0, 1440))
        elif s < e:
            out.append((d, s, e))
        else:
            out.append((d, s, 1440))
            if e > 0:
                out.append(((d + 1) % 7, 0, e))
    return out


def blocks_overlap(a, b):
    for (da, sa, ea) in expand_block_days(a):
        for (db, sb, eb) in expand_block_days(b):
            if da == db and sa < eb and sb < ea:
                return True
    return False


def active_block(schedule, weekday, hhmm):
    m = _hhmm_to_min(hhmm)
    for blk in schedule:
        if not blk.get("enabled"):
            continue
        for (d, s, e) in expand_block_days(blk):
            if d == weekday and s <= m < e:
                return blk
    return None


def next_start(schedule, weekday, hhmm):
    """从此刻(weekday, hhmm)起, 一周内最近的一个时块起点. 返回 (weekday, 'HH:MM')."""
    now = weekday * 1440 + _hhmm_to_min(hhmm)
    best = None
    for blk in schedule:
        if not blk.get("enabled"):
            continue
        for (d, s, _e) in expand_block_days(blk):
            start_abs = d * 1440 + s
            delta = (start_abs - now) % (7 * 1440)
            if delta == 0:
                continue
            if best is None or delta < best[0]:
                best = (delta, d, "%02d:%02d" % divmod(s, 60))
    return None if best is None else (best[1], best[2])
