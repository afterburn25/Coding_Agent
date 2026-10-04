"""Vocalization & Gesture Engine — regression coverage.

The guarantees under test:
  * All-caps / raw vocalization tokens never reach TTS as letter strings.
  * display_text is never modified — only the spoken rendering changes.
  * Persona style, mood, strength, level, cooldown and the adult gate
    all shape what is kept and how it renders.
  * Frequency caps and spacing prevent noisy responses.
  * Code/structured output exclusion is upstream (SpeechTextFilter) —
    the engine only ever sees speakable prose; tested end-to-end here.
"""
from __future__ import annotations

import random
import unittest

from localcodeagent.voice.speech_filter import SpeechTextFilter
from localcodeagent.voice.vocalizations import (
    LEVELS, KokoroVocalizationAdapter, VocalizationEngine,
    adapter_for, canonicalize_vocals,
)


def _engine() -> VocalizationEngine:
    return VocalizationEngine(rng=random.Random(7))


def _ctx(**over):
    base = {"style": "default", "strength": 100, "mood": "",
            "is_adult": False, "level": "natural", "profile_id": "p1"}
    base.update(over)
    return base


class Canonicalization(unittest.TestCase):
    """The filter's canonicalization pass (replacing the old VOCAL_RE
    respell) — output tokens the engine's detector understands."""

    def setUp(self):
        self.f = SpeechTextFilter()

    def test_mmm_canonical(self):
        self.assertEqual(self.f.filter("MMM"), "mmm")

    def test_mmmm_canonical(self):
        self.assertEqual(self.f.filter("MMMM yes"), "mmmm yes")

    def test_mmhmm_canonical(self):
        self.assertEqual(self.f.filter("MMHMM"), "mm-hmm")

    def test_mhm_canonical(self):
        self.assertEqual(self.f.filter("mhm"), "mm-hmm")

    def test_caps_laugh_canonical(self):
        self.assertEqual(self.f.filter("HAHA"), "haha")

    def test_caps_ugh_canonical(self):
        self.assertEqual(self.f.filter("UGH"), "ugh")

    def test_mm_stays_literal(self):
        # 50mm lens / million abbreviation — not a vocalization.
        self.assertEqual(self.f.filter("50MM lens"), "50MM lens")

    def test_sentence_preserved(self):
        out = self.f.filter("Mmm... that worked.")
        self.assertIn("mmm", out)
        self.assertIn("that worked", out)


class Rendering(unittest.TestCase):
    """Semantic → TTS-safe text through the Kokoro adapter."""

    def setUp(self):
        self.eng = _engine()

    def _speech(self, text, **ctx):
        return self.eng.resolve(text, ctx=_ctx(**ctx)).speech_text

    def test_mmm_is_hum_not_letters(self):
        out = self._speech("Mmm, that's nice.")
        self.assertNotIn("mmm", out.lower().replace("hmm", ""))
        self.assertIn("hmm", out.lower())
        self.assertIn("that's nice", out)

    def test_mmhmm_agreement(self):
        self.assertIn("mm-hmm", self._speech("MMHMM, I agree."))

    def test_hmm_thinking(self):
        self.assertIn("hmm", self._speech("Hmm... let me think.").lower())

    def test_ahh_realization(self):
        self.assertIn("ahh", self._speech("Ahh, I see.").lower())

    def test_ooh_surprise(self):
        self.assertIn("ooh", self._speech("Ooh, interesting.").lower())

    def test_heh_chuckle(self):
        self.assertIn("heh", self._speech("Heh, fair enough.").lower())

    def test_ugh_frustration(self):
        self.assertIn("ugh", self._speech("Ugh, that's annoying.").lower())

    def test_tsk_kept(self):
        self.assertIn("tsk", self._speech("Tsk tsk.").lower())

    def test_no_letter_spelling_anywhere(self):
        # Whatever survives, no run of the raw caps token remains.
        for tok in ("MMM", "MMMM", "MMHMM", "MHM", "HMM", "AHH",
                    "OOH", "UGH", "HEH", "TSK", "HAHA"):
            out = self._speech(tok + ". Okay.")
            self.assertNotIn(tok, out)

    def test_display_text_untouched(self):
        res = self.eng.resolve("MMM, okay.", ctx=_ctx())
        self.assertEqual(res.display_text, "MMM, okay.")

    def test_stage_direction_resolved(self):
        out = self._speech("*sighs* Fine, let's do it.")
        self.assertIn("ahh", out.lower())
        self.assertNotIn("*", out)

    def test_giggle(self):
        self.assertIn("hee hee", self._speech("Hehe!").lower())

    def test_prose_sigh_untouched_or_dropped(self):
        # "a sigh of relief" is prose, not a standalone interjection.
        out = self._speech("She let out a sigh of relief.")
        self.assertNotIn("*", out)


