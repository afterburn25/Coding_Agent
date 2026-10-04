"""Voice polish pass — expanded phrase vocabulary, spoken notices for
worker/notification events, and feedback-driven category suppression.

Guarantees under test:
  * New surface phrases (yep, uh-oh, welp, bwahaha, …) detect + render
    without touching display_text.
  * New stage directions (*smirks*, (nods slowly), *perks up*) resolve.
  * record_feedback suppresses the newly-mapped categories per profile.
  * WorkerManager.release() emits worker_finished with the row.
  * AppState._speak_notice deduplicates and persona-wraps.
  * The notification listener only speaks the levels it should.
"""
from __future__ import annotations

import queue
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from localcodeagent.server import AppState
from localcodeagent.voice.vocalizations import VocalizationEngine
from localcodeagent.workers import (AdaptiveWorkerManager, ResourceMonitor)
from localcodeagent.workers.capacity import CapacitySnapshot


def _engine() -> VocalizationEngine:
    return VocalizationEngine(rng=random.Random(3))


def _ctx(**over):
    base = {"style": "default", "strength": 100, "mood": "",
            "is_adult": False, "level": "expressive", "profile_id": "p1"}
    base.update(over)
    return base


class ExpandedVocabulary(unittest.TestCase):
    """New token + stage-direction coverage — every form must render as
    a speakable sound and never leak raw markup to the TTS."""

    def test_casual_acknowledgments(self):
        eng = _engine()
        for tok in ("yep", "yeppers", "mkay", "gotcha", "aight"):
            res = eng.resolve(f"{tok}! That works.", ctx=_ctx())
            self.assertTrue(res.decisions, f"{tok!r} undetected")
            self.assertIn("that works", res.speech_text.lower())

    def test_reaction_interjections(self):
        eng = _engine()
        cases = {"uh-oh": "uh-oh", "welp": "welp", "yikes": "yikes",
                 "whew": "whew", "bwahaha": "ba-ha-ha",
                 "tee hee": "hee hee",  # routed to the hehe family
                 "muhahaha": "ba-ha-ha"}
        for tok, rendered in cases.items():
            out = eng.resolve(f"{tok}! Didn't see that coming.",
                              ctx=_ctx()).speech_text
            self.assertIn(rendered, out.lower(), f"{tok!r} -> {out!r}")

    def test_new_stage_directions(self):
        eng = _engine()
        for stage in ("*smirks*", "(nods slowly)", "*perks up*",
                      "*rolls her eyes*", "(whispers softly)",
                      "*sighs happily*", "*snorts*"):
            res = eng.resolve(f"{stage} Of course.", ctx=_ctx())
            self.assertNotIn("*", res.speech_text)
            self.assertNotIn("(", res.speech_text)
            self.assertIn("Of course.", res.speech_text)

    def test_display_text_never_mutated(self):
        eng = _engine()
        res = eng.resolve("*smirks* yep, that tracks.",
                          ctx=_ctx())
        self.assertEqual(res.display_text, "*smirks* yep, that tracks.")

    def test_gesture_events_for_new_categories(self):
        events = []
        eng = VocalizationEngine(
            rng=random.Random(0),
            publish=lambda kind, payload: events.append((kind, payload)))
        eng.resolve("(nods slowly) Got it.", ctx=_ctx())
        self.assertTrue(any(k == "gesture" for k, _ in events))


