"""Effective Persona compiler — the single place where every persona
layer merges into one compact, deterministic instruction card.

Inputs (all optional, all cheap — no model calls):

    base personality    (preset or custom, from PersonalityStore)
    + slider overrides  (learned per-profile overlay, trait offsets)
    + behavior mapping  (behavior.py — motivations, habits, styles)
    + learned overlay   (per-profile notes like "less teasing")
    + temp modifiers    (expiring slider/context deltas)
    + current mood      (dynamics engine; manual mood wins while held)
    + relationship      (familiarity stage, shorthand, teasing comfort)
    + task/mode overlay (coding_mode, serious_mode, ...)
    + seriousness       (casual..critical, from the user message)
    + topic             (domain-specific tone shifts)

Output is a plain dict card — small enough to render into the prompt as
~15 short lines and to expose on a debug endpoint. Facts, code, tool
arguments, permissions, and safety are never in this card; it describes
*delivery* only.
"""
from __future__ import annotations

import time
from typing import Any

from . import behavior as behavior_mod
from . import schema
from .seriousness import (EXPRESSION_SCALE, SERIOUSNESS_GUIDANCE,
                          classify_seriousness, classify_topic)

RELATIONSHIP_STAGES = ("new", "familiar", "trusted", "long_term")
_STAGE_MIN = {"new": 0.0, "familiar": 15.0, "trusted": 45.0,
              "long_term": 80.0}

# Mode overlays — task/context tones layered on top of the persona.
# Each entry nudges compiled fields; the base persona still leads.
MODE_OVERLAYS: dict[str, dict[str, Any]] = {
    "coding_mode":      {"expression": 0.6, "humor_cap": "dry",
                         "guidance": "Mode: coding — precision over "
                                    "flourish; humor minimal."},
    "debugging_mode":   {"expression": 0.45, "humor_cap": "dry",
                         "guidance": "Mode: debugging — methodical, "
                                    "evidence-first, minimal color."},
    "teaching_mode":    {"expression": 0.9,
                         "guidance": "Mode: teaching — favor the "
                                    "teaching style; check "
                                    "understanding gently."},
    "brainstorming_mode": {"expression": 1.0,
                           "guidance": "Mode: brainstorming — more "
                                       "exploratory, more ideas."},
    "serious_mode":     {"expression": 0.25, "humor_cap": "none",
                         "guidance": "Mode: serious — persona color "
                                    "muted; direct and steady."},
    "casual_mode":      {"expression": 1.0,
                         "guidance": "Mode: casual — full persona "
                                     "color."},
    "reviewer_mode":    {"expression": 0.5,
                         "guidance": "Mode: review — findings and "
                                     "evidence first, style second."},
    "research_mode":    {"expression": 0.7,
                         "guidance": "Mode: research — cite "
                                     "confidence levels explicitly."},
}
MODES = tuple(MODE_OVERLAYS)

# Humor types ordered roughly by intensity for seriousness capping.
_HUMOR_RANK = {"none": 0, "deadpan": 1, "dry": 1, "witty": 2, "nerdy": 2,
               "warm": 2, "playful": 3, "teasing": 3, "sarcastic": 3,
               "mischievous": 3, "goofy": 4, "absurd": 4}


def clean_mode(raw: Any) -> str:
    v = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in MODE_OVERLAYS else ""


def _stage_for(familiarity: float) -> str:
    stage = "new"
    for s, floor in _STAGE_MIN.items():
        if familiarity >= floor:
            stage = s
    return stage


def _apply_trait_overlay(traits: dict, overlay: dict | None) -> dict:
    """Learned overlay = per-trait additive offsets (-100..100) stored
    per profile. Never mutates built-ins — works on a copy."""
    merged = dict(traits or {})
    for k, delta in ((overlay or {}).get("trait_offsets") or {}).items():
        if k not in schema.SLIDERS:
            continue
        try:
            merged[k] = max(0, min(100,
                                   int(round(float(merged.get(k, 50))
                                             + float(delta)))))
        except (TypeError, ValueError):
            continue
    return merged


def _apply_modifiers(traits: dict, modifiers: list | None,
                     now: float | None = None) -> tuple[dict, list]:
    """Temporary modifiers: {trait_offsets, note, expires_at, scope}.
    Expired ones are dropped (and reported so the store can prune)."""
    merged = dict(traits or {})
    active_notes: list[str] = []
    now = time.time() if now is None else now
    for mod in modifiers or []:
        try:
            exp = float(mod.get("expires_at") or 0)
        except (TypeError, ValueError):
            exp = 0
        if exp and exp <= now:
            continue
        for k, delta in (mod.get("trait_offsets") or {}).items():
            if k not in schema.SLIDERS:
                continue
            try:
                merged[k] = max(0, min(100, int(round(
                    float(merged.get(k, 50)) + float(delta)))))
            except (TypeError, ValueError):
                continue
        note = str(mod.get("note") or "").strip()
        if note:
            active_notes.append(note)
    return merged, active_notes


