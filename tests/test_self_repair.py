"""Nexus Self-Repair — fault-injection scenarios and unit coverage.

Scenarios (per spec §32):
  A. injected code bug   → detect → localize → worktree patch → tests →
     review → canary → promote → learn
  B. bad patch           → candidate rejected, stable tree untouched,
     budgets stop the loop
  C. promoted regression → last-known-good snapshot restores the tree
  D. recurring runtime   → operational (procedural) repair, no code path
  E. restart mid-repair  → persisted incident resumes, finished stages
     are not repeated
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from localcodeagent.autonomy.state import AutonomyStore
from localcodeagent.self_repair import (
    SelfRepairCoordinator, Detector, Localizer, Diagnostician,
    RepairMemory)
from localcodeagent.self_repair.canary import Canary
from localcodeagent.self_repair.coordinator import redact
from localcodeagent.self_repair.models import (
    new_incident, transition, budget_exceeded)


# ----------------------------------------------------------------------
# helpers

def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=30)


def make_repo(td: str) -> Path:
    """A small git repo with a deliberately buggy module + failing test."""
    root = Path(td) / "repo"
    (root / "mod").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "mod" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "mod" / "calc.py").write_text(textwrap.dedent("""\
        def divide(a, b):
            return a / b
        """), encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(textwrap.dedent("""\
        import unittest
        from mod.calc import divide

        class CalcTests(unittest.TestCase):
            def test_divide_by_zero_returns_none(self):
                self.assertIsNone(divide(4, 0))
        """), encoding="utf-8")
    git("init", cwd=root)
    git("-c", "user.email=t@t", "-c", "user.name=t",
        "add", "-A", cwd=root)
    git("-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-m", "init", cwd=root)
    return root


TRACE = ('Traceback (most recent call last):\n'
         '  File "{root}/mod/calc.py", line 2, in divide\n'
         '    return a / b\n'
         'ZeroDivisionError: division by zero\n')


def make_coord(td: str, repo: Path, **kw):
    root = Path(td)
    store = AutonomyStore(root / "autonomy")
    defaults = dict(
        repo_root=repo, state_root=root / "autonomy",
        auto_promote=True,
        collectors={},
        emit=lambda p: None,
        audit=lambda k, **f: None,
        notify=lambda l, t, d: None)
    defaults.update(kw)
    return SelfRepairCoordinator(store, **defaults)


def report_bug(coord, repo: Path):
    trace = TRACE.replace("{root}", str(repo).replace("\\", "/"))
    inc, disp = coord.report_failure(
        source="test", subsystem="code",
        exc_type="ZeroDivisionError",
        error_message="ZeroDivisionError: division by zero "
                      "in tests.test_calc",
        stack_trace=trace)
    assert disp == "new"
    return inc


GOOD_PATCH = textwrap.dedent("""\
    def divide(a, b):
        if b == 0:
            return None
        return a / b
    """)

BAD_PATCH = "def divide(a, b):\n    raise RuntimeError('still broken')\n"


def good_generator(incident, wt: Path):
    """Fault-injection stand-in for the repair mission: writes the fix
    and a regression test into the worktree."""
    (wt / "mod" / "calc.py").write_text(GOOD_PATCH, encoding="utf-8")
    (wt / "tests" / "test_calc_repair.py").write_text(textwrap.dedent("""\
        import unittest
        from mod.calc import divide

        class RepairTests(unittest.TestCase):
            def test_zero_returns_none(self):
                self.assertIsNone(divide(8, 0))
            def test_normal_division(self):
                self.assertEqual(divide(8, 2), 4)
        """), encoding="utf-8")
    return {"files": ["mod/calc.py", "tests/test_calc_repair.py"],
            "test": "tests/test_calc_repair.py"}


def bad_generator(incident, wt: Path):
    (wt / "mod" / "calc.py").write_text(BAD_PATCH, encoding="utf-8")
    (wt / "tests" / "test_calc_repair.py").write_text(textwrap.dedent("""\
        import unittest
        from mod.calc import divide

        class RepairTests(unittest.TestCase):
            def test_zero_returns_none(self):
                self.assertIsNone(divide(8, 0))
        """), encoding="utf-8")
    return {"files": ["mod/calc.py", "tests/test_calc_repair.py"],
            "test": "tests/test_calc_repair.py"}


# ----------------------------------------------------------------------
# unit: detector

class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.rows = []
        self.det = Detector(lambda: self.rows)

    def tearDown(self):
        self.td.cleanup()

    def test_signature_stable_across_volatile_bits(self):
        s1 = self.detector_sig("TransportError",
                               "WinError 10054 connection to 10.0.0.1:52190 "
                               "reset pid 4412", "llama.cpp")
        s2 = self.detector_sig("TransportError",
                               "WinError 10054 connection to 10.0.0.9:30001 "
                               "reset pid 9987", "llama.cpp")
        self.assertEqual(s1, s2)

    def detector_sig(self, cls, msg, sub):
        from localcodeagent.self_repair.detector import normalize_signature
        return normalize_signature(cls, msg, sub)

    def test_dedupe_recurrence_bumps_one_incident(self):
        kw = dict(source="t", error_message="sqlite database is locked",
                  exc_type="", subsystem="storage")
        i1, d1 = self.det.ingest(**kw)
        self.rows.append(i1)
        i2, d2 = self.det.ingest(**kw)
        self.assertEqual(d1, "new")
        self.assertEqual(d2, "recurred")
        self.assertIs(i1, i2)
        self.assertEqual(i2["occurrences"], 2)

    def test_first_time_noise_suppressed(self):
        inc, disp = self.det.ingest(
            source="t", error_message="connection timed out once",
            exc_type="TimeoutError", subsystem="network")
        self.assertIsNone(inc)
        self.assertEqual(disp, "suppressed")

    def test_critical_class_opens_incident(self):
        inc, disp = self.det.ingest(
            source="t", error_message="installer build failed",
            exc_type="", subsystem="installer")
        self.assertIsNotNone(inc)
        self.assertIn(inc["severity"], {"critical", "high"})

    def test_severity_escalates_with_recurrence(self):
        kw = dict(source="t", error_message="runtime blew up",
                  exc_type="RuntimeError", subsystem="runtime")
        inc, disp = self.det.ingest(**kw)
        self.assertEqual(disp, "new")
        self.rows.append(inc)
        for _ in range(4):
            self.det.ingest(**kw)
        self.assertEqual(inc["occurrences"], 5)
        self.assertIn(inc["severity"], {"high", "critical"})


# ----------------------------------------------------------------------
# unit: localizer / diagnosis / memory / transitions

class LocalizerTests(unittest.TestCase):
    def test_traceback_maps_to_repo_file(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            loc = Localizer(repo)
            inc = new_incident(source="t", subsystem="code",
                               error_class="ZeroDivisionError",
                               error_message="boom",
                               signature="code:ZeroDivisionError:x",
                               stack_trace=TRACE.replace(
                                   "{root}", str(repo).replace("\\", "/")))
            suspects = loc.localize(inc)
            self.assertTrue(suspects)
            self.assertEqual(suspects[0]["path"], "mod/calc.py")
            self.assertEqual(suspects[0]["function"], "divide")
            self.assertGreater(suspects[0]["confidence"], 0.5)

    def test_no_repo_frames_empty_suspects(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            loc = Localizer(repo)
            inc = new_incident(source="t", subsystem="code",
                               error_class="Error", error_message="x",
                               signature="s",
                               stack_trace='File "C:/other/x.py", '
                                           'line 1, in f')
            self.assertEqual(loc.localize(inc), [])


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.diag = Diagnostician()

    def _inc(self, **kw):
        base = dict(source="t", subsystem="code", error_class="Error",
                    error_message="", signature="s")
        return new_incident(**{**base, **kw})

    def test_oom_is_operational(self):
        out = self.diag.diagnose(self._inc(
            error_class="OOM", error_message="CUDA out of memory"))
        self.assertEqual(out["category"], "hardware_pressure")
        self.assertEqual(out["repair_kind"], "operational")

    def test_transport_reset_is_operational(self):
        out = self.diag.diagnose(self._inc(
            error_class="TransportError", subsystem="llama.cpp",
            error_message="WinError 10054"))
        self.assertEqual(out["repair_kind"], "operational")

    def test_source_bug_needs_code_path(self):
        inc = self._inc(error_class="TypeError",
                        error_message="NoneType has no attr")
        inc["suspects"] = [{"path": "mod/calc.py", "function": "divide",
                            "line": 2, "confidence": 0.85,
                            "recent_commits": 0}]
        out = self.diag.diagnose(inc)
        self.assertEqual(out["category"], "source_bug")
        self.assertEqual(out["repair_kind"], "code")

    def test_unknown_when_no_rule_matches(self):
        out = self.diag.diagnose(self._inc(error_class="Error",
                                           error_message="???"))
        self.assertEqual(out["category"], "unknown")
        self.assertLess(out["confidence"], 0.5)


class RepairMemoryTests(unittest.TestCase):
    def test_recall_prefers_success(self):
        with tempfile.TemporaryDirectory() as td:
            mem = RepairMemory(Path(td) / "mem.json")
            mem.record("sig:a", kind="op", steps=["x"], success=True,
                       confidence=0.9)
            mem.record("sig:a", kind="op2", steps=["y"], success=False,
                       confidence=0.5)
            best = mem.best_fix("sig:a")
            self.assertIsNotNone(best)
            self.assertEqual(best["kind"], "op")

    def test_known_bad_suppression(self):
        with tempfile.TemporaryDirectory() as td:
            mem = RepairMemory(Path(td) / "mem.json")
            for _ in range(2):
                mem.record("sig:b", kind="operational:stale_process",
                           steps=["s"], success=False, confidence=0.4)
            self.assertTrue(
                mem.known_bad("sig:b", "operational:stale_process"))
            self.assertFalse(
                mem.known_bad("sig:b", "code"))

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mem.json"
            RepairMemory(p).record("sig:c", kind="code", steps=["s"],
                                   success=True, confidence=0.9)
            fresh = RepairMemory(p)
            self.assertTrue(fresh.best_fix("sig:c"))


class ModelTests(unittest.TestCase):
    def test_illegal_transition_rejected(self):
        inc = new_incident(source="t", subsystem="s", error_class="E",
                           error_message="m", signature="sig")
        self.assertFalse(transition(inc, "promoting"))   # detected→promoting
        self.assertEqual(inc["state"], "detected")

    def test_budget_exceeded(self):
        inc = new_incident(source="t", subsystem="s", error_class="E",
                           error_message="m", signature="sig")
        inc["attempts"]["patch"] = 3
        self.assertEqual(budget_exceeded(inc), "max_patch_attempts")

    def test_redact_strips_secrets(self):
        text = redact("api_key=sk-abcdefgh12345678 Bearer tokXYZ "
                      "password=hunter2")
        self.assertNotIn("sk-abcdefgh", text)
        self.assertNotIn("hunter2", text)


# ----------------------------------------------------------------------
# fault-injection scenarios

class ScenarioA_CodeRepairPromoted(unittest.TestCase):
    """Deliberate null/zero bug → full pipeline → promoted + learned."""

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            coord = make_coord(td, repo,
                               patch_generator=good_generator,
                               canary=Canary(lambda i, w, **k:
                                             {"ok": True, "port": 1}))
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"])
            final = coord.get(inc["id"])

            self.assertEqual(final["state"], "resolved")
            # localized to the real file
            self.assertEqual(final["suspects"][0]["path"], "mod/calc.py")
            self.assertEqual(final["repair_kind"], "code")
            # a regression test was added by the repair
            self.assertTrue(
                (repo / "tests" / "test_calc_repair.py").is_file())
            # fix actually promoted into the stable tree
            self.assertIn("if b == 0",
                          (repo / "mod" / "calc.py").read_text())
            # targeted + review + canary evidence all recorded
            self.assertTrue(final["verification"]["targeted"][0]["ok"])
            self.assertTrue(final["review"]["ok"])
            self.assertEqual(final["verification"]["canary"]["ok"], True)
            # learned into procedural memory
            self.assertIsNotNone(coord.memory.best_fix(final["signature"]))
            # promotion snapshot exists for rollback
            self.assertTrue((Path(td) / "autonomy" / "lkg" / inc["id"]
                             / "manifest.json").is_file())
            # audited timeline shows every stage
            states = [h["event"] for h in final["history"]]
            for stage in ("collecting", "localizing", "diagnosing",
                          "planning", "patching", "testing", "reviewing",
                          "canary", "promoting", "resolved"):
                self.assertIn(stage, states)

    def test_promotion_commits_repair_files(self):
        """Promoted repairs land as a scoped, auditable commit — never a
        silently dirty tree, never sweeping unrelated changes."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            # an unrelated dirty file must NOT join the repair commit
            (repo / "unrelated.txt").write_text("dirty", encoding="utf-8")
            coord = make_coord(td, repo,
                               patch_generator=good_generator,
                               canary=Canary(lambda i, w, **k:
                                             {"ok": True, "port": 1}),
                               commit_on_promote=True)
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"])
            final = coord.get(inc["id"])
            self.assertEqual(final["state"], "resolved")
            sha = (final.get("promotion") or {}).get("commit")
            self.assertTrue(sha)
            log = git("log", "--format=%s", cwd=repo).stdout
            self.assertIn("Self-repair:", log)
            # scoped commit — unrelated dirty file stays uncommitted
            status = git("status", "--porcelain", cwd=repo).stdout
            self.assertIn("unrelated.txt", status)
            self.assertNotIn("calc.py", status)

    def test_commit_disabled_leaves_working_tree(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            coord = make_coord(td, repo,
                               patch_generator=good_generator,
                               canary=Canary(lambda i, w, **k:
                                             {"ok": True, "port": 1}),
                               commit_on_promote=False)
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"])
            final = coord.get(inc["id"])
            self.assertEqual(final["state"], "resolved")
            self.assertIsNone((final.get("promotion") or {}).get("commit"))
            # fix present but uncommitted
            self.assertIn("calc.py",
                          git("status", "--porcelain", cwd=repo).stdout)


class ScenarioB_BadPatchRejected(unittest.TestCase):
    """A patch that keeps failing tests must never promote."""

    def test_bad_patch_never_touches_stable(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            original = (repo / "mod" / "calc.py").read_text()
            coord = make_coord(td, repo, patch_generator=bad_generator)
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"], max_steps=60)
            final = coord.get(inc["id"])

            self.assertIn(final["state"], {"needs_human", "abandoned",
                                           "planning", "patching"})
            # stable tree is byte-identical
            self.assertEqual((repo / "mod" / "calc.py").read_text(),
                             original)
            # the failed attempt was remembered
            procs = [p for p in coord.memory.data["procedures"]
                     if not p["success"]]
            self.assertTrue(procs)


class ScenarioC_Rollback(unittest.TestCase):
    """A promoted repair can be rolled back to last-known-good."""

    def test_rollback_restores_stable(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            original = (repo / "mod" / "calc.py").read_text()
            coord = make_coord(td, repo, patch_generator=good_generator)
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"])
            self.assertIn("if b == 0",
                          (repo / "mod" / "calc.py").read_text())
            result = coord.rollback_incident(inc["id"])
            self.assertTrue(result["ok"], result)
            self.assertEqual((repo / "mod" / "calc.py").read_text(),
                             original)
            # the new test file the repair added is removed too
            self.assertFalse(
                (repo / "tests" / "test_calc_repair.py").exists())
            self.assertEqual(coord.get(inc["id"])["state"], "rolled_back")


class ScenarioD_OperationalRepair(unittest.TestCase):
    """Recurring runtime failure resolved procedurally — no worktree."""

    def test_port_collision_uses_fixer(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            calls = []
            coord = make_coord(
                td, repo,
                fixers={"stale_process": lambda ctx: (
                    calls.append("stale_process"),
                    {"ok": True, "detail": "port reclaimed"})[1]})
            inc, disp = coord.report_failure(
                source="runtime", subsystem="network",
                exc_type="", error_message="WinError 10048: bind failed — "
                                           "address already in use on port 52190")
            coord.process_incident(inc["id"])
            final = coord.get(inc["id"])
            self.assertEqual(final["state"], "resolved")
            self.assertEqual(final["repair_kind"], "operational")
            self.assertEqual(calls, ["stale_process"])
            self.assertFalse(final["worktree"])     # no code path used
            # learned so next time the procedure is recalled first
            self.assertIsNotNone(coord.memory.best_fix(final["signature"]))


class ScenarioE_RestartResumes(unittest.TestCase):
    """Crash mid-repair → new coordinator on the same store resumes."""

    def test_incident_survives_restart(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            coord = make_coord(td, repo, patch_generator=good_generator)
            inc = report_bug(coord, repo)
            # advance only through collection+localization
            for _ in range(3):
                coord.tick()
            mid = coord.get(inc["id"])
            self.assertIn(mid["state"], {"localizing", "diagnosing",
                                         "planning", "patching"})
            # "restart" — a fresh coordinator over the same store
            coord2 = make_coord(td, repo, patch_generator=good_generator)
            resumed = coord2.get(inc["id"])
            self.assertIsNotNone(resumed)
            self.assertEqual(resumed["state"], mid["state"])
            self.assertEqual(resumed["evaluations"] if "evaluations" in
                             resumed else resumed["attempts"]["diagnosis"],
                             mid["attempts"]["diagnosis"])
            coord2.process_incident(inc["id"])
            self.assertEqual(coord2.get(inc["id"])["state"], "resolved")


# ----------------------------------------------------------------------
# coordinator policy

class CoordinatorPolicyTests(unittest.TestCase):
    def test_auto_promote_off_leaves_verified_candidate_for_human(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            coord = make_coord(td, repo, auto_promote=False,
                               patch_generator=good_generator)
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"], max_steps=60)
            final = coord.get(inc["id"])
            self.assertEqual(final["state"], "needs_human")
            self.assertIn("auto-promote", final["needs_human_reason"])
            # stable tree untouched — patch sits in the worktree only
            self.assertNotIn("if b == 0",
                             (repo / "mod" / "calc.py").read_text())

    def test_no_generator_escalates_with_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            coord = make_coord(td, repo)  # no patch_generator
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"])
            final = coord.get(inc["id"])
            self.assertEqual(final["state"], "needs_human")
            self.assertTrue(final["suspects"])
            self.assertTrue(final["hypotheses"])

    def test_async_mission_patch_path(self):
        """patch_generator may delegate to a mission — incident parks in
        'patching' until the mission goes terminal, then continues."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            status = {"s": "executing"}

            def gen(incident, wt):
                return {"mission_id": "m-x"}

            coord = make_coord(
                td, repo, patch_generator=gen,
                mission_status=lambda mid: status["s"])
            inc = report_bug(coord, repo)
            for _ in range(6):   # through planning → patching → wait
                coord.tick()
            self.assertEqual(coord.get(inc["id"])["state"], "patching")
            # mission completes and wrote the patch
            status["s"] = "completed"
            (repo / ".repair-worktrees" / inc["id"] / "mod" / "calc.py")\
                .write_text(GOOD_PATCH, encoding="utf-8")
            coord2 = coord
            coord2.process_incident(inc["id"])
            final = coord2.get(inc["id"])
            self.assertEqual(final["state"], "resolved")

    def test_budget_caps_stop_runaway(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)

            def flaky_gen(incident, wt):
                raise RuntimeError("generator explodes")

            coord = make_coord(td, repo, patch_generator=flaky_gen)
            inc = report_bug(coord, repo)
            coord.process_incident(inc["id"], max_steps=80)
            final = coord.get(inc["id"])
            self.assertIn(final["state"], {"needs_human", "abandoned"})

    def test_secrets_redacted_at_intake(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(td)
            coord = make_coord(td, repo)
            inc, _ = coord.report_failure(
                source="t", subsystem="code", exc_type="Error",
                error_message="call failed: api_key=sk-livekey123456")
            self.assertNotIn("sk-livekey123456",
                             json.dumps(inc))


if __name__ == "__main__":
    unittest.main()
