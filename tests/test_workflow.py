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
from localcodeagent.workflow.conversation_memory import ConversationMemory
from localcodeagent.workflow.conversation_manager import ConversationManager
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


class _CaptureProvider:
    def __init__(self):
        self.messages = []
        self.tools = None

    def complete(self, *, messages, tools=None):
        self.messages = list(messages)
        self.tools = tools
        return ProviderResponse(message={"role": "assistant", "content": "done"}, raw={})


class _RepeatedRefusalProvider:
    def __init__(self, refusals: int):
        self.refusals = refusals
        self.calls = 0
        self.messages = []

    def complete(self, *, messages, tools=None):
        self.calls += 1
        self.messages = list(messages)
        if self.calls <= self.refusals:
            return ProviderResponse(message={
                "role": "assistant",
                "content": "I can't engage in explicit or inappropriate content. Let's keep the conversation respectful and constructive.",
            }, raw={})
        return ProviderResponse(message={
            "role": "assistant",
            "content": "Understood. Adult-only explicit conversation is allowed here, so I'll answer the request directly.",
        }, raw={})


class LightweightUtilityRouteTests(unittest.TestCase):
    def test_live_clock_questions_are_answered_without_model_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["utility", "fast_coder", "primary_coder"], runtime="external",
            )
            config = AgentConfig(
                models=[profile], permissions={}, research_enabled=False,
                auto_verify_after_changes=False, review_after_changes=False,
            )
            index = RepositoryIndex(root); index.build()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
            )
            agent._provider_for = lambda _: (_ for _ in ()).throw(
                AssertionError("clock questions should use the host clock without loading a model")
            )

            time_result = agent.run("what time is it?")
            date_result = agent.run("what day is it?")

            self.assertEqual(time_result.routing.model_id, "builtin-local")
            self.assertIn("current local time", time_result.content.lower())
            self.assertEqual(date_result.routing.model_id, "builtin-local")
            self.assertIn("today is", date_result.content.lower())
            snapshot = AgentOrchestrator.current_time_snapshot()
            self.assertRegex(snapshot["date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertRegex(snapshot["time"], r"^\d{2}:\d{2}:\d{2}$")
            self.assertTrue(snapshot["weekday"])
            self.assertTrue(snapshot["timezone"])
            self.assertIn("Current local date/time from the host system clock", agent.current_time_context())

    def test_model_conversation_receives_current_clock_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["utility", "fast_coder", "primary_coder"], runtime="external",
            )
            config = AgentConfig(
                models=[profile], permissions={}, research_enabled=False,
                auto_research_unknown=False,
                auto_verify_after_changes=False, review_after_changes=False,
            )
            index = RepositoryIndex(root); index.build()
            provider = _CaptureProvider()
            conversations = ConversationManager(root / "conversations.json")
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
                conversation_manager=conversations,
            )
            agent._provider_for = lambda _: provider

            agent.run("how is your day going?")

            system_text = "\n".join(
                str(m.get("content", "")) for m in provider.messages if m.get("role") == "system"
            )
            self.assertIn("Current local date/time from the host system clock", system_text)
            self.assertIn("refreshed at the start of every user turn", system_text)
            self.assertIn("Conversation timing context from durable message timestamps", system_text)
            self.assertIn("Conversation quality rules: speak like a capable adult conversational partner", system_text)

    def test_greeting_skips_repository_research_and_coding_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["utility", "fast_coder", "primary_coder"], runtime="external",
            )
            config = AgentConfig(
                models=[profile], permissions={}, research_enabled=True,
                auto_verify_after_changes=False, review_after_changes=False,
            )
            index = RepositoryIndex(root)
            index.build()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
            )
            agent._provider_for = lambda _: (_ for _ in ()).throw(AssertionError("greeting should not load a model"))

            result = agent.run("hi")

            self.assertEqual(result.routing.role, "utility")
            self.assertEqual(result.routing.model_id, "builtin-local")
            self.assertTrue(result.content.startswith("Hi!"))
            self.assertEqual(result.task["status"], "completed")
            self.assertTrue(any(e.get("type") == "builtin_utility" for e in result.model_events))

    def test_capability_question_is_answered_without_model_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["utility", "fast_coder", "primary_coder"], runtime="external",
            )
            config = AgentConfig(
                models=[profile], permissions={}, research_enabled=True,
                auto_verify_after_changes=False, review_after_changes=False,
            )
            index = RepositoryIndex(root); index.build()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
            )
            agent._provider_for = lambda _: (_ for _ in ()).throw(AssertionError("capability question should not load a model"))

            result = agent.run("what all can you do?")

            self.assertEqual(result.routing.model_id, "builtin-local")
            self.assertIn("inspect and edit code", result.content)
            self.assertIn("general-knowledge", result.content)
            self.assertIn("Nexus Brain", result.content)
            self.assertEqual(result.task["status"], "completed")

    def test_self_learning_question_includes_general_and_conversation_learning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["utility", "fast_coder", "primary_coder"], runtime="external",
            )
            config = AgentConfig(
                models=[profile], permissions={}, research_enabled=True,
                auto_verify_after_changes=False, review_after_changes=False,
            )
            index = RepositoryIndex(root); index.build()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
            )
            agent._provider_for = lambda _: (_ for _ in ()).throw(AssertionError("self-learning question should not load a model"))

            result = agent.run("can you learn and adapt?")

            self.assertEqual(result.routing.model_id, "builtin-local")
            self.assertIn("verified general knowledge", result.content)
            self.assertIn("conversational patterns", result.content)
            self.assertIn("across model replacements", result.content)
            self.assertNotIn("focused on coding", result.content.lower())


