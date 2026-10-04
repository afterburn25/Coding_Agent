import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from localcodeagent.autonomy.missions import new_mission
from localcodeagent.autonomy.planner import MissionPlanner
from localcodeagent.autonomy.task_graph import TaskGraph
from localcodeagent.coding_pipeline import build_coding_tasks, stage_view
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.git import register_git_tools


def _git_ok() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True,
                       check=True)
        return True
    except Exception:
        return False


def _init_repo(root: Path) -> None:
    for argv in (["git", "init"], ["git", "config", "user.email", "t@t.t"],
                 ["git", "config", "user.name", "T"]):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)


class CodingPipelinePlanTests(unittest.TestCase):
    def _mission(self, **opts):
        return new_mission(
            "Build a todo app", title="Todo app",
            workspace="D:/Projects/TodoApp",
            pipeline="coding",
            pipeline_options=opts)

    def test_stage_order_and_deps(self):
        tasks = build_coding_tasks(self._mission(root="D:/Projects/TodoApp"))
        stages = [t["metadata"]["pipeline_stage"] for t in tasks]
        self.assertEqual(
            stages,
            ["understand", "understand", "plan", "implement",
             "dependencies", "build", "test", "review", "document"])
        # serial chain — every node depends on the previous
        for i in range(1, len(tasks)):
            self.assertEqual(tasks[i]["deps"], [tasks[i - 1]["id"]])
        kinds = {t["metadata"]["pipeline_stage"]: t["kind"]
                 for t in tasks}
        self.assertEqual(kinds["understand"], "job")
        self.assertEqual(kinds["implement"], "agent")
        self.assertEqual(kinds["review"], "review")

    def test_serve_and_commit_stages_optional(self):
        m = self._mission(root="D:/X", server_cmd="npm run dev",
                          commit=True)
        tasks = build_coding_tasks(m)
        stages = [t["metadata"]["pipeline_stage"] for t in tasks]
        self.assertIn("launch_verify", stages)
        self.assertEqual(stages[-1], "commit")
        sv = [t for t in tasks
              if t["metadata"]["pipeline_stage"] == "launch_verify"][0]
        self.assertEqual(sv["metadata"]["job"], "serve_check")
        self.assertEqual(sv["metadata"]["command"], "npm run dev")
        cm = [t for t in tasks
              if t["metadata"]["pipeline_stage"] == "commit"][0]
        self.assertEqual(cm["metadata"]["tool"], "git_commit")

    def test_planner_uses_pipeline(self):
        m = self._mission(root="D:/X")
        tasks = MissionPlanner().initial_plan(m)
        self.assertTrue(any(
            (t.get("metadata") or {}).get("pipeline_stage")
            for t in tasks))
        plan = new_mission("x")  # no pipeline
        std = MissionPlanner().initial_plan(plan)
        self.assertFalse(any(
            (t.get("metadata") or {}).get("pipeline_stage")
            for t in std))

    def test_stage_view_maps_node_states(self):
        m = self._mission(root="D:/X")
        tasks = build_coding_tasks(m)
        m["graph"]["nodes"] = tasks
        g = TaskGraph(m)
        g.mark(tasks[0]["id"], "completed",
               result={"ok": True, "output": "detected: python"})
        view = stage_view(m)
        self.assertEqual(len(view), len(tasks))
        self.assertEqual(view[0]["stage"], "understand")
        self.assertEqual(view[0]["state"], "completed")
        self.assertTrue(view[0]["result_ok"])
        self.assertEqual(view[1]["state"], "ready")

    def test_acyclic_graph(self):
        m = self._mission(root="D:/X", server_cmd="x", commit=True)
        m["graph"]["nodes"] = []
        g = TaskGraph(m)
        g.add_many(build_coding_tasks(m))  # raises on cycle


@unittest.skipUnless(_git_ok(), "git not installed")
class GitCommitToolTests(unittest.TestCase):
    def test_commit_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "a.txt").write_text("one")
            reg = ToolRegistry({"git.execute": True,
                                "filesystem.read": True})
            register_git_tools(reg, root)
            out = json.loads(reg.get("git_commit").handler(
                {"path": str(root), "message": "first",
                 "add_all": True}))
            self.assertTrue(out["committed"])
            self.assertEqual(len(out["sha"]), 40)
            self.assertIn("a.txt", out["files"])
            # nothing left → committed: False, not an error
            out2 = json.loads(reg.get("git_commit").handler(
                {"path": str(root), "message": "second"}))
            self.assertFalse(out2["committed"])

    def test_commit_outside_root_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            reg = ToolRegistry({"git.execute": True})
            register_git_tools(reg, root)
            with self.assertRaises(ValueError):
                reg.get("git_commit").handler(
                    {"path": "D:/Windows", "message": "x"})


if __name__ == "__main__":
    unittest.main()
