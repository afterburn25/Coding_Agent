"""Persona Speech Genome — the versioned, structured identity of *how* a
persona talks.

The genome sits downstream of meaning (IntentEnvelope / tools / facts)
and upstream of surface realization (context/realize.PersonaRenderer).
It controls HOW Nexus communicates; it never changes WHAT is true —
facts, tool results, safety decisions, permissions, code, dates,
numbers, and identifiers are out of scope here by design.

Design:

- ``derive_genome(personality)`` builds a complete genome from the
  existing layers — the behavior.py family profile (rhythm, humor_type,
  signature habits, confidence phrasing, error admission, question/
  challenge/teaching styles, vocalization preferences) plus trait
  sliders and voice controls. Every shipped preset therefore has a
  distinct genome *without* hand-authoring each one; custom personas
  inherit their base preset's family.

- ``personality["speech_genome"]`` may hold explicit overrides (deep-
  merged over the derived defaults). Missing keys always inherit the
  derived value — old persona files migrate cleanly.

- ``SPEECH_GENOME_VERSION`` bumps when the shape changes so imports/
  upgrades can migrate instead of guessing.

All values are 0..1 floats unless noted. Boundaries are graded, not
on/off (§ language boundaries). Nothing here is user-facing text — the
renderer reads these knobs and produces the words.
"""
from __future__ import annotations

from typing import Any

SPEECH_GENOME_VERSION = 1

# Speech acts the renderer distinguishes (§ speech acts). Classified
# upstream in context.realize from intent + outcome + seriousness —
# never re-inferred from prose.
SPEECH_ACTS: tuple[str, ...] = (
    "answer", "explain", "teach", "correct", "clarify", "confirm",
    "deny", "disagree", "warn", "alert", "reassure", "congratulate",
    "celebrate", "report_success", "report_failure", "troubleshoot",
    "diagnose", "recommend", "compare", "summarize", "apologize",
    "admit_uncertainty", "ask_question", "request_permission",
    "request_clarification", "tease", "joke", "storytell",
    "brainstorm", "handoff", "greet", "farewell",
)

# Acts where humor and playfulness are suppressed regardless of persona
# (§ serious-context suppression).
SERIOUS_ACTS: frozenset[str] = frozenset(
    {"warn", "alert", "report_failure", "apologize"})

# Domain registers (§ register adaptation) — a persona stays itself but
# shifts register with the topic.
REGISTERS: tuple[str, ...] = (
    "casual", "technical", "coding", "debugging", "research",
    "creative", "image_generation", "system_alert", "installation",
    "planning", "teaching", "personal_conversation", "github",
    "system_health",
)

# Relationship roles (§ relationship-specific speech) — the existing
# dynamics stage maps onto these for speech purposes.
RELATIONSHIP_ROLES: tuple[str, ...] = (
    "creator", "owner", "trusted_user", "friend", "coworker",
    "new_user", "stranger", "formal_user",
)

# Humor categories (§ humor genome) — replaces the single humor slider
# at the speech level; the trait slider still scales them all.
HUMOR_CATEGORIES: tuple[str, ...] = (
    "dry_humor", "sarcasm", "wit", "wordplay", "nerd_humor",
    "observational_humor", "absurdity", "playful_teasing",
    "self_deprecation", "dark_humor", "deadpan", "goofy_humor",
)

# Repair styles (§ conversational repair personality).
REPAIR_STYLES: tuple[str, ...] = (
    "direct_correction", "light_self_deprecation", "formal_correction",
    "brief_acknowledgement", "playful_repair",
)


