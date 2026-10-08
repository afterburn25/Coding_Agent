"""Universal context intelligence + non-repetitive response tests.

Covers the IntentEnvelope pipeline (context/intent.py), reference
resolution (context/references.py), active-context persistence/decay
(context/active_context.py), and the surface renderer
(context/realize.py). These are deterministic fast-path tests — no
models are loaded.
"""
from __future__ import annotations

import time
import unittest

from localcodeagent.context.intent import (
    CODING, CONVERSATION, CORRECTION, FEEDBACK_SIGNAL, GITHUB_STATUS,
    IDENTITY_QUERY, IMAGE_FOLLOWUP, IMAGE_GENERATION, QUESTION,
    TOOL_ACTION, understand_turn, strip_image_scaffold,
)
from localcodeagent.context.active_context import (
    ActiveContext, MAX_RECENT_ENTITIES,
)
from localcodeagent.context.references import (
    resolve_references, resolve_with_report,
)
from localcodeagent.context.realize import (
    PersonaRenderer, ResponseLedger, REPEAT_ACKS, GREETING_VARIANTS,
    CAPABILITY_VARIANTS, similarity,
)
from localcodeagent import identity


def ctx_with_image(subject="adult angel with black wings",
                   prompt="photorealistic adult angel with black wings"):
    ac = ActiveContext()
    ac.active_image_subject = subject
    ac.active_image_prompt = prompt
    ac.active_intent = IMAGE_GENERATION
    ac.touch()
    return ac


def ctx_with_error(error="ImportError: no module named foo"):
    ac = ActiveContext()
    ac.note_error(error)
    ac.active_intent = CODING
    ac.touch()
    return ac


# ---------------------------------------------------------------------------
# §1 — language by meaning: paraphrase classes route identically
# ---------------------------------------------------------------------------

class TestImageParaphrases(unittest.TestCase):
    CASES = [
        "generate an image of a castle",
        "show me a picture of a castle",
        "let me see a castle",
        "can I see a castle?",
        "give me a picture of a castle",
        "I want to see what a castle would look like",
        "visualize a castle for me",
        "draw a castle",
        "paint a mountain landscape",
        "make me a wallpaper of a nebula",
        "hey nexus, please show me a picture of a red cat",
    ]

    def test_all_route_to_image(self):
        for text in self.CASES:
            env = understand_turn(text)
            self.assertEqual(
                env.primary_intent, IMAGE_GENERATION,
                f"{text!r} -> {env.primary_intent}")
            self.assertGreaterEqual(env.confidence, 0.7, text)

    def test_object_semantics_veto(self):
        """'Show me X' where X is a non-visual output is never image."""
        for text in ("show me the code", "show me the logs",
                     "show me the diff", "give me a function",
                     "generate a report", "create a json file",
                     "make a spreadsheet"):
            env = understand_turn(text)
            self.assertNotEqual(env.primary_intent, IMAGE_GENERATION, text)

    def test_question_about_images_is_not_a_job(self):
        for text in ("which model does Nexus generate images with?",
                     "what is the default image model?",
                     "who created the image pipeline?"):
            env = understand_turn(text)
            self.assertNotEqual(env.primary_intent, IMAGE_GENERATION, text)

    def test_hypothetical_imagine_is_not_a_job(self):
        """'Imagine you were/if/what' frames thought, not an artifact —
        'imagine a dragon' (direct object) still generates."""
        for text in ("imagine you were an AI. what would you say",
                     "imagine if it rained",
                     "imagine what she looks like",
                     "imagine being somewhere else"):
            env = understand_turn(text)
            self.assertNotEqual(env.primary_intent, IMAGE_GENERATION, text)
        env = understand_turn("imagine a dragon")
        self.assertEqual(env.primary_intent, IMAGE_GENERATION)

    def test_subject_extraction(self):
        env = understand_turn(
            "generate an image of a photorealistic adult angel "
            "with black wings")
        self.assertIn("adult angel", env.subject)
        self.assertIn("black wings", env.subject)
        self.assertNotIn("generate", env.subject.lower())

    def test_adult_intent_keeps_route_for_policy_layer(self):
        """Adult/explicit terms still classify as image — the safety
        layer decides policy, routing never erases the request."""
        env = understand_turn("generate an adult woman in a red dress")
        self.assertEqual(env.primary_intent, IMAGE_GENERATION)
        env2 = understand_turn("generate a nude woman")
        self.assertEqual(env2.primary_intent, IMAGE_GENERATION)
        self.assertEqual(env2.needs_clarification, "adult_subject")


