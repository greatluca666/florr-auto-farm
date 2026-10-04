import threading

import pytest

import phase_marks

FARM, TRAVEL, OTHER = (phase_marks.PREFIX + n for n in ("farm", "travel", "other"))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv(phase_marks.ENV_VAR, "1")
    phase_marks._reset_for_tests()
    yield
    phase_marks._reset_for_tests()


def markers(capsys):
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(phase_marks.PREFIX)]


def test_prints_a_marker_on_entry_and_restores_the_outer_phase_on_exit(capsys):
    with phase_marks.phase("travel"):
        assert phase_marks.current() == "travel"
    assert phase_marks.current() == "other"
    assert markers(capsys) == [TRAVEL, OTHER]


def test_nested_phases_restore_the_enclosing_one(capsys):
    with phase_marks.phase("farm"):
        with phase_marks.phase("travel"):
            assert phase_marks.current() == "travel"
        assert phase_marks.current() == "farm"
    assert markers(capsys) == [FARM, TRAVEL, FARM, OTHER]


def test_entering_the_same_phase_again_does_not_repeat_the_marker(capsys):
    with phase_marks.phase("travel"):
        with phase_marks.phase("travel"):
            pass
    assert markers(capsys) == [TRAVEL, OTHER]


def test_an_exception_inside_the_block_still_restores_the_phase(capsys):
    with pytest.raises(RuntimeError):
        with phase_marks.phase("farm"):
            raise RuntimeError("boom")
    assert phase_marks.current() == "other"
    assert markers(capsys) == [FARM, OTHER]


def test_nothing_is_printed_unless_the_gui_asked_for_markers(monkeypatch, capsys):
    monkeypatch.delenv(phase_marks.ENV_VAR)
    with phase_marks.phase("farm"):
        assert phase_marks.current() == "farm"            # 阶段照样跟踪, 只是不打印
    assert capsys.readouterr().out == ""


def test_a_marker_skipped_while_disabled_is_not_remembered_as_printed(monkeypatch, capsys):
    monkeypatch.delenv(phase_marks.ENV_VAR)
    with phase_marks.phase("farm"):                  # 关着: 不打印, 也不能记成「farm 已经打印过」
        monkeypatch.setenv(phase_marks.ENV_VAR, "1")
        with phase_marks.phase("farm"):              # 这时候打开了: 这一条必须真的打出来
            pass
    assert markers(capsys) == [FARM, OTHER]


def test_an_unknown_phase_name_is_a_programming_error():
    with pytest.raises(ValueError):
        with phase_marks.phase("nap"):
            pass
    assert phase_marks.current() == "other"


def test_concurrent_phase_changes_do_not_corrupt_the_stack():
    def work():
        for _ in range(300):
            with phase_marks.phase("travel"):
                with phase_marks.phase("farm"):
                    pass
    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert phase_marks.current() == "other"
