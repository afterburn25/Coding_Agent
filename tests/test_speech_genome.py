"""Persona Speech Genome test suite.

Covers: schema versioning/migration, per-preset genome derivation,
idiolect distinctiveness, speech-act classification, register/seriousness
adaptation, relationship + address policies, uncertainty calibration,
humor suppression, phrase cooldowns, repeat evolution, Answer Memory /
builtin integration, fact-integrity invariants, delivery plans, the
preview API surface, and a long-session repetition soak.
"""
from __future__ import annotations

import random
import re
import unittest

from localcodeagent.context.realize import (
    PersonaRenderer, RenderContext, SemanticResponse,
    classify_speech_act)
from localcodeagent.personality.genome import (
    SPEECH_GENOME_VERSION, derive_genome, genome_summary,
    migrate_genome)
from localcodeagent.personality.presets import get_preset, list_presets


def personality_for(preset_id: str) -> dict:
    """Simulated PersonalityStore.resolve_active() output for a preset."""
    p = get_preset(preset_id)
    return {
        "name": p["name"], "base_preset": p["id"],
        "traits": dict(p["traits"]), "voice": dict(p.get("voice") or {}),
        "greeting_style": p.get("greeting_style") or "default",
        "address": p.get("address") or "",
        "speech_genome": dict(p.get("speech_genome") or {}),
        "personality_id": f"preset:{p['id']}",
    }


FACT_SEM = SemanticResponse(
    semantic_id="t_fact", speech_act="answer",
    facts=["The build uses Python 3.11.4.",
           "The entry point is localcodeagent/server.py."],
    exact_spans=["3.11.4", "localcodeagent/server.py"])

SUCCESS_SEM = SemanticResponse(
    semantic_id="t_success", speech_act="report_success",
    facts=["All 42 tests passed in 0.31s."],
    actions_completed=["ran python -m unittest"],
    exact_spans=["42 tests", "0.31s"])

WARN_SEM = SemanticResponse(
    semantic_id="t_warn", speech_act="warn",
    facts=["This deletes local history permanently."],
    warnings=["This cannot be undone."],
    exact_spans=["permanently"])


class TestGenomeSchema(unittest.TestCase):
    def test_version_pinned(self):
        g = derive_genome({})
        self.assertEqual(g["speech_genome_version"],
                         SPEECH_GENOME_VERSION)

    def test_required_sections(self):
        g = derive_genome({})
        for section in (
                "vocabulary", "syntax", "cadence", "pragmatics", "humor",
                "disagreement", "storytelling", "questions", "repair",
                "relationship", "address", "boundaries", "vocal",
                "micro_reactions", "repetition", "confidence"):
            self.assertIn(section, g, f"missing section {section}")

    def test_migrate_none_returns_default(self):
        g = migrate_genome(None)
        self.assertEqual(g["speech_genome_version"],
                         SPEECH_GENOME_VERSION)
        self.assertIn("vocabulary", g)

    def test_migrate_preserves_known_fields_fills_missing(self):
        g = migrate_genome({"syntax": {"fragment_rate": 0.9}})
        self.assertEqual(g["syntax"]["fragment_rate"], 0.9)
        self.assertIn("cadence", g)

    def test_migrate_never_raises_on_garbage(self):
        for raw in ({"a": 1}, [], "x", 42, {"humor": None},
                    {"speech_genome_version": 99}):
            g = migrate_genome(raw)
            self.assertEqual(g["speech_genome_version"],
                             SPEECH_GENOME_VERSION)

    def test_explicit_overrides_merge(self):
        p = personality_for("calm")
        p["speech_genome"] = {
            "vocabulary": {"signature_words": ["bespoke"]},
            "syntax": {"dash_usage": 0.99}}
        g = derive_genome(p)
        self.assertIn("bespoke", g["vocabulary"]["signature_words"])
        self.assertAlmostEqual(g["syntax"]["dash_usage"], 0.99)
        # untouched fields still inherited
        self.assertIn("humor", g)


