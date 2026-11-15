"""加固测试：这一批全是「功能测试全绿，但真用起来会丢数据/丢钱」的坑。

每个用例对应一个真实被证实过的缺陷，改之前是红的。
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from polylogue import bootstrap, config, context, credentials, guard, pricing, providers, room, session, store
from polylogue.context import estimate_tokens
from polylogue.room import Room, Turn
from polylogue.providers import PRESETS


def turn(text, speaker="a", role="assistant", error=None):
    return room.Turn(speaker=speaker, content=text, role=role, error=error)


class TmpHome(unittest.TestCase):
    """每个用例一个独立的 ~/.polylogue。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        for mod, attr, value in (
            (config, "CONFIG_PATH", self.home / "config.json"),
        ):
            patcher = mock.patch.object(mod, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        store.ensure_dirs()

    def write_conv(self, name, data):
        path = store._conv_dir() / f"{name}.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path


# ---------------------------------------------------------------- 删除/写入安全


class TestRemoveContactPathSafety(TmpHome):
    """删联系人时不能拿原始 id 去 unlink —— 那是任意文件删除。"""

    def test_traversal_id_does_not_delete_config(self):
        store.load_state()  # 先把 config.json 建出来
        cfg = config.CONFIG_PATH
        self.assertTrue(cfg.is_file())

        state = store.load_state()
        state["contacts"].append({"id": "../config.json", "name": "坏", "provider": "openai"})
        store.save_state(state)
        self.assertTrue(cfg.is_file())

        store.remove_contact("../config.json")
        self.assertTrue(cfg.is_file(), "配置文件被删掉了")

    def test_reserved_name_id_rejected(self):
        self.assertIsNone(store._avatar_file("nul"))
        self.assertIsNone(store._avatar_file("CON"))
        self.assertIsNone(store._avatar_file("com1"))

    def test_save_avatar_rejects_colliding_ids(self):
        uri = "data:image/png;base64,AAAA"
        self.assertFalse(store.save_avatar("a b", uri), "非法 id 不该悄悄写到 ab 上")
        self.assertFalse(store.save_avatar("", uri), "空 id 不该顶掉 generic 兜底图")
        self.assertFalse(store.save_avatar("nul", uri))
        self.assertTrue(store.save_avatar("ok-id", uri))


class TestAtomicWriteConcurrency(TmpHome):
    """两个线程同时写同一个文件不能互相踩。"""

    def test_concurrent_writes_do_not_raise(self):
        data = {"id": "race", "title": "初始", "members": [], "turns": []}
        store.save_conversation(data)
        errors = []

        def writer(n):
            try:
                for i in range(15):
                    d = dict(data)
                    d["title"] = f"线程{n}-{i}"
                    store.save_conversation(d)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(errors, [], f"并发写抛异常：{errors[:3]}")
        self.assertIsNotNone(store.load_conversation("race"))
        # 不能留下临时文件垃圾
        leftovers = list(store._conv_dir().glob("*.tmp"))
        self.assertEqual(leftovers, [], f"留下了临时文件：{leftovers}")

    def test_concurrent_add_contact_keeps_all(self):
        names = [f"模型{i}" for i in range(12)]
        threads = [threading.Thread(target=store.add_contact, args=(n, "openai")) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        got = {c["name"] for c in store.contacts()}
        missing = [n for n in names if n not in got]
        self.assertEqual(missing, [], f"并发新增丢了这些：{missing}")


# ---------------------------------------------------------------- 坏数据不能炸


class TestCorruptDataResilience(TmpHome):
    """一个坏文件不能让整个历史接口 500。"""

    def test_string_updated_does_not_break_list(self):
        self.write_conv("bad", {"id": "bad", "title": "坏", "updated": "2026-01-02", "turns": []})
        self.write_conv("good", {"id": "good", "title": "好", "updated": 100.0, "turns": []})
        items = store.list_conversations()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], "good")

    def test_turns_not_a_list(self):
        self.write_conv("a", {"id": "a", "title": "a", "turns": {"x": 1}, "updated": 1})
        self.assertEqual(len(store.list_conversations()), 1)
        # 不炸就行：turns 坏掉时按空历史处理，标题仍然能搜到
        hits = store.search("a")
        self.assertIsInstance(hits, list)
        self.assertTrue(all(h["where"] == "标题" for h in hits), f"坏 turns 被当成发言搜出来了：{hits}")

    def test_turn_content_not_a_string(self):
        self.write_conv(
            "b", {"id": "b", "title": "b", "turns": [{"content": 12345, "speaker": "x"}], "updated": 1}
        )
        items = store.list_conversations()
        self.assertEqual(items[0]["last"], "12345")
        self.assertEqual(store.search("123")[0]["snippet"], "12345")

    def test_top_level_not_a_dict(self):
        (store._conv_dir() / "arr.json").write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(store.list_conversations(), [])
        self.assertIsNone(store.load_conversation("arr"))

    def test_contacts_wrong_type_falls_back(self):
        config.update(contacts="oops", categories="nope")
        state = store.load_state()
        self.assertIsInstance(state["contacts"], list)
        self.assertTrue(all(isinstance(c, dict) for c in state["contacts"]))
        self.assertIsInstance(state["categories"], list)
        self.assertIsNotNone(store.contact_by_id("claude"))

    def test_conv_id_mismatch_uses_filename(self):
        self.write_conv("copy", {"id": "orig", "title": "复制", "updated": 1, "turns": []})
        items = store.list_conversations()
        self.assertEqual(items[0]["id"], "copy")
        self.assertIsNotNone(store.load_conversation(items[0]["id"]))

    def test_int_id_normalized_to_str(self):
        saved = store.save_conversation({"id": 123, "title": "t", "turns": []})
        self.assertIsInstance(saved["id"], str)

    def test_title_none_does_not_crash(self):
        conv = store.create_conversation(None, [])
        self.assertEqual(conv["title"], "未命名")
        store.rename_conversation(conv["id"], None)
        self.assertIsNotNone(store.load_conversation(conv["id"]))

    def test_overlong_id_raises_value_error(self):
        with self.assertRaises(ValueError):
            store.save_conversation({"id": "a" * 300, "title": "t", "turns": []})


