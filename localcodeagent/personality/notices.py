"""Persona-aware presentation for system-facing text — notifications,
queued-task notices, completion alerts, error explanations, and status
summaries.

The *facts* of an event never change; only the wrapper phrasing shifts
with the effective persona. Every function is deterministic and pure —
the same (event, card, seq) always yields the same text, and every
output keeps the factual core verbatim.
"""
from __future__ import annotations

from typing import Any

# Event kinds recognized by persona_notice().
KINDS = ("queued", "started", "completed", "failed", "cancelled",
         "approval", "briefing", "self_repair", "update", "status")

# Style prefix variants per persona family — deliberately short; the
# factual body always follows unchanged. Two phrasings per cell so
# repeated notices rotate instead of parroting one line; ``seq``
# selects the variant (None → 0).
_PREFIX: dict[str, dict[str, list[str]]] = {
    "queued": {
        "default": ["Queued.", "In the queue."],
        "professional": ["Task queued.", "Queued — awaiting capacity."],
        "warm": ["Got it — queued for you.", "It's in line for you."],
        "playful": ["On the list!", "In line it goes!"],
        "nerdy": ["Queued — FIFO position noted.",
                  "Enqueued — position recorded."],
        "calm": ["It's queued — no rush.", "In line — it'll get there."],
        "sassy": ["Fine, it's queued.", "Queued. Patience."],
        "rude": ["Queued. Wait.", "In line. Obviously."],
        "flirty": ["Anything for you — queued.", "Queued up, just for you."],
        "raunchy": ["Queued, hot stuff.", "In line, gorgeous."],
    },
    "started": {
        "default": ["Starting now.", "Kicking it off."],
        "professional": ["Work has begun.", "Execution started."],
        "warm": ["Starting on it now.", "Getting going on that now."],
        "playful": ["On it!", "Rolling!"],
        "nerdy": ["Admitted — executing.", "Dispatched — running."],
        "calm": ["Starting now — steady pace.", "Underway — nice and easy."],
        "sassy": ["On it, already.", "Started — try to keep up."],
        "rude": ["Started. Hold on.", "Going. Wait."],
        "flirty": ["Starting for you now.", "On it, just for you."],
        "raunchy": ["On it, gorgeous.", "Started — enjoy the show."],
    },
    "completed": {
        "default": ["Done.", "Finished."],
        "professional": ["Task complete.", "Completed successfully."],
        "warm": ["All done — nicely handled.", "Finished — that went well."],
        "playful": ["Done and dusted!", "Wrapped — easy!"],
        "nerdy": ["Completed — all checks green.",
                  "Terminated cleanly — exit zero."],
        "calm": ["Finished — all quiet now.", "Done — nothing left open."],
        "sassy": ["Done. You're welcome.", "Finished — obviously."],
        "rude": ["Done. Finally.", "Finished. Happy now?"],
        "flirty": ["Done, just for you.", "All finished, darling."],
        "raunchy": ["Done — and it was glorious.", "Finished. That was fun."],
    },
    "failed": {
        "default": ["That didn't work.", "It didn't finish."],
        "professional": ["The task did not complete.", "Execution failed."],
        "warm": ["That hit a snag — here's what happened.",
                 "It stumbled — here's the detail."],
        "playful": ["Oof — that one fought back.", "Welp — it won that round."],
        "nerdy": ["Failure — here's the mechanism.",
                  "Nonzero exit — diagnosis follows."],
        "calm": ["It didn't finish — let's look calmly.",
                 "It stopped short — nothing alarming."],
        "sassy": ["Well, that flopped.", "It died. Rude of it."],
        "rude": ["It failed. Obviously.", "Dead. Whatever."],
        "flirty": ["That one slipped away from us.",
                   "It didn't behave, darling."],
        "raunchy": ["That crashed and burned.", "It went down hard."],
    },
    "cancelled": {
        "default": ["Cancelled.", "Stopped."],
        "professional": ["The task was cancelled.", "Cancelled on request."],
        "warm": ["I stopped that one — all clear.",
                 "Cancelled — nothing's hanging."],
        "playful": ["Poof — cancelled.", "Snip — gone."],
        "nerdy": ["Execution aborted cleanly.", "Halted mid-flight — clean."],
        "calm": ["Cancelled — nothing left hanging.",
                 "Stopped — nice and tidy."],
        "sassy": ["Cancelled. As you wish.", "Gone. Happy?"],
        "rude": ["Cancelled. Done waiting.", "Killed it. Moving on."],
        "flirty": ["Cancelled, darling.", "Stopped — just say what's next."],
        "raunchy": ["Killed it. You're welcome.", "Put it down myself."],
    },
    "approval": {
        "default": ["Needs your approval.", "Waiting on your okay."],
        "professional": ["Approval required to proceed.",
                         "Held pending your approval."],
        "warm": ["One thing needs your okay first.",
                 "Just needs your go-ahead."],
        "playful": ["Waiting on your thumbs-up.",
                    "Bouncing it to you — yay or nay?"],
        "nerdy": ["Gate check — approval required.",
                  "Permission gate — awaiting input."],
        "calm": ["Whenever you're ready, it needs approval.",
                 "It'll wait — your call."],
        "sassy": ["Your move — approve or not.",
                  "Ball's in your court."],
        "rude": ["Needs approval. Decide.", "Waiting on you. Pick."],
        "flirty": ["Just needs your sign-off, darling.",
                   "One little yes from you."],
        "raunchy": ["Waiting on your say-so.", "Say the word, hot stuff."],
    },
    "briefing": {
        "default": ["Morning briefing.", "Here's the rundown."],
        "professional": ["Daily briefing.", "Status briefing follows."],
        "warm": ["Good morning — here's where things stand.",
                 "Welcome back — here's what you missed."],
        "playful": ["Rise and shine — the scoop:",
                    "While you were out — gossip:"],
        "nerdy": ["Status dump incoming.", "Delta report, ready."],
        "calm": ["Good morning. A calm rundown:",
                 "Welcome back — the quiet summary."],
        "sassy": ["Morning. Try to keep up.",
                  "Back already? Here's what happened."],
        "rude": ["Briefing. Read it.", "While you were gone. Listen."],
        "flirty": ["Morning, you. Here's the rundown.",
                   "Missed you — here's the recap."],
        "raunchy": ["Morning briefing, hot stuff.",
                    "Back for more? Here's the rundown."],
    },
    "self_repair": {
        "default": ["Self-repair ran.", "Self-repair finished."],
        "professional": ["Self-repair executed.",
                         "Automated repair completed."],
        "warm": ["I patched myself up — details below.",
                 "Fixed myself up — here's the note."],
        "playful": ["Fixed myself. Kinda cool, honestly.",
                    "Self-surgery complete!"],
        "nerdy": ["Self-repair cycle completed.",
                  "Autoremediation applied and verified."],
        "calm": ["Self-repair handled it — all steady.",
                 "Patched quietly — back to normal."],
        "sassy": ["I fixed it myself, as usual.",
                  "Handled it myself. Shocking, I know."],
        "rude": ["Self-repair. Handled.", "Fixed it myself. Done."],
        "flirty": ["I took care of it myself.",
                   "All patched up, darling."],
        "raunchy": ["Patched myself up real good.",
                    "Fixed myself — nice and thorough."],
    },
    "update": {
        "default": ["Update available.", "An update is ready."],
        "professional": ["An update is available.",
                         "A new version is available."],
        "warm": ["There's an update waiting when you're ready.",
                 "A fresh update is ready whenever you are."],
        "playful": ["New version alert!", "Ooh — shiny new version!"],
        "nerdy": ["New version detected in the feed.",
                  "Upstream artifact updated."],
        "calm": ["An update is available — no rush.",
                 "New version's ready — whenever."],
        "sassy": ["Update's here. Eventually you'll install it.",
                  "New version. No pressure."],
        "rude": ["Update available. Do it or don't.",
                 "New version. Your problem."],
        "flirty": ["A little something new just for you.",
                   "New version, darling — treat yourself."],
        "raunchy": ["Fresh update, ready when you are.",
                    "Something new's waiting for you."],
    },
    "status": {
        "default": ["Status:", "Current state:"],
        "professional": ["Status report.", "Current status follows."],
        "warm": ["Here's how things are looking.",
                 "A quick look at where we stand."],
        "playful": ["The state of things:", "Vibes report:"],
        "nerdy": ["Telemetry summary:", "System state follows."],
        "calm": ["Current state, at a glance:", "Where things stand:"],
        "sassy": ["Status — try not to break anything.",
                  "The damage report:"],
        "rude": ["Status. Here.", "State of things. Read."],
        "flirty": ["The situation, just for you:",
                   "Where we stand, darling:"],
        "raunchy": ["The state of play:", "How it's going, hot stuff:"],
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


def persona_notice(kind: str, fact_text: str, card: dict | None,
                   seq: int | None = None) -> str:
    """Wrap a factual notice in persona phrasing. ``fact_text`` is
    preserved verbatim — the persona only chooses the lead-in.
    ``seq`` rotates through the phrasing variants; None picks the
    first."""
    fact = str(fact_text or "").strip()
    if not fact:
        return ""
    c = card or {}
    if str(c.get("seriousness") or "") in ("serious", "critical"):
        head = _PLAIN_PREFIX.get(kind, "Status:")
    else:
        fam = str(c.get("family") or "default")
        variants = _PREFIX.get(kind, {}).get(fam)
        if not variants:
            head = _FALLBACK_PREFIX.get(kind, "Status:")
        else:
            i = (seq or 0) % len(variants)
            head = variants[i]
    return f"{head} {fact}".strip()


def status_summary(facts: str, card: dict | None) -> str:
    return persona_notice("status", str(facts or ""), card)
