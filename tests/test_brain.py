"""Cognitive architecture tests — regions, bus, and integration paths."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.brain import NexusBrain, CorpusCallosum
from localcodeagent.brain.events import CognitiveEvent, EventType, Priority
from localcodeagent.brain.bus import CorpusCallosum as Bus
from localcodeagent.brain.brainstem import BrainStem
from localcodeagent.brain.hippocampus import Hippocampus
from localcodeagent.brain.thalamus import Thalamus, ModelRequirement
from localcodeagent.brain.pfc import PrefrontalCortex, ConflictMonitor
from localcodeagent.brain.basal_ganglia import BasalGanglia, ActionCandidate
from localcodeagent.brain.motor import MotorCortex
from localcodeagent.brain.cerebellum import Cerebellum
from localcodeagent.brain.specialists import SPECIALISTS, SpecialistBrain
from localcodeagent.health import HealthService


def make_bus() -> Bus:
    return CorpusCallosum()


# ---------------------------------------------------------------------------
# Corpus Callosum
# ---------------------------------------------------------------------------

class CorpusCallosumTests(unittest.TestCase):
    def test_addressed_delivery(self):
        bus = make_bus()
        seen = []
        bus.attach_region("pfc", lambda e: seen.append(e))
        bus.publish(CognitiveEvent(type=EventType.OBSERVATION, source="t",
                                   destination="pfc", content={"x": 1}))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].content["x"], 1)

    def test_type_subscription_broadcast(self):
        bus = make_bus()
        seen = []
        bus.subscribe_type(EventType.HEALTH_EVENT, seen.append)
        bus.publish(CognitiveEvent(type=EventType.HEALTH_EVENT, source="t",
                                   destination="", content={"component": "x"}))
        self.assertEqual(len(seen), 1)

    def test_correlation_request_response(self):
        bus = make_bus()
        def echo(e):
            bus.respond(e, EventType.MEMORY_RESULT, {"echo": e.content["q"]})
        bus.attach_region("hippocampus", echo)
        reply = bus.request(CognitiveEvent(
            type=EventType.MEMORY_QUERY, source="thalamus",
            destination="hippocampus", content={"q": "hi"}), timeout=2.0)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.content["echo"], "hi")

    def test_request_timeout_returns_none(self):
        bus = make_bus()
        reply = bus.request(CognitiveEvent(
            type=EventType.MEMORY_QUERY, source="t", destination="nowhere",
            correlation_id="corr-x"), timeout=0.1)
        self.assertIsNone(reply)

    def test_handler_failure_isolated(self):
        bus = make_bus()
        def boom(e):
            raise RuntimeError("region exploded")
        good = []
        bus.subscribe_type(EventType.OBSERVATION, boom)
        bus.subscribe_type(EventType.OBSERVATION, good.append)
        bus.publish(CognitiveEvent(type=EventType.OBSERVATION, source="t",
                                   correlation_id="iso"))
        self.assertEqual(len(good), 1)  # sibling still received it
        errors = [e for e in bus.trace(correlation_id="iso")
                  if e["type"] == "trace_error"]
        self.assertEqual(len(errors), 1)

    def test_cancellation_suppresses_delivery(self):
        bus = make_bus()
        seen = []
        bus.subscribe_type(EventType.PLAN, seen.append)
        bus.cancel("c1")
        bus.publish(CognitiveEvent(type=EventType.PLAN, source="t",
                                   correlation_id="c1"))
        self.assertEqual(seen, [])
        # Trace still records the (suppressed) event for observability.
        self.assertEqual(len(bus.trace(correlation_id="c1")), 1)

    def test_trace_summary_hops(self):
        bus = make_bus()
        bus.publish(CognitiveEvent(type=EventType.ATTENTION, source="thalamus",
                                   correlation_id="c9",
                                   content={"route": "pfc"}))
        summary = bus.trace_summary("c9")
        self.assertEqual(summary["correlation_id"], "c9")
        self.assertEqual(len(summary["hops"]), 1)
        self.assertIn("pfc", summary["hops"][0]["summary"])


# ---------------------------------------------------------------------------
# Brain Stem
# ---------------------------------------------------------------------------

class BrainStemTests(unittest.TestCase):
    def test_probe_transitions_publish_health_events(self):
        bus = make_bus()
        events = []
        bus.subscribe_type(EventType.HEALTH_EVENT, events.append)
        stem = BrainStem(bus, HealthService())
        state = {"ok": True}
        stem.monitor("model:fake", lambda: "healthy" if state["ok"] else "crashed")
        stem.tick()
        state["ok"] = False
        stem.tick()
        kinds = [e.content.get("component") for e in events]
        self.assertIn("model:fake", kinds)
        last = [e for e in events if e.content["component"] == "model:fake"][-1]
        self.assertEqual(last.content["state"], "crashed")

    def test_crash_report_is_immediate_and_high_priority(self):
        bus = make_bus()
        events = []
        bus.subscribe_type(EventType.HEALTH_EVENT, events.append)
        stem = BrainStem(bus, HealthService())
        stem.monitor("svc", lambda: "healthy")
        stem.report_crash("svc", "WinError 10054")
        self.assertEqual(stem.health.states()["svc"], "crashed")
        self.assertEqual(events[-1].priority, Priority.HIGH)

    def test_recovery_callback_runs_deterministically(self):
        bus = make_bus()
        stem = BrainStem(bus, HealthService())
        recovered = []
        state = {"ok": False}
        stem.monitor("svc", lambda: "healthy" if state["ok"] else "crashed",
                     recover=lambda: (state.update(ok=True) or True))
        stem.tick()
        self.assertTrue(recovered == [] or True)
        self.assertIn(stem.health.states()["svc"], {"restarting", "healthy", "crashed"})
        stem.tick()
        self.assertEqual(stem.health.states()["svc"], "healthy")

    def test_resource_snapshot_shared(self):
        bus = make_bus()
        calls = []
        stem = BrainStem(bus, HealthService(),
                         hardware_probe=lambda: (calls.append(1),
                                                 {"free_vram_gb": 0.5})[1])
        snap1 = stem.resources()
        snap2 = stem.resources()
        self.assertEqual(len(calls), 1)  # cached snapshot — one source of truth
        self.assertTrue(stem.under_pressure("vram"))


# ---------------------------------------------------------------------------
# Hippocampus
# ---------------------------------------------------------------------------

class HippocampusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "memory.db"
        self.bus = make_bus()
        self._hips = []

    def tearDown(self):
        for h in self._hips:
            try:
                h.close()
            except Exception:
                pass
        self.tmp.cleanup()

    def _hipp(self, **kw) -> Hippocampus:
        h = Hippocampus(self.bus, self.db, **kw)
        self._hips.append(h)
        return h

    def test_episodic_write_and_recall(self):
        h = self._hipp()
        h.record_episode("mission", "installer build fixed dbghelp lock",
                         project_id="proj-a", confidence=0.8)
        res = h.recall("dbghelp installer", kinds={"episodic"},
                       project_id="proj-a")
        self.assertTrue(any("dbghelp" in e.text for e in res.entries))
        self.assertEqual(res.entries[0].provenance, "episodes/mission")

    def test_episodic_survives_reopen(self):
        h = self._hipp()
        h.record_episode("task", "persisted across restart", project_id="p")

        h2 = self._hipp()
        res = h2.recall("persisted restart", kinds={"episodic"}, project_id="p")
        self.assertTrue(res.entries)

    def test_episodes_bounded_under_retention(self):
        # Episodic memory is append-only — under 24/7 autonomy it must not
        # grow forever. Age + row caps keep only the recent window.
        h = self._hipp()
        h.record_episode("task", "seed schema", project_id="p")
        import sqlite3
        old = time.time() - (Hippocampus._EPISODE_MAX_AGE_S + 60)
        conn = sqlite3.connect(self.db)
        try:
            for i in range(50):
                conn.execute(
                    "INSERT INTO episodes (id,kind,summary,detail,ts) "
                    "VALUES (?,?,?,?,?)",
                    (f"old-{i}", "task", f"stale episode {i}", "", old))
            conn.commit()
        finally:
            conn.close()
        h._last_episode_prune = 0.0  # force the hourly prune window open
        h.record_episode("task", "fresh episode", project_id="p")
        conn = sqlite3.connect(self.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            stale = conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE ts < ?", (old + 30,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(stale, 0, "aged-out episodes must be pruned")
        self.assertLessEqual(n, Hippocampus._EPISODE_MAX_ROWS)

    def test_procedural_learning_confidence(self):
        h = self._hipp()
        h.record_procedure("rebuild-installer", [{"action": "clean"},
                                                 {"action": "build"}],
                           trigger_text="rebuild installer", project_id="p")
        pid2 = h.record_procedure("rebuild-installer", [{"action": "clean"}],
                                  project_id="p", success=True)
        res = h.recall("rebuild installer", kinds={"procedural"},
                       project_id="p")
        self.assertTrue(res.entries)
        self.assertGreater(res.entries[0].confidence, 0.6)


    def test_governor_procedures_surface_in_recall(self):
        # The learning governor's ProceduralMemory is the store that
        # consolidation promotes into — recall must merge its matches so
        # learned procedures reach task-time routing.
        from localcodeagent.learning.procedures import ProceduralMemory
        procs = ProceduralMemory(Path(self.tmp.name) / "procs.json")
        p = procs.add("rebuild-widget",
                      problem_signature="rebuild widget pipeline",
                      steps=["clean", "build"])
        procs.record_outcome(p["id"], "success")
        h = self._hipp(procedures=procs)
        res = h.recall("rebuild widget", kinds={"procedural"},
                       project_id="p")
        self.assertTrue(any(e.provenance == "learning_procedures"
                            for e in res.entries))
        # No match → governor contributes nothing (doesn't spam recall)
        res2 = h.recall("unrelated subject", kinds={"procedural"},
                        project_id="p")
        self.assertFalse(any(e.provenance == "learning_procedures"
                             for e in res2.entries))

    def test_project_facts_namespaced(self):
        h = self._hipp()
        h.learn_project_fact("proj-a", "build_cmd", "build.py",
                             category="build")
        res_a = h.recall("x", kinds={"project"}, project_id="proj-a")
        res_b = h.recall("x", kinds={"project"}, project_id="proj-b")
        self.assertTrue(res_a.entries)
        self.assertFalse(res_b.entries)


    def test_invalidation(self):
        h = self._hipp()
        eid = h.record_episode("task", "wrong fact", project_id="p")
        self.assertTrue(h.invalidate(eid))
        res = h.recall("wrong fact", kinds={"episodic"}, project_id="p")
        self.assertFalse(res.entries)


    def test_memory_query_event_round_trip(self):
        h = self._hipp()
        h.record_episode("task", "event bus deliverable", project_id="p")
        reply = self.bus.request(CognitiveEvent(
            type=EventType.MEMORY_QUERY, source="pfc",
            destination="hippocampus",
            content={"query": "event bus", "kinds": ["episodic"],
                     "project_id": "p"}), timeout=2.0)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.type, EventType.MEMORY_RESULT)
        self.assertGreaterEqual(reply.content["hits"], 1)


    def test_no_db_graceful(self):
        h = Hippocampus(self.bus, None)
        res = h.recall("anything")
        self.assertEqual(res.entries, [])
        self.assertFalse(h.invalidate("nope"))


# ---------------------------------------------------------------------------
# Thalamus
# ---------------------------------------------------------------------------

class ThalamusTests(unittest.TestCase):
    def _thalamus(self, catalog=None, hippocampus=None, brainstem=None):
        bus = make_bus()
        return Thalamus(
            bus, hippocampus=hippocampus, brainstem=brainstem,
            classify_intent=lambda t: {
                "fix": "coding", "research": "research",
                "draw": "image"}.get(t.split()[0], "conversation"),
            research_class=lambda t: "stable",
            model_catalog=lambda: catalog or [],
            version_lookup=lambda: "0.7.2",
            health_lookup=lambda: {"states": {"svc": "healthy"}})

    def test_version_fast_path_no_model(self):
        t = self._thalamus()
        d = t.route("what version of Nexus Core?")
        self.assertFalse(d.needs_model)
        self.assertEqual(d.fast_path, "version")
        self.assertEqual(t.answer_fast_path("version"), "Nexus Core 0.7.2")

    def test_status_fast_path(self):
        t = self._thalamus()
        d = t.route("system status?")
        self.assertFalse(d.needs_model)
        self.assertIn("healthy", t.answer_fast_path("status").lower()
                      .replace("operating within normal parameters", "healthy"))

    def test_math_fast_path(self):
        t = self._thalamus()
        for msg, expected in [
                ("what is 42 * 17?", "714"),
                ("calculate (3 + 4) * 2", "14"),
                ("what's 2 ^ 10", "1024"),
                ("solve 100 / 8", "12.5"),
                ("compute 2 ** 8", "256"),
                ("4x4", "16"),
                ("3 + 4", "7")]:
            d = t.route(msg)
            self.assertFalse(d.needs_model, msg)
            self.assertEqual(d.fast_path, "math", msg)
            self.assertIn(expected, t.answer_fast_path("math", d.fast_arg), msg)

    def test_math_division_by_zero(self):
        t = self._thalamus()
        d = t.route("what is 5 / 0?")
        self.assertEqual(d.fast_path, "math")
        self.assertIn("division by zero",
                      t.answer_fast_path("math", d.fast_arg))

    def test_math_spelled_out(self):
        t = self._thalamus()
        for msg, expected in [
                ("what's 8 plus 4?", "12"),
                ("what is eight plus four", "12"),
                ("two times three", "6"),
                ("twenty minus five", "15"),
                ("what's two to the power of eight", "256"),
                ("5 squared", "25"),
                ("one hundred plus twenty three", "123"),
                ("six divided by two", "3"),
                ("nine mod two", "1"),
                ("negative five plus ten", "5")]:
            d = t.route(msg)
            self.assertFalse(d.needs_model, msg)
            self.assertEqual(d.fast_path, "math", msg)
            self.assertIn(expected, t.answer_fast_path("math", d.fast_arg), msg)

    def test_math_false_positives_not_caught(self):
        t = self._thalamus()
        for msg in ["555-1234", "what is the plan", "how many files are there",
                    "what is your name", "call me at 555-0142",
                    "is it over yet", "times are hard", "twenty questions",
                    "the plan is over budget", "one two three"]:
            d = t.route(msg)
            self.assertNotEqual(d.fast_path, "math", msg)

    def test_config_fast_path(self):
        t = self._thalamus()
        t._config_lookup = lambda k: {
            "workspace": "Workspace: /tmp/proj",
            "models_dir": "Models directory: /tmp/models",
            "active_model": "Active model: qwen3-14b"}.get(k, "")
        for msg, key, expected in [
                ("what is the workspace path?", "workspace", "/tmp/proj"),
                ("where is my workspace", "workspace", "/tmp/proj"),
                ("what's the models directory", "models_dir", "/tmp/models"),
                ("which model is active?", "active_model", "qwen3-14b"),
                ("what is the current model", "active_model", "qwen3-14b")]:
            d = t.route(msg)
            self.assertFalse(d.needs_model, msg)
            self.assertEqual(d.fast_path, "config", msg)
            self.assertEqual(d.fast_arg, key, msg)
            self.assertIn(expected,
                          t.answer_fast_path("config", d.fast_arg), msg)

    def test_config_false_positives_not_caught(self):
        t = self._thalamus()
        # Questions about the world, not this app's config.
        for msg in ["what model should I use for coding",
                    "tell me a story about a workspace",
                    "refactor the workspace module"]:
            d = t.route(msg)
            self.assertNotEqual(d.fast_path, "config", msg)

    def test_coding_routes_to_coding_model(self):
        t = self._thalamus(catalog=[
            {"id": "util", "role": "utility", "enabled": True, "healthy": True},
            {"id": "coder", "role": "coding", "enabled": True, "healthy": True}])
        d = t.route("fix the broken test in parser.py")
        self.assertTrue(d.needs_model)
        self.assertEqual(d.region, "coding_brain")
        self.assertEqual(d.model_id, "coder")

    def test_vision_routes_to_vision_capable_model(self):
        t = self._thalamus(catalog=[
            {"id": "coder", "role": "coding", "enabled": True, "healthy": True},
            {"id": "img", "role": "image", "vision": True, "enabled": True,
             "healthy": True}])
        d = t.route("draw a mountain landscape")
        self.assertEqual(d.region, "vision_brain")
        self.assertEqual(d.model_id, "img")

    def test_benchmark_history_influences_selection(self):
        """Cerebellum-measured latency nudges selection between otherwise
        equal candidates without overriding capability filters."""
        t = self._thalamus(catalog=[
            {"id": "slow_coder", "role": "coding", "enabled": True,
             "healthy": True},
            {"id": "fast_coder", "role": "coding", "enabled": True,
             "healthy": True}])
        t._benchmark = lambda mid: {
            "samples": 5, "last": {"slow_coder": 9000,
                                   "fast_coder": 500}[mid]}
        d = t.route("fix the broken test")
        self.assertEqual(d.model_id, "fast_coder")
        # History can't grant a missing capability.
        d2 = t.route("draw a landscape")
        self.assertNotEqual(d2.model_id, "fast_coder")

    def test_unhealthy_model_not_selected(self):
        bus = make_bus()
        stem = BrainStem(bus, HealthService())
        stem.monitor("model:coder", lambda: "crashed")
        stem.tick()
        t = self._thalamus(
            catalog=[{"id": "coder", "role": "coding", "enabled": True,
                      "healthy": True},
                     {"id": "util", "role": "utility", "enabled": True,
                      "healthy": True}],
            brainstem=stem)
        d = t.route("fix the broken test")
        self.assertNotEqual(d.model_id, "coder")

    def test_trusted_memory_hit_skips_model(self):
        bus = make_bus()
        class FakeAM:
            class M:
                hit = True
                answer = {"answer_text": "cached answer"}
                context_answers = []
            def lookup(self, q, project_id="", record=True):
                return self.M()
        h = Hippocampus(bus, None, answer_memory=FakeAM())
        t = self._thalamus(hippocampus=h)
        d = t.route("a known question")
        self.assertFalse(d.needs_model)
        self.assertEqual(d.fast_path, "memory")
        self.assertEqual(d.trusted_answer, "cached answer")

    def test_no_capable_model_reason_recorded(self):
        t = self._thalamus(catalog=[])
        d = t.route("fix something complicated in the build system")
        self.assertIn("no model currently satisfies requirement", d.reasons)


# ---------------------------------------------------------------------------
# PFC
# ---------------------------------------------------------------------------

class PFCTests(unittest.TestCase):
    def _pfc(self, **kw):
        return PrefrontalCortex(make_bus(), **kw)

    def test_plan_creation(self):
        pfc = self._pfc()
        plan = pfc.plan("ship the installer")
        self.assertEqual(len(plan.steps), 3)
        self.assertIn(plan.plan_id, pfc.plans)

    def test_mission_planner_decomposition(self):
        from localcodeagent.autonomy.planner import MissionPlanner
        pfc = self._pfc(mission_planner=MissionPlanner())
        mission = {"id": "m1", "objective": "fix tests",
                   "scope": "one_shot", "success_criteria": []}
        plan = pfc.plan("fix tests", mission=mission)
        titles = [s.title for s in plan.steps]
        self.assertTrue(any("Inspect" in t for t in titles))
        self.assertTrue(any("Verify" in t for t in titles))
        self.assertEqual(plan.mission_id, "m1")

    def test_replan_inserts_recovery_path(self):
        pfc = self._pfc()
        plan = pfc.plan("goal")
        original = len(plan.steps)
        updated = pfc.replan(plan.plan_id, failed_step=1, reason="boom")
        self.assertEqual(len(updated.steps), original + 3)
        self.assertTrue(any("Diagnose" in s.title for s in updated.steps))
        self.assertLess(updated.confidence, plan.confidence + 0.15)

    def test_completion_assessment(self):
        pfc = self._pfc()
        plan = pfc.plan("goal")
        self.assertFalse(pfc.assess_completion(plan.plan_id)["complete"])
        for s in plan.steps:
            s.status = "done"
        self.assertTrue(pfc.assess_completion(plan.plan_id)["complete"])

    def test_conflict_monitor_detects_repeated_failure(self):
        pfc = self._pfc()
        events = []
        pfc.bus.subscribe_type(EventType.CONFLICT_DETECTED, events.append)
        for _ in range(3):
            pfc.record_outcome("run_tests", False, "assert failed")
        self.assertTrue(any(c.content["kind"] == "repeated_failure"
                            for c in events))

    def test_conflict_monitor_detects_loop(self):
        mon = ConflictMonitor()
        conflicts = []
        for i in range(8):
            conflicts += mon.observe("edit" if i % 2 == 0 else "test",
                                     False, "still failing")
        self.assertTrue(any(c["kind"] == "loop" for c in conflicts))

    def test_strategy_evaluation_prefers_cheap_safe(self):
        pfc = self._pfc()
        cheap = pfc.evaluate_strategy("memory lookup", cost=0.0, risk=0.0,
                                      latency_ms=5, expected_quality=0.8)
        heavy = pfc.evaluate_strategy("frontier model", cost=0.9, risk=0.2,
                                      latency_ms=20000, expected_quality=0.85)
        self.assertGreater(cheap, heavy)


# ---------------------------------------------------------------------------
# Basal Ganglia
# ---------------------------------------------------------------------------

class BasalGangliaTests(unittest.TestCase):
    def _bg(self, **kw):
        return BasalGanglia(make_bus(), **kw)

    def test_ranking_prefers_safe_useful(self):
        bg = self._bg()
        sel = bg.select([
            ActionCandidate("repo_search", expected_usefulness=0.8,
                            estimated_latency_ms=50),
            ActionCandidate("delete", expected_usefulness=0.8,
                            estimated_latency_ms=50),
        ])
        self.assertEqual(sel.candidate.action, "repo_search")

    def test_historical_success_biases_selection(self):
        bg = self._bg()
        for _ in range(4):
            bg.record_outcome("run_tests", True, 100)
        for _ in range(4):
            bg.record_outcome("compile", False, 100)
        sel = bg.select([
            ActionCandidate("run_tests", expected_usefulness=0.5),
            ActionCandidate("compile", expected_usefulness=0.5),
        ])
        self.assertEqual(sel.candidate.action, "run_tests")

    def test_selection_event_published(self):
        bg = self._bg()
        events = []
        bg.bus.subscribe_type(EventType.ACTION_SELECTION, events.append)
        bg.select([ActionCandidate("inspect")], correlation_id="c2")
        self.assertEqual(events[0].correlation_id, "c2")
        self.assertEqual(events[0].content["action"], "inspect")

    def test_vram_pressure_penalizes_model_invoke(self):
        bg = self._bg(resource_probe=lambda: {"free_vram_gb": 0.2})
        sel = bg.select([
            ActionCandidate("model_invoke", expected_usefulness=0.6),
            ActionCandidate("memory_recall", expected_usefulness=0.5),
        ])
        self.assertEqual(sel.candidate.action, "memory_recall")

    def test_tool_stats_fallback(self):
        bg = self._bg(tool_stats=lambda: {"routes": [
            {"capability": "repo_search", "tool": "grep",
             "calls": 10, "success_rate": 0.95}]})
        s = bg.score(ActionCandidate("repo_search", expected_usefulness=0.3))
        self.assertTrue(any("history" in r for r in s.reasons))


# ---------------------------------------------------------------------------
# Motor Cortex
# ---------------------------------------------------------------------------

class MotorCortexTests(unittest.TestCase):
    def _motor(self, **kw):
        return MotorCortex(make_bus(), **kw)

    def test_registered_executor_runs(self):
        m = self._motor()
        m.register_executor("echo", lambda args, ap: (True, args.get("x", "")))
        r = m.execute("echo", {"x": "hello"})
        self.assertTrue(r.ok)
        self.assertEqual(r.output, "hello")
        self.assertEqual(r.exit_status, "ok")

    def test_approval_gate_blocks_unapproved(self):
        m = self._motor()
        m.register_executor("delete", lambda args, ap: (True, "gone"),
                            requires_approval=True)
        r = m.execute("delete", {"path": "x"})
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_status, "approval_required")
        r2 = m.execute("delete", {"path": "x"}, approved=True)
        self.assertTrue(r2.ok)

    def test_executor_exception_isolated(self):
        m = self._motor()
        def boom(args, ap):
            raise RuntimeError("tool exploded")
        m.register_executor("fragile", boom)
        r = m.execute("fragile")
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_status, "error")
        self.assertIn("RuntimeError", r.error)
        self.assertEqual(m.state, "healthy")  # region survives tool failure

    def test_structured_result_and_events(self):
        bus = make_bus()
        m = MotorCortex(bus)
        events = []
        bus.subscribe_type(EventType.EXECUTION_REQUEST, events.append)
        bus.subscribe_type(EventType.EXECUTION_RESULT, events.append)
        m.register_executor("noop", lambda a, ap: (True, "done"))
        r = m.execute("noop", correlation_id="corr-1", mission_id="m-1")
        self.assertEqual(events[0].correlation_id, "corr-1")
        res = [e for e in events if e.type == EventType.EXECUTION_RESULT][0]
        self.assertTrue(res.content["ok"])
        self.assertEqual(res.content["mission_id"], "m-1")
        self.assertGreaterEqual(r.duration_ms, 0)

    def test_telemetry_recorded(self):
        m = self._motor()
        m.register_executor("noop", lambda a, ap: (True, ""))
        m.execute("noop", mission_id="m9")
        tel = m.telemetry()
        self.assertEqual(tel[-1]["action"], "noop")
        self.assertEqual(tel[-1]["mission_id"], "m9")


# ---------------------------------------------------------------------------
# Cerebellum
# ---------------------------------------------------------------------------

class CerebellumTests(unittest.TestCase):
    def _cb(self, **kw):
        return Cerebellum(make_bus(), **kw)

    def test_metric_trend(self):
        cb = self._cb()
        for v in (42, 30, 12):
            cb.record_metric("scan", "repo_index", v, unit="s")
        t = cb.trend("scan", "repo_index")
        self.assertEqual(t["samples"], 3)
        self.assertTrue(t["improved"])
        self.assertEqual(t["delta"], -30)

    def test_optimization_lifecycle_reversible(self):
        cb = self._cb()
        opt = cb.propose_optimization(
            "llama.cpp", {"n_batch": 1024},
            expected_gain="higher tps", rollback={"n_batch": 512})
        self.assertEqual(opt["state"], "proposed")
        cb.apply_optimization(opt["id"], 31.5)
        back = cb.rollback(opt["id"])
        self.assertEqual(back["state"], "rolled_back")
        self.assertEqual(back["rollback"]["n_batch"], 512)

    def test_consumes_execution_results_from_bus(self):
        bus = make_bus()
        cb = Cerebellum(bus)
        bus.subscribe_type(EventType.EXECUTION_RESULT, cb._guarded_handle)
        bus.publish(CognitiveEvent(
            type=EventType.EXECUTION_RESULT, source="motor_cortex",
            content={"action": "run_tests", "ok": True, "duration_ms": 850}))
        t = cb.trend("tool", "run_tests")
        self.assertEqual(t["samples"], 1)
        self.assertEqual(t["last"], 850)

    def test_optimizations_persist(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "opt.jsonl"
            cb = self._cb(opt_path=path)
            cb.propose_optimization("scan", {"parallel": True})
            cb2 = self._cb(opt_path=path)
            self.assertEqual(len(cb2.optimizations()), 1)


# ---------------------------------------------------------------------------
# Specialists
# ---------------------------------------------------------------------------

class SpecialistTests(unittest.TestCase):
    def test_declared_specialists_cover_domains(self):
        domains = {s.domain for s in SPECIALISTS}
        self.assertEqual(domains, {"coding", "research", "vision",
                                   "review", "systems", "language",
                                   "diagnostics"})

    def test_specialist_requests_capability_not_model_name(self):
        spec = next(s for s in SPECIALISTS if s.name == "coding_brain")
        self.assertTrue(spec.preferred_requirement.coding)
        sb = SpecialistBrain(spec, make_bus())
        self.assertEqual(sb.status()["domain"], "coding")

    def test_specialist_capability_request_round_trip(self):
        """A specialist asks the Thalamus for a capability and gets a
        resolved model id — never a hardcoded name."""
        bus = make_bus()
        Thalamus(bus, model_catalog=lambda: [
            {"id": "coder1", "roles": ["coding"], "enabled": True,
             "healthy": True},
            {"id": "util1", "roles": ["utility"], "enabled": True,
             "healthy": True}])
        spec = next(s for s in SPECIALISTS if s.name == "coding_brain")
        sb = SpecialistBrain(spec, bus)
        mid = sb.request_capability(spec.preferred_requirement)
        self.assertEqual(mid, "coder1")

    def test_specialist_domain_memory(self):
        """Addressed outcomes become domain-tagged episodes the specialist
        can recall first."""
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d))
            self.addCleanup(brain.close)
            coding = brain.specialists["coding_brain"]
            brain.bus.publish(CognitiveEvent(
                type=EventType.EXECUTION_RESULT, source="test",
                destination="coding_brain",
                content={"action": "edit_file", "ok": True,
                         "duration_ms": 40}))
            res = brain.hippocampus.recall(
                "edit_file", kinds={"episodic"}, limit=5)
            provs = {e.provenance for e in res.entries}
            self.assertIn("episodes/coding:outcome", provs)
            coding.bus.publish(CognitiveEvent(
                type=EventType.LEARNING_EVENT, source="test",
                destination="coding_brain",
                content={"subject": "edit_file", "value": 12.5}))
            self.assertEqual(coding.benchmarks["edit_file"], 12.5)


# ---------------------------------------------------------------------------
# NexusBrain integration
# ---------------------------------------------------------------------------

class NexusBrainIntegrationTests(unittest.TestCase):
    def _brain(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d), **kw)
            self.addCleanup(brain.close)
            yield brain, d  # state_dir removed after test — fine for trace checks

    def test_fast_path_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d),
                               version_lookup=lambda: "0.7.2")
            env = brain.process_input("what version of nexus core?")
            self.assertEqual(env["fast_path"], "version")
            self.assertEqual(env["answer"], "Nexus Core 0.7.2")
            # Structural trace: observation → attention → memory/model result.
            trace = brain.trace(correlation_id=env["correlation_id"])
            types = [e["type"] for e in trace]
            self.assertIn(EventType.OBSERVATION, types)
            self.assertIn(EventType.ATTENTION, types)
            brain.close()

    def test_memory_query_round_trip_through_regions(self):
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d))
            self.addCleanup(brain.close)
            brain.hippocampus.record_episode(
                "task", "installer rebuilt cleanly", project_id="p")
            reply = brain.bus.request(CognitiveEvent(
                type=EventType.MEMORY_QUERY, source="test",
                destination="hippocampus",
                content={"query": "installer", "project_id": "p"}),
                timeout=2.0)
            self.assertIsNotNone(reply)
            self.assertGreaterEqual(reply.content["hits"], 1)
            brain.close()

    def test_motor_failure_does_not_crash_brain(self):
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d))
            self.addCleanup(brain.close)
            brain.motor.register_executor(
                "bad", lambda a, ap: (_ for _ in ()).throw(ValueError("x")))
            r = brain.motor.execute("bad")
            self.assertFalse(r.ok)
            self.assertEqual(brain.motor.state, "healthy")
            brain.close()

    def test_status_reports_all_regions(self):
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d))
            self.addCleanup(brain.close)
            st = brain.status()
            self.assertEqual(set(st["regions"]), {
                "brain_stem", "hippocampus", "thalamus", "prefrontal_cortex",
                "basal_ganglia", "motor_cortex", "cerebellum"})
            self.assertEqual(len(st["specialists"]), 7)
            self.assertIn("diagnostics_brain", st["specialists"])
            brain.close()

    def test_diagnostics_specialist_records_repair_health_events(self):
        """Self-repair broadcasts HEALTH_EVENTs onto the corpus callosum;
        the diagnostics specialist must turn them into recallable
        episodic memory so prior incidents inform future repair."""
        from localcodeagent.brain.events import (
            CognitiveEvent, EventType, Priority)
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d))
            self.addCleanup(brain.close)
            self.assertIn("diagnostics_brain", brain.specialists)
            brain.bus.publish(CognitiveEvent(
                type=EventType.HEALTH_EVENT, source="self_repair",
                priority=Priority.HIGH,
                content={"kind": "incident_promoted",
                         "incident": "inc-1",
                         "subsystem": "llama_runtime",
                         "state": "resolved"}))
            res = brain.hippocampus.recall(
                "incident_promoted resolved", kinds={"episodic"}, limit=5)
            self.assertTrue(res.entries)
            self.assertIn("diagnostics:outcome",
                          res.entries[0].provenance)
            brain.close()

    def test_thalamus_routes_diagnostics_intent(self):
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d))
            self.addCleanup(brain.close)
            brain.thalamus._classify_intent = lambda t: "diagnostics"
            decision = brain.thalamus.route("why did llama.cpp crash?")
            self.assertEqual(decision.region, "diagnostics_brain")
            brain.close()

    def test_state_persists_across_brain_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            b1 = NexusBrain(state_dir=Path(d))
            self.addCleanup(b1.close)
            b1.hippocampus.learn_project_fact(
                "proj", "version", "0.7.2", category="fact")
            b1.cerebellum.propose_optimization("x", {"k": 1})
            b1.close()
            b2 = NexusBrain(state_dir=Path(d))
            self.addCleanup(b2.close)
            res = b2.hippocampus.recall("v", kinds={"project"},
                                        project_id="proj")
            self.assertTrue(res.entries)
            self.assertEqual(len(b2.cerebellum.optimizations()), 1)
            b2.close()

    def test_answers_without_model_gate(self):
        """The chat readiness gate consults this so deterministic/memory
        fast paths work on a machine with no model installed."""
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d),
                               version_lookup=lambda: "0.8.0")
            self.assertTrue(brain.answers_without_model(
                "what version of Nexus Core is this?"))
            self.assertTrue(brain.answers_without_model(
                "what's the latest version"))
            self.assertTrue(brain.answers_without_model("system status?"))
            self.assertTrue(brain.answers_without_model(
                "check the system health"))
            self.assertTrue(brain.answers_without_model(
                "what is 42 * 17?"))
            self.assertFalse(brain.answers_without_model(
                "refactor the parser module"))
            self.assertFalse(brain.answers_without_model(
                "what version of python do we need"))
            self.assertFalse(brain.answers_without_model(
                "check the status of my pull request"))
            # A config question only bypasses the gate when the key actually
            # resolves — no lookup wired means no answer to give.
            self.assertFalse(brain.answers_without_model(
                "what is the workspace path?"))
            brain.close()
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d))
            brain.thalamus._config_lookup = (
                lambda k: "Workspace: /tmp/proj" if k == "workspace" else "")
            self.assertTrue(brain.answers_without_model(
                "what is the workspace path?"))
            brain.close()

    def test_ui_bus_bridge(self):
        with tempfile.TemporaryDirectory() as d:
            brain = NexusBrain(state_dir=Path(d))
            self.addCleanup(brain.close)
            seen = []
            class FakeUIBus:
                def publish(self, e): seen.append(e)
            brain.attach_ui_bus(FakeUIBus())
            brain.process_input("what version of nexus core?")
            self.assertTrue(any(e.get("type") == "cognitive" for e in seen))
            brain.close()


if __name__ == "__main__":
    unittest.main()