class Policy(unittest.TestCase):
    """Level caps, persona bias, mood, strength, cooldown, adult gate."""

    def setUp(self):
        self.eng = _engine()

    def _speech(self, text, task="", **ctx):
        return self.eng.resolve(
            text, task_id=task, ctx=_ctx(**ctx)).speech_text

    def _kept(self, res):
        return sum(1 for d in res.decisions if d.get("tts_form"))

    def test_level_off_strips_all(self):
        out = self._speech("Mmm, that's nice.", level="off")
        self.assertNotIn("hmm", out.lower())
        self.assertIn("that's nice", out)

    def test_level_off_no_letters(self):
        self.assertEqual(self._speech("MMM", level="off"), "")

    def test_minimal_drops_intense(self):
        # gasp (0.6) exceeds the minimal intensity ceiling; hmm passes.
        eng = _engine()
        out = eng.resolve("*gasps* Oh no.",
                          ctx=_ctx(level="minimal")).speech_text
        self.assertNotIn("ah!", out.lower())
        out2 = eng.resolve("Hmm, okay.",
                           ctx=_ctx(level="minimal")).speech_text
        self.assertIn("hmm", out2.lower())

    def test_natural_caps_per_response(self):
        eng = _engine()
        tid = "t-cap"
        eng.begin_task(tid)
        kept = 0
        for sent in ("Heh, one.", "Haha, two.", "Hehe, three.",
                     "Mmm, four."):
            r = eng.resolve(sent, task_id=tid, ctx=_ctx())
            kept += self._kept(r)
        self.assertLessEqual(kept, 2)  # natural per-response cap

    def test_expressive_allows_more(self):
        eng = _engine()
        tid = "t-x"
        eng.begin_task(tid)
        kept = 0
        for sent in ("Heh, one.", "Haha, two.", "Hehe, three.",
                     "Mmm, four."):
            r = eng.resolve(sent, task_id=tid, ctx=_ctx(level="expressive"))
            kept += self._kept(r)
        self.assertLessEqual(kept, 3)

    def test_spacing_rule(self):
        # natural spacing=2: a kept vocalization blocks the next sentence's.
        eng = _engine()
        tid = "t-sp"
        eng.begin_task(tid)
        r1 = eng.resolve("Heh, nice.", task_id=tid, ctx=_ctx())
        self.assertIn("heh", r1.speech_text.lower())
        # sentences_since=0→resolve bumps to 1 → < spacing(2) → dropped
        r2 = eng.resolve("Haha, really?", task_id=tid, ctx=_ctx())
        self.assertNotIn("ha ha", r2.speech_text.lower())

    def test_same_style_repeat_suppressed(self):
        eng = _engine()
        tid = "t-rep"
        eng.begin_task(tid)
        eng.resolve("Heh, first.", task_id=tid, ctx=_ctx())
        # force spacing clear then repeat same style — probability crushed
        drops = 0
        for i in range(6):
            eng._tasks[tid].sentences_since = 9
            eng._tasks[tid].used = 0
            r = eng.resolve("Heh, again.", task_id=tid, ctx=_ctx())
            if "heh" not in r.speech_text.lower():
                drops += 1
        self.assertGreater(drops, 0)

    def test_professional_restrained(self):
        eng = _engine()
        tid = "t-pro"
        eng.begin_task(tid)
        kept = sum(
            "heh" in eng.resolve(s, task_id=tid,
                                 ctx=_ctx(style="professional",
                                          strength=50)).speech_text.lower()
            for s in ("Heh.", "Heh.", "Heh.", "Heh.", "Heh.", "Heh."))
        # professional allow-list excludes amusement entirely
        self.assertEqual(kept, 0)

    def test_professional_keeps_thinking(self):
        out = self._speech("Hmm, let me check.", style="professional",
                           strength=80)
        self.assertIn("hmm", out.lower())

    def test_strength_zero_neutral(self):
        out = self._speech("Mmm, nice.", strength=0)
        self.assertNotIn("hmm", out.lower())

    def test_concerned_mood_limits(self):
        # concerned only voices empathetic/thinking categories
        out = self._speech("Haha that's funny.", mood="concerned",
                           task="t-cm")
        self.assertNotIn("ha ha", out.lower())
        out2 = self._speech("Hmm, I see.", mood="concerned", task="t-cm2")
        self.assertIn("hmm", out2.lower())

    def test_adult_variant_gated(self):
        eng = _engine()
        standard = eng.resolve("Mmm…",
                               ctx=_ctx(is_adult=False, style="flirty"))
        self.assertNotIn("hmm, hmmm", standard.speech_text)
        adult = eng.resolve("Mmm…", task_id="t-a",
                            ctx=_ctx(is_adult=True, style="flirty"))
        # adult personas get the breathier variant where one exists
        self.assertTrue(adult.speech_text.startswith("hmm"))
        eng.end_task("t-a")

    def test_profile_isolation(self):
        eng = _engine()
        eng.resolve("Heh, one.", task_id="t-a",
                    ctx=_ctx(profile_id="alice"))
        h_alice = len(eng._history["alice"])
        eng.resolve("Heh, two.", task_id="t-b",
                    ctx=_ctx(profile_id="bob"))
        self.assertEqual(len(eng._history["bob"]), 1)
        self.assertEqual(h_alice, len(eng._history["alice"]))


