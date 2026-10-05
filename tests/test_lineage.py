"""Phase 8 — data lineage + artifact versioning hardening."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.artifacts import ArtifactManager
from localcodeagent.lineage import LineageStore


def make(tmp: str) -> LineageStore:
    return LineageStore(Path(tmp) / "lineage.json")


class LineageStoreTests(unittest.TestCase):
    def test_record_and_query(self):
        with tempfile.TemporaryDirectory() as td:
            s = make(td)
            row = s.record("art-1", target_kind="artifact",
                           contributors=[
                               {"kind": "file", "ref": "src/main.py"},
                               {"kind": "web", "ref": "docs page"},
                               {"kind": "bogus", "ref": "x"}],
                           project_id="p1", mission_id="m1")
            self.assertEqual(row["target_kind"], "artifact")
            self.assertEqual(len(row["contributors"]), 3)
            # Unknown contributor kinds are normalized, not rejected.
            self.assertEqual(row["contributors"][2]["kind"], "other")
            hits = s.for_target("art-1")
            self.assertEqual(len(hits), 1)
            rows = s.list(project_id="p1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(s.list(project_id="zzz"), [])

    def test_persistence_and_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            s = make(td)
            for i in range(1100):
                s.record(f"t-{i}")
            s2 = make(td)
            self.assertEqual(len(s2.list(limit=2000)), 1000)
            self.assertEqual(s2.list(limit=2000)[0]["target_id"],
                             "t-100")
            self.assertIn("records", s2.summary())


class ArtifactVersionTests(unittest.TestCase):
    def _artifact(self, td: str, name: str,
                  content: str = "v1", **kw) -> dict:
        src = Path(td) / name
        src.write_text(content)
        mgr = ArtifactManager(Path(td) / "art")
        return mgr, mgr.register(src, **kw)

    def test_versions_and_latest(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = ArtifactManager(Path(td) / "art")
            for i in range(3):
                f = Path(td) / "report.md"
                f.write_text(f"v{i+1}")
                mgr.register(f)
            vs = mgr.versions("report.md")
            self.assertEqual([v["version"] for v in vs], [1, 2, 3])
            self.assertEqual(mgr.latest("report.md")["version"], 3)
            self.assertEqual(mgr.versions("nope.txt"), [])
            self.assertIsNone(mgr.latest("nope.txt"))

    def test_project_and_requirement_linkage(self):
        with tempfile.TemporaryDirectory() as td:
            mgr, row = self._artifact(
                td, "patch.diff", project_id="proj-9",
                requirement_ids=["req-1", "req-2"])
            self.assertEqual(row["project_id"], "proj-9")
            self.assertEqual(row["requirement_ids"], ["req-1", "req-2"])

    def test_register_emits_lineage(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = ArtifactManager(Path(td) / "art")
            lin = make(td)
            mgr.lineage = lin
            src = Path(td) / "out.txt"
            src.write_text("hi")
            row = mgr.register(src, tool="writer", creator="worker-1",
                               project_id="p1", mission_id="m1",
                               requirement_ids=["req-9"],
                               provenance={"inputs": ["a.py", "b.py"]})
            recs = lin.for_target(row["id"])
            self.assertEqual(len(recs), 1)
            kinds = {c["kind"] for c in recs[0]["contributors"]}
            self.assertTrue({"tool_output", "worker", "requirement",
                             "file"} <= kinds)
            self.assertEqual(recs[0]["project_id"], "p1")

    def test_verify_detects_silent_modification(self):
        with tempfile.TemporaryDirectory() as td:
            mgr, row = self._artifact(td, "data.csv", "a,b")
            self.assertTrue(mgr.verify(row["id"])["ok"])
            Path(row["path"]).write_text("tampered")
            out = mgr.verify(row["id"])
            self.assertFalse(out["ok"])
            self.assertIn("hash", out["reason"])


class LineageHttpTests(unittest.TestCase):
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
        # A per-operation sqlite connection can still be closing when the
        # server stops — retry briefly so Windows file-lock release wins.
        import time
        for _ in range(10):
            try:
                cls._td.cleanup()
                return
            except PermissionError:
                time.sleep(0.2)
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

    def test_record_and_read_over_http(self):
        st, out = self._post("/api/lineage", {
            "target_id": "ans-1", "target_kind": "answer",
            "contributors": [{"kind": "memory", "ref": "note-3"},
                             {"kind": "file", "ref": "src/x.py"}],
            "project_id": "http-p"})
        self.assertEqual(st, 200)
        self.assertTrue(out["ok"])
        got = self._get("/api/lineage/ans-1")
        self.assertEqual(len(got["records"]), 1)
        listing = self._get("/api/lineage?project=http-p")
        self.assertEqual(len(listing["records"]), 1)
        self.assertIn("memory", listing["summary"]["contributor_kinds"])

    def test_versions_endpoint(self):
        # Register two versions of the same file directly, then read
        # the history over HTTP.
        ws = self.state.workspace
        for i in range(2):
            f = ws / "deliverable.txt"
            f.write_text(f"rev{i+1}")
            self.state.artifacts.register(f)
        out = self._get("/api/artifacts/versions/deliverable.txt")
        self.assertEqual(len(out["versions"]), 2)
        self.assertEqual(out["latest"]["version"], 2)
        # Artifact registration auto-recorded lineage for each version.
        lin = self._get("/api/lineage/" + out["latest"]["id"])
        self.assertEqual(len(lin["records"]), 1)


if __name__ == "__main__":
    unittest.main()