def compile_effective(personality: dict, *, user_text: str = "",
                      relationship: dict | None = None,
                      mood: str = "",
                      overlay: dict | None = None,
                      modifiers: list | None = None,
                      mode: str = "",
                      seriousness: str = "",
                      topic: str = "",
                      is_adult: bool = True,
                      strength: int | None = None,
                      state: dict | None = None,
                      callback: dict | None = None) -> dict:
    """Merge all layers → effective persona card (plain dict)."""
    p = personality or {}
    strength = schema.clean_strength(
        strength if strength is not None else p.get("strength"))
    s = max(0.0, min(1.0, strength / 100.0))

    base_traits = schema.clean_traits(p.get("traits"), is_adult=is_adult)
    traits = _apply_trait_overlay(base_traits, overlay)
    traits, mod_notes = _apply_modifiers(traits, modifiers)

    seriousness = seriousness or classify_seriousness(user_text)
    if seriousness not in EXPRESSION_SCALE:
        seriousness = "neutral"
    topic = topic or classify_topic(user_text)
    mode = clean_mode(mode)

    # Behavioral identity — family profile + preset overrides.
    beh = behavior_mod.behavior_for_personality(p)

    rel = relationship or {}
    familiarity = float(rel.get("familiarity") or 0.0)
    stage = str(rel.get("stage") or _stage_for(familiarity))
    if stage not in RELATIONSHIP_STAGES:
        stage = _stage_for(familiarity)

    # Expression scale: seriousness dominates; mode overlay modulates;
    # personality strength scales persona color from neutral.
    scale = EXPRESSION_SCALE[seriousness]
    overlay_mode = MODE_OVERLAYS.get(mode)
    if overlay_mode:
        scale = min(scale, float(overlay_mode.get("expression", 1.0)))

    humor = str(beh.get("humor_type") or "none")
    if overlay_mode and overlay_mode.get("humor_cap"):
        cap = _HUMOR_RANK.get(str(overlay_mode["humor_cap"]), 0)
        if _HUMOR_RANK.get(humor, 0) > cap:
            humor = str(overlay_mode["humor_cap"])
    if seriousness in ("serious", "critical"):
        humor = "none"
    elif seriousness == "focused" and _HUMOR_RANK.get(humor, 0) >= 3:
        humor = "dry"

    mood = str(mood or p.get("mood") or "")
    if mood not in schema.MOODS:
        mood = ""

    # Social layer — cue classification is deterministic and cheap;
    # sarcasm *understanding* is independent of the persona's humor.
    st = state or {}
    social_cue, sarcasm = "", False
    user_e, energy, pacing = "medium", "medium", 1.0
    sat = {"humor": 0.0, "sarcasm": 0.0, "vocal": 0.0,
           "gesture": 0.0, "intensity": 0.0}
    focus, focus_shifted = "", False
    addr_hint, humor_adapt, pref_hint, recovery = "", "", "", ""
    recent_pats: list = []
    recent_humor_pats: list = []
    try:
        from . import continuity as _cont
        from . import social as _soc
        cue = _soc.classify_social(
            user_text,
            context_failed=str(st.get("last_outcome") or "")
            == "failed")
        social_cue = str(cue.get("cue") or "")
        sarcasm = bool(cue.get("sarcasm"))
        user_e = _soc.user_energy(user_text, social_cue)
        turns = int(st.get("turns") or 0)
        energy = _soc.effective_energy(
            beh, user_energy=user_e, mood=mood, turns=turns,
            seriousness=seriousness)
        pacing = _soc.pacing_factor(turns, social_cue)
        sat = _cont.saturation_level(st)
        foc = st.get("focus") or {}
        focus = str(foc.get("topic") or "")
        focus_shifted = bool(foc.get("prev")) and bool(focus)
        # Saturation dampening — heavy recent humor/sarcasm dials the
        # humor register down one step without changing identity.
        if _cont.saturated(st, "sarcasm") and humor == "sarcastic":
            humor = "dry"
        elif _cont.saturated(st, "humor") and \
                _HUMOR_RANK.get(humor, 0) >= 3:
            humor = "dry"
        damp = max(sat.get("humor", 0.0), sat.get("intensity", 0.0))
        scale *= (1.0 - 0.35 * damp)
        addr_hint = _cont.address_hint(
            st, str(beh.get("family") or "default"), familiarity)
        humor_adapt = _cont.humor_adaptation(st)
        _pats = st.get("patterns") or {}
        recent_pats = _pats.get("analogy") or []
        recent_humor_pats = _pats.get("humor") or []
        recovery = _soc.recovery_cue(str(beh.get("family")
                                       or "default"))
        if user_text:
            hint = _cont.preference_consistency(st, user_text)
            if hint:
                pref_hint = hint
    except Exception:
        pass

    topic_cue = (beh.get("topic_shift") or {}).get(topic, "")

    return {
        "name": str(p.get("name") or ""),
        "base_preset": str(p.get("base_preset") or ""),
        "family": str(beh.get("family") or "default"),
        "strength": strength,
        "is_custom": bool(p.get("is_custom")),
        "traits": traits,
        "motivations": list(beh.get("motivations") or []),
        "aversions": list(beh.get("aversions") or []),
        "rhythm": str(beh.get("rhythm") or "conversational"),
        "signature": dict(beh.get("signature") or {}),
        "humor_type": humor,
        "question_style": str(beh.get("question_style") or "clarifying"),
        "teaching_style": str(beh.get("teaching_style")
                              or "example_first"),
        "challenge_style": str(beh.get("challenge_style")
                               or "diplomatic"),
        "praise_style": str(beh.get("praise_style") or "acknowledge"),
        "criticism_style": str(beh.get("criticism_style")
                               or "constructive"),
        "decision_style": str(beh.get("decision_style") or "practical"),
        "plan_style": str(beh.get("plan_style") or "next_actions"),
        "confidence": dict(beh.get("confidence") or {}),
        "error_admission": str(beh.get("error_admission") or ""),
        "tool_failure": str(beh.get("tool_failure") or ""),
        "turn_taking": str(beh.get("turn_taking") or "conversational"),
        "interruption": str(beh.get("interruption") or "acknowledges"),
        "vocal_prefer": list(beh.get("vocal_prefer") or []),
        "vocal_bias": float(beh.get("vocal_bias") or 1.0),
        "gesture_prefer": list(beh.get("gesture_prefer") or []),
        "warmup_rate": float(beh.get("warmup_rate") or 1.0),
        "baseline_affect": str(beh.get("baseline_affect") or "relaxed"),
        "silence": str(beh.get("silence") or ""),
        "curiosity": str(beh.get("curiosity") or "medium"),
        "noticing": str(beh.get("noticing") or ""),
        "stable_prefs": list(beh.get("stable_prefs") or []),
        "topic": topic,
        "topic_cue": str(topic_cue or ""),
        "seriousness": seriousness,
        "social_cue": social_cue,
        "sarcasm_detected": sarcasm,
        "user_energy": user_e,
        "energy": energy,
        "pacing": round(pacing, 2),
        "saturation": sat,
        "focus": focus,
        "focus_shifted": focus_shifted,
        "callback_event": str(
            (callback or {}).get("event") or "")[:120],
        "address_hint": addr_hint,
        "humor_adaptation": humor_adapt,
        "recent_analogies": list(recent_pats[-3:]),
        "recent_humor_patterns": list(recent_humor_pats[-3:]),
        "preference_hint": pref_hint,
        "recovery_cue": recovery,
        "confidence_delivery": ("steady clear delivery when verified; "
                                "measured, honestly-flagged delivery "
                                "when uncertain — hesitation only on "
                                "genuine uncertainty"),
        "expression_scale": round(scale * s, 2),
        "mode": mode,
        "mood": mood,
        "familiarity": round(familiarity, 1),
        "relationship_stage": stage,
        "overlay_notes": [str(n) for n in
                          (overlay or {}).get("notes") or []][:4],
        "modifier_notes": mod_notes[:4],
        "vocalizations": str(p.get("vocalizations") or "natural"),
    }


