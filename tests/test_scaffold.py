"""Scaffolding + builder-loop tools — real files, real evidence."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.scaffold import catalog, render_files, scaffold, TEMPLATES
from localcodeagent.workspace import WorkspaceManager
from localcodeagent.tools.project import register_project_tools, _setup_steps
from localcodeagent.tools.base import ToolRegistry


class ScaffoldTests(unittest.TestCase):

    def test_catalog_nonempty_and_unique(self):
        ids = [t["id"] for t in catalog()]
        self.assertGreaterEqual(len(ids), 6)
        self.assertEqual(len(ids), len(set(ids)))

    def test_render_substitutes_name_and_pkg(self):
        files = render_files("python_app", name="My Cool App",
                             description="a test app")
        self.assertIn("my_cool_app/main.py", files)
        self.assertIn("my-cool-app", files["README.md"]
                      .split("\n")[0].lower().replace(" ", "-")
                      .replace("--", "-") or "my-cool-app")
        self.assertNotIn("{{", "".join(files.values()))

    def test_scaffold_writes_files(self):
        with tempfile.TemporaryDirectory() as td:
            out = scaffold(Path(td) / "proj", "static_site")
            root = Path(out["root"])
            self.assertTrue((root / "index.html").is_file())
            self.assertIn("dev", out["commands"])

    def test_scaffold_refuses_nonempty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "busy"
            root.mkdir()
            (root / "userfile.txt").write_text("mine")
            with self.assertRaises(ValueError):
                scaffold(root, "static_site")

    def test_scaffold_force_allows_nonempty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "busy"
            root.mkdir()
            (root / "userfile.txt").write_text("mine")
            scaffold(root, "static_site", force=True)
            # User file untouched.
            self.assertEqual((root / "userfile.txt").read_text(), "mine")

    def test_unknown_template(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(KeyError):
                scaffold(Path(td) / "x", "not_a_template")

    def test_python_app_template_tests_pass(self):
        """The python_app template is genuinely runnable."""
        import subprocess, sys
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            scaffold(root, "python_app", name="demo")
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s",
                 "tests"], cwd=root, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0,
                             proc.stdout + proc.stderr)


class SetupStepsTests(unittest.TestCase):

    def test_npm_detected(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "package.json").write_text("{}")
            self.assertEqual(_setup_steps(Path(td)), ["npm install"])

    def test_requirements_uses_venv(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "requirements.txt").write_text("flask\n")
            steps = _setup_steps(Path(td))
            self.assertTrue(any("venv" in s for s in steps))
            self.assertTrue(any("pip install -r requirements.txt" in s
                                for s in steps))

    def test_no_markers_no_steps(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_setup_steps(Path(td)), [])


class ProjectToolTests(unittest.TestCase):

    def _registry(self, manager):
        reg = ToolRegistry({"filesystem.read": "allow",
                            "filesystem.write": "allow",
                            "shell.execute": "allow"})
        register_project_tools(reg, manager)
        return reg

    def _exec(self, reg, name, args):
        spec = reg.get(name)
        return spec.handler(args)

    def test_scaffold_tool_registers_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = WorkspaceManager(Path(td) / "ws.json", Path(td) / "main")
            reg = self._registry(mgr)
            out = json.loads(self._exec(reg, "project_scaffold", {
                "path": str(Path(td) / "newapp"),
                "template": "python_app"}))
            self.assertTrue(out["ok"])
            self.assertTrue((Path(td) / "newapp" / "demo").is_dir()
                            or any((Path(td) / "newapp").iterdir()))
            self.assertTrue(mgr.contains(Path(td) / "newapp" / "x.py"))

    def test_scaffold_tool_rejects_unknown_template(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = WorkspaceManager(Path(td) / "ws.json", Path(td) / "main")
            reg = self._registry(mgr)
            out = json.loads(self._exec(reg, "project_scaffold", {
                "path": str(Path(td) / "x"), "template": "nope"}))
            self.assertEqual(out["error"], "unknown_template")
            self.assertIn("static_site", out["available"])

    def test_templates_tool(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = WorkspaceManager(Path(td) / "ws.json", Path(td) / "main")
            reg = self._registry(mgr)
            out = json.loads(self._exec(reg, "project_templates", {}))
            self.assertTrue(any(t["id"] == "react_vite"
                                for t in out["templates"]))


if __name__ == "__main__":
    unittest.main()
