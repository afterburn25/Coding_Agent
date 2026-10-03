"""Procedural memory for recurring missions.

Repairs already learn procedures (self_repair/RepairMemory). Missions
recur too — detector investigations, goal-generated repairs, scheduled
runs — all keyed on ``source`` + ``source_id``. Recording each terminal
outcome lets the supervisor:

- hand the next occurrence its history (what was found, how it ended),
  so an investigation doesn't redo identical work, and
- stop re-spawning a signature that keeps failing identically — live
  dedupe can't see a *finished* failure, but memory can.

Records are bounded facts, never narratives: status, duration, task
counts, the objective head. Nothing here decides to widen scope or
permissions.
"""
from __future__ import annotations

import time
from typing import Any

from .missions import TERMINAL_MISSION_STATUSES
from .state import AutonomyStore

_FAILING_STATUSES = {"failed", "cancelled"}
_RECALL_LIMIT = 3


def mission_key(mission: dict) -> str:
    """Stable recurrence key. Only source-keyed missions (detector, goal,
    schedule, trigger origins) recur by design; ad-hoc missions get ""."""
    src = str(mission.get("source") or "")
    sid = str(mission.get("source_id") or "")
    if not src or not sid or src == "user":
        return ""
    return f"{src}:{sid}"[:160]


class ProcedureMemory:
    """Terminal outcomes of recurring missions, durable across restarts."""

    def __init__(self, store: AutonomyStore) -> None:
        self._store = store

    def _rows(self) -> list[dict]:
        return self._store.procedures.data.setdefault("procedures", [])

    def _save(self) -> None:
        self._store.procedures.save()

    def recorded_ids(self) -> set[str]:
        return {str(r.get("mission_id") or "") for r in self._rows()}

    def record_terminal(self, mission: dict) -> dict | None:
        """Persist a terminal mission's outcome. Idempotent on mission id;
        ignores non-terminal or unkeyed missions. Returns the row or None."""
        if str(mission.get("status")) not in TERMINAL_MISSION_STATUSES:
            return None
        key = mission_key(mission)
        mid = str(mission.get("id") or "")
        if not key or not mid:
            return None
        for r in self._rows():
            if r.get("mission_id") == mid:
                return None            # already learned
        from .task_graph import TaskGraph
        try:
            nodes = TaskGraph(mission).nodes or []
        except Exception:
            nodes = []
        done = sum(1 for n in nodes
                   if str(n.get("status")) in
                   {"completed", "verified", "skipped"})
        created = float(mission.get("created_at") or 0)
        finished = float(mission.get("completed_at") or time.time())
        row = {
            "mission_id": mid,
            "key": key,
            "source": str(mission.get("source") or ""),
            "source_id": str(mission.get("source_id") or ""),
            "title": str(mission.get("title") or "")[:160],
            "objective": str(mission.get("objective") or "")[:300],
            "status": str(mission.get("status") or ""),
            "tasks_total": len(nodes),
            "tasks_done": done,
            "duration_s": round(max(0.0, finished - created), 1)
            if created else None,
            "finished_at": finished,
        }
        self._rows().append(row)
        self._save()
        return row

    def recall(self, source: str, source_id: str, *,
               limit: int = _RECALL_LIMIT) -> list[dict]:
        """Recent runs for a signature, newest first."""
        key = f"{source}:{source_id}"[:160]
        rows = [dict(r) for r in self._rows() if r.get("key") == key]
        rows.sort(key=lambda r: -(r.get("finished_at") or 0))
        return rows[:limit]

    def failing(self, source: str, source_id: str, *,
                threshold: int = 2, min_gap_s: float = 6 * 3600) -> bool:
        """The last `threshold` runs all ended failed/cancelled AND the
        newest finished < `min_gap_s` ago — re-spawning now is a loop,
        not recovery. Once the back-off elapses a fresh attempt is
        allowed, so a fixed condition can still be learned from."""
        runs = self.recall(source, source_id, limit=threshold)
        if len(runs) < threshold:
            return False
        if not all(r.get("status") in _FAILING_STATUSES for r in runs):
            return False
        last = float(runs[0].get("finished_at") or 0)
        return (time.time() - last) < min_gap_s

    def summary(self, mission: dict) -> str:
        """One bounded line for the next occurrence's objective."""
        runs = self.recall(str(mission.get("source") or ""),
                           str(mission.get("source_id") or ""))
        if not runs:
            return ""
        last = runs[0]
        ok = sum(1 for r in runs
                 if r.get("status") not in _FAILING_STATUSES)
        dur = last.get("duration_s")
        dur_s = f", took {int(dur)}s" if isinstance(dur, (int, float)) else ""
        return (f"Prior runs: {len(runs)} ({ok} succeeded). "
                f"Last ended {last.get('status')}{dur_s}, "
                f"{last.get('tasks_done')}/{last.get('tasks_total')} "
                "tasks done. Don't repeat identical work — investigate "
                "what changed or why it persists.")[:400]
