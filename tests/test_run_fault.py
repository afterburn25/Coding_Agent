"""run() fault boundary — a raise inside _run_impl must close the ledger
row ("error", phase done) BEFORE propagating. The live defect: an
exception after tasks.create left status=running/planning with updated_at
frozen — a phantom "current" task that wedged the whole chat queue for
the session (every later prompt queued behind it)."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.router import ModelRouter
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore


class _FakeRuntime:
    def refresh_hardware(self):
        pass

    def fresh_hardware(self, max_age_s: float = 15.0):
        return None


def _agent(root: Path) -> AgentOrchestrator:
    profile = ModelProfile(
        id="local", endpoint="http://unused/v1", model="x",
        roles=["utility", "fast_coder", "primary_coder"], runtime="external",
    )
    config = AgentConfig(
        models=[profile], permissions={}, research_enabled=False,
        auto_verify_after_changes=False, review_after_changes=False,
    )
    return AgentOrchestrator(
        config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
        tasks=TaskStore(root), checkpoints=CheckpointManager(root),
        memory=ProjectMemory(root), repository_index=RepositoryIndex(root),
        research=None,
    )


class RunFaultBoundaryTests(unittest.TestCase):
    def test_raise_closes_task_then_propagates(self):
        with TemporaryDirectory() as td:
            agent = _agent(Path(td))

            def _boom(*_a, **_k):
                raise RuntimeError("boom")

            agent._act = _boom  # planning step raises — the old phantom
            with self.assertRaises(RuntimeError):
                agent.run("what is the capital of France?")

            tasks = agent.tasks.recent()
            self.assertEqual(len(tasks), 1)
            row = tasks[0]
            # 'error' (not 'failed'): the bounded watchdog retry lane picks
            # it up like every other execution failure.
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["phase"], "done")
            self.assertTrue(row["error"])

    def test_terminal_row_is_not_rewritten(self):
        # A raise AFTER the inner code already closed the task must not
        # clobber its verdict (e.g. _finalize marked it completed/error).
        with TemporaryDirectory() as td:
            agent = _agent(Path(td))
            created = {}

            orig_create = agent.tasks.create

            def create_and_mark(*a, **k):
                task = orig_create(*a, **k)
                created["id"] = task.id
                agent.tasks.update(task.id, status="cancelled", phase="done")
                return task

            agent.tasks.create = create_and_mark

            def _boom(*_a, **_k):
                raise RuntimeError("boom")

            agent._act = _boom
            with self.assertRaises(RuntimeError):
                agent.run("hi")
            self.assertEqual(
                agent.tasks.get(created["id"]).status, "cancelled")

    def test_raise_before_task_create_propagates_with_no_row(self):
        with TemporaryDirectory() as td:
            agent = _agent(Path(td))

            def _boom(*_a, **_k):
                raise OSError("disk full")

            agent.tasks.create = _boom
            with self.assertRaises(OSError):
                agent.run("hello")
            self.assertEqual(agent.tasks.recent(), [])

    def test_work_order_raise_closes_task(self):
        with TemporaryDirectory() as td:
            agent = _agent(Path(td))

            def _boom(*_a, **_k):
                raise RuntimeError("boom")

            agent._act = _boom
            with self.assertRaises(RuntimeError):
                agent.run_work_order("fix the endpoint")
            row = agent.tasks.recent()[0]
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["phase"], "done")


if __name__ == "__main__":
    unittest.main()
