"""Q 版头像的测试：必须是合法 SVG、互相能区分、覆盖所有主流厂商。"""

import unittest
import xml.etree.ElementTree as ET

from polylogue.avatars import AVATARS, avatar_for, avatar_names
from polylogue.providers import PRESETS


class TestAvatarValidity(unittest.TestCase):
    def test_every_avatar_parses_as_svg(self):
        for name, svg in AVATARS.items():
            with self.subTest(avatar=name):
                root = ET.fromstring(svg)
                self.assertTrue(root.tag.endswith("svg"))
                self.assertEqual(root.get("viewBox"), "0 0 64 64")

    def test_no_external_resources(self):
        """头像必须是自包含的：不联网、不引外部字体或图片。"""
        for name, svg in AVATARS.items():
            with self.subTest(avatar=name):
                self.assertNotIn("http", svg.replace("http://www.w3.org/2000/svg", ""))
                self.assertNotIn("<image", svg)
                self.assertNotIn("@import", svg)

    def test_all_distinct(self):
        seen = {}
        for name, svg in AVATARS.items():
            self.assertNotIn(svg, seen, f"{name} 和 {seen.get(svg)} 的头像一模一样")
            seen[svg] = name


class TestAvatarCoverage(unittest.TestCase):
    def test_covers_every_preset_provider(self):
        """每个支持的 provider 都要有自己的形象，不能都退化成灰头像。"""
        missing = []
        for name in PRESETS:
            svg = avatar_for(name)
            if svg == AVATARS["generic"]:
                missing.append(name)
        self.assertEqual(missing, [], f"这些 provider 没有专属头像：{missing}")

    def test_has_expected_brands(self):
        for key in ("openai", "claude", "gemini", "grok", "deepseek", "qwen", "kimi", "glm"):
            self.assertIn(key, AVATARS)

    def test_avatar_names_sorted(self):
        names = avatar_names()
        self.assertEqual(names, sorted(names))
        self.assertGreaterEqual(len(names), 14)


class TestAvatarLookup(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(avatar_for("openai"), AVATARS["openai"])

    def test_case_insensitive(self):
        self.assertEqual(avatar_for("OpenAI"), AVATARS["openai"])

    def test_alias(self):
        self.assertEqual(avatar_for("chatgpt"), AVATARS["openai"])
        self.assertEqual(avatar_for("anthropic"), AVATARS["claude"])
        self.assertEqual(avatar_for("xai"), AVATARS["grok"])

    def test_model_id(self):
        self.assertEqual(avatar_for("anthropic/claude-haiku-4.5"), AVATARS["claude"])
        self.assertEqual(avatar_for("openai/gpt-4o-mini"), AVATARS["openai"])
        self.assertEqual(avatar_for("google/gemini-2.5-flash"), AVATARS["gemini"])

    def test_unknown_falls_back(self):
        self.assertEqual(avatar_for("某个不存在的模型"), AVATARS["generic"])
        self.assertEqual(avatar_for(""), AVATARS["generic"])
        self.assertEqual(avatar_for(None), AVATARS["generic"])

    def test_contact_id(self):
        for cid in ("claude", "gpt", "gemini", "grok", "deepseek", "qwen", "kimi", "glm"):
            self.assertNotEqual(avatar_for(cid), AVATARS["generic"], cid)


if __name__ == "__main__":
    unittest.main()
