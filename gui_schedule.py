"""时块调度 UI: 编辑器(CTkToplevel) + 列表折叠行 + 校验纯函数 + Tooltip.

纯函数(_safe_dirname / validate_block / block_to_active)不碰 tk, 单测直接调.
控件类(TimeBlockEditor / ScheduleList)才 import customtkinter —— 放在文件下半段.
"""
import re
import tkinter as tk

import customtkinter as ctk

import app_config
import enemy_recommend
import enemy_species
import gui_theme as theme

WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")

_ACTIVE_KEYS = app_config._ACTIVE_KEYS


# loadout 切换和弦: 每个字段 = 开关 + 修饰键下拉 + 数字下拉.
_SWAP_MOD_LABELS = {"none": "无", "k": "k", "l": "l"}
_SWAP_MOD_FROM_LABEL = {v: k for k, v in _SWAP_MOD_LABELS.items()}
_SWAP_DIGIT_VALUES = list("1234567890")
_coerce_swap_obj = app_config._coerce_swap_obj   # 单一真源
_coerce_enemy_rules = app_config._coerce_enemy_rules   # 单一真源


# 目录名: 保留 \w(含汉字)和连字符, 其余替换成 _, 首尾 _ 去掉.
_SAFE_DIR_RE = re.compile(r"[^\w\-]", re.UNICODE)


def _safe_dirname(name):
    if not isinstance(name, str):
        return ""
    cleaned = _SAFE_DIR_RE.sub("_", name.strip())
    return cleaned.strip("_")


def block_to_active(block):
    """一个时块 -> worker 只读的 active 切片(7 个刷怪参数, 数值规整)."""
    loc = block["location"]
    area = block["farming_area"]
    return {
        "map": block["map"],
        "location": [int(loc[0]), int(loc[1])],
        "farming_area": [[int(area[0][0]), int(area[0][1])],
                         [int(area[1][0]), int(area[1][1])]],
        "farming_duration": int(block["farming_duration"]),
        "consecutive_short_round_limit": int(block["consecutive_short_round_limit"]),
        "enemy_ai_enabled": bool(block["enemy_ai_enabled"]),
        "auto_switch_server": bool(block["auto_switch_server"]),
        "invert_attack": bool(block.get("invert_attack", True)),
        "invert_defense": bool(block.get("invert_defense", False)),
        "enter_game_swap": _coerce_swap_obj(block.get("enter_game_swap")),
        "reach_area_swap": _coerce_swap_obj(block.get("reach_area_swap")),
        "enemy_rules": _coerce_enemy_rules(block.get("enemy_rules"), block.get("map")),
    }


def _positive_int(v):
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def validate_block(block, others):
    """返回错误中文串, 或 None 表示通过. others 里跟 block 同 id 的会被跳过."""
    if not block.get("days"):
        return "至少勾一个星期"
    start, end = block.get("start"), block.get("end")
    if not (app_config._valid_time(start) and app_config._valid_time(end)):
        return "时间格式要是 HH:MM"
    if start == end and start != "00:00":
        return "起止时间不能相同(全天请填 00:00–00:00)"
    if block.get("map") not in app_config._GUI_ENABLED_MAPS:
        return "这张图暂不可用, 请换一张"
    if not block.get("location") and not block.get("farming_area"):
        return "在地图上点个目标点, 或框个刷怪区"
    if not _positive_int(block.get("farming_duration")):
        return "刷怪时长要是正整数"
    if not _positive_int(block.get("consecutive_short_round_limit")):
        return "连续短局阈值要是正整数"
    for o in others:
        if o.get("id") == block.get("id"):
            continue
        if app_config.blocks_overlap(block, o):
            return f"跟时块 {o.get('id')} 时间重叠"
    return None


def _map_radio_state(map_name):
    """时块编辑器地图 radio 的 tk state: 不在 _GUI_ENABLED_MAPS 里的置灰."""
    return "normal" if map_name in app_config._GUI_ENABLED_MAPS else "disabled"


def _anthell_calibration_hint():
    """蚁狱没标定时会被 main._route_blocker 拦停 —— worker 一启动就退出。
    编辑器里提前提个醒, 不弹窗、不挡保存, 真正把关的还是 worker 自己。

    操作步骤直接用 map_routes 里那一份(跟 _route_blocker 的报错同源): 以前这里自己写了
    "跑 capture_map.py garden 生成 maps/garden.png", 而那张图现在是仓库自带的, 那条命令
    会把它覆盖掉。"""
    import map_routes
    return ("蚁狱暂缺标定: maps/garden.png(仓库自带, 丢了用 "
            "`git checkout -- maps/garden.png` 恢复)、传送点坐标 map_routes.ANTHELL_PORTAL "
            "—— 缺了 worker 启动即退出。" + map_routes.PORTAL_RECALIBRATION_RECIPE)


def _anthell_blocker():
    """蚁狱现在能不能跑: 能跑返回 None, 否则返回 _anthell_calibration_hint().
    以前不管选没选蚁狱、标没标定都常驻显示那条警告 —— 标定早已完成, 那句话只会吓人."""
    import os
    import map_routes
    import gui_map_picker
    route = map_routes.route_for("anthell")
    if not route.is_calibrated():
        return _anthell_calibration_hint()
    for stage in route.stages:
        if not os.path.exists(os.path.join(gui_map_picker._MAP_DIR, f"{stage.map_name}.png")):
            return _anthell_calibration_hint()
    return None


def new_block_template(cfg):
    """新建时块的默认值. 索敌 / 换服默认开(canvas 解码识怪, 不需要模型文件)."""
    return {
        "id": fresh_block_id(cfg), "enabled": True, "days": [],
        "start": "09:00", "end": "12:00",
        "profile": cfg["profiles"][0]["alias"] if cfg.get("profiles") else "默认",
        "map": "desert", "location": None, "farming_area": None,
        "farming_duration": 300, "consecutive_short_round_limit": 2,
        "enemy_ai_enabled": True, "auto_switch_server": True,
        "invert_attack": True, "invert_defense": False,
        "enter_game_swap": {"enabled": False, "mod": "none", "digit": "1"},
        "reach_area_swap": {"enabled": False, "mod": "none", "digit": "1"},
    }


# ── 索敌设置子窗的纯函数(不碰 tk, 单测直接调) ──────────────────────────────────────
# 物种 × 稀有度矩阵: 每个物种一行, 第一列「全部」管所有稀有度, 后面每个稀有度一格; 点一下往后切一格。
RULE_CYCLE = ("", "fight", "cautious", "avoid", "ignore")        # "" = 默认(内置打法 / 稀有度起点)
_RULE_LABELS = {"": "默认", "fight": "打", "cautious": "谨慎", "avoid": "躲", "ignore": "忽略"}
MATRIX_COLS = ("*",) + tuple(enemy_species.RARITY_ORDER)
_COL_HEADERS = {"*": "全部", "Common": "Com", "Unusual": "Unc", "Rare": "Rare", "Epic": "Epic",
                "Legendary": "Leg", "Mythic": "Myth", "Ultra": "Ultra", "Super": "Super",
                "Eternal": "Etrn", "Unique": "Uniq"}
_CELL_TEXT = {"": "·", "fight": "打", "cautious": "谨", "avoid": "躲", "ignore": "忽"}
_CELL_LOOK = {"": (theme.SURFACE_HI, theme.FAINT), "fight": (theme.SUCCESS, "#ffffff"),
              "cautious": (theme.WARN, "#1b1e24"), "avoid": (theme.DANGER, "#ffffff"),
              "ignore": (theme.FAINT, theme.TEXT)}