class FeedbackCoverage(unittest.TestCase):
    def test_fewer_giggles(self):
        eng = _engine()
        eng.record_feedback("p1", "please use fewer giggles")
        out = eng.resolve("*giggles* nice.", ctx=_ctx()).speech_text
        self.assertNotIn("hee", out.lower())

    def test_no_more_groans(self):
        eng = _engine()
        eng.record_feedback("p1", "no more groans")
        out = eng.resolve("*groans* again?", ctx=_ctx()).speech_text
        self.assertNotIn("ugh", out.lower())

    def test_fewer_gasps(self):
        eng = _engine()
        eng.record_feedback("p1", "fewer gasps please")
        out = eng.resolve("*gasps* wow.", ctx=_ctx()).speech_text
        self.assertNotIn("ah!", out.lower())

    def test_multiword_feedback_phrasing(self):
        eng = _engine()
        eng.record_feedback("p1", "stop making that sighing sound")
        out = eng.resolve("*sighs* alright.", ctx=_ctx()).speech_text
        self.assertNotIn("ahh", out.lower())

    def test_sounded_weird_feedback(self):
        eng = _engine()
        eng.record_feedback("p1", "that laugh sounded weird")
        out = eng.resolve("*laughs* right?", ctx=_ctx()).speech_text
        self.assertNotIn("ha ha", out.lower())

    def test_unrelated_words_do_not_suppress(self):
        eng = _engine()
        # "humans"/"humor" must not trip the hum stem
        eng.record_feedback("p1", "fewer humans would be weird honestly")
        out = eng.resolve("*hums* nice.", ctx=_ctx()).speech_text
        self.assertIn("hmm", out.lower())

    def test_third_person_stage_forms(self):
        eng = _engine()
        for stage in ("*she sighs*", "(he nods)", "*she chuckles*"):
            res = eng.resolve(f"{stage} Sure.", ctx=_ctx())
            self.assertNotIn("*", res.speech_text)
            self.assertNotIn("she", res.speech_text.lower())
            self.assertNotIn("he ", res.speech_text.lower())

    def test_reenable_clears_suppression(self):
        eng = _engine()
        eng.record_feedback("p1", "fewer giggles please")
        eng.record_feedback("p1", "you can giggle again")
        out = eng.resolve("*giggles* cute.", ctx=_ctx()).speech_text
        self.assertIn("hee", out.lower())

    def test_feedback_is_profile_scoped(self):
        eng = _engine()
        eng.record_feedback("p2", "fewer giggles")
        out = eng.resolve("*giggles* nice.",
                          ctx=_ctx(profile_id="p1")).speech_text
        self.assertIn("hee", out.lower())


def _snap():
    return CapacitySnapshot(ts=0, cpu_logical=16, cpu_util=0.1,
                            ram_total_mb=65536, ram_free_mb=48000,
                            vram_total_mb=12288, vram_free_mb=11000,
                            gpu_util=0.0, disk_free_gb=400,
                            hardware_id="testhw")


class WorkerFinishedEvent(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_release_emits_worker_finished(self):
        events = []
        mon = ResourceMonitor(self.tmp, sampler=_snap, sample_ttl=0)
        m = AdaptiveWorkerManager(self.tmp, monitor=mon,
                                  on_queue_event=lambda et, p:
                                  events.append((et, p)))
        out = m.submit("build the thing", role="tool")
        self.assertEqual(out["status"], "admitted")
        wid = out["worker"]["id"]
        m.release(wid, outcome="completed")
        fins = [p for et, p in events if et == "worker_finished"]
        self.assertEqual(len(fins), 1)
        self.assertEqual(fins[0]["outcome"], "completed")
        self.assertEqual(fins[0]["worker"]["id"], wid)

    def test_release_unknown_worker_no_emit(self):
        events = []
        mon = ResourceMonitor(self.tmp, sampler=_snap, sample_ttl=0)
        m = AdaptiveWorkerManager(self.tmp, monitor=mon,
                                  on_queue_event=lambda et, p:
                                  events.append((et, p)))
        m.release("w-nope", outcome="completed")
        self.assertFalse([e for e in events if e[0] == "worker_finished"])


class _StubVoice:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, task_id, text):
        self.enqueued.append((task_id, text))


class _StubState:
    """Bare-bones AppState surface for the spoken-notice helpers."""

    def __init__(self):
        self._queue_announced = set()
        self.voice = _StubVoice()
        self.events = SimpleNamespace(published=[],
                                      publish=lambda t, p:
                                      self.events.published.append((t, p)))
        self._persona_notice = lambda kind, fact: f"{kind}:{fact}"

    _speak_notice = AppState._speak_notice
    _on_worker_queue_event = AppState._on_worker_queue_event
    _spoken_notice_line = AppState._spoken_notice_line
    _SPOKEN_LEVELS = AppState._SPOKEN_LEVELS
    _LEVEL_KIND = AppState._LEVEL_KIND