class TestPresetDerivation(unittest.TestCase):
    def test_all_presets_derive(self):
        for p in list_presets(include_adult=True):
            g = derive_genome(personality_for(p["id"]))
            self.assertIn("vocabulary", g)
            self.assertIn("syntax", g)

    def test_families_differ(self):
        """Personas must differ in structure, not just wording."""
        prof = derive_genome(personality_for("professional"))
        rude = derive_genome(personality_for("rude"))
        play = derive_genome(personality_for("playful"))
        calm = derive_genome(personality_for("calm"))
        self.assertGreater(
            rude["disagreement"]["directness"],
            prof["disagreement"]["directness"])
        self.assertGreater(
            rude["syntax"]["fragment_rate"],
            prof["syntax"]["fragment_rate"])
        self.assertGreater(
            play["cadence"]["tempo"], calm["cadence"]["tempo"])
        self.assertGreater(
            len([c for c in play["humor"]["categories"].values()
                 if c["strength"] > 0]),
            len([c for c in prof["humor"]["categories"].values()
                 if c["strength"] > 0]))

    def test_distinct_idiolects(self):
        """Same semantic → recognizably different surface per persona."""
        pids = ["professional", "sassy", "nerdy", "playful", "rude"]
        surfaces = {}
        for pid in pids:
            r = PersonaRenderer(rng=random.Random(3))
            g = derive_genome(personality_for(pid))
            out = r.render_semantic(
                SUCCESS_SEM, g, RenderContext(register="coding"))
            surfaces[pid] = out.text
        # At least 3 distinct surfaces across 5 personas on ONE render —
        # identical output would mean personas are one voice with a
        # costume, which is exactly what this milestone forbids.
        distinct = len(set(surfaces.values()))
        self.assertGreaterEqual(distinct, 3,
                                f"surfaces too similar: {surfaces}")

    def test_signature_vocabulary_flows(self):
        """Each family's acknowledgement pool is its own."""
        prof = derive_genome(personality_for("professional"))
        sassy = derive_genome(personality_for("sassy"))
        prof_acks = set(prof["vocabulary"]["acknowledgements"])
        sassy_acks = set(sassy["vocabulary"]["acknowledgements"])
        self.assertTrue(prof_acks)
        self.assertTrue(sassy_acks)
        self.assertNotEqual(prof_acks, sassy_acks)


