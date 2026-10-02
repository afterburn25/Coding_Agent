"""Durable Scheduler — one-time / interval / daily / weekly schedules.

Schedules persist and survive restart. Daily/weekly use wall-clock local
time (computed fresh each fire), so DST transitions re-anchor correctly
instead of drifting by fixed 24h deltas.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

KINDS = {"once", "interval", "daily", "weekly"}


def _next_daily(hour: int, minute: int, *, weekday: int | None = None,
                after: float | None = None) -> float:
    """Next local wall-clock occurrence of hour:minute (optionally only on a
    specific weekday, Mon=0). DST-safe because it anchors on local date."""
    base = time.localtime(after or time.time())
    for day_offset in range(0, 8):
        cand = time.mktime((
            base.tm_year, base.tm_mon, base.tm_mday + day_offset,
            hour, minute, 0, 0, 0, -1))
        cand_lt = time.localtime(cand)
        if weekday is not None and cand_lt.tm_wday != weekday:
            continue
        if cand > (after or time.time()):
            return cand
    return (after or time.time()) + 86400  # unreachable fallback


class Scheduler:
    def __init__(self, store, *,
                 on_fire: Callable[[dict], None] | None = None) -> None:
        self._store = store
        self.on_fire = on_fire
        self._lock = threading.RLock()

    # -- CRUD --------------------------------------------------------------

    def add(self, name: str, kind: str, *, at: float | None = None,
            interval_s: float = 0, hour: int = 3, minute: int = 0,
            weekday: int | None = None, action: dict | None = None,
            enabled: bool = True, created_by: str = "user") -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown schedule kind '{kind}'")
        row = {
            "id": f"sc-{uuid.uuid4().hex[:10]}",
            "name": str(name or kind)[:140],
            "kind": kind,
            "at": float(at) if at else None,
            "interval_s": max(60.0, float(interval_s)) if interval_s else 0,
            "hour": max(0, min(23, int(hour))),
            "minute": max(0, min(59, int(minute))),
            "weekday": weekday if weekday in range(7) else None,
            "action": dict(action or {}),
            "enabled": bool(enabled),
            "created_by": created_by,
            "created_at": time.time(),
            "last_run": None,
            "next_run": None,
        }
        row["next_run"] = self._compute_next(row)
        with self._lock:
            self._store.schedules.data.setdefault("schedules", []).append(row)
            self._store.schedules.save()
        return dict(row)

    def remove(self, schedule_id: str) -> bool:
        with self._lock:
            rows = self._store.schedules.data.setdefault("schedules", [])
            for i, r in enumerate(rows):
                if r.get("id") == schedule_id:
                    rows.pop(i)
                    self._store.schedules.save()
                    return True
        return False

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        with self._lock:
            for r in self._store.schedules.data.setdefault("schedules", []):
                if r.get("id") == schedule_id:
                    r["enabled"] = bool(enabled)
                    r["next_run"] = self._compute_next(r) if enabled else None
                    self._store.schedules.save()
                    return True
        return False

    def list(self) -> list[dict]:
        return [dict(r) for r in self._store.schedules.rows()]

    # -- firing -------------------------------------------------------------

    def _compute_next(self, row: dict, *, after: float | None = None) -> float | None:
        now = after or time.time()
        kind = row.get("kind")
        if kind == "once":
            # Fire on next tick even if 'at' passed while the app was down —
            # a missed one-shot must not be silently dropped across restarts.
            at = row.get("at")
            return float(at) if at else None
        if kind == "interval":
            last = row.get("last_run") or row.get("created_at") or now
            return float(last) + float(row.get("interval_s") or 3600)
        if kind == "daily":
            return _next_daily(int(row.get("hour", 3)), int(row.get("minute", 0)),
                               after=now)
        if kind == "weekly":
            return _next_daily(int(row.get("hour", 3)), int(row.get("minute", 0)),
                               weekday=int(row.get("weekday") or 6), after=now)
        return None

    def tick(self) -> list[dict]:
        """Fire due schedules; recompute next_run. Returns fired rows."""
        fired: list[dict] = []
        now = time.time()
        with self._lock:
            for row in self._store.schedules.data.setdefault("schedules", []):
                if not row.get("enabled"):
                    continue
                nxt = row.get("next_run")
                if nxt is None:
                    row["next_run"] = self._compute_next(row)
                    continue
                if now >= float(nxt):
                    row["last_run"] = now
                    row["next_run"] = (self._compute_next(row, after=now)
                                       if row.get("kind") != "once" else None)
                    if row.get("kind") == "once":
                        row["enabled"] = False
                    fired.append(dict(row))
            if fired:
                self._store.schedules.save()
            else:
                self._store.schedules.save()
        for row in fired:
            if self.on_fire is not None:
                try:
                    self.on_fire(row)
                except Exception:
                    pass
        return fired

    def next_due(self) -> dict | None:
        rows = [r for r in self._store.schedules.rows()
                if r.get("enabled") and r.get("next_run")]
        if not rows:
            return None
        return min(rows, key=lambda r: r["next_run"])
