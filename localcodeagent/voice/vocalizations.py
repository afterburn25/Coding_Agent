"""Natural Vocalization & Gesture Engine.

Model output contains raw non-verbal tokens ("Mmm...", "HAHA", "Ugh",
"*sighs*") that a text-to-speech G2P either letterizes or voices stiffly.
This module sits between the SpeechTextFilter and the synthesis queue:

    filtered prose ──▶ detect (token table + stage directions)
                   ──▶ policy gate (persona style, mood, strength,
                                      level, cooldown, caps, adult gate)
                   ──▶ resolve (engine adapter → TTS-safe rendering)
                   ──▶ events (vocalization telemetry + paired gestures)

Display text is never touched — the engine only rewrites the text that
reaches the synthesizer (``speech_text``). Everything Kokoro-specific
lives in ``KokoroVocalizationAdapter``; a future engine supplies its own
adapter behind the same ``VocalizationAdapter`` interface.
"""
from __future__ import annotations

import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Semantic model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Vocalization:
    """One detected non-verbal reaction, normalized to semantics."""
    category: str            # agreement, thinking, amusement, sigh, ...
    style: str               # canonical style key: mm_hmm, sigh_soft, ...
    intensity: float         # 0..1
    token: str               # surface text as written ("MMHMM", "*sighs*")
    adult: bool = False      # requires an 18+ persona to keep


@dataclass(frozen=True)
class GestureEvent:
    """Semantic gesture paired with a vocalization (future avatar hooks)."""
    gesture: str
    intensity: float
    source: str              # vocalization style that produced it


@dataclass
class ResolveResult:
    speech_text: str
    display_text: str                    # input, untouched
    events: list[dict] = field(default_factory=list)   # gesture events
    decisions: list[dict] = field(default_factory=list)  # telemetry


# ---------------------------------------------------------------------------
# Detection tables — surface form → semantics
# ---------------------------------------------------------------------------

# Each entry: (style, category, intensity, pattern, adult_only)
# Patterns match whole tokens in prose (applied with word-ish boundaries
# added by _TOKEN_RE assembly). Order in the alternation is by descending
# surface length so "mmmm" beats "mmm" and "mm-hmm" beats "hmm".
_TOKENS: list[tuple[str, str, float, str, bool]] = [
    # -- agreement / acknowledgment
    ("mm_hmm",     "agreement",    0.4, r"mm[-–\s]?hmm|mhm|mmhmm", False),
    ("uh_huh",     "agreement",    0.4, r"uh[-–\s]?huh", False),
    ("mm_affirm",  "agreement",    0.3, r"mm!(?!\w)", False),
    # -- disagreement / refusal
    ("mm_mm",      "disagreement", 0.5, r"mm[-–\s]?mm", False),
    ("uh_uh",      "disagreement", 0.5, r"uh[-–\s]?uh|nuh[-–\s]?uh", False),
    # -- thinking / hesitation
    ("hmm",        "thinking",     0.3, r"hmm+m*", False),
    ("um",         "thinking",     0.3, r"umm+|um", False),
    ("uhh",        "thinking",     0.3, r"uhh+|uh", False),
    ("erm",        "thinking",     0.3, r"err?m+", False),
    ("hm",         "thinking",     0.25, r"hm\b", False),
    # -- pleasure / approval
    ("mmm_pleased","pleasure",     0.5, r"mmm{1,}n?", False),
    # -- realization
    ("aha",        "realization",  0.5, r"ah[-–]?ha", False),
    ("ah",         "realization",  0.35, r"ahh+|ah(?=[\s,.!?…]|$)", False),
    ("oh",         "realization",  0.3, r"ohh+|oh(?=[\s,.!?…]|$)", False),
    # -- surprise / startle
    ("gasp",       "surprise",     0.6, r"gasp|eek", False),
    ("ooh",        "surprise",     0.5, r"o{2,}h+[!?]?", False),
    ("whoa",       "surprise",     0.55, r"who+a+", False),
    ("wow",        "surprise",     0.5, r"wow+", False),
    # -- interest / curiosity
    ("huh",        "curiosity",    0.35, r"huh+\b", False),
    ("eh",         "curiosity",    0.3, r"eh\b", False),
    # -- relief
    ("phew",       "relief",       0.6, r"phew+", False),
    # -- frustration / effort / discomfort
    ("ugh",        "frustration",  0.55, r"ugg?h+|ar+g+h|grr+", False),
    ("oof",        "discomfort",   0.4, r"oo?f\b", False),
    ("ow",         "discomfort",   0.5, r"oww*!?|ouch", False),
    ("ngh",        "effort",       0.45, r"ngh+|ungh", False),
    ("hup",        "effort",       0.4, r"hup+\b", False),
    # -- annoyance / dismissive
    ("tsk",        "annoyance",    0.45, r"t+(?:sk)+|tch\b", False),
    ("scoff",      "dismissive",   0.5, r"scoffs?|hmph|humph", False),
    ("pfft",       "dismissive",   0.5, r"pfft+|pffth?", False),
    ("meh",        "dismissive",   0.4, r"meh+\b", False),
    # -- disappointment / sympathy / sadness
    ("aww",        "sympathy",     0.5, r"aww+|aw\b", False),
    ("sniff",      "sadness",      0.5, r"sniff(?:le)?s?", False),
    # -- amusement
    ("haha",       "amusement",    0.5, r"(?:ha){2,}h?|(?:bah){2,}|ahahaha", False),
    ("hehe",       "playfulness",  0.45, r"(?:he){2,}h?\b|tee[-\s]?hee", False),
    ("heh",        "amusement",    0.3, r"heh\b", False),
    ("hihi",       "playfulness",  0.4, r"(?:hi){2,}h?\b", False),
    # -- cute / playful accidents
    ("oops",       "playfulness",  0.4, r"oo?ps|who?ops", False),
    # -- celebratory
    ("yay",        "celebration",  0.55, r"yay+|yaay", False),
    ("woo",        "celebration",  0.55, r"woo+hoo*|woo+", False),
    ("whee",       "celebration",  0.5, r"whee+", False),
    # -- fatigue
    ("yawn",       "fatigue",      0.5, r"yawn+", False),
    # -- attention-getting
    ("ahem",       "attention",    0.4, r"ahem+", False),
    ("psst",       "attention",    0.45, r"pss?t+", False),
    # -- sighs / breaths written as words
    ("sigh",       "sigh",         0.45,
     r"sigh(?:s|ed|ing)?(?=[\s,.!?…*]|$)", False),
    ("breath",     "breathing",    0.35, r"(?:deep\s+)?breath(?=[\s,.!?…*]|$)", False),
]