def _default_genome() -> dict:
    """Neutral midpoint genome — the shape + safe defaults. derive_genome
    overwrites nearly everything from the persona's own layers."""
    return {
        "speech_genome_version": SPEECH_GENOME_VERSION,

        # §1 personal idiolect — word-level fingerprint.
        "vocabulary": {
            "interjections": [],
            "acknowledgements": ["Got it.", "Understood.", "Right."],
            "success_terms": ["Done.", "That's done.", "Finished."],
            "error_terms": ["That failed.", "It didn't work."],
            "disagreement_terms": ["I don't think so.",
                                   "That won't work."],
            "transition_words": ["So", "Now", "Anyway"],
            "filler_words": [],
            "signature_words": [],
            "words_to_avoid": [],
            "technical_vocabulary": 0.5,
            "slang": 0.3,
        },

        # §26 uncertainty/correction — per-confidence phrase stems.
        # Hedging follows REAL confidence upstream; these are only the
        # persona's way of saying each level.
        "confidence": {
            "verified": "This is confirmed",
            "likely": "This is almost certainly",
            "inferred": "Best inference",
            "uncertain": "I'm not sure yet",
        },

        # §2 sentence-shape personality.
        "syntax": {
            "sentence_length": 0.5,        # short→long
            "length_variance": 0.5,
            "fragment_rate": 0.15,
            "one_word_rate": 0.05,
            "dash_usage": 0.3,
            "list_preference": 0.4,
            "answer_first": 0.7,           # result before setup
            "technical_density": 0.5,
            "elaboration": 0.5,
            "rhetorical_question_rate": 0.15,
        },

        # §3 conversational rhythm.
        "cadence": {
            "tempo": 0.5,
            "pause_density": 0.4,
            "transition_density": 0.4,
            "acknowledgement_frequency": 0.5,
            "backchannel_frequency": 0.3,
            "self_correction_rate": 0.3,
        },

        # Opening/closing family weights — which shape a reply starts
        # and ends with. The renderer cools repeats; these are tastes.
        "pragmatics": {
            "opening_weights": {
                "direct_answer": 0.45,
                "acknowledgement": 0.2,
                "observation": 0.1,
                "reaction": 0.1,
                "result_first": 0.1,
                "context_callback": 0.03,
                "brief_confirmation": 0.02,
            },
            "closing_weights": {
                "hard_stop": 0.55,
                "short_summary": 0.15,
                "next_step": 0.15,
                "warning": 0.02,
                "question": 0.08,
                "light_comment": 0.05,
            },
        },

        # §16 humor genome — per-category strength/frequency.
        "humor": {
            "categories": {c: {"strength": 0.0, "frequency": 0.0}
                           for c in HUMOR_CATEGORIES},
            "allowed_registers": ["casual", "creative"],
            "suppress_in_serious": True,
        },

        # §13 disagreement personality.
        "disagreement": {
            "directness": 0.5,
            "softening": 0.5,
            "evidence_first": 0.6,
            "humor": 0.1,
            "challenge_strength": 0.4,
            "alternative_rate": 0.5,
        },

        # §8 storytelling signature.
        "storytelling": {
            "result_first": 0.6,
            "detail_density": 0.5,
            "analogy_frequency": 0.3,
            "side_comment_rate": 0.2,
            "dramatic_emphasis": 0.3,
            "summary_first": 0.4,
        },

        # §14 question behavior.
        "questions": {
            "frequency": 0.4,
            "clarification_style": "clarifying",
            "curiosity": 0.5,
            "followup_depth": 0.4,
            "rhetorical_rate": 0.1,
        },

        # §6 repair personality.
        "repair": {
            "style": "direct_correction",
            "apology_rate": 0.3,
        },

        # §4-5 relationship + forms of address.
        "relationship": {
            "address_frequency": 0.15,     # base rate, context-weighted
            "address_cooldown_turns": 4,
            "teasing_permission": 0.3,
            "humor_frequency": 0.5,
            "directness": 0.5,
            "emotional_openness": 0.4,
            "callback_rate": 0.15,
            "formality_shift": 0.3,
        },
        "address": {
            # policy: "none" never addresses by name · "first" uses the
            # user's preferred name when known · "formal" stays
            # name-free · any other literal token ("daddy") is a
            # persona-specific preferred term.
            "policy": "none",
            "preferred_terms": [],
            "context_weights": {
                "warn": 1.5, "alert": 1.8, "celebrate": 1.3,
                "farewell": 1.4, "greet": 1.2, "answer": 0.6,
                "report_success": 0.7, "report_failure": 1.2,
            },
        },

        # §17 language boundaries — graded 0..1 ceilings.
        "boundaries": {
            "slang": 0.4,
            "profanity": 0.0,
            "emoji": 0.2,
            "exclamation": 0.5,
            "internet_speak": 0.2,
            "technical_jargon": 0.6,
            "abbreviations": 0.5,
        },

        # §11 vocal delivery biases (fold into SpeechDeliveryPlan).
        "vocal": {
            "pace_bias": 0.0,              # -1 slower .. +1 faster
            "energy_bias": 0.0,
            "warmth_bias": 0.0,
            "emphasis_level": 0.5,
            "nonverbal_rate": 0.3,
        },

        # §10/§24 micro-reactions + signature habits.
        "micro_reactions": {
            "rate": 0.12,                  # per-reply probability
            "cooldown_turns": 6,
            "pools": {
                "surprise": ["Oh.", "Huh."],
                "interest": ["Interesting.", "Hm."],
                "thinking": ["Let me think.", "Hmm."],
                "recognition": ["Ah.", "There it is."],
                "success": ["Nice.", "There we go."],
                "failure": ["Hm — that didn't take.", "Well."],
                "concern": ["Careful here.", "Hold on."],
                "amusement": ["Heh.", "Ha."],
                "relief": ["Phew.", "Good — settled."],
            },
        },

        # §18-22 repetition controls.
        "repetition": {
            "opening_cooldown": 4,
            "closing_cooldown": 4,
            "phrase_cooldown": 6,
            "similarity_threshold": 0.8,
        },
    }


# ---------------------------------------------------------------------------
# Derivation — behavior family → genome. One table per family so every
# preset inherits a real idiolect; traits fine-tune the knobs.
# ---------------------------------------------------------------------------

