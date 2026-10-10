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
from localcodeagent.tools.base import ToolRegistry, ToolSpec
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
    registry = ToolRegistry({"filesystem.read": "allow", "filesystem.write": "allow"})
    registry.register(ToolSpec(
        "write_file", "Write a file", {"type": "object"},
        "filesystem.write", lambda a: "ok"))
    registry.register(ToolSpec(
        "read_file", "Read a file", {"type": "object"},
        "filesystem.read", lambda a: "ok"))
    registry.register(ToolSpec(
        "run_command", "Run a command", {"type": "object"},
        "filesystem.read", lambda a: "ok"))
    agent = AgentOrchestrator(
        config, ModelRouter(config.models), registry,
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
                       # Deterministic recall lane now owns "what did
                       # I say about X" — assert the answer, not the
                       # (absent) model prompt.
                       expect={"response_contains": "PostgreSQL"}),
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
                       # First-person recall ('what did i …') is owned by
                       # the deterministic fact lane now — assert the
                       # answer, not the (absent) model prompt.
                       expect={"response_contains": "Whiskers"}),
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

    def test_memory_lifecycle_end_to_end(self):
        """Teach → recall → correct → supersede → rule → revoke →
        is-fact → correction-supersession → forget, all through run():
        the retired value must disappear from later prompts."""
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            scenario = QaScenario("lifecycle-14", [
                QaTurn("Project Vega uses Cassandra."),
                QaTurn("what database does Vega use?",
                       expect={"system_contains": "Cassandra"}),
                QaTurn("actually it was FoundationDB not Cassandra"),
                QaTurn("what database does Vega use?",
                       expect={"system_contains": "FoundationDB",
                               "system_not_contains": "Cassandra"}),
                QaTurn("always answer in bullet points"),
                QaTurn("what color is the sky"),
                QaTurn("stop answering in bullet points"),
                QaTurn("the staging port is 8443"),
                QaTurn("what port is staging on",
                       # Recall lane answers deterministically — the
                       # assert moves from prompt to response.
                       expect={"response_contains": "8443"}),
                QaTurn("correction: the staging port is 9443"),
                QaTurn("what port is staging on",
                       expect={"response_contains": "9443",
                               "response_not_contains": "8443"}),
                QaTurn("forget about the staging port"),
                QaTurn("what port is staging on",
                       expect={"system_not_contains": "9443"}),
                QaTurn("what database does Vega use?",
                       expect={"system_contains": "FoundationDB"}),
            ], default_conversation_id="lc")
            run = runner.run(scenario)
            self.assertTrue(run.ok, run.failures)


