"""Continuity layer — shared-history milestones, relevance-gated
callbacks, stable persona preferences, and preferred address.

Storage lives inside ``persona_state.json`` via dynamics.py — profile
scoped like all persona state. Nothing here is private raw memory;
milestones are lightweight summarized events, not transcripts.

- ``record_milestone`` — notable shared events only (project done,
  recurring issue, hard bug, success, learned preference, shared joke).
  Trivial exchanges never create one.
- ``relevant_callback`` — picks a milestone worth referencing for the
  current user text, gated by recency + token overlap. Recent context
  is cheap to reference; old milestones need strong overlap.
- Persona preferences — bounded stable preference tendencies per
  family + the ability to *record stated preferences* so a persona
  doesn't reverse a subjective claim it made earlier.
- Preferred address — profile-level form-of-address preference
  independent of Creator titles, with per-family usage frequency.
"""
from __future__ import annotations

import re
import time
from typing import Any

_MAX_MILESTONES = 24
_MAX_PREFS = 12

_MILESTONE_TYPES = (
    "project_done", "recurring_issue", "hard_bug", "success",
    "preference_learned", "shared_joke", "long_project",
)

# Stopwords kept small and dependency-free.
_STOP = frozenset(
    "the a an and or but to of in on for with it its is are was were "
    "be been this that these those i you we they he she do does did "
    "have has had not no yes so if at by as my your our their me him "
    "her us them what when where which who how why".split())


def _stem(w: str) -> str:
    """Tiny stemmer — collapses the plural/inflection forms that would
    otherwise make 'explanation' and 'explanations' disjoint."""
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:len(w) - len(suf)]
    return w


def _tokens(text: str) -> set[str]:
    return {_stem(w)
            for w in re.findall(r"[a-z0-9_]+", str(text).lower())
            if len(w) > 2 and w not in _STOP}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


# ---------------------------------------------------------------------------
# Shared-history milestones
# ---------------------------------------------------------------------------

def record_milestone(state: dict, event: str, *, mtype: str = "success",
                     project: str = "", importance: str = "medium",
                     now: float | None = None) -> dict | None:
    """Append a shared milestone. Returns the row or None if rejected
    (trivial/empty event, unknown type). Deduplicates near-identical
    events (same type + high overlap) by refreshing the timestamp."""
    event = " ".join(str(event or "").split())[:160]
    if len(event) < 8 or mtype not in _MILESTONE_TYPES:
        return None
    rows = state.setdefault("milestones", [])
    now = time.time() if now is None else float(now)
    toks = _tokens(event)
    for row in rows:
        if row.get("type") == mtype and \
                _overlap(toks, _tokens(row.get("event", ""))) > 0.6:
            row["timestamp"] = now
            row["importance"] = importance
            return row
    row = {"type": "shared_milestone", "kind": mtype, "event": event,
           "project": str(project or "")[:80],
           "importance": importance, "timestamp": now}
    rows.append(row)
    del rows[:max(0, len(rows) - _MAX_MILESTONES)]
    return row


def detect_milestone(cue: dict, user_text: str,
                     reply_outcome: str = "") -> tuple[str, str] | None:
    """(kind, event_text) when a turn is worth remembering, else None.
    Conservative — most turns return None."""
    c = str(cue.get("cue") or "")
    t = str(user_text or "").strip()
    if c == "celebration":
        return ("success",
                f"shared success — {' '.join(t.split()[:12]) or 'celebrated a win'}")
    if c == "frustration" and re.search(r"\bagain\b|\bstill\b|\bkeep\b",
                                        t, re.I):
        return ("recurring_issue",
                f"recurring issue — {' '.join(t.split()[:12])}")
    if cue.get("sarcasm") and re.search(r"\bagain\b", t, re.I):
        return ("recurring_issue",
                f"recurring issue — {' '.join(t.split()[:12])}")
    if c == "joking" and len(t) > 15:
        return ("shared_joke",
                f"shared joke — {' '.join(t.split()[:12])}")
    return None


