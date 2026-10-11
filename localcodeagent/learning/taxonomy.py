"""Memory taxonomy for continual learning.

These are logical memory CLASSES — views and tags over the existing
stores (KnowledgeMemory, AnswerMemory, DecisionJournal, EvidenceBoard,
procedures, ModelGrowthLab), not six new databases. Every record that
enters long-term memory carries a class + a promotion state so the
LearningGovernor can route, gate, and consolidate consistently.
"""

from __future__ import annotations

# --- Memory classes (Part 2) -------------------------------------------------

SEMANTIC = "semantic"        # facts and concepts ("Python 3.13 match …")
EPISODIC = "episodic"        # what happened during a specific event
PROCEDURAL = "procedural"    # how to accomplish something
EVIDENCE = "evidence"        # why a belief exists (source refs, tests)
PREFERENCE = "preference"    # user/project preferences
WORKING = "working"          # current goal, constraints, open issues
TRAINING = "training"        # verified examples eligible for offline training

MEMORY_CLASSES = (
    SEMANTIC, EPISODIC, PROCEDURAL, EVIDENCE, PREFERENCE, WORKING, TRAINING,
)

# Which existing stores act as the canonical home for each class. The
# governor READS across stores; it does not copy records into a second
# database. Lessons/procedures/competencies live in learning/*.json.
STORE_FOR_CLASS = {
    SEMANTIC: "knowledge_memory",
    EPISODIC: "decisions",          # + answer_memory incidents, task ledger
    PROCEDURAL: "procedures",       # learning/procedures.json
    EVIDENCE: "evidence",           # EvidenceBoard
    PREFERENCE: "preferences",
    WORKING: "tasks",               # live ledger only — never persisted far
    TRAINING: "model_growth",       # ModelGrowthLab candidates
}

# --- Promotion states (Part 3) ----------------------------------------------

RAW = "raw"                  # model chatter, community comment, hypothesis
CANDIDATE = "candidate"      # plausible, not yet checked
SUPPORTED = "supported"      # corroborated or backed by decent evidence
VERIFIED = "verified"        # test passed / authoritative source / measured
TRUSTED = "trusted"          # verified repeatedly or user-confirmed verified
CONFLICTED = "conflicted"    # credible evidence disagrees — never silently
                             # resolved or erased
SUPERSEDED = "superseded"    # was true, newer evidence replaced it (history
                             # retained — not deleted)
EXPIRED = "expired"          # past validity window; excluded from retrieval
                             # pending revalidation

PROMOTION_STATES = (
    RAW, CANDIDATE, SUPPORTED, VERIFIED, TRUSTED,
    CONFLICTED, SUPERSEDED, EXPIRED,
)

# States allowed to surface in answers/knowledge context.
RETRIEVABLE_STATES = frozenset({SUPPORTED, VERIFIED, TRUSTED, CONFLICTED})

# States from which offline-training candidates may be drawn. RAW model
# output must never reach TRAINING memory.
TRAINING_ELIGIBLE_STATES = frozenset({VERIFIED, TRUSTED})

# --- Source types the promotion policy understands ---------------------------

SOURCE_MODEL = "model"               # anything the model asserted
SOURCE_COMMUNITY = "community"       # forums, comments, unverified users
SOURCE_WEB = "web"                   # generic web page
SOURCE_OFFICIAL = "official"         # official docs/vendor/primary source
SOURCE_PRIMARY = "primary"           # standards/source code/first-party data
SOURCE_TEST = "test"                 # passing test / executed verification
SOURCE_MEASUREMENT = "measurement"   # runtime observation / benchmark
SOURCE_USER = "user"                 # user statement (context-scoped)
SOURCE_TOOL = "tool"                 # tool/tool-chain output

SOURCE_TYPES = (
    SOURCE_MODEL, SOURCE_COMMUNITY, SOURCE_WEB, SOURCE_OFFICIAL,
    SOURCE_PRIMARY, SOURCE_TEST, SOURCE_MEASUREMENT, SOURCE_USER,
    SOURCE_TOOL,
)

# Verification methods, weakest → strongest.
VERIFY_NONE = "none"
VERIFY_MODEL_ASSERTED = "model_asserted"   # the model just said so
VERIFY_SINGLE_SOURCE = "single_source"
VERIFY_MULTI_SOURCE = "multi_source"       # independent strong sources
VERIFY_AUTHORITATIVE = "authoritative"     # primary/official confirmation
VERIFY_USER_CONFIRMED = "user_confirmed"   # scoped to that user/context
VERIFY_MEASURED = "measured"               # benchmark/runtime observation
VERIFY_TESTED = "tested"                   # executable check passed

_VERIFICATION_RANK = {
    VERIFY_NONE: 0, VERIFY_MODEL_ASSERTED: 0, VERIFY_SINGLE_SOURCE: 1,
    VERIFY_MULTI_SOURCE: 2, VERIFY_AUTHORITATIVE: 3,
    VERIFY_USER_CONFIRMED: 3, VERIFY_MEASURED: 4, VERIFY_TESTED: 5,
}


def verification_rank(method: str) -> int:
    return _VERIFICATION_RANK.get(method, 0)
