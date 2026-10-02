"""GitHub connector — first-class named-service access to the repo host.

Wraps ``GitHubCodingClient`` (dependency-free REST) behind the Connector
contract so GitHub calls share the registry's rate limiting, permission
gate, health probe, and audit history instead of going through tools
directly. Read capabilities need no token; writes require one and are
rejected at the client layer when it is missing. Credentials resolve from
the configured token env var first, then a ``github_token`` entry in the
encrypted SecretVault.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import Connector


class GitHubConnector(Connector):
    name = "github"
    capabilities = ("repo", "list_issues", "ci_status", "create_issue")
    permission = "external_api.call"
    rate_limit_per_min = 30

    def __init__(self, client_factory: Callable[[], Any],
                 slug: Callable[[], str | None] | str = "") -> None:
        # client_factory is lazy — the token env var may be set after AppState
        # boots, so the client must be rebuilt per call rather than captured.
        self._client_factory = client_factory
        self._slug = slug
        self._vault: Any = None

    def _client(self) -> Any:
        client = self._client_factory()
        if not getattr(client, "token", "") and self._vault is not None:
            client.token = self._vault.get("github_token") or ""
        return client

    def _repo_slug(self, override: Any = None) -> str:
        slug = str(override or (self._slug() if callable(self._slug)
                                else self._slug) or "").strip()
        if not slug:
            raise RuntimeError("no repository slug — pass 'slug' or set a "
                               "git origin remote")
        return slug

    # -- Connector contract -------------------------------------------------

    def authenticate(self, vault: Any) -> bool:
        # Capture the vault for token fallback. Reads work without a token;
        # authentication reflects whether a credential resolves, not whether
        # one is required.
        self._vault = vault
        return True

    def health(self) -> dict[str, Any]:
        t0 = time.time()
        try:
            data = self._client().request("GET", "/rate_limit")
            core = (data or {}).get("resources", {}).get("core", {})
            return {"ok": True,
                    "latency_ms": round((time.time() - t0) * 1000),
                    "remaining": core.get("remaining"),
                    "authenticated": bool(self._client().authenticated)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200],
                    "latency_ms": round((time.time() - t0) * 1000)}

    def call(self, capability: str, **params: Any) -> dict[str, Any]:
        client = self._client()
        if capability == "repo":
            slug = self._repo_slug(params.get("slug"))
            data = client.request("GET", f"/repos/{slug}")
            return {"ok": True, "repo": {
                "full_name": data.get("full_name"),
                "default_branch": data.get("default_branch"),
                "open_issues": data.get("open_issues_count"),
                "stars": data.get("stargazers_count"),
                "private": data.get("private")}}
        if capability == "list_issues":
            slug = self._repo_slug(params.get("slug"))
            data = client.request(
                "GET", f"/repos/{slug}/issues",
                params={"state": str(params.get("state") or "open"),
                        "per_page": min(50, int(params.get("limit") or 20))})
            issues = [{"number": i.get("number"), "title": i.get("title"),
                       "state": i.get("state"),
                       "labels": [l.get("name") for l in
                                  i.get("labels") or []]}
                      for i in (data or []) if isinstance(i, dict)
                      and "pull_request" not in i]
            return {"ok": True, "issues": issues, "count": len(issues)}
        if capability == "ci_status":
            slug = self._repo_slug(params.get("slug"))
            ref = str(params.get("ref") or "HEAD")
            data = client.request(
                "GET", f"/repos/{slug}/actions/runs",
                params={"per_page": min(20, int(params.get("limit") or 5))})
            runs = [{"id": r.get("id"), "name": r.get("name"),
                     "status": r.get("status"),
                     "conclusion": r.get("conclusion"),
                     "branch": r.get("head_branch")}
                    for r in (data or {}).get("workflow_runs", [])]
            return {"ok": True, "runs": runs, "count": len(runs),
                    "ref": ref}
        if capability == "create_issue":
            slug = self._repo_slug(params.get("slug"))
            title = str(params.get("title") or "").strip()
            if not title:
                return {"ok": False, "error": "title required"}
            data = client.request(
                "POST", f"/repos/{slug}/issues",
                body={"title": title[:200],
                      "body": str(params.get("body") or "")[:8000]},
                require_auth=True)
            return {"ok": True, "issue": {"number": data.get("number"),
                                          "url": data.get("html_url")}}
        return {"ok": False, "error": f"unknown capability '{capability}'"}
