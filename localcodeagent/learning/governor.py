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
