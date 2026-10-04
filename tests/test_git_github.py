"""Local git + GitHub connector tools — real git ops, fake REST."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.git import register_git_tools
from localcodeagent.tools import github as gh


PERMS = {"filesystem.read": "allow", "filesystem.write": "allow",
         "git.execute": "allow", "github.read": "allow",
         "github.write": "allow", "shell.execute": "allow"}


def _git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def _reg(workspace: Path, extra_roots=None) -> ToolRegistry:
    reg = ToolRegistry(dict(PERMS))
    register_git_tools(reg, workspace, extra_roots=extra_roots)
    return reg


def _exec(reg: ToolRegistry, name: str, args: dict) -> str:
    return reg.get(name).handler(args)


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root,
                   check=True, capture_output=True)


@unittest.skipUnless(_git_available(), "git not installed")
class LocalGitToolTests(unittest.TestCase):

    def test_init_status_commit_log(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "repo"
            root.mkdir()
            reg = _reg(root)
            out = json.loads(_exec(reg, "git_init", {"path": str(root)}))
            self.assertTrue((root / ".git").is_dir())
            # Make a commit via real git then check log.
            (root / "a.txt").write_text("one")
            subprocess.run(["git", "add", "a.txt"], cwd=root,
                           check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "first"], cwd=root,
                           check=True, capture_output=True)
            log = _exec(reg, "git_log", {"path": str(root)})
            self.assertIn("first", log)

    def test_user_changes_detects_dirty_and_untracked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "tracked.txt").write_text("v1")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root,
                           check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "c"], cwd=root,
                           check=True, capture_output=True)
            (root / "tracked.txt").write_text("v2")
            (root / "new.txt").write_text("n")
            reg = _reg(root)
            out = json.loads(_exec(reg, "git_user_changes", {}))
            self.assertTrue(out["has_user_changes"])
            self.assertIn("tracked.txt", out["modified"])
            self.assertIn("new.txt", out["untracked"])

    def test_switch_and_stash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _init_repo(root)
            (root / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=root, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-m", "c"], cwd=root,
                           check=True, capture_output=True)
            reg = _reg(root)
            out = json.loads(_exec(reg, "git_switch",
                                   {"branch": "feat", "create": True}))
            self.assertTrue(out["created"])
            (root / "wip.txt").write_text("wip")
            out = json.loads(_exec(reg, "git_stash",
                                   {"action": "push"}))
            self.assertIn("stash@{0}",
                          json.loads(_exec(reg, "git_stash",
                                           {"action": "list"}))["output"])

    def test_path_outside_roots_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "main"
            outside = Path(td) / "outside"
            root.mkdir(); outside.mkdir()
            reg = _reg(root)
            try:
                _exec(reg, "git_init", {"path": str(outside)})
                self.fail("expected ValueError")
            except ValueError:
                pass

    def test_extra_roots_allows_registered(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "main"
            ext = Path(td) / "ext"
            root.mkdir(); ext.mkdir()
            reg = _reg(root, extra_roots=lambda: [ext])
            out = json.loads(_exec(reg, "git_init",
                                   {"path": str(ext)}))
            self.assertTrue((ext / ".git").is_dir())


class FakeVault:
    def __init__(self):
        self.store = {}

    def get(self, name):
        return self.store.get(name)

    def set(self, name, value, *, description=""):
        self.store[name] = value
        return {"name": name}

    def delete(self, name):
        return self.store.pop(name, None) is not None


class _Cfg:
    github_api_url = "https://api.github.test"
    github_api_version = "2022-11-28"
    github_token_env = "GH_TEST_TOKEN"
    github_timeout = 5
    github_default_remote = "origin"


def _fake_response(payload, headers=None):
    class _Resp:
        def __init__(self):
            self.headers = headers or {}

        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return _Resp()


class GitHubClientTests(unittest.TestCase):

    def test_request_meta_returns_headers(self):
        client = gh.GitHubCodingClient(_Cfg())
        client.token = "tok"
        opener = lambda req, timeout: _fake_response(
            {"login": "octocat"},
            {"X-OAuth-Scopes": "repo, workflow"})
        client._opener = opener
        data, headers = client.request_meta("GET", "/user")
        self.assertEqual(data["login"], "octocat")
        self.assertIn("repo", headers["X-OAuth-Scopes"])

    def test_request_requires_auth_for_write(self):
        client = gh.GitHubCodingClient(_Cfg())
        client.token = ""
        with self.assertRaises(RuntimeError):
            client.request("POST", "/repos/a/b/issues", body={},
                           require_auth=True)

    def test_connect_validates_then_stores(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            _init_repo(ws)
            subprocess.run(
                ["git", "remote", "add", "origin",
                 "https://github.com/acme/thing.git"], cwd=ws,
                check=True, capture_output=True)
            reg = ToolRegistry(dict(PERMS))
            vault = FakeVault()
            gh.register_github_tools(reg, ws, _Cfg(), vault=vault)
            opener_payload = {"login": "afterburn25"}
            with patch.object(gh.GitHubCodingClient, "request_meta",
                              return_value=(opener_payload,
                                            {"X-OAuth-Scopes": "repo"})):
                out = json.loads(reg.get("github_connect").handler(
                    {"token": "ghp_secret"}))
            self.assertTrue(out["connected"])
            self.assertEqual(out["login"], "afterburn25")
            self.assertEqual(vault.get("github_token"), "ghp_secret")
            self.assertNotIn("ghp_secret", json.dumps(out))

    def test_disconnect_removes_vault_token(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            reg = ToolRegistry(dict(PERMS))
            vault = FakeVault()
            vault.set("github_token", "x")
            gh.register_github_tools(reg, ws, _Cfg(), vault=vault)
            out = json.loads(reg.get("github_disconnect").handler({}))
            self.assertTrue(out["disconnected"])
            self.assertIsNone(vault.get("github_token"))

    def test_auth_status_unauthorized_without_token(self):
        with tempfile.TemporaryDirectory() as td:
            import os
            os.environ.pop("GH_TEST_TOKEN", None)
            ws = Path(td)
            reg = ToolRegistry(dict(PERMS))
            gh.register_github_tools(reg, ws, _Cfg(), vault=FakeVault())
            out = json.loads(reg.get("github_auth_status").handler({}))
            self.assertFalse(out["authenticated"])
            self.assertIn("setup_required", out)


if __name__ == "__main__":
    unittest.main()
