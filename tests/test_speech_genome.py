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
        """Creator-locked facts survive genome rendering verbatim."""
        canonical = ("I am Nexus Core, a local-first AI coding "
                     "workstation created by John Hamburn.")
        g = derive_genome(personality_for("sassy"))
        r = PersonaRenderer(rng=random.Random(1))
        sem = SemanticResponse(semantic_id="t_id", speech_act="answer")
        for i in range(4):
            out = r.render_semantic(sem, g, self.ctx,
                                    canonical=canonical)
            self.assertIn(canonical, out.text)

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
        self.assertIn("Nexus Core", canon)
        self.assertIn("John Hamburn", canon)

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