class TestSpeechActClassifier(unittest.TestCase):
    def test_intent_mapping(self):
        self.assertEqual(classify_speech_act("greeting"), "greet")
        self.assertEqual(classify_speech_act("farewell"), "farewell")
        self.assertEqual(classify_speech_act("correction"), "correct")
        self.assertEqual(classify_speech_act("teaching"), "teach")

    def test_outcome_wins(self):
        self.assertEqual(
            classify_speech_act("code_fix", outcome="failed"),
            "report_failure")
        self.assertEqual(
            classify_speech_act("question", outcome="denied"), "deny")
        self.assertEqual(
            classify_speech_act("question", outcome="uncertain"),
            "admit_uncertainty")

    def test_seriousness_escalates(self):
        self.assertEqual(
            classify_speech_act("question", seriousness=3), "alert")

    def test_social_cue_shapes_noncanned(self):
        self.assertEqual(
            classify_speech_act("question", cue="frustrated"),
            "reassure")
        # canned lanes keep their own act
        self.assertEqual(
            classify_speech_act("greeting", cue="frustrated"), "greet")


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.ctx = RenderContext(register="casual")

    def test_facts_verbatim(self):
        g = derive_genome(personality_for("playful"))
        r = PersonaRenderer(rng=random.Random(1))
        out = r.render_semantic(FACT_SEM, g, self.ctx)
        for span in FACT_SEM.exact_spans:
            self.assertIn(span, out.text)
        for fact in FACT_SEM.facts:
            self.assertIn(fact, out.text)

    def test_identity_canonical_verbatim(self):
        """Creator-locked facts survive realization — wording may vary,
        the identity atoms (names, workstation) may not."""
        canonical = ("I'm Nexus — I live and work in Nexus Core, the "
                     "workstation my father John Hamburn built.")
        g = derive_genome(personality_for("sassy"))
        r = PersonaRenderer(rng=random.Random(1))
        sem = SemanticResponse(semantic_id="t_id", speech_act="answer")
        for i in range(4):
            out = r.render_semantic(sem, g, self.ctx,
                                    canonical=canonical)
            for locked in ("Nexus", "Nexus Core", "John Hamburn"):
                self.assertIn(locked, out.text)

    def test_verified_gets_no_hedge(self):
        g = derive_genome(personality_for("calm"))
        r = PersonaRenderer(rng=random.Random(1))
        sem = SemanticResponse(semantic_id="t_ver",
                               speech_act="answer",
                               confidence="verified",
                               facts=["X is Y."])
        out = r.render_semantic(sem, g, self.ctx)
        for stem in g["confidence"].values():
            self.assertNotIn(stem.rstrip(":"), out.text)

    def test_uncertain_gets_persona_stem(self):
        g = derive_genome(personality_for("calm"))
        r = PersonaRenderer(rng=random.Random(1))
        stem = g["confidence"]["uncertain"]
        sem = SemanticResponse(semantic_id="t_unc",
                               speech_act="admit_uncertainty",
                               confidence="uncertain",
                               facts=["The sync may lag."])
        out = r.render_semantic(sem, g, self.ctx)
        self.assertIn(stem, out.text)

    def test_serious_suppresses_humor_and_micro(self):
        g = derive_genome(personality_for("playful"))
        r = PersonaRenderer(rng=random.Random(1))
        ctx = RenderContext(register="casual", seriousness=3)
        out = r.render_semantic(WARN_SEM, g, ctx)
        self.assertEqual(out.closing_family, "hard_stop")
        self.assertEqual(out.micro_reaction, "")
        self.assertGreaterEqual(out.plan.seriousness, 2)

    def test_register_gates_humor(self):
        """Playful persona in a technical register doesn't joke."""
        g = derive_genome(personality_for("playful"))
        r = PersonaRenderer(rng=random.Random(2))
        quips = 0
        for _ in range(20):
            out = r.render_semantic(
                SUCCESS_SEM, g, RenderContext(register="debugging"))
            if out.closing_family == "light_comment":
                quips += 1
        self.assertEqual(quips, 0)

    def test_humor_feedback_bias(self):
        """Negative feedback on a humor style starves its genome
        categories; positive favors them — the same ledger the prompt
        card narrates."""
        r = PersonaRenderer(rng=random.Random(3))
        disliked = RenderContext(
            register="casual",
            humor_feedback={"deadpan": {"neg": 5, "pos": 0}})
        favored = RenderContext(
            register="casual",
            humor_feedback={"deadpan": {"neg": 0, "pos": 5}})
        self.assertEqual(
            r._humor_bias("deadpan", disliked), 0.25)
        self.assertEqual(
            r._humor_bias("deadpan", favored), 1.5)
        # Unrelated categories are untouched.
        self.assertEqual(
            r._humor_bias("sarcasm", disliked), 1.0)
        # Sparse or mixed feedback doesn't move the needle.
        mixed = RenderContext(
            register="casual",
            humor_feedback={"deadpan": {"neg": 2, "pos": 2}})
        self.assertEqual(r._humor_bias("deadpan", mixed), 1.0)

    def test_humor_feedback_reduces_quips(self):
        g = derive_genome(personality_for("deadpan"))
        # Suppress every category the deadpan genome carries — style
        # names AND category names both land in the ledger.
        fb = {k: {"neg": 6, "pos": 0}
              for k, v in g["humor"]["categories"].items()
              if v.get("strength")}
        quips, baseline = 0, 0
        for seed in range(6):
            r = PersonaRenderer(rng=random.Random(seed))
            for _ in range(30):
                if r.render_semantic(
                        SUCCESS_SEM, g,
                        RenderContext(register="casual",
                                      humor_feedback=fb)
                ).closing_family == "light_comment":
                    quips += 1
            r2 = PersonaRenderer(rng=random.Random(seed))
            for _ in range(30):
                if r2.render_semantic(
                        SUCCESS_SEM, g,
                        RenderContext(register="casual")
                ).closing_family == "light_comment":
                    baseline += 1
        self.assertGreater(baseline, 0)
        self.assertLess(quips, baseline // 2)

    def test_saturation_damps_humor(self):
        g = derive_genome(personality_for("deadpan"))
        quips, baseline = 0, 0
        for seed in range(6):
            r = PersonaRenderer(rng=random.Random(seed))
            for _ in range(30):
                if r.render_semantic(
                        SUCCESS_SEM, g,
                        RenderContext(register="casual", saturation=1.0)
                ).closing_family == "light_comment":
                    quips += 1
            r2 = PersonaRenderer(rng=random.Random(seed))
            for _ in range(30):
                if r2.render_semantic(
                        SUCCESS_SEM, g,
                        RenderContext(register="casual")
                ).closing_family == "light_comment":
                    baseline += 1
        self.assertGreater(baseline, 0)
        self.assertLess(quips, baseline)

    def test_address_policy(self):
        g_none = derive_genome(personality_for("professional"))  # formal
        g_first = dict(g_none)
        g_first["address"] = dict(g_none["address"])
        g_first["address"]["policy"] = "first"
        g_first["relationship"] = dict(g_none["relationship"])
        g_first["relationship"]["address_frequency"] = 0.95
        r = PersonaRenderer(rng=random.Random(0))
        ctx = RenderContext(register="casual", address="John")
        saw = False
        for _ in range(10):
            out = r.render_semantic(SUCCESS_SEM, g_first, ctx)
            if out.used_address:
                self.assertIn("John", out.text)
                saw = True
        self.assertTrue(saw, "address term never appeared")
        # "none" policy never addresses
        r2 = PersonaRenderer(rng=random.Random(0))
        for _ in range(20):
            out = r2.render_semantic(
                SUCCESS_SEM, g_none,
                RenderContext(register="casual", address="John"))
            self.assertFalse(out.used_address)
            self.assertNotIn("John,", out.text)

    def test_delivery_plan(self):
        g = derive_genome(personality_for("playful"))
        r = PersonaRenderer(rng=random.Random(1))
        out = r.render_semantic(SUCCESS_SEM, g,
                                RenderContext(register="coding"))
        plan = out.plan
        self.assertGreaterEqual(plan.pace, 0.5)
        self.assertLessEqual(plan.pace, 1.8)
        self.assertEqual(plan.emphasis_spans, SUCCESS_SEM.exact_spans)
        self.assertEqual(plan.speech_act, "report_success")
        self.assertEqual(plan.register, "coding")

    def test_delivery_pace_differs(self):
        """Vocal bias lands in the plan — calm vs playful pace."""
        calm = derive_genome(personality_for("calm"))
        play = derive_genome(personality_for("playful"))
        sem = SemanticResponse(semantic_id="t_pace",
                               speech_act="answer", facts=["Done."])
        rc = PersonaRenderer(rng=random.Random(5))
        rp = PersonaRenderer(rng=random.Random(5))
        pc = rc.render_semantic(sem, calm, self.ctx).plan.pace
        pp = rp.render_semantic(sem, play, self.ctx).plan.pace
        self.assertGreater(pp, pc)

    def test_repeat_evolution(self):
        g = derive_genome(personality_for("professional"))
        r = PersonaRenderer(rng=random.Random(1))
        idxs = []
        texts = set()
        for _ in range(6):
            out = r.render_semantic(SUCCESS_SEM, g, self.ctx)
            idxs.append(out.repeat_index)
            texts.add(out.text)
        self.assertEqual(idxs, [0, 1, 2, 3, 4, 5])
        # Later replays evolve shape — at idx>=2 the body compresses to
        # the load-bearing fact with honest repeat framing.
        late = r.render_semantic(SUCCESS_SEM, g, self.ctx)
        self.assertGreaterEqual(late.repeat_index, 3)

    def test_canonical_repeat_gets_ack(self):
        g = derive_genome(personality_for("warm"))
        r = PersonaRenderer(rng=random.Random(1))
        sem = SemanticResponse(semantic_id="t_am", speech_act="answer")
        canon = "The capital of France is Paris."
        r.render_semantic(sem, g, self.ctx, canonical=canon)
        out2 = r.render_semantic(sem, g, self.ctx, canonical=canon)
        self.assertIn(canon, out2.text)
        self.assertNotEqual(out2.text, canon)  # honest repeat framing

    def test_opening_cooldown(self):
        """A just-used opening can't immediately recur."""
        g = derive_genome(personality_for("nerdy"))
        r = PersonaRenderer(rng=random.Random(4))
        sem = SemanticResponse(semantic_id="t_cd", speech_act="answer",
                               facts=["Point one."])
        openings = []
        for _ in range(8):
            out = r.render_semantic(sem, g, self.ctx)
            openings.append(out.text.split(".")[0])
        # No three identical openings back-to-back at this rate.
        for i in range(len(openings) - 2):
            self.assertFalse(
                openings[i] == openings[i + 1] == openings[i + 2],
                f"opening repeated 3x consecutively: {openings}")

    def test_multi_turn_consistency(self):
        """Persona stays itself across a session — the idiolect pools
        it draws from are ITS pools."""
        g = derive_genome(personality_for("professional"))
        r = PersonaRenderer(rng=random.Random(9))
        prof_markers = ("Understood", "Noted", "Confirmed", "Complete",
                        "Finished", "Status")
        hits = 0
        total = 24
        for i in range(total):
            out = r.render_semantic(
                SemanticResponse(semantic_id=f"t_mt{i}",
                                 speech_act="report_success",
                                 facts=[f"Job {i} finished."]),
                g, self.ctx)
            if any(m in out.text for m in prof_markers):
                hits += 1
        self.assertGreater(hits, 0)


class TestBuiltinIntegration(unittest.TestCase):
    def _agent(self, preset_id=None):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        agent = AgentOrchestrator.__new__(AgentOrchestrator)
        if preset_id is None:
            agent._speech_resolver = None
        else:
            g = derive_genome(personality_for(preset_id))
            ctx = RenderContext(register="casual")
            agent._speech_resolver = lambda ut: (g, ctx)
        return agent

    def test_semantic_detection(self):
        agent = self._agent()
        pair = agent.builtin_semantic("what time is it")
        self.assertIsNotNone(pair)
        sem, canon = pair
        self.assertEqual(sem.speech_act, "answer")
        self.assertIn("local time", canon)
        self.assertIsNone(agent.builtin_semantic("definitely not a lane"))

    def test_identity_lane_semantic(self):
        agent = self._agent()
        pair = agent.builtin_semantic("what's your name")
        self.assertIsNotNone(pair)
        sem, canon = pair
        # Nexus is the person answering; Nexus Core is her workstation —
        # the canonical names her and never self-describes as software.
        self.assertIn("Nexus", canon)
        self.assertNotIn("not a person", canon.lower())
        self.assertNotIn("software", canon.lower())

    def _github_registry(self, authed: bool):
        from localcodeagent.capabilities import CapabilityRegistry
        return CapabilityRegistry(env={
            "github_enabled": lambda: True,
            "github_authorized": lambda: authed,
        })

    def test_github_status_lane_connected(self):
        agent = self._agent()
        agent.capabilities = self._github_registry(authed=True)
        out = agent._builtin_reply("can you connect to github")
        self.assertIsNotNone(out)
        self.assertIn("connected", out.text.lower())
        self.assertNotIn("not connected", out.text.lower())

    def test_github_status_lane_unauthorized(self):
        agent = self._agent()
        agent.capabilities = self._github_registry(authed=False)
        out = agent._builtin_reply("i need you to connect to github")
        self.assertIsNotNone(out)
        self.assertIn("credential", out.text.lower())

    def test_github_status_lane_skips_actions_and_other(self):
        agent = self._agent()
        agent.capabilities = self._github_registry(authed=True)
        # Action requests stay with the tools/model lane — the status
        # lane only answers connection/status questions.
        self.assertIsNone(agent._github_status_reply("push this to github"))
        self.assertIsNone(agent._github_status_reply("open a github pr"))
        self.assertIsNone(agent._github_status_reply("who are you"))

    def _github_target_agent(self, prior_invite: bool = True,
                             repos=None, tool_error: bool = False,
                             current: str = "afterburn25"):
        import json as _json
        from types import SimpleNamespace
        agent = self._agent()
        assistant = (
            "GitHub's already connected — I'm authorized. "
            "Point me at a repo and I'll get to work."
            if prior_invite else "Sure, sounds good.")
        agent.conversation_manager = SimpleNamespace(
            active=lambda: {"messages": [
                {"role": "user", "content": "connect to github"},
                {"role": "assistant", "content": assistant},
                # The in-flight turn is already in history at lane time.
                {"role": "user", "content": current},
            ]})
        repos = repos if repos is not None else [
            {"full_name": "afterburn25/Coding_Agent"},
            {"full_name": "afterburn25/Notes"},
            {"full_name": "other/Shared"}]
        if tool_error:
            agent.tools = SimpleNamespace(
                execute=lambda name, args: "ERROR: no token")
        else:
            agent.tools = SimpleNamespace(
                execute=lambda name, args: _json.dumps(
                    {"repositories": repos}))
        return agent

    def test_github_target_lane_owner(self):
        agent = self._github_target_agent()
        out = agent._github_target_reply("afterburn25")
        self.assertIsNotNone(out)
        self.assertIn("Coding_Agent", out.text)
        self.assertIn("afterburn25", out.text)

    def test_github_target_lane_exact_repo(self):
        agent = self._github_target_agent(current="coding_agent")
        out = agent._github_target_reply("coding_agent")
        self.assertIsNotNone(out)
        self.assertIn("afterburn25/Coding_Agent", out.text)
        agent2 = self._github_target_agent(current="afterburn25/Notes")
        out2 = agent2._github_target_reply("afterburn25/Notes")
        self.assertIsNotNone(out2)
        self.assertIn("afterburn25/Notes", out2.text)

    def test_github_target_lane_unknown_lists_repos(self):
        agent = self._github_target_agent(current="not-a-repo")
        out = agent._github_target_reply("not-a-repo")
        self.assertIsNotNone(out)
        self.assertIn("don't see", out.text)
        self.assertIn("Coding_Agent", out.text)

    def test_github_target_lane_requires_invite(self):
        # The same bare token WITHOUT the repo invite falls through —
        # no context, no call.
        agent = self._github_target_agent(prior_invite=False)
        self.assertIsNone(agent._github_target_reply("afterburn25"))

    def test_github_target_lane_rejects_non_tokens(self):
        agent = self._github_target_agent()
        self.assertIsNone(agent._github_target_reply("what is github"))
        self.assertIsNone(agent._github_target_reply("please list my repos"))
        self.assertIsNone(agent._github_target_reply("hello world"))

    def test_github_target_lane_tool_failure_falls_through(self):
        agent = self._github_target_agent(tool_error=True)
        self.assertIsNone(agent._github_target_reply("afterburn25"))

    def test_identity_lane_phrasings(self):
        agent = self._agent()
        for q in (
                "who are you", "what are you", "what is your name",
                "whats your name", "are you human", "are you a bot",
                "are you an ai", "are you ai", "are you real",
                "are you a person", "are you nexus", "are you sentient",
                "who's nexus", "tell me about yourself",
                "introduce yourself", "you're a bot", "are you alive"):
            with self.subTest(q=q):
                pair = agent.builtin_semantic(q)
                self.assertIsNotNone(pair, q)
                first = pair[0]
                sid = (getattr(first, "semantic_id", None)
                       or getattr(first, "intent", ""))
                self.assertTrue(sid.startswith("identity"), q)

    def test_origin_lane_phrasings(self):
        agent = self._agent()
        # Creation-verb questions must hit the canonical creator-locked
        # lane (identity.py), never the model — "how could I have created
        # you" used to fall through and produce rambling narration.
        for q in (
                "who made you", "who created you", "who built you",
                "who is your father", "who's your creator",
                "how were you made", "how were you created",
                "how could i have created you", "how did i create you",
                "did i make you", "do you have a father",
                "where did you come from", "are you my creation",
                "are you his daughter"):
            with self.subTest(q=q):
                pair = agent.builtin_semantic(q)
                self.assertIsNotNone(pair, q)
                sid = getattr(pair[0], "semantic_id", "") or ""
                self.assertTrue(sid.startswith("identity"), q)
                self.assertIn("john hamburn", pair[1].lower())

    def test_parentage_lane_acknowledges_creator(self):
        from localcodeagent import identity as _ident
        agent = self._agent()
        # "are you my daughter" is a yes/no about the RELATIONSHIP — she
        # must acknowledge the asker as her father/parent, not recite
        # the canonical name. Unknown asker → canonical creator frame.
        for q in ("are you my daughter", "are you really my daughter",
                  "am i your father", "are you my biological daughter"):
            with self.subTest(q=q):
                pair = agent.builtin_semantic(q)
                self.assertIsNotNone(pair, q)
                sid = getattr(pair[0], "semantic_id", "") or ""
                self.assertTrue(sid.startswith("identity"), q)
                self.assertIn("daughter", pair[1].lower())
        # A non-creator asker gets the correction, not a false "yes".
        for q in ("are you my daughter", "am i your father"):
            with self.subTest(q=q):
                out = _ident.response_for(q, asker_is_creator=False)
                self.assertIsNotNone(out)
                self.assertIn("john hamburn", out.lower())
                self.assertIn("daughter", out.lower())

    def test_birth_lane_phrasings(self):
        agent = self._agent()
        # Birthday questions reveal the birthday — the requested slot.
        for q in ("when were you born", "what's your birthday",
                  "when is your birthday"):
            with self.subTest(q=q):
                pair = agent.builtin_semantic(q)
                self.assertIsNotNone(pair, q)
                sid = getattr(pair[0], "semantic_id", "") or ""
                self.assertTrue(sid.startswith("identity"), q)
                # Canonical NEXUS_BIRTHDAY — not a model guess.
                self.assertIn("september 30", pair[1].lower())
        # Age questions reveal the age — the birthday is supporting
        # context and must NOT appear (response-scope contract).
        for q in ("how old are you", "what's your age"):
            with self.subTest(q=q):
                pair = agent.builtin_semantic(q)
                self.assertIsNotNone(pair, q)
                self.assertIn("old", pair[1].lower())
                self.assertNotIn("september 30", pair[1].lower())
                self.assertNotIn("birthday", pair[1].lower())

    def test_identity_lane_no_model_confabulation(self):
        from localcodeagent import identity as _ident
        # Origin questions must resolve through the canonical locked
        # facts — the same answers every time, never invented dates.
        for q in ("who made you", "how could i have created you"):
            self.assertIsNotNone(_ident.response_for(q), q)

    def test_identity_lane_misses_unrelated(self):
        agent = self._agent()
        for q in (
                "what time is it", "are you done", "who wrote this",
                "are you sure", "are you busy"):
            with self.subTest(q=q):
                pair = agent.builtin_semantic(q)
                first = pair[0] if pair else None
                sid = (getattr(first, "semantic_id", None)
                       or getattr(first, "intent", ""))
                self.assertFalse(
                    sid == "identity" or sid.startswith("identity:"),
                    q)

    def test_address_inversion_repaired(self):
        agent = self._agent()
        out = agent._fix_address_inversion(
            "You call me 'Father' — that's your title, not mine.")
        self.assertIn('I call you "Father"', out)
        self.assertNotIn("call me", out)

    def test_address_inversion_modal_and_bare(self):
        agent = self._agent()
        out = agent._fix_address_inversion(
            "You can call me Father anytime.")
        self.assertIn('I call you "Father"', out)
        out2 = agent._fix_address_inversion("Just call me Father, ok?")
        self.assertIn("call you Father", out2)
        self.assertNotIn("call me", out2)

    def test_address_inversion_keeps_correct_use(self):
        agent = self._agent()
        # Correct direction + vocative use must not be touched.
        s = "I call you \"Father\". What's on your mind, Father?"
        self.assertEqual(agent._fix_address_inversion(s), s)
        s2 = "You called me, Father — as usual."
        self.assertEqual(agent._fix_address_inversion(s2), s2)

    def test_address_inversion_custom_title(self):
        agent = self._agent()
        agent._creator_address = lambda: "Dad"
        out = agent._fix_address_inversion("You call me Dad, right?")
        self.assertIn('I call you "Dad"', out)
        # "Father" still repaired — the default leaks from training data
        # even when the profile picked another title.
        out2 = agent._fix_address_inversion("you call me Father")
        self.assertIn('I call you "Father"', out2)

    def test_genome_reply_vs_plain(self):
        plain = self._agent()._builtin_reply("hi")
        self.assertFalse(plain.genome_rendered)
        genome = self._agent("sassy")._builtin_reply("hi")
        self.assertTrue(genome.genome_rendered)
        self.assertTrue(genome.text)

    def test_answer_memory_repeat_path(self):
        """The AM lane semantic-id keys repeat evolution — second ask
        of the same Q gets honest repeat framing."""
        g = derive_genome(personality_for("warm"))
        r = PersonaRenderer(rng=random.Random(2))
        sem = SemanticResponse(semantic_id="am:abc123",
                               speech_act="answer")
        canon = "Nexus supports local image generation via ComfyUI."
        ctx = RenderContext(register="casual")
        o1 = r.render_semantic(sem, g, ctx, canonical=canon)
        self.assertIn(canon, o1.text)
        o2 = r.render_semantic(sem, g, ctx, canonical=canon)
        self.assertIn(canon, o2.text)
        self.assertGreater(o2.repeat_index, o1.repeat_index)


class TestPreviewAPI(unittest.TestCase):
    def test_preview_renders_shape(self):
        """The preview battery covers acts + returns plans + summary."""
        from localcodeagent.context.realize import PersonaRenderer
        g = derive_genome(personality_for("sassy"))
        r = PersonaRenderer(rng=random.Random(1))
        sem = SemanticResponse(semantic_id="pv", speech_act="disagree",
                               facts=["That plan drops the index."])
        out = r.render_semantic(sem, g, RenderContext(register="casual"))
        d = out.as_dict()
        for k in ("text", "plan", "speech_act", "repeat_index",
                  "opening_family", "closing_family"):
            self.assertIn(k, d)
        self.assertIn("pace", d["plan"])


class TestRepetitionSoak(unittest.TestCase):
    def test_long_session_no_exact_duplicates(self):
        """200 renders of rotating acts — a persona should not emit the
        identical surface twice within a short window (§50 metric)."""
        g = derive_genome(personality_for("playful"))
        r = PersonaRenderer(rng=random.Random(42))
        sems = [
            SemanticResponse(semantic_id=f"soak{i%7}",
                             speech_act=act,
                             facts=[f"Fact {i%7} holds."])
            for i, act in enumerate(
                ["answer", "report_success", "explain", "answer",
                 "report_failure", "answer", "summarize"] * 30)]
        ctx = RenderContext(register="casual")
        recent: list[str] = []
        dupes = 0
        for sem in sems:
            out = r.render_semantic(sem, g, ctx)
            if out.text in recent:
                dupes += 1
            recent = (recent + [out.text])[-6:]
        self.assertLessEqual(dupes, 3,
                             f"{dupes} near-window duplicates in soak")

    def test_soak_metrics_exposed(self):
        g = derive_genome(personality_for("default-nexus"))
        r = PersonaRenderer(rng=random.Random(1))
        sem = SemanticResponse(semantic_id="m1", speech_act="answer",
                               facts=["Metric fact."])
        out = r.render_semantic(sem, g, RenderContext())
        score = r.ledger.repetition_score(out.text, intent="answer")
        self.assertTrue(score["exact_duplicate"])
        score2 = r.ledger.repetition_score("Unrelated reply here.",
                                           intent="answer")
        self.assertFalse(score2["exact_duplicate"])


if __name__ == "__main__":
    unittest.main()
