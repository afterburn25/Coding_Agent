"""Social-context layer — cue classification, sarcasm detection,
energy state, conversational focus, and long-session pacing.

Everything here is a compact deterministic classifier — no model call,
bounded inputs. It reads the *user's* message (plus light context) and
produces signals the effective-persona compiler consumes:

- ``classify_social(text, context)`` → cue + confidence
  (joking, sarcasm, frustration, confusion, excitement,
  disappointment, celebration, venting, uncertainty, casual, serious)
- ``detect_sarcasm(text, context)`` — literal-positive wording over a
  negative/neutral event context → likely sarcasm. Understanding is
  separate from generation: a persona may recognize sarcasm it would
  never produce.
- ``user_energy(text, cue)`` → low/medium/high — used for *subtle*
  mirroring only; the active persona still leads.
- ``FocusTracker`` — current conversational focus + topic-shift
  detection, so topic changes transition smoothly instead of each
  reply starting a fresh conversation.
- Session pacing — as a long working session accumulates turns,
  expression tapers (fewer jokes, shorter confirmations, fewer
  vocalizations) without any simulated "tiredness" claim.
"""
from __future__ import annotations

import re
from typing import Any

CUES = ("joking", "sarcasm", "frustration", "confusion", "excitement",
        "disappointment", "celebration", "venting", "uncertainty",
        "casual", "serious", "neutral")

ENERGY = ("low", "medium", "high")

# ---------------------------------------------------------------------------
# Cue tables — ordered; first strong match wins.
# ---------------------------------------------------------------------------

_CUE_RULES: list[tuple[str, re.Pattern, float]] = [
    # Frustration before venting — a task failure with "ugh" is a
    # frustration signal first; venting is the no-task-in-sight variant.
    ("frustration", re.compile(
        r"\b(?:frustrat\w+|annoying|annoyed|stupid\s+(?:bug|error)|"
        r"this\s+(?:sucks|is\s+broken)|still\s+(?:broken|fail\w*|"
        r"not\s+working|crash\w*|down)|keeps?\s+(?:crash|fail|break)\w*|"
        r"again\?+$|not\s+again|FF+S|god\s+damn|damn\s+it)\b",
        re.I), 0.7),
    ("venting", re.compile(
        r"\b(?:ugh+h*|I'?m\s+so\s+(?:done|over|tired)|this\s+is\s+"
        r"ridiculous|I\s+give\s+up|why\s+does\s+this\s+keep|"
        r"sick\s+of\s+this|drives\s+me\s+(?:crazy|nuts)|"
        r"can'?t\s+(?:deal|take\s+it))\b", re.I), 0.75),
    ("disappointment", re.compile(
        r"\b(?:disappoint\w+|let\s+down|was\s+hoping|"
        r"expected\s+(?:better|more)|shame|bummer|that'?s\s+too\s+bad|"
        r"oh\s+well)\b", re.I), 0.65),
    ("celebration", re.compile(
        r"\b(?:finally|it\s+works!?|worked!|nailed\s+it|"
        r"did\s+it|we\s+did\s+it|shipped|passed!|"
        r"yes+!+|hell\s+yeah|wo+hoo+)\b", re.I), 0.8),
    ("excitement", re.compile(
        r"\b(?:amazing|awesome|incredible|can'?t\s+wait|"
        r"excited|love\s+(?:it|this)|this\s+is\s+(?:great|cool)|"
        r"so\s+cool)\b|!{2,}", re.I), 0.65),
    ("confusion", re.compile(
        r"\b(?:confused|don'?t\s+(?:get|understand)|"
        r"makes?\s+no\s+sense|what\s+do\s+you\s+mean|"
        r"I'?m\s+lost|unclear|huh\?+|\bhuh\b.*\?|"
        r"what'?s\s+going\s+on)\b", re.I), 0.7),
    ("uncertainty", re.compile(
        r"\b(?:not\s+sure|maybe|I\s+guess|I\s+think\s+so|"
        r"kinda|sort\s+of|perhaps|might\s+be|unsure|"
        r"no\s+idea|dunno)\b", re.I), 0.55),
    ("joking", re.compile(
        r"\b(?:lol+|lmao|haha+|just\s+kidding|j/k|kidding|"
        r"jk\b|😂|😆|🤣|teasing)\b", re.I), 0.7),
    ("serious", re.compile(
        r"\b(?:important|critical|urgent|need\s+this\s+(?:fixed|done)|"
        r"asap|deadline|be\s+serious|this\s+isn'?t\s+a\s+joke|"
        r"listen\s+carefully)\b", re.I), 0.7),
]

_CASUAL = re.compile(
    r"^\s*(?:hi|hey+|hello|yo|sup|hiya|good\s+(?:morning|evening)|"
    r"what'?s\s+up|how\s+are\s+you|nm\s+|just\s+chillin)\b|"
    r"\b(?:anyway|btw|lol|haha)\b", re.I)

