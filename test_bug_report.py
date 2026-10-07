"""bug 自动上报(bug_report.py): 脱敏 / traceback 识别 / 签名 / 报告字段 / 节流 / 本地副本 / 发送编排。"""
import json
import os
import re
import threading

import pytest

import bug_report as br
import telemetry

IID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _fixed_install_id(monkeypatch):
    monkeypatch.setattr(telemetry, "install_id", lambda: IID)
    monkeypatch.delenv("FLORR_TELEMETRY", raising=False)


TB = (
    "Traceback (most recent call last):\n"
    '  File "C:\\Users\\Alice\\florr\\main.py", line 120, in run_worker\n'
    "    do_thing()\n"
    '  File "C:\\Users\\Alice\\florr\\enemy_detect.py", line 55, in select_action\n'
    "    return best[\"x\"]\n"
    "KeyError: 'x'"
)


# ── 脱敏 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, want", [
    (r"C:\Users\Alice\AppData\Local\x", r"C:\Users\<user>\AppData\Local\x"),
    (r"c:\users\ALICE\x", r"c:\users\<user>\x"),
    ("C:/Users/Alice/x.py", "C:/Users/<user>/x.py"),
    ("/Users/bob/Library/Application Support", "/Users/<user>/Library/Application Support"),
    ("/home/carol/.local/share", "/home/<user>/.local/share"),
    ('File "C:\\Users\\张三\\florr\\main.py", line 3', 'File "C:\\Users\\<user>\\florr\\main.py", line 3'),
])
def test_home_directories_lose_the_os_username(raw, want):
    assert br.redact(raw) == want


def test_non_default_profile_aliases_are_replaced_everywhere_but_the_default_one_is_kept():
    text = "切到 小号A 的 chrome-profiles/小号A/Default, 默认 账号不动"
    out = br.redact(text, aliases=["小号A", "默认"])
    assert "小号A" not in out
    assert out == "切到 <profile> 的 chrome-profiles/<profile>/Default, 默认 账号不动"


def test_one_character_aliases_are_not_replaced_they_would_shred_every_message():
    assert br.redact("a b c 1 2", aliases=["a", "1"]) == "a b c 1 2"


def test_aliases_with_regex_metacharacters_are_matched_literally():
    assert br.redact("号 a.b(c) 在", aliases=["a.b(c)"]) == "号 <profile> 在"


