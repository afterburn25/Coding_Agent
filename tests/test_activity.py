from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.provider import ProviderResponse
from localcodeagent.models.router import ModelRouter
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.workflow.activity import OUTPUT_TAIL_LIMIT, ActivityStore
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore


class _FakeRuntime:
    def refresh_hardware(self):
        pass

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


if __name__ == "__main__":
    unittest.main()
