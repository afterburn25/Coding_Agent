"""Requirement system — creation, lifecycle, derivation, mission sync,
and the HTTP surface."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class RequirementStoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.requirements import RequirementStore
        self.store = RequirementStore(Path(self.td.name) / "requirements.json")

    def test_create_and_lifecycle(self):
        r = self.store.create("Backend health endpoint responds",
                              source="user", scope_type="mission",
                              scope_id="m-1")
        self.assertEqual(r["status"], "not_started")
        self.assertFalse(r["inferred"])
        for st in ("planned", "in_progress", "implemented", "verified"):
            r = self.store.set_status(r["id"], st, evidence=f"passed {st}")
            self.assertEqual(r["status"], st)
            self.assertIsNotNone(r["last_checked"])
        self.assertTrue(r["evidence"])
        # reload from disk
        from localcodeagent.requirements import RequirementStore
        again = RequirementStore(self.store.path)
        self.assertEqual(again.get(r["id"])["status"], "verified")

    def test_invalid_status_rejected_and_unknown_id(self):
        r = self.store.create("x")
        self.assertIsNone(self.store.set_status(r["id"], "bogus"))
        self.assertIsNone(self.store.set_status("req-nope", "verified"))
        self.assertIsNone(self.store.add_evidence("req-nope", "k", "d"))

    def test_scope_filtering_and_evidence(self):
        self.store.create("a", scope_type="mission", scope_id="m1")
        self.store.create("b", scope_type="project", scope_id="p1")
        self.assertEqual(len(self.store.list(scope_type="mission")), 1)
        self.assertEqual(self.store.list(scope_id="p1")[0]["description"], "b")
        r = self.store.list(scope_id="p1")[0]
        self.store.add_evidence(r["id"], "test_result", "tests green",
                                ref="run-42")
        self.assertEqual(r["id"],
                         self.store.get(r["id"])["evidence"][-1]["ref"]
                         and r["id"])

    def test_inference_marking(self):
        r = self.store.create("derived thing", source="inferred")
        self.assertTrue(r["inferred"])

    def test_derive_fix_startup(self):
        from localcodeagent.requirements import derive_requirement_specs
        specs = derive_requirement_specs("Fix startup.")
        descs = [s["description"].lower() for s in specs]
        self.assertTrue(any("health" in d for d in descs))
        self.assertTrue(any("timeout" in d for d in descs))
        self.assertTrue(any("stale" in d for d in descs))
        self.assertTrue(any("restart" in d for d in descs))
        self.assertTrue(all(s["source"] == "inferred" for s in specs))

    def test_derive_generic_and_make_work(self):
        from localcodeagent.requirements import (derive_requirement_specs,
                                                 is_repair_intent)
        self.assertTrue(is_repair_intent("make this work"))
        self.assertTrue(is_repair_intent("get the build running"))
        self.assertFalse(is_repair_intent("what time is it"))
        specs = derive_requirement_specs("add a dark mode toggle")
        self.assertTrue(any(s["verification"]["kind"] == "verify_passed"
                            for s in specs))
        self.assertTrue(any(s["verification"]["kind"] == "no_failures"
                            for s in specs))

    def test_attach_mission_fills_criteria(self):
        m = {"id": "m-9", "objective": "fix the installer update",
             "success_criteria": []}
        reqs = self.store.attach_mission(m)
        self.assertTrue(reqs)
        self.assertEqual(m["requirement_ids"], [r["id"] for r in reqs])
        kinds = {c["kind"] for c in m["success_criteria"]}
        self.assertIn("verify_passed", kinds)
        # criteria link back to their requirement for status sync
        self.assertTrue(all(c.get("requirement_id")
                            for c in m["success_criteria"]))
        # no second derive on re-attach
        self.assertEqual(self.store.attach_mission(m), [])

    def test_sync_mission_updates_status(self):
        m = {"id": "m-7", "objective": "fix startup",
             "success_criteria": []}
        reqs = self.store.attach_mission(m)
        rid = reqs[0]["id"]
        self.store.sync_mission(
            {"id": "m-7", "requirement_ids": [rid],
             "graph": {"nodes": []},
             "criteria_results": []},
            criteria_results=[{"kind": "custom", "met": True,
                               "detail": "health ok", "requirement_id": rid}])
        self.assertEqual(self.store.get(rid)["status"], "verified")
        self.assertTrue(self.store.get(rid)["evidence"])
        # regression → failed
        self.store.sync_mission(
            {"id": "m-7", "requirement_ids": [rid]},
            criteria_results=[{"kind": "custom", "met": False,
                               "detail": "still hangs", "requirement_id": rid}])
        self.assertEqual(self.store.get(rid)["status"], "failed")
        # running work → in_progress for untouched rows
        m2 = {"id": "m-8", "objective": "x", "success_criteria": []}
        reqs2 = self.store.attach_mission(m2)
        self.store.sync_mission(
            {"id": "m-8", "requirement_ids": [r["id"] for r in reqs2],
             "graph": {"nodes": [{"state": "running"}]},
             "criteria_results": []})
        self.assertEqual(self.store.get(reqs2[0]["id"])["status"],
                         "in_progress")


class RequirementHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        from tests.test_end_to_end import _FakeModelServer
        cls._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._llm = _FakeModelServer()
        ws = Path(cls._td.name)
        cfg = AgentConfig(
            profiles_onboarding_gate=False,
            models=[ModelProfile(id="fake", endpoint=cls._llm.endpoint,
                                 model="fake-model",
                                 roles=["primary_coder", "utility",
                                        "fast_coder"],
                                 runtime="external")],
            process_watchdog=False, research_enabled=False,
            autonomy_enabled=True)
        cls.server, cls.state = create_server(
            cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        # Registered first so it runs LAST (class cleanups are LIFO):
        # the server must be fully stopped before the temp dir is removed,
        # otherwise in-flight writes race rmtree -> "Directory not empty".
        cls.addClassCleanup(cls._td.cleanup)
        cls.addClassCleanup(lambda: (cls.server.shutdown(),
                                     cls.server.server_close(),
                                     cls._llm.close(),
                                     stop_state(cls.state)))

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return json.loads(r.read())

    def _post(self, path, body):
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

    def test_crud_over_http(self):
        code, out = self._post("/api/requirements", {
            "description": "Backend health endpoint responds",
            "scope_type": "project", "scope_id": "p-http"})
        self.assertEqual(code, 200, out)
        rid = out["requirement"]["id"]
        code, out = self._post("/api/requirements/evidence",
                               {"id": rid, "kind": "test_result",
                                "detail": "smoke green"})
        self.assertEqual(code, 200)
        code, out = self._post("/api/requirements/status",
                               {"id": rid, "status": "verified",
                                "evidence": "observed in smoke"})
        self.assertEqual(code, 200)
        data = self._get("/api/requirements?scope_id=p-http")
        self.assertEqual(data["requirements"][0]["status"], "verified")
        code, _ = self._post("/api/requirements/status",
                             {"id": rid, "status": "nonsense"})
        self.assertEqual(code, 404)

    def test_mission_creation_derives_requirements(self):
        code, out = self._post("/api/missions", {
            "objective": "Fix installer update startup",
            "scope": "one_shot", "start": False})
        self.assertEqual(code, 200, out)
        m = out["mission"]
        self.assertTrue(m.get("requirement_ids"))
        self.assertTrue(m.get("success_criteria"))
        data = self._get("/api/requirements?scope_id=" + m["id"])
        descs = [r["description"].lower() for r in data["requirements"]]
        self.assertTrue(any("install" in d for d in descs))
        self.assertTrue(all(r["inferred"] for r in data["requirements"]))

    def test_chat_fix_request_derives_acceptance(self):
        code, out = self._post("/api/chat", {
            "message": "fix the startup — it hangs on boot",
            "mode": "auto"})
        self.assertEqual(code, 200, out)
        self.assertTrue(out.get("acceptance_criteria"))
        reqs = out.get("requirements") or []
        self.assertTrue(reqs)
        self.assertTrue(all(r["inferred"] for r in reqs))
        descs = [r["description"].lower() for r in reqs]
        self.assertTrue(any("startup" in d or "stale" in d
                            for d in descs))


if __name__ == "__main__":
    unittest.main()
