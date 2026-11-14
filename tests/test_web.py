"""工作台 HTTP 接口的测试。

用真的 HTTP 服务（端口 0 让系统分配），不是 mock —— 之前出过
「接口改了但测试还在测旧路由」的问题，端到端才能抓到。
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from polylogue import avatars, config, credentials, store
from polylogue.web import serve

ROOT = Path(__file__).resolve().parent.parent


class FakeUpstream:
    """假的上游模型服务。返回 OpenAI 兼容结构，不联网。"""

    def __init__(self, reply="好的，我看到了。"):
        self.reply = reply
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append(body)
                data = json.dumps(
                    {
                        "choices": [{"message": {"content": outer.reply}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Live:
    """起一个隔离的工作台（独立配置目录 + 摘掉 API key 的环境变量）。

    注意：只摘 API key，**不要** `clear=True` 清空整个环境。
    Windows 的 winsock / OpenSSL 初始化依赖 SYSTEMROOT，清空后连 socket
    都建不起来（WinError 10106），看起来像产品挂了，其实是测试把环境拆了。
    """

    KEY_VARS = (
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY",
        "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY", "LLM_API_KEY",
    )

    def __init__(self, with_key=False):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = base / "config.json"

        self.patches = [
            mock.patch.object(config, "CONFIG_PATH", self.cfg),
            mock.patch.object(credentials, "DSH_CREDENTIALS", base / "none.yaml"),
            mock.patch.object(credentials, "OWN_CONFIG", self.cfg),
        ]
        for p in self.patches:
            p.start()

        env = {k: v for k, v in os.environ.items() if k not in self.KEY_VARS}
        if with_key:
            env["OPENROUTER_API_KEY"] = "sk-or-v1-test"
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

        self.ready = threading.Event()
        holder = {}
        self.thread = threading.Thread(
            target=serve,
            kwargs={"port": 0, "on_ready": lambda url: (holder.__setitem__("u", url), self.ready.set())},
            daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(20):
            raise RuntimeError("工作台没起来")
        self.host = holder["u"].split("://")[1]

    def req(self, method, path, body=None, timeout=120):
        conn = http.client.HTTPConnection(self.host, timeout=timeout)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        # http.client 只接受 latin-1，中文路径必须自己编码
        conn.request(method, quote(path, safe="/?=&%:"), body=payload, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        ctype = r.getheader("Content-Type") or ""
        conn.close()
        if "json" in ctype:
            return r.status, (json.loads(raw) if raw else {})
        return r.status, raw.decode("utf-8", "replace")

    def stop(self):
        self.env.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class TestStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = Live()

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def test_index_served(self):
        status, html = self.live.req("GET", "/")
        self.assertEqual(status, 200)
        for marker in ("polylogue", 'id="log"', 'id="list"', 'id="btnTheme"'):
            self.assertIn(marker, html)

    def test_no_external_resources(self):
        """离线可用是硬要求：不能引 CDN、外部字体、外部图片。"""
        status, html = self.live.req("GET", "/")
        self.assertEqual(status, 200)
        for bad in ("http://", "https://", "//cdn.", "@import url("):
            self.assertNotIn(bad, html, f"前端引了外部资源：{bad}")

    def test_has_both_themes(self):
        status, html = self.live.req("GET", "/")
        self.assertIn('data-theme="dark"', html)
        self.assertIn(':root[data-theme="light"]', html)


class TestInfoAndContacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = Live()

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def test_info_shape(self):
        status, info = self.live.req("GET", "/api/info")
        self.assertEqual(status, 200)
        for key in ("contacts", "categories", "conversations", "providers", "theme", "price_table"):
            self.assertIn(key, info)
        self.assertGreaterEqual(len(info["contacts"]), 8)
        self.assertIn("常用", info["categories"])

    def test_onboarding_when_no_key(self):
        status, info = self.live.req("GET", "/api/info")
        self.assertTrue(info["onboarding"], "没有 key 时应该进引导模式")

    def test_default_contacts_have_ids(self):
        status, info = self.live.req("GET", "/api/info")
        ids = [c["id"] for c in info["contacts"]]
        for expected in ("claude", "gpt", "gemini", "grok", "deepseek"):
            self.assertIn(expected, ids)

    def test_add_contact(self):
        status, c = self.live.req(
            "POST", "/api/contacts",
            {"name": "测试机器人", "provider": "openrouter", "model": "a/b", "category": "测试"},
        )
        self.assertEqual(status, 201, msg=str(c))
        self.assertEqual(c["name"], "测试机器人")
        status, info = self.live.req("GET", "/api/info")
        self.assertIn(c["id"], [x["id"] for x in info["contacts"]])
        self.assertIn("测试", info["categories"])

    def test_add_contact_needs_name(self):
        status, err = self.live.req("POST", "/api/contacts", {"provider": "openrouter"})
        self.assertEqual(status, 400)

    def test_update_contact(self):
        status, c = self.live.req(
            "POST", "/api/contacts", {"name": "改我", "provider": "openrouter", "model": "a/b"}
        )
        status, got = self.live.req("PATCH", f"/api/contacts/{c['id']}", {"name": "改好了"})
        self.assertEqual(status, 200)
        self.assertEqual(got["name"], "改好了")

    def test_delete_contact(self):
        status, c = self.live.req(
            "POST", "/api/contacts", {"name": "删我", "provider": "openrouter", "model": "a/b"}
        )
        status, ok = self.live.req("DELETE", f"/api/contacts/{c['id']}")
        self.assertEqual(status, 200)
        self.assertTrue(ok["ok"])
        status, info = self.live.req("GET", "/api/info")
        self.assertNotIn(c["id"], [x["id"] for x in info["contacts"]])


class TestConversations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = Live()

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def test_create_group(self):
        status, c = self.live.req(
            "POST", "/api/conversations", {"title": "群聊", "members": ["claude", "gpt"]}
        )
        self.assertEqual(status, 201, msg=str(c))
        self.assertEqual(c["kind"], "group")
        self.assertEqual(c["members"], ["claude", "gpt"])

    def test_create_dm_auto_title(self):
        status, c = self.live.req("POST", "/api/conversations", {"members": ["gemini"]})
        self.assertEqual(status, 201)
        self.assertEqual(c["kind"], "dm")
        self.assertEqual(c["title"], "Gemini", "单聊没给名字时应该用成员名")

    def test_create_needs_member(self):
        status, err = self.live.req("POST", "/api/conversations", {"members": []})
        self.assertEqual(status, 400)
        status, err = self.live.req("POST", "/api/conversations", {"members": ["不存在的人"]})
        self.assertEqual(status, 400)

    def test_get_and_rename(self):
        status, c = self.live.req("POST", "/api/conversations", {"title": "旧名", "members": ["claude"]})
        status, got = self.live.req("GET", f"/api/conversations/{c['id']}")
        self.assertEqual(got["title"], "旧名")
        status, renamed = self.live.req("PATCH", f"/api/conversations/{c['id']}", {"title": "新名"})
        self.assertEqual(status, 200)
        self.assertEqual(renamed["title"], "新名")
        status, again = self.live.req("GET", f"/api/conversations/{c['id']}")
        self.assertEqual(again["title"], "新名")

    def test_rename_category(self):
        status, c = self.live.req("POST", "/api/conversations", {"title": "x", "members": ["claude"]})
        status, got = self.live.req(
            "PATCH", f"/api/conversations/{c['id']}", {"title": "x", "category": "工作"}
        )
        self.assertEqual(got["category"], "工作")

    def test_delete(self):
        status, c = self.live.req("POST", "/api/conversations", {"title": "删", "members": ["claude"]})
        status, ok = self.live.req("DELETE", f"/api/conversations/{c['id']}")
        self.assertTrue(ok["ok"])
        status, _ = self.live.req("GET", f"/api/conversations/{c['id']}")
        self.assertEqual(status, 404)

    def test_missing_returns_404(self):
        for method, path in [
            ("GET", "/api/conversations/nope"),
            ("PATCH", "/api/conversations/nope"),
            ("DELETE", "/api/conversations/nope"),
        ]:
            status, _ = self.live.req(method, path, {"title": "x"} if method == "PATCH" else None)
            self.assertEqual(status, 404, f"{method} {path}")

    def test_path_traversal_blocked(self):
        status, _ = self.live.req("GET", "/api/conversations/..%2F..%2Fetc%2Fpasswd")
        self.assertIn(status, (400, 404))

    def test_export(self):
        status, c = self.live.req("POST", "/api/conversations", {"title": "导出我", "members": ["claude"]})
        conv = store.load_conversation(c["id"])
        conv["turns"] = [{"speaker": "Claude", "role": "assistant", "content": "要被导出的话"}]
        store.save_conversation(conv)
        status, md = self.live.req("GET", f"/api/conversations/{c['id']}/export")
        self.assertEqual(status, 200)
        self.assertIn("导出我", md)
        self.assertIn("要被导出的话", md)


class TestAvatars(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = Live()

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def test_builtin_svg(self):
        for key in ("openai", "claude", "gemini", "grok", "deepseek", "qwen", "kimi", "glm"):
            status, body = self.live.req("GET", f"/api/avatar/{key}")
            self.assertEqual(status, 200, key)
            self.assertIn("<svg", body, key)
            self.assertIn("viewBox", body, key)

    def test_unknown_gets_generic(self):
        status, body = self.live.req("GET", "/api/avatar/完全不存在")
        self.assertEqual(status, 200)
        self.assertEqual(body, avatars.AVATARS["generic"])

    def test_contact_id_avatar(self):
        status, body = self.live.req("GET", "/api/avatar/claude")
        self.assertIn("<svg", body)

    def test_upload_overrides(self):
        uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
        status, ok = self.live.req("POST", "/api/avatar/kimi", {"data": uri})
        self.assertEqual(status, 200, msg=str(ok))
        status, body = self.live.req("GET", "/api/avatar/kimi")
        self.assertEqual(status, 200)
        # 上传后返回的是二进制图片，不再是 SVG
        self.assertNotIn("<svg", body if isinstance(body, str) else "")

    def test_upload_rejects_url(self):
        status, err = self.live.req("POST", "/api/avatar/grok", {"data": "https://evil/x.png"})
        self.assertEqual(status, 400)


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = Live()

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def test_empty_query(self):
        status, d = self.live.req("GET", "/api/search?q=")
        self.assertEqual(status, 200)
        self.assertEqual(d["hits"], [])

    def test_finds_history(self):
        status, c = self.live.req("POST", "/api/conversations", {"title": "搜我", "members": ["claude"]})
        conv = store.load_conversation(c["id"])
        conv["turns"] = [{"speaker": "Claude", "role": "assistant", "content": "这里讨论了连接池泄漏"}]
        store.save_conversation(conv)

        status, d = self.live.req("GET", "/api/search?q=连接池泄漏")
        self.assertEqual(status, 200)
        self.assertEqual(len(d["hits"]), 1)
        self.assertEqual(d["hits"][0]["conversation"], c["id"])

    def test_no_match(self):
        status, d = self.live.req("GET", "/api/search?q=绝对不存在的词xyz")
        self.assertEqual(d["hits"], [])


class TestSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live = Live()

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def test_theme_persists(self):
        status, d = self.live.req("POST", "/api/settings", {"theme": "light"})
        self.assertEqual(status, 200)
        self.assertEqual(d["info"]["theme"], "light")
        status, info = self.live.req("GET", "/api/info")
        self.assertEqual(info["theme"], "light")

    def test_theme_rejects_garbage(self):
        self.live.req("POST", "/api/settings", {"theme": "rainbow"})
        status, info = self.live.req("GET", "/api/info")
        self.assertIn(info["theme"], ("dark", "light"))

    def test_context_and_budget(self):
        status, d = self.live.req(
            "POST",
            "/api/settings",
            {"context": {"keep_recent": 4, "budget_tokens": 2000, "enabled": True},
             "budget": {"max_rounds": 3, "max_cost_usd": 0.2}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(d["info"]["context"]["keep_recent"], 4)
        self.assertEqual(d["info"]["budget"]["max_rounds"], 3)


class TestSay(unittest.TestCase):
    """真的走一遍 SSE：假上游 → 工作台 → 落盘。"""

    @classmethod
    def setUpClass(cls):
        cls.live = Live()
        cls.up = FakeUpstream("这是我真实返回的一句话。")
        status, c = cls.live.req(
            "POST", "/api/contacts",
            {"name": "Fake", "provider": "testbot", "model": "fake-1", "base_url": cls.up.base_url},
        )
        cls.contact = c
        status, conv = cls.live.req("POST", "/api/conversations", {"title": "SSE", "members": [c["id"]]})
        cls.conv = conv

    @classmethod
    def tearDownClass(cls):
        cls.up.stop()
        cls.live.stop()

    def _say(self, text):
        conn = http.client.HTTPConnection(self.live.host, timeout=60)
        conn.request(
            "POST",
            f"/api/conversations/{self.conv['id']}/say",
            body=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        r = conn.getresponse()
        events = []
        for line in r:
            line = line.decode("utf-8").strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        conn.close()
        return events

    def test_stream_returns_turn_and_done(self):
        events = self._say("你好")
        kinds = [e["type"] for e in events]
        self.assertIn("turn", kinds)
        self.assertIn("done", kinds)
        turn = next(e for e in events if e["type"] == "turn")
        self.assertEqual(turn["content"], "这是我真实返回的一句话。")
        self.assertGreater(turn["tokens"], 0)

    def test_persisted_to_disk(self):
        self._say("落盘测试")
        conv = store.load_conversation(self.conv["id"])
        texts = [t.get("content") for t in conv["turns"]]
        self.assertIn("落盘测试", texts, "用户发言要落盘")
        self.assertIn("这是我真实返回的一句话。", texts, "模型回答要落盘")

    def test_reload_keeps_history(self):
        """再读一次会话，历史还在（模拟关掉再打开）。"""
        self._say("还记得吗")
        status, d = self.live.req("GET", f"/api/conversations/{self.conv['id']}")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(d["turns"]), 2)

    def test_empty_text_rejected(self):
        conn = http.client.HTTPConnection(self.live.host, timeout=30)
        conn.request(
            "POST",
            f"/api/conversations/{self.conv['id']}/say",
            body=json.dumps({"text": "   "}).encode(),
            headers={"Content-Type": "application/json"},
        )
        r = conn.getresponse()
        self.assertEqual(r.status, 400)
        conn.close()

    def test_unknown_conversation(self):
        conn = http.client.HTTPConnection(self.live.host, timeout=30)
        conn.request(
            "POST",
            "/api/conversations/nope/say",
            body=json.dumps({"text": "hi"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()

    def test_mention_routing(self):
        """@名字 时只有被点名的人回答。"""
        before = len(self.up.requests)
        self._say("@Fake 你怎么看")
        self.assertGreater(len(self.up.requests), before)


class TestWithKey(unittest.TestCase):
    """有 key 时应该自动建一个默认群聊，用户打开就能说话。"""

    @classmethod
    def setUpClass(cls):
        cls.live = Live(with_key=True)

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def test_default_conversation_created(self):
        status, info = self.live.req("GET", "/api/info")
        self.assertFalse(info["onboarding"])
        self.assertEqual(len(info["conversations"]), 1)
        conv = info["conversations"][0]
        self.assertEqual(conv["kind"], "group")
        self.assertEqual(len(conv["members"]), 3)

    def test_setup_endpoint(self):
        status, d = self.live.req(
            "POST", "/api/setup", {"provider": "openrouter", "api_key": "sk-or-v1-abc"}
        )
        self.assertEqual(status, 201, msg=str(d))
        self.assertFalse(d["info"]["onboarding"])
        self.assertEqual(config.load()["api_keys"]["openrouter"], "sk-or-v1-abc")


class TestWindowsEncoding(unittest.TestCase):
    """Windows 上 stdout 可能是 cp1252，打印中文不能把服务打死。"""

    def test_server_survives_cp1252_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "cp1252"
            env["USERPROFILE"] = tmp
            env["HOME"] = tmp
            env["PYTHONPATH"] = str(ROOT)
            env.pop("OPENROUTER_API_KEY", None)

            proc = subprocess.Popen(
                [sys.executable, "-m", "polylogue", "--web", "--port", str(port)],
                cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                ok = False
                deadline = time.time() + 25
                while time.time() < deadline:
                    try:
                        conn = http.client.HTTPConnection(f"127.0.0.1:{port}", timeout=2)
                        conn.request("GET", "/api/info")
                        if conn.getresponse().status == 200:
                            ok = True
                            conn.close()
                            break
                        conn.close()
                    except OSError:
                        time.sleep(0.3)
                self.assertTrue(ok, "cp1252 stdout 下服务没起来（打印中文把它打死了）")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()


class TestCliWiring(unittest.TestCase):
    def test_web_flag_starts_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()

            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            env["USERPROFILE"] = tmp
            env["HOME"] = tmp
            env["PYTHONIOENCODING"] = "utf-8"
            env.pop("OPENROUTER_API_KEY", None)

            proc = subprocess.Popen(
                [sys.executable, "-m", "polylogue", "--web", "--port", str(port)],
                cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                info = None
                deadline = time.time() + 25
                while time.time() < deadline:
                    try:
                        conn = http.client.HTTPConnection(f"127.0.0.1:{port}", timeout=2)
                        conn.request("GET", "/api/info")
                        r = conn.getresponse()
                        if r.status == 200:
                            info = json.loads(r.read())
                            conn.close()
                            break
                        conn.close()
                    except OSError:
                        time.sleep(0.3)
                self.assertIsNotNone(info, "CLI 没把服务起起来")
                self.assertIn("contacts", info)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
