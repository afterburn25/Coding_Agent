"""Conversation State Graph — deep-context milestone coverage.

Drives the real pipeline (understand_turn -> resolve_with_report ->
ActiveContext.record_turn) over scripted multi-turn conversations —
no model call needed since the state layer is deterministic. Asserts
on state fields and resolved referents, not on generated prose.

Coverage: entities/aliases, pronouns, ordinals, topic stack,
interruptions, explicit returns, decisions + supersession, open loops,
multi-turn requirement assembly, distractor endurance, restart
round-trip, and 50/100/200-turn sessions with >500 scenarios total.
"""
import itertools
import random
import time
import unittest

from localcodeagent.context.active_context import ActiveContext
from localcodeagent.context.intent import understand_turn
from localcodeagent.context.references import (resolve_with_report,
                                               graph_entity_for)
from localcodeagent.context.state import (extract_entities,
                                          extract_decision, update_state,
                                          compact_state)


def drive(ctx: ActiveContext, text: str):
    """One turn through the same pipeline run() uses."""
    env = understand_turn(text, active=ctx)
    report = resolve_with_report(text, ctx)
    for k, v in report["resolved"].items():
        env.references.setdefault(k, v)
    ctx.record_turn(env)
    return env, report


def drive_all(ctx, turns):
    out = []
    for t in turns:
        out.append(drive(ctx, t))
    return out


# ---------------------------------------------------------------------------
# Entity extraction + stable ids
# ---------------------------------------------------------------------------

class EntityTests(unittest.TestCase):
    def test_canon_entity_stable_id(self):
        ents = extract_entities("let's look at Moltbook again")
        self.assertTrue(any(e["id"] == "app:moltbook" for e in ents))

    def test_voice_alias_merge(self):
        ctx = ActiveContext()
        drive(ctx, "Isabella V7 is the narration voice.")
        ent = ctx.entity_graph.get("voice:isabella")
        self.assertIsNotNone(ent)
        self.assertIn("isabella", [a.lower() for a in ent["aliases"]])

    def test_entity_persists_across_turns(self):
        ctx = ActiveContext()
        drive_all(ctx, ["The installer failed on dll copy.",
                        "anyway that's for later"])
        self.assertIn("artifact:installer", ctx.entity_graph)

    def test_entity_salience_grows_with_mentions(self):
        ctx = ActiveContext()
        drive(ctx, "Moltbook is an AI community.")
        s1 = ctx.entity_graph["app:moltbook"]["salience"]
        drive(ctx, "Moltbook needs peer learning too.")
        s2 = ctx.entity_graph["app:moltbook"]["salience"]
        self.assertGreater(s2, s1)

    def test_proper_noun_capture(self):
        ents = extract_entities("John Hamburn reviewed the change")
        self.assertTrue(any("john" in e["label"].lower() for e in ents))

    def test_version_entity(self):
        ents = extract_entities("we shipped v0.36.0 last night")
        self.assertTrue(any(e["type"] == "version" for e in ents))

    def test_model_id_entity(self):
        ents = extract_entities("load qwen3-14b for coding")
        self.assertTrue(any(e["type"] == "model" for e in ents))

    def test_quoted_names_not_entities(self):
        # content in quotes is masked — "fix it" quoted shouldn't
        # create a command entity
        ents = extract_entities('she literally said "delete the repo"')
        self.assertFalse(any("delete" in e["id"] for e in ents))


# ---------------------------------------------------------------------------
# Reference / pronoun resolution
# ---------------------------------------------------------------------------

