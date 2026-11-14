"""编排循环测试。全部用 MockProvider，不联网、不花钱。"""

import unittest

from polylogue.guard import Budget, novelty, looks_converged
from polylogue.providers import MockProvider, ProviderError
from polylogue.room import Room
from polylogue.session import Session


def build(members, scripts=None, budget=None, parallel=True, min_novelty=0.0):
    scripts = scripts or {}
    providers = {m: MockProvider(m, script=list(scripts.get(m, []))) for m in members}
    room = Room(members)
    session = Session(room, providers, budget=budget, parallel=parallel, min_novelty=min_novelty)
    return room, session, providers


class TestRoutingLoop(unittest.TestCase):
    def test_user_without_mention_gets_all_models(self):
        room, session, _ = build(["a", "b"], {"a": ["A 的回答"], "b": ["B 的回答"]})
        result = session.say("说说看")
        speakers = [t.speaker for t in result.turns]
        self.assertEqual(speakers, ["a", "b"])

    def test_model_can_hand_off_to_another(self):
        """a 点名 b → 下一轮 b 说话 → 这轮就结束了。"""
        room, session, _ = build(
            ["a", "b"],
            {"a": ["我认为 X，@b 你补充"], "b": ["补充：Y"]},
        )
        result = session.say("@a 说说")
        self.assertEqual([t.speaker for t in result.turns], ["a", "b"])

    def test_handoff_chain_three_models(self):
        room, session, _ = build(
            ["a", "b", "c"],
            {"a": ["@b 你看"], "b": ["@c 你看"], "c": ["我看完了，可以定了"]},
        )
        result = session.say("@a 开始")
        self.assertEqual([t.speaker for t in result.turns], ["a", "b", "c"])
        self.assertEqual(result.stop.code, "converged")

    def test_stops_when_nobody_mentioned(self):
        room, session, _ = build(["a"], {"a": ["我说完了"]})
        result = session.say("@a 说说")
        self.assertEqual(result.stop.code, "awaiting_user")

    def test_ping_pong_between_two_models(self):
        """a→b→a→b… 必须被轮次上限截断，不能无限转。"""
        room, session, _ = build(
            ["a", "b"],
            {
                "a": ["@b 继续"] * 10,
                "b": ["@a 继续"] * 10,
            },
            budget=Budget(max_rounds=4),
        )
        result = session.say("@a 开始")
        self.assertEqual(result.stop.code, "max_rounds")
        self.assertEqual(session.rounds, 4)


class TestParallelSemantics(unittest.TestCase):
    def test_same_round_models_do_not_see_each_other(self):
        """同一轮并行问的时候，a 的上下文里不该有 b 这一轮的话。"""
        room, session, providers = build(
            ["a", "b"],
            {"a": ["A 说"], "b": ["B 说"]},
        )
        session.say("同时问你们俩")
        a_context = providers["a"].calls[0]
        self.assertFalse(any("B 说" in m.content for m in a_context))

    def test_sequential_mode_does_see_previous(self):
        room, session, providers = build(
            ["a", "b"],
            {"a": ["A 说"], "b": ["B 说"]},
            parallel=False,
        )
        session.say("问你们俩")
        b_context = providers["b"].calls[0]
        self.assertTrue(any("A 说" in m.content for m in b_context))

    def test_next_round_sees_previous_round(self):
        room, session, providers = build(
            ["a", "b"],
            {"a": ["@b 你补充"], "b": ["补充完毕"]},
        )
        session.say("@a 说说")
        b_context = providers["b"].calls[0]
        self.assertTrue(any("你补充" in m.content for m in b_context))


