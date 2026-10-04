"""Per-worker isolated workspaces for parallel coding.

Wraps multiagent.WorktreeAgent with worker-scoped naming, scope recording
and cleanup policy — several coding workers may run on the SAME repository
concurrently because each owns a ``.nexus/worktrees/<worker>`` checkout on
a traceable ``nexus/worker-NNN/<slug>`` branch. Nothing merges to main
without the integrator path.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ..multiagent import WorktreeAgent, is_repo, sweep_worktree_orphans


def _slug(text: str, *, limit: int = 24) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:limit].strip("-") or "task"


# Paths multiple workers are likely to touch — planner/integrator owns
# these; a worker whose scope intersects them is flagged for review.
SHARED_HOTSPOTS = (
    "VERSION", "CHANGELOG.md", "PROJECT_STATUS.md", "pyproject.toml",
    "package.json", "package-lock.json", "requirements", "installer/",
    "localcodeagent/models/router.py", "localcodeagent/server.py",
)


class WorkerWorkspace:
    """Isolated worktree + branch bound to a worker record."""

    def __init__(self, repo: Path, worker_id: str, title: str,
                 *, scope: list[str] | None = None,
                 worktree_root: Path | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.worker_id = worker_id
        self.scope = list(scope or [])
        self.agent = WorktreeAgent(self.repo, role="worker")
        self.agent.id = worker_id
        self.agent.branch = f"nexus/{worker_id}/{_slug(title)}"
        self.agent.wt_root = (Path(worktree_root) if worktree_root
                              is not None else self.repo / ".nexus"
                              / "worktrees")
        self.agent.path = self.agent.wt_root / worker_id

    @property
    def path(self) -> Path:
        return self.agent.path

    @property
    def branch(self) -> str:
        return self.agent.branch

    def provision(self, *, base_ref: str = "HEAD") -> dict[str, Any]:
        out = self.agent.provision(base_ref=base_ref)
        if out.get("ok"):
            out["worker_id"] = self.worker_id
            out["scope"] = self.scope
        return out

    def diff_files(self) -> list[str]:
        """Files this worker changed vs the base ref it started from."""
        rc, out = _git(self.agent.path,
                       "diff", "--name-only", "HEAD") \
            if self.agent.path.is_dir() else (1, "")
        files = [l.strip() for l in out.splitlines() if l.strip()]
        if rc != 0:
            return files
        rc2, out2 = _git(self.agent.path, "status", "--porcelain")
        if rc2 == 0:
            for l in out2.splitlines():
                p = l[3:].strip()
                if p and p not in files:
                    files.append(p)
        return sorted(files)

    def overlaps_shared(self, files: list[str] | None = None) -> list[str]:
        """Changed files intersecting the integration-owned hotspot list."""
        hits = []
        for f in (files if files is not None else self.diff_files()):
            for hot in SHARED_HOTSPOTS:
                if f == hot or f.startswith(hot) or hot.startswith(f):
                    hits.append(f)
                    break
        return hits

    def commit(self, message: str) -> dict[str, Any]:
        return self.agent.commit_work(message)

    def teardown(self, *, merged: bool = False) -> dict[str, Any]:
        return self.agent.teardown(delete_branch=not merged)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    import subprocess
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, timeout=120)
    return out.returncode, (out.stdout + out.stderr).strip()


def detect_scope_overlap(scopes: dict[str, list[str]]) -> dict[str, list[str]]:
    """Map file-pattern → [worker_ids] for patterns claimed by more than one
    worker scope. The planner uses this to keep workers apart; the
    integrator uses it to pre-flag conflict risk."""
    claims: dict[str, list[str]] = {}
    for wid, pats in scopes.items():
        for p in pats:
            claims.setdefault(p, []).append(wid)
    return {p: wids for p, wids in claims.items() if len(wids) > 1}


def reconcile_worktrees(repo: Path) -> dict[str, Any]:
    """Startup sweep — stranded worktrees from a crash are reclaimed;
    surviving nexus/* branches are surfaced, never silently deleted."""
    out = sweep_worktree_orphans(repo)
    nexus = sweep_worktree_orphans(
        repo, root=Path(repo).resolve() / ".nexus" / "worktrees")
    out["removed"] = out.get("removed", 0) + nexus.get("removed", 0)
    kept = set(out.get("kept_branches") or [])
    kept.update(nexus.get("kept_branches") or [])
    out["kept_branches"] = sorted(kept)
    out["ts"] = time.time()
    return out


__all__ = ["WorkerWorkspace", "detect_scope_overlap", "reconcile_worktrees",
           "SHARED_HOTSPOTS", "is_repo"]