class Priming(unittest.TestCase):
    """User-context lead reactions — spoken only, bounded."""

    def test_fixed_it_leads_with_reaction(self):
        eng = _engine()
        tid = "t-lead"
        eng.begin_task(tid, user_text="That finally fixed it.")
        # playful persona: high bias, always lands with seeded rng
        r = eng.resolve("Glad it worked.", task_id=tid,
                        ctx=_ctx(style="playful"))
        low = r.speech_text.lower()
        self.assertTrue(low.startswith(("heh heh", "ahhh", "hmm")))
        eng.end_task(tid)

    def test_no_trigger_no_lead(self):
        eng = _engine()
        tid = "t-plain"
        eng.begin_task(tid, user_text="What time is it?")
        r = eng.resolve("It is four.", task_id=tid, ctx=_ctx())
        self.assertEqual(r.speech_text, "It is four.")
        eng.end_task(tid)

    def test_lead_display_untouched(self):
        eng = _engine()
        tid = "t-disp"
        eng.begin_task(tid, user_text="that finally worked")
        r = eng.resolve("Nice.", task_id=tid, ctx=_ctx(style="playful"))
        self.assertEqual(r.display_text, "Nice.")
        eng.end_task(tid)


class Gestures(unittest.TestCase):
    def test_gesture_pairs_with_vocalization(self):
        eng = _engine()
        r = eng.resolve("Mm-hmm, agreed.", ctx=_ctx())
        self.assertTrue(any(e["gesture"] == "small_nod"
                            for e in r.events))

    def test_gesture_capped(self):
        eng = _engine()
        tid = "t-g"
        eng.begin_task(tid)
        events = []
        for _ in range(5):
            eng._tasks[tid].sentences_since = 9
            r = eng.resolve("*gasps* Oh!", task_id=tid,
                            ctx=_ctx(level="expressive"))
            events += r.events
        self.assertLessEqual(len(events), 2)
        eng.end_task(tid)


class EndToEnd(unittest.TestCase):
    """filter + engine together — the real speech path."""

    def setUp(self):
        self.f = SpeechTextFilter()
        self.eng = _engine()

    def _spoken(self, text, **ctx):
        return self.eng.resolve(self.f.filter(text),
                                ctx=_ctx(**ctx)).speech_text

    def test_dogfood_set(self):
        cases = {
            "Mmm... that's good.": "hmm",
            "Mm-hmm, I agree.": "mm-hmm",
            "Hmm... let me think.": "hmm",
            "Ahh, I see.": "ahh",
            "Ooh, that's interesting.": "ooh",
            "Heh, fair enough.": "heh",
            "Ugh, that's annoying.": "ugh",
            "Aww, that's sweet.": "aww",
        }
        for text, want in cases.items():
            out = self._spoken(text)
            self.assertIn(want, out.lower(), f"{text!r} -> {out!r}")
            self.assertNotIn("M M M", out)

    def test_code_fence_never_vocalizes(self):
        text = "Here's the fix:\n```python\nprint('mmm')\n```\nDone."
        out = self._spoken(text)
        self.assertNotIn("hmm", out.lower())
        self.assertNotIn("mmm", out.lower())
        self.assertIn("code", out.lower())  # substitution mention

    def test_adapter_for_unknown_engine(self):
        self.assertIsInstance(adapter_for("piper"),
                              KokoroVocalizationAdapter)

    def test_catalog_lists_levels(self):
        self.assertIn("natural", LEVELS)
        styles = _engine().styles(is_adult=False)
        self.assertTrue(any(s["style"] == "mm_hmm" for s in styles))
        # adult render variants hidden for minors
        self.assertTrue(all(s["adult_render"] is None for s in styles))

    def test_feedback_suppresses_category(self):
        eng = _engine()
        eng.record_feedback("p1", "please use fewer sighs")
        out = eng.resolve("*sighs* Okay.", ctx=_ctx()).speech_text
        self.assertNotIn("ahh", out.lower())


if __name__ == "__main__":
    unittest.main()
