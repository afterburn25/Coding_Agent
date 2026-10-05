from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.debugger import register_debug_tools


class DebugRunTests(unittest.TestCase):
    def _run(self, td: str, name: str, args: dict) -> dict:
        reg = ToolRegistry({"shell.execute": "allow",
                            "filesystem.read": "allow"})
        register_debug_tools(reg, Path(td))
        return json.loads(reg.get(name).handler(args))

    def test_breakpoint_captures_locals(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "t.py").write_text(
                "x = 1\ny = x + 41\nprint(y)\n", encoding="utf-8")
            out = self._run(td, "debug_run", {
                "script": "t.py",
                "breakpoints": [{"file": "t.py", "line": 3}],
            })
            self.assertEqual(out["returncode"], 0)
            self.assertEqual(out["stdout"].strip(), "42")
            hits = out["debug"]["hits"]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["locals"]["y"], "42")
            self.assertEqual(hits[0]["locals"]["x"], "1")

    def test_uncaught_exception_dumps_postmortem(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "t.py").write_text(
                "def boom():\n"
                "    secret = 1234\n"
                "    raise ValueError('kaboom')\n"
                "boom()\n",
                encoding="utf-8")
            out = self._run(td, "debug_run", {"script": "t.py"})
            exc = out["debug"]["exception"]
            self.assertEqual(exc["type"], "ValueError")
            self.assertEqual(exc["message"], "kaboom")
            top = next(f for f in exc["frames"]
                       if f["function"] == "boom")
            self.assertEqual(top["locals"]["secret"], "1234")

    def test_script_args_forwarded(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "t.py").write_text(
                "import sys; print(sys.argv[1])", encoding="utf-8")
            out = self._run(td, "debug_run",
                            {"script": "t.py", "args": ["hello"]})
            self.assertEqual(out["stdout"].strip(), "hello")

    def test_outside_workspace_refused(self):
        with tempfile.TemporaryDirectory() as td, \
             tempfile.TemporaryDirectory() as other:
            (Path(other) / "evil.py").write_text("print(1)")
            reg = ToolRegistry({"shell.execute": "allow"})
            register_debug_tools(reg, Path(td))
            out = json.loads(reg.get("debug_run").handler(
                {"script": str(Path(other) / "evil.py")}))
            self.assertIn("error", out)

    def test_invalid_breakpoint_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "t.py").write_text("print(1)\n")
            out = self._run(td, "debug_run", {
                "script": "t.py",
                "breakpoints": [{"file": "t.py", "line": -3}],
            })
            self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
