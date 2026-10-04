"""Phase 7 — resource modes, expiring overrides, offline mode,
project egress policies."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.policies import (
    EGRESS_MODES, NETWORK_ACTIONS, ResourcePolicies)


def make(tmp: str) -> ResourcePolicies:
    return ResourcePolicies(Path(tmp) / "policies.json")


class ResourceModeTests(unittest.TestCase):
    def test_modes_and_knobs(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            self.assertEqual(p.mode(), "balanced")
            for mode in ("performance", "quiet", "battery",
                         "conservative"):
                self.assertTrue(p.set_mode(mode))
                self.assertEqual(p.mode(), mode)
            self.assertFalse(p.set_mode("ludicrous"))
            # Conservative caps workers at 2 but keeps models warm.
            self.assertEqual(p.knobs()["max_workers"], 2)
            self.assertTrue(p.knobs()["warm_models"])
            # Battery yields GPU, caps workers at 1, small models only.
            p.set_mode("battery")
            self.assertEqual(p.knobs()["max_workers"], 1)
            self.assertFalse(p.knobs()["background_jobs"])
            self.assertTrue(p.knobs()["gpu_yield"])
            self.assertEqual(p.knobs()["max_model_tier"], "small")

    def test_mode_persists(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            p.set_mode("quiet")
            self.assertEqual(make(td).mode(), "quiet")
            self.assertFalse(make(td).knobs()["background_jobs"])


class OverrideTests(unittest.TestCase):
    def test_override_expires(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            row = p.add_override("priority", "coding",
                                 ttl_seconds=0.05, source="test")
            self.assertEqual(p.effective("priority"), "coding")
            time.sleep(0.08)
            self.assertIsNone(p.effective("priority"))
            self.assertEqual(p.active_overrides(), [])
            # Expired rows remain in the bounded history.
            self.assertEqual(p.data["overrides"][0]["id"], row["id"])

    def test_revoke(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            row = p.add_override("model_deny", "30b")
            self.assertEqual(p.effective("model_deny"), "30b")
            p.revoke(row["id"])
            self.assertIsNone(p.effective("model_deny"))
            self.assertIsNone(p.revoke("nope"))

    def test_session_override_has_no_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            p.add_override("priority", "image")  # ttl=0
            self.assertEqual(make(td).effective("priority"), "image")


class OfflineAndEgressTests(unittest.TestCase):
    def test_offline_denies_network(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            ok, _ = p.network_allowed(action="research")
            self.assertTrue(ok)
            p.set_offline(True)
            ok, why = p.network_allowed(action="research")
            self.assertFalse(ok)
            self.assertIn("offline", why)
            self.assertTrue(make(td).is_offline())

    def test_project_egress(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            self.assertIsNone(p.set_egress("p1", "bogus"))
            p.set_egress("p1", "local_only")
            ok, why = p.network_allowed(project_id="p1",
                                        action="research")
            self.assertFalse(ok)
            self.assertIn("local_only", why)
            # Unknown projects default to network_read_allowed.
            ok, _ = p.network_allowed(project_id="other",
                                      action="research")
            self.assertTrue(ok)

    def test_restricted_egress_blocks_writes_not_reads(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            p.set_egress("p2", "restricted")
            ok, _ = p.network_allowed(project_id="p2", action="research")
            self.assertTrue(ok)
            ok, why = p.network_allowed(project_id="p2",
                                        action="git_push")
            self.assertFalse(ok)
            self.assertIn("restricted", why)


class AutonomyGateTests(unittest.TestCase):
    """Offline/egress denies flow through AutonomyPolicy.check."""

    def _policy(self, tmp):
        from localcodeagent.autonomy.state import AutonomyStore
        from localcodeagent.autonomy.policy import AutonomyPolicy
        store = AutonomyStore(Path(tmp))
        return AutonomyPolicy(store, policies=make(tmp)), make(tmp)

    def test_offline_denies_research(self):
        with tempfile.TemporaryDirectory() as td:
            from localcodeagent.autonomy.state import AutonomyStore
            from localcodeagent.autonomy.policy import AutonomyPolicy
            pols = make(td)
            ap = AutonomyPolicy(AutonomyStore(Path(td)), policies=pols)
            self.assertNotEqual(
                ap.check("research", profile="extended_autonomous"),
                "deny")
            pols.set_offline(True)
            self.assertEqual(
                ap.check("research", profile="extended_autonomous"),
                "deny")
            # Non-network actions unaffected.
            self.assertNotEqual(
                ap.check("read_files", profile="local_autonomous"),
                "deny")

    def test_local_only_scope_denies_research(self):
        with tempfile.TemporaryDirectory() as td:
            from localcodeagent.autonomy.state import AutonomyStore
            from localcodeagent.autonomy.policy import AutonomyPolicy
            pols = make(td)
            pols.set_egress("proj-a", "local_only")
            ap = AutonomyPolicy(AutonomyStore(Path(td)), policies=pols)
            self.assertEqual(
                ap.check("research", profile="extended_autonomous",
                         scope="proj-a"), "deny")


class NaturalLanguageTests(unittest.TestCase):
    def test_pause_background(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            out = p.parse_request("Pause background work for 30 minutes.")
            keys = [a["key"] for a in out["applied"]]
            self.assertIn("background_jobs", keys)
            self.assertEqual(p.effective("background_jobs"), False)
            self.assertGreater(out["applied"][0]["expires_at"],
                               time.time() + 1700)

    def test_spec_example(self):
        """'For the next hour, prioritize coding and don't load 30B.'"""
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            out = p.parse_request(
                "For the next hour, prioritize coding and "
                "don't load 30B.")
            self.assertEqual(p.effective("priority"), "coding")
            self.assertIn("30b", str(p.effective("model_deny")))
            for a in out["applied"]:
                self.assertGreaterEqual(a["expires_at"],
                                        time.time() + 3500)

    def test_quiet_and_offline(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            out = p.parse_request("Run quietly.")
            self.assertEqual(out["mode"], "quiet")
            self.assertEqual(p.mode(), "quiet")
            out = p.parse_request("Go offline tonight.")
            self.assertTrue(p.is_offline())
            p.parse_request("Back online.")
            self.assertFalse(p.is_offline())

    def test_expiry_restores_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            p = make(td)
            p.parse_request("Pause background work for 1 second.")
            self.assertFalse(p.effective("background_jobs"))
            time.sleep(1.05)
            self.assertIsNone(p.effective("background_jobs"))


class PoliciesHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory()
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
        with urllib.request.urlopen(self.base + path,
                                    timeout=15) as r:
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

    def test_summary_and_mode(self):
        s = self._get("/api/policies")
        self.assertIn(s["mode"],
                      ("performance", "balanced", "quiet", "battery",
                       "conservative"))
        st, out = self._post("/api/policies/mode", {"mode": "battery"})
        self.assertEqual(st, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["summary"]["mode"], "battery")
        self.assertEqual(out["summary"]["knobs"]["max_workers"], 1)
        st, _ = self._post("/api/policies/mode", {"mode": "warp"})
        self.assertEqual(st, 400)
        # Legacy autonomy route still accepts the new modes.
        st, out = self._post("/api/autonomy/policy",
                             {"resource_mode": "balanced"})
        self.assertEqual(st, 200)
        self.assertEqual(out["resource_mode"], "balanced")

    def test_nl_request_over_http(self):
        st, out = self._post("/api/policies/request", {
            "text": "Pause background work for 2 hours."})
        self.assertEqual(st, 200)
        self.assertFalse(self._get("/api/policies")
                         ["active_overrides"] == [])
        self.assertEqual(
            self.state.policies.effective("background_jobs"), False)

    def test_offline_and_egress_over_http(self):
        st, out = self._post("/api/policies/offline", {"on": True})
        self.assertEqual(st, 200)
        self.assertTrue(out["offline"])
        self.assertTrue(self._get("/api/policies")["offline"])
        self._post("/api/policies/offline", {"on": False})
        st, out = self._post("/api/policies/egress", {
            "project_id": "http-proj", "mode": "local_only"})
        self.assertEqual(st, 200)
        eg = self._get("/api/policies/egress/http-proj")
        self.assertEqual(eg["mode"], "local_only")
        st, _ = self._post("/api/policies/egress", {
            "project_id": "x", "mode": "yolo"})
        self.assertEqual(st, 400)

    def test_override_revoke_over_http(self):
        st, out = self._post("/api/policies/override", {
            "key": "priority", "value": "image", "ttl_seconds": 600})
        self.assertEqual(st, 200)
        pid = out["override"]["id"]
        st, out = self._post("/api/policies/revoke", {"id": pid})
        self.assertEqual(st, 200)
        self.assertTrue(out["override"]["revoked"])
        st, _ = self._post("/api/policies/revoke", {"id": "ghost"})
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
