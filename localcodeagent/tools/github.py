from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..config import AgentConfig
from .base import ToolRegistry, ToolSpec


def _run_git(workspace: Path, args: list[str], *, timeout: int = 60, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    output = (proc.stdout + proc.stderr).strip()
    if check and proc.returncode != 0:
        raise RuntimeError(output or f"git {' '.join(args)} failed with exit code {proc.returncode}")
    return output


def _repo_slug_from_remote(remote_url: str) -> str:
    """Convert common GitHub HTTPS/SSH remotes to owner/repo."""
    value = str(remote_url or "").strip()
    patterns = (
        r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
        r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
        r"^git://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    raise ValueError("Git remote is not a supported github.com repository URL")


def _current_branch(workspace: Path) -> str:
    branch = _run_git(workspace, ["branch", "--show-current"])
    if not branch:
        raise RuntimeError("Git repository is in detached HEAD state")
    return branch


def _repo_slug(workspace: Path, remote: str = "origin") -> str:
    url = _run_git(workspace, ["config", "--get", f"remote.{remote}.url"])
    if not url:
        raise RuntimeError(f"Git remote '{remote}' is not configured")
    return _repo_slug_from_remote(url)


def _safe_git_paths(paths: list[Any]) -> list[str]:
    cleaned: list[str] = []
    for raw in paths:
        value = str(raw or "").strip().replace("\\", "/")
        if not value:
            continue
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe Git path: {value}")
        if path.parts and path.parts[0] == ".agent":
            raise ValueError("Nexus Core .agent metadata cannot be staged by the coding agent")
        cleaned.append(value)
    if not cleaned:
        raise ValueError("at least one repository path is required")
    return cleaned


class GitHubCodingClient:
    """Small dependency-free REST client for explicit GitHub coding actions."""

    def __init__(self, config: AgentConfig, *, opener=None) -> None:
        self.base_url = config.github_api_url.rstrip("/")
        self.api_version = config.github_api_version
        self.token_env = config.github_token_env
        self.token = os.environ.get(self.token_env, "")
        self.timeout = max(3, int(config.github_timeout))
        self._opener = opener

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "Chat-Nexus/0.6",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request_meta(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        require_auth: bool = False,
    ) -> tuple[Any, dict[str, str]]:
        """Like request() but also returns response headers — needed for
        X-OAuth-Scopes on the auth-status surface."""
        if require_auth and not self.authenticated:
            raise RuntimeError(
                f"GitHub write action requires a token in environment variable {self.token_env}"
            )
        if not path.startswith("/"):
            path = "/" + path
        query = urlencode({k: v for k, v in (params or {}).items() if v not in {None, ""}})
        url = self.base_url + path + ("?" + query if query else "")
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = self._headers()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method.upper())
        try:
            if self._opener is None:
                response = urlopen(request, timeout=self.timeout)
            else:
                response = self._opener(request, self.timeout)
            with response as handle:
                raw = handle.read()
                data = json.loads(raw.decode("utf-8")) if raw else None
                return data, dict(handle.headers.items())
        except HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = str(data.get("message") or exc.reason)
            except Exception:
                detail = str(exc.reason)
            raise RuntimeError(f"GitHub API {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"GitHub API unavailable: {exc}") from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        require_auth: bool = False,
    ) -> Any:
        data, _ = self.request_meta(
            method, path, params=params, body=body,
            require_auth=require_auth)
        return data


def register_github_tools(registry: ToolRegistry, workspace: Path,
                          config: AgentConfig, *, vault=None,
                          workspaces=None, client=None,
                          account=None) -> None:
    # `client`: shared GitHubCodingClient — the account service refreshes
    # its token on connect/disconnect, so every surface (tools, connector,
    # API) converges without a restart. Falls back to a private instance
    # for standalone use.
    client = client if client is not None else GitHubCodingClient(config)
    remote_default = config.github_default_remote or "origin"

    def _ensure_token() -> None:
        """Vault fallback — a token connected after boot authenticates
        without a restart; env var always wins."""
        if not client.token and vault is not None:
            try:
                client.token = vault.get("github_token") or ""
            except Exception:
                pass

    def git_current_branch(_: dict) -> str:
        return _current_branch(workspace)

    def git_create_branch(args: dict) -> str:
        branch = str(args.get("branch", "")).strip()
        if not branch:
            raise ValueError("branch is required")
        _run_git(workspace, ["check-ref-format", "--branch", branch])
        _run_git(workspace, ["switch", "-c", branch])
        return json.dumps({"branch": branch, "status": "created"}, indent=2)

    def git_commit(args: dict) -> str:
        message = str(args.get("message", "")).strip()
        if not message:
            raise ValueError("commit message is required")
        paths = _safe_git_paths(list(args.get("paths") or []))
        _run_git(workspace, ["add", "--", *paths])
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if staged.returncode == 0:
            raise RuntimeError("No staged changes to commit")
        if staged.returncode not in {0, 1}:
            raise RuntimeError((staged.stdout + staged.stderr).strip() or "git diff --cached failed")
        output = _run_git(workspace, ["commit", "-m", message], timeout=120)
        sha = _run_git(workspace, ["rev-parse", "HEAD"])
        return json.dumps({"commit": sha, "message": message, "output": output[-8000:]}, indent=2)

    def git_push(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        branch = str(args.get("branch") or "").strip() or _current_branch(workspace)
        _ensure_token()
        command = []
        if client.authenticated:
            # Header-scoped auth — same pattern as github_clone: the
            # connected credential drives the push without landing in
            # .git/config or the process-visible remote URL.
            command += ["-c",
                        f"http.extraHeader=AUTHORIZATION: bearer {client.token}"]
        command += ["push"]
        if bool(args.get("set_upstream", True)):
            command.extend(["--set-upstream", remote, branch])
        else:
            command.extend([remote, branch])
        output = _run_git(workspace, command, timeout=180)
        return json.dumps({"remote": remote, "branch": branch, "output": output[-8000:]}, indent=2)

    def github_repository(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        data = client.request("GET", f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}")
        return json.dumps({
            "repository": repo,
            "default_branch": data.get("default_branch"),
            "visibility": data.get("visibility") or ("private" if data.get("private") else "public"),
            "html_url": data.get("html_url"),
            "authenticated": client.authenticated,
        }, indent=2)

    def github_list_issues(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        state = str(args.get("state") or "open")
        if state not in {"open", "closed", "all"}:
            raise ValueError("state must be open, closed, or all")
        limit = max(1, min(50, int(args.get("limit") or 10)))
        rows = client.request(
            "GET",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues",
            params={"state": state, "per_page": limit},
        ) or []
        compact = [{
            "number": row.get("number"),
            "title": row.get("title"),
            "state": row.get("state"),
            "url": row.get("html_url"),
            "type": "pull_request" if row.get("pull_request") else "issue",
        } for row in rows[:limit]]
        return json.dumps({"repository": repo, "items": compact}, indent=2)

    def github_create_issue(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        title = str(args.get("title", "")).strip()
        if not title:
            raise ValueError("title is required")
        body = str(args.get("body") or "")
        data = client.request(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues",
            body={"title": title, "body": body},
            require_auth=True,
        )
        return json.dumps({
            "repository": repo,
            "number": data.get("number"),
            "title": data.get("title"),
            "url": data.get("html_url"),
        }, indent=2)

    def github_create_pull_request(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        head = str(args.get("head") or "").strip() or _current_branch(workspace)
        title = str(args.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        body_text = str(args.get("body") or "")
        repo_info = client.request("GET", f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}")
        base = str(args.get("base") or "").strip() or str(repo_info.get("default_branch") or "main")
        if head == base:
            raise ValueError("pull request head branch must differ from base branch")
        data = client.request(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls",
            body={
                "title": title,
                "head": head,
                "base": base,
                "body": body_text,
                "draft": bool(args.get("draft", False)),
            },
            require_auth=True,
        )
        return json.dumps({
            "repository": repo,
            "number": data.get("number"),
            "title": data.get("title"),
            "head": head,
            "base": base,
            "draft": data.get("draft"),
            "url": data.get("html_url"),
        }, indent=2)

    def github_ci_status(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        branch = str(args.get("branch") or "").strip() or _current_branch(workspace)
        limit = max(1, min(20, int(args.get("limit") or 5)))
        data = client.request(
            "GET",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/actions/runs",
            params={"branch": branch, "per_page": limit},
        ) or {}
        rows = []
        for run in list(data.get("workflow_runs") or [])[:limit]:
            rows.append({
                "id": run.get("id"),
                "name": run.get("name"),
                "title": run.get("display_title"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "sha": run.get("head_sha"),
                "url": run.get("html_url"),
                "created_at": run.get("created_at"),
            })
        return json.dumps({"repository": repo, "branch": branch, "runs": rows}, indent=2)

    # -- authenticated GitHub surface --------------------------------------

    def github_auth_status(_: dict) -> str:
        _ensure_token()
        if not client.authenticated:
            return json.dumps({
                "authenticated": False,
                "setup_required": f"token in env {client.token_env} "
                                  "or github_connect",
            }, indent=2)
        data, headers = client.request_meta("GET", "/user")
        scopes = headers.get("X-OAuth-Scopes") or \
            headers.get("x-oauth-scopes") or ""
        return json.dumps({
            "authenticated": True,
            "login": (data or {}).get("login"),
            "name": (data or {}).get("name"),
            "scopes": [s.strip() for s in scopes.split(",") if s.strip()],
        }, indent=2)

    def github_connect(args: dict) -> str:
        token = str(args.get("token") or "").strip()
        if not token:
            raise ValueError("token is required")
        if account is not None:
            # Account service is the single connect path — vault store,
            # shared-client refresh, connector refresh, classified errors.
            return json.dumps(account.connect(token), indent=2)
        # Validate against /user before persisting anything.
        probe = GitHubCodingClient(config)
        probe.token = token
        try:
            data, headers = probe.request_meta("GET", "/user")
        except RuntimeError as exc:
            return json.dumps({"connected": False,
                               "error": str(exc)}, indent=2)
        if vault is None:
            return "ERROR: no secret vault — cannot store credential"
        vault.set("github_token", token,
                  description="GitHub personal access token")
        client.token = token
        scopes = headers.get("X-OAuth-Scopes") or \
            headers.get("x-oauth-scopes") or ""
        return json.dumps({
            "connected": True,
            "login": (data or {}).get("login"),
            "scopes": [s.strip() for s in scopes.split(",") if s.strip()],
        }, indent=2)

    def github_disconnect(_: dict) -> str:
        if account is not None:
            return json.dumps(account.disconnect(), indent=2)
        removed = False
        if vault is not None:
            try:
                removed = bool(vault.delete("github_token"))
            except Exception:
                pass
        client.token = os.environ.get(client.token_env, "")
        return json.dumps({"disconnected": True,
                           "removed_stored_token": removed}, indent=2)

    def github_list_repos(args: dict) -> str:
        _ensure_token()
        limit = max(1, min(50, int(args.get("limit") or 20)))
        visibility = str(args.get("visibility") or "all").lower()
        if visibility not in {"all", "public", "private"}:
            raise ValueError("visibility must be all, public, or private")
        rows = client.request("GET", "/user/repos", params={
            "per_page": limit, "visibility": visibility,
            "sort": "updated"}, require_auth=True) or []
        return json.dumps({"repositories": [{
            "full_name": r.get("full_name"),
            "private": bool(r.get("private")),
            "default_branch": r.get("default_branch"),
            "updated_at": r.get("updated_at"),
            "description": r.get("description"),
        } for r in rows[:limit]]}, indent=2)

    def github_clone(args: dict) -> str:
        slug = str(args.get("repo") or "").strip().rstrip("/")
        if slug.endswith(".git"):
            slug = slug[:-4]
        if "/" not in slug:
            raise ValueError("repo must be owner/name or a GitHub URL")
        if "github.com" in slug:
            slug = slug.split("github.com/", 1)[-1] \
                .split("github.com:", 1)[-1].lstrip("/")
        raw_dest = str(args.get("dest") or "").strip()
        if not raw_dest:
            raise ValueError("dest directory is required")
        dest = Path(raw_dest).expanduser()
        dest = (workspace / dest).resolve() if not dest.is_absolute() \
            else dest.resolve()
        roots = [workspace.resolve()]
        if workspaces is not None:
            try:
                roots = workspaces.allowed_roots()
            except Exception:
                pass
        parent = dest.parent
        if not any(parent == b or b in parent.parents for b in roots) \
                and not any(dest == b or b in dest.parents for b in roots):
            raise ValueError(
                "clone destination must be inside a registered workspace")
        if dest.exists() and any(dest.iterdir()):
            return f"ERROR: destination is not empty: {dest}"
        _ensure_token()
        url = f"https://github.com/{slug}.git"
        argv = ["git"]
        if client.authenticated:
            # Header-scoped auth: the token never lands in .git/config or
            # the process-visible remote URL.
            argv += ["-c",
                     f"http.extraHeader=AUTHORIZATION: bearer {client.token}"]
        argv += ["clone", url, str(dest)]
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(
                f"git clone failed: {(proc.stderr or proc.stdout)[-2000:]}")
        if workspaces is not None:
            try:
                workspaces.open(dest)
            except Exception:
                pass
        return json.dumps({"cloned": slug, "dest": str(dest)},
                          indent=2)

    def github_pull_request(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        num = int(args.get("number") or 0)
        if num <= 0:
            raise ValueError("number is required")
        _ensure_token()
        oq, nq = quote(owner, safe=''), quote(name, safe='')
        pr = client.request("GET", f"/repos/{oq}/{nq}/pulls/{num}") or {}
        files = client.request(
            "GET", f"/repos/{oq}/{nq}/pulls/{num}/files",
            params={"per_page": 50}) or []
        reviews = client.request(
            "GET", f"/repos/{oq}/{nq}/pulls/{num}/reviews",
            params={"per_page": 20}) or []
        checks = client.request(
            "GET", f"/repos/{oq}/{nq}/commits/"
            f"{pr.get('head', {}).get('sha', '')}/check-runs",
            params={"per_page": 20}) or {}
        return json.dumps({
            "number": pr.get("number"), "title": pr.get("title"),
            "state": pr.get("state"), "draft": pr.get("draft"),
            "mergeable": pr.get("mergeable"),
            "mergeable_state": pr.get("mergeable_state"),
            "head": pr.get("head", {}).get("ref"),
            "base": pr.get("base", {}).get("ref"),
            "url": pr.get("html_url"),
            "files": [{"file": f.get("filename"),
                       "status": f.get("status"),
                       "changes": f.get("changes")}
                      for f in files[:50]],
            "reviews": [{"user": (r.get("user") or {}).get("login"),
                         "state": r.get("state")} for r in reviews[:20]],
            "checks": [{"name": c.get("name"),
                        "status": c.get("status"),
                        "conclusion": c.get("conclusion")}
                       for c in (checks.get("check_runs") or [])[:20]],
        }, indent=2)

    def github_actions_run(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        run_id = int(args.get("run_id") or 0)
        if run_id <= 0:
            raise ValueError("run_id is required")
        _ensure_token()
        oq, nq = quote(owner, safe=''), quote(name, safe='')
        run = client.request(
            "GET", f"/repos/{oq}/{nq}/actions/runs/{run_id}") or {}
        jobs = client.request(
            "GET", f"/repos/{oq}/{nq}/actions/runs/{run_id}/jobs",
            params={"per_page": 30}) or {}
        return json.dumps({
            "run": {"id": run.get("id"), "name": run.get("name"),
                    "title": run.get("display_title"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "sha": run.get("head_sha"),
                    "url": run.get("html_url")},
            "jobs": [{"id": j.get("id"), "name": j.get("name"),
                      "status": j.get("status"),
                      "conclusion": j.get("conclusion"),
                      "failed_steps": [s.get("name") for s in
                                       (j.get("steps") or [])
                                       if s.get("conclusion") == "failure"]}
                     for j in (jobs.get("jobs") or [])[:30]],
        }, indent=2)

    def github_rerun(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        run_id = int(args.get("run_id") or 0)
        if run_id <= 0:
            raise ValueError("run_id is required")
        _ensure_token()
        client.request(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/actions/runs/{run_id}/rerun", require_auth=True)
        return json.dumps({"rerun": run_id, "repository": repo}, indent=2)

    def github_issue_comment(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        num = int(args.get("number") or 0)
        body_text = str(args.get("body") or "").strip()
        if num <= 0 or not body_text:
            raise ValueError("number and body are required")
        _ensure_token()
        data = client.request(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{num}/comments", body={"body": body_text},
            require_auth=True) or {}
        return json.dumps({"commented": num, "url": data.get("html_url")},
                          indent=2)

    def github_compare(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        repo = _repo_slug(workspace, remote)
        owner, name = repo.split("/", 1)
        base = str(args.get("base") or "").strip()
        head = str(args.get("head") or "").strip()
        if not base or not head:
            raise ValueError("base and head are required")
        _ensure_token()
        data = client.request(
            "GET", f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/compare/{quote(base, safe='')}...{quote(head, safe='')}") or {}
        return json.dumps({
            "repository": repo, "base": base, "head": head,
            "status": data.get("status"),
            "ahead_by": data.get("ahead_by"),
            "behind_by": data.get("behind_by"),
            "files": [{"file": f.get("filename"),
                       "status": f.get("status")}
                      for f in (data.get("files") or [])[:50]],
        }, indent=2)

    def github_checkout_pr(args: dict) -> str:
        num = int(args.get("number") or 0)
        if num <= 0:
            raise ValueError("number is required")
        remote = str(args.get("remote") or remote_default).strip()
        branch = f"pr-{num}"
        out = _run_git(workspace, [
            "fetch", remote, f"pull/{num}/head:{branch}"], timeout=120)
        _run_git(workspace, ["switch", branch])
        return json.dumps({"checked_out": branch, "pr": num,
                           "output": out[-2000:]}, indent=2)

    registry.register(ToolSpec("git_current_branch", "Show the current local Git branch.", {
        "type": "object", "properties": {}
    }, "filesystem.read", git_current_branch))

    registry.register(ToolSpec("git_create_branch", "Create and switch to a new local Git branch.", {
        "type": "object",
        "properties": {"branch": {"type": "string"}},
        "required": ["branch"],
    }, "git.execute", git_create_branch))

    registry.register(ToolSpec("git_commit", "Stage explicit repository paths and create a local Git commit.", {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["message", "paths"],
    }, "git.execute", git_commit))

    registry.register(ToolSpec("git_push", "Push a local branch to a configured GitHub remote.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "branch": {"type": "string"},
            "set_upstream": {"type": "boolean"},
        },
    }, "github.write", git_push))

    registry.register(ToolSpec("github_repository", "Read metadata for the GitHub repository configured as a local Git remote.", {
        "type": "object", "properties": {"remote": {"type": "string"}}
    }, "github.read", github_repository))

    registry.register(ToolSpec("github_list_issues", "List GitHub issues and pull requests for the current repository.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    }, "github.read", github_list_issues))

    registry.register(ToolSpec("github_create_issue", "Create a GitHub issue in the current repository.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["title"],
    }, "github.write", github_create_issue))

    registry.register(ToolSpec("github_create_pull_request", "Open a GitHub pull request from a pushed branch.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
            "head": {"type": "string"},
            "base": {"type": "string"},
            "draft": {"type": "boolean"},
        },
        "required": ["title"],
    }, "github.write", github_create_pull_request))

    registry.register(ToolSpec("github_ci_status", "Show recent GitHub Actions runs for the current repository branch.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "branch": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    }, "github.read", github_ci_status))

    registry.register(ToolSpec("github_auth_status", "Show GitHub connection state: authenticated account, granted scopes, or setup instructions.", {
        "type": "object", "properties": {}
    }, "github.read", github_auth_status, category="github",
        capabilities=["github_auth", "connection_status"]))

    registry.register(ToolSpec("github_connect", "Connect GitHub: validate a personal access token against /user then store it in the encrypted local vault. Never echoes the token back.", {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
    }, "credentials.use", github_connect, category="github",
        capabilities=["github_auth", "connect_account"]))

    registry.register(ToolSpec("github_disconnect", "Disconnect GitHub: remove the stored vault token (env-var credentials are untouched).", {
        "type": "object", "properties": {}
    }, "credentials.use", github_disconnect, category="github",
        capabilities=["github_auth", "disconnect_account"]))

    registry.register(ToolSpec("github_list_repos", "List the connected account's repositories (requires github_connect or env token).", {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 20},
            "visibility": {"type": "string",
                           "enum": ["all", "public", "private"]},
        },
    }, "github.read", github_list_repos, category="github",
        capabilities=["github_repos", "list_repositories"]))

    registry.register(ToolSpec("github_clone", "Clone a GitHub repository into a registered workspace (dest parent must be inside a registered root). Uses header-scoped auth — the token never persists in .git/config.", {
        "type": "object",
        "properties": {
            "repo": {"type": "string",
                     "description": "owner/name or GitHub URL"},
            "dest": {"type": "string"},
        },
        "required": ["repo", "dest"],
    }, "github.write", github_clone, category="github",
        capabilities=["github_clone", "clone_repository"]))

    registry.register(ToolSpec("github_pull_request", "Inspect a pull request: mergeable state, changed files, reviews, and check runs.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "number": {"type": "integer"},
        },
        "required": ["number"],
    }, "github.read", github_pull_request, category="github",
        capabilities=["github_pr", "inspect_pr"]))

    registry.register(ToolSpec("github_actions_run", "Inspect a GitHub Actions run: status, jobs, and failed step names.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "run_id": {"type": "integer"},
        },
        "required": ["run_id"],
    }, "github.read", github_actions_run, category="github",
        capabilities=["github_actions", "ci_inspection"]))

    registry.register(ToolSpec("github_rerun", "Re-run a GitHub Actions workflow run (requires authorization).", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "run_id": {"type": "integer"},
        },
        "required": ["run_id"],
    }, "github.write", github_rerun, category="github",
        capabilities=["github_actions", "rerun_workflow"]))

    registry.register(ToolSpec("github_issue_comment", "Comment on a GitHub issue or pull request (requires authorization).", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "number": {"type": "integer"},
            "body": {"type": "string"},
        },
        "required": ["number", "body"],
    }, "github.write", github_issue_comment, category="github",
        capabilities=["github_issues", "comment"]))

    registry.register(ToolSpec("github_compare", "Compare two refs on the remote: ahead/behind and changed files.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "base": {"type": "string"},
            "head": {"type": "string"},
        },
        "required": ["base", "head"],
    }, "github.read", github_compare, category="github",
        capabilities=["github_compare", "diff_refs"]))

    registry.register(ToolSpec("github_checkout_pr", "Fetch and check out a pull request locally as pr-<number>.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "number": {"type": "integer"},
        },
        "required": ["number"],
    }, "git.execute", github_checkout_pr, category="github",
        capabilities=["github_pr", "checkout_pr"]))
