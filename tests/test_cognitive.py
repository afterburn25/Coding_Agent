"""Cognitive Phase 1 — Reasoning Strategy Router, Requirements
Compiler, Assumption Ledger."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.governor.metacognition import (
    assess, classify_strategy, STRATEGY_OPS)
from localcodeagent.governor.governor import IntelligenceGovernor
from localcodeagent.assumptions import AssumptionLedger
from localcodeagent.requirements import (
    RequirementStore, classify_clause, compile_requirement_spec,
    compile_to_store)


class _Policy:
    level = ""
    is_current = False
    is_error = False
    has_url = False
    is_coding = False


def _pol(**kw):
    p = _Policy()
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class TestStrategyRouter(unittest.TestCase):
    def test_math_routes_formal(self):
        s, _, conf, _ = classify_strategy("What's 14 × 37?")
        self.assertEqual(s, "formal_math")
        self.assertGreaterEqual(conf, 0.8)

    def test_crash_routes_diagnostic_causal(self):
        s, secs, _, _ = classify_strategy(
            "Why does InvokeAI keep crashing during generation?",
            is_error=True)
        self.assertEqual(s, "diagnostic")
        self.assertIn("causal", secs)

    def test_comparison_routes_comparative_decision(self):
        s, secs, _, _ = classify_strategy(
            "Should we keep SQLite or move this subsystem to Postgres?")
        self.assertIn(s, ("comparative", "decision_analysis"))
        other = {"comparative", "decision_analysis"} - {s}
        self.assertTrue(other & set(secs) or not other)

    def test_constraints_route_optimization(self):
        s, secs, _, _ = classify_strategy(
            "How can we schedule four models within 12 GB VRAM?")
        self.assertIn(s, ("optimization", "constraint_solving"))
        self.assertIn("constraint_solving", [s] + secs)

    def test_counterfactual(self):
        s, _, _, _ = classify_strategy(
            "What happens if we load RealVisXL without unloading Qwen?")
        self.assertEqual(s, "counterfactual")

    def test_simple_question_stays_direct(self):
        s, secs, conf, _ = classify_strategy("What time is it?")
        self.assertEqual(s, "direct_retrieval")
        self.assertEqual(secs, [])
        self.assertGreaterEqual(conf, 0.7)

    def test_assess_carries_strategy(self):
        m = assess("What's 14 × 37?", policy=_pol())
        self.assertEqual(m.strategy, "formal_math")
        self.assertIn("run_formal_solver", m.recommended_ops)

    def test_diagnostic_assess_adds_hypothesis_ops(self):
        m = assess("the backend keeps crashing on boot",
                   policy=_pol(is_error=True))
        self.assertEqual(m.strategy, "diagnostic")
        self.assertIn("generate_hypotheses", m.recommended_ops)
        self.assertIn("test_hypothesis", m.recommended_ops)

    def test_plan_surfaces_strategy(self):
        gov = IntelligenceGovernor(capabilities={"model", "solvers"})
        plan = gov.plan("What's 14 × 37?")
        self.assertEqual(plan.assessment.strategy, "formal_math")
        op_types = {o.type for o in plan.operations}
        self.assertIn("run_formal_solver", op_types)

    def test_fast_mode_keeps_plan_cheap(self):
        m = assess("why is the sky blue", policy=_pol(),
                   think_mode="fast")
        self.assertLessEqual(len(m.recommended_ops), 5)

    def test_all_strategy_ops_are_known_ops(self):
        from localcodeagent.governor.scheduler import OP_PROFILES
        for strat, ops in STRATEGY_OPS.items():
            for op in ops:
                self.assertIn(op, OP_PROFILES,
                              f"{strat} maps to unknown op {op}")


class TestFormalMathFastPath(unittest.TestCase):
    """formal_math strategy ↔ the existing thalamus fast path — the
    strategy labels the problem class; the brain computes exactly."""

    def _thalamus(self):
        from localcodeagent.brain.bus import CorpusCallosum as Bus
        from localcodeagent.brain.thalamus import Thalamus
        return Thalamus(Bus())

    def test_math_asks_route_and_compute(self):
        th = self._thalamus()
        s, _, _, _ = classify_strategy("What's 14 × 37?")
        self.assertEqual(s, "formal_math")
        self.assertIn("14", th.answer_fast_path("math", "14 * 37"))

    def test_prose_numbers_never_compute(self):
        th = self._thalamus()
        # Numbers inside prose extract to '' — never computed.
        self.assertEqual(th._math_expr(
            "the server has 14 cores, why does it crash?"), "")
        self.assertEqual(th._math_expr("explain the x factor"), "")

    def test_unsafe_expressions_rejected(self):
        th = self._thalamus()
        self.assertEqual(th._answer_math("__import__('os')"), "")
        self.assertEqual(th._answer_math("2**999999999"), "")


class TestRequirementsCompiler(unittest.TestCase):
    def test_modal_categories(self):
        spec = compile_requirement_spec(
            "Make RealVis the default for adult images. "
            "Don't change ordinary image routing. "
            "Prefer keeping InvokeAI as the backend. "
            "Optionally add a config toggle. "
            "Assume the user selected Auto backend.")
        self.assertTrue(any("RealVis" in c for c in spec["MUST"]))
        self.assertTrue(any("ordinary" in c for c in spec["MUST_NOT"]))
        self.assertTrue(any("InvokeAI" in c for c in spec["SHOULD"]))
        self.assertTrue(any("toggle" in c for c in spec["MAY"]))
        self.assertTrue(any("Auto" in c for c in spec["ASSUMPTION"]))

    def test_must_not_beats_imperative(self):
        self.assertEqual(
            classify_clause("don't change the normal routing"),
            "MUST_NOT")
        self.assertEqual(
            classify_clause("never delete user state"), "MUST_NOT")

    def test_question_category(self):
        self.assertEqual(classify_clause("which backend is active?"),
                         "QUESTION")

    def test_imperative_defaults_must(self):
        self.assertEqual(classify_clause("keep the user informed"),
                         "MUST")

    def test_compile_to_store_persists_and_separates(self):
        with tempfile.TemporaryDirectory() as td:
            store = RequirementStore(Path(td) / "requirements.json")
            out = compile_to_store(
                store,
                "Make RealVis the adult default. "
                "Don't change ordinary routing. "
                "Which model tag should adult use?",
                scope_type="mission", scope_id="m-1")
            descs = [r["description"] for r in out["requirements"]]
            self.assertTrue(any("adult default" in d for d in descs))
            self.assertTrue(any(d.startswith("[MUST_NOT]")
                                for d in descs))
            self.assertEqual(len(out["unresolved"]["questions"]), 1)
            # persisted + scoped
            rows = store.list(scope_type="mission", scope_id="m-1")
            self.assertGreaterEqual(len(rows), 2)
            # MUST_NOT carried high priority
            mn = [r for r in rows if "[MUST_NOT]" in r["description"]]
            self.assertEqual(mn[0]["priority"], "high")

    def test_compile_feeds_assumption_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            store = RequirementStore(Path(td) / "requirements.json")
            ledger = AssumptionLedger(Path(td) / "assumptions.json")
            out = compile_to_store(
                store,
                "Make RealVis the adult default. "
                "Assume the selected backend is Auto.",
                scope_type="mission", scope_id="m-3",
                assumptions=ledger)
            self.assertEqual(len(out["assumption_rows"]), 1)
            row = out["assumption_rows"][0]
            self.assertEqual(row["state"], "untested")
            # dependents point back at the created requirement rows
            dep_ids = {r["id"] for r in out["requirements"]}
            self.assertEqual(
                set(row["dependents"]["requirements"]), dep_ids)
            out2 = ledger.invalidate(row["id"], evidence="backend is CPU")
            self.assertEqual(set(out2["dependents"]["requirements"]),
                             dep_ids)


class TestAssumptionLedger(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ledger = AssumptionLedger(
            Path(self.td.name) / "assumptions.json")

    def tearDown(self):
        self.td.cleanup()

    def test_lifecycle(self):
        a = self.ledger.add(
            "InvokeAI supports this checkpoint format",
            scope_type="mission", scope_id="m-1",
            dependents={"tasks": ["t-1"], "decisions": ["dec-2"]})
        self.assertEqual(a["state"], "untested")
        self.ledger.set_state(a["id"], "supported",
                              evidence="schema probe matched")
        row = self.ledger.get(a["id"])
        self.assertEqual(row["state"], "supported")
        self.assertGreater(row["confidence"], a["confidence"])
        self.ledger.set_state(a["id"], "verified",
                              evidence="load probe OK")
        self.assertGreaterEqual(
            self.ledger.get(a["id"])["confidence"], 0.9)

    def test_invalidation_reports_dependents(self):
        a = self.ledger.add(
            "10 GB free VRAM is sufficient",
            dependents={"tasks": ["t-9", "t-10"],
                        "decisions": ["dec-3"]})
        out = self.ledger.invalidate(a["id"], evidence="OOM at load")
        self.assertEqual(out["invalidated"]["state"], "invalidated")
        self.assertEqual(set(out["dependents"]["tasks"]),
                         {"t-9", "t-10"})
        self.assertEqual(out["dependents"]["decisions"], ["dec-3"])

    def test_supersede_links(self):
        a = self.ledger.add("API stays compatible with v5")
        b = self.ledger.supersede(a["id"], "API compatible with v6")
        self.assertIsNotNone(b)
        old = self.ledger.get(a["id"])
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["superseded_by"], b["id"])

    def test_weakest_probe(self):
        weak = self.ledger.add("unverified dep", confidence=0.4)
        strong = self.ledger.add("verified dep", confidence=0.5)
        self.ledger.set_state(strong["id"], "verified",
                              evidence="checked")
        self.ledger.set_state(weak["id"], "supported")
        weakest = self.ledger.weakest()
        self.assertEqual(weakest[0]["id"], weak["id"])
        self.assertNotIn(strong["id"], [w["id"] for w in weakest])

    def test_link_and_scope(self):
        a = self.ledger.add("endpoint remains stable",
                            scope_type="mission", scope_id="m-2")
        self.ledger.link(a["id"], "procedures", "proc-1")
        row = self.ledger.get(a["id"])
        self.assertIn("proc-1", row["dependents"]["procedures"])
        scoped = self.ledger.list(scope_type="mission", scope_id="m-2")
        self.assertEqual(len(scoped), 1)

    def test_persistence_reopen(self):
        a = self.ledger.add("persists across restart")
        again = AssumptionLedger(Path(self.td.name) / "assumptions.json")
        self.assertEqual(again.get(a["id"])["text"],
                         "persists across restart")


if __name__ == "__main__":
    unittest.main()
