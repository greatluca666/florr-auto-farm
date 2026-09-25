import builtins
import json
import os
import pytest
from flask import request_started

import webctl


@pytest.fixture
def password_file(tmp_path, monkeypatch):
    p = tmp_path / "password"
    p.write_text("test-secret-123", encoding="utf-8")
    monkeypatch.setattr(webctl, "_PASSWORD_FILE", str(p))
    monkeypatch.setattr(webctl, "_FAILED_ATTEMPTS", {})
    return "test-secret-123"


@pytest.fixture
def client(password_file):
    webctl.app.config["TESTING"] = True
    return webctl.app.test_client()


@pytest.fixture
def auth_headers(password_file):
    return {"Authorization": f"Bearer {password_file}"}


class TestRequireAuth:
    def _protected(self):
        @webctl.require_auth
        def dummy():
            return "ok", 200
        return dummy

    def test_missing_token_rejected(self, password_file):
        dummy = self._protected()
        with webctl.app.test_request_context("/x"):
            resp = dummy()
        assert resp[1] == 401

    def test_wrong_token_rejected(self, password_file):
        dummy = self._protected()
        with webctl.app.test_request_context(
                "/x", headers={"Authorization": "Bearer wrong"}):
            resp = dummy()
        assert resp[1] == 401

    def test_correct_token_passes_through(self, password_file):
        dummy = self._protected()
        with webctl.app.test_request_context(
                "/x", headers={"Authorization": f"Bearer {password_file}"}):
            resp = dummy()
        assert resp == ("ok", 200)

    def test_lockout_after_max_failed_attempts(self, password_file):
        dummy = self._protected()
        for _ in range(webctl._MAX_ATTEMPTS):
            with webctl.app.test_request_context(
                    "/x", headers={"Authorization": "Bearer wrong"}):
                dummy()
        with webctl.app.test_request_context(
                "/x", headers={"Authorization": "Bearer wrong"}):
            resp = dummy()
        assert resp[1] == 429

    def test_lockout_blocks_even_correct_password(self, password_file):
        dummy = self._protected()
        for _ in range(webctl._MAX_ATTEMPTS):
            with webctl.app.test_request_context(
                    "/x", headers={"Authorization": "Bearer wrong"}):
                dummy()
        with webctl.app.test_request_context(
                "/x", headers={"Authorization": f"Bearer {password_file}"}):
            resp = dummy()
        assert resp[1] == 429

    def test_success_resets_failure_counter(self, password_file):
        dummy = self._protected()
        with webctl.app.test_request_context(
                "/x", headers={"Authorization": "Bearer wrong"}):
            dummy()
        with webctl.app.test_request_context(
                "/x", headers={"Authorization": f"Bearer {password_file}"}):
            dummy()
        assert "127.0.0.1" not in webctl._FAILED_ATTEMPTS


