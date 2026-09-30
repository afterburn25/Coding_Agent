import tempfile
import unittest
from pathlib import Path

from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.filesystem import register_filesystem_tools


class ToolTests(unittest.TestCase):
    def test_workspace_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reg = ToolRegistry({"filesystem.read": "allow", "filesystem.write": "allow"})
            register_filesystem_tools(reg, root)
            self.assertIn("WROTE", reg.execute("write_file", {"path": "a.txt", "content": "hello"}))
            self.assertIn("hello", reg.execute("read_file", {"path": "a.txt"}))
            result = reg.execute("read_file", {"path": "../outside.txt"})
            self.assertTrue(result.startswith("ERROR"))

    def test_permission_gate(self):
        with tempfile.TemporaryDirectory() as td:
            reg = ToolRegistry({"filesystem.read": "allow", "filesystem.write": "ask"})
            register_filesystem_tools(reg, Path(td))
            result = reg.execute("write_file", {"path": "a.txt", "content": "x"})
            self.assertTrue(result.startswith("APPROVAL_REQUIRED"))


if __name__ == "__main__":
    unittest.main()
