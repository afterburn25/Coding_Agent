"""Realization-layer regression suite — the "same meaning, new words"
contract. Covers the §42 direct-failure regression (wrapper-only
variation), greeting/farewell soaks, repeated-question battery,
protected spans, negation and confidence preservation."""
import random
import re
import tempfile
import unittest
from pathlib import Path

from localcodeagent.context.realization import (
    MeaningFrame, detect_exact_spans, frame_from_canonical,
    norm_words, structure_signature, too_similar, validate_realization)
from localcodeagent.context.realize import (
    PersonaRenderer, RenderContext, SemanticResponse,
    capability_frame, self_learning_frame)
from localcodeagent.personality.genome import derive_genome
from localcodeagent.personality.greetings import GreetingService
from localcodeagent.personality.presets import get_preset


def personality_for(preset_id: str) -> dict:
    """Simulated PersonalityStore.resolve_active() output."""
    p = get_preset(preset_id)
    return {
        "name": p["name"], "base_preset": p["id"],
        "traits": dict(p["traits"]), "voice": dict(p.get("voice") or {}),
        "greeting_style": p.get("greeting_style") or "default",
        "address": p.get("address") or "",
        "speech_genome": dict(p.get("speech_genome") or {}),
        "strength": 60, "mood": "relaxed"}


def _norm(text):
    return " ".join(norm_words(text))


def _strip_wrapper(text):
    """Remove leading opening/micro-reaction/address material so only
    the BODY remains — §31's body_duplicate_rate operates on this."""
    s = str(text or "").strip()
    # Drop a leading "Opener —"/"Opener." fragment (openings never
    # contain a verb-bearing clause longer than 8 words).
    m = re.match(r"^([^.—!?]{1,40}[—.])\s+(.+)$", s)
    if m and len(norm_words(m.group(1))) <= 6:
        s = m.group(2).strip()
    return s


