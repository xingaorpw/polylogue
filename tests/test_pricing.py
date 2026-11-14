"""价格表的测试。

这里出错的后果很隐蔽：界面上一律显示「未知」，看起来像「没数据」，
实际上是查表逻辑坏了。所以要专门测 id 形态。
"""

import unittest

from polylogue.pricing import PRICE_TABLE_DATE, PRICES, cost_of
from polylogue.store import DEFAULT_CONTACTS


class TestLookupByModelId(unittest.TestCase):
    def test_vendor_prefix_stripped(self):
        """OpenRouter 的 id 带 vendor/ 前缀，必须能查到。"""
        cost = cost_of("anthropic/claude-haiku-4.5", 1_000_000, 0)
        self.assertIsNotNone(cost, "带 vendor 前缀的模型查不到价格")
        self.assertAlmostEqual(cost, 1.00, places=6)

    def test_every_default_contact_has_price(self):
        """默认通讯录里的模型必须都能算出钱，否则主路径上全是「未知」。"""
        missing = []
        for c in DEFAULT_CONTACTS:
            if cost_of(c["model"], 1000, 1000) is None:
                missing.append(c["model"])
        self.assertEqual(missing, [], f"这些默认模型没有价格：{missing}")

    def test_plain_name_still_works(self):
        self.assertIsNotNone(cost_of("gpt-4o-mini", 10, 10))

    def test_date_suffixed_version(self):
        """grok-4.6-0301 这种带日期后缀的落到 grok-4.6。"""
        a = cost_of("grok-4.6-0301", 1_000_000, 0)
        b = cost_of("grok-4.6", 1_000_000, 0)
        self.assertEqual(a, b)

    def test_longest_prefix_wins(self):
        """gpt-4o-mini 不能匹配成 gpt-4o 的价格。"""
        mini = cost_of("gpt-4o-mini", 1_000_000, 0)
        full = cost_of("gpt-4o", 1_000_000, 0)
        self.assertAlmostEqual(mini, 0.15, places=6)
        self.assertAlmostEqual(full, 2.50, places=6)
        self.assertNotEqual(mini, full)

    def test_gemini_lite_not_confused_with_flash(self):
        lite = cost_of("google/gemini-2.5-flash-lite", 1_000_000, 0)
        flash = cost_of("google/gemini-2.5-flash", 1_000_000, 0)
        self.assertAlmostEqual(lite, 0.10, places=6)
        self.assertAlmostEqual(flash, 0.30, places=6)


class TestFreeAndUnknown(unittest.TestCase):
    def test_free_variant_is_zero_not_unknown(self):
        """:free 是真的 0 元 —— 已知的 0，不能返回 None。"""
        self.assertEqual(cost_of("baidu/ernie-5.5-flash-preview:free", 1000, 1000), 0.0)

    def test_unknown_returns_none(self):
        self.assertIsNone(cost_of("完全不存在的模型-xyz", 1000, 1000))

    def test_unknown_is_not_zero(self):
        """查不到必须是 None，不能返回 0 冒充免费。"""
        self.assertIsNotNone(cost_of("完全不存在的模型-xyz", 0, 0) is None)

    def test_batch_suffix_ignored(self):
        plain = cost_of("claude-opus-4.6", 1_000_000, 0)
        batch = cost_of("anthropic/claude-opus-4.6:batch", 1_000_000, 0)
        self.assertEqual(plain, batch)


class TestCostMath(unittest.TestCase):
    def test_input_and_output_priced_separately(self):
        # gpt-4o: 2.50 输入 / 10.00 输出
        cost = cost_of("gpt-4o", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 12.50, places=6)

    def test_zero_tokens(self):
        self.assertEqual(cost_of("gpt-4o", 0, 0), 0.0)

    def test_empty_model(self):
        self.assertIsNone(cost_of("", 10, 10))
        self.assertIsNone(cost_of(None, 10, 10))


class TestTableSanity(unittest.TestCase):
    def test_all_prices_non_negative(self):
        for name, (pin, pout) in PRICES.items():
            self.assertGreaterEqual(pin, 0, name)
            self.assertGreaterEqual(pout, 0, name)

    def test_output_not_cheaper_than_input(self):
        """输出比输入还便宜的，基本都是录错了。"""
        bad = [n for n, (i, o) in PRICES.items() if o < i and not n.startswith("mock")]
        self.assertEqual(bad, [], f"这些模型输出比输入便宜，可能录错：{bad}")

    def test_table_date_present(self):
        self.assertRegex(PRICE_TABLE_DATE, r"^\d{4}-\d{2}$")


if __name__ == "__main__":
    unittest.main()
