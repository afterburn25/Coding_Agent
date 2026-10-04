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
from .behavior import behavior_for_personality, guidance_lines
from .dynamics import PersonaDynamics
from .effective import compile_effective, card_guidance, debug_view
from .seriousness import (SERIOUSNESS, classify_seriousness,
                          classify_topic)

__all__ = [
    "ADULT_SLIDERS", "CATEGORY_LABELS", "MOODS", "SERIOUSNESS",
    "SLIDERS", "VOICE_CONTROLS", "PRESETS", "PersonaDynamics",
    "PersonalityStore", "GreetingService",
    "behavior_for_personality", "card_guidance",
    "classify_seriousness", "classify_topic", "clean_mood",
    "clean_strength", "clean_traits", "clean_voice",
    "compile_effective", "debug_view", "get_preset", "guidance_lines",
    "list_presets", "map_voice", "preferred_address",
]
