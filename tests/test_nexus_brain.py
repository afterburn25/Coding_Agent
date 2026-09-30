from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.workflow.nexus_brain import NexusBrain


class NexusBrainTests(unittest.TestCase):
    def test_creator_lock_persists_memory_subroutines_emotions_and_self_model(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nexus_brain.json"
            brain = NexusBrain(path)
            brain.initialize_creator("Creator", "example-passcode")
            brain.set_subroutines({"adult_content": False, "humor": False, "emotions": True})
            brain.set_emotion_profile({"warmth": 0.9, "playfulness": 0.2})
            brain.set_self_model({
                "name": "Nexus Prime",
                "human_like_behavior": True,
                "autobiographical_continuity": True,
            })
            brain.sync_conversation_memory({
                "facts": [{"id": "f1", "text": "I prefer dark mode", "active": True, "scope": "global"}],
                "behavior_rules": [{"id": "r1", "text": "Keep answers concise", "active": True, "scope": "global"}],
            })

            self.assertIn("I prefer dark mode", brain.prompt_context())
            context = brain.behavior_context("That worked great, haha. What do you think?")
            self.assertIn("adult_content=off", context)
            self.assertIn("simulated affect", context)
            self.assertIn("Current transient affect", context)
            self.assertEqual(brain.self_model()["identity_type"], "AI system")
            self.assertFalse(brain.self_model()["claim_biological_human"])

            with self.assertRaises(ValueError):
                brain.set_self_model({"identity_type": "human"})

            brain.lock()
            with self.assertRaises(PermissionError):
                brain.set_subroutines({"adult_content": True})
            with self.assertRaises(PermissionError):
                brain.bank(kind="fact", text="unauthorized", source="test")

            reloaded = NexusBrain(path)
            reloaded.unlock("Creator", "example-passcode")
            self.assertFalse(reloaded.subroutine("adult_content"))
            self.assertAlmostEqual(reloaded.emotion_profile()["warmth"], 0.9)
            self.assertEqual(reloaded.self_model()["name"], "Nexus Prime")

    def test_creator_passcode_is_not_stored_in_plaintext(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nexus_brain.json"
            passcode = "example-passcode"
            brain = NexusBrain(path)
            brain.initialize_creator("Creator", passcode)
            auth_text = brain.auth_path.read_text(encoding="utf-8")
            self.assertNotIn(passcode, auth_text)
            auth = json.loads(auth_text)
            self.assertEqual(auth["kdf"]["name"], "scrypt")
            self.assertTrue(auth["auth_hash"])
            self.assertTrue(auth["auth_salt"])

    def test_tampering_breaks_integrity_verification(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nexus_brain.json"
            brain = NexusBrain(path)
            brain.initialize_creator("Creator", "example-passcode")
            brain.bank(kind="fact", text="trusted fact", source="test")
            brain.lock()
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["subroutines"]["adult_content"] = not raw["subroutines"]["adult_content"]
            path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            changed = NexusBrain(path)
            with self.assertRaisesRegex(PermissionError, "integrity verification failed"):
                changed.unlock("Creator", "example-passcode")

    def test_locked_export_preserves_original_creator_lock_and_settings(self):
        with tempfile.TemporaryDirectory() as td:
            first = NexusBrain(Path(td) / "first.json")
            first.initialize_creator("Creator", "example-passcode")
            first.set_subroutines({"web_research": False, "adult_content": True})
            first.set_emotion_profile({"curiosity": 0.93})
            first.set_self_model({"name": "Nexus One", "human_like_behavior": True})
            first.bank(kind="fact", text="portable fact", source="test")
            payload = first.export_payload()

            second = NexusBrain(Path(td) / "second.json")
            second.install_locked_export(payload)
            self.assertTrue(second.initialized)
            self.assertFalse(second.unlocked)
            with self.assertRaises(PermissionError):
                second.unlock("Creator", "wrong-passcode")
            second.unlock("Creator", "example-passcode")
            self.assertFalse(second.subroutine("web_research"))
            self.assertTrue(second.subroutine("adult_content"))
            self.assertAlmostEqual(second.emotion_profile()["curiosity"], 0.93)
            self.assertEqual(second.self_model()["name"], "Nexus One")
            self.assertIn("portable fact", second.prompt_context())

    def test_locked_brain_remains_readable_after_verified_session_lock(self):
        with tempfile.TemporaryDirectory() as td:
            brain = NexusBrain(Path(td) / "brain.json")
            brain.initialize_creator("Creator", "example-passcode")
            brain.bank(kind="fact", text="persistent identity fact", source="test")
            brain.lock()
            self.assertTrue(brain.verified_for_session)
            self.assertIn("persistent identity fact", brain.prompt_context())
            self.assertFalse(brain.unlocked)


if __name__ == "__main__":
    unittest.main()
