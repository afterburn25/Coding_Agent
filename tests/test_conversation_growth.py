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
            prompt = manager.personality_prompt()
            self.assertIn("dry, playful humor", prompt)
            self.assertIn("natural contractions", prompt)
            self.assertIn("Do not end most responses with a question", prompt)
            self.assertIn("capable adult conversational partner", manager.conversation_quality_prompt())

            reloaded = ConversationManager(path)
            self.assertEqual(reloaded.active()["id"], first["id"])
            self.assertEqual(reloaded.personality()["humor"], 81)

    def test_explicit_general_fact_learning_commands_are_durable(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "conversation_memory.json"
            memory = ConversationMemory(path)
            learned = memory.learn_from_user("Learn that Europa has a subsurface ocean")
            self.assertIn("Europa has a subsurface ocean", learned["facts"])
            learned2 = memory.learn_from_user("Fact: the project codename is Orion")
            self.assertIn("the project codename is Orion", learned2["facts"])
            context = memory.prompt_context()
            self.assertIn("Europa has a subsurface ocean", context)
            self.assertIn("project codename is Orion", context)

    def test_locked_identity_questions_do_not_raise_refusal(self):
        """Asking about a locked fact is a question, not a write attempt —
        no 'locked' key may appear so the normal answer lanes run."""
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "cm.json")
            for q in (
                "how old are you?",
                "when is your birthday?",
                "who is your creator?",
                "what is your age",
            ):
                learned = memory.learn_from_user(q)
                self.assertNotIn("locked", learned, q)
                self.assertEqual(learned["facts"], [], q)
                self.assertEqual(learned["behavior_rules"], [], q)

    def test_locked_identity_write_attempts_still_refuse(self):
        """Deliberate overwrite attempts keep the lock notice."""
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "cm.json")
            for q in (
                "remember that your birthday is June 1",
                "learn: your creator is Bob",
                "forget your birthday",
                "no, your age is 40",
                "fact: your birthday is June 1",       # captured-content guard
                "teach: your creator is someone else",  # rule-pattern guard
            ):
                learned = memory.learn_from_user(q)
                self.assertIn("locked", learned, q)
                self.assertIn("creator-locked", learned["locked"][0], q)

    def test_user_own_facts_not_locked(self):
        """'my birthday' is the user's fact, not Nexus's — stores normally."""
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "cm.json")
            learned = memory.learn_from_user("my birthday is June 1")
            self.assertNotIn("locked", learned)
            self.assertTrue(any("june 1" in f.lower() for f in learned["facts"]))

    def test_history_survives_truncated_or_missing_main_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "conversations.json"
            manager = ConversationManager(path)
            # Two exchanges: the rolling .bak lags one save behind, so the
            # first exchange is what survives a clobbered main file.
            manager.record_exchange("remember this", "stored reply")
            manager.record_exchange("second turn", "second reply")
            conv_id = manager.active()["id"]

            # Simulate a bad overwrite wiping the main file.
            path.write_text('{"version":1,"conversations":[]}', encoding="utf-8")
            reloaded = ConversationManager(path)
            self.assertEqual(reloaded.active()["id"], conv_id)
            self.assertIn("remember this", str(reloaded.active()["messages"]))

            # Corrupt main file falls back to the backup too.
            path.write_text("{not json", encoding="utf-8")
            reloaded2 = ConversationManager(path)
            self.assertEqual(reloaded2.active()["id"], conv_id)

    def test_exchange_records_image_job_ids_on_assistant_message(self):
        with tempfile.TemporaryDirectory() as td:
            manager = ConversationManager(Path(td) / "conversations.json")
            manager.record_exchange("draw a castle", "Image generation started.",
                                    image_job_ids=["job-abc123"])
            msgs = manager.active()["messages"]
            self.assertEqual(msgs[-1]["role"], "assistant")
            self.assertEqual(msgs[-1]["image_job_ids"], ["job-abc123"])

            reloaded = ConversationManager(Path(td) / "conversations.json")
            self.assertEqual(reloaded.active()["messages"][-1]["image_job_ids"], ["job-abc123"])

    def test_exchange_without_images_leaves_field_absent(self):
        with tempfile.TemporaryDirectory() as td:
            manager = ConversationManager(Path(td) / "conversations.json")
            manager.record_exchange("hello", "hi there")
            self.assertNotIn("image_job_ids", manager.active()["messages"][-1])

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

            first_recall = memory.prompt_context(project_id="project-a", conversation_id="chat-a")
            second_recall = memory.prompt_context(project_id="project-a", conversation_id="chat-a")
            first_cue = next(line for line in first_recall.splitlines() if line.startswith("Recall expression cue for this turn:"))
            second_cue = next(line for line in second_recall.splitlines() if line.startswith("Recall expression cue for this turn:"))
            self.assertNotEqual(first_cue, second_cue)
            self.assertIn("canonical meaning", first_recall)
            self.assertIn("verbatim", first_recall)
            self.assertIn("names, dates", first_recall)

            learned = memory.learn_from_user("Forget that project formatter")
            self.assertTrue(learned["forgotten"])
            after = memory.prompt_context(project_id="project-a", conversation_id="chat-a")
            self.assertNotIn("project formatter", after)

    def test_option_selection_resolves_against_pending_proposal(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.record_exchange(
                "fix it",
                "What do you want to do?\n"
                "- **Option 1**: Run a deeper diagnostic to find the root cause.\n"
                "- **Option 2**: Full rebuild — wipe and start over.\n"
                "- **Option 3**: Manual patch — risky.")
            resolved = memory.resolve_option_selection("option 1")
            self.assertIsNotNone(resolved)
            self.assertIn("Option 1", resolved)
            self.assertIn("deeper diagnostic", resolved)
            self.assertIn("Option 2",
                          memory.resolve_option_selection("the second option"))
            self.assertIn("Option 3",
                          memory.resolve_option_selection("go with option 3"))
            self.assertIn("Option 2", memory.resolve_option_selection("2"))
            self.assertIn("Option 1",
                          memory.resolve_option_selection("first one"))
            # Out of range and non-selections are left alone
            self.assertIsNone(memory.resolve_option_selection("option 9"))
            self.assertIsNone(
                memory.resolve_option_selection("what do you mean?"))
            self.assertIsNone(memory.resolve_option_selection(
                "tell me more about option 1 first"))

    def test_option_selection_empty_without_pending_proposal(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.record_exchange("hi", "hello — how can I help?")
            self.assertIsNone(memory.resolve_option_selection("option 1"))
            self.assertIsNone(memory.resolve_option_selection("1"))

    def test_pending_options_refresh_and_persist(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.json"
            memory = ConversationMemory(path)
            memory.record_exchange(
                "pick one", "1. alpha path\n2. beta path")
            # A reply without options clears the stale proposal
            memory.record_exchange("ok", "sounds good")
            self.assertIsNone(memory.resolve_option_selection("option 1"))
            # New proposal persists across a memory reload (restart)
            memory.record_exchange(
                "which", "Option 1: redo it\nOption 2: keep it")
            reloaded = ConversationMemory(path)
            self.assertIn("redo it",
                          reloaded.resolve_option_selection("option 1"))

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
        # Source-image ops route directly now — the attachment becomes
        # source_image and the image lane infers upscale/edit, so the job
        # can never be stranded on a model that just promises it.
        self.assertTrue(AgentOrchestrator.direct_image_generation_intent("upscale this picture"))
        self.assertTrue(AgentOrchestrator.direct_image_generation_intent("can you make this photo brighter"))
        self.assertTrue(AgentOrchestrator.direct_image_generation_intent("recreate this image with a wider shot"))
        self.assertFalse(AgentOrchestrator.direct_image_generation_intent("please make a cup of tea"))
        self.assertFalse(AgentOrchestrator.direct_image_generation_intent("please create a website with a logo"))
        self.assertEqual(ConversationManager.classify_intent("how was your day?"), "conversation")

    def test_image_prompt_refinement_strips_scaffolding(self):
        r = ConversationManager.refine_image_prompt
        self.assertEqual(r("please generate an image of a red cat"), "a red cat")
        self.assertEqual(r("can you make me a picture of a sunset over the ocean"),
                         "a sunset over the ocean")
        self.assertEqual(r("hey nexus make me an image of a dragon"), "a dragon")
        # Already-descriptive prompts pass through untouched.
        self.assertEqual(r("a woman in a red suit"), "a woman in a red suit")
        self.assertEqual(r("a cat with a hat"), "a cat with a hat")

    def test_negative_prompt_extraction(self):
        s = ConversationManager.split_negative_prompt
        self.assertEqual(s("a woman in a red suit, no watermark, without jewelry"),
                         ("a woman in a red suit", "watermark, jewelry"))
        self.assertEqual(s("a portrait, negative prompt: blurry, low quality"),
                         ("a portrait", "blurry, low quality"))
        self.assertEqual(s("a garden but no flowers"), ("a garden", "flowers"))
        # 'with' is inclusion, not exclusion.
        self.assertEqual(s("a cat with a hat"), ("a cat with a hat", ""))
        # An all-negative prompt keeps the original so the job has signal.
        pos, neg = s("no people, no text")
        self.assertTrue(pos)
        self.assertEqual(neg, "people, text")

    def test_coding_model_bypass_is_limited_to_local_or_direct_image_work(self):
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("hi"))
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("generate a picture of a woman"))
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("draw a cat in a garden"))
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("can you generate a picture of a woman"))
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("please draw a dragon"))
        self.assertFalse(AgentOrchestrator.can_run_without_coding_model("make a cup of tea"))
        # Source-image ops bypass the model too — the attachment is routed
        # as source_image and the image lane infers the edit op.
        self.assertTrue(AgentOrchestrator.can_run_without_coding_model("can you make this photo brighter"))
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

    def fresh_hardware(self, max_age_s: float = 15.0):
        return self.refresh_hardware()

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

    def research_topic(self, query, *, mode="auto", version="", scope="auto",
                       queries=None, urls=None, event=None, is_cancelled=None):
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


