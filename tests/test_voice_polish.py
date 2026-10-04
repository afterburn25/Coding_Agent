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

import json
import queue
import random
import tempfile
import threading
import time
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

    def test_literal_phrases_are_not_tokens(self):
        # Literal-word reactions ("no way", "fair enough") must NOT be
        # detected as vocalizations — their render equals the words
        # anyway, and a dropped token would eat real text.
        eng = _engine()
        for prose in ("No way! It actually worked.",
                      "Heh, fair enough.",
                      "Oh snap, it compiled.",
                      "Oh well, moving on.",
                      "Wait, what?"):
            res = eng.resolve(prose, ctx=_ctx())
            styles = {d.get("style") for d in res.decisions}
            self.assertFalse(
                {"no_way", "fair_enough", "oh_snap", "oh_well",
                 "wait_what"} & styles, f"{prose!r} -> {styles}")

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

    def test_queued_entry_cancel_emits(self):
        events = []
        mon = ResourceMonitor(self.tmp, sampler=_snap, sample_ttl=0)
        m = AdaptiveWorkerManager(self.tmp, monitor=mon, max_workers=1,
                                  on_queue_event=lambda et, p:
                                  events.append((et, p)))
        m.submit("first job", role="tool", user_initiated=True)
        out = m.submit("queued job", role="tool", user_initiated=True)
        self.assertEqual(out["status"], "queued")
        eid = out["entry"]["id"]
        self.assertTrue(m.cancel(eid))
        canc = [p for et, p in events if et == "worker_cancelled"]
        self.assertEqual(len(canc), 1)
        self.assertEqual(canc[0]["entry"]["id"], eid)

    def test_clean_streak_emits_capacity_restored(self):
        events = []
        mon = ResourceMonitor(self.tmp, sampler=_snap, sample_ttl=0)
        m = AdaptiveWorkerManager(self.tmp, monitor=mon,
                                  on_queue_event=lambda et, p:
                                  events.append((et, p)))
        m.record_resource_failure()  # drop the ceiling first
        for _ in range(5):
            out = m.submit("job", role="tool")
            if out["status"] != "admitted":
                break
            wid = out["worker"]["id"]
            m.worker_started(wid)
            m.release(wid, outcome="completed")
        self.assertTrue([e for e in events
                         if e[0] == "worker_capacity_restored"])


class _StubVoice:
    def __init__(self):
        self.enqueued = []
        self._muted = False
        self.repeated = 0
        self._repeat_ok = True

    def enqueue(self, task_id, text, **kw):
        self.enqueued.append((task_id, text))
        self.last_vocalize = kw.get("vocalize", True)

    def set_muted(self, muted):
        self._muted = bool(muted)

    def muted(self):
        return self._muted

    def repeat_last(self):
        if not self._repeat_ok:
            return False
        self.repeated += 1
        return True


