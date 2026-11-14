"""凭据安全的测试。

这一组测的不是功能，是「key 会不会漏」。每个用例对应一条真实的泄漏路径：
界面回显、接口响应、错误信息、文件权限、跨站请求。
"""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from polylogue import config, credentials
from polylogue.web import MAX_BODY, _scrub

from .test_web import Live

ROOT = Path(__file__).resolve().parent.parent

# 一个形状真实的假 key。用它来断言「这个串不能出现在任何地方」。
FAKE_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"


class TestMask(unittest.TestCase):
    def test_long_key_shows_at_most_six_chars(self):
        masked = credentials.mask(FAKE_KEY)
        self.assertLessEqual(len(masked), 8)
        self.assertNotIn(FAKE_KEY[4:], masked)

    def test_short_key_fully_hidden(self):
        """短串露 6 个字符可能就把大半内容给出去了，所以短的一律全藏。"""
        for value in ("abc", "short-key-12345", "x" * 16):
            self.assertEqual(credentials.mask(value), "…")

    def test_empty(self):
        self.assertEqual(credentials.mask(""), "")

    def test_mask_is_stable(self):
        self.assertEqual(credentials.mask(FAKE_KEY), credentials.mask(FAKE_KEY))


class TestRedact(unittest.TestCase):
    def test_known_key_removed(self):
        text = f"请求失败：Authorization: Bearer {FAKE_KEY}"
        out = credentials.redact(text, [FAKE_KEY])
        self.assertNotIn(FAKE_KEY, out)
        self.assertIn("已隐藏", out)

    def test_unknown_key_shape_removed(self):
        """上游错误响应体里可能回显我们不认识的凭据，正则兜底。"""
        text = "upstream said: sk-live-abcdefghijklmnopqrstuvwxyz is invalid"
        out = credentials.redact(text, [])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", out)

    def test_normal_text_untouched(self):
        text = "这个方案有个竞态：两个线程同时写配置"
        self.assertEqual(credentials.redact(text, []), text)

    def test_none_and_empty(self):
        self.assertIsNone(credentials.redact(None))
        self.assertEqual(credentials.redact(""), "")

    def test_short_secret_not_mangled(self):
        """太短的值不当 secret 替换，否则会误伤普通文本。"""
        text = "用了 abc 这个值"
        self.assertEqual(credentials.redact(text, ["abc"]), text)

    def test_scrub_walks_structures(self):
        data = {"a": [{"b": FAKE_KEY}], "c": 1, "d": None}
        out = _scrub(data)
        self.assertNotIn(FAKE_KEY, json.dumps(out, ensure_ascii=False))
        self.assertEqual(out["c"], 1)