class SpokenNotices(unittest.TestCase):
    def test_worker_finished_speaks_for_user_work(self):
        st = _StubState()
        st._on_worker_queue_event("worker_finished", {
            "worker": {"id": "w1", "user_initiated": True,
                       "title": "fix the build"},
            "outcome": "completed"})
        self.assertEqual(len(st.voice.enqueued), 1)
        self.assertIn("fix the build", st.voice.enqueued[0][1])
        self.assertIn("completed:", st.voice.enqueued[0][1])

    def test_worker_finished_skips_background(self):
        st = _StubState()
        st._on_worker_queue_event("worker_finished", {
            "worker": {"id": "w1", "user_initiated": False,
                       "title": "mission node"},
            "outcome": "completed"})
        self.assertEqual(st.voice.enqueued, [])

    def test_worker_failure_names_outcome(self):
        st = _StubState()
        st._on_worker_queue_event("worker_finished", {
            "worker": {"id": "w2", "user_initiated": True,
                       "title": "deploy"},
            "outcome": "failed"})
        self.assertIn("failed:", st.voice.enqueued[0][1])
        self.assertIn("deploy", st.voice.enqueued[0][1])

    def test_notice_deduplicates(self):
        st = _StubState()
        st._speak_notice("x1", "completed", "done thing")
        st._speak_notice("x1", "completed", "done thing")
        self.assertEqual(len(st.voice.enqueued), 1)

    def test_notice_silent_without_voice(self):
        st = _StubState()
        st.voice = None
        st._speak_notice("x1", "completed", "done thing")

    def test_notification_levels_gated(self):
        st = _StubState()
        report = {"type": "notification", "notification": {
            "id": "n-1", "level": "info",
            "title": "Weekly report ready", "message": ""}}
        info = {"type": "notification", "notification": {
            "id": "n-2", "level": "info",
            "title": "Prefetch complete", "message": ""}}
        failure = {"type": "notification", "notification": {
            "id": "n-3", "level": "failure",
            "title": "Mission failed", "message": ""}}
        self.assertTrue(st._spoken_notice_line(report))
        self.assertFalse(st._spoken_notice_line(info))
        self.assertTrue(st._spoken_notice_line(failure))
        self.assertEqual(len(st.voice.enqueued), 2)
        # replayed events don't double-speak
        self.assertFalse(st._spoken_notice_line(report))

    def test_non_notification_events_ignored(self):
        st = _StubState()
        self.assertFalse(st._spoken_notice_line({"type": "worker"}))
        self.assertFalse(st._spoken_notice_line({"type": "voice"}))

    def test_queued_task_started_speaks(self):
        st = _StubState()
        st._on_worker_queue_event("queued_task_started", {
            "worker": {"id": "w9", "user_initiated": True,
                       "title": "lint sweep"},
            "title": "lint sweep"})
        self.assertIn("started:", st.voice.enqueued[0][1])
        self.assertIn("lint sweep", st.voice.enqueued[0][1])

    def test_cancelled_outcome_uses_cancelled_kind(self):
        st = _StubState()
        st._on_worker_queue_event("worker_finished", {
            "worker": {"id": "w3", "user_initiated": True,
                       "title": "long scan"},
            "outcome": "cancelled"})
        self.assertIn("cancelled:", st.voice.enqueued[0][1])

    def test_briefing_speaks_once_per_away_window(self):
        st = _StubState()
        st._speak_notice("brief-1000", "briefing",
                         "While you were away: 2 tasks completed.")
        st._speak_notice("brief-1000", "briefing",
                         "While you were away: 2 tasks completed.")
        self.assertEqual(len(st.voice.enqueued), 1)
        # a different away-window (different `since`) speaks again
        st._speak_notice("brief-2000", "briefing",
                         "While you were away: 1 task completed.")
        self.assertEqual(len(st.voice.enqueued), 2)


class NoticeKinds(unittest.TestCase):
    def test_started_and_cancelled_exist(self):
        from localcodeagent.personality.notices import (
            KINDS, persona_notice)
        self.assertIn("started", KINDS)
        self.assertIn("cancelled", KINDS)
        line = persona_notice("started", "the build",
                              {"family": "playful"})
        self.assertIn("On it!", line)
        self.assertIn("the build", line)
        plain = persona_notice("cancelled", "the scan",
                               {"seriousness": "critical",
                                "family": "sassy"})
        self.assertTrue(plain.startswith("Cancelled."))


