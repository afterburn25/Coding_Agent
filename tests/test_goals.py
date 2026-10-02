"""Nexus Goal Manager — durable evaluated goals and self-generated repairs."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.autonomy import AutonomousSupervisor
from localcodeagent.autonomy.goals import GoalManager
from localcodeagent.autonomy.metrics import MetricRegistry
from localcodeagent.autonomy.state import AutonomyStore


def make_sup(td: str, metrics=None, **kw):
    root = Path(td)
    defaults = dict(
        workspace=root,
        store_root=root / "data" / "autonomy",
        executor=lambda m, n, cb: {"ok": True, "output": "done"},
        lane_free=lambda: True,
        metrics=metrics,
    )
    defaults.update(kw)
    return AutonomousSupervisor(**defaults)


def breached_goal(gm: GoalManager, value_key: str = "incidents",
                  **kw) -> dict:
    args = dict(title="Keep incidents low",
                type="reliability",
                metrics=[{"key": value_key, "op": "<=", "target": 3.0}])
    args.update(kw)
    return gm.add(**args)


def terminal(sup: AutonomousSupervisor, mission_id: str, status: str):
    """Drive a spawned repair to a terminal state via legal transitions."""
    sup.missions.transition(mission_id, "active")
    sup.missions.transition(mission_id, "executing")
    sup.missions.transition(mission_id, status)


class MetricRegistryTests(unittest.TestCase):
    def test_measured_value(self):
        reg = MetricRegistry()
        reg.register("disk_free_gb", lambda: 512.0,
                     description="free disk", unit="GB")
        out = reg.measure("disk_free_gb")
        self.assertTrue(out["ok"])
        self.assertEqual(out["value"], 512.0)
        self.assertEqual(out["unit"], "GB")

    def test_unregistered_metric_is_unknown(self):
        out = MetricRegistry().measure("does_not_exist")
        self.assertFalse(out["ok"])
        self.assertIsNone(out["value"])

    def test_failing_provider_is_unknown_not_fake(self):
        reg = MetricRegistry()
        reg.register("broken", lambda: 1 / 0)
        out = reg.measure("broken")
        self.assertFalse(out["ok"])
        self.assertIsNone(out["value"])

    def test_non_numeric_result_is_unknown(self):
        reg = MetricRegistry()
        reg.register("bad", lambda: "not a number")
        out = reg.measure("bad")
        self.assertFalse(out["ok"])

    def test_available_lists_specs_for_ui(self):
        reg = MetricRegistry()
        reg.register("a", lambda: 1.0, description="metric a", unit="x")
        specs = reg.available()
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["key"], "a")
        self.assertEqual(specs[0]["unit"], "x")


class GoalSchemaTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.gm = GoalManager(
            AutonomyStore(Path(self.td.name) / "autonomy"),
            MetricRegistry())

    def tearDown(self):
        self.td.cleanup()

    def test_add_persists_full_schema(self):
        g = self.gm.add(
            "Keep incidents low", description="desc", owner="user",
            priority="high", type="security",
            constraints=["no_external_writes"],
            metrics=[{"key": "incidents", "op": "<=", "target": 3,
                      "warn": 1}],
            escalation_policy="notify",
            autonomy_profile="local_autonomous",
            review_interval_s=120, mission_cooldown_s=600)
        self.assertTrue(g["id"].startswith("g-"))
        self.assertEqual(g["status"], "active")
        self.assertEqual(g["health"], "unknown")
        self.assertEqual(g["owner"], "user")
        self.assertEqual(g["priority"], "high")
        self.assertEqual(g["type"], "security")
        self.assertIn("created_at", g)
        self.assertIn("linked_missions", g)
        self.assertIn("next_review_at", g)
        self.assertEqual(g["metrics"][0]["warn"], 1.0)
        self.assertEqual(g["history"][0]["event"], "created")

    def test_persistence_roundtrip(self):
        g = breached_goal(self.gm)
        fresh = GoalManager(
            AutonomyStore(Path(self.td.name) / "autonomy"),
            MetricRegistry())
        got = fresh.get(g["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["title"], "Keep incidents low")
        self.assertEqual(got["metrics"][0]["key"], "incidents")

    def test_blank_title_rejected(self):
        with self.assertRaises(ValueError):
            self.gm.add("   ")

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError):
            self.gm.add("g", type="not_a_type")

    def test_bad_metric_rows_dropped(self):
        g = self.gm.add("g", metrics=[
            {"key": "x", "op": "≈", "target": 1},          # bad op
            {"key": "", "op": "<=", "target": 1},          # no key
            {"key": "ok", "op": "<=", "target": 2}])
        self.assertEqual(len(g["metrics"]), 1)
        self.assertEqual(g["metrics"][0]["key"], "ok")

    def test_enable_disable(self):
        g = breached_goal(self.gm)
        self.assertTrue(self.gm.set_enabled(g["id"], False))
        self.assertEqual(self.gm.get(g["id"])["status"], "paused")
        self.assertTrue(self.gm.set_enabled(g["id"], True))
        self.assertEqual(self.gm.get(g["id"])["status"], "active")

    def test_archive(self):
        g = breached_goal(self.gm)
        self.assertTrue(self.gm.archive(g["id"]))
        self.assertEqual(self.gm.get(g["id"])["status"], "archived")
        self.assertNotIn(g["id"], {r["id"] for r in self.gm.list()})

    def test_interval_floors(self):
        g = self.gm.add("g", metrics=[{"key": "x", "op": "<", "target": 1}],
                        review_interval_s=1, mission_cooldown_s=1)
        self.assertGreaterEqual(g["review_interval_s"], 60)
        self.assertGreaterEqual(g["mission_cooldown_s"], 60)

    def test_seed_defaults_creates_health_floor(self):
        goals = self.gm.list()
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["owner"], "system")
        self.assertTrue(goals[0]["metrics"])


class GoalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    def sup(self, providers: dict, **kw):
        reg = MetricRegistry()
        for key, fn in providers.items():
            reg.register(key, fn)
        return make_sup(self.td.name, metrics=reg, **kw)

    def test_healthy_when_threshold_met(self):
        sup = self.sup({"incidents": lambda: 0.0})
        g = sup.goal_manager.add("Zero incidents", metrics=[
            {"key": "incidents", "op": "<", "target": 3}])
        out = sup.goal_manager.evaluate(g["id"])
        self.assertEqual(out["health"], "healthy")
        self.assertEqual(out["metrics"][0]["state"], "ok")
        self.assertEqual(out["metrics"][0]["value"], 0.0)

    def test_satisfied_terminal_goal(self):
        sup = self.sup({"incidents": lambda: 0.0})
        g = sup.goal_manager.add("One-shot", continuous=False, metrics=[
            {"key": "incidents", "op": "<", "target": 3}])
        out = sup.goal_manager.evaluate(g["id"])
        self.assertEqual(out["health"], "satisfied")
        self.assertEqual(sup.goal_manager.get(g["id"])["status"],
                         "completed")

    def test_violated_when_threshold_breached(self):
        sup = self.sup({"incidents": lambda: 9.0})
        g = sup.goal_manager.add("Zero incidents", metrics=[
            {"key": "incidents", "op": "<", "target": 3}])
        out = sup.goal_manager.evaluate(g["id"])
        self.assertEqual(out["health"], "violated")
        self.assertEqual(out["metrics"][0]["state"], "breach")

    def test_degrading_on_warn_band(self):
        sup = self.sup({"incidents": lambda: 2.0})
        g = sup.goal_manager.add("Incidents", metrics=[
            {"key": "incidents", "op": "<=", "target": 3, "warn": 1}])
        out = sup.goal_manager.evaluate(g["id"])
        self.assertEqual(out["health"], "degrading")
        self.assertEqual(out["metrics"][0]["state"], "warn")

    def test_unknown_metric_not_treated_healthy(self):
        sup = self.sup({})  # no providers bound
        g = sup.goal_manager.add("Measured by nothing", metrics=[
            {"key": "phantom", "op": "<", "target": 3}])
        out = sup.goal_manager.evaluate(g["id"])
        self.assertEqual(out["health"], "unknown")
        self.assertEqual(sup.missions.list(), [])

    def test_no_metrics_is_unknown(self):
        sup = self.sup({})
        g = sup.goal_manager.add("No metrics")
        out = sup.goal_manager.evaluate(g["id"])
        self.assertEqual(out["health"], "unknown")

    def test_evaluate_missing_goal(self):
        sup = self.sup({})
        self.assertIsNone(sup.goal_manager.evaluate("g-nope"))

    def test_evaluation_recorded_in_history(self):
        sup = self.sup({"x": lambda: 1.0})
        g = sup.goal_manager.add("g", metrics=[
            {"key": "x", "op": ">", "target": 0}])
        sup.goal_manager.evaluate(g["id"])
        got = sup.goal_manager.get(g["id"])
        self.assertEqual(got["evaluations"], 1)
        self.assertEqual(len(got["health_history"]), 1)
        self.assertEqual(got["health_history"][0]["health"], "healthy")

    def test_next_review_advances(self):
        sup = self.sup({"x": lambda: 1.0})
        g = sup.goal_manager.add("g", metrics=[
            {"key": "x", "op": ">", "target": 0}],
            review_interval_s=99)
        before = sup.goal_manager.get(g["id"])["next_review_at"]
        sup.goal_manager.evaluate(g["id"])
        after = sup.goal_manager.get(g["id"])["next_review_at"]
        self.assertGreater(after, before)


class GoalRepairMissionTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    def degraded_sup(self, value=99.0, **goal_kw):
        reg = MetricRegistry()
        reg.register("incidents", lambda: value)
        sup = make_sup(self.td.name, metrics=reg)
        g = sup.goal_manager.add("Keep incidents low", metrics=[
            {"key": "incidents", "op": "<=", "target": 3}], **goal_kw)
        return sup, g

    def test_degraded_goal_spawns_repair_mission(self):
        sup, g = self.degraded_sup()
        sup.goal_manager.evaluate(g["id"])
        missions = sup.missions.list()
        self.assertEqual(len(missions), 1)
        m = missions[0]
        self.assertEqual(m["source"], "goal")
        self.assertEqual(m["source_id"], g["id"])
        self.assertEqual(m["goal_id"], g["id"])
        self.assertEqual(m["created_by"], "goal_manager")
        self.assertEqual(m["status"], "ready")
        # WHY the mission exists is recorded as structured evidence
        ev = m["trigger_evidence"]
        self.assertEqual(ev["health"], "violated")
        self.assertEqual(ev["metrics"][0]["value"], 99.0)
        # and the goal links back to its repair mission
        links = sup.goal_manager.get(g["id"])["linked_missions"]
        self.assertEqual(links[-1]["id"], m["id"])

    def test_duplicate_suppression_while_repair_live(self):
        sup, g = self.degraded_sup()
        sup.goal_manager.evaluate(g["id"])
        sup.goal_manager.evaluate(g["id"])  # repair still live → suppressed
        self.assertEqual(len(sup.missions.list()), 1)
        hist = sup.goal_manager.get(g["id"])["history"]
        suppressed = [h for h in hist if h["event"] == "mission_suppressed"]
        self.assertTrue(suppressed)
        self.assertIn("already live", suppressed[-1]["detail"])

    def test_cooldown_after_mission_terminates(self):
        sup, g = self.degraded_sup()
        sup.goal_manager.evaluate(g["id"])
        m = sup.missions.list()[0]
        terminal(sup, m["id"], "completed")
        # still degraded + inside cooldown → no respawn
        sup.goal_manager.evaluate(g["id"])
        self.assertEqual(len(sup.missions.list()), 1)
        hist = sup.goal_manager.get(g["id"])["history"]
        suppressed = [h for h in hist if h["event"] == "mission_suppressed"]
        self.assertTrue(suppressed)
        self.assertIn("cooldown", suppressed[-1]["detail"])

    def test_notify_policy_does_not_spawn(self):
        sup, g = self.degraded_sup(escalation_policy="notify")
        sup.goal_manager.evaluate(g["id"])
        self.assertEqual(sup.missions.list(), [])
        notifs = sup.store.notifications.rows()
        self.assertTrue(any("Keep incidents low" in
                            (r.get("title") or "") + (r.get("message") or "")
                            for r in notifs))

    def test_observe_policy_is_silent(self):
        sup, g = self.degraded_sup(escalation_policy="observe")
        sup.goal_manager.evaluate(g["id"])
        self.assertEqual(sup.missions.list(), [])
        self.assertEqual(sup.store.notifications.rows(), [])

    def test_paused_autonomy_blocks_spawn(self):
        sup, g = self.degraded_sup()
        sup.policy.set_paused(True)
        sup.goal_manager.evaluate(g["id"])
        self.assertEqual(sup.missions.list(), [])
        self.assertEqual(sup.goal_manager.get(g["id"])["health"], "blocked")


class GoalOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    def _setup(self):
        reg = MetricRegistry()
        reg.register("incidents", lambda: 99.0)
        sup = make_sup(self.td.name, metrics=reg)
        g = sup.goal_manager.add("g", metrics=[
            {"key": "incidents", "op": "<=", "target": 3}])
        sup.goal_manager.evaluate(g["id"])
        m = sup.missions.list()[0]
        return sup, g, m

    def test_repair_success_raises_confidence(self):
        sup, g, m = self._setup()
        before = sup.goal_manager.get(g["id"])["confidence"]
        terminal(sup, m["id"], "completed")
        sup.tick()  # _resolve_missions feeds the outcome back
        got = sup.goal_manager.get(g["id"])
        self.assertGreater(got["confidence"], before)
        self.assertEqual(got["mission_outcomes"]["success"], 1)
        link = got["linked_missions"][-1]
        self.assertTrue(link["resolved"])
        self.assertEqual(link["status"], "completed")

    def test_repair_failure_drops_confidence(self):
        sup, g, m = self._setup()
        before = sup.goal_manager.get(g["id"])["confidence"]
        terminal(sup, m["id"], "failed")
        sup.tick()
        got = sup.goal_manager.get(g["id"])
        self.assertLess(got["confidence"], before)
        self.assertEqual(got["mission_outcomes"]["failure"], 1)
        # a failed repair makes the next evaluation report "degrading"
        # even if the metric recovered — evidence of an unresolved cause
        reg = MetricRegistry()
        reg.register("incidents", lambda: 0.0)
        sup.metric_registry = reg
        sup.goal_manager._metrics = reg
        out = sup.goal_manager.evaluate(g["id"])
        self.assertEqual(out["health"], "degrading")


class TickIntegrationTests(unittest.TestCase):
    def test_tick_evaluates_due_goals(self):
        with tempfile.TemporaryDirectory() as td:
            reg = MetricRegistry()
            reg.register("incidents", lambda: 99.0)
            sup = make_sup(td, metrics=reg)
            g = sup.goal_manager.add("g", metrics=[
                {"key": "incidents", "op": "<=", "target": 3}])
            sup.tick()
            got = sup.goal_manager.get(g["id"])
            self.assertEqual(got["health"], "violated")
            self.assertEqual(len(sup.missions.list()), 1)

    def test_tick_skips_disabled_goals(self):
        with tempfile.TemporaryDirectory() as td:
            reg = MetricRegistry()
            reg.register("incidents", lambda: 99.0)
            sup = make_sup(td, metrics=reg)
            g = sup.goal_manager.add("g", enabled=False, metrics=[
                {"key": "incidents", "op": "<=", "target": 3}])
            sup.tick()
            self.assertEqual(sup.missions.list(), [])
            self.assertIsNone(
                sup.goal_manager.get(g["id"])["last_evaluated_at"])

    def test_stopped_autonomy_blocks_repairs(self):
        with tempfile.TemporaryDirectory() as td:
            reg = MetricRegistry()
            reg.register("incidents", lambda: 99.0)
            sup = make_sup(td, metrics=reg)
            sup.goal_manager.add("g", metrics=[
                {"key": "incidents", "op": "<=", "target": 3}])
            sup.policy.set_stopped(True)
            sup.tick()
            self.assertEqual(sup.missions.list(), [])
            got = [g for g in sup.goal_manager.list()
                   if g["owner"] == "user"][0]
            self.assertEqual(got["health"], "blocked")


if __name__ == "__main__":
    unittest.main()
