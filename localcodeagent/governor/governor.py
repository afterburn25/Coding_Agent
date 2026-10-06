"""IntelligenceGovernor — one entry point for the cognitive loop.

Turn flow: metacognitive assess → CognitiveScheduler proposes/ranks ops
→ an IntelPlan the orchestrator executes through its existing lanes
(research, model routing, verification). The Governor decides nothing
in prose; it emits a typed plan plus a high-level activity label — the
user sees "Searching documentation", never hidden reasoning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .metacognition import MetaAssessment, assess
from .scheduler import (CognitiveOperation, CognitiveScheduler,
                        ComputeBudget)

# High-level user-visible labels per op type — lifecycle only.
ACTIVITY_LABELS = {
    "retrieve_memory": "Checking memory",
    "query_belief_graph": "Checking what I know",
    "inspect_local_state": "Checking local state",
    "inspect_repository": "Inspecting repository",
    "search_web": "Searching the web",
    "read_source": "Reading sources",
    "ask_fast_model": "Answering",
    "ask_deep_model": "Thinking deeper",
    "spawn_specialist": "Investigating in parallel",
    "generate_hypotheses": "Testing possible causes",
    "test_hypothesis": "Testing a theory",
    "run_formal_solver": "Computing exactly",
    "run_experiment": "Running an experiment",
    "simulate_plan": "Simulating the plan",
    "run_benchmark": "Benchmarking",
    "ask_critic": "Reviewing the answer",
    "run_verifier": "Verifying",
    "request_user_clarification": "Need clarification",
    "stop_reasoning": "Ready to answer",
}


@dataclass
class IntelPlan:
    assessment: MetaAssessment
    operations: list[CognitiveOperation] = field(default_factory=list)
    budget: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "assessment": self.assessment.as_dict(),
            "operations": [o.as_dict() for o in self.operations],
            "budget": self.budget,
        }

    def activity_label(self, op: CognitiveOperation) -> str:
        return ACTIVITY_LABELS.get(op.type, "Working")


class IntelligenceGovernor:
    """Composes MetaAssessment + CognitiveScheduler into per-turn plans.

    ``capabilities`` is the set of lanes the host can actually execute
    (research/model/verify/tools/…); the scheduler degrades cleanly past
    anything absent. ``think_mode`` comes from the /think command.
    """

    def __init__(self, *, capabilities: set[str] | None = None) -> None:
        self.capabilities = set(capabilities or {"model"})

    def plan(
        self,
        text: str,
        *,
        policy=None,
        knowledge_hit: bool = False,
        answer_memory_hit: bool = False,
        last_evidence: dict | None = None,
        think_mode: str = "auto",
        prior_uncertain: bool = False,
    ) -> IntelPlan:
        mode = think_mode if think_mode != "auto" else self._auto_mode(
            policy, knowledge_hit, answer_memory_hit)
        assessment = assess(
            text, policy=policy, knowledge_hit=knowledge_hit,
            answer_memory_hit=answer_memory_hit,
            last_evidence=last_evidence, think_mode=mode,
            prior_uncertain=prior_uncertain)
        budget = ComputeBudget.for_mode(mode)
        scheduler = CognitiveScheduler(
            budget, capabilities=self.capabilities)
        ops = scheduler.propose(assessment)
        ranked = scheduler.rank(ops,
                                importance=1.0 + assessment.novelty * 0.5)
        return IntelPlan(
            assessment=assessment,
            operations=ranked[: budget.max_operations],
            budget={"max_operations": budget.max_operations,
                    "max_cost": budget.max_cost,
                    "max_model_calls": budget.max_model_calls,
                    "max_specialists": budget.max_specialists,
                    "max_web_pages": budget.max_web_pages},
        )

    @staticmethod
    def _auto_mode(policy, knowledge_hit: bool, answer_hit: bool) -> str:
        """Cheapest sufficient strategy: confident local answers stay
        fast; volatile/error work earns a deeper ladder."""
        level = str(getattr(policy, "level", "") or "")
        if getattr(policy, "is_error", False):
            return "deep"
        if level == "web_required":
            return "normal"
        if answer_hit or (knowledge_hit and level == "local_confident"):
            return "fast"
        return "normal"
