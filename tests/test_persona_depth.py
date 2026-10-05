"""Persona depth milestone — behavior profiles, seriousness/topic
classification, effective-persona compilation, dynamics (relationship +
mood), and prompt-card rendering. All deterministic, no model calls."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.personality.behavior import (
    PRESET_BEHAVIOR, _FAMILIES, behavior_for, behavior_for_personality,
    guidance_lines)
from localcodeagent.personality.dynamics import (
    PersonaDynamics, mood_event)
from localcodeagent.personality.effective import (
    MODE_OVERLAYS, compile_effective, card_guidance, debug_view)
from localcodeagent.personality.prompt import prompt_context
from localcodeagent.personality.schema import MOODS, SLIDERS
from localcodeagent.personality.seriousness import (
    classify_seriousness, classify_topic)
from localcodeagent.personality.presets import PRESETS, get_preset
from localcodeagent.personality.store import PersonalityStore


def _profile():
    return {"profile_id": "p1", "first_name": "Sam",
            "last_name": "Doe", "sex": "male"}


class TestBehaviorProfiles(unittest.TestCase):
    def test_every_family_has_full_profile(self):
        required = ("motivations", "aversions", "rhythm", "signature",
                    "humor_type", "question_style", "teaching_style",
                    "challenge_style", "praise_style", "criticism_style",
                    "decision_style", "plan_style", "confidence",
                    "error_admission", "tool_failure", "turn_taking",
                    "interruption", "warmup_rate", "baseline_affect",
                    "vocal_prefer", "vocal_bias", "gesture_prefer",
                    "topic_shift", "silence")
        for fam, prof in _FAMILIES.items():
            for key in required:
                self.assertIn(key, prof, f"{fam} missing {key}")
            self.assertTrue(prof["motivations"], fam)
            self.assertIn(prof["rhythm"],
                          ("compact", "measured", "conversational",
                           "energetic", "formal", "fragmented",
                           "narrative", "analytical"))

    def test_preset_overrides_merge_not_replace(self):
        beh = behavior_for("taskmaster", "professional")
        self.assertEqual(beh["rhythm"], "compact")
        self.assertIn("maintain momentum", beh["motivations"])
        # Family fields survive the override.
        self.assertEqual(beh["family"], "professional")
        self.assertTrue(beh["aversions"])

    def test_every_preset_maps_to_a_family(self):
        from localcodeagent.personality.behavior import _FAMILIES as F
        for pid, p in PRESETS.items():
            fam = str(p.get("greeting_style") or "default")
            self.assertIn(fam, F, pid)

    def test_guidance_lines_bounded(self):
        for fam in _FAMILIES:
            lines = guidance_lines(_FAMILIES[fam])
            self.assertLessEqual(len(lines), 18, fam)
            self.assertTrue(any("Drives:" in l for l in lines), fam)


class TestSeriousness(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(classify_seriousness("hey what's up"), "casual")
        self.assertEqual(classify_seriousness(
            "tell me about the project"), "neutral")
        self.assertEqual(classify_seriousness(
            "fix the failing test"), "focused")
        self.assertEqual(classify_seriousness(
            "the service crashed and won't start"), "serious")
        self.assertEqual(classify_seriousness(
            "we had a security breach and lost all my files"),
            "critical")

    def test_critical_outranks_casual(self):
        self.assertEqual(classify_seriousness(
            "lol our production is down"), "critical")

    def test_topic(self):
        self.assertEqual(classify_topic("the build crashed"), "diagnostics")
        self.assertEqual(classify_topic("how was your weekend"), "personal")
        self.assertEqual(classify_topic("refactor this function"),
                         "technical")
        self.assertEqual(classify_topic("hey lol"), "casual")


class TestEffectivePersona(unittest.TestCase):
    def _active(self, preset_id="professional"):
        p = get_preset(preset_id)
        return {"name": p["name"], "base_preset": p["id"],
                "greeting_style": p["greeting_style"],
                "traits": dict(p["traits"]), "strength": 80,
                "mood": "", "vocalizations": "natural"}

    def test_compile_merges_layers(self):
        card = compile_effective(
            self._active("taskmaster"),
            user_text="fix the failing test",
            relationship={"familiarity": 50.0, "stage": "trusted"},
            mode="coding_mode")
        self.assertEqual(card["seriousness"], "focused")
        self.assertEqual(card["mode"], "coding_mode")
        self.assertEqual(card["relationship_stage"], "trusted")
        self.assertEqual(card["rhythm"], "compact")
        self.assertEqual(card["family"], "professional")

    def test_seriousness_caps_humor(self):
        active = self._active("sassy")
        card = compile_effective(active,
                                 user_text="production is down")
        self.assertEqual(card["seriousness"], "critical")
        self.assertEqual(card["humor_type"], "none")

    def test_modifiers_expire(self):
        active = self._active()
        mod = [{"note": "extra silly",
                "trait_offsets": {"silliness": 40},
                "expires_at": time.time() - 5}]
        card = compile_effective(active, modifiers=mod)
        self.assertNotIn("silliness", card["traits"])
        mod[0]["expires_at"] = time.time() + 3600
        card = compile_effective(active, modifiers=mod)
        self.assertEqual(card["traits"]["silliness"], 50 + 40)

    def test_overlay_offsets_apply(self):
        active = self._active()
        card = compile_effective(
            active, overlay={"trait_offsets": {"sarcasm": -30},
                             "notes": ["less sarcasm"]})
        self.assertEqual(
            card["traits"].get("sarcasm",
                               active["traits"].get("sarcasm", 50)),
            max(0, active["traits"].get("sarcasm", 50) - 30))
        self.assertIn("less sarcasm", card["overlay_notes"])

    def test_card_guidance_is_bounded(self):
        card = compile_effective(self._active("nerdy"),
                                 user_text="hello")
        lines = card_guidance(card)
        self.assertLessEqual(len(lines), 18)
        joined = "\n".join(lines)
        self.assertIn("style:", joined.lower())

    def test_debug_view_hides_private_notes(self):
        card = compile_effective(
            self._active(), overlay={"notes": ["secret pref"]})
        dbg = debug_view(card)
        self.assertNotIn("overlay_notes", dbg)


class TestDynamics(unittest.TestCase):
    def _dyn(self, td):
        return PersonaDynamics(Path(td))

    def test_familiarity_grows_and_stages(self):
        with tempfile.TemporaryDirectory() as td:
            dyn = self._dyn(td)
            for _ in range(30):
                dyn.note_turn("tell me more", warmup_rate=1.4)
            rel = dyn.relationship()
            self.assertGreater(rel["familiarity"], 20)
            self.assertIn(rel["stage"], ("familiar", "trusted"))
            self.assertEqual(rel["turns"], 30)

    def test_fast_vs_slow_warmup(self):
        with tempfile.TemporaryDirectory() as td:
            fast, slow = (PersonaDynamics(Path(td) / s)
                          for s in ("a", "b"))
            for _ in range(10):
                fast.note_turn("hi", warmup_rate=1.5)
                slow.note_turn("hi", warmup_rate=0.4)
            self.assertGreater(fast.relationship()["familiarity"],
                               slow.relationship()["familiarity"] + 5)

    def test_mood_transition_then_decay(self):
        with tempfile.TemporaryDirectory() as td:
            dyn = self._dyn(td)
            dyn.note_turn("the app crashed again", baseline_affect="relaxed")
            self.assertEqual(dyn.effective_mood(), "concerned")
            dyn.note_turn("ok")
            dyn.note_turn("ok")
            dyn.note_turn("ok")
            mood = dyn.effective_mood()
            self.assertIn(mood, ("concerned", "relaxed"))
            # Decayed by turns without new emotional input.
            st = dyn.state()["mood"]
            self.assertLessEqual(st["intensity"], 0.35)

    def test_success_lifts_concern(self):
        with tempfile.TemporaryDirectory() as td:
            dyn = self._dyn(td)
            dyn.note_turn("it's broken", baseline_affect="relaxed")
            self.assertEqual(dyn.effective_mood(), "concerned")
            dyn.note_turn("finally it works")
            self.assertEqual(dyn.effective_mood(), "celebratory")

    def test_manual_mood_holds(self):
        with tempfile.TemporaryDirectory() as td:
            dyn = self._dyn(td)
            dyn.note_turn("it crashed", manual_mood="relaxed")
            self.assertEqual(dyn.effective_mood(manual_mood="relaxed"),
                             "relaxed")

    def test_overlay_and_modifiers_persist(self):
        with tempfile.TemporaryDirectory() as td:
            dyn = self._dyn(td)
            dyn.adjust_overlay({"sarcasm": -30}, note="less sarcasm")
            dyn.add_modifier(trait_offsets={"playfulness": 20},
                             note="playful tonight",
                             ttl_seconds=60)
            fresh = PersonaDynamics(Path(td))
            self.assertEqual(fresh.overlay()["trait_offsets"]
                             .get("sarcasm"), -30)
            self.assertEqual(len(fresh.modifiers()), 1)
            # Expired modifiers prune.
            dyn.add_modifier(trait_offsets={"humor": 10},
                             note="expired", ttl_seconds=-1)
            self.assertEqual(
                len(PersonaDynamics(Path(td)).modifiers()), 1)

    def test_clear_modifiers_and_reset_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            dyn = self._dyn(td)
            dyn.add_modifier(note="temp", trait_offsets={"humor": 5})
            dyn.adjust_overlay({"sass": -10})
            self.assertEqual(dyn.clear_modifiers(), 1)
            self.assertEqual(dyn.modifiers(), [])
            dyn.reset_overlay()
            self.assertEqual(dyn.overlay()["trait_offsets"], {})

    def test_reply_tracking_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            dyn = self._dyn(td)
            for i in range(20):
                dyn.note_reply(f"Opener {i}. Body. Closer {i}.")
            rec = dyn.recent_phrases()
            self.assertLessEqual(len(rec["openers"]), 12)
            self.assertEqual(rec["openers"][-1], "opener 19")

    def test_corrupt_state_resets(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "persona_state.json").write_text("not json")
            dyn = self._dyn(td)
            self.assertEqual(dyn.relationship()["stage"], "new")


class TestPromptCard(unittest.TestCase):
    def test_card_renders_into_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            store = PersonalityStore(Path(td))
            store.set_active("preset:executive-assistant",
                             is_adult=True)
            active = store.resolve_active(is_adult=True)
            dyn = PersonaDynamics(Path(td))
            card = compile_effective(
                active, user_text="fix the deploy",
                relationship=dyn.relationship(), mode="coding_mode")
            text = prompt_context(_profile(), active, effective=card)
            self.assertIn("Active persona:", text)
            self.assertIn("Effective persona", text)
            self.assertIn("Mode: coding", text)
            self.assertIn("unchanged.", text)  # safety boundary

    def test_critical_context_suppresses_vocals_line(self):
        with tempfile.TemporaryDirectory() as td:
            store = PersonalityStore(Path(td))
            store.set_active("preset:default-nexus", is_adult=True)
            active = store.resolve_active(is_adult=True)
            card = compile_effective(
                active, user_text="security breach — data loss")
            text = prompt_context(_profile(), active, effective=card)
            self.assertIn("serious", text.lower())
            self.assertNotIn("vocal reactions are available", text)

    def test_creator_address_direction_is_unambiguous(self):
        # Regression: 'call them: Father' inverted on small models into
        # "you call me Father". The title must belong to the user.
        profile = _profile()
        profile.update({"is_creator": True,
                        "creator_address": "Father",
                        "creator_title_conversation": True})
        text = prompt_context(profile, {"name": "Isabella", "strength": 85})
        self.assertIn("your creator", text)
        self.assertIn('You address the user as "Father"', text)
        self.assertIn("never to you", text)
        self.assertNotIn("call them:", text)

    def test_noncreator_address_still_marks_direction(self):
        text = prompt_context(_profile(), {"name": "Isabella",
                                         "address": "boss"})
        self.assertIn('You address the user as "boss"', text)
        self.assertIn("never to you", text)
        self.assertNotIn("your creator", text)


class TestPersonaCommands(unittest.TestCase):
    def setUp(self):
        from localcodeagent.personality.commands import (
            apply_command, parse_persona_command)
        self.parse = parse_persona_command
        self.apply = apply_command

    def test_less_trait_persistent_overlay(self):
        cmd = self.parse("be less sarcastic")
        self.assertEqual(cmd["op"], "overlay")
        self.assertEqual(cmd["trait_offsets"], {"sarcasm": -20})

    def test_more_trait_with_time_modifier(self):
        cmd = self.parse("be more playful for the next hour")
        self.assertEqual(cmd["op"], "modifier")
        self.assertEqual(cmd["trait_offsets"], {"playfulness": 20})
        self.assertEqual(cmd["ttl_seconds"], 3600.0)

    def test_tone_down(self):
        cmd = self.parse("tone down the sass tonight")
        self.assertEqual(cmd["op"], "modifier")
        self.assertEqual(cmd["trait_offsets"], {"sass": -20})

    def test_mode_commands(self):
        cmd = self.parse("use serious mode")
        self.assertEqual(cmd["op"], "mode")
        self.assertEqual(cmd["mode"], "serious_mode")
        cmd = self.parse("be in debugging mode")
        self.assertEqual(cmd["mode"], "debugging_mode")

    def test_task_scoped_modifier(self):
        cmd = self.parse("be more concise for this task")
        self.assertEqual(cmd["op"], "modifier")
        self.assertEqual(cmd["scope"], "task")
        self.assertEqual(cmd["trait_offsets"], {"verbosity": -20})

    def test_resets(self):
        self.assertEqual(self.parse("reset personality")["op"],
                         "reset_all")
        self.assertEqual(self.parse("back to normal")["op"],
                         "reset_temp")

    def test_non_commands_pass_through(self):
        for t in ("fix the failing test", "what is sarcasm?",
                  "can you be less sarcastic in code comments?",
                  "tell me about your day"):
            self.assertIsNone(self.parse(t), t)

    def test_apply_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            dyn = PersonaDynamics(Path(td))
            res = self.apply(dyn, self.parse("be less sarcastic"))
            self.assertEqual(res["applied"], "overlay")
            self.assertEqual(
                dyn.overlay()["trait_offsets"]["sarcasm"], -20)
            res = self.apply(dyn, self.parse("reset personality"))
            self.assertEqual(res["applied"], "reset_all")
            self.assertEqual(dyn.overlay()["trait_offsets"], {})


class TestCustomize(unittest.TestCase):
    def test_coherence_detects_conflicts(self):
        from localcodeagent.personality.customize import coherence_check
        r = coherence_check({"formality": 90, "slang_usage": 90,
                             "warmth": 90, "rudeness": 85})
        self.assertEqual(r["coherence"], "mixed")
        self.assertTrue(any("Formality" in n for n in r["conflicts"]))
        r = coherence_check({"formality": 90, "patience": 80})
        self.assertEqual(r["coherence"], "high")

    def test_blend_weighted(self):
        from localcodeagent.personality.customize import blend_presets
        merged = blend_presets([{"preset": "professional",
                                 "weight": 0.7},
                                {"preset": "playful", "weight": 0.3}])
        self.assertEqual(merged["base_preset"], "professional")
        self.assertEqual(len(merged["blend"]), 2)
        # Trait merge leans toward the heavier contributor: both
        # presets define formality (80 vs 15) → weighted mean.
        self.assertAlmostEqual(
            merged["traits"].get("formality", 50),
            0.7 * 80 + 0.3 * 15, delta=2)

    def test_blend_requires_valid_presets(self):
        from localcodeagent.personality.customize import (
            blend_presets, create_blend)
        from localcodeagent.profiles.model import ProfileError
        with self.assertRaises(ProfileError):
            blend_presets([{"preset": "nope", "weight": 1}])
        with tempfile.TemporaryDirectory() as td:
            store = PersonalityStore(Path(td))
            custom = create_blend(
                store, [{"preset": "professional", "weight": 0.6},
                        {"preset": "warm", "weight": 0.4}],
                name="ProWarm", is_adult=True)
            self.assertEqual(custom["base_preset"], "professional")
            st = store._load()
            c = store._custom(st, custom["personality_id"])
            self.assertEqual(len(c["blend"]), 2)

    def test_export_import_roundtrip(self):
        from localcodeagent.personality.customize import (
            export_custom, import_custom)
        with tempfile.TemporaryDirectory() as td:
            store = PersonalityStore(Path(td))
            c = store.create_custom(is_adult=True, name="Tester",
                                    base_preset="warm",
                                    traits={"humor": 90})
            pkg = export_custom(store, c["personality_id"])
            self.assertEqual(pkg["format"], "nexus-persona")
            self.assertNotIn("memory", pkg)
            with tempfile.TemporaryDirectory() as td2:
                store2 = PersonalityStore(Path(td2))
                imp = import_custom(store2, pkg, is_adult=True)
                self.assertEqual(imp["name"], "Tester")
                self.assertTrue(imp.get("imported"))

    def test_export_never_carries_memory(self):
        from localcodeagent.personality.customize import export_custom
        with tempfile.TemporaryDirectory() as td:
            store = PersonalityStore(Path(td))
            c = store.create_custom(is_adult=True, name="M",
                                    base_preset="warm")
            pkg = export_custom(store, c["personality_id"])
            for k in pkg:
                self.assertNotIn(k, ("memories", "relationship",
                                     "overlay", "modifiers"))


class TestNotices(unittest.TestCase):
    def test_persona_notice_preserves_facts(self):
        from localcodeagent.personality.notices import persona_notice
        card = {"family": "sassy", "seriousness": "casual"}
        out = persona_notice("queued",
                             "Build task is queued (position 2)", card)
        self.assertIn("Build task is queued (position 2)", out)
        self.assertNotEqual(out, "Build task is queued (position 2)")

    def test_serious_context_strips_flavor(self):
        from localcodeagent.personality.notices import persona_notice
        card = {"family": "sassy", "seriousness": "critical"}
        out = persona_notice("failed", "deploy failed", card)
        self.assertNotIn("flopped", out)
        self.assertIn("deploy failed", out)


class TestConsistency(unittest.TestCase):
    def test_repetition_detects_repeat_opener(self):
        from localcodeagent.personality.consistency import (
            check_repetition)
        recent = {"openers": ["sure thing boss", "sure thing boss"],
                  "closers": []}
        r = check_repetition("Sure thing boss. Here you go.", recent)
        self.assertIn("repeated_opener", r["flags"])

    def test_drift_flags(self):
        from localcodeagent.personality.consistency import detect_drift
        card = {"family": "professional", "seriousness": "casual",
                "rhythm": "formal"}
        r = detect_drift("lol bruh gonna fix that fr fr lol", card)
        self.assertIn("slang_drift", r["flags"])
        card = {"family": "sassy", "seriousness": "critical"}
        r = detect_drift("haha lol the server is down", card)
        self.assertIn("humor_in_serious_context", r["flags"])

    def test_feedforward_hints(self):
        from localcodeagent.personality.consistency import (
            feedforward_hints)
        hints = feedforward_hints(
            {"openers": ["sure", "sure", "ok"],
             "closers": ["done", "done"]})
        self.assertTrue(any("opener" in h for h in hints))


class TestMultiTurnDifferentiation(unittest.TestCase):
    """Personas must compile recognizably different cards for the same
    inputs, and invariants must hold across the board."""

    REPRESENTATIVE = ["professional", "warm", "direct", "nerdy",
                      "playful", "calm", "sassy", "critical-reviewer"]

    SCENARIOS = [
        "hey, how's it going?",
        "can you explain what a mutex is?",
        "the deploy failed again, nothing works",
        "finally got it working!",
        "review my pull request please",
        "production is down, we lost data",
        "write me a haiku about compilers",
        "what do you think about microservices?",
    ]

    def _card(self, preset_id, text, fam=0.0):
        p = get_preset(preset_id)
        if p is None:
            return None
        active = {"name": p["name"], "base_preset": p["id"],
                  "greeting_style": p["greeting_style"],
                  "traits": dict(p["traits"]), "strength": 80,
                  "mood": "", "vocalizations": "natural"}
        return compile_effective(
            active, user_text=text,
            relationship={"familiarity": fam})

    def test_personas_differ(self):
        cards = [self._card(pid, "hey, how's it going?")
                 for pid in self.REPRESENTATIVE]
        cards = [c for c in cards if c]
        self.assertGreaterEqual(len(cards), 6)
        # Style vectors differ meaningfully across personas.
        vectors = set()
        for c in cards:
            vectors.add((c["rhythm"], c["humor_type"],
                         c["question_style"], c["turn_taking"],
                         c["criticism_style"]))
        self.assertGreaterEqual(len(vectors), 5)

    def test_serious_scenario_uniform_suppression(self):
        for pid in self.REPRESENTATIVE:
            card = self._card(pid, "security breach — credentials leaked")
            if card is None:
                continue
            self.assertEqual(card["seriousness"], "critical", pid)
            self.assertEqual(card["humor_type"], "none", pid)

    def test_card_invariants_hold(self):
        """Facts/tools boundary: no card field can carry instructions
        that touch code correctness — card fields are all style."""
        style_keys = {"rhythm", "humor_type", "question_style",
                      "teaching_style", "challenge_style", "praise_style",
                      "criticism_style", "decision_style", "plan_style",
                      "turn_taking", "interruption", "silence",
                      "vocalizations", "gesture_prefer", "vocal_prefer"}
        for pid in self.REPRESENTATIVE:
            card = self._card(pid, "fix the bug")
            if card is None:
                continue
            for k in style_keys:
                self.assertIn(k, card)

    def test_familiarity_progression_per_persona(self):
        """Warm personas warm faster than professional ones."""
        with tempfile.TemporaryDirectory() as td:
            warm = PersonaDynamics(Path(td) / "w")
            pro = PersonaDynamics(Path(td) / "p")
            for _ in range(12):
                warm.note_turn("hi", warmup_rate=1.3)
                pro.note_turn("hi", warmup_rate=0.5)
            self.assertGreater(
                warm.relationship()["familiarity"],
                pro.relationship()["familiarity"])

    def test_relationship_card_line(self):
        card = self._card("warm", "hello again", fam=50.0)
        lines = "\n".join(card_guidance(card))
        self.assertIn("trusted", lines)


if __name__ == "__main__":
    unittest.main()
