"""Phase 2 — hypothesis lifecycle, causal memory, decision journal, and
their wiring into the self-repair pipeline."""
import tempfile
import unittest
from pathlib import Path


class HypothesisStoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.hypotheses import HypothesisStore
        self.store = HypothesisStore(Path(self.td.name) / "hyp.json")

    def test_lifecycle_and_persistence(self):
        h = self.store.propose("Port held by stale process",
                               kind="stale_process", confidence=0.7,
                               incident_id="ri-1",
                               test={"name": "check port binding"})
        self.assertEqual(h["status"], "proposed")
        self.store.set_status(h["id"], "testing")
        self.store.record_test(h["id"], passed=True, output="port busy")
        row = self.store.get(h["id"])
        self.assertEqual(row["status"], "supported")
        self.assertTrue(row["test_result"]["passed"])
        self.store.confirm(h["id"], evidence="incident resolved")
        self.assertEqual(self.store.get(h["id"])["status"], "confirmed")
        # reload
        from localcodeagent.hypotheses import HypothesisStore
        again = HypothesisStore(self.store.path)
        self.assertEqual(again.get(h["id"])["status"], "confirmed")
        self.assertTrue(again.get(h["id"])["history"])

    def test_evidence_moves_confidence_and_status(self):
        h = self.store.propose("x", kind="k", confidence=0.5,
                               incident_id="ri-2")
        self.store.add_evidence(h["id"], supporting=True,
                                detail="matches signature")
        self.assertGreater(self.store.get(h["id"])["confidence"], 0.5)
        h2 = self.store.propose("y", kind="k2", confidence=0.5,
                                incident_id="ri-2")
        self.store.add_evidence(h2["id"], supporting=False,
                                detail="ruled out by log")
        row = self.store.get(h2["id"])
        self.assertEqual(row["status"], "weakened")
        self.assertLess(row["confidence"], 0.5)
        # invalid states/ids rejected
        self.assertIsNone(self.store.set_status(h["id"], "maybe"))
        self.assertIsNone(self.store.add_evidence("hyp-nope",
                                                  supporting=True,
                                                  detail="x"))

    def test_failed_test_weakens(self):
        h = self.store.propose("z", kind="k", confidence=0.6,
                               incident_id="ri-3")
        self.store.record_test(h["id"], passed=False,
                               output="port was free")
        row = self.store.get(h["id"])
        self.assertEqual(row["status"], "weakened")
        self.assertFalse(row["test_result"]["passed"])

    def test_upsert_dedups_and_discrimination_picks(self):
        hyps = [{"kind": "a", "detail": "A", "confidence": 0.6},
                {"kind": "b", "detail": "B", "confidence": 0.5}]
        first = self.store.upsert_for_incident("ri-9", hyps)
        second = self.store.upsert_for_incident("ri-9", hyps)
        self.assertEqual([r["id"] for r in first],
                         [r["id"] for r in second])
        # discriminating test that rules out 'b' wins over generic
        self.store._mutate(first[0]["id"], lambda h: h["test"].update(
            {"name": "port check", "distinguishes": ["b"]}))
        pick = self.store.pick_discriminating("ri-9")
        self.assertEqual(pick["id"], first[0]["id"])

    def test_list_filters(self):
        self.store.propose("a", incident_id="i1")
        self.store.propose("b", incident_id="i2", mission_id="m1")
        self.assertEqual(len(self.store.list(incident_id="i1")), 1)
        self.assertEqual(len(self.store.list(mission_id="m1")), 1)
        self.assertEqual(self.store.summary()["total"], 2)


class CausalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.causal import CausalMemory
        self.mem = CausalMemory(Path(self.td.name) / "causal.json")

    def test_record_and_priors(self):
        self.mem.record(
            "backend health endpoint timeout",
            root_cause="runtime_lock",
            mechanism="status endpoint blocked on model-load lock",
            fix=["release lock during spawn"],
            verification="status responsive during load",
            subsystem="backend", incident_id="ri-1")
        priors = self.mem.priors("health endpoint timeout backend",
                                 subsystem="backend")
        self.assertTrue(priors)
        self.assertEqual(priors[0]["kind"], "runtime_lock")
        self.assertEqual(priors[0]["source"], "causal_memory")
        self.assertGreaterEqual(priors[0]["confidence"], 0.3)
        # unrelated symptoms don't surface priors
        self.assertEqual(self.mem.priors("totally different domain "
                                         "unrelated symptom"), [])

    def test_failed_records_rank_lower_and_persist(self):
        self.mem.record("symptom x y", root_cause="bad_cause",
                        mechanism="m", success=False)
        self.mem.record("symptom x y", root_cause="good_cause",
                        mechanism="m2", success=True)
        priors = self.mem.priors("symptom x y")
        self.assertEqual(priors[0]["kind"], "good_cause")
        from localcodeagent.causal import CausalMemory
        again = CausalMemory(self.mem.path)
        self.assertEqual(len(again.list()), 2)


class DecisionJournalTests(unittest.TestCase):
    def test_record_outcome_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            from localcodeagent.decisions import DecisionJournal
            j = DecisionJournal(Path(td) / "dec.json")
            d = j.record("backend hung",
                         alternatives=["port collision", "lock"],
                         evidence=["health timeout"],
                         decision="test port first",
                         expected_outcome="cheap discriminating test",
                         actor="self_repair",
                         context={"incident_id": "ri-1"})
            self.assertEqual(d["status"], "open")
            out = j.outcome(d["id"], "port was free; lock was cause",
                            reviewer_result="correct prioritization",
                            lessons="check lock waiters early")
            self.assertEqual(out["status"], "closed")
            self.assertEqual(out["lessons"], "check lock waiters early")
            self.assertIsNone(j.outcome("dec-nope", "x"))
            again = DecisionJournal(j.path)
            self.assertEqual(again.get(d["id"])["status"], "closed")
            self.assertEqual(again.summary()["closed"], 1)


