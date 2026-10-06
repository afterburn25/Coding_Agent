"""CognitiveScheduler — decides WHICH cognitive operation runs next.

Distinct from the task/job scheduler (WHEN work runs). At each step it
ranks candidate operations by a bounded expected-value heuristic:

    EV = information_gain * p_useful * importance
         - time_cost - compute_cost - risk_cost

and stops when confidence, verification, and contradiction checks pass
— or the compute budget runs out. Heuristics are deliberately coarse;
they are meant to be calibrated from outcomes, not fake precision.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from .metacognition import CONFLICTED, KNOWN, PARTIAL

# Operation type catalog — base cost in abstract units (~seconds),
# base latency weight, risk weight, and capabilities each op needs.
OP_PROFILES: dict[str, dict[str, Any]] = {
    "retrieve_memory":            {"cost": 0.1, "risk": 0.0, "cap": ()},
    "query_belief_graph":         {"cost": 0.1, "risk": 0.0, "cap": ()},
    "inspect_local_state":        {"cost": 0.2, "risk": 0.0, "cap": ()},
    "inspect_repository":         {"cost": 0.3, "risk": 0.0, "cap": ()},
    "search_web":                 {"cost": 1.2, "risk": 0.1, "cap": ("research",)},
    "read_source":                {"cost": 0.6, "risk": 0.0, "cap": ("research",)},
    "ask_fast_model":             {"cost": 0.5, "risk": 0.1, "cap": ("model",)},
    "ask_deep_model":             {"cost": 2.5, "risk": 0.1, "cap": ("deep_model",)},
    "spawn_specialist":           {"cost": 4.0, "risk": 0.2, "cap": ("swarm",)},
    "generate_hypotheses":        {"cost": 0.8, "risk": 0.0, "cap": ()},
    "test_hypothesis":            {"cost": 1.5, "risk": 0.2, "cap": ("tools",)},
    "run_formal_solver":          {"cost": 1.0, "risk": 0.0, "cap": ("solvers",)},
    "run_experiment":             {"cost": 3.0, "risk": 0.3, "cap": ("experiments",)},
    "simulate_plan":              {"cost": 1.5, "risk": 0.0, "cap": ("simulate",)},
    "run_benchmark":              {"cost": 3.5, "risk": 0.1, "cap": ("benchmarks",)},
    "ask_critic":                 {"cost": 1.5, "risk": 0.0, "cap": ("model",)},
    "run_verifier":               {"cost": 1.0, "risk": 0.0, "cap": ("verify",)},
    "request_user_clarification": {"cost": 0.0, "risk": 0.0, "cap": ()},
    "stop_reasoning":             {"cost": 0.0, "risk": 0.0, "cap": ()},
}

_ids = itertools.count(1)


@dataclass
class CognitiveOperation:
    type: str
    goal: str = ""
    expected_information_gain: float = 0.5
    estimated_cost: float = 1.0
    estimated_latency: float = 1.0
    estimated_resource_cost: float = 0.0
    risk: float = 0.0
    dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    confidence_before: float = 0.0
    expected_confidence_after: float = 0.0
    status: str = "pending"      # pending|running|done|skipped|failed
    result_refs: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"cop-{next(_ids)}")

    def expected_value(self, *, importance: float = 1.0) -> float:
        gain = (self.expected_information_gain
                * max(0.0, min(1.0, importance)))
        return (gain - self.estimated_cost * 0.3
                - self.estimated_latency * 0.1
                - self.estimated_resource_cost * 0.2
                - self.risk * 0.5)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "id", "type", "goal", "expected_information_gain",
            "estimated_cost", "estimated_latency",
            "estimated_resource_cost", "risk", "dependencies",
            "required_capabilities", "confidence_before",
            "expected_confidence_after", "status", "result_refs")}


@dataclass
class ComputeBudget:
    """Hard bounds per intelligence session — never infinite."""
    max_operations: int = 8
    max_cost: float = 12.0
    max_model_calls: int = 4
    max_specialists: int = 0
    max_web_pages: int = 10

    @classmethod
    def for_mode(cls, mode: str) -> "ComputeBudget":
        return {
            "fast": cls(max_operations=3, max_cost=3.0, max_model_calls=1,
                        max_specialists=0, max_web_pages=4),
            "normal": cls(max_operations=6, max_cost=8.0, max_model_calls=2,
                          max_specialists=0, max_web_pages=8),
            "deep": cls(max_operations=10, max_cost=20.0, max_model_calls=4,
                        max_specialists=2, max_web_pages=12),
            "exhaustive": cls(max_operations=16, max_cost=40.0,
                              max_model_calls=6, max_specialists=4,
                              max_web_pages=20),
        }.get(mode, cls())


# How much each op can plausibly raise answer confidence, by state.
_GAIN_TABLE: dict[str, dict[str, float]] = {
    "unknown":    {"retrieve_memory": 0.15, "search_web": 0.55,
                   "ask_fast_model": 0.2, "ask_deep_model": 0.4,
                   "generate_hypotheses": 0.25, "test_hypothesis": 0.45},
    "partial":    {"retrieve_memory": 0.1, "search_web": 0.35,
                   "ask_fast_model": 0.2, "ask_deep_model": 0.3,
                   "run_verifier": 0.25, "ask_critic": 0.2},
    "stale":      {"search_web": 0.6, "retrieve_memory": 0.1,
                   "ask_fast_model": 0.1},
    "conflicted": {"search_web": 0.4, "run_verifier": 0.35,
                   "ask_critic": 0.3, "test_hypothesis": 0.5},
    "known":      {"run_verifier": 0.1, "ask_critic": 0.05},
}


class CognitiveScheduler:
    """Ranks and bounds the next cognitive operation."""

    def __init__(self, budget: ComputeBudget | None = None,
                 *, capabilities: set[str] | None = None) -> None:
        self.budget = budget or ComputeBudget()
        # What this install can actually do — ops needing unavailable
        # capabilities degrade cleanly instead of being scheduled.
        self.capabilities = set(capabilities or {"model"})
        self.spent_cost = 0.0
        self.model_calls = 0
        self.specialists = 0
        self.web_pages = 0
        self._pending: list[CognitiveOperation] = []

    def propose(self, assessment) -> list[CognitiveOperation]:
        """Assessment's recommended ops → costed CognitiveOperations."""
        ops: list[CognitiveOperation] = []
        state = assessment.knowledge_state
        gains = _GAIN_TABLE.get(state, {})
        for op_type in assessment.recommended_ops:
            profile = OP_PROFILES.get(op_type)
            if profile is None:
                continue
            gain = 0.0 if op_type == "stop_reasoning" else gains.get(
                op_type, 0.15)
            ops.append(CognitiveOperation(
                type=op_type,
                goal=f"reduce uncertainty: {', '.join(assessment.uncertainty_sources) or 'answer the question'}",
                expected_information_gain=gain,
                estimated_cost=float(profile["cost"]),
                estimated_latency=float(profile["cost"]) * 0.6,
                estimated_resource_cost=0.5 if "model" in op_type else 0.1,
                risk=float(profile["risk"]),
                required_capabilities=list(profile["cap"]),
                confidence_before=assessment.confidence,
                expected_confidence_after=min(
                    1.0, assessment.confidence + gain),
            ))
        return ops

    def rank(self, ops: list[CognitiveOperation],
             *, importance: float = 1.0) -> list[CognitiveOperation]:
        return sorted(ops, key=lambda o: o.expected_value(
            importance=importance), reverse=True)

    def available(self, op: CognitiveOperation) -> bool:
        if not set(op.required_capabilities) <= self.capabilities:
            return False
        if op.type in ("ask_fast_model", "ask_deep_model", "ask_critic") \
                and self.model_calls >= self.budget.max_model_calls:
            return False
        if op.type == "spawn_specialist" \
                and self.specialists >= self.budget.max_specialists:
            return False
        if op.type in ("search_web", "read_source") \
                and self.web_pages >= self.budget.max_web_pages:
            return False
        return self.spent_cost + op.estimated_cost <= self.budget.max_cost

    def should_stop(self, assessment, ran: list[CognitiveOperation],
                    *, confidence: float | None = None,
                    unresolved_contradictions: int = 0) -> bool:
        """Acceptance + verification + confidence + contradiction checks,
        then hard budget bounds. Do not think forever."""
        conf = assessment.confidence if confidence is None else confidence
        verified = any(o.type == "run_verifier" and o.status == "done"
                       for o in ran)
        if (conf >= 0.8
                and (verified or assessment.stakes != "high")
                and unresolved_contradictions == 0):
            return True
        if len(ran) >= self.budget.max_operations:
            return True
        if self.spent_cost >= self.budget.max_cost:
            return True
        # Confidence adequate for the stakes and nothing left that helps.
        if conf >= 0.6 and assessment.stakes != "high" \
                and not any(self.available(o) and o.status == "pending"
                            and o.expected_information_gain > 0.3
                            for o in self._pending):
            return True
        return False

    def record(self, op: CognitiveOperation, *,
               web_pages: int = 0) -> None:
        self.spent_cost += op.estimated_cost
        if op.type in ("ask_fast_model", "ask_deep_model", "ask_critic"):
            self.model_calls += 1
        if op.type == "spawn_specialist":
            self.specialists += 1
        self.web_pages += web_pages

    def next_op(self, candidates: list[CognitiveOperation],
                assessment) -> CognitiveOperation | None:
        """Highest-EV affordable operation, or None when stopping."""
        self._pending = [o for o in candidates if o.status == "pending"]
        if self.should_stop(assessment,
                            [o for o in candidates if o.status == "done"]):
            return None
        for op in self.rank(self._pending,
                            importance=1.0 + assessment.novelty * 0.5):
            if self.available(op):
                return op
        return None