_CELL_MIN = 40                   # 一格最小宽度(像素): 11 格 + 名字列要放进卡片, 太宽最右一列会被截掉
_SWARM_LABELS = {"": "默认", "on": "开", "off": "关"}
_SWARM_FROM_LABEL = {v: k for k, v in _SWARM_LABELS.items()}
# 躲避起点稀有度下拉: "" = 不写键(内置 Ultra); 其余键 = app_config 认的档名。显示名用英文档名本身 ——
# 仓库里高档稀有度的中文译名并不统一(farm_baseline / analyze_recording 的表就对不上), 不在这里再猜一份。
_TIER_LABELS = {
    "": f"默认 ({enemy_species.AVOID_MIN_RARITY_DEFAULT})",
    **{t: t for t in enemy_species.AVOID_MIN_RARITY_CHOICES
       if t != enemy_species.AVOID_MIN_RARITY_NEVER},
    enemy_species.AVOID_MIN_RARITY_NEVER: "从不",
}
_TIER_FROM_LABEL = {v: k for k, v in _TIER_LABELS.items()}
# 五个整数旋钮: (键, 子窗里显示的名字)。顺序 = 子窗里的顺序。
_KNOB_LABELS = (
    ("hold_active_px", "主动怪停步"),
    ("hold_passive_px", "被动/不动怪停步"),
    ("chase_max_px", "追击上限"),
    ("far_chase_px", "远距追击上限"),
    ("avoid_px", "躲避半径"),
    ("cautious_px", "谨慎保持距离"),
)
# 「按装备推荐」: 下拉的显示名。"请选择" 是初始占位, 不是合法值(recommendation_for 会拒掉)。
_STYLE_LABELS = {"melee": "近战", "ranged": "远程", "summon": "召唤"}
_STYLE_FROM_LABEL = {v: k for k, v in _STYLE_LABELS.items()}
_REC_PICK = "请选择"
_REC_KNOB_NAMES = {"avoid_min_rarity": "躲避起点稀有度",
                   **{k: label for k, label in _KNOB_LABELS if k in enemy_recommend.GOVERNED_KNOBS}}
_KIND_LABELS = {"active": "主动", "passive": "被动", "static": "不动"}
_DIGITS_RE = re.compile(r"[0-9]+")


def empty_rules():
    return {"species": {}, "knobs": {}}


def rules_summary(rules):
    """时块编辑器里「索敌设置…」按钮旁边那句话。"""
    r = rules or {}
    n, m = len(r.get("species") or {}), len(r.get("knobs") or {})
    parts = ([f"{n} 个物种"] if n else []) + ([f"{m} 项数值"] if m else [])
    return "自定义 " + ", ".join(parts) if parts else "全部默认"


def next_rule(cur, step=1):
    """矩阵格子点一下切到的下一个状态(step=-1 往回切); 不认识的状态当默认。"""
    i = RULE_CYCLE.index(cur) if cur in RULE_CYCLE else 0
    return RULE_CYCLE[(i + step) % len(RULE_CYCLE)]


def species_cells(rule):
    """一个物种的规则(字符串 / {稀有度: 打法} / None) -> {列: 状态}, 只含有打法的列。字符串 = 「全部」列。"""
    if isinstance(rule, str):
        return {"*": rule} if rule else {}
    if isinstance(rule, dict):
        return {k: v for k, v in rule.items() if v}
    return {}


def species_rule_from_cells(cells):
    """{列: 状态} -> 这个物种存进配置的规则: 全空 -> None(不写); 只有「全部」-> 字符串(老写法);
    否则 {稀有度: 打法}, 键按列顺序(「全部」、再稀有度从低到高), 空格子不写。"""
    kept = {c: cells[c] for c in MATRIX_COLS if cells.get(c)}
    if not kept:
        return None
    if set(kept) == {"*"}:
        return kept["*"]
    return kept


def rules_for_map(rules, map_name):
    """换图时: 丢掉不属于新图的物种覆盖, 旋钮原样保留。返回新 dict(入参可为 None), 逐稀有度的字典也是拷贝。
    读纯数据模块 enemy_species(不是 enemy_detect, 后者会拉进 cv2 / cdp_bridge)。"""
    known = enemy_species.MAP_SPECIES.get(map_name, frozenset())
    r = rules or {}
    return {"species": {s: (dict(v) if isinstance(v, dict) else v)
                        for s, v in (r.get("species") or {}).items() if s in known},
            "knobs": dict(r.get("knobs") or {})}


def species_rows(map_name):
    """子窗的物种行: [(slug, 中文名, 英文名, 内置打法标签)], 按 slug 排序; 地图不认识 -> []。"""
    import enemy_detect
    rows = []
    for slug in sorted(enemy_detect.MAP_SPECIES.get(map_name, ())):
        names = enemy_detect.SPECIES_NAMES[slug]
        t = enemy_detect.SPECIES_TRAITS.get(slug, {})
        kind = ("绕圈" if t.get("tactic") == "strafe"
                else _KIND_LABELS[t.get("kind", "active")])
        rows.append((slug, names["zh"], names["en"], kind))
    return rows


def knob_defaults(map_name):
    """子窗输入框的 placeholder: 本图的内置值(用户留空 = 用这个)。"""
    import enemy_detect
    return {
        "hold_active_px": enemy_detect.ENGAGE_HOLD_PX,
        "hold_passive_px": enemy_detect.PASSIVE_HOLD_PX,
        "chase_max_px": enemy_detect.CHASE_MAX_PX,
        "far_chase_px": enemy_detect.FAR_CHASE_PX,
        "avoid_px": enemy_detect.avoid_trigger_px_for(
            map_name, enemy_detect.DEFAULT_AVOID_TRIGGER_PX),
        "cautious_px": enemy_detect.CAUTIOUS_HOLD_PX,
    }


def knob_applies(map_name):
    """{旋钮键: 本图上这个旋钮真会生效吗}, 键 = 六个整数旋钮 + swarm + avoid_min_rarity。

    躲避半径(avoid_px)和躲避起点稀有度(avoid_min_rarity, 经 classify_action 决定谁算 AVOID)都喂给
    逃跑判定, 两种选目标策略都走, 恒为 True; 谨慎保持距离(cautious_px)也是两种策略的分支都读(沙漠的
    Ultra 沙尘暴 / 仙人掌就是谨慎怪), 同样恒为 True。其余(两个停步半径、追击上限、
    远距追击上限、蚁群开关)只在 select_action 的 target_policy == "nearest" 分支里被读 ——
    按稀有度追击("priority", 沙漠)的图上改了也没用, 子窗据此把它们置灰。
    """
    import enemy_detect
    nearest = enemy_detect.target_policy_for(map_name) == "nearest"
    out = {key: nearest for key, _label in _KNOB_LABELS}
    out["avoid_px"] = True
    out["cautious_px"] = True
    out["swarm"] = nearest
    out["avoid_min_rarity"] = True
    return out


def recommendation_for(style, petal, map_name):
    """(流派键, 花瓣稀有度, 地图) -> ({旋钮键: 值或 None}, None) 或 (None, 错误串)。

    None = 清回默认(近战不覆盖停步半径: 先套远程再套近战, 不该留着远程的数)。只含**本图上真会生效**的旋钮
    (knob_applies): 沙漠按稀有度追击, 停步半径在那儿不读, 往里填一个没用的数只会骗人。"""
    if style not in enemy_recommend.STYLES or petal not in enemy_recommend.PETAL_RARITIES:
        return None, "先选流派和花瓣稀有度"
    rec = enemy_recommend.recommend(style, petal)
    applies = knob_applies(map_name)
    return {k: rec.get(k) for k in enemy_recommend.GOVERNED_KNOBS if applies[k]}, None


def recommendation_note(values):
    """套用推荐之后, 子窗里那行提示: 填了什么、没填什么、有什么风险。values 是 recommendation_for 的结果。"""
    parts = [f"{_REC_KNOB_NAMES[k]} {'默认' if values[k] is None else values[k]}"
             for k in enemy_recommend.GOVERNED_KNOBS if k in values]
    text = "已填: " + ", ".join(parts) + "。点「确定」才保存。"
    if any(values.get(k) is not None for k in ("hold_active_px", "hold_passive_px")):
        text += " 停步半径是占位值, 没有实机数据。"
    if len(values) < len(enemy_recommend.GOVERNED_KNOBS):
        text += " 本图停步半径不生效, 没填。"
    tier = values.get("avoid_min_rarity")
    order = enemy_species.RARITY_ORDER
    if tier in order and order.index(tier) > order.index("Ultra"):
        text += " ⚠ 起点高于 Ultra: Ultra 当普通怪打, 连内置要躲的 Ultra 蝎子 / 甲虫也一样。"
    return text


