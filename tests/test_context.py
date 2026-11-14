"""上下文压缩的测试。

重点不是「函数返回对不对」，而是「它到底省了多少 token」——
这个数字必须能被量出来，否则「优化了上下文」就是空话。
"""

import unittest

from polylogue.context import compress, estimate_tokens, summarize
from polylogue.room import Room
from polylogue.providers import Usage
from polylogue.room import Turn
from tests.support import safe_print


def make_turns(n, speaker="a", body="这是一段比较长的发言内容，用来模拟真实的模型输出长度。"):
    return [Turn(speaker=speaker, content=body, role="assistant") for _ in range(n)]


class TestEstimate(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_chinese_costs_more_than_ascii_per_char(self):
        zh = estimate_tokens("这是一段中文" * 10)
        en = estimate_tokens("abcdefghij" * 10)
        self.assertGreater(zh, en, "同样字符数，中文应该估得更高")

    def test_does_not_underestimate_chinese(self):
        """中文按 1 字 1 token 估，不能低于字数。低估会导致预算失效。"""
        text = "数据库连接池" * 20
        self.assertGreaterEqual(estimate_tokens(text), 120)


class TestSummarize(unittest.TestCase):
    def test_keeps_first_sentence(self):
        turns = make_turns(1, body="结论是第一句。后面还有很多展开，压缩时应该丢掉。")
        digest = summarize(turns)
        self.assertIn("结论是第一句", digest)
        self.assertNotIn("后面还有很多展开", digest)

    def test_keeps_mentions(self):
        """@名字 决定交接关系，压缩时必须保住，否则对话链会断。"""
        turns = make_turns(1, body="这里有问题。@builder 你怎么看？")
        digest = summarize(turns)
        self.assertIn("@builder", digest)

    def test_marks_failed_turns(self):
        t = Turn(speaker="a", content="", role="assistant", error="超时")
        self.assertIn("失败", summarize([t]))

    def test_shorter_than_original(self):
        turns = make_turns(10, body="这是一段很长的发言内容。" * 8)
        digest = summarize(turns)
        raw = sum(estimate_tokens(t.content) for t in turns)
        self.assertLess(estimate_tokens(digest), raw)


class TestCompress(unittest.TestCase):
    def test_no_compression_when_short(self):
        turns = make_turns(3)
        digest, kept, stats = compress(turns, keep_recent=8)
        self.assertIsNone(digest)
        self.assertEqual(len(kept), 3)
        self.assertFalse(stats["compressed"])

    def test_compresses_when_long(self):
        turns = make_turns(20, body="这是一段很长的发言内容。" * 8)
        digest, kept, stats = compress(turns, keep_recent=8)
        self.assertIsNotNone(digest)
        self.assertEqual(len(kept), 8)
        self.assertTrue(stats["compressed"])
        self.assertEqual(stats["older_turns"], 12)
        self.assertLess(stats["digest_tokens"], stats["raw_tokens"])

    def test_short_turns_are_not_compressed(self):
        """压不小就别压 —— 短发言加前缀后摘要反而更长，那是负优化。"""
        turns = make_turns(20, body="好")
        digest, kept, stats = compress(turns, keep_recent=8)
        self.assertIsNone(digest, "不该产出比原文更长的摘要")
        self.assertFalse(stats["compressed"])
        self.assertTrue(stats.get("skipped"))
        self.assertEqual(len(kept), 20, "跳过后应该原样保留全部发言")

    def test_keep_recent_zero(self):
        turns = make_turns(5, body="这是一段很长的发言内容。" * 8)
        digest, kept, _ = compress(turns, keep_recent=0)
        self.assertIsNotNone(digest)
        self.assertEqual(len(kept), 0)


class TestRoomUsesCompression(unittest.TestCase):
    def test_context_shrinks(self):
        room = Room(["a", "b"])
        for i in range(20):
            room.add_user(f"第{i}个问题，请分析这个场景下的并发风险。")
            room.add_model("a", f"第{i}轮回答：" + "这里是一段较长的分析内容。" * 5, usage=Usage(10, 5))
            room.add_model("b", f"第{i}轮补充：" + "另一段较长的分析内容。" * 5, usage=Usage(10, 5))

        naive = room.context_for("a", keep_recent=None)
        packed = room.context_for("a", keep_recent=8)

        naive_tokens = sum(estimate_tokens(m.content) for m in naive)
        packed_tokens = sum(estimate_tokens(m.content) for m in packed)

        self.assertLess(len(packed), len(naive), "压缩后消息条数应该更少")
        self.assertLess(packed_tokens, naive_tokens * 0.7, "压缩后 token 应明显下降")
        self.assertTrue(room.last_compression["compressed"])

    def test_digest_goes_first(self):
        """摘要必须放在最前面，否则模型会把它当成最新指令。"""
        room = Room(["a", "b"])
        for i in range(20):
            room.add_user(f"问题{i}")
            room.add_model("a", f"回答{i}。细节细节细节。", usage=Usage(10, 5))
        msgs = room.context_for("a", keep_recent=4)
        self.assertIn("较早的发言已压缩", msgs[0].content)
        self.assertEqual(msgs[0].speaker, "summary")

    def test_own_turns_stay_assistant(self):
        room = Room(["a", "b"])
        for i in range(20):
            room.add_model("a", f"我说的话{i}。", usage=Usage(10, 5))
        msgs = room.context_for("a", keep_recent=4)
        assistant = [m for m in msgs if m.role == "assistant"]
        self.assertTrue(all(m.speaker == "a" for m in assistant))


class TestCompressionSavesRealMoney(unittest.TestCase):
    """端到端量化：10 轮 3 模型，压缩到底省了多少 prompt token。

    用真实模型的输出长度（多句、约 200 字）。单句短发言压不动是正常的，
    那种情况有单独的守卫测试（test_short_turns_are_not_compressed）。
    """

    REPLY = (
        "这一轮的核心风险是写冲突和元数据膨胀，这是 CRDT 方案的固有代价。"
        "建议用分片加分段压缩来控制，每片独立合并，避免全局锁。"
        "具体到实现还需要讨论快照的触发时机，以及清理策略对延迟的影响。"
    )

    ROUNDS = 30
    BUDGET = 1500

    def _rounds(self, keep_recent, budget_tokens=None, rounds=None):
        """返回每一轮里单个模型看到的上下文 token 数。"""
        room = Room(["a", "b", "c"])
        per_round = []
        for i in range(rounds or self.ROUNDS):
            room.add_user(f"第{i}轮：我们要设计一个高并发写入的文档服务，请评估风险。")
            for name in ("a", "b", "c"):
                room.add_model(name, f"{name}：{self.REPLY}", usage=Usage(800, 120))
            msgs = room.context_for("a", keep_recent=keep_recent, budget_tokens=budget_tokens)
            per_round.append(sum(estimate_tokens(m.content) for m in msgs))
        return per_round

    def test_growth_is_capped(self):
        """真正的验收标准：上下文撞到预算就封顶，不再随轮次增长。

        「省百分之多少」只是参考数字。需求是：第 30 轮的上下文不该比第 10 轮
        大很多，更不该像未压缩那样一路涨下去。
        """
        naive = self._rounds(None)
        packed = self._rounds(8, budget_tokens=self.BUDGET)

        safe_print(f"\n  未压缩: {naive[0]} → {naive[-1]}（{self.ROUNDS} 轮）")
        safe_print(f"  压缩后: {packed[0]} → {packed[-1]}")
        safe_print(f"  压缩后峰值: {max(packed)}  预算: {self.BUDGET}")

        # 对照组必须一路涨，否则这个测试没意义
        self.assertGreater(naive[-1], naive[1] * 8, "对照组应该持续增长")

        # 压缩后必须封顶
        self.assertLessEqual(max(packed), self.BUDGET * 1.15, "压缩后不该超过预算")
        # 撞顶之后必须持平，不能还在慢慢涨
        tail = packed[-6:]
        self.assertLess(max(tail) - min(tail), 60, f"撞顶后应该持平，实际还在涨：{tail}")

    def test_saves_more_as_conversation_grows(self):
        """轮次越多，省得越多 —— 因为未压缩是平方级，压缩是常数级。"""
        naive = self._rounds(None)
        packed = self._rounds(8, budget_tokens=self.BUDGET)
        saved = 1 - sum(packed) / sum(naive)
        safe_print(f"\n  {self.ROUNDS} 轮累计节省: {saved:.1%}")
        self.assertGreater(saved, 0.6, "长对话里压缩应该省掉大部分")

    def test_budget_is_a_hard_cap(self):
        """给了预算就是硬上限，不是「大概小一点」。"""
        room = Room(["a", "b"])
        for i in range(30):
            room.add_user(f"问题{i}")
            room.add_model("a", f"{i}：" + self.REPLY * 2, usage=Usage(10, 5))
            room.add_model("b", f"{i}：" + self.REPLY * 2, usage=Usage(10, 5))

        budget = 400
        msgs = room.context_for("a", keep_recent=2, budget_tokens=budget)
        used = sum(estimate_tokens(m.content) for m in msgs)
        self.assertLessEqual(used, budget, f"上下文 {used} token 超过了预算 {budget}")
        self.assertGreater(room.last_compression["dropped"], 0, "超预算时应该真的丢了行")

    def test_dropped_are_marked(self):
        """丢掉的内容要明说，不能让模型以为那就是全部历史。"""
        room = Room(["a"])
        for i in range(30):
            room.add_model("a", f"{i}：" + self.REPLY * 2, usage=Usage(10, 5))
        msgs = room.context_for("a", keep_recent=1, budget_tokens=200)
        self.assertIn("已省略", msgs[0].content)


if __name__ == "__main__":
    unittest.main()