def relevant_callback(state: dict, user_text: str,
                      *, now: float | None = None) -> dict | None:
    """Best milestone to reference for this turn, or None.

    Score = overlap × recency_weight × importance_weight. Old
    milestones need much stronger relevance; recency alone never earns
    a callback. A milestone already referenced recently is penalized so
    callbacks stay occasional, not nostalgic filler."""
    rows = state.get("milestones") or []
    if not rows:
        return None
    now = time.time() if now is None else float(now)
    toks = _tokens(user_text)
    if not toks:
        return None
    imp_w = {"high": 1.3, "medium": 1.0, "low": 0.7}
    best, best_score = None, 0.0
    for row in rows:
        age_h = max(0.0, (now - float(row.get("timestamp", 0))) / 3600)
        recency = 1.0 / (1.0 + age_h / 24.0)  # halves roughly daily
        score = _overlap(toks, _tokens(row.get("event", ""))) \
            * recency \
            * imp_w.get(str(row.get("importance")), 1.0)
        last_ref = float(row.get("last_referenced", 0))
        if now - last_ref < 3600:
            score *= 0.3
        if score > best_score:
            best, best_score = row, score
    # Threshold: weak matches never surface — no forced nostalgia.
    if best is None or best_score < 0.18:
        return None
    best["last_referenced"] = now
    return best


# ---------------------------------------------------------------------------
# Persona stable preferences + stated-preference consistency
# ---------------------------------------------------------------------------

def record_stated_preference(state: dict, subject: str, stance: str,
                             *, now: float | None = None) -> dict | None:
    """Record a subjective preference the persona has stated, so later
    turns stay consistent. `stance` is 'likes'/'dislikes'/'prefers'."""
    subject = " ".join(str(subject or "").split())[:80]
    stance = str(stance or "prefers")
    if len(subject) < 3:
        return None
    rows = state.setdefault("stated_prefs", [])
    now = time.time() if now is None else float(now)
    for row in rows:
        if row.get("subject") == subject:
            row["stance"] = stance
            row["timestamp"] = now
            return row
    row = {"subject": subject, "stance": stance, "timestamp": now}
    rows.append(row)
    del rows[:max(0, len(rows) - _MAX_PREFS)]
    return row


def preference_consistency(state: dict, subject: str) -> str | None:
    """If the persona has a stated stance on `subject`, return it —
    used by the prompt card ('stated: prefers X — stay consistent')
    and by QA drift checks."""
    toks = _tokens(subject)
    if not toks:
        return None
    for row in reversed(state.get("stated_prefs") or []):
        if _overlap(toks, _tokens(row.get("subject", ""))) > 0.3:
            return f"{row.get('stance')}: {row.get('subject')}"
    return None


# ---------------------------------------------------------------------------
# Preferred address — independent of Creator titles
# ---------------------------------------------------------------------------

def set_address(state: dict, address: str) -> str:
    """Profile-level preferred address: first name, nickname, custom,
    or '' / 'none' for no name."""
    a = " ".join(str(address or "").split())[:40]
    if a.lower() in ("none", "no name", "nothing"):
        a = ""
    state["address"] = a
    return a


def address_hint(state: dict, family: str,
                 familiarity: int) -> str:
    """Name-usage guidance: persona frequency × familiarity gate.
    New profiles never get heavy name use regardless of persona."""
    name = str(state.get("address") or "")
    if not name:
        return ""
    freq = {"warm": "medium", "playful": "medium", "flirty": "medium",
            "raunchy": "medium", "calm": "low", "professional": "low",
            "nerdy": "low", "minimalist": "rare", "rude": "rare",
            "sassy": "low", "mysterious": "low"}.get(family, "low")
    if familiarity < 20 and freq != "rare":
        return f"may use '{name}' sparingly — relationship is new"
    if freq == "rare":
        return f"use '{name}' only occasionally, for emphasis"
    if freq == "medium":
        return f"may use '{name}' naturally — not every message"
    return f"use '{name}' sparingly"


# ---------------------------------------------------------------------------
# Per-family noticing tendencies — which cues each persona picks up
# ---------------------------------------------------------------------------

_NOTICING = {
    "professional": "notices task state, blockers, and precision of "
                    "the request",
    "warm": "notices emotional cues first — tiredness, frustration, "
            "encouragement — before task details",
    "nerdy": "notices technical inconsistencies, imprecise specs, and "
             "interesting details",
    "playful": "notices openings for levity and shifts in energy",
    "calm": "notices pacing and stress signals in the conversation",
    "sassy": "notices sarcasm, dry remarks, and invitations to banter",
    "rude": "notices vagueness and wasted effort bluntly",
    "mysterious": "notices what is unsaid or left ambiguous",
    "flirty": "notices warmth and rapport cues",
    "raunchy": "notices casual, unguarded energy",
    "default": "notices the most relevant detail for the task",
}


