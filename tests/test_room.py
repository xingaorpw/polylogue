"""房间路由测试。"""

import unittest

from polylogue.room import USER, Room


class TestMentions(unittest.TestCase):
    def setUp(self):
        self.room = Room(["gemini", "grok", "deepseek"])

    def test_single_mention(self):
        self.assertEqual(self.room.mentions("@grok 你怎么看"), ["grok"])

    def test_multiple_mentions_keep_order(self):
        self.assertEqual(self.room.mentions("@deepseek @gemini 都说说"), ["deepseek", "gemini"])

    def test_mention_all_expands_to_everyone(self):
        self.assertEqual(self.room.mentions("@all 开始"), ["gemini", "grok", "deepseek"])

    def test_case_insensitive(self):
        self.assertEqual(self.room.mentions("@Gemini 你说"), ["gemini"])

    def test_unknown_mention_ignored(self):
        self.assertEqual(self.room.mentions("@gpt4 你说"), [])

    def test_email_is_not_a_mention(self):
        """a@b.com 里的 @b 不该被当成点名。"""
        self.assertEqual(self.room.mentions("发到 me@example.com"), [])

    def test_duplicate_mention_counted_once(self):
        self.assertEqual(self.room.mentions("@grok @grok 你说"), ["grok"])

    def test_mentions_user(self):
        self.assertTrue(self.room.mentions_user("这个要 @user 定"))
        self.assertFalse(self.room.mentions_user("交给 @grok"))


class TestNextSpeakers(unittest.TestCase):
    def setUp(self):
        self.room = Room(["gemini", "grok", "deepseek"])

    def test_user_without_mention_asks_everyone(self):
        """「同时问多个模型」就是这种：没点名 → 全员回答。"""
        turn = self.room.add_user("这个方案怎么样？")
        self.assertEqual(self.room.next_speakers(turn), ["gemini", "grok", "deepseek"])

    def test_user_with_mention_asks_only_them(self):
        turn = self.room.add_user("@grok 说说")
        self.assertEqual(self.room.next_speakers(turn), ["grok"])

    def test_model_mention_routes_to_named(self):
        turn = self.room.add_model("gemini", "我认为不行，@deepseek 你验证一下")
        self.assertEqual(self.room.next_speakers(turn), ["deepseek"])

    def test_model_cannot_mention_itself(self):
        turn = self.room.add_model("gemini", "@gemini 自己再想想")
        self.assertEqual(self.room.next_speakers(turn), [])

    def test_model_without_mention_stops(self):
        """模型不点名 = 主张收敛，对话停给用户。"""
        turn = self.room.add_model("gemini", "我认为可以定了。")
        self.assertEqual(self.room.next_speakers(turn), [])

    def test_model_mentioning_user_stops(self):
        turn = self.room.add_model("gemini", "这个得 @user 拍板")
        self.assertEqual(self.room.next_speakers(turn), [])


class TestContext(unittest.TestCase):
    def setUp(self):
        self.room = Room(["gemini", "grok"])
        self.room.add_user("方案 A 还是 B？")
        self.room.add_model("gemini", "我选 A")
        self.room.add_model("grok", "我选 B")

    def test_own_turns_are_assistant(self):
        ctx = self.room.context_for("gemini")
        mine = [m for m in ctx if m.role == "assistant"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].content, "我选 A")

    def test_others_are_user_with_speaker_prefix(self):
        """别人的发言要包成 user 并标上是谁说的，否则模型会以为是自己在说。"""
        ctx = self.room.context_for("gemini")
        grok_msgs = [m for m in ctx if "我选 B" in m.content]
        self.assertEqual(len(grok_msgs), 1)
        self.assertEqual(grok_msgs[0].role, "user")
        self.assertIn("[grok]", grok_msgs[0].content)

    def test_user_turn_prefixed_as_user(self):
        ctx = self.room.context_for("gemini")
        first = ctx[0]
        self.assertEqual(first.role, "user")
        self.assertIn("[用户]", first.content)

    def test_failed_turns_excluded(self):
        self.room.add_model("grok", "", error="超时")
        ctx = self.room.context_for("gemini")
        self.assertFalse(any(m.content == "" for m in ctx))

    def test_system_prompt_lists_other_members(self):
        system = self.room.system_for("gemini")
        self.assertIn("grok", system)
        self.assertNotIn("还有：gemini", system)

    def test_system_prompt_includes_role(self):
        room = Room(["gemini"], roles={"gemini": "负责找漏洞"})
        self.assertIn("负责找漏洞", room.system_for("gemini"))


class TestStats(unittest.TestCase):
    def test_cost_sums_known_and_flags_unknown(self):
        room = Room(["a", "b"])
        room.add_model("a", "x", cost=0.5)
        room.add_model("b", "y", cost=None)
        total, unknown = room.total_cost()
        self.assertAlmostEqual(total, 0.5)
        self.assertTrue(unknown)

    def test_failed_turn_not_counted_in_cost(self):
        room = Room(["a"])
        room.add_model("a", "", cost=None, error="挂了")
        total, unknown = room.total_cost()
        self.assertEqual(total, 0.0)
        self.assertFalse(unknown)

    def test_usage_accumulates(self):
        from polylogue.providers import Usage

        room = Room(["a"])
        room.add_model("a", "x", usage=Usage(10, 5))
        room.add_model("a", "y", usage=Usage(20, 3))
        self.assertEqual(room.total_usage.total, 38)


class TestValidation(unittest.TestCase):
    def test_empty_room_rejected(self):
        with self.assertRaises(ValueError):
            Room([])


if __name__ == "__main__":
    unittest.main()