class ToolUseQaTests(unittest.TestCase):
    """§8/§9 — action requests must reach the model with tools; pure
    questions must not. Coding context survives topic switches."""

    def test_action_requests_offer_tools_questions_do_not(self):
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            scenario = QaScenario("tool-gating", [
                # Action-shaped but outside the deterministic
                # local-action grammar — the model lane must own this
                # turn so tool-offering can be measured.
                QaTurn("save notes.txt into the project",
                       expect={"tool_calls": True}),
                QaTurn("why is the sky blue", expect={"no_tool_calls": True}),
                QaTurn("fix the bug in parser.py", expect={"tool_calls": True}),
                QaTurn("what is 2+2", expect={"no_tool_calls": True}),
                QaTurn("run the tests", expect={"tool_calls": True}),
                QaTurn("how do I poach an egg",
                       expect={"no_tool_calls": True}),
            ])
            run = runner.run(scenario)
            self.assertTrue(run.ok, run.failures)

    def test_narration_without_execution_retriggers_tool_call(self):
        """§9: a model that narrates a plan instead of calling a tool gets
        re-prompted with tool_choice='required' — it can't talk its way out
        of executing an action request."""
        with tempfile.TemporaryDirectory() as td:
            provider = ScriptedProvider(
                script=["I'll create the file notes.txt for you now."])
            agent, provider, convos = _make(Path(td), provider=provider)
            agent.run("save notes.txt into the project")
            self.assertGreaterEqual(len(provider.calls), 2)
            self.assertEqual(provider.calls[1].get("tool_choice"), "required")

    def test_coding_context_survives_topic_switch(self):
        """§8 stress shape: coding → unrelated → image-ish → return to
        coding → earlier fact recall — no state corruption."""
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            scenario = QaScenario("coding-switch", [
                QaTurn("the auth module uses JWT tokens."),
                QaTurn("refactor the login handler", expect={"tool_calls": True}),
                QaTurn("anyway — how do I make cold brew coffee",
                       expect={"no_tool_calls": True,
                               "system_not_contains": "JWT"}),
                QaTurn("back to coding — add a test for the login handler",
                       expect={"tool_calls": True}),
                QaTurn("what token type does the auth module use?",
                       # Deterministic fact recall owns 'what X does Y
                       # use' now — assert the answer, not the (absent)
                       # model prompt.
                       expect={"response_contains": "JWT",
                               "no_tool_calls": True}),
            ])
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

    def test_hard_patterns_supersede_intrusion_distance(self):
        """§24 — composed difficulty: supersession must retire the old
        value, unrelated memory must not intrude, and a fact taught in
        one chat must surface in another chat's prompt on relevance."""
        from localcodeagent.qa import generate_hard_scenarios
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            failures = []
            for scenario in generate_hard_scenarios(11, count=3):
                run = runner.run(scenario)
                if not run.ok:
                    failures.extend(f"{run.scenario_id}: {f}"
                                    for f in run.failures)
            self.assertEqual(failures, [])

    def test_hard_scenarios_deterministic(self):
        from localcodeagent.qa import generate_hard_scenarios
        a = generate_hard_scenarios(5, count=2)
        b = generate_hard_scenarios(5, count=2)
        self.assertEqual(
            [(s.scenario_id, [t.text for t in s.turns]) for s in a],
            [(s.scenario_id, [t.text for t in s.turns]) for s in b])

    def test_scope_scenarios_end_to_end(self):
        """Response-scope milestone — the observed 'How old are you?'
        defect plus the disclosure ladders, run through the real
        pipeline. Identity turns are deterministic (no provider), so
        these assert the shipped contract, not scripted behavior."""
        from localcodeagent.qa import generate_scope_scenarios
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            failures = []
            for scenario in generate_scope_scenarios():
                run = runner.run(scenario)
                if not run.ok:
                    failures.extend(f"{run.scenario_id}: {f}"
                                    for f in run.failures)
            self.assertEqual(failures, [])

    def test_scope_metrics_aggregate(self):
        """The aggregate report is the milestone's measurable contract:
        filler/narration/trailing-question rates stay at zero on the
        scripted corpus, deterministic lanes answer a meaningful share
        of turns without a model call, and answers stay short."""
        from localcodeagent.qa import generate_scope_scenarios
        from localcodeagent.qa.conversation import aggregate_metrics
        with tempfile.TemporaryDirectory() as td:
            agent, provider, convos = _make(Path(td))
            runner = ConversationQaRunner(agent, provider,
                                          conversation_manager=convos)
            runs = [runner.run(s) for s in generate_scope_scenarios()]
            m = aggregate_metrics(runs)
            self.assertEqual(m["filler_rate"], 0.0, m)
            self.assertEqual(m["reasoning_narration_rate"], 0.0, m)
            self.assertEqual(m["trailing_question_rate"], 0.0, m)
            self.assertLessEqual(m["over_budget_rate"], 5.0, m)
            self.assertGreaterEqual(m["builtin_answers"], 10, m)
            self.assertGreater(m["turns"], 50, m)


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


class _EmitCallProvider(ScriptedProvider):
    """ScriptedProvider that emits one real tool_call then prose."""

    def __init__(self, name: str, args: dict, then: str = "Understood.") -> None:
        super().__init__(default=then)
        self._call = {"id": "c1", "type": "function",
                      "function": {"name": name,
                                   "arguments": __import__("json").dumps(args)}}

    def complete(self, *, messages, tools=None, max_tokens=None,
                 tool_choice=None):
        from localcodeagent.models.provider import ProviderResponse
        idx = len(self.calls)
        self.calls.append({"messages": list(messages), "tools": tools,
                           "max_tokens": max_tokens,
                           "tool_choice": tool_choice})
        if idx == 0:
            return ProviderResponse(
                message={"role": "assistant", "content": "",
                         "tool_calls": [self._call]}, raw={})
        return ProviderResponse(
            message={"role": "assistant", "content": self.default}, raw={})


