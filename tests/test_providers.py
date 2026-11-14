"""适配器测试：验证请求格式和响应解析，全部拦截网络。"""

import unittest
from unittest import mock

from polylogue.pricing import cost_of
from polylogue.providers import (
    ALIASES,
    PRESETS,
    AnthropicProvider,
    GeminiProvider,
    Message,
    OpenAICompatProvider,
    ProviderError,
    build_provider,
    resolve_name,
)


class Recorder:
    """替换 _post，记录请求并返回预设响应。"""

    def __init__(self, response):
        self.response = response
        self.url = None
        self.payload = None
        self.headers = None

    def __call__(self, url, payload, headers, timeout=120.0):
        self.url = url
        self.payload = payload
        self.headers = headers
        return self.response


def patch_post(provider, response):
    rec = Recorder(response)
    provider._post = rec  # type: ignore[method-assign]
    return rec


MSGS = [
    Message(role="user", content="[用户] 方案 A 还是 B？"),
    Message(role="assistant", content="我选 A", speaker="claude"),
    Message(role="user", content="[gemini] 我选 B", speaker="gemini"),
]


class TestOpenAICompat(unittest.TestCase):
    RESPONSE = {
        "choices": [{"message": {"content": "  选 A  "}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }

    def test_payload_shape(self):
        p = OpenAICompatProvider("deepseek", "https://api.deepseek.com/v1", "sk-x", "deepseek-chat")
        rec = patch_post(p, self.RESPONSE)
        reply = p.complete(MSGS, system="你是批评者")

        self.assertEqual(rec.url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(rec.payload["model"], "deepseek-chat")
        # system 必须是第一条 message
        self.assertEqual(rec.payload["messages"][0], {"role": "system", "content": "你是批评者"})
        self.assertEqual(rec.headers["Authorization"], "Bearer sk-x")
        self.assertEqual(reply.text, "选 A")

    def test_usage_parsed(self):
        p = OpenAICompatProvider("x", "https://x/v1", "k", "m")
        patch_post(p, self.RESPONSE)
        reply = p.complete(MSGS)
        self.assertEqual(reply.usage.prompt_tokens, 120)
        self.assertEqual(reply.usage.completion_tokens, 30)

    def test_no_system_means_no_system_message(self):
        p = OpenAICompatProvider("x", "https://x/v1", "k", "m")
        rec = patch_post(p, self.RESPONSE)
        p.complete(MSGS)
        self.assertEqual(rec.payload["messages"][0]["role"], "user")

    def test_bad_structure_raises(self):
        p = OpenAICompatProvider("x", "https://x/v1", "k", "m")
        patch_post(p, {"unexpected": True})
        with self.assertRaises(ProviderError):
            p.complete(MSGS)

    def test_missing_usage_defaults_to_zero(self):
        p = OpenAICompatProvider("x", "https://x/v1", "k", "m")
        patch_post(p, {"choices": [{"message": {"content": "hi"}}]})
        reply = p.complete(MSGS)
        self.assertEqual(reply.usage.total, 0)

    def test_base_url_trailing_slash_normalized(self):
        p = OpenAICompatProvider("x", "https://x/v1/", "k", "m")
        rec = patch_post(p, self.RESPONSE)
        p.complete(MSGS)
        self.assertNotIn("//chat", rec.url)


class TestAnthropic(unittest.TestCase):
    RESPONSE = {
        "content": [{"type": "text", "text": "选 A"}, {"type": "tool_use", "id": "t"}],
        "usage": {"input_tokens": 200, "output_tokens": 40},
    }

    def test_payload_has_required_max_tokens(self):
        """Anthropic 的 max_tokens 是必填，漏了会 400。"""
        p = AnthropicProvider("sk-ant", "claude-sonnet-4-20250514", max_tokens=1024)
        rec = patch_post(p, self.RESPONSE)
        p.complete(MSGS)

        self.assertEqual(rec.payload["max_tokens"], 1024)
        self.assertEqual(rec.payload["model"], "claude-sonnet-4-20250514")

    def test_system_is_top_level_not_a_message(self):
        p = AnthropicProvider("k", "m")
        rec = patch_post(p, self.RESPONSE)
        p.complete(MSGS, system="你是批评者")

        self.assertEqual(rec.payload["system"], "你是批评者")
        self.assertTrue(all(m["role"] != "system" for m in rec.payload["messages"]))

    def test_headers_use_x_api_key(self):
        p = AnthropicProvider("sk-ant-123", "m")
        rec = patch_post(p, self.RESPONSE)
        p.complete(MSGS)

        self.assertEqual(rec.headers["x-api-key"], "sk-ant-123")
        self.assertIn("anthropic-version", rec.headers)
        self.assertNotIn("Authorization", rec.headers)

    def test_only_text_blocks_concatenated(self):
        """content 里可能有 tool_use 块，不能把它的字段当文本。"""
        p = AnthropicProvider("k", "m")
        patch_post(p, self.RESPONSE)
        self.assertEqual(p.complete(MSGS).text, "选 A")

    def test_usage_maps_input_output(self):
        p = AnthropicProvider("k", "m")
        patch_post(p, self.RESPONSE)
        usage = p.complete(MSGS).usage
        self.assertEqual(usage.prompt_tokens, 200)
        self.assertEqual(usage.completion_tokens, 40)

    def test_bad_structure_raises(self):
        p = AnthropicProvider("k", "m")
        patch_post(p, {"error": "x"})
        with self.assertRaises(ProviderError):
            p.complete(MSGS)


class TestGemini(unittest.TestCase):
    RESPONSE = {
        "candidates": [{"content": {"parts": [{"text": "选 A"}, {"text": "。"}]}}],
        "usageMetadata": {"promptTokenCount": 300, "candidatesTokenCount": 50},
    }

    def test_assistant_maps_to_model_role(self):
        """Gemini 不认 assistant，要用 model。"""
        p = GeminiProvider("key", "gemini-2.5-flash")
        rec = patch_post(p, self.RESPONSE)
        p.complete(MSGS)

        roles = [c["role"] for c in rec.payload["contents"]]
        self.assertEqual(roles, ["user", "model", "user"])

    def test_system_instruction_separate(self):
        p = GeminiProvider("key", "m")
        rec = patch_post(p, self.RESPONSE)
        p.complete(MSGS, system="你是批评者")

        self.assertEqual(
            rec.payload["systemInstruction"], {"parts": [{"text": "你是批评者"}]}
        )

    def test_url_contains_model_and_key(self):
        p = GeminiProvider("secret", "gemini-2.5-pro")
        rec = patch_post(p, self.RESPONSE)
        p.complete(MSGS)

        self.assertIn("gemini-2.5-pro", rec.url)
        self.assertIn(":generateContent", rec.url)
        self.assertIn("key=secret", rec.url)

    def test_multiple_parts_concatenated(self):
        p = GeminiProvider("k", "m")
        patch_post(p, self.RESPONSE)
        self.assertEqual(p.complete(MSGS).text, "选 A。")

    def test_blocked_reason_reported_clearly(self):
        """被安全策略拦下时没有 candidates，要报出原因而不是 KeyError。"""
        p = GeminiProvider("k", "m")
        patch_post(p, {"promptFeedback": {"blockReason": "SAFETY"}})
        with self.assertRaises(ProviderError) as ctx:
            p.complete(MSGS)
        self.assertIn("SAFETY", str(ctx.exception))

    def test_usage_parsed(self):
        p = GeminiProvider("k", "m")
        patch_post(p, self.RESPONSE)
        usage = p.complete(MSGS).usage
        self.assertEqual(usage.prompt_tokens, 300)
        self.assertEqual(usage.completion_tokens, 50)


class TestBuildProvider(unittest.TestCase):
    def test_aliases_resolve(self):
        self.assertEqual(resolve_name("gpt"), "openai")
        self.assertEqual(resolve_name("ChatGPT"), "openai")
        self.assertEqual(resolve_name("anthropic"), "claude")
        self.assertEqual(resolve_name("google"), "gemini")
        self.assertEqual(resolve_name("moonshot"), "kimi")

    def test_every_preset_is_buildable_with_key(self):
        """所有预设都必须能建出来，且不抛异常。"""
        for name, preset in PRESETS.items():
            env = preset["env"]
            with mock.patch.dict("os.environ", {env: "fake-key"} if env else {}, clear=False):
                provider = build_provider(name)
                self.assertEqual(provider.model, preset["model"], msg=f"{name} 的默认模型不对")

    def test_kind_selects_adapter(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}, clear=False):
            self.assertIsInstance(build_provider("claude"), AnthropicProvider)
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "k"}, clear=False):
            self.assertIsInstance(build_provider("gemini"), GeminiProvider)
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "k"}, clear=False):
            self.assertIsInstance(build_provider("openai"), OpenAICompatProvider)

    def test_missing_key_raises_with_env_name(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ProviderError) as ctx:
                build_provider("openai")
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_google_api_key_fallback(self):
        with mock.patch.dict("os.environ", {"GOOGLE_API_KEY": "g"}, clear=True):
            self.assertIsInstance(build_provider("gemini"), GeminiProvider)

    def test_unknown_provider_lists_known(self):
        with self.assertRaises(ProviderError) as ctx:
            build_provider("nonexistent")
        self.assertIn("openai", str(ctx.exception))

    def test_custom_base_url_accepted(self):
        p = build_provider("myvllm", base_url="http://localhost:8000/v1", model="local")
        self.assertIsInstance(p, OpenAICompatProvider)
        self.assertEqual(p.base_url, "http://localhost:8000/v1")

    def test_model_override(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "k"}, clear=False):
            p = build_provider("openai", model="gpt-4.1-mini")
        self.assertEqual(p.model, "gpt-4.1-mini")

    def test_grok_and_groq_are_different(self):
        """xAI 的 grok 和推理托管商 groq 是两个东西，不能混。"""
        self.assertIn("grok", PRESETS)
        self.assertIn("groq", PRESETS)
        self.assertNotEqual(PRESETS["grok"]["base_url"], PRESETS["groq"]["base_url"])
        self.assertNotEqual(PRESETS["grok"]["env"], PRESETS["groq"]["env"])


