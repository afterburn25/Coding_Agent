"""Training-candidate quality gate (Parts 39–45).

Wraps ModelGrowthLab.collect() so only evidence-backed experience becomes
training data. Every row gets a quality grade + provenance; dataset
export defaults to high quality only.

Grades: GOLD (trusted, repeatedly verified) > VERIFIED (one solid
verification) > REVIEWED (plausible, human-approved) > WEAK > REJECTED.
RAW model chatter NEVER enters — that's the answer-memory contamination
rule applied to training data.
"""

from __future__ import annotations

from . import taxonomy as t
from .promotion import KnowledgePromotionPolicy

QUALITY_GOLD = "gold"
QUALITY_VERIFIED = "verified"
QUALITY_REVIEWED = "reviewed"
QUALITY_WEAK = "weak"
QUALITY_REJECTED = "rejected"

_TRAIN_BY_DEFAULT = (QUALITY_GOLD, QUALITY_VERIFIED)


class TrainingCandidateGate:
    def __init__(self, model_growth, *,
                 policy: KnowledgePromotionPolicy | None = None) -> None:
        self.growth = model_growth
        self.policy = policy or KnowledgePromotionPolicy()

    def _quality_for(self, decision_state: str) -> str:
        return {
            t.TRUSTED: QUALITY_GOLD,
            t.VERIFIED: QUALITY_VERIFIED,
            t.SUPPORTED: QUALITY_REVIEWED,
            t.CANDIDATE: QUALITY_WEAK,
        }.get(decision_state, QUALITY_REJECTED)

    def submit(
        self,
        *,
        instruction: str,
        response: str,
        kind: str = "behavior",
        source_type: str = t.SOURCE_MODEL,
        source_trust=0.2,
        verification: str = t.VERIFY_NONE,
        confidence: float = 0.5,
        corroboration: int = 0,
        conflict: bool = False,
        provenance: dict | None = None,
        negative_example: bool = False,
    ) -> dict | None:
        """Gate one candidate row. Returns the collected item or None
        when the evidence floor isn't met."""
        decision = self.policy.decide(
            source_type=source_type, source_trust=source_trust,
            verification=verification, confidence=confidence,
            corroboration=corroboration, conflict=conflict)
        quality = self._quality_for(decision.state)
        # Negative examples (preferred vs. rejected behavior — Part 40)
        # may come from a VERIFIED failure even though the "response" is
        # the corrected behavior; they still need evidence.
        if quality == QUALITY_REJECTED and not negative_example:
            return None
        if quality == QUALITY_REJECTED and negative_example:
            quality = QUALITY_WEAK
        meta = {
            "quality": quality,
            "promotion_state": decision.state,
            "decision_reasons": decision.reasons,
            "provenance": dict(provenance or {}),
            "negative_example": bool(negative_example),
        }
        # Auto-approve only evidence-backed rows; weaker ones queue for
        # human review via ModelGrowthLab's pending state.
        return self.growth.collect(
            kind=kind, instruction=instruction, response=response,
            source="learning_gate",
            metadata=meta,
            auto_approved=quality in _TRAIN_BY_DEFAULT)

    def exportable(self, *, include_weak: bool = False,
                   limit: int = 2000) -> list[dict]:
        """Training-eligible candidates with provenance attached."""
        allowed = set(_TRAIN_BY_DEFAULT)
        if include_weak:
            allowed |= {QUALITY_REVIEWED, QUALITY_WEAK}
        rows = []
        for c in self.growth.candidates(limit=5000):
            q = ((c.get("metadata") or {}).get("quality"))
            if q in allowed or (q is None and c.get("status") == "approved"
                                and QUALITY_REVIEWED in allowed):
                rows.append(c)
            if len(rows) >= limit:
                break
        return rows