# ---------------------------------------------------------------- 预算


class TestUnknownCostCap(unittest.TestCase):
    """查不到价格时，成本上限必须仍然生效。"""

    def test_all_unknown_still_stops(self):
        b = guard.Budget(max_cost_usd=0.5)
        self.assertIsNone(b.check(rounds=0, calls=1, cost=0.0, cost_unknown=True, tokens=0, unknown_calls=1))
        stop = b.check(rounds=0, calls=5, cost=0.0, cost_unknown=True, tokens=0, unknown_calls=5)
        self.assertIsNotNone(stop, "全部未知价格时成本上限完全失效了")
        self.assertEqual(stop.code, "max_cost_uncertain")

    def test_known_cost_untouched(self):
        b = guard.Budget(max_cost_usd=0.5)
        self.assertIsNone(b.check(rounds=0, calls=1, cost=0.1, cost_unknown=False, tokens=0))

    def test_no_cap_means_no_stop(self):
        # max_calls 也要放大，否则先被调用次数上限拦下，测不到成本这一项
        b = guard.Budget(max_calls=999, max_cost_usd=None)
        self.assertIsNone(
            b.check(rounds=0, calls=50, cost=99.0, cost_unknown=True, tokens=0, unknown_calls=50)
        )


class FakeProvider:
    """记录自己收到的上下文，用来验证并行快照。"""

    free = True

    def __init__(self, name, delay=0.0):
        self.name = name
        self.delay = delay
        self.seen = []

    def complete(self, messages, system=None):
        if self.delay:
            time.sleep(self.delay)
        self.seen.append([m.content for m in messages])
        return providers.Reply(
            text=f"{self.name} 的回复",
            model="fake",
            usage=providers.Usage(prompt_tokens=1, completion_tokens=1),
            latency_s=0.0,
        )


class TestSessionHardCaps(unittest.TestCase):
    def test_max_calls_is_a_hard_cap(self):
        names = ["a", "b", "c", "d"]
        room_obj = room.Room(names)
        provs = {n: FakeProvider(n) for n in names}
        ses = session.Session(
            room_obj, provs, budget=guard.Budget(max_rounds=99, max_calls=1), parallel=False
        )
        ses.say("都说说")
        self.assertLessEqual(ses.calls, 1, f"调用次数上限被突破：{ses.calls}")

    def test_parallel_round_does_not_leak_context(self):
        """同一轮的模型必须互相看不见 —— 否则并行退化成串行，还会多花钱。"""
        names = [f"m{i}" for i in range(6)]
        room_obj = room.Room(names)
        provs = {n: FakeProvider(n, delay=0.02) for n in names}
        ses = session.Session(
            room_obj, provs, budget=guard.Budget(max_rounds=1, max_calls=99), parallel=True
        )
        ses.say("同时问")
        for n, p in provs.items():
            first = p.seen[0]
            self.assertEqual(len(first), 1, f"{n} 看到了同轮其他人的发言：{first}")
            self.assertIn("同时问", first[0])

    def test_unknown_mention_does_not_broadcast(self):
        room_obj = room.Room(["claude", "gpt"])
        provs = {n: FakeProvider(n) for n in ["claude", "gpt"]}
        ses = session.Session(room_obj, provs, budget=guard.Budget(max_rounds=3, max_calls=9))
        result = ses.say("@gpt5 你怎么看")
        self.assertEqual(result.turns, [], "点错名字不该触发全员调用")
        self.assertEqual(result.stop.code, "unknown_mention")
        self.assertEqual(ses.calls, 0)


# ---------------------------------------------------------------- 上下文预算


