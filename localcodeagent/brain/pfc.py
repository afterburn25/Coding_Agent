"""Prefrontal Cortex — goals, plans, replanning, completion assessment.

Wraps the existing autonomy MissionPlanner/MissionStore rather than
replacing them: the PFC owns *cognitive* decisions — decompose a goal,
track a plan's working state, detect when progress has stalled or results
contradict, and decide when to re-plan or escalate.

Subcomponents:
- dorsolateral: technical plan construction (via MissionPlanner)
- orbitofrontal: cost/latency/risk/quality evaluation of strategies
- anterior cingulate: conflict monitor — repeated failures, loops,
  contradictory outcomes, worsening trends → ConflictDetected events
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .events import CognitiveEvent, EventType, Priority
from .regions import BrainRegion
from . import events as ev


@dataclass(slots=True)
class PlanStep:
    title: str
    instruction: str = ""
    kind: str = "task"               # task|verify|decide|research
    status: str = "pending"          # pending|running|done|failed|skipped
    attempts: int = 0
    result: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "instruction": self.instruction,
                "kind": self.kind, "status": self.status,
                "attempts": self.attempts, "result": self.result[:400]}


@dataclass(slots=True)
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    confidence: float = 0.6
    assumptions: list[str] = field(default_factory=list)
    mission_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, "goal": self.goal,
                "steps": [s.as_dict() for s in self.steps],
                "confidence": self.confidence,
                "assumptions": self.assumptions,
                "mission_id": self.mission_id,
                "created_at": self.created_at}


class ConflictMonitor:
    """Anterior cingulate — watches execution signals for stuck states."""

    LOOP_WINDOW = 6

    def __init__(self) -> None:
        self._action_tail: list[str] = []
        self._failure_counts: dict[str, int] = {}
        self._last_outcomes: dict[str, str] = {}

    def observe(self, action: str, ok: bool, outcome: str = "") -> list[dict]:
        """Return detected conflicts for one observed action outcome."""
        conflicts: list[dict] = []
        self._action_tail.append(action)
        self._action_tail = self._action_tail[-self.LOOP_WINDOW * 2:]
        if ok:
            self._failure_counts.pop(action, None)
        else:
            n = self._failure_counts.get(action, 0) + 1
            self._failure_counts[action] = n
            if n >= 3:
                conflicts.append({
                    "kind": "repeated_failure", "action": action,
                    "count": n, "detail": outcome[:200]})
        # Loop: same two actions alternating with failures.
        tail = self._action_tail[-self.LOOP_WINDOW:]
        if len(tail) == self.LOOP_WINDOW and len(set(tail)) == 2 \
                and sum(1 for a in tail if self._failure_counts.get(a, 0) > 0):
            conflicts.append({"kind": "loop",
                              "actions": sorted(set(tail)),
                              "detail": "alternating actions with failures"})
        # Contradiction: same action reporting success then failure.
        prev = self._last_outcomes.get(action)
        now = "ok" if ok else "fail"
        if prev and prev != now and not ok:
            conflicts.append({"kind": "contradiction", "action": action,
                              "detail": f"was {prev}, now {now}"})
        self._last_outcomes[action] = now
        return conflicts


class PrefrontalCortex(BrainRegion):
    name = ev.REGION_PFC

    def __init__(self, bus, *, mission_planner=None, mission_store=None,
                 hippocampus=None, evaluator=None) -> None:
        super().__init__(bus)
        self.mission_planner = mission_planner
        self.mission_store = mission_store
        self.hippocampus = hippocampus
        self.evaluator = evaluator
        self.conflict_monitor = ConflictMonitor()
        self.plans: dict[str, Plan] = {}

    # -- planning (dorsolateral) -----------------------------------------------------
    def plan(self, goal: str, *, mission: dict | None = None,
             correlation_id: str = "", project_id: str = "") -> Plan:
        """Build a working plan. Uses the mission planner's decomposition when
        a mission exists; otherwise a structured default decomposition.
        Hippocampus recall runs first so prior experience shapes the plan."""
        prior = []
        if self.hippocampus is not None:
            try:
                recall = self.hippocampus.recall(
                    goal, kinds={"episodic", "procedural", "project"},
                    project_id=project_id, limit=4)
                prior = [e.text for e in recall.entries]
            except Exception:
                prior = []

        steps: list[PlanStep] = []
        if mission is not None and self.mission_planner is not None:
            try:
                for node in self.mission_planner.initial_plan(mission):
                    steps.append(PlanStep(
                        title=str(node.get("title") or node.get("instruction") or "step"),
                        instruction=str(node.get("instruction") or ""),
                        kind=str(node.get("kind") or "task")))
            except Exception:
                steps = []
        if not steps:
            steps = self._default_steps(goal)

        plan = Plan(goal=goal, steps=steps,
                    mission_id=str((mission or {}).get("id") or ""))
        if prior:
            plan.assumptions.append(
                f"{len(prior)} prior memories informed this plan")
            plan.confidence = min(0.85, 0.6 + 0.05 * len(prior))
        self.plans[plan.plan_id] = plan
        self.publish(EventType.PLAN, {
            "goal": goal[:200], "steps": len(steps),
            "plan_id": plan.plan_id, "confidence": plan.confidence,
            "prior_memories": len(prior),
        }, correlation_id=correlation_id, mission_id=plan.mission_id)
        return plan

    def _default_steps(self, goal: str) -> list[PlanStep]:
        return [
            PlanStep("Gather context", f"Collect relevant files/facts for: {goal}", "research"),
            PlanStep("Execute", goal, "task"),
            PlanStep("Verify", f"Verify the outcome of: {goal}", "verify"),
        ]

    # -- replanning -------------------------------------------------------------------
    def replan(self, plan_id: str, *, failed_step: int | None = None,
               reason: str = "", correlation_id: str = "") -> Plan | None:
        plan = self.plans.get(plan_id)
        if plan is None:
            return None
        if failed_step is not None and 0 <= failed_step < len(plan.steps):
            step = plan.steps[failed_step]
            step.status = "failed"
            step.attempts += 1
            step.result = reason[:400]
            # Dorsolateral recovery: split the failed step into investigate
            # → retry-with-change → verify rather than blind retry.
            recovery = [
                PlanStep(f"Diagnose: {step.title}", reason or step.instruction, "research"),
                PlanStep(f"Retry (modified): {step.title}", step.instruction, "task"),
                PlanStep(f"Verify: {step.title}", "", "verify"),
            ]
            plan.steps[failed_step + 1:failed_step + 1] = recovery
        plan.confidence = max(0.2, plan.confidence - 0.15)
        self.publish(EventType.PLAN, {
            "plan_id": plan.plan_id, "replan": True, "reason": reason[:200],
            "steps": len(plan.steps), "confidence": plan.confidence,
        }, correlation_id=correlation_id, mission_id=plan.mission_id)
        return plan

    # -- strategy evaluation (orbitofrontal) -------------------------------------------
    def evaluate_strategy(self, strategy: str, *, cost: float = 0.0,
                          latency_ms: float = 0.0, risk: float = 0.0,
                          expected_quality: float = 0.5) -> float:
        """Score a strategy 0..1 — high quality cheap fast safe wins."""
        score = (0.45 * expected_quality
                 + 0.20 * (1.0 - min(1.0, risk))
                 + 0.20 * (1.0 - min(1.0, cost))
                 + 0.15 * (1.0 - min(1.0, latency_ms / 30000.0)))
        return round(score, 3)

    # -- completion assessment ---------------------------------------------------------
    def assess_completion(self, plan_id: str) -> dict[str, Any]:
        plan = self.plans.get(plan_id)
        if plan is None:
            return {"complete": False, "reason": "unknown_plan"}
        done = sum(1 for s in plan.steps if s.status == "done")
        failed = sum(1 for s in plan.steps if s.status == "failed")
        pending = sum(1 for s in plan.steps if s.status in {"pending", "running"})
        complete = pending == 0 and failed == 0
        return {
            "complete": complete,
            "plan_id": plan.plan_id, "steps_done": done,
            "steps_failed": failed, "steps_pending": pending,
            "confidence": plan.confidence,
            "unresolved_assumptions": list(plan.assumptions),
        }

    def record_outcome(self, action: str, ok: bool, outcome: str = "",
                       *, correlation_id: str = "", mission_id: str = "") -> None:
        """Feed execution outcomes into the conflict monitor; a conflict is
        published so the PFC/replanning path can react."""
        for conflict in self.conflict_monitor.observe(action, ok, outcome):
            self.publish(EventType.CONFLICT_DETECTED, conflict,
                         correlation_id=correlation_id, mission_id=mission_id,
                         priority=Priority.HIGH)

    # -- status -------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        base = super().status()
        base["plans"] = {pid: {"goal": p.goal[:80], "confidence": p.confidence,
                               "steps": len(p.steps),
                               "done": sum(1 for s in p.steps if s.status == "done")}
                         for pid, p in list(self.plans.items())[-10:]}
        return base
