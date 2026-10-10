"""Whole-utterance semantic frame — meaning before lane selection.

ARCHITECTURAL INVARIANT
    Lexical triggers may nominate candidate meanings or tools, but the
    final interpretation is determined from whole-utterance semantics.
    No deterministic fast lane may claim a turn solely because one
    keyword appears. Slash commands remain an intentional exception:
    they are explicit command syntax, not natural language.

Pipeline position:

    normalized turn
      → quote masking        (quoted words are discussed, not asked)
      → clause segmentation  (multi-sentence messages)
      → speech act + semantic roles on the MAIN clause
      → candidate intents    (evidence + confidence)
      → adjudication         (semantic_intent)
      → lane eligibility     (frame.allows(lane))
      → IntentEnvelope + deterministic fast lanes

The frame is computed once in ``understand_turn`` and attached to the
envelope; every Tier-0 keyword lane must consult ``allows()`` before
claiming the turn. ``to_trace()`` is the safe developer surface —
structured classifier evidence, never chain-of-thought.
"""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Metrics — keyword-hijack rate and adjudication counters. ``veto.<lane>``
# counts turns where a lane's lexical trigger matched but the frame refused
# the claim; the spec's north star is that every vetoed capability_inventory
# nomination is a prevented hijack.
# ---------------------------------------------------------------------------

_METRICS: Counter = Counter()


def metrics_snapshot() -> dict[str, int]:
    return dict(_METRICS)


def metrics_reset() -> None:
    _METRICS.clear()


def _tick(name: str) -> None:
    _METRICS[name] += 1


# ---------------------------------------------------------------------------
# Speech acts — communicative purpose, not topic.
# ---------------------------------------------------------------------------

QUESTION = "question"                  # information ask
REQUEST = "request"                    # polite modal request ("can you run…")
COMMAND = "command"                    # imperative ("push it")
OFFER = "offer"                        # declarative offer ("i can give you X")
PREFERENCE_Q = "preference_question"   # "would you like X", "should i give…"
ASSERTION = "assertion"                # statement, no ask
COMPLAINT = "complaint"                # bug report / negative report
PROHIBITION = "prohibition"            # "don't X", "i'm not asking about X"
HYPOTHETICAL = "hypothetical"          # counterfactual ("if you had X…")
COMPARISON = "comparison"              # "which X is faster"
GREETING = "greeting"
CONFIRMATION = "confirmation"
REFUSAL = "refusal"

# Speech acts under which NO deterministic content/action lane may claim
# the turn — the communicative purpose is not a lookup or a mutation.
# CONFIRMATION is deliberately absent: "do it" / "yes" IS the approval of
# a pending action — context supplies the referent, the frame must not
# veto what it cannot see.
_VETO_ALL_ACTS = frozenset({
    OFFER, PREFERENCE_Q, PROHIBITION, HYPOTHETICAL, COMPARISON,
    GREETING, REFUSAL,
})

# Requested slots — what the interrogative is actually asking for.
SLOT_CAPABILITY_LIST = "capability_list"
SLOT_PREFERENCE = "preference"
SLOT_ABILITY = "ability"
SLOT_OPERATIONAL = "operational_status"
SLOT_DEFINITION = "definition"
SLOT_EXPLANATION = "explanation"
SLOT_REASON = "reason"
SLOT_VALUE = "value"
SLOT_PERSON = "person"
SLOT_TIME = "time"
SLOT_LOCATION = "location"
SLOT_COMPARISON = "comparison"
SLOT_ACTION = "action"
SLOT_NONE = ""

_QA_SLOTS = frozenset({
    SLOT_CAPABILITY_LIST, SLOT_PREFERENCE, SLOT_ABILITY,
    SLOT_OPERATIONAL, SLOT_DEFINITION, SLOT_EXPLANATION, SLOT_REASON,
    SLOT_VALUE, SLOT_PERSON, SLOT_TIME, SLOT_LOCATION, SLOT_COMPARISON,
})


@dataclass
class SemanticFrame:
    """The whole-utterance meaning of one user turn."""

    text: str = ""                # normalized original
    masked: str = ""              # lowercased text, quoted spans removed
    masked_case: str = ""         # original-case text, quoted spans removed
    quoted_spans: list[str] = field(default_factory=list)
    clauses: list[str] = field(default_factory=list)
    main_clause: str = ""         # clause carrying the communicative act
    supporting: list[str] = field(default_factory=list)   # background clauses
    speech_act: str = ASSERTION
    target: str = "other"         # nexus|user|other
    subject: str = ""             # who performs the main action
    predicate: str = ""           # main verb phrase (first verb-ish token)
    obj: str = ""                 # what is acted upon
    proposition: str = ""         # normalized restatement of the content
    requested_slot: str = SLOT_NONE
    purpose: str = ""             # trailing purpose clause ("to join X")
    modality: tuple[str, ...] = ()
    negated: bool = False         # main clause carries a negator
    negated_spans: list[str] = field(default_factory=list)
    conditional: bool = False     # real condition gate ("if tests pass, push")
    hypothetical: bool = False    # counterfactual ("if you had X")
    prohibition: bool = False     # negated imperative / "i don't want you to"
    semantic_intent: str = "conversation"
    confidence: float = 0.5
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    # True when the main clause carries no finite-verb predicate — a
    # bare noun phrase. In a chat addressed to Nexus a fragment is
    # pragmatically a request for the thing named ("a picture of a
    # dragon" = "make me a picture of a dragon"); a clause with its own
    # subject + predicate is a proposition ABOUT the world.
    fragment: bool = False

    # -- lane contract ----------------------------------------------------

    def allows(self, lane: str) -> bool:
        """Whether a deterministic lane may claim this turn.

        Lane names: capability_inventory, self_learning, identity,
        control, local_action, navigation, github_status, git_state,
        github_read, memory_recall, feature_status, feature_explain,
        diagnostics, image_action, offer_response, conversation.
        """
        if lane in ("conversation", "model"):
            return True
        ok = self._lane_ok(lane)
        _tick(f"lane.{lane}.{'allow' if ok else 'veto'}")
        if not ok:
            _tick("fast_lane_vetoed")
            _tick(f"veto.{lane}")
        return ok

    def _lane_ok(self, lane: str) -> bool:
        act = self.speech_act
        # The offer lane is the ONLY lane an offer/preference act may
        # claim — check it before the blanket veto.
        if lane == "offer_response":
            return act in (OFFER, PREFERENCE_Q)
        if lane == "identity" and act == PROHIBITION:
            # "stop pretending to be human" is still identity pressure —
            # the locked answer must win. "don't tell me who your
            # father is" is suppressed disclosure — veto.
            neg = " ".join(self.negated_spans)
            return not re.search(
                r"\b(?:tell|say|list|reveal|describe|share|disclose|"
                r"show|give|speak|talk|discuss|repeat)\b", neg)
        if act in _VETO_ALL_ACTS:
            return False
        if not self._topic_in_masked(lane):
            return False
        if lane == "capability_inventory":
            return (self.requested_slot == SLOT_CAPABILITY_LIST
                    and act in (QUESTION, COMMAND, REQUEST))
        if lane == "self_learning":
            return act == QUESTION and self.requested_slot in _QA_SLOTS
        if lane in ("control", "local_action", "image_action",
                    "git_action", "tool_action"):
            # Fragments count as request-shaped: in a chat addressed to
            # Nexus the named thing IS the ask ("a picture of a dragon").
            # A clause with its own predicate is a proposition — "the
            # image on the wall needs a frame" describes the world.
            return (act in (COMMAND, REQUEST) or self.fragment) \
                and not self.prohibition
        if lane in ("github_status", "git_state", "memory_recall",
                    "github_read"):
            # github_read carries no topic vocabulary on purpose —
            # follow-ups like "read it" name no repo; the lane resolves
            # the referent from conversation context.
            return act in (QUESTION, COMMAND, REQUEST)
        if lane in ("feature_status", "feature_explain", "diagnostics"):
            # Complaints are bug reports — they route to investigation,
            # never to a canned feature-status/explanation dump. An
            # action-slot request ("i want you to research X") belongs
            # to the action lanes, not a readiness answer.
            return (act in (QUESTION, COMMAND, REQUEST, ASSERTION)
                    and self.requested_slot != SLOT_ACTION)
        if lane == "navigation":
            # "show me the settings" is action-shaped AND navigation —
            # keep the slot; the lane itself decides which screen.
            return act in (QUESTION, COMMAND, REQUEST, ASSERTION)
        if lane == "identity":
            return act in (QUESTION, ASSERTION, COMMAND, REQUEST)
        return True

    def _topic_in_masked(self, lane: str) -> bool:
        """The lane's nominating vocabulary must exist OUTSIDE quoted
        spans — 'the error said "push to origin"' may not nominate the
        git lane because its keywords were only ever discussed text."""
        pat = _LANE_TOPIC_RE.get(lane)
        if pat is None:
            return True
        return bool(re.search(pat, self.masked))

    def lane_vetoed(self, lane: str) -> bool:
        return not self._lane_ok(lane)

    def vetoes_canned(self) -> bool:
        """True when NO deterministic content lane may claim the turn —
        offers, prohibitions, hypotheticals, comparisons, greetings."""
        return self.speech_act in _VETO_ALL_ACTS

    def to_trace(self) -> dict[str, Any]:
        """Structured classifier evidence — debuggable, never CoT."""
        return {
            "speech_act": self.speech_act,
            "target": self.target,
            "predicate": self.predicate[:60],
            "object": self.obj[:80],
            "slot": self.requested_slot,
            "semantic_intent": self.semantic_intent,
            "confidence": round(self.confidence, 3),
            "modality": list(self.modality),
            "negated": self.negated,
            "prohibition": self.prohibition,
            "hypothetical": self.hypothetical,
            "conditional": self.conditional,
            "fragment": self.fragment,
            "quoted": len(self.quoted_spans),
            "main_clause": self.main_clause[:120],
            "supporting": len(self.supporting),
            "candidates": [{k: c[k] for k in ("lane", "confidence")
                            if k in c} for c in self.candidates[:4]],
            "rejected": self.rejected[:4],
        }


