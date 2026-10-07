"""Deterministic local-action lane tests — the never-claim-unverified
invariant made executable.

Covers the full lifecycle: intent parse -> capability -> permission ->
execute -> verify -> evidence -> truthful response, for create/write/
move/copy/delete, plus every failure shape: denied, approval required,
approval denied, tool disabled, tool failure, verification failure,
outside-workspace targets, and ambiguous requests that must fall
through to the model lane instead of guessing.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.action_ledger import ActionLedger
from localcodeagent.action_ops import (
    ActionPlan,
    execute_plan,
    parse_local_action,
)
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.filesystem import register_filesystem_tools


def make_env(perms=None):
    td = tempfile.TemporaryDirectory()
    ws = Path(td.name) / "ws"
    ws.mkdir()
    reg = ToolRegistry(perms or {
        "filesystem.read": "allow",
        "filesystem.write": "allow",
        "filesystem.delete": "allow",
    })
    register_filesystem_tools(reg, ws)
    ledger = ActionLedger(Path(td.name) / "ledger.json")
    return td, ws, reg, ledger


def run(text, ws, reg, ledger, **kw):
    plan = parse_local_action(text, workspace=ws)
    assert plan is not None, f"lane did not claim {text!r}"
    return plan, execute_plan(plan, tools=reg, ledger=ledger, **kw)


class TestParse(unittest.TestCase):
    def setUp(self):
        td, self.ws, _, _ = make_env()
        self.addCleanup(td.cleanup)

    def test_windows_absolute_folder_request(self):
        # The exact observed failure — claimed as mkdir, outside the
        # registered roots, never answered as a capability question.
        plan = parse_local_action(
            "can you create a folder d:\\Nexus", workspace=self.ws)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.kind, "mkdir")
        self.assertEqual(plan.tool, "fs_mkdir")
        self.assertTrue(plan.outside_root)
        self.assertEqual(Path(plan.params["path"]), Path("d:\\Nexus"))

    def test_polite_prefixes_and_named_forms(self):
        for text in ("please create a folder named testdir",
                     "could you make a directory called 'my stuff'",
                     "mkdir scratch"):
            plan = parse_local_action(text, workspace=self.ws)
            self.assertIsNotNone(plan, text)
            self.assertEqual(plan.kind, "mkdir")

    def test_two_path_operations(self):
        for text, kind in (("move a.txt to sub/b.txt", "move"),
                           ("rename foo.txt to bar.txt", "rename"),
                           ("copy c.txt to d/c.txt", "copy")):
            plan = parse_local_action(text, workspace=self.ws)
            self.assertIsNotNone(plan, text)
            self.assertEqual(plan.kind, kind)
            self.assertIn("src", plan.params)
            self.assertIn("dst", plan.params)

    def test_missing_target_asks_not_guesses(self):
        for text, kind in (("create a folder", "mkdir"),
                           ("delete the folder", "delete"),
                           ("create a file", "write")):
            plan = parse_local_action(text, workspace=self.ws)
            self.assertIsNotNone(plan, text)
            self.assertEqual(plan.kind, kind)
            self.assertTrue(plan.clarify, text)

    def test_questions_and_code_requests_fall_through(self):
        for text in ("what is a folder",
                     "how do I create a folder",
                     "can you explain recursion",
                     "write a python script that sorts a list",
                     "can you open notepad",
                     "tell me about directories",
                     "why did the build fail"):
            self.assertIsNone(
                parse_local_action(text, workspace=self.ws), text)

    def test_delete_recursion_flag(self):
        plan = parse_local_action(
            "delete folder tmp recursively", workspace=self.ws)
        self.assertTrue(plan.params.get("recursive"))
        self.assertEqual(Path(plan.params["path"]).name, "tmp")


class TestExecuteInRoot(unittest.TestCase):
    def setUp(self):
        td, self.ws, self.reg, self.ledger = make_env()
        self.addCleanup(td.cleanup)

    def test_mkdir_executes_and_verifies(self):
        _, out = run("create a folder named alpha",
                     self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertTrue((self.ws / "alpha").is_dir())
        self.assertIn("Created", out["text"])
        self.assertIn("alpha", out["text"])

    def test_write_executes_and_verifies(self):
        _, out = run("create a file notes.txt with content hello",
                     self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertEqual(
            (self.ws / "notes.txt").read_text(), "hello")

    def test_move_copy_delete_execute(self):
        (self.ws / "a.txt").write_text("x")
        _, out = run("move a.txt to sub/b.txt",
                     self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertFalse((self.ws / "a.txt").exists())
        self.assertTrue((self.ws / "sub" / "b.txt").is_file())

        _, out = run("copy sub/b.txt to c.txt",
                     self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertEqual((self.ws / "c.txt").read_text(), "x")

        _, out = run("delete c.txt", self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "verified")
        self.assertFalse((self.ws / "c.txt").exists())
        self.assertIn("Deleted", out["text"])

    def test_idempotent_noops_are_honest(self):
        (self.ws / "exists").mkdir()
        _, out = run("create a folder named exists",
                     self.ws, self.reg, self.ledger)
        self.assertIn("already exists", out["text"])
        _, out = run("delete ghost.txt", self.ws, self.reg, self.ledger)
        self.assertIn("doesn't exist", out["text"])
        self.assertNotIn("Deleted", out["text"])

    def test_failure_reported_not_claimed(self):
        (self.ws / "full").mkdir()
        (self.ws / "full" / "f.txt").write_text("x")
        _, out = run("delete full", self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "failed")
        self.assertIn("failed", out["text"].lower())
        self.assertNotIn("Deleted", out["text"])
        self.assertTrue((self.ws / "full").exists())

    def test_clarify_never_mutates(self):
        _, out = run("create a folder", self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "clarify")
        self.assertEqual(list(self.ws.iterdir()), [])


class TestPermissions(unittest.TestCase):
    def setUp(self):
        td, self.ws, self.reg, self.ledger = make_env({
            "filesystem.write": "deny",
            "filesystem.delete": "deny",
        })
        self.addCleanup(td.cleanup)

    def test_deny_reports_denial_not_success(self):
        _, out = run("create a folder named nope",
                     self.ws, self.reg, self.ledger)
        self.assertEqual(out["status"], "denied")
        self.assertNotIn("Created", out["text"])
        self.assertIn("deny", out["text"])
        self.assertFalse((self.ws / "nope").exists())


class TestApprovalFlow(unittest.TestCase):
    def test_ask_parks_then_approved_executes(self):
        td, ws, reg, ledger = make_env({"filesystem.write": "ask"})
        self.addCleanup(td.cleanup)
        plan, out = run("create a folder named pending",
                        ws, reg, ledger)
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertIn("approval", out["text"])
        self.assertNotIn("Created", out["text"])
        self.assertFalse((ws / "pending").exists())
        # Resume after user approval — same executor, verified.
        out2 = execute_plan(plan, tools=reg, ledger=ledger,
                            approved=True)
        self.assertEqual(out2["status"], "verified")
        self.assertTrue((ws / "pending").is_dir())
        self.assertIn("Created", out2["text"])

    def test_outside_workspace_always_asks(self):
        td, ws, reg, ledger = make_env()  # write=allow
        self.addCleanup(td.cleanup)
        outside = Path(td.name) / "outside_ws" / "Nexus"
        text = f"create a folder {outside}"
        plan, out = run(text, ws, reg, ledger)
        self.assertTrue(plan.outside_root)
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertIn("outside the workspace", out["text"])
        self.assertFalse(outside.exists())
        out2 = execute_plan(plan, tools=reg, ledger=ledger,
                            approved=True)
        self.assertEqual(out2["status"], "verified")
        self.assertTrue(outside.is_dir())
        self.assertIn("Created", out2["text"])

    def test_denied_profile_cannot_approve_outside(self):
        td, ws, reg, ledger = make_env({"filesystem.write": "deny"})
        self.addCleanup(td.cleanup)
        outside = Path(td.name) / "nowhere" / "x"
        _, out = run(f"create a folder {outside}",
                     ws, reg, ledger)
        self.assertEqual(out["status"], "denied")
        self.assertFalse(outside.exists())


class TestAvailabilityAndLedger(unittest.TestCase):
    def test_disabled_tool_reports_unavailable(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        reg.set_enabled("fs_mkdir", False)
        _, out = run("create a folder named x",
                     ws, reg, ledger)
        self.assertEqual(out["status"], "unavailable")
        self.assertNotIn("Created", out["text"])
        self.assertFalse((ws / "x").exists())

    def test_ledger_records_every_outcome(self):
        td, ws, reg, ledger = make_env({"filesystem.write": "ask"})
        self.addCleanup(td.cleanup)
        plan, _ = run("create a folder named gateddir", ws, reg, ledger)
        execute_plan(plan, tools=reg, ledger=ledger, approved=True)
        run("create a folder", ws, reg, ledger)  # clarify
        rows = ledger.recent(10)
        statuses = [r["status"] for r in rows]
        self.assertIn("awaiting_approval", statuses)
        self.assertIn("verified", statuses)
        self.assertIn("clarify", statuses)
        verified = [r for r in rows if r["status"] == "verified"]
        self.assertTrue(all(r["verified"] for r in verified))
        self.assertTrue(all(r["artifact"] for r in verified))
        # Every entry has task/tool/params/timestamps — the evidence
        # shape, not free-form text.
        for r in rows:
            self.assertIn("id", r)
            self.assertIn("tool", r)
            self.assertIn("params", r)
            self.assertIn("started_at", r)

    def test_ledger_persists_across_instances(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        run("create a folder named durable", ws, reg, ledger)
        ledger2 = ActionLedger(ledger.path)
        rows = ledger2.recent(5)
        self.assertTrue(any(
            r["kind"] == "mkdir" and r["status"] == "verified"
            for r in rows))

    def test_mission_rollup_summarizes_evidence(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        e1 = ledger.begin(kind="mission_node", action="scaffold app",
                          mission_id="m-1")
        ledger.finish(e1["id"], status="verified",
                      verification="task status: completed",
                      verified=True)
        e2 = ledger.begin(kind="mission_node", action="wire deps",
                          mission_id="m-1")
        ledger.finish(e2["id"], status="failed", failure="boom")
        e3 = ledger.begin(kind="mission_node", action="other mission",
                          mission_id="m-2")
        ledger.finish(e3["id"], status="verified", verified=True)
        roll = ledger.mission_rollup("m-1")
        self.assertEqual(roll["actions"], 2)
        self.assertEqual(roll["statuses"].get("verified"), 1)
        self.assertEqual(roll["statuses"].get("failed"), 1)
        self.assertEqual(roll["recent_verified"], ["scaffold app"])
        self.assertEqual(roll["recent_failures"], ["wire deps"])
        self.assertEqual(
            ledger.mission_rollup("m-2")["actions"], 1)
        self.assertEqual(
            ledger.mission_rollup("m-none")["actions"], 0)

    def test_recover_orphans_closes_interrupted_entries(self):
        td, ws, reg, ledger = make_env()
        self.addCleanup(td.cleanup)
        # An entry begun but never finished (crash mid-action) must not
        # survive a restart looking like pending work.
        e1 = ledger.begin(kind="mkdir", action="create tmp")
        e2 = ledger.begin(kind="mkdir", action="create gated")
        ledger.finish(e2["id"], status="awaiting_approval")
        e3 = ledger.begin(kind="mkdir", action="create done")
        ledger.finish(e3["id"], status="verified", verified=True)
        ledger2 = ActionLedger(ledger.path)  # "restart"
        self.assertEqual(ledger2.recover_orphans(), 1)
        rows = {r["id"]: r for r in ledger2.recent(10)}
        self.assertEqual(rows[e1["id"]]["status"], "unverified")
        self.assertIn("interrupted", rows[e1["id"]]["detail"])
        # Parked approvals and finished entries are untouched.
        self.assertEqual(
            rows[e2["id"]]["status"], "awaiting_approval")
        self.assertEqual(rows[e3["id"]]["status"], "verified")
        self.assertEqual(ledger2.recover_orphans(), 0)


class TestOrchestratorLane(unittest.TestCase):
    """The lane inside AgentOrchestrator — claims the turn, produces a
    truthful reply, parks/resumes approvals through the task store."""

    class _FakeRuntime:
        def refresh_hardware(self):
            return None

        def fresh_hardware(self, max_age_s: float = 15.0):
            return None

        def ensure_ready(self, profile):
            return profile.endpoint

        def recover(self, profile):
            return profile.endpoint

    def _orch(self, ws, perms):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        from localcodeagent.config import AgentConfig
        from localcodeagent.models.router import ModelRouter
        from localcodeagent.tools.base import ToolRegistry
        from localcodeagent.tools.filesystem import (
            register_filesystem_tools)
        from localcodeagent.workflow.checkpoint import CheckpointManager
        from localcodeagent.workflow.memory import ProjectMemory
        from localcodeagent.workflow.repository import RepositoryIndex
        from localcodeagent.workflow.tasks import TaskStore

        td = tempfile.TemporaryDirectory()
        root = Path(td.name)
        tasks = TaskStore(ws)
        checkpoints = CheckpointManager(ws)
        reg = ToolRegistry(perms)
        register_filesystem_tools(reg, ws)
        ledger = ActionLedger(root / "ledger.json")
        orch = AgentOrchestrator(
            AgentConfig(), ModelRouter([]), reg,
            self._FakeRuntime(),
            tasks=tasks, checkpoints=checkpoints,
            memory=ProjectMemory(ws),
            repository_index=RepositoryIndex(ws),
            action_ledger=ledger)
        return td, orch, tasks, ledger

    def test_lane_claims_and_executes(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        td2, orch, tasks, ledger = self._orch(
            ws, {"filesystem.read": "allow",
                 "filesystem.write": "allow"})
        self.addCleanup(td2.cleanup)
        result = orch.run("create a folder named orchtest",
                          event_callback=None)
        self.assertTrue((ws / "orchtest").is_dir())
        self.assertIn("Created", result.content)
        # Ledger entry exists and is verified — the reply's evidence.
        rows = [r for r in ledger.recent(10)
                if r["kind"] == "mkdir" and r["status"] == "verified"]
        self.assertTrue(rows)

    def test_lane_parks_for_approval_and_resumes(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        td2, orch, tasks, ledger = self._orch(
            ws, {"filesystem.read": "allow",
                 "filesystem.write": "ask"})
        self.addCleanup(td2.cleanup)
        result = orch.run("create a folder named gated",
                          event_callback=None)
        task = tasks.get(result.task["id"])
        self.assertEqual(task.status, "waiting_approval")
        self.assertIn("approval", result.content.lower())
        self.assertNotIn("Created", result.content)
        self.assertFalse((ws / "gated").exists())
        # Approval resumes through the persisted path and executes.
        resumed = orch.resume(task.id, approved=True)
        self.assertTrue((ws / "gated").is_dir())
        self.assertIn("Created", resumed.content)

    def test_lane_denial_is_honest(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        td2, orch, tasks, ledger = self._orch(
            ws, {"filesystem.read": "allow",
                 "filesystem.write": "ask"})
        self.addCleanup(td2.cleanup)
        result = orch.run("create a folder named refused",
                          event_callback=None)
        task = tasks.get(result.task["id"])
        resumed = orch.resume(task.id, approved=False)
        self.assertIn("did not", resumed.content.lower())
        self.assertNotIn("Created", resumed.content)
        self.assertFalse((ws / "refused").exists())
        rows = ledger.recent(10)
        self.assertTrue(any(r["status"] == "denied" for r in rows))

    def test_compound_local_actions_execute_in_order(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        td2, orch, tasks, ledger = self._orch(
            ws, {"filesystem.read": "allow",
                 "filesystem.write": "allow"})
        self.addCleanup(td2.cleanup)
        result = orch.run(
            "create a folder named alpha, and then "
            "create a folder named beta",
            event_callback=None)
        self.assertTrue((ws / "alpha").is_dir())
        self.assertTrue((ws / "beta").is_dir())
        self.assertIn("alpha", result.content.lower())
        self.assertIn("beta", result.content.lower())
        verified = [r for r in ledger.recent(10)
                    if r["status"] == "verified"]
        self.assertGreaterEqual(len(verified), 2)

    def test_compound_stops_at_approval_gate(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        td2, orch, tasks, ledger = self._orch(
            ws, {"filesystem.read": "allow",
                 "filesystem.write": "allow",
                 "filesystem.delete": "ask"})
        self.addCleanup(td2.cleanup)
        (ws / "gone.txt").write_text("x")
        result = orch.run(
            "create a folder named firstdir, and then "
            "delete gone.txt",
            event_callback=None)
        # First clause verified; the delete parked — nothing past the
        # gate ran or was claimed.
        self.assertTrue((ws / "firstdir").is_dir())
        self.assertTrue((ws / "gone.txt").exists())
        task = tasks.get(result.task["id"])
        self.assertEqual(task.status, "waiting_approval")
        self.assertIn("approval", result.content.lower())

    def test_compound_approval_resume_continues_tail(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        td2, orch, tasks, ledger = self._orch(
            ws, {"filesystem.read": "allow",
                 "filesystem.write": "allow",
                 "filesystem.delete": "ask"})
        self.addCleanup(td2.cleanup)
        (ws / "gone.txt").write_text("x")
        (ws / "keep.txt").write_text("y")
        result = orch.run(
            "create a folder named seqdir, and then "
            "delete gone.txt, and then move keep.txt to moved.txt",
            event_callback=None)
        task = tasks.get(result.task["id"])
        self.assertEqual(task.status, "waiting_approval")
        # Approve the gate — the sequence continues through the tail
        # instead of dropping it.
        resumed = orch.resume(task.id, approved=True)
        self.assertFalse((ws / "gone.txt").exists())
        self.assertTrue((ws / "moved.txt").exists())
        self.assertIn("Deleted", resumed.content)

    def test_compound_denial_cancels_tail(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        td2, orch, tasks, ledger = self._orch(
            ws, {"filesystem.read": "allow",
                 "filesystem.write": "allow",
                 "filesystem.delete": "ask"})
        self.addCleanup(td2.cleanup)
        (ws / "a.txt").write_text("x")
        (ws / "b.txt").write_text("y")
        result = orch.run(
            "delete a.txt, and then delete b.txt",
            event_callback=None)
        task = tasks.get(result.task["id"])
        resumed = orch.resume(task.id, approved=False)
        self.assertTrue((ws / "a.txt").exists())
        self.assertTrue((ws / "b.txt").exists())
        self.assertIn("cancelled", resumed.content.lower())

    def test_compound_unparseable_clause_falls_through(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        td2, orch, tasks, ledger = self._orch(
            ws, {"filesystem.read": "allow",
                 "filesystem.write": "allow"})
        self.addCleanup(td2.cleanup)
        # Second clause is not a local action — the lane must NOT
        # partially execute the first and narrate the second.
        from localcodeagent.context.intent import understand_turn
        text = ("create a folder named halfway, and then "
                "tell me a joke")
        env = understand_turn(text, active=None)
        self.assertTrue(env.compound)
        reply = orch._local_action_reply(text, "t1", env=env)
        self.assertIsNone(reply)
        self.assertFalse((ws / "halfway").exists())

    def test_compound_mid_sequence_unavailable_is_honest(self):
        # A tool going missing mid-sequence must not silently drop the
        # tail (there is no model lane to fall into once clauses have
        # executed) — and must not under-claim the work that did run.
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        td2, orch, tasks, ledger = self._orch(
            ws, {"filesystem.read": "allow",
                 "filesystem.write": "allow",
                 "filesystem.delete": "allow"})
        self.addCleanup(td2.cleanup)
        (ws / "gone.txt").write_text("x")
        orch.tools.set_enabled("fs_delete", False)
        result = orch.run(
            "create a folder named keepdir, and then delete gone.txt",
            event_callback=None)
        self.assertTrue((ws / "keepdir").is_dir())
        self.assertTrue((ws / "gone.txt").exists())
        self.assertIn("Created", result.content)
        self.assertIn("unavailable", result.content.lower())
        self.assertIn("nothing was changed", result.content.lower())


class TestGitStateLane(unittest.TestCase):
    """Regression — 'what branches are in github for your project?'
    answered from persona ('I don't have a public repo') while a real
    remote sat in .git/config. Repo-state questions are answered from
    real git output; git verbs still route to the tools lane."""

    def _orch(self, ws):
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        from localcodeagent.config import AgentConfig
        from localcodeagent.models.router import ModelRouter
        from localcodeagent.tools.base import ToolRegistry
        from localcodeagent.workflow.checkpoint import CheckpointManager
        from localcodeagent.workflow.memory import ProjectMemory
        from localcodeagent.workflow.repository import RepositoryIndex
        from localcodeagent.workflow.tasks import TaskStore

        class _FakeRuntime:
            def refresh_hardware(self): return None
            def fresh_hardware(self, max_age_s=15.0): return None
            def ensure_ready(self, p): return p.endpoint
            def recover(self, p): return p.endpoint

        return AgentOrchestrator(
            AgentConfig(), ModelRouter([]), ToolRegistry({}),
            _FakeRuntime(),
            tasks=TaskStore(ws), checkpoints=CheckpointManager(ws),
            memory=ProjectMemory(ws),
            repository_index=RepositoryIndex(ws))

    def _repo(self, ws):
        import subprocess
        def g(*a):
            subprocess.run(["git", *a], cwd=ws, capture_output=True,
                           check=True)
        g("init", "-b", "main")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (ws / "f.txt").write_text("x")
        g("add", ".")
        g("commit", "-m", "init")
        g("remote", "add", "origin",
          "https://github.com/owner/repo.git")
        g("checkout", "-b", "feature-x")

    def test_branches_question_answers_from_real_git(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        self._repo(ws)
        orch = self._orch(ws)
        r = orch._git_state_reply(
            "what branches are in github for your project?")
        self.assertIsNotNone(r)
        self.assertIn("github.com/owner/repo", r.text)
        self.assertIn("feature-x", r.text)
        self.assertIn("main", r.text)

    def test_no_repo_is_honest(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        orch = self._orch(ws)
        r = orch._git_state_reply("what branches does this repo have")
        self.assertIsNotNone(r)
        self.assertIn("isn't a git repository", r.text)

    def test_git_verbs_fall_through(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        orch = self._orch(ws)
        for q in ("push the branch to origin",
                  "delete the old branches",
                  "create a branch called x"):
            self.assertIsNone(orch._git_state_reply(q), q)

    def test_unrelated_questions_fall_through(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name) / "ws"
        ws.mkdir()
        orch = self._orch(ws)
        self.assertIsNone(
            orch._git_state_reply("what is the capital of france"))


if __name__ == "__main__":
    unittest.main()
