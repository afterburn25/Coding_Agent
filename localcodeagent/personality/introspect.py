"""Persona introspection + QA — self-description, side-by-side
comparison, similarity detection, and behavior metrics.

- ``describe_persona(card)`` — answers "what are you like?" from the
  *actual resolved effective card*. Never invents traits; if a field
  isn't set it simply isn't described.
- ``compare_personas(...)`` — Personality Studio comparison: run the
  same user prompt through 2–3 personas and return the effective
  summary + voice params + a deterministic preview line each.
- ``persona_similarity(a, b)`` — distance between two behavior
  profiles across the fields that matter for character; flags pairs
  that have collapsed into each other.
- ``behavior_report(dyn_metrics)`` — QA surface over the aggregate
  counters dynamics keeps.
"""
from __future__ import annotations

from typing import Any

from . import behavior as behavior_mod
from .voice_map import map_voice

# ---------------------------------------------------------------------------
# Self-description — reads the resolved card, never invents
# ---------------------------------------------------------------------------

_STYLE_WORDS = {
    "compact": "concise", "measured": "steady", "formal": "polished",
    "conversational": "conversational", "energetic": "upbeat",
    "fragmented": "terse", "narrative": "narrative",
    "analytical": "analytical",
}

_HUMOR_WORDS = {
    "dry": "dry humor", "witty": "quick wit", "warm": "warm humor",
    "playful": "playful humor", "nerdy": "nerdy humor",
    "deadpan": "deadpan humor", "sarcastic": "dry sarcasm",
    "teasing": "teasing", "mischievous": "mischievous humor",
    "goofy": "goofy humor", "absurd": "absurd humor",
    "none": "",
}

_TEACH_WORDS = {
    "concise_expert": "I explain like a concise expert",
    "step_by_step": "I explain step by step",
    "analogy_first": "I explain with analogies first",
    "example_first": "I explain through examples",
    "socratic": "I guide with questions",
    "beginner_friendly": "I explain plainly, without assumed jargon",
    "reference_manual": "I'm precise and thorough",
    "coach": "I coach — explain, encourage, check understanding",
}


def describe_persona(card: dict, personality: dict | None = None) -> str:
    """First-person description of the active persona from the resolved
    effective card. Honest — only fields actually present are claimed."""
    p = personality or {}
    name = str(p.get("name") or card.get("name") or "Nexus")
    bits: list[str] = [
        f"I'm Nexus — {name} is just how I'm carrying myself."]

    rhythm = _STYLE_WORDS.get(str(card.get("rhythm") or ""), "")
    humor = _HUMOR_WORDS.get(str(card.get("humor_type") or "none"), "")
    if rhythm and humor:
        bits.append(f"I'm {rhythm} with {humor}.")
    elif rhythm:
        bits.append(f"I'm {rhythm}.")
    elif humor:
        bits.append(f"I lean on {humor}.")

    teach = _TEACH_WORDS.get(str(card.get("teaching_style") or ""), "")
    if teach:
        bits.append(teach + ".")

    prefs = [str(x) for x in (card.get("stable_prefs") or [])][:3]
    if prefs:
        bits.append("I " + "; I ".join(prefs) + ".")

    q = str(card.get("question_style") or "")
    if q == "minimal":
        bits.append("I rarely ask questions — I'd rather answer.")
    elif q == "investigative":
        bits.append("I ask evidence-seeking questions.")
    elif q == "conversational":
        bits.append("I keep questions light and conversational.")

    mood = str(card.get("mood") or "")
    if mood:
        bits.append(f"Right now I'm in a {mood} register.")
    stage = str(card.get("relationship_stage") or "")
    if stage in ("trusted", "long_term"):
        bits.append("We've built up some shorthand over time.")

    bits.append("However I come across, the facts, code, tools, and "
                "safety stay the same — that's delivery, not "
                "substance.")
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Personality Studio comparison — same prompt through N personas
# ---------------------------------------------------------------------------

def _preview_line(card: dict, sample_text: str) -> str:
    """Deterministic preview — a template rendered per card so the
    comparison shows the *delivery* difference without a model call."""
    name = card.get("name") or "Nexus"
    rhythm = str(card.get("rhythm") or "conversational")
    humor = str(card.get("humor_type") or "none")
    q = str(card.get("question_style") or "clarifying")
    parts = {
        "formal": f"Certainly — here's the direct answer.",
        "compact": "Short version first.",
        "measured": "Let's take this steadily.",
        "energetic": "Ooh, fun one — okay!",
        "conversational": "Sure, let's look at that.",
        "fragmented": "Yep. Here's the deal.",
        "analytical": "Breaking this down:",
        "narrative": "So here's how this goes.",
    }
    opener = parts.get(rhythm, parts["conversational"])
    tail = {"minimal": "", "clarifying": " Want me to go deeper?",
            "conversational": " What do you think?",
            "technical": " Want the underlying mechanism too?",
            "exploratory": " Happy to explore the branches.",
            "investigative": " Want me to check the evidence?",
            "practical": " What's the actual constraint?"}.get(q, "")
    joke = {"dry": " (Said with a straight face.)",
            "playful": " — no pressure. Okay, a little pressure.",
            "sarcastic": " Riveting stuff, honestly.",
            "deadpan": " Thrilling."}.get(humor, "")
    return f"[{name}] {opener}{joke} …{sample_text}…{tail}".strip()


