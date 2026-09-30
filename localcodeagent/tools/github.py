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
            raise ValueError("Chat Nexus .agent metadata cannot be staged by the coding agent")
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

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        require_auth: bool = False,
    ) -> Any:
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
                return json.loads(raw.decode("utf-8")) if raw else None
        except HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = str(data.get("message") or exc.reason)
            except Exception:
                detail = str(exc.reason)
            raise RuntimeError(f"GitHub API {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"GitHub API unavailable: {exc}") from exc


def register_github_tools(registry: ToolRegistry, workspace: Path, config: AgentConfig) -> None:
    client = GitHubCodingClient(config)
    remote_default = config.github_default_remote or "origin"

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
        command = ["push"]
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
