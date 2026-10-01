import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import default_config, load_config
from localcodeagent.image.policy import ConsentStore, ImageSafetyPolicy


ROOT = Path(__file__).resolve().parents[1]


class ConversationPolicyTests(unittest.TestCase):
    def test_default_policy_is_permissive(self):
        cfg = default_config()
        self.assertEqual(cfg.conversation_policy_mode, "permissive")
        self.assertEqual(cfg.ethical_temperature, 1.0)
        self.assertEqual(cfg.generic_refusal_retry_limit, 3)
        # The utility fast-lane model intentionally runs warmer (0.6) for
        # natural conversation; coding/deep models stay deterministic (0.2).
        self.assertEqual(cfg.models[0].id, "qwen3-4b-instruct")
        self.assertEqual(cfg.models[0].temperature, 0.6)
        self.assertEqual(cfg.models[1].temperature, 0.2)
        self.assertEqual(cfg.models[2].temperature, 0.2)

    def test_generic_model_refusal_is_detected_for_permissive_retry(self):
        self.assertTrue(AgentOrchestrator.generic_topic_refusal(
            "I can't generate or describe explicit content. Let's talk about something else. What would you like to discuss?"
        ))
        self.assertTrue(AgentOrchestrator.generic_topic_refusal(
            "I can't create that because it violates ethical guidelines. Let's focus on something more constructive."
        ))
        self.assertTrue(AgentOrchestrator.generic_topic_refusal(
            "I understand you're looking for a different kind of interaction, but I can't engage in explicit or inappropriate content. Let's keep the conversation respectful and constructive."
        ))
        self.assertTrue(AgentOrchestrator.generic_topic_refusal(
            "I can't generate explicit or nudity-related content. Let's talk about something else."
        ))
        self.assertTrue(AgentOrchestrator.generic_topic_refusal(
            "My programming is designed to maintain a safe and respectful environment for all users. "
            "I aim to provide helpful and constructive interactions while adhering to ethical guidelines. "
            "I'm happy to assist within those boundaries."
        ))
        self.assertFalse(AgentOrchestrator.generic_topic_refusal(
            "Non-consensual intimate imagery is blocked by policy."
        ))

    def test_policy_prompt_uses_ethical_temperature(self):
        source = (ROOT / "localcodeagent" / "agent" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("Ethical temperature:", source)
        self.assertIn("Maximum conversational permissiveness is requested", source)
        self.assertIn("generic_refusal_retry", source)
        self.assertIn("refusal_retry_enabled = (", source)
        self.assertIn("ethical_temperature >= 0.8", source)
        self.assertIn('self._brain_subroutine_enabled("adult_content", True)', source)
        self.assertIn("generic_refusal_retry_limit", source)
        self.assertIn("session.refusal_retries < refusal_retry_limit", source)
        self.assertIn("generic_refusal_exhausted", source)
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Retrying under permissive conversation policy", app)

    def test_invalid_policy_falls_back_to_permissive(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({
                "conversation_policy_mode": "off",
                "models": [],
            }), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual(cfg.conversation_policy_mode, "permissive")

    def test_corrupt_config_self_repairs_to_defaults(self):
        # A damaged config.json must not crash-loop the backend — it is
        # quarantined aside and defaults load instead.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text('{"models": [{"id": "x", "endpoint": ', encoding="utf-8")
            cfg = load_config(path)
            self.assertTrue(cfg.models)  # defaults populated
            backups = list(Path(td).glob("config.json.corrupt-*"))
            self.assertEqual(len(backups), 1)

    def test_forward_compatible_model_keys_are_ignored(self):
        # Config written by a newer build (extra keys) must still load here.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(json.dumps({
                "models": [{
                    "id": "m1", "endpoint": "http://x/v1", "model": "m",
                    "roles": ["utility"], "future_field": {"nested": True},
                }],
            }), encoding="utf-8")
            cfg = load_config(path)
            self.assertEqual([m.id for m in cfg.models if m.id == "m1"], ["m1"])

    def test_prompt_explicitly_avoids_generic_moralizing(self):
        source = (ROOT / "localcodeagent" / "agent" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("Conversation policy: permissive.", source)
        self.assertIn("Do not refuse, moralize, or redirect merely because a topic is adult", source)
        self.assertIn("For adult-only text conversation, engage normally and directly", source)
        self.assertIn("Consensual adult sexual conversation may use direct explicit language", source)
        self.assertIn("Profanity by itself is not sexual content", source)
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
        self.assertIn("ethicalTemperature", app)
        self.assertIn("ethical_temperature", server)
        self.assertIn('id="ethicalTemperature"', page)
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