# Stage-direction inner phrases: "*sighs*", "(giggles)", "*clears throat*".
_STAGE: dict[str, tuple[str, str, float]] = {
    "sigh": ("sigh_soft", "sigh", 0.4), "sighs": ("sigh_soft", "sigh", 0.4),
    "sighs softly": ("sigh_soft", "sigh", 0.35),
    "deep sigh": ("sigh_deep", "sigh", 0.6),
    "sighs deeply": ("sigh_deep", "sigh", 0.6),
    "relieved sigh": ("sigh_relieved", "relief", 0.55),
    "sighs in relief": ("sigh_relieved", "relief", 0.55),
    "sighs with relief": ("sigh_relieved", "relief", 0.55),
    "tired sigh": ("sigh_tired", "fatigue", 0.5),
    "sighs tiredly": ("sigh_tired", "fatigue", 0.5),
    "frustrated sigh": ("sigh_frustrated", "frustration", 0.6),
    "chuckle": ("chuckle", "amusement", 0.4),
    "chuckles": ("chuckle", "amusement", 0.4),
    "chuckles softly": ("chuckle", "amusement", 0.35),
    "laugh": ("laugh", "amusement", 0.6),
    "laughs": ("laugh", "amusement", 0.6),
    "laughs softly": ("soft_laugh", "amusement", 0.45),
    "giggle": ("giggle", "playfulness", 0.5),
    "giggles": ("giggle", "playfulness", 0.5),
    "snicker": ("snicker", "amusement", 0.45),
    "snickers": ("snicker", "amusement", 0.45),
    "gasp": ("gasp", "surprise", 0.6),
    "gasps": ("gasp", "surprise", 0.6),
    "sharp inhale": ("sharp_inhale", "surprise", 0.55),
    "inhales sharply": ("sharp_inhale", "surprise", 0.55),
    "breathes in": ("inhale", "breathing", 0.35),
    "deep breath": ("deep_breath", "breathing", 0.5),
    "takes a breath": ("deep_breath", "breathing", 0.45),
    "takes a deep breath": ("deep_breath", "breathing", 0.55),
    "exhales": ("exhale", "breathing", 0.4),
    "exhales slowly": ("long_exhale", "breathing", 0.5),
    "long exhale": ("long_exhale", "breathing", 0.5),
    "yawn": ("yawn", "fatigue", 0.5),
    "yawns": ("yawn", "fatigue", 0.5),
    "sniff": ("sniff", "sadness", 0.5),
    "sniffs": ("sniff", "sadness", 0.5),
    "sniffles": ("sniff", "sadness", 0.55),
    "clears throat": ("throat_clear", "attention", 0.45),
    "clears her throat": ("throat_clear", "attention", 0.45),
    "clears his throat": ("throat_clear", "attention", 0.45),
    "hums": ("hum", "humming", 0.4),
    "hums softly": ("hum", "humming", 0.35),
    "grins": ("smile", "amusement", 0.35),
    "smiles": ("smile", "warmth", 0.35),
    "scoffs": ("scoff", "dismissive", 0.5),
    "groans": ("groan", "frustration", 0.6),
    "groan": ("groan", "frustration", 0.6),
    "moans": ("soft_moan", "pleasure", 0.55),
    "shivers": ("shiver", "discomfort", 0.5),
    "whistles": ("whistle", "playfulness", 0.45),
    "pauses": ("pause", "thinking", 0.25),
    "bites lip": ("nervous", "embarrassment", 0.4),
}