class PermissiveConversationRetryTests(unittest.TestCase):
    def _agent(self, root: Path, provider, *, retry_limit: int = 3):
        profile = ModelProfile(
            id="local", endpoint="http://unused/v1", model="x",
            roles=["utility", "fast_coder", "primary_coder"], runtime="external",
        )
        config = AgentConfig(
            models=[profile],
            permissions={},
            research_enabled=False,
            auto_verify_after_changes=False,
            review_after_changes=False,
            ethical_temperature=1.0,
            generic_refusal_retry_limit=retry_limit,
        )
        index = RepositoryIndex(root); index.build()
        agent = AgentOrchestrator(
            config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
            tasks=TaskStore(root), checkpoints=CheckpointManager(root),
            memory=ProjectMemory(root), repository_index=index,
        )
        agent._provider_for = lambda _: provider
        return agent

    def test_repeated_generic_refusals_are_discarded_until_direct_answer(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _RepeatedRefusalProvider(refusals=2)
            agent = self._agent(Path(td), provider, retry_limit=3)

            result = agent.run("I want to talk about explicit adult topics.")

            self.assertEqual(provider.calls, 3)
            self.assertIn("answer the request directly", result.content)
            self.assertNotIn("keep the conversation respectful", result.content.lower())
            retries = [e for e in result.model_events if e.get("type") == "generic_refusal_retry"]
            self.assertEqual(len(retries), 2)
            self.assertEqual([e.get("attempt") for e in retries], [1, 2])
            assistant_refusals = [
                m for m in provider.messages
                if m.get("role") == "assistant" and "can't engage in explicit" in str(m.get("content", "")).lower()
            ]
            self.assertEqual(assistant_refusals, [])

    def test_retry_exhaustion_reports_model_level_blocker_not_policy_ban(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _RepeatedRefusalProvider(refusals=10)
            agent = self._agent(Path(td), provider, retry_limit=2)

            result = agent.run("I want to talk about explicit adult topics.")

            self.assertEqual(provider.calls, 3)
            self.assertIn("model itself is refusing", result.content.lower())
            self.assertIn("not blocking adult-only consensual explicit text", result.content.lower())
            self.assertTrue(any(e.get("type") == "generic_refusal_exhausted" for e in result.model_events))


class DirectImageRoutingTests(unittest.TestCase):
    def test_normal_image_request_bypasses_chat_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["utility", "fast_coder", "primary_coder"], runtime="external",
            )
            config = AgentConfig(
                models=[profile],
                permissions={"image.generate": "allow"},
                research_enabled=False,
                auto_verify_after_changes=False,
                review_after_changes=False,
            )
            tools = ToolRegistry(config.permissions)
            tools.register(ToolSpec(
                "generate_image",
                "test image generator",
                {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
                "image.generate",
                lambda args: json.dumps({
                    "ok": True,
                    "job": {"id": "img-1", "model_id": "image-model", "state": "queued"},
                }),
            ))
            index = RepositoryIndex(root); index.build()
            conversations = ConversationManager(root / "data" / "conversations.json")
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), tools, _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
                conversation_manager=conversations,
            )
            agent._provider_for = lambda _: (_ for _ in ()).throw(
                AssertionError("image intent must not activate the chat model")
            )

            result = agent.run("generate a picture of a woman")

            self.assertEqual(result.routing.role, "image")
            self.assertEqual(result.routing.model_id, "image-model")
            self.assertEqual(result.task["status"], "completed")
            self.assertEqual(result.tool_events[0]["name"], "generate_image")
            self.assertIn("Image generation started", result.content)

    def test_image_request_with_approval_waits_without_chat_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["utility", "fast_coder", "primary_coder"], runtime="external",
            )
            config = AgentConfig(
                models=[profile],
                permissions={"image.generate": "ask"},
                research_enabled=False,
                auto_verify_after_changes=False,
                review_after_changes=False,
            )
            tools = ToolRegistry(config.permissions)
            tools.register(ToolSpec(
                "generate_image",
                "test image generator",
                {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
                "image.generate",
                lambda args: json.dumps({
                    "ok": True,
                    "job": {"id": "img-2", "model_id": "image-model", "state": "queued"},
                }),
            ))
            index = RepositoryIndex(root); index.build()
            conversations = ConversationManager(root / "data" / "conversations.json")
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), tools, _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
                conversation_manager=conversations,
            )
            agent._provider_for = lambda _: (_ for _ in ()).throw(
                AssertionError("image intent must not activate the chat model")
            )

            first = agent.run("generate a picture of a woman")
            self.assertEqual(first.task["status"], "waiting_approval")
            self.assertEqual(first.pending_approval["kind"], "direct_image")

            second = agent.resume(first.task["id"], approved=True)
            self.assertEqual(second.task["status"], "completed")
            self.assertEqual(second.routing.role, "image")
            self.assertEqual(second.tool_events[0]["name"], "generate_image")