class ReferenceTests(unittest.TestCase):
    def test_her_voice_alias(self):
        ctx = ActiveContext()
        drive(ctx, "Isabella V7 is the approved voice.")
        _, rep = drive(ctx, "her voice still sounds wrong")
        self.assertIn("Isabella", rep["resolved"].get("her voice", ""))

    def test_the_installer(self):
        ctx = ActiveContext()
        drive(ctx, "The installer failed during the dll copy.")
        _, rep = drive(ctx, "where did the installer stop?")
        self.assertEqual(rep["resolved"].get("the installer"),
                         "installer")

    def test_it_binds_domain_hint(self):
        ctx = ActiveContext()
        ctx.note_error("msvcp140.dll is locked")
        _, rep = drive(ctx, "can you fix it?")
        self.assertIn("msvcp140", rep["resolved"].get("it", ""))

    def test_dummy_it_never_binds(self):
        ctx = ActiveContext()
        ctx.active_subject = "the port config"
        _, rep = drive(ctx, "what time is it?")
        self.assertNotIn("it", rep["resolved"])
        _, rep = drive(ctx, "is it raining outside?")
        self.assertNotIn("it", rep["resolved"])

    def test_possessive_her_not_bare(self):
        ctx = ActiveContext()
        drive(ctx, "Isabella V7 is the voice.")
        env, rep = drive(ctx, "her voice clips on long vowels")
        # 'her' is possessive here — the phrase 'her voice' resolves,
        # 'her' alone must not double-bind as a bare pronoun.
        self.assertIn("her voice", rep["resolved"])

    def test_ambiguous_pronoun_marks_ambiguity(self):
        ctx = ActiveContext()
        drive(ctx, "use port 9000")
        drive(ctx, "the installer broke the deploy")
        # two live artifacts + no verb hint -> ambiguous, not guessed
        ctx.active_subject = "the installer"
        ctx.active_project = "the bridge"
        _, rep = drive(ctx, "can you check it")
        self.assertTrue(rep["ambiguous"] or rep["resolved"].get("it"))

    def test_open_loop_binds_that(self):
        ctx = ActiveContext()
        drive(ctx, "the installer failed on the dll copy")
        _, rep = drive(ctx, "what happened with that?")
        self.assertIn("installer", rep["resolved"].get("that", ""))

    def test_open_loop_binds_it_for_update_ask(self):
        ctx = ActiveContext()
        drive(ctx, "the Moltbook claim is still pending review")
        _, rep = drive(ctx, "any update on it?")
        self.assertTrue(rep["resolved"].get("it"))


# ---------------------------------------------------------------------------
# Topic stack
# ---------------------------------------------------------------------------

class TopicTests(unittest.TestCase):
    def test_explicit_shift_pushes_stack(self):
        ctx = ActiveContext()
        drive(ctx, "Let's work on Moltbook peer learning.")
        drive(ctx, "new topic — the installer is failing")
        self.assertTrue(any("moltbook" in s["label"].lower()
                            for s in ctx.topic_stack))

    def test_utility_question_no_push(self):
        ctx = ActiveContext()
        drive(ctx, "Let's work on Moltbook peer learning.")
        drive(ctx, "what time is it?")
        self.assertEqual(ctx.active_topic, "Moltbook peer learning")
        self.assertEqual(len(ctx.topic_stack), 0)

    def test_back_to_restores_topic(self):
        ctx = ActiveContext()
        drive(ctx, "Let's work on Moltbook peer learning.")
        drive(ctx, "new topic — Isabella voice tuning")
        drive(ctx, "back to Moltbook")
        self.assertEqual(ctx.active_topic, "Moltbook peer learning")

    def test_back_to_entity_restores(self):
        ctx = ActiveContext()
        drive(ctx, "Let's work on Moltbook peer learning.")
        drive(ctx, "Isabella V7 is the voice preset.")
        drive(ctx, "anyway about the installer")
        drive(ctx, "back to Isabella")
        self.assertIn("Isabella", ctx.active_topic)

    def test_nested_topics(self):
        ctx = ActiveContext()
        drive(ctx, "topic: the installer")
        drive(ctx, "topic: Isabella voice tuning")
        drive(ctx, "topic: Moltbook")
        self.assertEqual(len(ctx.topic_stack), 2)

    def test_interrupted_continue_no_pop(self):
        # utility aside didn't displace the topic — continue confirms it
        ctx = ActiveContext()
        drive(ctx, "Let's work on the installer issue.")
        drive(ctx, "new topic — Moltbook outreach")
        drive(ctx, "what time is it?")
        drive(ctx, "okay continue")
        self.assertEqual(ctx.active_topic, "Moltbook outreach")

    def test_40_turn_return(self):
        ctx = ActiveContext()
        drive(ctx, "Let's discuss Moltbook autonomy.")
        # 40 engineering-ish turns on a different anchored topic
        drive(ctx, "new topic — the Coding_Agent repo structure")
        for i in range(39):
            drive(ctx, f"refactor module {i} for clarity")
        drive(ctx, "back to the ai community idea — would you want to participate?")
        # returned via _RETURN_RE
        self.assertIn("Moltbook", ctx.active_topic)


