"""GitHub account service — connect/disconnect/status/test semantics.

The surface is the first-class API the Settings UI and agent tools
share: one credential store (SecretVault), one shared client, live
connector/capability refresh, classified errors — never a blanket
"connection failed"."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from localcodeagent.config import AgentConfig
from localcodeagent.github_account import GitHubAccountService
from localcodeagent.connectors import ConnectorRegistry
from localcodeagent.connectors.github import GitHubConnector


class FakeVault:
    """In-memory stand-in with the SecretVault get/set/delete surface."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, description=""):
        self.store[key] = value

    def delete(self, key):
        return self.store.pop(key, None) is not None


class FakeClient:
    """Minimal GitHubCodingClient stand-in — request_meta/request driven
    by a routes table; tracks every call for token-leak assertions."""

    token_env = "GITHUB_TOKEN"

    def __init__(self, routes=None):
        self.token = ""
        self.routes = routes if routes is not None else {}
        self.calls = []

    @property
    def authenticated(self):
        return bool(self.token)

    def request_meta(self, method, path, **kw):
        self.calls.append((method, path, bool(self.token)))
        handler = self.routes.get((method, path))
        if handler is None:
            raise RuntimeError("GitHub API 404: not found")
        if isinstance(handler, Exception):
            raise handler
        data, headers = handler
        return data, headers

    def request(self, method, path, **kw):
        self.calls.append((method, path, bool(self.token)))
        handler = self.routes.get((method, path))
        if handler is None:
            raise RuntimeError("GitHub API 404: not found")
        if isinstance(handler, Exception):
            raise handler
        return handler[0] if isinstance(handler, tuple) else handler


USER = ({"login": "afterburn25", "name": "John H"},
        {"X-OAuth-Scopes": "repo, workflow"})
REPO = ({"full_name": "afterburn25/Coding_Agent", "private": True,
         "default_branch": "main",
         "permissions": {"pull": True, "push": True, "admin": True}}, {})
REPO_READONLY = ({"full_name": "afterburn25/Coding_Agent", "private": True,
                  "default_branch": "main",
                  "permissions": {"pull": True, "push": False}}, {})
RUNS = ({"workflow_runs": [{"id": 1, "name": "tests"}]}, {})


def make(routes=None, *, vault=None, slug="", connectors=None,
         perm=lambda p: "allow", client=None, probe=None):
    cfg = AgentConfig()
    client = client if client is not None else FakeClient(routes or {})
    svc = GitHubAccountService(
        cfg, vault if vault is not None else FakeVault(),
        client=client,
        slug_resolver=(lambda: slug) if slug else None,
        connectors=connectors,
        permission_check=perm,
        probe_factory=probe)
    return svc, client


def probe_with(routes):
    return lambda: FakeClient(routes)


