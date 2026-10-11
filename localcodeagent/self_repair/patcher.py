"""Isolated repair worktrees.

Code repairs never touch the stable tree directly. Each incident gets a
git worktree under `<repo>/.repair-worktrees/<incident-id>` (on the
`repair/<incident-id>` branch). When git is unavailable the patcher falls
back to a plain file-copy sandbox so the pipeline still works — promotion
then applies changed files rather than merging a branch.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from ..procutil import no_window_flags


class Patcher:
    def __init__(self, repo_root: Path, *,
                 git: Callable[..., subprocess.CompletedProcess] | None = None):
        self.repo_root = Path(repo_root)
        self._git = git or self._run_git

    def _run_git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True, text=True, timeout=30,
            creationflags=no_window_flags(), encoding="utf-8", errors="replace")

    def _git_ok(self, *args: str) -> bool:
        try:
            return self._git(*args).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def is_git_repo(self) -> bool:
        return self._git_ok("rev-parse", "--is-inside-work-tree")

    # ------------------------------------------------------------------
    def create(self, incident_id: str) -> Path:
        """Isolated workspace for the candidate repair."""
        wt = self.repo_root / ".repair-worktrees" / incident_id
        if wt.exists():
            return wt  # resume after restart — same worktree is reused
        if self.is_git_repo():
            r = self._git("worktree", "add", "-b", f"repair/{incident_id}",
                          str(wt), "HEAD")
            if r.returncode == 0:
                return wt
            # Stale branch/registration — the worktree dir may be gone but
            # git still lists repair/<id> as checked out, so `branch -D`
            # would refuse. Drop the worktree registration first, then the
            # branch, then retry once.
            self._git("worktree", "remove", "--force", str(wt))
            self._git("worktree", "prune")
            self._git("branch", "-D", f"repair/{incident_id}")
            r = self._git("worktree", "add", "-b",
                          f"repair/{incident_id}", str(wt), "HEAD")
            if r.returncode == 0:
                return wt
        # Fallback sandbox: copy the repo shallowly (code only) so tests
        # still exercise the pipeline where git is unavailable.
        wt.mkdir(parents=True, exist_ok=True)
        skip = {".git", ".repair-worktrees", "__pycache__", "node_modules",
                "data", "output", "models", "tools", "build", "dist",
                ".venv", "venv"}
        for src in self.repo_root.iterdir():
            if src.is_dir() and src.name not in skip:
                shutil.copytree(src, wt / src.name,
                                ignore=shutil.ignore_patterns(
                                    "__pycache__", ".git",
                                    ".repair-worktrees"))
        return wt

    def changed_files(self, wt: Path, base_files: list[str] | None = None) -> list[str]:
        """Repo-relative *file* paths modified/added inside the worktree.
        Directories, `__pycache__`, and other repair worktrees are never
        candidate payload."""
        def _ok(rel: str) -> bool:
            if not rel or rel.endswith("/"):
                return False
            parts = Path(rel).parts
            return "__pycache__" not in parts \
                and ".repair-worktrees" not in parts
        if self.is_git_repo():
            try:
                out = subprocess.run(
                    ["git", "-C", str(wt), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=no_window_flags(), encoding="utf-8", errors="replace").stdout
                files = []
                for line in out.splitlines():
                    if not line.strip():
                        continue
                    rel = line[3:].strip().split(" -> ")[-1].strip('"')
                    if _ok(rel) and (wt / rel).is_file():
                        files.append(rel)
                return files
            except (OSError, subprocess.TimeoutExpired):
                return []
        # sandbox: diff against the stable tree byte-for-byte
        changed = []
        for p in wt.rglob("*"):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            rel = p.relative_to(wt)
            src = self.repo_root / rel
            if not src.is_file() or src.read_bytes() != p.read_bytes():
                changed.append(str(rel).replace("\\", "/"))
        return changed

    def promote(self, wt: Path) -> str:
        """Apply the candidate to the stable tree. Returns a description
        of how it was applied (for the audit trail)."""
        if self.is_git_repo() and (wt / ".git").exists():
            r = subprocess.run(
                ["git", "-C", str(wt), "diff", "HEAD", "--binary"],
                capture_output=True, timeout=30,
                creationflags=no_window_flags())
            if r.returncode == 0 and r.stdout.strip():
                ap = subprocess.run(
                    ["git", "-C", str(self.repo_root), "apply", "--whitespace=nowarn"],
                    input=r.stdout, capture_output=True, timeout=30,
                    creationflags=no_window_flags())
                if ap.returncode == 0:
                    # git diff excludes untracked additions (e.g. a new
                    # regression test) — copy anything still differing.
                    for f in self.changed_files(wt):
                        src, dst = wt / f, self.repo_root / f
                        if src.is_file() and (not dst.is_file() or
                                              src.read_bytes()
                                              != dst.read_bytes()):
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)
                    return "git-apply"
            # fallback — copy all differing files
            self._copy_changed(wt)
            return "copy-untracked"
        self._copy_changed(wt)
        return "copy-sandbox"

    def _copy_changed(self, wt: Path) -> None:
        for f in self.changed_files(wt):
            src, dst = wt / f, self.repo_root / f
            if not src.is_file():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def commit_promotion(self, files: list[str], *, incident_id: str,
                         summary: str, evidence: str = "") -> str:
        """Commit exactly the promoted repair files — an auditable,
        revertible record instead of a silently dirty tree. Never sweeps
        unrelated staged work (the pathspec bounds the commit). Returns
        the commit hash, or '' when no commit was possible."""
        if not self.is_git_repo():
            return ""
        paths = [f for f in files
                 if f and (self.repo_root / f).is_file()]
        if not paths:
            return ""
        try:
            if self._git("add", "--", *paths).returncode != 0:
                return ""
            msg = (f"Self-repair: {summary}\n\n"
                   f"Incident: {incident_id}\n"
                   f"{evidence}\n\n"
                   "Generated by Nexus self-repair.")
            r = self._git("commit", "-m", msg, "--", *paths)
            if r.returncode != 0:
                # No configured identity — attribute the repair to Nexus.
                r = self._git("-c", "user.name=Nexus Self-Repair",
                              "-c", "user.email=self-repair@nexus.local",
                              "commit", "-m", msg, "--", *paths)
                if r.returncode != 0:
                    return ""
            head = self._git("rev-parse", "--short", "HEAD")
            return head.stdout.strip() if head.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def cleanup(self, wt: Path, incident_id: str) -> None:
        if self.is_git_repo():
            self._git("worktree", "remove", "--force", str(wt))
            self._git("branch", "-D", f"repair/{incident_id}")
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