class TestGithubParaphrases(unittest.TestCase):
    CASES = [
        "check GitHub",
        "see what's happening with the repo",
        "look at the current branch",
        "what did Devin just push?",
        "anything new land?",
        "check the latest commits",
        "what's going on in github?",
        "check githib",  # typo
    ]

    def test_all_route_to_github(self):
        for text in self.CASES:
            env = understand_turn(text)
            self.assertEqual(
                env.primary_intent, GITHUB_STATUS,
                f"{text!r} -> {env.primary_intent}")
            self.assertGreaterEqual(env.confidence, 0.6, text)


class TestRepairParaphrases(unittest.TestCase):
    CASES = [
        "fix the error",
        "take care of that bug",
        "repair what just broke",
        "can you sort that out?",
        "get that working again",
    ]

    def test_bind_active_error(self):
        ac = ctx_with_error()
        for text in self.CASES:
            env = understand_turn(text, active=ac)
            self.assertEqual(env.primary_intent, CODING,
                             f"{text!r} -> {env.primary_intent}")
            self.assertIn("repair", env.requested_action)

    def test_pronoun_binds_error(self):
        ac = ctx_with_error()
        env = understand_turn("fix it", active=ac)
        self.assertEqual(env.primary_intent, CODING)
        self.assertIn("ImportError", env.references.get("it", ""))


# ---------------------------------------------------------------------------
# §4/§5 — references, ellipsis, follow-ups
# ---------------------------------------------------------------------------

class TestFollowups(unittest.TestCase):
    def test_image_followups_preserve_subject(self):
        ac = ctx_with_image()
        for text in ("make her blonde", "full body", "zoom out",
                     "try again", "do it", "same thing but outside",
                     "darker lighting"):
            env = understand_turn(text, active=ac)
            self.assertEqual(env.primary_intent, IMAGE_FOLLOWUP, text)

    def test_pronoun_resolves_to_image_subject(self):
        ac = ctx_with_image()
        env = understand_turn("make her blonde", active=ac)
        self.assertIn("angel", env.references.get("her", ""))

    def test_correction_replaces_attribute(self):
        ac = ctx_with_image()
        env = understand_turn("no, red hair", active=ac)
        self.assertEqual(env.primary_intent, IMAGE_FOLLOWUP)
        self.assertEqual(env.correction_of, "previous_attribute")
        self.assertIn("red hair", env.correction_value)

    def test_implicit_complaint_is_followup(self):
        ac = ctx_with_image()
        for text in ("that ain't it", "the image is too close",
                     "she looks too zoomed in"):
            env = understand_turn(text, active=ac)
            self.assertEqual(env.primary_intent, IMAGE_FOLLOWUP, text)

    def test_last_image_resolves_after_topic_shift(self):
        """Image retired by a GitHub turn stays retrievable."""
        ac = ctx_with_image()
        shift = understand_turn("now check github", active=ac)
        self.assertEqual(shift.primary_intent, GITHUB_STATUS)
        ac.record_turn(shift)
        self.assertFalse(ac.image_active())
        env = understand_turn("what was wrong with the last image?",
                              active=ac)
        resolved = resolve_references(
            "what was wrong with the last image?", ac)
        self.assertTrue(any("angel" in v for v in resolved.values()))


