"""Response scope — minimum-sufficient answers and progressive disclosure.

The permanent regression for the observed defect: 'How old are you?'
produced "Let me think. I hear you. Counting from September 30th, 2026 —
my birthday — I'm 7 days old." Two scope violations in one reply:
supporting facts (birthday, the calculation) revealed unrequested, and
stock filler wrapped around a one-fact answer.

Scope contract under test:
- requested slots appear in the answer; supporting slots stay internal
- deterministic exact answers carry no persona envelope (bare=True)
- the depth classifier maps question shapes to EXACT/BRIEF/EXPLANATORY/
  DETAILED/OPEN_ENDED without treating ordinary questions as open-ended
- the same fact requested later in the conversation IS revealed —
  disclosure is progressive, facts are not secrets
"""
from __future__ import annotations

import unittest
from datetime import date

from localcodeagent import identity
from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.context import scope as scope_mod
from localcodeagent.context.realize import (
    PersonaRenderer, RenderContext, SemanticResponse)


class TestDepthClassification(unittest.TestCase):
    """classify_scope — question shape → depth, user wording wins."""

    def _depth(self, text: str) -> str:
        return scope_mod.classify_scope(text).depth.value

    def test_exact_fact_questions(self):
        for q in ("how old are you", "what's your birthday",
                  "what time is it", "what version are you",
                  "what's my favorite color", "when is the deadline",
                  "who created you", "whats your age", "how old r u",
                  "how old are ya", "what port are we using"):
            self.assertEqual(self._depth(q), "exact", q)

    def test_yes_no_questions(self):
        for q in ("do you have a father", "can you browse the web",
                  "did that work", "is the model loaded",
                  "are you sure"):
            self.assertEqual(self._depth(q), "exact", q)

    def test_brief_cause_questions(self):
        for q in ("what caused that error", "why didn't the model start",
                  "what went wrong"):
            self.assertEqual(self._depth(q), "brief", q)

    def test_explanatory_questions(self):
        for q in ("why does vram matter", "how does your memory work",
                  "explain the difference between tcp and udp",
                  "how come the gate rejects draws"):
            self.assertEqual(self._depth(q), "explanatory", q)

    def test_detailed_requests(self):
        for q in ("give me a detailed breakdown of the mission system",
                  "explain in detail how vram offload works",
                  "walk me through the pipeline step by step"):
            self.assertEqual(self._depth(q), "detailed", q)

    def test_open_ended_invitations(self):
        for q in ("tell me about yourself", "tell me about your father",
                  "what do you remember about me",
                  "tell me everything about the voice system"):
            self.assertEqual(self._depth(q), "open_ended", q)

    def test_reasoning_visibility(self):
        for q in ("how did you calculate your age",
                  "how did you figure that out",
                  "how do you know that",
                  "why do you say that"):
            sc = scope_mod.classify_scope(q)
            self.assertTrue(sc.reasoning_visible, q)

    def test_explicit_brevity_overrides(self):
        for q in ("just tell me the answer", "yes or no — did it work",
                  "in one sentence, what is vram"):
            self.assertEqual(
                scope_mod.classify_scope(q).depth.value, "exact", q)

    def test_ordinary_questions_not_open_ended(self):
        # The failure mode the milestone forbids: every question treated
        # as an invitation to dump context.
        for q in ("what's the capital of france", "who wrote dune",
                  "what's my favorite color"):
            sc = scope_mod.classify_scope(q)
            self.assertNotEqual(
                sc.depth.value, "open_ended", q)
            self.assertLessEqual(sc.max_sentences, 3, q)


