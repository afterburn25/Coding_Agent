"""Custom-persona intelligence: coherence scoring, preset blending,
and portable import/export.

- ``coherence_check(traits, voice)`` — detects contradictory settings
  (max formality + max slang, minimalist + max storytelling, …) and
  returns a coarse score: high / mixed / low. Never blocks a custom —
  it informs, doesn't gate.
- ``blend_presets(specs)`` — weighted merge of built-in presets into a
  merged trait/voice/behavior payload for ``create_custom``. Built-ins
  are never mutated.
- ``export_custom`` / ``import_custom`` — portable persona packages.
  Personal memory, relationship state, and dynamics never export.
"""
from __future__ import annotations

from typing import Any

from ..profiles.model import ProfileError
from . import schema
from .presets import BEHAVIOR_VERSION, get_preset

EXPORT_FORMAT = "nexus-persona"
EXPORT_VERSION = 1

# ---------------------------------------------------------------------------
# Coherence — declarative conflict rules between whitelisted sliders.
# Each rule: (a_key, a_pred, b_key, b_pred, human-readable note)
# ---------------------------------------------------------------------------

def _hi(v: float) -> bool:
    return v >= 75


def _lo(v: float) -> bool:
    return v <= 25


_CONFLICTS = [
    ("formality", _hi, "slang_usage", _hi,
     "Formality and slang usage are both very high"),
    ("calmness", _hi, "dramatic_flair", _hi,
     "Calmness and dramatic flair pull opposite directions"),
    ("calmness", _hi, "energy", _hi,
     "Calm and hyperactive energy conflict"),
    ("verbosity", _lo, "storytelling", _hi,
     "Minimal verbosity conflicts with heavy storytelling"),
    ("emotional_expressiveness", _lo, "dramatic_flair", _hi,
     "Low emotional expression conflicts with high dramatic flair"),
    ("warmth", _hi, "rudeness", _hi,
     "Warmth and rudeness fight each other"),
    ("verbosity", _lo, "explanation_depth", _hi,
     "Short answers conflict with deep explanations"),
    ("humor", _lo, "silliness", _hi,
     "No humor conflicts with high silliness"),
    ("humor", _lo, "sarcasm", _hi,
     "No humor conflicts with heavy sarcasm"),
    ("patience", _lo, "teaching_tendency", _hi,
     "Low patience conflicts with a strong teaching style"),
    ("directness", _hi, "empathy", _hi,
     "Maximum bluntness and maximum empathy can read inconsistently"),
    ("playfulness", _hi, "formality", _hi,
     "High playfulness and high formality clash"),
]


def coherence_check(traits: dict | None,
                    voice: dict | None = None) -> dict:
    """Score a custom persona's settings. Coarse on purpose —
    'high'/'mixed'/'low' avoids fake precision."""
    tr = schema.clean_traits(traits or {}, is_adult=True)
    notes: list[str] = []
    for a, ap, b, bp, note in _CONFLICTS:
        va, vb = tr.get(a), tr.get(b)
        if va is None or vb is None:
            continue
        try:
            if ap(float(va)) and bp(float(vb)):
                notes.append(note)
        except (TypeError, ValueError):
            continue
    if not notes:
        score = "high"
    elif len(notes) <= 2:
        score = "mixed"
    else:
        score = "low"
    return {"coherence": score, "conflicts": notes}


# ---------------------------------------------------------------------------
# Blending — weighted merge of built-in presets. Numeric fields
# (traits, voice controls, pitch_bias) merge weighted; categorical
# fields (greeting_style → behavior family, address) follow the
# heaviest contributor.
# ---------------------------------------------------------------------------