class TestContextBudgetIsReal(unittest.TestCase):
    def test_budget_applies_when_turn_count_is_small(self):
        """条数 ≤ keep_recent 但每条很长时，预算也必须生效。"""
        turns = [turn("字" * 4000) for _ in range(8)]
        digest, recent, stats = context.compress(turns, keep_recent=8, budget_tokens=1200)
        total = estimate_tokens(digest or "") + sum(estimate_tokens(t.content) for t in recent)
        self.assertLessEqual(total, 1200, f"预算被突破：{total} tokens，stats={stats}")
        self.assertLess(len(recent), 8, "超预算时应该裁掉最老的几条")

    def test_budget_trims_recent_when_digest_is_empty(self):
        turns = [turn("字" * 1000) for _ in range(4)]
        digest, recent, stats = context.compress(turns, keep_recent=0, budget_tokens=800)
        total = estimate_tokens(digest or "") + sum(estimate_tokens(t.content) for t in recent)
        self.assertLessEqual(total, 800)

    def test_no_negative_saved_tokens(self):
        turns = [turn("好") for _ in range(20)]
        digest, recent, stats = context.compress(turns, keep_recent=0, budget_tokens=30)
        self.assertGreaterEqual(stats.get("saved_tokens", 0), 0)

    def test_omitted_counts_empty_turns(self):
        turns = [turn("")] * 4 + [turn("内容" * 50)] * 6
        digest, recent, stats = context.compress(turns, keep_recent=0, budget_tokens=50)
        represented = 0 if not digest else digest.count("\n[") + (1 if digest.startswith("[") else 0)
        self.assertGreaterEqual(stats["dropped"], len(turns) - represented - 2)

    def test_japanese_and_korean_not_underestimated(self):
        ja = "あ" * 100
        ko = "가" * 100
        self.assertGreaterEqual(estimate_tokens(ja), 100, "日文被当成 4 字符 1 token")
        self.assertGreaterEqual(estimate_tokens(ko), 100, "韩文被当成 4 字符 1 token")

    def test_emoji_counted(self):
        self.assertGreaterEqual(estimate_tokens("😀" * 50), 50)

    def test_chinese_mention_survives_compression(self):
        """压缩最该保住的就是「谁该接话」，中文名字不能被正则漏掉。"""
        turns = [turn("@小助手 你来定", speaker="用户")] + [
            turn("无关的早期发言" * 40) for _ in range(10)
        ]
        digest, recent, stats = context.compress(turns, keep_recent=2, budget_tokens=None)
        self.assertIsNotNone(digest)
        self.assertIn("@小助手", digest, "中文 @名字在压缩时被抹掉了，交接链会断")

    def test_compress_is_linear_not_quadratic(self):
        """这段代码每个成员每轮都跑一次，O(n²) 会直接把请求线程卡住。

        800 条曾经要 743 ms；线性版本 ~30 ms。这里给 6 倍余量，
        只为挡住「又退回 O(n²)」这种回归，不是精确的性能断言。
        """
        big = [turn("这是一句比较长的模型发言，用来撑起 token 数。" * 6) for _ in range(800)]
        started = time.monotonic()
        context.compress(big, keep_recent=8, budget_tokens=6000)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2, f"compress(800) 用了 {elapsed*1000:.0f} ms，可能又变成 O(n²) 了")

    def test_budget_respected_with_many_turns(self):
        big = [turn("字" * 800) for _ in range(300)]
        digest, recent, stats = context.compress(big, keep_recent=8, budget_tokens=2000)
        total = estimate_tokens(digest or "") + sum(estimate_tokens(t.content) for t in recent)
        self.assertLessEqual(total, 2000, f"长历史下预算被突破：{total}")
        self.assertGreater(stats["dropped"], 200)


# ---------------------------------------------------------------- 路由


class TestMentionRouting(unittest.TestCase):
    def test_chinese_member_name_can_be_mentioned(self):
        r = room.Room(["小助手", "GPT"])
        self.assertEqual(r.mentions("@小助手 你看呢"), ["小助手"])

    def test_name_with_space_can_be_mentioned(self):
        r = room.Room(["gpt-4o mini", "claude"])
        self.assertEqual(r.mentions("@gpt-4o mini 回答"), ["gpt-4o mini"])

    def test_prefix_name_not_confused(self):
        r = room.Room(["gpt", "gpt4"])
        self.assertEqual(r.mentions("@gpt4 你说"), ["gpt4"])
        self.assertEqual(r.mentions("@gpt 你说"), ["gpt"])

    def test_empty_turn_excluded_from_context(self):
        r = room.Room(["a"])
        r.add_user("问题")
        r.add_model("a", "")
        r.add_model("a", "正常回复")
        ctx = r.context_for("a")
        self.assertEqual([m.content for m in ctx], ["[用户] 问题", "正常回复"])


# ---------------------------------------------------------------- 价格


class TestPriceBoundaries(unittest.TestCase):
    def test_sibling_model_not_priced_as_parent(self):
        self.assertIsNone(pricing.cost_of("o3-mini", 1_000_000, 0), "o3-mini 被套用了 o3 的价格")
        self.assertIsNone(pricing.cost_of("grok-4-fast", 1_000_000, 0))

    def test_dated_version_still_matches(self):
        self.assertIsNotNone(pricing.cost_of("gpt-4o-mini-2024-08-06", 1_000_000, 0))
        self.assertIsNotNone(pricing.cost_of("claude-sonnet-4-20250514", 1_000_000, 0))


# ---------------------------------------------------------------- 协议


class TestAnthropicRoles(unittest.TestCase):
    def test_consecutive_user_messages_are_merged(self):
        p = providers.AnthropicProvider(api_key="sk-test", model="claude-x")
        msgs = [
            providers.Message(role="user", content="[用户] 问题"),
            providers.Message(role="user", content="[gemini] 我选 B"),
            providers.Message(role="user", content="[用户] @claude 你怎么看"),
        ]
        captured = {}

        def fake_post(url, payload, headers, timeout=120.0):
            captured["roles"] = [m["role"] for m in payload["messages"]]
            return {"content": [{"type": "text", "text": "好的"}], "usage": {}}

        p._post = fake_post
        p.complete(msgs, system="sys")
        roles = captured["roles"]
        self.assertEqual(roles, ["user"], f"角色没有合并：{roles}")
        for a, b in zip(roles, roles[1:]):
            self.assertNotEqual(a, b, "出现了连续同角色，Anthropic 会 400")

    def test_empty_content_raises(self):
        p = providers.AnthropicProvider(api_key="sk-test", model="claude-x")
        p._post = lambda *a, **k: {"content": [{"type": "thinking"}], "stop_reason": "max_tokens"}
        with self.assertRaises(providers.ProviderError) as ctx:
            p.complete([providers.Message(role="user", content="hi")])
        self.assertIn("空内容", str(ctx.exception))


