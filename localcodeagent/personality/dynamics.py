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
            # Social layer — last classified user cue + surface energy.
            "last_cue": "", "sarcasm_detected": False,
            "user_energy": "medium", "last_outcome": "",
            # Conversational focus + shared-history continuity.
            "focus": {"topic": "", "prev": "", "since_turn": 0},
            "milestones": [], "stated_prefs": [], "address": "",
            # Expression memory — saturation counters and pattern tags
            # keep strong personas from caricaturing over long sessions.
            "saturation": {"humor": 0.0, "sarcasm": 0.0, "vocal": 0.0,
                           "gesture": 0.0, "intensity": 0.0},
            "patterns": {}, "humor_feedback": {},
            # QA metrics — aggregate counters, not surveillance.
            "metrics": {"words": 0, "questions": 0, "name_uses": 0,
                        "user_sarcasm": 0, "humor": 0, "vocal": 0,
                        "gesture": 0, "callbacks": 0},
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

        # Social layer — cue classification (sarcasm-aware), user
        # energy, focus tracking, milestone detection, saturation decay.
        try:
            from . import continuity as _cont
            from . import social as _soc
            cue = _soc.classify_social(
                user_text,
                context_failed=str(st.get("last_outcome") or "")
                == "failed")
            st["last_cue"] = cue.get("cue", "")
            st["sarcasm_detected"] = bool(cue.get("sarcasm"))
            st["user_energy"] = _soc.user_energy(
                user_text, cue.get("cue", ""))
            new_focus = _soc.tag_focus(user_text)
            foc = st.setdefault("focus",
                                {"topic": "", "prev": "",
                                 "since_turn": 0})
            if _soc.topic_shifted(str(foc.get("topic") or ""),
                                  new_focus):
                foc["prev"], foc["topic"] = foc.get("topic"), new_focus
                foc["since_turn"] = st["turns"]
            elif new_focus and not foc.get("topic"):
                foc["topic"], foc["since_turn"] = new_focus, st["turns"]
            ms = _cont.detect_milestone(cue, user_text)
            if ms:
                _cont.record_milestone(st, ms[1], mtype=ms[0])
            _cont.decay_saturation(st)
            met = st.setdefault("metrics", {})
            if cue.get("sarcasm"):
                met["user_sarcasm"] = int(
                    met.get("user_sarcasm") or 0) + 1
        except Exception:
            pass
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

    _REPLY_FAIL = re.compile(
        r"\b(?:failed|failure|error|didn'?t\s+work|couldn'?t|"
        r"unable\s+to|still\s+broken)\b", re.I)
    _REPLY_HUMOR = re.compile(
        r"\b(?:ha(?:ha)+|hehe?|lol)\b|[😂😄😉]|punchline|"
        r"just\s+kidding", re.I)

    def note_reply(self, reply_text: str) -> None:
        """Track opener/closer n-grams for the repetition guard, plus
        reply metrics (length, questions, name usage, humor markers,
        outcome) for QA and sarcasm context. Bounded."""
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
            met = st.setdefault("metrics", {})
            met["words"] = int(met.get("words") or 0) + len(text.split())
            met["questions"] = int(met.get("questions") or 0) \
                + text.count("?")
            addr = str(st.get("address") or "")
            if addr and re.search(r"\b" + re.escape(addr.lower())
                                  + r"\b", text.lower()):
                met["name_uses"] = int(met.get("name_uses") or 0) + 1
            if self._REPLY_HUMOR.search(text):
                met["humor"] = int(met.get("humor") or 0) + 1
                self._bump_saturation(st, "humor")
            st["last_outcome"] = ("failed"
                                  if self._REPLY_FAIL.search(text)
                                  else "ok")
        self._save(st)

    def _bump_saturation(self, st: dict, kind: str,
                         amount: float = 1.0) -> None:
        sat = st.setdefault("saturation", {})
        sat[kind] = round(min(6.0, float(sat.get(kind) or 0.0)
                              + amount), 2)

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

    # -- social / continuity ------------------------------------------------

    def social(self) -> dict:
        """Last-turn social signals for prompt compilation."""
        st = self._load()
        return {"cue": str(st.get("last_cue") or ""),
                "sarcasm": bool(st.get("sarcasm_detected")),
                "user_energy": str(st.get("user_energy") or "medium"),
                "last_outcome": str(st.get("last_outcome") or "")}

    def focus_info(self) -> dict:
        st = self._load()
        foc = st.get("focus") or {}
        return {"topic": str(foc.get("topic") or ""),
                "prev": str(foc.get("prev") or ""),
                "since_turn": int(foc.get("since_turn") or 0)}

    def callback_for(self, user_text: str) -> dict | None:
        """Best shared-history callback for this user message — gated
        by relevance + recency. Persists the last_referenced marker so
        callbacks stay occasional."""
        st = self._load()
        try:
            from . import continuity as _cont
            hit = _cont.relevant_callback(st, user_text)
            if hit is not None:
                met = st.setdefault("metrics", {})
                met["callbacks"] = int(met.get("callbacks") or 0) + 1
                self._save(st)
            return hit
        except Exception:
            return None

    def note_expression(self, kinds: list[str]) -> None:
        """Bump expression-saturation counters (called by the vocal
        engine / reply path when humor, sarcasm, vocals, or gestures
        were actually expressed)."""
        st = self._load()
        try:
            from . import continuity as _cont
            _cont.note_expression(st, kinds)
            met = st.setdefault("metrics", {})
            for k in kinds:
                if k in ("vocal", "gesture"):
                    met[k] = int(met.get(k) or 0) + 1
            self._save(st)
        except Exception:
            pass

    def record_pattern(self, kind: str, pattern: str) -> None:
        """Remember an analogy/humor/metaphor structure tag so it isn't
        re-used too soon."""
        st = self._load()
        try:
            from . import continuity as _cont
            _cont.note_pattern(st, kind, pattern)
            self._save(st)
        except Exception:
            pass

    def humor_feedback(self, style: str, positive: bool) -> None:
        st = self._load()
        try:
            from . import continuity as _cont
            _cont.humor_feedback(st, positive, style)
            self._save(st)
        except Exception:
            pass

    def record_stated_pref(self, subject: str, stance: str) -> None:
        st = self._load()
        try:
            from . import continuity as _cont
            _cont.record_stated_preference(st, subject, stance)
            self._save(st)
        except Exception:
            pass

    def set_address(self, address: str) -> str:
        """Profile-level preferred address — independent of Creator
        titles ('none' clears it)."""
        st = self._load()
        try:
            from . import continuity as _cont
            out = _cont.set_address(st, address)
            self._save(st)
            return out
        except Exception:
            return str(st.get("address") or "")

    def metrics(self) -> dict:
        """Aggregate QA rates — words/reply, question rate, humor rate,
        callback rate, name-usage rate. Vocal/gesture counters are
        bumped via note_expression."""
        st = self._load()
        met = st.get("metrics") or {}
        turns = max(1, int(st.get("turns") or 0))
        words = int(met.get("words") or 0)
        return {"turns": turns,
                "avg_words": round(words / turns, 1),
                "question_rate": round(
                    int(met.get("questions") or 0) / turns, 2),
                "humor_rate": round(
                    int(met.get("humor") or 0) / turns, 2),
                "sarcasm_user_rate": round(
                    int(met.get("user_sarcasm") or 0) / turns, 2),
                "vocal_rate": round(
                    int(met.get("vocal") or 0) / turns, 2),
                "gesture_rate": round(
                    int(met.get("gesture") or 0) / turns, 2),
                "callback_rate": round(
                    int(met.get("callbacks") or 0) / turns, 2),
                "name_use_rate": round(
                    int(met.get("name_uses") or 0) / turns, 2)}

    def continuity_view(self) -> dict:
        """Safe inspect surface — counts/stances, no raw memory."""
        try:
            from . import continuity as _cont
            return _cont.continuity_summary(self._load())
        except Exception:
            return {}

    def reset_continuity(self) -> None:
        """Clear shared-history milestones, stated preferences,
        pattern memory, humor feedback — relationship familiarity and
        profile memory are preserved."""
        st = self._load()
        st["milestones"], st["stated_prefs"] = [], []
        st["patterns"], st["humor_feedback"] = {}, {}
        st["saturation"] = {k: 0.0 for k in st.get("saturation") or {}}
        self._save(st)

    def reset_relationship(self) -> None:
        """Reset familiarity/mood adaptation without touching memories
        or persona config."""
        st = self._load()
        st["familiarity"], st["stage"] = 0.0, "new"
        st["mood"] = _blank()["mood"]
        st["focus"] = {"topic": "", "prev": "", "since_turn": 0}
        self._save(st)

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
