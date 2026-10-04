"""Durable autonomy state: bounded per-concern JSON stores under
data/autonomy/ (or a caller-supplied root).

Follows the existing persistence convention used by TaskStore/WorkQueue/
JobManager: atomic_write_text for whole-file writes, append-only JSONL for
audit streams, and quarantine-on-corrupt so damaged state can never wedge
startup. Schema version lives in meta.json; _migrate() upgrades older
payloads in place.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text

SCHEMA_VERSION = 1


class JsonStore:
    """One bounded JSON document with atomic writes + corrupt quarantine."""

    def __init__(self, path: Path, *, default: Any, limit: int | None = None,
                 key: str | None = None, preserve=None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._default = default
        self._limit = limit          # max rows kept for list payloads
        self._key = key              # payload key wrapping the list
        self._preserve = preserve    # row predicate retained past the tail
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> Any:
        if not self.path.is_file():
            return self._fresh()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._migrate(raw)
        except (OSError, ValueError, TypeError):
            try:
                shutil.copy2(self.path, self.path.with_name(
                    self.path.name + f".corrupt-{int(time.time())}"))
            except OSError:
                pass
            return self._fresh()

    def _fresh(self) -> Any:
        if self._key is not None:
            return {"version": SCHEMA_VERSION, self._key: []}
        return json.loads(json.dumps(self._default))

    def _migrate(self, raw: Any) -> Any:
        if isinstance(raw, dict) and raw.get("version") != SCHEMA_VERSION:
            raw["version"] = SCHEMA_VERSION
        return raw

    def save(self) -> None:
        with self._lock:
            if self._key is not None and self._limit:
                rows = self.data.get(self._key, [])
                if len(rows) > self._limit:
                    tail_ids = {id(row) for row in rows[-self._limit:]}
                    self.data[self._key] = [
                        row for row in rows
                        if id(row) in tail_ids or self._kept(row)]
            atomic_write_text(
                self.path,
                json.dumps(self.data, indent=2, ensure_ascii=False, default=str))

    def _kept(self, row: dict) -> bool:
        if self._preserve is None:
            return False
        try:
            return bool(self._preserve(row))
        except Exception:
            return False

    def rows(self) -> list[dict]:
        with self._lock:
            if self._key is None:
                return []
            return list(self.data.get(self._key) or [])


class JsonlLog:
    """Append-only JSONL audit stream, capped by rewriting on overflow."""

    def __init__(self, path: Path, *, max_bytes: int = 4 * 1024 * 1024,
                 keep_tail: int = 2 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._max = max_bytes
        self._keep = keep_tail
        self._lock = threading.RLock()
        self._ticks = 0

    def append(self, row: dict[str, Any]) -> None:
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                self._ticks += 1
                if self._ticks % 100 == 0 and self.path.stat().st_size > self._max:
                    raw = self.path.read_bytes()[-self._keep:]
                    nl = raw.find(b"\n")
                    self.path.write_bytes(raw[nl + 1:] if nl != -1 else b"")
            except OSError:
                pass

    def tail(self, n: int = 100) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_bytes().splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


class AutonomyStore:
    """Root handle for all persisted autonomy state."""

    FILES = (
        "missions", "standing_goals", "goals", "triggers", "schedules",
        "grants", "notifications", "approvals", "repairs", "findings",
    )

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Completed history is bounded, but any user-actionable or live row
        # survives retention — losing one would leave durable work unowned.
        self.missions = JsonStore(
            self.root / "missions.json", default=None, key="missions",
            limit=200,
            preserve=lambda row: str(row.get("status")) not in
            {"completed", "completed_with_warnings", "failed",
             "cancelled", "archived"})
        self.standing_goals = JsonStore(self.root / "standing_goals.json",
                                        default=None, key="goals", limit=100)
        # Durable evaluated goals (GoalManager) — distinct from
        # standing_goals, which are schedule/trigger-bound recurring runs.
        self.goals = JsonStore(self.root / "goals.json",
                               default=None, key="goals", limit=100)
        self.triggers = JsonStore(self.root / "triggers.json",
                                  default=None, key="triggers", limit=200)
        self.schedules = JsonStore(self.root / "schedules.json",
                                   default=None, key="schedules", limit=200)
        self.grants = JsonStore(self.root / "grants.json",
                                default=None, key="grants", limit=200)
        self.notifications = JsonStore(self.root / "notifications.json",
                                       default=None, key="notifications", limit=300)
        self.approvals = JsonStore(
            self.root / "approvals.json", default=None, key="approvals",
            limit=300,
            preserve=lambda row: str(row.get("state")) == "pending")
        # Durable self-repair incidents (SelfRepairCoordinator) —
        # persisted so a crash mid-repair resumes instead of repeating.
        self.repairs = JsonStore(
            self.root / "repairs.json", default=None, key="repairs",
            limit=200,
            preserve=lambda row: str(row.get("state")) not in
            {"resolved", "rolled_back", "needs_human", "abandoned"})
        # Detector findings — evidence-based problems/opportunities the
        # SignalScanner emits (deduped by signature, cooldown-bounded).
        self.findings = JsonStore(self.root / "findings.json",
                                  default=None, key="findings", limit=200)
        # Procedure memory — terminal outcomes of source-keyed missions
        # (detector investigations, goal repairs, schedules) so recurring
        # signatures recall what worked and stop looping on what didn't.
        self.procedures = JsonStore(self.root / "procedures.json",
                                    default=None, key="procedures",
                                    limit=300)
        self.control = JsonStore(self.root / "control.json",
                                 default={"version": SCHEMA_VERSION,
                                          "paused": False,
                                          "stop": False,
                                          "resource_mode": "balanced"})
        self.receipts = JsonlLog(self.root / "receipts.jsonl")
        self.audit = JsonlLog(self.root / "audit.jsonl")
        self.lessons = JsonlLog(self.root / "lessons.jsonl")

    def health(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "schema_version": SCHEMA_VERSION,
            "missions": len(self.missions.rows()),
            "standing_goals": len(self.standing_goals.rows()),
            "goals": len(self.goals.rows()),
            "triggers": len(self.triggers.rows()),
            "schedules": len(self.schedules.rows()),
            "notifications": len(self.notifications.rows()),
        }