class TestConnect(unittest.TestCase):
    ROUTES = {("GET", "/user"): USER,
              ("GET", "/user/repos"): ([], {}),
              ("GET", "/repos/o/r"): REPO,
              ("GET", "/repos/o/r/actions/runs"): RUNS}

    def setUp(self):
        os.environ.pop("GITHUB_TOKEN", None)

    def test_connect_valid_token(self):
        svc, client = make(self.ROUTES,
                           probe=probe_with(self.ROUTES))
        out = svc.connect("ghp_test123")
        self.assertTrue(out["connected"])
        self.assertEqual(out["login"], "afterburn25")
        self.assertEqual(out["state"], "connected")
        self.assertIn("repo", out["scopes"])

    def test_connect_never_echoes_token(self):
        svc, _ = make(self.ROUTES, probe=probe_with(self.ROUTES))
        token = "ghp_secret_abcdef"
        out = svc.connect(token)
        self.assertNotIn(token, json.dumps(out))

    def test_connect_invalid_token_not_stored(self):
        routes = {("GET", "/user"): RuntimeError("GitHub API 401: bad credentials")}
        vault = FakeVault()
        svc, _ = make(vault=vault, probe=probe_with(routes))
        out = svc.connect("ghp_bad")
        self.assertFalse(out["connected"])
        self.assertEqual(out["state"], "invalid_token")
        self.assertIn("expired", out["error"])
        self.assertIsNone(vault.get("github_token"))

    def test_connect_network_error_classified(self):
        routes = {("GET", "/user"): RuntimeError(
            "GitHub API unavailable: <urlopen error [Errno 11001] getaddrinfo failed>")}
        svc, _ = make(probe=probe_with(routes))
        out = svc.connect("ghp_x")
        self.assertFalse(out["connected"])
        self.assertEqual(out["state"], "network_error")
        self.assertIn("api.github.com", out["error"])

    def test_connect_vault_failure_classified(self):
        class BadVault(FakeVault):
            def set(self, *a, **k):
                raise OSError("locked")
        svc, _ = make(self.ROUTES, vault=BadVault(),
                      probe=probe_with(self.ROUTES))
        out = svc.connect("ghp_ok")
        self.assertFalse(out["connected"])
        self.assertEqual(out["state"], "vault_error")
        self.assertIn("securely", out["error"])

    def test_vault_persistence_across_instances(self):
        vault = FakeVault()
        svc, _ = make(self.ROUTES, vault=vault,
                      probe=probe_with(self.ROUTES))
        svc.connect("ghp_persist")
        # "Restart": a brand-new service over the same vault is already
        # authorized — no re-connect needed.
        svc2, client2 = make(self.ROUTES, vault=vault)
        self.assertTrue(svc2.authorized())
        st = svc2.status(refresh=True)
        self.assertEqual(st["state"], "connected")
        self.assertEqual(st["login"], "afterburn25")

    def test_immediate_connector_refresh(self):
        routes = dict(self.ROUTES)
        reg = ConnectorRegistry(vault=FakeVault())
        conn = GitHubConnector(client_factory=lambda: FakeClient(routes),
                               slug="")
        reg.register(conn)
        # Before connect: no credential → authed false.
        self.assertFalse(reg.status()[0]["authed"])
        svc, _ = make(routes, vault=reg.vault, connectors=reg,
                      probe=probe_with(routes))
        svc.connect("ghp_live")
        # After connect: connector reports authed without restart/call.
        self.assertTrue(reg.status()[0]["authed"])
        self.assertEqual(reg.status()[0].get("credential_source"), "vault")

    def test_capability_flag_live(self):
        svc, _ = make(self.ROUTES, probe=probe_with(self.ROUTES))
        self.assertFalse(svc.authorized())
        svc.connect("ghp_cap")
        self.assertTrue(svc.authorized())
        svc.disconnect()
        self.assertFalse(svc.authorized())

    def test_disconnect_removes_vault_and_refreshes(self):
        vault = FakeVault()
        reg = ConnectorRegistry(vault=vault)
        conn = GitHubConnector(client_factory=lambda: FakeClient({}),
                               slug="")
        reg.register(conn)
        svc, client = make(self.ROUTES, vault=vault, connectors=reg,
                           probe=probe_with(self.ROUTES))
        svc.connect("ghp_gone")
        out = svc.disconnect()
        self.assertTrue(out["disconnected"])
        self.assertTrue(out["removed_vault_token"])
        self.assertFalse(out["env_credential_active"])
        self.assertIsNone(vault.get("github_token"))
        self.assertFalse(reg.status()[0]["authed"])
        self.assertEqual(svc.status()["state"], "not_configured")

    def test_env_token_survives_disconnect(self):
        os.environ["GITHUB_TOKEN"] = "env_tok"
        try:
            svc, client = make(self.ROUTES, probe=probe_with(self.ROUTES))
            svc.connect("ghp_vault")
            out = svc.disconnect()
            self.assertTrue(out["env_credential_active"])
            self.assertEqual(client.token, "env_tok")
        finally:
            os.environ.pop("GITHUB_TOKEN", None)


class TestStatus(unittest.TestCase):
    ROUTES = {("GET", "/user"): USER}

    def setUp(self):
        os.environ.pop("GITHUB_TOKEN", None)

    def test_not_configured(self):
        svc, _ = make()
        st = svc.status()
        self.assertEqual(st["state"], "not_configured")

    def test_permission_blocked(self):
        svc, _ = make(perm=lambda p: "deny")
        svc._vault.set("github_token", "x")
        st = svc.status()
        self.assertEqual(st["state"], "permission_blocked")

    def test_disabled(self):
        svc, _ = make()
        svc._config.github_enabled = False
        st = svc.status()
        self.assertEqual(st["state"], "disabled")

    def test_invalid_cached_then_refresh(self):
        routes = {("GET", "/user"): RuntimeError("GitHub API 401: bad")}
        svc, _ = make(routes)
        svc._vault.set("github_token", "stale")
        st = svc.status(refresh=True)
        self.assertEqual(st["state"], "invalid_token")

    def test_no_remote_does_not_break_status(self):
        svc, _ = make(self.ROUTES)
        svc._vault.set("github_token", "x")
        st = svc.status(refresh=True)
        self.assertEqual(st["state"], "connected")
        self.assertIsNone(st["workspace_repo"])


class TestReposAndTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GITHUB_TOKEN", None)

    ROUTES = {
        ("GET", "/user"): USER,
        ("GET", "/user/repos"): ([{"full_name": "a/b", "private": False,
                                   "default_branch": "main",
                                   "updated_at": "t", "description": "d"}], {}),
        ("GET", "/repos/o/r"): REPO,
        ("GET", "/repos/o/r/actions/runs"): RUNS,
    }

    def test_list_repos(self):
        svc, _ = make(self.ROUTES, probe=probe_with(self.ROUTES))
        svc.connect("ghp_x")
        out = svc.list_repos()
        self.assertTrue(out["ok"])
        self.assertEqual(out["repositories"][0]["full_name"], "a/b")

    def test_list_repos_unauthenticated(self):
        svc, _ = make(self.ROUTES)
        out = svc.list_repos()
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "not_configured")

    def test_test_evidence_full_chain(self):
        svc, _ = make(self.ROUTES, slug="o/r",
                      probe=probe_with(self.ROUTES))
        svc.connect("ghp_x")
        out = svc.test()
        self.assertTrue(out["ok"])
        self.assertTrue(out["workspace_linked"])
        names = [s["name"] for s in out["steps"]]
        self.assertEqual(names[:3],
                         ["status", "list_repositories", "workspace_repo"])
        self.assertIn("actions_access", names)

    def test_test_no_workspace_remote(self):
        svc, _ = make(self.ROUTES, probe=probe_with(self.ROUTES))
        svc.connect("ghp_x")
        out = svc.test()
        self.assertTrue(out["ok"])
        self.assertFalse(out["workspace_linked"])
        repo_step = next(s for s in out["steps"]
                         if s["name"] == "workspace_repo")
        self.assertTrue(repo_step["ok"])
        self.assertIn("not linked", repo_step["detail"])

    def test_readonly_token_reports_missing_push(self):
        routes = dict(self.ROUTES)
        routes[("GET", "/repos/o/r")] = REPO_READONLY
        svc, _ = make(routes, slug="o/r", probe=probe_with(routes))
        svc.connect("ghp_ro")
        out = svc.test()
        self.assertTrue(out["ok"])
        self.assertIn("does not have permission to push", out["warning"])


class TestToolPermissionMapping(unittest.TestCase):
    """The agent-tool surface must not require github.write to store a
    credential — connecting is credential management, not repo write."""

    def test_connect_disconnect_use_credentials_permission(self):
        from localcodeagent.tools.base import ToolRegistry
        from localcodeagent.tools.github import register_github_tools
        reg = ToolRegistry({})
        register_github_tools(reg, Path(tempfile.mkdtemp()),
                              AgentConfig(), vault=FakeVault())
        self.assertEqual(
            reg.get("github_connect").permission, "credentials.use")
        self.assertEqual(
            reg.get("github_disconnect").permission, "credentials.use")
        # Writes keep the strong gate.
        self.assertEqual(
            reg.get("github_create_pull_request").permission, "github.write")

    def test_tool_connect_delegates_to_account_service(self):
        from localcodeagent.tools.base import ToolRegistry
        from localcodeagent.tools.github import register_github_tools
        reg = ToolRegistry({})
        client = FakeClient({("GET", "/user"): USER})
        svc, _ = make({("GET", "/user"): USER}, client=client,
                      probe=probe_with({("GET", "/user"): USER}))
        register_github_tools(reg, Path(tempfile.mkdtemp()),
                              AgentConfig(), vault=svc._vault,
                              client=client, account=svc)
        out = json.loads(reg.get("github_connect").handler(
            {"token": "ghp_tool"}))
        self.assertTrue(out["connected"])
        self.assertEqual(client.token, "ghp_tool")
        self.assertNotIn("ghp_tool", json.dumps(out))


class TestTokenHygiene(unittest.TestCase):
    def setUp(self):
        os.environ.pop("GITHUB_TOKEN", None)

    def test_token_never_in_status_or_test(self):
        routes = {("GET", "/user"): USER,
                  ("GET", "/user/repos"): ([], {}),
                  ("GET", "/repos/o/r"): REPO,
                  ("GET", "/repos/o/r/actions/runs"): RUNS}
        vault = FakeVault()
        svc, _ = make(routes, vault=vault, slug="o/r",
                      probe=probe_with(routes))
        svc.connect("ghp_hygiene_token")
        blob = json.dumps([svc.status(), svc.test(), svc.list_repos()])
        self.assertNotIn("ghp_hygiene_token", blob)


if __name__ == "__main__":
    unittest.main()