# ---------------------------------------------------------------------------
# Text scaffolding
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).strip()


# Quoted spans are discussed content, never instructions. Double-quoted
# and backticked spans always mask; single quotes only mask a real
# quoted phrase so contractions ("don't") survive — the opening quote
# must not sit inside a word and the content must start alphanumeric.
_QUOTED_RE = re.compile(
    r'"[^"\n]{1,400}"|`[^`\n]{1,400}`|'
    r"(?<!\w)'([A-Za-z0-9][^'\n]{0,400}?)'(?!\w)")


def _strip_quotes(t: str) -> tuple[str, list[str]]:
    spans: list[str] = []

    def _blank(m: re.Match) -> str:
        spans.append(m.group(0))
        return " " * len(m.group(0))

    masked = _QUOTED_RE.sub(_blank, t)
    return _norm(masked), spans


# Clause segmentation — sentence punctuation, em-dashes, and explicit
# sequencing conjunctions. Commas split only before discourse pivots;
# "fix the build, it keeps failing" keeps both clauses distinct.
_CLAUSE_SPLIT_RE = re.compile(
    r"[.!?;]+|\s*[—–]{1,2}\s*|\n+|,\s+(?=but\b|and\s+then\b)|"
    r"\s+but\s+|\s+and\s+then\s+|\s+then\s+", re.IGNORECASE)

# An "if/when/unless" condition wrapping an action clause — the ACTION
# clause carries the act; the condition is structure, not a hypothetical.
_COND_HEAD_RE = re.compile(
    r"^(?:if|when|once|after|as\s+soon\s+as|before|unless)\s+"
    r"(.{3,120}?)(?:,\s*then\s+|,\s*|\s+then\s+)(.{3,160})$",
    re.IGNORECASE)

# Counterfactual frames — "if you had/could/were", "suppose", "what if".
_HYPOTHETICAL_RE = re.compile(
    r"\bif\s+(?:you|u|nexus)\s+(?:had|could|were|got|'d)\b|"
    r"\bif\s+i\s+(?:gave|give|let|added|allowed|enabled|granted|"
    r"installed|built|made)\b|\bsuppos(?:e|ing)\b|\bwhat\s+if\b|"
    r"\bimagine\s+(?:if|you|u|nexus)\s+(?:had|could|were|got)\b|"
    r"\bimagine\s+if\b|\bpretend\b|\bhypothetically\b|"
    r"\bwould\s+you\s+(?:do|want|say|build|make)\b.{0,40}\bif\b|"
    r"\bif\s+it\s+(?:were|was)\b|"
    # Past-subjunctive conditions are counterfactual discussion —
    # "if the tools all worked, which would you use". A real condition
    # uses present tense ("if it works, push it") and stays a command.
    r"\bif\b[^.?!]{0,80}\b(?:were\b|worked\b|could\b|had\b)",
    re.IGNORECASE)

# Imperative negation — "don't X", "never X", "stop X", "no need to X",
# plus meta-negation ("i'm not asking…") and user-side negation
# ("i don't want you to…"). `don't you/we/it` is a QUESTION, not a
# prohibition — excluded by the lookahead.
_PROHIBITION_LEAD_RE = re.compile(
    r"^(?:please\s+)?(?:don'?t|do\s+not|never|stop|quit|refrain\s+from|"
    r"no\s+need\s+to|there'?s\s+no\s+need\s+to)\s+"
    r"(?!you\b|u\b|it\b|they\b|we\b|i\b)\S",
    re.IGNORECASE)
_META_NEGATION_RE = re.compile(
    r"\bi(?:'m|\s+am)\s+not\s+(?:asking|telling|saying|talking|looking)\b|"
    r"\bi\s+(?:don'?t|do\s+not|didn'?t)\s+(?:want|need|ask)\s+"
    r"(?:you|u)\s+to\b|\bi\s+(?:don'?t|do\s+not)\s+want\b|"
    r"\bstop\s+(?:telling|saying|showing|listing|"
    r"repeating|giving)\b|\bnot\s+(?:asking|telling)\b.{0,30}\babout\b|"
    r"\b(?:you|u)\s+(?:don'?t|do\s+not|won'?t|needn'?t)\s+"
    r"(?:need|have)\s+to\b|\bno\s+need\s+(?:for\s+you\s+)?to\b",
    re.IGNORECASE)

