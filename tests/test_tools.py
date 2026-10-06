import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from localcodeagent.config import AgentConfig
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.filesystem import register_filesystem_tools
from localcodeagent.tools.github import _repo_slug_from_remote, register_github_tools
from localcodeagent.tools.shell import register_shell_tools
from localcodeagent.tools.terminal import run_process_streaming


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


class GitHubCodingToolTests(unittest.TestCase):
    @staticmethod
    def _init_repo(root: Path) -> None:
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Chat Nexus Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "chat-nexus-test@example.invalid"], cwd=root, check=True)
        (root / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True, text=True)

    def test_repo_slug_parses_https_and_ssh_remotes(self):
        self.assertEqual(_repo_slug_from_remote("https://github.com/owner/repo.git"), "owner/repo")
        self.assertEqual(_repo_slug_from_remote("git@github.com:owner/repo.git"), "owner/repo")

    def test_branch_and_explicit_path_commit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_repo(root)
            permissions = {
                "filesystem.read": "allow",
                "git.execute": "allow",
                "github.read": "allow",
                "github.write": "ask",
            }
            config = AgentConfig(permissions=permissions)
            reg = ToolRegistry(permissions)
            register_github_tools(reg, root, config)

            created = json.loads(reg.execute("git_create_branch", {"branch": "feature/native-github"}))
            self.assertEqual(created["branch"], "feature/native-github")
            (root / "README.md").write_text("changed\n", encoding="utf-8")
            committed = json.loads(reg.execute("git_commit", {
                "message": "Update README",
                "paths": ["README.md"],
            }))
            self.assertTrue(committed["commit"])
            branch = reg.execute("git_current_branch", {})
            self.assertEqual(branch, "feature/native-github")
            changed = subprocess.run(
                ["git", "show", "--name-only", "--format=", "HEAD"],
                cwd=root, check=True, capture_output=True, text=True,
            ).stdout.splitlines()
            self.assertEqual(changed, ["README.md"])

    def test_agent_metadata_cannot_be_staged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            permissions = {"git.execute": "allow"}
            config = AgentConfig(permissions=permissions)
            reg = ToolRegistry(permissions)
            register_github_tools(reg, root, config)
            result = reg.execute("git_commit", {
                "message": "bad commit",
                "paths": [".agent/tasks.json"],
            })
            self.assertTrue(result.startswith("ERROR: ValueError"))
            self.assertIn(".agent metadata", result)

    def test_remote_push_requires_explicit_approval(self):
        with tempfile.TemporaryDirectory() as td:
            permissions = {"git.push": "ask"}
            config = AgentConfig(permissions=permissions)
            reg = ToolRegistry(permissions)
            register_github_tools(reg, Path(td), config)
            result = reg.execute("git_push", {"remote": "origin", "branch": "main"})
            self.assertTrue(result.startswith("APPROVAL_REQUIRED"))
            self.assertIn("git.push", result)


class ProcessStreamingTests(unittest.TestCase):
    def test_cancel_check_kills_running_process(self):
        with tempfile.TemporaryDirectory() as td:
            polls = []
            code, out, err, killed = run_process_streaming(
                ["python", "-c", "import time; print('start'); time.sleep(30)"],
                cwd=Path(td), timeout=60,
                cancel_check=lambda: (polls.append(1), True)[1],
            )
            self.assertTrue(killed)
            self.assertTrue(polls)

    def test_no_cancel_check_runs_to_completion(self):
        with tempfile.TemporaryDirectory() as td:
            code, out, err, killed = run_process_streaming(
                ["python", "-c", "print('done')"], cwd=Path(td), timeout=60,
            )
            self.assertFalse(killed)
            self.assertEqual(code, 0)
            self.assertIn("done", out)


class PerCommandCancelTests(unittest.TestCase):
    """The timeline Stop button sets command_cancel[task_id]; the shared
    cancel_check must kill the live subprocess without cancelling the task."""

    def test_command_flag_kills_subprocess_not_task(self):
        with tempfile.TemporaryDirectory() as td:
            reg = ToolRegistry({"shell.execute": "allow"})
            register_shell_tools(reg, Path(td))
            flag = threading.Event()
            task_cancelled = threading.Event()
            reg.context["command_cancel"] = {"t1": flag}
            reg.context["cancel_checks"] = {
                "t1": lambda: task_cancelled.is_set() or flag.is_set()
            }
            reg.context["task_id"] = "t1"

            def click_stop():
                time.sleep(0.6)
                flag.set()

            threading.Thread(target=click_stop, daemon=True).start()
            started = time.monotonic()
            result = reg.execute("run_shell", {
                "command": f"{sys.executable} -c \"import time; time.sleep(30)\"",
                "timeout": 45,
            })
            self.assertLess(time.monotonic() - started, 20)
            self.assertIn("[cancelled]", result)
            # Task-level cancel never fired — the task continues.
            self.assertFalse(task_cancelled.is_set())




if __name__ == "__main__":
    unittest.main()
