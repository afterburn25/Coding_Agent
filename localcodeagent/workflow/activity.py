"""Structured task activity timeline.

Records high-level execution steps (planning, model routing, tool runs,
commands, tests, review, recovery) as durable rows so the UI can render a
Devin-style expandable timeline — for live tasks via the event bus and for
completed/interrupted tasks via persisted history.

Rows are append-only JSONL; nothing here is hidden chain-of-thought — only
operator-visible status summaries.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

OPEN_STATES = {"running", "waiting"}
TERMINAL_STATES = {"completed", "failed", "interrupted", "skipped"}

# Live output chunks repersist/republish a whole row; throttle to keep SSE and
# disk I/O sane while stdout streams.
OUTPUT_FLUSH_SECONDS = 0.4

CATEGORIES = {
    "planning", "thinking", "routing", "model", "vram", "memory", "brain",
    "investigating", "file", "search", "research", "fetch", "tool", "command",
    "editing", "diff", "building", "testing", "review", "download", "install",
    "service", "approval", "model_wait", "recovery", "retry", "image",
    "artifact", "git", "github", "complete", "error",
}

OUTPUT_TAIL_LIMIT = 20000


class ActivityStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.on_row: Callable[[dict[str, Any]], None] | None = None
        self._by_task: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._output_flush_at: dict[str, float] = {}
        self._load()

    # ------------------------------------------------------------------
    # persistence

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                task_id = str(row.get("task_id") or "")
                act_id = str(row.get("id") or "")
                if not task_id or not act_id:
                    continue
                rows = self._by_task.setdefault(task_id, [])
                existing = next((r for r in rows if r["id"] == act_id), None)
                if existing is None:
                    rows.append(row)
                else:
                    existing.update(row)
            # Anything still open at load means the process died mid-step.
            for rows in self._by_task.values():
                for row in rows:
                    if row.get("state") in OPEN_STATES:
                        row["state"] = "interrupted"
                        row["ended_at"] = row.get("ended_at") or time.time()
        except OSError:
            pass

    def _persist(self, row: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _publish(self, row: dict[str, Any]) -> None:
        if self.on_row is None:
            return
        try:
            self.on_row(dict(row))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # public API

    def open(
        self,
        task_id: str,
        category: str,
        title: str,
        summary: str = "",
        *,
        details: dict[str, Any] | None = None,
        parent: str | None = None,
        activity_id: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "id": activity_id or uuid.uuid4().hex[:12],
            "task_id": str(task_id),
            "category": category if category in CATEGORIES else "tool",
            "state": "running",
            "title": str(title)[:200],
            "summary": str(summary)[:500],
            "details": dict(details or {}),
            "parent": parent,
            "started_at": time.time(),
            "ended_at": None,
            "elapsed": None,
        }
        self._by_task.setdefault(str(task_id), []).append(row)
        self._persist(row)
        self._publish(row)
        return row

    def update(
        self,
        task_id: str,
        activity_id: str,
        *,
        state: str | None = None,
        summary: str | None = None,
        details: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> dict[str, Any] | None:
        row = self._find(task_id, activity_id)
        if row is None:
            return None
        if title is not None:
            row["title"] = str(title)[:200]
        if summary is not None:
            row["summary"] = str(summary)[:500]
        if details:
            row["details"].update(details)
        if state is not None:
            row["state"] = state
            if state in TERMINAL_STATES:
                row["ended_at"] = time.time()
                row["elapsed"] = round(row["ended_at"] - row["started_at"], 2)
        self._persist(row)
        self._publish(row)
        self._output_flush_at.pop(f"{task_id}:{activity_id}", None)
        return row

    def append_output(
        self,
        task_id: str,
        activity_id: str,
        chunk: str,
        *,
        limit: int = OUTPUT_TAIL_LIMIT,
    ) -> dict[str, Any] | None:
        """Append a bounded rolling stdout/stderr tail to a running row."""
        row = self._find(task_id, activity_id)
        if row is None:
            return None
        with self._lock:
            tail = str(row["details"].get("output_tail") or "") + str(chunk)
            row["details"]["output_tail"] = tail[-limit:]
            row["state"] = "running"
            now = time.monotonic()
            due = now - self._output_flush_at.get(f"{task_id}:{activity_id}", 0.0) >= OUTPUT_FLUSH_SECONDS
            if due:
                self._output_flush_at[f"{task_id}:{activity_id}"] = now
                self._persist(row)
                self._publish(row)
                return dict(row)
            return None

    def close_open(self, task_id: str, state: str = "interrupted") -> None:
        for row in self._by_task.get(str(task_id), []):
            if row.get("state") in OPEN_STATES:
                self.update(task_id, row["id"], state=state)

    def for_task(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._by_task.get(str(task_id), [])]

    def _find(self, task_id: str, activity_id: str) -> dict[str, Any] | None:
        for row in self._by_task.get(str(task_id), []):
            if row["id"] == activity_id:
                return row
        return None
