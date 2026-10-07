"""Phase 11 — dependency intelligence + environment manifests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.dependencies import DependencyStore, version_newer
from localcodeagent.environment import EnvironmentStore, _satisfies


def make_deps(tmp: str) -> DependencyStore:
    return DependencyStore(Path(tmp) / "deps.json")


def make_env(tmp: str, probe=None) -> EnvironmentStore:
    return EnvironmentStore(Path(tmp) / "env.json", probe=probe)


class VersionCompareTests(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(version_newer("2.0.0", "1.9.9"))
        self.assertTrue(version_newer("1.10.0", "1.9.9"))
        self.assertFalse(version_newer("1.9.9", "2.0.0"))
        self.assertFalse(version_newer("abc", "1.0"))

    def test_satisfies(self):
        self.assertTrue(_satisfies("3.11.4", ">=3.10"))
        self.assertFalse(_satisfies("3.9.0", ">=3.10"))
        self.assertTrue(_satisfies("20.1.2", "20.x"))
        self.assertFalse(_satisfies("21.0.0", "20.x"))
        self.assertTrue(_satisfies("anything", ""))


class DependencyStoreTests(unittest.TestCase):
    def test_track_update_outdated(self):
        with tempfile.TemporaryDirectory() as td:
            s = make_deps(td)
            row = s.track("requests", installed_version="2.31.0",
                          available_version="2.32.3",
                          advisory="CVE-2024-x", project_id="p1")
            self.assertTrue(row["outdated"])
            # Update same row.
            s.track("requests", ecosystem="pip",
                    installed_version="2.32.3")
            deps = s.list()
            self.assertEqual(len(deps), 1)
            self.assertFalse(deps[0]["outdated"])
            self.assertEqual(deps[0]["advisory"], "CVE-2024-x")
            self.assertEqual(len(s.list(project_id="p1")), 1)
            self.assertEqual(len(s.list(outdated_only=True)), 0)
            self.assertEqual(make_deps(td).summary()["tracked"], 1)

    def test_staged_upgrade_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            s = make_deps(td)
            dep = s.track("numpy", installed_version="1.26.0",
                          available_version="2.0.0")
            upg = s.stage_upgrade(dep["id"], "2.0.0")
            self.assertEqual(upg["status"], "pending")
            # Can't promote early.
            self.assertIsNone(
                s.conclude_upgrade(upg["id"], promote=True))
            for stage in ("isolated_env", "install", "build", "tests",
                          "review"):
                row = s.record_upgrade_stage(upg["id"], stage, "passed")
            self.assertEqual(row["status"], "passed")
            row = s.conclude_upgrade(upg["id"], promote=True)
            self.assertEqual(row["status"], "promoted")
            # Dependency row now reflects the new version.
            self.assertEqual(s.list()[0]["installed_version"], "2.0.0")

    def test_failed_stage_blocks_promotion(self):
        with tempfile.TemporaryDirectory() as td:
            s = make_deps(td)
            dep = s.track("pandas", installed_version="2.0.0",
                          available_version="2.1.0")
            upg = s.stage_upgrade(dep["id"], "2.1.0")
            s.record_upgrade_stage(upg["id"], "isolated_env", "passed")
            row = s.record_upgrade_stage(upg["id"], "tests", "failed",
                                         detail="3 failures")
            self.assertEqual(row["status"], "failed")
            self.assertIsNone(
                s.conclude_upgrade(upg["id"], promote=True))
            # Version untouched — production deps never blind-upgrade.
            self.assertEqual(s.list()[0]["installed_version"], "2.0.0")


class EnvironmentTests(unittest.TestCase):
    def test_declare_and_verify_with_fake_probe(self):
        with tempfile.TemporaryDirectory() as td:
            def probe(name):
                return {"python": {"installed": True,
                                   "version": "3.11.4"},
                        "node": {"installed": True,
                                 "version": "20.1.0"},
                        "cuda": {"installed": False, "version": ""},
                        }.get(name, {"installed": False,
                                    "version": ""})
            env = make_env(td, probe)
            env.declare("proj", [
                {"name": "python", "requirement": ">=3.10"},
                {"name": "node", "requirement": "20.x"},
                {"name": "cuda", "requirement": ">=12"}])
            out = env.verify("proj")
            self.assertFalse(out["ok"])
            self.assertEqual(out["missing"], ["cuda"])
            self.assertEqual(len(out["satisfied"]), 2)
            # Reloaded store keeps the manifest.
            self.assertIsNotNone(make_env(td).manifest("proj"))

    def test_mismatch_reported(self):
        with tempfile.TemporaryDirectory() as td:
            env = make_env(td, probe=lambda n: {
                "installed": True, "version": "3.9.0"})
            env.declare("p", [{"name": "python",
                               "requirement": ">=3.10"}])
            out = env.verify("p")
            self.assertFalse(out["ok"])
            self.assertEqual(out["mismatched"][0]["installed"],
                             "3.9.0")

    def test_detect_uses_probe_and_os(self):
        with tempfile.TemporaryDirectory() as td:
            env = make_env(td, probe=lambda n: {"installed": True,
                                                "version": "1.0"})
            out = env.detect(["git", "node"])
            self.assertEqual(out["components"]["git"]["version"],
                             "1.0")
            self.assertIn("os", out)


class DepsEnvHttpTests(unittest.TestCase):
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
            process_watchdog=False, research_enabled=False)
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
            try:
                return e.code, json.loads(e.read() or b"{}")
            finally:
                e.close()

    def test_dependency_lifecycle_over_http(self):
        st, out = self._post("/api/dependencies/track", {
            "name": "rich", "installed_version": "13.0.0",
            "available_version": "14.0.0"})
        self.assertEqual(st, 200)
        dep_id = out["dependency"]["id"]
        listing = self._get("/api/dependencies?outdated=1")
        self.assertTrue(any(d["id"] == dep_id
                            for d in listing["dependencies"]))
        st, out = self._post("/api/dependencies/upgrade", {
            "dependency_id": dep_id, "candidate_version": "14.0.0"})
        upg = out["upgrade"]
        self.assertEqual(upg["status"], "pending")
        for stage in ("isolated_env", "install", "build", "tests",
                      "review"):
            st, out = self._post(
                "/api/dependencies/upgrade/stage",
                {"upgrade_id": upg["id"], "stage": stage,
                 "status": "passed"})
            self.assertEqual(st, 200)
        st, out = self._post("/api/dependencies/upgrade/conclude",
                             {"upgrade_id": upg["id"], "promote": True})
        self.assertEqual(out["upgrade"]["status"], "promoted")
        self.assertEqual(self.state.dependencies.list()
                         [0]["installed_version"], "14.0.0")

    def test_environment_manifest_over_http(self):
        st, out = self._post("/api/environment/manifest", {
            "project_id": "env-proj",
            "components": [{"name": "python", "requirement": ">=3.8"}]})
        self.assertEqual(st, 200)
        m = self._get("/api/environment/manifest/env-proj")
        self.assertEqual(m["components"][0]["name"], "python")
        v = self._get("/api/environment/verify/env-proj")
        # The real host runs Python ≥3.8 — satisfied.
        self.assertTrue(v["ok"])
        st, _ = self._post("/api/dependencies/upgrade",
                           {"dependency_id": "ghost",
                            "candidate_version": "1"})
        self.assertEqual(st, 404)
        det = self._get("/api/environment")
        self.assertIn("components", det)
        self.assertIn("os", det)
        self.assertIn("python", det["components"])


if __name__ == "__main__":
    unittest.main()
