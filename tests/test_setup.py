"""零配置引导的测试。

验证两件事：
1. 本机什么 key 都没有时，服务照常起来并进入引导模式（不是启动失败）
2. 在界面上填一次 key，就能建出会话开始用 —— 不用改环境变量、不用重启
"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from polylogue import bootstrap, config, credentials
from polylogue.web import serve


class Isolated(unittest.TestCase):
    """把配置文件和 DSH 凭据都指到临时目录，别碰用户真实数据。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = base / "config.json"
        self.dsh = base / "creds.yaml"

        self.patches = [
            mock.patch.object(config, "CONFIG_PATH", self.cfg),
            mock.patch.object(credentials, "DSH_CREDENTIALS", self.dsh),
            mock.patch.object(credentials, "OWN_CONFIG", self.cfg),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class TestConfigFile(Isolated):
    def test_defaults_when_missing(self):
        data = config.load()
        self.assertFalse(data["onboarded"])
        self.assertEqual(data["api_keys"], {})

    def test_save_and_load(self):
        config.save({"onboarded": True, "api_keys": {"openrouter": "sk-x"}})
        self.assertTrue(self.cfg.is_file())
        data = config.load()
        self.assertTrue(data["onboarded"])
        self.assertEqual(data["api_keys"]["openrouter"], "sk-x")

    def test_corrupt_file_falls_back(self):
        """配置文件被写坏不能让程序起不来。"""
        self.cfg.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.write_text("{ 这不是 json", encoding="utf-8")
        data = config.load()
        self.assertFalse(data["onboarded"])

    def test_update_merges(self):
        config.update(onboarded=True)
        config.update(budget={"max_rounds": 3})
        data = config.load()
        self.assertTrue(data["onboarded"])
        self.assertEqual(data["budget"]["max_rounds"], 3)
        self.assertEqual(data["budget"]["max_calls"], 24, "没改的字段要保留默认")


class TestCredentialDiscovery(Isolated):
    def test_nothing_found(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(credentials.discover(), {})

    def test_env_var_found(self):
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-env"}, clear=True):
            self.assertEqual(credentials.discover()["openrouter"], "sk-env")

    def test_dsh_reuse(self):
        """DSH 的 refs 块值是裸串（不带引号），解析必须能吃下。"""
        self.dsh.parent.mkdir(parents=True, exist_ok=True)
        self.dsh.write_text(
            "version: 1\n"
            "refs:\n"
            "  {\n"
            "    OPENROUTER_API_KEY: sk-or-v1-abcdef,\n"
            "    TONGJI_API_KEY: sk-other\n"
            "  }\n",
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", {}, clear=True):
            keys = credentials.discover()
        self.assertEqual(keys.get("openrouter"), "sk-or-v1-abcdef")
        self.assertNotIn("tongji", keys)

    def test_opt_out(self):
        self.dsh.parent.mkdir(parents=True, exist_ok=True)
        self.dsh.write_text("refs:\n  {\n    OPENROUTER_API_KEY: sk-x\n  }\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {"POLYLOGUE_NO_REUSE": "1"}, clear=True):
            self.assertEqual(credentials.discover(), {})

    def test_mask_hides_value(self):
        self.assertNotIn("abcdefgh", credentials.mask("sk-abcdefghij"))
        self.assertEqual(credentials.mask(""), "")


class TestBootstrap(Isolated):
    def test_no_credentials_needs_onboarding(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            members, origin = bootstrap.resolve_members(None)
        self.assertEqual(members, [])
        self.assertIn("引导", origin)

    def test_openrouter_gives_full_room(self):
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-x"}, clear=True):
            members, origin = bootstrap.resolve_members(None)
        self.assertEqual(len(members), 3)
        self.assertEqual({m["provider"] for m in members}, {"openrouter"})
        # 三个成员必须来自不同厂商，否则多模型没有意义
        vendors = {m["model"].split("/")[0] for m in members}
        self.assertGreaterEqual(len(vendors), 2, "成员应该覆盖不同厂商")

    def test_saved_config_wins(self):
        config.save({"members": [{"name": "x", "provider": "openrouter", "model": "a/b"}]})
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "sk-x"}, clear=True):
            members, origin = bootstrap.resolve_members(None)
        self.assertEqual(len(members), 1)
        self.assertIn("上次保存", origin)


class TestSetupEndpoint(Isolated):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls._patches = [
            mock.patch.object(config, "CONFIG_PATH", base / "config.json"),
            mock.patch.object(credentials, "DSH_CREDENTIALS", base / "none.yaml"),
            mock.patch.object(credentials, "OWN_CONFIG", base / "config.json"),
        ]
        for p in cls._patches:
            p.start()

        # 只摘掉 API key，不要清空整个环境：Windows 的 winsock/OpenSSL
        # 初始化依赖 SYSTEMROOT，清空后 socket 都建不起来。
        _drop = {
            "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY",
            "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHER_API_KEY", "LLM_API_KEY",
        }
        cls._env = mock.patch.dict(
            "os.environ",
            {k: v for k, v in os.environ.items() if k not in _drop},
            clear=True,
        )
        cls._env.start()

        ready = threading.Event()
        holder = {}

        def run():
            serve([], port=0, on_ready=lambda url: (holder.__setitem__("url", url), ready.set()))

        threading.Thread(target=run, daemon=True).start()
        if not ready.wait(15):
            raise RuntimeError("引导模式的服务没起来")
        cls.host = holder["url"].split("://")[1]

    @classmethod
    def tearDownClass(cls):
        cls._env.stop()
        for p in cls._patches:
            p.stop()
        cls._tmp.cleanup()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection(self.host, timeout=20)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, (json.loads(raw) if raw else {})

    def test_01_onboarding_mode(self):
        """没有 key 时服务也要起来，并告诉前端该走引导。"""
        status, info = self.req("GET", "/api/info")
        self.assertEqual(status, 200)
        self.assertTrue(info["onboarding"])
        self.assertEqual(info["conversations"], [])
        self.assertIn("presets", info)
        self.assertTrue(info["contacts"], "引导模式也要先有通讯录可选")

    def test_02_setup_creates_conversation(self):
        status, data = self.req(
            "POST",
            "/api/setup",
            {"provider": "openrouter", "api_key": "sk-or-v1-test", "preset": "省钱"},
        )
        self.assertEqual(status, 201, msg=str(data))
        self.assertFalse(data["info"]["onboarding"])
        convs = data["info"]["conversations"]
        self.assertTrue(convs, "填完 key 应该自动建一个能直接说话的会话")
        self.assertTrue(convs[0]["members"])
        # key 落盘了
        self.assertEqual(config.load()["api_keys"]["openrouter"], "sk-or-v1-test")
        self.assertTrue(config.load()["onboarded"])

    def test_03_settings_rebuilds_room(self):
        status, data = self.req(
            "POST",
            "/api/settings",
            {"context": {"keep_recent": 5, "enabled": True, "budget_tokens": 3000}},
        )
        self.assertEqual(status, 200, msg=str(data))
        self.assertEqual(config.load()["context"]["keep_recent"], 5)


if __name__ == "__main__":
    unittest.main()
