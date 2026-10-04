"""Personality schema — slider whitelist, voice controls, moods.

The whitelist is the security boundary: arbitrary personality JSON can
only ever touch these keys. Personality influences presentation only —
never factual results, code correctness, tool permissions, or
safety/authorization state.
"""
from __future__ import annotations

# --- sliders (0-100), by category -----------------------------------------
# Adult-only sliders are a separate whitelist: they are silently stripped
# for under-18 profiles rather than clamped, so a crafted payload can
# never smuggle adult behavior into a minor's personality.

SLIDERS: dict[str, dict] = {
    # Core social
    "warmth":               {"label": "Warmth",               "category": "core_social"},
    "friendliness":         {"label": "Friendliness",         "category": "core_social"},
    "empathy":              {"label": "Empathy",              "category": "core_social"},
    "patience":             {"label": "Patience",             "category": "core_social"},
    "confidence":           {"label": "Confidence",           "category": "core_social"},
    "assertiveness":        {"label": "Assertiveness",        "category": "core_social"},
    "directness":           {"label": "Directness",           "category": "core_social"},
    "formality":            {"label": "Formality",            "category": "core_social"},
    "rudeness":             {"label": "Rudeness",             "category": "core_social"},
    # Fun
    "humor":                {"label": "Humor",                "category": "fun"},
    "wit":                  {"label": "Wit",                  "category": "fun"},
    "goofiness":            {"label": "Goofiness",            "category": "fun"},
    "silliness":            {"label": "Silliness",            "category": "fun"},
    "playfulness":          {"label": "Playfulness",          "category": "fun"},
    "sarcasm":              {"label": "Sarcasm",              "category": "fun"},
    "sass":                 {"label": "Sass",                 "category": "fun"},
    "mischievousness":      {"label": "Mischievousness",      "category": "fun"},
    # Intellect
    "technical_depth":      {"label": "Technical Depth",      "category": "intellect"},
    "nerdiness":            {"label": "Nerdiness",            "category": "intellect"},
    "curiosity":            {"label": "Curiosity",            "category": "intellect"},
    "creativity":           {"label": "Creativity",           "category": "intellect"},
    "analyticalness":       {"label": "Analyticalness",       "category": "intellect"},
    "skepticism":           {"label": "Skepticism",           "category": "intellect"},
    "teaching_tendency":    {"label": "Teaching Tendency",    "category": "intellect"},
    "detail_orientation":   {"label": "Detail Orientation",   "category": "intellect"},
    # Energy / emotion
    "energy":               {"label": "Energy",               "category": "energy_emotion"},
    "enthusiasm":           {"label": "Enthusiasm",           "category": "energy_emotion"},
    "optimism":             {"label": "Optimism",             "category": "energy_emotion"},
    "emotional_expressiveness": {"label": "Emotional Expressiveness",
                             "category": "energy_emotion"},
    "dramatic_flair":       {"label": "Dramatic Flair",       "category": "energy_emotion"},
    "calmness":             {"label": "Calmness",             "category": "energy_emotion"},
    # Response style
    "verbosity":            {"label": "Verbosity",            "category": "response_style"},
    "explanation_depth":    {"label": "Explanation Depth",    "category": "response_style"},
    "question_frequency":   {"label": "Question Frequency",   "category": "response_style"},
    "proactivity":          {"label": "Proactivity",          "category": "response_style"},
    "suggestion_frequency": {"label": "Suggestion Frequency", "category": "response_style"},
    "challenge_level":      {"label": "Challenge Level",      "category": "response_style"},
    "brainstorming_tendency": {"label": "Brainstorming Tendency",
                             "category": "response_style"},
    "storytelling":         {"label": "Storytelling",         "category": "response_style"},
    "slang_usage":          {"label": "Slang Usage",          "category": "response_style"},
    "emoji_usage":          {"label": "Emoji Usage",          "category": "response_style"},
    # Adult-only (is_adult required)
    "flirtiness":           {"label": "Flirtiness",           "category": "adult",
                             "adult_only": True},
    "sensuality":           {"label": "Sensuality",           "category": "adult",
                             "adult_only": True},
    "seductiveness":        {"label": "Seductiveness",        "category": "adult",
                             "adult_only": True},
    "provocativeness":      {"label": "Provocativeness",      "category": "adult",
                             "adult_only": True},
    "adult_humor":          {"label": "Adult Humor",          "category": "adult",
                             "adult_only": True},
    "explicitness":         {"label": "Explicitness",         "category": "adult",
                             "adult_only": True},
    "sexual_forwardness":   {"label": "Sexual Forwardness",   "category": "adult",
                             "adult_only": True},
}

ADULT_SLIDERS = frozenset(
    k for k, v in SLIDERS.items() if v.get("adult_only"))

CATEGORY_ORDER = ["core_social", "fun", "intellect", "energy_emotion",
                  "response_style", "adult"]
CATEGORY_LABELS = {
    "core_social": "Core Social",
    "fun": "Fun",
    "intellect": "Intellect",
    "energy_emotion": "Energy & Emotion",
    "response_style": "Response Style",
    "adult": "Adult-Only",
}

# --- voice-performance controls (0-100) -----------------------------------
# UI-level prosody knobs. voice_map translates them into the params the
# TTS/DSP stack actually supports; unsupported acoustics become
# documented text/prosody preprocessing hints, never faked.

VOICE_CONTROLS: dict[str, dict] = {
    "speaking_speed":      {"label": "Speaking Speed"},
    "energy":              {"label": "Energy"},
    "expressiveness":      {"label": "Expressiveness"},
    "pitch_variation":     {"label": "Pitch Variation"},
    "pause_length":        {"label": "Pause Length"},
    "softness":            {"label": "Softness"},
    "breathiness":         {"label": "Breathiness"},
    "emphasis":            {"label": "Emphasis"},
    "vocal_confidence":    {"label": "Vocal Confidence"},
    "emotional_intensity": {"label": "Emotional Intensity"},
}

# --- moods (temporary context; never overrides profile settings) -----------

MOODS = ("focused", "relaxed", "excited", "concerned",
         "celebratory", "curious", "serious")

# Natural vocalization levels — how freely the voice layer may voice
# non-verbal reactions (hums, sighs, chuckles). Profile-scoped like
# strength/mood; the VocalizationEngine consumes this.
VOCAL_LEVELS = ("off", "minimal", "natural", "expressive")

DEFAULT_TRAIT = 50
DEFAULT_STRENGTH = 70


def clean_traits(raw: object, *, is_adult: bool) -> dict[str, int]:
    """Whitelist+clamp a traits dict. Returns only supported sliders;
    adult sliders require is_adult. Non-int coercible values clamp 0-100."""
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        spec = SLIDERS.get(str(key))
        if spec is None:
            continue
        if spec.get("adult_only") and not is_adult:
            continue                       # stripped, not clamped
        try:
            out[str(key)] = max(0, min(100, int(val)))
        except (TypeError, ValueError):
            continue
    return out


def clean_voice(raw: object) -> dict[str, int]:
    """Whitelist+clamp voice controls."""
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        if str(key) not in VOICE_CONTROLS:
            continue
        try:
            out[str(key)] = max(0, min(100, int(val)))
        except (TypeError, ValueError):
            continue
    return out


def clean_strength(raw: object) -> int:
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_STRENGTH


def clean_mood(raw: object) -> str:
    m = str(raw or "").strip().lower()
    return m if m in MOODS else ""


def clean_vocal_level(raw: object) -> str:
    v = str(raw or "").strip().lower()
    return v if v in VOCAL_LEVELS else "natural"
