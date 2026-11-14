"""错误翻译与空内容的测试。

这些是「用户第一次打开就看到」的路径，必须有测试钉住：
报错要能看懂，空回答不能变成空气泡。
"""

import unittest
from unittest import mock

from polylogue.providers import (
    Message,
    OpenAICompatProvider,
    ProviderError,
    _explain,
)


class TestExplain(unittest.TestCase):
    def test_402_says_how_much_you_can_afford(self):
        detail = (
            '{"error":{"message":"This request requires more credits, or fewer '
            'max_tokens. You requested up to 4096 tokens, but can only afford 1599."}}'
        )
        msg = _explain("openrouter", 402, detail)
        self.assertIn("余额不够", msg)
        self.assertIn("4096", msg)
        self.assertIn("1599", msg)
        self.assertIn("max_tokens", msg)

    def test_402_without_numbers(self):
        self.assertIn("余额不够", _explain("x", 402, "insufficient credits"))

    def test_404_dead_model(self):
        detail = '{"error":{"message":"anthropic/claude-3.5-haiku is no longer available"}}'
        msg = _explain("openrouter", 404, detail)
        self.assertIn("下线", msg)

    def test_429_mentions_rate_limit(self):
        self.assertIn("限流", _explain("openrouter", 429, '{"code":429}'))

    def test_401_mentions_key(self):
        self.assertIn("key", _explain("openai", 401, "invalid api key"))

    def test_5xx_is_transient(self):
        self.assertIn("过一会儿", _explain("x", 503, ""))

    def test_unknown_code_keeps_detail(self):
        msg = _explain("x", 418, "i am a teapot")
        self.assertIn("418", msg)
        self.assertIn("teapot", msg)

    def test_no_raw_json_in_402(self):
        """翻译过之后不该再把裸 JSON 甩给用户。"""
        detail = '{"error":{"message":"This request requires more credits"}}'
        self.assertNotIn('{"error"', _explain("openrouter", 402, detail))


def fake_provider(response):
    p = OpenAICompatProvider(
        name="openrouter",
        base_url="https://example.invalid/v1",
        api_key="sk-x",
        model="test/model",
    )
    p._post = mock.Mock(return_value=response)
    return p


MSG = [Message(role="user", content="hi")]


class TestEmptyContent(unittest.TestCase):
    def test_reasoning_only_raises_with_hint(self):
        """推理模型把预算烧在思考上时，不能返回空字符串。"""
        p = fake_provider(
            {
                "choices": [{"message": {"content": "", "reasoning": "想了很久…"},
                             "finish_reason": "length"}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 1024,
                    "completion_tokens_details": {"reasoning_tokens": 1024},
                },
            }
        )
        with self.assertRaises(ProviderError) as ctx:
            p.complete(MSG)
        msg = str(ctx.exception)
        self.assertIn("思考", msg)
        self.assertIn("max_tokens", msg)
        self.assertIn("1024", msg)

    def test_plain_empty_raises(self):
        p = fake_provider(
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}], "usage": {}}
        )
        with self.assertRaises(ProviderError) as ctx:
            p.complete(MSG)
        self.assertIn("空内容", str(ctx.exception))
        self.assertIn("stop", str(ctx.exception))

    def test_normal_content_passes(self):
        p = fake_provider(
            {
                "choices": [{"message": {"content": "  你好  "}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            }
        )
        reply = p.complete(MSG)
        self.assertEqual(reply.text, "你好")
        self.assertEqual(reply.usage.total, 8)


class TestMaxTokensAlwaysSent(unittest.TestCase):
    def test_payload_has_max_tokens(self):
        """不带 max_tokens 会让低余额账户直接 402 —— 必须一直带上。"""
        p = fake_provider(
            {"choices": [{"message": {"content": "ok"}}], "usage": {}}
        )
        p.complete(MSG)
        payload = p._post.call_args[0][1]
        self.assertIn("max_tokens", payload)
        self.assertEqual(payload["max_tokens"], 1024)

    def test_can_override(self):
        p = OpenAICompatProvider(
            name="x", base_url="https://e.invalid", api_key="k", model="m", max_tokens=256
        )
        p._post = mock.Mock(return_value={"choices": [{"message": {"content": "ok"}}], "usage": {}})
        p.complete(MSG)
        self.assertEqual(p._post.call_args[0][1]["max_tokens"], 256)


if __name__ == "__main__":
    unittest.main()