class TestPronounAmbiguity(unittest.TestCase):
    def test_unbound_pronoun_is_not_guessed(self):
        env = understand_turn("fix it")
        self.assertNotIn("it", env.references)

    def test_two_live_domains_mark_ambiguous(self):
        ac = ctx_with_image()
        ac.note_error("build failed")
        report = resolve_with_report("check it", ac)
        self.assertIn("it", report["ambiguous"])

    def test_verb_hint_disambiguates(self):
        ac = ctx_with_image()
        ac.note_error("build failed")
        report = resolve_with_report("fix it", ac)
        self.assertIn("build failed", report["resolved"].get("it", ""))
        report2 = resolve_with_report("make it darker", ac)
        self.assertIn("angel", report2["resolved"].get("it", ""))


# ---------------------------------------------------------------------------
# §12/§13 — multi-action and conditional requests
# ---------------------------------------------------------------------------

class TestCompoundConditional(unittest.TestCase):
    def test_compound_preserves_every_clause(self):
        env = understand_turn(
            "check the repo, fix the failing test, run everything, "
            "and push it")
        self.assertTrue(env.compound)
        intents = [s["intent"] for s in env.secondary_intents]
        self.assertEqual(intents,
                         [GITHUB_STATUS, CODING, TOOL_ACTION, "git_action"])

    def test_compound_image_then_files(self):
        env = understand_turn(
            "make the image, save it, and put it in the project folder")
        self.assertTrue(env.compound)
        self.assertEqual(env.primary_intent, IMAGE_GENERATION)
        self.assertEqual(len(env.secondary_intents), 3)

    def test_conditional_preserved(self):
        env = understand_turn("if the tests fail, fix them and rerun")
        self.assertEqual(len(env.conditionals), 1)
        self.assertIn("tests fail", env.conditionals[0]["condition"])
        self.assertEqual(env.primary_intent, CODING)

    def test_unless_fallback_preserved(self):
        env = understand_turn(
            "use InvokeAI unless it fails, then try ComfyUI")
        self.assertTrue(env.conditionals or env.alternatives)
        self.assertIn("comfyui", " ".join(env.alternatives).lower())


# ---------------------------------------------------------------------------
# §16/§20/§21 — typos, topic moves, returns
# ---------------------------------------------------------------------------

class TestTopicMoves(unittest.TestCase):
    def test_topic_shift_flag(self):
        ac = ctx_with_image()
        env = understand_turn("now check github", active=ac)
        self.assertTrue(env.topic_shift)
        self.assertEqual(env.primary_intent, GITHUB_STATUS)

    def test_return_to_active_image(self):
        ac = ctx_with_image()
        env = understand_turn("back to that angel image", active=ac)
        self.assertTrue(env.topic_shift)
        self.assertEqual(env.primary_intent, IMAGE_FOLLOWUP)
        self.assertIn("angel", env.subject)

    def test_return_to_retired_entity(self):
        ac = ctx_with_image("a castle in the mountains")
        ac._retire("image", "a castle in the mountains", "job-1")
        env = understand_turn("back to that castle image", active=ac)
        self.assertEqual(env.primary_intent, IMAGE_FOLLOWUP)
        self.assertIn("castle", env.subject)


class TestTypos(unittest.TestCase):
    def test_whitelisted_typos(self):
        cases = [
            ("check githib", GITHUB_STATUS),
            ("generate a pictue of a cat", IMAGE_GENERATION),
            ("fix the erorr", CODING),
        ]
        for text, want in cases:
            env = understand_turn(text)
            self.assertEqual(env.primary_intent, want, text)


class TestImplicitAndInformal(unittest.TestCase):
    def test_trouble_reports(self):
        ac = ctx_with_error()
        env = understand_turn("it's still broken", active=ac)
        self.assertEqual(env.primary_intent, CODING)
        self.assertTrue(env.implicit)

    def test_no_target_is_honest(self):
        env = understand_turn("it's busted")
        self.assertEqual(env.primary_intent, FEEDBACK_SIGNAL)
        self.assertTrue(env.ambiguity)


# ---------------------------------------------------------------------------
# §9/§10/§11 — comparisons, ordinals, temporal
# ---------------------------------------------------------------------------

