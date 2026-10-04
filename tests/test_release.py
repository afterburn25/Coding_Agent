"""Phase 10 — Release Candidate mode + scorecard."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.release import RCManager, SECTIONS


def make(tmp: str) -> RCManager:
    return RCManager(Path(tmp) / "rc.json")


class RCModeTests(unittest.TestCase):
    def test_enter_freezes_and_exit_releases(self):
        with tempfile.TemporaryDirectory() as td:
            rc = make(td)
            self.assertFalse(rc.is_active())
            st = rc.enter("v0.15.0-rc1")
            self.assertTrue(st["active"])
            self.assertIn("self_development", st["frozen_actions"])
            self.assertIn("packages", st["frozen_actions"])
            self.assertTrue(make(td).is_active())  # persisted
            rc.exit()
            self.assertFalse(rc.is_active())

    def test_rc_denies_frozen_actions_via_policy(self):
        with tempfile.TemporaryDirectory() as td:
            from localcodeagent.autonomy.state import AutonomyStore
            from localcodeagent.autonomy.policy import AutonomyPolicy
            rc = make(td)
            ap = AutonomyPolicy(AutonomyStore(Path(td)), rc=rc)
            rc.enter()
            self.assertEqual(
                ap.check("self_development",
                         profile="extended_autonomous"), "deny")
            self.assertEqual(
                ap.check("packages", profile="extended_autonomous"),
                "deny")
            # Essential work is NOT frozen.
            self.assertNotEqual(
                ap.check("run_tests", profile="local_autonomous"),
                "deny")
            rc.exit()
            self.assertNotEqual(
                ap.check("packages", profile="extended_autonomous"),
                "deny")


class ScorecardTests(unittest.TestCase):
    def _fill(self, rc: RCManager, status: str = "pass") -> None:
        for s in SECTIONS:
            rc.record_section(s, status, detail="ok")

    def test_all_pass_verdict(self):
        with tempfile.TemporaryDirectory() as td:
            rc = make(td)
            self._fill(rc)
            card = rc.run_scorecard()
            self.assertEqual(card["verdict"], "PASS")
            self.assertEqual(card["blocking_issues"], [])
            self.assertEqual(len(card["sections"]), len(SECTIONS))
            # Persisted history survives reload.
            rc2 = make(td)
            self.assertEqual(rc2.status()["scorecards"], 1)
            self.assertEqual(
                rc2.status()["last_scorecard"]["verdict"], "PASS")

    def test_blocking_fail_means_fail(self):
        with tempfile.TemporaryDirectory() as td:
            rc = make(td)
            self._fill(rc)
            rc.record_section("installer", "fail",
                              detail="installer smoke failed")
            card = rc.run_scorecard()
            self.assertEqual(card["verdict"], "FAIL")
            self.assertTrue(any("installer" in i
                                for i in card["blocking_issues"]))

    def test_unknown_blocking_means_fail(self):
        with tempfile.TemporaryDirectory() as td:
            rc = make(td)
            self._fill(rc)
            # Drop one manual record → section unknown → not ready.
            del rc.data["manual"]["voice"]
            card = rc.run_scorecard()
            self.assertEqual(card["verdict"], "FAIL")

    def test_nonblocking_fail_is_known_issue(self):
        with tempfile.TemporaryDirectory() as td:
            rc = make(td)
            self._fill(rc)
            rc.record_section("images", "fail", blocking=False,
                              detail="image backend offline")
            card = rc.run_scorecard()
            self.assertEqual(card["verdict"], "PASS")
            self.assertTrue(any("images" in i
                                for i in card["known_issues"]))

    def test_live_check_overrides_and_raises_fail(self):
        with tempfile.TemporaryDirectory() as td:
            rc = make(td)
            self._fill(rc)
            rc.register_check("voice", lambda: {
                "status": "fail", "detail": "TTS probe silent"})
            card = rc.run_scorecard()
            self.assertEqual(card["verdict"], "FAIL")
            self.assertEqual(
                card["sections"]["voice"]["source"], "live")

            def boom():
                raise RuntimeError("probe crashed")
            rc.register_check("profiles", boom)
            card = rc.run_scorecard()
            self.assertEqual(card["sections"]["profiles"]["status"],
                             "fail")

    def test_invalid_section_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            rc = make(td)
            self.assertIsNone(rc.record_section("bogus", "pass"))
            self.assertIsNone(rc.record_section("voice", "meh"))


class RCHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True)
        ws = Path(cls._td.name)
        cfg = AgentConfig(
            profiles_onboarding_gate=False,
            models=[ModelProfile(id="fake",
                                 endpoint="http://127.0.0.1:1/v1",
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
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return json.loads(r.read())

    def _post(self, path, body):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_rc_lifecycle_and_scorecard_over_http(self):
        st, out = self._post("/api/rc/enter", {"label": "rc-http"})
        self.assertEqual(st, 200)
        self.assertTrue(out["active"])
        # Freeze is real: self_development denies under RC.
        self.assertEqual(
            self.state.autonomy.policy.check(
                "self_development", profile="extended_autonomous"),
            "deny")
        # Fill the remaining sections; live checks cover
        # workers/performance/memory.
        for s in SECTIONS:
            if s in {"workers", "performance", "memory"}:
                continue
            st, _ = self._post("/api/rc/section", {
                "section": s, "status": "pass"})
            self.assertEqual(st, 200)
        st, card = self._post("/api/rc/scorecard", {})
        self.assertEqual(st, 200)
        self.assertEqual(card["verdict"], "PASS")
        self.assertEqual(card["sections"]["workers"]["source"], "live")
        status = self._get("/api/rc")
        self.assertEqual(status["last_scorecard"]["id"], card["id"])
        st, _ = self._post("/api/rc/exit", {})
        self.assertEqual(st, 200)
        st, _ = self._post("/api/rc/section",
                           {"section": "bogus", "status": "pass"})
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