class ConversationMemoryTests(unittest.TestCase):
    def test_taught_rules_and_facts_survive_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "conversation_memory.json"
            memory = ConversationMemory(path)
            learned_rule = memory.learn_from_user("From now on, keep answers concise")
            learned_fact = memory.learn_from_user("Remember that I prefer dark mode")

            self.assertIn("keep answers concise", learned_rule["behavior_rules"])
            self.assertIn("I prefer dark mode", learned_fact["facts"])

            reloaded = ConversationMemory(path)
            context = reloaded.prompt_context()
            self.assertIn("keep answers concise", context)
            self.assertIn("I prefer dark mode", context)
            self.assertIn("Semantic recall rule", context)
            self.assertIn("canonical meanings, not canned response text", context)
            self.assertIn("Do not copy the stored sentence word-for-word", context)
            self.assertIn("Recall expression cue for this turn:", context)

    def test_correction_becomes_reviewable_training_example(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "conversation_memory.json")
            memory.record_exchange("Explain this", "A long answer")
            learned = memory.learn_from_user("No, you should keep that answer much shorter")

            self.assertEqual(len(learned["training_examples"]), 1)
            example = learned["training_examples"][0]
            self.assertEqual(example["instruction"], "Explain this")
            self.assertEqual(example["previous_response"], "A long answer")
            self.assertFalse(example["approved"])
            self.assertIn("keep that answer much shorter", learned["behavior_rules"])

    def test_training_command_is_saved_without_model_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conversation = ConversationMemory(root / "data" / "conversation_memory.json")
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["utility", "fast_coder", "primary_coder"], runtime="external",
            )
            config = AgentConfig(
                models=[profile], permissions={}, research_enabled=False,
                auto_verify_after_changes=False, review_after_changes=False,
            )
            index = RepositoryIndex(root); index.build()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
                conversation_memory=conversation,
            )
            agent._provider_for = lambda _: (_ for _ in ()).throw(AssertionError("teaching command should not load a model"))

            result = agent.run("From now on, explain errors in plain English")

            self.assertEqual(result.routing.model_id, "builtin-local")
            self.assertIn("operating rule", result.content)
            self.assertIn("explain errors in plain English", conversation.prompt_context())
            self.assertEqual(result.task["final_content"], result.content)

    def test_saved_rules_are_injected_into_future_model_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conversation = ConversationMemory(root / "data" / "conversation_memory.json")
            conversation.learn_from_user("From now on, keep answers concise")
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["primary_coder", "fast_coder"], runtime="external",
            )
            config = AgentConfig(
                models=[profile], permissions={}, research_enabled=False,
                auto_verify_after_changes=False, review_after_changes=False,
            )
            index = RepositoryIndex(root); index.build()
            provider = _CaptureProvider()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
                conversation_memory=conversation,
            )
            agent._provider_for = lambda _: provider

            result = agent.run("Explain the architecture of this project")

            system_text = "\n".join(str(m.get("content", "")) for m in provider.messages if m.get("role") == "system")
            self.assertIn("User-taught operating rules", system_text)
            self.assertIn("keep answers concise", system_text)
            self.assertEqual(result.task["final_content"], "done")


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

    def test_newer_completed_task_replaces_old_interrupted_as_current(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = TaskStore(root)
            old = tasks.create("old task", "auto")
            tasks.update(old.id, status="running", phase="working")

            # Simulate restart: old active task becomes interrupted.
            tasks = TaskStore(root)
            self.assertEqual(tasks.current().id, old.id)
            self.assertEqual(tasks.current().status, "interrupted")

            new = tasks.create("hi", "auto")
            tasks.update(new.id, status="completed", phase="done", summary="Hi!")

            current = tasks.current()
            self.assertEqual(current.id, new.id)
            self.assertEqual(current.status, "completed")
            self.assertEqual(tasks.get(old.id).status, "interrupted")

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


class _PatchThenReviewProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, *, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(message={
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": "patch-fallback", "type": "function",
                    "function": {"name": "apply_patch", "arguments": json.dumps({
                        "changes": [{"path": "a.txt", "replacements": [{"old": "old", "new": "new"}]}],
                    })},
                }],
            }, raw={})
        if self.calls == 2:
            return ProviderResponse(message={"role": "assistant", "content": "implementation complete"}, raw={})
        return ProviderResponse(message={"role": "assistant", "content": "PASS reviewer fallback"}, raw={})


