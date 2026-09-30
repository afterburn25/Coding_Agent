from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.provider import ProviderResponse
from localcodeagent.models.router import ModelRouter
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.training.model_growth import ModelGrowthLab
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.conversation_manager import ConversationManager
from localcodeagent.workflow.conversation_memory import ConversationMemory
from localcodeagent.workflow.knowledge_memory import KnowledgeMemory
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore


class ConversationManagerTests(unittest.TestCase):
    def test_sessions_restore_search_and_personality(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "conversations.json"
            manager = ConversationManager(path, summarize_after_messages=8)
            first = manager.active()
            manager.record_exchange("I am building a renderer", "Tell me about the renderer.")
            for i in range(4):
                manager.record_exchange(f"renderer topic {i}", f"response {i}")

            self.assertTrue(manager.active()["summary"])
            second = manager.create("Second chat")
            manager.record_exchange("database question", "database answer")
            self.assertNotEqual(first["id"], second["id"])

            hits = manager.search("renderer")
            self.assertTrue(hits)
            self.assertEqual(hits[0]["id"], first["id"])

            selected = manager.set_active(first["id"])
            self.assertEqual(selected["id"], first["id"])
            history = manager.history(limit=20)
            self.assertEqual(history[0]["role"], "user")

            personality = manager.update_personality({"humor": 81, "verbosity": 22})
            self.assertEqual(personality["humor"], 81)
            self.assertIn("humor=81", manager.personality_prompt())

            reloaded = ConversationManager(path)
            self.assertEqual(reloaded.active()["id"], first["id"])
            self.assertEqual(reloaded.personality()["humor"], 81)

    def test_feedback_is_durable(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "conversations.json"
            manager = ConversationManager(path)
            saved = manager.add_feedback(rating="down", note="Too long")
            self.assertEqual(saved["rating"], "down")
            reloaded = ConversationManager(path)
            self.assertEqual(reloaded.snapshot()["feedback_count"], 1)

    def test_timing_context_describes_elapsed_time_and_previous_conversations(self):
        with tempfile.TemporaryDirectory() as td:
            manager = ConversationManager(Path(td) / "conversations.json")
            active = manager._get()
            now = 1_800_000_000.0
            active["messages"] = [
                {"id": "u1", "role": "user", "content": "Remember the blue folder.", "timestamp": now - 7200},
                {"id": "a1", "role": "assistant", "content": "I will remember the blue folder.", "timestamp": now - 7190},
                {"id": "u2", "role": "user", "content": "What did I say earlier?", "timestamp": now - 300},
            ]
            active["updated_at"] = now - 300
            previous = manager.create("Yesterday topic")
            previous_row = manager._get(previous["id"])
            previous_row["messages"] = [
                {"id": "p1", "role": "user", "content": "We talked about the generator.", "timestamp": now - 90000},
            ]
            previous_row["updated_at"] = now - 90000
            manager.set_active(active["id"])

            context = manager.timing_context(now=now)

            self.assertIn("2 hours ago", context)
            self.assertIn("5 minutes ago", context)
            self.assertIn("Remember the blue folder", context)
            self.assertIn("Yesterday topic", context)
            self.assertIn("1 day", context)

    def test_feedback_captures_prompt_and_answer_for_training(self):
        with tempfile.TemporaryDirectory() as td:
            manager = ConversationManager(Path(td) / "conversations.json")
            manager.record_exchange("Tell me something interesting.", "Here is an interesting answer.")
            assistant = manager.active()["messages"][-1]

            saved = manager.add_feedback(message_id=assistant["id"], rating="up")

            self.assertEqual(saved["user_prompt"], "Tell me something interesting.")
            self.assertEqual(saved["assistant_response"], "Here is an interesting answer.")
            self.assertEqual(saved["message_id"], assistant["id"])
            self.assertTrue(manager.snapshot()["feedback"])


class ScopedConversationMemoryTests(unittest.TestCase):
    def test_project_and_conversation_scopes_are_isolated_and_forgettable(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user(
                "For this project, always use the project formatter",
                project_id="project-a",
                conversation_id="chat-a",
            )
            memory.learn_from_user(
                "For this conversation, always keep examples tiny",
                project_id="project-a",
                conversation_id="chat-a",
            )
            memory.learn_from_user("Remember that I prefer dark mode")

            a = memory.prompt_context(project_id="project-a", conversation_id="chat-a")
            other_project = memory.prompt_context(project_id="project-b", conversation_id="chat-a")
            other_chat = memory.prompt_context(project_id="project-a", conversation_id="chat-b")

            self.assertIn("project formatter", a)
            self.assertIn("examples tiny", a)
            self.assertIn("dark mode", a)
            self.assertNotIn("project formatter", other_project)
            self.assertNotIn("examples tiny", other_chat)
            self.assertIn("dark mode", other_project)
            self.assertIn("dark mode", other_chat)

            learned = memory.learn_from_user("Forget that project formatter")
            self.assertTrue(learned["forgotten"])
            after = memory.prompt_context(project_id="project-a", conversation_id="chat-a")
            self.assertNotIn("project formatter", after)

    def test_conversation_manager_intent_classification(self):
        self.assertEqual(ConversationManager.classify_intent("write an email to the team"), "writing")
        self.assertEqual(ConversationManager.classify_intent("teach me how recursion works"), "tutoring")
        self.assertEqual(ConversationManager.classify_intent("research the latest release"), "research")
        self.assertEqual(ConversationManager.classify_intent("build a webpage"), "coding")
        self.assertEqual(ConversationManager.classify_intent("generate a picture of a woman"), "image")
        self.assertEqual(ConversationManager.classify_intent("generate a naked woman"), "image")
        self.assertEqual(ConversationManager.classify_intent("draw a cat in a garden"), "image")
        self.assertEqual(ConversationManager.classify_intent("can you generate a picture of a woman"), "image")
        self.assertEqual(ConversationManager.classify_intent("could you please draw a dragon"), "image")
        self.assertEqual(ConversationManager.classify_intent("please paint a sunset"), "image")
        self.assertTrue(AgentOrchestrator.direct_image_generation_intent("generate a picture of a woman"))
        self.assertTrue(AgentOrchestrator.direct_image_generation_intent("draw a cat in a garden"))
        self.assertTrue(AgentOrchestrator.direct_image_generation_intent("can you generate a picture of a woman"))
        self.assertTrue(AgentOrchestrator.direct_image_generation_intent("could you please draw a dragon"))
        self.assertFalse(AgentOrchestrator.direct_image_generation_intent("edit image C:/tmp/source.png"))
        self.assertFalse(AgentOrchestrator.direct_image_generation_intent("upscale this picture"))
        self.assertFalse(AgentOrchestrator.direct_image_generation_intent("can you make this photo brighter"))
        self.assertFalse(AgentOrchestrator.direct_image_generation_intent("please make a cup of tea"))
        self.assertFalse(AgentOrchestrator.direct_image_generation_intent("please create a website with a logo"))
        self.assertEqual(ConversationManager.classify_intent("how was your day?"), "conversation")

    def test_coding_model_bypass_is_limited_to_local_or_direct_image_work(self):
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("hi"))
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("generate a picture of a woman"))
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("draw a cat in a garden"))
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("can you generate a picture of a woman"))
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("please draw a dragon"))
        self.assertFalse(AgentOrchestrator.can_run_without_coding_model("make a cup of tea"))
        self.assertFalse(AgentOrchestrator.can_run_without_coding_model("can you make this photo brighter"))
        self.assertFalse(AgentOrchestrator.can_run_without_coding_model("edit image C:/tmp/source.png"))
        self.assertFalse(AgentOrchestrator.can_run_without_coding_model("fix this Python bug"))