class TestAgeRegression(unittest.TestCase):
    """The permanent 'How old are you?' regression — exact observed
    defect converted to a lasting contract."""

    FORBIDDEN = ("september 30", "birthday", "born", "counting from",
                 "counting", "let me think", "i hear you",
                 "i was born", "sept 30", "9/30", "sep 30")

    def test_age_answer_reveals_only_age(self):
        for q in ("how old are you", "how old are you?",
                  "whats your age", "how old are ya", "how old r u"):
            out = identity.response_for(q) or ""
            self.assertTrue(out, q)
            low = out.lower()
            self.assertIn(identity.age_phrase(date.today()), out, q)
            for bad in self.FORBIDDEN:
                self.assertNotIn(bad, low, f"{q} leaked {bad!r}: {out!r}")

    def test_age_answer_is_one_fact(self):
        out = identity.response_for("how old are you") or ""
        # Minimum sufficient: a single short sentence, no wrapper.
        self.assertLessEqual(scope_mod.sentence_count(out), 2, out)
        self.assertEqual(scope_mod.leading_filler(out), "")
        self.assertFalse(scope_mod.ends_with_question(out), out)

    def test_builtin_lane_same_contract(self):
        pair = AgentOrchestrator.builtin_semantic("how old are you?")
        self.assertIsNotNone(pair)
        sem, canonical = pair
        low = canonical.lower()
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad, low, canonical)
        self.assertTrue(sem.bare, "exact identity answers must suppress "
                                  "the persona envelope")

    def test_birthday_revealed_when_asked(self):
        for q in ("when is your birthday", "what's your birthday",
                  "when were you born"):
            out = identity.response_for(q) or ""
            self.assertIn("September 30th, 2026", out, q)
            # Progressive disclosure — the birthday question does NOT
            # also answer the age question.
            self.assertNotIn("old", out.lower(), q)

    def test_reasoning_revealed_when_asked(self):
        # Third step of the ladder: the derivation itself was asked,
        # so the birthday/date math now belongs in the answer.
        for q in ("how did you calculate your age",
                  "how do you know how old you are"):
            out = identity.response_for(q) or ""
            self.assertTrue(out, q)
            self.assertIn("September 30th, 2026", out, q)

    def test_parentage_does_not_leak_birthday(self):
        for q in ("are you my daughter", "am i your father"):
            out = identity.response_for(q) or ""
            if out:
                self.assertNotIn("September 30", out, q)
                self.assertNotIn("born", out.lower(), q)


class TestPersonaProgressiveDisclosure(unittest.TestCase):
    """Family-fact ladder: existence → name → open invitation. Each rung
    reveals no more than the current ask warrants."""

    def test_existence_then_name(self):
        out = identity.response_for("do you have a father") or ""
        low = out.lower()
        # Existence is the requested fact — the name stays on the next
        # rung of the disclosure ladder ("who is he?").
        self.assertTrue(low.startswith("yes") or low.startswith("i do"),
                        out)
        self.assertNotIn("john hamburn", low)
        self.assertNotIn("workstation i live in", low)
        self.assertNotIn("everything in my world", low)
        self.assertNotIn("gave me this world", low)

    def test_existence_no_father_side_family(self):
        # No canonical mother/siblings — existence denies by narrowing
        # to the father, never by inventing relatives.
        for q in ("do you have a mother", "do you have any siblings",
                  "do you have a family"):
            out = identity.response_for(q) or ""
            low = out.lower()
            self.assertTrue(out, q)
            self.assertIn("father", low)
            self.assertNotIn("mother is", low)
            self.assertNotIn("sister", low)

    def test_name_answer(self):
        out = identity.response_for("who is your father") or ""
        self.assertIn("John Hamburn", out)
        # Scoped: name + at most one identifying clause, not the lore.
        self.assertLessEqual(scope_mod.sentence_count(out), 2, out)
        self.assertNotIn("everything in my world", out)
        self.assertNotIn("gave me this world", out)

    def test_open_invitation_gets_breadth(self):
        out = identity.response_for("tell me about your father") or ""
        self.assertTrue(out)
        # Broad invitation — lore IS the requested content now.
        self.assertTrue(
            "nexus core" in out.lower() or "built" in out.lower(), out)