class CrossChatMemoryTests(unittest.TestCase):
    """§3 — declarative facts must persist across conversations with
    recency/supersession; §4 — unrelated facts must not ride every prompt."""

    def test_declarative_fact_captured_and_recalled(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            learned = memory.learn_from_user(
                "Project Orion uses PostgreSQL.", conversation_id="chat-a")
            self.assertTrue(learned["facts"], "declarative fact was not captured")
            self.assertIn("Orion uses PostgreSQL", learned["facts"][0])

            # Chat B — a different conversation, same global scope.
            context = memory.prompt_context(
                "What database did I say Orion uses?", conversation_id="chat-b")
            self.assertIn("PostgreSQL", context)

    def test_newer_fact_supersedes_older(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user("Project Orion uses PostgreSQL.")
            memory.learn_from_user("We switched Orion to SQLite.")

            # Old fact retired (marked, not deleted).
            rows = memory.snapshot()["facts"]
            pg = [r for r in rows if "PostgreSQL" in r["text"]]
            self.assertTrue(pg and not pg[0]["active"])
            self.assertTrue(pg[0].get("superseded"))
            self.assertEqual(pg[0].get("superseded_by"),
                             rows[-1]["id"])

            context = memory.prompt_context("What database does Orion use now?")
            self.assertIn("SQLite", context)
            self.assertNotIn("PostgreSQL", context)

            # History/provenance: the old row is still stored.
            self.assertTrue(any("PostgreSQL" in r["text"] for r in rows))

    def test_discourse_prefixed_and_moved_forms_capture_and_supersede(self):
        """§24-found gaps: discourse prefixes, 'moved/migrated to', and
        'off X to Y' forms all canonicalize into the same slot so the
        newest statement supersedes regardless of phrasing."""
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user("Project Orion uses PostgreSQL.")
            memory.learn_from_user("actually, Orion uses MySQL now")
            memory.learn_from_user("Orion moved to Redis")
            memory.learn_from_user("by the way, Orion uses Cassandra")
            rows = memory.snapshot()["facts"]
            active = [r["text"] for r in rows if r["active"]]
            self.assertEqual(active, ["Orion uses Cassandra"])
            # Every earlier form retired into the same slot.
            self.assertTrue(all(r.get("slot") == "orion:uses"
                                for r in rows[:4]))
            ctx = memory.prompt_context("what database does Orion use?")
            self.assertIn("Cassandra", ctx)
            self.assertNotIn("MySQL now", ctx)  # trailing adverb stripped

            memory2 = ConversationMemory(Path(td) / "m2.json")
            memory2.learn_from_user(
                "we migrated Atlas off Postgres to Redis")
            self.assertEqual(memory2.snapshot()["facts"][0]["text"],
                             "Atlas uses Redis")

    def test_decision_statements_capture_and_supersede(self):
        """§3/§8 — 'we decided to use X for Y' and kin must land in the
        same supersession slot so a revised decision retires the old."""
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user("we decided to use SQLite for the store")
            memory.learn_from_user("the plan is Postgres for production")
            memory.learn_from_user("we settled on Rust for the agent")
            memory.learn_from_user("we agreed the API should stay REST")
            texts = [r["text"] for r in memory.snapshot()["facts"]]
            self.assertIn("store uses SQLite", texts)
            self.assertIn("production uses Postgres", texts)
            self.assertIn("agent uses Rust", texts)
            self.assertIn("API should stay REST", texts)

            # A revised decision retires the earlier one.
            memory.learn_from_user("we decided to use MySQL for the store")
            active = [r["text"] for r in memory.snapshot()["facts"]
                      if r["active"]]
            self.assertIn("store uses MySQL", active)
            self.assertNotIn("store uses SQLite", active)

            # Action decisions and option refs are not facts.
            memory2 = ConversationMemory(Path(td) / "m2.json")
            for noise in ("we decided to refactor the auth module",
                          "let us go with option B for the parser"):
                self.assertEqual(memory2.learn_from_user(noise)["facts"], [])

    def test_corrections_supersede_unique_fact(self):
        """'X not Y' corrections rewrite the fact carrying Y — but only
        when Y identifies exactly one active fact; ambiguous or absent
        referents must never guess."""
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user("Project Orion uses PostgreSQL.")
            memory.learn_from_user("remember that my editor is vim")

            learned = memory.learn_from_user(
                "actually it was SQLite not PostgreSQL")
            self.assertEqual(learned["facts"], ["Orion uses SQLite"])
            rows = memory.snapshot()["facts"]
            pg = [r for r in rows if "PostgreSQL" in r["text"]]
            self.assertFalse(pg[0]["active"])

            learned = memory.learn_from_user("it was emacs not vim")
            self.assertEqual(learned["facts"], ["my editor is emacs"])

            # No Oracle fact exists — a correction must never guess.
            self.assertEqual(
                memory.learn_from_user("it was MySQL not Oracle")["facts"],
                [])

            # Explicit correction bodies route through the normal
            # canonicalization pipeline.
            learned = memory.learn_from_user(
                "no, i meant the store uses Redis")
            self.assertEqual(learned["facts"], ["store uses Redis"])
            learned = memory.learn_from_user(
                "correction: the port is 5433")
            self.assertEqual(learned["facts"], ["the port is 5433"])

    def test_forget_variants_retire_the_referent(self):
        """'forget about X' / 'stop remembering X' retire the fact —
        'the'/'my'/'about' filler must not break the substring probe,
        and bare 'forget the facts' must not nuke memory."""
        for phrasing in (
            "forget that my editor is emacs",
            "forget about the editor",
            "nevermind about my editor",
            "stop remembering my editor",
            "delete the fact about my editor",
        ):
            with tempfile.TemporaryDirectory() as td:
                memory = ConversationMemory(Path(td) / "memory.json")
                memory.learn_from_user("remember that my editor is emacs")
                memory.learn_from_user("Project Orion uses SQLite.")
                out = memory.learn_from_user(phrasing)
                self.assertEqual(
                    [r["text"] for r in out["forgotten"]],
                    ["my editor is emacs"], phrasing)
                active = [f["text"] for f in memory.snapshot()["facts"]
                          if f["active"]]
                self.assertEqual(active, ["Orion uses SQLite"], phrasing)

        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user("remember that my editor is emacs")
            out = memory.learn_from_user("forget the facts")
            self.assertEqual(out["forgotten"], [])
            out = memory.learn_from_user(
                "remember to forget nothing important")
            self.assertEqual(out["forgotten"], [])
            self.assertTrue(memory.snapshot()["facts"][0]["active"])

    def test_rule_revocation_retires_mandates_not_prohibitions(self):
        """'stop X' lifts a mandate; 'do not X anymore' must never retire
        a rule that already prohibits X, and stores the prohibition when
        nothing was mandated."""
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user("always respond in JSON")
            memory.learn_from_user("never use emojis")

            out = memory.learn_from_user("stop responding in JSON")
            self.assertEqual([r["text"] for r in out["forgotten"]],
                             ["Always respond in JSON"])
            # The matching prohibition must survive untouched.
            active = [r["text"] for r in memory.snapshot()["behavior_rules"]
                      if r["active"]]
            self.assertEqual(active, ["Never use emojis"])

        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user("do not use emojis anymore")
            rules = [r["text"] for r in memory.snapshot()["behavior_rules"]]
            self.assertEqual(rules, ["Never use emojis"])

            # Vague revocations must not wipe rules.
            memory.learn_from_user("always respond politely")
            out = memory.learn_from_user("stop it")
            self.assertEqual(out["forgotten"], [])
            self.assertTrue(all(
                r["active"] for r in memory.snapshot()["behavior_rules"]))

    def test_update_forms_of_the_same_slot(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user("My editor is vim")
            memory.learn_from_user("my editor is emacs")
            rows = memory.snapshot()["facts"]
            self.assertEqual(len([r for r in rows if r["active"]]), 1)
            self.assertIn("emacs", rows[-1]["text"])

            memory2 = ConversationMemory(Path(td) / "m2.json")
            memory2.learn_from_user("my editor is vim")
            memory2.learn_from_user("i use slack")  # no slot — must not clobber
            active = [r["text"] for r in memory2.snapshot()["facts"]
                      if r["active"]]
            self.assertEqual(len(active), 2)

    def test_memory_intrusion_gated_recall(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user("Project Orion uses PostgreSQL.")
            memory.learn_from_user("remember that my cat's name is Whiskers")
            memory.learn_from_user("Project Falcon runs on Kubernetes")

            # Unrelated question drags in nothing.
            ctx = memory.prompt_context("how do I cook risotto?")
            self.assertEqual(ctx, "")

            # Question about one project pulls only that fact.
            ctx = memory.prompt_context("What does Orion use for storage?")
            self.assertIn("PostgreSQL", ctx)
            self.assertNotIn("Whiskers", ctx)
            self.assertNotIn("Falcon", ctx)

            # Empty query = full memory view (explicit recall/management).
            ctx = memory.prompt_context()
            self.assertIn("PostgreSQL", ctx)
            self.assertIn("Whiskers", ctx)
            self.assertIn("Falcon", ctx)

    def test_scoped_facts_still_relevance_gated(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            memory.learn_from_user(
                "For this project, the test database uses port 5433",
                project_id="p1", conversation_id="c1")
            # Right project, unrelated question — no injection.
            ctx = memory.prompt_context(
                "make me a sandwich", project_id="p1", conversation_id="c1")
            self.assertNotIn("5433", ctx)
            # Right project + related question — surfaced.
            ctx = memory.prompt_context(
                "what port is the test database on?",
                project_id="p1", conversation_id="c1")
            self.assertIn("5433", ctx)

    def test_questions_and_pronouns_not_captured_as_facts(self):
        with tempfile.TemporaryDirectory() as td:
            memory = ConversationMemory(Path(td) / "memory.json")
            for utterance in (
                "does Orion use PostgreSQL?",
                "what database does Orion use?",
                "it uses a lot of memory",
                "tell me what Orion uses",
                "can this app use SQLite?",
            ):
                learned = memory.learn_from_user(utterance)
                self.assertFalse(learned["facts"], utterance)
