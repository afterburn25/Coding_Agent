"""Data lineage — what contributed to an output.

`LineageStore` (`data/lineage.json`) records, for each produced target
(answer, report, code change, generated artifact), the contributors
that fed it: source files, project memory, Answer Memory, web research,
tool output, workers, reviewer verdicts, decision journal entries.

Bounded store (1000 records); every record links a target to a list of
contributor refs so provenance is inspectable rather than implied.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

MAX_RECORDS = 1000

CONTRIBUTOR_KINDS = {
    "file", "memory", "answer_memory", "web", "tool_output",
    "worker", "reviewer", "decision", "requirement", "artifact",
    "user", "other",
}

TARGET_KINDS = {"answer", "report", "code_change", "artifact",
                "decision", "mission", "other"}


class LineageStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._rows = list(data.get("records") or [])[-MAX_RECORDS:]
        except (OSError, ValueError):
            self._rows = []

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            {"version": 1, "records": self._rows[-MAX_RECORDS:]},
            indent=2, ensure_ascii=False, default=str))

    def record(self, target_id: str, *, target_kind: str = "",
               contributors: list[dict] | None = None,
               project_id: str = "", mission_id: str = "",
               task_id: str = "", note: str = "") -> dict:
        """Append a lineage record. Each contributor: {kind, ref, note}."""
        kind = target_kind if target_kind in TARGET_KINDS else "other"
        contribs = []
        for c in list(contributors or [])[:50]:
            ck = str(c.get("kind") or "other")
            if ck not in CONTRIBUTOR_KINDS:
                ck = "other"
            contribs.append({"kind": ck,
                             "ref": str(c.get("ref") or "")[:400],
                             "note": str(c.get("note") or "")[:300]})
        row = {"id": f"lin-{uuid.uuid4().hex[:12]}",
               "target_id": str(target_id)[:120],
               "target_kind": kind,
               "contributors": contribs,
               "project_id": str(project_id)[:80],
               "mission_id": str(mission_id)[:80],
               "task_id": str(task_id)[:80],
               "note": str(note)[:300],
               "created_at": time.time()}
        with self._lock:
            self._rows.append(row)
            self._save()
        return dict(row)

    def for_target(self, target_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows
                    if r.get("target_id") == target_id]

    def list(self, *, target_kind: str = "", project_id: str = "",
             mission_id: str = "", limit: int = 200) -> list[dict]:
        with self._lock:
            rows = [r for r in self._rows
                    if (not target_kind or r.get("target_kind") == target_kind)
                    and (not project_id or r.get("project_id") == project_id)
                    and (not mission_id or r.get("mission_id") == mission_id)]
        return [dict(r) for r in rows[-limit:]]

    def summary(self) -> dict:
        with self._lock:
            kinds: dict[str, int] = {}
            for r in self._rows:
                for c in r.get("contributors") or []:
                    kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
            return {"records": len(self._rows),
                    "contributor_kinds": kinds}
