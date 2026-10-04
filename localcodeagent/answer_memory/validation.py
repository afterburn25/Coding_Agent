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
    r"same(?: thing)?|again|retry|try again|once more"
    r")(?:[\s,]+(?:yes|yeah|please|ok(?:ay)?|sure|do it|go ahead|that|them))*"
    r"[\s.!?,]*$",
    re.I,
)


def is_context_dependent(text: str) -> bool:
    """True when the message only resolves against live conversation
    context — affirmatives, deictic picks, bare continue/cancel."""
    return bool(_CONTEXT_DEPENDENT_RE.match(str(text or "")))

# Secret / credential indicators — suppress persistent learning entirely.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret)\b\s*[:=]\s*[^\s]{6,}", re.I),
    re.compile(r"\b(?:password|passwd|pwd)\b\s*[:=]\s*[^\s]{4,}", re.I),
    re.compile(r"\b(?:my\s+)?(?:password|passwd|pwd)\s+is\s+\S{3,}", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b|\bgho_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:session|cookie)\s*[:=]\s*[^\s]{10,}", re.I),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),  # long hex blobs (keys/tokens)
    re.compile(r"\b(?:recovery|backup)\s+codes?\b\s*[:=]", re.I),
]

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
