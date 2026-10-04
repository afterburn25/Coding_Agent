"""Seriousness + topic classification — deterministic, no model call.

Two tiny classifiers that steer persona *expression*:

- ``classify_seriousness(text)`` → casual|neutral|focused|serious|
  critical. Humor, sass, vocalizations and gestures scale down as
  seriousness rises; facts and task behavior never change.
- ``classify_topic(text)`` → one of behavior.TOPICS (casual, technical,
  diagnostics, personal, review, operational, discovery, creative).

Both are keyword/pattern tables — deliberately simple, cheap, and
explainable. They never gate tools or permissions.
"""
from __future__ import annotations

import re

SERIOUSNESS = ("casual", "neutral", "focused", "serious", "critical")

# Highest precedence first — critical safety/failure language outranks
# casual markers even when both appear.
_CRITICAL = re.compile(
    r"\b(?:data\s+loss|lost\s+(?:all|my)\s+(?:data|work|files)|"
    r"security\s+(?:breach|incident|vulnerability)|"
    r"credential|password\s+leak|breach(?:ed)?|ransomware|"
    r"production\s+(?:is\s+)?down|down\s+in\s+prod|"
    r"wiped|deleted\s+everything|corrupt(?:ed|ion)|"
    r"can'?t\s+recover|unrecoverable|bricked|"
    r"irreversible|catastroph\w+|emergency)\b", re.I)

_SERIOUS = re.compile(
    r"\b(?:crash(?:ed|ing)?|broken|keeps?\s+fail|still\s+fail|"
    r"won'?t\s+(?:start|work|boot|launch)|doesn'?t\s+work|"
    r"lost\s+(?:my|the)|deadline|urgent|asap|"
    r"production|deploy(?:ed|ment)?\s+fail|outage|"
    r"error\s+(?:every|again|still)|same\s+error|"
    r"i'?m\s+(?:stuck|frustrated)|this\s+is\s+frustrating|"
    r"nothing\s+works|keeps?\s+(?:crash|fail|break))\b", re.I)

_FOCUSED = re.compile(
    r"\b(?:fix|debug|implement|refactor|deploy|review|migrate|"
    r"investigate|diagnose|reproduce|regression|failing|"
    r"build\s+the|write\s+(?:a|the)\s+(?:test|code|function)|"
    r"pull\s+request|commit|merge|pipeline|deadline)\b", re.I)

_CASUAL = re.compile(
    r"^\s*(?:hi|hey+|hello|yo|sup|good\s+(?:morning|afternoon|evening)|"
    r"lol|haha+|lmao|thanks|thank\s+you|nice|cool|awesome|"
    r"how\s+are\s+you|what'?s\s+up|gm|gn)\b|\b(?:lol|haha+|lmao|"
    r"joke|funny|chat(?:ting)?|just\s+checking\s+in|"
    r"how'?s\s+it\s+going)\b", re.I)

_TOPIC_RULES: list[tuple[str, re.Pattern]] = [
    ("diagnostics", re.compile(
        r"\b(?:crash(?:ed|es|ing)?|errors?|stack\s*trace|traceback|"
        r"exception|segfault|fail(?:ed|s|ing|ure)?|bugs?|debug|"
        r"regression|log\s+(?:shows|says)|stacktrace)\b", re.I)),
    ("operational", re.compile(
        r"\b(?:deploy|status|uptime|server|service|restart|"
        r"rollback|queue|job|pipeline|health|monitor)\b", re.I)),
    ("review", re.compile(
        r"\b(?:review|code\s+review|pull\s+request|pr\s+feedback|"
        r"audit|lint|critique)\b", re.I)),
    ("technical", re.compile(
        r"\b(?:function|class|api|algorithm|database|sql|regex|"
        r"compile|runtime|memory\s+leak|async|thread|"
        r"implement|refactor|architecture|docker|kubernetes|"
        r"python|javascript|typescript|rust|golang|c\+\+)\b", re.I)),
    ("personal", re.compile(
        r"\b(?:feel(?:ing)?|sad|happy|angry|upset|tired|"
        r"rough\s+day|bad\s+day|stressed|anxious|worried|"
        r"congrat|birthday|weekend|family|friend)\b", re.I)),
    ("discovery", re.compile(
        r"\b(?:found\s+(?:it|the)|figured\s+it|discovered|"
        r"turns?\s+out|interesting|breakthrough|works!\s*$)\b",
        re.I)),
    ("creative", re.compile(
        r"\b(?:write\s+(?:a|the)\s+(?:story|poem|song|script)|"
        r"brainstorm|imagine|design\s+a\s+logo|creative)\b",
        re.I)),
    ("casual", _CASUAL),
]

# How strongly persona expression scales per seriousness level.
EXPRESSION_SCALE = {"casual": 1.0, "neutral": 0.9, "focused": 0.65,
                    "serious": 0.3, "critical": 0.1}

# Extra instructions per level appended to the prompt card.
SERIOUSNESS_GUIDANCE = {
    "casual": "Tone: casual exchange — full persona color is welcome.",
    "neutral": "Tone: normal exchange — persona applies as usual.",
    "focused": ("Tone: focused work — keep persona color light; "
                "humor and flourishes only if they don't cost words."),
    "serious": ("Tone: serious situation — drop humor, teasing, and "
                "flourishes; be direct and steady."),
    "critical": ("Tone: critical/emergency context — plain, calm, "
                 "direct language only; no humor, no persona color."),
}


def classify_seriousness(text: str) -> str:
    """casual|neutral|focused|serious|critical — first match by
    severity precedence wins (critical > serious > focused > casual)."""
    t = str(text or "").strip()
    if not t:
        return "neutral"
    if _CRITICAL.search(t):
        return "critical"
    if _SERIOUS.search(t):
        return "serious"
    if _FOCUSED.search(t):
        return "focused"
    if _CASUAL.search(t):
        return "casual"
    return "neutral"


def classify_topic(text: str) -> str:
    """Best-match domain for topic_shift cues; 'casual' as fallback."""
    t = str(text or "").strip()
    if not t:
        return "casual"
    for topic, rx in _TOPIC_RULES:
        if rx.search(t):
            return topic
    return "casual"