# Offers / preference questions — the user is giving or proposing; the
# requested output is Nexus's preference, never an inventory.
_OFFER_Q_RE = re.compile(
    r"\bwould\s+you\s+(?:\w+\s+){0,2}(?:like|want|prefer|enjoy|fancy|"
    r"be\s+interested)\b|(?<!i\s)\bwanna\b|"
    r"\bdo\s+you\s+(?:want|wanna|like|fancy)\b|\bwould\s+you\s+be\s+"
    r"interested\b|\bare\s+you\s+interested\b|\bwould\s+having\b|"
    r"\bhow\s+would\s+you\s+(?:like|feel)\b|\bwould\s+you\s+rather\b|"
    r"\bwant\s+me\s+to\b|\bwould\s+you\s+(?:like|want)\s+me\s+to\b|"
    r"\bshould\s+i\b|\bshall\s+i\b|\bcan\s+i\s+(?:give|get|add)\b|"
    r"\bcould\s+i\s+(?:give|get|add)\b|\bwould\s+it\s+help\b|"
    r"\bwould\s+you\s+care\s+(?:for|to)\b|\blet\s+me\s+know\s+if\s+"
    r"you'?d\b|\bdo\s+you\s+want\s+me\s+to\b",
    re.IGNORECASE)
_OFFER_DECL_RE = re.compile(
    r"\bi\s+(?:can|could|'?ll|will|'?m\s+going\s+to|am\s+going\s+to|"
    r"might|may|can\s+probably|could\s+probably)\s+(?:probably\s+|"
    r"maybe\s+)?(?:give|get|add|set\s+up|provide|let|grant|offer|"
    r"hook|enable|install|build|make|connect|teach|show)\b|"
    r"\bi\s+(?:want|would\s+like|'?d\s+like|plan|mean)\s+to\s+"
    r"(?:give|add|set\s+up|let|provide|enable|teach)\b|"
    r"\blet\s+me\s+(?:give|add|set\s+up|hook)\b|"
    r"\bi'?m\s+(?:offering|going\s+to\s+give)\b",
    re.IGNORECASE)

# Comparison — "which is faster", "X vs Y", "difference between".
_COMPARISON_RE = re.compile(
    r"\bwhich\b.{0,60}\b(?:better|faster|slower|cheaper|more|less|"
    r"worse|preferred|safer|stronger)\b|\bvs\.?\b|\bversus\b|"
    r"\bcompare\b|\bcomparison\b|\bdifference\s+between\b|"
    r"\bwhich\s+one\b|\bwhich\s+of\s+(?:them|these|those|the)\b|"
    r"\b(?:better|worse|faster|slower|cheaper)\s+than\b",
    re.IGNORECASE)

# Failure predicates — complaint/bug-report structure.
_COMPLAINT_RE = re.compile(
    r"\b(?:keeps?|kept|keep)\s+\w*ing\b|\bstopped\s+working\b|"
    r"\bisn'?t\s+working\b|\bis\s+not\s+working\b|\bdoesn'?t\s+work\b|"
    r"\bdoes\s+not\s+work\b|\bwon'?t\s+(?:work|run|load|start|open|"
    r"connect|install|save|play|respond)\b|\bcan'?t\s+get\b|"
    r"\bcouldn'?t\s+get\b|\b(?:is|are|was|looks?|seems?|feels?|"
    r"sounds?)\s+(?:still\s+)?(?:broken|busted|dead|fried|messed\s+up|"
    r"hosed|screwed|terrible|awful|broken[-\s]?looking|wrong|slow|"
    r"laggy|glitchy|janky|crap|bad|off)\b|\bbroke\b|\bbroken\b|"
    r"\bcrash(?:es|ed|ing)?\b|\bfail(?:s|ed|ing|ure|ures)?\b|"
    r"\bhangs?\b|\bfreez(?:es|ing)\b|\bstuck\b|\btoo\s+slow\b|"
    r"\bstalls?\b|\bstalling\b|\btime(?:d|s)?\s*out\b|\btiming\s+out\b|"
    r"\bcut(?:s|ting)?\s+(?:off|out)\b|\bdrops?\b|\bdropping\b|"
    r"\blagg(?:ing|y|s)\b|\bflickers?\b|\bwon'?t\s+open\b|"
    r"\bwon'?t\s+(?:load|save|play|respond|close)\b|"
    r"\bsame\s+(?:error|problem|issue|bug)\b|\bbug\b|\berrors?\b|"
    r"\bglitch(?:es|ed|ing|y)?\b|"
    r"\b(?:way\s+)?too\s+(?:high|low|much|slow|fast|big|small|long|"
    r"little|many|few|loud|quiet|bright|dark)\b|"
    r"\b(?:being|acting|seeming|getting)\s+(?:weird|off|strange|funny|"
    r"odd|wonky|glitchy|janky|flaky|funky)\b|\bthrough\s+the\s+roof\b",
    re.IGNORECASE)

# Action verbs — an imperative lead or modal-request verb.
_ACTION_LEAD_RE = re.compile(
    r"^(?:(?:please|hey|ok|okay|yo|so|now|nexus)\b[,!]?\s+|"
    r"(?:hey|ok|okay|yo|hi|hello)\s+\w+[,!]?\s+)*(?:"
    r"create|make|write|edit|fix|delete|remove|add|build|rebuild|run|"
    r"execute|update|upgrade|install|uninstall|download|upload|open|"
    r"close|move|copy|rename|push|pull|commit|deploy|save|start|stop|"
    r"restart|kill|set|turn|mute|unmute|pause|resume|enable|disable|"
    r"connect|disconnect|check|show|list|give|put|get|fetch|grab|"
    r"search|find|look\s+up|look\s+into|research|generate|draw|paint|"
    r"render|remember|forget|tell|explain|describe|summari[sz]e|print|"
    r"export|import|schedule|post|share|clean|sync|publish|merge|test|"
    r"verify|apply|use|try|switch|launch|revert|undo|redo|reset|"
    r"continue|proceed|carry\s+on|go\s+on|keep\s+going|"
    r"diagnos\w*|inspect|scaffold|clone|answer|help|join|sign|log|read|"
    r"watch|review|design|redesign|document|improve|optimize|translate|"
    r"record|attach|insert|calculate|compute|evaluate|solve|retry|"
    r"gimme|"
    r"repeat|load|unload|dump|flush|visuali[sz]e|imagine|depict|"
    r"illustrate|produce|conjure|compose|see|view|watch|look\s+at|"
    r"look\s+into|pull\s+up|self[-\s]diagnos\w*)\b",
    re.IGNORECASE)

# Explicit request wrappers — "i want you to X", "can you run X".
_REQUEST_WRAPPER_RE = re.compile(
    r"^(?:i\s+(?:want|need|would\s+like|'?d\s+like|wanna)\s+"
    r"(?:you|u|nexus)\s+to\b|"
    r"(?:can|could|will|would|may)\s+(?:you|u|nexus)\s+(?:please\s+)?|"
    r"(?:can|could|will|would)\s+(?:you|u)\s+(?:please\s+)?|"
    r"please\s+|go\s+ahead\s+and\s+|i\s+need\s+you\s+to\b|"
    r"i'?d\s+like\s+you\s+to\b)", re.IGNORECASE)

