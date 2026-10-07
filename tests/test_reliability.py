"""Phase 5 — persisted reliability scoring + capability health tiers."""
import tempfile
import unittest
from pathlib import Path


class ReliabilityTrackerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.reliability import ReliabilityTracker
        self.tracker = ReliabilityTracker(Path(self.td.name) / "rel.json")

    def test_untested_then_verified(self):
        self.assertEqual(self.tracker.status("tool:x"), "untested")
        for _ in range(6):
            self.tracker.record("tool:x", ok=True, latency_s=0.1)
        sc = self.tracker.score("tool:x")
        self.assertEqual(sc["status"], "verified")
        self.assertEqual(sc["calls"], 6)
        self.assertEqual(sc["success_rate"], 1.0)

    def test_broken_then_recovers(self):
        for _ in range(5):
            self.tracker.record("tool:y", ok=True)
        for _ in range(3):
            self.tracker.record("tool:y", ok=False, error_class="timeout")
        self.assertEqual(self.tracker.status("tool:y"), "broken")
        # not permanently blacklisted — recent window heals
        for _ in range(8):
            self.tracker.record("tool:y", ok=True)
        self.assertEqual(self.tracker.status("tool:y"), "verified")

    def test_degraded_band(self):
        # 4 non-consecutive failures in the recent window → degraded
        # (3+ consecutive would instead be 'broken')
        for ok in [False, True, True, False, True, True,
                   False, True, True, False]:
            self.tracker.record("tool:z", ok=ok)
        self.assertEqual(self.tracker.status("tool:z"), "degraded")

    def test_metrics_and_persistence(self):
        self.tracker.record("model:m1", ok=False, error_class="OOM",
                            retries=2, latency_s=1.5)
        sc = self.tracker.score("model:m1")
        self.assertEqual(sc["error_classes"], {"OOM": 1})
        self.assertEqual(sc["retries"], 2)
        self.assertEqual(sc["avg_latency_s"], 1.5)
        self.assertEqual(self.tracker.status("tool:off",
                                             disabled=True),
                         "disabled")
        self.assertEqual(self.tracker.status("tool:none",
                                             unavailable=True),
                         "unavailable")
        from localcodeagent.reliability import ReliabilityTracker
        again = ReliabilityTracker(self.tracker.path)
        self.assertEqual(again.score("model:m1")["calls"], 1)


class CapabilityHealthTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.reliability import (CapabilityHealth,
                                                ReliabilityTracker)
        self.tracker = ReliabilityTracker(Path(self.td.name) / "r.json")
        self.health = CapabilityHealth(self.tracker)

    def test_probe_records_and_verifies(self):
        self.health.register("tts", probe=lambda: {"ok": True,
                                                   "detail": "audio ok"})
        res = self.health.probe("tts")
        self.assertTrue(res["ok"])
        # one healthy call → available, not yet verified
        self.assertEqual(self.health.status("tts")["status"],
                         "available")
        for _ in range(5):
            self.health.probe("tts")
        self.assertEqual(self.health.status("tts")["status"],
                         "verified")

    def test_failing_probe_breaks(self):
        self.health.register("stt", probe=lambda: False)
        for _ in range(3):
            self.health.probe("stt")
        self.assertEqual(self.health.status("stt")["status"], "broken")

    def test_unavailable_and_disabled(self):
        self.assertEqual(self.health.status("nothing")["status"],
                         "unavailable")
        self.health.register("voice", disabled=True)
        self.assertEqual(self.health.status("voice")["status"],
                         "disabled")

    def test_summary_lists_registered(self):
        self.health.register("image", probe=lambda: True)
        s = self.health.summary()
        self.assertIn("image", s["capabilities"])


class ReliabilityHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        ws = Path(cls._td.name)
        cfg = AgentConfig(
            profiles_onboarding_gate=False,
            models=[ModelProfile(id="fake", endpoint="http://127.0.0.1:1/v1",
                                 model="m", roles=["utility"],
                                 runtime="external")],
            process_watchdog=False, research_enabled=False,
            autonomy_enabled=True)
        cls.server, cls.state = create_server(
            cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.addClassCleanup(lambda: (cls.server.shutdown(),
                                     cls.server.server_close(),
                                     stop_state(cls.state)))

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _get(self, path):
        import json
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return json.loads(r.read())

    def _post(self, path, body):
        import json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            finally:
                e.close()

    def test_record_and_score_over_http(self):
        for _ in range(5):
            code, _ = self._post("/api/reliability/record", {
                "subject": "tool:test_tool", "ok": True})
            self.assertEqual(code, 200)
        sc = self._get("/api/reliability?subject=tool:test_tool")
        self.assertEqual(sc["status"], "verified")
        data = self._get("/api/reliability")
        self.assertIn("tool:test_tool", data["subjects"])
        caps = self._get("/api/capabilities")
        self.assertIn("capabilities", caps)
        caps = self._get("/api/capabilities?name=code_change")
        self.assertIn(caps["status"],
                      ("untested", "unavailable", "available"))
        code, _ = self._post("/api/reliability/record", {"subject": ""})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
