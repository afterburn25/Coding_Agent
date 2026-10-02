"""Basal Ganglia — action selection.

The PFC decides WHAT should happen; the Basal Ganglia picks WHICH action
next. Candidates are scored on expected usefulness, historical success
(tool telemetry + procedural memory), cost, latency, risk, and resource
readiness. Selected sequences that succeed are eligible for recording into
procedural memory — the beginnings of learned habits.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .events import CognitiveEvent, EventType
from .regions import BrainRegion
from . import events as ev

# Risk/approval classes — destructive or external-side-effect actions pay
# a scoring penalty and may require an approval gate downstream.
RISK_BY_ACTION = {
    "shell": 0.5, "run_shell": 0.5, "edit_file": 0.4, "write_file": 0.4,
    "delete": 0.8, "git_push": 0.6, "git_commit": 0.3,
    "browser": 0.3, "computer_use": 0.5, "mcp": 0.3, "api": 0.3,
    "github": 0.4, "install": 0.5, "model_invoke": 0.2,
    "memory_recall": 0.0, "repo_search": 0.0, "read_file": 0.0,
    "inspect": 0.0, "run_tests": 0.2, "compile": 0.2,
    "pause": 0.0, "escalate": 0.1, "retry": 0.2,
}


@dataclass(slots=True)
class ActionCandidate:
    action: str                     # capability/action name
    arguments: dict[str, Any] = field(default_factory=dict)
    expected_usefulness: float = 0.5
    estimated_cost: float = 0.1     # 0..1 normalized
    estimated_latency_ms: float = 1000.0
    risk: float | None = None       # None → looked up by action name
    requires_approval: bool = False
    tool: str = ""                  # preferred concrete tool if known


@dataclass(slots=True)
class ScoredAction:
    candidate: ActionCandidate
    score: float
    reasons: list[str] = field(default_factory=list)


class BasalGanglia(BrainRegion):
    name = ev.REGION_BASAL_GANGLIA

    def __init__(self, bus, *, hippocampus=None, tool_stats: Callable[[], dict] | None = None,
                 resource_probe: Callable[[], dict] | None = None) -> None:
        super().__init__(bus)
        self.hippocampus = hippocampus
        self._tool_stats = tool_stats or (lambda: {})
        self._resource_probe = resource_probe or (lambda: {})
        # action -> {"wins": int, "calls": int, "avg_ms": float}
        self._history: dict[str, dict[str, float]] = {}

    # -- scoring ---------------------------------------------------------------------
    def _success_rate(self, action: str) -> tuple[float, int]:
        h = self._history.get(action)
        if h and h["calls"] >= 2:
            return h["wins"] / h["calls"], int(h["calls"])
        # Fall back to aggregate tool telemetry.
        try:
            stats = self._tool_stats() or {}
            for row in stats.get("routes", []):
                if row.get("capability") == action or row.get("tool") == action:
                    if row.get("calls", 0) >= 2:
                        return float(row["success_rate"]), int(row["calls"])
        except Exception:
            pass
        return 0.5, 0

    def _procedural_boost(self, action: str, context_text: str = "") -> float:
        if self.hippocampus is None or not context_text:
            return 0.0
        try:
            recall = self.hippocampus.recall(
                context_text, kinds={"procedural"}, limit=3)
            for e in recall.entries:
                if action.lower() in e.text.lower() and e.confidence > 0.6:
                    return 0.10
        except Exception:
            pass
        return 0.0

    def score(self, cand: ActionCandidate, *, context_text: str = "") -> ScoredAction:
        reasons: list[str] = []
        risk = cand.risk if cand.risk is not None else RISK_BY_ACTION.get(cand.action, 0.3)
        rate, calls = self._success_rate(cand.action)
        if calls:
            reasons.append(f"history {rate:.0%} over {calls} calls")
        boost = self._procedural_boost(cand.action, context_text)
        if boost:
            reasons.append("matches a learned procedure")
        score = (
            0.35 * cand.expected_usefulness
            + 0.25 * rate
            + 0.10 * (1.0 - min(1.0, cand.estimated_cost))
            + 0.10 * (1.0 - min(1.0, cand.estimated_latency_ms / 20000.0))
            + 0.15 * (1.0 - min(1.0, risk))
            + boost
        )
        if cand.requires_approval:
            score -= 0.05
            reasons.append("requires approval")
        # Resource pressure: expensive actions lose when VRAM/RAM is tight.
        try:
            snap = self._resource_probe() or {}
            if float(snap.get("free_vram_gb") or 8) < 1.0 and cand.action in {
                    "model_invoke", "computer_use", "image"}:
                score -= 0.2
                reasons.append("VRAM pressure")
        except Exception:
            pass
        return ScoredAction(cand, round(max(0.0, score), 3), reasons)

    def select(self, candidates: list[ActionCandidate], *,
               context_text: str = "", correlation_id: str = "",
               mission_id: str = "") -> ScoredAction | None:
        if not candidates:
            return None
        scored = [self.score(c, context_text=context_text) for c in candidates]
        scored.sort(key=lambda s: -s.score)
        chosen = scored[0]
        self.publish(EventType.ACTION_SELECTION, {
            "action": chosen.candidate.action,
            "tool": chosen.candidate.tool,
            "score": chosen.score,
            "alternatives": [{"action": s.candidate.action, "score": s.score}
                             for s in scored[1:4]],
            "reasons": chosen.reasons,
        }, correlation_id=correlation_id, mission_id=mission_id)
        return chosen

    # -- learning ----------------------------------------------------------------------
    def record_outcome(self, action: str, ok: bool, elapsed_ms: float = 0.0) -> None:
        h = self._history.setdefault(action, {"wins": 0, "calls": 0, "total_ms": 0.0})
        h["calls"] += 1
        h["wins"] += 1 if ok else 0
        h["total_ms"] += elapsed_ms

    def status(self) -> dict[str, Any]:
        base = super().status()
        base["action_stats"] = {
            a: {"calls": int(h["calls"]),
                "success_rate": round(h["wins"] / h["calls"], 3) if h["calls"] else 0}
            for a, h in list(self._history.items())[-25:]}
        return base
