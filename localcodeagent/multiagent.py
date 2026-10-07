"""Multi-agent workflows via isolated Git worktrees (Part F).

Each concurrent coding agent gets its own worktree + branch under
``.agent/worktrees/``; agents never share a working copy. ``merge_back``
rebases/merges the agent's branch into the base in a controlled,
auditable step. The supervisor remains responsible for the objective —
this module is plumbing, not an autonomous agenda.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .procutil import no_window_flags


def _git(repo: Path, *args: str) -> tuple[int, str]:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, timeout=120,
                         creationflags=no_window_flags())
    return out.returncode, (out.stdout + out.stderr).strip()


def is_repo(path: Path) -> bool:
    rc, out = _git(path, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out.strip() == "true"


class WorktreeAgent:
    """One specialized agent bound to an isolated worktree."""

    def __init__(self, repo: Path, role: str,
                 worktree_root: Path | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.role = role
        self.id = f"{role}-{uuid.uuid4().hex[:8]}"
        self.branch = f"nexus-agent/{self.id}"
        self.wt_root = Path(worktree_root) if worktree_root else (
            self.repo / ".agent" / "worktrees")
        self.path = self.wt_root / self.id
        self.state = "created"
        self.created_at = time.time()
        self.report: dict[str, Any] = {}

    # -- lifecycle -----------------------------------------------------------

    def provision(self, *, base_ref: str = "HEAD") -> dict[str, Any]:
        if not is_repo(self.repo):
            return {"ok": False, "error": f"{self.repo} is not a git repo"}
        self.wt_root.mkdir(parents=True, exist_ok=True)
        rc, out = _git(self.repo, "worktree", "add", "-b", self.branch,
                       str(self.path), base_ref)
        if rc != 0:
            return {"ok": False, "error": out[:400]}
        self.state = "ready"
        return {"ok": True, "path": str(self.path), "branch": self.branch}

    def status(self) -> dict[str, Any]:
        rc, out = _git(self.path, "status", "--porcelain") if self.path.is_dir() \
            else (1, "no worktree")
        return {"id": self.id, "role": self.role, "state": self.state,
                "path": str(self.path), "branch": self.branch,
                "dirty": bool(out.strip()) if rc == 0 else None}

    def commit_work(self, message: str) -> dict[str, Any]:
        """Commit all changes inside the agent worktree."""
        rc, out = _git(self.path, "add", "-A")
        if rc != 0:
            return {"ok": False, "error": out[:300]}
        rc, out = _git(self.path, "-c", "user.name=Nexus Agent",
                       "-c", "user.email=nexus-agent@local",
                       "commit", "-m", str(message)[:300])
        if rc != 0 and "nothing to commit" in out.lower():
            return {"ok": True, "committed": False}
        if rc != 0:
            return {"ok": False, "error": out[:400]}
        return {"ok": True, "committed": True}

    def merge_back(self) -> dict[str, Any]:
        """Controlled merge of the agent branch into the repo's current
        branch. Refuses to proceed on conflicts — nothing is left half-
        merged."""
        rc, head = _git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        base = head.strip() or "HEAD"
        # --no-ff creates a merge commit; CI runners/fresh machines may have
        # no configured identity — attribute it to the agent runtime.
        rc, out = _git(self.repo, "-c", "user.name=Nexus Agent",
                       "-c", "user.email=nexus-agent@local",
                       "merge", "--no-ff", "--no-edit", self.branch)
        if rc != 0:
            _git(self.repo, "merge", "--abort")
            return {"ok": False, "error": f"merge conflict — aborted: {out[:300]}",
                    "base": base}
        self.state = "merged"
        return {"ok": True, "base": base, "merged": self.branch}

    def teardown(self, *, delete_branch: bool = True) -> dict[str, Any]:
        rc, out = _git(self.repo, "worktree", "remove", "--force",
                       str(self.path))
        shutil.rmtree(self.path, ignore_errors=True)
        if delete_branch:
            _git(self.repo, "branch", "-D", self.branch)
        self.state = "closed"
        return {"ok": True}


class AgentPool:
    """Manages a set of concurrent role-scoped worktree agents."""

    ROLES = ("planner", "researcher", "coder", "reviewer", "tester",
             "debugger", "security")

    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo).resolve()
        self.agents: dict[str, WorktreeAgent] = {}

    def spawn(self, role: str, *, base_ref: str = "HEAD") -> dict[str, Any]:
        if role not in self.ROLES:
            return {"ok": False, "error": f"unknown role '{role}'"}
        agent = WorktreeAgent(self.repo, role)
        out = agent.provision(base_ref=base_ref)
        if out.get("ok"):
            self.agents[agent.id] = agent
            out["id"] = agent.id
            out["role"] = role
        return out

    def status(self) -> list[dict]:
        return [a.status() for a in self.agents.values()]

    def collect(self) -> dict[str, Any]:
        """Merge every committed agent branch back, in spawn order —
        stopping at the first conflict so a partial merge never lands."""
        results = []
        for agent in self.agents.values():
            if agent.state != "ready":
                continue
            commit = agent.commit_work(f"[{agent.role}] nexus agent work")
            if not commit.get("ok"):
                results.append({"id": agent.id, "ok": False,
                                "stage": "commit", "error": commit["error"]})
                continue
            merge = agent.merge_back()
            results.append({"id": agent.id, "ok": merge.get("ok"),
                            "stage": "merge", **merge})
            if not merge.get("ok"):
                return {"ok": False, "results": results}
        return {"ok": True, "results": results}

    def teardown_all(self) -> None:
        for a in self.agents.values():
            a.teardown(delete_branch=a.state != "merged")
        self.agents.clear()


def sweep_worktree_orphans(repo: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Reclaim worktree dirs stranded by a crashed/killed process.

    Worktree directories under ``.agent/worktrees`` (or ``root`` when given)
    are removed (``git worktree remove --force`` with an rmtree fallback).
    ``nexus-agent/*``/``nexus/*`` branches are deliberately kept — a
    merge-conflict path preserves them for recovery — and their names are
    returned so callers can surface them rather than silently discarding
    work.
    """
    repo = Path(repo).resolve()
    if not is_repo(repo):
        return {"removed": 0, "kept_branches": []}
    root = Path(root) if root is not None else repo / ".agent" / "worktrees"
    removed = 0
    if root.is_dir():
        rc, out = _git(repo, "worktree", "list", "--porcelain")
        live = set()
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("worktree "):
                    live.add(str(Path(line[9:].strip()).resolve()))
        for d in root.iterdir():
            if not d.is_dir():
                continue
            if str(d.resolve()) in live:
                _git(repo, "worktree", "remove", "--force", str(d))
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
            removed += 1
    kept = []
    for pattern in ("nexus-agent/*", "nexus/*"):
        rc, out = _git(repo, "branch", "--list", pattern)
        if rc == 0:
            kept.extend(line.strip().lstrip("* ").strip()
                        for line in out.splitlines() if line.strip())
    return {"removed": removed, "kept_branches": kept}
