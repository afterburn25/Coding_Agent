"""Static deploy adapter — publish a built directory to a git branch
(gh-pages style) via a detached worktree. The user's checkout is never
touched: a temporary worktree hosts the deploy branch, receives the
directory contents, commits, pushes, and is removed.

Honesty rules: every step reports its real git output; a push is only
claimed after `git push` returns 0; failures surface the step that died.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..procutil import no_window_flags
from .base import ToolRegistry, ToolSpec


def _git(root: Path, *argv: str, timeout: int = 120) -> tuple[int, str]:
    proc = subprocess.run(["git", *argv], cwd=str(root), text=True,
                          capture_output=True, timeout=timeout,
                          creationflags=no_window_flags(), encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def register_deploy_tools(registry: ToolRegistry, workspace: Path, *,
                          extra_roots=None) -> None:

    def _roots() -> list[Path]:
        roots = [workspace.resolve()]
        if extra_roots:
            try:
                for r in extra_roots() or []:
                    p = Path(r).resolve()
                    if p not in roots:
                        roots.append(p)
            except Exception:
                pass
        return roots

    def _resolve(raw: str) -> Path | None:
        if not raw:
            return None
        cand = Path(raw)
        root = (workspace / cand).resolve() if not cand.is_absolute() \
            else cand.resolve()
        if not any(root == b or b in root.parents for b in _roots()):
            return None
        return root if root.is_dir() else None

    def _repo_root(path: Path) -> Path | None:
        cur = path.resolve()
        for cand in (cur, *cur.parents):
            if (cand / ".git").exists():
                return cand
            if cand in _roots() and cand.parent == cand:
                break
        return None

    def deploy_static(args: dict[str, Any]) -> str:
        src = _resolve(str(args.get("path") or ""))
        if src is None:
            return json.dumps({"error": "path must be a directory inside "
                               "a registered workspace"})
        branch = str(args.get("branch") or "gh-pages").strip() or "gh-pages"
        message = str(args.get("message") or "").strip()
        repo = _repo_root(src)
        if repo is None:
            return json.dumps({"error": "no git repository found at or "
                               "above the target directory"})
        rc, remote = _git(repo, "remote", "get-url", "origin")
        if rc or not remote:
            return json.dumps({"error": "repo has no 'origin' remote",
                               "detail": remote})
        steps: list[str] = []
        tmp = repo / ".agent" / "deploy" / f"{branch}-{int(time.time())}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Isolated worktree — the user's branch/index are untouched.
            rc, out = _git(repo, "worktree", "add", "--detach",
                           str(tmp), "HEAD")
            if rc:
                return json.dumps({"error": "worktree add failed",
                                   "detail": out})
            steps.append("worktree added")

            # Check out the remote branch if it exists, else orphan it.
            rc, _ = _git(repo, "rev-parse", "--verify",
                         f"origin/{branch}")
            if rc == 0:
                rc, out = _git(tmp, "checkout", "-B", branch,
                               f"origin/{branch}")
            else:
                rc, out = _git(tmp, "checkout", "--orphan", branch)
            if rc:
                return json.dumps({"error": "branch checkout failed",
                                   "detail": out, "steps": steps})
            steps.append(f"on {branch}")

            # Replace worktree contents with the deploy dir.
            for child in tmp.iterdir():
                if child.name == ".git":
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            n = 0
            for f in src.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(src)
                if any(part in {".git", "node_modules", "__pycache__"}
                       for part in rel.parts):
                    continue
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                n += 1
            if not n:
                return json.dumps({"error": "deploy directory is empty",
                                   "steps": steps})
            steps.append(f"copied {n} files")

            _git(tmp, "add", "-A")
            rc, out = _git(tmp, "diff", "--cached", "--quiet")
            if rc == 0:
                return json.dumps({"ok": True, "branch": branch,
                                   "changed": False,
                                   "note": "branch already matches — "
                                           "nothing to deploy",
                                   "steps": steps})
            msg = message or f"Deploy {src.name} — {time.strftime('%Y-%m-%d %H:%M:%S')}"
            rc, out = _git(tmp, "-c", "user.email=nexus@local",
                           "-c", "user.name=Nexus Core",
                           "commit", "-m", msg)
            if rc:
                return json.dumps({"error": "commit failed",
                                   "detail": out, "steps": steps})
            rc, sha = _git(tmp, "rev-parse", "--short", "HEAD")
            steps.append(f"committed {sha}")

            rc, out = _git(tmp, "push", "origin", branch)
            if rc:
                return json.dumps({"error": "push failed", "detail": out,
                                   "commit": sha, "steps": steps})
            steps.append("pushed")
            return json.dumps({"ok": True, "branch": branch,
                               "commit": sha, "files": n,
                               "remote": remote, "steps": steps},
                              ensure_ascii=False)
        finally:
            if tmp.exists():
                _git(repo, "worktree", "remove", "--force", str(tmp))
                shutil.rmtree(tmp, ignore_errors=True)

    registry.register(ToolSpec(
        "deploy_static",
        "Publish a built directory to a git branch (default gh-pages) on the repo's origin remote using a temporary detached worktree — the user's checkout is never modified. Reports the real commit SHA and push result.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "built directory inside a registered workspace"},
                "branch": {"type": "string", "default": "gh-pages"},
                "message": {"type": "string"},
            },
            "required": ["path"],
        },
        "github.write",
        deploy_static,
        category="deploy",
        capabilities=["deploy_static", "git_push"],
    ))

    def deploy_release(args: dict[str, Any]) -> str:
        """Publish artifacts as a GitHub Release via the gh CLI."""
        gh_exe = shutil.which("gh")
        if gh_exe is None:
            return json.dumps({
                "error": "gh_cli_unavailable",
                "detail": "install GitHub CLI and run 'gh auth login'"})
        tag = str(args.get("tag") or "").strip()
        if not tag:
            return json.dumps({"error": "tag is required"})
        files: list[Path] = []
        for raw in args.get("files") or []:
            p = Path(str(raw))
            rp = (workspace / p).resolve() if not p.is_absolute() \
                else p.resolve()
            if not any(rp == b or b in rp.parents for b in _roots()):
                return json.dumps(
                    {"error": f"artifact {raw!r} is outside a registered "
                              "workspace"})
            if not rp.is_file():
                return json.dumps({"error": f"artifact not found: {raw!r}"})
            files.append(rp)
        repo_root = workspace.resolve()
        probe = repo_root
        while not (probe / ".git").exists() and probe != probe.parent:
            probe = probe.parent
        if not (probe / ".git").exists():
            return json.dumps({"error": "workspace is not inside a git repo"})
        rc, remote = _git(probe, "remote", "get-url", "origin")
        if rc or not remote:
            return json.dumps({"error": "repo has no 'origin' remote",
                               "detail": remote})
        # Resolve once — PATHEXT handles gh.exe/gh.cmd/.bat uniformly.
        cmd = [gh_exe, "release", "create", tag]
        cmd += [str(f) for f in files]
        name = str(args.get("name") or "").strip()
        if name:
            cmd += ["--title", name]
        notes = str(args.get("notes") or "").strip()
        if notes:
            cmd += ["--notes", notes]
        else:
            cmd += ["--generate-notes"]
        if args.get("draft"):
            cmd.append("--draft")
        if args.get("prerelease"):
            cmd.append("--prerelease")
        try:
            proc = subprocess.run(cmd, cwd=str(probe), capture_output=True,
                                  text=True, timeout=180,
                                  creationflags=no_window_flags(), encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "gh release create timed out"})
        if proc.returncode:
            return json.dumps({"error": "release create failed",
                               "detail": (proc.stderr or
                                          proc.stdout).strip()[:4000]})
        url = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        return json.dumps({"ok": True, "tag": tag, "release": url,
                           "files": len(files), "remote": remote},
                          ensure_ascii=False)

    registry.register(ToolSpec(
        "deploy_release",
        "Create a GitHub Release on the workspace repo's origin via the gh CLI and attach workspace artifact files. Reports the real release URL or the real gh error — never claims a release that didn't happen.",
        {
            "type": "object",
            "properties": {
                "tag": {"type": "string",
                        "description": "release tag, e.g. v0.20.0"},
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "artifact paths inside a workspace"},
                "name": {"type": "string"},
                "notes": {"type": "string",
                          "description": "release notes (default: generated)"},
                "draft": {"type": "boolean"},
                "prerelease": {"type": "boolean"},
            },
            "required": ["tag"],
        },
        "github.write",
        deploy_release,
        category="deploy",
        capabilities=["deploy_release", "git_push"],
    ))
