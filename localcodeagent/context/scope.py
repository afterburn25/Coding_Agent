"""Response scope planning — answer exactly what was asked.

Every user turn is classified into a response depth before any answer
is produced. The depth drives three things:

1. The per-turn prompt directive injected beside the intent advisory —
   the model is told the answer-size budget and the reveal rule before
   it writes.
2. Deterministic lanes (identity, clock, git-state, capability) mark
   their SemanticResponse ``bare`` so the genome envelope (micro-
   reactions, acknowledgements, closings) cannot decorate a minimum-
   sufficient answer.
3. QA/metrics — scenarios assert scope behavior instead of exact
   strings.

Core rule: *requested* facts may appear in the answer; *supporting*
facts are used internally and stay hidden unless the user asks for
them; *optional related* facts never appear unless the question is
broad enough to invite them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ResponseDepth(str, Enum):
    EXACT = "exact"                # one direct fact — 1 sentence
    BRIEF = "brief"                # answer + one qualifier — 1-3 sentences
    EXPLANATORY = "explanatory"    # how/why explanation
    DETAILED = "detailed"          # detail explicitly requested
    OPEN_ENDED = "open_ended"      # broad invitation — "tell me about X"


@dataclass
class ResponseScope:
    """What kind of answer the turn called for and what it may reveal."""
    depth: ResponseDepth = ResponseDepth.BRIEF
    # Slots the user explicitly asked for (may appear in the answer).
    requested_slots: tuple[str, ...] = ()
    # Slots needed internally to derive the answer (stay hidden).
    supporting_slots: tuple[str, ...] = ()
    # The user asked HOW the answer was derived — reasoning may be shown.
    reasoning_visible: bool = False
    # The missing referent changes the answer — ask, don't guess.
    needs_clarification: bool = False
    # Answer-size guidance (soft — user wording overrides).
    max_sentences: int = 3
    # A trailing question is natural for this turn.
    allow_trailing_question: bool = False
    # Deterministic exact answers — suppress the persona envelope
    # (micro-reactions, acknowledgement openers, closings, address).
    bare: bool = False

    @property
    def reveal_slots(self) -> tuple[str, ...]:
        """Slots permitted in the final answer — requested by default."""
        return self.requested_slots

    def to_dict(self) -> dict:
        return {
            "depth": self.depth.value,
            "requested_slots": list(self.requested_slots),
            "supporting_slots": list(self.supporting_slots),
            "reasoning_visible": self.reasoning_visible,
            "needs_clarification": self.needs_clarification,
            "max_sentences": self.max_sentences,
            "bare": self.bare,
        }


# ---------------------------------------------------------------------------
# Depth signals — ordered strongest-first; explicit user wording always wins.
# ---------------------------------------------------------------------------

# Explicit depth requests — the user's own words override every default.
_DETAILED_RE = re.compile(
    r"\b(?:in\s+(?:full\s+)?detail|detailed|comprehensive|thorough|"
    r"step[-\s]by[-\s]step|walk\s+me\s+through|full\s+breakdown|"
    r"everything\s+(?:there\s+is\s+to\s+know\s+)?about|"
    r"in[-\s]depth|deep\s+dive|exhaustive|long\s+version)\b", re.I)
_BREVITY_RE = re.compile(
    r"\b(?:just\s+tell\s+me|just\s+answer|yes\s+or\s+no|one\s+word|"
    r"short\s+answer|quick(?:ly)?\s*[—:,-]|tl;?dr|keep\s+it\s+short|"
    r"in\s+one\s+sentence|bottom\s+line)\b", re.I)
_OPEN_ENDED_RE = re.compile(
    r"\b(?:tell\s+me\s+(?:more\s+|everything\s+)?about|"
    r"tell\s+me\s+about\s+(?:yourself|him|her|it|them|this|that)|"
    r"what\s+do\s+you\s+know\s+about|what\s+do\s+you\s+(?:remember|"
    r"recall)\s+about|describe\b|what'?s?\s+(?:he|she|it|they|"
    r"your\s+\w+)\s+like\b|the\s+(?:whole|full)\s+story)\b", re.I)

# Reasoning-visibility — "how did you get that number" makes the
# derivation itself the requested content.
_REASONING_RE = re.compile(
    r"\bhow\s+(?:did|do)\s+you\s+(?:figure|calculate|compute|determine|"
    r"know|find\s+out|derive|work\s+(?:that|it)\s+out|get\s+(?:that|"
    r"this|the)\s+(?:number|answer|result|value)|arrive\s+at)\b"
    r"|\bwhere\s+did\s+you\s+(?:get|learn|read|hear)\s+that\b"
    r"|\bhow\s+do\s+you\s+know\b"
    r"|\bwhy\s+do\s+you\s+(?:think|say|believe)\s+that\b"
    r"|\bwhat\s+(?:makes|made)\s+you\s+(?:think|say|believe)\s+that\b",
    re.I)

# Explanation requests — how/why at natural depth.
_EXPLANATORY_RE = re.compile(
    r"^(?:why|how\s+(?:does|do|did|would|can|could|should|is|are|"
    r"comes?|exactly)|explain|what\s+causes?|what'?s?\s+the\s+"
    r"(?:difference|point|reason|purpose|meaning)|how\s+come|"
    r"what\s+happens\s+(?:if|when))\b", re.I)
_BRIEF_ASK_RE = re.compile(
    r"^(?:what\s+(?:caused|happened|went\s+wrong|was\s+wrong)|"
    r"why\s+(?:didn'?t|did|won'?t|wouldn'?t|isn'?t|aren'?t|wasn'?t)|"
    r"what'?s?\s+wrong|what\s+broke)\b", re.I)
_BRIEFLY_RE = re.compile(r"\b(?:briefly|in\s+brief|summar(?:y|ize)|"
                         r"short\s+version)\b", re.I)

# Exact-fact shapes — one retrievable value answers the whole turn.
_EXACT_RE = re.compile(
    r"^(?:what\s+time|what'?s?\s+the\s+(?:time|date|version|port|"
    r"name|status|path|url|branch|commit|tag|size|path|count|number|"
    r"model|ip)|when\s+(?:is|are|was|were|did|does|will)|"
    r"who\s+(?:is|are|was|were|made|created|built|wrote|designed)|"
    r"how\s+(?:old|much|many|tall|long|far|big|late|early)|"
    r"how\s+old\s+(?:are|r)\s+(?:you|u|ya)|whats?\s+your\s+age|"
    r"where\s+(?:is|are|was|did|do|does)|"
    r"which\s+\w+\s+(?:is|are|was|do|did|should)|"
    r"what\s+\w+\s+(?:is|are|was|were|do|does|did)|"
    r"what'?s?\s+(?:my|your|the|our)\s+\w+|"
    r"what\s+(?:is|was|are|were)\s+(?:my|your|the|our)\s+\w+|"
    r"what\s+version|today'?s?\s+date|"
    r"what\s+day\s+is\s+(?:it|today))\b", re.I)
_YESNO_RE = re.compile(
    r"^(?:do|does|did|is|are|was|were|can|could|will|would|have|has|"
    r"should|shall|may|might|must|am)\s+", re.I)

# Conversation-management commands ("stop", "thanks", "never mind")
# and non-question statements get no scope directive at all.


def classify_scope(text: str, env=None) -> ResponseScope:
    """Classify the response depth one user turn calls for.

    ``env`` is an optional IntentEnvelope — ambiguity markers and
    compound structure it already detected feed the scope instead of
    being re-derived.
    """
    t = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not t:
        return ResponseScope()
    # Strip trailing punctuation once for anchored tests.
    bare_t = t.rstrip("!?. ")

    reasoning = bool(_REASONING_RE.search(t))

    if _OPEN_ENDED_RE.search(t):
        return ResponseScope(
            depth=ResponseDepth.OPEN_ENDED, max_sentences=20,
            allow_trailing_question=True,
            reasoning_visible=reasoning)
    if _DETAILED_RE.search(t):
        return ResponseScope(
            depth=ResponseDepth.DETAILED, max_sentences=30,
            reasoning_visible=reasoning)
    if reasoning:
        # "How did you calculate that" — the reasoning IS the answer.
        return ResponseScope(
            depth=ResponseDepth.EXPLANATORY, max_sentences=8,
            reasoning_visible=True)
    if _BREVITY_RE.search(t):
        return ResponseScope(depth=ResponseDepth.EXACT, max_sentences=1)
    if _BRIEFLY_RE.search(t):
        return ResponseScope(depth=ResponseDepth.BRIEF, max_sentences=2)
    if _BRIEF_ASK_RE.search(t):
        return ResponseScope(depth=ResponseDepth.BRIEF, max_sentences=3)
    if _EXPLANATORY_RE.search(t):
        return ResponseScope(
            depth=ResponseDepth.EXPLANATORY, max_sentences=8)
    if _EXACT_RE.search(t):
        return ResponseScope(depth=ResponseDepth.EXACT, max_sentences=1,
                             bare=True)
    if _YESNO_RE.match(t) and len(bare_t.split()) <= 25:
        return ResponseScope(depth=ResponseDepth.EXACT, max_sentences=2)
    # Statements, imperatives, and ordinary conversation default to a
    # natural brief answer — not a lecture.
    return ResponseScope()


_DIRECTIVES = {
    ResponseDepth.EXACT: (
        "Answer scope: the user asked for one specific fact — reply with "
        "just that fact, one sentence. Do NOT mention related facts, "
        "memories, or background they did not ask for. Do not open with "
        "acknowledgements ('I hear you', 'Let me think', 'Certainly') or "
        "narrate your reasoning. Do not end with a question."),
    ResponseDepth.BRIEF: (
        "Answer scope: answer directly in 1-3 sentences — the answer plus "
        "at most one useful qualifier. No filler opening, no unrequested "
        "background, no trailing question unless it is genuinely needed."),
    ResponseDepth.EXPLANATORY: (
        "Answer scope: the user asked for an explanation — lead with the "
        "answer, then explain clearly. Match the depth to what they "
        "asked; do not pad."),
    ResponseDepth.DETAILED: (
        "Answer scope: the user explicitly asked for detail — a thorough, "
        "structured answer is expected here."),
    ResponseDepth.OPEN_ENDED: (
        "Answer scope: the user invited a broad answer — relevant detail "
        "and personality are welcome. Stay on their subject."),
}

_SUPPORT_RULE = (
    "Context blocks (memories, persona facts, earlier turns) are evidence "
    "for your reasoning — reveal only what this question actually asks "
    "for; do not restate or list context that was not requested.")

_REASONING_RULE = (
    "The user asked how you arrived at the answer — showing the "
    "derivation IS the requested content here.")

_CLARIFY_RULE = (
    "If a referent is genuinely ambiguous and the wrong guess changes "
    "the answer, ask one short clarifying question instead of guessing.")


def scope_directive(scope: ResponseScope, env=None) -> str:
    """The per-turn prompt line carrying this turn's answer budget.

    Injected beside the intent advisory so the depth instruction reaches
    the model on the same turn the question does. Empty for turns that
    carry no question shape (imperatives/statements still get the BRIEF
    default — a normal instruction deserves a normal answer).
    """
    lines = [_DIRECTIVES[scope.depth]]
    if scope.reasoning_visible:
        lines.append(_REASONING_RULE)
    lines.append(_SUPPORT_RULE)
    # Genuine-ambiguity handling already rides env.ambiguity; restate it
    # scoped so the model pairs "clarify" with "only when it matters".
    if env is not None and getattr(env, "ambiguity", None):
        lines.append(_CLARIFY_RULE)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response-side scope checks — used by QA metrics and (optionally) a final
# guard. These measure the answer, never rewrite it.
# ---------------------------------------------------------------------------

_STOCK_OPENERS_RE = re.compile(
    r"^\s*(?:let\s+me\s+(?:think|see|check|look|figure)|"
    r"i\s+hear\s+you|i\s+understand|certainly|absolutely|of\s+course|"
    r"sure\s+thing|sure\b|great\s+question|good\s+question|"
    r"that'?s?\s+a\s+(?:good|great)\s+question|here'?s?\s+the\s+thing|"
    r"based\s+on\s+(?:what|my)|according\s+to\s+(?:my|the)|"
    r"from\s+the\s+information|okay[,!]?\s+so|alright[,!]?\s+so|"
    r"glad\s+you\s+asked|interesting\s+question)",
    re.I)

_REASONING_NARRATION_RE = re.compile(
    r"\b(?:let\s+me\s+calculate|counting\s+from|i\s+looked\s+through|"
    r"i\s+searched\s+my\s+memor|i\s+determined\s+that|i\s+reasoned\s+"
    r"that|based\s+on\s+my\s+internal|i\s+checked\s+my\s+memory|"
    r"let\s+me\s+recall|thinking\s+it\s+through)\b", re.I)


def leading_filler(text: str) -> str:
    """The stock opener the response leads with, or ''."""
    m = _STOCK_OPENERS_RE.match(str(text or ""))
    return m.group(0).strip() if m else ""


def narrates_reasoning(text: str) -> str:
    """The internal-reasoning narration found, or ''."""
    m = _REASONING_NARRATION_RE.search(str(text or ""))
    return m.group(0) if m else ""


def sentence_count(text: str) -> int:
    """Approximate sentence count — terminators not inside code/paths."""
    t = re.sub(r"```.*?```", " ", str(text or ""), flags=re.S)
    parts = [p for p in re.split(r"[.!?]+(?:\s|$)", t) if p.strip()]
    return max(len(parts), 1 if t.strip() else 0)


def ends_with_question(text: str) -> bool:
    t = str(text or "").rstrip()
    return t.endswith("?")


def scope_metrics(text: str, scope: ResponseScope) -> dict:
    """Measured scope behavior for one final response."""
    t = str(text or "")
    return {
        "depth": scope.depth.value,
        "sentences": sentence_count(t),
        "chars": len(t),
        "leading_filler": leading_filler(t),
        "reasoning_narration": narrates_reasoning(t),
        "trailing_question": ends_with_question(t),
        "over_budget": sentence_count(t) > scope.max_sentences,
    }