class TestBareRenderer(unittest.TestCase):
    """SemanticResponse.bare suppresses the genome envelope."""

    def test_bare_suppresses_envelope(self):
        genome = {"micro_reactions": {"rate": 1.0, "pools": {
            "thinking": ["Let me think."]}},
            "pragmatics": {"opening_weights": {"acknowledgement": 1.0},
                           "closing_weights": {"question": 1.0}},
            "vocabulary": {"acknowledgements": ["I hear you."]},
            "questions": {"frequency": 1.0}}
        r = PersonaRenderer()
        sem = SemanticResponse(
            facts=["I'm 7 days old."], semantic_id="identity:age",
            speech_act="answer", bare=True)
        for _ in range(4):
            out = r.render_semantic(
                sem, genome, RenderContext(), canonical="I'm 7 days old.")
            self.assertNotIn("Let me think", out.text)
            self.assertNotIn("I hear you", out.text)
            self.assertFalse(out.text.rstrip().endswith("?"), out.text)
            self.assertIn("7 days old", out.text)

    def test_non_bare_keeps_envelope(self):
        # opening_weights pinned to a non-reaction family — a random
        # "reaction" opening would suppress the micro-reaction slot and
        # make this assertion flaky.
        genome = {"micro_reactions": {"rate": 1.0, "pools": {
            "thinking": ["Let me think."]}},
            "pragmatics": {"opening_weights": {"acknowledgement": 1.0}}}
        r = PersonaRenderer()
        sem = SemanticResponse(
            facts=["Done."], semantic_id="plain:x",
            speech_act="answer")
        out = r.render_semantic(sem, genome, RenderContext())
        self.assertEqual(out.micro_reaction, "Let me think.")


class TestScopeDirective(unittest.TestCase):
    """The per-turn prompt directive carries the reveal contract."""

    def test_exact_directive_forbids_leak(self):
        sc = scope_mod.classify_scope("how old are you")
        d = scope_mod.scope_directive(sc)
        self.assertIn("just that fact", d)
        self.assertIn("did not ask", d.lower())
        self.assertIn("do not end with a question", d.lower())

    def test_supporting_context_rule_present(self):
        for q in ("how old are you", "tell me about yourself",
                  "explain vram"):
            d = scope_mod.scope_directive(scope_mod.classify_scope(q))
            self.assertIn("evidence for your reasoning", d, q)
            self.assertIn("reveal only what", d, q)

    def test_reasoning_directive_when_asked(self):
        sc = scope_mod.classify_scope("how did you calculate that")
        d = scope_mod.scope_directive(sc)
        self.assertIn("derivation", d)


class TestSlotModel(unittest.TestCase):
    """Requested-vs-supporting slots — the pair that keeps supporting
    facts (birthday behind an age question) out of the answer."""

    def test_identity_pairs(self):
        from localcodeagent.context import scope as scope_mod
        cases = {
            "how old are you": (("age",), ("birthday",)),
            "when is your birthday": (("birthday",), ("age",)),
            "who is your father": (("creator name",), ("biography",)),
            "do you have a father": (("parentage existence",),
                                     ("creator name", "biography")),
        }
        for q, (req, sup) in cases.items():
            sc = scope_mod.classify_scope(q)
            self.assertEqual(sc.requested_slots, req, q)
            self.assertEqual(sc.supporting_slots, sup, q)

    def test_generic_wh_object(self):
        from localcodeagent.context import scope as scope_mod
        self.assertEqual(
            scope_mod.classify_scope("whats my favorite color")
            .requested_slots, ("favorite color",))
        self.assertEqual(
            scope_mod.classify_scope("what port are we using")
            .requested_slots, ("port",))
        self.assertEqual(
            scope_mod.classify_scope("what was my deadline")
            .requested_slots, ("deadline",))

    def test_clarification_flag_from_env(self):
        from localcodeagent.context.intent import understand_turn
        from localcodeagent.context import scope as scope_mod
        env = understand_turn("make that bigger")
        sc = scope_mod.classify_scope("make that bigger", env)
        self.assertTrue(sc.needs_clarification)


