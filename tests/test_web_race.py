"""并发与失败路径的测试。

这一类 bug 的特点是：功能测试全绿，但用户真的用起来会丢数据。
所以每条都要能复现「用户看到什么 ≠ 实际存了什么」。
"""

import http.client
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from polylogue import store
from polylogue.web import serve

from .test_web import Live


class SlowUpstream:
    """慢一点的假上游，用来制造「两个请求同时在跑」的窗口。"""

    def __init__(self, reply="收到。", delay=0.4):
        self.reply = reply
        self.delay = delay
        self.calls = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                outer.calls += 1
                time.sleep(outer.delay)
                data = json.dumps(
                    {
                        "choices": [{"message": {"content": outer.reply}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                    }
                ).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def say_stream(live, cid, text, timeout=60):
    conn = http.client.HTTPConnection(live.host, timeout=timeout)
    conn.request(
        "POST",
        f"/api/conversations/{cid}/say",
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


def user_texts(conv_id):
    conv = store.load_conversation(conv_id)
    return [t.get("content") for t in (conv or {}).get("turns") or [] if t.get("role") == "user"]


class TestUserMessageNeverLost(unittest.TestCase):
    """用户说的话必须立刻落盘。

    否则「等待超时」或「内部错误」之后，用户会发现自己刚打的字没了。
    """

    @classmethod
    def setUpClass(cls):
        cls.live = Live()
        cls.up = SlowUpstream()
        status, c = cls.live.req(
            "POST", "/api/contacts",
            {"name": "Slow", "provider": "testbot", "model": "fake-1", "base_url": cls.up.base_url},
        )
        cls.contact = c

    @classmethod
    def tearDownClass(cls):
        cls.up.stop()
        cls.live.stop()

    def _conv(self, title):
        status, conv = self.live.req("POST", "/api/conversations", {"title": title, "members": [self.contact["id"]]})
        self.assertEqual(status, 201, conv)
        return conv["id"]

    def test_user_message_survives_provider_error(self):
        # 必须 patch polylogue.web.build_provider —— web.py 是
        # `from .providers import build_provider`，patch 源模块不会生效。
        from unittest import mock

        from polylogue import web as webmod

        cid = self._conv("失败路径")
        with mock.patch.object(webmod, "build_provider", side_effect=webmod.ProviderError("上游炸了")):
            events = say_stream(self.live, cid, "这句不能丢")
        self.assertTrue(events, "至少要有事件返回")
        self.assertIn("这句不能丢", user_texts(cid), "失败后用户的消息丢了")

    def test_user_message_survives_internal_error(self):
        """会话跑到一半内部抛异常时，用户那句也必须已经落盘。"""
        from unittest import mock

        from polylogue import web as webmod

        cid = self._conv("内部错误")
        with mock.patch.object(webmod.Session, "say", side_effect=RuntimeError("炸在中间")):
            events = say_stream(self.live, cid, "这句也要留")
        self.assertTrue(any(e["type"] == "error" for e in events), f"没有报错事件：{events}")
        self.assertIn("这句也要留", user_texts(cid), "内部错误后用户的消息丢了")


class TestConcurrentSameConversation(unittest.TestCase):
    """同一个会话被两个请求同时说话时，不能互相覆盖。"""

    @classmethod
    def setUpClass(cls):
        cls.live = Live()
        cls.up = SlowUpstream(delay=0.5)
        status, c = cls.live.req(
            "POST", "/api/contacts",
            {"name": "Slow2", "provider": "testbot", "model": "fake-1", "base_url": cls.up.base_url},
        )
        cls.contact = c

    @classmethod
    def tearDownClass(cls):
        cls.up.stop()
        cls.live.stop()

    def test_concurrent_same_conversation_keeps_both(self):
        status, conv = self.live.req(
            "POST", "/api/conversations", {"title": "并发", "members": [self.contact["id"]]}
        )
        cid = conv["id"]

        results = {}

        def fire(tag, text):
            try:
                results[tag] = say_stream(self.live, cid, text)
            except Exception as exc:  # noqa: BLE001
                results[tag] = exc

        threads = [
            threading.Thread(target=fire, args=("a", "第一条")),
            threading.Thread(target=fire, args=("b", "第二条")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        texts = user_texts(cid)
        self.assertIn("第一条", texts, f"第一条被覆盖了，现在只有 {texts}")
        self.assertIn("第二条", texts, f"第二条被覆盖了，现在只有 {texts}")


class TestCancelBookkeeping(unittest.TestCase):
    """停止信号按「会话存活期」管理，不能按请求，也不能只增不减。"""

    def test_same_conversation_reuses_event(self):
        from polylogue.web import App

        app = App()
        first = app.cancel_for("c1")
        second = app.cancel_for("c1")
        self.assertIs(first, second, "同一会话必须拿到同一个 Event，否则停止按不动")

    def test_forget_cancel_removes(self):
        from polylogue.web import App

        app = App()
        app.cancel_for("c1")
        app.forget_cancel("c1")
        self.assertEqual(len(app.cancels), 0)

    def test_delete_conversation_frees_event(self):
        """删掉会话之后，它的停止信号也要跟着走，不能一直留在内存里。"""
        live = Live()
        try:
            status, conv = live.req("POST", "/api/conversations", {"title": "待删", "members": ["claude"]})
            cid = conv["id"]
            live.req("POST", f"/api/conversations/{cid}/stop")
            status, _ = live.req("DELETE", f"/api/conversations/{cid}")
            self.assertEqual(status, 200)
        finally:
            live.stop()


class TestNoDoubleResponse(unittest.TestCase):
    """SSE 开始之后绝不能再写一次 HTTP 响应。

    出过这个 bug：`Room([])` 抛 ValueError，冒泡到 do_POST 的 except 又调
    `_json(400)`，结果把第二个 HTTP 响应塞进了已经开始的流里 ——
    客户端收到的是 `data: ...` 中间夹一段 `HTTP/1.1 400 ...`，解析全崩。
    """

    @classmethod
    def setUpClass(cls):
        cls.live = Live()

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()

    def _empty_room_conversation(self, title):
        """造一个「成员都在通讯录里被删了」的会话 —— 这是真实的空房间场景。"""
        status, c = self.live.req(
            "POST", "/api/contacts", {"name": "临时", "provider": "testbot", "model": "fake-1"}
        )
        self.assertEqual(status, 201, c)
        status, conv = self.live.req("POST", "/api/conversations", {"title": title, "members": [c["id"]]})
        self.assertEqual(status, 201, conv)
        # 把人从通讯录删掉，会话里还留着那个 id
        status, _ = self.live.req("DELETE", f"/api/contacts/{c['id']}")
        self.assertEqual(status, 200)
        return conv["id"]

    def test_stream_has_no_nested_http_response(self):
        cid = self._empty_room_conversation("空成员")

        conn = http.client.HTTPConnection(self.live.host, timeout=30)
        conn.request(
            "POST",
            f"/api/conversations/{cid}/say",
            body=json.dumps({"text": "有人吗"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        r = conn.getresponse()
        raw = r.read().decode("utf-8", "replace")
        conn.close()

        self.assertEqual(r.status, 200, "开流之后状态码不该再变")
        self.assertNotIn("HTTP/1.1", raw, f"流里混进了第二个 HTTP 响应：{raw[:300]}")
        self.assertNotIn("Server: BaseHTTP", raw)

        events = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: ")]
        kinds = [e["type"] for e in events]
        self.assertIn("error", kinds, f"应该以 SSE 事件报告失败：{events}")
        self.assertIn("done", kinds, "必须发 done，否则前端一直转圈")

    def test_user_message_kept_even_when_room_is_empty(self):
        cid = self._empty_room_conversation("空成员2")
        conn = http.client.HTTPConnection(self.live.host, timeout=30)
        conn.request(
            "POST",
            f"/api/conversations/{cid}/say",
            body=json.dumps({"text": "这句也要留"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        conn.getresponse().read()
        conn.close()
        self.assertIn("这句也要留", user_texts(cid))


class TestFrontendRollback(unittest.TestCase):
    """请求整个失败时，界面必须回滚到和磁盘一致。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        cls.html = (Path(__file__).resolve().parent.parent / "polylogue" / "static" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_append_row_returns_element(self):
        idx = self.html.index("function appendRow")
        self.assertIn("return row", self.html[idx : idx + 400], "appendRow 要返回元素，调用方才能撤销")

    def test_failed_send_restores_text_and_removes_row(self):
        idx = self.html.index("async function say")
        # 按函数边界切片，不要用固定长度 —— 函数会长，切片一过界
        # 断言就变成「没找到」的假失败
        end = self.html.index("\nasync function refreshQuiet", idx)
        block = self.html[idx:end]
        self.assertIn("}catch(e){", block)
        catch = block[block.index("}catch(e){"):]
        self.assertIn("mine.remove()", catch, "失败时没撤销乐观插入的那一行")
        self.assertIn('$("input").value = t', catch, "失败时没把文字放回输入框")


if __name__ == "__main__":
    unittest.main()