class RealizationCoreTests(unittest.TestCase):
    def setUp(self):
        self.r = PersonaRenderer(rng=random.Random(7))
        self.genome = derive_genome(personality_for("calm"))
        self.ctx = RenderContext(mood="relaxed", register="casual")

    # -- §42: the wrapper-only failure, directly -----------------------

    def test_body_duplicate_rate_canonical_list(self):
        """10 renders of the same canonical list-answer must produce
        substantially different BODIES — not the same paragraph with a
        new opener."""
        canonical = ("I can inspect and edit code, debug errors, run "
                     "tests behind permission gates, work with GitHub "
                     "when authorized, and manage local models.")
        bodies = set()
        for _ in range(10):
            out = self.r.render_semantic(
                SemanticResponse(semantic_id="t_dup",
                                 speech_act="answer"),
                self.genome, self.ctx, canonical=canonical)
            bodies.add(_norm(_strip_wrapper(out.text)))
        # Finite-bank failure mode yields <=3 unique bodies.
        self.assertGreaterEqual(len(bodies), 7)

    def test_no_byte_duplicate_bodies(self):
        canonical = ("I can inspect and edit code, debug errors, run "
                     "tests behind permission gates, work with GitHub "
                     "when authorized, and manage local models.")
        outs = [self.r.render_semantic(
            SemanticResponse(semantic_id="t_dup2", speech_act="answer"),
            self.genome, self.ctx, canonical=canonical).text
            for _ in range(10)]
        norms = [_norm(o) for o in outs]
        self.assertEqual(len(set(norms)), len(norms))

    def test_structure_signature_rotates(self):
        """Same structural signature twice consecutively is rejected —
        over 10 renders at least 3 distinct signatures must appear."""
        canonical = ("I can inspect and edit code, debug errors, run "
                     "tests behind permission gates, and manage models.")
        sigs = set()
        for _ in range(10):
            out = self.r.render_semantic(
                SemanticResponse(semantic_id="t_sig",
                                 speech_act="answer"),
                self.genome, self.ctx, canonical=canonical)
            sigs.add(structure_signature(_strip_wrapper(out.text)))
        self.assertGreaterEqual(len(sigs), 3)

    # -- protected spans / negation / confidence -----------------------

    def test_exact_spans_byte_exact(self):
        canonical = ("My birthday is September 30th, 2026. "
                     "The backend listens on port 26602.")
        for _ in range(6):
            out = self.r.render_semantic(
                SemanticResponse(semantic_id="t_span",
                                 speech_act="answer"),
                self.genome, self.ctx, canonical=canonical)
            self.assertIn("September 30th, 2026", out.text)
            self.assertIn("26602", out.text)

    def test_negation_polarity_survives(self):
        """'NOT connected' must never realize as connected (§22)."""
        canonical = "GitHub is NOT connected. The token is missing."
        for _ in range(8):
            out = self.r.render_semantic(
                SemanticResponse(semantic_id="t_neg",
                                 speech_act="answer"),
                self.genome, self.ctx, canonical=canonical)
            low = out.text.lower()
            # The negated sentence must keep a negator attached.
            self.assertTrue(re.search(
                r"(not connected|n't connected|not linked|"
                r"isn't connected|no connection)", low),
                msg=out.text)
            # A bare affirmative must never appear.
            self.assertIsNone(re.search(
                r"(?<!not )(?<!n't )github is connected", low))

    def test_validate_catches_negation_flip(self):
        frame = MeaningFrame(
            semantic_id="v1", speech_act="answer",
            facts=["GitHub is not connected"],
            negations=["github is not connected"])
        bad = "GitHub is connected."
        good = "GitHub is not connected yet."
        self.assertTrue(validate_realization(bad, frame))
        self.assertFalse(validate_realization(good, frame))

    def test_validate_catches_dropped_required_term(self):
        frame = MeaningFrame(
            semantic_id="v2", speech_act="answer",
            facts=["Tests pass"], required_terms=["42 tests"])
        self.assertTrue(validate_realization("All green.", frame))
        self.assertFalse(
            validate_realization("All 42 tests pass.", frame))

    def test_confidence_hedge_required(self):
        """Non-verified claims must carry a hedge (§23)."""
        frame = MeaningFrame(
            semantic_id="v3", speech_act="answer",
            facts=["The fix works"], confidence="likely")
        self.assertTrue(
            validate_realization("The fix works.", frame))
        self.assertFalse(
            validate_realization("The fix likely works.", frame))

    def test_detect_exact_spans(self):
        spans = detect_exact_spans(
            "Commit be2daf7f landed v0.22.0; birthday is "
            "September 30th, 2026; see `config.json`.")
        self.assertIn("v0.22.0", spans)
        self.assertIn("September 30th, 2026", spans)

    # -- frame extraction ----------------------------------------------

    def test_list_canonical_decomposes(self):
        frame = frame_from_canonical(
            "I can inspect code, run tests, and manage models.")
        self.assertEqual(len(frame.atoms), 3)
        self.assertTrue(frame.lead)

    def test_binder_modifier_stays_attached(self):
        """'run tests, with permission gates' — the 'with' clause is a
        modifier of the previous atom, not a list member."""
        atoms, _ = __import__(
            "localcodeagent.context.realization", fromlist=["x"]
        )._decompose_list(
            "I can inspect code, run tests, with permission gates, "
            "and manage models")
        joined = " ".join(a for a in atoms)
        self.assertIn("with permission gates", joined)
        self.assertNotIn("with permission gates, and", joined)


class LaneFrameTests(unittest.TestCase):
    """The shipped builtin lanes carry realizable frames (§6/§10)."""

    def setUp(self):
        self.r = PersonaRenderer(rng=random.Random(3))
        self.genome = derive_genome(personality_for("warm"))
        self.ctx = RenderContext(mood="relaxed", register="casual")

    def test_capability_body_varies(self):
        bodies = set()
        for _ in range(10):
            out = self.r.render_semantic(
                SemanticResponse(semantic_id="cap", speech_act="answer",
                                 frame=capability_frame()),
                self.genome, self.ctx,
                canonical="I can inspect and edit code.")
            bodies.add(_norm(_strip_wrapper(out.text)))
        self.assertGreaterEqual(len(bodies), 7)

    def test_capability_facts_preserved(self):
        out = self.r.render_semantic(
            SemanticResponse(semantic_id="cap2", speech_act="answer",
                             frame=capability_frame()),
            self.genome, self.ctx, canonical="x")
        low = out.text.lower()
        self.assertIn("github", low)
        self.assertIn("nexus brain", low)

    def test_self_learning_body_varies(self):
        bodies = set()
        for _ in range(10):
            out = self.r.render_semantic(
                SemanticResponse(semantic_id="sl", speech_act="answer",
                                 frame=self_learning_frame()),
                self.genome, self.ctx, canonical="x")
            bodies.add(_norm(_strip_wrapper(out.text)))
        self.assertGreaterEqual(len(bodies), 6)