_GREETINGS = frozenset({
    "hi", "hello", "hey", "hey there", "good morning", "good afternoon",
    "good evening", "yo", "hiya", "howdy", "sup", "hello there",
    "hey nexus", "hi nexus", "hello nexus",
})
_CONFIRM_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|sounds\s+good|go\s+ahead|"
    r"do\s+it|please\s+do|confirmed?|absolutely|definitely|"
    r"that\s+works|perfect|great)\s*[.!]*$", re.IGNORECASE)
_REFUSAL_RE = re.compile(
    r"^(?:no|nope|nah|never\s+mind|no\s+thanks|don'?t|"
    r"that'?s\s+(?:ok|okay|fine)|i'?m\s+good|cancel\s+that)\s*[.!]*$",
    re.IGNORECASE)

_INTERROGATIVE_LEAD_RE = re.compile(
    r"^(?:what|who|whom|whose|when|where|why|how|which|is|are|am|was|"
    r"were|do|does|did|can|could|will|would|should|shall|may|might|"
    r"have|has|haven'?t|won'?t|isn'?t|aren'?t|don'?t|doesn'?t|"
    r"didn'?t|didn'?t)\b", re.IGNORECASE)

# "i want to know …" — an embedded question is still a question.
_WANT_TO_KNOW_RE = re.compile(
    r"\bi\s+(?:want|would\s+like|'?d\s+like|wanna|need)\s+to\s+know\b|"
    r"\b(?:tell|let)\s+me\s+know\b|\btell\s+me\s+(?:whether|if|what|"
    r"why|how|about|your|the)\b|\bi'?m\s+(?:curious|wondering)\b|"
    r"\bi\s+wonder\b|\bwondering\s+(?:if|whether|what|how|why)\b|"
    r"\bwhat\s+does\b.{0,60}\bmean\b|\bwhat\s+(?:does|do|did)\s+"
    r"(?:that|this|it)\s+mean\b",
    re.IGNORECASE)

# A negated imperative anywhere in the message vetoes action lanes even
# when it isn't the main clause — "he said X, don't do that" must never
# produce an execution.
_EMBEDDED_PROHIBITION_RE = re.compile(
    r"\b(?:don'?t|do\s+not|never)\s+"
    r"(?:do|run|delete|install|uninstall|create|push|execute|"
    r"download|upload|open|touch|change|modify|remove|use|try|send|"
    r"post|share|deploy|commit|save|move|copy|rename|start|stop|kill|"
    r"restart|enable|disable|connect|disconnect|add|write|edit|fix|"
    r"build|update|apply|merge|publish|schedule|erase|format)\b",
    re.IGNORECASE)

# Per-lane topic contract — the lane's nominating vocabulary must exist
# outside quoted spans. Execution lanes require an action verb to remain
# in the masked text; content lanes require their domain noun.
_EXEC_TOPIC_RE = (
    r"\b(?:create|make|write|edit|fix|delete|remove|add|build|rebuild|"
    r"run|execute|update|upgrade|install|uninstall|download|upload|"
    r"open|close|move|copy|rename|push|pull|commit|deploy|save|start|"
    r"stop|restart|kill|set|turn|mute|unmute|pause|resume|enable|"
    r"disable|connect|disconnect|generate|draw|paint|render|search|"
    r"find|fetch|get|put|list|show|check|print|export|import|schedule|"
    r"post|share|sync|publish|merge|test|verify|apply|use|switch|"
    r"launch|revert|undo|reset|inspect|clone|read|join|attach|insert|"
    r"format|erase|wipe|record|capture|grab|remember|forget|watch|"
    r"click|press|browse|visit|navigate|scroll|type)\b")

_LANE_TOPIC_RE: dict[str, str] = {
    # capability_inventory is absent on purpose: its gate requires
    # requested_slot == capability_list, which already demands the
    # whole-utterance ask shape — quoted/masked capability phrases can't
    # produce that slot, and literal vocabulary would block legit asks
    # like "tell me everything you can do".
    "self_learning": r"\blearn|\badapt\b",
    "control": (
        r"\b(?:voices?|volume|muted?|unmute|workers?|settings?|preset|"
        r"models?|backend|speak|speech|quieter|louder|autonomy|theme|"
        r"brightness|performance)\b|" + _EXEC_TOPIC_RE),
    "local_action": _EXEC_TOPIC_RE,
    "tool_action": _EXEC_TOPIC_RE,
    "git_action": _EXEC_TOPIC_RE,
    "image_action": (
        r"\b(?:images?|pictures?|photos?|pics?|draw|paint|render|"
        r"wallpaper|logo|icon|art\w*|portrait|sketch|illustrat\w*|"
        r"selfie|see|view|watch|look|show|visuali[sz]e|imagine|"
        r"depict|produce|generate|create|make)\b|" + _EXEC_TOPIC_RE),
    "github_status": (
        r"\b(?:github|repos?(?:itory|itories)?|branch(?:es)?|remotes?|"
        r"origin|upstream|fork|commits?|push|pull|merge|clone)\b"),
    "git_state": (
        r"\b(?:git|github|branch(?:es)?|remotes?|"
        r"repos(?:itory|itories)?|origin|upstream|fork|"
        r"push|pull|commits?|merges?|rebase|checkout|clone|"
        r"fetch|stash|pull\s+request|pr)\b"),
    # identity and memory_recall are absent on purpose: their claim lanes
    # already match structure on the masked text (identity.response_for,
    # _FACT_RECALL_RE), and a recall turn can name ANY stored fact — the
    # topic vocabulary is unbounded, so only the speech act gates them.
}

_MODAL_RE = re.compile(
    r"\b(?:can|could|would|should|shall|may|might|must|will|won'?t|"
    r"can'?t|cannot|want|wanna|need|have\s+to|got\s+to)\b",
    re.IGNORECASE)
_NEGATOR_RE = re.compile(
    r"\b(?:don'?t|do\s+not|doesn'?t|does\s+not|didn'?t|did\s+not|"
    r"isn'?t|is\s+not|aren'?t|are\s+not|won'?t|will\s+not|can'?t|"
    r"cannot|couldn'?t|shouldn'?t|wouldn'?t|never|no\s+longer|"
    r"not|nothing|none|without)\b", re.IGNORECASE)
_NEGATED_SPAN_RE = re.compile(
    r"\b(?:don'?t|do\s+not|never|stop|no\s+need\s+to|"
    r"not\s+(?:asking|telling|saying))\s+([^,.;?!—]{2,80})",
    re.IGNORECASE)