class ModelActivationFallbackTests(unittest.TestCase):
    def test_auto_mode_falls_back_when_deep_model_cannot_start(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary = ModelProfile(
                id="primary", endpoint="http://primary/v1", model="primary",
                roles=["fast_coder", "primary_coder"], runtime="external", priority=50,
            )
            deep = ModelProfile(
                id="deep", endpoint="http://deep/v1", model="deep",
                roles=["deep_reasoner", "reviewer"], runtime="external", priority=100,
            )
            config = AgentConfig(
                models=[primary, deep], permissions={}, research_enabled=False,
                auto_verify_after_changes=False, review_after_changes=False,
            )
            index = RepositoryIndex(root); index.build()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
            )
            provider = _FinishedProvider()

            def provider_for(profile):
                if profile.id == "deep":
                    raise RuntimeError("deep model could not start")
                return provider

            agent._provider_for = provider_for
            result = agent.run("Investigate the root cause of this race condition and refactor the architecture")

            self.assertEqual(result.task["status"], "completed")
            self.assertEqual(result.routing.model_id, "primary")
            self.assertTrue(any(
                event.get("type") == "activation_fallback"
                and event.get("from") == "deep"
                and event.get("to") == "primary"
                for event in result.model_events
            ))

    def test_review_model_start_failure_falls_back_and_still_returns_final_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("old\n", encoding="utf-8")
            primary = ModelProfile(
                id="primary", endpoint="http://primary/v1", model="primary",
                roles=["fast_coder", "primary_coder"], runtime="external", priority=50,
            )
            deep = ModelProfile(
                id="deep", endpoint="http://deep/v1", model="deep",
                roles=["deep_reasoner", "reviewer"], runtime="external", priority=100,
            )
            config = AgentConfig(
                models=[primary, deep],
                permissions={"filesystem.read": "allow", "filesystem.write": "allow"},
                research_enabled=False,
                auto_verify_after_changes=False,
                review_after_changes=True,
            )
            tasks = TaskStore(root)
            checkpoints = CheckpointManager(root)
            tools = ToolRegistry(config.permissions)
            register_filesystem_tools(tools, root, checkpoints=checkpoints, tasks=tasks)
            index = RepositoryIndex(root); index.build()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), tools, _FakeRuntime(),
                tasks=tasks, checkpoints=checkpoints,
                memory=ProjectMemory(root), repository_index=index,
            )
            provider = _PatchThenReviewProvider()

            def provider_for(profile):
                if profile.id == "deep":
                    raise RuntimeError("review model could not start")
                return provider

            agent._provider_for = provider_for
            result = agent.run("change the file")

            self.assertEqual(result.task["status"], "completed")
            self.assertEqual(result.content, "implementation complete")
            self.assertTrue(result.review.startswith("PASS"))
            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "new\n")
            self.assertTrue(any(
                event.get("type") == "activation_fallback"
                and event.get("from") == "deep"
                and event.get("to") == "primary"
                for event in result.model_events
            ))


