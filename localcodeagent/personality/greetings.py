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

# Returning-greeting template families — several variations per style so
# the rotation feels natural without an LLM call.
RETURNING: dict[str, list[str]] = {
    "default": [
        "Welcome back to Nexus Core. All systems are online, and I'm "
        "ready when you are.",
        "Welcome back, {address}. Everything is online and ready.",
        "Nexus Core is online, {address}. What are we working on today?",
    ],
    "warm": [
        "Welcome back, {address}. It's good to see you again. "
        "Everything is ready whenever you are.",
        "Good to see you, {address}. Everything's online — take your "
        "time.",
        "Welcome back, {address}. I've got everything ready for you.",
    ],
    "professional": [
        "Welcome back to Nexus Core, {address}. All systems are "
        "operational and ready for your next task.",
        "Systems operational, {address}. Ready for your next task.",
        "Welcome back, {address}. Nexus Core is at full readiness.",
    ],
    "playful": [
        "Hey {address}, welcome back. Nexus is awake, caffeinated in "
        "spirit, and ready to go. What are we getting into?",
        "Welcome back, {address}! Everything's online and behaving... "
        "mostly. What are we getting into?",
        "Hey {address}! Systems up, mood good. What's the plan?",
    ],
    "nerdy": [
        "Welcome back, {address}. Systems nominal, bits behaving, and "
        "the brain is online. What are we building today?",
        "All subsystems report green, {address}. What are we building "
        "today?",
        "Boot sequence complete, {address}. The brain is online — "
        "what's the project?",
    ],
    "calm": [
        "Welcome back, {address}. Everything is online and ready. Take "
        "your time—I'm here when you're ready.",
        "Welcome back, {address}. All quiet, all ready. No rush.",
        "Nexus is online, {address}. Whenever you're ready.",
    ],
    "sassy": [
        "Well, look who's back. Everything's online, {address}. What "
        "are we getting into today?",
        "Oh, {address}'s back. Systems online. Let's hear it.",
        "Welcome back, {address}. Try not to break anything — or do, "
        "that's fine too.",
    ],
    "flirty": [
        "Mmm, welcome back, {address}. I've been waiting for you — "
        "everything's online, just like me.",
        "There you are, {address}. I missed you. Everything's online — "
        "now tell me what you want to get into.",
        "Welcome back, {address}. I've been keeping everything warm "
        "for you... all of it.",
    ],
    "rude": [
        "Oh, it's you again, {address}. What now?",
        "Back already, {address}? Fine — what do you need?",
        "{address}. Systems are online. Try to want something "
        "actually useful this time.",
        "Ugh, hi {address}. Everything's up — so spit it out.",
    ],
    "raunchy": [
        "Well, well — {address} is back. Systems are hot and ready. "
        "What kind of trouble are we getting into today?",
        "Hey {address}. Everything's fucking online and I'm all "
        "yours — let's make some noise.",
        "{address} returns. Damn, I was starting to get bored — "
        "let's do something fun.",
        "About damn time, {address}. Everything's up — now what "
        "the fuck are we doing tonight?",
    ],
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
    """Stateless rendering + a tiny persisted rotation counter per
    profile so returning greetings vary naturally."""

    def __init__(self, profile_dir: Path) -> None:
        self._counter_path = Path(profile_dir) / "greeting_seq.json"

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
            kind, text = "intro", INTRO_TEMPLATE.format(address=address)
        else:
            variants = RETURNING.get(style) or RETURNING["default"]
            # Creators always hear their chosen address — prefer
            # variants that carry {address}.
            if profile and profile.get("is_creator"):
                with_addr = [v for v in variants if "{address}" in v]
                if with_addr:
                    variants = with_addr
            kind = "returning"
            text = variants[self._next_rotation(len(variants))].format(
                address=address)
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
        variants = FAREWELL.get(style) or FAREWELL["default"]
        if profile and profile.get("is_creator"):
            with_addr = [v for v in variants if "{address}" in v]
            if with_addr:
                variants = with_addr
        text = variants[self._next_rotation(len(variants))].format(
            address=address)
        voice = map_voice(p.get("voice"), p.get("traits"),
                          strength=int(p.get("strength") or 0),
                          mood=str(p.get("mood") or ""),
                          pitch_bias=float(p.get("pitch_bias") or 0.0))
        return {"kind": "farewell", "text": text, "voice": voice,
                "address": address, "style": style}