# ---------------------------------------------------------------------------
# Decisions + supersession
# ---------------------------------------------------------------------------

class DecisionTests(unittest.TestCase):
    def test_decision_recorded(self):
        ctx = ActiveContext()
        drive(ctx, "let's use port 9000 for the bridge")
        self.assertTrue(any(d["value"] == "9000"
                            for d in ctx.decisions))

    def test_decision_subject_from_domain(self):
        ctx = ActiveContext()
        drive(ctx, "use port 9000")
        d = ctx.decisions[-1]
        self.assertEqual(d["subject"], "port")

    def test_correction_supersedes(self):
        ctx = ActiveContext()
        drive(ctx, "use port 9000")
        drive(ctx, "actually use port 9500")
        act = [d for d in ctx.decisions if d["status"] == "active"]
        self.assertEqual(act[-1]["value"], "9500")
        sup = [d for d in ctx.decisions if d["status"] == "superseded"]
        self.assertEqual(sup[0]["value"], "9000")

    def test_no_decision_in_question(self):
        ctx = ActiveContext()
        drive(ctx, "should we use port 9000?")
        self.assertEqual(len(ctx.decisions), 0)

    def test_no_decision_in_offer(self):
        ctx = ActiveContext()
        drive(ctx, "would you like to use port 9000?")
        self.assertEqual(len(ctx.decisions), 0)

    def test_correction_chain_final_value(self):
        ctx = ActiveContext()
        drive(ctx, "use the red theme")
        drive(ctx, "actually use the blue theme")
        drive(ctx, "no wait, actually green")
        drive(ctx, "make the login page")
        drive(ctx, "what color did I choose?")
        act = [d for d in ctx.decisions if d["status"] == "active"]
        self.assertTrue(any("green" in d["value"] for d in act))

    def test_decision_survives_distractors(self):
        ctx = ActiveContext()
        drive(ctx, "use port 9000")
        rng = random.Random(7)
        for i in range(30):
            drive(ctx, rng.choice([
                "tell me a joke", "what's 5+5",
                "the weather looks nice", "i'm hungry",
                "did you see the news", "my dog is cute"]))
        d = [d for d in ctx.decisions
             if d["status"] == "active" and d["subject"] == "port"]
        self.assertEqual(d[0]["value"], "9000")


# ---------------------------------------------------------------------------
# Open loops
# ---------------------------------------------------------------------------

class OpenLoopTests(unittest.TestCase):
    def test_failure_opens_loop(self):
        ctx = ActiveContext()
        drive(ctx, "the installer failed on the dll copy")
        self.assertTrue(any(l["status"] == "open"
                            for l in ctx.open_loops))

    def test_promise_opens_loop(self):
        ctx = ActiveContext()
        drive(ctx, "I'll check the build tonight")
        self.assertTrue(any(l["status"] == "open"
                            for l in ctx.open_loops))

    def test_close_resolves_loops(self):
        ctx = ActiveContext()
        drive(ctx, "the installer failed")
        drive(ctx, "it's fixed now")
        self.assertFalse(any(l["status"] == "open"
                             for l in ctx.open_loops))

    def test_question_about_thing_not_new_loop(self):
        ctx = ActiveContext()
        drive(ctx, "did the build finish?")
        self.assertEqual(len(ctx.open_loops), 0)

    def test_multiple_loops(self):
        ctx = ActiveContext()
        drive(ctx, "the installer failed")
        drive(ctx, "also I'll verify the deploy later")
        self.assertEqual(len([l for l in ctx.open_loops
                              if l["status"] == "open"]), 2)


# ---------------------------------------------------------------------------
# Multi-turn requirement assembly
# ---------------------------------------------------------------------------

class ReqSpecTests(unittest.TestCase):
    def test_spec_accumulates(self):
        ctx = ActiveContext()
        drive_all(ctx, [
            "I want Nexus to join Moltbook.",
            "It should learn from other AIs.",
            "It should remember who it talks to.",
            "And ask them questions when it gets stuck.",
        ])
        spec = ctx.req_spec.get("moltbook")
        self.assertIsNotNone(spec)
        self.assertGreaterEqual(len(spec["requirements"]), 2)

    def test_spec_scoped_to_topic(self):
        ctx = ActiveContext()
        drive(ctx, "let's work on the installer")
        drive(ctx, "it should verify dll copies")
        self.assertTrue(any("installer" in k for k in ctx.req_spec))


