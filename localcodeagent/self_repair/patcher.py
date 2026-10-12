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

    def worktree_base(self, wt: Path) -> str:
        """The primary-checkout commit the worktree diverged from —
        `git diff <base>` inside the worktree describes the whole
        candidate, committed AND uncommitted. '' when unresolvable."""
        try:
            head = self._git("rev-parse", "HEAD")
            if head.returncode != 0:
                return ""
            mb = subprocess.run(
                ["git", "-C", str(wt), "merge-base", "HEAD",
                 head.stdout.strip()],
                capture_output=True, text=True, timeout=15,
                creationflags=no_window_flags(), encoding="utf-8",
                errors="replace")
            return mb.stdout.strip() if mb.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def changed_files(self, wt: Path, base_files: list[str] | None = None) -> list[str]:
        """Repo-relative *file* paths the candidate changes — dirty and
        untracked work plus anything committed on the repair branch
        (missions may commit inside their sandbox; `status --porcelain`
        alone would call a fully committed patch "empty").
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
                files: set[str] = set()
                out = subprocess.run(
                    ["git", "-C", str(wt), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=no_window_flags(), encoding="utf-8", errors="replace").stdout
                for line in out.splitlines():
                    if not line.strip():
                        continue
                    rel = line[3:].strip().split(" -> ")[-1].strip('"')
                    if _ok(rel) and (wt / rel).is_file():
                        files.add(rel)
                base = self.worktree_base(wt)
                if base:
                    committed = subprocess.run(
                        ["git", "-C", str(wt), "diff", "--name-only", base],
                        capture_output=True, text=True, timeout=15,
                        creationflags=no_window_flags(), encoding="utf-8",
                        errors="replace").stdout
                    for rel in committed.splitlines():
                        rel = rel.strip()
                        if _ok(rel) and (wt / rel).is_file():
                            files.add(rel)
                return sorted(files)
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

    def _drifted(self, wt: Path, base: str,
                 files: list[str]) -> str | None:
        """Name of a candidate file the primary moved since the
        worktree was cut, else None. Copying or applying over a drifted
        file would silently revert primary-tree changes made after the
        worktree's base — promotion must fail loudly instead."""
        for f in files:
            try:
                show = subprocess.run(
                    ["git", "-C", str(self.repo_root), "rev-parse",
                     f"{base}:{f}"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=no_window_flags())
                base_blob = show.stdout.strip() \
                    if show.returncode == 0 else None
                dst = self.repo_root / f
                if dst.is_file():
                    # hash-object normalizes line endings the same way
                    # the stored blob does — raw byte compares flag CRLF
                    # checkouts as drift they aren't.
                    cur = subprocess.run(
                        ["git", "-C", str(self.repo_root),
                         "hash-object", "--", f],
                        capture_output=True, text=True, timeout=15,
                        creationflags=no_window_flags())
                    cur_blob = cur.stdout.strip() \
                        if cur.returncode == 0 else "<unreadable>"
                else:
                    cur_blob = None
            except (OSError, subprocess.TimeoutExpired):
                return f
            if cur_blob != base_blob:
                return f
        return None

    def promote(self, wt: Path, files: list[str] | None = None) -> str:
        """Apply the candidate to the stable tree. Returns a description
        of how it was applied (for the audit trail).

        The whole candidate — committed AND uncommitted worktree work —
        lands as one diff against the worktree's merge-base, plus copies
        of untracked additions. If the primary moved any candidate file
        since the worktree was cut, promotion refuses loudly: wholesale
        copying would silently revert newer primary-tree changes.

        `files` bounds the payload to the reviewed set — the diff
        pathspec and the untracked-copy loop both honor it, so a
        worktree file the reviewer never saw can never promote."""
        if not (self.is_git_repo() and (wt / ".git").exists()):
            self._copy_changed(wt, files)
            return "copy-sandbox"
        base = self.worktree_base(wt)
        files = files or self.changed_files(wt)
        if base:
            drifted = self._drifted(wt, base, files)
            if drifted:
                raise RuntimeError(
                    f"primary checkout moved {drifted} since the repair "
                    "worktree was cut — promotion aborted "
                    "(conflict, never clobbered)")
        diff_cmd = ["git", "-C", str(wt), "diff", base or "HEAD",
                    "--binary"]
        if files:
            diff_cmd += ["--", *files]
        r = subprocess.run(
            diff_cmd, capture_output=True, timeout=30,
            creationflags=no_window_flags())
        if r.returncode == 0 and r.stdout.strip():
            ap = subprocess.run(
                ["git", "-C", str(self.repo_root), "apply",
                 "--whitespace=nowarn"],
                input=r.stdout, capture_output=True, timeout=30,
                creationflags=no_window_flags())
            if ap.returncode != 0:
                raise RuntimeError(
                    "candidate diff does not apply on the current "
                    "tree: " + (ap.stderr or b"").decode(
                        "utf-8", errors="replace")[:400])
        # Anything still differing is an untracked addition (diff never
        # covers `??` files) — copy it over.
        for f in files or self.changed_files(wt):
            src, dst = wt / f, self.repo_root / f
            if src.is_file() and (not dst.is_file() or
                                  src.read_bytes() != dst.read_bytes()):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        return "git-apply"

    def _copy_changed(self, wt: Path,
                      files: list[str] | None = None) -> None:
        for f in (files or self.changed_files(wt)):
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
