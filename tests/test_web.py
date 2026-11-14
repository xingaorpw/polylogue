"""端到端测试：假上游 + 真 HTTP + 真 SSE。

这条测试证明的不是「函数返回对不对」，而是「浏览器打开能用的那条链路通不通」：
HTTP 请求 → 房间管理 → 会话编排 → 并行调用 → SSE 推回。
"""

import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from polylogue.guard import Budget
from polylogue.web import serve


class FakeUpstream(BaseHTTPRequestHandler):
    """假的 OpenAI 兼容服务。"""

    protocol_version = "HTTP/1.1"
    hits = 0
    lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

        last = ""
        for m in body.get("messages", []):
            if m.get("role") == "user":
                last = m.get("content", "")

        with FakeUpstream.lock:
            FakeUpstream.hits += 1
            n = FakeUpstream.hits

        payload = {
            "choices": [{"message": {"content": f"回复{n}：看到了「{last[-40:]}」"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream_url = f"http://127.0.0.1:{cls.upstream.server_address[1]}/v1"
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

        ready = threading.Event()
        holder = {}

        def run():
            serve(
                ["fake1", "fake2"],
                port=0,
                budget=Budget(max_rounds=3, max_calls=6),
                custom={"fake1": cls.upstream_url, "fake2": cls.upstream_url},
                on_ready=lambda url: (holder.__setitem__("url", url), ready.set()),
            )

        threading.Thread(target=run, daemon=True).start()
        if not ready.wait(10):
            raise RuntimeError("polylogue web 没起来")
        cls.base = holder["url"]
        cls.host = cls.base.split("://")[1]

    # ---------------------------------------------------------------- 工具

    def request(self, method, path, body=None, timeout=30):
        conn = http.client.HTTPConnection(self.host, timeout=timeout)
        payload = None
        headers = {}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        ctype = resp.getheader("Content-Type") or ""
        conn.close()
        return resp.status, ctype, raw

    def get_json(self, path):
        status, _, raw = self.request("GET", path)
        return status, json.loads(raw)

    def say(self, room_id, text):
        status, ctype, raw = self.request("POST", f"/api/rooms/{room_id}/say", {"text": text})
        events = []
        for chunk in raw.decode("utf-8").split("\n\n"):
            chunk = chunk.strip()
            if not chunk.startswith("data: "):
                continue
            try:
                events.append(json.loads(chunk[6:]))
            except ValueError:
                pass
        return status, ctype, events

    def default_room(self):
        _, info = self.get_json("/api/info")
        return info["default_room"]


class TestStatic(WebCase):
    def test_index_is_workbench(self):
        status, ctype, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        html = raw.decode("utf-8")
        # 工作台该有的三块区域
        self.assertIn("房间", html)
        self.assertIn("polylogue", html)
        self.assertIn("/api/rooms", html)

    def test_no_external_resources(self):
        """页面不能引任何外部资源，否则断网打不开。"""
        _, _, raw = self.request("GET", "/")
        html = raw.decode("utf-8")
        for bad in ("cdn.", "unpkg", "jsdelivr", "googleapis", "//cdn"):
            self.assertNotIn(bad, html, msg=f"页面引了外部资源：{bad}")


class TestInfoAndRooms(WebCase):
    def test_info_lists_providers(self):
        status, info = self.get_json("/api/info")
        self.assertEqual(status, 200)
        self.assertIn("providers", info)
        names = {p["name"] for p in info["providers"]}
        # 主流厂商都要在列表里
        for expected in ("openai", "claude", "gemini", "grok", "deepseek", "groq"):
            self.assertIn(expected, names)
        self.assertIn("default_room", info)
        self.assertIn("price_table", info)

    def test_provider_reports_availability(self):
        """没配 key 的要标成不可用，而不是等用户点了才报错。"""
        _, info = self.get_json("/api/info")
        by_name = {p["name"]: p for p in info["providers"]}
        self.assertFalse(by_name["openai"]["available"])
        self.assertTrue(by_name["ollama"]["available"])  # 本地模型不需要 key

    def test_rooms_listed(self):
        status, data = self.get_json("/api/rooms")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["rooms"]), 1)
        room = data["rooms"][0]
        self.assertEqual(room["members"], ["fake1", "fake2"])
        self.assertIn("budget", room)

    def test_create_room(self):
        status, _, raw = self.request("POST", "/api/rooms", {"name": "测试房间", "members": ["ollama"]})
        self.assertEqual(status, 201)
        room = json.loads(raw)
        self.assertEqual(room["name"], "测试房间")
        self.assertEqual(room["members"], ["ollama"])

        # 新房间应该出现在列表里
        _, data = self.get_json("/api/rooms")
        self.assertIn(room["id"], [r["id"] for r in data["rooms"]])

        # 清理
        self.request("DELETE", f"/api/rooms/{room['id']}")

    def test_create_room_without_members_rejected(self):
        status, _, raw = self.request("POST", "/api/rooms", {"name": "空", "members": []})
        self.assertEqual(status, 400)

    def test_create_room_with_unusable_member_rejected(self):
        """认不出的成员名要报错，不能悄悄建一个空房间。"""
        status, _, _ = self.request("POST", "/api/rooms", {"members": ["不存在的模型"]})
        self.assertEqual(status, 400)

    def test_unknown_room_404(self):
        status, _ = self.get_json("/api/rooms/deadbeef")
        self.assertEqual(status, 404)

    def test_delete_room(self):
        _, _, raw = self.request("POST", "/api/rooms", {"name": "删我", "members": ["ollama"]})
        room_id = json.loads(raw)["id"]
        status, _, _ = self.request("DELETE", f"/api/rooms/{room_id}")
        self.assertEqual(status, 200)
        status, _ = self.get_json(f"/api/rooms/{room_id}")
        self.assertEqual(status, 404)

    def test_delete_unknown_room_404(self):
        status, _, _ = self.request("DELETE", "/api/rooms/nope")
        self.assertEqual(status, 404)


class TestSay(WebCase):
    def test_without_mention_asks_everyone(self):
        status, ctype, events = self.say(self.default_room(), "这个方案怎么样")
        self.assertEqual(status, 200)
        self.assertIn("event-stream", ctype)

        turns = [e for e in events if e["type"] == "turn"]
        self.assertEqual(len(turns), 2)
        self.assertEqual({t["speaker"] for t in turns}, {"fake1", "fake2"})

    def test_with_mention_asks_only_one(self):
        _, _, events = self.say(self.default_room(), "@fake1 只问你")
        turns = [e for e in events if e["type"] == "turn"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["speaker"], "fake1")

    def test_done_event_has_stats(self):
        _, _, events = self.say(self.default_room(), "算一下花费")
        done = [e for e in events if e["type"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertGreater(done[0]["tokens"], 0)
        self.assertIn("stats", done[0])
        self.assertIsInstance(done[0]["stats"], list)

    def test_empty_text_rejected(self):
        status, _, _ = self.request("POST", f"/api/rooms/{self.default_room()}/say", {"text": "   "})
        self.assertEqual(status, 400)

    def test_bad_json_rejected(self):
        conn = http.client.HTTPConnection(self.host, timeout=10)
        conn.request(
            "POST", f"/api/rooms/{self.default_room()}/say",
            body=b"not json", headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 400)

    def test_unknown_room_404(self):
        status, _, _ = self.request("POST", "/api/rooms/nope/say", {"text": "hi"})
        self.assertEqual(status, 404)


class TestTranscript(WebCase):
    def test_turns_persisted(self):
        room_id = self.default_room()
        self.say(room_id, "记下来")
        status, data = self.get_json(f"/api/rooms/{room_id}")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["turns"]), 3)  # 1 user + 2 models
        self.assertEqual(data["turns"][0]["role"], "user")

    def test_stats_breakdown(self):
        room_id = self.default_room()
        self.say(room_id, "统计一下")
        status, data = self.get_json(f"/api/rooms/{room_id}/stats")
        self.assertEqual(status, 200)
        speakers = {r["speaker"] for r in data["by_model"]}
        self.assertEqual(speakers, {"fake1", "fake2"})
        self.assertGreater(data["total_tokens"], 0)

    def test_export_markdown(self):
        room_id = self.default_room()
        self.say(room_id, "导出测试")
        status, ctype, raw = self.request("GET", f"/api/rooms/{room_id}/export")
        self.assertEqual(status, 200)
        self.assertIn("markdown", ctype)
        text = raw.decode("utf-8")
        self.assertIn("# ", text)  # 标题
        self.assertIn("[user]", text)
        self.assertIn("估算成本", text)

    def test_export_unknown_room_404(self):
        status, _, _ = self.request("GET", "/api/rooms/nope/export")
        self.assertEqual(status, 404)


class TestWindowsEncoding(unittest.TestCase):
    """Windows 下 stdout 可能是 cp1252，打印中文会抛 UnicodeEncodeError。

    这个 bug 曾让 CI 的 Windows job 全红：服务线程在打印启动信息时死掉，
    之后所有连接都被拒。它也会打到真实用户 —— 任何把输出重定向的场景。
    """

    def test_out_survives_cp1252_stdout(self):
        import io
        import sys as _sys
        from unittest import mock

        from polylogue.web import _out

        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="cp1252")
        with mock.patch.object(_sys, "stdout", stream):
            _out("polylogue 工作台 → http://127.0.0.1:8765")  # 不能抛

    def test_ensure_utf8_streams_reconfigures(self):
        import io
        import sys as _sys
        from unittest import mock

        from polylogue.web import ensure_utf8_streams

        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch.object(_sys, "stdout", stream):
            ensure_utf8_streams()
            self.assertEqual(stream.encoding.lower().replace("-", ""), "utf8")
            print("工作台")  # 现在不该抛了

    def test_server_survives_cp1252_stdout(self):
        """真的起一个子进程，stdout 设成 cp1252，确认服务不会在启动时死掉。"""
        import socket
        import subprocess
        import sys as _sys
        import time
        import urllib.request
        from pathlib import Path

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        env = dict(**__import__("os").environ)
        env["PYTHONIOENCODING"] = "cp1252"
        root = Path(__file__).resolve().parent.parent

        proc = subprocess.Popen(
            [_sys.executable, "-m", "polylogue", "--web", "--models", "ollama", "--port", str(port)],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            ok = False
            for _ in range(40):
                if proc.poll() is not None:
                    break  # 进程已经死了，别等了
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=1) as r:
                        if r.status == 200:
                            ok = True
                            break
                except Exception:
                    time.sleep(0.25)

            self.assertIsNone(proc.poll(), msg="进程在启动阶段就退出了")
            self.assertTrue(ok, msg="cp1252 的 stdout 下服务没能响应")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
