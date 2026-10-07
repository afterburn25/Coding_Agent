"""Feedback and correction detection for Answer Memory.

Conservative on purpose: casual phrases containing "wrong" must not invalidate
answers, and "thanks" alone must not verify arbitrary facts.
"""

from __future__ import annotations

import re

_CORRECTION_PATTERNS = [
    r"^no[,.]?\s+that's\s+(?:wrong|incorrect|not right|outdated)",
    r"^no[,.]?\s+that\s+is\s+(?:wrong|incorrect|not\s+right|outdated)",
    r"^that'?s\s+(?:wrong|incorrect|not right|outdated|not correct)\b",
    r"^you'?re\s+wrong\b",
    r"^wrong\b",
    r"^incorrect\b",
    r"^that'?s\s+not\s+(?:right|correct|true)\b",
    r"^actually[,]?\s+",
    r"^no[, ]+the\s+answer\s+is",
    r"^no[,.]?\s+i\s+meant\b",
    r"^i\s+meant\b",
    r"^the\s+(?:correct|right)\s+answer\s+is",
    r"^it\s+should\s+be",
    r"^correction\s*:",
    # "X is/was Y not Z" — an explicit replacement assertion.
    r"^(?:it|that|this)\s+(?:is|was)\s+.+\s+not\s+\S",
    r"^the\s+\w[\w .-]{0,30}?\s+(?:is|was)\s+.+\s+not\s+\S",
    r"^that'?s\s+outdated\b",
    r"^don'?t\s+answer\s+that\s+again\b",
]

_POSITIVE_PATTERNS = [
    r"^that'?s\s+(?:right|correct|exactly\s+right)\b",
    r"^correct[.!]?$",
    r"^exactly[.!]?$",
    r"^perfect[.!]?$",
    r"^yes[,]?\s+that'?s\s+(?:right|correct)\b",
    r"^you\s+got\s+it\b",
]

_LEARN_PATTERNS = [
    r"^learn\s+(?:this|that|the)\s+answer",
    r"^remember\s+(?:this|that|the)\s+answer",
    r"^save\s+(?:this|that|the)\s+answer",
    r"^learn\s+that\b",
    r"^remember\s+that\b",
    r"^that's\s+correct[,]?\s+remember\b",
]

_FORGET_PATTERNS = [
    r"^forget\s+(?:the\s+)?answer\s+(?:for|to)\s+",
    r"^forget\s+that\s+answer\b",
    r"^don'?t\s+use\s+this\s+answer\b",
    r"^unlearn\s+",
]

_WHEN_ASK = re.compile(
    r"^when\s+i\s+ask\s+(.+?)(?:,|\s+)?\s*(?:answer|respond|reply|say|tell\s+me)\s*[:\-]?\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_REPLACE = re.compile(
    r"^replace\s+the\s+answer\s+(?:for|to)\s+(.+?)\s+with\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_FORGET_Q = re.compile(
    r"^forget\s+the\s+answer\s+(?:for|to)\s+(.+)$", re.IGNORECASE | re.DOTALL
)
_WHAT_LEARNED = re.compile(
    r"^(?:what\s+have\s+you\s+learned|show\s+(?:me\s+)?(?:the\s+)?learned\s+answers|"
    r"what\s+do\s+you\s+remember)\s*(?:about\s+(.+))?$",
    re.IGNORECASE | re.DOTALL,
)
_WHY_MEMORY = re.compile(
    r"^(?:why\s+did\s+you|did\s+you)\s+(?:answer|respond|reply)\s*"
    r"(?:this|that)?\s*from\s+memory"
    r"|^was\s+that\s+(?:answered\s+)?from\s+memory"
    r"|^did\s+that\s+come\s+from\s+(?:your\s+)?memory",
    re.IGNORECASE,
)
_LEARN_PAIR = re.compile(
    r"^(?:learn|remember|save)\s*:?\s*(.+?)\s*(?:->|=>|→)\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def is_correction(text: str) -> bool:
    t = str(text or "").strip().lower()
    return any(re.match(p, t) for p in _CORRECTION_PATTERNS)


def is_positive(text: str) -> bool:
    t = str(text or "").strip().lower()
    # "thanks" alone is deliberately NOT positive verification.
    return any(re.match(p, t) for p in _POSITIVE_PATTERNS)


def is_negative(text: str) -> bool:
    return is_correction(text)


def is_learn_command(text: str) -> bool:
    t = str(text or "").strip().lower()
    return any(re.match(p, t) for p in _LEARN_PATTERNS)


def is_forget_command(text: str) -> bool:
    t = str(text or "").strip().lower()
    return any(re.match(p, t) for p in _FORGET_PATTERNS)


def correction_answer(text: str) -> str:
    """Extract the replacement answer from a correction, if one is embedded.

    "No, the answer is X" / "it should be X" / "actually X" → X
    """
    t = str(text or "").strip()
    for pat in (
        r"the\s+(?:correct|right)\s+answer\s+is\s*[:\-]?\s*(.+)$",
        r"the\s+answer\s+is\s*[:\-]?\s*(.+)$",
        r"it\s+should\s+be\s*[:\-]?\s*(.+)$",
        r"correction\s*:\s*(.+)$",
        r"actually[,]?\s+(.+)$",
    ):
        m = re.search(pat, t, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()
    return ""


def correction_question(text: str) -> str:
    """Extract which prior answer a correction targets, e.g.

    "No, Qwen isn't the default anymore" → corrected statement, not a question;
    returns '' so the caller falls back to the previous exchange's question.
    """
    return ""


def parse_command(text: str) -> dict | None:
    """Map a natural-language memory command onto an operation."""
    t = str(text or "").strip()
    m = _WHEN_ASK.match(t)
    if m:
        return {"op": "learn", "question": m.group(1).strip().rstrip("?."), "answer": m.group(2).strip()}
    m = _LEARN_PAIR.match(t)
    if m:
        return {"op": "learn", "question": m.group(1).strip().rstrip("?."), "answer": m.group(2).strip()}
    m = _REPLACE.match(t)
    if m:
        return {"op": "replace", "question": m.group(1).strip().rstrip("?."), "answer": m.group(2).strip()}
    m = _FORGET_Q.match(t)
    if m:
        return {"op": "forget", "question": m.group(1).strip().rstrip("?.")}
    m = _WHAT_LEARNED.match(t)
    if m:
        return {"op": "list", "topic": (m.group(1) or "").strip()}
    if _WHY_MEMORY.match(t):
        return {"op": "explain"}
    if is_learn_command(t):
        return {"op": "learn_last"}
    if is_forget_command(t):
        return {"op": "forget", "question": ""}
    return None
