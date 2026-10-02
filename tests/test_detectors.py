"""Signal scanner — evidence-based problem/opportunity detection,
routing, dedupe, cooldown, and supervisor scheduling."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.autonomy import AutonomousSupervisor
from localcodeagent.autonomy.detectors import (
    DETECTORS, SignalScanner, detect_answer_memory_decay,
    detect_approval_backlog, detect_ci_failures, detect_crash_storm,
    detect_disk_pressure, detect_mission_failures, detect_model_failures,
    detect_repair_thrash, detect_startup_regression, new_finding)
from localcodeagent.autonomy.state import AutonomyStore


def make_store(td: str) -> AutonomyStore:
    return AutonomyStore(Path(td) / "data" / "autonomy")


def make_scanner(td: str, sources=None, detectors=None, route=None,
                 interval=0.0) -> SignalScanner:
    return SignalScanner(make_store(td), sources=sources or {},
                         detectors=detectors or DETECTORS,
                         interval_s=interval, route=route)


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


NOW = time.time()


class DetectorUnitTests(unittest.TestCase):
    """Each detector fires on evidence and stays silent without it."""

    def test_crash_storm_fires_at_threshold(self):
        rows = [{"time": NOW - 60, "kind": "llama", "code": -1}
                for _ in range(3)]
        f = detect_crash_storm({"crash_history": lambda n: rows})
        self.assertIsNotNone(f)
        self.assertEqual(f["route"], "repair")
        self.assertGreaterEqual(f["confidence"], 0.6)

    def test_crash_storm_ignores_old_and_sparse(self):
        rows = [{"time": NOW - 7200}] * 5      # all outside window
        self.assertIsNone(detect_crash_storm(
            {"crash_history": lambda n: rows}))
        self.assertIsNone(detect_crash_storm(
            {"crash_history": lambda n: [{"time": NOW}]}))
        self.assertIsNone(detect_crash_storm({}))   # no source → silent

    def test_crash_storm_survives_bad_source(self):
        def boom(_n):
            raise RuntimeError("no telemetry")
        self.assertIsNone(detect_crash_storm({"crash_history": boom}))

    def test_mission_failures(self):
        ms = [{"status": "failed", "updated_at": NOW}] * 3 + \
             [{"status": "completed", "updated_at": NOW}]
        f = detect_mission_failures({"missions": lambda: ms})
        self.assertIsNotNone(f)
        self.assertEqual(f["route"], "mission")
        self.assertAlmostEqual(f["confidence"], 0.75, places=2)
        # too few samples / low rate → silent
        self.assertIsNone(detect_mission_failures(
            {"missions": lambda: ms[:2]}))
        self.assertIsNone(detect_mission_failures(
            {"missions": lambda: [{"status": "completed",
                                   "updated_at": NOW}] * 5}))

    def test_answer_memory_decay_needs_samples(self):
        low = {"hits": 5, "misses": 45}
        f = detect_answer_memory_decay(
            {"answer_memory_stats": lambda: low})
        self.assertIsNotNone(f)
        self.assertEqual(f["route"], "suggestion")   # opportunity, not alarm
        # below the sample floor → silent
        self.assertIsNone(detect_answer_memory_decay(
            {"answer_memory_stats": lambda: {"hits": 0, "misses": 10}}))
        # healthy rate → silent
        self.assertIsNone(detect_answer_memory_decay(
            {"answer_memory_stats": lambda: {"hits": 80, "misses": 20}}))

    def test_model_failures(self):
        rows = [{"id": "m1", "samples": 30, "failures": 6},
                {"id": "m2", "samples": 10, "failures": 0}]
        f = detect_model_failures(
            {"model_telemetry": lambda: {"models": rows}})
        self.assertIsNotNone(f)
        self.assertEqual(f["evidence"]["worst_model"], "m1")
        self.assertIsNone(detect_model_failures(
            {"model_telemetry": lambda: {"models": [
                {"samples": 100, "failures": 2}]}}))

    def test_disk_pressure_severity(self):
        crit = detect_disk_pressure({"disk_free_gb": lambda: 0.5})
        self.assertEqual(crit["severity"], "critical")
        high = detect_disk_pressure({"disk_free_gb": lambda: 1.5})
        self.assertEqual(high["severity"], "high")
        self.assertIsNone(detect_disk_pressure(
            {"disk_free_gb": lambda: 50.0}))

    def test_repair_thrash(self):
        rows = [{"signature": "sig:A"}] * 3 + [{"signature": "sig:B"}]
        f = detect_repair_thrash({"repairs": lambda: rows})
        self.assertIsNotNone(f)
        self.assertEqual(f["route"], "mission")
        self.assertIn("sig:A", f["signature"])
        self.assertIsNone(detect_repair_thrash(
            {"repairs": lambda: [{"signature": "sig:A"}] * 2}))

    def test_startup_regression(self):
        f = detect_startup_regression(
            {"startup_ms": lambda: {"total_ms": 20000,
                                    "expected_ms": 8000}})
        self.assertIsNotNone(f)
        self.assertIsNone(detect_startup_regression(
            {"startup_ms": lambda: {"total_ms": 9000,
                                    "expected_ms": 8000}}))
        self.assertIsNone(detect_startup_regression(
            {"startup_ms": lambda: {"total_ms": 20000}}))  # no baseline

    def test_approval_backlog(self):
        stale = [{"created_at": NOW - 90000}]
        f = detect_approval_backlog(
            {"pending_approvals": lambda: stale})
        self.assertIsNotNone(f)
        self.assertEqual(f["route"], "suggestion")
        self.assertIsNone(detect_approval_backlog(
            {"pending_approvals": lambda: [{"created_at": NOW}]}))

    def test_ci_failures(self):
        runs = [{"databaseId": 123, "displayTitle": "v0.11.0",
                 "conclusion": "failure"},
                {"databaseId": 124, "displayTitle": "wip",
                 "conclusion": "failure"}]
        out = detect_ci_failures({"ci_failures": lambda: runs})
        self.assertEqual(len(out), 2)                 # one finding per run
        self.assertEqual(out[0]["route"], "repair")
        self.assertEqual(out[0]["signature"], "ci_failure:123")
        self.assertIsNone(detect_ci_failures({"ci_failures": lambda: []}))
        self.assertIsNone(detect_ci_failures({}))     # no gh → silent

    def test_finding_defaults_and_clamps(self):
        f = new_finding(kind="k", title="t", severity="bogus",
                        confidence=9.9)
        self.assertEqual(f["severity"], "normal")
        self.assertEqual(f["confidence"], 1.0)
        self.assertEqual(f["status"], "open")


class ScannerTests(unittest.TestCase):
    def test_interval_gate(self):
        sc = make_scanner(self._td(), interval=3600,
                          detectors=[lambda s: new_finding(
                              kind="k", title="t")])
        self.assertEqual(len(sc.tick(NOW)), 1)
        self.assertEqual(sc.tick(NOW + 10), [])        # inside interval
        self.assertEqual(len(sc.tick(NOW + 3700)), 0)  # deduped, not new

    _tds = []

    def _td(self):
        td = tempfile.mkdtemp()
        self._tds.append(td)
        return td

    def test_dedupe_merges_sightings(self):
        det = lambda s: new_finding(kind="k", title="same",
                                    signature="sig:1")
        sc = make_scanner(self._td(), detectors=[det])
        sc.tick(NOW)
        sc._last_scan = 0
        sc.tick(NOW + 200)
        rows = sc.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sightings"], 2)

    def test_route_called_once_per_finding(self):
        routed = []
        det = lambda s: new_finding(kind="k", title="t",
                                    signature="sig:r", route="repair")
        sc = make_scanner(self._td(), detectors=[det],
                          route=lambda f: routed.append(f) or "inc-1")
        sc.tick(NOW)
        self.assertEqual(len(routed), 1)
        row = sc.list()[0]
        self.assertEqual(row["status"], "acted")
        self.assertEqual(row["routed_to"], "inc-1")
        sc.force(NOW + 100)
        self.assertEqual(len(routed), 1)     # no re-route inside cooldown

    def test_cooldown_allows_re_route(self):
        from localcodeagent.autonomy import detectors as d
        routed = []
        det = lambda s: new_finding(kind="k", title="t",
                                    signature="sig:c", route="repair")
        sc = make_scanner(self._td(), detectors=[det],
                          route=lambda f: routed.append(f) or "inc-x")
        sc.tick(NOW)
        old = d.ROUTE_COOLDOWN_S
        try:
            d.ROUTE_COOLDOWN_S = 60.0
            sc.force(NOW + 120)
        finally:
            d.ROUTE_COOLDOWN_S = old
        self.assertEqual(len(routed), 2)

    def test_failed_route_stays_open(self):
        det = lambda s: new_finding(kind="k", title="t", route="mission")
        sc = make_scanner(self._td(), detectors=[det],
                          route=lambda f: "")
        sc.tick(NOW)
        self.assertEqual(sc.list()[0]["status"], "open")

    def test_suggestions_never_route(self):
        routed = []
        det = lambda s: new_finding(kind="k", title="tip",
                                    route="suggestion")
        sc = make_scanner(self._td(), detectors=[det],
                          route=lambda f: routed.append(f) or "x")
        sc.tick(NOW)
        self.assertEqual(routed, [])
        self.assertEqual(sc.list()[0]["status"], "open")

    def test_bad_detector_never_stalls(self):
        def boom(_s):
            raise RuntimeError("detector crashed")
        good = lambda s: new_finding(kind="ok", title="fine")
        sc = make_scanner(self._td(), detectors=[boom, good])
        out = sc.tick(NOW)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "ok")

    def test_dismiss(self):
        det = lambda s: new_finding(kind="k", title="t")
        sc = make_scanner(self._td(), detectors=[det])
        sc.tick(NOW)
        fid = sc.list()[0]["id"]
        self.assertTrue(sc.dismiss(fid))
        self.assertEqual(sc.list(), [])
        self.assertFalse(sc.dismiss("nope"))

    def test_findings_persist_across_restart(self):
        td = self._td()
        det = lambda s: new_finding(kind="k", title="t",
                                    signature="sig:p")
        sc = make_scanner(td, detectors=[det])
        sc.tick(NOW)
        sc2 = make_scanner(td, detectors=[det])
        self.assertEqual(len(sc2.list()), 1)   # durable across instances

    def test_severity_ordering(self):
        dets = [lambda s: new_finding(kind="lo", title="low",
                                      severity="low", signature="a"),
                lambda s: new_finding(kind="hi", title="high",
                                      severity="critical", signature="b")]
        sc = make_scanner(self._td(), detectors=dets)
        sc.tick(NOW)
        self.assertEqual(sc.list()[0]["severity"], "critical")


class SupervisorRoutingTests(unittest.TestCase):
    def test_repair_route_opens_incident(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            calls = []

            class FakeRepair:
                def report_failure(self, **kw):
                    calls.append(kw)
                    return {"id": "inc-9"}, True

                def list(self):
                    return []

            sup.repair = FakeRepair()
            rid = sup._route_finding(new_finding(
                kind="disk_pressure", title="Disk low",
                route="repair", signature="sig:d"))
            self.assertEqual(rid, "inc-9")
            self.assertEqual(calls[0]["source"], "detector:disk_pressure")

    def test_mission_route_creates_and_dedupes(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            f = new_finding(kind="mission_failure_rate",
                            title="Failures high", route="mission",
                            severity="high", signature="sig:m",
                            evidence={"failed": 4})
            m1 = sup._route_finding(f)
            m2 = sup._route_finding(f)
            self.assertEqual(m1, m2)           # same live mission
            mission = sup.missions.get(m1)
            self.assertEqual(mission["source"], "detector")
            self.assertEqual(mission["source_id"], "sig:m")
            self.assertEqual(mission["status"], "ready")
            self.assertIn("sig:m", str(mission))

    def test_suggestion_route_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            self.assertEqual(sup._route_finding(new_finding(
                kind="k", title="t", route="suggestion")), "")

    def test_scanner_tick_inside_supervisor(self):
        with tempfile.TemporaryDirectory() as td:
            seen = []
            sup = make_sup(td)
            sup.scanner.detectors = [
                lambda s: new_finding(kind="k", title="sig",
                                      signature="sig:sup")]
            sup.scanner.interval_s = 0
            sup.scanner.tick(time.time())
            self.assertEqual(len(sup.scanner.list()), 1)

    def test_critical_finding_maps_urgent_priority(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            mid = sup._route_finding(new_finding(
                kind="crash_storm", title="storm", route="mission",
                severity="critical", signature="sig:crit"))
            self.assertEqual(sup.missions.get(mid)["priority"], "urgent")
            mid2 = sup._route_finding(new_finding(
                kind="repair_thrash", title="thrash", route="mission",
                severity="normal", signature="sig:bg"))
            self.assertEqual(sup.missions.get(mid2)["priority"],
                             "background")


class SchedulingTests(unittest.TestCase):
    def _mission(self, sup, priority, age_s=0):
        m = sup.missions.create(
            objective=f"{priority} work", title=priority,
            scope="one_shot", priority=priority,
            workspace=str(sup.workspace))
        if age_s:
            sup.missions.mutate(m["id"], lambda r: r.update(
                created_at=time.time() - age_s))
        return m["id"]

    def test_priority_ordering(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            bg = self._mission(sup, "background")
            urg = self._mission(sup, "urgent")
            norm = self._mission(sup, "normal")
            order = [m["id"] for m in sup.missions.list()
                     if m["id"] in {bg, urg, norm}]
            self.assertEqual(order, [urg, norm, bg])

    def test_aging_promotes_starved_background(self):
        from localcodeagent.autonomy.missions import effective_rank
        young = {"priority": "maintenance", "created_at": time.time()}
        old = {"priority": "maintenance",
               "created_at": time.time() - 25 * 3600}
        self.assertEqual(effective_rank(young), 4)
        self.assertEqual(effective_rank(old), 2)   # floor: normal

    def test_aging_never_outranks_interactive(self):
        from localcodeagent.autonomy.missions import effective_rank
        ancient = {"priority": "maintenance",
                   "created_at": time.time() - 30 * 24 * 3600}
        inter = {"priority": "interactive",
                 "created_at": time.time()}
        self.assertGreater(effective_rank(ancient),
                           effective_rank(inter))

    def test_lane_busy_blocks_normal_not_urgent(self):
        """Interactive lane busy → normal missions wait, urgent proceed."""
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td, lane_free=lambda: False)
            # build a mission whose graph has a runnable agent node
            for prio in ("normal", "urgent"):
                m = sup.missions.create(
                    objective="x", title=prio, scope="one_shot",
                    priority=prio, workspace=str(sup.workspace))
                sup.missions.transition(m["id"], "ready")
                graph = {"nodes": [{"id": "n1", "kind": "agent",
                                    "state": "ready", "title": "t"}]}
                sup.missions.update(m["id"], status="executing",
                                    graph=graph)
            sup._step_executing  # exists
            # stepping the normal mission must not claim the node
            norm = next(m for m in sup.missions.list()
                        if m["title"] == "normal")
            urg = next(m for m in sup.missions.list()
                       if m["title"] == "urgent")
            sup._step_executing(norm["id"])
            n_norm = sup.missions.get(norm["id"])["graph"]["nodes"][0]
            self.assertEqual(n_norm["state"], "ready")   # still waiting
            sup._step_executing(urg["id"])
            n_urg = sup.missions.get(urg["id"])["graph"]["nodes"][0]
            self.assertIn(n_urg["state"], {"running", "completed"})


if __name__ == "__main__":
    unittest.main()
