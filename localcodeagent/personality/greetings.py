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
    "Welcome {address}, to Nexus Core. I'm Nexus, your personal AI "
    "assistant.\n"
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
        "Welcome back, {address}. I've been waiting for you. "
        "Everything's online—tell me what you want to get into.",
        "There you are, {address}. Everything's online and so am I.",
        "Welcome back, {address}. I've been keeping everything warm "
        "for you.",
    ],
}


def preferred_address(profile: dict | None) -> str:
    """What Nexus calls this user — creator_address for Creators who
    set one, otherwise first name."""
    if not profile:
        return ""
    if profile.get("is_creator") and profile.get("creator_address"):
        return str(profile["creator_address"])
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
        address = preferred_address(profile) or "there"
        style = str(p.get("greeting_style") or "default")
        if style == "flirty" and not is_adult:
            style = "default"            # adult template only for adults

        if profile and not profile.get("has_completed_intro"):
            kind, text = "intro", INTRO_TEMPLATE.format(address=address)
        else:
            variants = RETURNING.get(style) or RETURNING["default"]
            kind = "returning"
            text = variants[self._next_rotation(len(variants))].format(
                address=address)
        if health and health != "ok":
            text += (" (Note: some background systems are still coming "
                     "online.)")
        voice = map_voice(p.get("voice"), p.get("traits"),
                          strength=int(p.get("strength") or 0),
                          mood=str(p.get("mood") or ""))
        return {"kind": kind, "text": text, "voice": voice,
                "address": address,
                "style": style}