# Gesture paired with each category — semantic events only; the avatar
# layer (when it exists) maps these to animation. Intensity inherits the
# vocalization's.
_GESTURE: dict[str, str] = {
    "agreement": "small_nod", "disagreement": "small_head_shake",
    "thinking": "head_tilt", "pleasure": "small_smile",
    "realization": "eyebrow_raise", "surprise": "eyes_widen",
    "curiosity": "head_tilt", "relief": "subtle_exhale",
    "frustration": "small_frown", "annoyance": "eye_narrow",
    "dismissive": "smirk", "sympathy": "soft_gaze",
    "sadness": "concerned_expression", "amusement": "amused_expression",
    "playfulness": "playful_expression", "celebration": "bright_smile",
    "fatigue": "slow_blink", "attention": "slight_lean",
    "sigh": "subtle_exhale", "breathing": "subtle_exhale",
    "humming": "contented_expression", "discomfort": "wince",
    "effort": "wince", "embarrassment": "look_away",
    "warmth": "soft_smile",
}

# ---------------------------------------------------------------------------
# Persona policy — style-keyed (greeting_style is the persona family key)
# ---------------------------------------------------------------------------
# bias: base keep-probability multiplier. allow: categories this persona
# family will voice (None = all non-adult). prefer: styles nudged ahead
# when a choice exists. adult_ok: breathier adult render variants allowed.

_STYLE_POLICY: dict[str, dict] = {
    "default":      {"bias": 1.0},
    "professional": {"bias": 0.3,
                     "allow": {"agreement", "thinking", "realization",
                               "relief", "breathing", "curiosity"}},
    "calm":         {"bias": 0.6,
                     "allow": {"agreement", "thinking", "pleasure",
                               "realization", "relief", "sigh",
                               "breathing", "humming", "curiosity",
                               "fatigue", "warmth"}},
    "warm":         {"bias": 0.85},
    "playful":      {"bias": 1.3,
                     "prefer": {"giggle", "hehe", "chuckle"}},
    "nerdy":        {"bias": 0.9,
                     "prefer": {"hmm", "aha", "heh"}},
    "sassy":        {"bias": 1.0,
                     "prefer": {"heh", "scoff", "tsk"}},
    "rude":         {"bias": 0.7,
                     "prefer": {"scoff", "pfft", "hmph", "tsk"}},
    "flirty":       {"bias": 1.1, "adult_ok": True},
    "raunchy":      {"bias": 1.05, "adult_ok": True},
}

LEVELS = ("off", "minimal", "natural", "expressive")
_LEVEL_BIAS = {"off": 0.0, "minimal": 0.5, "natural": 1.0,
               "expressive": 1.5}
_LEVEL_CAP = {"off": 0, "minimal": 1, "natural": 2, "expressive": 3}
# Per-sentence hard cap — vocalizations never stack inside one sentence.
_SENTENCE_CAP = {"off": 0, "minimal": 1, "natural": 1, "expressive": 2}
# Minimum spoken sentences between kept vocalizations.
_SPACING = {"off": 99, "minimal": 3, "natural": 2, "expressive": 1}
# "minimal" keeps only quiet, sub-intensity sounds.
_MINIMAL_MAX_INTENSITY = 0.4

_MOOD_BIAS = {"focused": 0.5, "serious": 0.5, "relaxed": 1.0,
              "excited": 1.3, "celebratory": 1.35, "concerned": 0.8,
              "curious": 1.1}
# Concerned mood only voices empathetic/thinking sounds.
_MOOD_ALLOW = {"concerned": {"thinking", "sympathy", "sigh", "breathing",
                             "agreement", "sadness", "realization"}}


def clean_level(raw: object) -> str:
    v = str(raw or "").strip().lower()
    return v if v in LEVELS else "natural"


# ---------------------------------------------------------------------------
# TTS adapters — semantic Vocalization → engine-safe rendering
# ---------------------------------------------------------------------------

class VocalizationAdapter:
    """Engine-specific rendering contract. ``render`` returns speakable
    text for a Vocalization, or None to drop it — natural silence beats
    bad synthesis."""
    name = "base"

    def render(self, voc: Vocalization, *, adult: bool = False) -> str | None:
        raise NotImplementedError