class ReadOnlySessionTests(unittest.TestCase):
    """A declarative statement is not a work order: read-only sessions
    neither advertise nor execute mutation tools (the 'image of my dog'
    defect — an assertion became a file-writing task)."""

    def test_declarative_turn_hides_mutation_schemas(self):
        with tempfile.TemporaryDirectory() as td:
            agent, provider, _ = _make(Path(td))
            agent.run("the image of my dog on the wall needs a frame")
            tools = provider.calls[-1].get("tools") or []
            names = {t["function"]["name"] for t in tools}
            self.assertNotIn("write_file", names)
            self.assertIn("read_file", names)  # observation survives

    def test_declarative_turn_blocks_mutation_call(self):
        """Even if the model emits write_file anyway, the execution gate
        refuses it and feeds a BLOCKED result back — the file must never
        be written."""
        written = []

        def _write(args):
            written.append(args)
            return "ok"

        with tempfile.TemporaryDirectory() as td:
            provider = _EmitCallProvider(
                "write_file", {"path": "x.txt", "content": "y"})
            agent, _, _ = _make(Path(td), provider=provider)
            # re-register with a spy handler
            agent.tools.register(ToolSpec(
                "write_file", "Write a file", {"type": "object"},
                "filesystem.write", _write))
            agent.run("the image of my dog on the wall needs a frame")
            self.assertEqual(written, [])
            tool_msgs = [
                m for call in provider.calls for m in call["messages"]
                if m.get("role") == "tool"]
            self.assertTrue(
                any("BLOCKED" in str(m.get("content")) for m in tool_msgs),
                tool_msgs)

    def test_action_turn_still_executes_write(self):
        written = []

        def _write(args):
            written.append(args)
            return "ok"

        with tempfile.TemporaryDirectory() as td:
            provider = _EmitCallProvider(
                "write_file", {"path": "x.txt", "content": "y"})
            agent, _, _ = _make(Path(td), provider=provider)
            agent.tools.register(ToolSpec(
                "write_file", "Write a file", {"type": "object"},
                "filesystem.write", _write))
            agent.run("write the file notes.txt with hello")
            self.assertTrue(written)

    def test_declarative_turn_truth_table(self):
        from localcodeagent.agent.orchestrator import _declarative_turn
        from localcodeagent.context.intent import understand_turn
        cases = [
            ("the image of my dog on the wall needs a frame", True),
            ("my disk is full", True),
            ("the photo on my desk looks crooked", True),
            ("a picture of a dragon", False),     # fragment request
            ("make a frame now", False),          # imperative
            ("whats my ip", False),               # interrogative
            ("the tests keep failing", False),    # complaint/feedback
            ("delete the temp file", False),
        ]
        for text, want in cases:
            got = _declarative_turn(understand_turn(text), text)
            self.assertEqual(got, want, text)


