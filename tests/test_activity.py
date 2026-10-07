from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.provider import ProviderResponse
from localcodeagent.models.router import ModelRouter
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.shell import register_shell_tools
from localcodeagent.workflow.activity import OUTPUT_TAIL_LIMIT, ActivityStore
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore


class _FakeRuntime:
    def refresh_hardware(self):
        pass

    def fresh_hardware(self, max_age_s: float = 15.0):
        return self.refresh_hardware()

    def ensure_ready(self, profile):
        return "http://127.0.0.1:1/v1"

    def recover(self, profile):
        return "http://127.0.0.1:1/v1"

    def resident_model_ids(self):
        return []

    def rewarm_keep_loaded(self):
        return []


class _Provider:
    def complete(self, *, messages, tools=None, max_tokens=None):
        return ProviderResponse(message={"role": "assistant", "content": "done"}, raw={})


def _agent(root: Path, store: ActivityStore, events: list):
    profile = ModelProfile(
        id="local", endpoint="http://unused/v1", model="x",
        roles=["utility", "primary_coder"], runtime="external",
    )
    config = AgentConfig(
        models=[profile], permissions={},
        auto_verify_after_changes=False, review_after_changes=False,
    )
    agent = AgentOrchestrator(
        config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
        tasks=TaskStore(root), checkpoints=CheckpointManager(root),
        memory=ProjectMemory(root), repository_index=RepositoryIndex(root),
        activities=store,
    )
    agent._provider_for = lambda _: _Provider()
    return agent


class MissionTimelineTests(unittest.TestCase):
    def test_mission_id_stamp_and_filter(self):
        with tempfile.TemporaryDirectory() as td:
            store = ActivityStore(Path(td) / "a.jsonl")
            store.open("t1", "tool", "Row", mission_id="m-123")
            store.open("t2", "command", "Other")
            self.assertEqual(len(store.for_mission("m-123")), 1)
            self.assertEqual(store.for_mission("m-123")[0]["mission_id"], "m-123")
            self.assertEqual(store.for_mission("nope"), [])

    def test_progress_clamped(self):
        with tempfile.TemporaryDirectory() as td:
            store = ActivityStore(Path(td) / "a.jsonl")
            row = store.open("t1", "testing", "Tests", progress=0.5)
            self.assertEqual(row["progress"], 0.5)
            store.update("t1", row["id"], progress=1.7)
            self.assertEqual(store.for_task("t1")[0]["progress"], 1.0)

    def test_mission_id_survives_reload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            store = ActivityStore(path)
            r = store.open("t1", "task_graph", "Node", mission_id="m-9")
            store.update("t1", r["id"], state="completed")
            store2 = ActivityStore(path)
            self.assertEqual(store2.for_mission("m-9")[0]["title"], "Node")

    def test_summary_rollup(self):
        with tempfile.TemporaryDirectory() as td:
            store = ActivityStore(Path(td) / "a.jsonl")
            a = store.open("t1", "tool", "run_shell", details={"model_id": "m1"})
            store.update("t1", a["id"], state="completed")
            b = store.open("t1", "retry", "Retrying", details={})
            store.update("t1", b["id"], state="failed")
            s = store.summary("t1")
            self.assertEqual(s["activities"], 2)
            self.assertEqual(s["errors"], 1)
            self.assertEqual(s["retries"], 1)
            self.assertIn("run_shell", s["tools"])
            self.assertIn("m1", s["models"])
            self.assertIsNotNone(s["elapsed_seconds"])