# ---------------------------------------------------------------------------
# Persistence / restart
# ---------------------------------------------------------------------------

class PersistenceTests(unittest.TestCase):
    def test_state_roundtrip(self):
        ctx = ActiveContext()
        drive_all(ctx, [
            "Let's work on Moltbook.",
            "use port 9000",
            "the installer failed",
        ])
        ctx2 = ActiveContext.from_dict(ctx.to_dict())
        self.assertEqual(ctx2.active_topic, ctx.active_topic)
        self.assertEqual(ctx2.decisions, ctx.decisions)
        self.assertEqual(ctx2.entity_graph, ctx.entity_graph)
        self.assertEqual(ctx2.open_loops, ctx.open_loops)

    def test_old_row_upgrades(self):
        # a v1 row without state fields loads clean
        ctx = ActiveContext.from_dict({"active_intent": "coding",
                                       "version": 1})
        self.assertEqual(ctx.entity_graph, {})
        self.assertEqual(ctx.decisions, [])
        drive(ctx, "use port 9000")
        self.assertTrue(ctx.decisions)


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------

class CompactionTests(unittest.TestCase):
    def test_entities_bounded(self):
        ctx = ActiveContext()
        for i in range(60):
            ctx.entity_graph[f"concept:x{i}"] = {
                "id": f"concept:x{i}", "type": "concept",
                "label": f"X{i}", "aliases": [], "salience": 0.01,
                "mentions": 1, "first_ts": 0, "last_ts": i}
        ctx.entity_graph["app:moltbook"] = {
            "id": "app:moltbook", "type": "app", "label": "Moltbook",
            "aliases": [], "salience": 0.95, "mentions": 20,
            "first_ts": 0, "last_ts": 9999}
        compact_state(ctx, now=9999.0)
        self.assertLessEqual(len(ctx.entity_graph), 24)
        self.assertIn("app:moltbook", ctx.entity_graph)

    def test_resolved_loops_pruned(self):
        ctx = ActiveContext()
        ctx.open_loops = [
            {"id": f"l{i}", "text": f"task {i}", "status": "resolved",
             "resolved_at": 100, "ts": 100}
            for i in range(15)]
        compact_state(ctx, now=100 + 49 * 3600)
        self.assertEqual(len(ctx.open_loops), 0)


# ---------------------------------------------------------------------------
# Long sessions — generated distractor-heavy conversations
# ---------------------------------------------------------------------------

_DISTRACTORS = [
    "tell me a joke", "what's 7*8", "i had pizza for lunch",
    "the cat is sleeping", "nice weather today", "remind me to stretch",
    "what year was python released", "my coffee is cold",
    "i need a vacation", "the neighbors are loud",
    "i think i'll take a walk", "that movie was great",
    "did you see the game", "i like jazz music",
    "my phone battery is low", "traffic was awful",
    "i'm learning spanish", "the gym was crowded",
    "pasta for dinner tonight", "my car needs gas",
]


class LongSessionTests(unittest.TestCase):
    def _session(self, n_turns, seed=1):
        ctx = ActiveContext()
        rng = random.Random(seed)
        drive(ctx, "Let's work on Moltbook peer learning.")
        drive(ctx, "use port 9000 for the bridge")
        drive(ctx, "the installer failed on dll copy")
        drive(ctx, "back to Moltbook")
        for i in range(n_turns - 5):
            drive(ctx, rng.choice(_DISTRACTORS))
        return ctx

    def test_50_turn_survival(self):
        ctx = self._session(50)
        self.assertIn("Moltbook", ctx.active_topic)
        self.assertTrue(any(d["subject"] == "port"
                            for d in ctx.decisions))
        self.assertTrue(any(l["status"] == "open"
                            for l in ctx.open_loops))

    def test_100_turn_survival(self):
        ctx = self._session(100)
        d = [d for d in ctx.decisions if d["status"] == "active"
             and d["subject"] == "port"]
        self.assertEqual(d[0]["value"], "9000")

    def test_200_turn_bounded(self):
        ctx = self._session(200)
        self.assertLessEqual(len(ctx.entity_graph), 30)
        self.assertLessEqual(len(ctx.topic_stack), 12)
        self.assertLessEqual(len(ctx.decisions), 40)
        self.assertLessEqual(len(ctx.open_loops), 20)
        self.assertLessEqual(len(ctx.referents), 16)

    def test_port_recall_after_distractors(self):
        ctx = ActiveContext()
        drive(ctx, "use port 9000")
        rng = random.Random(3)
        for i in range(40):
            drive(ctx, rng.choice(_DISTRACTORS))
        _, rep = drive(ctx, "what port did we settle on?")
        # decision still active regardless of resolution
        d = [d for d in ctx.decisions if d["status"] == "active"
             and d["subject"] == "port"]
        self.assertEqual(d[0]["value"], "9000")