class TestBudget(unittest.TestCase):
    def test_max_calls_stops(self):
        room, session, _ = build(
            ["a", "b"],
            {"a": ["@b 继续"] * 10, "b": ["@a 继续"] * 10},
            budget=Budget(max_rounds=99, max_calls=2),
        )
        result = session.say("@a 开始")
        self.assertEqual(result.stop.code, "max_calls")

    def test_max_cost_stops(self):
        from polylogue.providers import Usage

        providers = {"a": MockProvider("a", script=["@a 再想"] * 10)}
        # 让 mock 的模型名有价格，成本才不是 None
        providers["a"].model = "gpt-4o"
        providers["a"].usage = Usage(1_000_000, 1_000_000)
        room = Room(["a"])
        session = Session(room, providers, budget=Budget(max_rounds=99, max_cost_usd=1.0))

        # MockProvider 固定返回 Usage(10,5)，手动塞一个大成本回合来触发
        session.say("@a 开始")
        room.add_model("a", "贵的回复", cost=5.0)
        stop = session._stop_reason()
        self.assertIsNotNone(stop)
        self.assertEqual(stop.code, "max_cost")

    def test_unknown_cost_near_limit_stops(self):
        """查不到价格的调用，成本是下界，不能当成安全放行。"""
        room = Room(["a"])
        session = Session(room, {"a": MockProvider("a")}, budget=Budget(max_cost_usd=1.0))
        room.add_model("a", "x", cost=0.5)  # 已知 0.5
        room.add_model("a", "y", cost=None)  # 未知
        stop = session._stop_reason()
        self.assertIsNotNone(stop)
        self.assertEqual(stop.code, "max_cost_uncertain")


class TestConvergence(unittest.TestCase):
    def test_converged_text_stops(self):
        room, session, _ = build(["a", "b"], {"a": ["@b 你还有补充吗"], "b": ["没有更多补充"]})
        result = session.say("@a 开始")
        self.assertEqual(result.stop.code, "converged")

    def test_low_novelty_stops(self):
        """两个模型说同一件事 —— 继续下去就是烧钱。"""
        room, session, _ = build(
            ["a", "b"],
            {
                "a": ["这个方案的风险在于并发写入会导致数据不一致"],
                "b": ["这个方案的风险在于并发写入会导致数据不一致"],
            },
            min_novelty=0.5,
        )
        result = session.say("都评一下")
        self.assertEqual(result.stop.code, "no_new_info")

    def test_high_novelty_continues(self):
        room, session, _ = build(
            ["a", "b"],
            {
                "a": ["风险是并发写入导致不一致，@b 你怎么看"],
                "b": ["我认为可以用悲观锁解决，但吞吐会下降三成"],
            },
            min_novelty=0.2,
        )
        result = session.say("@a 开始")
        self.assertEqual(len(result.turns), 2)


class TestFailureIsolation(unittest.TestCase):
    def test_one_model_failing_does_not_kill_round(self):
        class Boom(MockProvider):
            def complete(self, messages, system=None):
                raise ProviderError("boom 挂了")

        providers = {"a": MockProvider("a", script=["A 正常"]), "b": Boom("b")}
        room = Room(["a", "b"])
        session = Session(room, providers, parallel=False)
        result = session.say("都说说")

        self.assertEqual(len(result.turns), 2)
        self.assertTrue(result.turns[0].ok)
        self.assertFalse(result.turns[1].ok)
        self.assertIn("boom", result.turns[1].error)

    def test_all_failing_stops(self):
        class Boom(MockProvider):
            def complete(self, messages, system=None):
                raise ProviderError("挂了")

        room = Room(["a", "b"])
        session = Session(room, {"a": Boom("a"), "b": Boom("b")}, parallel=False)
        result = session.say("都说说")
        self.assertEqual(result.stop.code, "all_failed")

    def test_missing_provider_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            Session(Room(["a", "b"]), {"a": MockProvider("a")})


class TestNovelty(unittest.TestCase):
    def test_identical_text_is_zero(self):
        self.assertEqual(novelty("并发写入不一致", ["并发写入不一致"]), 0.0)

    def test_all_new_text_is_one(self):
        self.assertEqual(novelty("完全不同的内容", ["abc def"]), 1.0)

    def test_partial_overlap(self):
        score = novelty("并发写入会导致锁竞争", ["并发写入会导致数据不一致"])
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_empty_text_is_zero(self):
        self.assertEqual(novelty("", ["abc"]), 0.0)

    def test_convergence_phrases(self):
        self.assertTrue(looks_converged("我认为可以定了"))
        self.assertTrue(looks_converged("No further comments"))
        self.assertFalse(looks_converged("我不同意"))


if __name__ == "__main__":
    unittest.main()
