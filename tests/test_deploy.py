"""deploy_static — detached-worktree gh-pages deploy. Tests run real git
against a local bare remote so push is genuinely exercised."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.deploy import register_deploy_tools


def _git(root: Path, *argv):
    return subprocess.run(["git", *argv], cwd=root, check=True,
                          capture_output=True, text=True)


def _git_avail() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True)
        return True
    except OSError:
        return False


@unittest.skipUnless(_git_avail(), "git not installed")
class DeployToolTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.remote = self.ws.parent / f"remote-{self.ws.name}"
        self.remote.mkdir()
        _git(self.remote, "init", "--bare")
        _git(self.ws, "init")
        _git(self.ws, "config", "user.email", "t@t.t")
        _git(self.ws, "config", "user.name", "T")
        (self.ws / "README.md").write_text("x")
        _git(self.ws, "add", "-A")
        _git(self.ws, "commit", "-m", "init")
        _git(self.ws, "remote", "add", "origin", str(self.remote))
        _git(self.ws, "push", "-u", "origin", "HEAD")
        self.reg = ToolRegistry({"github.write": "allow",
                                 "filesystem.read": "allow"})
        register_deploy_tools(self.reg, self.ws)

    def _run(self, **args):
        return json.loads(self.reg.get("deploy_static").handler(args))

    def test_deploy_pushes_branch(self):
        dist = self.ws / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<h1>deployed</h1>")
        out = self._run(path="dist")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["files"], 1)
        self.assertTrue(out["steps"][-1] == "pushed")
        # The remote really received the branch with our file.
        ls = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "gh-pages"],
            cwd=self.remote, capture_output=True, text=True).stdout
        self.assertIn("index.html", ls)
        # User's checkout untouched — same HEAD, same branch, no
        # deploy worktree residue. (dist/ is untracked test content.)
        head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                              cwd=self.ws, capture_output=True,
                              text=True).stdout.strip()
        self.assertNotEqual(head, "gh-pages")
        wt = subprocess.run(["git", "worktree", "list", "--porcelain"],
                            cwd=self.ws, capture_output=True,
                            text=True).stdout
        self.assertNotIn("deploy", wt)

    def test_no_remote_errors(self):
        other = Path(tempfile.mkdtemp())
        _git(other, "init")
        reg = ToolRegistry({"github.write": "allow"})
        register_deploy_tools(reg, self.ws, extra_roots=lambda: [other])
        (other / "site").mkdir()
        (other / "site" / "a.txt").write_text("x")
        out = json.loads(reg.get("deploy_static").handler(
            {"path": str(other / "site")}))
        self.assertIn("origin", out["error"])

    def test_empty_dir_errors(self):
        (self.ws / "empty").mkdir()
        out = self._run(path="empty")
        self.assertIn("error", out)

    def test_outside_root_refused(self):
        out = self._run(path=str(self.ws.parent.parent))
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