class GreetingFarewellSoakTests(unittest.TestCase):
    """§28/§29/§43/§44 — same context, genuinely fresh wording."""

    def _svc(self, td):
        return GreetingService(Path(td))

    def _profile(self):
        return {"id": "p1", "first_name": "John", "is_creator": True,
                "has_completed_intro": True}

    def _personality(self, style="warm"):
        return {"greeting_style": style, "traits": {}, "voice": {},
                "strength": 50, "mood": "relaxed"}

    def test_greeting_soak_no_exact_dupes(self):
        with tempfile.TemporaryDirectory() as td:
            svc = self._svc(td)
            outs = [svc.greeting(self._profile(), self._personality(),
                                 is_adult=False)["text"]
                    for _ in range(50)]
            norms = [_norm(o) for o in outs]
            self.assertEqual(len(set(norms)), len(norms),
                             "exact duplicate greetings")

    def test_greeting_soak_low_body_duplication(self):
        with tempfile.TemporaryDirectory() as td:
            svc = self._svc(td)
            bodies = {_norm(_strip_wrapper(
                svc.greeting(self._profile(), self._personality("warm"),
                             is_adult=False)["text"]))
                for _ in range(50)}
            self.assertGreaterEqual(len(bodies), 15)

    def test_farewell_soak_no_exact_dupes(self):
        with tempfile.TemporaryDirectory() as td:
            svc = self._svc(td)
            outs = [svc.farewell(self._profile(),
                                 self._personality("sassy"),
                                 is_adult=False)["text"]
                    for _ in range(50)]
            norms = [_norm(o) for o in outs]
            self.assertEqual(len(set(norms)), len(norms),
                             "exact duplicate farewells")

    def test_farewell_means_shutdown(self):
        """Every farewell — whatever the wording — still says the core
        is going offline. No fabricated context, no missing act."""
        with tempfile.TemporaryDirectory() as td:
            svc = self._svc(td)
            for style in ("default", "warm", "sassy", "formal"):
                for _ in range(10):
                    t = svc.farewell(
                        self._profile(), self._personality(style),
                        is_adult=False)["text"].lower()
                    self.assertTrue(re.search(
                        r"(shut|power|offline|dark|down|signing|"
                        r"sleep|exit|quiet|rest|settl|nap|winding|"
                        r"ending|concluding|heading|turning off|"
                        r"going to bed|going quiet|out\b)", t),
                        msg=f"{style}: {t}")

    def test_greeting_means_welcome(self):
        """Every greeting still acknowledges the return."""
        with tempfile.TemporaryDirectory() as td:
            svc = self._svc(td)
            for _ in range(10):
                t = svc.greeting(
                    self._profile(), self._personality("warm"),
                    is_adult=False)["text"]
                self.assertTrue(len(t) > 5)

    def test_creator_always_addressed(self):
        with tempfile.TemporaryDirectory() as td:
            svc = self._svc(td)
            for _ in range(10):
                t = svc.greeting(self._profile(),
                                 self._personality("warm"),
                                 is_adult=False)["text"]
                self.assertIn("Father", t)


class SimilarityGateTests(unittest.TestCase):
    def test_too_similar_exact(self):
        self.assertTrue(too_similar("The tests passed.",
                                    ["The tests passed."]))
        self.assertTrue(too_similar("The tests passed!",
                                    ["the tests passed"]))
        self.assertFalse(too_similar("Green across the board.",
                                     ["The tests passed."]))

    def test_same_opening_rejected(self):
        prev = ["I can inspect code and run tests locally."]
        self.assertTrue(too_similar(
            "I can inspect code and run diagnostics instead.", prev))


if __name__ == "__main__":
    unittest.main()
