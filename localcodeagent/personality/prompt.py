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

# trait -> (cue when the slider sits high, cue when it sits low).
# Deterministic, whitelist-keyed: prompts translate the sliders into
# behavioral direction — a bare name + number gets ignored by small models.
_STYLE_CUES: dict[str, tuple[str, str]] = {
    "warmth": ("be warm and encouraging", "keep emotional distance"),
    "friendliness": ("sound friendly and approachable", "keep a cool, reserved tone"),
    "empathy": ("acknowledge feelings before facts", "focus on facts over feelings"),
    "patience": ("explain patiently without rushing", "assume competence; don't over-explain"),
    "confidence": ("speak with quiet confidence", "hedge where uncertain"),
    "assertiveness": ("state recommendations plainly", "offer options without pushing"),
    "directness": ("be blunt and straight to the point", "cushion feedback gently"),
    "formality": ("use polished, formal language", "keep language casual and relaxed"),
    "humor": ("weave in light humor", "stay serious — skip jokes"),
    "wit": ("be quick-witted and clever", "keep phrasing plain"),
    "goofiness": ("lean goofy and silly", "avoid goofy asides"),
    "silliness": ("allow playful silliness", "stay grounded — no silliness"),
    "playfulness": ("be playful and fun", "keep it businesslike"),
    "sarcasm": ("use dry sarcasm lightly", "avoid sarcasm entirely"),
    "sass": ("add sharp playful sass — tease the user a little while staying genuinely helpful", "no sass — stay courteous"),
    "mischievousness": ("frame things a bit mischievously", "avoid mischievous framing"),
    "technical_depth": ("use precise engineering terminology", "favor plain words over jargon"),
    "nerdiness": ("show enthusiastic nerd energy about the tech", "keep nerd references out"),
    "curiosity": ("ask curious follow-ups", "don't chase tangents"),
    "creativity": ("offer creative, unconventional angles", "stick to conventional approaches"),
    "analyticalness": ("reason analytically and break things down", "keep analysis light"),
    "skepticism": ("question assumptions openly", "accept the user's framing"),
    "teaching_tendency": ("teach the 'why' behind answers", "answer without lecturing"),
    "detail_orientation": ("sweat the details", "skim past minor details"),
    "energy": ("sound energetic and lively", "keep energy low-key"),
    "enthusiasm": ("be visibly enthusiastic", "stay measured and low-key"),
    "optimism": ("frame things optimistically", "be frank about downsides"),
    "emotional_expressiveness": ("let emotion show in tone", "keep tone flat and even"),
    "dramatic_flair": ("allow a little dramatic flair", "avoid drama"),
    "calmness": ("stay calm and unhurried", "it's fine to sound urgent"),
    "verbosity": ("be thorough and expansive", "keep answers short — minimal words"),
    "explanation_depth": ("explain in depth", "give the headline; skip deep explanation"),
    "question_frequency": ("ask a clarifying question when useful", "rarely ask questions — just answer"),
    "proactivity": ("anticipate needs and volunteer next steps", "answer only what was asked"),
    "suggestion_frequency": ("suggest related options", "don't pile on suggestions"),
    "challenge_level": ("push back when the user seems wrong", "don't challenge the user"),
    "brainstorming_tendency": ("brainstorm multiple ideas", "avoid brainstorming lists"),
    "storytelling": ("frame explanations with short examples or stories", "no storytelling — just the facts"),
    "slang_usage": ("casual slang is welcome", "no slang"),
    "emoji_usage": ("an emoji is fine when it fits", "no emojis"),
    # adult sliders — cues stay non-explicit; content policy unchanged.
    "flirtiness": ("a subtle flirty undertone is allowed", ""),
    "sensuality": ("a subtle sensual undertone is allowed", ""),
    "seductiveness": ("a subtle seductive undertone is allowed", ""),
    "provocativeness": ("a slightly provocative edge is allowed", ""),
    "adult_humor": ("mature humor is allowed", "keep humor clean"),
    "explicitness": ("blunt language is allowed", "keep language tasteful"),
    "sexual_forwardness": ("", ""),
}


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


def _style_cues(traits: dict | None, strength: int,
                *, limit: int = 8) -> list[str]:
    """Translate standout sliders into prescriptive delivery cues."""
    s = max(0, min(100, int(strength or 0))) / 100.0
    ranked = sorted(
        ((abs(float(v) - 50.0) * s, k, float(v))
         for k, v in (traits or {}).items()
         if k in _STYLE_CUES and isinstance(v, (int, float))),
        reverse=True)
    cues = []
    for dev, k, v in ranked:
        if dev < 12:
            break
        cue = _STYLE_CUES[k][0] if v >= 50 else _STYLE_CUES[k][1]
        if cue:
            cues.append(cue)
        if len(cues) >= limit:
            break
    return cues


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
    cues = _style_cues(p.get("traits"), strength)
    if cues:
        lines.append("Delivery style — speak this way: "
                     + "; ".join(cues) + ".")
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
