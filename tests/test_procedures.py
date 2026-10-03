"""Procedural memory for recurring missions — learn what worked, stop
re-spawning what keeps failing."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.autonomy import AutonomousSupervisor
from localcodeagent.autonomy.procedures import (
    ProcedureMemory, mission_key)
from localcodeagent.autonomy.state import AutonomyStore
from localcodeagent.autonomy.detectors import new_finding


NOW = time.time()


def make_store(td: str) -> AutonomyStore:
    return AutonomyStore(Path(td) / "data" / "autonomy")


def make_sup(td: str, **kw) -> AutonomousSupervisor:
    root = Path(td)
    defaults = dict(
        workspace=root,
        store_root=root / "data" / "autonomy",
        executor=lambda m, n, cb: {"ok": True, "output": "done"},
        lane_free=lambda: True,
    )
    defaults.update(kw)
    return AutonomousSupervisor(**defaults)


def _mission(mid: str, status="completed", source="detector",
             source_id="sig:x", **kw) -> dict:
    m = dict(id=mid, title=f"m {mid}", objective="investigate x",
             status=status, source=source, source_id=source_id,
             created_at=NOW - 120, completed_at=NOW - 10,
             tasks=[])
    m.update(kw)
    return m


class MemoryTests(unittest.TestCase):
    def test_records_terminal_keyed_mission(self):
        with tempfile.TemporaryDirectory() as td:
            mem = ProcedureMemory(make_store(td))
            row = mem.record_terminal(_mission("m1"))
            self.assertIsNotNone(row)
            self.assertEqual(row["key"], "detector:sig:x")
            self.assertEqual(row["status"], "completed")
            self.assertAlmostEqual(row["duration_s"], 110, delta=1)

    def test_ignores_unkeyed_and_live(self):
        with tempfile.TemporaryDirectory() as td:
            mem = ProcedureMemory(make_store(td))
            self.assertIsNone(mem.record_terminal(
                _mission("u1", source="user", source_id="")))
            self.assertIsNone(mem.record_terminal(
                _mission("live", status="executing")))
            self.assertIsNone(mem.record_terminal(
                _mission("nosrc", source="", source_id="")))

    def test_idempotent_on_mission_id(self):
        with tempfile.TemporaryDirectory() as td:
            mem = ProcedureMemory(make_store(td))
            mem.record_terminal(_mission("m1"))
            self.assertIsNone(mem.record_terminal(_mission("m1")))
            self.assertEqual(len(mem.recall("detector", "sig:x")), 1)

    def test_recall_newest_first(self):
        with tempfile.TemporaryDirectory() as td:
            mem = ProcedureMemory(make_store(td))
            mem.record_terminal(_mission(
                "old", completed_at=NOW - 500))
            mem.record_terminal(_mission(
                "new", status="failed", completed_at=NOW))
            runs = mem.recall("detector", "sig:x")
            self.assertEqual([r["mission_id"] for r in runs],
                             ["new", "old"])

    def test_failing_needs_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as td:
            mem = ProcedureMemory(make_store(td))
            mem.record_terminal(_mission("a", status="failed"))
            self.assertFalse(mem.failing("detector", "sig:x"))
            mem.record_terminal(_mission("b", status="cancelled"))
            self.assertTrue(mem.failing("detector", "sig:x",
                                        min_gap_s=3600))
            # a success in between breaks the streak
            mem.record_terminal(_mission("c", completed_at=NOW + 100))
            self.assertFalse(mem.failing("detector", "sig:x"))

    def test_failing_backoff_releases(self):
        with tempfile.TemporaryDirectory() as td:
            mem = ProcedureMemory(make_store(td))
            mem.record_terminal(_mission(
                "a", status="failed", completed_at=NOW - 7000))
            mem.record_terminal(_mission(
                "b", status="failed", completed_at=NOW - 6500))
            # last failure > min_gap ago → attempt allowed again
            self.assertFalse(mem.failing("detector", "sig:x",
                                         min_gap_s=3600))
            self.assertTrue(mem.failing("detector", "sig:x",
                                        min_gap_s=10000))

    def test_summary_line(self):
        with tempfile.TemporaryDirectory() as td:
            mem = ProcedureMemory(make_store(td))
            mem.record_terminal(_mission("a", status="failed"))
            line = mem.summary({"source": "detector",
                                "source_id": "sig:x"})
            self.assertIn("Prior runs", line)
            self.assertIn("failed", line)
            self.assertEqual(
                mem.summary({"source": "detector",
                             "source_id": "sig:none"}), "")

    def test_persistence_across_instances(self):
        with tempfile.TemporaryDirectory() as td:
            mem = ProcedureMemory(make_store(td))
            mem.record_terminal(_mission("m1"))
            mem2 = ProcedureMemory(make_store(td))
            self.assertEqual(len(mem2.recall("detector", "sig:x")), 1)

    def test_mission_key(self):
        self.assertEqual(mission_key(_mission("m")), "detector:sig:x")
        self.assertEqual(mission_key(_mission("m", source="user")), "")
        self.assertEqual(mission_key(_mission("m", source_id="")), "")


class SupervisorIntegrationTests(unittest.TestCase):
    def test_terminal_missions_recorded_on_tick(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.missions.create(
                objective="investigate", title="t",
                scope="one_shot", priority="normal",
                source="detector", source_id="sig:t",
                created_by="test", workspace=str(sup.workspace))
            sup.missions.transition(m["id"], "ready")
            sup.missions.transition(m["id"], "planning")
            sup.missions.transition(m["id"], "failed",
                                    detail="injected")
            sup.tick()
            runs = sup.procedures.recall("detector", "sig:t")
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "failed")

    def test_failing_signature_suppresses_respawn(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.procedures.record_terminal(_mission(
                "f1", status="failed", source_id="sig:loop"))
            sup.procedures.record_terminal(_mission(
                "f2", status="failed", source_id="sig:loop"))
            out = sup._route_finding(new_finding(
                kind="repair_thrash", title="thrash", route="mission",
                signature="sig:loop"))
            self.assertEqual(out, "suppressed")
            live = [m for m in sup.missions.list()
                    if m.get("source_id") == "sig:loop"]
            self.assertEqual(live, [])

    def test_prior_runs_reach_objective(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.procedures.record_terminal(_mission(
                "p1", status="completed", source_id="sig:p",
                completed_at=NOW - 8000))
            out = sup._route_finding(new_finding(
                kind="repair_thrash", title="thrash", route="mission",
                signature="sig:p"))
            mission = sup.missions.get(out)
            self.assertIsNotNone(mission)
            self.assertIn("Prior runs", mission["objective"])

    def test_goal_repair_suppression_not_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.procedures.record_terminal(_mission(
                "g1", status="failed", source="goal",
                source_id="goal-1"))
            sup.procedures.record_terminal(_mission(
                "g2", status="failed", source="goal",
                source_id="goal-1"))
            goal = {"id": "goal-1", "title": "keep green",
                    "priority": "normal"}
            out = sup._spawn_goal_repair_mission(goal, {"health": "degraded"})
            self.assertEqual(out.get("suppressed"), "prior_runs_failing")
            self.assertEqual(sup.missions.list(), [])


if __name__ == "__main__":
    unittest.main()
