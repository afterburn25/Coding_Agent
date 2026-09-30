from __future__ import annotations

import json
import tempfile
import time
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
            self.assertEqual(auth["key_type"], "Ed25519")
            self.assertIn("ENCRYPTED PRIVATE KEY", auth["encrypted_private_key_pem"])
            self.assertIn("PUBLIC KEY", auth["public_key_pem"])
            self.assertEqual(len(auth["public_key_sha256"]), 64)

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
            self.assertFalse(changed.verified_for_session)
            self.assertEqual(changed.summary()["integrity"], "tampered")
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
            first.bank(
                kind="knowledge",
                text="The fictional Example Planet has two moons.",
                source="knowledge_memory",
                source_id="portable-k1",
                metadata={
                    "query": "How many moons does Example Planet have?",
                    "expires_at": 0,
                    "current_sensitive": False,
                    "sources": [{"title": "Example astronomy", "url": "https://example.test/moons"}],
                },
            )
            first.bank(
                kind="training_signal",
                text="Instruction: greet me warmly\nResponse: Good to see you — what are we getting into today?",
                source="model_growth",
                source_id="portable-t1",
                metadata={"candidate_kind": "conversation_example", "candidate_status": "approved"},
            )
            payload = first.export_payload()
            self.assertNotIn("encrypted_private_key_pem", payload["creator_lock"])
            self.assertTrue(payload["creator_lock"]["distribution_read_only"])
            self.assertIn("PUBLIC KEY", payload["creator_lock"]["public_key_pem"])

            second = NexusBrain(Path(td) / "second.json")
            second.install_locked_export(payload)
            self.assertTrue(second.initialized)
            self.assertFalse(second.unlocked)
            self.assertTrue(second.verified_for_session)
            self.assertEqual(second.summary()["signature_scheme"], "ed25519")
            self.assertTrue(second.summary()["distribution_read_only"])
            self.assertFalse(second.summary()["creator_signing_key_available"])
            self.assertIn("portable fact", second.prompt_context())
            with self.assertRaisesRegex(PermissionError, "public read-only Nexus Brain distribution"):
                second.unlock("Creator", "example-passcode")
            self.assertFalse(second.subroutine("web_research"))
            self.assertTrue(second.subroutine("adult_content"))
            self.assertAlmostEqual(second.emotion_profile()["curiosity"], 0.93)
            self.assertEqual(second.self_model()["name"], "Nexus One")
            self.assertIn("portable fact", second.prompt_context())
            imported_knowledge = second.knowledge_context("How many moons does Example Planet have?")
            self.assertIn("two moons", imported_knowledge)
            self.assertIn("example.test/moons", imported_knowledge)
            imported_skill = second.training_context("Please greet me warmly")
            self.assertIn("Good to see you", imported_skill)
            self.assertIn("do not copy", imported_skill)

    def test_general_knowledge_is_recalled_and_expired_current_sensitive_records_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            brain = NexusBrain(Path(td) / "brain.json")
            brain.initialize_creator("Creator", "example-passcode")
            brain.sync_knowledge_records([
                {
                    "id": "k1",
                    "query": "What is ExampleLib?",
                    "answer": "ExampleLib is a fictional test library.",
                    "sources": [{"title": "Official docs", "url": "https://example.test/docs"}],
                    "current_sensitive": False,
                    "learned_at": 100,
                    "expires_at": 0,
                },
                {
                    "id": "k2",
                    "query": "What is the current ExampleLib version?",
                    "answer": "ExampleLib version 1.0 is current.",
                    "sources": [{"title": "Release notes", "url": "https://example.test/releases"}],
                    "current_sensitive": True,
                    "learned_at": 100,
                    "expires_at": 1,
                },
            ])
            context = brain.knowledge_context("Tell me about ExampleLib")
            self.assertIn("fictional test library", context)
            self.assertNotIn("version 1.0 is current", context)
            self.assertIn("https://example.test/docs", context)

    def test_creator_sync_banks_bounded_autobiographical_continuity(self):
        with tempfile.TemporaryDirectory() as td:
            brain = NexusBrain(Path(td) / "brain.json")
            brain.initialize_creator("Creator", "example-passcode")
            count = brain.sync_conversations({
                "conversations": [{
                    "id": "chat1",
                    "title": "Built the scanner",
                    "summary": "User topics: scanner latency. Recent responses: fixed batching.",
                    "message_count": 8,
                    "updated_at": 1234.0,
                }]
            })
            self.assertEqual(count, 1)
            context = brain.prompt_context()
            self.assertIn("Autobiographical continuity", context)
            self.assertIn("Built the scanner", context)
            self.assertIn("scanner latency", context)

    def test_public_distribution_accepts_only_newer_same_creator_signed_updates(self):
        with tempfile.TemporaryDirectory() as td:
            creator = NexusBrain(Path(td) / "creator.json")
            creator.initialize_creator("Creator", "example-passcode")
            creator.bank(kind="fact", text="version one fact", source="test")
            first_payload = creator.export_payload()

            recipient = NexusBrain(Path(td) / "recipient.json")
            recipient.install_locked_export(first_payload)
            self.assertTrue(recipient.verified_for_session)
            self.assertIn("version one fact", recipient.prompt_context())

            time.sleep(0.01)
            creator.bank(kind="fact", text="version two fact", source="test")
            second_payload = creator.export_payload()
            result = recipient.install_signed_update(second_payload)
            self.assertTrue(result["updated"])
            self.assertIn("version two fact", recipient.prompt_context())

            stale = recipient.install_signed_update(first_payload)
            self.assertFalse(stale["updated"])

            other = NexusBrain(Path(td) / "other.json")
            other.initialize_creator("Other Creator", "other-passcode")
            other.bank(kind="fact", text="hostile replacement", source="test")
            with self.assertRaisesRegex(PermissionError, "existing creator key"):
                recipient.install_signed_update(other.export_payload())

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
