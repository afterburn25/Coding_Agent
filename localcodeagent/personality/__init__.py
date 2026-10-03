"""Personality system — presets, sliders, strength, moods, voice map.

Presentation-only: personality shapes how results are delivered, never
what they are — facts, code correctness, tool permissions, and
safety/authorization state are untouched.

Whitelist-first: arbitrary personality JSON can only ever touch
schema.SLIDERS / schema.VOICE_CONTROLS keys; adult sliders and adult
presets additionally require is_adult derived from the immutable
birthdate — enforced here, not in the UI.
"""
from .schema import (
    ADULT_SLIDERS, CATEGORY_LABELS, MOODS, SLIDERS, VOICE_CONTROLS,
    clean_mood, clean_strength, clean_traits, clean_voice,
)
from .presets import PRESETS, get_preset, list_presets
from .store import PersonalityStore
from .greetings import GreetingService, preferred_address
from .voice_map import map_voice

__all__ = [
    "ADULT_SLIDERS", "CATEGORY_LABELS", "MOODS", "SLIDERS",
    "VOICE_CONTROLS", "PRESETS", "PersonalityStore", "GreetingService",
    "clean_mood", "clean_strength", "clean_traits", "clean_voice",
    "get_preset", "list_presets", "map_voice", "preferred_address",
]
