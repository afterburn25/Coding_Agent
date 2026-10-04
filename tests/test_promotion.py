"""Phase 3 — risk classification and the gated promotion pipeline."""
import tempfile
import unittest
from pathlib import Path


class RiskTests(unittest.TestCase):
    def test_classification_table(self):
        from localcodeagent.risk import (RISK_HIGH, RISK_LOW, RISK_MEDIUM,
                                         classify_action)
        self.assertEqual(classify_action("read_file")["risk"], RISK_LOW)
        self.assertEqual(classify_action("search")["risk"], RISK_LOW)
        self.assertEqual(classify_action("code_change")["risk"],
                         RISK_MEDIUM)
        self.assertEqual(classify_action("shell_command")["risk"],
                         RISK_MEDIUM)
        for high in ("installer", "migration", "dependency_upgrade",
                     "desktop_automation", "update", "destructive"):
            self.assertEqual(classify_action(high)["risk"], RISK_HIGH,
                             high)

    def test_context_only_raises_tier(self):
        from localcodeagent.risk import (RISK_HIGH, RISK_MEDIUM,
                                         classify_action)
        self.assertEqual(
            classify_action("read_file",
                            context={"production": True})["risk"],
            RISK_HIGH)
        self.assertEqual(
            classify_action("search",
                            context={"network": True})["risk"],
            RISK_MEDIUM)
        # isolated staging downgrades an upgrade experiment…
        self.assertEqual(
            classify_action("dependency_upgrade",
                            context={"isolated": True})["risk"],
            RISK_MEDIUM)
        # …but never irreversible/secret kinds
        for pinned in ("destructive", "secret_access", "self_modify"):
            self.assertEqual(
                classify_action(pinned,
                                context={"isolated": True})["risk"],
                RISK_HIGH, pinned)

    def test_required_stages_track_risk(self):
        from localcodeagent.risk import REQUIRED_STAGES, classify_action
        self.assertEqual(classify_action("status")["required_stages"], [])
        self.assertEqual(classify_action("code_change")
                         ["required_stages"],
                         list(REQUIRED_STAGES["medium"]))
        self.assertEqual(classify_action("installer")
                         ["required_stages"],
                         list(REQUIRED_STAGES["high"]))


class PromotionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.promotion import PromotionPipeline
        self.pipe = PromotionPipeline(Path(self.td.name) / "promo.json")

    def test_low_risk_promotes_directly(self):
        c = self.pipe.create("read-only check", action="read_file")
        self.assertEqual(c["required_stages"], [])
        out = self.pipe.promote(c["id"])
        self.assertIsNotNone(out)
        self.assertEqual(out["status"], "promoted")

    def test_high_risk_requires_full_chain(self):
        c = self.pipe.create("upgrade dep", action="dependency_upgrade",
                             context={"isolated": False})
        self.assertEqual(c["risk"], "high")
        # out-of-order stage refused
        self.assertIsNone(self.pipe.record_stage(
            c["id"], "canary", ok=True))
        # missing stages → no promotion
        self.assertIsNone(self.pipe.promote(c["id"]))
        ok, missing = self.pipe.ready(c)
        self.assertFalse(ok)
        for stage in ("simulate", "targeted_tests", "regression_tests",
                      "review", "canary"):
            self.assertIsNotNone(self.pipe.record_stage(
                c["id"], stage, ok=True), stage)
        out = self.pipe.promote(c["id"], actor="self_repair")
        self.assertIsNotNone(out)
        self.assertEqual(out["status"], "promoted")
        self.assertEqual(out["promoted_by"], "self_repair")
        # terminal — no further writes
        self.assertIsNone(self.pipe.record_stage(
            c["id"], "review", ok=True))

    def test_failed_stage_blocks_promotion_and_rejects(self):
        c = self.pipe.create("config change", action="runtime_config")
        self.assertEqual(c["risk"], "high")
        self.pipe.record_stage(c["id"], "simulate", ok=True)
        self.pipe.record_stage(c["id"], "targeted_tests",
                               ok=False, detail="3 failed")
        self.assertIsNone(self.pipe.promote(c["id"]))
        out = self.pipe.reject(c["id"], reason="targeted tests failed")
        self.assertEqual(out["status"], "rejected")

    def test_extra_evidence_allowed_and_persistence(self):
        c = self.pipe.create("code edit", action="code_change")
        # canary isn't required for medium — but is allowed as evidence
        self.assertIsNotNone(self.pipe.record_stage(
            c["id"], "canary", ok=True, detail="voluntary check"))
        self.assertIsNone(self.pipe.promote(c["id"]))  # still gated
        for stage in ("simulate", "targeted_tests", "review"):
            self.pipe.record_stage(c["id"], stage, ok=True)
        self.assertIsNotNone(self.pipe.promote(c["id"]))
        from localcodeagent.promotion import PromotionPipeline
        again = PromotionPipeline(self.pipe.path)
        self.assertEqual(again.get(c["id"])["status"], "promoted")
        self.assertTrue(again.get(c["id"])["history"])

    def test_summary_and_unknown_ids(self):
        self.assertIsNone(self.pipe.record_stage("cand-x", "review",
                                                 ok=True))
        self.assertIsNone(self.pipe.promote("cand-x"))
        c = self.pipe.create("x", action="code_change")
        self.assertEqual(self.pipe.summary()["total"], 1)
        self.assertEqual(self.pipe.list(status="candidate")[0]["id"],
                         c["id"])


class PromotionHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory()
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
            return e.code, json.loads(e.read() or b"{}")

    def test_pipeline_over_http(self):
        # risk preview
        cls = self._get("/api/risk?action=dependency_upgrade&target=x")
        self.assertEqual(cls["risk"], "high")
        self.assertTrue(cls["required_stages"])
        # create a high-risk candidate; promote refused until gated
        code, out = self._post("/api/promotions", {
            "description": "installer change", "action": "installer"})
        self.assertEqual(code, 200, out)
        cid = out["candidate"]["id"]
        code, out = self._post("/api/promotions/promote", {"id": cid})
        self.assertEqual(code, 409)
        # out-of-order stage refused
        code, out = self._post("/api/promotions/stage",
                               {"id": cid, "stage": "review",
                                "ok": True})
        self.assertEqual(code, 409)
        for stage in ("simulate", "targeted_tests", "regression_tests",
                      "review", "canary"):
            code, out = self._post("/api/promotions/stage",
                                   {"id": cid, "stage": stage,
                                    "ok": True})
            self.assertEqual(code, 200, (stage, out))
        code, out = self._post("/api/promotions/promote",
                               {"id": cid, "actor": "test"})
        self.assertEqual(code, 200)
        self.assertEqual(out["candidate"]["status"], "promoted")
        data = self._get("/api/promotions")
        self.assertEqual(data["summary"]["by_status"]["promoted"], 1)


if __name__ == "__main__":
    unittest.main()
