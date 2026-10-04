"""Natural-language persona commands — deterministic parser.

Recognizes explicit style commands like::

    "be less sarcastic"                 → persistent overlay offset
    "be more playful tonight"           → temp modifier (time)
    "use serious mode for the next hour"→ mode modifier
    "be more concise for this task"     → temp modifier (task scope)
    "reset personality"                 → clear modifiers + overlay
    "stop being so formal"              → overlay offset

Deliberately strict patterns — a loose match could swallow a real chat
message. Ambiguous mid-conversation phrasing falls through to the agent.
"""
from __future__ import annotations

import re
from typing import Any

from . import schema

# Words users say → slider keys (aliases kept tight on purpose).
_TRAIT_ALIASES: dict[str, str] = {
    "sarcastic": "sarcasm", "sarcasm": "sarcasm",
    "sassy": "sass", "sass": "sass",
    "funny": "humor", "humorous": "humor", "humor": "humor",
    "playful": "playfulness", "silly": "silliness",
    "goofy": "goofiness", "serious": "humor",   # "more serious" → -humor
    "formal": "formality", "casual": "formality",
    "warm": "warmth", "friendly": "friendliness",
    "nice": "friendliness", "mean": "rudeness",
    "rude": "rudeness", "polite": "rudeness",
    "talkative": "verbosity", "verbose": "verbosity",
    "concise": "verbosity", "brief": "verbosity", "short": "verbosity",
    "chatty": "verbosity", "quiet": "verbosity",
    "curious": "curiosity", "nerdy": "nerdiness",
    "technical": "technical_depth", "detailed": "detail_orientation",
    "enthusiastic": "enthusiasm", "energetic": "energy",
    "calm": "calmness", "dramatic": "dramatic_flair",
    "emotional": "emotional_expressiveness",
    "direct": "directness", "blunt": "directness",
    "flirty": "flirtiness", "teasing": "playfulness",
}
# Trait direction: word → whether "more" raises or lowers the slider.
_NEGATIVE_TRAITS = {"serious": -1, "casual": -1, "concise": -1,
                    "brief": -1, "short": -1, "quiet": -1,
                    "polite": -1, "nice": -1}

_STEP = 20

_TTL = [
    (re.compile(r"\b(?:for\s+)?(?:the\s+)?(?:next\s+)?"
                r"(?:(an?|one|two|three|four|five|six|few|\d+)\s*)?"
                r"(?:hours?|hrs?)\b", re.I), 3600),
    (re.compile(r"\b(?:for\s+)?(?:the\s+)?(?:next\s+)?"
                r"(?:(an?|one|two|three|four|five|six|few|\d+)\s*)?"
                r"(?:minutes?|mins?)\b", re.I), 60),
    (re.compile(r"\btonight\b|\bthis\s+evening\b", re.I), 4 * 3600),
    (re.compile(r"\btoday\b|\bfor\s+the\s+day\b", re.I), 12 * 3600),
]
_WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
             "four": 4, "five": 5, "six": 6, "few": 3}
_SCOPE = [
    (re.compile(r"\bthis\s+task\b|\bfor\s+this\s+task\b|"
                r"\bwhile\s+(?:you'?re|you\s+are)\s+(?:working|fixing)\b",
                re.I), "task"),
    (re.compile(r"\bthis\s+conversation\b|\bthis\s+chat\b|"
                r"\bfor\s+now\b|\bright\s+now\b", re.I), "conversation"),
]
_MODES = {
    "serious": "serious_mode", "coding": "coding_mode",
    "code": "coding_mode", "debugging": "debugging_mode",
    "debug": "debugging_mode", "teaching": "teaching_mode",
    "teach": "teaching_mode", "brainstorm": "brainstorming_mode",
    "brainstorming": "brainstorming_mode", "casual": "casual_mode",
    "review": "reviewer_mode", "reviewer": "reviewer_mode",
    "research": "research_mode",
}

_TRAIT_CMD = re.compile(
    r"^\s*(?:please\s+)?(?:be|act|talk|sound|respond|answer)\s+"
    r"(?:a\s+(?:little|bit|lot)\s+)?(more|less)\s+"
    r"(\w+(?:\s+\w+)?)\s*(.*?)\s*$", re.I)
_TONE_DOWN = re.compile(
    r"^\s*(?:please\s+)?(?:tone\s+down|dial\s+(?:it\s+)?(?:down|back)|"
    r"ease\s+up\s+on|cut\s+(?:back\s+on|out)|stop\s+being\s+so)\s+"
    r"(?:the\s+|your\s+)?(\w+(?:\s+\w+)?)\s*(.*?)\s*$", re.I)