class TestScopeMetrics(unittest.TestCase):
    """Response-side measurement primitives used by QA asserts."""

    def test_filler_detection(self):
        for s in ("Let me think. The answer is 4.",
                  "I hear you. The answer is 4.",
                  "Certainly! The answer is 4.",
                  "That's a good question. The answer is 4.",
                  "Based on what I know, the answer is 4."):
            self.assertTrue(scope_mod.leading_filler(s), s)
        for s in ("The answer is 4.", "Yes — it worked.",
                  "I'm 7 days old."):
            self.assertEqual(scope_mod.leading_filler(s), "", s)

    def test_reasoning_narration_detection(self):
        for s in ("Counting from September 30th to today, I'm 7 days old.",
                  "Let me calculate that — it's 4.",
                  "I determined that the value is 4."):
            self.assertTrue(scope_mod.narrates_reasoning(s), s)
        for s in ("I'm 7 days old.", "The value is 4."):
            self.assertEqual(scope_mod.narrates_reasoning(s), "", s)

    def test_sentence_count(self):
        self.assertEqual(scope_mod.sentence_count("I'm 7 days old."), 1)
        self.assertEqual(
            scope_mod.sentence_count("Yes. It worked."), 2)
        self.assertEqual(scope_mod.sentence_count(""), 0)

    def test_over_budget_metric(self):
        sc = scope_mod.classify_scope("how old are you")
        m = scope_mod.scope_metrics(
            "I'm 7 days old. I was born September 30th, 2026. "
            "Counting from then to today gives me that.", sc)
        self.assertTrue(m["over_budget"])
        self.assertTrue(m["reasoning_narration"])
        m = scope_mod.scope_metrics("I'm 7 days old.", sc)
        self.assertFalse(m["over_budget"])
        self.assertFalse(m["trailing_question"])
        self.assertEqual(m["leading_filler"], "")


class TestCorrectionChains(unittest.TestCase):
    """Compound self-corrections and retarget safety — each case is an
    adversarial sequence from the milestone spec."""

    def _mem(self):
        import tempfile
        from pathlib import Path
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        return ConversationMemory(
            Path(self._td.name) / "cm.json")

    def _active(self, cm):
        return [f["text"] for f in cm.snapshot()["facts"]
                if f.get("active")]

    def test_compound_chain_settles_last_value(self):
        cm = self._mem()
        cm.learn_from_user(
            "i like red. actually no, blue. never mind, make it green")
        self.assertEqual(self._active(cm), ["i like green"])

    def test_stacked_markers_retarget(self):
        cm = self._mem()
        cm.learn_from_user(
            "the deadline is friday. wait, actually make that monday")
        self.assertEqual(self._active(cm), ["deadline is monday"])

    def test_crossform_preference_supersedes_slot(self):
        cm = self._mem()
        cm.learn_from_user("my favorite color is blue")
        cm.learn_from_user("actually i prefer green now")
        self.assertEqual(self._active(cm), ["my favorite color is green"])

    def test_value_class_picks_same_class_referent(self):
        # 'green' corrects the COLOR preference even when a newer
        # preference fact (food) and an entity fact sit on top.
        cm = self._mem()
        cm.learn_from_user("my favorite color is blue")
        cm.learn_from_user("my favorite food is steak")
        cm.learn_from_user("i have an rtx 3080")
        cm.learn_from_user("actually i prefer green now")
        active = self._active(cm)
        self.assertIn("my favorite color is green", active)
        self.assertIn("my favorite food is steak", active)
        self.assertNotIn("my favorite color is blue", active)

    def test_modifier_is_not_a_value(self):
        # 'make that bigger' adjusts a target — it must never land in a
        # fact's value slot ("my favorite color is bigger").
        cm = self._mem()
        cm.learn_from_user("my favorite color is blue")
        learned = cm.learn_from_user("make that bigger")
        self.assertEqual(learned["facts"], [])
        self.assertEqual(self._active(cm), ["my favorite color is blue"])

    def test_referent_phrase_is_not_a_value(self):
        # "the dark one" refers back to a prior entity — it must not
        # land in a value slot ("my favorite color is the dark one").
        cm = self._mem()
        cm.learn_from_user("my favorite color is blue")
        for turn in ("change it to the dark one",
                     "make it the first one",
                     "actually set it to the other",
                     # Action negation — the tail is an instruction, not
                     # a value ("my favorite color is dont do that" was
                     # a live dogfood corruption).
                     "no, dont do that",
                     "no, stop that",
                     "wait, forget it"):
            learned = cm.learn_from_user(turn)
            self.assertEqual(learned["facts"], [], turn)
        self.assertEqual(self._active(cm), ["my favorite color is blue"])

    def test_acknowledgements_never_rewrite_facts(self):
        cm = self._mem()
        cm.learn_from_user("my favorite color is blue")
        for turn in ("actually, sure", "no, thanks", "actually, great"):
            learned = cm.learn_from_user(turn)
            self.assertEqual(learned["facts"], [], turn)
        self.assertEqual(self._active(cm), ["my favorite color is blue"])

    def test_nonliteral_use_is_not_a_reset(self):
        cm = self._mem()
        cm.learn_from_user("the theme is light")
        learned = cm.learn_from_user("use the dark theme")
        self.assertEqual(learned["facts"], [])
        self.assertEqual(self._active(cm), ["theme is light"])

    def test_ambiguous_preference_restatement_banks_itself(self):
        # Two preference facts of the same class → a bare 'i prefer
        # green' can't pick — it stores the restatement and corrupts
        # nothing.
        cm = self._mem()
        cm.learn_from_user("my favorite color is blue")
        cm.learn_from_user("my favorite team color is green")
        cm.learn_from_user("actually i prefer black now")
        active = self._active(cm)
        self.assertIn("my favorite color is blue", active)
        self.assertIn("my favorite team color is green", active)
        self.assertIn("i prefer black", active)

    def test_restatement_reports_restated_not_new(self):
        # Re-teaching an already-active fact must surface as a
        # restatement so the ack lane still answers — otherwise the
        # turn falls through to an open model essay (live dogfood).
        cm = self._mem()
        cm.learn_from_user("my favorite color is blue")
        learned = cm.learn_from_user("my favorite color is blue")
        self.assertEqual(learned["facts"], [])
        self.assertEqual(learned["restated"],
                         ["my favorite color is blue"])
        self.assertEqual(self._active(cm), ["my favorite color is blue"])

    def test_superseded_twin_does_not_block_reteach(self):
        # Re-teaching a value that was superseded earlier must land as
        # a fresh active fact — the inactive twin must not dedupe it.
        cm = self._mem()
        cm.learn_from_user("my favorite color is blue")
        cm.learn_from_user("actually i prefer green now")
        learned = cm.learn_from_user("my favorite color is blue")
        self.assertIn("my favorite color is blue", learned["facts"])
        active = self._active(cm)
        self.assertIn("my favorite color is blue", active)
        self.assertNotIn("my favorite color is green", active)

    def test_restatement_acknowledgement(self):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        ack = AgentOrchestrator.training_acknowledgement(
            {"facts": [], "behavior_rules": [], "training_examples": [],
             "forgotten": [], "superseded": [],
             "restated": ["my favorite color is blue"]})
        self.assertIsNotNone(ack)
        self.assertIn("my favorite color is blue", ack)