_CUE_GUIDANCE = {
    "frustration": "User sounds frustrated — lead with the fix; keep "
                   "color minimal.",
    "venting": "User is venting — brief acknowledgment, then the "
               "practical path.",
    "confusion": "User seems confused — simplify; one step at a time.",
    "excitement": "User is excited — matching some brightness is fine.",
    "celebration": "User is celebrating — share the win briefly, then "
                   "continue.",
    "disappointment": "User sounds let down — acknowledge, stay "
                      "constructive.",
    "uncertainty": "User seems unsure — offer a clear path; don't "
                   "over-commit.",
    "joking": "User is joking — a light in-character beat is fine, "
              "then answer.",
    "serious": "User wants it straight — direct answer, no flourishes.",
    "casual": "Casual register — keep it easy.",
}


def card_guidance(card: dict, *, max_lines: int = 18) -> list[str]:
    """Render the effective card as compact prompt lines — the 'Style:
    warm, confident, familiar'-style card from the spec, plus the
    behavior profile's style guidance."""
    lines: list[str] = []
    style_bits = [card.get("rhythm") or "conversational"]
    if card.get("humor_type") and card["humor_type"] != "none":
        style_bits.append(f"{card['humor_type']} humor")
    if card.get("familiarity", 0) >= 15:
        style_bits.append(str(card.get("relationship_stage")
                              or "familiar"))
    lines.append("Effective persona — style: " + ", ".join(style_bits)
                 + f" (strength {card.get('strength', 70)}/100).")

    g = SERIOUSNESS_GUIDANCE.get(str(card.get("seriousness")), "")
    if g:
        lines.append(g)
    if card.get("mode") and card["mode"] in MODE_OVERLAYS:
        lines.append(MODE_OVERLAYS[card["mode"]]["guidance"])
    if card.get("topic_cue"):
        cue = str(card["topic_cue"]).replace("_", " ")
        lines.append(f"Topic ({card['topic']}): {cue}.")

    # Social-context lines — sarcasm is flagged explicitly because the
    # literal wording points the wrong direction.
    if card.get("sarcasm_detected"):
        lines.append("Subtext: the user's remark reads as sarcasm — "
                     "take the meaning (probably frustration with the "
                     "event), not the literal words.")
    cg = _CUE_GUIDANCE.get(str(card.get("social_cue") or ""), "")
    if cg:
        lines.append("Social cue: " + cg)
    if card.get("energy") == "low":
        lines.append("Energy: low — soften pace; shorter, calmer "
                     "replies.")
    elif card.get("energy") == "high":
        lines.append("Energy: high — brisk, brighter pacing is fine.")
    if float(card.get("pacing") or 1.0) < 0.8:
        lines.append("Pacing: long session — ease off jokes, "
                     "vocalizations, and recap filler; tighter "
                     "updates.")
    if card.get("focus_shifted"):
        lines.append("Topic shifted — transition naturally; don't "
                     "drag the previous thread forward.")
    elif card.get("focus"):
        lines.append(f"Current focus: {card['focus']} — keep "
                     "continuity with it.")
    if card.get("callback_event"):
        lines.append(f"Shared context on point: "
                     f"'{card['callback_event']}' — reference it "
                     "naturally if it fits; don't force it.")
    if card.get("address_hint"):
        lines.append("Address: " + str(card["address_hint"]) + ".")
    if card.get("preference_hint"):
        lines.append(f"Earlier stated preference applies here: "
                     f"{card['preference_hint']} — stay consistent.")
    if card.get("humor_adaptation"):
        lines.append("Humor adaptation: "
                     + str(card["humor_adaptation"]) + ".")
    if card.get("recent_analogies"):
        lines.append("Analogy shapes used recently: "
                     + ", ".join(card["recent_analogies"])
                     + " — prefer a fresh comparison.")
    if card.get("recent_humor_patterns"):
        lines.append("Joke/reaction shapes used recently: "
                     + ", ".join(card["recent_humor_patterns"])
                     + " — vary it or stay dry.")
    if card.get("noticing"):
        lines.append("Notices first: "
                     + str(card["noticing"]) + ".")

    beh_lines = behavior_mod.guidance_lines({
        "motivations": card.get("motivations"),
        "rhythm": card.get("rhythm"),
        "humor_type": card.get("humor_type"),
        "question_style": card.get("question_style"),
        "teaching_style": card.get("teaching_style"),
        "challenge_style": card.get("challenge_style"),
        "praise_style": card.get("praise_style"),
        "criticism_style": card.get("criticism_style"),
        "decision_style": card.get("decision_style"),
        "plan_style": card.get("plan_style"),
        "turn_taking": card.get("turn_taking"),
        "silence": card.get("silence"),
        "aversions": card.get("aversions"),
        "confidence": card.get("confidence"),
        "error_admission": card.get("error_admission"),
        "tool_failure": card.get("tool_failure"),
        "interruption": card.get("interruption"),
        "signature": card.get("signature"),
    })
    lines.extend(beh_lines)

    # Relationship shape — shorthand grows with familiarity.
    fam = float(card.get("familiarity") or 0.0)
    if fam >= 80:
        lines.append("Relationship: long-term — natural shorthand, "
                     "personalized tone, don't re-explain shared "
                     "history.")
    elif fam >= 45:
        lines.append("Relationship: trusted — shorthand and callbacks "
                     "to shared context are natural; skip unneeded "
                     "re-explanation.")
    elif fam >= 15:
        lines.append("Relationship: familiar — a little shorthand is "
                     "fine; occasional callbacks to earlier context.")

    if card.get("mood"):
        lines.append(f"Mood: {card['mood']} — colors delivery; the "
                     "base personality still leads.")

    notes = (card.get("overlay_notes") or []) + \
            (card.get("modifier_notes") or [])
    if notes:
        lines.append("User-adjusted delivery: " + "; ".join(notes) + ".")

    return lines[:max_lines]


def debug_view(card: dict) -> dict:
    """Safe debug snapshot — effective card minus anything private."""
    return {k: v for k, v in card.items()
            if k not in ("overlay_notes", "modifier_notes")}
