"""Localizer — narrow a failure to ranked suspect files/functions.

Evidence sources, in confidence order:
1. Traceback frames that resolve to real repo files (innermost first).
2. A failing test name → the test file + the module it imports.
3. Recent commits touching the frame files (regression correlation).

Confidence is deliberately conservative: a stack frame alone never
exceeds 0.85; only frame + fresh-commit correlation or a reproduction
approaches 1.0. When nothing maps into the repo, suspects is empty —
the diagnoser then favors environmental/operational causes.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable

_FRAME_RX = re.compile(
    r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>[\w<>]+)',
    re.M)
_TEST_ID_RX = re.compile(r"([\w.]+) \(([\w.]+)\)|FAIL: (\S+)")


def parse_traceback(stack: str) -> list[dict[str, Any]]:
    out = []
    for m in _FRAME_RX.finditer(stack or ""):
        out.append({"path": m.group("path"), "line": int(m.group("line")),
                    "function": m.group("func")})
    return out


class Localizer:
    def __init__(self, repo_root: Path,
                 git: Callable[..., subprocess.CompletedProcess] | None = None):
        self.repo_root = Path(repo_root)
        self._git = git or self._run_git

    def _run_git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True, text=True, timeout=15)

    def _in_repo(self, path: str) -> str:
        """Map a traceback path to a repo-relative path, or ''.

        Foreign-root paths (CI runners, site-packages) can't resolve
        relative to repo_root — fall back to a suffix match so
        '/home/runner/work/.../localcodeagent/x.py' still maps to
        'localcodeagent/x.py'."""
        try:
            p = Path(path)
            if not p.is_absolute():
                p = (self.repo_root / p)
            rel = p.resolve().relative_to(self.repo_root.resolve())
            if (self.repo_root / rel).is_file():
                return str(rel).replace("\\", "/")
        except (OSError, ValueError):
            pass
        # Suffix match: try repo_root/<tail> for each leading cut depth.
        try:
            parts = Path(path).parts
            for i in range(1, len(parts)):
                cand = self.repo_root.joinpath(*parts[i:])
                if cand.is_file():
                    return str(cand.relative_to(self.repo_root)
                               ).replace("\\", "/")
        except (OSError, ValueError):
            pass
        return ""

    def _recent_commit_files(self, n: int = 10) -> dict[str, int]:
        """file → number of recent commits touching it (bounded)."""
        try:
            out = self._git("log", f"-{n}", "--name-only",
                            "--pretty=format:").stdout
        except Exception:
            return {}
        counts: dict[str, int] = {}
        for line in out.splitlines():
            f = line.strip()
            if f:
                counts[f] = counts.get(f, 0) + 1
        return counts

    def localize(self, incident: dict) -> list[dict[str, Any]]:
        stack = incident.get("stack_trace") or ""
        frames = parse_traceback(stack)
        recent = self._recent_commit_files(15)
        suspects: dict[str, dict[str, Any]] = {}
        depth = 0
        for fr in reversed(frames):  # innermost first
            rel = self._in_repo(fr["path"])
            if not rel:
                continue
            depth += 1
            base = max(0.3, 0.85 - 0.15 * (depth - 1))
            commits = recent.get(rel, 0)
            conf = min(0.97, base + min(commits, 3) * 0.04)
            s = suspects.setdefault(rel, {"path": rel,
                                          "function": fr["function"],
                                          "line": fr["line"],
                                          "confidence": 0.0,
                                          "recent_commits": commits})
            s["confidence"] = max(s["confidence"], round(conf, 2))
            if depth == 1:
                s["line"] = fr["line"]
                s["function"] = fr["function"]

        # A failing test name maps to its test file — useful when the
        # traceback itself is empty (CI log, pytest summary line).
        for m in _TEST_ID_RX.finditer(incident.get("error_message") or ""):
            name = m.group(2) or m.group(3) or ""
            mod = name.split(".")[0] if "." in name else name
            for cand in self.repo_root.glob(f"tests/**/{mod}.py"):
                rel = str(cand.relative_to(self.repo_root)).replace("\\", "/")
                suspects.setdefault(rel, {
                    "path": rel, "function": "", "line": 0,
                    "confidence": 0.5,
                    "recent_commits": recent.get(rel, 0)})

        out = sorted(suspects.values(),
                     key=lambda s: -s["confidence"])
        return out[:8]