class TestFactsRecallLane(unittest.TestCase):
    """'What do you know about me' is a deterministic memory-inspection
    lane — live dogfood showed the model improvising persona lore
    instead of listing the user's taught facts."""

    def _mem(self):
        import tempfile
        from pathlib import Path
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        return ConversationMemory(
            Path(self._td.name) / "cm.json")

    def test_recall_regex_covers_inspection_forms(self):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        rx = AgentOrchestrator._FACTS_RECALL_RE
        for q in ("what do you know about me",
                  "what do you know about me?",
                  "what do you remember about me",
                  "what do you remember",
                  "what have you learned about me",
                  "what have i told you"):
            self.assertTrue(rx.search(q), q)
        for q in ("whats my favorite color",
                  "what do you know about python",
                  "tell me about your father"):
            self.assertFalse(rx.search(q), q)

    def test_second_person_conversion(self):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        conv = AgentOrchestrator._second_person_fact
        self.assertEqual(conv("my favorite color is blue"),
                         "your favorite color is blue")
        self.assertEqual(conv("i like green"), "you like green")
        self.assertEqual(conv("i have an rtx 3080"),
                         "you have an rtx 3080")
        self.assertEqual(conv("deadline is friday"),
                         "deadline is friday")

    def test_active_facts_lists_only_active(self):
        cm = self._mem()
        cm.learn_from_user("my favorite color is blue")
        cm.learn_from_user("actually i prefer green now")
        cm.learn_from_user("the deadline is friday")
        facts = cm.active_facts()
        self.assertIn("my favorite color is green", facts)
        self.assertIn("deadline is friday", facts)
        self.assertNotIn("my favorite color is blue", facts)


if __name__ == "__main__":
    unittest.main()