class KnowledgeMemoryTests(unittest.TestCase):
    def test_sourced_knowledge_keeps_provenance_and_is_reused(self):
        with tempfile.TemporaryDirectory() as td:
            memory = KnowledgeMemory(Path(td) / "knowledge.json", current_ttl_hours=12)
            row = memory.remember_research(
                "What is the current ExampleLib version?",
                "ExampleLib is 9.1 according to the retrieved release page.",
                [{
                    "id": "src1",
                    "title": "ExampleLib releases",
                    "url": "https://example.com/releases",
                    "provider": "web",
                    "reliability": "High",
                    "retrieved_at": 123.0,
                }],
            )
            self.assertIsNotNone(row)
            self.assertTrue(row["current_sensitive"])
            found = memory.lookup("What is the current ExampleLib version?")
            self.assertEqual(found["sources"][0]["url"], "https://example.com/releases")
            context = memory.prompt_context("What is the current ExampleLib version?")
            self.assertIn("ExampleLib releases", context)
            self.assertIn("https://example.com/releases", context)

    def test_unrelated_query_does_not_reuse_knowledge(self):
        with tempfile.TemporaryDirectory() as td:
            memory = KnowledgeMemory(Path(td) / "knowledge.json")
            memory.remember_research("alpha package version", "alpha 1.0", [{"title": "Alpha", "url": "https://a.example"}])
            self.assertIsNone(memory.lookup("completely unrelated weather question"))