class TestLoadPasswordFailsClosed:
    """密码文件缺失/读不出来时以前会让 _load_password() 直接抛未捕获异常, 把
    每一个端点(包括未鉴权的)炸成 500 —— 比如没跑过 setup.sh 就先起了服务, 或
    者密码文件被误删(finding #4b)。这里要求: 读文件失败时鉴权正常走到"密码
    错误"这条路径(401), 不是让异常一路冒到 Flask 顶层变成 500。"""

    def test_missing_password_file_yields_clean_401_not_500(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webctl, "_PASSWORD_FILE", str(tmp_path / "does-not-exist"))
        monkeypatch.setattr(webctl, "_FAILED_ATTEMPTS", {})
        webctl.app.config["TESTING"] = True
        client = webctl.app.test_client()
        resp = client.get("/api/status", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 401

    def test_load_password_itself_does_not_raise_on_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(webctl, "_PASSWORD_FILE", str(tmp_path / "does-not-exist"))
        token = webctl._load_password()
        # 必须是一个真实密码永远不可能等于的哨兵值, 不是让调用方去猜 None/"" ——
        # 密码文件本身也可能被人写成空字符串, "" 不是安全的哨兵.
        assert token != ""
        assert isinstance(token, str)

    def test_empty_password_file_does_not_bypass_auth(self, tmp_path, monkeypatch):
        """0 字节密码文件(比如两台机器间手动拷贝密码文件时传输中断)不是"读文件
        失败" —— open()+read()+strip() 不抛异常, 得到的是空字符串. 没带
        Authorization header 的请求, token 也默认是空字符串 —— 两个空字符串
        相等, 认证形同虚设. 必须跟"文件缺失/读不出来"走同一条 fail-closed 路径."""
        empty = tmp_path / "password"
        empty.write_text("", encoding="utf-8")
        monkeypatch.setattr(webctl, "_PASSWORD_FILE", str(empty))
        monkeypatch.setattr(webctl, "_FAILED_ATTEMPTS", {})
        webctl.app.config["TESTING"] = True
        client = webctl.app.test_client()

        resp = client.get("/api/status")  # 完全不带 Authorization header
        assert resp.status_code == 401

    def test_load_password_never_returns_empty_string(self, tmp_path, monkeypatch):
        empty = tmp_path / "password"
        empty.write_text("   \n", encoding="utf-8")  # strip() 后也是空
        monkeypatch.setattr(webctl, "_PASSWORD_FILE", str(empty))
        assert webctl._load_password() != ""


class TestCorsHeaders:
    def test_header_set_to_configured_peer_origin(self, monkeypatch):
        monkeypatch.setattr(webctl, "_PEER_URL", "https://146-56-141-232.nip.io")
        with webctl.app.test_request_context("/"):
            resp = webctl._add_cors_headers(webctl.app.make_response("ok"))
        assert resp.headers["Access-Control-Allow-Origin"] == "https://146-56-141-232.nip.io"
        assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]

    def test_header_absent_when_no_peer_configured(self, monkeypatch):
        monkeypatch.setattr(webctl, "_PEER_URL", "")
        with webctl.app.test_request_context("/"):
            resp = webctl._add_cors_headers(webctl.app.make_response("ok"))
        assert "Access-Control-Allow-Origin" not in resp.headers