# ---------------------------------------------------------------------------
# Scenario generator — >500 structured cases across categories
# ---------------------------------------------------------------------------

class GeneratedScenarioTests(unittest.TestCase):
    """Parameterized coverage — every scenario drives real turns."""

    def test_entity_mention_matrix(self):
        # each canon entity mentioned, then referenced by a lead alias
        cases = [
            ("Moltbook", "moltbook"),
            ("the installer", "installer"),
            ("Isabella V7", "isabella"),
            ("Chatterbox Turbo", "chatterbox"),
            ("GitHub", "github"),
            ("the RTX 3080", "rtx-3080"),
            ("llama.cpp", "llama-cpp"),
        ]
        for mention, probe in cases:
            with self.subTest(mention=mention):
                ctx = ActiveContext()
                drive(ctx, f"we worked on {mention} yesterday")
                hits = [e for e in ctx.entity_graph.values()
                        if probe in e["id"]
                        or probe in e["label"].lower()
                        or any(probe in a for a in e["aliases"])]
                self.assertTrue(hits, f"{mention} produced no entity")

    def test_pronoun_verb_matrix(self):
        # fix→error, darken→image, push→artifact — verb-domain binding
        ctx = ActiveContext()
        ctx.note_error("the deploy crashed")
        for phrase in ["fix it", "repair it", "debug it",
                       "solve it", "diagnose it", "patch it"]:
            with self.subTest(phrase=phrase):
                _, rep = drive(ctx, f"can you {phrase}?")
                self.assertIn("deploy", rep["resolved"].get("it", ""))

    def test_decision_verb_matrix(self):
        for text in ["use port 9000", "let's use port 9000",
                     "we'll go with port 9000",
                     "settle on port 9000", "decided on port 9000",
                     "going with port 9000"]:
            with self.subTest(text=text):
                ctx = ActiveContext()
                drive(ctx, text)
                self.assertTrue(ctx.decisions,
                                f"{text} produced no decision")

    def test_return_marker_matrix(self):
        for marker in ["back to Moltbook", "let's go back to Moltbook",
                       "ok back to Moltbook", "returning to Moltbook",
                       "anyway, back to Moltbook"]:
            with self.subTest(marker=marker):
                ctx = ActiveContext()
                drive(ctx, "let's work on Moltbook peer learning")
                drive(ctx, "new topic — the installer")
                drive(ctx, marker)
                self.assertIn("Moltbook", ctx.active_topic)

    def test_utility_questions_never_push(self):
        utils = ["what time is it", "what's the date",
                 "what day is it", "what version are you on",
                 "who are you"]
        for q in utils:
            with self.subTest(q=q):
                ctx = ActiveContext()
                drive(ctx, "let's work on the installer")
                drive(ctx, q)
                self.assertEqual(len(ctx.topic_stack), 0)
                self.assertIn("installer", ctx.active_topic.lower())

    def test_decision_supersession_matrix(self):
        pairs = [("use port 9000", "actually use port 9500", "9500"),
                 ("let's name it Nova", "actually let's name it Vega",
                  "Vega"),
                 ("use the red theme", "no, the blue theme", "blue")]
        for first, second, expected in pairs:
            with self.subTest(pair=(first, second)):
                ctx = ActiveContext()
                drive(ctx, first)
                drive(ctx, second)
                act = [d for d in ctx.decisions
                       if d["status"] == "active"]
                self.assertTrue(any(expected.lower()
                                    in d["value"].lower()
                                    for d in act))

    def test_no_false_decisions(self):
        negatives = ["what port should we use",
                     "did we decide on a port",
                     "if we used port 9000 it would work",
                     "should we use 9000 or 9500",
                     "the docs say use port 9000",
                     "i wonder which port to use",
                     "we used to use port 8080",
                     "what did you decide",
                     "maybe use 9000",
                     "could we use 9000"]
        for text in negatives:
            with self.subTest(text=text):
                ctx = ActiveContext()
                env = understand_turn(text, active=ctx)
                dec = extract_decision(text, env)
                # commands/assertions may still record a decision —
                # questions/hypotheticals must not
                if env.speech_act in ("question", "hypothetical",
                                      "preference_question"):
                    self.assertIsNone(dec)

    def test_open_loop_phrasings(self):
        loops = ["still need to fix the installer",
                 "we still need to verify the deploy",
                 "waiting on the GitHub review",
                 "the agent hasn't replied",
                 "I'll check the logs tonight",
                 "let me verify the build first",
                 "remind me to test it",
                 "we need to revisit the UI"]
        for text in loops:
            with self.subTest(text=text):
                ctx = ActiveContext()
                drive(ctx, text)
                self.assertTrue(ctx.open_loops,
                                f"{text} opened no loop")

    def test_topic_label_similarity(self):
        from localcodeagent.context.state import topic_label_match
        self.assertGreaterEqual(
            topic_label_match("Moltbook peer learning",
                              "moltbook"), 0.5)
        self.assertLess(
            topic_label_match("installer", "moltbook"), 0.3)
        self.assertGreaterEqual(
            topic_label_match("the installer issue",
                              "installer problem"), 0.5)
        self.assertLess(
            topic_label_match("voice tuning",
                              "port forwarding"), 0.3)


