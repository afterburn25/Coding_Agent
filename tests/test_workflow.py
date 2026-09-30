import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.provider import ProviderResponse
from localcodeagent.models.router import ModelRouter
from localcodeagent.tools.base import ToolRegistry, ToolSpec
from localcodeagent.tools.filesystem import register_filesystem_tools
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore
from localcodeagent.workflow.verify import detect_verification_commands


class WorkflowTests(unittest.TestCase):
    def test_transactional_patch_and_restore(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / "a.txt"
            original.write_text("hello world\n", encoding="utf-8")
            tasks = TaskStore(root)
            task = tasks.create("edit", "auto")
            checkpoints = CheckpointManager(root)
            reg = ToolRegistry({"filesystem.read": "allow", "filesystem.write": "allow"})
            register_filesystem_tools(reg, root, checkpoints=checkpoints, tasks=tasks)
            reg.context["task_id"] = task.id

            result = reg.execute("apply_patch", {"changes": [{
                "path": "a.txt",
                "replacements": [{"old": "hello", "new": "goodbye"}],
            }]})
            self.assertIn("PATCH_APPLIED", result)
            self.assertEqual(original.read_text(encoding="utf-8"), "goodbye world\n")
            self.assertEqual(tasks.get(task.id).files_changed, ["a.txt"])
            self.assertIn("-hello world", checkpoints.diff(task.id))

            restored = checkpoints.restore(task.id)
            self.assertEqual(restored, ["a.txt"])
            self.assertEqual(original.read_text(encoding="utf-8"), "hello world\n")

    def test_patchset_validates_before_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("alpha\n", encoding="utf-8")
            (root / "b.txt").write_text("beta\n", encoding="utf-8")
            reg = ToolRegistry({"filesystem.write": "allow"})
            register_filesystem_tools(reg, root)
            result = reg.execute("apply_patch", {"changes": [
                {"path": "a.txt", "replacements": [{"old": "alpha", "new": "changed"}]},
                {"path": "b.txt", "replacements": [{"old": "missing", "new": "x"}]},
            ]})
            self.assertTrue(result.startswith("ERROR"))
            self.assertEqual((root / "a.txt").read_text(), "alpha\n")
            self.assertEqual((root / "b.txt").read_text(), "beta\n")

    def test_repository_index_searches_paths_and_symbols(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "engine.py").write_text("class Router:\n    def choose_model(self):\n        pass\n", encoding="utf-8")
            idx = RepositoryIndex(root)
            summary = idx.build()
            self.assertEqual(summary["file_count"], 1)
            matches = idx.search("choose_model")
            self.assertEqual(matches[0]["path"], "engine.py")

    def test_verification_detection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            (root / "tests").mkdir()
            cmds = detect_verification_commands(root)
            self.assertTrue(any("unittest" in c["command"] for c in cmds))


class _FakeRuntime:
    def refresh_hardware(self):
        return None

    def ensure_ready(self, profile):
        return profile.endpoint

    def recover(self, profile):
        return profile.endpoint


class _SequencedProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, *, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "dangerous_test_tool", "arguments": json.dumps({"value": "ok"})},
                }],
            }, raw={})
        return ProviderResponse(message={"role": "assistant", "content": "finished"}, raw={})


class _FinishedProvider:
    def complete(self, *, messages, tools=None):
        return ProviderResponse(message={"role": "assistant", "content": "finished after recovery"}, raw={})


class ApprovalResumeTests(unittest.TestCase):
    def test_approval_resumes_exact_tool_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["primary_coder", "fast_coder", "deep_reasoner", "reviewer"], runtime="external",
            )
            config = AgentConfig(models=[profile], permissions={"test.execute": "ask"}, auto_verify_after_changes=False, review_after_changes=False)
            router = ModelRouter(config.models)
            tools = ToolRegistry(config.permissions)
            seen = []
            tools.register(ToolSpec("dangerous_test_tool", "test", {
                "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]
            }, "test.execute", lambda args: seen.append(args["value"]) or "OK"))
            tasks = TaskStore(root)
            checkpoints = CheckpointManager(root)
            memory = ProjectMemory(root)
            index = RepositoryIndex(root)
            index.build()
            agent = AgentOrchestrator(
                config, router, tools, _FakeRuntime(),
                tasks=tasks, checkpoints=checkpoints, memory=memory, repository_index=index,
            )
            provider = _SequencedProvider()
            agent._provider_for = lambda _: provider

            first = agent.run("do the thing")
            self.assertIsNotNone(first.pending_approval)
            self.assertEqual(first.task["status"], "waiting_approval")
            self.assertEqual(seen, [])

            second = agent.resume(first.task["id"], approved=True)
            self.assertEqual(seen, ["ok"])
            self.assertEqual(second.content, "finished")
            self.assertEqual(second.task["status"], "completed")

    def test_persisted_approval_resumes_after_process_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["primary_coder", "fast_coder", "deep_reasoner", "reviewer"], runtime="external",
            )
            config = AgentConfig(models=[profile], permissions={"test.execute": "ask"}, auto_verify_after_changes=False, review_after_changes=False)
            router = ModelRouter(config.models)
            seen = []

            def make_tools():
                tools = ToolRegistry(config.permissions)
                tools.register(ToolSpec("dangerous_test_tool", "test", {
                    "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]
                }, "test.execute", lambda args: seen.append(args["value"]) or "OK"))
                return tools

            tasks1 = TaskStore(root)
            checkpoints = CheckpointManager(root)
            memory = ProjectMemory(root)
            index = RepositoryIndex(root); index.build()
            agent1 = AgentOrchestrator(
                config, router, make_tools(), _FakeRuntime(),
                tasks=tasks1, checkpoints=checkpoints, memory=memory, repository_index=index,
            )
            provider1 = _SequencedProvider()
            agent1._provider_for = lambda _: provider1
            first = agent1.run("do the thing")
            self.assertEqual(first.task["status"], "waiting_approval")

            # Simulate a full application restart: new task store + orchestrator,
            # with no in-memory _AgentSession from the first process.
            tasks2 = TaskStore(root)
            agent2 = AgentOrchestrator(
                config, router, make_tools(), _FakeRuntime(),
                tasks=tasks2, checkpoints=checkpoints, memory=memory, repository_index=index,
            )
            agent2._provider_for = lambda _: _FinishedProvider()
            resumed = agent2.resume(first.task["id"], approved=True)
            self.assertEqual(seen, ["ok"])
            self.assertEqual(resumed.content, "finished after recovery")
            self.assertEqual(resumed.task["status"], "completed")
            self.assertEqual(resumed.task["recovery_count"], 1)


