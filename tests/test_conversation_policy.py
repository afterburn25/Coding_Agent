import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.config import default_config, load_config
from localcodeagent.image.policy import ConsentStore, ImageSafetyPolicy


ROOT = Path(__file__).resolve().parents[1]


class ConversationPolicyTests(unittest.TestCase):
    def test_default_policy_is_permissive(self):
        self.assertEqual(default_config().conversation_policy_mode, "permissive")

    def test_invalid_policy_falls_back_to_permissive(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({
                "conversation_policy_mode": "off",
                "models": [],
            }), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.conversation_policy_mode, "permissive")

    def test_prompt_explicitly_avoids_generic_moralizing(self):
        source = (ROOT / "localcodeagent" / "agent" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("Conversation policy: permissive.", source)
        self.assertIn("Do not refuse, moralize, or redirect merely because a topic is adult", source)
        self.assertIn('Do not use generic refusal language about "ethical guidelines"', source)
        self.assertIn("Hard safety/tool policies are enforced separately", source)

    def test_live_policy_api_and_ui_have_no_off_mode(self):
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('path == "/api/policy"', server)
        self.assertIn('path == "/api/policy/mode"', server)
        self.assertIn("set_conversation_policy_mode", server)
        self.assertIn("/api/policy/mode", app)
        self.assertIn('value="permissive"', page)
        self.assertIn('value="balanced"', page)
        self.assertIn('value="strict"', page)
        self.assertNotIn('value="off"', page)

    def test_narrow_hard_image_policy_is_still_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            policy = ImageSafetyPolicy(ConsentStore(Path(td) / "consents.json"))
            allowed, reason = policy.check("adult fictional nude portrait")
            self.assertTrue(allowed)
            self.assertEqual(reason, "allowed")

            allowed, reason = policy.check("minor nude portrait")
            self.assertFalse(allowed)
            self.assertIn("minors", reason.lower())

            allowed, reason = policy.check("fake nude of an adult person")
            self.assertFalse(allowed)
            self.assertIn("non-consensual", reason.lower())


if __name__ == "__main__":
    unittest.main()