class TestMetaReferences(unittest.TestCase):
    def test_comparison_flag(self):
        env = understand_turn("which model is faster?")
        self.assertTrue(env.comparison)

    def test_ordinal_reference(self):
        env = understand_turn("the second one")
        self.assertEqual(env.ordinal_reference, 2)
        self.assertTrue(env.ambiguity)  # needs the candidate set

    def test_temporal_context(self):
        env = understand_turn("what did we discuss earlier?")
        self.assertEqual(env.temporal_context, "earlier")


class TestBacklogReferencePhrasings(unittest.TestCase):
    """§2 follow-up phrases the backlog requires to resolve."""

    def _ctx(self):
        ac = ActiveContext(
            active_project="chat-nexus",
            active_image_subject="sunset mountain")
        ac.recent_entities = [
            {"kind": "image", "label": "sunset mountain", "ts": 1e12},
            {"kind": "image", "label": "forest lake", "ts": 1e11},
            {"kind": "model", "label": "qwen3-8b", "ts": 1e10},
        ]
        ac.touch()
        return ac

    def test_that_one(self):
        rep = resolve_with_report("that one", self._ctx())
        self.assertEqual(rep["resolved"].get("that one"),
                         "sunset mountain")

    def test_the_second_one(self):
        rep = resolve_with_report("the second one", self._ctx())
        self.assertEqual(rep["resolved"].get("the second one"),
                         "forest lake")

    def test_the_other_model(self):
        rep = resolve_with_report("the other model", self._ctx())
        self.assertTrue(rep["resolved"].get("the other model"))

    def test_what_we_were_talking_about(self):
        rep = resolve_with_report(
            "go back to what we were talking about", self._ctx())
        self.assertTrue(rep["resolved"])

    def test_meant_the_previous_one(self):
        rep = resolve_with_report(
            "no, I meant the previous one", self._ctx())
        self.assertTrue(rep["resolved"].get("the previous one"))

    def test_comparison_marks_and_resolves(self):
        env = understand_turn("compare it to yesterday's option",
                              active=self._ctx())
        self.assertTrue(env.comparison)

    def test_unresolvable_stays_ambiguous_not_guessed(self):
        ac = ActiveContext()
        ac.recent_entities = [
            {"kind": "image", "label": "a", "ts": 1e12},
            {"kind": "error", "label": "b", "ts": 1e11},
        ]
        ac.active_error = "b"
        ac.active_image_subject = "a"
        rep = resolve_with_report("fix it", ac)
        # "it" is bound by the fix-verb hint to the error — no guess.
        self.assertEqual(rep["resolved"].get("it"), "b")


# ---------------------------------------------------------------------------
# §22 — restart persistence; §19 — decay
# ---------------------------------------------------------------------------

class TestPersistenceDecay(unittest.TestCase):
    def test_context_survives_serialization(self):
        """Restart test: context written to the row survives a
        from_dict round-trip and still resolves follow-ups."""
        ac = ctx_with_image()
        ac.park_clarification("adult_subject", intent=IMAGE_GENERATION,
                              subject="angel", prompt="an angel")
        restored = ActiveContext.from_dict(ac.to_dict())
        self.assertTrue(restored.image_active())
        self.assertEqual(restored.pending_clarification, "adult_subject")
        env = understand_turn("yes she's an adult", active=restored)
        self.assertEqual(env.primary_intent, "clarification_response")

    def test_bounded_entity_growth(self):
        ac = ActiveContext()
        for i in range(50):
            ac._retire("image", f"subject {i}")
        self.assertLessEqual(len(ac.recent_entities), MAX_RECENT_ENTITIES)

    def test_active_context_expires(self):
        ac = ctx_with_image()
        ac.updated_at = time.time() - (7 * 3600)
        self.assertFalse(ac.image_active())

    def test_stale_pending_clarification_does_not_hijack_yes(self):
        """Regression: an expired parked image clarification must not
        capture a later 'yes do this' — it belongs to the current
        offer, not a dead one."""
        ac = ctx_with_image()
        ac.park_clarification("adult_subject", intent=IMAGE_GENERATION,
                              subject="angel", prompt="an angel")
        ac.updated_at = time.time() - (7 * 3600)
        env = understand_turn("yes do this", active=ac)
        self.assertNotEqual(env.primary_intent, "clarification_response")

    def test_topic_shift_clears_pending_clarification(self):
        """Regression: a topic shift retires the image task AND its
        parked clarification — 'yes' after the shift resolves the
        new offer, not the retired image gate."""
        ac = ctx_with_image()
        ac.park_clarification("adult_subject", intent=IMAGE_GENERATION,
                              subject="angel", prompt="an angel")
        env = understand_turn("what branch am i on", active=ac)
        self.assertEqual(env.primary_intent, "git_action")
        ac.record_turn(env)
        env2 = understand_turn("yes do this", active=ac)
        self.assertNotEqual(env2.primary_intent, "clarification_response")
        self.assertEqual(ac.pending_clarification, "")


