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


def is_noise(text: str) -> bool:
    norm = normalize_question(text)
    if not norm or len(norm) < 4:
        return True
    if norm in _NOISE:
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
