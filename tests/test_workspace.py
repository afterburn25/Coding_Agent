"""Workspace Manager — inspect/adopt/persist/boundaries."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.workspace import WorkspaceManager
from localcodeagent.tools.filesystem import _safe_path


def _make(tmp: Path, store_name: str = "workspaces.json") -> WorkspaceManager:
    return WorkspaceManager(tmp / store_name, tmp)


class WorkspaceInspectTests(unittest.TestCase):

    def test_missing_directory(self):
        with tempfile.TemporaryDirectory() as td:
            wm = _make(Path(td))
            r = wm.inspect(Path(td) / "nope")
            self.assertFalse(r["exists"])

    def test_generic_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            wm = _make(Path(td))
            r = wm.inspect(td)
            self.assertTrue(r["exists"])
            self.assertEqual(r["project_type"], "generic")
            self.assertTrue(r["is_empty"])

    def test_node_react_detection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "app"
            root.mkdir()
            (root / "package.json").write_text(json.dumps({
                "scripts": {"dev": "vite", "build": "vite build",
                            "test": "vitest"},
                "dependencies": {"react": "^18"}}))
            wm = _make(Path(td))
            r = wm.inspect(root)
            self.assertEqual(r["project_type"], "react")
            self.assertEqual(r["toolchain"], "node")
            self.assertEqual(r["commands"]["dev"], "npm run dev")
            self.assertEqual(r["commands"]["build"], "npm run build")
            self.assertEqual(r["commands"]["test"], "npm run test")

    def test_python_pytest_detection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "svc"
            root.mkdir()
            (root / "requirements.txt").write_text("fastapi\npytest\n")
            wm = _make(Path(td))
            r = wm.inspect(root)
            self.assertEqual(r["project_type"], "fastapi")
            self.assertEqual(r["toolchain"], "python")
            self.assertEqual(r["commands"]["test"], "python -m pytest")

    def test_cmake_detection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "native"
            root.mkdir()
            (root / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)")
            wm = _make(Path(td))
            r = wm.inspect(root)
            self.assertEqual(r["project_type"], "cpp_cmake")
            self.assertIn("cmake", r["commands"].get("build", ""))

    def test_language_census(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "mix"
            root.mkdir()
            (root / "a.py").write_text("x=1")
            (root / "b.py").write_text("x=2")
            (root / "c.js").write_text("x=3")
            wm = _make(Path(td))
            r = wm.inspect(root)
            self.assertEqual(r["languages"][0], "python")
            self.assertIn("javascript", r["languages"])


class WorkspaceAdoptionTests(unittest.TestCase):

    def test_open_persists_record(self):
        with tempfile.TemporaryDirectory() as td:
            wm = _make(Path(td))
            root = Path(td) / "proj"
            root.mkdir()
            rec = wm.open(root)
            self.assertEqual(rec["root"], str(root.resolve()))
            self.assertIn("id", rec)
            # Reload from disk — persistence round-trip.
            wm2 = _make(Path(td))
            self.assertEqual(wm2.active()["id"], rec["id"])

    def test_open_missing_without_create_raises(self):
        with tempfile.TemporaryDirectory() as td:
            wm = _make(Path(td))
            with self.assertRaises(FileNotFoundError):
                wm.open(Path(td) / "ghost")

    def test_open_missing_with_create(self):
        with tempfile.TemporaryDirectory() as td:
            wm = _make(Path(td))
            rec = wm.open(Path(td) / "newdir", create=True)
            self.assertTrue((Path(td) / "newdir").is_dir())
            self.assertEqual(rec["name"], "newdir")

    def test_reopen_refreshes_not_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            wm = _make(Path(td))
            root = Path(td) / "p"
            root.mkdir()
            r1 = wm.open(root)
            r2 = wm.open(root)
            self.assertEqual(r1["id"], r2["id"])
            self.assertEqual(len(wm.list()), 1)

    def test_remove(self):
        with tempfile.TemporaryDirectory() as td:
            wm = _make(Path(td))
            root = Path(td) / "p"
            root.mkdir()
            rec = wm.open(root)
            self.assertTrue(wm.remove(rec["id"]))
            self.assertIsNone(wm.active())
            self.assertFalse(wm.list())


class WorkspaceBoundaryTests(unittest.TestCase):

    def test_allowed_roots_includes_registered(self):
        with tempfile.TemporaryDirectory() as td:
            wm = _make(Path(td))
            ext = Path(td) / "ext"
            ext.mkdir()
            self.assertEqual(len(wm.allowed_roots()), 1)
            wm.open(ext)
            roots = wm.allowed_roots()
            self.assertEqual(len(roots), 2)
            self.assertIn(ext.resolve(), roots)

    def test_safe_path_allows_registered_root(self):
        with tempfile.TemporaryDirectory() as td:
            primary = Path(td) / "primary"
            ext = Path(td) / "ext"
            primary.mkdir()
            ext.mkdir()
            wm = WorkspaceManager(Path(td) / "ws.json", primary)
            wm.open(ext)
            # Absolute path inside the registered workspace passes.
            p = _safe_path(primary, str(ext / "file.py"),
                           extra_roots=wm.allowed_roots)
            self.assertEqual(p, (ext / "file.py").resolve())
            # Outside both — still refused.
            with self.assertRaises(ValueError):
                _safe_path(primary, td,
                           extra_roots=wm.allowed_roots)

    def test_safe_path_agent_dir_still_blocked_in_extra_root(self):
        with tempfile.TemporaryDirectory() as td:
            primary = Path(td) / "primary"
            ext = Path(td) / "ext"
            primary.mkdir()
            ext.mkdir()
            wm = WorkspaceManager(Path(td) / "ws.json", primary)
            wm.open(ext)
            with self.assertRaises(ValueError):
                _safe_path(primary, str(ext / ".agent" / "x.json"),
                           extra_roots=wm.allowed_roots)

    def test_contains(self):
        with tempfile.TemporaryDirectory() as td:
            primary = Path(td) / "primary"
            ext = Path(td) / "ext"
            primary.mkdir()
            ext.mkdir()
            wm = WorkspaceManager(Path(td) / "ws.json", primary)
            wm.open(ext)
            self.assertTrue(wm.contains(ext / "sub" / "f.py"))
            self.assertFalse(wm.contains(Path(td) / "other" / "f.py"))


if __name__ == "__main__":
    unittest.main()