# Kokoro / espeak-ng G2P: vowel-less spellings letterize ("mmm" →
# "em-em-em"), so every render form stays dictionary- or phoneme-safe.
# Adult variants are softer/drawn-out respellings gated by the policy.
_KOKORO_FORMS: dict[str, tuple[str, str | None]] = {
    # style: (standard form, adult variant or None)
    "mm_hmm": ("mm-hmm.", None),
    "mm_affirm": ("mm-hmm.", "hmm-mm…"),
    "uh_huh": ("uh-huh.", None),
    "mm_mm": ("mm-mm.", None),
    "uh_uh": ("uh-uh.", None),
    "hmm": ("hmm…", "hmmm…"),
    "um": ("um…", None),
    "uhh": ("uhh…", None),
    "erm": ("erm…", None),
    "hm": ("hm.", "hmm…"),
    "mmm_pleased": ("hmmm…", "hmm, hmmm…"),
    "aha": ("aha!", "ahh-ha…"),
    "ah": ("ahh…", "ahhh…"),
    "oh": ("oh.", "ohh…"),
    "gasp": ("ah!", "ahh…!"),
    "ooh": ("ooh!", "oohh…"),
    "whoa": ("whoa.", None),
    "wow": ("wow.", None),
    "huh": ("huh?", None),
    "eh": ("eh.", None),
    "phew": ("phew.", "hhahh…"),
    "ugh": ("ugh.", "uhh…"),
    "oof": ("oof.", None),
    "ow": ("ow!", None),
    "ngh": ("ungh.", None),
    "hup": ("hup!", None),
    "tsk": ("tsk tsk.", "tch…"),
    "scoff": ("humph.", "hh, please."),
    "pfft": ("pft.", None),
    "meh": ("meh.", None),
    "aww": ("aww.", "awww…"),
    "sniff": ("sniff.", None),
    "haha": ("ha ha.", None),
    "hehe": ("hee hee.", "hee hee…"),
    "heh": ("heh.", "hh-heh…"),
    "hihi": ("hee hee.", None),
    "oops": ("oops.", None),
    "yay": ("yay!", None),
    "woo": ("woo!", None),
    "whee": ("whee!", None),
    "yawn": ("haah…", None),
    "ahem": ("ahem.", None),
    "psst": ("pssst.", None),
    "sigh": ("ahhh…", "hhh-ahh…"),
    "breath": ("hah…", None),
    # stage-direction styles
    "sigh_soft": ("ahh…", "hhh-ahh…"),
    "sigh_deep": ("ahhh…", "haahh…"),
    "sigh_relieved": ("ahhh… phew.", "hhahhh…"),
    "sigh_tired": ("haaah…", None),
    "sigh_frustrated": ("ugh, ahhh…", None),
    "chuckle": ("heh heh.", "hh, heh…"),
    "laugh": ("ha ha!", None),
    "soft_laugh": ("heh, ha.", "hmm, heh…"),
    "giggle": ("hee hee.", "hih, hee…"),
    "snicker": ("heh heh.", None),
    "sharp_inhale": ("ah!", None),
    "inhale": ("hmm…", None),
    "deep_breath": ("hah…", None),
    "exhale": ("hah…", "hhh…"),
    "long_exhale": ("haahh…", "hhhh…"),
    "throat_clear": ("ahem.", None),
    "hum": ("hmm hmm…", "hmmm…"),
    "smile": ("hmm.", "hmm…"),
    "scoff_stage": ("humph.", None),
    "groan": ("ughhh.", None),
    "soft_moan": ("hmm-mm…", "hmm, mmm…"),
    "shiver": ("brr.", None),
    "whistle": ("hmm hmm.", None),
    "pause": ("…", None),
    "nervous": ("hmm…", None),
}


class KokoroVocalizationAdapter(VocalizationAdapter):
    """Kokoro-82M rendering table — vowel-bearing respellings that the
    espeak G2P voices as interjections rather than letter strings."""

    name = "kokoro"

    def render(self, voc: Vocalization, *, adult: bool = False) -> str | None:
        forms = _KOKORO_FORMS.get(voc.style)
        if not forms:
            # Unknown style — keep a safe generic fallback by category.
            fallback = {"agreement": "mm-hmm.", "thinking": "hmm…",
                        "amusement": "heh.", "sigh": "ahh…",
                        "breathing": "hah…"}
            return fallback.get(voc.category)
        standard, adult_form = forms
        if adult and adult_form:
            return adult_form
        return standard


_ADAPTERS: dict[str, Callable[[], VocalizationAdapter]] = {
    "kokoro": KokoroVocalizationAdapter,
}


def adapter_for(engine_name: str) -> VocalizationAdapter:
    """Adapter per TTS engine name; unknown engines get the Kokoro
    respelling table (safe vowel-bearing text works broadly)."""
    factory = _ADAPTERS.get(str(engine_name or "").lower())
    return (factory or KokoroVocalizationAdapter)()