# Comparative adjectives carry their own direction — "be nicer" needs
# no "more". word → (slider, sign).
_COMPARATIVES: dict[str, tuple[str, int]] = {
    "nicer": ("friendliness", 1), "friendlier": ("friendliness", 1),
    "kinder": ("friendliness", 1), "sweeter": ("warmth", 1),
    "warmer": ("warmth", 1), "gentler": ("warmth", 1),
    "meaner": ("rudeness", 1), "ruder": ("rudeness", 1),
    "funnier": ("humor", 1), "sassier": ("sass", 1),
    "snarkier": ("sarcasm", 1), "calmer": ("calmness", 1),
    "chattier": ("verbosity", 1), "quieter": ("verbosity", -1),
    "briefer": ("verbosity", -1), "shorter": ("verbosity", -1),
    "sharper": ("directness", 1), "blunter": ("directness", 1),
    "nerdier": ("nerdiness", 1), "livelier": ("energy", 1),
    "bouncier": ("energy", 1),
}
_BE_ADJ = re.compile(
    r"^\s*(?:please\s+)?(?:be|act|talk|sound|respond|answer)\s+"
    r"(?:a\s+(?:little|bit|lot)\s+)?"
    r"(nicer|friendlier|kinder|sweeter|warmer|gentler|meaner|ruder|"
    r"funnier|sassier|snarkier|calmer|chattier|quieter|briefer|"
    r"shorter|sharper|blunter|nerdier|livelier|bouncier)"
    r"\s*(.*?)\s*$", re.I)
_USE_MODE = re.compile(
    r"^\s*(?:please\s+)?(?:use|switch\s+to|go\s+into|enter)\s+"
    r"(\w+)\s+mode\s*(.*?)\s*$", re.I)
_BE_MODE = re.compile(
    r"^\s*(?:please\s+)?be\s+(?:in\s+)?(serious|coding|debugging|"
    r"teaching|casual|review|research|brainstorming)\s+mode"
    r"\s*(.*?)\s*$", re.I)
_RESET = re.compile(
    r"^\s*(?:please\s+)?(?:reset|restore)\s+(?:your\s+|the\s+)?"
    r"(?:personality|persona|normal\s+self)\b", re.I)
_RESET_TEMP = re.compile(
    r"^\s*(?:please\s+)?(?:back\s+to\s+normal|be\s+yourself\s+again|"
    r"drop\s+the\s+(?:modifier|act|mode))\b", re.I)
_ADDRESS = re.compile(
    r"^\s*(?:please\s+)?(?:you\s+can\s+)?(?:call\s+me|"
    r"address\s+me\s+as|my\s+name\s+is)\s+"
    r"([A-Za-z0-9' _.-]{1,40})\s*\.?\s*$", re.I)
_NO_NAME = re.compile(
    r"^\s*(?:please\s+)?(?:don'?t|do\s+not|stop)\s+"
    r"(?:use\s+my\s+name|call\s+me\s+(?:by\s+)?name)\b", re.I)
_RESET_ADAPT = re.compile(
    r"^\s*(?:please\s+)?(?:reset|clear|forget)\s+(?:the\s+)?"
    r"(?:learned\s+)?(?:adaptations?|learned\s+(?:tone|style|habits)|"
    r"shared\s+(?:history|memories|context)|familiarity)\b", re.I)
_VOICE_MUTE = re.compile(
    r"^\s*(?:please\s+)?(?:stop\s+talking|mute\s+(?:yourself|"
    r"your\s+voice)|voice\s+off|no\s+talking|be\s+quiet|"
    r"silence\s+(?:yourself|your\s+voice))\s*[.!]?\s*$", re.I)
_VOICE_UNMUTE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:you\s+can\s+)?speak\s+again|unmute|"
    r"unmute\s+yourself|voice\s+on|talk\s+to\s+me(?:\s+again)?|"
    r"speak\s+up)\s*[.!]?\s*$", re.I)
_VOICE_RATE_UP = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"(?:speak|talk)\s+(?:a\s+(?:bit|little)\s+)?(?:faster|quicker)|"
    r"speed\s+up(?:\s+a\s+(?:bit|little))?)\s*[.!]?\s*$", re.I)
