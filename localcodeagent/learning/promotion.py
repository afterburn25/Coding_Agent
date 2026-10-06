"""Knowledge promotion policy (Part 4).

Decides which promotion state a piece of knowledge earns, which memory
classes it may enter, its freshness handling, and whether it is eligible
for training data — from evidence, not from model confidence.

Anti-contamination rule (Part 4): model-generated text is NEVER trusted
memory. ``source_type=SOURCE_MODEL`` with only ``model_asserted``
verification can reach at most RAW, regardless of how confident the model
sounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import taxonomy as t


@dataclass
class PromotionDecision:
    state: str
    memory_classes: list[str] = field(default_factory=list)
    freshness_class: str = "moderate"
    training_eligible: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "memory_classes": list(self.memory_classes),
            "freshness_class": self.freshness_class,
            "training_eligible": self.training_eligible,
            "reasons": list(self.reasons),
        }


# Source trust tiers, mirroring the Source Trust Registry's scale: a
# trust score in [0,1] where primary/official score high and community
# scores low. Callers may pass either a score or a tier name.
_TRUST_TIER = {
    "unknown": 0.2, "untrusted": 0.1, "community": 0.35,
    "secondary": 0.55, "official": 0.85, "primary": 0.95,
    "runtime": 1.0, "user_scoped": 0.7,
}


def _trust_value(source_trust) -> float:
    if isinstance(source_trust, (int, float)):
        return max(0.0, min(1.0, float(source_trust)))
    return _TRUST_TIER.get(str(source_trust or "unknown").lower(), 0.2)


class KnowledgePromotionPolicy:
    """Deterministic promotion rules. Tunable thresholds, no model calls."""

    def __init__(
        self,
        *,
        supported_min_trust: float = 0.4,
        supported_min_corroboration: int = 1,
        verified_min_rank: int = t.verification_rank(t.VERIFY_AUTHORITATIVE),
        verified_min_confidence: float = 0.6,
        trusted_min_confirmations: int = 2,
    ) -> None:
        self.supported_min_trust = float(supported_min_trust)
        self.supported_min_corroboration = int(supported_min_corroboration)
        self.verified_min_rank = int(verified_min_rank)
        self.verified_min_confidence = float(verified_min_confidence)
        self.trusted_min_confirmations = int(trusted_min_confirmations)

    def decide(
        self,
        *,
        source_type: str = t.SOURCE_MODEL,
        source_trust=0.2,
        verification: str = t.VERIFY_NONE,
        confidence: float = 0.0,
        corroboration: int = 0,
        conflict: bool = False,
        user_approved: bool = False,
        outcome: str = "",          # "", "success", "failure"
        confirmations: int = 0,     # prior independent verifications
        freshness_class: str = "moderate",
        memory_classes: list[str] | None = None,
    ) -> PromotionDecision:
        reasons: list[str] = []
        trust = _trust_value(source_trust)
        rank = t.verification_rank(verification)
        classes = list(memory_classes or [t.SEMANTIC])

        # Contradiction blocks promotion past CANDIDATE and forces the
        # CONFLICTED state — history and both sides are kept.
        if conflict:
            return PromotionDecision(
                state=t.CONFLICTED, memory_classes=classes,
                freshness_class=freshness_class,
                reasons=["credible evidence conflicts; promotion blocked "
                         "pending resolution"])

        # Hard ceiling for unverified model output (anti-contamination).
        if source_type == t.SOURCE_MODEL and rank <= 0:
            return PromotionDecision(
                state=t.RAW, memory_classes=classes,
                freshness_class=freshness_class,
                reasons=["unverified model output stays raw"])

        # VERIFIED — an acceptable verification method actually ran.
        if rank >= self.verified_min_rank and confidence >= self.verified_min_confidence:
            state = t.VERIFIED
            reasons.append(f"verified via {verification}")
            # TRUSTED — verified repeatedly, or a verified record the user
            # explicitly approved.
            if (confirmations >= self.trusted_min_confirmations
                    or (user_approved and rank >= self.verified_min_rank)):
                state = t.TRUSTED
                reasons.append("promoted to trusted: repeated verification"
                               if confirmations >= self.trusted_min_confirmations
                               else "promoted to trusted: user-approved verified record")
            return PromotionDecision(
                state=state, memory_classes=classes,
                freshness_class=freshness_class,
                training_eligible=state in t.TRAINING_ELIGIBLE_STATES,
                reasons=reasons)

        # SUPPORTED — decent-source corroboration without executable or
        # authoritative verification.
        if (trust >= self.supported_min_trust
                and corroboration >= self.supported_min_corroboration
                and rank >= t.verification_rank(t.VERIFY_SINGLE_SOURCE)):
            return PromotionDecision(
                state=t.SUPPORTED, memory_classes=classes,
                freshness_class=freshness_class,
                reasons=["corroborated by trusted source(s)"])

        # CANDIDATE — some signal exists but not enough evidence.
        if source_type != t.SOURCE_MODEL or corroboration > 0 or trust >= 0.5:
            return PromotionDecision(
                state=t.CANDIDATE, memory_classes=classes,
                freshness_class=freshness_class,
                reasons=["plausible but evidence below promotion threshold"])

        return PromotionDecision(
            state=t.RAW, memory_classes=classes,
            freshness_class=freshness_class,
            reasons=["no acceptable evidence"])
