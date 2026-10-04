"""全仓库共用的 pytest 夹具。"""
import sys

import pytest


def _reset_enemy_rules_if_loaded():
    """清掉 enemy_detect 里的用户自配规则(模块级 _RULES, 进程内共享)。

    只在 enemy_detect 已经被导入时才动手, 自己不导入它: 它会一路拉进 cv2 / cdp_bridge / pyautogui,
    没碰过它的测试模块不该为了清规则平白背上这些。
    """
    ed = sys.modules.get("enemy_detect")
    set_rules = getattr(ed, "set_rules", None)
    if callable(set_rules):
        set_rules(None)


@pytest.fixture(autouse=True)
def _reset_enemy_rules():
    """一条用例装了规则(set_rules / main._apply_worker_config 都会装)却没清, 后面的用例会被它影响,
    结果跟执行顺序有关。每条用例前后都清 —— 以前是 test_enemy_detect / test_main_worker 各写一份, 新模块
    忘了写就漏; 统一放这里。守护用例见 test_conftest_rules_isolation.py。"""
    _reset_enemy_rules_if_loaded()
    yield
    _reset_enemy_rules_if_loaded()
