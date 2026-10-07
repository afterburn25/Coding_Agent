"""Verification gates for repair candidates.

Two levels:
- targeted: the tests most relevant to the fault (a failing test named
  by the incident, or the suspect module's test file)
- regression: the full suite (bounded — the coordinator decides whether
  it's required for promotion)

Each run is a bounded subprocess in the *worktree* — never the stable
tree — and reports structured results rather than parsing prose.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..procutil import no_window_flags


def run_unittest(workdir: Path, target: str, *, timeout_s: float = 300.0,
                 extra_env: dict | None = None) -> dict[str, Any]:
    """Run `python -m unittest <target>` inside the worktree.
    `target` may be a dotted module or a file path."""
    t0 = time.time()
    tgt = target.replace("/", ".").replace("\\", ".")
    if tgt.endswith(".py"):
        tgt = tgt[:-3]
    try:
        r = subprocess.run(
            [sys.executable, "-m", "unittest", tgt, "-v"],
            cwd=str(workdir), capture_output=True, text=True,
            timeout=timeout_s,
            env=None if extra_env is None else
            {**__import__("os").environ, **extra_env},
            creationflags=no_window_flags())
        tail = (r.stdout + r.stderr)[-4000:]
        return {"ok": r.returncode == 0, "returncode": r.returncode,
                "elapsed_s": round(time.time() - t0, 2),
                "target": target, "output_tail": tail}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -9, "timeout": True,
                "elapsed_s": timeout_s, "target": target,
                "output_tail": "timed out"}
    except OSError as exc:
        return {"ok": False, "returncode": -1, "error": str(exc),
                "target": target, "output_tail": str(exc)}


def targeted_tests_for(incident: dict, repo_root: Path) -> list[str]:
    """Smallest relevant suite: the failing test named in the incident,
    else a test file matching the top suspect's module name."""
    targets: list[str] = []
    msg = incident.get("error_message") or ""
    import re
    for m in re.finditer(r"(test[\w.]+)", msg, re.I):
        t = m.group(1)
        if t.startswith("tests."):
            # Dotted module inferred from error text — only run it if the
            # file actually exists in the tree; a hallucinated/misnamed
            # target must not count as a patch failure.
            rel = Path(*t.split(".")).with_suffix(".py")
            if (repo_root / rel).exists():
                targets.append(t)
    for s in (incident.get("suspects") or [])[:2]:
        stem = Path(s["path"]).stem
        for cand in repo_root.glob(f"tests/test_{stem}.py"):
            targets.append(str(cand.relative_to(repo_root))
                           .replace("\\", "/"))
        for cand in repo_root.glob(f"tests/*{stem}*.py"):
            targets.append(str(cand.relative_to(repo_root))
                           .replace("\\", "/"))
    rt = incident.get("regression_test")
    if rt:
        targets.insert(0, rt)
    seen, out = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:4]