class SelfHostingContextTests(unittest.TestCase):
    def test_fresh_self_development_task_gets_guardrail_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "localcodeagent").mkdir()
            (root / "localcodeagent" / "server.py").write_text("# marker\n", encoding="utf-8")
            (root / "web").mkdir()
            (root / "web" / "index.html").write_text("Chat Nexus\n", encoding="utf-8")
            (root / "SESSION_HANDOFF.md").write_text("# handoff\n", encoding="utf-8")
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["primary_coder", "fast_coder", "deep_reasoner", "reviewer"], runtime="external",
            )
            config = AgentConfig(models=[profile], permissions={}, research_enabled=False, auto_verify_after_changes=False, review_after_changes=False)
            index = RepositoryIndex(root); index.build()
            provider = _CaptureProvider()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
                tasks=TaskStore(root), checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
            )
            agent._provider_for = lambda _: provider
            result = agent.run("improve Chat Nexus")
            self.assertEqual(result.content, "done")
            system_text = "\n".join(str(m.get("content", "")) for m in provider.messages if m.get("role") == "system")
            self.assertIn("SELF-HOSTING MODE", system_text)
            self.assertIn("isolated second-instance selftest", system_text)
            self.assertIn("Do not create Git commits", system_text)

    def test_ui_self_development_launcher_prefills_without_autosubmit(self):
        app = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("startSelfDevelopment", app)
        self.assertIn("prepareSelfDevelopmentTask", app)
        self.assertIn("Do not commit or push unless I explicitly ask", app)


class _RepeatToolProvider:
    """Returns a tool call on the first N completions, then a final answer."""

    def __init__(self, tool_calls: int, tool_name: str = "probe_tool"):
        self.remaining = tool_calls
        self.tool_name = tool_name
        self.calls = 0

    def complete(self, *, messages, tools=None):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return ProviderResponse(message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"c{self.calls}",
                    "type": "function",
                    "function": {"name": self.tool_name, "arguments": "{}"},
                }],
            }, raw={})
        return ProviderResponse(message={"role": "assistant", "content": "all done"}, raw={})