def blend_presets(specs: list[dict] | list[tuple]) -> dict:
    """``[{"preset": "professional", "weight": 0.7}, ...]`` → merged
    persona payload for create_custom. Raises ProfileError when no
    spec resolves."""
    parts: list[tuple[dict, float]] = []
    for spec in specs or []:
        if isinstance(spec, dict):
            pid, w = spec.get("preset") or spec.get("id"), spec.get(
                "weight", 1.0)
        else:
            pid, w = spec[0], (spec[1] if len(spec) > 1 else 1.0)
        p = get_preset(str(pid or ""))
        if p is None:
            raise ProfileError(f"no such preset: {pid}")
        try:
            w = max(0.0, float(w))
        except (TypeError, ValueError):
            w = 0.0
        if w > 0:
            parts.append((p, w))
    if not parts:
        raise ProfileError("blend needs at least one weighted preset")
    total = sum(w for _p, w in parts)

    def _merge_numeric(key: str) -> dict:
        keys: set[str] = set()
        for p, _w in parts:
            keys |= set((p.get(key) or {}).keys())
        out = {}
        for k in keys:
            acc = sum(float((p.get(key) or {}).get(k, 50)) * w
                      for p, w in parts)
            out[k] = int(round(acc / total))
        return out

    winner = max(parts, key=lambda pw: pw[1])[0]
    traits = _merge_numeric("traits")
    voice = _merge_numeric("voice")
    pitch = sum(float(p.get("pitch_bias") or 0.0) * w
                for p, w in parts) / total
    adult_only = any(p.get("adult_only") for p, _w in parts)
    return {
        "name": " + ".join(p["name"] for p, _w in parts),
        "base_preset": winner["id"],
        "greeting_style": winner.get("greeting_style") or "default",
        "address": str(winner.get("address") or ""),
        "pitch_bias": round(pitch, 2),
        "traits": traits,
        "voice": voice,
        "adult_only": adult_only,
        "blend": [{"preset": p["id"], "weight": round(w / total, 3)}
                  for p, w in parts],
        "behavior_version": BEHAVIOR_VERSION,
    }


def create_blend(store: Any, specs: list, *, name: str | None = None,
                 is_adult: bool) -> dict:
    """Blend presets → a stored custom persona. Built-ins are read
    only; the blend lands as a new custom with provenance metadata."""
    merged = blend_presets(specs)
    if merged.get("adult_only") and not is_adult:
        raise ProfileError("adult personality requires 18+")
    custom = store.create_custom(
        is_adult=is_adult,
        name=name or merged["name"],
        base_preset=merged["base_preset"],
        traits=merged["traits"], voice=merged["voice"])
    st = store._load()
    c = store._custom(st, str(custom.get("personality_id") or ""))
    if c is not None:
        c["blend"] = merged["blend"]
        c["behavior_version"] = merged["behavior_version"]
        store._save(st)
        return store._public_custom(c)
    return custom


# ---------------------------------------------------------------------------
# Import / export — persona packages carry presentation only.
# ---------------------------------------------------------------------------

def export_custom(store: Any, personality_id: str) -> dict:
    """Serialize a stored custom persona to a portable package."""
    st = store._load()
    c = store._custom(st, str(personality_id or ""))
    if c is None:
        raise ProfileError("no such custom personality")
    return {
        "format": EXPORT_FORMAT,
        "export_version": EXPORT_VERSION,
        "behavior_version": int(
            c.get("behavior_version") or BEHAVIOR_VERSION),
        "name": str(c.get("name") or "Custom"),
        "base_preset": str(c.get("base_preset") or ""),
        "traits": dict(c.get("traits") or {}),
        "voice": dict(c.get("voice") or {}),
        "blend": list(c.get("blend") or []),
    }


def import_custom(store: Any, package: dict, *,
                  is_adult: bool) -> dict:
    """Import a persona package → creates a custom. Unknown/missing
    fields get defaults; memory and dynamics never come along."""
    if not isinstance(package, dict):
        raise ProfileError("invalid persona package")
    if str(package.get("format") or "") != EXPORT_FORMAT:
        raise ProfileError("not a nexus-persona package")
    name = str(package.get("name") or "Imported persona")
    base = str(package.get("base_preset") or "")
    traits = package.get("traits") if isinstance(
        package.get("traits"), dict) else {}
    voice = package.get("voice") if isinstance(
        package.get("voice"), dict) else {}
    custom = store.create_custom(
        is_adult=is_adult, name=name,
        base_preset=base or None, traits=traits, voice=voice)
    # Record provenance + blend + version on the stored custom.
    st = store._load()
    c = store._custom(st, str(custom.get("personality_id") or ""))
    if c is not None:
        c["blend"] = [b for b in (package.get("blend") or [])
                      if isinstance(b, dict)]
        c["behavior_version"] = int(
            package.get("behavior_version") or BEHAVIOR_VERSION)
        c["imported"] = True
        store._save(st)
        out = store._public_custom(c)
        return out
    return custom
