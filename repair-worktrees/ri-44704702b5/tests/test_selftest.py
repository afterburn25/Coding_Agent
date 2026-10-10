import unittest
from localcodeagent.selftest import validate_self_update, is_chat_nexus_tree
from pathlib import Path
import time

ROOT = Path(__file__).parent.parent

class TestSelfUpdate(unittest.TestCase):
    def test_isolated_second_instance_smoke_test(self):
        result = validate_self_update(ROOT, run_tests=False, startup_timeout=30)
        self.assertTrue(result["ok"], msg=result.get("error") or result.get("log_tail"))
        checks = result["smoke"]["checks"]
        self.assertTrue(checks["status"]["ok"])
        src = (ROOT / "localcodeagent" / "selftest.py").read_text(encoding="utf-8")
        server_src = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        self.assertIn('encoding="utf-8"', src)
        self.assertIn('errors="replace"', src)
        self.assertIn("gh", "run", "view", "rid", "--log-failed", server_src)
        self.assertIn('text=True, errors="replace", timeout=25', server_src)
        for name in (
            "main_ui", "image_ui", "research_ui", "trainer_ui",
            "chat_ui", "code_ui", "data_ui", "settings_ui",
            "help_ui", "about_ui",
        ):
            self.assertIn(name, checks["ui"])

if __name__ == "__main__":
    unittest.main()