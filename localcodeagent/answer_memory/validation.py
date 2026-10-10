"""Noise filtering, secret protection, and cacheability classification."""

from __future__ import annotations

import re

from .normalization import normalize_question

_NOISE = {
    "hi", "hello", "hey", "hey there", "yo", "sup", "ok", "okay", "k", "kk",
    "yes", "no", "yep", "nope", "sure", "thanks", "thank you", "thx", "ty",
    "cool", "nice", "great", "good", "fine", "lol", "hmm", "hm", "test",
    "bye", "goodbye", "good night", "good morning", "good afternoon",
}

# Utterances that only mean something relative to the previous assistant
# turn — "do it", "yes go ahead", "the second one". They must never be
# learned or resolved as standalone questions: the referent lives in live
# conversation state, and a stored Q/A pair injects a stale answer as a
# "possibly relevant" hint or replays an earlier exchange verbatim.
_CONTEXT_DEPENDENT_RE = re.compile(
    r"^\s*(?:"
    r"yes|yeah|yep|yup|ya|yea|sure|ok(?:ay)?|kk|alright|fine|cool|"
    r"affirmative|absolutely|definitely|of course|please do|"
    r"do it|do that|do this|go ahead|go for it|sounds good|"
    r"let'?s do it|proceed|continue|carry on|keep going|resume|"
    r"why not|no|nope|nah|negative|don'?t|do not|"
    r"never ?mind|cancel(?: that)?|skip it|forget (?:it|that)|"
    r"that one|this one|the (?:first|second|third|last) one|"
    r"(?:first|second|third|last) one|both|all of them|neither|either one|"
    r"same(?: thing)?|again|retry|try again|once more|"
    # Bare interjections/profanity — pure emotional context, nothing to
    # match a stored question against.
    r"wtf|wth|huh|lol|lmao|rofl|haha+|omg|ugh|wow|damn+|shit|crap|"
    r"fuck(?:\s+(?:you|this|that|off|it|me|sake))?|"
    r"what\s+the\s+\w+|the\s+hell"
    r")(?:[\s,]+(?:yes|yeah|please|ok(?:ay)?|sure|do it|go ahead|that|"
    r"them|continue|carry on|keep going|resume|go on|then|now))*"
    r"[\s.!?,]*$",
    re.I,
)


# Discourse references — the ask's referent is the live conversation
# itself ("what were we talking about", "summarize our chat", "back to
# the voice", "did we settle on a port"). No stored Q/A pair can answer
# these: the correct content lives in the current history and injected
# memory, and replaying an earlier exchange injects a stale answer from
# an unrelated topic (the observed "As before — Voice: TTS…" reply to a
# summarize-the-chat ask).
_DISCOURSE_REF_RE = re.compile(
    r"\bwhat\b[^.?!]{0,40}\bwe\b|"
    r"\bwhere (?:were|did) we\b|"
    r"\bwe (?:were|was) (?:just )?(?:talking|discussing|saying|working)\b|"
    r"\b(?:summari[sz]e|recap|repeat|go back over|go over)\b[^.?!]{0,40}"
    r"\b(?:chat|conversation|discuss\w*|talking|said|covered|went over|"
    r"we were|we just|last thing|earlier|before)\b|"
    r"\bdid we (?:ever )?(?:settle|decide|agree|pick|choose|land|"
    r"end up|figure)\b|"
    r"\b(?:the|that) (?:thing|stuff|issue|topic|one|part|option|file|"
    r"version|preset|name)\s+(?:from |we |that )?(?:earlier|before|"
    r"last time|just now)\b|"
    r"\bback to (?:the|our|what)\b|"
    r"\bdid\s+i\s+(?:just\s+)?(?:say|mention|tell|ask|name|call)\b|"
    # Second-person utterance recall — the referent is something the
    # assistant said in THIS conversation: "what did you just say",
    # "you said a port earlier", "your last suggestion". No stored
    # answer can resolve a reference into live history.
    r"\b(?:did|do|would|could)\s+you\s+(?:just\s+)?(?:say|mean|tell|"
    r"mention|show|explain|claim|promise|suggest|recommend)\b|"
    r"\byou\s+(?:just\s+)?(?:said|mentioned|told|meant|suggested)\b|"
    r"\bwhat\s+you\s+(?:just\s+)?(?:said|meant|told|promised|"
    r"suggested)\b|"
    r"\b(?:your|the)\s+(?:last|previous|earlier)\s+(?:answer|reply|"
    r"response|message|point|suggestion|recommendation|idea)\b|"
    r"\b(?:what|which)\b[^.?!]{0,30}\b(?:again|earlier|before|"
    r"just now)\b|"
    r"\bthe (?:first|second|other|last) (?:thing|one|part|option)\b",
    re.I)