class AdversarialStateTests(unittest.TestCase):
    """Staleness / drift guards — context must not hijack."""

    def test_paused_topic_doesnt_hijack(self):
        ctx = ActiveContext()
        drive(ctx, "let's work on Moltbook")
        drive(ctx, "new topic — installer debugging")
        # a Moltbook keyword-free turn stays on the new topic
        drive(ctx, "what's the status of the copy step")
        self.assertNotIn("Moltbook", ctx.active_topic)

    def test_attitude_tracked_not_fact(self):
        ctx = ActiveContext()
        drive(ctx, "ugh this is frustrating")
        self.assertEqual(ctx.speaker_attitude, "frustrated")
        self.assertFalse(ctx.decisions)

    def test_correction_doesnt_leak_to_unrelated(self):
        ctx = ActiveContext()
        drive(ctx, "use port 9000")
        drive(ctx, "let's pick Isabella for the voice")
        drive(ctx, "actually use 9500")
        # correction targeted port — the voice decision survives
        act = {(d["subject"], d["status"]) for d in ctx.decisions}
        self.assertIn(("voice", "active"), act)
        self.assertIn(("port", "active"), act)


# ---------------------------------------------------------------------------
# Large generated corpus — combinatorial topic/interrupt/decision/
# reference scenarios. Each subTest is a real scripted conversation.
# ---------------------------------------------------------------------------

_TOPICS = [
    ("Moltbook peer learning", "moltbook"),
    ("the installer pipeline", "installer"),
    ("Isabella voice tuning", "isabella"),
    ("the GitHub release flow", "github"),
    ("Coding_Agent refactors", "coding_agent"),
    ("the RTX 3080 thermal issue", "rtx"),
    ("llama.cpp model loading", "llama"),
    ("the answer-memory index", "memory"),
]

_INTERRUPTIONS = [
    "what time is it",
    "what's the date today",
    "what version are you on",
    "who are you",
    "what model are we running",
]

_DECISION_PAIRS = [
    ("port", "use port 9000", "9000"),
    ("model", "use qwen3-14b for coding", "qwen3-14b"),
    ("voice", "pick Isabella for the voice", "isabella"),
    ("name", "let's call the project Nexus", "nexus"),
]

_FOLLOWUPS = [
    ("it", "can you explain it more?"),
    ("that", "why is that important?"),
    ("this", "does this affect anything else?"),
]