class TestPricing(unittest.TestCase):
    def test_known_model(self):
        cost = cost_of("deepseek-chat", 1_000_000, 0)
        self.assertAlmostEqual(cost, 0.28, places=4)

    def test_prefix_match_for_dated_version(self):
        """grok-3-0301 这种带日期的版本要落到 grok-3。"""
        cost = cost_of("grok-3-0301", 1_000_000, 0)
        self.assertAlmostEqual(cost, 3.00, places=4)

    def test_longest_prefix_wins(self):
        """gpt-4o-mini 不能落到 gpt-4o 的价格上。"""
        mini = cost_of("gpt-4o-mini", 1_000_000, 0)
        full = cost_of("gpt-4o", 1_000_000, 0)
        self.assertAlmostEqual(mini, 0.15, places=4)
        self.assertAlmostEqual(full, 2.50, places=4)

    def test_unknown_returns_none_not_zero(self):
        """查不到价格必须返回 None —— 返回 0 会让人以为免费。"""
        self.assertIsNone(cost_of("some-unknown-model", 1000, 1000))

    def test_output_tokens_cost_more(self):
        self.assertGreater(cost_of("gpt-4o", 0, 1_000_000), cost_of("gpt-4o", 1_000_000, 0))


class TestAliasesComplete(unittest.TestCase):
    def test_every_alias_points_to_real_preset(self):
        for alias, target in ALIASES.items():
            self.assertIn(target, PRESETS, msg=f"别名 {alias} 指向不存在的 {target}")


if __name__ == "__main__":
    unittest.main()