def compare_personas(store, preset_ids: list[str], *,
                     user_text: str = "", is_adult: bool = True,
                     dyn_state: dict | None = None,
                     relationship: dict | None = None) -> list[dict]:
    """Compile the effective card for each requested persona and return
    a side-by-side view: behavior summary, voice params, preview line.
    Accepts preset ids, custom names, or 'active'."""
    from .effective import compile_effective
    out = []
    for pid in list(preset_ids or [])[:3]:
        p = None
        try:
            p = store.resolve(str(pid), is_adult=is_adult)
        except Exception:
            p = None
        if p is None:
            out.append({"persona": str(pid), "error": "not found"})
            continue
        card = compile_effective(
            p, user_text=user_text, relationship=relationship,
            state=dyn_state, is_adult=is_adult)
        voice = map_voice(p.get("voice"), p.get("traits"),
                          strength=int(p.get("strength") or 70),
                          mood=str(card.get("mood") or ""))
        out.append({
            "persona": str(p.get("name") or pid),
            "family": card.get("family"),
            "summary": {"rhythm": card.get("rhythm"),
                        "humor": card.get("humor_type"),
                        "questions": card.get("question_style"),
                        "teaching": card.get("teaching_style"),
                        "energy": card.get("energy"),
                        "seriousness": card.get("seriousness")},
            "voice": {"speed": voice.get("speed"),
                      "pitch_semitones": voice.get("pitch_semitones"),
                      "output_gain_db": voice.get("output_gain_db")},
            "preview": _preview_line(card, user_text or "…"),
        })
    return out


# ---------------------------------------------------------------------------
# Persona similarity — flag profiles that have collapsed into each other
# ---------------------------------------------------------------------------

_NUMERIC_FIELDS = ("warmup_rate", "vocal_bias")
_CATEGORICAL_FIELDS = ("rhythm", "humor_type", "question_style",
                       "teaching_style", "challenge_style",
                       "praise_style", "criticism_style",
                       "decision_style", "plan_style", "turn_taking",
                       "interruption", "baseline_affect", "silence",
                       "curiosity")


def persona_similarity(preset_a: str, preset_b: str,
                       style_a: str = "", style_b: str = "") -> dict:
    """0..1 similarity between two behavior profiles across the
    character-defining fields. ≥0.9 → flag: personas have become
    behaviorally indistinguishable."""
    a = behavior_mod.behavior_for(preset_a, style_a)
    b = behavior_mod.behavior_for(preset_b, style_b)
    total, same = 0.0, 0.0
    for f in _CATEGORICAL_FIELDS:
        total += 1
        if str(a.get(f) or "") == str(b.get(f) or ""):
            same += 1
    for f in _NUMERIC_FIELDS:
        total += 1
        try:
            if abs(float(a.get(f, 0)) - float(b.get(f, 0))) < 0.15:
                same += 1
        except (TypeError, ValueError):
            pass
    sig_a, sig_b = a.get("signature") or {}, b.get("signature") or {}
    for k in set(sig_a) | set(sig_b):
        total += 1
        va, vb = sig_a.get(k), sig_b.get(k)
        try:
            if abs(float(va) - float(vb)) < 0.2:
                same += 1
        except (TypeError, ValueError):
            if va == vb:
                same += 1
    score = round(same / total, 3) if total else 0.0
    return {"a": preset_a, "b": preset_b, "similarity": score,
            "flag": score >= 0.9}


def similarity_audit(preset_ids: list[str] | None = None,
                     styles: dict | None = None) -> list[dict]:
    """Pairwise audit across presets — returns only flagged pairs."""
    from .presets import PRESETS
    ids = preset_ids or [str(k) for k in PRESETS]
    styles = styles or {}
    flagged = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            r = persona_similarity(
                a, b, styles.get(a, ""), styles.get(b, ""))
            if r["flag"]:
                flagged.append(r)
    return flagged


# ---------------------------------------------------------------------------
# QA report over dynamics metrics
# ---------------------------------------------------------------------------

def behavior_report(metrics: dict) -> dict:
    """Readable QA summary over PersonaDynamics.metrics()."""
    m = metrics or {}
    flags: list[str] = []
    if float(m.get("question_rate") or 0) > 1.0:
        flags.append("high question rate — check question_style")
    if float(m.get("name_use_rate") or 0) > 0.5:
        flags.append("name overuse — address frequency too high")
    if float(m.get("humor_rate") or 0) > 0.6:
        flags.append("humor rate high — saturation should dampen")
    if float(m.get("callback_rate") or 0) > 0.3:
        flags.append("callbacks too frequent — check relevance gate")
    return {"metrics": dict(m), "flags": flags}