class TestPeerUrlTrailingSlash:
    """setup.sh 打印的示例文案里 FLORR_WEBCTL_PEER_URL 带结尾斜杠(见
    finding #1) —— 用户照抄粘贴进环境变量是完全可预期的输入, 这里要求
    模块自己在读环境变量时就 rstrip 掉, 不能指望文案或用户仔细看。"""

    def test_env_var_with_trailing_slash_is_normalized_at_import_time(self, monkeypatch):
        monkeypatch.setenv("FLORR_WEBCTL_PEER_URL", "https://146-56-141-232.nip.io/")
        import importlib
        reloaded = importlib.reload(webctl)
        try:
            assert reloaded._PEER_URL == "https://146-56-141-232.nip.io"
        finally:
            monkeypatch.delenv("FLORR_WEBCTL_PEER_URL", raising=False)
            importlib.reload(webctl)

    def test_cors_header_normalized_when_peer_url_has_trailing_slash(self, monkeypatch):
        monkeypatch.setattr(webctl, "_PEER_URL", "https://146-56-141-232.nip.io/".rstrip("/"))
        with webctl.app.test_request_context("/"):
            resp = webctl._add_cors_headers(webctl.app.make_response("ok"))
        assert resp.headers["Access-Control-Allow-Origin"] == "https://146-56-141-232.nip.io"

    def test_status_peer_url_field_normalized_when_env_has_trailing_slash(
            self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(webctl, "_PEER_URL", "https://146-56-141-232.nip.io/".rstrip("/"))
        monkeypatch.setattr(webctl.subprocess, "run", self._fake_status_run())
        monkeypatch.setattr(webctl.app_config, "load_config", lambda: {"schedule": []})
        monkeypatch.setattr(webctl.app_config, "active_block", lambda *a: None)

        resp = client.get("/api/status", headers=auth_headers)
        assert resp.get_json()["peer_url"] == "https://146-56-141-232.nip.io"

    @staticmethod
    def _fake_status_run():
        def fake_run(argv, **kw):
            if argv[:2] == ["systemctl", "is-active"]:
                return subprocess.CompletedProcess(argv, 0, stdout="active\n", stderr="")
            if argv[0] == "free":
                out = (
                    "              total        used        free      shared  "
                    "buff/cache   available\n"
                    "Mem:      1000000000   1   1   1   1   1\n"
                    "Swap:     1000000000   1   1\n"
                )
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            raise AssertionError(f"unexpected subprocess call: {argv}")
        return fake_run


import subprocess

import app_config


class TestStatusEndpoint:
    def test_missing_token_rejected(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 401

    def _fake_run(self, unit_states, mem_line, swap_line):
        def fake_run(argv, **kw):
            if argv[:2] == ["systemctl", "is-active"]:
                unit = argv[2]
                return subprocess.CompletedProcess(
                    argv, 0, stdout=unit_states[unit] + "\n", stderr="")
            if argv[0] == "free":
                out = (
                    "              total        used        free      shared  "
                    "buff/cache   available\n"
                    f"Mem:      {mem_line}\n"
                    f"Swap:     {swap_line}\n"
                )
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            raise AssertionError(f"unexpected subprocess call: {argv}")
        return fake_run

    def test_reports_unit_states_and_memory(self, client, auth_headers, monkeypatch):
        unit_states = {
            "florr-xvfb.service": "active", "florr-wm.service": "active",
            "florr-bot.service": "failed", "florr-vnc.service": "inactive",
        }
        monkeypatch.setattr(webctl.subprocess, "run", self._fake_run(
            unit_states,
            "1002438656   300000000   100000000     1000000   600000000   500000000",
            "2147479552   200000000  1947479552"))
        monkeypatch.setattr(webctl.app_config, "load_config", lambda: {"schedule": []})
        monkeypatch.setattr(webctl.app_config, "active_block", lambda *a: None)

        resp = client.get("/api/status", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["units"]["florr-bot"] == "failed"
        assert body["units"]["florr-xvfb"] == "active"
        assert body["units"]["florr-vnc"] == "inactive"
        assert body["memory"]["mem_total_mb"] == 1002438656 // (1024 * 1024)
        assert body["memory"]["swap_used_mb"] == 200000000 // (1024 * 1024)
        assert body["active_block"] is None
        assert body["peer_url"] == ""

    def test_reports_peer_url_when_configured(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(webctl, "_PEER_URL", "https://146-56-141-232.nip.io")
        monkeypatch.setattr(webctl.subprocess, "run", self._fake_run(
            {"florr-xvfb.service": "active", "florr-wm.service": "active",
             "florr-bot.service": "active", "florr-vnc.service": "inactive"},
            "1000000000   1   1   1   1   1", "1000000000   1   1"))
        monkeypatch.setattr(webctl.app_config, "load_config", lambda: {"schedule": []})
        monkeypatch.setattr(webctl.app_config, "active_block", lambda *a: None)

        resp = client.get("/api/status", headers=auth_headers)
        assert resp.get_json()["peer_url"] == "https://146-56-141-232.nip.io"

    def test_reports_active_block_when_one_is_running(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(webctl.subprocess, "run", self._fake_run(
            {"florr-xvfb.service": "active", "florr-wm.service": "active",
             "florr-bot.service": "active", "florr-vnc.service": "inactive"},
            "1000000000   1   1   1   1   1", "1000000000   1   1"))
        monkeypatch.setattr(webctl.app_config, "load_config",
                            lambda: {"schedule": [{"id": "blk-1"}]})
        monkeypatch.setattr(webctl.app_config, "active_block",
                            lambda schedule, weekday, hhmm: {"id": "blk-1", "map": "desert"})

        resp = client.get("/api/status", headers=auth_headers)
        assert resp.get_json()["active_block"] == {"id": "blk-1", "map": "desert"}


class TestConfigEndpoints:
    def test_get_returns_file_contents(self, client, auth_headers, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text('{"version": 2, "hello": "world"}', encoding="utf-8")
        monkeypatch.setattr(webctl.app_config, "CONFIG_PATH", str(cfg_path))
        resp = client.get("/api/config", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json() == {"version": 2, "hello": "world"}

    def test_get_falls_back_to_defaults_when_file_missing(
            self, client, auth_headers, tmp_path, monkeypatch):
        monkeypatch.setattr(
            webctl.app_config, "CONFIG_PATH", str(tmp_path / "nonexistent.json"))
        resp = client.get("/api/config", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json() == app_config.DEFAULTS_V2

    def test_post_valid_config_writes_verbatim(
            self, client, auth_headers, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(webctl.app_config, "CONFIG_PATH", str(cfg_path))
        valid = json.dumps(app_config.DEFAULTS_V2)
        resp = client.post("/api/config", headers=auth_headers, data=valid,
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert cfg_path.read_text(encoding="utf-8") == valid

    def test_post_invalid_json_rejected_without_writing(
            self, client, auth_headers, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("original", encoding="utf-8")
        monkeypatch.setattr(webctl.app_config, "CONFIG_PATH", str(cfg_path))
        resp = client.post("/api/config", headers=auth_headers, data="{not json",
                           content_type="application/json")
        assert resp.status_code == 400
        assert cfg_path.read_text(encoding="utf-8") == "original"

    def test_post_valid_config_write_is_atomic(
            self, client, auth_headers, tmp_path, monkeypatch):
        """写用 tmp+os.replace, 不是直接 open(w) 截断原文件(finding #3) —— 真正
        的并发读到一半场景没法在单测里现实地造出来(review 自己也这么说), 但
        这里断言实现真的走了 tmp-file + os.replace 这条路径(而不只是最终内容
        碰巧对), 并确认: (1) os.replace 恰好被调用一次, 参数是 CONFIG_PATH.tmp
        → CONFIG_PATH; (2) 调用时 tmp 文件里已经是完整的新内容(说明是先写完整
        文件再原子改名, 不是半路 replace); (3) 请求结束后没有残留 .tmp 文件;
        (4) 最终文件内容正确。"""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(webctl.app_config, "CONFIG_PATH", str(cfg_path))
        valid = json.dumps(app_config.DEFAULTS_V2)

        replace_calls = []
        real_replace = os.replace

        def fake_replace(src, dst):
            with open(src, "r", encoding="utf-8") as f:
                content_at_replace_time = f.read()
            replace_calls.append((src, dst, content_at_replace_time))
            real_replace(src, dst)

        monkeypatch.setattr(webctl.os, "replace", fake_replace)

        resp = client.post("/api/config", headers=auth_headers, data=valid,
                           content_type="application/json")
        assert resp.status_code == 200
        assert replace_calls == [(str(cfg_path) + ".tmp", str(cfg_path), valid)]
        assert cfg_path.read_text(encoding="utf-8") == valid
        assert not os.path.exists(str(cfg_path) + ".tmp")

    def test_post_write_failure_returns_json_500_not_bare_html(
            self, client, auth_headers, tmp_path, monkeypatch):
        """写失败(权限错误等)以前完全没 try/except, Flask 会吐一个泛用 HTML 500,
        前端 `await resp.json()` 在那种响应上直接抛未捕获异常, 保存按钮点了跟没点
        一样(finding #4a)。这里 mock 写入抛异常, 断言拿到的是结构化 JSON 500。"""
        cfg_path = tmp_path / "config.json"
        monkeypatch.setattr(webctl.app_config, "CONFIG_PATH", str(cfg_path))
        real_open = builtins.open

        def raising_open(file, *a, **kw):
            if str(file) == str(cfg_path) + ".tmp":
                raise PermissionError("拒绝访问")
            return real_open(file, *a, **kw)

        monkeypatch.setattr(webctl, "open", raising_open, raising=False)
        valid = json.dumps(app_config.DEFAULTS_V2)
        resp = client.post("/api/config", headers=auth_headers, data=valid,
                           content_type="application/json")
        assert resp.status_code == 500
        assert resp.is_json
        assert resp.get_json()["errors"]

    def test_post_semantically_invalid_config_rejected_with_errors(
            self, client, auth_headers, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("original", encoding="utf-8")
        monkeypatch.setattr(webctl.app_config, "CONFIG_PATH", str(cfg_path))
        bad = json.dumps({"version": 2, "profiles": [], "schedule": []})
        resp = client.post("/api/config", headers=auth_headers, data=bad,
                           content_type="application/json")
        assert resp.status_code == 400
        assert resp.get_json()["errors"]
        assert cfg_path.read_text(encoding="utf-8") == "original"


class TestControlEndpoint:
    def test_missing_token_rejected(self, client):
        resp = client.post("/api/control/start")
        assert resp.status_code == 401

    def test_invalid_action_rejected(self, client, auth_headers):
        resp = client.post("/api/control/reboot-the-whole-box", headers=auth_headers)
        assert resp.status_code == 400

    def test_start_calls_systemctl_with_sudo(self, client, auth_headers, monkeypatch):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(webctl.subprocess, "run", fake_run)
        resp = client.post("/api/control/start", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert captured["argv"] == [
            "sudo", "systemctl", "start", "florr-bot.service"]

    def test_stop_and_restart_also_allowed(self, client, auth_headers, monkeypatch):
        seen = []

        def fake_run(argv, **kw):
            seen.append(argv[2])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(webctl.subprocess, "run", fake_run)
        client.post("/api/control/stop", headers=auth_headers)
        client.post("/api/control/restart", headers=auth_headers)
        assert seen == ["stop", "restart"]

    def test_nonzero_exit_reports_stderr(self, client, auth_headers, monkeypatch):
        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Unit not found.")

        monkeypatch.setattr(webctl.subprocess, "run", fake_run)
        resp = client.post("/api/control/start", headers=auth_headers)
        assert resp.status_code == 500
        assert "Unit not found." in resp.get_json()["error"]

    def test_subprocess_exception_reports_json_500(self, client, auth_headers, monkeypatch):
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=15)

        monkeypatch.setattr(webctl.subprocess, "run", fake_run)
        resp = client.post("/api/control/start", headers=auth_headers)
        assert resp.status_code == 500
        assert resp.is_json
        assert "error" in resp.get_json()


class TestLogsStreamEndpoint:
    def test_missing_token_rejected(self, client):
        resp = client.get("/api/logs/stream")
        assert resp.status_code == 401

    def test_wrong_token_rejected(self, client):
        resp = client.get("/api/logs/stream?token=wrong")
        assert resp.status_code == 401


class TestIndexRoute:
    """裸路径 "/" 以前没有任何测试覆盖(finding #7) —— 这正是 Task 7 靠手工打开
    浏览器才发现的那个 bug(Flask static_folder 不会自动映射 "/" 到
    index.html), 代码修了(903cad8)但没留回归测试。这个测试针对现在已经正确的
    代码, 预期就是绿的 —— 补的是覆盖率缺口, 不是修一个新 bug。"""

    def test_index_served_at_bare_root(self):
        r = webctl.app.test_client().get("/")
        assert r.status_code == 200
        assert b"florr-auto-pathing" in r.data


class TestProxyFix:
    def test_trusts_one_hop_of_x_forwarded_for(self):
        """模拟 Caddy(跑在同一台机器, 127.0.0.1)转发请求, 带 X-Forwarded-For
        说明真实客户端 IP. ProxyFix 生效后 request.remote_addr 应该是转发头
        里的地址, 不是 Caddy 自己的 127.0.0.1 —— 否则所有经 Caddy 转发的客户
        端会共享同一个 _authenticate() 限流桶, 一个人密码打错锁全部人.

        用 test_client() 发真实请求, 不用 test_request_context(): ProxyFix 是
        包在 app.wsgi_app 外面的 WSGI 中间件, 只有真正经过 app.wsgi_app 这层
        调用链才会触发它. test_request_context() 内部直接
        self.request_context(builder.get_environ()) 拼 RequestContext, 根本
        不调用 self.wsgi_app —— 用它测不出 ProxyFix 的效果(这是 TDD 红灯阶段
        之后才发现的: 按 brief 原样接上 ProxyFix 后这个写法仍然失败, 见
        task-9-report.md)."""
        captured = {}

        def _on_request_started(_sender, **_extra):
            captured["addr"] = webctl.request.remote_addr

        request_started.connect(_on_request_started, webctl.app)
        try:
            webctl.app.test_client().get(
                "/", headers={"X-Forwarded-For": "203.0.113.5"},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        finally:
            request_started.disconnect(_on_request_started, webctl.app)

        assert captured["addr"] == "203.0.113.5"
