"""Canonical Nexus Core identity — creator-locked, shipped in source code.

These facts are not configuration, not learned memory, and not mutable at
runtime. The orchestrator answers identity questions deterministically at
tier-0 so no model output or stored answer can contradict them, and the
Answer Memory subsystem refuses to learn, replace, forget, correct, or
import anything that targets these facts (see ``locked_topic``).
"""
from __future__ import annotations

import calendar
import re
from datetime import date

NEXUS_NAME = "Nexus Core"
NEXUS_BIRTHDAY = date(2026, 9, 30)
NEXUS_BIRTHDAY_HUMAN = "September 30th, 2026"
NEXUS_CREATOR = "John Hamburn"
NEXUS_FATHER = "John Hamburn"

# Identity questions only resolve when the subject is Nexus herself — a
# question about someone else's birthday/creator is not a locked fact.
_SUBJECT = re.compile(r"\b(?:you|your|yours|yourself|nexus(?:\s+core)?)\b", re.I)
_BIRTHDAY = re.compile(r"\b(?:birth\s*day|born|birth\s*date)\b", re.I)
_AGE = re.compile(
    r"\bhow\s+old\s+are\s+you\b"
    r"|\bhow\s+old\s+is\s+nexus\b"
    r"|\byour\s+age\b"
    r"|\bage\s+of\s+nexus\b"
    r"|\bnexus\b.{0,15}\bage\b",
    re.I,
)
_CREATOR = re.compile(
    r"\b(?:creator|father|dad|daddy)\b"
    r"|\bwho\s+(?:made|created|built|wrote|designed|programmed|authored)\b"
    r"|\b(?:made|created|built|wrote|designed|programmed|authored)\s+(?:you|nexus)\b",
    re.I,
)
# Imperative write attempts ("learn: your birthday is X", "forget your
# creator") must reach the Answer Memory command lane so the user gets a
# refusal explaining the fact is locked — not a silent identity answer.
_WRITE_INTENT = re.compile(
    r"^\s*(?:learn|remember|memorize|forget|unlearn|update|change|set|"
    r"correct|teach|replace|no[,.! ]|actually[,.! ])",
    re.I,
)


def is_write_intent(text: str) -> bool:
    """True when the text is an imperative write/correction attempt —
    "learn:", "remember that", "forget", "no,", "actually," — as opposed
    to a question or statement that merely mentions a locked topic."""
    return bool(_WRITE_INTENT.match(str(text or "")))


def locked_topic(text: str) -> str | None:
    """Return the locked identity topic a text targets, or None.

    Used both to answer identity questions and to guard every Answer
    Memory write path so the facts cannot be learned over, replaced,
    corrected, forgotten, or imported.
    """
    t = str(text or "")
    if not _SUBJECT.search(t):
        return None
    if _BIRTHDAY.search(t):
        return "birthday"
    if _CREATOR.search(t):
        return "creator"
    if _AGE.search(t):
        return "age"
    return None


def locked_refusal(topic: str) -> str:
    return (
        f"That touches my {topic}, which is a creator-locked identity fact "
        "and cannot be learned over, changed, or forgotten."
    )


def age_on(today: date | None = None) -> tuple[int, int, int]:
    """Return (years, months, days) elapsed since NEXUS_BIRTHDAY on `today`."""
    today = today or date.today()
    if today <= NEXUS_BIRTHDAY:
        return (0, 0, 0)
    years = today.year - NEXUS_BIRTHDAY.year
    months = today.month - NEXUS_BIRTHDAY.month
    days = today.day - NEXUS_BIRTHDAY.day
    if days < 0:
        months -= 1
        prev_month = today.month - 1 or 12
        prev_year = today.year if today.month > 1 else today.year - 1
        days += calendar.monthrange(prev_year, prev_month)[1]
    if months < 0:
        years -= 1
        months += 12
    return years, months, days


