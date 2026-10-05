"""Phase 9 — Safe Mode + golden-config snapshots + failure detection."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.safemode import (
    GoldenConfigStore, SafeModeStore, OFFER_THRESHOLD)


class SafeModeTests(unittest.TestCase):
    def make(self, td: str) -> SafeModeStore:
        return SafeModeStore(Path(td) / "safe_mode.json")

    def test_failure_counting_and_offer(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.make(td)
            self.assertFalse(s.should_offer())
            for _ in range(OFFER_THRESHOLD):
                s.record_boot(previous_clean=False)
            self.assertTrue(s.should_offer())
            self.assertEqual(s.consecutive_failures, OFFER_THRESHOLD)
            # A clean boot resets the streak.
            s.record_boot(previous_clean=True)
            self.assertEqual(s.consecutive_failures, 0)
            self.assertFalse(s.should_offer())

    def test_enter_exit_and_capabilities(self):
        with tempfile.TemporaryDirectory() as td:
            s = self.make(td)
            st = s.enter("repeated startup failures")
            self.assertTrue(st["active"])
            self.assertIn("model_autostart", st["disabled"])
            self.assertIn("autonomous_workers", st["disabled"])
            self.assertIn("diagnostics", st["available"])
            self.assertTrue(s.disabled("plugins"))
            self.assertFalse(s.disabled("core_ui"))
            # Persists across reload.
            self.assertTrue(self.make(td).is_active())
            st = s.exit()
            self.assertFalse(st["active"])
            self.assertEqual(st["consecutive_failures"], 0)

    def test_safe_mode_denies_autonomous_work(self):
        with tempfile.TemporaryDirectory() as td:
            from localcodeagent.autonomy.state import AutonomyStore
            from localcodeagent.autonomy.policy import AutonomyPolicy
            sm = self.make(td)
            ap = AutonomyPolicy(AutonomyStore(Path(td)), safemode=sm)
            self.assertNotEqual(
                ap.check("write_workspace",
                         profile="local_autonomous"), "deny")
            sm.enter("crash loop")
            self.assertTrue(ap.is_stopped())
            self.assertEqual(
                ap.check("read_files", profile="local_autonomous"),
                "deny")
            sm.exit()
            self.assertNotEqual(
                ap.check("read_files", profile="local_autonomous"),
                "deny")


class GoldenConfigTests(unittest.TestCase):
    def test_snapshot_verify_restore(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "config.json").write_text('{"v": 1}')
            (ws / "data" / "autonomy").mkdir(parents=True)
            (ws / "data" / "autonomy" / "control.json").write_text(
                '{"stop": false}')
            g = GoldenConfigStore(ws / "golden", ws)
            out = g.snapshot(label="known-good")
            self.assertTrue(out["ok"])
            self.assertEqual(out["files"], 2)  # policies.json absent
            # Corrupt live config, restore the snapshot.
            (ws / "config.json").write_text('{"v": 999}')
            out = g.restore(out["name"])
            self.assertTrue(out["ok"])
            self.assertEqual(json.loads(
                (ws / "config.json").read_text())["v"], 1)

    def test_tampered_snapshot_refuses_restore(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "config.json").write_text('{"v": 1}')
            g = GoldenConfigStore(ws / "golden", ws)
            name = g.snapshot()["name"]
            (ws / "golden" / name / "config.json").write_text(
                '{"v": "evil"}')
            out = g.verify(name)
            self.assertFalse(out["ok"])
            self.assertIn("hash", out["reason"])
            out = g.restore(name)
            self.assertFalse(out["ok"])
            # Live config untouched.
            self.assertEqual(json.loads(
                (ws / "config.json").read_text())["v"], 1)
            self.assertFalse(g.restore("snap-999")["ok"])

    def test_listing(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "config.json").write_text("{}")
            g = GoldenConfigStore(ws / "golden", ws)
            g.snapshot(label="a")
            g.snapshot(label="b")
            snaps = g.list()
            self.assertEqual(len(snaps), 2)
            self.assertTrue((ws / "golden" / "latest.txt").exists())


class SafeModeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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

    def test_safemode_roundtrip_over_http(self):
        st = self._get("/api/safemode")
        self.assertIn("active", st)
        self.assertIn("should_offer", st)
        out = self._post("/api/safemode/enter",
                         {"reason": "test"})[1]
        self.assertTrue(out["active"])
        # While active, the autonomy policy denies all action classes.
        self.assertTrue(self.state.autonomy.policy.is_stopped())
        out = self._post("/api/safemode/exit", {})[1]
        self.assertFalse(out["active"])
        self.assertFalse(self.state.autonomy.policy.is_stopped())

    def test_golden_over_http(self):
        cfg = self.state.workspace / "config.json"
        cfg.write_text('{"golden": true}')
        st, out = self._post("/api/golden/snapshot",
                             {"label": "http-test"})
        self.assertEqual(st, 200)
        self.assertTrue(out["ok"])
        snaps = self._get("/api/golden")["snapshots"]
        self.assertTrue(any(s["name"] == out["name"] for s in snaps))
        ver = self._get(f"/api/golden/verify/{out['name']}")
        self.assertTrue(ver["ok"])
        cfg.write_text('{"golden": false}')
        st, out = self._post("/api/golden/restore",
                             {"name": out["name"]})
        self.assertEqual(st, 200)
        self.assertTrue(out["ok"])
        self.assertIn("config.json", out["restored"])
        self.assertTrue(json.loads(cfg.read_text())["golden"])
        st, _ = self._post("/api/golden/restore", {"name": "ghost"})
        self.assertEqual(st, 400)


class SessionMarkerTests(unittest.TestCase):
    """Per-install session-marker scoping — a foreign backend that shares
    the data dir (dev/soak runs) must not dirty this install's crash
    accounting; that once tripped a spurious LKG auto-rollback."""

    def _bare_state(self, marker: Path):
        from localcodeagent.server import AppState
        st = AppState.__new__(AppState)
        st._session_marker = marker
        st._session_started = 1700000000.0
        return st

    def test_owner_key_is_stable_and_path_shaped(self):
        from localcodeagent.server import _session_owner_key
        k1 = _session_owner_key()
        self.assertRegex(k1, r"^[0-9a-f]{12}$")
        self.assertEqual(k1, _session_owner_key())

    def test_legacy_marker_migrated_once(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            data.mkdir(parents=True)
            scoped = data / "session-aaaa1111bbbb.json"
            legacy = data / "session.json"
            legacy.write_text(json.dumps(
                {"pid": 1, "started": 1.0, "clean_shutdown": False}))
            st = self._bare_state(scoped)
            prior = st._read_prior_session()
            self.assertIsNotNone(prior)
            self.assertFalse(legacy.exists())
            # After migration, foreign writes to the legacy file are
            # ignored entirely — a hard-killed dev backend cannot inject
            # phantom crashes into this install's accounting.
            legacy.write_text(json.dumps(
                {"pid": 2, "started": 2.0, "clean_shutdown": False}))
            st._write_session_marker(clean=True)
            st2 = self._bare_state(scoped)
            self.assertIsNone(st2._read_prior_session())

    def test_foreign_scoped_markers_do_not_interfere(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            data.mkdir(parents=True)
            mine = data / "session-aaaa1111bbbb.json"
            foreign = data / "session-cccc3333dddd.json"
            # A foreign install dies dirty — writes only its own file.
            foreign.write_text(json.dumps(
                {"pid": 9, "started": 1.0, "clean_shutdown": False}))
            st = self._bare_state(mine)
            self.assertIsNone(st._read_prior_session())
            # And its clean shutdown cannot erase evidence of MY crash.
            mine.write_text(json.dumps(
                {"pid": 4, "started": 1.0, "clean_shutdown": False}))
            foreign.write_text(json.dumps(
                {"pid": 9, "started": 1.0, "clean_shutdown": True}))
            self.assertIsNotNone(st._read_prior_session())

    def test_marker_pruning_bounds_files(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            data.mkdir(parents=True)
            mine = data / "session-aaaa1111bbbb.json"
            mine.write_text("{}")
            for i in range(12):
                (data / f"session-{i:012x}.json").write_text("{}")
            st = self._bare_state(mine)
            st._prune_session_markers()
            leftovers = list(data.glob("session-*.json"))
            self.assertLessEqual(len(leftovers), 8)
            self.assertIn(mine, leftovers)


if __name__ == "__main__":
    unittest.main()
