"""First-class GitHub account service — one coherent account state for
the API, tools, connector, and capability surfaces.

The agent tools historically exposed ``github_connect``/``github_disconnect``
behind the ``github.write`` permission — connecting an account required
broad repository-write approval, and the connector/capability registries
each maintained their own idea of whether a credential existed (often
stale until a restart or first call).

This service is the single source of truth:

- token resolution: env var first, then encrypted SecretVault — the same
  precedence the connector uses; there is exactly one credential store
- connect(): validate against ``GET /user``, capture granted scopes,
  persist to the vault, then refresh every dependent surface immediately
  (shared client instance, connector registry auth flag)
- disconnect(): remove the vault credential and reset the shared client
  to any env-var credential
- status(): differentiated state — not_configured / connected /
  invalid_token / permission_blocked / network_error — never a blanket
  "connection failed"
- test(): real read-only evidence (user, repos, workspace repo metadata
  with push permission, actions access) — no writes are performed

The token is NEVER echoed, logged, written to config, embedded in git
remote URLs, or returned by any method.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable

_VAULT_KEY = "github_token"
# Status probe results are cached briefly so UI polling doesn't hit
# api.github.com on every render; connect/disconnect invalidate it.
_PROBE_TTL_S = 60.0
_HTTP_CODE_RE = re.compile(r"GitHub API (\d{3})")


def _scopes_from(headers: dict[str, str]) -> list[str]:
    raw = ""
    for k, v in (headers or {}).items():
        if k.lower() == "x-oauth-scopes":
            raw = v or ""
            break
    return [s.strip() for s in raw.split(",") if s.strip()]


class GitHubAccountService:
    def __init__(self, config: Any, vault: Any, *, client: Any,
                 workspace: Any = None,
                 slug_resolver: Callable[[], str | None] | None = None,
                 connectors: Any = None,
                 permission_check: Callable[[str], str] | None = None,
                 audit: Callable[[str, dict], None] | None = None,
                 probe_factory: Callable[[], Any] | None = None) -> None:
        self._config = config
        self._vault = vault
        self._client = client            # shared GitHubCodingClient
        self._probe_factory = probe_factory  # tests inject fakes
        self._workspace = workspace
        self._slug_resolver = slug_resolver
        self._connectors = connectors
        self._perm = permission_check or (lambda _p: "allow")
        self._audit = audit or (lambda _e, _d: None)
        self._lock = threading.RLock()
        self._probe_cache: dict[str, Any] | None = None
        self._probe_at = 0.0

    # -- token resolution --------------------------------------------------

    def _env_token(self) -> str:
        import os
        return os.environ.get(getattr(self._client, "token_env", "GITHUB_TOKEN"), "") or ""

    def _vault_token(self) -> str:
        try:
            return str(self._vault.get(_VAULT_KEY) or "") if self._vault else ""
        except Exception:
            return ""

    def _resolve_token(self) -> tuple[str, str]:
        """Returns (token, source) — env wins, matching connector semantics."""
        env = self._env_token()
        if env:
            return env, "env"
        vault = self._vault_token()
        if vault:
            return vault, "vault"
        return "", ""

    def authorized(self) -> bool:
        """Capability-registry hook: a credential exists at all — live,
        no cached negative state."""
        return bool(self._resolve_token()[0])

    def _client_token(self) -> None:
        """Pull the effective credential onto the shared client — tools
        and connector both read ``client.token``."""
        token, _ = self._resolve_token()
        if token and getattr(self._client, "token", "") != token:
            self._client.token = token

    def _refresh_connector(self) -> None:
        """Re-run connector authentication so ``authed`` reflects the
        credential that was just stored/removed — no restart, no stale
        negative cache."""
        try:
            rec = (self._connectors.connectors or {}).get("github")
            if rec is not None:
                rec["authed"] = bool(
                    rec["conn"].authenticate(self._connectors.vault))
        except Exception:
            pass

    def _invalidate(self) -> None:
        self._probe_cache = None
        self._probe_at = 0.0

    # -- error classification ----------------------------------------------

    @staticmethod
    def _classify(exc: Exception) -> tuple[str, str]:
        """Map a client RuntimeError to (state, user-facing detail)."""
        msg = str(exc)
        m = _HTTP_CODE_RE.search(msg)
        if m:
            code = int(m.group(1))
            if code == 401:
                return "invalid_token", "Invalid or expired token."
            if code == 403:
                if "rate limit" in msg.lower():
                    return "network_error", "GitHub rate limit reached — try again shortly."
                return "invalid_token", ("Token accepted but permission denied "
                                         "or blocked by policy (403).")
            if code == 404:
                return "network_error", f"GitHub API 404: {msg[-140:]}"
            return "network_error", f"GitHub API {code}: {msg[-140:]}"
        low = msg.lower()
        if any(k in low for k in ("unavailable", "timed out", "timeout",
                                  "name resolution", "ssl", "certificate",
                                  "refused", "unreachable")):
            return "network_error", f"Unable to reach api.github.com: {msg[-140:]}"
        return "network_error", msg[-160:]

    # -- state -------------------------------------------------------------

    def _workspace_slug(self) -> str:
        try:
            return str(self._slug_resolver() or "") \
                if self._slug_resolver else ""
        except Exception:
            return ""

    def _probe_user(self, client: Any) -> tuple[dict, list[str], str]:
        """GET /user → (data, scopes, error_state). error_state is '' on
        success or a state keyword on failure."""
        try:
            data, headers = client.request_meta("GET", "/user")
            return data or {}, _scopes_from(headers), ""
        except Exception as exc:
            state, _ = self._classify(exc)
            return {}, [], state

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            token, source = self._resolve_token()
            slug = self._workspace_slug()
            base = {
                "enabled": bool(getattr(self._config, "github_enabled", True)),
                "workspace_repo": slug or None,
                "source": source or None,
            }
            if not base["enabled"]:
                return {**base, "state": "disabled",
                        "detail": "GitHub integration is disabled in configuration."}
            if self._perm("github.read") == "deny":
                return {**base, "state": "permission_blocked",
                        "detail": "The active Nexus permission profile denies GitHub access."}
            if not token:
                return {**base, "state": "not_configured",
                        "detail": "No GitHub credential stored. Connect with a personal access token."}
            now = time.time()
            if (not refresh and self._probe_cache is not None
                    and now - self._probe_at < _PROBE_TTL_S):
                cached = dict(self._probe_cache)
                cached.update(base)
                cached["cached"] = True
                return cached
            self._client_token()
            data, scopes, err = self._probe_user(self._client)
            if err:
                state = err
                detail = ("Invalid or expired token." if err == "invalid_token"
                          else "Unable to reach api.github.com.")
                out = {**base, "state": state, "detail": detail}
            else:
                out = {**base, "state": "connected",
                       "login": data.get("login"),
                       "name": data.get("name"),
                       "scopes": scopes}
            self._probe_cache = dict(out)
            self._probe_at = now
            return out

    # -- mutations ---------------------------------------------------------

    def connect(self, token: str) -> dict[str, Any]:
        token = str(token or "").strip()
        if not token:
            return {"connected": False, "state": "not_configured",
                    "error": "token is required"}
        with self._lock:
            # Validate first — never persist an unverified credential.
            if self._probe_factory is not None:
                probe = self._probe_factory()
            else:
                from .tools.github import GitHubCodingClient
                probe = GitHubCodingClient(self._config)
            probe.token = token
            data, scopes, err = self._probe_user(probe)
            if err:
                detail = ("Invalid or expired token." if err == "invalid_token"
                          else "Unable to reach api.github.com — check "
                               "network, proxy, or firewall.")
                return {"connected": False, "state": err, "error": detail}
            login = str((data or {}).get("login") or "")
            if not login:
                return {"connected": False, "state": "invalid_token",
                        "error": "GitHub did not return an account identity."}
            if self._vault is None:
                return {"connected": False, "state": "vault_error",
                        "error": "Credential could not be stored securely — no secret vault."}
            try:
                self._vault.set(_VAULT_KEY, token,
                                description="GitHub personal access token")
            except Exception as exc:
                return {"connected": False, "state": "vault_error",
                        "error": f"Credential could not be stored securely: "
                                f"{type(exc).__name__}"}
            # Same-credential refresh: every surface converges on the
            # shared client + vault immediately — no restart.
            self._client.token = token
            self._refresh_connector()
            self._invalidate()
            self._audit("github_connect", {"login": login})
            st = self.status(refresh=True)
            return {"connected": True, **{k: v for k, v in st.items()
                                          if k != "cached"}}

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            removed = False
            if self._vault is not None:
                try:
                    removed = bool(self._vault.delete(_VAULT_KEY))
                except Exception:
                    pass
            # Env credentials are untouched — they were never Nexus-managed.
            self._client.token = self._env_token()
            self._refresh_connector()
            self._invalidate()
            self._audit("github_disconnect", {"removed_vault_token": removed})
            return {"disconnected": True,
                    "removed_vault_token": removed,
                    "env_credential_active": bool(self._env_token()),
                    "state": self.status()["state"]}

    # -- evidence ----------------------------------------------------------

    def list_repos(self, *, limit: int = 20,
                   visibility: str = "all") -> dict[str, Any]:
        if not self.authorized():
            return {"ok": False, "state": "not_configured",
                    "error": "No GitHub credential stored."}
        self._client_token()
        try:
            rows = self._client.request(
                "GET", "/user/repos",
                params={"per_page": max(1, min(50, int(limit))),
                        "visibility": visibility, "sort": "updated"},
                require_auth=True) or []
        except Exception as exc:
            state, detail = self._classify(exc)
            return {"ok": False, "state": state, "error": detail}
        return {"ok": True, "state": "connected", "repositories": [{
            "full_name": r.get("full_name"),
            "private": bool(r.get("private")),
            "default_branch": r.get("default_branch"),
            "updated_at": r.get("updated_at"),
            "description": r.get("description"),
        } for r in rows]}

    def test(self) -> dict[str, Any]:
        """Real read-only evidence: user → repos → workspace repo (with
        push permission) → actions access. No writes."""
        evidence: dict[str, Any] = {"steps": []}

        def step(name: str, ok: bool, detail: Any = None) -> bool:
            evidence["steps"].append(
                {"name": name, "ok": bool(ok), "detail": detail})
            return bool(ok)

        st = self.status(refresh=True)
        if st.get("state") != "connected":
            step("status", False, st.get("detail") or st.get("state"))
            evidence.update({"ok": False, "state": st.get("state"),
                             "detail": st.get("detail")})
            return evidence
        step("status", True, f"connected as {st.get('login')}")
        client = self._client
        try:
            repos = client.request("GET", "/user/repos",
                                   params={"per_page": 5,
                                           "sort": "updated"},
                                   require_auth=True) or []
            step("list_repositories", True,
                 f"{len(repos)} shown of accessible repos")
        except Exception as exc:
            state, detail = self._classify(exc)
            step("list_repositories", False, detail)
            evidence.update({"ok": False, "state": state, "detail": detail})
            return evidence
        slug = self._workspace_slug()
        if not slug:
            step("workspace_repo", True,
                 "this workspace is not linked to a GitHub remote")
            evidence["workspace_linked"] = False
            evidence["ok"] = True
            evidence["state"] = "connected"
            evidence["login"] = st.get("login")
            return evidence
        try:
            repo = client.request("GET", f"/repos/{slug}") or {}
            perms = repo.get("permissions") or {}
            step("workspace_repo", True, {
                "full_name": repo.get("full_name"),
                "private": repo.get("private"),
                "default_branch": repo.get("default_branch"),
                "permissions": {k: bool(perms.get(k)) for k in
                                ("pull", "push", "admin")},
            })
            if not perms.get("push"):
                evidence["warning"] = (
                    f"Connected to GitHub, but this token does not have "
                    f"permission to push to {slug}.")
        except Exception as exc:
            state, detail = self._classify(exc)
            step("workspace_repo", False, detail)
            evidence.update({"ok": False, "state": state,
                             "detail": detail})
            return evidence
        try:
            runs = client.request(
                "GET", f"/repos/{slug}/actions/runs",
                params={"per_page": 3}) or {}
            step("actions_access", True,
                 f"{len(runs.get('workflow_runs') or [])} recent runs readable")
        except Exception as exc:
            _state, detail = self._classify(exc)
            # Actions may be disabled on the repo — evidence, not failure.
            step("actions_access", False, detail)
        evidence["workspace_linked"] = True
        evidence["ok"] = True
        evidence["state"] = "connected"
        evidence["login"] = st.get("login")
        return evidence


def sanitize_log_line(line: str, token: str) -> str:
    """Defensive: guarantee a token can never appear in a log/audit line
    even if a future caller interpolates it."""
    if token:
        return line.replace(token, "***")
    return line