class CoordinatorIntegrationTests(unittest.TestCase):
    """The repair pipeline now persists hypotheses, journals the repair
    decision, and writes causal memory on resolution."""

    def test_pipeline_records_hypotheses_decision_causal(self):
        from tests.test_self_repair import (make_repo, make_coord,
                                            report_bug)
        from localcodeagent.self_repair.canary import Canary
        from localcodeagent.hypotheses import HypothesisStore
        from localcodeagent.causal import CausalMemory
        from localcodeagent.decisions import DecisionJournal
        from tests.test_self_repair import good_generator
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            root = Path(td)
            hyps = HypothesisStore(root / "hyp.json")
            causal = CausalMemory(root / "causal.json")
            decisions = DecisionJournal(root / "dec.json")
            coord = make_coord(
                td, repo, patch_generator=good_generator,
                canary=Canary(lambda i, w, **k: {"ok": True, "port": 1}),
                hypotheses=hyps, causal=causal, decisions=decisions)
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"])
            final = coord.get(inc["id"])
            self.assertEqual(final["state"], "resolved")
            # hypotheses persisted with lifecycle outcome
            rows = hyps.list(incident_id=inc["id"])
            self.assertTrue(rows)
            self.assertTrue(any(r["status"] == "confirmed"
                                for r in rows))
            self.assertTrue(any(r["status"] == "rejected"
                                for r in rows)
                            or len(rows) == 1)
            # decision journaled with alternatives
            decs = decisions.list(actor="self_repair")
            self.assertTrue(decs)
            self.assertTrue(decs[0]["alternatives"])
            # causal memory captured the mechanism, not just the symptom
            recs = causal.list()
            self.assertTrue(recs)
            self.assertEqual(recs[0]["root_cause"], "source_bug")
            self.assertTrue(recs[0]["mechanism"])

    def test_causal_priors_seed_next_diagnosis(self):
        from tests.test_self_repair import (make_repo, make_coord,
                                            report_bug, good_generator)
        from localcodeagent.self_repair.canary import Canary
        from localcodeagent.causal import CausalMemory
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            root = Path(td)
            causal = CausalMemory(root / "causal.json")
            coord = make_coord(
                td, repo, patch_generator=good_generator,
                canary=Canary(lambda i, w, **k: {"ok": True, "port": 1}),
                causal=causal)
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"])
            self.assertEqual(coord.get(inc["id"])["state"], "resolved")
            self.assertTrue(causal.list())
            # a second, similar incident gets a causal-memory prior
            trace = ('Traceback (most recent call last):\n'
                     f'  File "{str(repo).replace(chr(92), "/")}/mod/calc.py", '
                     'line 2, in divide\n'
                     '    return a / b\n'
                     'ZeroDivisionError: division by zero\n')
            inc2, disp = coord.report_failure(
                source="test", subsystem="code",
                exc_type="ZeroDivisionError",
                error_message="ZeroDivisionError again in mod.calc",
                stack_trace=trace)
            if disp != "new":
                self.skipTest("dedup folded the repeat incident")
            coord.process_incident(inc2["id"])
            final2 = coord.get(inc2["id"])
            kinds = [h.get("source") for h in
                     (final2.get("hypotheses") or [])]
            self.assertIn("causal_memory", kinds)


class CognitiveHttpTests(unittest.TestCase):
    """Phase-2 API surface over a live server."""

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
            return e.code, json.loads(e.read() or b"{}")

    def test_hypothesis_routes(self):
        code, out = self._post("/api/hypotheses", {
            "statement": "port collision", "kind": "port_collision",
            "confidence": 0.7, "incident_id": "ri-http",
            "test": {"name": "bind check"}})
        self.assertEqual(code, 200, out)
        hid = out["hypothesis"]["id"]
        code, out = self._post("/api/hypotheses/test",
                               {"id": hid, "passed": True,
                                "output": "bind failed"})
        self.assertEqual(code, 200)
        self.assertEqual(out["hypothesis"]["status"], "supported")
        code, out = self._post("/api/hypotheses/status",
                               {"id": hid, "action": "confirm",
                                "evidence": "resolved"})
        self.assertEqual(code, 200)
        data = self._get("/api/hypotheses?incident_id=ri-http")
        self.assertEqual(data["hypotheses"][0]["status"], "confirmed")
        code, _ = self._post("/api/hypotheses",
                             {"statement": ""})
        self.assertEqual(code, 400)

    def test_decision_and_causal_routes(self):
        code, out = self._post("/api/decisions", {
            "problem": "which backend", "alternatives": ["a", "b"],
            "decision": "a", "expected_outcome": "fast"})
        self.assertEqual(code, 200, out)
        did = out["decision"]["id"]
        code, out = self._post("/api/decisions/outcome",
                               {"id": did, "actual": "a worked",
                                "lessons": "keep it"})
        self.assertEqual(code, 200)
        data = self._get("/api/decisions?status=closed")
        self.assertEqual(data["decisions"][0]["id"], did)
        data = self._get("/api/causal-memory")
        self.assertIn("records", data)
        self.assertIn("summary", data)


if __name__ == "__main__":
    unittest.main()