class ModelGrowthLabTests(unittest.TestCase):
    def test_feedback_import_keeps_positive_examples_and_excludes_negative_sft_targets(self):
        with tempfile.TemporaryDirectory() as td:
            lab = ModelGrowthLab(Path(td) / "growth")
            snapshot = {
                "feedback": [
                    {
                        "rating": "up",
                        "user_prompt": "Tell me a story.",
                        "assistant_response": "A good conversational story.",
                        "conversation_id": "c1",
                        "message_id": "a1",
                    },
                    {
                        "rating": "down",
                        "user_prompt": "Tell me a joke.",
                        "assistant_response": "A bad repetitive joke.",
                        "conversation_id": "c1",
                        "message_id": "a2",
                    },
                ]
            }

            imported = lab.import_conversation_feedback(snapshot)
            self.assertEqual(imported, 2)
            positive = next(x for x in lab.candidates() if x["kind"] == "conversation_example")
            negative = next(x for x in lab.candidates() if x["kind"] == "negative_feedback")
            self.assertEqual(positive["status"], "approved")
            self.assertEqual(negative["status"], "pending")

            # Even if a negative signal were manually approved later, never use the bad answer as an SFT target.
            lab.review(negative["id"], status="approved")
            dataset = lab.export_dataset(name="feedback-test")
            rows = [json.loads(line) for line in Path(dataset["path"]).read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(any(row["messages"][0]["content"] == "Tell me a story." for row in rows))
            self.assertFalse(any(row["messages"][0]["content"] == "Tell me a joke." for row in rows))

    def test_review_export_job_registry_and_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "growth"
            lab = ModelGrowthLab(root)
            a = lab.collect(kind="behavior_rule", instruction="When asked for code", response="Explain the plan first")
            b = lab.collect(kind="sourced_knowledge", instruction="Example version?", response="9.1", source="web")
            self.assertEqual(lab.summary()["candidate_counts"]["pending"], 2)

            lab.review(a["id"], status="approved")
            lab.review(b["id"], status="approved")
            dataset = lab.export_dataset(name="personal-v1", include_knowledge=False)
            self.assertEqual(dataset["examples"], 1)
            lines = Path(dataset["path"]).read_text(encoding="utf-8").strip().splitlines()
            payload = json.loads(lines[0])
            self.assertEqual(payload["messages"][1]["content"], "Explain the plan first")

            job = lab.create_training_job(
                base_model_id="qwen3-14b",
                dataset_path=dataset["path"],
                method="lora",
                output_name="ChatNexus-Personal-v1",
            )
            self.assertEqual(job["status"], "planned")
            self.assertTrue(lab.jobs())

            candidate = lab.register_candidate(
                job_id=job["id"],
                base_model_id="qwen3-14b",
                artifact_path=str(root / "adapters" / "personal.gguf"),
                metrics={"quality": 0.91},
            )
            with self.assertRaises(ValueError):
                lab.promote(candidate["id"])
            evaluated = lab.evaluate(candidate["id"], passed=True, metrics={"quality": 0.93})
            self.assertEqual(evaluated["status"], "evaluated")
            promoted = lab.promote(candidate["id"])
            self.assertEqual(promoted["status"], "active")
            self.assertEqual(lab.registry()["active_candidate_id"], candidate["id"])
            rolled = lab.rollback()
            self.assertEqual(rolled["active_candidate_id"], "")


class _FakeRuntime:
    def refresh_hardware(self):
        return None

    def ensure_ready(self, profile):
        return profile.endpoint

    def recover(self, profile):
        return profile.endpoint


class _CaptureProvider:
    def __init__(self):
        self.messages = []

    def complete(self, *, messages, tools=None):
        self.messages = list(messages)
        return ProviderResponse(message={"role": "assistant", "content": "Answer based on fresh research."}, raw={})


class _FakeResearch:
    def __init__(self):
        self.calls = 0

    def plan(self, task, mode="auto"):
        return SimpleNamespace(needed=True, mode="balanced")

    def research_topic(self, query, *, mode="auto", version=""):
        self.calls += 1
        return {
            "id": "research1",
            "status": "completed",
            "summary": "Research Summary: ExampleLib 9.1 is the current release.",
            "sources": [{
                "id": "source1",
                "title": "Official ExampleLib release",
                "url": "https://example.com/releases/9.1",
                "provider": "web",
                "reliability": "High",
                "retrieved_at": 1000.0,
            }],
            "findings": ["ExampleLib 9.1"],
        }

    def prepare_task(self, task, mode="auto"):
        raise AssertionError("fresh auto research should replace repository-only preflight")


class ModelGrowthExecutionTests(unittest.TestCase):
    def test_external_trainer_job_can_execute_and_finish(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = ModelGrowthLab(root / "growth")
            dataset = root / "train.jsonl"
            dataset.write_text('{"messages":[]}\n', encoding="utf-8")
            script = root / "fake_trainer.py"
            script.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "out=Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)\n"
                "(out/'adapter.gguf').write_text('fake',encoding='utf-8')\n",
                encoding="utf-8",
            )
            command = f'"{sys.executable}" "{script}" "{{dataset}}" "{{output}}"'
            job = lab.create_training_job(
                base_model_id="qwen3-14b",
                dataset_path=str(dataset),
                method="lora",
                output_name="execution-test",
                trainer_command=command,
            )
            started = lab.start_training_job(job["id"])
            self.assertEqual(started["status"], "running")

            deadline = time.time() + 5
            final = None
            while time.time() < deadline:
                rows = {row["id"]: row for row in lab.jobs()}
                final = rows[job["id"]]
                if final["status"] in {"finished", "failed"}:
                    break
                time.sleep(0.05)

            self.assertIsNotNone(final)
            self.assertEqual(final["status"], "finished", msg=lab.training_log(job["id"]))
            self.assertTrue((Path(final["output_dir"]) / "adapter.gguf").is_file())


class AutomaticResearchLearningTests(unittest.TestCase):
    def test_current_question_researches_saves_and_injects_sourced_knowledge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = ModelProfile(
                id="fast",
                endpoint="http://unused/v1",
                model="fast",
                roles=["utility", "fast_coder", "primary_coder"],
                runtime="external",
            )
            config = AgentConfig(
                models=[profile],
                permissions={},
                research_enabled=True,
                research_mode="balanced",
                auto_research_unknown=True,
                auto_verify_after_changes=False,
                review_after_changes=False,
            )
            index = RepositoryIndex(root)
            index.build()
            conversation_memory = ConversationMemory(root / "conversation_memory.json")
            conversations = ConversationManager(root / "conversations.json")
            knowledge = KnowledgeMemory(root / "knowledge.json")
            growth = ModelGrowthLab(root / "growth")
            research = _FakeResearch()
            provider = _CaptureProvider()
            agent = AgentOrchestrator(
                config,
                ModelRouter(config.models),
                ToolRegistry(config.permissions),
                _FakeRuntime(),
                tasks=TaskStore(root),
                checkpoints=CheckpointManager(root),
                memory=ProjectMemory(root),
                repository_index=index,
                research=research,
                conversation_memory=conversation_memory,
                conversation_manager=conversations,
                knowledge_memory=knowledge,
                model_growth=growth,
            )
            agent._provider_for = lambda _: provider

            result = agent.run("What is the current ExampleLib version?")

            self.assertEqual(result.task["status"], "completed")
            self.assertEqual(research.calls, 1)
            self.assertIsNotNone(knowledge.lookup("What is the current ExampleLib version?"))
            system_text = "\n".join(m.get("content", "") for m in provider.messages if m.get("role") == "system")
            self.assertIn("Previously researched sourced knowledge", system_text)
            self.assertIn("https://example.com/releases/9.1", system_text)
            self.assertGreaterEqual(growth.summary()["candidate_counts"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()