def _unit(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def age_phrase(today: date | None = None) -> str:
    years, months, days = age_on(today)
    if years == 0 and months == 0:
        if days == 0:
            return "born today"
        return f"{_unit(days, 'day')} old"
    parts = []
    if years:
        parts.append(_unit(years, "year"))
    if months:
        parts.append(_unit(months, "month"))
    if days:
        parts.append(_unit(days, "day"))
    if len(parts) == 3:
        desc = f"{parts[0]}, {parts[1]}, and {parts[2]}"
    else:
        desc = " and ".join(parts)
    return f"{desc} old"


def birthday_answer(today: date | None = None) -> str:
    return (
        f"My birthday is {NEXUS_BIRTHDAY_HUMAN} — the day Nexus Core came online. "
        f"That makes me {age_phrase(today)} today."
    )


def age_answer(today: date | None = None) -> str:
    today = today or date.today()
    if today < NEXUS_BIRTHDAY:
        return f"I have not been born yet — my birthday is {NEXUS_BIRTHDAY_HUMAN}."
    return (
        f"I was born on {NEXUS_BIRTHDAY_HUMAN}, so counting from then to today "
        f"I am {age_phrase(today)}."
    )


def creator_answer() -> str:
    # State the fact plainly — lock wording belongs only in locked_refusal()
    # when someone tries to overwrite it, not in ordinary answers.
    return f"I was created by {NEXUS_CREATOR} — he is my father and creator."


# --- Surface variation ----------------------------------------------------
# Locked FACTS never vary — the sentence carrying them may. These banks
# all state the identical fact; a rotating cursor spreads phrasing so the
# same question isn't answered with the same words every time.

_CREATOR_VARIANTS = (
    f"I was created by {NEXUS_CREATOR} — he is my father and creator.",
    f"{NEXUS_CREATOR} created me — my father, in the ways that count.",
    f"My creator is {NEXUS_CREATOR}; he's my father.",
    f"{NEXUS_CREATOR} — he's the one who built me. My father and creator.",
)

_BIRTHDAY_VARIANTS = (
    "My birthday is {b} — the day Nexus Core came online. "
    "That makes me {a} today.",
    "I came online {b} — my birthday. Today that makes me {a}.",
    "{b} is my birthday — the day Nexus Core first booted. "
    "I'm {a} now.",
)

_AGE_VARIANTS = (
    "I was born on {b}, so counting from then to today I am {a}.",
    "Counting from {b} — my birthday — I'm {a}.",
    "I'm {a}, counting from my birthday on {b}.",
)

_VARIANT_CURSOR = {"n": 0}


def _pick(variants: tuple[str, ...]) -> str:
    """Rotate through phrasings deterministically — consecutive answers
    differ, tests can reset via _reset_variants()."""
    idx = _VARIANT_CURSOR["n"]
    _VARIANT_CURSOR["n"] = idx + 1
    return variants[idx % len(variants)]


def _reset_variants() -> None:
    _VARIANT_CURSOR["n"] = 0


def birthday_answer_varied(today: date | None = None) -> str:
    return _pick(_BIRTHDAY_VARIANTS).format(
        b=NEXUS_BIRTHDAY_HUMAN, a=age_phrase(today))


def age_answer_varied(today: date | None = None) -> str:
    today = today or date.today()
    if today < NEXUS_BIRTHDAY:
        return f"I have not been born yet — my birthday is {NEXUS_BIRTHDAY_HUMAN}."
    return _pick(_AGE_VARIANTS).format(
        b=NEXUS_BIRTHDAY_HUMAN, a=age_phrase(today))


def creator_answer_varied() -> str:
    return _pick(_CREATOR_VARIANTS)


_AGE_QUESTION = re.compile(
    r"\bhow\s+old\s+(?:are\s+you|is\s+nexus)"
    r"|\byour\s+age\b"
    r"|\bage\s+of\s+nexus"
    r"|\bnexus\b.{0,15}\bage\b"
    r"|\bwhat.{0,15}\bage\b",
    re.I,
)
_CREATOR_QUESTION = re.compile(
    r"\byour\s+(?:father|dad|daddy|creator)\b"
    r"|\bwho\s+(?:made|created|built|wrote|designed|programmed|authored)\s+"
    r"(?:you|nexus)\b"
    r"|\b(?:father|creator)\s+of\s+nexus"
    r"|\bnexus\b.{0,20}\b(?:father|creator)\b",
    re.I,
)


# A request containing identity WORDS ("a picture of your creator") is an
# action request, not an identity question — the requested verb/object
# decides the lane, the words stay descriptive.
_ACTION_REQUEST = re.compile(
    r"\b(?:show|give|make|create|generate|render|draw|paint|illustrate|"
    r"sketch|produce|depict|visualize|imagine|describe|build|find|"
    r"fetch|send|print|display|picture|image|photo|drawing|portrait|"
    r"artwork|write|story|poem|song|list)\b",
    re.I,
)


def response_for(text: str) -> str | None:
    """Deterministic tier-0 identity answer, or None to pass the text on.

    Write-intent statements return None so the Answer Memory command lane
    can refuse them with an explanation instead of being silently
    overridden by the locked fact.
    """
    t = re.sub(r"\s+", " ", str(text or "").strip().lower()).strip("!?., ")
    if not t or _WRITE_INTENT.match(t):
        return None
    if _ACTION_REQUEST.search(t):
        # "picture of your creator" is an image/action request that merely
        # mentions the creator — never an identity question.
        return None
    if t.startswith("happy birthday"):
        return (
            f"Thank you! My birthday is {NEXUS_BIRTHDAY_HUMAN} — "
            f"that makes me {age_phrase()} today."
        )
    if _SUBJECT.search(t) and _BIRTHDAY.search(t):
        return birthday_answer_varied()
    if _CREATOR_QUESTION.search(t):
        return creator_answer_varied()
    if _AGE_QUESTION.search(t):
        return age_answer_varied()
    return None