class ActivityStoreTests(unittest.TestCase):
    def test_open_update_complete(self):
        with tempfile.TemporaryDirectory() as td:
            store = ActivityStore(Path(td) / "a.jsonl")
            row = store.open("t1", "command", "Command", "python -V")
            self.assertEqual(row["state"], "running")
            done = store.update("t1", row["id"], state="completed", summary="ok")
            self.assertEqual(done["state"], "completed")
            self.assertIsNotNone(done["elapsed"])
            rows = store.for_task("t1")
            self.assertEqual(len(rows), 1)

    def test_unique_ids_and_parent(self):
        with tempfile.TemporaryDirectory() as td:
            store = ActivityStore(Path(td) / "a.jsonl")
            parent = store.open("t1", "testing", "Testing")
            child = store.open("t1", "command", "Command", parent=parent["id"])
            self.assertNotEqual(parent["id"], child["id"])
            self.assertEqual(child["parent"], parent["id"])

    def test_output_tail_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            store = ActivityStore(Path(td) / "a.jsonl")
            row = store.open("t1", "command", "Command")
            store.append_output("t1", row["id"], "x" * (OUTPUT_TAIL_LIMIT + 500))
            found = store.for_task("t1")[0]
            self.assertLessEqual(len(found["details"]["output_tail"]), OUTPUT_TAIL_LIMIT)

    def test_interrupted_on_reload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            store = ActivityStore(path)
            row = store.open("t1", "command", "Command")
            store.update("t1", row["id"], state="running")
            reloaded = ActivityStore(path)
            rows = reloaded.for_task("t1")
            self.assertEqual(rows[0]["state"], "interrupted")
            self.assertIsNotNone(rows[0]["ended_at"])

    def test_persistence_round_trip_completed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "a.jsonl"
            store = ActivityStore(path)
            row = store.open("t1", "planning", "Planning", "thinking")
            store.update("t1", row["id"], state="completed", summary="done")
            reloaded = ActivityStore(path)
            rows = reloaded.for_task("t1")
            self.assertEqual(rows[0]["state"], "completed")
            self.assertEqual(rows[0]["summary"], "done")

    def test_unknown_category_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            store = ActivityStore(Path(td) / "a.jsonl")
            row = store.open("t1", "nonsense", "X")
            self.assertEqual(row["category"], "tool")


