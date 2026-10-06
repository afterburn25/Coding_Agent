"""Intelligence Governor — metacognition + cognitive scheduling.

The Governor reads existing signals (web policy, memory hits, evidence,
think mode) and emits a typed IntelPlan: what Nexus knows, what she's
uncertain about, and the cheapest sufficient ladder of cognitive
operations. No models, no prose — typed assessment and plans only.
"""
from .metacognition import (
    CONFLICTED, KNOWN, PARTIAL, STALE, UNKNOWN, MetaAssessment, assess)
from .scheduler import (
    CognitiveOperation, CognitiveScheduler, ComputeBudget, OP_PROFILES)
from .governor import ACTIVITY_LABELS, IntelPlan, IntelligenceGovernor

__all__ = [
    "CONFLICTED", "KNOWN", "PARTIAL", "STALE", "UNKNOWN",
    "MetaAssessment", "assess",
    "CognitiveOperation", "CognitiveScheduler", "ComputeBudget",
    "OP_PROFILES", "ACTIVITY_LABELS", "IntelPlan", "IntelligenceGovernor",
]
