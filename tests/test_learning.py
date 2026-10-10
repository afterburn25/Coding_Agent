"""Tests for the continual-learning subsystem (L1–L12)."""

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.learning import (
    taxonomy as t, LearningGovernor, KnowledgePromotionPolicy,
    CompetencyMap, ProceduralMemory, StrategyEvaluator,
    LessonStore, LessonExtractor, FreshnessPolicy,
    ConsolidationEngine, ConsolidationBudget,
    ActiveLearningPlanner, CurriculumManager, StudySessionStore,
    MasteryEvaluator,
)


class TaxonomyTests(unittest.TestCase):
    def test_memory_classes_and_states(self):
        for cls in ("semantic", "episodic", "procedural", "evidence",
                    "preference", "working", "training"):
            self.assertIn(cls, t.MEMORY_CLASSES)
        for st in ("raw", "candidate", "supported", "verified", "trusted",
                   "conflicted", "superseded", "expired"):
            self.assertIn(st, t.PROMOTION_STATES)

    def test_verification_ranking(self):
        self.assertLess(t.verification_rank("model_asserted"),
                        t.verification_rank("tested"))
        self.assertLess(t.verification_rank("single_source"),
                        t.verification_rank("authoritative"))


class PromotionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = KnowledgePromotionPolicy()

    def test_unverified_model_output_stays_raw(self):
        d = self.policy.decide(source_type=t.SOURCE_MODEL,
                               verification=t.VERIFY_MODEL_ASSERTED,
                               confidence=0.99)
        self.assertEqual(d.state, t.RAW)
        self.assertFalse(d.training_eligible)

    def test_tested_claim_verifies(self):
        d = self.policy.decide(source_type=t.SOURCE_TEST,
                               verification=t.VERIFY_TESTED,
                               confidence=0.8)
        self.assertEqual(d.state, t.VERIFIED)
        self.assertTrue(d.training_eligible)

    def test_authoritative_source_verifies(self):
        d = self.policy.decide(source_type=t.SOURCE_OFFICIAL,
                               source_trust="official",
                               verification=t.VERIFY_AUTHORITATIVE,
                               confidence=0.7)
        self.assertEqual(d.state, t.VERIFIED)

    def test_repeated_verification_trusts(self):
        d = self.policy.decide(source_type=t.SOURCE_TEST,
                               verification=t.VERIFY_TESTED,
                               confidence=0.9, confirmations=3)
        self.assertEqual(d.state, t.TRUSTED)

    def test_conflict_blocks_promotion(self):
        d = self.policy.decide(source_type=t.SOURCE_TEST,
                               verification=t.VERIFY_TESTED,
                               confidence=0.9, conflict=True)
        self.assertEqual(d.state, t.CONFLICTED)
        self.assertFalse(d.training_eligible)

    def test_community_source_is_supported_not_verified(self):
        d = self.policy.decide(source_type=t.SOURCE_COMMUNITY,
                               source_trust="community",
                               verification=t.VERIFY_SINGLE_SOURCE,
                               corroboration=1, confidence=0.5)
        self.assertIn(d.state, (t.CANDIDATE, t.SUPPORTED))
        self.assertFalse(d.training_eligible)


class FreshnessTests(unittest.TestCase):
    def test_classification(self):
        f = FreshnessPolicy()
        self.assertEqual(f.classify("latest python version"), "fast")
        self.assertEqual(f.classify("price of gold today"), "volatile")
        self.assertEqual(f.classify("sorting algorithm"), "slow")

    def test_stale_marking(self):
        f = FreshnessPolicy()
        self.assertTrue(f.is_stale({"expires_at": 1.0}, now=2.0))
        self.assertFalse(f.is_stale({"expires_at": 5.0}, now=2.0))
        rec = f.mark_expired({"id": "x", "expires_at": 9.0})
        self.assertEqual(rec["promotion"], t.EXPIRED)
        self.assertEqual(rec["id"], "x")  # record kept, marked


class LessonTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.store = LessonStore(Path(self.td.name) / "lessons.json")
        self.extractor = LessonExtractor()

    def test_extract_success_lesson(self):
        task = {"id": "t1", "goal": "fix the crash in parser",
                "status": "completed", "model": "qwen-14b",
                "research": {"session": {"sources": [
                    {"url": "https://docs.python.org/x"}]}}}
        les = self.extractor.extract(task)
        self.assertEqual(les["outcome"], "success")
        self.assertEqual(les["problem_class"], "debugging")
        self.assertIn("model:qwen-14b", les["strategies_used"])
        self.assertIn("task:t1", les["evidence_refs"])
        self.assertNotIn("transcript", les)
        self.assertNotIn("messages", les)  # no raw conversation stored

    def test_extract_failure_lesson(self):
        task = {"id": "t2", "goal": "install the driver",
                "status": "error", "model": "m1"}
        les = self.extractor.extract(task)
        self.assertEqual(les["outcome"], "failure")
        self.assertTrue(les["failed_strategies"])
        self.assertTrue(les["regression_candidate"])

    def test_correction_lesson(self):
        task = {"id": "t3", "goal": "what is x", "status": "completed"}
        les = self.extractor.extract(task, correction="actually it's y")
        self.assertEqual(les["outcome"], "correction")
        self.assertEqual(les["correction"], "actually it's y")

    def test_store_roundtrip(self):
        rec = self.store.add({"problem_class": "debugging",
                              "outcome": "success",
                              "signature": "debugging:x"})
        self.assertEqual(self.store.get(rec["id"])["outcome"], "success")
        rows = self.store.list(problem_class="debugging")
        self.assertEqual(len(rows), 1)


class CompetencyTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.map = CompetencyMap(Path(self.td.name) / "comp.json")

    def test_untested_and_no_fake_precision(self):
        row = self.map.present("programming.cuda")
        self.assertEqual(row["status"], "untested")
        self.assertIsNone(row["success_rate"])
        self.map.record("programming.cuda", "success")
        self.map.record("programming.cuda", "success")
        row = self.map.present("programming.cuda")
        # 2 samples: developing at best, low confidence — no fake mastery.
        self.assertEqual(row["status"], "developing")
        self.assertLess(row["confidence"], 0.2)
        self.assertEqual(row["attempts"], 2)

    def test_weak_and_strong_paths(self):
        for _ in range(10):
            self.map.record("programming.python", "success")
        for _ in range(8):
            self.map.record("programming.cuda", "failure")
        py = self.map.present("programming.python")
        cuda = self.map.present("programming.cuda")
        self.assertEqual(py["status"], "strong")
        self.assertEqual(cuda["status"], "weak")
        weak = self.map.weaknesses()
        self.assertEqual(weak[0]["id"], "programming.cuda")

    def test_model_breakdown(self):
        self.map.record("programming.c++", "success", model="30b")
        self.map.record("programming.c++", "failure", model="8b")
        row = self.map.present("programming.c++")
        self.assertEqual(row["model_breakdown"]["30b"]["successes"], 1)
        self.assertEqual(row["model_breakdown"]["8b"]["successes"], 0)

    def test_parent_hierarchy_usage(self):
        self.map.record("programming.python.debugging", "success")
        parent = self.map.present("programming.python")
        self.assertGreaterEqual(parent.get("descendant_attempts", 0), 1)


class ProcedureTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.procs = ProceduralMemory(Path(self.td.name) / "procs.json")

    def test_candidate_requires_repetition(self):
        sig = "debugging:cmake unresolved symbol"
        for i in range(2):
            c = self.procs.propose_candidate(
                sig, lesson_id=f"l{i}", steps=["inspect", "link", "rebuild"])
            self.assertFalse(c["promoted"])
            self.assertEqual(len(self.procs.list()), 0)
        c = self.procs.propose_candidate(sig, lesson_id="l3",
                                         steps=["inspect", "link", "rebuild"])
        self.assertTrue(c["promoted"])
        self.assertEqual(len(self.procs.list(status="candidate")), 1)

    def test_outcome_stats_and_verification(self):
        p = self.procs.add("cmake-linker-v1",
                           problem_signature="debugging:cmake linker",
                           steps=["a", "b"])
        for _ in range(3):
            p = self.procs.record_outcome(p["id"], "success")
        self.assertEqual(p["status"], "verified")
        self.assertEqual(p["success_count"], 3)

    def test_consecutive_failures_make_stale(self):
        p = self.procs.add("p", problem_signature="x:y", steps=["a"])
        for _ in range(3):
            p = self.procs.record_outcome(p["id"], "success")
        p = self.procs.record_outcome(p["id"], "failure")
        self.assertEqual(p["status"], "verified")
        p = self.procs.record_outcome(p["id"], "failure")
        self.assertEqual(p["status"], "stale")

    def test_versioned_adaptation(self):
        p = self.procs.add("cmake-linker-v1",
                           problem_signature="d:cmake", steps=["a"])
        v2 = self.procs.adapt(p["id"], extra_steps=["new step"],
                              note="needed extra probe")
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["derived_from"], p["id"])
        self.assertIn("new step", v2["steps"])

    def test_match_respects_context(self):
        self.procs.add("nvidia-fix", problem_signature="cuda driver error",
                       steps=["x"], contexts=["nvidia"])
        hits = self.procs.match("cuda driver error", context="windows")
        self.assertTrue(hits)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.strat = StrategyEvaluator(Path(self.td.name) / "strat.json")

    def test_recommend_best(self):
        for _ in range(5):
            self.strat.record("debugging", "inspect-env-first", ok=True)
            self.strat.record("debugging", "reinstall", ok=False)
        rec = self.strat.recommend("debugging")
        self.assertEqual(rec["strategy"], "inspect-env-first")
        avoid = self.strat.avoid("debugging")
        self.assertEqual(avoid[0]["strategy"], "reinstall")

    def test_insufficient_data_no_recommendation(self):
        self.strat.record("debugging", "x", ok=True)
        self.assertIsNone(self.strat.recommend("debugging"))


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.lessons = LessonStore(root / "lessons.json")
        self.procs = ProceduralMemory(root / "procs.json")
        self.strat = StrategyEvaluator(root / "strat.json")
        self.eng = ConsolidationEngine(lessons=self.lessons,
                                       procedures=self.procs,
                                       strategies=self.strat)

    def test_repeated_lessons_consolidate(self):
        for i in range(3):
            self.lessons.add({"problem_class": "debugging",
                              "signature": "debugging:invokeai cold start",
                              "outcome": "success",
                              "successful_strategy": "inspect-registry"})
        report = self.eng.run()
        self.assertGreaterEqual(report.clustered, 3)
        self.assertGreaterEqual(report.promoted, 1)
        # procedure candidate got promoted after 3 repeats
        self.assertEqual(len(self.procs.list()), 1)

    def test_similar_phrasings_cluster(self):
        # Same problem, different words — consolidation must still find
        # the repetition (the "17 InvokeAI incidents" case).
        goals = [
            "debugging:invokeai cold start fails",
            "debugging:invokeai cold start broken",
            "debugging:invokeai cold start crash",
        ]
        for g in goals:
            self.lessons.add({"problem_class": "debugging",
                              "signature": g, "outcome": "success",
                              "successful_strategy": "inspect-registry"})
        report = self.eng.run()
        self.assertGreaterEqual(report.clustered, 3)
        self.assertEqual(len(self.procs.list()), 1)

    def test_mixed_outcomes_mark_conflicted_not_erased(self):
        for oc in ("success", "success", "failure"):
            self.lessons.add({"problem_class": "debugging",
                              "signature": "debugging:same thing",
                              "outcome": oc,
                              "failed_strategies": ["x"] if oc == "failure" else []})
        report = self.eng.run()
        self.assertEqual(report.contradictions, 1)
        # all three lessons still exist — history preserved
        self.assertEqual(len(self.lessons.recent()), 3)

    def test_budget_enforced(self):
        for i in range(20):
            self.lessons.add({"problem_class": "x",
                              "signature": f"s{i}", "outcome": "success"})
        report = self.eng.run(ConsolidationBudget(records=5, seconds=60))
        self.assertLessEqual(report.examined, 5)
        self.assertTrue(report.stopped_by_budget)


class CurriculumTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.map = CompetencyMap(root / "comp.json")
        self.planner = ActiveLearningPlanner(self.map)
        self.cur = CurriculumManager()
        self.sessions = StudySessionStore(root / "study.json")

    def test_weak_used_competency_prioritized(self):
        for _ in range(6):
            self.map.record("programming.cuda", "failure")
            self.map.record("programming.cuda", "attempt")
        for _ in range(20):
            self.map.record("programming.python", "success")
        prios = self.planner.priorities()
        self.assertTrue(prios)
        self.assertEqual(prios[0]["id"], "programming.cuda")
        # mastered python isn't a learning target
        self.assertNotIn("programming.python", [p["id"] for p in prios])

    def test_curriculum_progression(self):
        c = self.cur.build("C++ concurrency")
        levels = [l["level"] for l in c["levels"]]
        self.assertEqual(levels[0], "foundation")
        self.assertEqual(levels[-1], "mastery")
        self.assertEqual(len(levels), 7)

    def test_study_session_lifecycle(self):
        s = self.sessions.open("CUDA debugging",
                               competency_ids=["programming.cuda"])
        self.assertEqual(s["status"], "active")
        self.assertEqual(self.sessions.active()["id"], s["id"])
        self.sessions.update(s["id"], concepts=["memory model"])
        self.sessions.close(s["id"])
        self.assertIsNone(self.sessions.active())
        self.assertEqual(self.sessions.get(s["id"])["status"], "completed")


class MasteryTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.map = CompetencyMap(Path(self.td.name) / "comp.json")
        self.mast = MasteryEvaluator(Path(self.td.name) / "mast.json",
                                     competency_map=self.map)

    def test_pipeline_and_mastery_flag(self):
        for stage in ("closed_book", "practical", "adversarial", "verified"):
            self.mast.record("programming.python.debugging", stage, 0.85)
        lvl = self.mast.mastery_level("programming.python.debugging")
        self.assertTrue(lvl["mastered"])
        self.assertEqual(lvl["evaluations"], 4)

    def test_difficulty_adapts(self):
        self.mast.record("x", "closed_book", 0.95, difficulty=1)
        self.assertEqual(self.mast.next_difficulty("x", "closed_book"), 2)
        self.mast.record("x", "closed_book", 0.3, difficulty=2)
        self.mast.record("x", "closed_book", 0.4, difficulty=2)
        self.assertEqual(self.mast.next_difficulty("x", "closed_book"), 1)

    def test_retention_schedule_and_decay(self):
        self.mast.record("x", "verified", 0.9)
        lvl = self.mast.mastery_level("x")
        self.assertGreater(lvl["retention"]["next_check"], 0)
        r = self.mast.record_retention_check("x", 0.4)  # decayed
        self.assertEqual(r["interval_index"], 0)
        r = self.mast.record_retention_check("x", 0.9)  # refreshed
        self.assertEqual(r["interval_index"], 1)

    def test_competency_feeds_from_mastery(self):
        self.mast.record("programming.x", "closed_book", 0.8)
        row = self.map.present("programming.x")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["verified_successes"], 1)


class SkillPromotionTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.procs = ProceduralMemory(root / "procs.json")
        from localcodeagent.learning import SkillPromotionEngine
        self.eng = SkillPromotionEngine(root / "scands.json",
                                        procedures=self.procs)

    def _verified_proc(self, successes=5):
        p = self.procs.add("cmake-linker-v1",
                           problem_signature="debugging:cmake linker",
                           steps=["identify", "link", "rebuild"])
        for _ in range(successes):
            p = self.procs.record_outcome(p["id"], "success")
        return p

    def test_propose_requires_verified_and_repetition(self):
        p = self.procs.add("p", problem_signature="x", steps=["a"])
        self.assertIsNone(self.eng.propose(p["id"]))  # not verified
        proc = self._verified_proc(successes=5)
        prop = self.eng.propose(proc["id"])
        self.assertIsNotNone(prop)
        self.assertEqual(prop["status"], "pending")
        self.assertIn("cmake", prop["spec"]["name"])
        self.assertEqual(prop["spec"]["permissions"], [])

    def test_reject_keeps_history(self):
        proc = self._verified_proc()
        prop = self.eng.propose(proc["id"])
        self.eng.reject(prop["id"])
        self.assertEqual(len(self.eng.pending()), 0)
        self.assertEqual(self.eng.summary()["proposals"], 1)


class TrainingGateTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.training.model_growth import ModelGrowthLab
        from localcodeagent.learning import TrainingCandidateGate
        self.growth = ModelGrowthLab(Path(self.td.name) / "growth")
        self.gate = TrainingCandidateGate(self.growth)

    def test_unverified_model_output_rejected(self):
        out = self.gate.submit(
            instruction="what is x", response="x is y",
            source_type=t.SOURCE_MODEL, verification=t.VERIFY_MODEL_ASSERTED)
        self.assertIsNone(out)
        self.assertEqual(len(self.growth.candidates()), 0)

    def test_verified_row_auto_approved(self):
        out = self.gate.submit(
            instruction="fix linker error", response="steps…",
            source_type=t.SOURCE_TEST, verification=t.VERIFY_TESTED,
            confidence=0.85,
            provenance={"task": "t1"})
        self.assertIsNotNone(out)
        self.assertEqual(out["status"], "approved")
        self.assertEqual(out["metadata"]["quality"], "verified")
        self.assertEqual(out["metadata"]["provenance"]["task"], "t1")

    def test_exportable_defaults_to_high_quality(self):
        self.gate.submit(instruction="a", response="b",
                         source_type=t.SOURCE_TEST,
                         verification=t.VERIFY_TESTED, confidence=0.9)
        self.growth.collect(kind="x", instruction="raw", response="raw",
                            source="conversation")  # ungated, pending
        rows = self.gate.exportable()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metadata"]["quality"], "verified")


class TeacherStudentTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        from localcodeagent.training.model_growth import ModelGrowthLab
        from localcodeagent.learning import (TeacherStudentPipeline,
                                             TrainingCandidateGate)
        self.map = CompetencyMap(root / "comp.json")
        self.gate = TrainingCandidateGate(ModelGrowthLab(root / "g"))
        self.pipe = TeacherStudentPipeline(gate=self.gate,
                                           competencies=self.map)

    def test_verified_teacher_produces_candidate(self):
        res = self.pipe.run(
            "hard problem",
            competency="programming.debugging",
            teacher_fn=lambda p: "verified solution",
            verify_fn=lambda p, s: True,
            review_fn=lambda p, s: "looks right",
            student_fn=lambda p: "wrong answer")
        # student fails verify via verify_fn=True? we pass verify always
        # True here; check stage list
        stages = [s["stage"] for s in res["stages"]]
        self.assertEqual(stages, ["teacher", "verify", "review", "student"])
        self.assertTrue(res["training_candidate"])

    def test_unverified_teacher_no_candidate(self):
        res = self.pipe.run(
            "hard problem",
            teacher_fn=lambda p: "guess",
            verify_fn=lambda p, s: False)
        self.assertFalse(res["teacher_ok"])
        self.assertFalse(res["training_candidate"])


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.gov = LearningGovernor(Path(self.td.name) / "learning")

    def test_observe_task_updates_lessons_competency_strategies(self):
        task = {"id": "t1", "goal": "fix the crash in the parser",
                "status": "completed", "model": "m1"}
        self.gov.observe_task(task)
        self.assertEqual(self.gov.lessons.summary()["total"], 1)
        row = self.gov.competencies.present("programming.debugging")
        self.assertEqual(row["verified_successes"], 1)
        self.assertTrue(self.gov.strategies.for_problem("debugging"))

    def test_command_turns_teach_nothing(self):
        # Deterministic/builtin turns aren't learning experiences —
        # otherwise "/study stop" becomes a "lesson".
        for src in ("command", "research_followup", "brain_fast_path",
                    "answer_memory"):
            self.assertIsNone(self.gov.observe_task({
                "id": "c1", "goal": "x", "status": "completed",
                "response_source": src}))
        self.assertIsNone(self.gov.observe_task({
            "id": "c2", "goal": "x", "status": "completed",
            "model_id": "builtin-local"}))
        self.assertEqual(self.gov.lessons.summary()["total"], 0)

    def test_observe_correction_penalizes(self):
        task = {"id": "t2", "goal": "explain widgets", "status": "completed"}
        self.gov.observe_correction(task, "no, widgets are other thing")
        rows = [r for r in self.gov.competencies.all()
                if r.get("verified_failures")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verified_failures"], 1)

    def test_start_and_stop_study(self):
        s = self.gov.start_study("C++ concurrency")
        self.assertEqual(s["status"], "active")
        # second session refused while one is active
        self.assertIn("error", self.gov.start_study("rust"))
        self.gov.stop_study()
        self.assertIsNone(self.gov.study_sessions.active())

    def test_summary_shape(self):
        s = self.gov.summary()
        for key in ("lessons", "procedures", "strategies",
                    "competencies", "study", "mastery", "priorities"):
            self.assertIn(key, s)

    def test_consolidate_stages_pending_skill_proposals(self):
        # A verified procedure with enough successes becomes eligible —
        # consolidate auto-stages the proposal so /api/learning surfaces
        # it for explicit approval.
        p = self.gov.procedures.add(
            "repair-widget", problem_signature="widgets:repair widget",
            steps=["inspect", "repair", "verify"])
        for _ in range(5):
            self.gov.procedures.record_outcome(p["id"], "success")
        self.assertEqual(self.gov.procedures.get(p["id"])["status"],
                         "verified")
        out = self.gov.consolidate()
        self.assertGreaterEqual(out["skill_proposals_staged"], 1)
        self.assertGreaterEqual(out["skill_proposals_pending"], 1)
        pending = self.gov.skill_promotion.pending()
        self.assertEqual(pending[0]["procedure_id"], p["id"])
        # Idempotent — a second consolidate reuses the pending row.
        out2 = self.gov.consolidate()
        self.assertEqual(len(self.gov.skill_promotion.pending()), 1)

    def test_consolidate_no_eligible_stages_nothing(self):
        out = self.gov.consolidate()
        self.assertEqual(out["skill_proposals_staged"], 0)
        self.assertEqual(out["skill_proposals_pending"], 0)


if __name__ == "__main__":
    unittest.main()