_VOICE_RATE_DOWN = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"(?:speak|talk)\s+(?:a\s+(?:bit|little)\s+)?slower|"
    r"slow\s+down(?:\s+a\s+(?:bit|little))?|"
    r"slow\s+your\s+(?:speech|voice)\s+down)\s*[.!]?\s*$", re.I)
_VOICE_RATE_RESET = re.compile(
    r"^\s*(?:please\s+)?(?:normal\s+speed|default\s+speed|"
    r"speak\s+(?:at\s+)?normal(?:ly)?|talk\s+normal(?:ly)?|"
    r"reset\s+your\s+(?:voice|speech)\s+speed)\s*[.!]?\s*$", re.I)
_VOICE_GAIN_UP = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"(?:speak|talk)\s+(?:a\s+(?:bit|little)\s+)?louder|"
    r"volume\s+up|turn\s+(?:it|the\s+volume)\s+up|"
    r"raise\s+your\s+voice)\s*[.!]?\s*$", re.I)
_VOICE_GAIN_DOWN = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"(?:speak|talk)\s+(?:a\s+(?:bit|little)\s+)?softer|"
    r"volume\s+down|turn\s+(?:it|the\s+volume)\s+down|"
    r"lower\s+your\s+voice)\s*[.!]?\s*$", re.I)
_VOICE_GAIN_RESET = re.compile(
    r"^\s*(?:please\s+)?(?:normal\s+volume|default\s+volume|"
    r"reset\s+your\s+(?:voice\s+)?volume)\s*[.!]?\s*$", re.I)
_SELF_DESCRIBE = re.compile(
    r"^\s*(?:what\s+are\s+you\s+like|describe\s+your\s+(?:personality|"
    r"persona|style)|who\s+are\s+you\s+right\s+now|"
    r"what'?s\s+your\s+personality)\s*\??\s*$", re.I)


def _duration(text: str) -> tuple[float, str]:
    """Returns (ttl_seconds, scope)."""
    for rx, mult in _TTL:
        m = rx.search(text)
        if m:
            raw = (m.group(1) or "1").lower() if m.lastindex else "1"
            n = int(raw) if raw.isdigit() else _WORD_NUM.get(raw, 1)
            return float(n * mult), "time"
    for rx, scope in _SCOPE:
        if rx.search(text):
            return 0.0, scope
    return 0.0, "conversation"