class TestGeminiUsage(unittest.TestCase):
    def test_thought_tokens_counted(self):
        p = providers.GeminiProvider(api_key="k", model="gemini-2.5-flash")
        p._post = lambda *a, **k: {
            "candidates": [{"content": {"parts": [{"text": "答案"}]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 200,
                "thoughtsTokenCount": 1500,
                "totalTokenCount": 1800,
            },
        }
        reply = p.complete([providers.Message(role="user", content="hi")])
        self.assertEqual(reply.usage.completion_tokens, 1700, "思考 token 没算进输出")

    def test_empty_content_raises(self):
        p = providers.GeminiProvider(api_key="k", model="gemini-2.5-flash")
        p._post = lambda *a, **k: {
            "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]
        }
        with self.assertRaises(providers.ProviderError) as ctx:
            p.complete([providers.Message(role="user", content="hi")])
        self.assertIn("空内容", str(ctx.exception))


class TestProviderGuards(unittest.TestCase):
    def test_reasoning_model_uses_max_completion_tokens(self):
        p = providers.OpenAICompatProvider(
            name="openai", base_url="http://x/v1", api_key="k", model="o3", max_tokens=100
        )
        self.assertEqual(p._token_param(), "max_completion_tokens")
        q = providers.OpenAICompatProvider(
            name="openai", base_url="http://x/v1", api_key="k", model="gpt-4o", max_tokens=100
        )
        self.assertEqual(q._token_param(), "max_tokens")

    def test_oversized_response_rejected(self):
        p = providers.OpenAICompatProvider(
            name="x", base_url="http://x/v1", api_key="k", model="m"
        )
        big = b"x" * (providers.MAX_RESPONSE + 10)

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=None):
                return big[:n] if n else big

        with mock.patch.object(providers, "urlopen", return_value=FakeResp()):
            with self.assertRaises(providers.ProviderError) as ctx:
                p._post("http://x/v1/chat/completions", {}, {})
        self.assertIn("MB", str(ctx.exception))

    def test_local_base_url_marked_free(self):
        p = providers.build_provider("testbot", base_url="http://127.0.0.1:1234/v1", model="m")
        self.assertTrue(p.free, "本地端点应该是已知 0 元，不是未知价格")
        q = providers.build_provider("testbot2", base_url="https://api.example.com/v1", model="m")
        self.assertFalse(q.free)


class TestFrontendContracts(unittest.TestCase):
    """前端是单文件、没有构建步骤，只能对源码做契约断言。

    这些不是「格式好看」的检查 —— 每一条都对应一个用户真的踩到过的
    交互故障（消息串到别的会话、中文输入法误发、停止按钮卡死…）。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parent.parent / "polylogue" / "static" / "index.html").read_text(
            encoding="utf-8"
        )

    def block(self, start, end):
        i = self.html.index(start)
        j = self.html.index(end, i)
        return self.html[i:j]

    def test_stream_is_owned_by_conversation(self):
        block = self.block("async function say", "\nasync function refreshQuiet")
        self.assertIn("const here = () => S.run === run && S.cur === convId", block)
        self.assertIn("if(!here()) continue", block, "切走以后还会把事件画到当前窗口")

    def test_switching_conversation_drops_stale_load(self):
        block = self.block("async function openConv", "function renderMemAv")
        self.assertIn("if(S.cur !== id) return", block, "并发打开会话时结果会画错窗口")

    def test_ime_enter_does_not_send(self):
        idx = self.html.index('$("input").addEventListener("keydown"')
        seg = self.html[idx : idx + 400]
        self.assertIn("isComposing", seg, "中文输入法选词时回车会把消息发出去")

    def test_search_ignores_stale_response(self):
        block = self.block("async function doSearch", "function renderHits")
        self.assertIn("seq !== qSeq", block, "过期搜索结果会覆盖新结果")

    def test_empty_query_never_builds_empty_regex(self):
        block = self.block("function renderHits", "/* ───────── 弹层")
        self.assertIn("if(!S.q)", block, "清空搜索框后空正则会在每个字符间插高亮")
        self.assertNotIn('new RegExp("("+q+")"', block.replace(" ", ""))

    def test_stop_button_always_reenabled(self):
        block = self.block("async function stopRun", "/* ───────── 会话操作")
        self.assertIn("finally", block, "请求失败后停止按钮永久 disabled")
        self.assertIn("btn.disabled = false", block)

    def test_title_write_is_null_safe(self):
        self.assertIn("function setTitle(", self.html)
        block = self.block("function setTitle(", "async function openConv")
        self.assertIn('if(!el) return', block, "行内改名时 $('ttl') 是 null，直接赋值会崩")

    def test_onboarding_uses_detected_provider(self):
        idx = self.html.index("submitOnb({preset:\"省钱\"")
        seg = self.html[idx : idx + 80]
        self.assertIn("provider:p.name", seg, "点「就用这个」会把 key 存到下拉框选中的 provider 名下")

    def test_deleting_current_conversation_clears_state(self):
        block = self.block("async function deleteConv", "async function renameInline")
        self.assertIn("S.conv = null", block, "删掉当前会话后窗口里还留着已删除的对话")

    def test_send_does_not_reload_the_log(self):
        block = self.block("async function say", "\nasync function refreshQuiet")
        self.assertIn("refreshQuiet()", block)
        self.assertNotIn("\n    refresh();", block, "发送完整体重画会抹掉刚追加的错误气泡")

    def test_modal_backdrop_tracks_mousedown(self):
        idx = self.html.index('document.querySelectorAll(".modal")')
        seg = self.html[idx : idx + 500]
        self.assertIn("mousedown", seg, "在输入框里拖选到遮罩上松手会把表单关掉")

    def test_every_referenced_element_exists(self):
        import re

        js = re.search(r"<script>(.*?)</script>", self.html, re.S).group(1)
        ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        used = set(re.findall(r'\$\("([^"]+)"\)', js))
        self.assertEqual(sorted(used - ids), [], "JS 引用了 HTML 里不存在的 id，运行时会 null 崩")

    def test_every_named_handler_is_defined(self):
        import re

        js = re.search(r"<script>(.*?)</script>", self.html, re.S).group(1)
        defined = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", js))
        defined |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", js))
        called = set(re.findall(r"\.on\w+\s*=\s*([A-Za-z_$][\w$]*)\s*;", js))
        called -= {"null", "true", "false", "undefined", "this"}
        self.assertEqual(sorted(called - defined), [], "绑定了未定义的函数名")


class TestBootstrapDiscovery(TmpHome):
    """有 key 却用不了 —— 这类问题用户根本没法自己诊断。"""

    def test_detected_key_without_builtin_preset_is_usable(self):
        from polylogue import bootstrap

        with mock.patch.object(bootstrap, "discover", return_value={"qwen": "sk-qwen"}):
            members, source = bootstrap.resolve_members(None)
        self.assertTrue(members, "检测到 qwen 的 key 却返回「没有可用凭据」")
        self.assertEqual(members[0]["provider"], "qwen")
        self.assertTrue(members[0]["model"])
        self.assertIn("qwen", source)

    def test_openrouter_preset_is_a_copy(self):
        from polylogue import bootstrap

        with mock.patch.object(bootstrap, "discover", return_value={"openrouter": "sk-or"}):
            members, _ = bootstrap.resolve_members(None)
        before = len(bootstrap.MODEL_PRESETS["省钱"])
        members.append({"name": "注入", "provider": "openai", "model": None})
        self.assertEqual(len(bootstrap.MODEL_PRESETS["省钱"]), before, "返回的是模块常量的引用，被调用方改脏了")

    def test_alias_provider_gets_the_key(self):
        from polylogue import bootstrap

        members = [{"name": "克劳德", "provider": "anthropic", "model": "claude-x"}]
        with mock.patch.object(bootstrap, "discover", return_value={"claude": "sk-ant"}):
            provs, active, skipped = bootstrap.build_all(members)
        self.assertEqual(active, ["克劳德"], f"别名 provider 拿不到 key，成员被跳过：{skipped}")
        self.assertEqual(provs["克劳德"].api_key, "sk-ant")

    def test_string_members_are_normalized(self):
        from polylogue import bootstrap

        config.update(members=["claude"])
        members, source = bootstrap.resolve_members(None)
        self.assertIsInstance(members[0], dict)
        self.assertEqual(members[0]["provider"], "claude")
        self.assertEqual(source, "上次保存的配置")

    def test_build_all_skips_garbage_members(self):
        from polylogue import bootstrap

        with mock.patch.object(bootstrap, "discover", return_value={"openai": "sk-o"}):
            provs, active, skipped = bootstrap.build_all([{"nope": 1}, 42])
        self.assertEqual(provs, {})
        self.assertEqual(active, [])
        self.assertEqual(len(skipped), 2)


class TestLauncherErrorClassification(unittest.TestCase):
    def test_bind_errors_recognized(self):
        from polylogue import launcher

        e = OSError("busy")
        e.winerror = 10048
        self.assertTrue(launcher._is_bind_error(e))

    def test_permission_error_not_called_port_problem(self):
        from polylogue import launcher

        # 真实形态：mkdir/open 抛的 PermissionError 第三个参数是路径
        e = PermissionError(13, "Permission denied", r"C:\Users\x\.polylogue\conversations")
        self.assertFalse(launcher._is_bind_error(e), "数据目录不可写被报成端口问题，会把用户引错方向")

    def test_missing_data_dir_error_is_not_bind(self):
        from polylogue import launcher

        self.assertFalse(launcher._is_bind_error(FileNotFoundError(2, "no such file")))


class UnknownCostProvider(FakeProvider):
    """价格表里查不到的模型：cost 是 None，不是 0。

    回复故意用互不重叠的词：否则收敛检测会先以 no_new_info 停下，
    就测不到「未知价格」这条上限了。
    """

    free = False
    _WORDS = [
        "苹果 香蕉 橘子 西瓜",
        "量子 纠缠 相对论 引力",
        "索引 事务 隔离 快照",
        "珊瑚 礁石 潮汐 海藻",
        "铜管 单簧 圆号 定音鼓",
        "火山 岩浆 玄武岩 断层",
    ]

    def __init__(self, name, idx=0):
        super().__init__(name)
        self.model = "totally-unknown-model-xyz"
        self._text = self._WORDS[idx % len(self._WORDS)]

    def complete(self, messages, system=None):
        self.seen.append([m.content for m in messages])
        return providers.Reply(
            text=self._text,
            model=self.model,
            usage=providers.Usage(prompt_tokens=1, completion_tokens=1),
            latency_s=0.0,
        )


class TestUnknownCostStopsSession(unittest.TestCase):
    def test_session_stops_after_too_many_unknown_calls(self):
        names = [f"u{i}" for i in range(6)]
        room_obj = room.Room(names)
        provs = {n: UnknownCostProvider(n, i) for i, n in enumerate(names)}
        ses = session.Session(
            room_obj,
            provs,
            # 必须配了成本上限才谈得上「保护」：没设上限就没有要守的线
            budget=guard.Budget(max_rounds=9, max_calls=99, max_cost_usd=0.5),
            parallel=False,
        )
        result = ses.say("都说说")
        self.assertIsNotNone(result.stop, "价格全未知时没有任何上限拦住它")
        self.assertEqual(result.stop.code, "max_cost_uncertain")
        # 上限在轮次边界检查，所以允许这一轮已发出的成员全部跑完
        # （超额量最多 = 一轮的成员数），但不能再有第二轮
        self.assertEqual(len(result.turns), len(names), "撞了未知价格上限还在继续开会")
        self.assertEqual(ses.rounds, 1)


class TestConfigCache(TmpHome):
    """配置读缓存：快，但不能读到旧值。读旧值就是丢数据。"""

    def test_own_write_visible_immediately(self):
        ct = store.add_contact("模型A", "openai")
        self.assertEqual(store.contact_by_id(ct["id"])["name"], "模型A")
        store.update_contact(ct["id"], name="模型B")
        self.assertEqual(store.contact_by_id(ct["id"])["name"], "模型B", "自己刚写的都读不到")

    def test_external_edit_visible_after_ttl(self):
        ct = store.add_contact("模型A", "openai")
        store.contact_by_id(ct["id"])  # 先让缓存热起来
        raw = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
        for c in raw["contacts"]:
            if c["id"] == ct["id"]:
                c["name"] = "外部改的"
        config.CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        time.sleep(config._CACHE_TTL + 0.1)
        self.assertEqual(store.contact_by_id(ct["id"])["name"], "外部改的", "手工编辑配置后读不到")

    def test_load_returns_independent_copies(self):
        store.load_state()  # 先把默认通讯录写进 config.json
        first = config.load()
        self.assertIn("contacts", first)
        first["contacts"].clear()
        first["theme"] = "污染"
        second = config.load()
        self.assertTrue(second["contacts"], "改 load() 的返回值污染了缓存")
        self.assertNotEqual(second.get("theme"), "污染")

    def test_list_conversations_sees_new_conversation(self):
        store.create_conversation("新建的", ["claude"])
        titles = [c["title"] for c in store.list_conversations()]
        self.assertIn("新建的", titles, "刚建的会话在列表里看不到")

    def test_deleted_conversation_disappears(self):
        conv = store.create_conversation("待删", ["claude"])
        self.assertEqual(len(store.list_conversations()), 1)
        store.delete_conversation(conv["id"])
        self.assertEqual(store.list_conversations(), [], "删掉的会话还在列表里")

    def test_renamed_conversation_shows_new_title(self):
        conv = store.create_conversation("旧名字", ["claude"])
        store.list_conversations()
        store.rename_conversation(conv["id"], "新名字")
        self.assertEqual(store.list_conversations()[0]["title"], "新名字")


class TestStoreRejectsBadTypes(TmpHome):
    """store 不该把非字符串字段落盘。

    这些字段会流进 `PRESETS.get(provider)`、文件路径、正则 —— 存进去
    一个 dict，下一次加载会在别处炸，现场看起来跟写入点毫无关系。
    """

    def test_add_contact_coerces_types(self):
        c = store.add_contact("名字", {"bad": 1}, model=["m"], category={"c": 1}, role=None)
        self.assertEqual(c["provider"], "openrouter", "dict 类型的 provider 被存进去了")
        self.assertIsInstance(c["model"], str)
        self.assertEqual(c["category"], "常用")
        self.assertIsInstance(c["role"], str)

    def test_add_contact_keeps_numeric_model_as_string(self):
        c = store.add_contact("数字模型", "openai", model=4)
        self.assertEqual(c["model"], "4")

    def test_persisted_contact_survives_reload(self):
        store.add_contact("脏数据", {"bad": 1})
        state = store.load_state()  # 校验器会把不合法字段退回默认
        for c in state["contacts"]:
            self.assertIsInstance(c["provider"], str, f"{c} 的 provider 不是字符串")
            self.assertIsInstance(c["category"], str)

    def test_update_contact_ignores_empty_name(self):
        c = store.add_contact("原名字", "openai")
        store.update_contact(c["id"], name="")
        self.assertEqual(store.contact_by_id(c["id"])["name"], "原名字", "空名字把联系人清空了")

    def test_update_contact_never_touches_id(self):
        c = store.add_contact("某模型", "openai")
        store.update_contact(c["id"], id="hacked", cid="hacked")
        self.assertEqual(store.contact_by_id(c["id"])["id"], c["id"], "联系人 id 被改掉了")

    def test_categories_stay_strings(self):
        store.add_contact("分类脏数据", "openai", category={"x": 1})
        state = store.load_state()
        self.assertTrue(all(isinstance(x, str) for x in state["categories"]), state["categories"])


class TestErrorMessagesAreActionable(unittest.TestCase):
    """错误文案要告诉用户下一步做什么，不是把上游 JSON 甩给他。"""

    def test_region_block_says_switch_model_not_switch_key(self):
        msg = providers._explain(
            "gpt", 403, '{"error":{"message":"This model is not available in your region."}}'
        )
        self.assertIn("地区", msg)
        self.assertIn("换一个模型", msg, "地区限制被说成 key 有问题，用户会去反复重配")

    def test_retired_model_says_switch_model(self):
        msg = providers._explain(
            "gpt", 404, '{"error":{"message":"x is no longer available on OpenRouter"}}'
        )
        self.assertIn("下线", msg)

    def test_plain_403_still_shows_detail(self):
        msg = providers._explain("gpt", 403, '{"error":{"message":"forbidden by policy"}}')
        self.assertIn("forbidden by policy", msg)


class TestConfigBom(TmpHome):
    """带 UTF-8 BOM 的配置文件不能被当成损坏。

    记事本和 PowerShell 5 的 `Set-Content -Encoding utf8` 都会写 BOM。
    用 `utf-8` 读会带出一个 \\ufeff，json.loads 报错 → 整份配置被当成
    损坏 → key、成员、主题**静默全丢**，用户看到的是「本机没有 key」。
    """

    def test_bom_config_is_read(self):
        body = {
            "api_keys": {"openrouter": "sk-or-v1-" + "a" * 40},
            "members": [{"name": "评论家", "provider": "openrouter", "model": "x"}],
            "theme": "light",
        }
        config.CONFIG_PATH.write_bytes(b"\xef\xbb\xbf" + json.dumps(body, ensure_ascii=False).encode())
        config._bump_generation()
        loaded = config.load()
        self.assertEqual(loaded["theme"], "light")
        self.assertEqual(len(loaded["members"]), 1)
        self.assertTrue(loaded["api_keys"]["openrouter"].startswith("sk-or"))

    def test_save_key_does_not_wipe_bom_config(self):
        config.CONFIG_PATH.write_bytes(
            b"\xef\xbb\xbf"
            + json.dumps(
                {"api_keys": {"openai": "sk-" + "b" * 40}, "theme": "light", "members": [{"name": "x"}]},
                ensure_ascii=False,
            ).encode()
        )
        config._bump_generation()
        credentials.save_key("openrouter", "sk-or-v1-" + "c" * 40)
        raw = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8-sig"))
        self.assertEqual(raw["theme"], "light", "save_key 把主题清掉了")
        self.assertEqual(len(raw["members"]), 1, "save_key 把成员清掉了")
        self.assertIn("openai", raw["api_keys"], "save_key 把别的 key 清掉了")


class TestRedactPatterns(unittest.TestCase):
    """上游把凭据回显进错误信息时，终端和日志里不能出现原文。"""

    def test_google_key(self):
        key = "AIza" + "SyC1234567890abcdefghijklmnopqrstuv"
        out = credentials.redact(f"invalid token {key}", secrets=[])
        self.assertNotIn(key, out)

    def test_key_in_query_string(self):
        out = credentials.redact("see https://api.example.com/v1?key=abc123def456ghi789&x=1", secrets=[])
        self.assertNotIn("abc123def456ghi789", out)
        self.assertIn("x=1", out, "把 query 里无关参数也吃掉了")

    def test_new_vendor_prefixes(self):
        for key in ("nvapi-" + "a" * 24, "pplx-" + "b" * 24, "hf_" + "c" * 32):
            out = credentials.redact(f"token {key}", secrets=[])
            self.assertNotIn(key, out, key[:6])

    def test_ordinary_identifiers_untouched(self):
        text = "参见 api-key-rotation-policy 和 sk-learn-pipeline-2026"
        self.assertEqual(credentials.redact(text, secrets=[]), text, "普通标识符被误伤")

    def test_sources_match_discover_priority(self):
        """来源标注必须和 discover() 的优先级一致，否则掩码和来源对不上。"""
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-v1-" + "e" * 40}):
            config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            config.CONFIG_PATH.write_text(
                json.dumps({"api_keys": {"openrouter": "sk-or-v1-" + "f" * 40}}), encoding="utf-8"
            )
            config._bump_generation()
            self.assertIn("e" * 10, credentials.discover()["openrouter"], "env 应该压过 config")
            self.assertEqual(credentials.credential_sources()["openrouter"], "环境变量")


class TestConfigValueValidation(TmpHome):
    """手改配置写错类型/范围，不该让程序变成另一副样子。"""

    def _load(self, body: dict) -> dict:
        config.CONFIG_PATH.write_text(json.dumps(body), encoding="utf-8")
        config._bump_generation()
        return config.load()

    def test_bad_types_fall_back_to_defaults(self):
        data = self._load(
            {"budget": {"max_rounds": "abc", "max_calls": -3, "max_cost_usd": float("nan")}, "members": "openrouter", "theme": 5}
        )
        self.assertEqual(data["budget"]["max_rounds"], 6)
        self.assertEqual(data["budget"]["max_calls"], 24)
        self.assertEqual(data["budget"]["max_cost_usd"], 0.5)
        self.assertEqual(data["members"], [], "字符串被拆成了单字母成员")
        self.assertEqual(data["theme"], "dark")

    def test_null_budget_and_list_context(self):
        data = self._load({"budget": None, "context": [1, 2]})
        self.assertEqual(data["budget"], {"max_rounds": 6, "max_calls": 24, "max_cost_usd": 0.5})
        self.assertEqual(data["context"]["keep_recent"], 8)

    def test_valid_values_survive(self):
        data = self._load({"budget": {"max_rounds": 3, "max_calls": 9, "max_cost_usd": 2.5}, "theme": "light"})
        self.assertEqual(data["budget"], {"max_rounds": 3, "max_calls": 9, "max_cost_usd": 2.5})
        self.assertEqual(data["theme"], "light")

    def test_corrupt_file_makes_load_and_load_shared_agree(self):
        config.CONFIG_PATH.write_text('{"theme": "light"}', encoding="utf-8")
        config._bump_generation()
        self.assertEqual(config.load()["theme"], "light")
        config.CONFIG_PATH.write_text("{ 半截", encoding="utf-8")
        time.sleep(config._CACHE_TTL + 0.05)
        self.assertEqual(config.load()["theme"], "dark")
        self.assertEqual(config.load_shared()["theme"], "dark", "load_shared 还拿着旧缓存")

    def test_save_rejects_non_dict(self):
        with self.assertRaises(TypeError):
            config.save(["a"])

    def test_failed_write_leaves_no_temp_file(self):
        with self.assertRaises(TypeError):
            config.save({"x": object()})
        self.assertEqual(list(config.CONFIG_PATH.parent.glob(".*.tmp")), [])


class TestSaveKeyLock(TmpHome):
    def test_concurrent_update_is_not_lost(self):
        """save_key 读-改-写必须和 config.update 串行。"""
        config.CONFIG_PATH.write_text(json.dumps({"theme": "dark", "api_keys": {}}), encoding="utf-8")
        config._bump_generation()
        done = threading.Event()

        def slow_save():
            credentials.save_key("openrouter", "sk-or-v1-" + "b" * 40)
            done.set()

        t = threading.Thread(target=slow_save)
        t.start()
        time.sleep(0.01)
        config.update(theme="light")
        done.wait(5)
        t.join(5)
        final = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8-sig"))
        self.assertEqual(final["theme"], "light", "并发的 theme 被 save_key 覆盖了")
        self.assertIn("openrouter", final["api_keys"])


class TestMentionsAreNotEmails(unittest.TestCase):
    """邮箱里的 @ 不是点名。"""

    def test_email_does_not_mention(self):
        r = Room(["claude", "a"])
        self.assertEqual(r.mentions("发到 bob@claude.ai"), [])
        self.assertEqual(r.mentions("邮箱 a@all.com"), [], "邮箱触发了全员广播")

    def test_real_all_still_broadcasts(self):
        r = Room(["claude", "a"])
        self.assertEqual(sorted(r.mentions("@all 都看看")), ["a", "claude"])

    def test_email_does_not_block_the_message(self):
        r = Room(["a", "b"])
        self.assertEqual(r.unmatched_mentions("发到 me@example.com"), [])
        turn = Turn(speaker="user", content="发到 me@example.com", role="user")
        self.assertEqual(r.next_speakers(turn), ["a", "b"], "邮箱让整条消息无人应答")

    def test_chinese_name_followed_by_chinese(self):
        r = Room(["小助手"])
        self.assertEqual(r.mentions("@小助手你好，看看这个"), ["小助手"], "中文没有词边界，应该匹配上")
        self.assertEqual(r.mentions("@小助手 你好"), ["小助手"])

    def test_latin_prefix_still_strict(self):
        r = Room(["gpt"])
        self.assertEqual(r.mentions("@gpt4 在吗"), [], "@gpt 不该命中 @gpt4")

    def test_misspelled_mention_is_reported(self):
        r = Room(["a"])
        self.assertEqual(r.unmatched_mentions("@nosuch 在吗"), ["@nosuch"])


class TestNoveltyKeepsDigitsAndLetters(unittest.TestCase):
    """词袋丢数字会让「方案 A」和「方案 B」被判成没新信息。"""

    def test_single_letter_differs(self):
        self.assertGreater(guard.novelty("建议用方案 A", ["建议用方案 B"]), 0)

    def test_digit_differs(self):
        self.assertGreater(guard.novelty("预计 3 天能做完", ["预计 7 天能做完"]), 0)
        self.assertGreater(guard.novelty("第 1 步做迁移", ["第 2 步做迁移"]), 0)

    def test_identical_is_still_zero(self):
        self.assertEqual(guard.novelty("完全一样的话", ["完全一样的话"]), 0.0)


class TestPresetModelsArePriced(unittest.TestCase):
    """内置 preset 的默认模型必须有价格。

    没有价格的后果不是「显示 ?」这么轻：未知成本让成本护栏按次数兜底，
    四次调用后会话被停掉，而用户用的正是我们给的默认模型。
    """

    def test_every_remote_preset_has_a_price(self):
        missing = []
        for name, preset in PRESETS.items():
            model = preset.get("model")
            if not model or not preset.get("env"):
                continue  # 本地端点（ollama/lmstudio）成本是真的 0，不走价格表
            if pricing.cost_of(model, 1000, 1000) is None:
                missing.append(f"{name} → {model}")
        self.assertEqual(missing, [], "这些内置模型查不到价格：" + "; ".join(missing))

    def test_model_presets_have_prices(self):
        missing = []
        for preset_name, items in bootstrap.MODEL_PRESETS.items():
            for item in items:
                if pricing.cost_of(item["model"], 1000, 1000) is None:
                    missing.append(f"{preset_name} → {item['model']}")
        self.assertEqual(missing, [], "这些预设模型查不到价格：" + "; ".join(missing))


class TestBuildAllDedupes(unittest.TestCase):
    def test_duplicate_member_is_skipped(self):
        rows = [
            {"name": "a", "provider": "openrouter", "model": "x"},
            {"name": "a", "provider": "openrouter", "model": "x"},
        ]
        providers, active, skipped = bootstrap.build_all(rows)
        self.assertEqual(active, ["a"], "同名成员被建了两次，会双倍付费")
        self.assertTrue(any("重名" in s for s in skipped), skipped)

    def test_resolve_members_dedupes_explicit(self):
        rows, _ = bootstrap.resolve_members(["claude", "claude"])
        self.assertEqual(len(rows), 1)


class TestConvergenceIsNotSubstringMatching(unittest.TestCase):
    """收敛判断不能只看「有没有出现这几个字」。"""

    def test_plain_convergence_still_detected(self):
        for text in ("没有更多补充", "我同意这个结论。", "讨论可以结束", "结论已经清楚了，没有异议。"):
            self.assertTrue(guard.looks_converged(text), text)

    def test_contrast_after_phrase_is_not_convergence(self):
        for text in (
            "他说「没有异议」只是复述别人的话",
            "没有更多补充的话我就继续问下一个问题",
            "NO FURTHER COMMENTS 但我还要说一点",
        ):
            self.assertFalse(guard.looks_converged(text), text)


class TestEstimateTokensNeverZero(unittest.TestCase):
    def test_non_empty_text_costs_at_least_one(self):
        for text in ("a", "𠀀", "🇨🇳", "x"):
            self.assertGreaterEqual(estimate_tokens(text), 1, repr(text))

    def test_empty_is_zero(self):
        self.assertEqual(estimate_tokens(""), 0)


class TestCancelReportedAtRoundEnd(unittest.TestCase):
    """请求飞行途中被取消、且模型没 @ 人时，要报「已手动停止」。"""

    def test_cancel_before_awaiting_user(self):
        r = Room(["a"])
        cancelled = {"yes": False}
        ses = session.Session(
            r,
            {"a": providers.Provider()},
            budget=guard.Budget(max_rounds=3),
            should_stop=lambda: cancelled["yes"],
        )
        cancelled["yes"] = True
        result = ses._run(["a"])
        self.assertEqual(result.stop.code, "cancelled")


if __name__ == "__main__":
    unittest.main()