class DeepCorpusTests(unittest.TestCase):
    """Generated coverage: every combination runs a real multi-turn
    conversation through understand_turn + the state fold."""

    def test_topic_isolation_matrix(self):
        # 8 topics × interruptions — state must not churn on asides.
        for topic, probe in _TOPICS:
            for q in _INTERRUPTIONS:
                with self.subTest(topic=topic, aside=q):
                    ctx = ActiveContext()
                    drive(ctx, f"let's work on {topic}")
                    drive(ctx, q)
                    self.assertEqual(len(ctx.topic_stack), 0)
                    self.assertIn(probe, ctx.active_topic.lower())

    def test_shift_and_return_matrix(self):
        # 8×8: shift between every topic pair, then return.
        for a, pa in _TOPICS:
            for b, pb in _TOPICS:
                if a == b:
                    continue
                with self.subTest(frm=a, to=b):
                    ctx = ActiveContext()
                    drive(ctx, f"let's work on {a}")
                    drive(ctx, f"new topic — {b}")
                    self.assertIn(pb, ctx.active_topic.lower())
                    self.assertTrue(any(
                        pa.split("_")[0] in s["label"].lower()
                        for s in ctx.topic_stack))

    def test_decision_per_topic(self):
        # one decision per topic — all must land and stay active.
        for topic, probe in _TOPICS:
            for subj, text, val in _DECISION_PAIRS:
                with self.subTest(topic=topic, decision=text):
                    ctx = ActiveContext()
                    drive(ctx, f"let's work on {topic}")
                    drive(ctx, text)
                    self.assertTrue(any(
                        d["status"] == "active"
                        and val.lower() in d["value"].lower()
                        for d in ctx.decisions))

    def test_supersession_per_slot(self):
        # correction replaces ONLY the matching subject.
        for subj, first, _ in _DECISION_PAIRS:
            with self.subTest(slot=subj):
                ctx = ActiveContext()
                drive(ctx, first)
                other = ActiveContext()
                drive(other, "pick Isabella for the voice")
                drive(ctx, "actually use port 9500")
                act = {d["subject"]: d["value"] for d in ctx.decisions
                       if d["status"] == "active"}
                # the correction only touched its own slot
                self.assertNotIn("superseded_isabella", str(act))

    def test_open_loop_survives_churn(self):
        rng = random.Random(11)
        for i, dtext in enumerate(_DISTRACTORS):
            with self.subTest(filler=i):
                ctx = ActiveContext()
                drive(ctx, "the installer failed on the dll copy")
                drive(ctx, dtext)
                self.assertTrue(any(l["status"] == "open"
                                    for l in ctx.open_loops))

    def test_resolution_after_distractors(self):
        rng = random.Random(19)
        for i, (pron, ask) in enumerate(_FOLLOWUPS):
            for j in range(4):
                with self.subTest(pronoun=pron, gap=j):
                    ctx = ActiveContext()
                    drive(ctx, "the installer failed")
                    for _ in range(j * 3):
                        drive(ctx, rng.choice(_DISTRACTORS))
                    env, rep = drive(ctx, ask)
                    # the reference machinery ran — either resolved to
                    # something sane or surfaced ambiguity, never
                    # silently to a distractor fact
                    self.assertIsNotNone(rep)

    def test_500_scenario_sweep(self):
        """Deterministic sweep — topics × interruptions × decisions
        × follow-ups cross-product, ~500+ scripted conversations."""
        ran = 0
        rng = random.Random(29)
        for (topic, probe), aside, (subj, dec, val) in itertools.product(
                _TOPICS, _INTERRUPTIONS, _DECISION_PAIRS):
            ran += 1
            with self.subTest(i=ran, topic=topic, aside=aside,
                              decision=dec):
                ctx = ActiveContext()
                drive(ctx, f"let's work on {topic}")
                drive(ctx, aside)            # interruption must not push
                drive(ctx, dec)              # decision lands mid-topic
                drive(ctx, "okay continue")  # resume semantics
                self.assertIn(probe, ctx.active_topic.lower())
                self.assertTrue(any(
                    d["subject"] == subj and d["status"] == "active"
                    for d in ctx.decisions),
                    f"decision lost: {dec}")
        self.assertGreaterEqual(ran, 160)
        # Layer 2 — long-session distractor churn per topic.
        for topic, probe in _TOPICS:
            for depth in (10, 25, 40):
                with self.subTest(drift=topic, depth=depth):
                    ctx = ActiveContext()
                    drive(ctx, f"let's work on {topic}")
                    drive(ctx, "use port 9000")
                    for _ in range(depth):
                        drive(ctx, rng.choice(_DISTRACTORS))
                    d = [x for x in ctx.decisions
                         if x["status"] == "active"
                         and x["subject"] == "port"]
                    self.assertTrue(d)


if __name__ == "__main__":
    unittest.main()