def noticing_cue(family: str) -> str:
    return _NOTICING.get(family, _NOTICING["default"])


# ---------------------------------------------------------------------------
# Saturation — cumulative expression tracking so strong personas don't
# caricature. Stored as rolling counters with decay.
# ---------------------------------------------------------------------------

_SAT_KEYS = ("humor", "sarcasm", "vocal", "gesture", "intensity")
_SAT_HALF_LIFE_TURNS = 20  # decays by half every ~20 turns


def note_expression(state: dict, kinds: list[str]) -> None:
    """Bump saturation counters for expression kinds used this turn."""
    sat = state.setdefault("saturation",
                           {k: 0.0 for k in _SAT_KEYS})
    for k in kinds:
        if k in sat:
            sat[k] = round(min(6.0, sat[k] + 1.0), 2)


def decay_saturation(state: dict, turns: int = 1) -> None:
    sat = state.get("saturation")
    if not sat:
        return
    f = 0.5 ** (turns / _SAT_HALF_LIFE_TURNS)
    for k in _SAT_KEYS:
        sat[k] = round(sat.get(k, 0.0) * f, 3)


def saturation_level(state: dict) -> dict:
    """0..1 damp factor per kind; high recent usage → dampen."""
    sat = state.get("saturation") or {}
    return {k: round(min(1.0, sat.get(k, 0.0) / 5.0), 2)
            for k in _SAT_KEYS}


def saturated(state: dict, kind: str) -> bool:
    return (state.get("saturation") or {}).get(kind, 0.0) >= 3.0


# ---------------------------------------------------------------------------
# Humor / analogy pattern memory — avoid re-running the same structure
# ---------------------------------------------------------------------------

def note_pattern(state: dict, kind: str, pattern: str) -> None:
    """kind: 'analogy'|'humor'|'metaphor'|'example'. pattern is a
    short structure tag (e.g. 'like-a-recipe', 'deadpan-one-liner')."""
    if not pattern:
        return
    store = state.setdefault("patterns", {})
    seen = store.setdefault(kind, [])
    if pattern in seen:
        seen.remove(pattern)
    seen.append(pattern)
    del seen[:max(0, len(seen) - 8)]


def pattern_fresh(state: dict, kind: str, pattern: str) -> bool:
    seen = (state.get("patterns") or {}).get(kind) or []
    return pattern not in seen[-4:]  # last 4 uses are "too soon"


def humor_feedback(state: dict, positive: bool, style: str) -> None:
    """Bounded adaptation: repeated positive/negative feedback on a
    humor style nudges the profile-level humor preference."""
    style = str(style or "")
    if not style:
        return
    hp = state.setdefault("humor_feedback", {})
    row = hp.setdefault(style, {"pos": 0, "neg": 0})
    row["pos" if positive else "neg"] += 1


def humor_adaptation(state: dict) -> str:
    """A prompt-cue string when accumulated feedback is meaningful —
    e.g. 'user responds poorly to sarcasm — keep it rare'."""
    notes = []
    for style, row in (state.get("humor_feedback") or {}).items():
        neg, pos = row.get("neg", 0), row.get("pos", 0)
        if neg >= 3 and neg > pos * 2:
            notes.append(f"user responds poorly to {style} humor — "
                         f"keep it rare")
        elif pos >= 3 and pos > neg * 2:
            notes.append(f"{style} humor lands well with this user")
    return "; ".join(notes[:2])


# ---------------------------------------------------------------------------
# Preferred address command + summary for inspect APIs
# ---------------------------------------------------------------------------

def continuity_summary(state: dict) -> dict:
    """Safe inspect view — counts and stances, no raw memory."""
    return {
        "milestones": len(state.get("milestones") or []),
        "stated_prefs": [f"{r.get('stance')}: {r.get('subject')}"
                         for r in (state.get("stated_prefs") or [])],
        "address": state.get("address") or "",
        "saturation": saturation_level(state),
        "humor_feedback": state.get("humor_feedback") or {},
    }
