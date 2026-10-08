from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..config import AgentConfig
from ..procutil import no_window_flags
from .base import ToolRegistry, ToolSpec


class _UploadCancelled(RuntimeError):
    """Raised inside the streaming reader when the job's cancel flag
    flips — aborts the POST between blocks; GitHub creates no asset on
    an aborted body, so nothing partial is left server-side."""


_UPLOAD_LOCK = threading.Lock()
_UPLOAD_FLAGS: dict[str, threading.Event] = {}
_UPLOAD_LATEST: list[str] = []


def cancel_upload_job(job_id: str = "") -> str | None:
    """Abort an in-flight asset upload by job id (or the most recent).
    Returns the cancelled job id, or None when nothing is running.
    Shared by the ``github_cancel_upload`` tool and the generic
    ``/api/jobs/cancel`` route."""
    with _UPLOAD_LOCK:
        jid = job_id or (_UPLOAD_LATEST[-1] if _UPLOAD_LATEST else "")
        flag = _UPLOAD_FLAGS.get(jid)
    if flag is None:
        return None
    flag.set()
    return jid


class _ProgressReader:
    """File-object wrapper that reports bytes-sent to a callback —
    http.client streams request bodies via read(blocksize), so this
    gives live upload progress without buffering the file. When a
    ``cancel`` callable is supplied it is polled per block so an
    in-flight upload can be aborted."""

    def __init__(self, fh, total: int, callback=None, cancel=None) -> None:
        self._fh = fh
        self._total = total
        self._done = 0
        self._cb = callback
        self._cancel = cancel

    def read(self, size: int = -1) -> bytes:
        if self._cancel is not None and self._cancel():
            raise _UploadCancelled("upload cancelled")
        chunk = self._fh.read(size)
        self._done += len(chunk)
        if self._cb is not None:
            try:
                self._cb(self._done, self._total)
            except Exception:
                pass
        return chunk

    def close(self) -> None:
        pass  # caller owns the real handle