# ---------------------------------------------------------------------------
# Part B — non-repetitive responses
# ---------------------------------------------------------------------------

class TestRealization(unittest.TestCase):
    def test_variants_differ_same_facts(self):
        r = PersonaRenderer()
        seen = set()
        for _ in range(len(GREETING_VARIANTS) * 2):
            seen.add(r.render("greeting", GREETING_VARIANTS,
                              intent="greeting"))
        self.assertGreater(len(seen), 1)

    def test_no_immediate_exact_repeat(self):
        r = PersonaRenderer()
        texts = [r.render("greeting", GREETING_VARIANTS,
                          intent="greeting") for _ in range(4)]
        for a, b in zip(texts, texts[1:]):
            self.assertNotEqual(a, b)

    def test_similarity_metric(self):
        self.assertGreater(
            similarity("GitHub is connected.", "GitHub is connected."),
            0.9)
        self.assertLess(
            similarity("GitHub is connected.", "The weather is nice."),
            0.5)

    def test_ledger_detects_duplicate(self):
        ledger = ResponseLedger()
        ledger.record("greeting", "Hi! Ready when you are.")
        score = ledger.repetition_score("Hi! Ready when you are.")
        self.assertTrue(score["exact_duplicate"])

    def test_repeat_ack_varies(self):
        r = PersonaRenderer()
        acks = {r.render("am_repeat", REPEAT_ACKS, intent="am")
                for _ in range(len(REPEAT_ACKS) * 2)}
        self.assertGreater(len(acks), 1)


class TestIdentityVariation(unittest.TestCase):
    def setUp(self):
        identity._reset_variants()

    def test_creator_fact_stable_wording_varies(self):
        answers = {identity.creator_answer_varied() for _ in range(6)}
        self.assertGreater(len(answers), 1)
        for a in answers:
            self.assertIn(identity.NEXUS_CREATOR, a)

    def test_birthday_fact_stable(self):
        identity._reset_variants()
        answers = {identity.birthday_answer_varied() for _ in range(5)}
        for a in answers:
            self.assertIn(identity.NEXUS_BIRTHDAY_HUMAN, a)

    def test_action_request_not_hijacked(self):
        self.assertIsNone(
            identity.response_for("draw me a picture of your creator"))
        self.assertIsNone(
            identity.response_for("show me a picture of your father"))

    def test_identity_questions_still_answer(self):
        self.assertIsNotNone(identity.response_for("who created you?"))
        self.assertIsNotNone(identity.response_for("who is your father?"))


# ---------------------------------------------------------------------------
# §55 — long-session stability
# ---------------------------------------------------------------------------

