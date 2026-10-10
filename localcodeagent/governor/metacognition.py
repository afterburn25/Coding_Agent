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


# Reasoning strategies — the problem-class a turn wants, chosen before
# the op ladder is built. Deterministic classification only: the router
# reads the whole utterance (question shape, modal verbs, policy flags)
# and emits a primary strategy plus composable secondaries — complex
# problems legitimately need more than one (diagnose → causal →
# experiment), so this is never forced into a single label.
STRATEGY_DIRECT = "direct_retrieval"
STRATEGIES = (
    STRATEGY_DIRECT, "deductive", "inductive", "causal", "debugging",
    "diagnostic", "counterfactual", "optimization", "constraint_solving",
    "analogy", "experimental", "comparative", "planning", "research",
    "simulation", "decision_analysis", "multi_agent_review",
    "formal_math",
)

# Strategy → extra cognitive ops the ladder should include. These are
# *additions* to the knowledge-state ladder, not replacements — a
# diagnostic turn still gets retrieve_memory and an answer path.
STRATEGY_OPS: dict[str, list[str]] = {
    "diagnostic": ["generate_hypotheses", "test_hypothesis"],
    "debugging": ["generate_hypotheses", "test_hypothesis"],
    "causal": ["generate_hypotheses", "test_hypothesis"],
    "counterfactual": ["simulate_plan"],
    "optimization": ["run_formal_solver"],
    "constraint_solving": ["run_formal_solver"],
    "formal_math": ["run_formal_solver"],
    "experimental": ["generate_hypotheses", "run_experiment"],
    "comparative": ["run_verifier"],
    "decision_analysis": ["run_verifier", "ask_critic"],
    "multi_agent_review": ["ask_critic", "spawn_specialist"],
    "simulation": ["simulate_plan"],
    "analogy": ["retrieve_memory"],
    "planning": ["inspect_repository"],
    "research": ["search_web"],
}

_MATH_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*[×x*/+\-^]\s*\d+(?:\.\d+)?\b|"
    r"\b(?:calculate|compute|solve|simplify|factor|integral|derivative)\b|"
    r"\bsqrt\b|\b(?:square root|modulo|percent of)\b)", re.I)
_COUNTERFACT_RE = re.compile(
    r"\b(?:what (?:would|will|happens?|might|could) happen|what if|"
    r"happens if|difference if|trade.?off|instead of)\b", re.I)
_OPT_RE = re.compile(
    r"\b(?:optimi[sz]e|allocat\w+|schedul\w+|maximi[sz]e|minimi[sz]e|"
    r"fit (?:all|both|these|\w+ models?)|pack|budget of|within \d+|"
    r"\bhow (?:can|should) we (?:fit|pack|schedule|allocate))\b", re.I)
_COMPARE_RE = re.compile(
    r"\b(?:should (?:i|we|it)\b.{0,60}\bor\b|versus| vs\.? |"
    r"which (?:is |would be |option )?(?:better|best|right)|"
    r"keep \w+ or (?:switch|move)|pros and cons|trade.?offs? between|"
    r"worth (?:it|switching|upgrading))\b", re.I)
_DECISION_RE = re.compile(
    r"\b(?:should (?:i|we)\b|decide|decision|adopt|migrate to|"
    r"move (?:this|the|\w+) (?:to|onto)|choose (?:between|whether))\b",
    re.I)
_EXPERIMENT_RE = re.compile(
    r"\b(?:experiment|a/?b test|benchmark (?:it|this|whether)|"
    r"test whether|measure (?:the|if|whether)|controlled test)\b", re.I)
_SIM_RE = re.compile(r"\b(?:simulat\w+|dry.?run|rehearse|model the)\b",
                   re.I)
_ANALOGY_RE = re.compile(
    r"\b(?:similar to (?:the|that|when)|like (?:the|that) (?:bug|issue|"
    r"problem|time)|same (?:pattern|structure|shape) as|reminds? us)\b",
    re.I)
_PLAN_RE = re.compile(
    r"\b(?:plan (?:for|to|out)|roadmap|step.?by.?step (?:plan|approach)|"
    r"how (?:should|do|can) (?:we|i) (?:approach|build|implement|do)|"
    r"break (?:this|it) (?:down|into)|strategy for)\b", re.I)
_RESEARCH_RE = re.compile(
    r"\b(?:research|look (?:it|this|that)? ?up|search (?:for|the web)|"
    r"find out|what'?s the latest|current (?:best|recommended|version)|"
    r"latest (?:version|release|news))\b", re.I)
_REVIEW_RE = re.compile(
    r"\b(?:review (?:this|my|the)|second opinion|sanity.?check|"
    r"get (?:a|another) (?:review|opinion)|cross.?check)\b", re.I)