def is_context_dependent(text: str) -> bool:
    """True when the message only resolves against live conversation
    context — affirmatives, deictic picks, bare continue/cancel, and
    discourse references to the conversation itself."""
    t = str(text or "")
    return bool(_CONTEXT_DEPENDENT_RE.match(t)
                or _DISCOURSE_REF_RE.search(t))


def references_conversation(text: str) -> bool:
    """True when the turn's referent is the conversation itself —
    summary/recall/discourse asks whose answer lives in live history
    ("summarize what we were talking about", "did we settle on a port").
    Deterministic lanes (settings, status, Answer Memory) can never
    carry that referent."""
    return bool(_DISCOURSE_REF_RE.search(str(text or "")))

# Secret / credential indicators — suppress persistent learning entirely.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret)\b\s*[:=]\s*[^\s]{6,}", re.I),
    re.compile(r"\b(?:password|passwd|pwd|passcode|pass[\s_-]?phrase)\b"
               r"\s*[:=]\s*[^\s]{4,}", re.I),
    re.compile(r"\b(?:my\s+)?(?:password|passwd|pwd|passcode|"
               r"pass[\s_-]?phrase|pin|pin\s*code)\s+is\s+\S{3,}", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b|\bgho_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:session|cookie)\s*[:=]\s*[^\s]{10,}", re.I),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),  # long hex blobs (keys/tokens)
    re.compile(r"\b(?:recovery|backup)\s+codes?\b\s*[:=]", re.I),
]

# Assistant-side failure text — "I couldn't reach GitHub — unknown tool
# 'x'", permission/infra refusals, tool errors. These are runtime events,
# not knowledge: storing them learns "the answer to 'explain github' is a
# connection error", which then replays to unrelated questions.
_ANSWER_FAILURE_RE = re.compile(
    r"\bunknown tool\b|"
    r"\bi\s*(?:'m|am)?\s*(?:sorry\s*,?\s*|unfortunately\s*,?\s*)?"
    r"(?:couldn['’]?t|could not|can['’]?t|cannot|wasn['’]?t able|"
    r"was not able|failed|am unable)\s*(?:to\s+)?"
    r"(?:reach|connect|contact|fetch|retrieve|load|access|open|"
    r"run|execute|download|pull|complete|perform)\b|"
    r"\bthe (?:request|tool call|command|operation|lookup) "
    r"(?:failed|errored|timed out)\b|"
    r"^\s*(?:error[:\s]|ap(?:proval_required)|permission_denied|"
    r"creator_|tool_)\b",
    re.I)


def is_error_answer(text: str) -> bool:
    """True when the text is an assistant failure report rather than an
    answer — infra errors, permission refusals, 'couldn't reach X'. These
    must never be learned or replayed: the failure is transient, and a
    cached error parrots forever."""
    return bool(_ANSWER_FAILURE_RE.search(str(text or "")))


# Answers that cannot stand alone — continuation fragments produced
# mid-thread while resolving a parked clarification ("— no auto",
# "… the second option"). As a canonical Q/A they are meaningless, and
# injected as hints they read as non-sequiturs on unrelated questions.
_FRAGMENT_ANSWER_RE = re.compile(r"^\s*(?:—|–|\.\.\.|…|;|-{2,})")


def is_fragment_answer(text: str) -> bool:
    """True when the reply is a mid-thread fragment, not a standalone
    answer — a leading continuation marker ("— no auto", "… the second
    one") literally cannot begin a standalone reply. Bare short answers
    ("42", "yes", "harmless") stay learnable — they can be canonical."""
    t = str(text or "").strip()
    if not t:
        return True
    return bool(_FRAGMENT_ANSWER_RE.match(t))


# Machine-authored work-order scaffolds — mission/autonomy lanes issue
# fixed template prompts ("Work the scoped lane of this mission…",
# "A mission task failed. Diagnose the concrete cause…",
# "Apply the diagnosis to make progress on the mission objective…").
# A response to a template is mission evidence, not a user question;
# replaying it as a learned answer injects mission state into chats.
_MACHINE_QUESTION_RE = re.compile(
    r"^\s*(?:work the scoped lane of (?:this|the) mission\b|"
    r"a mission task failed\b[.\s]*diagnose\b|"
    r"apply the diagnosis to make progress on the mission\b)",
    re.I | re.S)


def is_machine_authored_question(text: str) -> bool:
    """True when the stored 'question' is a machine-authored work-order
    scaffold rather than something a user asked — mission traffic must
    never be replayed as user-facing memory (BUG-011/016 lineage)."""
    return bool(_MACHINE_QUESTION_RE.match(str(text or "")))


