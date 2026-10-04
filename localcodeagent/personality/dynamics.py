"""Persona dynamics — per-profile relationship state + mood engine.

Persisted in ``<profile_dir>/persona_state.json``:

    {"schema_version": 1,
     "turns": int,
     "familiarity": float,        # 0..100, asymptotic growth
     "stage": "new|familiar|trusted|long_term",
     "mood": {"current": <MOODS|"">, "intensity": 0..1,
              "turns_held": int, "updated_at": ts},
     "modifiers": [ {note, trait_offsets, mode, expires_at, scope} ],
     "mode": <MODE_OVERLAYS key|"">,
     "overlay": {"trait_offsets": {slider: -100..100},
                 "notes": [str]},
     "recent": {"openers": [str], "closers": [str]}}

This is *interaction history and adaptation*, not simulated human
attachment — familiarity is just accumulated exchange count shaped by
the persona's warmup rate. All values bounded; a corrupt file resets to
a blank state without affecting personality.json.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text
from . import schema
from .effective import RELATIONSHIP_STAGES, clean_mode

SCHEMA_VERSION = 1
_FAM_PER_TURN = 2.0          # base familiarity gain per exchange
_TEASE_RATE = 0.6            # teasing comfort grows slower than rapport
_MOOD_DECAY = 0.72           # intensity kept per turn without new input
_MOOD_FLOOR = 0.15           # below this, mood reverts to baseline
_MAX_RECENT = 12
_MAX_MODIFIERS = 16


def _blank() -> dict:
    return {"schema_version": SCHEMA_VERSION, "turns": 0,
            "familiarity": 0.0, "stage": "new",
            "mood": {"current": "", "intensity": 0.0,
                   "turns_held": 0, "updated_at": 0.0},
            "modifiers": [], "mode": "",
            "overlay": {"trait_offsets": {}, "notes": []},
            "recent": {"openers": [], "closers": []},
            "last_seen": 0.0}


def _stage(familiarity: float) -> str:
    for s, floor in (("long_term", 80.0), ("trusted", 45.0),
                     ("familiar", 15.0)):
        if familiarity >= floor:
            return s
    return "new"


# ---------------------------------------------------------------------------
# Mood transitions — user-text events → mood candidates. First match wins;
# a new mood replaces the held one only when its intensity is >= the held
# intensity (so a crash outranks a joke, a joke doesn't erase concern).
# ---------------------------------------------------------------------------

_MOOD_EVENTS: list[tuple[re.Pattern, str, float]] = [
    (re.compile(r"\b(?:data\s+loss|security\s+breach|production\s+down|"
                r"deleted\s+everything|unrecoverable|bricked|"
                r"emergency|credentials?\s+leak)\b", re.I),
     "serious", 0.9),
    (re.compile(r"\b(?:crash(?:ed|es|ing)?|broke|broken|"
                r"fail(?:ed|s|ing|ure)?|errors?|bugs?|"
                r"won'?t\s+work|doesn'?t\s+work|stuck|"
                r"frustrated|frustrating|annoying)\b", re.I),
     "concerned", 0.6),
    (re.compile(r"\b(?:finally|it\s+works|worked|fixed|solved|"
                r"nailed\s+it|did\s+it|passed|shipped)\b", re.I),
     "celebratory", 0.7),
    (re.compile(r"\b(?:interesting|curious|wonder|what\s+if|"
                r"how\s+does|why\s+does|figured)\b", re.I),
     "curious", 0.5),
    (re.compile(r"\b(?:lol|haha+|funny|joke|hilarious|lolol)\b", re.I),
     "relaxed", 0.4),
    (re.compile(r"\b(?:sad|upset|rough\s+day|tired|exhausted|"
                r"stressed|anxious|worried)\b", re.I),
     "concerned", 0.5),
    (re.compile(r"\b(?:fix|implement|deploy|review|build|"
                r"let'?s\s+(?:do|build|fix)|task)\b", re.I),
     "focused", 0.4),
]

_BASELINE_TO_MOOD = {"relaxed": "relaxed", "focused": "focused",
                     "curious": "curious", "excited": "excited",
                     "neutral_controlled": "focused"}


def mood_event(user_text: str) -> tuple[str, float] | None:
    """First matching mood event in the user message."""
    t = str(user_text or "")
    if not t:
        return None
    for rx, mood, intensity in _MOOD_EVENTS:
        if rx.search(t):
            return mood, intensity
    return None


class PersonaDynamics:
    """Per-profile persona state: relationship, mood, modifiers,
    learned overlay, mode, recent phrasing."""

    def __init__(self, profile_dir: Path) -> None:
        self.path = Path(profile_dir) / "persona_state.json"

    # -- IO ----------------------------------------------------------------

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                st = _blank()
                st.update({k: raw[k] for k in st if k in raw})
                if not isinstance(st.get("mood"), dict):
                    st["mood"] = _blank()["mood"]
                if not isinstance(st.get("overlay"), dict):
                    st["overlay"] = _blank()["overlay"]
                if not isinstance(st.get("recent"), dict):
                    st["recent"] = _blank()["recent"]
                for k in ("modifiers",):
                    if not isinstance(st.get(k), list):
                        st[k] = []
                return st
        except Exception:
            pass
        return _blank()

    def _save(self, st: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(st, indent=2,
                                               ensure_ascii=False))

    # -- turn lifecycle ----------------------------------------------------

    def note_turn(self, user_text: str, *, warmup_rate: float = 1.0,
                  baseline_affect: str = "relaxed",
                  manual_mood: str = "") -> dict:
        """Called once per completed user exchange. Updates familiarity,
        stage, mood transition/decay, and prunes expired modifiers.
        Returns the new state."""
        st = self._load()
        now = time.time()
        st["turns"] = int(st.get("turns") or 0) + 1
        st["last_seen"] = now

        # Familiarity: asymptotic approach to 100, scaled by warmup rate.
        rate = max(0.1, min(2.0, float(warmup_rate or 1.0)))
        fam = float(st.get("familiarity") or 0.0)
        fam += _FAM_PER_TURN * rate * (1.0 - fam / 100.0)
        st["familiarity"] = round(min(100.0, fam), 2)
        st["stage"] = _stage(st["familiarity"])

        # Mood: a manual mood (personality.json) holds until cleared;
        # otherwise transitions + decay toward baseline.
        md = st["mood"]
        held = str(md.get("current") or "")
        held_int = float(md.get("intensity") or 0.0)
        ev = mood_event(user_text)
        if not manual_mood:
            if ev and (held not in schema.MOODS
                       or ev[1] >= held_int
                       or held_int < _MOOD_FLOOR):
                md["current"], md["intensity"] = ev
                md["turns_held"], md["updated_at"] = 0, now
            elif held in schema.MOODS:
                held_int *= _MOOD_DECAY
                md["intensity"] = round(held_int, 3)
                md["turns_held"] = int(md.get("turns_held") or 0) + 1
                if held_int < _MOOD_FLOOR:
                    md["current"] = _BASELINE_TO_MOOD.get(
                        baseline_affect, "")
                    md["intensity"] = 0.25 if md["current"] else 0.0
                    md["turns_held"] = 0
            else:
                md["current"] = _BASELINE_TO_MOOD.get(
                    baseline_affect, "")
                md["intensity"] = 0.25 if md["current"] else 0.0
        self._prune_modifiers(st, now)
        self._save(st)
        return st

    def note_reply(self, reply_text: str) -> None:
        """Track opener/closer n-grams for the repetition guard.
        Bounded — keeps the last few first/last sentences."""
        st = self._load()
        text = str(reply_text or "").strip()
        if text:
            sents = [s.strip() for s in re.split(r"[.!?…]\s+|\n", text)
                     if s.strip()]
            rec = st["recent"]
            if sents:
                rec.setdefault("openers", []).append(
                    " ".join(sents[0].lower().split()[:6]))
                if len(sents) > 1:
                    rec.setdefault("closers", []).append(
                        " ".join(sents[-1].lower().split()[:6]))
            for k in ("openers", "closers"):
                rec[k] = rec.get(k, [])[-_MAX_RECENT:]
        self._save(st)

    # -- queries -----------------------------------------------------------

    def state(self) -> dict:
        st = self._load()
        self._prune_modifiers(st)
        return st

    def effective_mood(self, *, manual_mood: str = "") -> str:
        """Manual mood wins; else the dynamics engine's current mood."""
        if manual_mood in schema.MOODS:
            return manual_mood
        cur = str(self._load()["mood"].get("current") or "")
        return cur if cur in schema.MOODS else ""

    def relationship(self) -> dict:
        st = self._load()
        return {"familiarity": float(st.get("familiarity") or 0.0),
                "stage": str(st.get("stage") or "new"),
                "turns": int(st.get("turns") or 0)}

    def overlay(self) -> dict:
        ov = self._load().get("overlay") or {}
        return {"trait_offsets": dict(ov.get("trait_offsets") or {}),
                "notes": [str(n) for n in (ov.get("notes") or [])][:8]}

    def modifiers(self) -> list[dict]:
        st = self._load()
        self._prune_modifiers(st)
        return [m for m in st.get("modifiers") or []
                if isinstance(m, dict)]

    def mode(self) -> str:
        return clean_mode(self._load().get("mode"))

    def recent_phrases(self) -> dict:
        rec = self._load().get("recent") or {}
        return {"openers": list(rec.get("openers") or []),
                "closers": list(rec.get("closers") or [])}

    # -- mutations ----------------------------------------------------------

    def _prune_modifiers(self, st: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        mods = []
        for m in st.get("modifiers") or []:
            try:
                exp = float(m.get("expires_at") or 0)
            except (TypeError, ValueError):
                exp = 0
            if not exp or exp > now:
                mods.append(m)
        st["modifiers"] = mods[-_MAX_MODIFIERS:]

    def adjust_overlay(self, trait_offsets: dict | None = None,
                       note: str = "") -> dict:
        """Learned per-profile overlay — additive trait offsets + a
        human-readable note. Built-ins are never touched."""
        st = self._load()
        ov = st["overlay"]
        offs = ov.setdefault("trait_offsets", {})
        for k, delta in (trait_offsets or {}).items():
            if k not in schema.SLIDERS:
                continue
            try:
                offs[k] = max(-100, min(100, int(round(
                    float(offs.get(k, 0)) + float(delta)))))
            except (TypeError, ValueError):
                continue
            if offs[k] == 0:
                offs.pop(k, None)
        n = str(note or "").strip()
        if n and n not in ov["notes"]:
            ov["notes"] = (ov.get("notes") or [])[-7:] + [n]
        self._save(st)
        return self.overlay()

    def add_modifier(self, *, trait_offsets: dict | None = None,
                     note: str = "", ttl_seconds: float = 0.0,
                     scope: str = "conversation",
                     mode: str = "") -> dict:
        """Temporary persona modifier — expires by ttl or scope reset.
        Never silently permanent."""
        st = self._load()
        mod = {"id": f"m{int(time.time() * 1000)}",
               "note": str(note or "").strip()[:120],
               "trait_offsets": {},
               "mode": clean_mode(mode),
               "scope": scope if scope in
                        ("conversation", "task", "time") else
                        "conversation",
               "expires_at": (time.time() + ttl_seconds
                              if ttl_seconds else 0.0),
               "created_at": time.time()}
        for k, delta in (trait_offsets or {}).items():
            if k in schema.SLIDERS:
                try:
                    mod["trait_offsets"][k] = max(
                        -100, min(100, int(round(float(delta)))))
                except (TypeError, ValueError):
                    continue
        st.setdefault("modifiers", []).append(mod)
        st["modifiers"] = st["modifiers"][-_MAX_MODIFIERS:]
        if mod["mode"]:
            st["mode"] = mod["mode"]
        self._save(st)
        return mod

    def set_mode(self, mode: str) -> str:
        st = self._load()
        st["mode"] = clean_mode(mode)
        self._save(st)
        return st["mode"]

    def clear_modifiers(self, *, scope: str = "") -> int:
        """Conversation end / task end / manual reset. Returns count
        removed."""
        st = self._load()
        keep, removed = [], 0
        for m in st.get("modifiers") or []:
            if scope and str(m.get("scope")) != scope:
                keep.append(m)
            else:
                removed += 1
        st["modifiers"] = keep
        if not keep:
            st["mode"] = ""
        self._save(st)
        return removed

    def reset_overlay(self) -> None:
        """Restore built-in persona behavior; modifiers and unrelated
        state are preserved."""
        st = self._load()
        st["overlay"] = {"trait_offsets": {}, "notes": []}
        self._save(st)

    def set_mood(self, mood: str, *, intensity: float = 0.6) -> None:
        st = self._load()
        m = mood if mood in schema.MOODS else ""
        st["mood"] = {"current": m,
                      "intensity": max(0.0, min(1.0, intensity)),
                      "turns_held": 0, "updated_at": time.time()}
        self._save(st)
