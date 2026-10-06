"""LearningGovernor — coordinates the continual-learning stores (Part 1).

This is the single entry point the rest of Nexus talks to. It owns the
stores that didn't exist (lessons, procedures, strategies, competencies,
curriculum/study, mastery) and COORDINATES the ones that did
(KnowledgeMemory, EvidenceBoard, DecisionJournal, ModelGrowthLab,
regressions, benchmarks) — never duplicating them.

Experience → outcome → verification → lesson → classification →
consolidation → memory classes → competency → curriculum → study →
mastery → training candidates.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import taxonomy as t
from .competencies import CompetencyMap
from .consolidation import ConsolidationBudget, ConsolidationEngine
from .curriculum import (ActiveLearningPlanner, CurriculumManager,
                         StudySessionStore)
from .freshness import FreshnessPolicy
from .lessons import LessonExtractor, LessonStore, classify_problem
from .mastery import MasteryEvaluator
from .procedures import ProceduralMemory
from .promotion import KnowledgePromotionPolicy
from .skill_promotion import SkillPromotionEngine
from .strategies import StrategyEvaluator
from .teacher_student import TeacherStudentPipeline
from .training_gate import TrainingCandidateGate

# Map problem classes → competency ids so experience lands on the right
# skill node (dotted ids build the hierarchy on write).
_CLASS_COMPETENCY = {
    "debugging": "programming.debugging",
    "configuration": "system.configuration",
    "creation": "programming.general",
    "research": "research.general",
    "explanation": "knowledge.explanation",
    "general": "general",
}


class LearningGovernor:
    def __init__(
        self,
        data_dir: Path,
        *,
        knowledge_memory=None,
        evidence=None,
        decisions=None,
        regressions=None,
        benchmarks=None,
        model_growth=None,
    ) -> None:
        data_dir = Path(data_dir).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        # Coordinated (existing) stores.
        self.knowledge_memory = knowledge_memory
        self.evidence = evidence
        self.decisions = decisions
        self.regressions = regressions
        self.benchmarks = benchmarks
        self.model_growth = model_growth
        # Owned stores.
        self.lessons = LessonStore(data_dir / "lessons.json")
        self.procedures = ProceduralMemory(data_dir / "procedures.json")
        self.strategies = StrategyEvaluator(data_dir / "strategies.json")
        self.competencies = CompetencyMap(data_dir / "competencies.json")
        self.study_sessions = StudySessionStore(data_dir / "study_sessions.json")
        self.mastery = MasteryEvaluator(data_dir / "mastery.json",
                                        competency_map=self.competencies)
        # Policies/engines.
        self.promotion = KnowledgePromotionPolicy()
        self.freshness = FreshnessPolicy()
        self.curriculum = CurriculumManager()
        self.planner = ActiveLearningPlanner(self.competencies)
        self.consolidator = ConsolidationEngine(
            lessons=self.lessons, procedures=self.procedures,
            strategies=self.strategies,
            knowledge_memory=knowledge_memory,
            freshness=self.freshness,
            promotion_policy=self.promotion)
        self.extractor = LessonExtractor()
        # L13–L15: skill promotion needs user approval; training rows
        # pass the quality gate; teacher/student feeds both.
        self.skill_promotion = SkillPromotionEngine(
            data_dir / "skill_candidates.json", procedures=self.procedures)
        self.training_gate = (TrainingCandidateGate(
            model_growth, policy=self.promotion)
            if model_growth is not None else None)
        self.teacher_student = TeacherStudentPipeline(
            gate=self.training_gate, competencies=self.competencies)
        self.last_consolidation: dict | None = None

    # -- experience intake ----------------------------------------------------

    def _competency_for(self, problem_class: str, goal: str = "") -> str:
        cid = _CLASS_COMPETENCY.get(problem_class)
        if cid:
            return cid
        return _CLASS_COMPETENCY["general"]

    # Turns that never teach anything: deterministic command executions,
    # builtin fast paths, answer-memory replays, research follow-up text.
    _SKIP_SOURCES = frozenset({
        "command", "builtin", "brain_fast_path", "answer_memory",
        "research_followup",
    })

    def observe_task(self, task: dict, *, user_feedback: str = "") -> dict | None:
        """Post-task hook: extract a lesson + update competency/strategy
        bookkeeping. Only structured fields are stored — no transcripts.
        Deterministic no-model turns teach nothing and are skipped."""
        src = str(task.get("response_source") or "")
        if src in self._SKIP_SOURCES or (
                not src and str(task.get("model_id") or "") == "builtin-local"):
            return None
        lesson = self.extractor.extract(task, user_feedback=user_feedback)
        self.lessons.add(lesson)
        pclass = lesson["problem_class"]
        cid = self._competency_for(pclass, lesson.get("goal") or "")
        outcome = ("success" if lesson["outcome"] == "success"
                   else "failure" if lesson["outcome"] == "failure"
                   else "partial")
        self.competencies.record(cid, outcome, model=lesson.get("model") or "")
        for st in lesson.get("strategies_used") or []:
            self.strategies.record(
                pclass, st, ok=lesson["outcome"] == "success",
                retries=int(task.get("retries") or 0))
        for st in lesson.get("failed_strategies") or []:
            self.strategies.record(pclass, st, ok=False)
        # Part 48: the same behavior failing after verified passes opens
        # a real regression event in the shared store.
        if self.regressions is not None:
            try:
                self.regressions.record(
                    str(lesson.get("signature") or "")[:200],
                    ok=lesson["outcome"] != "failure",
                    detail=str(lesson.get("correction") or "")[:200])
            except Exception:
                pass
        return lesson

    def observe_correction(self, task: dict, correction: str) -> dict:
        """User correction → lesson + competency penalty + candidate
        evidence (never universal truth until verified — Part 47)."""
        lesson = self.extractor.extract(task, correction=correction)
        self.lessons.add(lesson)
        pclass = lesson["problem_class"]
        cid = self._competency_for(pclass, lesson.get("goal") or "")
        self.competencies.record(cid, "failure", model=lesson.get("model") or "")
        return lesson

    # -- consolidation ---------------------------------------------------------

    def consolidate(self, budget: ConsolidationBudget | None = None) -> dict:
        report = self.consolidator.run(budget)
        return report.as_dict()

    # -- competency / planning -------------------------------------------------

    def weaknesses(self, *, limit: int = 10) -> list[dict]:
        return self.competencies.weaknesses(limit=limit)

    def learning_priorities(self, *, limit: int = 10) -> list[dict]:
        return self.planner.priorities(limit=limit)

    # -- study ------------------------------------------------------------------

    def start_study(self, topic: str, *,
                    competency_ids: list[str] | None = None,
                    budget: dict | None = None) -> dict:
        active = self.study_sessions.active()
        if active:
            return {"error": "study session already active",
                    "session": active}
        cur = self.curriculum.build(topic)
        return self.study_sessions.open(
            topic, competency_ids=competency_ids, curriculum=cur,
            budget=budget)

    def stop_study(self) -> dict | None:
        active = self.study_sessions.active()
        if not active:
            return None
        return self.study_sessions.close(active["id"], status="stopped")

    def run_study_step(self, *, research_fn=None,
                       session_id: str = "") -> dict:
        """One bounded study step: fetch sources (within budget), extract
        structured concepts from verified claims, generate questions.
        Research ≠ study — this only runs inside a session with a
        defined curriculum/budget, never free-roams (Part 59)."""
        sess = (self.study_sessions.get(session_id) if session_id
                else self.study_sessions.active())
        if not sess or sess.get("status") != "active":
            return {"error": "no active study session"}
        budget = dict(sess.get("budget") or {"sources": 8, "seconds": 600})
        src_budget = max(1, int(budget.get("sources") or 8))
        used = len(sess.get("sources") or [])
        added: dict = {"sources": [], "concepts": [], "questions": []}

        if used < src_budget and callable(research_fn):
            res = research_fn(sess["topic"]) or {}
            session = res.get("session") or res
            for s in list(session.get("sources") or [])[:src_budget - used]:
                if isinstance(s, dict) and s.get("url"):
                    added["sources"].append({
                        "title": str(s.get("title") or "")[:160],
                        "url": str(s.get("url") or "")[:400],
                        "badges": list(s.get("badges") or [])[:4],
                    })
            ev = session.get("evidence") or {}
            for cl in list(ev.get("claims") or [])[:10]:
                txt = str(cl.get("claim") or cl.get("claim_text")
                          or cl).strip()[:240] if isinstance(cl, dict) \
                    else str(cl).strip()[:240]
                if txt:
                    added["concepts"].append({
                        "concept": txt,
                        "support": str(cl.get("support") or "")[:40]
                        if isinstance(cl, dict) else "",
                    })

        # Question generation from verified material (Part 20): recall +
        # application prompts derived from concepts and the current
        # curriculum stage. Answering/grading is a later, explicit step.
        stage = len(sess.get("exercises") or [])
        levels = (sess.get("curriculum") or {}).get("levels") or []
        stage_name = (levels[min(stage, len(levels) - 1)]["level"]
                      if levels else "foundation")
        for c in added["concepts"][:4]:
            added["questions"].append({
                "kind": "recall",
                "prompt": f"Explain: {c['concept'][:120]}",
                "stage": stage_name,
            })
        if added["concepts"]:
            added["questions"].append({
                "kind": "application",
                "prompt": f"Apply '{sess['topic']}' at the "
                          f"{stage_name} level: "
                          f"{levels[min(stage, len(levels)-1)]['objective'][:120]}"
                          if levels else f"Apply {sess['topic']}.",
                "stage": stage_name,
            })
        if any(added.values()):
            self.study_sessions.update(
                sess["id"], **added)
        return {"session_id": sess["id"], "stage": stage_name,
                "added": {k: len(v) for k, v in added.items()},
                "sources_total": used + len(added["sources"]),
                "sources_budget": src_budget}

    def run_mastery_eval(self, topic: str, *, answer_fn=None,
                         grade_fn=None, max_questions: int = 5) -> dict:
        """Closed-book mastery evaluation (Parts 20-22): questions come
        from the topic's study session (or are generated); answers and
        grading come from injected model callables — never trusted
        self-assessment. Score is recorded through MasteryEvaluator
        which handles difficulty adaptation + retention scheduling."""
        if not callable(answer_fn) or not callable(grade_fn):
            return {"error": "evaluation unavailable"}
        topic = str(topic or "").strip()
        if not topic:
            return {"error": "no topic"}
        sess = self.study_sessions.active()
        if not sess or sess.get("topic") != topic:
            sess = next(
                (s for s in reversed(self.study_sessions.recent(limit=10))
                 if s.get("topic") == topic), None)
        questions = [q["prompt"] for q in (sess or {}).get("questions", [])]
        if not questions:
            questions = [
                f"Define {topic} and its core invariants.",
                f"Show a small correct example of {topic}.",
                f"Describe a subtle failure mode in {topic} and how to "
                f"diagnose it.",
            ]
        questions = questions[:max(1, int(max_questions))]
        difficulty = self.mastery.next_difficulty(topic, "closed_book")
        answers, scores = [], []
        for q in questions:
            ans = str(answer_fn(q) or "")[:2000]
            if not ans:
                continue
            sc = grade_fn(q, ans)
            try:
                sc = max(0.0, min(1.0, float(sc)))
            except (TypeError, ValueError):
                continue
            answers.append({"question": q, "answer": ans[:400],
                            "score": round(sc, 3)})
            scores.append(sc)
        if not scores:
            return {"error": "no gradeable answers"}
        score = sum(scores) / len(scores)
        passed = sum(1 for s in scores if s >= 0.6)
        ev = self.mastery.record(
            topic, "closed_book", score, total=len(scores),
            passed=passed, difficulty=difficulty,
            evidence=f"closed-book eval, {len(scores)} questions")
        return {"competency": topic, "score": ev["score"],
                "passed": passed, "total": len(scores),
                "difficulty": difficulty, "verdict": ev["score"] >= 0.75,
                "answers": answers}

    # -- dashboard --------------------------------------------------------------

    def summary(self) -> dict:
        return {
            "lessons": self.lessons.summary(),
            "procedures": self.procedures.summary(),
            "strategies": self.strategies.summary(),
            "competencies": self.competencies.summary(),
            "study": self.study_sessions.summary(),
            "mastery": self.mastery.summary(),
            "priorities": self.learning_priorities(limit=5),
        }
