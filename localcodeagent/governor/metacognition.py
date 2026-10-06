"""Metacognitive assessment — what Nexus knows before answering.

Consumes only existing signals (web-research policy, knowledge memory,
answer memory, last-session evidence, think-mode override) and produces
a structured read of the turn: what is known, what is uncertain, how
much stake the answer carries, and which cognitive operations would
help. Never a model call — this is the cheap pre-flight the Governor
and CognitiveScheduler schedule against.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Knowledge states
KNOWN = "known"
PARTIAL = "partial"
UNKNOWN = "unknown"
STALE = "stale"
CONFLICTED = "conflicted"

_STAKES_RE = re.compile(
    r"(?:\bmedical\b|\bdiagnos\w+\b|\bsymptom\w*\b|\bdosage\b|\bmedication\b|"
    r"\blegal\b|\blawyer\b|\bcontract\b|\bsue\b|\bliabilit\w+\b|\bcompliance\b|"
    r"\bfinancial\b|\binvest\w+\b|\btax\w*\b|\bmortgage\b|\bloan\b|"
    r"\bdelete\b|\berase\b|\bwipe\b|\bformat\b|\bdestroy\b|\birreversible\b|"
    r"\bcredential\w*\b|\bpassword\b|\bproduction\b|\bdeploy\b.*\bprod\b)",
    re.I,
)


@dataclass
class MetaAssessment:
    knowledge_state: str = UNKNOWN
    confidence: float = 0.3          # prior: a direct answer would be right
    uncertainty_sources: list[str] = field(default_factory=list)
    novelty: float = 0.5
    stakes: str = "normal"           # low|normal|high
    recommended_ops: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    think_mode: str = "auto"

    def as_dict(self) -> dict[str, Any]:
        return {
            "knowledge_state": self.knowledge_state,
            "confidence": round(self.confidence, 3),
            "uncertainty_sources": self.uncertainty_sources,
            "novelty": round(self.novelty, 3),
            "stakes": self.stakes,
            "recommended_ops": self.recommended_ops,
            "reasons": self.reasons,
            "think_mode": self.think_mode,
        }


def assess(
    text: str,
    *,
    policy=None,
    knowledge_hit: bool = False,
    answer_memory_hit: bool = False,
    last_evidence: dict | None = None,
    think_mode: str = "auto",
    prior_uncertain: bool = False,
) -> MetaAssessment:
    """Structured pre-answer assessment from existing signals only."""
    m = MetaAssessment(think_mode=think_mode)
    level = str(getattr(policy, "level", "") or "")
    is_current = bool(getattr(policy, "is_current", False))
    is_error = bool(getattr(policy, "is_error", False))
    has_url = bool(getattr(policy, "has_url", False))
    is_coding = bool(getattr(policy, "is_coding", False))

    # -- what we already know -------------------------------------------
    if answer_memory_hit:
        m.knowledge_state = KNOWN
        m.confidence = 0.9
        m.reasons.append("verified answer memory hit")
    elif knowledge_hit:
        m.knowledge_state = KNOWN if not is_current else STALE
        m.confidence = 0.8 if not is_current else 0.45
        m.reasons.append("trusted knowledge record exists" if not is_current
                         else "knowledge exists but the question is volatile")
    elif last_evidence:
        conf = str(last_evidence.get("confidence") or "")
        if conf in ("high", "moderate"):
            m.knowledge_state = PARTIAL
            m.confidence = 0.6
            m.reasons.append("related research evidence on hand")
        elif conf == "conflicted":
            m.knowledge_state = CONFLICTED
            m.confidence = 0.25
            m.uncertainty_sources.append("conflicting evidence")
            m.reasons.append("prior evidence conflicts")
        else:
            m.knowledge_state = UNKNOWN
            m.confidence = 0.3
    else:
        m.knowledge_state = UNKNOWN
        m.confidence = 0.35 if is_coding else 0.45
        m.reasons.append("no trusted local record")

    # -- uncertainty sources --------------------------------------------
    if is_current:
        m.uncertainty_sources.append("volatile/current information")
        m.confidence = min(m.confidence, 0.35)
    if is_error:
        m.uncertainty_sources.append("error signature — diagnosis needed")
    if prior_uncertain:
        m.uncertainty_sources.append("previous uncertainty on this topic")
        m.confidence = min(m.confidence, 0.5)
    if level == "web_required" and "explicit/volatile ask" not in m.uncertainty_sources:
        m.uncertainty_sources.append("explicit or volatile ask")
    if _STAKES_RE.search(text or ""):
        m.stakes = "high"
        m.uncertainty_sources.append("high-stakes domain")
        m.confidence = min(m.confidence, 0.4)

    # -- novelty --------------------------------------------------------
    # Cheap signature: coding/error/volatile topics without local memory
    # are the least charted territory for a small-model chat lane.
    if m.knowledge_state in (UNKNOWN, STALE, CONFLICTED):
        m.novelty = 0.8 if (is_error or is_current) else 0.6
    else:
        m.novelty = 0.2

    # -- recommended operations, cheapest-first --------------------------
    ops: list[str] = ["retrieve_memory"]
    if m.knowledge_state not in (KNOWN,):
        if level in ("web_required", "web_recommended") or is_current or has_url:
            ops.append("search_web")
        elif level == "web_optional":
            ops.append("search_web")
    ops.append("ask_fast_model")
    if is_error:
        ops.append("generate_hypotheses")
    if m.stakes == "high" or m.knowledge_state == CONFLICTED:
        ops.append("run_verifier")
        ops.append("ask_critic")
    if m.confidence < 0.35 and m.stakes != "low":
        ops.append("ask_deep_model")
    if m.confidence < 0.2:
        ops.append("request_user_clarification")
    ops.append("stop_reasoning")
    # /think modulation: fast strips everything past the first cheap
    # answer; deep/exhaustive keep the full ladder.
    if think_mode == "fast":
        ops = [o for o in ops if o in (
            "retrieve_memory", "ask_fast_model", "stop_reasoning")]
        if "search_web" in ops or level == "web_required":
            ops.insert(-1, "search_web")
        m.reasons.append("think=fast — minimal ladder")
    elif think_mode in ("deep", "exhaustive"):
        for extra in ("ask_critic", "run_verifier", "ask_deep_model"):
            if extra not in ops:
                ops.insert(-1, extra)
        m.reasons.append(f"think={think_mode} — extended ladder")
    m.recommended_ops = ops
    return m
