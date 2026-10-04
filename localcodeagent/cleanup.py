"""Bounded idle cleanup — scan for reclaimable waste, suggest, and
(optionally) delete only inside the runtime data root.

`IdleCleaner` never touches the user's source tree. `scan()` finds
candidates (stale worktrees, oversized logs, expired temp dirs, old
artifact stores); `run()` deletes bounded candidates only when
explicitly invoked, and reports exactly what was reclaimed.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

# Only these runtime subtrees are ever eligible for cleanup.
SCAN_DIRS = ("worktrees", "data/logs", "data/tmp", "data/cache",
             "tmp", "logs")
STALE_SECONDS = 24 * 3600
BIG_FILE_BYTES = 25 * 1024 * 1024
MAX_DELETE_BYTES = 512 * 1024 * 1024          # per run
MAX_DELETE_ITEMS = 200


def _size(p: Path) -> int:
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


class IdleCleaner:
    def __init__(self, runtime_root: Path) -> None:
        self.root = Path(runtime_root)

    def _within_root(self, p: Path) -> bool:
        try:
            p.resolve().relative_to(self.root.resolve())
            return True
        except (OSError, ValueError):
            return False

    def scan(self, *, stale_after: float = STALE_SECONDS,
             big_file: int = BIG_FILE_BYTES) -> list[dict]:
        """Find candidates — suggestion only, nothing deleted."""
        now = time.time()
        out: list[dict] = []
        for rel in SCAN_DIRS:
            base = self.root / rel
            if not base.is_dir():
                continue
            try:
                entries = list(base.iterdir())
            except OSError:
                continue
            for e in entries:
                try:
                    age = now - e.stat().st_mtime
                    sz = _size(e)
                except OSError:
                    continue
                if e.is_dir() and age > stale_after and sz > 0:
                    out.append({"path": str(e), "kind": "stale_dir",
                                "bytes": sz, "age_hours":
                                round(age / 3600, 1),
                                "reason": f"idle {age/3600:.0f}h in {rel}"})
                elif e.is_file() and sz > big_file:
                    out.append({"path": str(e), "kind": "big_file",
                                "bytes": sz,
                                "reason": f"{sz/1e6:.0f} MB in {rel}"})
        out.sort(key=lambda c: -c["bytes"])
        return out[:500]

    def run(self, *, dry_run: bool = True) -> dict:
        """Delete scan candidates, bounded per run. dry_run (default)
        only reports what would be reclaimed."""
        cands = self.scan()
        reclaimed = 0
        done, skipped = [], []
        for c in cands:
            p = Path(c["path"])
            if not self._within_root(p):
                skipped.append({"path": c["path"],
                                "reason": "outside runtime root"})
                continue
            if reclaimed + c["bytes"] > MAX_DELETE_BYTES or \
                    len(done) >= MAX_DELETE_ITEMS:
                skipped.append({"path": c["path"],
                                "reason": "bounded limit reached"})
                continue
            if dry_run:
                done.append({"path": c["path"], "bytes": c["bytes"],
                             "dry_run": True})
                reclaimed += c["bytes"]
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                done.append({"path": c["path"], "bytes": c["bytes"]})
                reclaimed += c["bytes"]
            except OSError as exc:
                skipped.append({"path": c["path"],
                                "reason": str(exc)[:120]})
        return {"dry_run": dry_run, "reclaimed_bytes": reclaimed,
                "candidates": len(cands), "deleted": done,
                "skipped": skipped[:50]}
