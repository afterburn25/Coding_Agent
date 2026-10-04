"""Personality → voice mapping.

Translates the 10 UI-level voice controls (plus trait/mood context)
into the parameters the TTS stack actually supports:

- Kokoro:    ``speed`` (0.5–2.0)
- DSP chain: ``pitch_semitones``, ``tempo``, ``output_gain_db``

Controls with no direct engine equivalent (breathiness, pause length,
emphasis, softness texture) become *preprocessing hints* — punctuation/
prosody guidance the presentation layer may apply to text before
synthesis. They are documented, never faked as acoustic params.
"""
from __future__ import annotations

from . import schema

# How strongly each mood nudges delivery (fractions of a 0-100 control).
_MOOD_NUDGE: dict[str, dict[str, float]] = {
    "excited":     {"speed": 0.06, "energy": 0.10, "pitch": 0.10},
    "celebratory": {"speed": 0.04, "energy": 0.12, "pitch": 0.08},
    "concerned":   {"speed": -0.06, "energy": -0.10, "pitch": -0.04},
    "serious":     {"speed": -0.05, "energy": -0.08, "pitch": -0.06},
    "focused":     {"speed": -0.02, "energy": -0.03},
    "relaxed":     {"speed": -0.04, "energy": -0.06, "pitch": -0.02},
    "curious":     {"speed": 0.03, "pitch": 0.05, "energy": 0.05},
}


def _v(voice: dict, key: str) -> float:
    """Voice control 0-100 → centered -1..1 (50 = neutral)."""
    return (schema.clean_voice({key: voice.get(key)}).get(key, 50) - 50) / 50.0


# Max step between adjacent utterances — voice transitions should feel
# smooth, never an audible jump when mood/persona state shifts.
_MAX_STEP = {"speed": 0.08, "pitch_semitones": 0.6,
             "output_gain_db": 1.5}


def _smooth(current: dict, previous: dict | None) -> dict:
    """Clamp per-key deltas against the previous delivery map so
    parameter changes interpolate instead of jumping."""
    if not previous:
        return current
    out = dict(current)
    for key, step in _MAX_STEP.items():
        try:
            prev = float(previous.get(key))
            cur = float(out.get(key))
        except (TypeError, ValueError):
            continue
        if abs(cur - prev) > step:
            out[key] = round(prev + step * (1 if cur > prev else -1), 3)
    try:
        out["tempo"] = out["speed"]
    except (TypeError, ValueError):
        pass
    return out


def map_voice(voice_controls: dict | None = None,
              traits: dict | None = None,
              *, strength: int = schema.DEFAULT_STRENGTH,
              mood: str = "", pitch_bias: float = 0.0,
              previous: dict | None = None) -> dict:
    """Effective voice params + preprocessing hints.

    ``strength`` scales how far delivery moves from neutral: at 0 the
    voice is the plain base voice; at 100 the full mapping applies.
    Returns::

        {"speed": float,          # Kokoro speed + DSP tempo
         "pitch_semitones": float,
         "tempo": float,
         "output_gain_db": float,
         "preprocess": [...],     # documented prosody hints
         "controls": {...}}       # cleaned inputs (for UI/debug)
    """
    vc = schema.clean_voice(voice_controls or {})
    tr = schema.clean_traits(traits or {}, is_adult=True)  # traits only
    #                                                       # modulate style,
    #                                                       # not eligibility
    s = max(0, min(100, int(strength or 0))) / 100.0

    # Trait influence on delivery style (subtle — controls dominate).
    trait_energy = (tr.get("energy", 50) - 50) / 50.0 * 0.5
    trait_drama = (tr.get("dramatic_flair", 50) - 50) / 50.0 * 0.5

    speed_raw = (_v(vc, "speaking_speed") * 0.7
                 + _v(vc, "energy") * 0.2 + trait_energy * 0.1)
    # Soft/breathy voices read lower — that's most of what makes a
    # sultry preset *sound* sultry rather than just slightly slower.
    pitch_raw = (_v(vc, "pitch_variation") * 0.5
                 + _v(vc, "expressiveness") * 0.15
                 + _v(vc, "softness") * -0.15
                 + _v(vc, "breathiness") * -0.2
                 + _v(vc, "vocal_confidence") * 0.15
                 + trait_drama * 0.1)
    gain_raw = (_v(vc, "softness") * -0.6      # soft → quieter
                + _v(vc, "vocal_confidence") * 0.4)

    nudge = _MOOD_NUDGE.get(mood, {})
    speed_raw += nudge.get("speed", 0.0) * 2.0
    pitch_raw += nudge.get("pitch", 0.0) * 2.0
    gain_raw += nudge.get("energy", 0.0) * 0.5

    speed = max(0.5, min(2.0, 1.0 + speed_raw * 0.45 * s))
    # pitch_bias is a per-preset semitone floor (e.g. sultry=-1.5) so
    # styles stay audibly distinct even without slider edits.
    semis = max(-4.0, min(4.0, pitch_raw * 3.0 * s + pitch_bias * s))
    gain = max(-6.0, min(3.0, gain_raw * 5.0 * s))

    hints: list[str] = []
    if _v(vc, "pause_length") > 0.2:
        hints.append("pause_length: prefer longer punctuation/ellipsis "
                     "between clauses")
    elif _v(vc, "pause_length") < -0.2:
        hints.append("pause_length: tighten clause breaks")
    if _v(vc, "emphasis") > 0.2:
        hints.append("emphasis: stress key words via formatting")
    if _v(vc, "breathiness") > 0.2:
        hints.append("breathiness: softer phrasing, no acoustic "
                     "equivalent (documented limitation)")
    if _v(vc, "emotional_intensity") > 0.3:
        hints.append("emotional_intensity: heighten expressive word "
                     "choice")

    return _smooth({
        "speed": round(speed, 3),
        "pitch_semitones": round(semis, 2),
        "tempo": round(speed, 3),
        "output_gain_db": round(gain, 2),
        "preprocess": hints,
        "controls": vc,
    }, previous)