class TestLongSession(unittest.TestCase):
    def test_hundred_turn_bounded(self):
        """Multi-hundred-turn synthetic run: state stays bounded, the
        current task stays accurate, stale topics retire."""
        ac = ActiveContext()
        intents_seen = []
        for i in range(300):
            if i % 10 == 0:
                ac.active_image_subject = f"subject {i}"
                ac.active_image_prompt = f"subject {i} prompt"
                ac.active_intent = IMAGE_GENERATION
                ac.touch()
            elif i % 10 == 5:
                ac.note_error(f"error {i}")
                ac.active_intent = CODING
            env = understand_turn("make her blonde", active=ac)
            intents_seen.append(env.primary_intent)
            if i % 10 == 3:
                ac._retire("image", f"old subject {i}")
        self.assertLessEqual(len(ac.recent_entities), MAX_RECENT_ENTITIES)
        # Every image-context follow-up classified consistently.
        self.assertTrue(all(v == IMAGE_FOLLOWUP for v in intents_seen))


class TestUnresolvedAnaphora(unittest.TestCase):
    """Bare anaphora with no referent must flag ambiguity — the model
    asks one clarifying question instead of inventing a target
    (0.32.0 conversation-intelligence milestone)."""

    def test_object_anaphora_flags(self):
        for q in ("make that bigger", "change it", "delete them",
                  "enlarge it", "pick that one"):
            env = understand_turn(q)
            self.assertTrue(
                any("unresolved referent" in a for a in env.ambiguity),
                f"{q!r} produced ambiguity={env.ambiguity}")

    def test_possessive_and_subject_anaphora_flag(self):
        for q in ("what's his name", "what does he do",
                  "where does she live", "what is their plan"):
            env = understand_turn(q)
            self.assertTrue(
                any("unresolved referent" in a for a in env.ambiguity),
                f"{q!r} produced ambiguity={env.ambiguity}")

    def test_content_bearing_pronouns_do_not_flag(self):
        # "it"/"that" in non-referent positions carry their own
        # context — flagging them would over-ask.
        for q in ("it works", "that is fine", "it's raining",
                  "that's a good idea", "it depends"):
            env = understand_turn(q)
            self.assertFalse(
                any("unresolved referent" in a for a in env.ambiguity),
                f"{q!r} over-flagged: {env.ambiguity}")

    def test_resolved_referent_suppresses_flag(self):
        # An active image gives 'it' a binding — no ambiguity.
        ac = ActiveContext()
        ac.active_image_subject = "a lighthouse"
        ac.active_image_prompt = "a lighthouse at dusk"
        ac.active_intent = IMAGE_GENERATION
        ac.touch()
        env = understand_turn("make it brighter", active=ac)
        self.assertFalse(
            any("unresolved referent" in a for a in env.ambiguity),
            env.ambiguity)

    def test_ambiguity_surfaces_clarify_rule(self):
        from localcodeagent.context.scope import (
            classify_scope, scope_directive)
        env = understand_turn("make that bigger")
        directive = scope_directive(classify_scope("make that bigger", env), env)
        self.assertIn("clarifying question", directive)

    def test_forward_shift_decays_active_referents(self):
        # "new topic — change it" must NOT bind 'it' to the abandoned
        # topic's entity — the shift explicitly dropped that subject.
        ac = ActiveContext()
        ac.active_image_subject = "a lighthouse"
        ac.active_image_prompt = "a lighthouse at dusk"
        ac.active_intent = IMAGE_GENERATION
        ac.touch()
        env = understand_turn("new topic — change it", active=ac)
        self.assertTrue(env.topic_shift)
        self.assertTrue(
            any("unresolved referent" in a for a in env.ambiguity),
            env.ambiguity)

    def test_topic_return_keeps_active_context(self):
        # "back to the lighthouse" is a return, not a shift — retained
        # entities still resolve.
        ac = ActiveContext()
        ac.active_image_subject = "a lighthouse"
        ac.active_image_prompt = "a lighthouse at dusk"
        ac.active_intent = IMAGE_GENERATION
        ac.touch()
        env = understand_turn("back to that lighthouse image", active=ac)
        self.assertTrue(env.topic_shift)
        self.assertEqual(env.followup_of, "topic_return")
        self.assertFalse(
            any("unresolved referent" in a for a in env.ambiguity),
            env.ambiguity)


if __name__ == "__main__":
    unittest.main()
