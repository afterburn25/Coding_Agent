from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.selftest import (
    _smoke_config,
    is_chat_nexus_tree,
    validate_self_update,
)
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

    def test_smoke_config_disables_source_sync(self):
        # The smoke instance runs against the checked-out tree — a startup
        # fast-forward would mutate the code under test mid-suite (this
        # actually broke a CI version test when a push landed mid-run).
        self.assertFalse(_smoke_config()["sync_source_on_start"])

    def test_isolated_second_instance_smoke_test(self):
        result = validate_self_update(ROOT, run_tests=False, startup_timeout=20)
        self.assertTrue(result["ok"], msg=result.get("error") or result.get("log_tail"))
        checks = result["smoke"]["checks"]
        self.assertTrue(checks["status"]["ok"])
        src = (ROOT / "localcodeagent" / "selftest.py").read_text(
            encoding="utf-8")
        server_src = (ROOT / "localcodeagent" / "server.py").read_text(
            encoding="utf-8")
        self.assertIn('encoding="utf-8"', src)
        self.assertIn('errors="replace"', src)
        self.assertIn('"gh", "run", "view", rid, "--log-failed"',
                      server_src)
        self.assertIn('text=True, errors="replace", timeout=25',
                      server_src)
        for name in (
            "main_ui", "image_ui", "research_ui", "trainer_ui",
            "missions_ui", "projects_ui", "tools_ui", "models_ui",
            "system_ui", "answers_ui", "settings_ui", "voice_ui",
            "personality_ui", "health_api", "skills_api", "knowledge_api",
            "rag_api", "lsp_api", "missions_api", "autonomy_api",
            "tools_api", "workflows_api", "mcp_api", "jobs_api",
            "projects_api", "preferences_api", "library_api",
            "processes_api", "resources_api", "readiness_api",
            "tasks_api", "activity_api", "queue_api", "stt_api",
            "nexus_state_api", "briefing_api", "events_bus",
        ):
            self.assertTrue(checks[name]["ok"], msg=f"{name}: {checks[name]}")


if __name__ == "__main__":
    unittest.main()
