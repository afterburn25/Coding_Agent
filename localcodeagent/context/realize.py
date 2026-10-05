"""Persona-aware surface realization — same facts, different words.

SemanticResponse carries WHAT must be communicated; rendering decides
HOW it is said. Variation lives inside the persona's style envelope —
facts, numbers, identifiers, permissions, and safety decisions never
mutate (§43–§46).

The ledger fingerprints each realization; a near-duplicate of a recent
reply is rerendered rather than repeated (§31–§32, §50).
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any


def _norm_words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9\s']", " ", str(text or "").lower()).split()


def fingerprint(text: str) -> str:
    """Stable surface fingerprint — the sentence-pattern signature used
    for duplicate detection, not a semantic hash."""
    return hashlib.sha1(_norm_text(text).encode()).hexdigest()[:16]


def _norm_text(text: str) -> str:
    return " ".join(_norm_words(text))


def similarity(a: str, b: str) -> float:
    """Lexical similarity — Jaccard over normalized word sets plus a
    length factor so a short canned line can't hide inside a long one."""
    wa, wb = set(_norm_words(a)), set(_norm_words(b))
    if not wa or not wb:
        return 0.0
    jaccard = len(wa & wb) / len(wa | wb)
    size = min(len(wa), len(wb)) / max(len(wa), len(wb))
    return jaccard * (0.5 + 0.5 * size)


def opening_of(text: str) -> str:
    """First clause — the opening-style signature for rotation checks."""
    first = re.split(r"[.!?\n]", str(text or "").strip(), maxsplit=1)[0]
    return " ".join(_norm_words(first)[:6])


def closing_of(text: str) -> str:
    last = re.split(r"[.!?\n]", str(text or "").strip())[-1].strip()
    if not last:
        parts = [p for p in re.split(r"[.!?\n]", str(text or "")) if p.strip()]
        last = parts[-1].strip() if parts else ""
    return " ".join(_norm_words(last)[-6:])


@dataclass
class SemanticResponse:
    """WHAT must be communicated — fact slots, never prose."""
    facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    actions_completed: list[str] = field(default_factory=list)
    next_options: list[str] = field(default_factory=list)
    error_code: str = ""


@dataclass
class ResponseFingerprint:
    intent: str
    opening: str
    closing: str
    text_hash: str
    ts: float


class ResponseLedger:
    """Rolling bounded history of realizations — the repetition memory
    (§41). Cooldowns: an opening/closing/pattern just used can't be
    picked again until enough different ones intervene."""

    def __init__(self, maxlen: int = 24):
        self._rows: list[ResponseFingerprint] = []
        self._maxlen = maxlen

    def record(self, intent: str, text: str) -> ResponseFingerprint:
        fp = ResponseFingerprint(
            intent=intent,
            opening=opening_of(text),
            closing=closing_of(text),
            text_hash=fingerprint(text),
            ts=time.time(),
        )
        self._rows.append(fp)
        self._rows = self._rows[-self._maxlen:]
        return fp

    def repetition_score(self, text: str, *, intent: str = "") -> dict[str, Any]:
        """Observable repetition metric (§50) — diagnostics metadata,
        never reasoning."""
        cand_hash = fingerprint(text)
        recent = self._rows[-8:]
        scores = {
            "exact_duplicate": any(r.text_hash == cand_hash for r in recent),
            "max_lexical_similarity": 0.0,
            "opening_reuse": False,
            "closing_reuse": False,
            "recent_count": len(recent),
        }
        op, cl = opening_of(text), closing_of(text)
        for r in recent:
            if r.opening and r.opening == op:
                scores["opening_reuse"] = True
            if r.closing and r.closing == cl:
                scores["closing_reuse"] = True
        return scores

    def recent_openings(self, n: int = 6) -> list[str]:
        return [r.opening for r in self._rows[-n:] if r.opening]

    def recent_closings(self, n: int = 6) -> list[str]:
        return [r.closing for r in self._rows[-n:] if r.closing]