class TestNoLeakOverHttp(unittest.TestCase):
    """跑一个真服务，把每个 GET 接口的原始响应都扫一遍。"""

    @classmethod
    def setUpClass(cls):
        cls.live = Live(with_key=True)
        cls.live.req("POST", "/api/setup", {"provider": "openrouter", "api_key": FAKE_KEY})

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def _raw(self, path):
        conn = http.client.HTTPConnection(self.live.host, timeout=30)
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return body.decode("utf-8", "replace")

    def test_info_never_contains_full_key(self):
        body = self._raw("/api/info")
        self.assertNotIn(FAKE_KEY, body, "接口把完整 key 吐出来了")
        self.assertIn("…", body, "应该给掩码")

    def test_conversation_never_contains_key(self):
        for row in json.loads(self._raw("/api/conversations"))["conversations"]:
            body = self._raw(f"/api/conversations/{row['id']}")
            self.assertNotIn(FAKE_KEY, body)

    def test_export_never_contains_key(self):
        for row in json.loads(self._raw("/api/conversations"))["conversations"]:
            self.assertNotIn(FAKE_KEY, self._raw(f"/api/conversations/{row['id']}/export"))

    def test_search_never_contains_key(self):
        self.assertNotIn(FAKE_KEY, self._raw("/api/search?q=sk"))

    def test_avatar_never_contains_key(self):
        self.assertNotIn(FAKE_KEY, self._raw("/api/avatar/claude"))

    def test_setup_response_has_no_key(self):
        status, data = self.live.req(
            "POST", "/api/setup", {"provider": "openrouter", "api_key": FAKE_KEY}
        )
        self.assertIn(status, (200, 201))
        self.assertNotIn(FAKE_KEY, json.dumps(data, ensure_ascii=False))

    def test_error_message_is_redacted(self):
        """让上游把 key 回显进错误信息，确认发到前端时已经擦掉。

        必须真的走「说话」这条路 —— 只建会话不会构建 provider，
        错误分支根本不会触发，那样的测试是假通过。
        """
        from polylogue import providers

        status, data = self.live.req(
            "POST", "/api/conversations", {"title": "红队", "members": ["claude"]}
        )
        cid = data.get("id") or data.get("conversation", {}).get("id")
        self.assertIsNotNone(cid, f"会话没建出来：{data}")

        boom = providers.ProviderError(f"401 from upstream: token {FAKE_KEY} invalid")
        with mock.patch.object(providers, "build_provider", side_effect=boom):
            status, stream = self.live.req(
                "POST", f"/api/conversations/{cid}/say", {"text": "你好"}
            )

        self.assertEqual(status, 200, f"SSE 没起来：{stream}")
        self.assertIsInstance(stream, str)
        self.assertNotIn(FAKE_KEY, stream, "错误信息把 key 带到前端了")
        # 同时确认错误确实发生了（否则这条测试又是在测空气）
        self.assertIn("error", stream, f"没有触发错误分支，测试无效：{stream[:300]}")


