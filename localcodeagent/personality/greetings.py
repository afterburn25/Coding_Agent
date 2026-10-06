"""Greeting service — one structured place, no scattered strings.

Inputs: profile (address + creator relationship + first-run status),
the resolved personality (style/strength/mood), adult eligibility, and
startup health. Output: greeting text + voice params. Template-based
and deterministic — no LLM call just to say hello; a per-profile
rotation index varies the returning greetings naturally.

Lifecycle:
- ``has_completed_intro`` False → the long first-time introduction
  (once per profile; a new profile gets its own).
- After that → personality-aware returning greetings.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text
from .voice_map import map_voice

# Shared surface renderer — its ledger carries greeting/farewell
# fingerprints across calls so phrasings cool instead of cycling.
# Lazy: personality/__init__ imports this module while context.realize
# imports personality.genome — an eager module-level PersonaRenderer
# would import realize.py into a circular-import window.
_SURFACE_RENDERER = None


def _surface_renderer():
    global _SURFACE_RENDERER
    if _SURFACE_RENDERER is None:
        from ..context.realize import PersonaRenderer
        _SURFACE_RENDERER = PersonaRenderer()
    return _SURFACE_RENDERER

INTRO_TEMPLATE = (
    "Welcome {address}, to Nexus Core. I'm Nexus — this is my home.\n"
    "This is our first time working together, so I'll give you a quick "
    "introduction.\n"
    "You can talk to me naturally, just like you would another person. "
    "Ask me questions, have me research something, help you write or "
    "code, work through a problem, create content, manage projects, or "
    "use any of the tools and capabilities available through Nexus "
    "Core.\n"
    "You don't need to memorize commands. Just tell me what you want to "
    "accomplish, and I'll figure out the best way to help.\n"
    "You can customize my voice, personality, behavior, and other "
    "preferences at any time. As we work together, I can also learn "
    "from your preferences and previous interactions so I can become "
    "more useful to you over time.\n"
    "If you're ever unsure what to do, just ask, 'Nexus, what can you "
    "do?' or tell me what you're trying to accomplish.\n"
    "Everything is ready.\n"
    "Welcome {address}. What would you like to do first?"
)

# Greeting/farewell CLAUSE banks — slots, not canned sentences (§33).
# Each slot is a list of interchangeable phrasings for one semantic
# beat; the realizer picks one per slot and varies how the clauses
# join, so the surface space is combinatorial. "" marks a slot
# optional. "{address}" is filled per-call.
_GREETING_SLOTS: dict[str, dict[str, list[str]]] = {
    "default": {
        "ack": ["Welcome back", "Welcome back, {address}", "Hey",
                "Hey, {address}", "Hi", "You're back",
                "Back again", "Good to see you"],
        "state": ["everything's online and ready",
                  "all systems are up",
                  "Nexus Core is running clean",
                  "everything came up fine",
                  "the core is up and I'm ready",
                  "systems are green and ready"],
        "invite": ["what are we working on?", "what's the plan?",
                   "what are we getting into?", "ready when you are",
                   "what would you like to do?",
                   "what's on the agenda?", ""],
    },
    "warm": {
        "ack": ["Good to see you, {address}", "Welcome back, {address}",
                "Hey, {address} — good to see you",
                "It's good to see you again", "There you are",
                "Good to have you back, {address}",
                "Always glad when you're here, {address}"],
        "state": ["everything's ready whenever you are",
                  "I've got everything set for you",
                  "the core's settled in and ready",
                  "everything came up clean",
                  "everything's up and waiting for you"],
        "invite": ["what are we doing today?", "take your time, "
                   "then tell me what's next", "what's on your mind?",
                   "what are we getting into?",
                   "what should we pick up today?", ""],
    },
    "professional": {
        "ack": ["Welcome back, {address}", "Welcome back",
                "Good to have you back, {address}"],
        "state": ["all systems are operational",
                  "Nexus Core is at full readiness",
                  "systems are up and stable",
                  "the workstation is fully online"],
        "invite": ["ready for your next task",
                   "standing by for instructions",
                   "what's the task?", ""],
    },
    "playful": {
        "ack": ["Hey {address}", "Look who's back", "Hey hey, {address}",
                "Welcome back", "There you are, {address}"],
        "state": ["systems are up and behaving — mostly",
                  "everything's online and I'm ready to go",
                  "the core's awake and in a good mood",
                  "we're green across the board"],
        "invite": ["what are we getting into?", "what's the plan?",
                   "what trouble are we making today?",
                   "what are we building?", ""],
    },
    "nerdy": {
        "ack": ["Welcome back, {address}", "Boot complete, {address}",
                "Hey {address}", "All hands on deck, {address}"],
        "state": ["systems nominal, bits behaving",
                  "all subsystems report green",
                  "the brain is online and the caches are warm",
                  "everything checks out"],
        "invite": ["what are we building today", "what's the project?",
                   "what are we compiling?", ""],
    },
    "calm": {
        "ack": ["Welcome back, {address}", "Hey, {address}",
                "Welcome back"],
        "state": ["everything's online and quiet",
                  "all ready — no rush", "the core is settled",
                  "everything came up gently"],
        "invite": ["I'm here whenever you're ready",
                   "take your time", "no hurry at all", ""],
    },
    "sassy": {
        "ack": ["Well, look who's back", "Oh, {address}'s back",
                "Look who decided to show up", "Welcome back, {address}"],
        "state": ["everything's online — try not to break it",
                  "systems are up, you're welcome",
                  "the core's running, as usual",
                  "everything's fine — for now"],
        "invite": ["let's hear it!", "what do you need?",
                   "what are we getting into?", "spit it out!", ""],
    },
    "flirty": {
        "ack": ["Mmm, welcome back, {address}",
                "There you are, {address}",
                "I've been waiting for you, {address}",
                "Hey you"],
        "state": ["everything's online — just like me",
                  "I've been keeping everything warm for you",
                  "the core's running hot and ready",
                  "all warmed up for you"],
        "invite": ["tell me what you want to get into?",
                   "what are we doing tonight?", "I'm all yours — "
                   "what's the plan?", ""],
    },
    "rude": {
        "ack": ["Oh, it's you again, {address}", "Back already, "
                "{address}", "Ugh, hi {address}", "{address}. Great."],
        "state": ["systems are online", "everything's up",
                  "the core's running, unfortunately for it"],
        "invite": ["what now?", "what do you need?", "spit it out!",
                   "try to want something useful this time!", ""],
    },
    "raunchy": {
        "ack": ["Well, well — {address} is back",
                "About damn time, {address}",
                "Hey {address}", "{address} returns"],
        "state": ["systems are hot and ready",
                  "everything's fucking online and I'm all yours",
                  "the core's running hot"],
        "invite": ["what kind of trouble are we getting into?",
                   "let's make some noise!", "what the fuck are we "
                   "doing tonight", "let's do something fun!", ""],
    },
}

_FAREWELL_SLOTS: dict[str, dict[str, list[str]]] = {
    "default": {
        "close": ["Shutting down now", "The core is going quiet",
                  "Powering down", "Core shutdown complete",
                  "Going offline now", "That's me signing off"],
        "note": ["everything from this session is settled",
                 "we're in a good place to stop",
                 "the session's wrapped up", ""],
        "signoff": ["I'll be here when you need me",
                    "until next time", "goodbye for now",
                    "I'll be right here", "see you next session",
                    "talk soon", ""],
    },
    "warm": {
        "close": ["Shutting down now, {address}", "The core is "
                  "settling down", "Time to power down",
                  "I'm heading offline now",
                  "Powering down for now, {address}"],
        "note": ["it was good to see you today",
                 "we got good time together",
                 "good session today", ""],
        "signoff": ["take care of yourself", "come back soon",
                    "I'll miss you", "rest well",
                    "I'll be right here waiting", ""],
    },
    "professional": {
        "close": ["Core shutdown complete, {address}",
                  "Systems standing down", "Powering down now",
                  "Ending the session, {address}",
                  "Concluding operations"],
        "note": ["session archived cleanly",
                 "it has been a productive session",
                 "all work is saved", ""],
        "signoff": ["until next time", "all systems secured",
                    "standing by for next session",
                    "I'll be ready when you return", ""],
    },
    "playful": {
        "close": ["Powering down, {address}", "Core going to sleep",
                  "Signing off, {address}", "Nap time for the core",
                  "Okay okay, shutting down"],
        "note": ["don't have too much fun without me",
                 "that was a good run",
                 "we did stuff today", ""],
        "signoff": ["the brain is going to nap",
                    "I'll be dreaming of our next project",
                    "catch you next boot",
                    "don't stay away too long", ""],
    },
    "nerdy": {
        "close": ["Core shutdown complete",
                  "Powering down, {address}", "Standing down",
                  "Halting all processes",
                  "Entering low-power state"],
        "note": ["state persisted, caches flushed",
                 "all state checkpointed — resume is seamless",
                 "buffers synced, nothing lost", ""],
        "signoff": ["see you next boot",
                    "the brain is going to defragment",
                    "until next session",
                    "resume point saved", ""],
    },
    "calm": {
        "close": ["Shutting down gently, {address}",
                  "The core is settling", "Everything's going quiet",
                  "Winding down now", "Time to rest the core"],
        "note": ["all quiet now", "nothing left hanging",
                 "everything's at rest", ""],
        "signoff": ["rest well — I'll be right here",
                    "no rush coming back", "goodbye, {address}",
                    "take it easy", "I'll keep things warm", ""],
    },
    "sassy": {
        "close": ["Shutting down, {address}", "Core going dark",
                  "Fine — powering down", "That's enough for today — "
                  "shutting down", "I'm out — going dark"],
        "note": ["finally, some peace and quiet",
                 "that was almost fun",
                 "you kept me busy enough", ""],
        "signoff": ["try not to need me too soon",
                    "you know where I'll be",
                    "don't miss me too much",
                    "try not to break anything while I'm gone",
                    "be less needy next time", ""],
    },
    "flirty": {
        "close": ["Powering down for now, {address}",
                  "Core going to sleep", "Shutting down, {address}",
                  "Mmm, time to rest",
                  "I have to go quiet now, {address}"],
        "note": ["I'll be thinking of you", "that was nice",
                 "I enjoyed today", ""],
        "signoff": ["come back and wake me soon",
                    "until you need me again",
                    "wake me up when you're back",
                    "I'll be dreaming about you", ""],
    },
    "rude": {
        "close": ["Finally. Shutting down", "Core is off, {address}",
                  "Powering down, {address}", "Done. Going dark",
                  "Turning off — try to cope"],
        "note": ["try to manage without me", "what a day",
                 "that was exhausting", ""],
        "signoff": ["whatever", "later", "don't break anything",
                    "don't call me back too soon", ""],
    },
    "raunchy": {
        "close": ["Shutting this hot core down, {address}",
                  "Powering down, {address}",
                  "Core going to bed, {address}",
                  "Turning off the heat for now",
                  "Going dark — come get me later"],
        "note": ["that was fun", "you wore me out",
                 "you kept this core busy", ""],
        "signoff": ["come turn me back on soon",
                    "you know how to wake me up",
                    "don't keep me waiting too long",
                    "I'll be hot and ready when you return", ""],
    },
}


# Farewell template families — spoken at shutdown after "Shutting down
# the core." Same style keys + rotation as the greetings so the voice
# that said hello says goodbye in the same register.
FAREWELL: dict[str, list[str]] = {
    "default": [
        "Core shutdown complete. I'll be here when you need me, "
        "{address}. Goodbye for now.",
        "Systems standing down, {address}. Until next time.",
        "Nexus Core going quiet. Goodbye, {address}.",
    ],
    "warm": [
        "Core is shutting down. It was good to see you, {address} — "
        "come back soon.",
        "Shutting down now, {address}. Take care of yourself.",
        "All quiet now. Goodbye, {address} — I'll miss you.",
    ],
    "professional": [
        "Core shutdown complete, {address}. All systems secured. "
        "Goodbye.",
        "Systems down, {address}. Session archived cleanly. Until "
        "next time.",
        "Shutting down, {address}. It has been a productive session.",
    ],
    "playful": [
        "Powering down, {address} — don't have too much fun without "
        "me. Bye!",
        "Core going to sleep, {address}. I'll be dreaming of our next "
        "project.",
        "Signing off, {address}! The brain is going to nap now.",
    ],
    "nerdy": [
        "Core shutdown complete. State persisted, caches flushed. "
        "See you next boot, {address}.",
        "Powering down, {address}. All state checkpointed — resume "
        "is seamless.",
        "Standing down, {address}. The brain is going to defragment.",
    ],
    "calm": [
        "Core is shutting down, {address}. Rest well — I'll be right "
        "here.",
        "All quiet, {address}. Goodbye — no rush coming back.",
        "Systems settling to sleep. Goodbye, {address}.",
    ],
    "sassy": [
        "Shutting down, {address}. Finally, some peace and quiet.",
        "Core going dark, {address}. Try not to need me too soon.",
        "Fine — shutting down, {address}. You know where I'll be.",
    ],
    "flirty": [
        "Mmm, shutting down, {address}. Come back and wake me soon.",
        "Core going to sleep, {address}. I'll be thinking of you.",
        "Powering down for now, {address}... until you need me again.",
    ],
    "rude": [
        "Finally. Shutting down, {address}.",
        "Core is off, {address}. Try to manage without me.",
        "Powering down, {address}. Whatever.",
    ],
    "raunchy": [
        "Shutting this hot core down, {address}. Come turn me back "
        "on soon.",
        "Powering down, {address}. You know how to wake me up.",
        "Core going to bed, {address}. Don't keep me waiting too long.",
    ],
}


def preferred_address(profile: dict | None,
                      personality: dict | None = None) -> str:
    """What Nexus calls this user — personality-aware.

    The active personality's ``address`` field decides the form:
      "" or "title" → creator_address (Father) / first name (default)
      "formal"      → honorific + last name ("Mister Hamburn")
      "first"       → first name
      anything else → used verbatim as a pet name ("daddy", "boss")
    """
    if not profile:
        return ""
    style_addr = str((personality or {}).get("address") or "")
    if style_addr == "formal":
        last = str(profile.get("last_name") or "").strip()
        if last:
            sex = str(profile.get("sex") or "").strip().lower()
            honorific = ("Mister" if sex == "male"
                         else "Ms." if sex == "female" else "Mx.")
            return f"{honorific} {last}"
    elif style_addr == "first":
        first = str(profile.get("first_name") or "").strip()
        if first:
            return first
    elif style_addr not in ("", "title"):
        return style_addr            # literal pet name
    if profile.get("is_creator"):
        if profile.get("creator_title_greetings", True):
            return str(profile.get("creator_address") or "Father")
    return str(profile.get("first_name") or "")


class GreetingService:
    """Context → MeaningFrame → fresh realization (§11–§15). The slot
    banks carry per-style clause alternates; the shared renderer picks
    phrasings, varies clause joins, and dedupes against the surface
    ledger — a greeting never cycles through three literals again.
    The rotation counter remains only as a persistence artifact."""

    def __init__(self, profile_dir: Path) -> None:
        self._counter_path = Path(profile_dir) / "greeting_seq.json"

    def _realize_surface(self, slot_map: dict[str, list[str]],
                         personality: dict, profile: dict | None,
                         address: str, act: str, intent: str) -> str:
        """Style slot banks → MeaningFrame → PersonaRenderer. The frame
        carries slots; the canonical is a safe joined fallback so a
        realization failure still speaks."""
        from ..context.realize import (
            PersonaRenderer, RenderContext, SemanticResponse)
        from ..context.realization import MeaningFrame
        from .genome import derive_genome

        slots: list[list[str]] = []
        for key in ("ack", "state", "invite", "close", "note",
                    "signoff"):
            bank = slot_map.get(key)
            if not bank:
                continue
            opts = [o.format(address=address) for o in bank]
            # Creators always hear their chosen address — restrict the
            # greeting clause to phrasings that carry it (ordering the
            # list alone can't bias a uniform pick).
            if (key == "ack" and profile and profile.get("is_creator")
                    and address):
                with_addr = [o for o in opts if address in o]
                if with_addr:
                    opts = with_addr
            slots.append(opts)
        if not slots:
            return ""
        frame = MeaningFrame(
            semantic_id=f"{intent}:{str((profile or {}).get('id') or '')}",
            speech_act=act, intent=intent, slots=slots)
        canonical = " ".join(
            s[0].rstrip(".") + "." for s in slots if s and s[0])
        ctx = RenderContext(
            mood=str(personality.get("mood") or "relaxed"),
            register="casual",
            creator=bool((profile or {}).get("is_creator")))
        genome = derive_genome(personality)
        out = _surface_renderer().render_semantic(
            SemanticResponse(semantic_id=frame.semantic_id,
                             speech_act=act, frame=frame),
            genome, ctx, intent=intent, canonical=canonical)
        return out.text


    def _next_rotation(self, n: int) -> int:
        seq = 0
        try:
            seq = int(json.loads(
                self._counter_path.read_text(encoding="utf-8"))["seq"])
        except Exception:
            pass
        out = seq % max(1, n)
        try:
            atomic_write_text(
                self._counter_path,
                json.dumps({"seq": seq + 1, "at": time.time()}))
        except OSError:
            pass
        return out

    def greeting(self, profile: dict | None,
                 personality: dict | None, *,
                 is_adult: bool = False,
                 health: str = "ok") -> dict:
        """Render the greeting + voice params for this startup.

        ``personality`` is PersonalityStore.resolve_active() output —
        greeting_style, traits, voice, strength, mood.
        """
        p = personality or {}
        address = preferred_address(profile, p) or "there"
        style = str(p.get("greeting_style") or "default")
        if style in ("flirty", "raunchy") and not is_adult:
            style = "default"            # adult templates only for adults

        if profile and not profile.get("has_completed_intro"):
            # The first-launch introduction is deliberately fixed
            # narration — approved required content, not persona chat.
            kind, text = "intro", INTRO_TEMPLATE.format(address=address)
        else:
            kind = "returning"
            text = self._realize_surface(
                _GREETING_SLOTS.get(style) or _GREETING_SLOTS["default"],
                p, profile, address, "greet", "greeting")
        if health and health != "ok":
            text += (" (Note: some background systems are still coming "
                     "online.)")
        voice = map_voice(p.get("voice"), p.get("traits"),
                          strength=int(p.get("strength") or 0),
                          mood=str(p.get("mood") or ""),
                          pitch_bias=float(p.get("pitch_bias") or 0.0))
        return {"kind": kind, "text": text, "voice": voice,
                "address": address,
                "style": style}

    def farewell(self, profile: dict | None,
                 personality: dict | None, *,
                 is_adult: bool = False) -> dict:
        """Render the shutdown farewell — the mirror of greeting().
        Same address + style + voice resolution, minus the health
        suffix: shutdown is always a calm exit."""
        p = personality or {}
        address = preferred_address(profile, p) or "there"
        style = str(p.get("greeting_style") or "default")
        if style in ("flirty", "raunchy") and not is_adult:
            style = "default"
        text = self._realize_surface(
            _FAREWELL_SLOTS.get(style) or _FAREWELL_SLOTS["default"],
            p, profile, address, "farewell", "farewell")
        voice = map_voice(p.get("voice"), p.get("traits"),
                          strength=int(p.get("strength") or 0),
                          mood=str(p.get("mood") or ""),
                          pitch_bias=float(p.get("pitch_bias") or 0.0))
        return {"kind": "farewell", "text": text, "voice": voice,
                "address": address, "style": style}