# Finite verbs — copulas, auxiliaries, and the common main-clause
# predicates that turn a noun phrase into a proposition. A turn whose
# main clause carries none of these (outside relative clauses) is a
# bare phrase — pragmatically a request in a chat addressed to Nexus.
# Coverage is deliberately broad rather than exhaustive: a missed rare
# verb only under-detects propositions, never invents one.
_FINITE_VERB_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|am|ain'?t|"
    r"has|have|had|haven'?t|hasn'?t|hadn'?t|"
    r"do|does|did|done|don'?t|doesn'?t|didn'?t|"
    r"will|would|shall|should|can|could|may|might|must|"
    r"won'?t|can'?t|cannot|couldn'?t|shouldn'?t|wouldn'?t|"
    r"need|needs|needed|want|wants|wanted|"
    r"seem|seems|seemed|look|looks|looked|feel|feels|felt|"
    r"mean|means|meant|say|says|said|"
    r"get|gets|got|gotten|keep|keeps|kept|"
    r"go|goes|went|gone|come|comes|came|"
    r"make|makes|made|take|takes|took|taken|work|works|worked|"
    r"stay|stays|stayed|remain|remains|remained|"
    r"belong|belongs|belonged|hang|hangs|hung|sit|sits|sat|"
    r"stand|stands|stood|cost|costs|"
    r"show|shows|shown|tell|tells|told|know|knows|knew|known|"
    r"think|thinks|thought|like|likes|liked|love|loves|loved|"
    r"hate|hates|hated|live|lives|lived|sound|sounds|sounded|"
    r"exist|exists|existed|happen|happens|happened|"
    r"fail|fails|failed|break|breaks|broke|broken|"
    r"run|runs|ran|deserve|deserves|deserved|"
    r"require|requires|required|contain|contains|contained|"
    r"include|includes|included|fit|fits|fitted|suit|suits|suited|"
    r"match|matches|matched|own|owns|owned|owe|owes|owed|"
    r"lack|lacks|lacked|use|uses|used|"
    r"become|becomes|became|crash|crashes|crashed|"
    r"freeze|freezes|froze|frozen|stuck|hung)\b",
    re.IGNORECASE)
_RELATIVE_MARKERS = frozenset(
    {"that", "which", "who", "whom", "whose", "where", "when"})
_SUBJECT_PRONOUNS = frozenset({"i", "you", "we", "they", "he", "she"})


def _main_clause_has_predicate(clause: str) -> bool:
    """Whether the clause predicates — a finite verb that isn't embedded
    in a relative clause modifying a noun phrase.

    "the image on the wall NEEDS a frame" → proposition.
    "a picture of a dog that NEEDS a frame" → the verb opens a relative
    clause; the whole utterance is still one noun phrase (a fragment).
    "a photo of the sunset WE SAW" → zero-relative — the pronoun+verb
    sits inside the noun phrase, so it stays a fragment; but a clause
    that *starts* with a subject pronoun ("we saw a dragon") is a real
    proposition.
    """
    t = clause.lower()
    starts_pronominal = bool(re.match(
        r"^\s*(?:i|you|we|they|he|she|it|nexus|there|my|your|his|her|"
        r"our|their|its)\s+(?=\S)", t))
    for m in _FINITE_VERB_RE.finditer(t):
        head = t[:m.start()].rstrip()
        prev = head.rsplit(" ", 1)[-1] if head else ""
        if prev in _RELATIVE_MARKERS:
            continue  # verb opens a relative clause
        if prev in _SUBJECT_PRONOUNS and not starts_pronominal:
            continue  # zero-relative: "the sunset we saw"
        return True
    return False

# Capability-inventory asks — the ONLY shapes that may claim the
# inventory lane. "What can you do"-family: the user asks FOR the list.
_CAP_LIST_RE = re.compile(
    r"\bwhat\s+(?:all\s+)?(?:can|could)\s+(?:you|u|nexus)\s+do\b|"
    r"\bwhat\s+are\s+(?:you|u|nexus)\s+(?:able\s+to\s+do|capable\s+of)\b|"
    r"\bwhat\s+(?:all\s+)?(?:\w+\s+){0,2}are\s+(?:you|u|nexus)\s+"
    r"(?:able\s+to\s+do|capable\s+of)\b|"
    r"\b(?:gimme|give\s+me)\b.{0,30}\b(?:list|features|capabilities|"
    r"tools|skills)\b|"
    r"\bwhat\s+are\s+your\s+(?:\w+\s+){0,3}"
    r"(?:capabilities|abilities|features|skills|tools|functions|"
    r"powers)\b|"
    r"\bwhat\s+(?:capabilities|abilities|features|tools|skills)\s+"
    r"(?:do|does|did)\s+(?:you|u|nexus)\s+(?:\w+\s+){0,3}have\b|"
    r"\bwhat\s+(?:capabilities|abilities|features|tools|skills)\s+"
    r"(?:are\s+there|have\s+you\s+got)\b|"
    r"\b(?:list|show\s+me|tell\s+me\s+about|tell\s+me|give\s+me|"
    r"share)\s+(?:me\s+)?(?:all\s+)?(?:of\s+)?your\s+(?:technical\s+)?"
    r"(?:capabilities|abilities|features|tools|skills)\b|"
    r"\btell\s+me\s+(?:all\s+)?(?:about\s+)?(?:everything\s+)?you\s+"
    r"can\s+do\b|\bwhat\s+(?:do|does)\s+(?:you|u|nexus)\s+do\b|"
    r"\bhow\s+can\s+(?:you|u|nexus)\s+help\b|"
    r"\bwhat\s+can\s+(?:you|u)\s+help\s+(?:me\s+)?with\b|"
    r"\bwhat\s+are\s+you\s+(?:good\s+at|used\s+for)\b|"
    r"\bwhat'?s\s+in\s+your\s+toolbox\b|"
    r"\byour\s+full\s+capabilit|\ball\s+your\s+capabilit|"
    r"\beverything\s+you\s+can\s+do\b|\bwhat\s+else\s+can\s+you\s+do\b",
    re.IGNORECASE)

_OPERATIONAL_RE = re.compile(
    r"\b(?:working|work|works|broken|connected|connecting|running|"
    r"up|down|enabled|disabled|installed|available|ready|alive|muted|"
    r"paused|online|offline|healthy|configured|set\s+up|active|"
    r"authorized|responsive|responding|on|off)\b", re.IGNORECASE)


def _clauses(masked: str) -> list[str]:
    parts = [p.strip(" ,.-") for p in _CLAUSE_SPLIT_RE.split(masked)]
    return [p for p in parts if p]


# Leading discourse markers / politeness particles are pragmatics, not
# the communicative clause — 'ok so, is the voice on' asks the same
# question as 'is the voice on'; 'pls push it' is the same command.
_DISCOURSE_FREE_RE = re.compile(
    r"^(?:(?:um+|uh+|er+|hmm+|ok(?:ay)?|so(?:\s+like)?|well|"
    r"anyways?|alright|also|yeah|yep|yup|tbh|btw|honestly|actually|"
    r"basically|literally|like|pls|please|kindly|now|hey)\b"
    r"[\s,.\-—–!?]*)+", re.IGNORECASE)
# Imperative-adjacent markers ('wait a second' is a command, 'wait,'
# is discourse) — only strip when punctuation separates them.
_DISCOURSE_COMMA_RE = re.compile(
    r"^(?:(?:wait|look|listen|yo|see)\b\s*[,—–\-]\s*)+",
    re.IGNORECASE)


def _strip_discourse_prefix(clause: str) -> str:
    out = clause.strip()
    for _ in range(3):
        prev = out
        out = _DISCOURSE_FREE_RE.sub("", out, count=1)
        out = _DISCOURSE_COMMA_RE.sub("", out, count=1)
        if out == prev:
            break
    return out.lstrip(" ,.—–-") or clause.strip()


