"""进游戏 / 到刷怪区时按一次和弦切换 florr loadout.

跟 florr_settings.py / server_lookup.py 一样: 小、单一职责、不 import GUI、
不 import main. 三个按键原语通过参数注入, 方便单测.

和弦 = 像 Ctrl+C: 按住修饰键 (k/l) → 按数字 → 松开修饰键. mod=="none" 就只按数字.
"""
import pyautogui

# tuple 不是 str —— `"12" in "1234567890"` 是子串匹配会误判成合法, tuple 成员判定才对.
_DIGITS = tuple("1234567890")
_MODS = ("k", "l")


def press_swap(cfg, *, press=pyautogui.press,
               key_down=pyautogui.keyDown, key_up=pyautogui.keyUp):
    """按一次 loadout 切换和弦.

    cfg: {"enabled": bool, "mod": "none"|"k"|"l", "digit": "1".."9"|"0"}.
      - 非 dict / enabled 假           → 什么都不做.
      - digit 不在 1..0               → 什么都不做 (+ ⚠️ 日志).
      - mod == "none" / 未知           → press(digit).
      - mod in (k, l)                 → key_down(mod); press(digit); key_up(mod).
        key_up 走 finally —— press 抛了也要把修饰键松开.

    任何异常吞掉 + ⚠️ 日志, 绝不抛给调用方: 切装备是附加动作, 不能打断刷怪轮次
    (对齐 main._reassert_florr_toggles 的 warn-only).
    """
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return
    digit = cfg.get("digit")
    mod = cfg.get("mod", "none")
    if not (isinstance(digit, str) and digit in _DIGITS):
        print(f"⚠️ 装备切换和弦: 无效数字键 {digit!r}, 跳过")
        return
    try:
        if mod in _MODS:
            key_down(mod)
            try:
                press(digit)
            finally:
                key_up(mod)
        else:
            press(digit)
    except Exception as e:
        print(f"⚠️ 装备切换按键失败 (mod={mod} digit={digit}): {e}")
        return
