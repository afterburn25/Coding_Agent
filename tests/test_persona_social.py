"""Persona social-continuity layer tests (v0.17.0 milestone).

Covers: social-cue classification, sarcasm detection (vs generation),
user energy + persona energy blending, long-session pacing, focus
tracking, shared-history milestones + relevance-gated callbacks,
stated-preference consistency, preferred address, saturation
dampening, humor adaptation, extended NL commands, voice smoothing,
gesture timing, self-description, persona comparison + similarity,
QA metrics, and a 50-turn continuity stress run.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


def _dyn(td):
    from localcodeagent.personality.dynamics import PersonaDynamics
    return PersonaDynamics(Path(td))


def _persona(preset_id="playful"):
    from localcodeagent.personality.presets import get_preset
    from localcodeagent.personality import schema
    p = get_preset(preset_id) or get_preset("default-nexus")
    return {"name": p["name"], "base_preset": p["id"],
            "traits": schema.clean_traits(p.get("traits"),
                                          is_adult=True),
            "voice": schema.clean_voice(p.get("voice")),
            "greeting_style": p.get("greeting_style") or "default",
            "strength": 70, "mood": "", "vocalizations": "natural",
            "is_adult": True}


def _card(preset_id="playful", user_text="", state=None, **kw):
    from localcodeagent.personality.effective import compile_effective
    return compile_effective(_persona(preset_id), user_text=user_text,
                             state=state or {}, is_adult=True, **kw)


class SocialCueTests(unittest.TestCase):

    def setUp(self):
        from localcodeagent.personality import social
        self.soc = social

    def test_cue_frustration(self):
        self.assertEqual(self.soc.classify_social(
            "ugh this bug keeps crashing again")["cue"], "frustration")

    def test_cue_celebration(self):
        self.assertEqual(self.soc.classify_social(
            "finally it works!")["cue"], "celebration")

    def test_cue_confusion(self):
        self.assertEqual(self.soc.classify_social(
            "I don't get it, this makes no sense")["cue"], "confusion")

    def test_cue_venting(self):
        self.assertEqual(self.soc.classify_social(
            "I'm so done with this pipeline")["cue"], "venting")

    def test_cue_joking(self):
        self.assertEqual(self.soc.classify_social(
            "lol nice try")["cue"], "joking")

    def test_cue_uncertainty(self):
        self.assertEqual(self.soc.classify_social(
            "I'm not sure, maybe it's the cache?")["cue"], "uncertainty")

    def test_neutral_default(self):
        self.assertEqual(self.soc.classify_social(
            "list the files in src")["cue"], "neutral")

    def test_sarcasm_positive_over_negative(self):
        r = self.soc.classify_social("Great, it broke again.")
        self.assertEqual(r["cue"], "sarcasm")
        self.assertTrue(r["sarcasm"])

    def test_sarcasm_needs_context_or_marker(self):
        # Positive words alone, no negative context → not sarcasm.
        self.assertFalse(self.soc.classify_social(
            "great, thanks!")["sarcasm"])
        # Positive over prior failure context → sarcasm.
        self.assertTrue(self.soc.classify_social(
            "well, that went perfectly", context_failed=True)["sarcasm"])

    def test_obvious_sarcasm_markers(self):
        self.assertTrue(self.soc.classify_social(
            "wow what a surprise, more errors")["sarcasm"])

    def test_user_energy(self):
        self.assertEqual(self.soc.user_energy("yeah"), "low")
        self.assertEqual(self.soc.user_energy(
            "it works!! this is amazing", "celebration"), "high")
        self.assertEqual(self.soc.user_energy(
            "can you add a retry loop to the uploader for me"), "medium")

    def test_effective_energy_capped_by_seriousness(self):
        from localcodeagent.personality.behavior import behavior_for
        beh = behavior_for("playful", "playful")
        e = self.soc.effective_energy(beh, user_energy="high",
                                      seriousness="critical")
        self.assertNotEqual(e, "high")

    def test_effective_energy_user_low_pulls_down(self):
        from localcodeagent.personality.behavior import behavior_for
        beh = behavior_for("playful", "playful")
        e = self.soc.effective_energy(beh, user_energy="low")
        self.assertNotEqual(e, "high")   # energetic persona calms down

    def test_pacing_decays_over_session(self):
        self.assertEqual(self.soc.pacing_factor(5), 1.0)
        self.assertLess(self.soc.pacing_factor(60),
                        self.soc.pacing_factor(20))
        # Emotional turns get a temporary pass.
        self.assertEqual(self.soc.pacing_factor(60, "celebration"), 1.0)

    def test_focus_tags(self):
        self.assertEqual(self.soc.tag_focus("the build keeps failing"),
                         "bug")
        self.assertEqual(self.soc.tag_focus("update the changelog"),
                         "docs")
        self.assertTrue(self.soc.topic_shifted("bug", "docs"))
        self.assertFalse(self.soc.topic_shifted("bug", ""))
        self.assertFalse(self.soc.topic_shifted("", "docs"))


class DynamicsContinuityTests(unittest.TestCase):

    def test_note_turn_records_social_state(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            st = d.note_turn("Great, the build crashed again")
            self.assertTrue(st["sarcasm_detected"])
            self.assertEqual(st["last_cue"], "sarcasm")
            self.assertEqual(int(st["metrics"]["user_sarcasm"]), 1)
            self.assertEqual(st["focus"]["topic"], "bug")

    def test_focus_shift_tracked(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            d.note_turn("the bug is fixed")
            st = d.note_turn("now update the docs and changelog")
            self.assertEqual(st["focus"]["topic"], "docs")
            self.assertEqual(st["focus"]["prev"], "bug")

    def test_celebration_records_milestone(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            st = d.note_turn("finally it works!!")
            kinds = [m["kind"] for m in st["milestones"]]
            self.assertIn("success", kinds)

    def test_recurring_issue_milestone(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            st = d.note_turn("this error is still crashing")
            kinds = [m["kind"] for m in st["milestones"]]
            self.assertIn("recurring_issue", kinds)

    def test_trivial_turns_make_no_milestones(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            for t in ("list files", "ok", "and the next one"):
                d.note_turn(t)
            self.assertEqual(d.state()["milestones"], [])

    def test_callback_requires_relevance(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            d.note_turn("finally the startup lock issue is fixed!!")
            # Unrelated turn → no callback.
            self.assertIsNone(d.callback_for("what's for lunch"))
            # Topically related turn → the milestone surfaces.
            hit = d.callback_for(
                "the startup lock is blocking boot again")
            self.assertIsNotNone(hit)
            self.assertIn("startup lock", hit["event"])

    def test_callback_cooldown_prevents_repeats(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            d.note_turn("finally the startup lock issue is fixed!!")
            hit1 = d.callback_for("the startup lock broke again")
            self.assertIsNotNone(hit1)
            # Same query right away — cooldown suppresses the repeat.
            hit2 = d.callback_for("the startup lock broke again")
            self.assertTrue(hit2 is None or
                            hit2.get("last_referenced"))

    def test_address_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            self.assertEqual(d.set_address("Ash"), "Ash")
            self.assertEqual(d.state()["address"], "Ash")
            self.assertEqual(d.set_address("none"), "")

    def test_name_use_counted(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            d.set_address("Ash")
            d.note_turn("hi")
            d.note_reply("Sure thing, Ash — here's the plan.")
            self.assertEqual(int(d.state()["metrics"]["name_uses"]), 1)

    def test_reply_metrics_and_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            d.note_turn("try the build")
            d.note_reply("It failed again — the compiler choked. "
                         "Want me to retry?")
            st = d.state()
            self.assertEqual(st["last_outcome"], "failed")
            self.assertEqual(int(st["metrics"]["questions"]), 1)
            self.assertGreater(int(st["metrics"]["words"]), 5)
            # Next sarcastic remark gets failure context.
            st = d.note_turn("well, that went perfectly")
            self.assertTrue(st["sarcasm_detected"])

    def test_saturation_dampens_sarcastic_humor(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            st = d.note_turn("hi")
            from localcodeagent.personality import continuity
            for _ in range(4):
                continuity.note_expression(st, ["sarcasm"])
            card = _card("sassy", "funny stuff", state=st)
            # sassy's humor is sarcastic; saturated → drops to dry.
            self.assertEqual(card["humor_type"], "dry")

    def test_humor_feedback_adapts(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            for _ in range(3):
                d.humor_feedback("sarcasm", positive=False)
            card = _card("sassy", "hi", state=d.state())
            self.assertIn("poorly", card["humor_adaptation"])

    def test_stated_preference_consistency(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            d.record_stated_pref("concise technical explanations",
                                 "prefers")
            card = _card("nerdy",
                         "can you give me a technical explanation?",
                         state=d.state())
            self.assertIn("prefers", card["preference_hint"])

    def test_pattern_freshness(self):
        from localcodeagent.personality import continuity
        st = {"patterns": {}}
        continuity.note_pattern(st, "analogy", "like-a-recipe")
        self.assertFalse(continuity.pattern_fresh(
            st, "analogy", "like-a-recipe"))
        self.assertTrue(continuity.pattern_fresh(
            st, "analogy", "like-a-garden"))

    def test_metrics_rates(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            for t in ("fix the bug?", "ok", "and now?"):
                d.note_turn(t)
                d.note_reply("Done. Want the next step?")
            m = d.metrics()
            self.assertEqual(m["turns"], 3)
            self.assertGreater(m["avg_words"], 0)
            self.assertGreater(m["question_rate"], 0)

    def test_reset_continuity_preserves_relationship(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            d.note_turn("finally it works!!")
            d.record_stated_pref("dry humor", "likes")
            d.reset_continuity()
            st = d.state()
            self.assertEqual(st["milestones"], [])
            self.assertEqual(st["stated_prefs"], [])
            self.assertGreater(st["familiarity"], 0)   # preserved

    def test_profile_isolation(self):
        with tempfile.TemporaryDirectory() as a, \
                tempfile.TemporaryDirectory() as b:
            da, db = _dyn(a), _dyn(b)
            da.set_address("Ash")
            da.note_turn("finally it works!!")
            self.assertEqual(db.state()["address"], "")
            self.assertEqual(db.state()["milestones"], [])


class EffectiveCardSocialTests(unittest.TestCase):

    def test_card_has_social_fields(self):
        card = _card("warm", "ugh this keeps failing")
        for k in ("social_cue", "sarcasm_detected", "user_energy",
                  "energy", "pacing", "saturation", "focus",
                  "confidence_delivery", "noticing", "curiosity",
                  "stable_prefs"):
            self.assertIn(k, card)
        self.assertEqual(card["social_cue"], "frustration")

    def test_guidance_flags_sarcasm(self):
        from localcodeagent.personality.effective import card_guidance
        card = _card("warm", "Great, it broke again.",
                     state={"last_outcome": "failed"})
        lines = "\n".join(card_guidance(card))
        self.assertIn("sarcasm", lines.lower())

    def test_guidance_energy_low(self):
        from localcodeagent.personality.effective import card_guidance
        # Formal baseline (medium) + a terse low-energy user → low.
        card = _card("professional", "yeah", state={"turns": 5})
        self.assertEqual(card["energy"], "low")
        self.assertTrue(any("Energy: low" in l
                            for l in card_guidance(card)))

    def test_energetic_persona_calms_for_tired_user(self):
        card = _card("playful", "yeah, whatever",
                     state={"turns": 5})
        self.assertNotEqual(card["energy"], "high")

    def test_guidance_long_session_pacing(self):
        from localcodeagent.personality.effective import card_guidance
        card = _card("playful", "add a retry", state={"turns": 55})
        self.assertLess(card["pacing"], 1.0)
        self.assertTrue(any("long session" in l
                            for l in card_guidance(card)))

    def test_address_hint_in_card(self):
        card = _card("warm", "hi",
                     state={"address": "Ash", "familiarity": 60.0})
        self.assertIn("Ash", card["address_hint"])

    def test_callback_event_reaches_card(self):
        cb = {"event": "shared success — startup lock resolved"}
        card = _card("warm", "the startup lock broke again",
                     callback=cb)
        self.assertIn("startup lock", card["callback_event"])

    def test_stable_prefs_in_card(self):
        card = _card("nerdy", "explain the scheduler")
        self.assertTrue(any("technical" in p
                            for p in card["stable_prefs"]))

    def test_serious_still_suppresses_everything(self):
        card = _card("sassy", "production is down, credentials leaked",
                     state={"turns": 3})
        self.assertEqual(card["humor_type"], "none")
        self.assertIn(card["seriousness"], ("serious", "critical"))


class CommandExtensionTests(unittest.TestCase):

    def setUp(self):
        from localcodeagent.personality.commands import (
            apply_command, parse_persona_command)
        self.parse = parse_persona_command
        self.apply = apply_command

    def test_call_me(self):
        cmd = self.parse("call me Ash")
        self.assertEqual(cmd["op"], "address")
        self.assertEqual(cmd["address"], "Ash")

    def test_call_me_rejects_clause(self):
        self.assertIsNone(self.parse("call me when it's done"))

    def test_dont_use_my_name(self):
        cmd = self.parse("don't use my name")
        self.assertEqual(cmd["op"], "address")
        self.assertEqual(cmd["address"], "")

    def test_what_are_you_like(self):
        self.assertEqual(self.parse("what are you like?")["op"],
                         "describe")
        self.assertEqual(
            self.parse("describe your personality")["op"], "describe")

    def test_reset_adaptations(self):
        cmd = self.parse("reset the learned adaptations")
        self.assertEqual(cmd["op"], "reset_adaptations")

    def test_apply_address_and_reset(self):
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            r = self.apply(d, self.parse("call me Ash"))
            self.assertEqual(r["applied"], "address")
            self.assertEqual(d.state()["address"], "Ash")
            d.note_turn("finally it works!!")
            r = self.apply(d, {"op": "reset_adaptations"})
            self.assertEqual(d.state()["milestones"], [])
            self.assertEqual(d.state()["address"], "Ash")  # preserved


class IntrospectTests(unittest.TestCase):

    def test_describe_persona_honest(self):
        from localcodeagent.personality.introspect import (
            describe_persona)
        card = _card("professional", "hi")
        text = describe_persona(card, _persona("professional"))
        # She describes herself as a person — the preset is her manner,
        # not a "persona" she is running.
        self.assertIn("Nexus", text)
        self.assertIn("delivery, not substance", text)
        self.assertNotIn("persona", text.lower())
        # Never claims invented traits — only resolved ones.
        self.assertNotIn("goofy", text.lower())

    def test_compare_personas_differs(self):
        import tempfile as _tf
        from localcodeagent.personality.store import PersonalityStore
        from localcodeagent.personality.introspect import (
            compare_personas)
        with _tf.TemporaryDirectory() as td:
            store = PersonalityStore(Path(td))
            rows = compare_personas(store, ["professional", "playful"],
                                    user_text="explain the queue")
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["summary"]["humor"],
                                rows[1]["summary"]["humor"])
            self.assertNotEqual(rows[0]["preview"], rows[1]["preview"])
            self.assertIn("speed", rows[0]["voice"])

    def test_similarity_self_flag(self):
        from localcodeagent.personality.introspect import (
            persona_similarity)
        r = persona_similarity("professional", "professional")
        self.assertGreaterEqual(r["similarity"], 0.9)
        self.assertTrue(r["flag"])

    def test_similarity_distinct_personas(self):
        from localcodeagent.personality.introspect import (
            persona_similarity)
        r = persona_similarity("professional", "playful",
                               "professional", "playful")
        self.assertLess(r["similarity"], 0.9)

    def test_similarity_audit_runs(self):
        from localcodeagent.personality.introspect import (
            similarity_audit)
        flagged = similarity_audit(["professional", "playful",
                                    "warm", "minimalist"])
        self.assertIsInstance(flagged, list)

    def test_behavior_report_flags(self):
        from localcodeagent.personality.introspect import (
            behavior_report)
        r = behavior_report({"question_rate": 1.5,
                             "name_use_rate": 0.9})
        self.assertTrue(any("question" in f for f in r["flags"]))
        self.assertTrue(any("name" in f for f in r["flags"]))


class VoiceAndGestureTests(unittest.TestCase):

    def test_voice_smoothing_clamps_jump(self):
        from localcodeagent.personality.voice_map import map_voice
        base = map_voice({}, {}, strength=70)
        shifted = map_voice(
            {"speaking_speed": 100, "pitch_variation": 100}, {},
            strength=100, mood="excited", previous=base)
        self.assertLessEqual(shifted["speed"] - base["speed"], 0.081)
        self.assertLessEqual(
            shifted["pitch_semitones"] - base["pitch_semitones"], 0.61)

    def test_voice_no_previous_unchanged(self):
        from localcodeagent.personality.voice_map import map_voice
        a = map_voice({"speaking_speed": 80}, {})
        b = map_voice({"speaking_speed": 80}, {}, previous=None)
        self.assertEqual(a["speed"], b["speed"])

    def test_hesitation_gate(self):
        from localcodeagent.voice.vocalizations import (
            VocalizationEngine)
        eng = VocalizationEngine()
        ctx = {"level": "natural", "strength": 100,
               "hesitation_ok": False, "style": "default"}
        r = eng.resolve("Hmm, the answer is 42.", task_id="t1",
                        ctx=ctx)
        self.assertNotIn("Hmm", r.speech_text)

    def test_gesture_timing_field(self):
        from localcodeagent.voice.vocalizations import (
            VocalizationEngine)
        eng = VocalizationEngine()
        ctx = {"level": "expressive", "strength": 100,
               "style": "default"}
        r = eng.resolve("Ha! That's great, (laughs) nicely done.",
                        task_id="t2", ctx=ctx)
        for ev in r.events:
            self.assertIn(ev.get("timing"), ("pre", "with"))


class FiftyTurnStressTests(unittest.TestCase):
    """Long synthetic conversation — persona stays coherent, pacing
    tapers, moods transition, no state blowup."""

    _SCRIPT = (
        ["hey"] * 3
        + ["the build keeps crashing, help me debug it"] * 10
        + ["Great, it broke again. classic"] * 5
        + ["still failing — this is annoying"] * 5
        + ["can you explain the retry logic?"] * 10
        + ["finally it works!! we fixed it"] * 5
        + ["update the docs and the changelog"] * 6
        + ["what's next on the plan?"] * 6
    )

    def test_fifty_turn_continuity(self):
        from localcodeagent.personality.effective import (
            card_guidance, compile_effective)
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            cards = []
            for text in self._SCRIPT:
                st = d.state()
                card = compile_effective(
                    _persona("playful"), user_text=text,
                    relationship=d.relationship(), state=st,
                    callback=d.callback_for(text))
                cards.append(card)
                d.note_turn(text)
                d.note_reply("Got it — here's the step.")
            st = d.state()
            # Relationship grew, mood transitioned, state stayed sane.
            self.assertGreater(st["familiarity"], 20)
            self.assertIn(st["focus"]["topic"], ("docs", "planning"))
            self.assertTrue(any(m["kind"] == "success"
                                for m in st["milestones"]))
            # Pacing tapered by late session.
            self.assertLess(cards[-1]["pacing"], cards[0]["pacing"])
            # Persona identity preserved throughout.
            self.assertTrue(all(c["family"] == "playful"
                                for c in cards))
            # Sarcasm turns were detected.
            self.assertTrue(cards[15]["sarcasm_detected"])
            # Cards render.
            self.assertTrue(all(card_guidance(c) for c in cards))
            # JSON-serializable state (persistable).
            json.dumps(st)

    def test_strong_persona_no_caricature(self):
        """Sassy saturating on sarcasm still produces measured cards."""
        from localcodeagent.personality import continuity
        with tempfile.TemporaryDirectory() as td:
            d = _dyn(td)
            st = d.note_turn("hi")
            continuity.note_expression(st, ["sarcasm", "humor",
                                            "intensity"])
            continuity.note_expression(st, ["sarcasm", "humor",
                                            "intensity"])
            continuity.note_expression(st, ["sarcasm", "humor"])
            card = _card("sassy", "and now?", state=st)
            self.assertEqual(card["humor_type"], "dry")
            self.assertLess(card["expression_scale"], 1.0)


if __name__ == "__main__":
    unittest.main()