def _clause_score(clause: str) -> int:
    """How strongly a clause carries the communicative act — later
    high-scoring clauses win the main-clause slot because the actual
    ask usually lands last after background setup."""
    t = clause.lower()
    if _INTERROGATIVE_LEAD_RE.match(t) or clause.rstrip().endswith("?"):
        return 3
    if _OFFER_Q_RE.search(t) or _WANT_TO_KNOW_RE.search(t):
        return 3
    if _ACTION_LEAD_RE.match(t) or _REQUEST_WRAPPER_RE.match(t):
        return 3
    if _OFFER_DECL_RE.search(t):
        return 2
    if _PROHIBITION_LEAD_RE.match(t):
        return 2
    if _COMPLAINT_RE.search(t):
        return 1
    return 0


def _main_clause_index(clauses: list[str]) -> int:
    best_idx, best_score = 0, -1
    for i, c in enumerate(clauses):
        s = _clause_score(c)
        if s >= best_score:      # later clause wins ties — the ask
            best_idx, best_score = i, s   # usually comes last
    return best_idx


def _requested_slot(clause: str) -> str:
    """What the interrogative/imperative clause actually asks for."""
    t = clause.lower()
    if _OFFER_Q_RE.search(t):
        return SLOT_PREFERENCE
    if _CAP_LIST_RE.search(t):
        return SLOT_CAPABILITY_LIST
    if re.search(r"\bwant\s+to\s+know\s+(?:whether|if)\b|"
                 r"\btell\s+me\s+(?:whether|if)\b|"
                 r"\bwhether\s+you'?d\b", t):
        return SLOT_PREFERENCE
    if re.search(r"\bwho\s+(?:is|are|was|were|made|created|built|"
                 r"wrote|designed|s)\b", t):
        return SLOT_PERSON
    if re.search(r"\bwhat\s+(?:time|day|date)\b|\bwhen\s+(?:is|are|was|"
                 r"were|did|does|will)\b", t):
        return SLOT_TIME
    if re.search(r"\bwhere\b|\bhow\s+do\s+i\s+(?:get|find|open|"
                 r"reach|navigate)\b|\bwhich\s+page\b", t):
        return SLOT_LOCATION
    if _COMPARISON_RE.search(t):
        return SLOT_COMPARISON
    if re.search(r"\bwhy\b|\bhow\s+come\b|\bwhat\s+for\b", t):
        return SLOT_REASON
    if re.search(r"\bhow\s+(?:do|does|did|would|can|could|to|is|are)\b|"
                 r"\bhow\s+does\s+\w+\s+work\b|\bhow\s+to\b", t):
        return SLOT_EXPLANATION
    if re.search(r"\bhow\s+(?:many|much|old|long|far|big|tall)\b", t):
        return SLOT_VALUE
    if re.search(r"\b(?:is|are|does|did|do|can|could|has|have|was|were|"
                 r"will)\b.{0,50}" + _OPERATIONAL_RE.pattern, t):
        return SLOT_OPERATIONAL
    if re.search(r"\b(?:can|could|do|does|are|is|have)\s+"
                 r"(?:you|u|nexus|there)\b", t):
        return SLOT_ABILITY
    if re.search(r"\bwhat\s+(?:is|are|was|were|does|do|did)\b|"
                 r"\bwhat'?s\b|\bwhats\b|\bdefine\b|\bmeaning\s+of\b|"
                 r"\bwhat\s+does\b.+\bmean\b", t):
        return SLOT_DEFINITION
    if re.search(r"\bwhich\b", t):
        return SLOT_VALUE
    if re.search(r"\bwhat\b|\bwhen\b|\bwhom\b|\bwhose\b", t):
        return SLOT_VALUE
    if _INTERROGATIVE_LEAD_RE.match(t):
        return SLOT_ABILITY
    if _ACTION_LEAD_RE.match(t):
        return SLOT_ACTION
    # Wrapped asks — "i want you to research X", "can you run the
    # tests": the clause doesn't lead with the verb but the requested
    # output is still an action.
    wrm = _REQUEST_WRAPPER_RE.match(t)
    if wrm:
        rest = t[wrm.end():].lstrip()
        first = rest.split(" ", 1)[0] if rest else ""
        if first in _ACTION_WORDS or _ACTION_LEAD_RE.match(first):
            return SLOT_ACTION
    return SLOT_NONE


def _target(clause: str, act: str) -> str:
    t = clause.lower()
    if act in (OFFER, PREFERENCE_Q):
        return "nexus"      # the offer/proposal concerns Nexus
    if re.search(r"\b(?:you|your|yourself|u|nexus)\b", t):
        return "nexus"
    if re.match(r"^(?:i|we|my)\b", t):
        return "user"
    return "other"


def _proposition(clause: str, act: str) -> str:
    """The offered/asked content with the speech-act wrapper removed —
    'would you like the capabilities to join an ai community' →
    'the capabilities to join an ai community'."""
    t = clause.strip(" ?.!")
    for pat in (
            r"^would\s+you\s+(?:like|want|prefer|enjoy|fancy|"
            r"be\s+interested\s+in)\s+(?:me\s+to\s+(?:give\s+you|"
            r"add|set\s+up)\s+)?",
            r"^do\s+you\s+(?:want|wanna|like)\s+",
            r"^(?:do\s+you\s+)?(?:want|wanna)\s+me\s+to\s+",
            r"^wanna\s+",
            r"^(?:should|shall|can|could)\s+i\s+",
            r"^i\s+(?:can|could|'?ll|will|might|may)\s+"
            r"(?:probably\s+|maybe\s+)?",
            r"^i\s+(?:want|would\s+like|'?d\s+like)\s+to\s+",
            r"^(?:can|could|will|would)\s+(?:you|u)\s+",
            r"^i\s+(?:want|need)\s+to\s+know\s+",
    ):
        t = re.sub(pat, "", t, flags=re.IGNORECASE).strip(" ,.")
    # Offer wrapper remainder — "should i give you X" leaves "give you
    # X", "should i let you onto moltbook" leaves "let you onto
    # moltbook"; the offered proposition is X itself.
    t = re.sub(r"^(?:give|get|add|set\s+up|provide|grant|offer|"
               r"hook\s+up|enable|install|build|make|teach|show|"
               r"connect|buy|create|let|allow)\s+"
               r"(?:me\s+)?(?:you|u|nexus)\s+",
               "", t, flags=re.IGNORECASE).strip(" ,.")
    # Leading preposition left by the let/allow strip — "onto moltbook"
    # → "moltbook".
    t = re.sub(r"^(?:onto|on\s+to|into|on)\s+", "", t,
               flags=re.IGNORECASE).strip(" ,.")
    return t


def analyze(text: str, *, _no_cache: bool = False) -> SemanticFrame:
    """Build the SemanticFrame for one user turn — deterministic,
    context-free, and cheap enough to run on every turn."""
    raw = _norm(text)
    if not _no_cache:
        hit = _CACHE.get(raw)
        if hit is not None:
            _CACHE.move_to_end(raw)
            _tick("cache.hit")
            return hit
    frame = _analyze(raw)
    if not _no_cache:
        _CACHE[raw] = frame
        if len(_CACHE) > 512:
            _CACHE.popitem(last=False)
    _tick("turns")
    _tick(f"semantic.{frame.semantic_intent}")
    for r in frame.rejected:
        _tick(f"nominated_vetoed.{r['lane']}")
    return frame


_CACHE: OrderedDict[str, SemanticFrame] = OrderedDict()