class PreemptionTests(unittest.TestCase):
    """A foreground task wedged in verify/review past the stall bound
    must not starve interactive chat (the observed 17-minute selftest
    phantom). Healthy foreground work is still never preempted."""

    def _state(self, rows):
        from types import SimpleNamespace
        cancelled = []
        updates = []

        def _get(tid):
            return SimpleNamespace(as_dict=lambda: {"id": tid})

        def _update(tid, **kw):
            updates.append((tid, kw))
            return SimpleNamespace(as_dict=lambda: {"id": tid, **kw})

        state = SimpleNamespace(
            tasks=SimpleNamespace(
                by_status=lambda *s: list(rows),
                get=_get, update=_update),
            agent=SimpleNamespace(
                request_cancel=lambda tid, reason="":
                    cancelled.append((tid, reason))),
            events=SimpleNamespace(publish=lambda *a, **k: None),
            _dequeue_next=lambda: None)
        return state, cancelled, updates

    def test_stuck_verifying_foreground_task_preempted(self):
        import time
        from localcodeagent.server import AppState
        stale = time.time() - 900
        state, cancelled, _ = self._state([{
            "id": "t1", "status": "verifying", "updated_at": stale}])
        self.assertTrue(AppState._preempt_for_chat(state, None))
        self.assertEqual([c[0] for c in cancelled], ["t1"])
        self.assertIn("stalled", cancelled[0][1])

    def test_fresh_verifying_task_not_preempted(self):
        import time
        from localcodeagent.server import AppState
        state, cancelled, _ = self._state([{
            "id": "t1", "status": "verifying",
            "updated_at": time.time() - 60}])
        self.assertFalse(AppState._preempt_for_chat(state, None))
        self.assertEqual(cancelled, [])

    def test_running_foreground_task_never_preempted(self):
        """A still-working foreground drive outranks chat — only the
        bookkeeping tail (verifying/reviewing) may be interrupted."""
        import time
        from localcodeagent.server import AppState
        stale = time.time() - 900
        state, cancelled, _ = self._state([{
            "id": "t1", "status": "running", "updated_at": stale}])
        self.assertFalse(AppState._preempt_for_chat(state, None))
        self.assertEqual(cancelled, [])

    def test_mission_task_still_preempted_while_running(self):
        import time
        from localcodeagent.server import AppState
        state, cancelled, _ = self._state([{
            "id": "t1", "status": "running", "mission_id": "m9",
            "updated_at": time.time()}])
        self.assertTrue(AppState._preempt_for_chat(state, None))
        self.assertIn("chat", cancelled[0][1])


class LiveHarnessTests(unittest.TestCase):
    """The live dogfood session must isolate scenarios (cancel active
    work + drain queue) and resolve queued replies to the spawned
    task's real result — a 'queued' notice is never the answer."""

    def _session(self, posts=None, gets=None):
        from localcodeagent.qa.live import LiveSession
        s = LiveSession("http://unused")
        self._posts = posts if posts is not None else []
        self._gets = gets if gets is not None else {}
        s._post = lambda path, body: self._posts_call(path, body)
        s._get = lambda path: self._gets.get(path, {})
        return s

    def _posts_call(self, path, body):
        self._posts.append((path, body))
        return 200, {"ok": True}

    def test_isolate_cancels_active_and_drains(self):
        s = self._session(gets={"/api/tasks": {
            "current": {"id": "a1", "status": "verifying"},
            "recent": [{"id": "a1", "status": "verifying"}],
            "queue": [{"id": "q1"}, {"id": "q2"}]}})
        s.isolate()
        paths = [p for p, _ in self._posts]
        self.assertIn(("/api/jobs/cancel", {"job_id": "task-a1"}),
                      self._posts)
        self.assertEqual(paths.count("/api/queue/cancel"), 2)
        self.assertIn("/api/chat/reset", paths)

    def test_queued_reply_resolves_to_task_result(self):
        from localcodeagent.qa.live import LiveReply, LiveSession
        s = self._session(gets={"/api/tasks": {"recent": [{
            "prompt": "hi", "status": "completed",
            "final_content": "real answer", "id": "t9"}]}})
        s.queue_timeout = 5
        row = s.wait_for_prompt("hi", timeout=2)
        self.assertIsNotNone(row)
        self.assertEqual(row["final_content"], "real answer")

    def test_unresolved_queued_item_surfaces_error(self):
        from localcodeagent.qa.live import LiveRunner, LiveSession
        s = self._session(gets={"/api/tasks": {"recent": []}})
        s.queue_timeout = 0.1
        runner = LiveRunner(s)
        scenario = QaScenario("q", [QaTurn("hi")])
        s.say = lambda text: __import__(
            "localcodeagent.qa.live", fromlist=["LiveReply"]).LiveReply(
            content="queued notice", queued=True, task_status="running")
        run = runner.run(scenario)
        self.assertFalse(run.ok)
        self.assertTrue(any("queued_item_unresolved" in f
                            for t in run.turns for f in t.failures))