class ComparativeCommands(unittest.TestCase):
    def test_comparative_adjectives(self):
        from localcodeagent.personality.commands import (
            parse_persona_command as p)
        self.assertEqual(p("be nicer")["trait_offsets"],
                         {"friendliness": 20})
        self.assertEqual(p("be quieter")["trait_offsets"],
                         {"verbosity": -20})
        m = p("act calmer for the next hour")
        self.assertEqual(m["op"], "modifier")
        self.assertEqual(m["ttl_seconds"], 3600.0)

    def test_address_variants(self):
        from localcodeagent.personality.commands import (
            parse_persona_command as p)
        self.assertEqual(p("you can call me Ash")["address"], "Ash")
        self.assertEqual(p("call me Boss.")["address"], "Boss")
        self.assertIsNone(p("call me when it is done"))


class AnalogyMemory(unittest.TestCase):
    """Reply analogy shapes recorded → the next compile warns to vary."""

    def test_reply_analogy_reaches_card(self):
        from localcodeagent.personality.dynamics import PersonaDynamics
        from localcodeagent.personality.effective import (
            card_guidance, compile_effective)
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            dyn = PersonaDynamics(Path(td))
            dyn.note_reply("Think of it like a recipe — "
                           "each step feeds the next.")
            st = dyn.state()
            pats = (st.get("patterns") or {}).get("analogy") or []
            self.assertTrue(pats)
            card = compile_effective(
                {"name": "Nexus", "base_preset": "professional"},
                state=st)
            self.assertTrue(card["recent_analogies"])
            lines = card_guidance(card)
            self.assertTrue(any("fresh comparison" in l
                                for l in lines))

    def test_no_analogy_no_line(self):
        from localcodeagent.personality.dynamics import PersonaDynamics
        from localcodeagent.personality.effective import (
            card_guidance, compile_effective)
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            dyn = PersonaDynamics(Path(td))
            dyn.note_reply("The build is fixed.")
            card = compile_effective(
                {"name": "Nexus", "base_preset": "professional"},
                state=dyn.state())
            self.assertFalse(card["recent_analogies"])
            self.assertFalse(any("fresh comparison" in l
                                 for l in card_guidance(card)))

    def test_humor_pattern_reaches_card(self):
        from localcodeagent.personality.dynamics import PersonaDynamics
        from localcodeagent.personality.effective import (
            card_guidance, compile_effective)
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            dyn = PersonaDynamics(Path(td))
            dyn.note_reply("Haha — that bug never stood a chance.")
            card = compile_effective(
                {"name": "Nexus", "base_preset": "playful"},
                state=dyn.state())
            self.assertTrue(card["recent_humor_patterns"])
            self.assertTrue(any("vary it or stay dry" in l
                                for l in card_guidance(card)))


class VoiceCommands(unittest.TestCase):
    def test_mute_unmute_parse(self):
        from localcodeagent.personality.commands import (
            parse_persona_command as p)
        for t in ("stop talking", "be quiet", "mute your voice",
                  "voice off", "silence yourself"):
            self.assertEqual(p(t), {"op": "voice_mute"}, t)
        for t in ("speak again", "unmute", "voice on",
                  "talk to me", "speak up"):
            self.assertEqual(p(t), {"op": "voice_unmute"}, t)

    def test_mute_does_not_collide(self):
        from localcodeagent.personality.commands import (
            parse_persona_command as p)
        # comparatives and real chat stay untouched
        self.assertNotEqual((p("be quieter") or {}).get("op"),
                            "voice_mute")
        self.assertIsNone(p("stop talking to strangers"))


class HumorFeedbackWiring(unittest.TestCase):
    def test_feedback_marks_adaptation(self):
        from localcodeagent.personality.dynamics import PersonaDynamics
        from localcodeagent.personality.continuity import (
            humor_adaptation)
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            dyn = PersonaDynamics(Path(td))
            for _ in range(3):
                dyn.humor_feedback("sarcastic", positive=False)
            adapt = humor_adaptation(dyn.state())
            self.assertIn("poorly", adapt)
            self.assertIn("sarcastic", adapt)


if __name__ == "__main__":
    unittest.main()