@pytest.mark.parametrize("raw, want", [
    ("联系 someone@example.com 了", "联系 <email> 了"),
    ("连 192.168.1.20 失败", "连 <ip> 失败"),
    ("GET https://florr.io/?server=abc123&k=v#frag 200", "GET https://florr.io/ 200"),
    ("https://user:pw@host.example/path?x=1", "https://host.example/path"),
    ("token=" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA", "token=<token>"),
])
def test_personal_looking_strings_are_masked(raw, want):
    assert br.redact(raw) == want


@pytest.mark.parametrize("text", [
    "版本 1.0.3 启动", "Lvl 37 Flower", "worker 已启动", "⚠️ 索敌出错, 本轮当漫游处理: 'x'",
    "未识别怪物名: 'Ladybug' (slug='ladybug')", "x=3.14, y=2.71", "a" * 31,
    "1.2.3", "test_the_threshold_reaches_the_flee_decision_and_more_words",
])
def test_ordinary_diagnostic_text_survives_untouched(text):
    assert br.redact(text) == text


def test_redact_is_idempotent_and_tolerates_junk():
    once = br.redact(TB, ["小号A"])
    assert br.redact(once, ["小号A"]) == once
    assert br.redact("", ["x1"]) == ""
    assert br.redact(None) == ""
    assert br.redact(12345) == "12345"


# ── traceback 识别 ───────────────────────────────────────────────────────────

def _feed_all(lines):
    sc, out = br.TracebackScanner(), []
    for ln in lines:
        got = sc.feed(ln)
        if got:
            out.append(got)
    tail = sc.flush()
    if tail:
        out.append(tail)
    return out


def test_a_traceback_is_emitted_once_when_the_next_ordinary_line_arrives():
    sc = br.TracebackScanner()
    got = [sc.feed(ln) for ln in (TB + "\nafter\n").splitlines(keepends=True)]
    emitted = [g for g in got if g]
    assert emitted == [TB]
    assert sc.flush() is None


def test_a_traceback_that_ends_the_stream_comes_out_on_flush():
    assert _feed_all((TB + "\n").splitlines(keepends=True)) == [TB]


def test_crlf_line_endings_are_fine():
    assert _feed_all([ln + "\r\n" for ln in TB.splitlines()]) == [TB]


def test_chained_exceptions_stay_one_report():
    chained = (
        "Traceback (most recent call last):\n"
        '  File "a.py", line 1, in f\n'
        "    g()\n"
        "ValueError: inner\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "a.py", line 5, in h\n'
        "    raise RuntimeError('outer')\n"
        "RuntimeError: outer\n"
    )
    out = _feed_all((chained + "next line\n").splitlines(keepends=True))
    assert len(out) == 1
    assert "ValueError: inner" in out[0] and out[0].rstrip().endswith("RuntimeError: outer")


def test_two_separate_tracebacks_are_two_reports():
    other = TB.replace("KeyError: 'x'", "IndexError: 3")
    out = _feed_all(["hello\n"] + (TB + "\n").splitlines(keepends=True) + ["middle\n"]
                    + (other + "\n").splitlines(keepends=True) + ["bye\n"])
    assert [o.splitlines()[-1] for o in out] == ["KeyError: 'x'", "IndexError: 3"]


def test_the_word_traceback_in_the_middle_of_a_line_does_not_start_one():
    assert _feed_all(["foo Traceback (most recent call last):\n", "  File x\n", "E: y\n"]) == []


def test_a_huge_traceback_is_bounded():
    frames = ['  File "a.py", line 1, in f\n', "    g()\n"] * 5000
    out = _feed_all(["Traceback (most recent call last):\n"] + frames + ["RecursionError: deep\n"])
    assert len(out) == 1
    assert len(out[0].splitlines()) <= br.MAX_TB_LINES
    assert out[0].rstrip().endswith("RecursionError: deep")


# ── 签名 ─────────────────────────────────────────────────────────────────────

def test_signature_ignores_line_numbers_home_dirs_and_the_message():
    a = br.signature("worker_traceback", TB)
    b = br.signature("worker_traceback", TB.replace("line 120", "line 999").replace("Alice", "Bob")
                     .replace("KeyError: 'x'", "KeyError: 'completely other text'"))
    assert a == b
    assert len(a) == 12 and all(c in "0123456789abcdef" for c in a)


def test_signature_differs_by_exception_type_and_by_the_innermost_frames():
    base = br.signature("worker_traceback", TB)
    assert br.signature("worker_traceback", TB.replace("KeyError", "IndexError")) != base
    assert br.signature("worker_traceback", TB.replace("select_action", "other_func")) != base


def test_signature_uses_only_the_last_traceback_of_a_chain():
    cause = "Traceback (most recent call last):\n  File \"z.py\", line 1, in zz\n    q()\nOSError: io\n\n" \
            "During handling of the above exception, another exception occurred:\n\n"
    assert br.signature("worker_traceback", cause + TB) == br.signature("worker_traceback", TB)


def test_signature_of_an_exit_code_and_different_kinds_never_collide():
    assert br.signature("worker_exit", exit_code=3221225477) == br.signature("worker_exit", exit_code=3221225477)
    assert br.signature("worker_exit", exit_code=2) != br.signature("worker_exit", exit_code=3)
    assert br.signature("worker_exit", exit_code=2) != br.signature("gui_exception", TB)
    assert br.signature("gui_exception", TB) != br.signature("worker_traceback", TB)


# ── 报告 ─────────────────────────────────────────────────────────────────────

_ALLOWED_KEYS = {"v", "type", "id", "ver", "os", "py", "frozen", "kind", "sig", "msg", "tb", "log", "ctx", "exit",
                 "note"}


def test_report_has_exactly_the_documented_fields():
    r = br.build_report("worker_traceback", tb=TB, log_lines=["a\n", "b\n"], ctx={"map": "garden"})
    assert set(r) <= _ALLOWED_KEYS
    assert {"v", "type", "id", "ver", "os", "py", "frozen", "kind", "sig", "msg", "tb", "log", "ctx"} <= set(r)
    assert r["v"] == 1 and r["type"] == "bug" and r["id"] == IID and r["kind"] == "worker_traceback"
    assert isinstance(r["frozen"], bool)
    assert r["msg"] == "KeyError: 'x'"
    assert r["sig"] == br.signature("worker_traceback", TB)
    assert r["log"] == "a\nb"
    json.dumps(r)                                                  # 必须能序列化


def test_report_text_fields_are_redacted():
    r = br.build_report("worker_traceback", tb=TB, aliases=["小号A"],
                        log_lines=[r"打开 C:\Users\Alice\x 里的 小号A", "联系 a@b.cc"])
    blob = json.dumps(r, ensure_ascii=False)
    for secret in ("Alice", "小号A", "a@b.cc"):
        assert secret not in blob
    assert "<user>" in r["tb"] and "<profile>" in r["log"] and "<email>" in r["log"]


def test_aliases_are_scrubbed_from_the_traceback_and_the_message_too():
    r = br.build_report("worker_traceback", tb=TB.replace("KeyError: 'x'", "KeyError: '小号A'"),
                        aliases=["小号A"])
    assert "小号A" not in r["tb"] and "小号A" not in r["msg"]
    assert r["msg"] == "KeyError: '<profile>'"


def test_the_log_tail_is_limited_by_line_count_not_only_by_characters():
    r = br.build_report("worker_exit", exit_code=2, log_lines=[f"l{i}" for i in range(500)])
    assert r["log"].splitlines() == [f"l{i}" for i in range(500 - br.TAIL_LINES, 500)]


def test_ctx_is_an_allowlist_and_never_carries_accounts_or_paths():
    block = {"id": "blk-1", "profile": "小号A", "days": [0, 1], "start": "00:00", "end": "23:59",
             "farming_area": [[1, 2], [3, 4]], "map": "garden", "enemy_ai_enabled": True,
             "auto_switch_server": False, "combat": "rules", "invert_attack": True,
             "invert_defense": False, "enemy_rules": {"species": {"rock": "ignore"},
                                                       "knobs": {"avoid_min_rarity": "Epic"}},
             "enter_game_swap": {"enabled": True}}
    r = br.build_report("worker_traceback", tb=TB, ctx=block)
    assert set(r["ctx"]) == {"map", "enemy_ai_enabled", "auto_switch_server", "combat",
                             "invert_attack", "invert_defense", "enemy_rules"}
    assert r["ctx"]["enemy_rules"] == {"species": {"rock": "ignore"}, "knobs": {"avoid_min_rarity": "Epic"}}
    assert "小号A" not in json.dumps(r, ensure_ascii=False) and "Alice" not in json.dumps(r)


def test_ctx_tolerates_none_and_unserialisable_values():
    assert br.build_report("worker_exit", exit_code=2, ctx=None)["ctx"] == {}
    r = br.build_report("worker_exit", exit_code=2, ctx={"map": object(), "combat": "rules"})
    assert r["ctx"] == {"combat": "rules"}


def test_worker_exit_report_carries_the_code_and_no_traceback():
    r = br.build_report("worker_exit", exit_code=3221225477, log_lines=["x\n"])
    assert r["exit"] == 3221225477 and r["tb"] == "" and "3221225477" in r["msg"]


def test_sizes_are_capped_and_the_tail_is_what_survives():
    lines = [f"line {i} " + "x" * 1000 for i in range(500)]
    r = br.build_report("worker_traceback", tb="Traceback (most recent call last):\n" + ("  File x\n" * 5000)
                        + "ValueError: " + "m" * 5000, log_lines=lines)
    assert len(r["tb"]) <= br.MAX_TB and len(r["msg"]) <= br.MAX_MSG
    assert len(r["log"]) <= br.MAX_LOG
    assert all(len(ln) <= br.MAX_LINE + 1 for ln in r["log"].splitlines())
    assert "line 499" in r["log"] and "line 0 " not in r["log"]
    assert len(r["log"].splitlines()) <= br.TAIL_LINES
    assert len(json.dumps(r, ensure_ascii=False).encode("utf-8")) < 28 * 1024      # 服务端上限 32 KB, 留余量


def test_report_building_never_raises_on_junk_input():
    r = br.build_report("worker_traceback", tb=None, log_lines=None, ctx="nope", exit_code="x")
    assert r["tb"] == "" and r["log"] == "" and r["ctx"] == {}


# ── 节流 ─────────────────────────────────────────────────────────────────────

def test_same_signature_is_throttled_within_the_cooldown_but_other_signatures_are_not(tmp_path):
    t = [1_000_000.0]
    th = br.Throttle(str(tmp_path / "state.json"), clock=lambda: t[0])
    assert th.allow("aaa") is True
    th.record("aaa")
    assert th.allow("aaa") is False
    assert th.allow("bbb") is True
    t[0] += br.SAME_SIG_COOLDOWN_S - 1
    assert th.allow("aaa") is False
    t[0] += 2
    assert th.allow("aaa") is True


def test_daily_cap_applies_across_signatures_and_resets_the_next_day(tmp_path):
    t = [1_000_000.0]
    th = br.Throttle(str(tmp_path / "state.json"), clock=lambda: t[0])
    for i in range(br.MAX_PER_DAY):
        assert th.allow(f"s{i}") is True
        th.record(f"s{i}")
    assert th.allow("fresh") is False
    t[0] += 86400 * 1.5
    assert th.allow("fresh") is True


def test_throttle_state_survives_a_restart(tmp_path):
    p = str(tmp_path / "state.json")
    t = [1_000_000.0]
    a = br.Throttle(p, clock=lambda: t[0])
    a.record("aaa")
    b = br.Throttle(p, clock=lambda: t[0])
    assert b.allow("aaa") is False


@pytest.mark.parametrize("content", ["", "{", "[]", '{"sigs": 5}', '{"sigs": {"a": "x"}, "n": "y"}'])
def test_corrupt_state_is_treated_as_empty(tmp_path, content):
    p = tmp_path / "state.json"
    p.write_text(content, encoding="utf-8")
    th = br.Throttle(str(p), clock=lambda: 1_000_000.0)
    assert th.allow("aaa") is True
    th.record("aaa")                                               # 写回也不抛
    assert th.allow("aaa") is False


def test_unwritable_state_never_raises_and_the_in_memory_cap_still_holds(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    th = br.Throttle(str(blocker / "sub" / "state.json"), clock=lambda: 1_000_000.0)
    for i in range(br.MAX_PER_DAY):
        assert th.allow(f"s{i}")
        th.record(f"s{i}")
    assert th.allow("more") is False


# ── 本地副本 ─────────────────────────────────────────────────────────────────

def test_local_copy_is_the_exact_payload_and_old_ones_are_pruned(tmp_path):
    payload = br.build_report("worker_traceback", tb=TB)
    path = br.save_local(str(tmp_path), payload)
    assert os.path.dirname(path) == os.path.join(str(tmp_path), "logs", "bug-reports")
    assert json.loads(open(path, encoding="utf-8").read()) == payload
    assert payload["sig"] in os.path.basename(path)
    for i in range(br.KEEP_LOCAL + 5):
        br.save_local(str(tmp_path), dict(payload, sig=f"{i:012x}"), now=lambda i=i: __import__("datetime")
                      .datetime(2026, 10, 4, 12, 0, i % 60, i))
    names = os.listdir(os.path.join(str(tmp_path), "logs", "bug-reports"))
    assert len(names) <= br.KEEP_LOCAL


def test_local_copy_failure_returns_none(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert br.save_local(str(blocker), br.build_report("worker_exit", exit_code=2)) is None


# ── prepare: 要不要弹窗问用户 ────────────────────────────────────────────────

def _th(tmp_path, t=None):
    t = t if t is not None else [1_000_000.0]
    return br.Throttle(str(tmp_path / "state.json"), clock=lambda: t[0]), t


def test_prepare_returns_the_redacted_payload_and_remembers_that_it_asked(tmp_path):
    th, _ = _th(tmp_path)
    p = br.prepare("worker_traceback", tb=TB, log_lines=["a\n"], ctx={"map": "garden"}, throttle=th)
    assert p == br.build_report("worker_traceback", tb=TB, log_lines=["a\n"], ctx={"map": "garden"})
    assert br.prepare("worker_traceback", tb=TB, throttle=th) is None          # 同一个问题问过了, 不再问
    assert br.prepare("worker_traceback", tb=TB.replace("KeyError", "IndexError"), throttle=th) is not None


def test_prepare_counts_the_ask_not_the_answer_so_declining_also_silences_it(tmp_path):
    # prepare 一返回就记了「问过」: 用户之后选不上报, 同一个问题也不会下一拍又弹出来
    th, t = _th(tmp_path)
    assert br.prepare("worker_exit", exit_code=2, throttle=th) is not None
    t[0] += br.SAME_SIG_COOLDOWN_S - 1
    assert br.prepare("worker_exit", exit_code=2, throttle=th) is None
    t[0] += 2
    assert br.prepare("worker_exit", exit_code=2, throttle=th) is not None


def test_prepare_asks_at_most_max_per_day_times(tmp_path):
    th, t = _th(tmp_path)
    for i in range(br.MAX_PER_DAY):
        assert br.prepare("worker_exit", exit_code=100 + i, throttle=th) is not None
    assert br.prepare("worker_exit", exit_code=999, throttle=th) is None
    t[0] += 86400 * 1.5
    assert br.prepare("worker_exit", exit_code=999, throttle=th) is not None


def test_prepare_is_silent_when_telemetry_is_disabled_and_leaves_no_trace(tmp_path, monkeypatch):
    monkeypatch.setenv("FLORR_TELEMETRY", "0")
    th, _ = _th(tmp_path)
    assert br.available() is False
    assert br.prepare("worker_traceback", tb=TB, throttle=th) is None
    assert not (tmp_path / "state.json").exists()
    monkeypatch.delenv("FLORR_TELEMETRY")
    assert br.available() is True
    assert br.prepare("worker_traceback", tb=TB, throttle=th) is not None      # 之前没被记「问过」


def test_prepare_ignores_unknown_kinds(tmp_path):
    th, _ = _th(tmp_path)
    assert br.prepare("nonsense", tb=TB, throttle=th) is None
    assert br.prepare("worker_traceback", tb=TB, throttle=th) is not None      # 也没被乱记


def test_prepare_never_raises(tmp_path):
    class Boom:
        def allow(self, sig):
            raise RuntimeError("x")

    assert br.prepare("worker_traceback", tb=TB, throttle=Boom()) is None
    assert br.prepare("worker_traceback", tb=TB, log_lines=None, ctx="nope", exit_code="x",
                      throttle=_th(tmp_path)[0]) is not None


def test_prepare_uses_the_per_user_state_file_by_default(tmp_path, monkeypatch):
    th, _ = _th(tmp_path)
    monkeypatch.setattr(br, "_default_throttle", lambda: th)
    assert br.prepare("worker_traceback", tb=TB) is not None
    assert br.prepare("worker_traceback", tb=TB) is None


# ── 用户补充说明 ─────────────────────────────────────────────────────────────

def test_with_note_adds_a_redacted_capped_note_without_touching_the_input():
    base = br.build_report("worker_exit", exit_code=2)
    out = br.with_note(base, "  刷到一半卡死了, 联系我 a@b.cc, 账号 小号A  ", aliases=["小号A"])
    assert out["note"] == "刷到一半卡死了, 联系我 <email>, 账号 <profile>"
    assert "note" not in base and out is not base
    assert len(br.with_note(base, "x" * 5000)["note"]) == br.MAX_NOTE


@pytest.mark.parametrize("note", ["", "   \n ", None, 5, ["x"]])
def test_with_note_adds_nothing_for_an_empty_or_non_text_note(note):
    base = br.build_report("worker_exit", exit_code=2)
    assert br.with_note(base, note) == base


# ── 预览: 用户同意之前要能看到将发送的全部内容 ───────────────────────────────

def test_the_preview_lists_every_field_that_would_be_sent():
    payload = br.with_note(br.build_report("worker_traceback", tb=TB, log_lines=["l1", "l2"],
                                           ctx={"map": "garden"}, exit_code=2), "我的说明")
    text = br.render_preview(payload)
    assert set(payload) <= _ALLOWED_KEYS
    for key in payload:
        assert f"【{br._PREVIEW_LABELS[key]}】" in text, key       # 每个字段都有标签行
    assert payload["id"] in text and payload["sig"] in text and payload["ver"] in text
    assert payload["msg"] in text and "我的说明" in text and "l1" in text and "l2" in text
    for line in payload["tb"].splitlines():
        assert line in text                                          # 堆栈每一行都在
    assert '"map": "garden"' in text


def test_the_preview_shows_unknown_extra_fields_too_so_nothing_goes_out_unseen():
    text = br.render_preview({"kind": "worker_exit", "secret_thing": "abc"})
    assert "secret_thing" in text and "abc" in text


def test_the_preview_indents_multiline_blocks_and_shows_booleans_in_words():
    text = br.render_preview({"frozen": True, "log": "a\nb"})
    assert "【打包版】 是" in text
    assert "【最近的输出】\n    a\n    b" in text
    assert "【打包版】 否" in br.render_preview({"frozen": False})


def test_the_preview_never_contains_what_redaction_removed():
    payload = br.build_report("worker_traceback", tb=TB, aliases=["小号A"],
                              log_lines=[r"C:\Users\Alice\x 小号A a@b.cc"])
    text = br.render_preview(payload)
    for secret in ("Alice", "小号A", "a@b.cc"):
        assert secret not in text


# ── submit: 用户点了「上报」 ─────────────────────────────────────────────────

def _submit(tmp_path, payload=None, **kw):
    sent, said = [], []
    payload = payload or br.build_report("worker_traceback", tb=TB, log_lines=["a"])
    kw.setdefault("post", sent.append)
    kw.setdefault("done", lambda ok, msg: said.append((ok, msg)))
    t = br.submit(payload, root=str(tmp_path), **kw)
    t.join(5)
    return payload, sent, said


def test_submit_saves_a_local_copy_posts_the_payload_with_the_note_and_says_so(tmp_path):
    payload, sent, said = _submit(tmp_path, note="我的说明 a@b.cc")
    assert len(sent) == 1 and sent[0]["note"] == "我的说明 <email>"
    assert {k: v for k, v in sent[0].items() if k != "note"} == payload
    assert "note" not in payload                                       # 入参没被改
    (ok, msg), = said
    assert ok is True and f"问题签名 {payload['sig']}" in msg and "本地副本" in msg and "bug-reports" in msg
    files = os.listdir(os.path.join(str(tmp_path), "logs", "bug-reports"))
    assert len(files) == 1
    with open(os.path.join(str(tmp_path), "logs", "bug-reports", files[0]), encoding="utf-8") as f:
        assert json.load(f) == sent[0]                                 # 副本就是发出去的那份


def test_submit_note_is_scrubbed_with_the_aliases(tmp_path):
    _, sent, _ = _submit(tmp_path, note="在 小号A 上出的", aliases=["小号A"])
    assert sent[0]["note"] == "在 <profile> 上出的"


def test_a_failed_upload_is_reported_through_done_and_the_copy_is_kept(tmp_path):
    def boom(_p):
        raise OSError("断网")

    _, _, said = _submit(tmp_path, post=boom)
    (ok, msg), = said
    assert ok is False and "上传失败" in msg and "OSError" in msg and "再点一次" in msg
    assert len(os.listdir(os.path.join(str(tmp_path), "logs", "bug-reports"))) == 1


def test_a_failed_upload_can_simply_be_submitted_again(tmp_path):
    payload = br.build_report("worker_traceback", tb=TB)
    calls = []

    def flaky(p):
        calls.append(p)
        if len(calls) == 1:
            raise OSError("断网")

    outcomes = []
    for _ in range(2):
        br.submit(payload, root=str(tmp_path), post=flaky, done=lambda ok, m: outcomes.append(ok)).join(5)
    assert outcomes == [False, True] and len(calls) == 2              # 提交本身不受冷却限制(冷却只管「要不要问」)


def test_submit_without_a_root_or_with_an_unwritable_one_still_uploads(tmp_path):
    sent, said = [], []
    br.submit(br.build_report("worker_exit", exit_code=2), root=None, post=sent.append,
              done=lambda ok, m: said.append((ok, m))).join(5)
    assert len(sent) == 1 and said[0][0] is True and "本地副本" not in said[0][1]
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    sent2, said2 = [], []
    br.submit(br.build_report("worker_exit", exit_code=2), root=str(blocker), post=sent2.append,
              done=lambda ok, m: said2.append((ok, m))).join(5)
    assert len(sent2) == 1 and said2[0][0] is True and "本地副本" not in said2[0][1]


def test_submit_survives_a_done_callback_that_raises(tmp_path):
    def bad(_ok, _msg):
        raise RuntimeError("弹窗已经关了")

    br.submit(br.build_report("worker_exit", exit_code=2), root=str(tmp_path), post=lambda p: None,
              done=bad).join(5)                    # 线程里的异常不能冒出来


def test_submit_without_a_done_callback_is_fine(tmp_path):
    sent = []
    br.submit(br.build_report("worker_exit", exit_code=2), root=str(tmp_path), post=sent.append).join(5)
    assert len(sent) == 1


def test_nothing_is_sent_or_saved_until_submit_is_called(tmp_path, monkeypatch):
    th, _ = _th(tmp_path)
    called = []
    monkeypatch.setattr(br, "_post", lambda p: called.append(p))
    assert br.prepare("worker_traceback", tb=TB, throttle=th) is not None
    assert called == [] and not os.path.exists(os.path.join(str(tmp_path), "logs"))


# ── 流水线: 泵线程里用的 StreamWatcher ───────────────────────────────────────

def test_stream_watcher_keeps_a_tail_and_hands_over_each_traceback_with_it():
    got = []
    w = br.StreamWatcher(lambda tb, tail: got.append((tb, tail)), tail_keep=5)
    for ln in ["l1\n", "l2\n"] + (TB + "\n").splitlines(keepends=True) + ["l3\n", "l4\n", "l5\n"]:
        w.feed(ln)
    w.flush()
    assert len(got) == 1
    tb, tail = got[0]
    assert tb == TB
    assert len(tail) <= 5 and tail[-1] == "l3"             # 交出去的是「识别完成那一刻」的尾巴
    assert all("\n" not in x for x in tail)


def test_stream_watcher_flush_emits_a_traceback_the_stream_ended_in():
    got = []
    w = br.StreamWatcher(lambda tb, tail: got.append(tb))
    for ln in (TB + "\n").splitlines(keepends=True):
        w.feed(ln)
    assert got == []
    w.flush()
    assert got == [TB]


def test_stream_watcher_survives_a_callback_that_raises():
    def bad(tb, tail):
        raise RuntimeError("x")

    w = br.StreamWatcher(bad)
    for ln in (TB + "\nnext\n").splitlines(keepends=True):
        w.feed(ln)                                          # 泵线程不能因此停下来
    w.flush()


# ── 哪些退出码算崩溃 / StreamWatcher 的尾巴和计数 ────────────────────────────

@pytest.mark.parametrize("code", [0, 1, -15, None, True, False, "2", 1.5])
def test_normal_or_deliberate_exits_are_not_crashes(code):
    # 0 = 正常退出; 1 = Python 未捕获异常(traceback 那条路已经报过了)或有意的 sys.exit(1)(Chrome 没就绪)、
    # Windows 上被 terminate; -15 = POSIX 上被 SIGTERM 收掉。
    assert br.is_crash_exit(code) is False


@pytest.mark.parametrize("code", [2, 3, 255, -9, -11, 3221225477, -1073741819])
def test_other_exit_codes_are_crashes(code):
    assert br.is_crash_exit(code) is True        # 含 Windows 访问违规 0xC0000005 / 段错误 / 被 SIGKILL


def test_stream_watcher_exposes_its_tail_and_how_many_tracebacks_it_handed_over():
    w = br.StreamWatcher(lambda tb, tail: None, tail_keep=3)
    assert w.tail() == [] and w.fired == 0
    for ln in ["a\n", "b\n", "c\n", "d\n"]:
        w.feed(ln)
    assert w.tail() == ["b", "c", "d"]
    for ln in (TB + "\nnext\n").splitlines(keepends=True):
        w.feed(ln)
    assert w.fired == 1
    w2 = br.StreamWatcher(lambda tb, tail: (_ for _ in ()).throw(RuntimeError("x")))
    for ln in (TB + "\nnext\n").splitlines(keepends=True):
        w2.feed(ln)
    assert w2.fired == 1                         # 回调抛了也算交出去过(不然会重复报同一个崩溃)


# ── 端到端: 真的崩一个 Python 子进程 ─────────────────────────────────────────

def test_a_really_crashing_process_becomes_exactly_one_redacted_report(tmp_path):
    import subprocess
    import sys
    # 子进程里的路径要写成原始字符串, 否则 '\\U...' 在它的源码里是非法转义, 先抛 SyntaxError 而不是我们要的 ValueError
    script = ("print(r'启动 C:\\Users\\Alice\\florr 账号 小号A')\n"
              "def f():\n    raise ValueError('boom 192.168.1.5')\n"
              "f()\n")
    proc = subprocess.Popen([sys.executable, "-u", "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    found = []
    watcher = br.StreamWatcher(lambda tb, tail: found.append((tb, tail)))
    for line in proc.stdout:                         # 跟 gui_app._pump_log 一样的喂法
        watcher.feed(line)
    watcher.flush()
    assert proc.wait() == 1 and len(found) == 1 and watcher.fired == 1
    assert br.is_crash_exit(1) is False              # 退出码 1 的崩溃走 traceback 这条路, 不会再报一次退出码

    sent = []
    th, _ = _th(tmp_path)
    tb, tail = found[0]
    payload = br.prepare("worker_traceback", tb=tb, log_lines=tail, aliases=["小号A"],
                         ctx={"map": "garden", "profile": "小号A"}, throttle=th)
    assert payload is not None and sent == []                    # 弹窗阶段: 什么都还没发
    br.submit(payload, note="在 小号A 上崩的", aliases=["小号A"], root=str(tmp_path),
              post=sent.append).join(5)
    (payload,) = sent
    blob = json.dumps(payload, ensure_ascii=False)
    for secret in ("Alice", "小号A", "192.168.1.5"):
        assert secret not in blob
    assert payload["msg"] == "ValueError: boom <ip>" and payload["kind"] == "worker_traceback"
    assert "启动 C:\\Users\\<user>\\florr 账号 <profile>" in payload["log"]
    assert payload["ctx"] == {"map": "garden"} and payload["note"] == "在 <profile> 上崩的"
    assert len(os.listdir(os.path.join(str(tmp_path), "logs", "bug-reports"))) == 1



# ── 主动反馈: 用户随时可以报, 不用等出错(kind=user_report) ───────────────────

def test_user_report_is_a_manual_kind_that_prepare_never_throttles_or_offers(tmp_path):
    assert br.MANUAL_KIND == "user_report" and br.MANUAL_KIND in br.KINDS
    assert br.MANUAL_KIND not in br.AUTO_KINDS
    assert set(br.AUTO_KINDS) | {br.MANUAL_KIND} == set(br.KINDS)
    th, _ = _th(tmp_path)
    assert br.prepare("user_report", tb=TB, throttle=th) is None      # 自动那条路不产出手动报告
    assert not (tmp_path / "state.json").exists()                      # 也没被记「问过」


def test_prepare_manual_builds_a_payload_with_environment_and_log_but_no_error(tmp_path):
    p = br.prepare_manual(log_lines=[f"l{i}\n" for i in range(100)] + ["联系 a@b.cc\n"],
                          ctx={"map": "garden", "secret": "x"}, aliases=["小号A"])
    assert p["kind"] == "user_report" and p["type"] == "bug" and p["id"] == IID
    assert p["tb"] == "" and p["msg"] == "" and "note" not in p and "exit" not in p
    assert p["log"].splitlines()[-1] == "联系 <email>" and len(p["log"].splitlines()) == br.TAIL_LINES
    assert p["ctx"] == {"map": "garden"}                               # 跟自动上报一样的白名单
    assert re.fullmatch(r"[0-9a-f]{12}", p["sig"])


def test_prepare_manual_has_no_cooldown_and_no_daily_cap(tmp_path, monkeypatch):
    # 用户主动点的, 不受「同一个问题 6 小时问一次 / 一天问 10 次」限制(那是防弹窗骚扰自己的)
    monkeypatch.setattr(br, "_default_throttle", lambda: pytest.fail("手动反馈不该碰节流"))
    for _ in range(br.MAX_PER_DAY + 3):
        assert br.prepare_manual(log_lines=["x"]) is not None


def test_prepare_manual_is_silent_when_telemetry_is_disabled(monkeypatch):
    monkeypatch.setenv("FLORR_TELEMETRY", "0")
    assert br.prepare_manual(log_lines=["x"]) is None
    monkeypatch.delenv("FLORR_TELEMETRY")
    assert br.prepare_manual(log_lines=["x"]) is not None


def test_prepare_manual_never_raises():
    assert br.prepare_manual(log_lines=None, ctx="nope", aliases=None) is not None
    assert br.prepare_manual(log_lines=5, ctx=[1], aliases=[None, 3]) is not None


def test_a_manual_reports_title_and_signature_come_from_the_users_description():
    base = br.prepare_manual(log_lines=["x"])
    out = br.with_note(base, "\n  走到一半不动了  \n第二行细节, 邮箱 a@b.cc ")
    assert out["note"] == "走到一半不动了  \n第二行细节, 邮箱 <email>"
    assert out["msg"] == "走到一半不动了"                               # 标题 = 说明的第一行(已脱敏)
    assert re.fullmatch(r"[0-9a-f]{12}", out["sig"]) and out["sig"] != base["sig"]
    assert br.with_note(base, "走到一半 不动了")["sig"] == br.with_note(base, " 走到一半  \n 不动了 ")["sig"]   # 空白折叠
    assert br.with_note(base, "走到一半不动了")["sig"] != br.with_note(base, "识别错了")["sig"]
    assert base["msg"] == "" and "note" not in base                    # 入参没被改


def test_a_manual_title_is_clipped_and_redacted_like_everything_else():
    out = br.with_note(br.prepare_manual(), "路径 C:\\Users\\Alice\\x " + "长" * 1000)
    assert "Alice" not in out["msg"] and "<user>" in out["msg"] and len(out["msg"]) <= br.MAX_MSG
    assert len(out["note"]) == br.MAX_NOTE


def test_with_note_leaves_the_signature_and_message_of_automatic_reports_alone():
    base = br.build_report("worker_traceback", tb=TB)
    out = br.with_note(base, "补充一句")
    assert out["sig"] == base["sig"] and out["msg"] == base["msg"] and out["note"] == "补充一句"


def test_the_manual_preview_calls_the_title_a_title_not_an_error():
    out = br.with_note(br.prepare_manual(log_lines=["l1"]), "卡住了")
    text = br.render_preview(out)
    assert "【标题" in text and "【错误】" not in text and "卡住了" in text
    assert "【错误】" in br.render_preview(br.build_report("worker_traceback", tb=TB))


def test_submit_refuses_a_manual_report_without_a_description_and_sends_nothing(tmp_path):
    for note in ("", "   \n", None):
        payload, sent, said = _submit(tmp_path, payload=br.prepare_manual(log_lines=["x"]), note=note)
        assert sent == [] and said[0][0] is False and "说明" in said[0][1]
    assert not os.path.exists(os.path.join(str(tmp_path), "logs"))     # 连本地副本都没存


def test_submit_sends_a_manual_report_with_its_title_and_signature(tmp_path):
    payload, sent, said = _submit(tmp_path, payload=br.prepare_manual(log_lines=["l1"]), note="索敌老是锁错怪\n细节")
    (body,) = sent
    assert body["kind"] == "user_report" and body["msg"] == "索敌老是锁错怪" and body["note"].endswith("细节")
    assert body["sig"] == br.with_note(payload, "索敌老是锁错怪\n细节")["sig"]
    assert said[0][0] is True


# ── 附件: 报告发出去后接着传最近 5 分钟的录像 / 截图 / 日志(blackbox.pack 打的 zip) ─────────────

def _zip(tmp_path, size=3 * 1024 * 1024):
    p = tmp_path / "attach-20261007-120000-000000.zip"
    p.write_bytes(b"z" * size)
    return str(p)


ATT = {"path": "/x/logs/bug-reports/attach-20261007-120000-000000.zip", "bytes": 4_800_000,
       "from": 1790000000.0, "to": 1790000300.0, "frames": 598, "shots": 60,
       "logs": ["worker-20261007-115500.log"], "window_s": 300}


def test_after_the_report_goes_through_the_attachment_follows_with_the_token(tmp_path):
    zp = _zip(tmp_path)
    puts = []
    _, sent, said = _submit(tmp_path, post=lambda p: {"id": 7, "token": "tok"}, attachment=zp,
                            put=lambda *a: puts.append(a), retry_s=0)
    assert puts == [(7, "tok", zp)]
    (ok, msg), = said
    assert ok is True and "附件" in msg and "3.0 MB" in msg


def test_an_attachment_that_will_not_upload_still_counts_as_sent_and_says_where_it_is(tmp_path):
    zp = _zip(tmp_path)
    tries = []

    def boom(*a):
        tries.append(a)
        raise OSError("断网")

    _, _, said = _submit(tmp_path, post=lambda p: {"id": 7, "token": "tok"}, attachment=zp, put=boom, retry_s=0)
    assert len(tries) == br.ATTACH_TRIES
    (ok, msg), = said
    assert ok is True and "文字已送达" in msg and "附件没传上去" in msg and "OSError" in msg and zp in msg


def test_an_old_server_without_tokens_keeps_the_attachment_local(tmp_path):
    zp = _zip(tmp_path)
    puts = []
    _, _, said = _submit(tmp_path, post=lambda p: None, attachment=zp, put=lambda *a: puts.append(a), retry_s=0)
    (ok, msg), = said
    assert ok is True and puts == [] and "附件" in msg and zp in msg


def test_a_failed_report_does_not_try_the_attachment(tmp_path):
    def boom(_p):
        raise OSError("断网")

    puts = []
    _, _, said = _submit(tmp_path, post=boom, attachment=_zip(tmp_path), put=lambda *a: puts.append(a), retry_s=0)
    assert puts == [] and said[0][0] is False and "上传失败" in said[0][1]


@pytest.mark.parametrize("ticket", [{"id": "7", "token": "t"}, {"id": 7}, {"token": "t"}, {"id": True, "token": "t"},
                                    {"id": 7, "token": 5}, [], "x"])
def test_a_strange_reply_is_treated_like_an_old_server(tmp_path, ticket):
    puts = []
    _, _, said = _submit(tmp_path, post=lambda p: ticket, attachment=_zip(tmp_path), put=lambda *a: puts.append(a),
                         retry_s=0)
    assert puts == [] and said[0][0] is True


def test_the_preview_lists_the_attachment_without_its_full_path():
    p = br.build_report("worker_traceback", tb=TB, log_lines=["a"])
    plain = br.render_preview(p)
    text = br.render_preview(p, attachment=ATT)
    assert text.startswith(plain) and "【附件(会一起发送)】" in text
    assert "598" in text and "60 张" in text and "worker-20261007-115500.log" in text and "4.6 MB" in text
    assert "截图" in text and "/x/logs" not in text
    assert "打包中" in br.render_preview(p, packing=True)
    assert br.render_preview(p, attachment=None, packing=False) == plain


def test_the_attachment_description_handles_a_logs_only_attachment():
    att = dict(ATT, frames=0, shots=0, **{"from": None, "to": None})
    text = br.describe_attachment(att)
    assert "worker-20261007-115500.log" in text and "没有录到画面" in text


class _Reply:
    def __init__(self, code, body):
        self.status, self._body = code, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("code, body, want", [
    (200, b'{"id": 3, "token": "abc"}', {"id": 3, "token": "abc"}),
    (204, b"", None), (200, b"not json", None), (200, b'{"id": 3}', None),
])
def test_post_reads_the_ticket_out_of_the_reply(monkeypatch, code, body, want):
    monkeypatch.setattr(br.urllib.request, "urlopen", lambda *a, **k: _Reply(code, body))
    assert br._post({"type": "bug"}) == want


def test_put_attachment_sends_the_zip_with_the_token_in_a_header(monkeypatch, tmp_path):
    zp = _zip(tmp_path, size=10)
    seen = []

    def fake(req, timeout=None, context=None):
        seen.append((req.get_method(), req.full_url, req.get_header("X-upload-token"), req.data, timeout))
        return _Reply(204, b"")

    monkeypatch.setattr(br.urllib.request, "urlopen", fake)
    br._put_attachment(9, "tok", zp)
    (method, url, token, data, timeout), = seen
    assert method == "PUT" and url.endswith("/api/bug/9/attachment") and token == "tok"
    assert data == b"z" * 10 and timeout == br.ATTACH_TIMEOUT_S