def parse_persona_command(text: str) -> dict | None:
    """Parse a style command → operation dict, or None.

    Operations:
      {"op": "overlay", "trait_offsets": {..}, "note": str}
      {"op": "modifier", "trait_offsets": {..}, "note": str,
       "ttl_seconds": float, "scope": str, "mode": str}
      {"op": "mode", "mode": str, "ttl_seconds": float}
      {"op": "reset_all"} | {"op": "reset_temp"}
    """
    t = str(text or "").strip()
    if not t or len(t) > 200:
        return None
    if _RESET.match(t):
        return {"op": "reset_all"}
    if _RESET_TEMP.match(t):
        return {"op": "reset_temp"}
    if _VOICE_MUTE.match(t):
        return {"op": "voice_mute"}
    if _VOICE_UNMUTE.match(t):
        return {"op": "voice_unmute"}
    if _VOICE_RATE_RESET.match(t):
        return {"op": "voice_rate", "set": 1.0}
    if _VOICE_RATE_UP.match(t):
        return {"op": "voice_rate", "delta": 0.1}
    if _VOICE_RATE_DOWN.match(t):
        return {"op": "voice_rate", "delta": -0.1}
    if _VOICE_GAIN_RESET.match(t):
        return {"op": "voice_gain", "set": 0.0}
    if _VOICE_GAIN_UP.match(t):
        return {"op": "voice_gain", "delta": 2.0}
    if _VOICE_GAIN_DOWN.match(t):
        return {"op": "voice_gain", "delta": -2.0}
    if _SELF_DESCRIBE.match(t):
        return {"op": "describe"}
    if _RESET_ADAPT.match(t):
        return {"op": "reset_adaptations"}
    m = _NO_NAME.match(t)
    if m:
        return {"op": "address", "address": ""}
    m = _ADDRESS.match(t)
    if m:
        name = m.group(1).strip().rstrip(".")
        # Guard against "call me when it's done" style captures —
        # a name is ≤3 words and doesn't open with a function word.
        words = name.split()
        if name and len(words) <= 3 and words[0].lower() not in (
                "when", "if", "once", "after", "before", "that",
                "a", "an", "the", "it", "you", "back"):
            return {"op": "address", "address": name}

    m = _USE_MODE.match(t) or _BE_MODE.match(t)
    if m:
        mode = _MODES.get(m.group(1).lower())
        if mode:
            ttl, scope = _duration(m.group(2) or "")
            if ttl or scope == "task":
                return {"op": "modifier", "mode": mode,
                        "trait_offsets": {}, "note": m.group(0).strip(),
                        "ttl_seconds": ttl, "scope": scope}
            return {"op": "mode", "mode": mode, "ttl_seconds": ttl}

    m = _TONE_DOWN.match(t)
    if m:
        words1 = m.group(1).strip().lower()
        word = words1.split()[0]
        trait = _TRAIT_ALIASES.get(word)
        if trait:
            # Tail for duration/scope = leftover of group(1) + group(2).
            tail = (words1[len(word):] + " " + (m.group(2) or "")
                    ).strip()
            ttl, scope = _duration(tail)
            off = {trait: -_STEP}
            note = f"less {word}"
            if ttl or scope == "task":
                return {"op": "modifier", "trait_offsets": off,
                        "note": note, "ttl_seconds": ttl, "scope": scope}
            return {"op": "overlay", "trait_offsets": off,
                    "note": note}

    m = _BE_ADJ.match(t)
    if m:
        word = m.group(1).lower()
        trait, sign = _COMPARATIVES[word]
        if trait in schema.SLIDERS:
            off = {trait: sign * _STEP}
            ttl, scope = _duration(m.group(2) or "")
            if ttl or scope == "task":
                return {"op": "modifier", "trait_offsets": off,
                        "note": f"be {word}", "ttl_seconds": ttl,
                        "scope": scope}
            return {"op": "overlay", "trait_offsets": off,
                    "note": f"be {word}"}

    m = _TRAIT_CMD.match(t)
    if m:
        direction, words, rest = (m.group(1).lower(),
                                  m.group(2).strip().lower(),
                                  m.group(3) or "")
        # Longest alias first so "more serious tone" parses right.
        trait = None
        word = ""
        for w in sorted(_TRAIT_ALIASES, key=len, reverse=True):
            if words.split()[0] == w or words.startswith(w + " "):
                trait, word = _TRAIT_ALIASES[w], w
                break
        if trait and trait in schema.SLIDERS:
            sign = 1 if direction == "more" else -1
            sign *= _NEGATIVE_TRAITS.get(word, 1)
            off = {trait: sign * _STEP}
            note = f"{direction} {word}"
            tail = (words[len(word):] + " " + rest).strip()
            ttl, scope = _duration(tail)
            if ttl or scope == "task":
                return {"op": "modifier", "trait_offsets": off,
                        "note": note, "ttl_seconds": ttl,
                        "scope": scope}
            return {"op": "overlay", "trait_offsets": off,
                    "note": note}
    return None


def apply_command(dyn: Any, cmd: dict) -> dict:
    """Apply a parsed command to PersonaDynamics. Returns a summary for
    the chat ack."""
    op = cmd.get("op")
    if op == "overlay":
        ov = dyn.adjust_overlay(cmd.get("trait_offsets"),
                                note=str(cmd.get("note") or ""))
        return {"applied": "overlay", "trait_offsets":
                ov["trait_offsets"], "note": cmd.get("note")}
    if op == "modifier":
        mod = dyn.add_modifier(
            trait_offsets=cmd.get("trait_offsets"),
            note=str(cmd.get("note") or ""),
            ttl_seconds=float(cmd.get("ttl_seconds") or 0.0),
            scope=str(cmd.get("scope") or "conversation"),
            mode=str(cmd.get("mode") or ""))
        return {"applied": "modifier", "modifier": mod}
    if op == "mode":
        dyn.set_mode(str(cmd.get("mode") or ""))
        return {"applied": "mode", "mode": dyn.mode()}
    if op == "reset_temp":
        n = dyn.clear_modifiers()
        return {"applied": "reset_temp", "cleared": n}
    if op == "reset_all":
        dyn.clear_modifiers()
        dyn.reset_overlay()
        dyn.set_mode("")
        return {"applied": "reset_all"}
    if op == "address":
        addr = dyn.set_address(cmd.get("address") or "")
        return {"applied": "address", "address": addr}
    if op == "reset_adaptations":
        dyn.reset_continuity()
        return {"applied": "reset_adaptations"}
    if op == "describe":
        return {"applied": "describe"}
    return {"applied": "none"}