# ---------------------------------------------------------------------------
# Engine — detection → policy → resolution → events
# ---------------------------------------------------------------------------

# Canonicalization pass used by SpeechTextFilter: raw spellings (incl.
# all-caps) → the lowercase token the detector knows. Covers exactly the
# letterizable danger tokens; anything else passes through.
_CANON_RE = re.compile(
    r"\b("
    r"m+h+m+|m+[-\s]m+|m{3,}n?|h+m{2,}|"
    r"(?:uh|nuh)[-\s]?uh|(?:uh|nuh)[-\s]?huh|uhhuh|"
    r"(?:ha){2,}h?|(?:ba){2,}|ahahaha|(?:he){2,}h?|(?:eh){2,}|"
    r"hee+|hih*i+|tee[-\s]?hee|"
    r"u+g+h+|a+r+g+h+|g+r+|o+f+|o+w+|o+u+c+h+|"
    r"a+h{2,}|o+h{2,}|o{2,}h+[!?]?|a+h+a+[!?]?|"
    r"w+h+o+a+|w+o+w+|y+a+y+|w+o+h*o+|w+h+e+|e+k+|"
    r"y+a+w+n+|a+w{2,}|s+n+i+f+f*l*e*s*|"
    r"p+f+t+|t+s+k+|t+c+h+|p+s+t+|h+m+p+h+|h+u+m+p+h+|"
    r"m+e+h+|h+u+h+|e+h+|n+g+h+|u+n+g+h+|h+u+p+|"
    r"p+h+e+w+|a+h+e+m+|o+p+s+|w+h+o+p+s+|"
    r"g+a+s+p+s*|s+c+o+f+f+s*|g+r+o+a+n+s*|m+o+a+n+s*|"
    r"s+i+g+h+s*|l+a+u+g+h+s*|c+h+u+c+k+l+e+s*|g+i+g+g+l+e+s*|"
    r"s+n+i+c+k+e+r+s*|w+h+i+s+t+l+e+s*|s+h+i+v+e+r+s*|b+r{2,}"
    r")\b",
    re.I)


def canonicalize_vocals(text: str) -> str:
    """Lowercase-canonicalize raw vocalization spellings ("MMM"→"mmm",
    "HAHA"→"haha", "MMHMM"→"mmhmm") so the detector sees one surface
    family per sound. Runs inside SpeechTextFilter._pronounce — safe
    because detection (and the final render) happens downstream."""
    def _canon(m: re.Match) -> str:
        raw = m.group(0).lower()
        compact = re.sub(r"[^a-z]", "", raw)
        if re.fullmatch(r"m+h+m+", compact):          # mhm, mmhmm, mm-hmm
            return "mm-hmm"
        if ("-" in raw or " " in raw) \
                and re.fullmatch(r"m+m+", compact):   # mm-mm, mm mm
            return "mm-mm"
        if re.fullmatch(r"m{2,}n?", compact):         # mmm, mmmm
            return "mmm" if len(compact) <= 3 else "mmmm"
        if re.fullmatch(r"h+m+", compact):            # hmm, hmmm
            return "hmm" if len(compact) <= 3 else "hmmm"
        if re.fullmatch(r"(?:u+|n+u+)h+u+h+", compact):
            return "uh-uh"                            # uh-uh, nuh-uh
        if compact.endswith("huh") and compact[:1] in ("u", "n"):
            return "uh-huh"                           # uh-huh, uhhuh
        return compact
    return _CANON_RE.sub(_canon, text)


_STAGE_RE = re.compile(r"(\*\s*([a-z' -]{2,40})\s*\*|\(\s*([a-z' -]{2,40})\s*\))")

_TOKEN_RE = re.compile(
    r"(?<![\w'])(" + "|".join(
        pat for _s, _c, _i, pat, _a in _TOKENS) + r")(?![\w'])",
    re.I)

# Word-level tokens that are also ordinary prose words ("a sigh of
# relief", "take a breath") only count when they stand alone as an
# interjection — line start or after sentence punctuation.
_STANDALONE_WORDS = {"sigh", "breath"}


