"""IntelligenceGovernor — metacognitive assessment + cognitive scheduler."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from localcodeagent.governor import (
    CognitiveScheduler, ComputeBudget, IntelligenceGovernor, assess)
from localcodeagent.research.policy import WebResearchPolicy


def _policy(text: str):
    return WebResearchPolicy().decide(text)


class AssessTests(unittest.TestCase):
    def test_known_answer_memory(self):
        m = assess("what is a mutex", policy=_policy("what is a mutex"),
                   answer_memory_hit=True)
        self.assertEqual(m.knowledge_state, "known")
        self.assertGreaterEqual(m.confidence, 0.85)

    def test_volatile_caps_confidence(self):
        q = "what's the latest python version"
        m = assess(q, policy=_policy(q), knowledge_hit=True)
        self.assertIn("volatile/current information", m.uncertainty_sources)
        self.assertLessEqual(m.confidence, 0.35)
        self.assertEqual(m.knowledge_state, "stale")

    def test_error_recommends_hypotheses(self):
        q = "ImportError: no module named flask when I run app.py"
        m = assess(q, policy=_policy(q))
        self.assertIn("generate_hypotheses", m.recommended_ops)

    def test_high_stakes(self):
        q = "what dosage of ibuprofen is safe for a child"
        m = assess(q, policy=_policy(q))
        self.assertEqual(m.stakes, "high")
        self.assertIn("run_verifier", m.recommended_ops)

    def test_fast_think_strips_ladder(self):
        q = "what is a mutex"
        m = assess(q, policy=_policy(q), think_mode="fast")
        self.assertEqual(m.recommended_ops,
                         ["retrieve_memory", "ask_fast_model",
                          "stop_reasoning"])

    def test_conflicted_evidence(self):
        m = assess("tell me more", policy=_policy("tell me more"),
                   last_evidence={"confidence": "conflicted"})
        self.assertEqual(m.knowledge_state, "conflicted")
        self.assertIn("ask_critic", m.recommended_ops)


class SchedulerTests(unittest.TestCase):
    def _assessment(self, text, **kw):
        return assess(text, policy=_policy(text), **kw)

    def test_propose_costed_ops(self):
        sched = CognitiveScheduler(capabilities={"model", "research"})
        a = self._assessment("what's the latest python version")
        ops = sched.propose(a)
        self.assertTrue(ops)
        for op in ops:
            if op.type not in ("stop_reasoning",
                               "request_user_clarification"):
                self.assertGreater(op.estimated_cost, 0)
            self.assertIn(op.type, {
                "retrieve_memory", "search_web", "ask_fast_model",
                "ask_deep_model", "generate_hypotheses", "run_verifier",
                "ask_critic", "request_user_clarification",
                "stop_reasoning"})

    def test_capability_gate(self):
        sched = CognitiveScheduler(capabilities={"model"})
        a = self._assessment("what's the latest python version")
        ops = sched.propose(a)
        web = [o for o in ops if o.type == "search_web"]
        self.assertTrue(web)
        self.assertFalse(sched.available(web[0]))

    def test_budget_bounds(self):
        sched = CognitiveScheduler(
            ComputeBudget(max_operations=2, max_cost=1.5),
            capabilities={"model", "research"})
        a = self._assessment("what's the latest python version")
        ops = sched.propose(a)
        picked = 0
        while True:
            op = sched.next_op(ops, a)
            if op is None:
                break
            op.status = "done"
            sched.record(op)
            picked += 1
        self.assertLessEqual(picked, 2)
        self.assertLessEqual(sched.spent_cost, 1.5 + 1e-9 + 1.2)

    def test_stop_on_confidence(self):
        sched = CognitiveScheduler(capabilities={"model"})
        a = self._assessment("what is a mutex", answer_memory_hit=True)
        ops = sched.propose(a)
        self.assertIsNone(sched.next_op(ops, a))

    def test_ev_ranking_prefers_cheap_gain(self):
        sched = CognitiveScheduler(capabilities={"model", "research"})
        a = self._assessment("what's the latest python version")
        ops = sched.propose(a)
        ranked = sched.rank(ops)
        # memory check (near-free) or web search (huge gain on volatile)
        # should outrank deep model.
        self.assertIn(ranked[0].type,
                      ("retrieve_memory", "search_web"))
        self.assertLess(
            ranked[-1].expected_value(),
            ranked[0].expected_value())


class GovernorTests(unittest.TestCase):
    def test_plan_simple_question_stays_fast(self):
        g = IntelligenceGovernor(capabilities={"model", "research"})
        p = g.plan("what is a mutex",
                   policy=_policy("what is a mutex"),
                   answer_memory_hit=True)
        self.assertEqual(p.assessment.think_mode, "fast")
        self.assertLessEqual(len(p.operations), 3)

    def test_plan_research_question(self):
        g = IntelligenceGovernor(capabilities={"model", "research"})
        q = "search the web for the latest stable version of Python"
        p = g.plan(q, policy=_policy(q))
        types = [o.type for o in p.operations]
        self.assertIn("search_web", types)
        d = p.as_dict()
        self.assertIn("assessment", d)
        self.assertIn("budget", d)

    def test_think_override(self):
        g = IntelligenceGovernor(capabilities={"model"})
        p = g.plan("explain quantum entanglement",
                   policy=_policy("explain quantum entanglement"),
                   think_mode="exhaustive")
        self.assertEqual(p.assessment.think_mode, "exhaustive")
        self.assertGreaterEqual(
            p.budget["max_operations"],
            ComputeBudget.for_mode("normal").max_operations)

    def test_error_question_goes_deep(self):
        g = IntelligenceGovernor(capabilities={"model"})
        q = "ImportError: no module named flask when I run app.py"
        p = g.plan(q, policy=_policy(q))
        self.assertEqual(p.assessment.think_mode, "deep")


if __name__ == "__main__":
    unittest.main()