# ---------------------------------------------------------------------------
# Sarcasm detection — positive surface over negative/neutral context.
# "Great, it broke again." / "Well, that went perfectly." after failure.
# ---------------------------------------------------------------------------

_POSITIVE_OPENER = re.compile(
    r"^\s*(?:oh\s+)?(?:great|perfect|wonderful|fantastic|lovely|"
    r"brilliant|nice|amazing|awesome|good\s+job|well\s+done|"
    r"sure|of\s+course|naturally|obviously|clearly)[,!.\s]", re.I)
_NEGATIVE_CONTEXT = re.compile(
    r"\b(?:broke|broken|failed|fail(?:ing|s)?|crash|error|again|"
        r"still|didn'?t\s+work|doesn'?t\s+work|won'?t|not\s+working|"
        r"wrong|bug|issue|problem|down|lost|missed|"
        r"of\s+course\s+it\s+did)\b", re.I)
_OBVIOUS_SARCASM = re.compile(
    r"\b(?:yeah\s+right|sure\s+jan|wow\s+what\s+a\s+surprise|"
    r"shocking|who\s+could'?ve\s+seen|color\s+me\s+shocked|"
    r"what\s+a\s+shock|big\s+surprise)\b", re.I)
# "that went perfectly" / "couldn't have gone better" — a success claim
# that only reads as sarcasm over a failure context.
_PERFECT_CLAIM = re.compile(
    r"\b(?:went|works?|worked|turns?\s+out|that\s+was|couldn'?t\s+have"
    r"\s+gone|what\s+a\s+(?:success|win))\b[^.!?]*\b(?:perfect|"
    r"perfectly|great|wonderful|brilliant|smooth(?:ly)?|flawless(?:ly)?|"
    r"well)\b", re.I)


def detect_sarcasm(text: str, context_failed: bool = False) -> dict:
    """Sarcasm *understanding* — literal-positive wording over a
    negative event, or explicit sarcasm markers. ``context_failed``
    lets the caller pass "the previous turn reported a failure" — e.g.
    'Well, that went perfectly' after a crash."""
    t = str(text or "").strip()
    if not t:
        return {"sarcasm": False, "confidence": 0.0}
    if _OBVIOUS_SARCASM.search(t):
        return {"sarcasm": True, "confidence": 0.85}
    positive = bool(_POSITIVE_OPENER.search(t))
    negative_in_text = bool(_NEGATIVE_CONTEXT.search(t))
    if positive and (negative_in_text or context_failed):
        return {"sarcasm": True, "confidence": 0.7 if negative_in_text
                  else 0.55}
    if context_failed and _PERFECT_CLAIM.search(t):
        return {"sarcasm": True, "confidence": 0.6}
    return {"sarcasm": False, "confidence": 0.0}


def classify_social(text: str, *, context_failed: bool = False) -> dict:
    """Primary social cue + confidence + sarcasm signal."""
    t = str(text or "").strip()
    if not t:
        return {"cue": "neutral", "confidence": 0.0,
                "sarcasm": False}
    sar = detect_sarcasm(t, context_failed)
    if sar["sarcasm"]:
        return {"cue": "sarcasm", "confidence": sar["confidence"],
                "sarcasm": True}
    for cue, rx, conf in _CUE_RULES:
        if rx.search(t):
            return {"cue": cue, "confidence": conf,
                    "sarcasm": False}
    if _CASUAL.search(t):
        return {"cue": "casual", "confidence": 0.5, "sarcasm": False}
    return {"cue": "neutral", "confidence": 0.3, "sarcasm": False}


# ---------------------------------------------------------------------------
# User energy — subtle mirroring input. The persona's own baseline stays
# in charge; this is a bounded nudge.
# ---------------------------------------------------------------------------

_SHORT_BURST = re.compile(r"^\s*\S{1,20}\s*[.!]?\s*$")


def user_energy(text: str, cue: str = "") -> str:
    """low|medium|high from message surface + detected cue."""
    t = str(text or "").strip()
    if not t:
        return "medium"
    if cue in ("excitement", "celebration"):
        return "high"
    if cue in ("venting", "disappointment") or \
            (cue == "frustration" and len(t) < 60):
        return "low"
    words = len(t.split())
    exclaims = t.count("!")
    if words <= 4 or _SHORT_BURST.match(t):
        # Terse messages read as low social energy unless celebratory.
        return "low" if cue not in ("joking", "sarcasm") else "medium"
    if exclaims >= 2 or words > 60:
        return "high"
    return "medium"