class PersonaRenderer:
    """Variant-bank realization for deterministic content — fast lane
    (§38): no model call to say "GitHub is connected" a different way.

    Selection: rotating cursor biased away from fingerprints in the
    ledger — same fact, new sentence shape, still one voice.
    """

    def __init__(self, ledger: ResponseLedger | None = None):
        self.ledger = ledger or ResponseLedger()
        self._cursor = 0

    def render(self, semantic_id: str, variants: list[str] | tuple[str, ...],
               *, intent: str = "") -> str:
        """Pick the least-recently-used variant that isn't a near-repeat
        of the last few replies. Falls back to rotation when every
        variant was used recently — the bank's size is the cooldown."""
        pool = [str(v) for v in variants if str(v).strip()]
        if not pool:
            return ""
        if len(pool) == 1:
            return pool[0]
        recent = self.ledger._rows[-max(3, len(pool) - 1):]
        banned = {r.text_hash for r in recent}
        banned_open = {r.opening for r in recent}
        start = self._cursor
        for i in range(len(pool)):
            cand = pool[(start + i) % len(pool)]
            if fingerprint(cand) in banned:
                continue
            if opening_of(cand) in banned_open and i < len(pool) - 1:
                continue
            self._cursor = (start + i + 1) % len(pool)
            return cand
        self._cursor = (start + 1) % len(pool)
        return pool[start % len(pool)]

    def wrap(self, text: str, *, openers: tuple[str, ...] = (),
             intent: str = "") -> str:
        """Stored-answer lane (§37): canonical content passes through
        untouched; only the wrapper varies — no fact drift, no replayed
        identical envelope."""
        text = str(text or "").strip()
        if not text:
            return text
        if not openers:
            return text
        banned = set(self.ledger.recent_openings())
        for i in range(len(openers)):
            cand = openers[(self._cursor + i) % len(openers)]
            if opening_of(cand) not in banned:
                self._cursor += i + 1
                return f"{cand} {text}"
        return text


# --------------------------------------------------------------------------
# Variant banks — deterministic content families. Every variant states the
# identical fact; the persona's voice lives in phrasing, not in the truth.
# --------------------------------------------------------------------------

GREETING_VARIANTS = (
    "Hi! Nexus Core is ready. What would you like to work on?",
    "Hey — Nexus Core online. What's on the docket?",
    "Hey. Everything's up — what are we building?",
    "Hi there. Systems are ready when you are.",
)

CAPABILITY_VARIANTS = (
    "I can inspect and edit code, build features, debug errors, run tests "
    "and commands with permission gates, research technical and "
    "general-knowledge questions, work with Git/GitHub when authorized, "
    "manage local models, use configured local image tools, and learn "
    "across conversations through Nexus Brain.",
    "Short version: code, debug, test, research technical and "
    "general-knowledge questions, Git/GitHub, local models, local image "
    "tools — plus Nexus Brain learning across sessions, all behind "
    "permission gates.",
    "I work the full loop — inspect, edit, build, test, debug — plus "
    "technical and general-knowledge research, Git and GitHub when "
    "connected, local model management, image generation and editing, "
    "and Nexus Brain memory that carries between sessions.",
    "Think of me as a workstation: code editing and building, test and "
    "command execution behind permissions, technical and "
    "general-knowledge research, GitHub integration, local models, "
    "local image tools, and persistent Nexus Brain learning.",
)

SELF_LEARNING_VARIANTS = (
    "Yes. With Nexus Brain enabled, I can adapt beyond coding: bank "
    "verified general knowledge, remember facts and preferences, learn "
    "conversational patterns from feedback and corrections, and carry "
    "approved training examples across model replacements.",
    "Yes — Nexus Brain learns across conversations: verified general "
    "knowledge, your preferences, conversational patterns from feedback, "
    "and reviewed training examples carried across model replacements.",
    "I can. Nexus Brain handles verified general knowledge, remembered "
    "preferences, conversational patterns from corrections and "
    "feedback, and approved training examples — carried across model "
    "replacements, independent of whichever model is loaded.",
)

IDENTITY_VARIANTS = (
    "I am Nexus Core, a local-first AI coding workstation created by "
    "John Hamburn. I am software, not a person.",
    "Nexus Core — a local-first AI coding workstation built by "
    "John Hamburn. Software, not a person.",
    "I'm Nexus Core: local-first coding workstation, built by "
    "John Hamburn. Not a person — software.",
)

# Answer-Memory repeat acknowledgements (§37): when the identical stored
# fact would replay back-to-back, the wrapper marks it a repeat honestly
# instead of parroting the same paragraph. Canonical text follows
# verbatim.
REPEAT_ACKS = (
    "Same answer as before —",
    "Still the case —",
    "As before —",
    "No change on that —",
    "Repeating the earlier answer —",
)
