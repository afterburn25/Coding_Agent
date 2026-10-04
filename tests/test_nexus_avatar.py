"""Nexus avatar state tests — bounded activity/expression hooks used by the
voice, gesture, and operational-state presentation layer."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.nexus_avatar import NexusAvatar


class NexusAvatarStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.web = Path(self.tmp.name) / "web"
        self.cache = Path(self.tmp.name) / "cache"
        (self.web / "assets").mkdir(parents=True)
        self.avatar = NexusAvatar(self.web, self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def test_activity_is_bounded_and_validated(self):
        self.assertFalse(self.avatar.set_activity("conscious"))
        self.assertTrue(self.avatar.set_activity("listening", hold_s=0.01))
        self.assertEqual(self.avatar.activity(), "listening")
        self.avatar._activity_until = time.time() - 0.01
        self.assertEqual(self.avatar.activity(), "idle")

    def test_utterance_sets_speaking(self):
        self.assertTrue(self.avatar.note_utterance(1.25))
        self.assertEqual(self.avatar.status()["activity"], "speaking")
        self.assertFalse(self.avatar.note_utterance(0))
        self.assertFalse(self.avatar.note_utterance("bad"))

    def test_voice_lifecycle_recovers_to_idle(self):
        self.avatar.on_voice_event({"event": "queued"})
        self.assertEqual(self.avatar.activity(), "thinking")
        self.avatar.on_voice_event({"event": "segment", "seconds": 2})
        self.assertEqual(self.avatar.activity(), "speaking")
        self.avatar.on_voice_event({"event": "stop"})
        self.assertEqual(self.avatar.activity(), "idle")

    def test_gesture_updates_semantic_expression(self):
        self.avatar.on_gesture("small_nod")
        status = self.avatar.status()
        self.assertEqual(status["expression"], "friendly")
        self.assertIn("speaking", status["activities"])

    def test_status_falls_back_to_emblem(self):
        status = self.avatar.status()
        self.assertFalse(status["canonical"])
        self.assertEqual(status["fallback"], "nexus-core-icon.png")


if __name__ == "__main__":
    unittest.main()