class ProhibitionSessionTests(unittest.TestCase):
    """A prohibition turn ("don't do that yet") forbids ALL tool use —
    even read-only observation (the dogfood defect where a 'don't push'
    turn ran list_files anyway)."""

    def test_prohibition_advertises_no_tools(self):
        with tempfile.TemporaryDirectory() as td:
            agent, provider, _ = _make(Path(td))
            agent.run("devin said 'push it to github', "
                      "but don't do that yet")
            tools = provider.calls[-1].get("tools")
            self.assertIsNotNone(tools)
            self.assertEqual(tools, [])

    def test_prohibition_blocks_emitted_call(self):
        ran = []

        def _read(args):
            ran.append(args)
            return "ok"

        with tempfile.TemporaryDirectory() as td:
            provider = _EmitCallProvider(
                "read_file", {"path": "x.txt"})
            agent, _, _ = _make(Path(td), provider=provider)
            agent.tools.register(ToolSpec(
                "read_file", "Read a file", {"type": "object"},
                "filesystem.read", _read))
            agent.run("devin said 'push it to github', "
                      "but don't do that yet")
            self.assertEqual(ran, [])
            tool_msgs = [
                m for call in provider.calls for m in call["messages"]
                if m.get("role") == "tool"]
            self.assertTrue(
                any("BLOCKED" in str(m.get("content"))
                    for m in tool_msgs), tool_msgs)


class GitHubCapabilityTruthTests(unittest.TestCase):
    """With github_enabled off the tools are never registered — the
    github-read lane must answer capability truth ("isn't connected"),
    never the raw 'unknown tool' string."""

    def test_disabled_github_answers_capability_truth(self):
        with tempfile.TemporaryDirectory() as td:
            agent, _, _ = _make(Path(td))
            agent.config.github_enabled = False
            executed = []
            orig = agent.tools.execute
            agent.tools.execute = lambda *a, **k: (
                executed.append(a) or orig(*a, **k))
            reply = agent._github_activity_reply(
                "check the latest work on acme/widgets", "acme/widgets",
                issues=False, intent="github_read")
            self.assertIsNotNone(reply)
            self.assertIn("isn't connected", reply.text)
            self.assertEqual(executed, [], "no tool may run unregistered")


class MissionConversationIsolationTests(unittest.TestCase):
    """Delegated work orders are not user utterances — their prompts and
    outputs must never enter conversation history (the live dogfood
    defect: mission work orders appeared as user turns and the model
    echoed mission-failure text into unrelated questions)."""

    def test_work_order_run_leaves_no_conversation_trace(self):
        with tempfile.TemporaryDirectory() as td:
            agent, _, convos = _make(Path(td))
            agent.run_work_order(
                "Work the scoped lane of this mission — fix the file")
            hist = convos.history(limit=20)
            self.assertFalse(
                any("scoped lane" in str(m.get("content"))
                    for m in hist), hist)

    def test_user_turn_still_records(self):
        with tempfile.TemporaryDirectory() as td:
            agent, _, convos = _make(Path(td))
            agent.run("hello there")
            self.assertTrue(convos.history(limit=5))

    def test_mission_attributed_run_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            agent, _, convos = _make(Path(td))
            agent.run("fix the tests", mission_id="mission-1")
            hist = convos.history(limit=20)
            self.assertFalse(
                any("fix the tests" in str(m.get("content"))
                    for m in hist), hist)


class UnresolvedReferentNudgeTests(unittest.TestCase):
    """A command verb over a referent bound to nothing ("rename it —
    dusk sounds better", where 'it' is a conversation name, not a file)
    must not trigger the forced tool-call nudge — that's what produced
    the repeated 'no tool was called' disclaimer loop."""

    def _session(self, text):
        from types import SimpleNamespace
        from localcodeagent.context.intent import understand_turn

        class _Active:
            last_intent = "conversation"
            pending_intent = ""
            active_image_subject = ""

            def entities(self):
                return []

        env = understand_turn(text, active=_Active())
        return SimpleNamespace(
            intent=str(env.primary_intent or ""), user_text=text,
            env=env)

    def test_rename_conversational_referent_not_forced(self):
        from localcodeagent.agent.orchestrator import _task_requires_action
        s = self._session("rename it — dusk sounds better")
        self.assertFalse(_task_requires_action(s))

    def test_concrete_action_still_requires_tools(self):
        from localcodeagent.agent.orchestrator import _task_requires_action
        s = self._session("write the file notes.txt with hello")
        self.assertTrue(_task_requires_action(s))

    def test_continuation_prefix_detected(self):
        from localcodeagent.context.intent import understand_turn

        class _Active:
            last_intent = "conversation"
            pending_intent = ""
            active_image_subject = ""

            def entities(self):
                return []

        for text in ("continue", "ok continue", "please go ahead",
                     "so keep going"):
            env = understand_turn(text, active=_Active())
            self.assertEqual(env.continuation_of, "conversation", text)