# Capability probes — "can you see my screen", "can you browse websites",
# "can you hear me". The truthful answer lives in live capability state;
# a cached answer goes stale the moment a subsystem toggles. Classify as
# volatile so they are neither learned nor replayed from memory.
_CAPABILITY_PROBE_RE = re.compile(
    r"^\s*(?:can|could|are|do|did|will|would|is|were)\s+(?:you|u)\s+"
    r"(?:currently\s+|still\s+|actually\s+|really\s+|able\s+to\s+)?"
    r"(?:see|browse|surf|visit|access|open|hear|view|watch|listen|"
    r"read|use|connect|control|click|type|look|check|execute|edit|"
    r"remember|record|monitor|speak|talk|generate|draw|search)\b",
    re.I)

_LIVE_MARKERS = (
    "weather", "forecast", "stock price", "share price", "crypto price",
    "bitcoin price", "score of", "who won", "game score", "traffic",
    "right now", "live score", "current temperature", "exchange rate",
    "news today", "breaking news", "current time", "time is it",
)

_TASK_MARKERS = (
    "debug this", "fix this", "rewrite this", "refactor this",
    "review this", "summarize this", "translate this", "explain this code",
    "this file", "this error", "this stack trace", "the attached",
    "analyze this image", "this screenshot", "above code", "this diff",
)

_QUESTION_STARTERS = (
    "what", "where", "when", "who", "whom", "whose", "which", "why", "how",
    "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "should", "would", "will", "may", "tell me", "explain", "describe",
    "list", "define", "show me",
)

# "Can you build me an app?" looks like a question but is a request for
# work — no stored answer is a valid reply, and a capability/self-intro
# answer surfaced as a "hint" just gets parroted by small models. Action
# verbs after a polite modal, an explicit "I need/want you to …" order,
# or an imperative opener all mark the turn task-specific.
_ACTION_VERBS = (
    "build", "make", "create", "write", "code", "fix", "debug", "add",
    "change", "update", "deploy", "run", "install", "setup", "set up",
    "generate", "design", "implement", "refactor", "test", "do", "get",
    "find", "open", "delete", "remove", "modify", "patch", "upgrade",
    "migrate", "scaffold", "compile", "review", "edit", "rename",
    "download", "configure", "program", "develop", "draw",
)
_ACTION_REQUEST = re.compile(
    r"(?:^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:"
    + "|".join(_ACTION_VERBS) + r")\b"
    r"|^i\s+(?:need|want|would like|need you)\s+you\s+to\s+(?:"
    + "|".join(_ACTION_VERBS) + r")\b"
    r"|^i'?d?\s+rather\s+you\s+(?:" + "|".join(_ACTION_VERBS) + r")\b"
    r"|^(?:please\s+)?(?:" + "|".join(_ACTION_VERBS)
    + r")\s+(?:me\s+|an?\s+|the\s+|my\s+|this\s+|that\s+))",
    re.I)


def is_noise(text: str) -> bool:
    norm = normalize_question(text)
    if not norm or len(norm) < 4:
        return True
    if norm in _NOISE:
        return True
    if is_context_dependent(text):
        return True
    if not re.search(r"[a-z0-9]", norm):
        return True
    return False


def contains_secret(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _SECRET_PATTERNS)


def classify_cacheability(text: str) -> str:
    """Deterministic cacheability: reusable | contextual | volatile | live |
    transformation | task_specific."""
    t = normalize_question(text)
    if not t:
        return "task_specific"
    if is_context_dependent(text):
        return "task_specific"
    if _CAPABILITY_PROBE_RE.search(str(text or "")):
        return "volatile"
    if any(m in t for m in _LIVE_MARKERS):
        return "live"
    raw = str(text)
    if (
        len(raw) > 1500
        or "```" in raw
        or re.search(r"Traceback \(most recent call last\)", raw)
        or re.search(r"\b\w+Error\b.*\n.*\n", raw)
    ):
        return "task_specific"
    if any(m in t for m in _TASK_MARKERS):
        return "task_specific"
    if re.match(r"^(rewrite|translate|summari[sz]e|paraphrase|reformat|convert)\b", t):
        return "transformation"
    if _ACTION_REQUEST.search(t):
        return "task_specific"
    if re.search(r"\b(latest|newest|current|recent|today's|this week)\b", t):
        return "volatile"
    first = t.split(" ", 1)[0] if t else ""
    if first in _QUESTION_STARTERS or t.endswith("?") or any(
        t.startswith(s + " ") for s in _QUESTION_STARTERS
    ):
        return "reusable"
    if "?" in t:
        return "reusable"
    return "contextual"
