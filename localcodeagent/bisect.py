"""Bounded git bisect in an isolated worktree — never the user's tree.

Given a known-good and known-bad ref plus a reproducible test command,
binary-search `good..bad` for the first failing commit inside a throwaway
`git worktree`. Iterations and per-step runtime are capped; the worktree
is always removed afterwards. Returns the first bad commit, its message,
a bounded diff stat, and per-step evidence — enough to feed the
hypothesis engine / self-repair without trusting guesswork.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from .procutil import no_window_flags


class GitBisector:
    def __init__(self, repo_root: Path | str, *,
                 work_root: Path | str | None = None):
        self.repo_root = Path(repo_root)
        self.work_root = Path(work_root) if work_root else None

    def _git(self, *args: str, cwd: Path | None = None,
             timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(cwd or self.repo_root),
            capture_output=True, text=True, timeout=timeout,
            errors="replace", creationflags=no_window_flags())

    def run(self, *, good: str, bad: str, test_command: str,
            timeout_s: float = 600.0, step_timeout_s: float = 120.0,
            max_steps: int = 12) -> dict[str, Any]:
        started = time.monotonic()
        evidence: list[dict] = []
        result: dict[str, Any] = {"ok": False, "first_bad": "",
                                  "first_bad_message": "",
                                  "steps": 0, "evidence": evidence,
                                  "detail": ""}

        def fail(msg: str) -> dict:
            result["detail"] = msg
            return result

        revs = self._git("rev-list", "--reverse", f"{good}..{bad}")
        if revs.returncode != 0 or not revs.stdout.strip():
            return fail(f"cannot list range {good}..{bad}: "
                        f"{revs.stderr.strip()[:200]}")
        commits = [c.strip() for c in revs.stdout.splitlines() if c.strip()]
        if len(commits) < 1:
            return fail("empty commit range")

        # Isolated worktree at `bad` — the user's checkout is untouched.
        import uuid
        wt = (self.work_root or (self.repo_root / ".bisect_tmp" /
                                 uuid.uuid4().hex[:8]))
        try:
            add = self._git("worktree", "add", "--detach", str(wt), bad,
                            timeout=120.0)
            if add.returncode != 0:
                return fail(f"worktree add failed: "
                            f"{add.stderr.strip()[:200]}")

            def run_test(sha: str) -> bool:
                """checkout sha in the worktree; test pass?"""
                co = self._git("checkout", "--detach", sha,
                               cwd=wt, timeout=60.0)
                if co.returncode != 0:
                    evidence.append({"sha": sha, "checkout": "failed",
                                     "detail": co.stderr.strip()[:200]})
                    return False
                try:
                    r = subprocess.run(
                        test_command, shell=True, cwd=str(wt),
                        capture_output=True, text=True,
                        timeout=step_timeout_s, errors="replace",
                        creationflags=no_window_flags())
                    ok = r.returncode == 0
                    tail = (r.stdout + r.stderr).strip()[-300:]
                except subprocess.TimeoutExpired:
                    ok, tail = False, "test timeout"
                evidence.append({"sha": sha, "ok": ok,
                                 "output": tail})
                return ok

            # Endpoints must satisfy the contract before searching.
            if not run_test(good):
                return fail(f"known-good ref {good[:8]} fails the "
                            "reproduction test — range is unusable")
            if run_test(bad):
                return fail(f"known-bad ref {bad[:8]} passes the "
                            "reproduction test — range is unusable")

            # `good` (verified above) sits just before commits[0]; `bad`
            # is commits[-1]. Invariant: index `lo` (or good when lo<0)
            # passes, index `hi` fails.
            lo, hi = -1, len(commits) - 1
            steps = 0
            while hi - lo > 1 and steps < max_steps \
                    and time.monotonic() - started < timeout_s:
                mid = (lo + hi) // 2
                steps += 1
                if run_test(commits[mid]):
                    lo = mid
                else:
                    hi = mid
            result["steps"] = steps
            first_bad = commits[hi]
            info = self._git("show", "--format=%H%n%s%n%an",
                             "--stat", "-1", first_bad, timeout=30.0)
            result["ok"] = True
            result["first_bad"] = first_bad
            result["first_bad_message"] = (
                info.stdout.splitlines()[1]
                if info.returncode == 0 and len(info.stdout.splitlines()) > 1
                else "")
            result["diff_stat"] = "\n".join(
                info.stdout.splitlines()[3:])[:2000] \
                if info.returncode == 0 else ""
            result["commits_scanned"] = len(commits)
            return result
        finally:
            # Always clean up the bisect worktree — bounded, forced.
            try:
                self._git("worktree", "remove", "--force", str(wt),
                          timeout=60.0)
            except Exception:
                pass