class TestCsrf(unittest.TestCase):
    """跨站请求必须被挡住 —— 这是「key 被改写 / 历史被读走」的主路径。"""

    @classmethod
    def setUpClass(cls):
        cls.live = Live()

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def _send(self, method, path, body, headers):
        conn = http.client.HTTPConnection(self.live.host, timeout=30)
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw) if raw else {}
        except ValueError:
            return r.status, raw.decode("utf-8", "replace")

    def test_plain_text_post_rejected(self):
        """表单能发 text/plain，且不触发预检 —— 必须挡住。"""
        status, data = self._send(
            "POST",
            "/api/setup",
            json.dumps({"provider": "openrouter", "api_key": "sk-evil-000000000000"}),
            {"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertIn("json", json.dumps(data).lower())

    def test_form_urlencoded_post_rejected(self):
        status, _ = self._send(
            "POST",
            "/api/setup",
            "provider=openrouter&api_key=sk-evil",
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 415)

    def test_multipart_post_rejected(self):
        status, _ = self._send(
            "POST", "/api/setup", "x", {"Content-Type": "multipart/form-data; boundary=x"}
        )
        self.assertEqual(status, 415)

    def test_cross_origin_post_rejected(self):
        status, _ = self._send(
            "POST",
            "/api/setup",
            json.dumps({"provider": "openrouter", "api_key": "sk-evil-000000000000"}),
            {"Content-Type": "application/json", "Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)

    def test_cross_origin_get_rejected(self):
        """DNS rebinding 读历史这条路。"""
        status, _ = self._send("GET", "/api/conversations", None, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_foreign_host_rejected(self):
        """恶意域名解析到 127.0.0.1 时，Host 头会是那个域名。"""
        status, _ = self._send(
            "POST",
            "/api/setup",
            json.dumps({"provider": "openrouter", "api_key": "sk-evil-000000000000"}),
            {"Content-Type": "application/json", "Host": "evil.example"},
        )
        self.assertEqual(status, 403)

    def test_loopback_origin_allowed(self):
        """自己页面的同源 POST 会带 Origin，不能被误杀。"""
        origin = f"http://{self.live.host}"
        status, _ = self._send(
            "POST",
            "/api/conversations",
            json.dumps({"title": "同源", "members": ["claude"]}),
            {"Content-Type": "application/json", "Origin": origin},
        )
        self.assertIn(status, (200, 201))

    def test_delete_without_body_allowed(self):
        """DELETE 没有 body 就不带 Content-Type，不能因此被挡。"""
        status, data = self.live.req("POST", "/api/conversations", {"title": "待删", "members": ["claude"]})
        cid = data.get("id") or data.get("conversation", {}).get("id")
        self.assertIsNotNone(cid)
        status, _ = self.live.req("DELETE", f"/api/conversations/{cid}")
        self.assertEqual(status, 200)

    def test_oversized_body_rejected(self):
        """只发头不发体。

        发真 body 的话，服务端在读完之前就拒绝并关连接，客户端还在 sendall
        就会 BrokenPipeError —— 那是 TCP 的正常行为，不是产品问题，
        但会让测试在不同平台上时红时绿。
        """
        conn = http.client.HTTPConnection(self.live.host, timeout=30)
        conn.putrequest("POST", "/api/setup")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_BODY + 1024))
        conn.endheaders()
        r = conn.getresponse()
        status = r.status
        r.read()
        conn.close()
        self.assertEqual(status, 413, "超大请求体应该在闸门就被拒绝")


class TestFilePermissions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.json"
        self.patch = mock.patch.object(config, "CONFIG_PATH", self.cfg)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_posix_mode_is_600(self):
        config.save({"api_keys": {"openrouter": FAKE_KEY}})
        if sys.platform == "win32":
            self.skipTest("Windows 上没有 POSIX 权限位，看 ACL 的那个用例")
        mode = self.cfg.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"配置里有明文 key，权限却是 {oct(mode)}")

    @unittest.skipUnless(sys.platform == "win32", "只在 Windows 上有 ACL")
    def test_windows_acl_restricted(self):
        """Windows 上 chmod 基本是空操作，必须靠 icacls 才真的挡住其他账户。"""
        config.save({"api_keys": {"openrouter": FAKE_KEY}})
        user = os.environ.get("USERNAME", "")
        out = subprocess.run(
            ["icacls", str(self.cfg)], capture_output=True, text=True, timeout=30
        ).stdout
        self.assertIn(user, out, "当前用户必须还有权限，否则自己也读不了")
        self.assertNotIn("Everyone", out, "Everyone 还能读，等于没保护")
        self.assertNotIn("BUILTIN\\Users", out, "Users 组还能读，等于没保护")

    def test_atomic_write_leaves_no_tmp(self):
        config.save({"api_keys": {"openrouter": FAKE_KEY}})
        leftovers = list(Path(self.tmp.name).glob("*.tmp"))
        self.assertEqual(leftovers, [], f"残留临时文件：{leftovers}")

    def test_save_key_preserves_other_fields(self):
        """save_key 以前直接 write_text，会顺手把别的字段写坏。"""
        config.save({"api_keys": {}, "contacts": [{"id": "claude"}], "theme": "light"})
        with mock.patch.object(credentials, "OWN_CONFIG", self.cfg):
            credentials.save_key("openrouter", FAKE_KEY)
        data = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["api_keys"]["openrouter"], FAKE_KEY)
        self.assertEqual(data["contacts"], [{"id": "claude"}], "通讯录被写丢了")
        self.assertEqual(data["theme"], "light")

    def test_save_key_empty_removes(self):
        config.save({"api_keys": {"openrouter": FAKE_KEY}})
        with mock.patch.object(credentials, "OWN_CONFIG", self.cfg):
            credentials.save_key("openrouter", "")
        data = json.loads(self.cfg.read_text(encoding="utf-8"))
        self.assertNotIn("openrouter", data["api_keys"])


class TestFrontendMarkup(unittest.TestCase):
    """前端不能把 key 暴露在屏幕上或浏览器存储里。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "polylogue" / "static" / "index.html").read_text(encoding="utf-8")

    def test_key_input_is_password(self):
        self.assertIn('id="onbKey" type="password"', self.html)

    def test_autocomplete_disabled(self):
        """password 类型光靠 autocomplete="off" 有的浏览器不认，要用 new-password。"""
        self.assertIn('autocomplete="new-password"', self.html)

    def test_no_password_manager_hooks(self):
        self.assertIn('data-lpignore="true"', self.html)
        self.assertIn('data-1p-ignore="true"', self.html)

    def test_spellcheck_off(self):
        self.assertIn('spellcheck="false"', self.html)

    def test_input_cleared_on_submit(self):
        """提交后必须立刻清空，不能让它停在 DOM 里。"""
        idx = self.html.index("async function submitOnb")
        block = self.html[idx : idx + 900]
        self.assertIn('$("onbKey").value = ""', block)

    def test_key_not_put_in_storage(self):
        for store_name in ("localStorage", "sessionStorage", "document.cookie"):
            for line in self.html.splitlines():
                if store_name in line and "onbKey" in line:
                    self.fail(f"key 进了浏览器存储：{line.strip()}")

    def test_toggle_defaults_to_hidden(self):
        self.assertIn("function hideKey", self.html)
        self.assertIn('i.type = "password"', self.html)


class TestCliHiddenInput(unittest.TestCase):
    def test_login_has_no_argv_key_option(self):
        """命令行参数会留在 shell 历史和进程列表里，不能作为输入方式。"""
        import argparse

        from polylogue import cli

        with mock.patch.object(argparse.ArgumentParser, "parse_args") as parse:
            parse.return_value = argparse.Namespace(
                whoami=False, forget=None, login=None, list_providers=False,
                models="", model=[], provider=[], roles=None, app=False, web=False,
                max_rounds=6, max_calls=24, max_cost=None, port=8765,
                sequential=False, quiet=False, prompt=None,
            )
            try:
                cli.main([])
            except SystemExit:
                pass
        # 解析器本身不接受 --key / --api-key
        import inspect

        src = inspect.getsource(cli.main)
        self.assertNotIn('"--key"', src)
        self.assertNotIn('"--api-key"', src)

    def test_prompt_secret_uses_getpass(self):
        import inspect

        src = inspect.getsource(credentials.prompt_secret)
        self.assertIn("getpass", src)


class TestPromptSecret(unittest.TestCase):
    """无回显输入。

    Windows 上 getpass 只要 `sys.stdin is sys.__stdin__` 就会去读真实键盘、
    忽略管道 —— 输入被重定向时会直接卡死。所以非终端路径必须自己处理。
    """

    def test_piped_input_reads_line(self):
        import io

        with mock.patch("sys.stdin", io.StringIO("sk-test-123\n")):
            self.assertEqual(credentials.prompt_secret(), "sk-test-123")

    def test_piped_empty_line_returns_empty(self):
        import io

        with mock.patch("sys.stdin", io.StringIO("\n")):
            self.assertEqual(credentials.prompt_secret(), "")

    def test_piped_eof_returns_empty(self):
        import io

        with mock.patch("sys.stdin", io.StringIO("")):
            self.assertEqual(credentials.prompt_secret(), "")

    def test_interactive_path_uses_getpass(self):
        fake = mock.Mock()
        fake.isatty.return_value = True
        with mock.patch("sys.stdin", fake), mock.patch.object(
            credentials.getpass, "getpass", return_value="  sk-x  "
        ) as gp:
            self.assertEqual(credentials.prompt_secret(), "sk-x")
            gp.assert_called_once()

    def test_getpass_failure_returns_empty_not_crash(self):
        fake = mock.Mock()
        fake.isatty.return_value = True
        with mock.patch("sys.stdin", fake), mock.patch.object(
            credentials.getpass, "getpass", side_effect=EOFError
        ):
            self.assertEqual(credentials.prompt_secret(), "")

    def test_warns_when_echo_cannot_be_disabled(self):
        import io

        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("k\n")), mock.patch("sys.stderr", err):
            credentials.prompt_secret()
        self.assertIn("关不掉回显", err.getvalue(), "不能假装安全")


if __name__ == "__main__":
    unittest.main()
