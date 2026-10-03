"""Render the active profile's personality + personal memory as prompt
context for the model.

Personality is a presentation layer — the boundary sentence in the
output is the explicit guardrail: delivery style may shift, facts/code/
math/permissions/safety never do. Deterministic, no LLM call.
"""
from __future__ import annotations

from . import schema

_BOUNDARY = ("Personality steers delivery style only — factual accuracy, "
             "code correctness, calculations, tool permissions, and safety "
             "policy are unchanged.")


def _standout_traits(traits: dict | None, strength: int,
                     *, limit: int = 8) -> list[str]:
    """Sliders meaningfully off-center once strength is applied."""
    s = max(0, min(100, int(strength or 0))) / 100.0
    ranked = sorted(
        ((abs(float(v) - 50.0) * s, k, float(v))
         for k, v in (traits or {}).items()
         if k in schema.SLIDERS and isinstance(v, (int, float))),
        reverse=True)
    return [f"{schema.SLIDERS[k].get('label', k)}={int(round(v))}"
            for dev, k, v in ranked if dev >= 12][:limit]


def prompt_context(profile: dict | None, personality: dict | None,
                   memories: list | None = None,
                   *, max_memories: int = 20) -> str:
    """Compact context block; "" when there is no active profile."""
    if not isinstance(profile, dict):
        return ""
    p = personality or {}
    address = ((profile.get("creator_address") or "")
               if profile.get("is_creator")
               else "") or profile.get("first_name") or "the user"
    lines = [f"Active user profile: {address}."]
    name = str(p.get("name") or "")
    strength = schema.clean_strength(p.get("strength"))
    if name:
        lines.append(f"Nexus personality: {name} "
                     f"(strength {strength}/100).")
    standouts = _standout_traits(p.get("traits"), strength)
    if standouts:
        lines.append("Style sliders (0-100, 50 = neutral): "
                     + ", ".join(standouts) + ".")
    mood = str(p.get("mood") or "")
    if mood in schema.MOODS:
        lines.append(f"Current mood: {mood} — temporary, "
                     "the base personality still leads.")
    lines.append(_BOUNDARY)
    mems = [m for m in (memories or [])
            if isinstance(m, dict) and m.get("text")][-max_memories:]
    if mems:
        lines.append("Personal memory (private to this profile):")
        lines += [f"- {m['text']}" for m in mems]
    return "\n".join(lines)