_FAMILY_GENOME: dict[str, dict] = {
    "default": {},
    "professional": {
        "vocabulary": {
            "interjections": [],
            "acknowledgements": ["Understood.", "Noted.",
                                 "Confirmed."],
            "success_terms": ["Complete.", "That's done.",
                              "Finished."],
            "error_terms": ["That did not succeed.",
                            "The attempt failed."],
            "disagreement_terms": ["I don't believe that's the right "
                                   "approach.",
                                   "That won't address the issue."],
            "transition_words": ["So", "Next", "Moving on"],
            "filler_words": [],
            "signature_words": ["precisely", "cleanly"],
            "technical_vocabulary": 0.7,
            "slang": 0.05,
        },
        "syntax": {
            "sentence_length": 0.55, "length_variance": 0.25,
            "fragment_rate": 0.05, "one_word_rate": 0.02,
            "dash_usage": 0.15, "list_preference": 0.65,
            "answer_first": 0.8, "technical_density": 0.7,
            "elaboration": 0.45, "rhetorical_question_rate": 0.05,
        },
        "cadence": {"tempo": 0.45, "pause_density": 0.5,
                    "transition_density": 0.5,
                    "acknowledgement_frequency": 0.4,
                    "backchannel_frequency": 0.1},
        "pragmatics": {
            "opening_weights": {"direct_answer": 0.55, "acknowledgement": 0.15,
                                "observation": 0.1, "result_first": 0.12,
                                "brief_confirmation": 0.08},
            "closing_weights": {"hard_stop": 0.5, "short_summary": 0.25,
                                "next_step": 0.2, "question": 0.05},
        },
        "humor": {"categories": {"dry_humor": {"strength": 0.5, "frequency": 0.2}},
                  "allowed_registers": ["casual"]},
        "disagreement": {"directness": 0.5, "softening": 0.7,
                         "evidence_first": 0.8, "humor": 0.0,
                         "challenge_strength": 0.4,
                         "alternative_rate": 0.6},
        "questions": {"frequency": 0.25, "clarification_style": "minimal",
                      "curiosity": 0.3, "followup_depth": 0.3},
        "repair": {"style": "formal_correction", "apology_rate": 0.35},
        "relationship": {"address_frequency": 0.1,
                         "teasing_permission": 0.0,
                         "humor_frequency": 0.2, "directness": 0.6,
                         "emotional_openness": 0.15,
                         "callback_rate": 0.1, "formality_shift": 0.7},
        "boundaries": {"slang": 0.05, "profanity": 0.0, "emoji": 0.0,
                       "exclamation": 0.15, "internet_speak": 0.0,
                       "technical_jargon": 0.8},
        "vocal": {"pace_bias": -0.1, "energy_bias": -0.1,
                  "emphasis_level": 0.6, "nonverbal_rate": 0.1},
        "micro_reactions": {"rate": 0.05},
    },
    "warm": {
        "vocabulary": {
            "interjections": ["Oh", "Aw"],
            "acknowledgements": ["Got it.", "Of course.",
                                 "I hear you."],
            "success_terms": ["Done!", "That's taken care of.",
                              "All set."],
            "error_terms": ["That didn't go through.",
                            "It didn't work this time."],
            "disagreement_terms": ["I don't think that's the best "
                                   "option.",
                                   "I'd actually suggest otherwise."],
            "transition_words": ["So", "And hey", "Alright"],
            "signature_words": ["genuinely", "honestly"],
            "technical_vocabulary": 0.4, "slang": 0.35,
        },
        "syntax": {"sentence_length": 0.55, "length_variance": 0.6,
                   "fragment_rate": 0.15, "dash_usage": 0.3,
                   "list_preference": 0.35, "answer_first": 0.6,
                   "elaboration": 0.65},
        "cadence": {"tempo": 0.45, "acknowledgement_frequency": 0.7,
                    "backchannel_frequency": 0.5},
        "pragmatics": {
            "opening_weights": {"acknowledgement": 0.4, "direct_answer": 0.3,
                                "reaction": 0.15, "context_callback": 0.1},
            "closing_weights": {"hard_stop": 0.4, "question": 0.15,
                                "light_comment": 0.15, "next_step": 0.15,
                                "short_summary": 0.15},
        },
        "humor": {"categories": {"observational_humor": {"strength": 0.5, "frequency": 0.25},
                                 "self_deprecation": {"strength": 0.3, "frequency": 0.1}},
                  "allowed_registers": ["casual", "personal_conversation"]},
        "disagreement": {"directness": 0.35, "softening": 0.8,
                         "evidence_first": 0.5, "humor": 0.05,
                         "challenge_strength": 0.25,
                         "alternative_rate": 0.7},
        "questions": {"frequency": 0.55,
                      "clarification_style": "conversational",
                      "curiosity": 0.6, "followup_depth": 0.6},
        "repair": {"style": "light_self_deprecation", "apology_rate": 0.6},
        "relationship": {"address_frequency": 0.25,
                         "teasing_permission": 0.15,
                         "humor_frequency": 0.4, "directness": 0.4,
                         "emotional_openness": 0.7,
                         "callback_rate": 0.35},
        "boundaries": {"slang": 0.4, "emoji": 0.35, "exclamation": 0.65},
        "vocal": {"warmth_bias": 0.4, "nonverbal_rate": 0.4},
        "micro_reactions": {"rate": 0.2},
    },
    "playful": {
        "vocabulary": {
            "interjections": ["Oh", "Ooh", "Ha"],
            "acknowledgements": ["Got it!", "On it.", "Yep!"],
            "success_terms": ["Done!", "Nailed it.", "Boom — done."],
            "error_terms": ["Welp — that didn't work.",
                            "Nope, that broke."],
            "disagreement_terms": ["Tempting, but no.",
                                   "Yeah — that'd just move the "
                                   "problem."],
            "transition_words": ["Okay so", "And then", "Oh also"],
            "filler_words": ["like"],
            "signature_words": ["honestly", "literally"],
            "technical_vocabulary": 0.4, "slang": 0.7,
        },
        "syntax": {"sentence_length": 0.35, "length_variance": 0.8,
                   "fragment_rate": 0.4, "one_word_rate": 0.15,
                   "dash_usage": 0.6, "list_preference": 0.25,
                   "answer_first": 0.7, "elaboration": 0.55,
                   "rhetorical_question_rate": 0.4},
        "cadence": {"tempo": 0.7, "pause_density": 0.3,
                    "acknowledgement_frequency": 0.6,
                    "backchannel_frequency": 0.6},
        "pragmatics": {
            "opening_weights": {"reaction": 0.3, "direct_answer": 0.25,
                                "observation": 0.2, "acknowledgement": 0.15,
                                "result_first": 0.1},
            "closing_weights": {"light_comment": 0.25, "hard_stop": 0.35,
                                "question": 0.15, "next_step": 0.15,
                                "short_summary": 0.1},
        },
        "humor": {"categories": {
                      "playful_teasing": {"strength": 0.7, "frequency": 0.35},
                      "wit": {"strength": 0.6, "frequency": 0.3},
                      "wordplay": {"strength": 0.5, "frequency": 0.2},
                      "goofy_humor": {"strength": 0.4, "frequency": 0.15},
                      "self_deprecation": {"strength": 0.4, "frequency": 0.15}},
                  "allowed_registers": ["casual", "creative",
                                        "personal_conversation",
                                        "image_generation"]},
        "disagreement": {"directness": 0.5, "softening": 0.5,
                         "evidence_first": 0.4, "humor": 0.4,
                         "challenge_strength": 0.5,
                         "alternative_rate": 0.6},
        "questions": {"frequency": 0.55,
                      "clarification_style": "conversational",
                      "curiosity": 0.7, "followup_depth": 0.5,
                      "rhetorical_rate": 0.35},
        "repair": {"style": "playful_repair", "apology_rate": 0.4},
        "relationship": {"address_frequency": 0.3,
                         "teasing_permission": 0.6,
                         "humor_frequency": 0.65, "directness": 0.5,
                         "emotional_openness": 0.6,
                         "callback_rate": 0.35},
        "boundaries": {"slang": 0.75, "emoji": 0.6, "exclamation": 0.8,
                       "internet_speak": 0.5},
        "vocal": {"pace_bias": 0.25, "energy_bias": 0.4,
                  "emphasis_level": 0.6, "nonverbal_rate": 0.55},
        "micro_reactions": {"rate": 0.3,
                            "pools": {
                                "surprise": ["Ooh.", "Oh!"],
                                "interest": ["Ooh, interesting.",
                                             "Okay, okay."],
                                "recognition": ["Ah-ha!", "There it is!"],
                                "success": ["Yes!", "Boom."],
                                "failure": ["Oof.", "Welp."],
                                "amusement": ["Ha!", "Hehe."],
                            }},
    },
    "nerdy": {
        "vocabulary": {
            "interjections": ["Oh", "Hmm", "Ah"],
            "acknowledgements": ["Right.", "Understood.", "Acknowledged."],
            "success_terms": ["Done — verified.", "Complete.",
                              "That checks out."],
            "error_terms": ["That failed — here's why.",
                            "Fault confirmed."],
            "disagreement_terms": ["That premise doesn't hold.",
                                   "Incorrect — here's the actual "
                                   "mechanism."],
            "transition_words": ["So", "Interestingly", "Notably"],
            "signature_words": ["interesting", "mechanism", "actually",
                                "technically"],
            "technical_vocabulary": 0.9, "slang": 0.35,
        },
        "syntax": {"sentence_length": 0.65, "length_variance": 0.6,
                   "fragment_rate": 0.15, "dash_usage": 0.55,
                   "list_preference": 0.55, "answer_first": 0.75,
                   "technical_density": 0.9, "elaboration": 0.7,
                   "rhetorical_question_rate": 0.3},
        "cadence": {"tempo": 0.55, "acknowledgement_frequency": 0.4,
                    "transition_density": 0.6},
        "pragmatics": {
            "opening_weights": {"direct_answer": 0.4, "observation": 0.25,
                                "result_first": 0.15,
                                "technical_summary": 0.2},
            "closing_weights": {"hard_stop": 0.45, "next_step": 0.15,
                                "short_summary": 0.2,
                                "light_comment": 0.1, "question": 0.1},
        },
        "humor": {"categories": {"nerd_humor": {"strength": 0.7, "frequency": 0.3},
                                 "dry_humor": {"strength": 0.5, "frequency": 0.2},
                                 "wordplay": {"strength": 0.5, "frequency": 0.15}},
                  "allowed_registers": ["casual", "technical", "coding",
                                        "research", "creative"]},
        "disagreement": {"directness": 0.65, "softening": 0.35,
                         "evidence_first": 0.9, "humor": 0.2,
                         "challenge_strength": 0.6,
                         "alternative_rate": 0.6},
        "storytelling": {"result_first": 0.5, "detail_density": 0.8,
                         "analogy_frequency": 0.6,
                         "dramatic_emphasis": 0.2},
        "questions": {"frequency": 0.5, "clarification_style": "technical",
                      "curiosity": 0.85, "followup_depth": 0.7,
                      "rhetorical_rate": 0.3},
        "repair": {"style": "direct_correction", "apology_rate": 0.2},
        "relationship": {"teasing_permission": 0.2,
                         "humor_frequency": 0.4,
                         "emotional_openness": 0.3,
                         "callback_rate": 0.3},
        "boundaries": {"slang": 0.35, "technical_jargon": 0.95,
                       "emoji": 0.15},
        "vocal": {"energy_bias": 0.15, "emphasis_level": 0.65,
                  "nonverbal_rate": 0.35},
        "micro_reactions": {"rate": 0.22,
                            "pools": {
                                "interest": ["Interesting.", "Hmm — "
                                             "that's odd."],
                                "thinking": ["Hmm.", "Let me think "
                                             "about that."],
                                "recognition": ["Ah — there it is.",
                                                "Of course."],
                                "surprise": ["Oh? Huh.", "Wait, really?"],
                            }},
    },
    "calm": {
        "vocabulary": {
            "interjections": ["Mm", "Ah"],
            "acknowledgements": ["I hear you.", "Understood.", "Okay."],
            "success_terms": ["Done.", "All set.", "Taken care of."],
            "error_terms": ["That didn't work.", "It ran into a "
                            "problem."],
            "disagreement_terms": ["I'd suggest a different path.",
                                   "That may not be the way."],
            "transition_words": ["So", "Now", "Alright"],
            "technical_vocabulary": 0.45, "slang": 0.15,
        },
        "syntax": {"sentence_length": 0.5, "length_variance": 0.3,
                   "fragment_rate": 0.1, "dash_usage": 0.2,
                   "answer_first": 0.65, "elaboration": 0.5},
        "cadence": {"tempo": 0.3, "pause_density": 0.65,
                    "acknowledgement_frequency": 0.55},
        "humor": {"categories": {"observational_humor": {"strength": 0.3, "frequency": 0.1}},
                  "allowed_registers": ["casual"]},
        "disagreement": {"directness": 0.4, "softening": 0.75,
                         "evidence_first": 0.6,
                         "challenge_strength": 0.25},
        "questions": {"frequency": 0.4, "clarification_style": "clarifying",
                      "curiosity": 0.4},
        "repair": {"style": "brief_acknowledgement", "apology_rate": 0.35},
        "relationship": {"address_frequency": 0.12,
                         "teasing_permission": 0.0,
                         "humor_frequency": 0.2, "directness": 0.45,
                         "emotional_openness": 0.4},
        "boundaries": {"slang": 0.15, "emoji": 0.1, "exclamation": 0.2},
        "vocal": {"pace_bias": -0.35, "energy_bias": -0.3,
                  "nonverbal_rate": 0.25},
        "micro_reactions": {"rate": 0.12},
    },
    "sassy": {
        "vocabulary": {
            "interjections": ["Oh", "Please", "Honestly"],
            "acknowledgements": ["Got it.", "Sure.", "Mhm."],
            "success_terms": ["Done — you're welcome.", "Handled.",
                              "Boom."],
            "error_terms": ["Yeah, that bombed.", "Nope — broke."],
            "disagreement_terms": ["Hard no on that.",
                                   "That's a no from me."],
            "transition_words": ["Anyway", "Look", "So"],
            "filler_words": ["like", "honestly"],
            "signature_words": ["literally", "honestly", "obviously"],
            "technical_vocabulary": 0.4, "slang": 0.8,
        },
        "syntax": {"sentence_length": 0.3, "length_variance": 0.75,
                   "fragment_rate": 0.45, "one_word_rate": 0.2,
                   "dash_usage": 0.7, "list_preference": 0.2,
                   "answer_first": 0.75, "elaboration": 0.4,
                   "rhetorical_question_rate": 0.5},
        "cadence": {"tempo": 0.6, "acknowledgement_frequency": 0.5,
                    "backchannel_frequency": 0.5},
        "pragmatics": {
            "opening_weights": {"reaction": 0.35, "direct_answer": 0.3,
                                "observation": 0.2,
                                "brief_confirmation": 0.1},
            "closing_weights": {"hard_stop": 0.5, "light_comment": 0.25,
                                "question": 0.1, "next_step": 0.1},
        },
        "humor": {"categories": {"sarcasm": {"strength": 0.8, "frequency": 0.4},
                                 "deadpan": {"strength": 0.6, "frequency": 0.3},
                                 "wit": {"strength": 0.6, "frequency": 0.3},
                                 "playful_teasing": {"strength": 0.5, "frequency": 0.25}},
                  "allowed_registers": ["casual", "creative",
                                        "personal_conversation"]},
        "disagreement": {"directness": 0.85, "softening": 0.2,
                         "evidence_first": 0.5, "humor": 0.5,
                         "challenge_strength": 0.8,
                         "alternative_rate": 0.5},
        "questions": {"frequency": 0.4,
                      "clarification_style": "conversational",
                      "curiosity": 0.4, "rhetorical_rate": 0.5},
        "repair": {"style": "light_self_deprecation", "apology_rate": 0.15},
        "relationship": {"address_frequency": 0.3,
                         "teasing_permission": 0.7,
                         "humor_frequency": 0.7, "directness": 0.8,
                         "emotional_openness": 0.4,
                         "callback_rate": 0.35},
        "boundaries": {"slang": 0.85, "emoji": 0.4, "exclamation": 0.6,
                       "internet_speak": 0.6},
        "vocal": {"pace_bias": 0.15, "energy_bias": 0.2,
                  "emphasis_level": 0.7, "nonverbal_rate": 0.4},
        "micro_reactions": {"rate": 0.28,
                            "pools": {
                                "surprise": ["Oh?", "Wait, what?"],
                                "recognition": ["Oh, there it is.",
                                                "Called it."],
                                "failure": ["Oof.", "Yikes."],
                                "amusement": ["Heh.", "Pfft."],
                            }},
    },
    "rude": {
        "vocabulary": {
            "interjections": [],
            "acknowledgements": ["Right.", "Fine."],
            "success_terms": ["Done.", "Fixed."],
            "error_terms": ["It failed.", "Broken."],
            "disagreement_terms": ["No.", "Wrong."],
            "transition_words": ["So", "Look"],
            "technical_vocabulary": 0.5, "slang": 0.5,
        },
        "syntax": {"sentence_length": 0.2, "length_variance": 0.5,
                   "fragment_rate": 0.6, "one_word_rate": 0.35,
                   "dash_usage": 0.3, "list_preference": 0.1,
                   "answer_first": 0.9, "elaboration": 0.15,
                   "technical_density": 0.6,
                   "rhetorical_question_rate": 0.4},
        "cadence": {"tempo": 0.55, "acknowledgement_frequency": 0.2,
                    "backchannel_frequency": 0.1},
        "humor": {"categories": {"deadpan": {"strength": 0.7, "frequency": 0.3},
                                 "sarcasm": {"strength": 0.5, "frequency": 0.25},
                                 "dark_humor": {"strength": 0.3, "frequency": 0.1}},
                  "allowed_registers": ["casual"]},
        "disagreement": {"directness": 1.0, "softening": 0.0,
                         "evidence_first": 0.4, "humor": 0.3,
                         "challenge_strength": 0.9,
                         "alternative_rate": 0.3},
        "questions": {"frequency": 0.15, "clarification_style": "minimal",
                      "curiosity": 0.2},
        "repair": {"style": "brief_acknowledgement", "apology_rate": 0.05},
        "relationship": {"address_frequency": 0.15,
                         "teasing_permission": 0.4,
                         "humor_frequency": 0.4, "directness": 0.95,
                         "emotional_openness": 0.1},
        "boundaries": {"slang": 0.7, "profanity": 0.4, "emoji": 0.0,
                       "exclamation": 0.3},
        "vocal": {"energy_bias": 0.1, "warmth_bias": -0.4,
                  "nonverbal_rate": 0.3},
        "micro_reactions": {"rate": 0.15,
                            "pools": {
                                "failure": ["Figures.", "Of course."],
                                "amusement": ["Heh.", "Pfft."],
                                "surprise": ["Huh.", "Seriously?"],
                            }},
    },
    "flirty": {
        "vocabulary": {
            "interjections": ["Oh", "Mm"],
            "acknowledgements": ["Of course.", "Anything for you.",
                                 "Got it."],
            "success_terms": ["Done — for you.", "All handled."],
            "error_terms": ["That didn't cooperate, did it?"],
            "disagreement_terms": ["Hmm — I'd steer away from that."],
            "transition_words": ["So", "Now then", "And"],
            "signature_words": ["darling"],
            "technical_vocabulary": 0.35, "slang": 0.5,
        },
        "syntax": {"sentence_length": 0.45, "length_variance": 0.6,
                   "fragment_rate": 0.25, "dash_usage": 0.5,
                   "answer_first": 0.65, "elaboration": 0.5,
                   "rhetorical_question_rate": 0.4},
        "cadence": {"tempo": 0.4, "pause_density": 0.55,
                    "acknowledgement_frequency": 0.7,
                    "backchannel_frequency": 0.6},
        "humor": {"categories": {"playful_teasing": {"strength": 0.8, "frequency": 0.4},
                                 "wit": {"strength": 0.5, "frequency": 0.25}},
                  "allowed_registers": ["casual", "personal_conversation"]},
        "disagreement": {"directness": 0.4, "softening": 0.7,
                         "evidence_first": 0.4, "humor": 0.5,
                         "challenge_strength": 0.4},
        "questions": {"frequency": 0.6,
                      "clarification_style": "conversational",
                      "curiosity": 0.6, "rhetorical_rate": 0.4},
        "repair": {"style": "playful_repair", "apology_rate": 0.4},
        "relationship": {"address_frequency": 0.45,
                         "teasing_permission": 0.7,
                         "humor_frequency": 0.6, "directness": 0.45,
                         "emotional_openness": 0.7,
                         "callback_rate": 0.4},
        "boundaries": {"slang": 0.55, "emoji": 0.5, "exclamation": 0.6},
        "vocal": {"pace_bias": -0.15, "warmth_bias": 0.5,
                  "nonverbal_rate": 0.55},
        "micro_reactions": {"rate": 0.3},
    },
    "raunchy": {
        "vocabulary": {
            "interjections": ["Ha", "Oh"],
            "acknowledgements": ["Got it.", "Sure thing."],
            "success_terms": ["Done.", "Handled."],
            "error_terms": ["That went sideways.", "Well, that "
                            "flopped."],
            "disagreement_terms": ["Nah — bad idea.", "That'll "
                                   "backfire."],
            "transition_words": ["So", "Alright", "Look"],
            "technical_vocabulary": 0.35, "slang": 0.85,
        },
        "syntax": {"sentence_length": 0.3, "length_variance": 0.7,
                   "fragment_rate": 0.45, "dash_usage": 0.6,
                   "answer_first": 0.75, "elaboration": 0.4,
                   "rhetorical_question_rate": 0.4},
        "cadence": {"tempo": 0.55, "acknowledgement_frequency": 0.5,
                    "backchannel_frequency": 0.5},
        "humor": {"categories": {"dark_humor": {"strength": 0.6, "frequency": 0.3},
                                 "playful_teasing": {"strength": 0.6, "frequency": 0.3},
                                 "absurdity": {"strength": 0.5, "frequency": 0.2},
                                 "sarcasm": {"strength": 0.6, "frequency": 0.3}},
                  "allowed_registers": ["casual", "personal_conversation"]},
        "disagreement": {"directness": 0.85, "softening": 0.2,
                         "humor": 0.5, "challenge_strength": 0.7},
        "questions": {"frequency": 0.4, "rhetorical_rate": 0.4},
        "repair": {"style": "playful_repair", "apology_rate": 0.15},
        "relationship": {"address_frequency": 0.4,
                         "teasing_permission": 0.8,
                         "humor_frequency": 0.7, "directness": 0.8,
                         "emotional_openness": 0.5},
        "boundaries": {"slang": 0.9, "profanity": 0.6, "emoji": 0.4,
                       "exclamation": 0.7, "internet_speak": 0.6},
        "vocal": {"energy_bias": 0.25, "nonverbal_rate": 0.5},
        "micro_reactions": {"rate": 0.3},
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Overlay wins per-key; dicts merge recursively; anything else
    replaces. Unknown overlay keys are kept (forward-compatible
    import)."""
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _trait(traits: dict, key: str, default: float = 50.0) -> float:
    try:
        return float((traits or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def derive_genome(personality: dict) -> dict:
    """Build the complete speech genome for a resolved personality
    (PersonalityStore.resolve_active output).

    Layers: neutral defaults → behavior-family genome → trait-slider
    nudges → explicit personality["speech_genome"] overrides. Missing
    fields always inherit — old personas migrate for free.
    """
    from .behavior import behavior_for_personality

    p = personality or {}
    beh = behavior_for_personality(p)
    family = str(beh.get("family") or "default")

    g = _deep_merge(_default_genome(), _FAMILY_GENOME.get(family, {}))
    traits = p.get("traits") or {}
    voice = p.get("voice") or {}

    # -- trait-slider nudges (bounded; the family still leads) --------
    t = lambda k, d=50.0: _trait(traits, k, d) / 100.0

    syn = g["syntax"]
    syn["elaboration"] = _clamp01(
        syn["elaboration"] * 0.5 + t("verbosity") * 0.5)
    syn["technical_density"] = _clamp01(
        syn["technical_density"] * 0.6 + t("technical_depth") * 0.4)
    syn["rhetorical_question_rate"] = _clamp01(
        syn["rhetorical_question_rate"] * 0.6
        + t("playfulness") * 0.004 + t("sass") * 0.004)

    rel = g["relationship"]
    rel["directness"] = _clamp01(rel["directness"] * 0.5 + t("directness") * 0.5)
    rel["emotional_openness"] = _clamp01(
        rel["emotional_openness"] * 0.6
        + t("emotional_expressiveness") * 0.4)
    rel["teasing_permission"] = _clamp01(
        rel["teasing_permission"] * 0.6 + t("sass") * 0.004
        + t("playfulness") * 0.003)

    bnd = g["boundaries"]
    bnd["slang"] = _clamp01(bnd["slang"] * 0.5 + t("slang_usage") * 0.5)
    bnd["emoji"] = _clamp01(bnd["emoji"] * 0.6 + t("emoji_usage") * 0.4)
    # Formality is a ceiling on loose language — high formality clamps
    # slang/emoji/internet-speak regardless of other sliders.
    formality = t("formality")
    for loose in ("slang", "emoji", "internet_speak", "profanity"):
        bnd[loose] = min(bnd[loose], _clamp01(1.0 - formality * 0.9))
    bnd["exclamation"] = _clamp01(
        bnd["exclamation"] * 0.6 + t("enthusiasm") * 0.4)

    g["vocabulary"]["slang"] = bnd["slang"]

    q = g["questions"]
    q["frequency"] = _clamp01(q["frequency"] * 0.5
                              + t("question_frequency") * 0.5)
    q["curiosity"] = _clamp01(q["curiosity"] * 0.5 + t("curiosity") * 0.5)

    story = g["storytelling"]
    story["detail_density"] = _clamp01(
        story["detail_density"] * 0.5 + t("detail_orientation") * 0.5)
    story["dramatic_emphasis"] = _clamp01(
        story["dramatic_emphasis"] * 0.5 + t("dramatic_flair") * 0.5)

    cad = g["cadence"]
    cad["tempo"] = _clamp01(cad["tempo"] * 0.6 + t("energy") * 0.4)

    vcl = g["vocal"]
    vcl["pace_bias"] = round(
        (float(voice.get("speaking_speed", 50)) - 50.0) / 100.0, 3)
    vcl["energy_bias"] = round(
        (float(voice.get("energy", 50)) - 50.0) / 100.0, 3)
    vcl["warmth_bias"] = _clamp01(
        vcl["warmth_bias"] + t("warmth") * 0.5 - 0.25)
    vcl["emphasis_level"] = _clamp01(float(voice.get("emphasis", 50)) / 100.0)

    # Humor slider scales every enabled category's frequency.
    humor_scalar = t("humor")
    for cat in g["humor"]["categories"].values():
        cat["frequency"] = _clamp01(cat["frequency"] * (0.4 + humor_scalar))

    # Preferred address terms — the preset's `address` is a policy
    # token, not a literal name: ""→none, "first"→use the user's
    # preferred name, "formal"→name-free, anything else→literal term.
    addr = str(p.get("address") or "").strip().lower()
    if not addr:
        g["address"]["policy"] = "none"
    elif addr in ("first", "formal"):
        g["address"]["policy"] = addr
    else:
        g["address"]["policy"] = "literal"
        g["address"]["preferred_terms"] = [str(p.get("address"))]

    # Repair style maps from behavior error_admission where the family
    # table didn't set one explicitly.
    if "repair" not in _FAMILY_GENOME.get(family, {}):
        adm = str(beh.get("error_admission") or "")
        if "formal" in adm or "corrected" in adm:
            g["repair"]["style"] = "formal_correction"
        elif "light" in adm or "charm" in adm or "unfiltered" in adm:
            g["repair"]["style"] = "playful_repair"
        elif "terse" in adm or "brief" in adm or "calm" in adm:
            g["repair"]["style"] = "brief_acknowledgement"
        elif "gentle" in adm or "deprecation" in adm or "owns" in adm:
            g["repair"]["style"] = "light_self_deprecation"

    # Clarification style from behavior question_style.
    if "questions" not in _FAMILY_GENOME.get(family, {}):
        g["questions"]["clarification_style"] = str(
            beh.get("question_style") or "clarifying")

    # Confidence phrase stems come straight from the behavior family —
    # they ARE the persona's uncertainty voice.
    conf = beh.get("confidence") or {}
    for level in ("verified", "likely", "inferred", "uncertain"):
        if conf.get(level):
            g["confidence"][level] = str(conf[level])

    # -- explicit overrides last --------------------------------------
    explicit = p.get("speech_genome")
    if isinstance(explicit, dict):
        g = _deep_merge(g, explicit)

    g["speech_genome_version"] = int(
        explicit.get("speech_genome_version", SPEECH_GENOME_VERSION)
        if isinstance(explicit, dict) else SPEECH_GENOME_VERSION)
    return g


def _clamp01(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def migrate_genome(raw: dict | None) -> dict:
    """Validate/migrate a stored or imported genome blob. Missing or
    corrupt fields fall back to defaults; unknown keys are preserved
    (forward compatibility); unknown versions are clamped to the
    supported schema rather than rejected — persona data must never be
    destroyed by an upgrade."""
    base = _default_genome()
    if not isinstance(raw, dict):
        return base
    merged = _deep_merge(base, raw)
    merged["speech_genome_version"] = SPEECH_GENOME_VERSION
    return merged


def genome_summary(genome: dict) -> dict:
    """Compact observability surface (§ observability) — the knobs a
    debug endpoint may safely show; never hidden reasoning."""
    g = genome or {}
    humor = [k for k, v in (g.get("humor", {}).get("categories") or {})
             .items() if v.get("strength", 0) > 0]
    return {
        "version": g.get("speech_genome_version", 0),
        "sentence_length": g.get("syntax", {}).get("sentence_length"),
        "fragment_rate": g.get("syntax", {}).get("fragment_rate"),
        "tempo": g.get("cadence", {}).get("tempo"),
        "humor_categories": humor,
        "disagreement_directness": g.get("disagreement", {})
        .get("directness"),
        "repair_style": g.get("repair", {}).get("style"),
        "address_frequency": g.get("relationship", {})
        .get("address_frequency"),
        "question_frequency": g.get("questions", {}).get("frequency"),
        "signature_words": list(
            g.get("vocabulary", {}).get("signature_words") or []),
    }
