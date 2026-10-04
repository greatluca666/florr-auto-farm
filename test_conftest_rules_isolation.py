"""根目录 conftest.py 里那个 autouse fixture 的守护。

enemy_detect._RULES 是进程内共享的模块级状态: 任何一条用例装了用户规则(set_rules)却没清, 后面的用例就会
被它悄悄影响, 结果跟执行顺序有关。以前靠 test_enemy_detect.py / test_main_worker.py 各写一份 fixture, 新
模块忘了写就漏; 现在统一放在 conftest.py, 这里守住它真的在、真的管用(删掉 conftest 里那个 fixture, 下面
至少两条会红; 只删「用例前清」或只删「用例后清」其中一半也各有一条会红)。"""
import builtins
import sys
import types

import pytest

import enemy_detect

_EMPTY = {"species": {}, "knobs": {}}


@pytest.fixture(scope="module", autouse=True)
def _wider_scoped_rules_probe():
    """比 conftest 里函数级的 fixture 先建、后拆, 专门用来把「用例前清」和「用例后清」两半拆开守:
      - 先建: 在第一条用例的「前清」之前往里装规则 —— 没有「前清」, test_0 就看得到它;
      - 后拆: 拆除发生在最后一条用例的函数级拆除之后 —— 没有「后清」, 最后一条用例留下的规则还在, 这里报错。
    (光靠 test_1 / test_2 这一对守不住: 前清和后清任缺一半, 下一条用例都仍然是干净的。)"""
    enemy_detect.set_rules({"species": {"bee": "avoid"}})
    yield
    assert enemy_detect.get_rules() == _EMPTY, "最后一条用例留下的规则没被清掉(缺「用例后清」)"


def test_0_rules_installed_before_the_test_starts_are_cleared_first():
    assert enemy_detect.get_rules() == _EMPTY


def test_1_a_test_installs_rules_and_does_not_clean_up_after_itself():
    enemy_detect.set_rules({"species": {"rock": "ignore"}, "knobs": {"swarm": False}})
    assert enemy_detect.rule_for("rock") == "ignore"


def test_2_the_next_test_starts_from_empty_rules():
    # 跟上一条是一对: 靠 pytest 按定义顺序跑同一个文件里的用例(没装 xdist / randomly)。单独只跑这一条
    # 也会过(没有东西可漏), 所以它只会"该红时红", 不会误报; 删掉 conftest 的 fixture 后整文件一起跑就红。
    assert enemy_detect.get_rules() == _EMPTY


def test_the_autouse_fixture_is_active_for_every_test(request):
    # 不依赖用例顺序的那一半: fixture 本身必须存在并且是 autouse。
    assert "_reset_enemy_rules" in request.fixturenames


def test_the_helper_resets_a_loaded_enemy_detect_through_set_rules_none(monkeypatch):
    import conftest
    calls = []
    monkeypatch.setitem(sys.modules, "enemy_detect", types.SimpleNamespace(set_rules=calls.append))
    conftest._reset_enemy_rules_if_loaded()
    assert calls == [None]


def test_the_helper_never_imports_enemy_detect_itself(monkeypatch):
    # 没碰过 enemy_detect 的测试不该为了清规则去背 cv2 / cdp_bridge / pyautogui。
    import conftest
    monkeypatch.delitem(sys.modules, "enemy_detect")
    real_import = builtins.__import__

    def guard(name, *a, **k):
        if name == "enemy_detect":
            raise AssertionError("不该导入 enemy_detect")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    conftest._reset_enemy_rules_if_loaded()
    assert "enemy_detect" not in sys.modules


def test_zz_the_last_test_leaves_rules_installed_for_the_module_teardown_to_check():
    # 必须是本文件最后一条: 它故意不清理, 由 _wider_scoped_rules_probe 的拆除段验证 conftest 替它清了。
    enemy_detect.set_rules({"species": {"rock": "avoid"}})
    assert enemy_detect.rule_for("rock") == "avoid"