def _analyze(raw: str) -> SemanticFrame:
    frame = SemanticFrame(text=raw)
    t = raw.lower()
    try:
        # The whitelist typo map is shared with the intent layer so the
        # frame sees the same normalized stream — 'whta time is it' is
        # a question at every level, not just in _classify_turn.
        from .intent import _fix_typos
        t = _fix_typos(t)
    except Exception:
        pass
    masked, frame.quoted_spans = _strip_quotes(t)
    frame.masked = masked
    # Case-preserving mask — path/identifier-sensitive parsers (the
    # local-action planner) must not see quoted commands, but must keep
    # real path case like D:\Nexus.
    frame.masked_case = _norm(_QUOTED_RE.sub(" ", raw))

    if not masked:
        frame.speech_act = ASSERTION
        frame.semantic_intent = "conversation"
        return frame

    # Global structural flags — whole message, not just the main clause.
    # "tell me/check/see if X" — the `if` is an interrogative complement
    # meaning "whether", not a conditional frame.
    masked_hy = re.sub(
        r"\b(?:tell\s+me|let\s+me\s+know|check(?:ing)?|see|ask(?:ing)?|"
        r"wonder(?:ing)?|know|find\s+out|figure\s+out|determine|"
        r"show\s+me|confirm|verify)\s+(?:whether\s+)?if\b",
        " whether", masked)
    frame.hypothetical = bool(_HYPOTHETICAL_RE.search(masked_hy))
    cm = _COND_HEAD_RE.match(masked_hy)
    frame.conditional = bool(cm or re.search(
        r"\bif\b.{5,80}\bthen\b|\bunless\b|\bprovided\s+(?:that|you)\b",
        masked_hy))
    frame.negated_spans = [m.group(0) for m in
                           _NEGATED_SPAN_RE.finditer(masked)]
    embedded_prohibition = bool(_EMBEDDED_PROHIBITION_RE.search(masked))

    clauses = _clauses(masked)
    frame.clauses = clauses
    if not clauses:
        clauses = [masked]
    main_idx = _main_clause_index(clauses)
    main = _strip_discourse_prefix(clauses[main_idx])
    frame.main_clause = main
    frame.supporting = [c for i, c in enumerate(clauses) if i != main_idx]

    # Conditional wrapper on the main clause — classify the action.
    m = _COND_HEAD_RE.match(main)
    if m and not frame.hypothetical:
        act_clause = m.group(2).strip()
        if _ACTION_LEAD_RE.match(act_clause):
            main = act_clause
            frame.main_clause = f"{main} [{m.group(1)}]"
    ml = main.lower()
    frame.negated = bool(_NEGATOR_RE.search(ml))
    frame.modality = tuple(dict.fromkeys(
        m.group(0) for m in _MODAL_RE.finditer(ml)))

    frame.speech_act = _speech_act(ml, raw_norm=masked)
    # A bare noun phrase (no finite-verb predicate) is pragmatically a
    # request for the named thing; a clause with subject + predicate is
    # a proposition about the world. Only assertions need the flag —
    # commands/questions/offers already carry their own act.
    frame.fragment = (frame.speech_act == ASSERTION
                      and not _main_clause_has_predicate(main))
    frame.target = _target(main, frame.speech_act)
    frame.requested_slot = _requested_slot(ml)
    frame.proposition = _proposition(main, frame.speech_act)
    frame.prohibition = (frame.speech_act == PROHIBITION
                         or embedded_prohibition)
    if frame.hypothetical and frame.speech_act in (
            QUESTION, ASSERTION, OFFER, PREFERENCE_Q, COMMAND, REQUEST):
        frame.speech_act = HYPOTHETICAL
    pm = re.match(r"^(?:please\s+)?(\w+(?:\s+\w+)?)", ml)
    if pm and frame.speech_act in (COMMAND, REQUEST, QUESTION):
        frame.predicate = pm.group(1)
    frame.subject, frame.obj = _roles(main, frame.speech_act)
    pm_purpose = re.search(r"\b(?:so\s+(?:that|you\s+can)|to\s+"
                           r"(?:join|use|access|participate|help|"
                           r"learn|work|talk|interact|connect)\b"
                           r"[^.?!]{0,60})", masked)
    if pm_purpose:
        frame.purpose = pm_purpose.group(0).strip(" ,.")

    frame.semantic_intent, frame.confidence = _adjudicate(frame)
    _candidates(frame)
    return frame


def _speech_act(clause: str, *, raw_norm: str) -> str:
    """Communicative purpose of the main clause — ordered most-
    constraining first."""
    low = _norm(clause)
    if low in _GREETINGS:
        return GREETING
    if _CONFIRM_RE.match(low):
        return CONFIRMATION
    if _REFUSAL_RE.match(low):
        return REFUSAL
    if _PROHIBITION_LEAD_RE.match(low) or _META_NEGATION_RE.search(low):
        return PROHIBITION
    if _OFFER_Q_RE.search(low):
        # "should i run this?" / "the docs say X — should i?" asks
        # advice about the USER's own action; only "should i give/get/
        # add (you)…" is an actual offer to Nexus.
        if re.match(r"^(?:should|shall)\s+i\b", low) and not re.match(
                r"^(?:should|shall)\s+i\s+(?:give|get|add|offer|provide|"
                r"set\s*up|install|teach|show|make|hook\s*up|grant|buy|"
                r"bring|send|share|enable|build|connect|lend|donate|"
                r"let|allow|permit|take|put)\b",
                low):
            return QUESTION
        return PREFERENCE_Q
    if _OFFER_DECL_RE.search(low) and not _INTERROGATIVE_LEAD_RE.match(low):
        return OFFER
    if _COMPARISON_RE.search(low) and (
            low.startswith(("which", "what", "how", "compare"))
            or re.search(r"\bwhich\s+(?:one|of\s+(?:them|these|those))\b|"
                         r"\bvs\.?\b|\bversus\b|\b(?:better|worse|faster|"
                         r"slower|cheaper)\s+than\b", low)):
        return COMPARISON
    # "let me see a castle" / "can I see a photo" / "lemme check" —
    # first-person show-me requests; the ask is FOR Nexus to produce.
    if re.match(r"^(?:(?:can|could|may)\s+i\s+|let\s+me\s+|lemme\s+|"
                r"i\s+(?:want|wanna|would\s+like|'?d\s+like)\s+to\s+|"
                r"i'?d\s+like\s+to\s+)"
                r"(?:see|show|view|watch|look|have|get|check|hear|"
                r"try)\b", low):
        return REQUEST
    # Polite modal request vs ability question — "can you run the tests"
    # is a request; "can you draw" (no object) asks about capability.
    rm = _REQUEST_WRAPPER_RE.match(low)
    if rm:
        rest = low[rm.end():].lstrip()
        verb = rest.split(" ", 1)[0] if rest else ""
        if verb and _ACTION_LEAD_RE.match(verb) and \
                len(rest.split()) > 1:
            return REQUEST
        if verb in _ACTION_WORDS and len(rest.split()) > 1:
            return REQUEST
        return QUESTION
    if _INTERROGATIVE_LEAD_RE.match(low) or clause.rstrip().endswith("?"):
        return QUESTION
    if _WANT_TO_KNOW_RE.search(low):
        return QUESTION
    if _ACTION_LEAD_RE.match(low):
        return COMMAND
    if _COMPLAINT_RE.search(low):
        return COMPLAINT
    return ASSERTION