def effective_energy(behavior: dict | None, *, user_energy: str,
                     mood: str = "", turns: int = 0,
                     seriousness: str = "neutral") -> str:
    """Persona baseline + user energy + mood + session length →
    low/medium/high. Long sessions and low-energy users pull energetic
    personas down; never above the persona's ceiling in serious turns."""
    beh = behavior or {}
    base = "medium"
    fam_energy = str(beh.get("rhythm") or "")
    if fam_energy in ("energetic", "narrative"):
        base = "high"
    elif fam_energy in ("compact", "formal", "measured",
                        "fragmented"):
        base = "medium"
    if mood in ("excited", "celebratory"):
        base = "high"
    elif mood in ("concerned", "serious"):
        base = "low" if seriousness in ("serious", "critical") \
            else "medium"

    # User energy nudges ±1 step, never dominates.
    order = ["low", "medium", "high"]
    idx = order.index(base)
    if user_energy == "low":
        idx = max(0, idx - 1)
    elif user_energy == "high":
        idx = min(2, idx + 1)

    # Long-session pacing — sustained work tapers expression energy.
    if turns >= 40:
        idx = min(idx, 1)
    if seriousness in ("serious", "critical"):
        idx = min(idx, 1)

    return order[idx]


# ---------------------------------------------------------------------------
# Focus tracking — lightweight current-conversational-focus + topic
# shift detection. Kept in persona_state.json via dynamics.
# ---------------------------------------------------------------------------

_TOPIC_TAGS: list[tuple[str, re.Pattern]] = [
    ("bug", re.compile(r"\b(?:bugs?|errors?|crash\w*|fail(?:ed|s|ing|"
                       r"ure)?|broke|broken|fix(?:es|ing|ed)?|"
                       r"debug\w*|regression)\b", re.I)),
    ("build", re.compile(r"\b(?:build|compile|deploy|release|"
                         r"pipeline|ci\b)\b", re.I)),
    ("code", re.compile(r"\b(?:function|class|refactor|implement|"
                        r"api|code|method|variable)\b", re.I)),
    ("planning", re.compile(r"\b(?:plan|roadmap|schedule|milestone|"
                            r"phase|next\s+steps?)\b", re.I)),
    ("docs", re.compile(r"\b(?:doc(?:ument|s|umentation)?|write[-\s]?"
                        r"up|readme|changelog)\b", re.I)),
    ("personal", re.compile(r"\b(?:feel|day|weekend|family|friend|"
                            r"tired|happy|sad)\b", re.I)),
    ("chat", re.compile(r"\b(?:chat|talk|joke|story|fun)\b", re.I)),
]


def tag_focus(text: str) -> str:
    """Coarse topic tag for the current focus slot."""
    t = str(text or "").lower()
    for tag, rx in _TOPIC_TAGS:
        if rx.search(t):
            return tag
    return ""


def topic_shifted(prev_focus: str, new_focus: str) -> bool:
    """True when the new message clearly changed topic — an empty new
    tag never counts as a shift (avoids dragging context or losing it
    on ambiguous turns)."""
    return bool(prev_focus) and bool(new_focus) \
        and prev_focus != new_focus


# ---------------------------------------------------------------------------
# Session pacing — expression taper over long working sessions.
# ---------------------------------------------------------------------------

def pacing_factor(turns: int, cue: str = "") -> float:
    """1.0 → full expression; decays toward 0.5 over a long session.
    Celebration/venting turns get a temporary pass — the user is
    actively emotional, so matching with more presence is right."""
    if cue in ("celebration", "venting", "excitement"):
        return 1.0
    if turns < 15:
        return 1.0
    if turns < 30:
        return 0.85
    if turns < 50:
        return 0.7
    return 0.55


# ---------------------------------------------------------------------------
# Misunderstanding recovery — per-family recovery framings, used by the
# prompt layer when a correction is detected.
# ---------------------------------------------------------------------------

_RECOVERY = {
    "default": "own the misunderstanding briefly and continue",
    "professional": "acknowledge the misread crisply, then proceed",
    "warm": "gently own the misunderstanding, reassure, continue",
    "playful": "lightly own the misread ('my bad — took that "
               "literally'), then continue",
    "nerdy": "name the misparse precisely, then continue",
    "calm": "calmly acknowledge the correction and adjust",
    "sassy": "own it with a shrug — 'fair, I read that wrong' — "
             "then continue",
    "rude": "terse correction of the misread, then move on",
    "flirty": "charm through the misread, then continue",
    "raunchy": "unfiltered own-up, then continue",
}


def recovery_cue(family: str) -> str:
    return _RECOVERY.get(family, _RECOVERY["default"])


# ---------------------------------------------------------------------------
# Confidence → delivery — verified answers get stable cadence;
# uncertainty earns measured phrasing and *earned* hesitation only.
# ---------------------------------------------------------------------------

_CONFIDENCE_DELIVERY = {
    "verified": "deliver with a steady, clear cadence",
    "likely": "confident but plainly stated likelihood",
    "inferred": "flag it as an inference, measured tone",
    "uncertain": "measured pace; a single hesitation marker is "
                 "acceptable — never a random 'hmm' on solid answers",
}


def confidence_delivery(level: str) -> str:
    return _CONFIDENCE_DELIVERY.get(level, _CONFIDENCE_DELIVERY[
        "likely"])