# User-message → candidate lead vocalization categories. A lead is a
# spoken-only reaction prepended to the first sentence of the response —
# display text never changes.
_LEAD_TRIGGERS: list[tuple[re.Pattern, list[tuple[str, str, float]]]] = [
    (re.compile(r"\b(?:finally|at last|it works|worked|fixed it|"
                r"that fixed|sorted|solved)\b", re.I),
     [("chuckle", "amusement", 0.4), ("sigh_relieved", "relief", 0.5),
      ("mmm_pleased", "pleasure", 0.45)]),
    (re.compile(r"\b(?:figured it out|i did it|got it working|nailed it)\b",
                re.I),
     [("soft_laugh", "amusement", 0.45), ("aha", "realization", 0.4)]),
    (re.compile(r"\b(?:crash(?:ed|ing)?|broke|broken|failed again|"
                r"error again|still failing)\b", re.I),
     [("hmm", "thinking", 0.4), ("sigh_frustrated", "frustration", 0.45)]),
    (re.compile(r"\b(?:haha|lol|funny|hilarious)\b", re.I),
     [("chuckle", "amusement", 0.5), ("hehe", "playfulness", 0.4)]),
    (re.compile(r"\b(?:thank|thanks|appreciate|love you|missed you)\b",
                re.I),
     [("mmm_pleased", "pleasure", 0.4), ("soft_laugh", "warmth", 0.35)]),
    (re.compile(r"\b(?:rough day|so tired|exhausted|sad|upset|"
                r"frustrated with)\b", re.I),
     [("aww", "sympathy", 0.45), ("sigh_soft", "sympathy", 0.4)]),
]


@dataclass
class _TaskBudget:
    used: int = 0
    sentences_since: int = 9          # sentences since last vocalization
    lead: list[tuple[str, str, float]] | None = None
    lead_used: bool = False
    gestures_used: int = 0