class TaskRecoveryTests(unittest.TestCase):
    def test_taskstore_marks_inflight_task_interrupted_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = TaskStore(root)
            task = tasks.create("continue me", "auto")
            tasks.update(task.id, status="running", phase="working", model_id="local", model_role="primary_coder")

            reloaded = TaskStore(root)
            recovered = reloaded.get(task.id)
            self.assertEqual(recovered.status, "interrupted")
            self.assertEqual(recovered.phase, "interrupted")
            self.assertEqual(recovered.interrupted_from, "working")
            self.assertIn("stopped before", recovered.error.lower())
            self.assertEqual(reloaded.current().id, task.id)

    def test_interrupted_task_can_recover_from_durable_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["primary_coder", "fast_coder", "deep_reasoner", "reviewer"], runtime="external",
            )
            config = AgentConfig(models=[profile], permissions={}, auto_verify_after_changes=False, review_after_changes=False)
            tasks = TaskStore(root)
            task = tasks.create("finish the interrupted work", "auto")
            tasks.update(task.id, status="running", phase="working", model_id="local", model_role="primary_coder", steps=2)

            # Restart normalizes the formerly active task to interrupted.
            tasks = TaskStore(root)
            self.assertEqual(tasks.get(task.id).status, "interrupted")
            router = ModelRouter(config.models)
            index = RepositoryIndex(root); index.build()
            agent = AgentOrchestrator(
                config, router, ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=tasks, checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
            )
            agent._provider_for = lambda _: _FinishedProvider()
            result = agent.recover(task.id)
            self.assertEqual(result.task["status"], "completed")
            self.assertEqual(result.task["recovery_count"], 1)
            self.assertTrue(any(e.get("type") == "session_recovery" for e in result.model_events))


class _RepairProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, *, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(message={
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": "patch1", "type": "function",
                    "function": {"name": "apply_patch", "arguments": json.dumps({"changes": [{"path": "a.txt", "replacements": [{"old": "old", "new": "new"}]}]})},
                }],
            }, raw={})
        if self.calls == 2:
            return ProviderResponse(message={"role": "assistant", "content": "first implementation"}, raw={})
        return ProviderResponse(message={"role": "assistant", "content": "repaired after verification research"}, raw={})


class VerificationRepairLoopTests(unittest.TestCase):
    def test_failed_verification_reenters_agent_then_retests(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("old\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            (root / "tests").mkdir()
            profile = ModelProfile(id="local", endpoint="http://unused/v1", model="x", roles=["primary_coder", "fast_coder", "deep_reasoner", "reviewer"], runtime="external")
            config = AgentConfig(
                models=[profile], permissions={"filesystem.read": "allow", "filesystem.write": "allow", "shell.execute": "allow"},
                auto_verify_after_changes=True, review_after_changes=False, max_auto_repair_cycles=1,
            )
            router = ModelRouter(config.models)
            tools = ToolRegistry(config.permissions)
            tasks = TaskStore(root); checkpoints = CheckpointManager(root)
            register_filesystem_tools(tools, root, checkpoints=checkpoints, tasks=tasks)
            shell_calls = []
            def fake_shell(args):
                shell_calls.append(args["command"])
                return ("OUTPUT:\nfailed\nEXIT_CODE=1" if len(shell_calls) == 1 else "OUTPUT:\npassed\nEXIT_CODE=0")
            tools.register(ToolSpec("run_shell", "test shell", {"type": "object", "properties": {"command": {"type": "string"}}}, "shell.execute", fake_shell))
            memory = ProjectMemory(root); index = RepositoryIndex(root); index.build()
            agent = AgentOrchestrator(config, router, tools, _FakeRuntime(), tasks=tasks, checkpoints=checkpoints, memory=memory, repository_index=index)
            provider = _RepairProvider(); agent._provider_for = lambda _: provider

            result = agent.run("change the file and make tests pass")
            self.assertEqual(result.task["status"], "completed")
            self.assertEqual(provider.calls, 3)
            self.assertEqual(len(shell_calls), 2)
            self.assertTrue(any(e.get("type") == "verification_repair" for e in result.model_events))
            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "new\n")


if __name__ == "__main__":
    unittest.main()