_ACTION_WORDS = frozenset({
    "create", "make", "write", "edit", "fix", "delete", "remove", "add",
    "build", "run", "execute", "update", "install", "download", "upload",
    "open", "close", "move", "copy", "rename", "push", "pull", "commit",
    "deploy", "save", "start", "stop", "restart", "kill", "set", "turn",
    "mute", "unmute", "pause", "resume", "enable", "disable", "connect",
    "disconnect", "check", "show", "list", "give", "put", "get", "fetch",
    "search", "find", "research", "generate", "draw", "paint", "render",
    "remember", "forget", "tell", "explain", "describe", "summarize",
    "print", "export", "import", "schedule", "post", "share", "clean",
    "sync", "publish", "merge", "test", "verify", "apply", "use", "try",
    "switch", "launch", "revert", "undo", "redo", "reset", "inspect",
    "clone", "answer", "join", "read", "review", "design", "redesign",
    "browse", "visit", "navigate", "click", "type", "press", "scroll",
})


def _roles(clause: str, act: str) -> tuple[str, str]:
    """Rough semantic roles — who acts, on what."""
    t = clause.lower()
    subject = ""
    m = re.match(r"^(i|you|we|they|he|she|it|nexus)\b", t)
    if m:
        subject = m.group(1)
    elif act in (COMMAND, PROHIBITION):
        subject = "nexus"       # imperative — the addressee acts
    elif _REQUEST_WRAPPER_RE.match(t):
        subject = "nexus"
    elif act in (OFFER,):
        subject = "user"
    # Object — the noun phrase after the main verb, best effort.
    obj = ""
    vm = re.search(r"\b(?:give|add|set\s+up|install|create|make|build|"
                   r"get|grant|enable|join|access|use|run|delete|"
                   r"remove|fix|open|push|upload|download)\s+"
                   r"(?:me\s+|you\s+|nexus\s+|it\s+|the\s+|a\s+|an\s+|"
                   r"your\s+|my\s+|new\s+|some\s+)?([^,.;?!—]{1,80})", t)
    if vm:
        obj = vm.group(1).strip()
    return subject, obj


def _adjudicate(frame: SemanticFrame) -> tuple[str, float]:
    """Speech act + requested slot → the semantic intent."""
    act, slot = frame.speech_act, frame.requested_slot
    if act in (OFFER, PREFERENCE_Q):
        return "offer_response", 0.85
    if act == PROHIBITION:
        return "prohibition", 0.85
    if act == HYPOTHETICAL or frame.hypothetical:
        return "hypothetical", 0.8
    if act == COMPARISON:
        return "comparison", 0.8
    if act == GREETING:
        return "greeting", 0.95
    if act in (CONFIRMATION, REFUSAL):
        return act, 0.9
    if slot == SLOT_CAPABILITY_LIST:
        return "capability_inventory", 0.9
    if act == COMPLAINT:
        return "bug_report", 0.75
    if act in (COMMAND, REQUEST):
        if slot in _QA_SLOTS:
            return "question", 0.7
        return "action_request", 0.8
    if act == QUESTION:
        return ({
            SLOT_OPERATIONAL: "status_question",
            SLOT_DEFINITION: "explanation",
            SLOT_EXPLANATION: "explanation",
            SLOT_REASON: "explanation",
            SLOT_LOCATION: "navigation",
            SLOT_ABILITY: "ability_question",
            SLOT_COMPARISON: "comparison",
            SLOT_PREFERENCE: "offer_response",
            SLOT_PERSON: "question",
            SLOT_TIME: "question",
            SLOT_VALUE: "question",
        }.get(slot) or "question"), 0.7
    return "conversation", 0.5


def _candidates(frame: SemanticFrame) -> None:
    """Lexical nominations with evidence — and the rejection record for
    every nominated lane the frame refused."""
    t = frame.masked
    nominated: list[tuple[str, float, str]] = []
    if re.search(r"\bcapabilit|\bfeatures\b|\babilities\b", t):
        nominated.append(("capability_inventory", 0.85,
                          "capability keyword"))
    if _OFFER_Q_RE.search(t) or _OFFER_DECL_RE.search(t):
        nominated.append(("offer_response", 0.85,
                          "offer/preference construction"))
    if _ACTION_LEAD_RE.match(t) or _REQUEST_WRAPPER_RE.match(t):
        nominated.append(("action_request", 0.75, "action verb lead"))
    if _COMPLAINT_RE.search(t):
        nominated.append(("bug_report", 0.7, "failure predicate"))
    if re.search(r"\bgithub\b|\brepo\b|\bgit\b", t):
        nominated.append(("github_status", 0.5, "github/git keyword"))
    if re.search(r"\b(?:image|picture|photo|draw|paint|render)\b", t):
        nominated.append(("image_action", 0.5, "visual keyword"))
    if re.search(r"\b(?:remember|memory|memories|recall)\b", t):
        nominated.append(("memory_recall", 0.5, "memory keyword"))
    if frame.speech_act in (QUESTION,) and frame.requested_slot:
        nominated.append((frame.semantic_intent, 0.7,
                          f"interrogative slot {frame.requested_slot}"))
    nominated.append(("conversation", 0.3, "fallback"))
    for lane, conf, ev in nominated:
        frame.candidates.append(
            {"lane": lane, "confidence": conf, "evidence": ev})
    # Rejection record — a nominated lane loses when it disagrees with
    # the adjudicated intent or the frame vetoes it.
    intent_lane = {
        "capability_inventory": "capability_inventory",
        "offer_response": "offer_response",
        "action_request": "local_action",
        "bug_report": "diagnostics",
        "github_status": "github_status",
        "image_action": "image_action",
        "memory_recall": "memory_recall",
        "conversation": "conversation",
    }
    for c in frame.candidates:
        lane = c["lane"]
        maps = intent_lane.get(lane, lane)
        if lane != frame.semantic_intent and maps != frame.semantic_intent:
            if not frame._lane_ok(lane) or lane == "capability_inventory":
                frame.rejected.append({
                    "lane": lane,
                    "reason": _reject_reason(frame, lane)})


def _reject_reason(frame: SemanticFrame, lane: str) -> str:
    if lane == "capability_inventory" and \
            frame.requested_slot != SLOT_CAPABILITY_LIST:
        if frame.speech_act in (OFFER, PREFERENCE_Q):
            return ("'capabilities' is the offered proposition object, "
                    "not the requested slot")
        if frame.speech_act == PROHIBITION:
            return "capability reference is negated — the ask is a prohibition"
        if frame.hypothetical:
            return "capability reference is inside a hypothetical"
        return (f"requested slot is {frame.requested_slot or 'none'}, "
                "not a capability list")
    if frame.speech_act == PROHIBITION:
        return "utterance is a prohibition"
    if frame.hypothetical:
        return "utterance is hypothetical"
    if frame.speech_act in (OFFER, PREFERENCE_Q):
        return "utterance is an offer/preference question"
    if frame.speech_act == COMPARISON:
        return "utterance asks for a comparison"
    return f"speech act {frame.speech_act} does not claim this lane"
