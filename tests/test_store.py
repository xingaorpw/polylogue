"""本地存储的测试：通讯录、分类、会话、搜索、头像。

这些都是「关掉程序再打开还在不在」的东西，坏了用户会直接丢历史。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from polylogue import config, store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.json"
        self.patch = mock.patch.object(config, "CONFIG_PATH", self.cfg)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()


class TestContacts(Base):
    def test_defaults_are_created(self):
        state = store.load_state()
        self.assertGreaterEqual(len(state["contacts"]), 8)
        self.assertIn("常用", state["categories"])
        ids = {c["id"] for c in state["contacts"]}
        for expected in ("claude", "gpt", "gemini", "grok", "deepseek", "qwen", "kimi", "glm"):
            self.assertIn(expected, ids)

    def test_default_contacts_have_model(self):
        for c in store.contacts():
            self.assertTrue(c.get("model"), f"{c['id']} 没有 model")
            self.assertTrue(c.get("provider"), f"{c['id']} 没有 provider")

    def test_add(self):
        c = store.add_contact("我的机器人", "openrouter", "a/b")
        self.assertEqual(c["name"], "我的机器人")
        self.assertIn(c["id"], [x["id"] for x in store.contacts()])

    def test_add_creates_category(self):
        store.add_contact("X", "openrouter", "a/b", category="新分类")
        self.assertIn("新分类", store.categories())

    def test_add_duplicate_name_gets_unique_id(self):
        a = store.add_contact("同名", "openrouter", "a/b")
        b = store.add_contact("同名", "openrouter", "a/b")
        self.assertNotEqual(a["id"], b["id"])

    def test_update(self):
        store.add_contact("旧名", "openrouter", "a/b")
        cid = store.contacts()[-1]["id"]
        got = store.update_contact(cid, name="新名", model="c/d")
        self.assertEqual(got["name"], "新名")
        self.assertEqual(got["model"], "c/d")
        self.assertEqual(store.contact_by_id(cid)["name"], "新名")

    def test_update_missing(self):
        self.assertIsNone(store.update_contact("不存在", name="x"))

    def test_remove(self):
        store.add_contact("要删的", "openrouter", "a/b")
        cid = store.contacts()[-1]["id"]
        self.assertTrue(store.remove_contact(cid))
        self.assertIsNone(store.contact_by_id(cid))

    def test_remove_missing(self):
        self.assertFalse(store.remove_contact("不存在"))

    def test_base_url_roundtrip(self):
        c = store.add_contact("本地", "ollama", "llama3", base_url="http://127.0.0.1:11434/v1")
        self.assertEqual(store.contact_by_id(c["id"])["base_url"], "http://127.0.0.1:11434/v1")


class TestConversations(Base):
    def test_create_and_load(self):
        conv = store.create_conversation("测试", ["claude", "gpt"])
        self.assertEqual(conv["kind"], "group")
        got = store.load_conversation(conv["id"])
        self.assertEqual(got["title"], "测试")
        self.assertEqual(got["members"], ["claude", "gpt"])
        self.assertEqual(got["turns"], [])

    def test_persists_to_disk(self):
        conv = store.create_conversation("落盘", ["claude"])
        self.assertTrue((self.cfg.parent / "conversations" / f"{conv['id']}.json").is_file())

    def test_survives_reload(self):
        """模拟「关掉程序再打开」：只靠磁盘，不靠内存。"""
        conv = store.create_conversation("还记得我吗", ["gpt"])
        again = store.load_conversation(conv["id"])
        self.assertEqual(again["title"], "还记得我吗")

    def _write_raw(self, cid, title, updated):
        """直接写文件，绕开 create/save 自动盖时间戳 —— 排序测试需要控制 updated。"""
        folder = self.cfg.parent / "conversations"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{cid}.json").write_text(
            json.dumps(
                {
                    "id": cid,
                    "title": title,
                    "kind": "group",
                    "members": ["claude"],
                    "category": "常用",
                    "created": updated,
                    "updated": updated,
                    "turns": [],
                }
            ),
            encoding="utf-8",
        )

    def test_list_sorted_by_updated(self):
        self._write_raw("aaa", "老的", 1000.0)
        self._write_raw("bbb", "新的", 2000.0)
        self.assertEqual([c["id"] for c in store.list_conversations()], ["bbb", "aaa"])

    def test_tie_broken_deterministically(self):
        """updated 相同时顺序必须稳定，否则界面每次刷新都会跳。

        Windows 上连续建两个对话很容易落在同一个时钟 tick —— 这不是理论问题，
        CI 就是这么红的。
        """
        self._write_raw("zzz", "同刻 A", 1000.0)
        self._write_raw("aaa", "同刻 B", 1000.0)
        first = [c["id"] for c in store.list_conversations()]
        second = [c["id"] for c in store.list_conversations()]
        self.assertEqual(first, second, "两次读取顺序不一致")
        self.assertEqual(first, ["aaa", "zzz"], "时间相同时应按 id 排")

    def test_list_contains_both(self):
        a = store.create_conversation("先", ["claude"])
        b = store.create_conversation("后", ["gpt"])
        listed = store.list_conversations()
        self.assertEqual({c["id"] for c in listed}, {a["id"], b["id"]})
        stamps = [c["updated"] for c in listed]
        self.assertEqual(stamps, sorted(stamps, reverse=True), "必须按 updated 降序")

    def test_list_has_last_snippet(self):
        conv = store.create_conversation("有内容", ["claude"])
        conv["turns"] = [{"speaker": "Claude", "role": "assistant", "content": "最后一句在这里"}]
        store.save_conversation(conv)
        row = next(c for c in store.list_conversations() if c["id"] == conv["id"])
        self.assertIn("最后一句", row["last"])

    def test_rename(self):
        conv = store.create_conversation("旧", ["claude"])
        store.rename_conversation(conv["id"], "新", category="别处")
        got = store.load_conversation(conv["id"])
        self.assertEqual(got["title"], "新")
        self.assertEqual(got["category"], "别处")

    def test_rename_missing(self):
        self.assertIsNone(store.rename_conversation("不存在", "x"))

    def test_delete(self):
        conv = store.create_conversation("删我", ["claude"])
        self.assertTrue(store.delete_conversation(conv["id"]))
        self.assertIsNone(store.load_conversation(conv["id"]))
        self.assertFalse(store.delete_conversation(conv["id"]))

    def test_path_traversal_rejected(self):
        """会话 id 来自 URL，不能让 ../../ 把文件写到外面去。"""
        self.assertIsNone(store.load_conversation("../../etc/passwd"))
        with self.assertRaises(ValueError):
            store.save_conversation({"id": "../evil", "turns": []})

    def test_corrupt_file_ignored(self):
        folder = self.cfg.parent / "conversations"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "bad.json").write_text("{不是 json", encoding="utf-8")
        self.assertEqual(store.list_conversations(), [])

    def test_dm_kind(self):
        conv = store.create_conversation("单聊", ["claude"], kind="dm")
        self.assertEqual(store.load_conversation(conv["id"])["kind"], "dm")


class TestSearch(Base):
    def test_empty_query_returns_nothing(self):
        store.create_conversation("有内容", ["claude"])
        self.assertEqual(store.search(""), [])
        self.assertEqual(store.search("   "), [])

    def test_finds_in_turns(self):
        conv = store.create_conversation("架构讨论", ["claude"])
        conv["turns"] = [
            {"speaker": "Claude", "role": "assistant", "content": "这里有个并发写入的隐患"}
        ]
        store.save_conversation(conv)
        hits = store.search("并发写入")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["conversation"], conv["id"])
        self.assertIn("并发写入", hits[0]["snippet"])

    def test_finds_in_title(self):
        store.create_conversation("数据库迁移方案", ["claude"])
        hits = store.search("数据库迁移")
        self.assertTrue(any(h["where"] == "标题" for h in hits))

    def test_case_insensitive(self):
        conv = store.create_conversation("x", ["claude"])
        conv["turns"] = [{"speaker": "GPT", "role": "assistant", "content": "Use Redis Cache"}]
        store.save_conversation(conv)
        self.assertEqual(len(store.search("redis cache")), 1)

    def test_snippet_has_ellipsis_when_truncated(self):
        conv = store.create_conversation("x", ["claude"])
        conv["turns"] = [{
            "speaker": "GPT", "role": "assistant",
            "content": "前面很长" * 40 + "关键词" + "后面很长" * 40,
        }]
        store.save_conversation(conv)
        hit = store.search("关键词")[0]
        self.assertTrue(hit["snippet"].startswith("…"))
        self.assertTrue(hit["snippet"].endswith("…"))

    def test_respects_limit(self):
        conv = store.create_conversation("x", ["claude"])
        conv["turns"] = [{"speaker": "GPT", "role": "assistant", "content": "命中"} for _ in range(50)]
        store.save_conversation(conv)
        self.assertEqual(len(store.search("命中", limit=5)), 5)

    def test_no_match(self):
        store.create_conversation("x", ["claude"])
        self.assertEqual(store.search("绝对不存在的词xyz"), [])


class TestAvatars(Base):
    def test_save_and_load(self):
        uri = "data:image/png;base64,iVBORw0KGgo="
        self.assertTrue(store.save_avatar("claude", uri))
        self.assertEqual(store.load_avatar("claude"), uri)

    def test_rejects_non_data_uri(self):
        self.assertFalse(store.save_avatar("claude", "https://evil.example/x.png"))
        self.assertFalse(store.save_avatar("claude", "javascript:alert(1)"))

    def test_rejects_oversized(self):
        self.assertFalse(store.save_avatar("claude", "data:image/png;base64," + "A" * 1_500_000))

    def test_missing_returns_none(self):
        self.assertIsNone(store.load_avatar("没人"))

    def test_path_traversal(self):
        """id 里有 ../ 也不能写到 avatars 目录外面。"""
        store.save_avatar("../../evil", "data:image/png;base64,AAA")
        self.assertFalse((self.cfg.parent.parent / "evil").exists())

    def test_removing_contact_removes_avatar(self):
        c = store.add_contact("临时", "openrouter", "a/b")
        store.save_avatar(c["id"], "data:image/png;base64,AAA")
        store.remove_contact(c["id"])
        self.assertIsNone(store.load_avatar(c["id"]))


class TestLegacyConfig(unittest.TestCase):
    """0.2 的配置里没有 contacts/categories，升级后不能崩、也不能丢设置。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "config.json"
        self.patch = mock.patch.object(config, "CONFIG_PATH", self.cfg)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_old_config_gains_contacts(self):
        self.cfg.write_text(
            json.dumps(
                {
                    "onboarded": True,
                    "api_keys": {"openrouter": "sk-or-v1-old"},
                    "members": [{"name": "x", "provider": "openrouter", "model": "a/b"}],
                    "budget": {"max_rounds": 4, "max_calls": 10, "max_cost_usd": 0.3},
                    "context": {"keep_recent": 5, "enabled": True, "budget_tokens": 3000},
                    "theme": "light",
                }
            ),
            encoding="utf-8",
        )
        state = store.load_state()
        self.assertGreaterEqual(len(state["contacts"]), 8)
        self.assertIn("常用", state["categories"])

        # 老设置必须原样保留，不能被默认值覆盖
        cfg = config.load()
        self.assertEqual(cfg["api_keys"]["openrouter"], "sk-or-v1-old")
        self.assertEqual(cfg["budget"]["max_rounds"], 4)
        self.assertEqual(cfg["context"]["keep_recent"], 5)
        self.assertEqual(cfg["theme"], "light")

    def test_corrupt_config_does_not_crash(self):
        self.cfg.write_text("{这不是 json", encoding="utf-8")
        state = store.load_state()
        self.assertGreaterEqual(len(state["contacts"]), 8)

    def test_contacts_written_back_once(self):
        store.load_state()
        first = self.cfg.read_text(encoding="utf-8")
        store.load_state()
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), first, "第二次不该再重写配置")


if __name__ == "__main__":
    unittest.main()