def rules_from_inputs(species_choices, knob_texts, swarm_choice, avoid_min_rarity=""):
    """子窗控件内容 -> (规则, None) 或 (None, 错误串)。
    species_choices: {slug: 打法字符串("" 默认不写 / fight / cautious / avoid / ignore, 对所有稀有度)
                      或 {列: 打法}(列 = "*" 或稀有度档名, 即矩阵一行的各格子)}
    knob_texts: {旋钮键: 输入框文本}(空串 = 默认, 不写)
    swarm_choice: "" / "on" / "off"
    avoid_min_rarity: ""(默认, 不写) / app_config 认的档名(含 "never")
    """
    rules = empty_rules()
    for slug, choice in species_choices.items():
        cells = {"*": choice} if isinstance(choice, str) else choice
        if not isinstance(cells, dict) or any(
                col not in MATRIX_COLS or not isinstance(a, str) or a not in RULE_CYCLE
                for col, a in cells.items()):
            return None, f"物种 {slug} 的打法不合法"
        rule = species_rule_from_cells(cells)
        if rule is not None:
            rules["species"][slug] = rule
    for key, label in _KNOB_LABELS:
        text = (knob_texts.get(key) or "").strip()
        if not text:
            continue
        lo, hi = app_config.ENEMY_KNOB_RANGES[key]
        if not _DIGITS_RE.fullmatch(text):
            return None, f"{label}要填整数(留空 = 默认)"
        if len(text) > 6:
            # 合法最大值(3000)只有 4 位。过长的全数字串先在这里挡掉: Python 3.11 的 int() 对 > 4300 位
            # 的串直接抛 ValueError, 不能让它冒到界面上变成崩溃而不是红字。
            return None, f"{label}要在 {lo}–{hi} 之间"
        n = int(text)
        if not (lo <= n <= hi):
            return None, f"{label}要在 {lo}–{hi} 之间"
        rules["knobs"][key] = n
    if swarm_choice == "on":
        rules["knobs"]["swarm"] = True
    elif swarm_choice == "off":
        rules["knobs"]["swarm"] = False
    if avoid_min_rarity != "":
        if avoid_min_rarity not in enemy_species.AVOID_MIN_RARITY_CHOICES:
            return None, "躲避起点稀有度不合法"
        rules["knobs"]["avoid_min_rarity"] = avoid_min_rarity
    return rules, None


