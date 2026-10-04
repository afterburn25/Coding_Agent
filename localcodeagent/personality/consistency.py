"""Persona consistency layer — deterministic post-hoc checks on
responses, plus feed-forward hints for the next prompt.

Two checks, both cheap and explainable:

- ``check_repetition`` — compares a reply's opener/closer n-grams
  against the profile's recent-phrases history; repeated openers/
  closers push a repetition score up.
- ``detect_drift`` — flags replies that violate the effective persona's
  hard-ish surface markers (professional + heavy slang, calm + ALL
  CAPS/exclamation storms, minimalist + huge paragraphs, serious-mode
  + jokes). Never blocks — it reports so callers can log/steer.

``consistency_report`` rolls both into a bounded dict for the debug
endpoint and the per-turn hook.
"""
from __future__ import annotations

import re
from typing import Any

_EXCLAIM = re.compile(r"!")
_ALLCAPS_WORD = re.compile(r"\b[A-Z]{3,}\b")
_SLANG = re.compile(r"\b(?:lol|lmao|bruh|gonna|wanna|kinda|y'all|"
                    r"ya|fr|ngl|imo|tbh|smh|dope|lit)\b", re.I)
_HUMOR_MARK = re.compile(r"\b(?:lol|haha|lmao|just kidding|"
                         r"kidding|j/k|😂|😆)\b", re.I)
_VOCAL_TOKEN = re.compile(
    r"\b(?:mm[-\s]?hmm|hmm+|heh|haha|hehe|giggle[sd]?|"
    r"sigh(?:s|ed)?|aww+|phew|ugh|tsk|scoff|oops|wow)\b", re.I)


def _first_last(text: str) -> tuple[str, str]:
    sents = [s.strip() for s in re.split(r"[.!?…]\s+|\n",
                                         str(text or "").strip())
             if s.strip()]
    if not sents:
        return "", ""
    opener = " ".join(sents[0].lower().split()[:6])
    closer = (" ".join(sents[-1].lower().split()[:6])
              if len(sents) > 1 else "")
    return opener, closer


def check_repetition(reply_text: str, recent: dict | None) -> dict:
    """Repeated openers/closers → score 0..1 + flags."""
    rec = recent or {}
    opener, closer = _first_last(reply_text)
    openers = list(rec.get("openers") or [])
    closers = list(rec.get("closers") or [])
    flags = []
    score = 0.0
    if opener and openers.count(opener) >= 2:
        flags.append("repeated_opener")
        score += 0.5
    if closer and closers.count(closer) >= 2:
        flags.append("repeated_closer")
        score += 0.5
    if opener and opener in openers[-2:]:
        score += 0.2
        if "repeated_opener" not in flags:
            flags.append("echoed_opener")
    return {"score": round(min(1.0, score), 2), "flags": flags,
            "opener": opener, "closer": closer}


def detect_drift(reply_text: str, card: dict | None) -> dict:
    """Heuristic drift flags: reply surface vs effective-persona
    expectations. Not a blocker — a diagnostic signal."""
    c = card or {}
    text = str(reply_text or "")
    flags: list[str] = []
    fam = str(c.get("family") or "default")
    serious = str(c.get("seriousness") or "")
    rh = str(c.get("rhythm") or "")

    words = len(text.split())
    exclaims = len(_EXCLAIM.findall(text))
    caps = len(_ALLCAPS_WORD.findall(text))
    slang = len(_SLANG.findall(text))

    if serious in ("serious", "critical") and _HUMOR_MARK.search(text):
        flags.append("humor_in_serious_context")
    if fam == "professional" and slang >= 3:
        flags.append("slang_drift")
    if fam in ("calm", "professional") and (caps >= 3 or exclaims >= 4):
        flags.append("energy_drift")
    if rh in ("compact", "fragmented") and words > 220:
        flags.append("verbosity_drift")
    if str(c.get("silence") or "") == "absolute_minimum" \
            and words > 120:
        flags.append("minimalist_drift")
    # Playful persona should still show *some* color in casual contexts —
    # flag the opposite drift (persona flattening) too.
    if fam in ("playful", "sassy", "flirty") and serious == "casual" \
            and words > 40 and exclaims == 0 and not _HUMOR_MARK.search(
                text) and not _VOCAL_TOKEN.search(text):
        flags.append("color_loss")
    return {"drifted": bool(flags), "flags": flags}


def feedforward_hints(recent: dict | None) -> list[str]:
    """Repetition hints for the next prompt — names the openers/closers
    the model should NOT reuse."""
    rec = recent or {}
    hints: list[str] = []
    seen = []
    for op in list(rec.get("openers") or [])[-4:]:
        if op and op not in seen:
            seen.append(op)
    if len(seen) >= 2:
        hints.append("Vary your opener — recently used: "
                     + "; ".join(f"'{o}…'" for o in seen[-3:]) + ".")
    closers = list(dict.fromkeys(rec.get("closers") or []))[-3:]
    if len(closers) >= 2:
        hints.append("Vary your closer — recently used: "
                     + "; ".join(f"'{c}…'" for c in closers) + ".")
    return hints


def consistency_report(dyn: Any, reply_text: str = "",
                       card: dict | None = None) -> dict:
    """Bounded report for debug/eval — repetition + drift + state."""
    recent = dyn.recent_phrases() if dyn is not None else {}
    rep = check_repetition(reply_text, recent) if reply_text else \
        {"score": 0.0, "flags": []}
    drift = detect_drift(reply_text, card) if reply_text else \
        {"drifted": False, "flags": []}
    rel = dyn.relationship() if dyn is not None else {}
    return {
        "repetition": rep,
        "drift": drift,
        "relationship": rel,
        "hints": feedforward_hints(recent),
    }