def _run_git(workspace: Path, args: list[str], *, timeout: int = 60, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=timeout,
        creationflags=no_window_flags(),
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
                try:
                    data = json.loads(exc.read().decode("utf-8", errors="replace"))
                    detail = str(data.get("message") or exc.reason)
                except Exception:
                    detail = str(exc.reason)
                raise RuntimeError(f"GitHub API {exc.code}: {detail}") from exc
            finally:
                exc.close()
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

    def api_url(self, path: str) -> str:
        """Absolute API URL for a path — the durable download manager
        needs a full URL; tokens travel as headers, never in the URL."""
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def upload_file(self, url: str, path, *, content_type: str,
                    progress=None, should_cancel=None) -> dict:
        """POST a file body to an absolute upload URL (the release
        ``upload_url`` host — uploads.github.com). Streams from disk in
        blocks; ``progress(done, total)`` fires per block for job UI and
        ``should_cancel()`` aborts mid-flight (raises
        :class:`_UploadCancelled`)."""
        if not self.authenticated:
            raise RuntimeError(
                f"GitHub upload requires a token in environment variable "
                f"{self.token_env} or github_connect")
        file_path = Path(path)
        size = file_path.stat().st_size
        headers = self._headers()
        headers["Content-Type"] = content_type or "application/octet-stream"
        headers["Content-Length"] = str(size)
        fh = file_path.open("rb")
        try:
            body = (_ProgressReader(fh, size, progress, should_cancel)
                    if (progress or should_cancel) else fh)
            request = Request(url, data=body, headers=headers,
                              method="POST")
            try:
                if self._opener is None:
                    response = urlopen(request, timeout=max(self.timeout, 300))
                else:
                    response = self._opener(request, max(self.timeout, 300))
                with response as handle:
                    raw = handle.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except _UploadCancelled:
                raise
            except HTTPError as exc:
                try:
                    try:
                        data = json.loads(
                            exc.read().decode("utf-8", errors="replace"))
                        detail = str(data.get("message") or exc.reason)
                        errors = data.get("errors") or []
                        if errors:
                            detail += " — " + "; ".join(
                                str(e.get("code") or e)
                                for e in errors[:3] if isinstance(e, dict))
                    except Exception:
                        detail = str(exc.reason)
                    raise RuntimeError(
                        f"GitHub upload failed {exc.code}: {detail}") from exc
                finally:
                    exc.close()
            except (URLError, TimeoutError, OSError) as exc:
                raise RuntimeError(
                    f"GitHub upload unavailable: {exc}") from exc
        finally:
            fh.close()


def register_github_tools(registry: ToolRegistry, workspace: Path,
                          config: AgentConfig, *, vault=None,
                          workspaces=None, client=None,
                          account=None, journal=None, events=None,
                          artifacts=None, downloads=None) -> None:
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

    def _journal(action_type: str, subject: str, **fields) -> None:
        if journal is None:
            return
        try:
            tls = registry.context.get("task_tls")
            journal.record(
                action_type, subject, actor="nexus",
                task_id=str(getattr(tls, "task_id", "")
                          or registry.context.get("task_id", "") or ""),
                mission_id=str(registry.context.get("mission_id", "")
                               or ""),
                **fields)
        except Exception:
            pass

    # CI/PR observations flow onto the event bus so autonomy triggers
    # (ci_failed / ci_completed / pull_request_updated) can react — that
    # is what closes the git→push→PR→CI loop without the model polling
    # in a chat turn. Dedupe: a run's conclusion is terminal, so each
    # (run, conclusion) fires at most once per process.
    _seen_ci: set[tuple] = set()
    _seen_pr: set[tuple] = set()

    def _emit(event: dict) -> None:
        if events is None:
            return
        try:
            events.publish(str(event.get("type") or "github"), event)
        except Exception:
            pass

    def _emit_ci(repo: str, run: dict) -> None:
        try:
            if str(run.get("status") or "") != "completed":
                return
            conclusion = str(run.get("conclusion") or "")
            if not conclusion:
                return
            key = (repo, int(run.get("id") or 0), conclusion)
            if key in _seen_ci:
                return
            _seen_ci.add(key)
            if len(_seen_ci) > 500:
                _seen_ci.clear()
            _emit({"type": "ci", "repository": repo,
                   "run_id": key[1], "conclusion": conclusion,
                   "name": str(run.get("name") or ""),
                   "sha": str(run.get("head_sha") or ""),
                   "url": str(run.get("html_url") or "")})
        except Exception:
            pass

    def _emit_pr(repo: str, pr: dict) -> None:
        try:
            key = (repo, int(pr.get("number") or 0),
                   str(pr.get("state") or ""))
            if key in _seen_pr:
                return
            _seen_pr.add(key)
            if len(_seen_pr) > 500:
                _seen_pr.clear()
            _emit({"type": "pull_request", "repository": repo,
                   "number": key[1], "state": key[2],
                   "merged": bool(pr.get("merged_at") or pr.get("merged")),
                   "title": str(pr.get("title") or ""),
                   "url": str(pr.get("html_url") or "")})
        except Exception:
            pass

    def git_current_branch(_: dict) -> str:
        return _current_branch(workspace)

    def git_create_branch(args: dict) -> str:
        branch = str(args.get("branch", "")).strip()
        if not branch:
            raise ValueError("branch is required")
        prev = _current_branch(workspace)
        _run_git(workspace, ["check-ref-format", "--branch", branch])
        _run_git(workspace, ["switch", "-c", branch])
        _journal("git_branch_create", f"branch '{branch}'",
                 before=prev, after=branch, reversible=True,
                 undo={"kind": "git_branch_delete", "branch": branch,
                       "previous_branch": prev, "root": str(workspace)},
                 risk="medium",
                 description=f"Created and switched to '{branch}'")
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
            creationflags=no_window_flags(),
        )
        if staged.returncode == 0:
            raise RuntimeError("No staged changes to commit")
        if staged.returncode not in {0, 1}:
            raise RuntimeError((staged.stdout + staged.stderr).strip() or "git diff --cached failed")
        output = _run_git(workspace, ["commit", "-m", message], timeout=120)
        sha = _run_git(workspace, ["rev-parse", "HEAD"])
        _journal("git_commit", f"commit {str(sha).strip()[:8]}",
                 after=str(sha).strip(), files=paths, reversible=True,
                 undo={"kind": "git_reset_soft", "sha": str(sha).strip(),
                       "root": str(workspace)},
                 risk="medium", description=message[:200])
        return json.dumps({"commit": sha, "message": message, "output": output[-8000:]}, indent=2)

    def git_push(args: dict) -> str:
        remote = str(args.get("remote") or remote_default).strip()
        branch = str(args.get("branch") or "").strip() or _current_branch(workspace)
        _ensure_token()
        command = []
        if client.authenticated:
            # Header-scoped auth — same pattern as github_clone: the
            # connected credential drives the push without landing in
            # .git/config or the process-visible remote URL. Basic
            # base64(x-access-token:token) — the actions/checkout form —
            # is accepted by git smart-HTTP for every token type;
            # "bearer" is rejected for OAuth (gho_) tokens.
            command += ["-c",
                        "http.extraHeader=AUTHORIZATION: basic "
                        + base64.b64encode(
                            f"x-access-token:{client.token}".encode()
                        ).decode()]
        command += ["push"]
        if bool(args.get("set_upstream", True)):
            command.extend(["--set-upstream", remote, branch])
        else:
            command.extend([remote, branch])
        output = _run_git(workspace, command, timeout=180)
        _journal("git_push", f"push '{branch}' to {remote}",
                 after={"remote": remote, "branch": branch},
                 reversible=False,
                 irreversible_reason="remote refs can't be safely "
                                     "rewritten by automatic undo",
                 risk="high",
                 description=f"Pushed {branch} to {remote}")
        return json.dumps({"remote": remote, "branch": branch, "output": output[-8000:]}, indent=2)

    def github_repository(args: dict) -> str:
        _ensure_token()
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
        _ensure_token()
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
        _ensure_token()
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
        _ensure_token()
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
        _emit_pr(repo, data or {})
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
        _ensure_token()
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
            _emit_ci(repo, run)
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
            # the process-visible remote URL. Basic base64 form — git
            # smart-HTTP rejects "bearer" for OAuth (gho_) tokens.
            argv += ["-c",
                     "http.extraHeader=AUTHORIZATION: basic "
                     + base64.b64encode(
                         f"x-access-token:{client.token}".encode()
                     ).decode()]
        argv += ["clone", url, str(dest)]
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=600,
                              creationflags=no_window_flags())
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
        _emit_pr(repo, pr)
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
        _emit_ci(repo, run)
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

    # -- releases, release assets, Actions artifacts -------------------------

    def _slug(args: dict) -> tuple[str, str, str, str]:
        """Resolve the repository — explicit 'owner/repo' (or GitHub URL)
        wins; otherwise the workspace's configured git remote."""
        slug = str(args.get("repo") or "").strip().rstrip("/")
        if slug.endswith(".git"):
            slug = slug[:-4]
        if "github.com" in slug:
            slug = slug.split("github.com/", 1)[-1] \
                .split("github.com:", 1)[-1].lstrip("/")
        if not slug or "/" not in slug:
            remote = str(args.get("remote") or remote_default).strip()
            slug = _repo_slug(workspace, remote)
        owner, name = slug.split("/", 1)
        return slug, quote(owner, safe=""), quote(name, safe="")

    def _release(oq: str, nq: str, args: dict) -> dict:
        """Resolve a release by id, tag, or 'latest'."""
        rid = int(args.get("release_id") or 0)
        tag = str(args.get("tag") or args.get("release") or "").strip()
        if rid > 0:
            data = client.request(
                "GET", f"/repos/{oq}/{nq}/releases/{rid}") or {}
        elif tag and tag.lower() not in ("latest", ""):
            data = client.request(
                "GET", f"/repos/{oq}/{nq}/releases/tags/"
                f"{quote(tag, safe='')}") or {}
        else:
            data = client.request(
                "GET", f"/repos/{oq}/{nq}/releases/latest") or {}
        if not data.get("id"):
            raise RuntimeError(
                "no matching GitHub release "
                f"(tag={tag or 'latest'}, id={rid or '—'})")
        return data

    def _asset_view(a: dict) -> dict:
        return {
            "id": a.get("id"), "name": a.get("name"),
            "size": a.get("size"), "state": a.get("state"),
            "content_type": a.get("content_type"),
            "download_count": a.get("download_count"),
            "url": a.get("browser_download_url"),
            "api_url": a.get("url"),
        }

    def github_list_releases(args: dict) -> str:
        _ensure_token()
        slug, oq, nq = _slug(args)
        limit = max(1, min(30, int(args.get("limit") or 10)))
        rows = client.request(
            "GET", f"/repos/{oq}/{nq}/releases",
            params={"per_page": limit}) or []
        return json.dumps({"repository": slug, "releases": [{
            "id": r.get("id"), "tag": r.get("tag_name"),
            "name": r.get("name"), "draft": r.get("draft"),
            "prerelease": r.get("prerelease"),
            "url": r.get("html_url"),
            "published_at": r.get("published_at"),
            "assets": [_asset_view(a) for a in
                       (r.get("assets") or [])[:20]],
        } for r in rows[:limit]]}, indent=2)

    def github_get_release(args: dict) -> str:
        _ensure_token()
        slug, oq, nq = _slug(args)
        rel = _release(oq, nq, args)
        return json.dumps({
            "repository": slug, "id": rel.get("id"),
            "tag": rel.get("tag_name"), "name": rel.get("name"),
            "draft": rel.get("draft"),
            "prerelease": rel.get("prerelease"),
            "url": rel.get("html_url"),
            "published_at": rel.get("published_at"),
            "assets": [_asset_view(a)
                       for a in (rel.get("assets") or [])],
        }, indent=2)

    def github_create_release(args: dict) -> str:
        _ensure_token()
        slug, oq, nq = _slug(args)
        tag = str(args.get("tag") or "").strip()
        if not tag:
            raise ValueError("tag is required")
        data = client.request(
            "POST", f"/repos/{oq}/{nq}/releases",
            body={
                "tag_name": tag,
                "name": str(args.get("name") or tag),
                "body": str(args.get("body") or ""),
                "draft": bool(args.get("draft", False)),
                "prerelease": bool(args.get("prerelease", False)),
            }, require_auth=True) or {}
        _journal("github_release_create", f"release '{tag}'",
                 after={"id": data.get("id"), "tag": tag},
                 reversible=False, risk="medium",
                 description=f"Created GitHub release {tag}")
        return json.dumps({
            "repository": slug, "id": data.get("id"),
            "tag": data.get("tag_name"), "name": data.get("name"),
            "url": data.get("html_url"),
            "draft": data.get("draft")}, indent=2)

    def github_list_release_assets(args: dict) -> str:
        _ensure_token()
        slug, oq, nq = _slug(args)
        rel = _release(oq, nq, args)
        assets = client.request(
            "GET", f"/repos/{oq}/{nq}/releases/{rel['id']}/assets",
            params={"per_page": 100}) or []
        return json.dumps({
            "repository": slug, "release_id": rel.get("id"),
            "tag": rel.get("tag_name"),
            "assets": [_asset_view(a) for a in assets]}, indent=2)

    def _local_artifact_or_path(args: dict):
        """Resolve (label, Path, artifact_row|None) from artifact_id,
        name, or a user-supplied path."""
        aid = str(args.get("artifact_id") or "").strip()
        name = str(args.get("name") or "").strip()
        raw_path = str(args.get("path") or "").strip()
        row = None
        if artifacts is not None:
            try:
                if aid:
                    row = artifacts.get(aid)
                elif name:
                    hits = artifacts.find(name)
                    row = hits[0] if hits else None
            except Exception:
                row = None
        if row is not None:
            p = Path(str(row.get("path") or ""))
            if not p.is_file():
                raise RuntimeError(
                    f"artifact {row.get('id')} local file is missing — "
                    "regenerate it or upload a different copy")
            return str(row.get("name") or p.name), p, row
        target = raw_path or name
        if not target:
            raise ValueError(
                "artifact_id, name, or path is required — nothing to "
                "upload")
        p = Path(target).expanduser()
        if not p.is_absolute():
            p = (workspace / p).resolve()
        if not p.is_file():
            raise RuntimeError(f"no such file or artifact: {target}")
        return p.name, p, None

    def github_upload_release_asset(args: dict) -> str:
        _ensure_token()
        if not client.authenticated:
            raise RuntimeError(
                "GitHub upload requires a connected account — connect "
                "GitHub first (github_connect).")
        slug, oq, nq = _slug(args)
        rel = _release(oq, nq, args)
        label, path, row = _local_artifact_or_path(args)
        asset_name = str(args.get("asset_name") or label).strip() or label
        # Verify local integrity before publishing — never upload a
        # file whose registered hash no longer matches.
        if row is not None:
            chk = artifacts.verify(str(row.get("id") or ""))
            if not chk.get("ok"):
                raise RuntimeError(
                    f"artifact integrity check failed: "
                    f"{chk.get('reason', 'unknown')} — refusing upload")
        elif artifacts is not None:
            # Path-sourced upload: register now so the reply carries a
            # card immediately — remote provenance attaches when the
            # job verifies.
            try:
                row = artifacts.register(
                    path, tool="github_upload_release_asset")
            except Exception:
                row = None
        size = path.stat().st_size
        existing = client.request(
            "GET", f"/repos/{oq}/{nq}/releases/{rel['id']}/assets",
            params={"per_page": 100}, require_auth=True) or []
        dupe = next((a for a in existing
                     if str(a.get("name")) == asset_name), None)
        if dupe is not None and not bool(args.get("replace")):
            return json.dumps({
                "ok": False, "repository": slug,
                "error": f"asset '{asset_name}' already exists on "
                         f"release {rel.get('tag_name')}",
                "existing_asset": _asset_view(dupe),
                "hint": "pass replace=true to overwrite (requires "
                        "explicit approval)"}, indent=2)
        if dupe is not None:
            client.request(
                "DELETE",
                f"/repos/{oq}/{nq}/releases/assets/{dupe['id']}",
                require_auth=True)
            _journal("github_asset_delete",
                     f"asset '{asset_name}' (replaced)",
                     risk="high", reversible=False,
                     irreversible_reason="replaced asset bytes are "
                                         "unrecoverable",
                     description=f"Deleted duplicate asset {asset_name}")
        template = str(rel.get("upload_url") or "").split("{")[0]
        if not template:
            raise RuntimeError("release has no upload endpoint")
        up_url = template + "?" + urlencode({"name": asset_name})
        import mimetypes as _mt
        ctype = _mt.guess_type(asset_name)[0] or "application/octet-stream"

        def _do_upload(progress=None, should_cancel=None) -> dict:
            nonlocal row
            resp = client.upload_file(
                up_url, path, content_type=ctype,
                progress=progress, should_cancel=should_cancel)
            # Verify the remote accepted the bytes — never claim
            # success on the HTTP status alone.
            asset_id = resp.get("id")
            ok = bool(asset_id) and resp.get("state") == "uploaded" and \
                int(resp.get("size") or -1) == size and \
                str(resp.get("name")) == asset_name
            if not ok:
                raise RuntimeError(
                    "upload response failed verification "
                    f"(state={resp.get('state')}, "
                    f"size={resp.get('size')}, expected={size})")
            # Second read-back: the asset must actually exist on the
            # release.
            check = client.request(
                "GET",
                f"/repos/{oq}/{nq}/releases/assets/{asset_id}",
                require_auth=True) or {}
            if check.get("id") != asset_id or \
                    int(check.get("size") or -1) != size:
                raise RuntimeError(
                    "uploaded asset failed read-back verification")
            remote = {
                "provider": "github", "repository": slug,
                "kind": "release_asset",
                "release_id": rel.get("id"),
                "release_url": rel.get("html_url"),
                "asset_id": asset_id, "tag": rel.get("tag_name"),
                "name": asset_name, "size": size,
                "web_url": check.get("browser_download_url"),
                "download_url": check.get("browser_download_url"),
                "uploaded_at": check.get("updated_at")
                or check.get("created_at"),
            }
            if row is None and artifacts is not None:
                # Path-sourced upload — the published file still becomes
                # a tracked artifact so the user gets a card and the
                # remote provenance is recorded.
                try:
                    row = artifacts.register(
                        path, tool="github_upload_release_asset")
                except Exception:
                    row = None
            if row is not None:
                try:
                    artifacts.attach_remote(str(row["id"]), remote)
                except Exception:
                    pass
            _journal(
                "github_upload",
                f"asset '{asset_name}' → {slug}@{rel.get('tag_name')}",
                after={"asset_id": asset_id, "size": size},
                reversible=False,
                irreversible_reason="remote asset can't be un-published "
                                    "by undo",
                risk="medium",
                description=f"Uploaded {asset_name} ({size} bytes)")
            return {
                "ok": True, "verified": True, "repository": slug,
                "artifact_id": str((row or {}).get("id") or ""),
                "asset": _asset_view(check),
                "release": {"id": rel.get("id"),
                            "tag": rel.get("tag_name"),
                            "url": rel.get("html_url")}}

        jobs = getattr(downloads, "jobs", None)
        if jobs is not None:
            # Async like downloads: the plan lane returns immediately so
            # 'cancel the upload' can reach us mid-flight; progress and
            # the verified result land on the job record.
            flag = threading.Event()
            record = jobs.submit(
                "github_upload",
                f"Upload {asset_name} → {slug}@{rel.get('tag_name')}",
                metadata={"repository": slug,
                          "tag": rel.get("tag_name"),
                          "asset": asset_name, "size": size,
                          "artifact_id": str((row or {}).get("id") or "")})
            jobs.update(record.id, state="running", status="uploading",
                        cancellable=True)
            with _UPLOAD_LOCK:
                _UPLOAD_FLAGS[record.id] = flag
                _UPLOAD_LATEST.append(record.id)
                del _UPLOAD_LATEST[:-8]

            def _prog(done: int, total: int) -> None:
                try:
                    jobs.update(record.id, state="running",
                                status="uploading",
                                progress=done / max(total, 1),
                                detail=f"{done:,} / {total:,} bytes")
                except Exception:
                    pass

            def _run() -> None:
                try:
                    out = _do_upload(progress=_prog,
                                     should_cancel=flag.is_set)
                    url = (out.get("release") or {}).get("url") or ""
                    jobs.update(record.id, state="completed",
                                status="finished", progress=1.0,
                                detail=url)
                    _emit({"type": "notification", "notification": {
                        "id": f"gh-upload-{record.id}",
                        "title": "GitHub upload verified",
                        "message": f"{asset_name} → {slug}@"
                                   f"{rel.get('tag_name')}"
                                   + (f" — {url}" if url else ""),
                        "level": "info",
                        "created_at": time.time(), "read": False}})
                except _UploadCancelled:
                    try:
                        jobs.update(
                            record.id, state="cancelled",
                            detail="cancelled — nothing published")
                    except Exception:
                        pass
                except Exception as exc:
                    try:
                        jobs.update(record.id, state="failed",
                                    error=str(exc)[:300])
                    except Exception:
                        pass
                    _emit({"type": "notification", "notification": {
                        "id": f"gh-upload-{record.id}",
                        "title": "GitHub upload failed",
                        "message": f"{asset_name}: {exc}",
                        "level": "warning",
                        "created_at": time.time(), "read": False}})
                finally:
                    with _UPLOAD_LOCK:
                        _UPLOAD_FLAGS.pop(record.id, None)

            threading.Thread(
                target=_run, daemon=True,
                name=f"nexus-gh-upload-{record.id[-6:]}").start()
            return json.dumps({
                "ok": True, "status": "uploading", "job_id": record.id,
                "repository": slug, "asset_name": asset_name,
                "size": size,
                "artifact_id": str((row or {}).get("id") or ""),
                "release": {"id": rel.get("id"),
                            "tag": rel.get("tag_name"),
                            "url": rel.get("html_url")}}, indent=2)
        return json.dumps(_do_upload(), indent=2)

    def github_cancel_upload(args: dict) -> str:
        """Abort an in-flight asset upload — aborts between blocks; an
        aborted POST leaves no asset on GitHub."""
        want = str(args.get("job_id") or "").strip()
        hit = cancel_upload_job(want)
        if hit is None:
            return json.dumps({"ok": False,
                               "error": "no upload is running"})
        jobs = getattr(downloads, "jobs", None)
        if jobs is not None:
            try:
                jobs.cancel(hit)
            except Exception:
                pass
        return json.dumps({"ok": True, "cancelled": hit}, indent=2)

    def github_download_release_asset(args: dict) -> str:
        _ensure_token()
        if downloads is None:
            raise RuntimeError("download manager unavailable")
        slug, oq, nq = _slug(args)
        rel = _release(oq, nq, args)
        assets = client.request(
            "GET", f"/repos/{oq}/{nq}/releases/{rel['id']}/assets",
            params={"per_page": 100}) or []
        asset = _pick_asset(assets, args)
        if asset is None:
            raise RuntimeError(
                "no matching asset on release "
                f"{rel.get('tag_name')} — {len(assets)} available")
        url = client.api_url(
            f"/repos/{oq}/{nq}/releases/assets/{asset['id']}")
        headers = {"Accept": "application/octet-stream"}
        if client.token:
            headers["Authorization"] = f"Bearer {client.token}"
        res = downloads.start(
            url, str(asset.get("name") or ""),
            expected_size=int(asset.get("size") or 0),
            headers=headers,
            artifact={
                "tool": "github_download_release_asset",
                "remote": {
                    "provider": "github", "repository": slug,
                    "kind": "release_asset",
                    "release_id": rel.get("id"),
                    "release_url": rel.get("html_url"),
                    "asset_id": asset.get("id"),
                    "tag": rel.get("tag_name"),
                    "name": asset.get("name"),
                    "size": asset.get("size"),
                    "web_url": asset.get("browser_download_url"),
                    "download_url": asset.get("browser_download_url"),
                }})
        res["release"] = {"id": rel.get("id"), "tag": rel.get("tag_name"),
                          "url": rel.get("html_url")}
        res["repository"] = slug
        return json.dumps(res, indent=2)

    def github_delete_release_asset(args: dict) -> str:
        _ensure_token()
        slug, oq, nq = _slug(args)
        if not bool(args.get("confirm")):
            raise ValueError(
                "deleting a release asset is permanent — pass "
                "confirm=true")
        asset_id = int(args.get("asset_id") or 0)
        if asset_id <= 0:
            rel = _release(oq, nq, args)
            assets = client.request(
                "GET", f"/repos/{oq}/{nq}/releases/{rel['id']}/assets",
                params={"per_page": 100}) or []
            asset = _pick_asset(assets, args)
            if asset is None:
                raise RuntimeError("no matching asset to delete")
            asset_id = int(asset["id"])
        client.request(
            "DELETE",
            f"/repos/{oq}/{nq}/releases/assets/{asset_id}",
            require_auth=True)
        _journal("github_asset_delete", f"asset {asset_id}",
                 risk="high", reversible=False,
                 irreversible_reason="deleted asset bytes are "
                                     "unrecoverable",
                 description=f"Deleted release asset {asset_id}")
        return json.dumps({"ok": True, "deleted": asset_id,
                           "repository": slug}, indent=2)

    def _pick_asset(assets: list, args: dict):
        want = str(args.get("asset") or args.get("name")
                   or "").strip().lower()
        aid = int(args.get("asset_id") or 0)
        for a in assets:
            if aid and int(a.get("id") or 0) == aid:
                return a
            if want and str(a.get("name") or "").lower() == want:
                return a
        if not want and not aid and len(assets) == 1:
            return assets[0]
        return None

    def github_list_run_artifacts(args: dict) -> str:
        _ensure_token()
        slug, oq, nq = _slug(args)
        run_id = int(args.get("run_id") or 0)
        run = None
        if run_id <= 0:
            runs = client.request(
                "GET", f"/repos/{oq}/{nq}/actions/runs",
                params={"status": "success", "per_page": 1}) or {}
            items = list(runs.get("workflow_runs") or [])
            if not items:
                raise RuntimeError("no successful Actions runs found")
            run = items[0]
            run_id = int(run.get("id") or 0)
        data = client.request(
            "GET", f"/repos/{oq}/{nq}/actions/runs/{run_id}/artifacts",
            params={"per_page": 50}) or {}
        rows = [{
            "id": a.get("id"), "name": a.get("name"),
            "size": a.get("size_in_bytes"),
            "expired": bool(a.get("expired")),
            "expires_at": a.get("expires_at"),
            "created_at": a.get("created_at"),
        } for a in (data.get("artifacts") or [])[:50]]
        out = {"repository": slug, "run_id": run_id,
               "artifacts": rows}
        if run is not None:
            out["run"] = {"id": run.get("id"),
                          "name": run.get("name"),
                          "title": run.get("display_title"),
                          "url": run.get("html_url")}
        return json.dumps(out, indent=2)

    def github_download_run_artifact(args: dict) -> str:
        _ensure_token()
        if downloads is None:
            raise RuntimeError("download manager unavailable")
        slug, oq, nq = _slug(args)
        aid = int(args.get("artifact_id") or 0)
        artifact = None
        run_id = int(args.get("run_id") or 0)
        if aid <= 0 or run_id <= 0:
            # Resolve through a run's artifact list so 'expired' is
            # checked before a doomed download starts.
            want = str(args.get("name") or "").strip().lower()
            if run_id <= 0:
                runs = client.request(
                    "GET", f"/repos/{oq}/{nq}/actions/runs",
                    params={"status": "success", "per_page": 1}) or {}
                items = list(runs.get("workflow_runs") or [])
                if not items:
                    raise RuntimeError("no successful Actions runs found")
                run = items[0]
                run_id = int(run.get("id") or 0)
            data = client.request(
                "GET",
                f"/repos/{oq}/{nq}/actions/runs/{run_id}/artifacts",
                params={"per_page": 50}) or {}
            for a in (data.get("artifacts") or []):
                if aid and int(a.get("id") or 0) == aid or \
                        (want and str(a.get("name") or "").lower()
                         == want):
                    artifact = a
                    break
            if artifact is None and aid <= 0 and not want:
                rows = list(data.get("artifacts") or [])
                if len(rows) == 1:
                    artifact = rows[0]
            if artifact is None:
                raise RuntimeError(
                    f"no matching artifact on run {run_id}")
            if artifact.get("expired"):
                raise RuntimeError(
                    f"artifact '{artifact.get('name')}' expired on "
                    f"{artifact.get('expires_at')} — GitHub deletes "
                    "retention-expired artifacts permanently")
            aid = int(artifact["id"])
        name = str((artifact or {}).get("name")
                   or f"artifact-{aid}")
        url = client.api_url(
            f"/repos/{oq}/{nq}/actions/artifacts/{aid}/zip")
        headers = {}
        if client.token:
            headers["Authorization"] = f"Bearer {client.token}"
        res = downloads.start(
            url, f"{name}.zip",
            expected_size=int((artifact or {}).get("size_in_bytes") or 0),
            headers=headers,
            artifact={
                "tool": "github_download_run_artifact",
                "extract_zip": True,
                "remote": {
                    "provider": "github", "repository": slug,
                    "kind": "run_artifact", "run_id": run_id,
                    "asset_id": aid, "artifact_name": name,
                    "expires_at": (artifact or {}).get("expires_at"),
                }})
        res["repository"] = slug
        res["run_id"] = run_id
        return json.dumps(res, indent=2)

    def github_get_repo_file(args: dict) -> str:
        _ensure_token()
        if downloads is None:
            raise RuntimeError("download manager unavailable")
        slug, oq, nq = _slug(args)
        rel = str(args.get("path") or "").strip().lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise ValueError("a repository file path is required")
        params = {}
        ref = str(args.get("ref") or "").strip()
        if ref:
            params["ref"] = ref
        data = client.request(
            "GET", f"/repos/{oq}/{nq}/contents/{quote(rel)}",
            params=params) or {}
        if isinstance(data, list):
            raise ValueError(
                f"'{rel}' is a directory — name a file path")
        raw = str(data.get("download_url") or "")
        if not raw:
            raise RuntimeError(
                f"no download URL for '{rel}' (it may be too large or "
                "a submodule)")
        headers = {}
        if client.token:
            headers["Authorization"] = f"Bearer {client.token}"
        res = downloads.start(
            raw, Path(rel).name,
            expected_size=int(data.get("size") or 0),
            headers=headers,
            artifact={
                "tool": "github_get_repo_file",
                "remote": {
                    "provider": "github", "repository": slug,
                    "kind": "repo_file", "name": Path(rel).name,
                    "web_url": data.get("html_url"),
                    "download_url": raw,
                }})
        res["repository"] = slug
        return json.dumps(res, indent=2)


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
    }, "git.push", git_push))

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

    registry.register(ToolSpec("github_list_releases", "List GitHub releases (tag, url, assets) for the repository.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string",
                     "description": "owner/name or GitHub URL (default: workspace remote)"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
    }, "github.read", github_list_releases, category="github",
        capabilities=["github_release", "list_releases"]))

    registry.register(ToolSpec("github_get_release", "Inspect one GitHub release by tag, id, or 'latest' — includes its assets.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string"},
            "tag": {"type": "string"},
            "release_id": {"type": "integer"},
        },
    }, "github.read", github_get_release, category="github",
        capabilities=["github_release", "inspect_release"]))

    registry.register(ToolSpec("github_create_release", "Create a GitHub release for a tag (requires authorization). Only for explicit publish requests.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string"},
            "tag": {"type": "string"},
            "name": {"type": "string"},
            "body": {"type": "string"},
            "draft": {"type": "boolean"},
            "prerelease": {"type": "boolean"},
        },
        "required": ["tag"],
    }, "github.write", github_create_release, category="github",
        capabilities=["github_release", "create_release"]))

    registry.register(ToolSpec("github_list_release_assets", "List the files attached to a GitHub release.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string"},
            "tag": {"type": "string"},
            "release_id": {"type": "integer"},
        },
    }, "github.read", github_list_release_assets, category="github",
        capabilities=["github_release", "list_assets"]))

    registry.register(ToolSpec("github_upload_release_asset", "Upload a local file or registered artifact to a GitHub release. Streams the bytes, then verifies the remote asset (name+size+read-back). Duplicate names are refused unless replace=true.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string"},
            "tag": {"type": "string"},
            "release_id": {"type": "integer"},
            "artifact_id": {"type": "string"},
            "name": {"type": "string",
                     "description": "artifact filename to look up, or file name"},
            "path": {"type": "string",
                     "description": "explicit file path when not a registered artifact"},
            "asset_name": {"type": "string"},
            "replace": {"type": "boolean"},
        },
    }, "github.write", github_upload_release_asset, category="github",
        capabilities=["github_release", "upload_asset"]))

    registry.register(ToolSpec("github_cancel_upload", "Abort an in-flight GitHub asset upload. Aborts between stream blocks; nothing is published. Pass job_id or omit to cancel the most recent upload.", {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
        },
    }, "github.read", github_cancel_upload, category="github",
        capabilities=["github_release", "upload_asset"]))

    registry.register(ToolSpec("github_download_release_asset", "Download a GitHub release asset through the durable download manager (resume, size verify, progress). Registers the result as an artifact.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string"},
            "tag": {"type": "string"},
            "release_id": {"type": "integer"},
            "asset": {"type": "string",
                      "description": "asset filename"},
            "asset_id": {"type": "integer"},
        },
    }, "github.read", github_download_release_asset, category="github",
        capabilities=["github_release", "download_asset"]))

    registry.register(ToolSpec("github_delete_release_asset", "Permanently delete a release asset (requires confirm=true and authorization).", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string"},
            "tag": {"type": "string"},
            "release_id": {"type": "integer"},
            "asset": {"type": "string"},
            "asset_id": {"type": "integer"},
            "confirm": {"type": "boolean"},
        },
        "required": ["confirm"],
    }, "github.write", github_delete_release_asset, category="github",
        capabilities=["github_release", "delete_asset"]))

    registry.register(ToolSpec("github_list_run_artifacts", "List GitHub Actions artifacts for a run — or the latest successful run when run_id is omitted.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string"},
            "run_id": {"type": "integer"},
        },
    }, "github.read", github_list_run_artifacts, category="github",
        capabilities=["github_actions", "list_artifacts"]))

    registry.register(ToolSpec("github_download_run_artifact", "Download a GitHub Actions artifact (the standard ZIP) through the durable download manager and extract it. Expired artifacts are reported, not fetched.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string"},
            "run_id": {"type": "integer"},
            "artifact_id": {"type": "integer"},
            "name": {"type": "string"},
        },
    }, "github.read", github_download_run_artifact, category="github",
        capabilities=["github_actions", "download_artifact"]))

    registry.register(ToolSpec("github_get_repo_file", "Download a single file from the repository (raw contents) into Downloads and register it as an artifact.", {
        "type": "object",
        "properties": {
            "remote": {"type": "string"},
            "repo": {"type": "string"},
            "path": {"type": "string"},
            "ref": {"type": "string"},
        },
        "required": ["path"],
    }, "github.read", github_get_repo_file, category="github",
        capabilities=["github_file", "download_file"]))
