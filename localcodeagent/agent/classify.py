"""Deterministic request classification for the fast lane.

Separates stable general knowledge from volatile/explicit research requests so
ordinary questions never pay research-preflight cost, while anything that could
be stale (news, prices, current releases) still routes through research.
"""

from __future__ import annotations

import re

# User asked for research explicitly — always honor it.
EXPLICIT_RESEARCH_PATTERNS = (
    r"\bsearch\b", r"\blook ?up\b", r"\blook\b[^.?!]*\bup\b", r"\bresearch\b", r"\bcheck online\b",
    r"\bfind out\b", r"\bgoogle\b", r"\bbrowse\b", r"\bfind the latest\b",
    r"\bweb ?search\b", r"\bcheck the web\b",
)

# Facts that decay: current state of the world the weights cannot know.
VOLATILE_PATTERNS = (
    r"\blatest\b", r"\bnewest\b", r"\brecent(ly)?\b", r"\btoday\b",
    r"\byesterday\b", r"\bthis (week|month|year)\b", r"\bnews\b",
    r"\bcurrent(ly)?\b", r"\bweather\b", r"\bforecast\b",
    r"\b(stock|share|crypto) ?price\b", r"\bprice of\b", r"\bscore\b",
    r"\brelease[sd]?\b", r"\bversion\b.*\b(out|now|latest|current)\b",
    r"\blatest version\b", r"\bupcoming\b", r"\belection\b",
    r"\bwho is the (current|new)\b", r"\bbreaking\b",
)

# Long-form requests get the bigger output budget instead of truncation.
LONG_FORM_PATTERNS = (
    r"\bdetailed\b", r"\bcomprehensive\b", r"\bexhaustive\b", r"\blong\b",
    r"\bin-?depth\b", r"\btutorial\b", r"\bessay\b", r"\bstep[- ]by[- ]step\b",
    r"\bfull guide\b", r"\bwalk ?through\b", r"\beverything about\b",
    r"\bexplain in detail\b", r"\bthorough\b",
)

_explicit_re = re.compile("|".join(EXPLICIT_RESEARCH_PATTERNS), re.IGNORECASE)
_volatile_re = re.compile("|".join(VOLATILE_PATTERNS), re.IGNORECASE)
_long_form_re = re.compile("|".join(LONG_FORM_PATTERNS), re.IGNORECASE)


def research_class(text: str) -> str:
    """Return 'explicit', 'volatile', or 'stable' for research routing."""
    t = text.strip()
    if _explicit_re.search(t):
        return "explicit"
    if _volatile_re.search(t):
        return "volatile"
    return "stable"


def wants_long_form(text: str) -> bool:
    return bool(_long_form_re.search(text))
