"""Continuous conversation QA harness (backlog §5-§7).

Deterministic multi-turn scenarios through AgentOrchestrator with a
scripted provider — asserting what the orchestrator actually fed the
model, which memory it injected, and how tasks closed. Failures land in
a FailureCorpus; discovered failures become permanent scenarios here.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.router import ModelRouter
from localcodeagent.qa import (
    ConversationQaRunner,
    FailureCorpus,
    CorpusEntry,
    QaScenario,
    QaTurn,
    ScriptedProvider,
    generate_scenarios,
)
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.conversation_manager import ConversationManager
from localcodeagent.workflow.conversation_memory import ConversationMemory
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore


class _FakeRuntime:
    def refresh_hardware(self):
        return None

    def fresh_hardware(self, max_age_s: float = 15.0):
        return self.refresh_hardware()

    def ensure_ready(self, profile):
        return profile.endpoint

    def recover(self, profile):
        return profile.endpoint


class _StubResearch:
    def plan(self, text, mode="auto"):
        class _Plan:
            needed = False
        return _Plan()

    def prepare_task(self, text, mode="auto"):
        return {"guidance": "preflight"}

    def research_topic(self, query, **kwargs):
        return {"status": "completed", "summary": "", "sources": [],
                "findings": [], "errors": []}


def _make(root: Path, *, provider: ScriptedProvider | None = None):
    profile = ModelProfile(
        id="local", endpoint="http://unused/v1", model="x",
        roles=["utility", "fast_coder", "primary_coder", "general"],
        runtime="external")
    config = AgentConfig(
        models=[profile], permissions={}, research_enabled=False,
        auto_verify_after_changes=False, review_after_changes=False)
    provider = provider or ScriptedProvider()
    conversations = ConversationManager(root / "conversations.json")
    agent = AgentOrchestrator(
        config, ModelRouter(config.models), ToolRegistry(config.permissions),
        _FakeRuntime(),
        tasks=TaskStore(root), checkpoints=CheckpointManager(root),
        memory=ProjectMemory(root), repository_index=RepositoryIndex(root),
        research=_StubResearch(),
        conversation_memory=ConversationMemory(root / "cm.json"),
        conversation_manager=conversations,
    )
    agent._provider_for = lambda _: provider
    return agent, provider, conversations


class HandAuthoredScenarioTests(unittest.TestCase):
    def test_typo_scenario_model_sees_corrected_text(self):
        """The real 'feticcinii' failure: the model must receive
        'fettuccine', never the misspelling, and the task must complete."""
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            scenario = QaScenario("typo-fettuccine", [
                QaTurn("give me a recipe for crawfish feticcinii", expect={
                    "user_contains": "fettuccine",
                    "user_not_contains": "feticcinii",
                    "task_status": "completed",
                }),
                QaTurn("whats the wether going to be", expect={
                    "user_contains": ["what's", "weather"],
                    "user_not_contains": "wether",
                }),
                QaTurn("how much ram dose nexus use", expect={
                    "user_contains": "does",
                    "user_not_contains": " dose ",
                }),
            ], default_conversation_id="typo-chat")
            run = runner.run(scenario)
            self.assertTrue(run.ok, run.failures)

    def test_typo_never_mangles_protected_content(self):
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            scenario = QaScenario("typo-protected", [
                QaTurn("open https://exmaple.com/page for me", expect={
                    "user_contains": "https://exmaple.com/page",
                }),
            ])
            run = runner.run(scenario)
            self.assertTrue(run.ok, run.failures)

    def test_cross_chat_memory_scenario(self):
        """§3 end-to-end through the real pipeline: declarative fact taught
        in chat A surfaces in chat B; an update in chat C supersedes it."""
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            scenario = QaScenario("orion-db", [
                QaTurn("Project Orion uses PostgreSQL.",
                       conversation_id="chat-a",
                       expect={"task_status": "completed"}),
                QaTurn("what database did I say Orion uses?",
                       conversation_id="chat-b",
                       expect={"system_contains": "PostgreSQL"}),
                QaTurn("We switched Orion to SQLite.",
                       conversation_id="chat-c",
                       expect={"task_status": "completed"}),
                QaTurn("what database does Orion use now?",
                       conversation_id="chat-d",
                       expect={
                           "system_contains": "SQLite",
                           "system_not_contains": "PostgreSQL",
                       }),
            ])
            run = runner.run(scenario)
            self.assertTrue(run.ok, run.failures)

    def test_memory_intrusion_scenario(self):
        """§4: unrelated stored facts must not be injected into an
        unrelated conversation."""
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            scenario = QaScenario("memory-intrusion", [
                QaTurn("remember that my cat's name is Whiskers",
                       conversation_id="chat-a"),
                QaTurn("Project Orion uses PostgreSQL.",
                       conversation_id="chat-a"),
                QaTurn("how do I poach an egg?", conversation_id="chat-b",
                       expect={
                           "system_not_contains": ["Whiskers", "PostgreSQL"],
                       }),
                QaTurn("what did I name my cat?", conversation_id="chat-b",
                       expect={"system_contains": "Whiskers"}),
            ])
            run = runner.run(scenario)
            self.assertTrue(run.ok, run.failures)

    def test_mixed_topic_long_conversation(self):
        """20-turn mixed-domain conversation: every turn completes and no
        unrelated memory leaks into prompts."""
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            scenario = QaScenario("mixed-20", [
                QaTurn("Project Meridian uses MongoDB."),
                QaTurn("why is the sky blue",
                       expect={"system_not_contains": "MongoDB"}),
                QaTurn("what is 17 times 24",
                       expect={"system_not_contains": "MongoDB"}),
                QaTurn("give me a recipe for chiken soup",
                       expect={"user_contains": "chicken"}),
                QaTurn("what database does Meridian use?",
                       expect={"system_contains": "MongoDB"}),
                QaTurn("how do I reverse a string in python",
                       expect={"system_not_contains": "MongoDB"}),
                QaTurn("what was the printing press's effect on Europe",
                       expect={"system_not_contains": "MongoDB"}),
                QaTurn("my laptop fan runs loud even when idle, what gives"),
                QaTurn("compare postgres and sqlite for a small desktop app"),
                QaTurn("what would happen if the moon disappeared"),
                QaTurn("help me write a thank-you note to my neighbor"),
                QaTurn("explain what a race condition is"),
                QaTurn("anyway, changing the subject — how do vaccines work"),
                QaTurn("reccomend a good mystery novel",
                       expect={"user_contains": "recommend"}),
                QaTurn("how do I get a coffee stain out of a shirt"),
                QaTurn("what does idempotent mean for an API"),
                QaTurn("how do I compute compound interest"),
                QaTurn("different question — who first circumnavigated the globe"),
                QaTurn("what database does Meridian use?",
                       expect={"system_contains": "MongoDB"}),
                QaTurn("my printer says offline but it's on"),
            ], default_conversation_id="mixed-chat")
            run = runner.run(scenario)
            self.assertTrue(run.ok, run.failures)


class SeededGenerationTests(unittest.TestCase):
    """Generated scenarios are deterministic by seed and complete cleanly."""

    def test_same_seed_same_scenario(self):
        a = generate_scenarios(42, turns=8, count=2)
        b = generate_scenarios(42, turns=8, count=2)
        self.assertEqual(
            [t.text for s in a for t in s.turns],
            [t.text for s in b for t in s.turns])

    def test_generated_conversations_complete(self):
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            for scenario in generate_scenarios(7, turns=10, count=3):
                run = runner.run(scenario)
                self.assertTrue(run.ok, run.failures)


class FailureCorpusTests(unittest.TestCase):
    def test_failures_recorded_and_closeable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            corpus = FailureCorpus(root / "qa_corpus.jsonl")
            agent, provider, convos = _make(root)
            runner = ConversationQaRunner(
                agent, provider, conversation_manager=convos, corpus=corpus)
            scenario = QaScenario("deliberate-fail", [
                QaTurn("hello there", expect={"response_contains": "banana"}),
            ])
            run = runner.run(scenario)
            self.assertFalse(run.ok)
            open_rows = corpus.open_regressions()
            self.assertEqual(len(open_rows), 1)
            self.assertEqual(open_rows[0]["scenario_id"], "deliberate-fail")
            self.assertEqual(open_rows[0]["category"], "response")

            corpus.mark_fixed(
                lambda row: row["scenario_id"] == "deliberate-fail",
                commit="abc123", version="0.29.0")
            self.assertEqual(corpus.open_regressions(), [])
            fixed = corpus.entries(status="fixed")
            self.assertEqual(fixed[0]["fixed_commit"], "abc123")

    def test_corpus_entry_schema(self):
        with tempfile.TemporaryDirectory() as td:
            corpus = FailureCorpus(Path(td) / "c.jsonl")
            corpus.record(CorpusEntry(
                category="typo_misunderstanding",
                conversation=[{"role": "user", "content": "feticcinii"}],
                failed_turn_index=0, user_text="feticcinii",
                expected="fettuccine", actual="etouffee detour",
                seed=5, scenario_id="s1"))
            row = corpus.entries()[0]
            for key in ("category", "conversation", "seed", "expected",
                        "actual", "status", "created_at"):
                self.assertIn(key, row)


if __name__ == "__main__":
    unittest.main()
