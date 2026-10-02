"""Nexus Autonomy — missions, DAG, triggers, scheduler, recovery, policy."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.autonomy import (
    AutonomousSupervisor, TaskGraph, ResourceLocks,
    FailureClass, classify_failure, EvalVerdict,
)
from localcodeagent.autonomy.missions import new_mission
from localcodeagent.autonomy.recovery import PLAYBOOKS, failure_signature
from localcodeagent.autonomy.task_graph import new_task


def make_sup(td: str, **kw):
    root = Path(td)
    defaults = dict(
        workspace=root,
        store_root=root / "data" / "autonomy",
        executor=lambda m, n, cb: {"ok": True, "output": "done"},
        lane_free=lambda: True,
    )
    defaults.update(kw)
    return AutonomousSupervisor(**defaults)


def drive(sup: AutonomousSupervisor, mission_id: str, ticks: int = 30,
          settle: float = 0.12) -> dict:
    """Tick until the mission reaches a terminal/blocked/waiting state."""
    m = sup.missions.get(mission_id)
    for _ in range(ticks):
        sup.tick()
        time.sleep(settle)
        m = sup.missions.get(mission_id)
        if m["status"] in {"completed", "completed_with_warnings", "failed",
                           "cancelled", "blocked", "waiting_approval", "paused"}:
            break
    return m


class SandboxedVerifyTests(unittest.TestCase):
    def test_generated_verify_command_runs_in_sandbox(self):
        # A node-supplied command is generated code — must run sandboxed
        # (job limits, clean env) with the repo as cwd, not raw shell=True.
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            marker = Path(td) / "verify_ran.txt"
            script = Path(td) / "mk.py"
            script.write_text(
                f"import pathlib;pathlib.Path(r'{marker}').write_text('ok')")
            import sys
            node = {"instruction": "run checks",
                    "metadata": {"command": f"{sys.executable} {script}"}}
            out = sup._default_verify({"autonomy_profile": "local_autonomous",
                                       "workspace": td}, node)
            self.assertTrue(out["ok"], out["output"])
            self.assertIn("[sandboxed]", out["output"])
            self.assertTrue(marker.exists())
            sup.stop()

    def test_detected_verify_command_not_sandboxed(self):
        # Repo-detected checks (e.g. pytest found in the project) are trusted —
        # they run on the host so fixtures/venv resolve normally.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pytest.ini").write_text("[pytest]\n")
            sup = make_sup(td)
            import localcodeagent.workflow.verify as vermod
            orig = vermod.detect_verification_commands
            vermod.detect_verification_commands = lambda w: [
                {"command": f'{__import__("sys").executable} -c "print(1)"',
                 "name": "pytest"}]
            try:
                out = sup._default_verify({"autonomy_profile": "local_autonomous",
                                           "workspace": td},
                                          {"instruction": "x", "metadata": {}})
            finally:
                vermod.detect_verification_commands = orig
            self.assertIn("pytest", out["output"])
            self.assertNotIn("[sandboxed]", out["output"])
            sup.stop()


class JobNodeTests(unittest.TestCase):
    def test_job_kind_is_accepted_and_carries_no_default_lock(self):
        n = new_task("Do job", "do it", kind="job",
                     metadata={"job": "backup"})
        self.assertEqual(n["kind"], "job")
        self.assertEqual(n["lock"], "")
        # Unknown kinds still degrade to agent.
        self.assertEqual(new_task("x", "x", kind="bogus")["kind"], "agent")

    def test_job_node_runs_job_runner_and_completes(self):
        with tempfile.TemporaryDirectory() as td:
            seen = []
            sup = make_sup(
                td,
                job_runner=lambda m, n: (seen.append(
                    (n.get("metadata") or {}).get("job")) or
                    {"ok": True, "output": "job done"}))
            m = sup.create_mission(
                objective="run a job",
                success_criteria=[{"kind": "all_tasks_completed"}])
            sup.missions.mutate(m["id"], lambda r: TaskGraph(r).add(
                new_task("Index repo", "refresh the index", kind="job",
                         metadata={"job": "rag_update"})))
            sup.start_mission(m["id"])
            m = drive(sup, m["id"])
            node = (m.get("graph") or {}).get("nodes", [])[0]
            self.assertEqual(seen, ["rag_update"])
            self.assertEqual(node["state"], "completed")
            self.assertTrue(node["result"]["ok"])
            sup.stop()

    def test_unknown_node_kind_fails_loudly(self):
        # A hand-corrupted graph must not report a phantom success.
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(
                objective="corrupted graph",
                success_criteria=[{"kind": "all_tasks_completed"}])
            def _inject(row):
                g = TaskGraph(row)
                g.add(new_task("Weird", "x", kind="internal"))
                g.nodes[-1]["kind"] = "bogus_kind"  # bypass new_task normalizer
            sup.missions.mutate(m["id"], _inject)
            sup.start_mission(m["id"])
            m = drive(sup, m["id"])
            node = (m.get("graph") or {}).get("nodes", [])[0]
            self.assertNotEqual(node["state"], "completed")
            self.assertIn("unknown node kind",
                          str((node.get("result") or {}).get("output") or ""))
            sup.stop()

    def test_research_node_dispatches_research_runner(self):
        with tempfile.TemporaryDirectory() as td:
            seen = []
            sup = make_sup(
                td,
                research_runner=lambda m, n: (seen.append(
                    n.get("instruction")) or {"ok": True,
                                              "output": "findings"}))
            m = sup.create_mission(
                objective="survey libs",
                success_criteria=[{"kind": "all_tasks_completed"}])
            sup.missions.mutate(m["id"], lambda r: TaskGraph(r).add(
                new_task("Survey", "python logging frameworks",
                         kind="research")))
            sup.start_mission(m["id"])
            m = drive(sup, m["id"])
            node = (m.get("graph") or {}).get("nodes", [])[0]
            self.assertEqual(seen, ["python logging frameworks"])
            self.assertEqual(node["state"], "completed")
            sup.stop()

    def test_wait_node_sleeps_and_aborts_on_mission_state(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            # Bound: a 5s wait completes; an aborting mission fails early.
            m = sup.create_mission(objective="wait test", title="wait")
            sup.start_mission(m["id"])
            sup.missions.mutate(
                m["id"], lambda r: r.update({"status": "executing"}))
            sup._running = True  # unit-test the wait body without tick loop
            t0 = time.time()
            out = sup._default_wait(m, {"metadata": {"seconds": 0.2}})
            self.assertTrue(out["ok"])
            self.assertGreaterEqual(time.time() - t0, 0.19)
            sup.missions.transition(m["id"], "paused")
            out = sup._default_wait(m, {"metadata": {"seconds": 30}})
            self.assertFalse(out["ok"])
            self.assertIn("paused", out["output"])
            sup.stop()

    def test_conservative_mode_defers_gpu_job_when_lane_busy(self):
        with tempfile.TemporaryDirectory() as td:
            seen = []
            sup = make_sup(
                td,
                lane_free=lambda: False,  # interactive lane is busy
                job_runner=lambda m, n: (seen.append(n["id"]) or
                                         {"ok": True, "output": "x"}))
            sup.policy.set_resource_mode("conservative")
            m = sup.create_mission(
                objective="render", success_criteria=[
                    {"kind": "all_tasks_completed"}])
            sup.missions.mutate(m["id"], lambda r: TaskGraph(r).add(
                new_task("Render", "make image", kind="job",
                         metadata={"job": "image"})))
            sup.start_mission(m["id"])
            sup.tick()
            node = (sup.missions.get(m["id"]).get("graph") or {})["nodes"][0]
            self.assertEqual(seen, [])
            self.assertEqual(node["state"], "ready")  # deferred, not claimed
            sup.policy.set_resource_mode("performance")
            seen.clear()
            # Balanced/performance no longer yield to the busy lane.
            tb = Path(td) / "b"
            tb.mkdir()
            sup2 = make_sup(str(tb), lane_free=lambda: True,
                            job_runner=lambda m, n: (seen.append(n["id"]) or
                                                     {"ok": True, "output": "x"}))
            m2 = sup2.create_mission(
                objective="render", success_criteria=[
                    {"kind": "all_tasks_completed"}])
            sup2.missions.mutate(m2["id"], lambda r: TaskGraph(r).add(
                new_task("Render", "make image", kind="job",
                         metadata={"job": "image"})))
            sup2.start_mission(m2["id"])
            sup2.tick()
            time.sleep(0.2)
            self.assertEqual(len(seen), 1)
            sup.stop()
            sup2.stop()

    def test_default_job_runner_reports_unwired(self):
        sup = make_sup(tempfile.mkdtemp())
        out = sup._default_job({}, {"metadata": {"job": "sandbox"}})
        self.assertFalse(out["ok"])
        self.assertIn("sandbox", out["output"])

    def test_default_job_runner_indexes_repo_standalone(self):
        # rag_update works without a wired runner — the supervisor builds a
        # transient RepoIndex over the workspace.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "m.py").write_text("class C:\n    pass\n")
            sup = make_sup(td)
            out = sup._default_job({}, {"metadata": {"job": "rag_update"}})
            self.assertTrue(out["ok"], out["output"])
            self.assertIn("+1 added", out["output"])
            sup.stop()

    def test_repository_plan_includes_index_job_node(self):
        from localcodeagent.autonomy.planner import MissionPlanner
        plan = MissionPlanner().initial_plan(
            {"objective": "refactor", "scope": "repository"})
        jobs = [t for t in plan if t["kind"] == "job"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["metadata"]["job"], "rag_update")
        inspect = next(t for t in plan if t["title"] == "Inspect current state")
        self.assertEqual(inspect["deps"], [jobs[0]["id"]])
        # Non-repo scopes stay index-free.
        plan2 = MissionPlanner().initial_plan(
            {"objective": "x", "scope": "one_shot"})
        self.assertFalse([t for t in plan2 if t["kind"] == "job"])

    def test_mission_job_sandbox_runs_in_workspace(self):
        import sys
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import AppState, stop_state
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(models=[ModelProfile(
                id="ext", endpoint="http://x/v1", model="m",
                roles=["primary_coder"], runtime="external")])
            state = AppState(cfg, Path(td), Path(td) / ".runtime")
            try:
                marker = Path(td) / "job_marker.txt"
                node = {"metadata": {"job": "sandbox",
                                     "argv": [sys.executable, "-c",
                                              "import pathlib;"
                                              f"pathlib.Path(r'{marker}')"
                                              ".write_text('ok')"]}}
                out = state._mission_job({"id": "m1"}, node)
                self.assertTrue(out["ok"], out.get("output"))
                self.assertTrue(marker.exists())
                self.assertIn("sandbox", out)
                # The op is recorded in the unified job ledger.
                ledger = state.jobs.list_jobs()
                self.assertEqual(len(ledger), 1)
                self.assertEqual(ledger[0]["kind"], "mission_job")
                self.assertEqual(ledger[0]["state"], "completed")
                self.assertEqual(ledger[0]["metadata"]["op"], "sandbox")
                self.assertEqual(ledger[0]["metadata"]["mission_id"], "m1")
            finally:
                stop_state(state)

    def test_mission_job_worktree_isolated_merge(self):
        import subprocess, sys
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.multiagent import is_repo
        from localcodeagent.server import AppState, stop_state
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo,
                           capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                            "-c", "user.email=t@t", "commit",
                            "--allow-empty", "-m", "init"],
                           capture_output=True)
            if not is_repo(repo):
                self.skipTest("git unavailable")
            cfg = AgentConfig(models=[ModelProfile(
                id="ext", endpoint="http://x/v1", model="m",
                roles=["primary_coder"], runtime="external")])
            state = AppState(cfg, repo, repo / ".runtime")
            try:
                node = {"metadata": {"job": "worktree",
                                     "argv": [sys.executable, "-c",
                                              "import pathlib;"
                                              "pathlib.Path('wt_file.txt')"
                                              ".write_text('made')"]}}
                out = state._mission_job({"id": "m4"}, node)
                self.assertTrue(out["ok"], out.get("output"))
                self.assertIn("merged", out["output"])
                # The change landed on the base branch — worktree is gone.
                self.assertTrue((repo / "wt_file.txt").is_file())
            finally:
                stop_state(state)

    def test_mission_job_worktree_failed_run_does_not_touch_base(self):
        import subprocess
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.multiagent import is_repo
        from localcodeagent.server import AppState, stop_state
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo,
                           capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                            "-c", "user.email=t@t", "commit",
                            "--allow-empty", "-m", "init"],
                           capture_output=True)
            if not is_repo(repo):
                self.skipTest("git unavailable")
            cfg = AgentConfig(models=[ModelProfile(
                id="ext", endpoint="http://x/v1", model="m",
                roles=["primary_coder"], runtime="external")])
            state = AppState(cfg, repo, repo / ".runtime")
            try:
                node = {"metadata": {
                    "job": "worktree",
                    "argv": ["cmd.exe" if os.name == "nt" else "sh",
                             "/c" if os.name == "nt" else "-c",
                             "echo dirty > wt_dirty.txt & exit 3"
                             if os.name == "nt"
                             else "echo dirty > wt_dirty.txt; exit 3"]}}
                out = state._mission_job({"id": "m5"}, node)
                self.assertFalse(out["ok"])
                # Failed run — base never sees the dirty file.
                self.assertFalse((repo / "wt_dirty.txt").exists())
            finally:
                stop_state(state)

    def test_register_output_artifact_records_kg_provenance(self):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import AppState, stop_state
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(models=[ModelProfile(
                id="ext", endpoint="http://x/v1", model="m",
                roles=["primary_coder"], runtime="external")])
            state = AppState(cfg, Path(td), Path(td) / ".runtime")
            try:
                out = Path(td) / "render.png"
                out.write_bytes(b"png")
                rec = state._register_output_artifact(
                    out, mission_id="m9", task_id="n1", tool="comfyui")
                self.assertTrue(rec["id"].startswith("art-"))
                self.assertEqual(rec["kind"], "image")
                self.assertEqual(rec["mission_id"], "m9")
                ctx = state.knowledge.context_for("render.png")
                self.assertIn("produced", ctx)
                self.assertIn("m9", ctx)
            finally:
                stop_state(state)

    def test_mission_job_image_registers_outputs(self):
        from types import SimpleNamespace
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import AppState, stop_state
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(models=[ModelProfile(
                id="ext", endpoint="http://x/v1", model="m",
                roles=["primary_coder"], runtime="external")])
            state = AppState(cfg, Path(td), Path(td) / ".runtime")
            try:
                out = Path(td) / "gen.png"
                out.write_bytes(b"png")
                job = SimpleNamespace(id="ij1", state="finished",
                                      stage="done", outputs=[str(out)])
                state.images.create_job = lambda req: job
                state.images.get_job = lambda jid: job
                node = {"id": "n1",
                        "metadata": {"job": "image", "prompt": "a cat"}}
                res = state._mission_job({"id": "m2"}, node)
                self.assertTrue(res["ok"], res.get("output"))
                arts = state.artifacts.list(mission_id="m2")
                self.assertEqual(len(arts), 1)
                self.assertEqual(arts[0]["name"], "gen.png")
                self.assertEqual(res["artifacts"], [arts[0]["id"]])
            finally:
                stop_state(state)


    def test_mission_job_model_install_reports_result(self):
        from types import SimpleNamespace
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import AppState, stop_state
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(models=[ModelProfile(
                id="ext", endpoint="http://x/v1", model="m",
                roles=["primary_coder"], runtime="external")])
            state = AppState(cfg, Path(td), Path(td) / ".runtime")
            try:
                profile = SimpleNamespace(id="sdxl-test")
                job = {"id": "inst1", "model_id": "sdxl-test",
                       "state": "finished", "progress": 1.0,
                       "error": "", "results": [{"status": "downloaded"}]}
                state.images.router.get_profile = lambda mid: (
                    profile if mid == "sdxl-test" else None)
                state.images.library.start_install = lambda p, repair=False: dict(job)
                state.images.library.install_jobs = lambda: [dict(job)]
                node = {"id": "n9",
                        "metadata": {"job": "model_install",
                                     "model": "sdxl-test"}}
                res = state._mission_job({"id": "m3"}, node)
                self.assertTrue(res["ok"], res.get("output"))
                self.assertEqual(res["job"]["model_id"], "sdxl-test")
                bad = state._mission_job(
                    {"id": "m3"},
                    {"id": "n10",
                     "metadata": {"job": "model_install", "model": "nope"}})
                self.assertFalse(bad["ok"])
                self.assertIn("unknown model", bad["output"])
            finally:
                stop_state(state)


    def test_mission_research_records_sources_in_kg(self):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import AppState, stop_state
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(models=[ModelProfile(
                id="ext", endpoint="http://x/v1", model="m",
                roles=["primary_coder"], runtime="external")])
            state = AppState(cfg, Path(td), Path(td) / ".runtime")
            try:
                state.research = type("R", (), {
                    "research_topic": lambda self, q: {
                        "status": "completed",
                        "summary": "summary",
                        "sources": [{"title": "Docs", "url": "https://x",
                                     "reliability": "high"}],
                        "errors": []}})()
                res = state._mission_research(
                    {"id": "m7"},
                    {"id": "r1", "instruction": "qwen3 coder context"})
                self.assertTrue(res["ok"])
                self.assertEqual(res["research"]["sources"], 1)
                ctx = state.knowledge.context_for("qwen3 coder context")
                self.assertIn("cites", ctx)
                self.assertIn("informed_by", ctx)
            finally:
                stop_state(state)


class MissionStoreTests(unittest.TestCase):
    def test_create_and_get(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="build a widget", title="widget")
            got = sup.missions.get(m["id"])
            self.assertEqual(got["objective"], "build a widget")
            self.assertEqual(got["status"], "draft")
            sup.stop()

    def test_persistence_across_instances(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="persist me",
                                   success_criteria=[{"kind": "verify_passed"}])
            sup.start_mission(m["id"])
            sup.stop()
            sup2 = make_sup(td)
            got = sup2.missions.get(m["id"])
            self.assertIsNotNone(got)
            self.assertEqual(got["success_criteria"][0]["kind"], "verify_passed")
            sup2.stop()

    def test_restart_recovers_inflight_mission(self):
        """A mission mid-execution when the process dies must recover —
        running nodes flip back to ready, status returns to a drivable
        state, and completed work is not repeated."""
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="crash test")
            sup.start_mission(m["id"])
            sup.tick()
            time.sleep(0.3)
            sup.tick()
            m = sup.missions.get(m["id"])
            # Simulate crash: leave a node 'running' with a live lease.
            graph = TaskGraph(m)
            node = graph.nodes[0]
            node["state"] = "running"
            node["lease"] = {"owner": "dead", "expires": time.time() + 300}
            sup.missions.update(m["id"], graph=graph.graph, status="executing")
            sup._running = False  # don't touch disk state on shutdown

            sup2 = make_sup(td)
            m2 = sup2.missions.get(m["id"])
            self.assertIn(m2["status"], {"active", "executing", "ready"})
            node2 = TaskGraph(m2).get(node["id"])
            self.assertNotEqual(node2["state"], "running")
            sup2.stop()

    def test_illegal_transition_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x")
            self.assertIsNone(sup.missions.transition(m["id"], "completed"))
            self.assertEqual(sup.missions.get(m["id"])["status"], "draft")
            sup.stop()

    def test_corrupt_store_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.create_mission(objective="x")
            sup.stop()
            path = Path(td) / "data" / "autonomy" / "missions.json"
            path.write_text("{corrupt!!!", encoding="utf-8")
            sup2 = make_sup(td)   # must not crash
            self.assertEqual(sup2.missions.list(), [])
            self.assertTrue(list(Path(td, "data", "autonomy").glob("*.corrupt-*")))
            sup2.stop()

    def test_mission_edit_bumps_revision(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="v1")
            rev = m["revision"]
            sup.missions.mutate(m["id"], lambda r: r.update(
                {"objective": "v2", "revision": r["revision"] + 1}))
            self.assertEqual(sup.missions.get(m["id"])["revision"], rev + 1)
            sup.stop()


class TaskGraphTests(unittest.TestCase):
    def _graph(self):
        m = new_mission("test")
        return TaskGraph(m)

    def test_dependency_ordering(self):
        g = self._graph()
        a = g.add(new_task("A", "a"))
        b = g.add(new_task("B", "b", deps=[a["id"]]))
        self.assertEqual([n["id"] for n in g.runnable()], [a["id"]])
        g.mark(a["id"], "completed")
        self.assertEqual([n["id"] for n in g.runnable()], [b["id"]])

    def test_parallel_independent_branches(self):
        g = self._graph()
        a = g.add(new_task("A", "a"))
        b = g.add(new_task("B", "b", kind="research"))
        c = g.add(new_task("C", "c", deps=[a["id"], b["id"]]))
        ready = {n["id"] for n in g.runnable()}
        self.assertEqual(ready, {a["id"], b["id"]})

    def test_failed_dependency_blocks_dependent(self):
        g = self._graph()
        a = g.add(new_task("A", "a"))
        b = g.add(new_task("B", "b", deps=[a["id"]]))
        g.mark(a["id"], "failed")
        g.refresh()
        self.assertEqual(b["state"], "blocked")

    def test_cycle_rejected(self):
        g = self._graph()
        a = g.add(new_task("A", "a"))
        g.add(new_task("B", "b", deps=[a["id"]]))
        with self.assertRaises(ValueError):
            g._assert_acyclic("self", {"self"})

    def test_lease_claim_and_reclaim(self):
        g = self._graph()
        n = g.add(new_task("A", "a"))
        n["state"] = "ready"
        self.assertTrue(g.claim(n["id"], "w1"))
        self.assertFalse(g.claim(n["id"], "w2"))   # exclusive
        # Expire the lease → reclaimable
        n["lease"]["expires"] = time.time() - 1
        reclaimed = g.reclaim_expired()
        self.assertEqual([r["id"] for r in reclaimed], [n["id"]])
        self.assertEqual(n["state"], "ready")
        self.assertEqual(n["retries"], 1)

    def test_resource_locks_exclusive(self):
        locks = ResourceLocks()
        self.assertTrue(locks.acquire("workspace_write", "t1"))
        self.assertFalse(locks.acquire("workspace_write", "t2"))
        locks.release("workspace_write", "t1")
        self.assertTrue(locks.acquire("workspace_write", "t2"))
        # wrong owner cannot release
        locks.release("workspace_write", "t1")
        self.assertEqual(locks.held_by("workspace_write"), "t2")


class RecoveryTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(classify_failure("CUDA out of memory"), FailureClass.CUDA_OOM)
        self.assertEqual(classify_failure("WinError 10054 connection reset"),
                         FailureClass.NETWORK_FAILURE)
        self.assertEqual(classify_failure("APPROVAL_REQUIRED: packages.install"),
                         FailureClass.PERMISSION_REQUIRED)
        self.assertEqual(classify_failure("AssertionError: 1 != 2 test failed"),
                         FailureClass.TEST_FAILURE)
        self.assertEqual(classify_failure("weirdness"), FailureClass.UNKNOWN)

    def test_playbook_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x")
            rec = sup.recovery.record_failure(m, "task", "CUDA out of memory")
            steps = []
            while True:
                s = sup.recovery.next_step(m, rec)
                if s is None:
                    break
                steps.append(s["action"])
            self.assertEqual(steps[-1], "escalate")
            self.assertLessEqual(len(steps), len(PLAYBOOKS[FailureClass.CUDA_OOM]))
            sup.stop()

    def test_same_failure_loop_detection(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x",
                                   budgets={"max_same_failure_retries": 3})
            for _ in range(3):
                sup.recovery.record_failure(m, "task", "same error output")
            reason = sup.recovery.budgets_exceeded(m)
            self.assertIn("same failure", reason)
            sup.stop()

    def test_signature_normalization(self):
        a = failure_signature("task", "error at offset 123456")
        b = failure_signature("task", "error at offset 999999")
        self.assertEqual(a, b)


class PolicyTests(unittest.TestCase):
    def test_profiles_gate_actions(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            # Supervised: shell work asks; reading is allowed.
            self.assertEqual(sup.policy.check("read_files", profile="supervised"), "allow")
            self.assertIn(sup.policy.check("git_push", profile="local_autonomous"),
                          {"ask", "deny"})
            # Extended autonomous allows remote actions (still subject to grant/perms)
            self.assertIn(sup.policy.check("git_push", profile="extended_autonomous"),
                          {"ask"})  # sensitive → asks without standing grant
            sup.stop()

    def test_standing_grant_allows_sensitive(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.policy.grant("git_push", scope="afterburn25/Coding_Agent")
            self.assertEqual(
                sup.policy.check("git_push", profile="local_autonomous",
                                 scope="afterburn25/Coding_Agent"),
                "allow")
            # Different scope does not inherit the grant.
            self.assertEqual(
                sup.policy.check("git_push", profile="local_autonomous",
                                 scope="other/repo"),
                "ask")
            sup.stop()

    def test_grant_expiry_and_revoke(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            g = sup.policy.grant("git_push", scope="repo/x", expires_in_s=-1)
            self.assertIsNone(sup.policy.matching_grant("git_push", scope="repo/x"))
            g2 = sup.policy.grant("git_push", scope="repo/x")
            self.assertIsNotNone(sup.policy.matching_grant("git_push", scope="repo/x"))
            sup.policy.revoke(g2["id"])
            self.assertIsNone(sup.policy.matching_grant("git_push", scope="repo/x"))
            sup.stop()

    def test_stop_autonomy_denies(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.policy.set_stopped(True)
            self.assertEqual(sup.policy.check("run_tests"), "deny")
            sup.stop()


class NotificationTests(unittest.TestCase):
    def test_policy_filtering(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            self.assertIsNone(sup.notifications.notify("spam", level="info",
                                                       policy="important"))
            self.assertIsNotNone(sup.notifications.notify("broke", level="failure",
                                                          policy="important"))
            self.assertIsNone(sup.notifications.notify("quiet", level="completion",
                                                       policy="silent"))
            sup.stop()

    def test_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.notifications.notify("same thing", level="failure")
            self.assertIsNone(sup.notifications.notify("same thing", level="failure"))
            sup.stop()

    def test_quiet_hours_mutes(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.notifications.quiet_hours = (0, 23)  # always quiet
            out = sup.notifications.notify("info ping", level="important")
            self.assertEqual(out["level"], "muted")
            # Critical severities still pass.
            out2 = sup.notifications.notify("broke", level="failure")
            self.assertEqual(out2["level"], "failure")
            sup.stop()

    def test_notify_mirrors_activity_row(self):
        from localcodeagent.workflow.activity import ActivityStore
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td, activities=ActivityStore(Path(td) / "data"))
            sup.notifications.notify("mission done", level="important",
                                     mission_id="m-1", title="Nexus Autonomy")
            rows = sup.activities.for_task("mission:m-1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["category"], "notification")
            self.assertEqual(rows[0]["state"], "completed")
            self.assertEqual(rows[0]["mission_id"], "m-1")
            self.assertIn("mission done", rows[0]["summary"])
            sup.notifications.notify("boom", level="failure")
            failed = sup.activities.for_task("mission:global")
            self.assertEqual(failed[-1]["state"], "failed")
            sup.stop()


class SchedulerTests(unittest.TestCase):
    def test_once_fires_and_disables(self):
        with tempfile.TemporaryDirectory() as td:
            fired = []
            sup = make_sup(td)
            sup.scheduler.on_fire = fired.append
            sup.scheduler.add("once test", "once", at=time.time() - 1)
            sup.scheduler.tick()
            self.assertEqual(len(fired), 1)
            row = sup.scheduler.list()[0]
            self.assertFalse(row["enabled"])  # once schedules auto-disable
            sup.stop()

    def test_interval_rearms(self):
        with tempfile.TemporaryDirectory() as td:
            fired = []
            sup = make_sup(td)
            sup.scheduler.on_fire = fired.append
            sup.scheduler.add("beat", "interval", interval_s=60)
            sup.scheduler.tick()  # not due yet
            self.assertEqual(fired, [])
            # Force due.
            sup.scheduler._store.schedules.data["schedules"][0]["next_run"] = time.time() - 1
            sup.scheduler.tick()
            self.assertEqual(len(fired), 1)
            self.assertTrue(sup.scheduler.list()[0]["next_run"] > time.time())
            sup.stop()

    def test_daily_wallclock(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            row = sup.scheduler.add("nightly", "daily", hour=3, minute=0)
            lt = time.localtime(row["next_run"])
            self.assertEqual((lt.tm_hour, lt.tm_min), (3, 0))
            self.assertGreater(row["next_run"], time.time())
            sup.stop()

    def test_schedule_persists_restart(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            s = sup.scheduler.add("weekly", "weekly", weekday=6, hour=3)
            sup.stop()
            sup2 = make_sup(td)
            rows = sup2.scheduler.list()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "weekly")
            sup2.stop()


class TriggerTests(unittest.TestCase):
    def test_condition_matching(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            fired = []
            sup.triggers.on_fire = lambda t, p: fired.append(t)
            sup.triggers.add("ci watch", "ci_failed",
                             conditions={"repo": "a/b", "branch": "main"})
            sup.triggers.fire("ci_failed", {"repo": "a/b", "branch": "dev"})
            self.assertEqual(fired, [])
            sup.triggers.fire("ci_failed", {"repo": "a/b", "branch": "main"})
            self.assertEqual(len(fired), 1)
            sup.stop()

    def test_debounce(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            fired = []
            sup.triggers.on_fire = lambda t, p: fired.append(t)
            sup.triggers.add("deb", "custom", debounce_s=60)
            sup.triggers.fire("custom", {})
            sup.triggers.fire("custom", {})
            self.assertEqual(len(fired), 1)
            sup.stop()

    def test_file_changed_scoped_and_debounced(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            fired = []
            sup.triggers.on_fire = lambda t, p: fired.append(t)
            watch = Path(td) / "watched.txt"
            watch.write_text("v1")
            sup.triggers.add("watch", "file_changed", watch="watched.txt",
                             debounce_s=0)
            sup.triggers.check_watches()   # baseline — no fire
            self.assertEqual(fired, [])
            time.sleep(0.02)
            watch.write_text("v2")
            sup.triggers.check_watches()
            self.assertEqual(len(fired), 1)
            # Outside-workspace watch is refused.
            sup.triggers.add("bad", "file_changed", watch="C:/Windows",
                             debounce_s=0)
            sup.triggers.check_watches()
            self.assertEqual(len(fired), 1)
            sup.stop()

    def test_trigger_fire_materializes_mission(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.triggers.add("auto", "startup",
                             action={"kind": "mission",
                                     "objective": "boot check"})
            sup.triggers.fire("startup", {})
            missions = sup.missions.list()
            self.assertEqual(len(missions), 1)
            self.assertEqual(missions[0]["objective"], "boot check")
            sup.stop()

    def test_trigger_suppressed_when_stopped(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.policy.set_stopped(True)
            sup.triggers.add("auto", "custom",
                             action={"kind": "mission", "objective": "x"})
            sup.triggers.fire("custom", {})
            self.assertEqual(sup.missions.list(), [])
            sup.stop()


class EvaluatorTests(unittest.TestCase):
    def test_incomplete_while_open(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x",
                                   success_criteria=[{"kind": "all_tasks_completed"}])
            sup.missions.mutate(m["id"], lambda r: TaskGraph(r).add(
                new_task("A", "a")))
            out = sup.evaluator.evaluate(sup.missions.get(m["id"]))
            self.assertEqual(out["verdict"], EvalVerdict.INCOMPLETE.value)
            sup.stop()

    def test_complete_when_criteria_met(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x",
                                   success_criteria=[{"kind": "all_tasks_completed"}])
            def _fn(r):
                g = TaskGraph(r)
                n = g.add(new_task("A", "a"))
                g.mark(n["id"], "completed")
                r["status"] = "evaluating"
            sup.missions.mutate(m["id"], _fn)
            out = sup.evaluator.evaluate(sup.missions.get(m["id"]))
            self.assertEqual(out["verdict"], EvalVerdict.COMPLETE.value)
            sup.stop()

    def test_needs_replan_on_unmet_criterion(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(
                objective="x",
                success_criteria=[{"kind": "artifact_exists", "target": "nope.bin"}])
            def _fn(r):
                g = TaskGraph(r)
                n = g.add(new_task("A", "a"))
                g.mark(n["id"], "completed")
                r["status"] = "evaluating"
            sup.missions.mutate(m["id"], _fn)
            out = sup.evaluator.evaluate(sup.missions.get(m["id"]))
            self.assertEqual(out["verdict"], EvalVerdict.NEEDS_REPLAN.value)
            self.assertTrue(any("artifact_exists" in r for r in out["reasons"]))
            sup.stop()

    def test_evaluation_records_history(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x")
            sup.evaluator.evaluate(m)
            self.assertTrue(m["evaluator_history"])
            sup.stop()


class SupervisorLifecycleTests(unittest.TestCase):
    def test_mission_completes_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(
                objective="finish a thing",
                success_criteria=[{"kind": "all_tasks_completed"},
                                  {"kind": "verify_passed"}])
            sup.start_mission(m["id"])
            final = drive(sup, m["id"])
            self.assertEqual(final["status"], "completed")
            self.assertIsNotNone(final.get("completion"))
            self.assertEqual(final["completion"]["state"], "completed")
            # Action receipts persisted for audit/idempotency.
            self.assertTrue(sup.store.receipts.tail(50))
            sup.stop()

    def test_approval_pauses_and_resumes(self):
        with tempfile.TemporaryDirectory() as td:
            gate = {"ask": True}
            calls = {"n": 0}

            def executor(m, n, cb):
                calls["n"] += 1
                if gate["ask"] and calls["n"] >= 2:
                    return {"ok": False,
                            "pending_approval": {"name": "shell.execute",
                                                 "kind": "autonomy",
                                                 "detail": "needs shell"}}
                return {"ok": True, "output": "ok"}

            sup = make_sup(td, executor=executor)
            m = sup.create_mission(objective="x",
                                   success_criteria=[{"kind": "all_tasks_completed"}])
            sup.start_mission(m["id"])
            final = drive(sup, m["id"], ticks=15)
            self.assertEqual(final["status"], "waiting_approval")
            appr = sup.approvals(pending_only=True)
            self.assertEqual(len(appr), 1)
            gate["ask"] = False
            sup.resolve_approval(appr[0]["id"], approve=True)
            final = drive(sup, m["id"], ticks=30)
            self.assertEqual(final["status"], "completed")
            sup.stop()

    def test_denial_triggers_replan(self):
        with tempfile.TemporaryDirectory() as td:
            calls = {"n": 0}

            def executor(m, n, cb):
                calls["n"] += 1
                if calls["n"] == 2:
                    return {"ok": False,
                            "pending_approval": {"name": "x", "kind": "autonomy"}}
                return {"ok": True, "output": "ok"}

            sup = make_sup(td, executor=executor)
            m = sup.create_mission(objective="x",
                                   success_criteria=[{"kind": "all_tasks_completed"}])
            sup.start_mission(m["id"])
            final = drive(sup, m["id"], ticks=15)
            self.assertEqual(final["status"], "waiting_approval")
            appr = sup.approvals(pending_only=True)[0]
            sup.resolve_approval(appr["id"], approve=False)
            final = drive(sup, m["id"], ticks=30)
            # Denial replans: new recovery tasks were added and the mission
            # continued to completion rather than stalling.
            self.assertEqual(final["status"], "completed")
            self.assertGreater(len(final["graph"]["nodes"]), 3)
            sup.stop()

    def test_stop_autonomy_pauses_missions(self):
        with tempfile.TemporaryDirectory() as td:
            slow = []

            def executor(m, n, cb):
                time.sleep(0.05)
                return {"ok": True, "output": "ok"}

            sup = make_sup(td, executor=executor)
            m = sup.create_mission(objective="x")
            sup.start_mission(m["id"])
            sup.stop_autonomy()
            sup.tick()
            sup.tick()
            self.assertEqual(sup.missions.get(m["id"])["status"], "paused")
            # Nothing new starts while stopped.
            sup.tick()
            self.assertEqual(sup.missions.get(m["id"])["status"], "paused")
            sup.stop()

    def test_pause_resume_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x")
            sup.start_mission(m["id"])
            sup.tick()
            sup.pause_mission(m["id"])
            self.assertEqual(sup.missions.get(m["id"])["status"], "paused")
            sup.resume_mission(m["id"])
            self.assertIn(sup.missions.get(m["id"])["status"],
                          {"executing", "active"})
            sup.cancel_mission(m["id"])
            self.assertEqual(sup.missions.get(m["id"])["status"], "cancelled")
            sup.stop()

    def test_interactive_lane_wins(self):
        """Supervisor never claims the agent lane while interactive work is
        active — background yields to the user."""
        with tempfile.TemporaryDirectory() as td:
            ran = []
            sup = make_sup(
                td, lane_free=lambda: False,
                executor=lambda m, n, cb: ran.append(n["id"]) or {"ok": True})
            m = sup.create_mission(objective="x")
            sup.start_mission(m["id"])
            for _ in range(5):
                sup.tick()
            self.assertEqual(ran, [])
            self.assertNotIn(sup.missions.get(m["id"])["status"],
                             {"completed", "failed"})
            sup.stop()

    def test_failed_node_retries_then_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            calls = {"n": 0}

            def executor(m, n, cb):
                calls["n"] += 1
                return {"ok": False, "output": "connection reset by peer"}

            sup = make_sup(td, executor=executor)
            m = sup.create_mission(
                objective="x",
                budgets={"max_task_retries": 1, "max_repair_loops": 1,
                         "max_same_failure_retries": 10})
            sup.start_mission(m["id"])
            final = drive(sup, m["id"], ticks=40, settle=0.05)
            # NETWORK_FAILURE playbook: bounded retries then replan then —
            # with repair budget of 1 — eventually blocked, not looping.
            self.assertIn(final["status"], {"blocked", "waiting_approval",
                                            "executing", "paused"})
            self.assertLessEqual(calls["n"], 30)
            sup.stop()

    def test_budget_runtime_pause(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x",
                                   budgets={"max_runtime_s": 0.001})
            sup.start_mission(m["id"])
            sup.missions.mutate(m["id"], lambda r: r.update(
                {"runtime_deadline": time.time() - 1}))
            sup.tick()
            sup.tick()
            self.assertIn(sup.missions.get(m["id"])["status"],
                          {"paused", "blocked"})
            sup.stop()

    def test_standing_goal_spawns_mission(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            g = sup.add_standing_goal(
                "daily check", schedule={"kind": "interval", "interval_s": 60})
            # Force due.
            sup.store.standing_goals.data["goals"][0]["next_run"] = time.time() - 1
            sup.tick()
            missions = sup.missions.list()
            self.assertTrue(any(m["source_id"] == g["id"] for m in missions))
            sup.stop()

    def test_disabled_goal_does_not_run(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            g = sup.add_standing_goal("off", schedule={"kind": "interval",
                                                       "interval_s": 60})
            sup.set_goal_enabled(g["id"], False)
            sup.store.standing_goals.data["goals"][0]["next_run"] = time.time() - 1
            sup.tick()
            self.assertEqual(sup.missions.list(), [])
            sup.stop()

    def test_daily_summary(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x",
                                   success_criteria=[{"kind": "all_tasks_completed"}])
            sup.start_mission(m["id"])
            drive(sup, m["id"])
            summary = sup.daily_summary()
            self.assertEqual(summary["missions_completed"], 1)
            self.assertGreaterEqual(summary["tasks_completed"], 3)
            sup.stop()

    def test_mission_qa_uses_state(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            m = sup.create_mission(objective="x")
            sup.start_mission(m["id"])
            sup.tick(); sup.tick()
            ans = sup.answer_about_mission(m["id"], "what is left?")
            self.assertNotEqual(ans, "Mission not found.")
            self.assertTrue(ans.strip())
            sup.stop()

    def test_heartbeat_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            sup = make_sup(td)
            sup.tick()
            st = sup.status()
            self.assertIn("store", st)
            self.assertIsNotNone(st["store"]["schema_version"])
            sup.stop()


if __name__ == "__main__":
    unittest.main()