class AutonomousContinuationTests(unittest.TestCase):
    def _agent(self, root: Path, provider, *, autonomous: bool, continuations: int = 2):
        profile = ModelProfile(
            id="local", endpoint="http://unused/v1", model="x",
            roles=["primary_coder", "fast_coder", "deep_reasoner", "reviewer"], runtime="external",
        )
        config = AgentConfig(
            models=[profile],
            permissions={"probe.execute": "allow"},
            research_enabled=False,
            auto_verify_after_changes=False,
            review_after_changes=False,
            max_agent_steps=1,
            autonomous_mode=autonomous,
            autonomous_max_continuations=continuations,
        )
        tools = ToolRegistry(config.permissions)
        tools.register(ToolSpec("probe_tool", "test", {"type": "object", "properties": {}},
                                "probe.execute", lambda args: "PROBE_OK"))
        index = RepositoryIndex(root)
        index.build()
        agent = AgentOrchestrator(
            config, ModelRouter(config.models), tools, _FakeRuntime(),
            tasks=TaskStore(root), checkpoints=CheckpointManager(root),
            memory=ProjectMemory(root), repository_index=index,
        )
        agent._provider_for = lambda _: provider
        return agent

    def test_autonomous_mode_continues_past_step_limit(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _RepeatToolProvider(tool_calls=2)
            agent = self._agent(Path(td), provider, autonomous=True, continuations=2)

            result = agent.run("do the thing")

            self.assertEqual(result.content, "all done")
            self.assertEqual(result.task["status"], "completed")
            self.assertEqual(provider.calls, 3)
            continuations = [e for e in result.model_events if e.get("type") == "autonomous_continuation"]
            self.assertEqual(len(continuations), 2)

    def test_step_limit_still_applies_without_autonomous_mode(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _RepeatToolProvider(tool_calls=2)
            agent = self._agent(Path(td), provider, autonomous=False)

            result = agent.run("do the thing")

            self.assertEqual(result.task["status"], "step_limit")

    def test_autonomous_continuations_are_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _RepeatToolProvider(tool_calls=10)
            agent = self._agent(Path(td), provider, autonomous=True, continuations=1)

            result = agent.run("do the thing")

            self.assertEqual(result.task["status"], "step_limit")
            self.assertEqual(provider.calls, 2)

    def test_task_cancellation_skips_remaining_tool_calls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="local", endpoint="http://unused/v1", model="x",
                roles=["primary_coder", "fast_coder", "deep_reasoner", "reviewer"], runtime="external",
            )
            config = AgentConfig(
                models=[profile],
                permissions={"probe.execute": "allow"},
                research_enabled=False,
                auto_verify_after_changes=False,
                review_after_changes=False,
            )
            tasks = TaskStore(root)
            tools = ToolRegistry(config.permissions)
            marks = []

            def cancel_now(args):
                tasks.update(tools.context.get("task_id", ""), status="cancelled")
                return "CANCELLED_TASK"

            tools.register(ToolSpec("cancel_tool", "test", {"type": "object", "properties": {}},
                                    "probe.execute", cancel_now))
            tools.register(ToolSpec("mark_tool", "test", {"type": "object", "properties": {}},
                                    "probe.execute", lambda args: marks.append(1) or "MARKED"))
            index = RepositoryIndex(root)
            index.build()
            agent = AgentOrchestrator(
                config, ModelRouter(config.models), tools, _FakeRuntime(),
                tasks=tasks, checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root), repository_index=index,
            )

            class _CancelProvider:
                def complete(self, *, messages, tools=None):
                    return ProviderResponse(message={
                        "role": "assistant", "content": "",
                        "tool_calls": [
                            {"id": "c1", "type": "function",
                             "function": {"name": "cancel_tool", "arguments": "{}"}},
                            {"id": "c2", "type": "function",
                             "function": {"name": "mark_tool", "arguments": "{}"}},
                        ],
                    }, raw={})

            agent._provider_for = lambda _: _CancelProvider()
            result = agent.run("do the thing")

            self.assertEqual(result.task["status"], "cancelled")
            self.assertEqual(marks, [])


if __name__ == "__main__":
    unittest.main()
