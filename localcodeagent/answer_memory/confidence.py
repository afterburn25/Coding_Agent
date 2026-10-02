"""Trust states and confidence rules for stored answers."""

from __future__ import annotations

TRUST_ORDER = {
    "observed": 0,
    "candidate": 1,
    "trusted": 2,
    "verified": 3,
    "stale": -1,
    "invalidated": -2,
}

# Only these states may fully bypass model inference.
BYPASS_STATES = frozenset({"trusted", "verified"})

# States that may contribute context to a smaller model without bypassing it.
CONTEXT_STATES = frozenset({"trusted", "verified", "candidate"})

# Repeated-observation promotion ladder.
PROMOTE_OCCURRENCES_CANDIDATE = 3
PROMOTE_OCCURRENCES_TRUSTED = 5


def usable(row: dict) -> bool:
    return (
        not row.get("invalidated")
        and row.get("trust_state") not in {"stale", "invalidated"}
    )


def may_bypass(row: dict) -> bool:
    return usable(row) and row.get("trust_state") in BYPASS_STATES


def promoted_state(occurrences: int, positive_feedback: bool = False) -> str | None:
    """Next earned trust state from repetition, or None."""
    if occurrences >= PROMOTE_OCCURRENCES_TRUSTED:
        return "trusted"
    if occurrences >= PROMOTE_OCCURRENCES_CANDIDATE:
        return "trusted" if positive_feedback else "candidate"
    if occurrences >= 2 and positive_feedback:
        return "candidate"
    return None


def rank(state: str) -> int:
    return TRUST_ORDER.get(state, 0)


def confidence_for(state: str, base: float = 0.5) -> float:
    return {
        "verified": 0.99,
        "trusted": 0.9,
        "candidate": 0.65,
        "observed": 0.4,
        "stale": 0.25,
        "invalidated": 0.0,
    }.get(state, base)