_QUESTIONISH_RE = re.compile(
    r"^(?:what|who|where|when|which|how (?:much|many|old|long))\b"
    r".{0,60}\??\s*$", re.I)


def classify_strategy(text: str, *, is_error: bool = False,
                      is_current: bool = False,
                      is_coding: bool = False
                      ) -> tuple[str, list[str], float, str]:
    """Reasoning Strategy Router — which method fits this problem.

    Returns (primary, secondaries, confidence, reason). Order matters:
    more specific problem shapes win over generic ones; a single short
    factual question stays ``direct_retrieval`` so simple asks never pay
    planning overhead.
    """
    t = str(text or "")
    low = t.lower()
    picks: list[tuple[str, float, str]] = []

    if _MATH_RE.search(t) and not is_error:
        picks.append(("formal_math", 0.9, "exact math — compute, don't guess"))
    if is_error or re.search(
            r"\b(?:crash|error|fail\w*|bug|broken|traceback|exception|"
            r"why does|why (?:is|isn't|won't)|keeps? (?:crash|fail|"
            r"freez|dying)|not working|doesn'?t work|regression)\b",
            low):
        picks.append(("diagnostic", 0.8 if is_error else 0.65,
                      "failure/why-shaped problem"))
        picks.append(("causal", 0.6, "needs mechanism, not just symptom"))
    if _EXPERIMENT_RE.search(low):
        picks.append(("experimental", 0.7, "explicit test/measure ask"))
    if _COUNTERFACT_RE.search(low):
        picks.append(("counterfactual", 0.7,
                      "what-if phrasing — predict before measuring"))
    if _OPT_RE.search(low):
        picks.append(("optimization", 0.7, "resource/limit language"))
        if re.search(r"\b\d+\s*(?:gb|mb|vram|ram|deadline|limit)\b", low):
            picks.append(("constraint_solving", 0.65,
                          "explicit numeric constraints"))
    if _SIM_RE.search(low):
        picks.append(("simulation", 0.65, "explicit simulation ask"))
    if _ANALOGY_RE.search(low):
        picks.append(("analogy", 0.6, "references a similar prior problem"))
    if _COMPARE_RE.search(low):
        picks.append(("comparative", 0.7, "either/or comparison"))
    if _DECISION_RE.search(low):
        picks.append(("decision_analysis", 0.65, "a choice is being made"))
    if _PLAN_RE.search(low) or (is_coding and re.search(
            r"\b(?:implement|build|add|refactor|migrate|create)\b", low)):
        picks.append(("planning", 0.6, "structured multi-step work"))
    if _REVIEW_RE.search(low):
        picks.append(("multi_agent_review", 0.6, "explicit review ask"))
    if is_current or _RESEARCH_RE.search(low):
        picks.append(("research", 0.7, "fresh/external information needed"))
    if re.search(r"\b(?:explain|why is|how does|what causes)\b", low) \
            and not any(p[0] == "diagnostic" for p in picks):
        picks.append(("inductive", 0.5, "explanation from observations"))

    if not picks:
        # A short single-clause factual/question ask: direct retrieval —
        # simple questions must not pay planning overhead.
        return STRATEGY_DIRECT, [], 0.8, "simple direct ask"

    # Primary = highest-scoring; secondaries = the rest, deduped and
    # ordered — composable reasoning, never a forced single label.
    seen: set[str] = set()
    ordered: list[tuple[str, float, str]] = []
    for p in sorted(picks, key=lambda x: -x[1]):
        if p[0] not in seen:
            seen.add(p[0])
            ordered.append(p)
    primary, conf, reason = ordered[0]
    secondaries = [p[0] for p in ordered[1:]]
    return primary, secondaries, conf, reason


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
    # Reasoning Strategy Router output — the problem class this turn
    # wants solved (primary) plus composable secondary strategies.
    strategy: str = STRATEGY_DIRECT
    strategies: list[str] = field(default_factory=list)
    strategy_confidence: float = 0.0

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
            "strategy": self.strategy,
            "strategies": self.strategies,
            "strategy_confidence": round(self.strategy_confidence, 3),
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

    # -- reasoning strategy router ---------------------------------------
    # Classify the problem class before building the op ladder — the
    # strategy decides *how* to reason, the ops are its instruments.
    primary, secondaries, sconf, sreason = classify_strategy(
        text or "", is_error=is_error, is_current=is_current,
        is_coding=is_coding)
    m.strategy = primary
    m.strategies = secondaries
    m.strategy_confidence = sconf
    if primary != STRATEGY_DIRECT:
        m.reasons.append(f"strategy: {primary} ({sreason})")

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
    # Strategy-implied ops — the router's instruments ride the same
    # ladder, deduped and kept ahead of the model-answer fallbacks.
    for strat in [primary] + secondaries:
        for sop in STRATEGY_OPS.get(strat, ()):
            if sop not in ops:
                ops.append(sop)
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