def fresh_block_id(cfg):
    n = 0
    for b in cfg.get("schedule", []):
        m = re.match(r"blk-(\d+)$", str(b.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return f"blk-{n + 1}"


def weekday_short(days):
    return "".join(WEEKDAY_LABELS[d] for d in sorted(days)) or "—"


def weekday_text(days):
    """列表里给人看的星期: 每天 / 工作日 / 周末 / 周一 三 五."""
    ds = sorted(set(days))
    if ds == list(range(7)):
        return "每天"
    if ds == list(range(5)):
        return "工作日"
    if ds == [5, 6]:
        return "周末"
    if not ds:
        return "未选星期"
    return "周" + " ".join(WEEKDAY_LABELS[d] for d in ds)


# ─────────────────────────────── 控件层 ───────────────────────────────

class _Tooltip:
    """悬停解释. CustomTkinter 没内置, 纯 tkinter 手搓: <Enter> 后 400ms 弹一个
    无边框 Toplevel, <Leave> / 点击销毁."""

    def __init__(self, widget, text):
        self._w = widget
        self._text = text
        self._tip = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._job = self._w.after(400, self._show)

    def _show(self):
        if self._tip is not None:
            return
        x = self._w.winfo_rootx() + 12
        y = self._w.winfo_rooty() + self._w.winfo_height() + 6
        self._tip = tk.Toplevel(self._w)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self._text, justify="left", wraplength=300,
                 bg=theme.SURFACE_HI, fg=theme.TEXT, relief="solid", borderwidth=1,
                 font=(theme.UI_FAMILY, 10), padx=10, pady=6).pack()

    def _hide(self, _e=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _cancel(self):
        if self._job is not None:
            self._w.after_cancel(self._job)
            self._job = None


def _help_mark(master, text):
    """一个灰色的 ⓘ, 悬停出解释."""
    q = ctk.CTkLabel(master, text="ⓘ", text_color=theme.MUTED, font=theme.font(13),
                     cursor="question_arrow")
    _Tooltip(q, text)
    return q


def _fmt_pt(pt):
    return f"({int(pt[0])}, {int(pt[1])})"


class EnemyRulesDialog(ctk.CTkToplevel):
    """索敌设置子窗: 物种「默认/打/躲/忽略」+ 数值旋钮。「确定」把结果交给 on_ok(rules), 不落盘 ——
    由时块编辑器的「保存」统一入库; 「取消」/ Esc 丢弃。非模态, 跟编辑器同风格。"""

    _TIPS = {
        "hold_active_px": "主动怪(会自己凑过来的)进到这个屏幕像素半径内就原地停下打。单位是屏幕像素, "
                          "随游戏 zoom 变。不影响蚁后 / 萤火虫 / 桶 / 绕圈怪这类自带半径的物种。",
        "hold_passive_px": "被动 / 中立 / 不动的怪不会自己过来, 要贴到这个半径内才停下打。同上, 不影响自带半径的物种。",
        "chase_max_px": "只追这个半径内的怪, 再远的交回漫游。",
        "far_chase_px": "超出追击上限、但在刷怪区里且小地图上直线走得到的怪, 追到这个半径。",
        "cautious_px": "谨慎怪(你设成「谨慎」的, 以及内置的 Ultra 沙尘暴 / 仙人掌等)追到这个距离就停; "
                       "比这个距离的 85% 还近就背离它走, 保持住。默认 500 屏幕像素, 随游戏 zoom 变。"
                       "远程 / 召唤想离得近一点打, 就调小。",
        "avoid_px": "躲避类怪(含你设成「躲」的)进到这个半径就跑。蚁穴内置 200, 其余图 400。只改触发半径, "
                    "不改躲开后继续躲的那 450px 滞回, 也不改蚁穴究极冲过来时 400px 的提前躲。",
    }
    _TIP_SWARM = "蚁群 = 一堆挤在一起的怪, 默认先打蚁群并保持距离。关掉就把它们当单只处理。"
    _HINT_RECOMMEND = ("躲避起点 = 花瓣稀有度高一档。近战用内置停步半径; 远程 / 召唤的停步半径是占位值, "
                       "没有实机数据, 套用后请按手感改。只填下面的控件, 不会自动保存。")
    _TIP_TIER = ("没设规则的怪, 稀有度达到这一档才躲, 默认 Ultra。调低 = 更谨慎(比如 Mythic: 神话怪也会触发躲避); "
                 "调高 = 更莽(比如 Super: Ultra 也当普通怪打); 「从不」= 不按稀有度躲。"
                 "你给物种设的「打 / 躲」优先于这个。")
    # 物种卡片顶上那句说明。三层意思都要留(spec §2.1 / §5, test_main_worker 里有守护用例): 打 / 躲不看稀有度;
    # 沙漠按稀有度追击, 「打」不等于一直追; 把平时要躲的 Ultra+ 设成「打」风险自担, 「忽略」的怪贴脸也不躲。
    _HINT_SPECIES = ("每个物种一行: 「全部」那格管所有稀有度, 右边每个稀有度的格子单独管那一档, 单独的格子优先于"
                     "「全部」; 全留空 = 内置打法。点格子往后切: · 默认 → 打 → 谨慎(打, 但保持距离, 太近就后退) → 躲 → 忽略, "
                     "右键往回切。打 / 躲不看稀有度起点。沙漠按稀有度追击, 「打」不会让低稀有度怪被专门追; "
                     "但「打」会去打平时要躲的怪(比如 Ultra 稀有度), 后果自负。「忽略」的怪就算贴脸也不躲。")
    _WRAP = 760                      # 卡片里说明文字的换行宽度(子窗比以前宽, 要放得下矩阵)
    _HINT_PRIORITY_MAP = ("本图按稀有度追击(只专门追 Mythic 及以上), 灰掉的几项只对「最近优先」的图生效, "
                          "这里只有「躲避半径」「谨慎保持距离」和「躲避起点稀有度」管用。已存的值不会被清掉, 换到最近优先的图又会生效。")

    def __init__(self, master, *, map_name, rules, on_ok):
        super().__init__(master, fg_color=theme.BG)
        self.title("索敌设置")
        theme.center_on(self, master, 840, 700)
        self.minsize(780, 400)
        self.resizable(True, True)
        self.transient(master)
        self._map = map_name
        self._on_ok = on_ok
        self._col_headers = {}           # 列 -> 表头 tk.Label(跟格子在同一个 grid 里, 同一列)
        self._cell_widgets = {}          # slug -> {列: tk.Label}
        self._cell_state = {}            # slug -> {列: 状态}
        self._knob_entries = {}
        self._build(rules_for_map(rules, map_name))
        self.bind("<Escape>", lambda _e: self.destroy())

    def _build(self, rules):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", pady=(12, 0))

        rec_card = theme.card(body)
        rec_card.pack(fill="x", padx=14, pady=(0, 12))
        theme.section_title(
            rec_card, "按装备推荐", "选流派和大部分花瓣的稀有度, 一键填好推荐值"
        ).pack(anchor="w", fill="x", padx=14, pady=(12, 2))
        self._rec_hint = theme.hint(rec_card, text=self._HINT_RECOMMEND, wraplength=self._WRAP)
        self._rec_hint.pack(anchor="w", fill="x", padx=14, pady=(0, 8))
        rec_row = ctk.CTkFrame(rec_card, fg_color="transparent")
        rec_row.pack(fill="x", padx=14, pady=(0, 12))
        menu_kw = dict(font=theme.font(12), dropdown_font=theme.font(12), fg_color=theme.SURFACE_HI,
                       button_color=theme.BORDER, button_hover_color=theme.FAINT, height=28)
        ctk.CTkLabel(rec_row, text="流派", font=theme.font(13),
                     text_color=theme.MUTED).pack(side="left")
        self._rec_style = ctk.CTkOptionMenu(
            rec_row, width=90, values=[_REC_PICK] + list(_STYLE_LABELS.values()), **menu_kw)
        self._rec_style.set(_REC_PICK)
        self._rec_style.pack(side="left", padx=(6, 14))
        ctk.CTkLabel(rec_row, text="大部分花瓣", font=theme.font(13),
                     text_color=theme.MUTED).pack(side="left")
        self._rec_petal = ctk.CTkOptionMenu(
            rec_row, width=110, values=[_REC_PICK] + list(enemy_recommend.PETAL_RARITIES), **menu_kw)
        self._rec_petal.set(_REC_PICK)
        self._rec_petal.pack(side="left", padx=(6, 14))
        theme.ghost_button(rec_row, "套用推荐", self._apply_recommendation,
                           width=84, height=28).pack(side="left")

        card = theme.card(body)
        card.pack(fill="x", padx=14, pady=(0, 12))
        # 说明分成两段: section_title 的副标题是不换行的单行标签, 一整句塞进去会被卡片右缘截掉。
        theme.section_title(
            card, "物种", "默认 = 内置打法; 忽略 = 当它不存在(不躲、不追)"
        ).pack(anchor="w", fill="x", padx=14, pady=(12, 2))
        self._species_hint = theme.hint(card, text=self._HINT_SPECIES, wraplength=self._WRAP)
        self._species_hint.pack(anchor="w", fill="x", padx=14, pady=(0, 8))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=(0, 12))
        rows = species_rows(self._map)
        if not rows:
            theme.hint(inner, text="本图暂不支持索敌").pack(anchor="w")
        else:
            legend = ctk.CTkFrame(inner, fg_color="transparent")
            legend.pack(fill="x", pady=(0, 6))
            for state in RULE_CYCLE:
                bg, fg = _CELL_LOOK[state]
                tk.Label(legend, text=f" {_CELL_TEXT[state]} {_RULE_LABELS[state]} ", bg=bg, fg=fg,
                         font=(theme.UI_FAMILY, 10)).pack(side="left", padx=(0, 6))
        # 表头和各行的格子放在同一个 grid 里: 同一列天然对齐(分开 pack 的话, 字号不同宽度就不同, 表头会越排越歪)
        grid = ctk.CTkFrame(inner, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure(0, minsize=210)
        grid.grid_columnconfigure(1, minsize=40)
        for ci in range(len(MATRIX_COLS)):
            grid.grid_columnconfigure(2 + ci, uniform="cell", minsize=_CELL_MIN)
        if rows:
            for ci, col in enumerate(MATRIX_COLS):
                lbl = tk.Label(grid, text=_COL_HEADERS[col], bg=theme.SURFACE, fg=theme.MUTED,
                               font=(theme.UI_FAMILY, 9))
                lbl.grid(row=0, column=2 + ci, padx=(1, 9) if col == "*" else 1, sticky="ew")
                self._col_headers[col] = lbl
                _Tooltip(lbl, "所有稀有度(没单独配的稀有度都按这一格)" if col == "*" else col)
        for r, (slug, zh, en, kind) in enumerate(rows, start=1):
            # 名字列要容得下最长的「白蚁主宰者  Termite Overmind」(macOS 实测 175px, Windows 字体更宽)
            ctk.CTkLabel(grid, text=f"{zh}  {en}", anchor="w", font=theme.font(13),
                         text_color=theme.TEXT).grid(row=r, column=0, sticky="w", pady=(0, 3))
            ctk.CTkLabel(grid, text=kind, anchor="w", font=theme.font(11),
                         text_color=theme.FAINT).grid(row=r, column=1, sticky="w", pady=(0, 3))
            saved = species_cells(rules["species"].get(slug))
            self._cell_widgets[slug], self._cell_state[slug] = {}, {}
            for ci, col in enumerate(MATRIX_COLS):
                w = tk.Label(grid, font=(theme.UI_FAMILY, 10), cursor="hand2", pady=2)
                w.grid(row=r, column=2 + ci, padx=(1, 9) if col == "*" else 1, pady=(0, 3), sticky="ew")
                w.bind("<Button-1>", lambda _e, s_=slug, c=col: self._cycle_cell(s_, c))
                for back in ("<Button-2>", "<Button-3>"):         # 右键(macOS 的右键是 Button-2): 往回切
                    w.bind(back, lambda _e, s_=slug, c=col: self._cycle_cell(s_, c, -1))
                self._cell_widgets[slug][col] = w
                self._set_cell(slug, col, saved.get(col, ""))

        card2 = theme.card(body)
        card2.pack(fill="x", padx=14, pady=(0, 12))
        applies = knob_applies(self._map)
        inert = any(not v for v in applies.values())
        theme.section_title(
            card2, "数值", "留空 = 内置值(输入框里的灰字)。单位是屏幕像素, 随游戏 zoom 变"
        ).pack(anchor="w", fill="x", padx=14, pady=(12, 2 if inert else 8))
        self._knob_hint = None
        if inert:
            self._knob_hint = theme.hint(card2, text=self._HINT_PRIORITY_MAP, wraplength=self._WRAP)
            self._knob_hint.pack(anchor="w", fill="x", padx=14, pady=(0, 8))
        inner2 = ctk.CTkFrame(card2, fg_color="transparent")
        inner2.pack(fill="x", padx=14, pady=(0, 12))
        defaults = knob_defaults(self._map)
        for key, label in _KNOB_LABELS:
            row = ctk.CTkFrame(inner2, fg_color="transparent")
            row.pack(fill="x", pady=(0, 6))
            ctk.CTkLabel(row, text=label, width=130, anchor="w", font=theme.font(13),
                         text_color=theme.MUTED).pack(side="left")
            e = ctk.CTkEntry(row, width=84, justify="center", font=theme.font(13),
                             placeholder_text=str(defaults[key]))
            if key in rules["knobs"]:
                e.insert(0, str(rules["knobs"][key]))
            if not applies[key]:
                # 先填值再置灰(置灰的输入框插不进去)。值不清掉: _ok 照样 e.get() 收走, 已存的规则
                # 在沙漠上点「确定」不会丢。
                theme.set_entry_enabled(e, False)
            e.pack(side="left")
            ctk.CTkLabel(row, text=" px ", text_color=theme.MUTED,
                         font=theme.font(12)).pack(side="left")
            _help_mark(row, self._TIPS[key]).pack(side="left")
            self._knob_entries[key] = e
        row = ctk.CTkFrame(inner2, fg_color="transparent")
        row.pack(fill="x", pady=(0, 0))
        ctk.CTkLabel(row, text="蚁群行为", width=130, anchor="w", font=theme.font(13),
                     text_color=theme.MUTED).pack(side="left")
        self._swarm = ctk.CTkSegmentedButton(row, values=list(_SWARM_LABELS.values()),
                                             font=theme.font(12), height=26)
        cur = rules["knobs"].get("swarm")
        self._swarm.set(_SWARM_LABELS["" if cur is None else ("on" if cur else "off")])
        if not applies["swarm"]:
            self._swarm.configure(state="disabled")
        self._swarm.pack(side="left")
        _help_mark(row, self._TIP_SWARM).pack(side="left", padx=6)
        row = ctk.CTkFrame(inner2, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(row, text="躲避起点稀有度", width=130, anchor="w", font=theme.font(13),
                     text_color=theme.MUTED).pack(side="left")
        self._tier = ctk.CTkOptionMenu(row, width=130, values=list(_TIER_LABELS.values()),
                                       font=theme.font(12), dropdown_font=theme.font(12),
                                       fg_color=theme.SURFACE_HI, button_color=theme.BORDER,
                                       button_hover_color=theme.FAINT, height=28)
        self._tier.set(_TIER_LABELS[rules["knobs"].get("avoid_min_rarity", "")])
        self._tier.pack(side="left")
        _help_mark(row, self._TIP_TIER).pack(side="left", padx=6)

        br = ctk.CTkFrame(self, fg_color=theme.SIDEBAR, corner_radius=0)
        br.grid(row=1, column=0, sticky="ew")
        br.grid_columnconfigure(0, weight=1)
        self._err = ctk.CTkLabel(br, text="", text_color="#ff6b6f", font=theme.font(13),
                                 anchor="w", justify="left", wraplength=260)
        self._err.grid(row=0, column=0, sticky="w", padx=16)
        theme.ghost_button(br, "全部恢复默认", self._reset, width=104, height=34).grid(
            row=0, column=1, padx=(0, 8), pady=12)
        theme.ghost_button(br, "取消", self.destroy, width=72, height=34).grid(
            row=0, column=2, padx=(0, 8), pady=12)
        theme.primary_button(br, "确定", self._ok, width=84, height=34).grid(
            row=0, column=3, padx=(0, 16), pady=12)

    # ---- 物种 × 稀有度矩阵 ----
    def _cell_get(self, slug, col):
        return self._cell_state[slug][col]

    def _set_cell(self, slug, col, state):
        bg, fg = _CELL_LOOK[state]
        self._cell_widgets[slug][col].configure(text=_CELL_TEXT[state], bg=bg, fg=fg)
        self._cell_state[slug][col] = state

    def _cycle_cell(self, slug, col, step=1):
        self._set_cell(slug, col, next_rule(self._cell_get(slug, col), step))

    def _apply_recommendation(self):
        """把推荐值填进下面的控件(只动推荐管的那几项, 物种选择和别的数值不碰), 不落盘 —— 「确定」才保存。
        本图不读的旋钮(沙漠的停步半径)recommendation_for 就没给, 灰框里已存的值原样留着。"""
        values, err = recommendation_for(
            _STYLE_FROM_LABEL.get(self._rec_style.get()), self._rec_petal.get(), self._map)
        if err:
            self._err.configure(text="⚠ " + err)
            self.bell()
            return
        self._err.configure(text="")
        for key, val in values.items():
            if key == "avoid_min_rarity":
                self._tier.set(_TIER_LABELS[val])
            else:
                e = self._knob_entries[key]
                e.delete(0, "end")
                if val is not None:
                    e.insert(0, str(val))
        self._rec_hint.configure(text=recommendation_note(values))

    def _reset(self):
        self._rec_style.set(_REC_PICK)
        self._rec_petal.set(_REC_PICK)
        self._rec_hint.configure(text=self._HINT_RECOMMEND)
        for slug, cols in self._cell_widgets.items():
            for col in cols:
                self._set_cell(slug, col, "")
        for e in self._knob_entries.values():
            # 置灰的输入框 delete 是空操作: 「全部恢复默认」要连灰的也清, 先放开再清再灰回去。
            locked = e.cget("state") == "disabled"
            if locked:
                theme.set_entry_enabled(e, True)
            e.delete(0, "end")
            if locked:
                theme.set_entry_enabled(e, False)
        self._swarm.set(_SWARM_LABELS[""])
        self._tier.set(_TIER_LABELS[""])
        self._err.configure(text="")

    def _ok(self):
        rules, err = rules_from_inputs(
            {slug: dict(cols) for slug, cols in self._cell_state.items()},
            {key: e.get() for key, e in self._knob_entries.items()},
            _SWARM_FROM_LABEL[self._swarm.get()],
            _TIER_FROM_LABEL[self._tier.get()])
        if err:
            self._err.configure(text="⚠ " + err)
            self.bell()
            return
        self._on_ok(rules)
        self.destroy()


class TimeBlockEditor(ctk.CTkToplevel):
    """一个时块的编辑窗. 非模态: 不 grab_set / 不 -topmost, 用户能最小化去干别的.
    保存前跑 validate_block, 失败红字不关窗. on_save(block_dict) 由调用方接。
    new_profile_cb(alias, on_ready) 由 App 注入 —— 处理"账号下拉里选＋新建"。"""

    _TIP_DURATION = ("一轮在刷怪区停留多少秒。也是『刷满』判定线 —— "
                     "一条命活过这个秒数才算这轮刷满。")
    _TIP_SHORT = ("连续这么多轮没撑到刷怪时长(被秒 / 到不了区), "
                  "且『自动换服务器』开着, 就自动跳服。")
    _NEW_PROFILE = "＋ 新建账号…"
    _LABEL_W = 72   # 表单左侧标签列宽

    def __init__(self, master, *, block, others, profiles, on_save, is_new=False):
        super().__init__(master, fg_color=theme.BG)
        self.title("新建时块" if is_new else f"编辑时块 · {block.get('id', '')}")
        theme.center_on(self, master, 640, 780)
        self.minsize(520, 360)
        self.resizable(True, True)
        self.transient(master)
        self._block = dict(block)
        self._others = others
        self._profiles = list(profiles)
        self._on_save = on_save
        self.new_profile_cb = None
        self._point = tuple(block["location"]) if block.get("location") else None
        self._area = ([tuple(block["farming_area"][0]), tuple(block["farming_area"][1])]
                      if block.get("farming_area") else None)
        self._rules = rules_for_map(block.get("enemy_rules"), block.get("map", "desert"))
        self._rules_dlg = None
        self._build()
        self.bind("<Escape>", lambda _e: self.destroy())

    # ---- 布局小工具 ----
    def _section(self, title, sub=None):
        c = theme.card(self._body)
        c.pack(fill="x", padx=14, pady=(0, 12))
        theme.section_title(c, title, sub).pack(anchor="w", fill="x", padx=14, pady=(12, 8))
        inner = ctk.CTkFrame(c, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=(0, 12))
        return inner

    def _form_row(self, parent, label, pady=(0, 8)):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=pady)
        ctk.CTkLabel(row, text=label, width=self._LABEL_W, anchor="w",
                     font=theme.font(13), text_color=theme.MUTED).pack(side="left")
        return row

    def _build(self):
        # 字段多 —— 字段区放进滚动框, 按钮条 + 报错 pin 在窗口底部永远可见
        # (报错以前在滚动区最底下, 字段一多用户点"保存"没反应也看不到原因).
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", pady=(12, 0))
        self._body = body

        self._build_time()
        self._build_account_map()
        self._build_location()
        self._build_combat()
        self._build_server_and_loadout()

        # ---- 底栏 ----
        br = ctk.CTkFrame(self, fg_color=theme.SIDEBAR, corner_radius=0)
        br.grid(row=1, column=0, sticky="ew")
        br.grid_columnconfigure(0, weight=1)
        self._err = ctk.CTkLabel(br, text="", text_color="#ff6b6f", font=theme.font(13),
                                 anchor="w", justify="left", wraplength=380)
        self._err.grid(row=0, column=0, sticky="w", padx=16)
        theme.ghost_button(br, "取消", self.destroy, width=84, height=34).grid(
            row=0, column=1, padx=(0, 8), pady=12)
        theme.primary_button(br, "保存", self._save, width=96, height=34).grid(
            row=0, column=2, padx=(0, 16), pady=12)

    def _build_time(self):
        sec = self._section("时间", "跨午夜: 开始晚于结束(如 22:00–02:00); 全天: 00:00–00:00")
        row = self._form_row(sec, "星期")
        self._day_vars = []
        self._day_btns = []
        for i, lab in enumerate(WEEKDAY_LABELS):
            v = ctk.IntVar(value=1 if i in self._block.get("days", []) else 0)
            b = ctk.CTkButton(row, text=lab, width=36, height=32, corner_radius=16,
                              font=theme.font(13), border_width=1,
                              command=lambda k=i: self._toggle_day(k))
            b.pack(side="left", padx=(0, 5))
            self._day_vars.append(v)
            self._day_btns.append(b)
        self._paint_days()

        quick = ctk.CTkFrame(sec, fg_color="transparent")
        quick.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(quick, text="", width=self._LABEL_W).pack(side="left")
        for txt, days in (("工作日", range(5)), ("周末", (5, 6)), ("每天", range(7)),
                          ("清空", ())):
            ctk.CTkButton(quick, text=txt, width=52, height=24, font=theme.font(11),
                          fg_color="transparent", hover_color=theme.SURFACE_HI,
                          text_color=theme.ACCENT,
                          command=lambda d=tuple(days): self._set_days(d)).pack(
                side="left", padx=(0, 4))

        tr = self._form_row(sec, "时段", pady=(0, 0))
        self._start_e = ctk.CTkEntry(tr, width=76, placeholder_text="09:00",
                                     font=theme.font(13), justify="center")
        self._start_e.insert(0, self._block.get("start", ""))
        self._start_e.pack(side="left")
        ctk.CTkLabel(tr, text="  到  ", text_color=theme.MUTED,
                     font=theme.font(13)).pack(side="left")
        self._end_e = ctk.CTkEntry(tr, width=76, placeholder_text="12:00",
                                   font=theme.font(13), justify="center")
        self._end_e.insert(0, self._block.get("end", ""))
        self._end_e.pack(side="left")
        for e in (self._start_e, self._end_e):
            e.bind("<FocusOut>", lambda ev, w=e: self._normalize_entry(w), add="+")
        theme.hint(tr, "   可以只写 9 或 930, 会自动规整").pack(side="left")

    def _toggle_day(self, i):
        v = self._day_vars[i]
        v.set(0 if v.get() else 1)
        self._paint_days()

    def _set_days(self, days):
        for i, v in enumerate(self._day_vars):
            v.set(1 if i in days else 0)
        self._paint_days()

    def _paint_days(self):
        for v, b in zip(self._day_vars, self._day_btns):
            if v.get():
                b.configure(fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                            border_color=theme.ACCENT, text_color="white")
            else:
                b.configure(fg_color="transparent", hover_color=theme.SURFACE_HI,
                            border_color=theme.BORDER, text_color=theme.MUTED)

    def _build_account_map(self):
        sec = self._section("账号与地图")
        row = self._form_row(sec, "账号")
        vals = [p["alias"] for p in self._profiles] + [self._NEW_PROFILE]
        self._acct = ctk.CTkOptionMenu(row, values=vals, command=self._on_acct_pick,
                                       width=200, font=theme.font(13),
                                       dropdown_font=theme.font(13),
                                       fg_color=theme.SURFACE_HI,
                                       button_color=theme.BORDER,
                                       button_hover_color=theme.FAINT)
        self._acct.set(self._block.get("profile", vals[0]))
        self._acct_prev = self._acct.get()
        self._acct.pack(side="left")

        self._map = tk.StringVar(value=self._block.get("map", "desert"))
        map_row = self._form_row(sec, "地图", pady=(0, 0))
        radios = ctk.CTkFrame(map_row, fg_color="transparent")
        radios.pack(side="left")
        for i, m in enumerate(app_config._VALID_MAPS):
            state = _map_radio_state(m)
            text = theme.map_label(m)
            if state == "disabled":
                text += "(暂不可用)"
            ctk.CTkRadioButton(radios, text=text, variable=self._map, value=m,
                               state=state, font=theme.font(13),
                               fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                               command=self._on_map_change).grid(
                row=i // 4, column=i % 4, sticky="w", padx=(0, 16), pady=(0, 6))
        self._map_hint = theme.hint(sec, wraplength=520, text_color=theme.WARN)
        self._sync_map_hint()

    def _build_location(self):
        sec = self._section(
            "刷怪位置",
            "左键点一下 = 目标点(绿十字);左键拖动 = 刷怪区域(蓝框);滚轮缩放。二选一即可")
        from gui_map_picker import MapPicker
        self._picker = MapPicker(sec, on_point_change=self._on_point,
                                 on_area_change=self._on_area, fg_color=theme.LOG_BG,
                                 corner_radius=8)
        # 滚动框里不能 expand=True(会把整条滚动内容撑没边); 给个固定高度.
        self._picker.configure(height=320)
        self._picker.pack(fill="x", pady=(0, 8))
        try:
            self._picker.pack_propagate(False)
        except Exception:
            pass
        self._picker.load_map(self._map.get())
        self._picker.set_point(self._point)
        self._picker.set_area(self._area)

        rd = ctk.CTkFrame(sec, fg_color="transparent")
        rd.pack(fill="x", pady=(0, 8))
        self._loc_readout = ctk.CTkLabel(rd, text="", font=theme.mono(12),
                                         text_color=theme.TEXT, anchor="w")
        self._loc_readout.pack(side="left")
        theme.ghost_button(rd, "清除", self._clear_location, width=56, height=26).pack(
            side="right")
        self._sync_readout()

        dur = self._form_row(sec, "刷怪时长", pady=(0, 0))
        self._dur_e = ctk.CTkEntry(dur, width=76, font=theme.font(13), justify="center")
        self._dur_e.insert(0, str(self._block.get("farming_duration", 300)))
        self._dur_e.pack(side="left")
        ctk.CTkLabel(dur, text=" 秒 ", text_color=theme.MUTED,
                     font=theme.font(13)).pack(side="left")
        _help_mark(dur, self._TIP_DURATION).pack(side="left")

    def _build_combat(self):
        sec = self._section("战斗")

        self._enemy = theme.switch(sec, "索敌 AI(追击 / 先清青怪)", command=self._sync_rules_button)
        if self._block.get("enemy_ai_enabled", True):
            self._enemy.select()
        self._enemy.pack(anchor="w")
        self._enemy_hint = theme.hint(sec)
        self._enemy_hint.pack(anchor="w", padx=(46, 0), pady=(0, 8))

        er = ctk.CTkFrame(sec, fg_color="transparent")
        er.pack(fill="x", padx=(46, 0), pady=(0, 8))
        self._rules_btn = theme.ghost_button(er, "索敌设置…", self._open_rules, width=96, height=28)
        self._rules_btn.pack(side="left")
        self._rules_summary = theme.hint(er, text=rules_summary(self._rules))
        self._rules_summary.pack(side="left", padx=(10, 0))

        inv = ctk.CTkFrame(sec, fg_color="transparent")
        inv.pack(fill="x")
        self._inv_attack = theme.switch(inv, "反转攻击键")
        if self._block.get("invert_attack", True):
            self._inv_attack.select()
        self._inv_attack.pack(side="left", padx=(0, 24))
        self._inv_defense = theme.switch(inv, "反转防御键")
        if self._block.get("invert_defense", False):
            self._inv_defense.select()
        self._inv_defense.pack(side="left")
        _help_mark(inv, "florr 设置里的「反转攻击 / 防御键」。每轮开局由 worker 自动写进游戏;"
                        "寻路时脚本不按攻击键, 靠反转攻击才能边走边打。").pack(
            side="left", padx=6)
        self._sync_enemy_enabled()

    def _build_server_and_loadout(self):
        sec = self._section("换服与换装")
        sw = ctk.CTkFrame(sec, fg_color="transparent")
        sw.pack(fill="x", pady=(0, 10))
        self._autosw = theme.switch(sw, "自动换服务器", command=self._sync_short_enabled)
        if self._block.get("auto_switch_server", True):
            self._autosw.select()
        self._autosw.pack(side="left")
        ctk.CTkLabel(sw, text="   连续短局", text_color=theme.MUTED,
                     font=theme.font(13)).pack(side="left")
        self._short_e = ctk.CTkEntry(sw, width=50, font=theme.font(13), justify="center")
        self._short_e.insert(0, str(self._block.get("consecutive_short_round_limit", 2)))
        self._short_e.pack(side="left", padx=6)
        ctk.CTkLabel(sw, text="轮就换", text_color=theme.MUTED,
                     font=theme.font(13)).pack(side="left")
        _help_mark(sw, self._TIP_SHORT).pack(side="left", padx=6)
        self._sync_short_enabled()

        self._enter_swap_w = self._build_swap_field(
            sec, "enter_game_swap", "进游戏时换装",
            "每轮真的(重新)进游戏后按一次和弦换 loadout: 按住修饰键 → 按数字 → 松开.")
        self._reach_swap_w = self._build_swap_field(
            sec, "reach_area_swap", "到刷怪区换装",
            "寻路到刷怪区后按一次和弦换 loadout.")

    def _build_swap_field(self, parent, key, title, tip):
        """一个 loadout 切换字段: 开关 + 修饰键下拉(无/k/l) + 数字下拉(1..0).
        返回 (switch, mod_menu, digit_menu); _collect 从这三个读回 {enabled,mod,digit}."""
        cur = _coerce_swap_obj(self._block.get(key))
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, 6))
        sw = theme.switch(row, title, width=150)
        if cur["enabled"]:
            sw.select()
        sw.pack(side="left")
        _Tooltip(sw, tip)
        menu_kw = dict(font=theme.font(12), dropdown_font=theme.font(12),
                       fg_color=theme.SURFACE_HI, button_color=theme.BORDER,
                       button_hover_color=theme.FAINT, height=28)
        ctk.CTkLabel(row, text="按住", text_color=theme.MUTED,
                     font=theme.font(12)).pack(side="left", padx=(12, 4))
        mod = ctk.CTkOptionMenu(row, width=64, values=list(_SWAP_MOD_LABELS.values()),
                                **menu_kw)
        mod.set(_SWAP_MOD_LABELS[cur["mod"]])
        mod.pack(side="left")
        ctk.CTkLabel(row, text="再按", text_color=theme.MUTED,
                     font=theme.font(12)).pack(side="left", padx=(8, 4))
        digit = ctk.CTkOptionMenu(row, width=56, values=_SWAP_DIGIT_VALUES, **menu_kw)
        digit.set(cur["digit"])
        digit.pack(side="left")
        return sw, mod, digit

    def _collect_swap(self, widgets):
        sw, mod, digit = widgets
        return {
            "enabled": bool(sw.get()),
            "mod": _SWAP_MOD_FROM_LABEL.get(mod.get(), "none"),
            "digit": digit.get(),
        }

    def _normalize_entry(self, entry):
        """失焦: 能规整就把输入框内容替换成规范 HH:MM; 规整不了就原样留着,
        交给保存时的 validate_block 红字. 纯 UI 便利, 不参与校验闭环(_collect 自己也规整)."""
        fixed = app_config.normalize_time(entry.get().strip())
        if fixed is not None and fixed != entry.get():
            entry.delete(0, "end")
            entry.insert(0, fixed)

    def _sync_short_enabled(self):
        theme.set_entry_enabled(self._short_e, bool(self._autosw.get()))

    def _on_acct_pick(self, val):
        if val != self._NEW_PROFILE:
            self._acct_prev = val
            return
        # 取消 / 出错时回到用户原来选的那个账号, 而不是一律跳回列表第一个.
        dlg = ctk.CTkInputDialog(text="新账号别名:", title="新建账号")
        alias = (dlg.get_input() or "").strip()
        if not alias:
            self._acct.set(self._acct_prev)
            return
        if self.new_profile_cb is None:
            self._err.configure(text="新建账号未接线")
            self._acct.set(self._acct_prev)
            return
        # 登录引导完成前先回到原账号; 完成后 _add_profile_to_menu 会选中新账号.
        self._acct.set(self._acct_prev)
        self.new_profile_cb(alias, self._add_profile_to_menu)

    def _add_profile_to_menu(self, new_alias):
        """新建账号 + 登录引导完成后回调: 把新别名加进账号下拉并选中.
        登录引导是非模态的, 用户可能在登录完成前把这个编辑器关掉了 —— 那样
        self / self._acct 的 tk 控件已销毁, configure 会 TclError. profile 本身
        已被 new_profile_cb 建好 + 存盘, 这里只是刷下拉, 编辑器没了就直接跳过."""
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self._profiles.append({"alias": new_alias, "dir": f"chrome-profiles/{new_alias}"})
        self._acct.configure(values=[p["alias"] for p in self._profiles] + [self._NEW_PROFILE])
        self._acct.set(new_alias)
        self._acct_prev = new_alias

    def _on_map_change(self):
        # CTkRadioButton 的 command 不带值, 从 StringVar 自己读.
        self._point = None
        self._area = None
        self._picker.load_map(self._map.get())
        self._picker.set_point(None)
        self._picker.set_area(None)
        self._sync_readout()
        self._close_rules_dialog()        # 子窗是按旧地图的物种列表建的
        self._set_rules(rules_for_map(self._rules, self._map.get()))
        self._sync_enemy_enabled()
        self._sync_map_hint()

    def _sync_map_hint(self):
        text = _anthell_blocker() if self._map.get() == "anthell" else None
        if text:
            self._map_hint.configure(text="⚠ " + text)
            self._map_hint.pack(anchor="w", pady=(8, 0))
        else:
            self._map_hint.pack_forget()


    def _sync_enemy_enabled(self):
        """索敌只在物种表非空的图上可用(现在七张图都有; 空表图留给以后新增的图)。空表图上开关置灰 ——
        worker 那边 main._apply_worker_config 本来就会强制关, 界面上跟它一致,
        免得用户以为开了就生效。"""
        # 延迟导入: enemy_detect 会拉进 cv2 / cdp_bridge / canvas_decode, GUI 的
        # 启动路径不该为了一个开关状态背这些。(同文件里 MapPicker 也是这么导的。)
        from enemy_detect import species_supported
        supported = species_supported(self._map.get())
        self._enemy.configure(state="normal" if supported else "disabled")
        self._enemy_hint.configure(
            text="本图支持索敌" if supported else "本图暂不支持索敌")
        self._sync_rules_button()

    def _sync_rules_button(self):
        """「索敌设置…」只在索敌真会生效时可点: 索敌开关开着、本图有物种表。
        按钮置灰时顺手关掉已经开着的子窗 —— 不然灰按钮底下还有个能点「确定」的窗口。"""
        from enemy_detect import species_supported
        on = bool(self._enemy.get()) and species_supported(self._map.get())
        self._rules_btn.configure(state="normal" if on else "disabled")
        if not on:
            self._close_rules_dialog()

    def _close_rules_dialog(self):
        if self._rules_dlg is not None and self._rules_dlg.winfo_exists():
            self._rules_dlg.destroy()
        self._rules_dlg = None

    def _open_rules(self):
        if self._rules_dlg is not None and self._rules_dlg.winfo_exists():
            self._rules_dlg.lift()
            return
        self._rules_dlg = EnemyRulesDialog(self, map_name=self._map.get(),
                                           rules=self._rules, on_ok=self._set_rules)

    def _set_rules(self, rules):
        self._rules = rules
        self._rules_summary.configure(text=rules_summary(rules))


    def _on_point(self, pt):
        self._point = pt
        self._sync_readout()

    def _on_area(self, area):
        self._area = [tuple(area[0]), tuple(area[1])]
        self._sync_readout()

    def _clear_location(self):
        self._point = None
        self._area = None
        self._picker.set_point(None)
        self._picker.set_area(None)
        self._sync_readout()

    def _sync_readout(self):
        parts = []
        if self._point:
            parts.append(f"目标点 {_fmt_pt(self._point)}")
        if self._area:
            parts.append(f"区域 {_fmt_pt(self._area[0])}–{_fmt_pt(self._area[1])}")
        if parts:
            self._loc_readout.configure(text="  ·  ".join(parts), text_color=theme.TEXT)
        else:
            self._loc_readout.configure(text="还没选位置", text_color=theme.FAINT)

    def _collect(self):
        from gui_app import resolve_point_and_area
        point, area = resolve_point_and_area(self._point, self._area)
        days = [i for i, v in enumerate(self._day_vars) if v.get()]
        start_raw = self._start_e.get().strip()
        end_raw = self._end_e.get().strip()
        blk = dict(self._block)
        blk.update(
            days=days,
            start=app_config.normalize_time(start_raw) or start_raw,
            end=app_config.normalize_time(end_raw) or end_raw,
            profile=self._acct.get(), map=self._map.get(),
            location=list(point) if point else None,
            farming_area=[list(area[0]), list(area[1])] if area else None,
            enemy_ai_enabled=bool(self._enemy.get()),
            auto_switch_server=bool(self._autosw.get()),
            invert_attack=bool(self._inv_attack.get()),
            invert_defense=bool(self._inv_defense.get()),
            enter_game_swap=self._collect_swap(self._enter_swap_w),
            reach_area_swap=self._collect_swap(self._reach_swap_w),
        )
        if self._rules["species"] or self._rules["knobs"]:
            blk["enemy_rules"] = {"species": dict(self._rules["species"]),
                                  "knobs": dict(self._rules["knobs"])}
        else:
            blk.pop("enemy_rules", None)          # 空规则不写键, 老时块文件保持干净
        try:
            blk["farming_duration"] = int(self._dur_e.get())
        except ValueError:
            blk["farming_duration"] = 0
        try:
            blk["consecutive_short_round_limit"] = int(self._short_e.get())
        except ValueError:
            blk["consecutive_short_round_limit"] = 0
        return blk

    def _save(self):
        blk = self._collect()
        err = validate_block(blk, self._others)
        if err:
            self._err.configure(text="⚠ " + err)
            self.bell()
            return
        self._on_save(blk)
        self.destroy()


def _block_sort_key(blk):
    return (min(blk.get("days") or [7]), blk.get("start", ""))


class ScheduleList(ctk.CTkScrollableFrame):
    """时块卡片列表 + ＋新增. get_cfg() 返回当前 cfg dict; save_cfg(cfg) 落盘并
    回读; open_editor(block_or_None) 由 App 提供(弹 TimeBlockEditor)。"""

    def __init__(self, master, *, get_cfg, save_cfg, open_editor):
        super().__init__(master, fg_color="transparent")
        self._get_cfg = get_cfg
        self._save_cfg = save_cfg
        self._open_editor = open_editor
        self._readonly = False
        self._running_id = None
        self.refresh()

    def set_readonly(self, ro):
        self._readonly = bool(ro)
        self.refresh()

    def set_running(self, block_id):
        if block_id != self._running_id:
            self._running_id = block_id
            self.refresh()

    def refresh(self):
        for w in list(self.winfo_children()):
            w.destroy()
        st = "disabled" if self._readonly else "normal"
        if self._readonly:
            theme.hint(self, "调度运行中, 时间表只读 —— 停止调度后才能修改",
                       text_color=theme.WARN).pack(anchor="w", padx=2, pady=(0, 6))
        blocks = sorted(self._get_cfg().get("schedule", []), key=_block_sort_key)
        if not blocks:
            empty = theme.card(self)
            empty.pack(fill="x", pady=4)
            ctk.CTkLabel(empty, text="还没有时块", font=theme.font(15, "bold"),
                         text_color=theme.TEXT).pack(pady=(22, 2))
            theme.hint(empty, "新增一个时块: 选星期和时段、账号、地图, 再在地图上点出刷怪位置",
                       anchor="center").pack(pady=(0, 22))
        for blk in blocks:
            self._build_row(blk, st)
        theme.primary_button(self, "＋  新增时块", lambda: self._open_editor(None),
                             state=st, height=36).pack(fill="x", pady=(8, 2))

    def _build_row(self, blk, st):
        running = blk["id"] == self._running_id
        on = bool(blk["enabled"])
        row = theme.card(self, border_color=theme.SUCCESS if running else theme.BORDER,
                         border_width=2 if running else 1)
        row.pack(fill="x", pady=4)
        row.grid_columnconfigure(1, weight=1)

        ev = ctk.IntVar(value=1 if on else 0)
        sw = theme.switch(row, "", width=44, variable=ev, state=st,
                          command=lambda b=blk, v=ev: self._toggle(b, v))
        sw.grid(row=0, column=0, rowspan=2, padx=(14, 6), pady=12)
        _Tooltip(sw, "启用 / 停用这个时块(停用的不参与调度)")

        main_c = theme.TEXT if on else theme.FAINT
        sub_c = theme.MUTED if on else theme.FAINT
        top = ctk.CTkFrame(row, fg_color="transparent")
        top.grid(row=0, column=1, sticky="w", pady=(10, 0))
        ctk.CTkLabel(top, text=f"{blk['start']} – {blk['end']}", font=theme.mono(15),
                     text_color=main_c).pack(side="left")
        ctk.CTkLabel(top, text="   " + weekday_text(blk["days"]), font=theme.font(13),
                     text_color=main_c).pack(side="left")
        if running:
            ctk.CTkLabel(top, text=" ● 运行中 ", font=theme.font(11, "bold"),
                         text_color=theme.SUCCESS).pack(side="left", padx=8)
        elif not on:
            ctk.CTkLabel(top, text="  已停用", font=theme.font(11),
                         text_color=theme.FAINT).pack(side="left", padx=4)

        tags = [blk["profile"], theme.map_label(blk["map"]),
                ("索敌" if blk.get("enemy_ai_enabled") else "不索敌"),
                f"刷 {blk.get('farming_duration', 0)} 秒"]
        if blk.get("auto_switch_server"):
            tags.append("自动换服")
        ctk.CTkLabel(row, text="  ·  ".join(str(t) for t in tags), font=theme.font(12),
                     text_color=sub_c, anchor="w").grid(row=1, column=1, sticky="w",
                                                        pady=(0, 10))

        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.grid(row=0, column=2, rowspan=2, padx=(6, 12))
        theme.ghost_button(btns, "编辑", lambda b=blk: self._open_editor(b), width=56,
                           height=28, state=st).pack(side="left", padx=(0, 6))
        theme.danger_button(btns, "删除", lambda b=blk: self._delete(b), width=56,
                            height=28, state=st).pack(side="left")

    def _toggle(self, blk, var):
        cfg = self._get_cfg()
        for b in cfg["schedule"]:
            if b["id"] == blk["id"]:
                b["enabled"] = bool(var.get())
        self._save_cfg(cfg)
        self.refresh()

    def _delete(self, blk):
        from tkinter import messagebox
        if not messagebox.askyesno(
                "删除时块",
                f"删掉「{weekday_text(blk['days'])} {blk['start']}–{blk['end']}」"
                f"({blk['profile']} · {theme.map_label(blk['map'])}) 这个时块?",
                parent=self):
            return
        cfg = self._get_cfg()
        cfg["schedule"] = [b for b in cfg["schedule"] if b["id"] != blk["id"]]
        self._save_cfg(cfg)
        self.refresh()