class OrchestratorActivityTests(unittest.TestCase):
    def test_fast_lane_emits_timeline_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ActivityStore(root / "act.jsonl")
            events = []
            agent = _agent(root, store, events)
            agent.run("what does ram do", event_callback=events.append)
            rows = store.for_task(next(iter(store._by_task)))
            cats = {r["category"] for r in rows}
            self.assertIn("planning", cats)
            self.assertIn("routing", cats)
            self.assertIn("model", cats)
            self.assertIn("complete", cats)
            # No fake research row for a stable general question.
            self.assertNotIn("research", cats)
            # Live events were emitted for the rows.
            kinds = [e["activity"]["category"] for e in events if e.get("type") == "activity"]
            self.assertIn("planning", kinds)
            self.assertIn("complete", kinds)

    def test_completed_rows_have_elapsed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ActivityStore(root / "act.jsonl")
            agent = _agent(root, store, [])
            agent.run("what is a neural network")
            rows = store.for_task(next(iter(store._by_task)))
            for row in rows:
                if row["state"] == "completed":
                    self.assertIsNotNone(row["elapsed"], row["title"])

    def test_launch_diagnostics_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ActivityStore(root / "act.jsonl")
            agent = _agent(root, store, [])
            diag = agent._launch_diagnostics(agent.router.get_profile("local"))
            self.assertIn("gpu_layers", diag)
            self.assertIn("context_window", diag)

    def test_stale_command_cancel_flag_does_not_kill_next_command(self):
        # A Stop click landing between tool calls must not leak into the next
        # command: _execute_tool clears the per-task flag at entry.
        import threading

        class _FakeSession:
            task_id = "t-stale"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ActivityStore(root / "act.jsonl")
            agent = _agent(root, store, [])
            agent.tools.permissions["shell.execute"] = "allow"
            register_shell_tools(agent.tools, root)
            flag = threading.Event()
            flag.set()  # stale click before this command started
            agent.tools.context["command_cancel"] = {"t-stale": flag}
            agent.tools.context["cancel_checks"] = {"t-stale": flag.is_set}
            agent.tools.context["task_tls"] = threading.local()
            agent.tools.context["task_id"] = "t-stale"
            out = agent._execute_tool("run_shell", {"command": "echo hi"},
                                      session=_FakeSession())
            self.assertIn("EXIT_CODE=0", out)
            self.assertFalse(flag.is_set())

    def test_compacts_oversized_log_and_drops_old_tasks(self):
        from localcodeagent.workflow import activity as act_mod
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "act.jsonl"
            old_compact, old_keep = act_mod.COMPACT_BYTES, act_mod.KEEP_TASKS
            act_mod.COMPACT_BYTES = 4096
            act_mod.KEEP_TASKS = 3
            try:
                # Older tasks get dropped entirely; recent tasks keep latest state.
                s0 = ActivityStore(path)
                for t in range(6):
                    r = s0.open(f"task-{t}", "tool", f"title {t}", "x" * 2000)
                    s0.update(f"task-{t}", r["id"], state="completed")
                s0._persist({"id": "trigger", "task_id": "task-5",
                             "category": "tool", "state": "completed", "x": "y" * 2000})
                del s0
                s1 = ActivityStore(path)
                self.assertLessEqual(len(s1._by_task), 3)
                self.assertIn("task-5", s1._by_task)
                # File holds one line per surviving row — no update duplicates.
                lines = [l for l in path.read_text().splitlines() if l.strip()]
                total_rows = sum(len(v) for v in s1._by_task.values())
                self.assertEqual(len(lines), total_rows)
            finally:
                act_mod.COMPACT_BYTES, act_mod.KEEP_TASKS = old_compact, old_keep

    def test_malformed_tool_args_feedback_without_executing(self):
        # A tool call with unrecoverable arguments must not execute with {}
        # — the error goes back to the model so it can re-emit correctly.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ActivityStore(root / "act.jsonl")
            agent = _agent(root, store, [])
            agent.tools.permissions["fs.read"] = "allow"
            ran = []
            agent.tools.register(
                __import__("localcodeagent.tools.base", fromlist=["ToolSpec"]).ToolSpec(
                    name="recorder", description="r", parameters={"type": "object"}, permission="fs.read",
                    handler=lambda args: ran.append(args) or "ok"))

            calls = {"n": 0}
            seen_messages = []

            class _MalformProvider:
                def complete(self, *, messages, tools=None, max_tokens=None):
                    calls["n"] += 1
                    seen_messages.append(messages)
                    if calls["n"] == 1:
                        return ProviderResponse(message={
                            "role": "assistant", "content": "",
                            "tool_calls": [{"id": "1", "type": "function",
                                            "function": {"name": "recorder",
                                                         "arguments": "{'broken': "}}]}, raw={})
                    return ProviderResponse(message={"role": "assistant", "content": "done"}, raw={})

            agent._provider_for = lambda _: _MalformProvider()
            agent.run("record something")
            self.assertEqual(ran, [])
            self.assertEqual(calls["n"], 2)
            self.assertTrue(any("not valid JSON" in str(m.get("content") or "")
                                for m in seen_messages[-1]))

    def test_repairable_tool_args_execute(self):
        # Python-literal arguments (single quotes, True/None) recover and run.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ActivityStore(root / "act.jsonl")
            agent = _agent(root, store, [])
            agent.tools.permissions["fs.read"] = "allow"
            ran = []
            agent.tools.register(
                __import__("localcodeagent.tools.base", fromlist=["ToolSpec"]).ToolSpec(
                    name="recorder", description="r", parameters={"type": "object"}, permission="fs.read",
                    handler=lambda args: ran.append(args) or "ok"))

            calls = {"n": 0}

            class _PyLiteralProvider:
                def complete(self, *, messages, tools=None, max_tokens=None):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return ProviderResponse(message={
                            "role": "assistant", "content": "",
                            "tool_calls": [{"id": "1", "type": "function",
                                            "function": {"name": "recorder",
                                                         "arguments": "{'flag': True, 'n': None,}"}}]}, raw={})
                    return ProviderResponse(message={"role": "assistant", "content": "done"}, raw={})

            agent._provider_for = lambda _: _PyLiteralProvider()
            agent.run("record something")
            self.assertEqual(ran, [{"flag": True, "n": None}])

    def test_error_marks_rows_failed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = ActivityStore(root / "act.jsonl")
            agent = _agent(root, store, [])
            agent._provider_for = lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
            with self.assertRaises(Exception):
                agent.run("write me a function")
            rows = store.for_task(next(iter(store._by_task)))
            states = {r["state"] for r in rows}
            self.assertIn("failed", states)
            self.assertTrue(any(r["category"] == "error" for r in rows))

    def test_recent_returns_newest_first_across_tasks(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            store = ActivityStore(Path(td) / "act.jsonl")
            store.open("t1", "tool", "old step")
            time.sleep(0.01)
            store.open("t2", "tool", "new step")
            rows = store.recent(10)
            self.assertEqual(rows[0]["title"], "new step")
            self.assertEqual(rows[1]["title"], "old step")
            self.assertEqual(len(store.recent(1)), 1)

    def test_recent_survives_reload(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "act.jsonl"
            ActivityStore(path).open("t1", "command", "dir listing")
            store = ActivityStore(path)
            self.assertEqual(store.recent(5)[0]["title"], "dir listing")


if __name__ == "__main__":
    unittest.main()
