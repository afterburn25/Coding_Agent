from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.selftest import is_chat_nexus_tree, validate_self_update
from localcodeagent.workflow.verify import detect_verification_commands


ROOT = Path(__file__).resolve().parents[1]


class SelfUpdateValidationTests(unittest.TestCase):
    def test_detects_chat_nexus_source_tree(self):
        self.assertTrue(is_chat_nexus_tree(ROOT))
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(is_chat_nexus_tree(Path(td)))

    def test_chat_nexus_verification_uses_isolated_validator(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "localcodeagent").mkdir()
            (root / "localcodeagent" / "server.py").write_text("# marker\n", encoding="utf-8")
            (root / "web").mkdir()
            (root / "web" / "index.html").write_text("Chat Nexus\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "pyproject.toml").write_text("[project]\nname='chat-nexus-test'\n", encoding="utf-8")
            commands = detect_verification_commands(root)
            self.assertEqual(commands[0]["name"], "Nexus Core isolated self-update validation")
            self.assertIn("localcodeagent.selftest", commands[0]["command"])
            self.assertFalse(any("unittest discover" in item["command"] for item in commands))

    def test_isolated_second_instance_smoke_test(self):
        result = validate_self_update(ROOT, run_tests=False, startup_timeout=20)
        self.assertTrue(result["ok"], msg=result.get("error") or result.get("log_tail"))
        checks = result["smoke"]["checks"]
        self.assertTrue(checks["status"]["ok"])
        self.assertTrue(checks["main_ui"]["ok"])
        self.assertTrue(checks["image_ui"]["ok"])
        self.assertTrue(checks["research_ui"]["ok"])
        self.assertTrue(checks["trainer_ui"]["ok"])


if __name__ == "__main__":
    unittest.main()
