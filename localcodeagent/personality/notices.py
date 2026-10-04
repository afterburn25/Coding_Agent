"""Persona-aware presentation for system-facing text — notifications,
queued-task notices, completion alerts, error explanations, and status
summaries.

The *facts* of an event never change; only the wrapper phrasing shifts
with the effective persona. Every function is deterministic and pure —
the same (event, card) always yields the same text, and every output
keeps the factual core verbatim.
"""
from __future__ import annotations

from typing import Any

# Event kinds recognized by persona_notice().
KINDS = ("queued", "started", "completed", "failed", "cancelled",
         "approval", "briefing", "self_repair", "update", "status")

# Style prefix/suffix per persona family — deliberately short; the
# factual body always follows unchanged.
_PREFIX: dict[str, dict[str, str]] = {
    "queued": {
        "default": "Queued.", "professional": "Task queued.",
        "warm": "Got it — queued for you.",
        "playful": "On the list!",
        "nerdy": "Queued — FIFO position noted.",
        "calm": "It's queued — no rush.",
        "sassy": "Fine, it's queued.", "rude": "Queued. Wait.",
        "flirty": "Anything for you — queued.",
        "raunchy": "Queued, hot stuff.",
    },
    "started": {
        "default": "Starting now.", "professional": "Work has begun.",
        "warm": "Starting on it now.", "playful": "On it!",
        "nerdy": "Admitted — executing.",
        "calm": "Starting now — steady pace.",
        "sassy": "On it, already.", "rude": "Started. Hold on.",
        "flirty": "Starting for you now.", "raunchy": "On it, gorgeous.",
    },
    "completed": {
        "default": "Done.", "professional": "Task complete.",
        "warm": "All done — nicely handled.",
        "playful": "Done and dusted!",
        "nerdy": "Completed — all checks green.",
        "calm": "Finished — all quiet now.",
        "sassy": "Done. You're welcome.",
        "rude": "Done. Finally.", "flirty": "Done, just for you.",
        "raunchy": "Done — and it was glorious.",
    },
    "failed": {
        "default": "That didn't work.",
        "professional": "The task did not complete.",
        "warm": "That hit a snag — here's what happened.",
        "playful": "Oof — that one fought back.",
        "nerdy": "Failure — here's the mechanism.",
        "calm": "It didn't finish — let's look calmly.",
        "sassy": "Well, that flopped.",
        "rude": "It failed. Obviously.",
        "flirty": "That one slipped away from us.",
        "raunchy": "That crashed and burned.",
    },
    "cancelled": {
        "default": "Cancelled.", "professional": "The task was cancelled.",
        "warm": "I stopped that one — all clear.",
        "playful": "Poof — cancelled.",
        "nerdy": "Execution aborted cleanly.",
        "calm": "Cancelled — nothing left hanging.",
        "sassy": "Cancelled. As you wish.",
        "rude": "Cancelled. Done waiting.",
        "flirty": "Cancelled, darling.",
        "raunchy": "Killed it. You're welcome.",
    },
    "approval": {
        "default": "Needs your approval.",
        "professional": "Approval required to proceed.",
        "warm": "One thing needs your okay first.",
        "playful": "Waiting on your thumbs-up.",
        "nerdy": "Gate check — approval required.",
        "calm": "Whenever you're ready, it needs approval.",
        "sassy": "Your move — approve or not.",
        "rude": "Needs approval. Decide.",
        "flirty": "Just needs your sign-off, darling.",
        "raunchy": "Waiting on your say-so.",
    },
    "briefing": {
        "default": "Morning briefing.",
        "professional": "Daily briefing.",
        "warm": "Good morning — here's where things stand.",
        "playful": "Rise and shine — the scoop:",
        "nerdy": "Status dump incoming.",
        "calm": "Good morning. A calm rundown:",
        "sassy": "Morning. Try to keep up.",
        "rude": "Briefing. Read it.",
        "flirty": "Morning, you. Here's the rundown.",
        "raunchy": "Morning briefing, hot stuff.",
    },
    "self_repair": {
        "default": "Self-repair ran.",
        "professional": "Self-repair executed.",
        "warm": "I patched myself up — details below.",
        "playful": "Fixed myself. Kinda cool, honestly.",
        "nerdy": "Self-repair cycle completed.",
        "calm": "Self-repair handled it — all steady.",
        "sassy": "I fixed it myself, as usual.",
        "rude": "Self-repair. Handled.",
        "flirty": "I took care of it myself.",
        "raunchy": "Patched myself up real good.",
    },
    "update": {
        "default": "Update available.",
        "professional": "An update is available.",
        "warm": "There's an update waiting when you're ready.",
        "playful": "New version alert!",
        "nerdy": "New version detected in the feed.",
        "calm": "An update is available — no rush.",
        "sassy": "Update's here. Eventually you'll install it.",
        "rude": "Update available. Do it or don't.",
        "flirty": "A little something new just for you.",
        "raunchy": "Fresh update, ready when you are.",
    },
    "status": {
        "default": "Status:", "professional": "Status report.",
        "warm": "Here's how things are looking.",
        "playful": "The state of things:",
        "nerdy": "Telemetry summary:",
        "calm": "Current state, at a glance:",
        "sassy": "Status — try not to break anything.",
        "rude": "Status. Here.",
        "flirty": "The situation, just for you:",
        "raunchy": "The state of play:",
    },
}

_FALLBACK_PREFIX = {
    "queued": "Queued.", "started": "Started.", "completed": "Done.",
    "failed": "That didn't work.", "cancelled": "Cancelled.",
    "approval": "Needs your approval.",
    "briefing": "Briefing.", "self_repair": "Self-repair ran.",
    "update": "Update available.", "status": "Status:",
}

# Seriousness suppression — critical/serious contexts get a plain
# wrapper regardless of persona.
_PLAIN_PREFIX = {k: v for k, v in _FALLBACK_PREFIX.items()}


def persona_notice(kind: str, fact_text: str, card: dict | None) -> str:
    """Wrap a factual notice in persona phrasing. ``fact_text`` is
    preserved verbatim — the persona only chooses the lead-in."""
    fact = str(fact_text or "").strip()
    if not fact:
        return ""
    c = card or {}
    if str(c.get("seriousness") or "") in ("serious", "critical"):
        head = _PLAIN_PREFIX.get(kind, "Status:")
    else:
        fam = str(c.get("family") or "default")
        head = _PREFIX.get(kind, {}).get(
            fam, _FALLBACK_PREFIX.get(kind, "Status:"))
    return f"{head} {fact}".strip()


def error_explanation(kind: str, detail: str,
                      card: dict | None) -> str:
    """User-facing error line — persona lead-in + verbatim technical
    detail. Detail is never rewritten."""
    return persona_notice("failed", str(detail or ""), card)


def status_summary(facts: str, card: dict | None) -> str:
    return persona_notice("status", str(facts or ""), card)