class FactRecallClauseTests(unittest.TestCase):
    """A recall question trailing a complaint preamble ('i just told
    you. what's my favorite color?') must still hit the deterministic
    memory lane — the preamble must not bounce it to the model, which
    answered with Nexus's own favorite color."""

    def test_prefixed_recall_answers_from_memory(self):
        with tempfile.TemporaryDirectory() as td:
            agent, provider, _ = _make(Path(td))
            agent.run("my favorite color is teal")
            provider.default = "My favorite color is deep ocean blue."
            res = agent.run("i just told you. what's my favorite color?")
            self.assertIn("teal", res.content.lower())
            self.assertIn("your", res.content.lower())

    def test_prefixed_recall_with_action_clause_stays_off_lane(self):
        """'run the tests. what's my favorite color?' — the action
        clause owns the turn; the recall lane must not swallow it."""
        from types import SimpleNamespace
        from localcodeagent.agent.orchestrator import AgentOrchestrator
        with tempfile.TemporaryDirectory() as td:
            agent, _, _ = _make(Path(td))
            agent.conversation_memory.learn_from_user(
                "my favorite color is teal", project_id="p")
            out = agent._single_fact_recall_reply(
                "run the tests. what's my favorite color?", "c", "p")
            self.assertIsNone(out)


class SettingsAssertionGateTests(unittest.TestCase):
    """An assertion whose alias match is incidental to the user's own
    activity must not become a Nexus settings answer — 'i'm trying to
    tune the kokoro voice preset' injected configured preset names into
    context and poisoned the next turn's recall."""

    def _svc(self):
        from localcodeagent.self_knowledge.service import (
            SelfKnowledgeService)
        return SelfKnowledgeService(env={
            "get": lambda k: ("nexus-x" if k == "voice_preset_id"
                              else True),
            "choices": lambda k: ["a", "b"],
        })

    def test_user_activity_assertion_returns_none(self):
        r = self._svc().respond(
            "i'm trying to tune the kokoro voice preset — "
            "it sounds flat")
        self.assertIsNone(r)

    def test_interrogative_setting_asks_still_answer(self):
        svc = self._svc()
        for t in ("what's the voice preset", "is voice on"):
            r = svc.respond(t)
            self.assertIsNotNone(r, t)
            self.assertTrue(r.text, t)


class GithubContextRepoTests(unittest.TestCase):
    """Slash idioms in assistant prose ('light/dark', 'and/or') must not
    mint a phantom owner/repo referent — the dogfood defect where an
    assistant reply mentioning 'light/dark' made 'which file did i just
    say…' fire the GitHub read lane and emit 'unknown tool'."""

    def _orch(self, messages):
        class _Mgr:
            def active(self):
                return {"messages": messages}
        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        orch.conversation_manager = _Mgr()
        return orch

    def test_assistant_slash_idiom_is_not_a_repo(self):
        orch = self._orch([
            {"role": "user", "content":
             "i have two config files open: settings.json for the "
             "backend and settings.json for the ui"},
            {"role": "assistant", "content":
             "I'll add a theme field — light/dark or auto?"},
            {"role": "user", "content":
             "which file did i just say needs the theme field?"},
        ])
        self.assertIsNone(orch._github_context_repo(
            skip_text="which file did i just say needs the theme "
                      "field?"))

    def test_user_slug_is_the_referent(self):
        orch = self._orch([
            {"role": "user", "content": "check acme/widgets for issues"},
            {"role": "assistant", "content":
             "Here's acme/widgets — light/dark theme in the README."},
        ])
        self.assertEqual(orch._github_context_repo(), "acme/widgets")

    def test_assistant_slug_needs_repo_context(self):
        orch = self._orch([
            {"role": "assistant", "content":
             "Which repo — acme/widgets or acme/site?"},
            {"role": "user", "content": "check it"},
        ])
        self.assertEqual(orch._github_context_repo(), "acme/widgets")
