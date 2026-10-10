"""Storage audit (backlog §23).

Scans JSON persistence under the runtime root and reports which stores
have outgrown flat files: lookup speed, write contention, durability,
concurrency, and scalability all degrade once a JSON document is large
*and* high-frequency. Heavy stores (answer_memory, rag, hippocampus,
knowledge) already live in SQLite — this audits what remains so future
migrations are driven by measurement, not vibes.

Classification:
- ``sqlite_candidate`` — large (>1 MB) or record-heavy (>500 rows):
  flat-file rewrite cost per mutation is already O(file size).
- ``watch``           — medium (256 KB–1 MB or 100–500 rows).
- ``bounded_ok``      — small/static config-shaped files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SQLITE_SIZE_BYTES = 1_000_000
SQLITE_RECORDS = 500
WATCH_SIZE_BYTES = 256_000
WATCH_RECORDS = 100
_MAX_SCAN_FILES = 2000


def _record_count(data) -> int:
    if isinstance(data, dict):
        total = 0
        for v in data.values():
            if isinstance(v, (list, dict)):
                total += len(v)
            else:
                total += 1
        return total
    if isinstance(data, list):
        return len(data)
    return 1


def audit_storage(root: Path, *, max_files: int = _MAX_SCAN_FILES) -> dict:
    """Walk `root` for *.json stores; classify each by size/records."""
    root = Path(root).resolve()
    rows: list[dict] = []
    scanned = 0
    skipped_dirs = {".git", "node_modules", "__pycache__", ".venv",
                    ".repair-worktrees", ".nexus", ".agent"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skipped_dirs]
        for name in filenames:
            if scanned >= max_files:
                break
            if not name.lower().endswith(".json"):
                continue
            p = Path(dirpath) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            scanned += 1
            records = -1
            if size < 8 * 1024 * 1024:
                try:
                    records = _record_count(
                        json.loads(p.read_text(encoding="utf-8",
                                               errors="replace")))
                except (OSError, ValueError):
                    records = -1
            if size >= SQLITE_SIZE_BYTES or records >= SQLITE_RECORDS:
                cls = "sqlite_candidate"
            elif size >= WATCH_SIZE_BYTES or records >= WATCH_RECORDS:
                cls = "watch"
            else:
                cls = "bounded_ok"
            rows.append({
                "path": str(p.relative_to(root)),
                "size_bytes": size,
                "records": records,
                "class": cls,
            })
    rows.sort(key=lambda r: -r["size_bytes"])
    classes = {}
    for r in rows:
        classes[r["class"]] = classes.get(r["class"], 0) + 1
    return {
        "root": str(root),
        "files_scanned": scanned,
        "truncated": scanned >= max_files,
        "by_class": classes,
        "sqlite_candidates": [r for r in rows
                              if r["class"] == "sqlite_candidate"],
        "largest": rows[:10],
    }