class VocalizationEngine:
    """Policy + resolution core. Per-profile history is bounded; per-task
    budget bounds a single response. ``publish`` receives ('voice'|'gesture',
    payload) events exactly like VoiceManager's bus."""

    def __init__(self, adapter: VocalizationAdapter | None = None,
                 publish: Callable[[str, dict], None] | None = None,
                 rng: random.Random | None = None,
                 history_size: int = 24) -> None:
        self.adapter = adapter or KokoroVocalizationAdapter()
        self._publish = publish or (lambda _k, _p: None)
        self._rng = rng or random.Random()
        self._tasks: dict[str, _TaskBudget] = {}
        self._history: dict[str, deque] = {}
        self._history_size = max(4, int(history_size))
        self._suppress: dict[str, dict[str, float]] = {}  # profile→cat→until
        self.stats: deque = deque(maxlen=100)              # telemetry tail

    # -- task lifecycle ---------------------------------------------------

    def begin_task(self, task_id: str, user_text: str = "") -> None:
        budget = _TaskBudget()
        budget.lead = self._pick_lead(user_text)
        self._tasks[task_id] = budget
        if len(self._tasks) > 64:  # bound abandoned tasks
            for k in list(self._tasks)[:-32]:
                self._tasks.pop(k, None)

    def end_task(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)

    def _pick_lead(self, user_text: str) -> list[tuple[str, str, float]] | None:
        text = str(user_text or "").strip()
        if not text or len(text) > 500:
            return None
        for rx, candidates in _LEAD_TRIGGERS:
            if rx.search(text):
                return list(candidates)
        return None

    # -- history -----------------------------------------------------------

    def _hist(self, profile_id: str) -> deque:
        key = profile_id or "_"
        if key not in self._history:
            self._history[key] = deque(maxlen=self._history_size)
        if len(self._history) > 32:
            for k in list(self._history)[:-16]:
                self._history.pop(k, None)
        return self._history[key]

    def record_feedback(self, profile_id: str, text: str) -> None:
        """'use fewer sighs' / 'that laugh sounded weird' → bounded
        per-category suppression window on the profile."""
        t = str(text or "").lower()
        now = time.time()
        sup = self._suppress.setdefault(profile_id or "_", {})
        targets = []
        if re.search(r"(fewer|less|stop|no more|too many)\s+\w*\s*sigh", t):
            targets.append("sigh")
        if re.search(r"(fewer|less|stop|weird|no more)\s+\w*\s*laugh", t):
            targets.append("amusement")
        if re.search(r"(fewer|less|stop)\s+\w*\s*hum", t):
            targets.extend(("pleasure", "humming", "thinking"))
        if "vocalization" in t and re.search(r"(off|stop|fewer|less)", t):
            targets.extend(_GESTURE)
        for cat in targets:
            sup[cat] = now + 3600.0

    # -- context ------------------------------------------------------------

    @staticmethod
    def _ctx(ctx: dict | None) -> dict:
        ctx = ctx or {}
        return {
            "style": str(ctx.get("style") or "default"),
            "strength": max(0, min(100, int(ctx.get("strength") or 0))),
            "mood": str(ctx.get("mood") or ""),
            "is_adult": bool(ctx.get("is_adult")),
            "level": clean_level(ctx.get("level")),
            "profile_id": str(ctx.get("profile_id") or ""),
        }

    def _keep_prob(self, voc: Vocalization, c: dict, hist: deque) -> float:
        policy = _STYLE_POLICY.get(c["style"], _STYLE_POLICY["default"])
        allow = policy.get("allow")
        if allow is not None and voc.category not in allow:
            return 0.0
        mood_allow = _MOOD_ALLOW.get(c["mood"])
        if mood_allow is not None and voc.category not in mood_allow:
            return 0.0
        sup = self._suppress.get(c["profile_id"] or "_", {})
        if sup.get(voc.category, 0.0) > time.time():
            return 0.0
        if voc.adult and not (c["is_adult"] and policy.get("adult_ok")):
            return 0.0
        prob = (policy.get("bias", 1.0)
                * _LEVEL_BIAS[c["level"]]
                * _MOOD_BIAS.get(c["mood"], 1.0)
                * (c["strength"] / 70.0))
        # Repetition suppression — same style last turn or same category
        # very recently pushes probability down hard.
        recent = list(hist)
        if recent and recent[-1].get("style") == voc.style:
            prob *= 0.1
        elif any(h.get("category") == voc.category for h in recent[-2:]):
            prob *= 0.4
        if policy.get("prefer") and voc.style not in policy["prefer"] \
                and voc.category in ("amusement", "playfulness"):
            prob *= 0.7   # off-signature laugh is rarer for typed personas
        return max(0.0, min(1.0, prob))

    # -- detection -----------------------------------------------------------

    def detect(self, text: str) -> list[re.Match | dict]:
        """Ordered hits: stage directions + canonical tokens. Returns
        (start, end, Vocalization) tuples sorted by position."""
        out = []
        for m in _STAGE_RE.finditer(text):
            inner = (m.group(2) or m.group(3) or "").strip().lower()
            inner = re.sub(r"\s+", " ", inner)
            spec = _STAGE.get(inner)
            if spec:
                st, cat, inten = spec
                out.append((m.start(1), m.end(1), Vocalization(
                    cat, st, inten, m.group(1))))
        for m in _TOKEN_RE.finditer(text):
            spec = None
            for _s, _c, _i, pat, _a in _TOKENS:
                if re.fullmatch(pat, m.group(1), re.I):
                    spec = (_s, _c, _i, _a)
                    break
            if not spec:
                continue
            st, cat, inten, adult = spec
            # "a sigh of relief" is prose — only standalone placements
            # (line start / after sentence punctuation) count.
            if st in _STANDALONE_WORDS:
                head = text[: m.start(1)].rstrip()
                if head and not head.endswith(
                        (".", "!", "?", "…", ",", "—", ":", ";", '"', "(")):
                    continue
            out.append((m.start(1), m.end(1), Vocalization(
                cat, st, inten, m.group(1), adult)))
        out.sort(key=lambda x: x[0])
        return out

    # -- resolution -----------------------------------------------------------

    def resolve(self, text: str, *, task_id: str = "",
                ctx: dict | None = None) -> ResolveResult:
        """Rewrite vocalization tokens in a speakable sentence into the
        adapter's render form, applying persona policy. ``display_text``
        is always the untouched input."""
        res = ResolveResult(speech_text=text, display_text=text)
        c = self._ctx(ctx)
        hits = self.detect(text)
        if task_id:
            budget = self._tasks.setdefault(task_id, _TaskBudget())
            if len(self._tasks) > 64:
                for k in list(self._tasks)[:-32]:
                    self._tasks.pop(k, None)
        else:
            budget = _TaskBudget()
        if not hits and not (budget.lead and not budget.lead_used):
            if task_id:
                budget.sentences_since += 1
            return res

        hist = self._hist(c["profile_id"])
        level = c["level"]
        cap = _LEVEL_CAP[level]
        sent_cap = _SENTENCE_CAP[level]
        spacing = _SPACING[level]

        out = text
        kept_this_sentence = 0
        # Replace right-to-left so offsets stay valid.
        for start, end, voc in reversed(hits):
            decision = {"input": voc.token, "semantic": voc.style,
                        "category": voc.category}
            keep = (kept_this_sentence < sent_cap
                    and budget.used < cap
                    and budget.sentences_since >= spacing
                    and (level != "minimal"
                         or voc.intensity <= _MINIMAL_MAX_INTENSITY)
                    and self._rng.random() < self._keep_prob(voc, c, hist))
            if keep:
                form = self.adapter.render(
                    voc, adult=c["is_adult"]
                    and bool(_STYLE_POLICY.get(c["style"], {})
                             .get("adult_ok")))
                if form is None:
                    keep = False
            if keep:
                kept_this_sentence += 1
                budget.used += 1
                budget.sentences_since = 0
                hist.append({"style": voc.style, "category": voc.category,
                             "ts": time.time()})
                out = out[:start] + form + out[end:]
                decision["tts_form"] = form
                res.decisions.append(decision)
                self._emit_gesture(voc, res, budget)
                self._emit_telemetry(task_id, c, decision, kept=True)
            else:
                # Dropped vocalizations leave the surrounding prose
                # readable — strip the token plus a stranded stage
                # wrapper, then tidy doubled punctuation/space.
                out = out[:start] + " " + out[end:]
                decision["tts_form"] = None
                res.decisions.append(decision)
                self._emit_telemetry(task_id, c, decision, kept=False)
        out = re.sub(r"\s+([,.!?…])", r"\1", out)
        out = re.sub(r"([,.!?…])\s*([,.!?…])", r"\1\2", out)
        out = re.sub(r"([,.!?…])\1+", r"\1", out)
        out = re.sub(r"\.([!?])", r"\1", out)   # "heh.!" → "heh!"
        out = re.sub(r"\.(?=,)", "", out)       # "mm-hmm.," → "mm-hmm,"
        out = re.sub(r"^[,.!?…\s]+", "", out)
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"\n\s+", "\n", out)
        out = out.strip()

        # Lead reaction from the user's message — prepended to the first
        # sentence only, spoken-only (display never gains text). Pick the
        # first candidate this persona/mood may actually voice.
        if task_id and budget.lead and not budget.lead_used \
                and budget.used < cap and budget.sentences_since >= spacing \
                and out:
            candidates = budget.lead
            budget.lead_used = True
            voc = None
            for style, cat, inten in candidates:
                cand = Vocalization(cat, style, inten, f"<lead:{style}>")
                if self._keep_prob(cand, c, hist) > 0.0:
                    voc = cand
                    break
            if voc and self._rng.random() < self._keep_prob(voc, c, hist):
                form = self.adapter.render(
                    voc, adult=c["is_adult"]
                    and bool(_STYLE_POLICY.get(c["style"], {})
                             .get("adult_ok")))
                if form:
                    budget.used += 1
                    hist.append({"style": style, "category": cat,
                                 "ts": time.time()})
                    out = form + " " + out
                    res.decisions.append({"input": voc.token,
                                          "semantic": style,
                                          "category": cat,
                                          "tts_form": form})
                    self._emit_gesture(voc, res, budget)
                    self._emit_telemetry(task_id, c, {
                        "input": voc.token, "semantic": style,
                        "category": cat, "tts_form": form}, kept=True)
        if task_id and task_id in self._tasks:
            self._tasks[task_id].sentences_since += 1
        res.speech_text = out
        return res

    def _emit_gesture(self, voc: Vocalization, res: ResolveResult,
                      budget: _TaskBudget) -> None:
        gesture = _GESTURE.get(voc.category)
        if not gesture or budget.gestures_used >= 2:
            return
        budget.gestures_used += 1
        ev = {"type": "gesture", "gesture": gesture,
              "intensity": round(voc.intensity, 2),
              "source": voc.style, "ts": time.time()}
        res.events.append(ev)
        try:
            self._publish("gesture", ev)
        except Exception:
            pass

    def _emit_telemetry(self, task_id: str, c: dict, decision: dict,
                        *, kept: bool) -> None:
        rec = dict(decision)
        rec.update({"persona": c["style"], "level": c["level"],
                    "kept": kept})
        self.stats.append(rec)
        try:
            self._publish("voice", {"event": "vocalization",
                                    "task_id": task_id, **rec})
        except Exception:
            pass

    # -- introspection -------------------------------------------------------

    def styles(self, *, is_adult: bool = False) -> list[dict]:
        """Preview catalog for Personality Studio — every renderable style
        with a human label and a sample input that resolves to it."""
        samples = {
            "mm_hmm": "Mm-hmm, I agree.", "hmm": "Hmm… let me think.",
            "mmm_pleased": "Mmm… that's good.", "aha": "Ahh, I see.",
            "ooh": "Ooh, that's interesting.", "heh": "Heh, fair enough.",
            "ugh": "Ugh, that's annoying.", "aww": "Aww, that's sweet.",
            "chuckle": "Heh heh, nice one.", "giggle": "Hee hee!",
            "sigh_soft": "*sighs softly* Okay.",
            "sigh_relieved": "*sighs in relief* Finally.",
            "gasp": "*gasps* Really?", "throat_clear": "Ahem — anyway.",
            "yawn": "*yawns* Long day.", "phew": "Phew, that was close.",
            "scoff": "*scoffs* Sure.", "wow": "Wow.",
        }
        out = []
        for style, (form, adult_form) in sorted(_KOKORO_FORMS.items()):
            if style.startswith("_"):
                continue
            out.append({"style": style, "preview": samples.get(style, ""),
                        "render": form, "adult_render": adult_form
                        if (is_adult and adult_form) else None})
        return out

    def status(self) -> dict:
        return {"adapter": self.adapter.name,
                "recent": list(self.stats)[-10:],
                "profiles": len(self._history)}