class _StubState:
    """Bare-bones AppState surface for the spoken-notice helpers."""

    def __init__(self, style: str = "playful"):
        self._queue_announced = set()
        self._queue_line_cursor = {}
        self._notice_cursor = {}
        self._queue_burst = []
        self._style = style
        self.voice = _StubVoice()
        self.events = SimpleNamespace(published=[],
                                      publish=lambda t, p:
                                      self.events.published.append((t, p)),
                                      unsubscribe=lambda q: None)
        self._shutdown = threading.Event()
        self._persona_notice = \
            lambda kind, fact, seq=None: f"{kind}:{seq}:{fact}"

    _speak_notice = AppState._speak_notice
    _speak_queue_notice = AppState._speak_queue_notice
    speak_greeting = AppState.speak_greeting
    _persona_command = AppState._persona_command
    _voice_json_adjust = AppState._voice_json_adjust
    _queue_notice_line = AppState._queue_notice_line
    _on_worker_queue_event = AppState._on_worker_queue_event
    _spoken_notice_line = AppState._spoken_notice_line
    _notice_loop = AppState._notice_loop
    _SPOKEN_LEVELS = AppState._SPOKEN_LEVELS
    _LEVEL_KIND = AppState._LEVEL_KIND
    _QUEUED_LINES = AppState._QUEUED_LINES

    def _vocalization_context(self):
        return {"style": self._style}


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

    def test_capacity_reduction_speaks_once_per_level(self):
        st = _StubState()
        for _ in range(3):
            st._on_worker_queue_event("worker_capacity_reduced",
                                      {"ceiling": 3})
        self.assertEqual(len(st.voice.enqueued), 1)
        self.assertIn("slower", st.voice.enqueued[0][1])
        st._on_worker_queue_event("worker_capacity_reduced",
                                  {"ceiling": 2})
        self.assertEqual(len(st.voice.enqueued), 2)

    def test_notification_kind_detection(self):
        st = _StubState()
        cases = [
            ("Self-repair completed on watchdog", "important",
             "self_repair:"),
            ("Update available: v0.18.0", "important", "update:"),
            ("Weekly report ready", "important", "briefing:"),
            ("Approval needed for deploy", "approval", "approval:"),
        ]
        for i, (title, level, want) in enumerate(cases):
            ev = {"type": "notification", "notification": {
                "id": f"n-k{i}", "level": level, "title": title,
                "message": ""}}
            self.assertTrue(st._spoken_notice_line(ev), title)
            self.assertIn(want, st.voice.enqueued[-1][1], title)

    def test_interrupted_reads_as_cancelled(self):
        st = _StubState()
        st._on_worker_queue_event("worker_finished", {
            "worker": {"id": "w-i", "user_initiated": True,
                       "title": "render", "status": "interrupted"},
            "outcome": "interrupted"})
        self.assertTrue(st.voice.enqueued)
        self.assertIn("cancelled:", st.voice.enqueued[-1][1])

    def test_capacity_restored_speaks(self):
        st = _StubState()
        st._on_worker_queue_event("worker_capacity_restored",
                                  {"ceiling": 4})
        self.assertEqual(len(st.voice.enqueued), 1)
        self.assertIn("recovering", st.voice.enqueued[0][1])
        # Oscillation: reduce → restore → reduce again speaks each move.
        st._on_worker_queue_event("worker_capacity_reduced",
                                  {"ceiling": 3})
        st._on_worker_queue_event("worker_capacity_restored",
                                  {"ceiling": 4})
        st._on_worker_queue_event("worker_capacity_reduced",
                                  {"ceiling": 3})
        self.assertEqual(len(st.voice.enqueued), 4)

    def test_mute_speaks_farewell_before_muting(self):
        st = _StubState()
        res = st._persona_command("stop talking")
        self.assertEqual(res["applied"], "voice_mute")
        self.assertTrue(st.voice.muted())
        tid, text = st.voice.enqueued[0]
        self.assertEqual(tid, "voice-mute-ack")
        self.assertIn("status:", text)

    def test_voice_rate_commands(self):
        from localcodeagent.personality.commands import (
            parse_persona_command)
        self.assertEqual(parse_persona_command("speak faster"),
                         {"op": "voice_rate", "delta": 0.1})
        self.assertEqual(parse_persona_command("talk a bit slower"),
                         {"op": "voice_rate", "delta": -0.1})
        self.assertEqual(parse_persona_command("slow down"),
                         {"op": "voice_rate", "delta": -0.1})
        self.assertEqual(parse_persona_command("normal speed"),
                         {"op": "voice_rate", "set": 1.0})
        # Ordinary prose must not parse.
        self.assertIsNone(parse_persona_command(
            "can you slow down the explanation"))
        self.assertIsNone(parse_persona_command(
            "speak up at the meeting"))

    def test_voice_rate_applies_and_persists(self):
        st = _StubState()
        tmp = Path(tempfile.mkdtemp())
        prof = {"profile_id": "p1"}
        st.profiles = SimpleNamespace(
            active=lambda: prof,
            profile_dir=lambda pid, create=False: tmp)
        res = st._persona_command("speak faster")
        self.assertEqual(res["applied"], "voice_rate")
        vsel = json.loads((tmp / "voice.json").read_text())
        self.assertAlmostEqual(vsel["speed"], 1.1)
        res = st._persona_command("normal speed")
        vsel = json.loads((tmp / "voice.json").read_text())
        self.assertAlmostEqual(vsel["speed"], 1.0)
        self.assertIn("normal", res["ack"].lower())

    def test_notice_seq_cursor_rotates(self):
        st = _StubState()
        st._speak_notice("a1", "completed", "build")
        st._speak_notice("a2", "completed", "build")
        self.assertIn("completed:0:", st.voice.enqueued[0][1])
        self.assertIn("completed:1:", st.voice.enqueued[1][1])

    def test_voice_gain_commands(self):
        from localcodeagent.personality.commands import (
            parse_persona_command)
        self.assertEqual(parse_persona_command("speak louder"),
                         {"op": "voice_gain", "delta": 2.0})
        self.assertEqual(parse_persona_command("talk a bit softer"),
                         {"op": "voice_gain", "delta": -2.0})
        self.assertEqual(parse_persona_command("volume down"),
                         {"op": "voice_gain", "delta": -2.0})
        self.assertEqual(parse_persona_command("normal volume"),
                         {"op": "voice_gain", "set": 0.0})
        # "be quieter" stays a persona-style command, not volume.
        self.assertNotEqual(
            (parse_persona_command("be quieter") or {}).get("op"),
            "voice_gain")
        self.assertIsNone(parse_persona_command("turn up the heat"))

    def test_voice_gain_persists(self):
        st = _StubState()
        tmp = Path(tempfile.mkdtemp())
        prof = {"profile_id": "p1"}
        st.profiles = SimpleNamespace(
            active=lambda: prof,
            profile_dir=lambda pid, create=False: tmp)
        res = st._persona_command("speak louder")
        self.assertEqual(res["applied"], "voice_gain")
        vsel = json.loads((tmp / "voice.json").read_text())
        self.assertAlmostEqual(vsel["gain_db"], 2.0)
        res = st._persona_command("normal volume")
        vsel = json.loads((tmp / "voice.json").read_text())
        self.assertAlmostEqual(vsel["gain_db"], 0.0)
        self.assertIn("normal volume", res["ack"].lower())

    def test_queue_burst_collapses(self):
        st = _StubState()
        for i in range(5):
            st._speak_queue_notice(f"item-{i}")
        enq = st.voice.enqueued
        self.assertEqual(len(enq), 3)
        self.assertIn("More tasks", enq[-1][1])

    def test_worker_cancelled_entry_speaks(self):
        st = _StubState()
        st._on_worker_queue_event("worker_cancelled", {
            "entry": {"id": "e1", "title": "big render",
                      "user_initiated": True}})
        self.assertTrue(st.voice.enqueued)
        self.assertIn("cancelled:", st.voice.enqueued[-1][1])
        # Background churn stays silent.
        st2 = _StubState()
        st2._on_worker_queue_event("worker_cancelled", {
            "entry": {"id": "e2", "title": "bg sweep",
                      "user_initiated": False}})
        self.assertFalse(st2.voice.enqueued)

    def test_failed_notice_names_error(self):
        st = _StubState()
        st._on_worker_queue_event("worker_finished", {
            "worker": {"id": "w-f", "user_initiated": True,
                       "title": "deploy",
                       "status": "failed",
                       "result": {"error": "OOM while loading model\n"
                                           "Traceback line two"}},
            "outcome": "failed"})
        line = st.voice.enqueued[-1][1]
        self.assertIn("OOM while loading model", line)
        self.assertNotIn("Traceback", line)

    def test_greeting_speaks_once_per_profile(self):
        st = _StubState()
        st.speak_greeting("p1", "Hey — welcome back.")
        st.speak_greeting("p1", "Hey — welcome back.")
        st.speak_greeting("p2", "Hi, I'm ready.")
        self.assertEqual(len(st.voice.enqueued), 2)
        self.assertEqual(st.voice.enqueued[0][0], "greet-p1")
        self.assertEqual(st.voice.enqueued[1][0], "greet-p2")

    def test_queue_reason_explains_wait(self):
        st = _StubState()
        st._on_worker_queue_event("task_queued", {
            "user_initiated": True,
            "reason": "waiting_for_dependency",
            "entry": {"id": "e-dep", "title": "build"}})
        self.assertIn("another task", st.voice.enqueued[-1][1])
        st2 = _StubState()
        st2._on_worker_queue_event("task_queued", {
            "user_initiated": True,
            "reason": "waiting_for_worker",
            "entry": {"id": "e-w", "title": "scan"}})
        self.assertNotIn("Waiting for", st2.voice.enqueued[-1][1])

    def test_voice_repeat_commands(self):
        from localcodeagent.personality.commands import (
            parse_persona_command)
        self.assertEqual(parse_persona_command("say that again"),
                         {"op": "voice_repeat"})
        self.assertEqual(parse_persona_command("repeat it"),
                         {"op": "voice_repeat"})
        self.assertEqual(parse_persona_command("what did you say?"),
                         {"op": "voice_repeat"})
        self.assertIsNone(parse_persona_command(
            "what did you say about the deploy"))

    def test_voice_repeat_handler(self):
        st = _StubState()
        st.voice._repeat_ok = False
        res = st._persona_command("say it again")
        self.assertEqual(res["applied"], "voice_repeat")
        self.assertIn("nothing", res["ack"].lower())
        st.voice._repeat_ok = True
        res = st._persona_command("repeat that")
        self.assertIn("once more", res["ack"].lower())
        self.assertEqual(st.voice.repeated, 1)

    def test_persona_notice_caches_card(self):
        st = _StubState()
        tmp = Path(tempfile.mkdtemp())
        st.profiles = SimpleNamespace(
            active=lambda: {"profile_id": "p1"},
            profile_dir=lambda pid, create=False: tmp)
        real = lambda kind, fact, seq=None: AppState._persona_notice(
            st, kind, fact, seq=seq)
        self.assertIn("the build", real("completed", "the build"))
        self.assertTrue(getattr(st, "_pnc_cache", None))
        # Rotation still flows through the cached card.
        self.assertNotEqual(real("completed", "x", seq=0),
                            real("completed", "x", seq=1))

    def test_empty_notification_still_speaks(self):
        st = _StubState()
        ev = {"type": "notification", "notification": {
            "id": "n-empty", "level": "important", "title": "",
            "message": ""}}
        self.assertTrue(st._spoken_notice_line(ev))
        self.assertIn("needs a look", st.voice.enqueued[-1][1])

    def test_urgent_notice_stashed_not_dropped(self):
        st = _StubState()
        st._NOTICE_GAP_S = 0.3
        st._NOTICE_GAP_URGENT_S = 0.2
        q = queue.Queue()
        t = threading.Thread(target=st._notice_loop, args=(q,),
                             daemon=True)
        t.start()
        try:
            q.put({"type": "notification", "notification": {
                "id": "n-f", "level": "failure", "title": "it broke",
                "message": ""}})
            q.put({"type": "notification", "notification": {
                "id": "n-a", "level": "approval",
                "title": "approve deploy", "message": ""}})
            time.sleep(0.35)          # urgent gap elapses
            q.put({"type": "tick"})   # wake → pending is serviced
            deadline = time.time() + 5
            while time.time() < deadline and len(st.voice.enqueued) < 2:
                time.sleep(0.05)
        finally:
            st._shutdown.set()
            q.put({"type": "tick"})
            t.join(timeout=3)
        ids = [tid for tid, _ in st.voice.enqueued]
        self.assertEqual(len(ids), 2)
        self.assertIn("notice-n-f", ids)
        self.assertIn("notice-n-a", ids)

    def test_repeat_muted_ack(self):
        st = _StubState()
        st.voice.set_muted(True)
        res = st._persona_command("say that again")
        self.assertIn("muted", res["ack"].lower())

    def test_notices_bypass_vocalization(self):
        st = _StubState()
        st._speak_notice("n1", "completed", "the build")
        self.assertFalse(st.voice.last_vocalize)
        st._speak_queue_notice("q1")
        self.assertFalse(st.voice.last_vocalize)
        st.speak_greeting("p1", "Hey there.")
        self.assertTrue(st.voice.last_vocalize)  # authored text still resolves

    def test_queue_lines_rotate(self):
        st = _StubState(style="playful")
        lines = {st._queue_notice_line() for _ in range(3)}
        self.assertEqual(len(lines), 2)  # two variants alternate
        again = _StubState(style="nerdy")
        self.assertIn("scheduler", again._queue_notice_line().lower())

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

    def test_prefix_variants_rotate(self):
        from localcodeagent.personality.notices import (
            KINDS, persona_notice)
        card = {"family": "playful"}
        for kind in KINDS:
            a = persona_notice(kind, "the fact", card, seq=0)
            b = persona_notice(kind, "the fact", card, seq=1)
            self.assertNotEqual(a, b, kind)
            self.assertTrue(a.endswith("the fact"), kind)
            self.assertTrue(b.endswith("the fact"), kind)
        # serious/critical stays plain regardless of rotation
        sc = {"family": "sassy", "seriousness": "critical"}
        self.assertEqual(
            persona_notice("completed", "x", sc, seq=0),
            persona_notice("completed", "x", sc, seq=1))
        self.assertTrue(
            persona_notice("completed", "x", sc).startswith("Done."))


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
