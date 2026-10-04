"""Temporary mission-specific specialists.

Unlike the fixed `SPECIALISTS` registry in `brain/specialists.py`,
`TempSpecialistStore` (`data/temp_specialists.json`) lets the PFC
spawn *task-scoped* roles — "Windows Installer Investigator", "CUDA
Performance Analyst" — each carrying only:

- an explicit task + acceptance criteria
- an allowlist of tool capabilities
- relevant context refs (project/memory slices)
- a bound (ttl or mission end) — after expiry the spec stays as a
  record but is no longer dispatchable.

Scope is enforced at dispatch: `spawn` returns a spec the worker layer
consumes; `capabilities` intersects with the permanent registry so a
temporary specialist can never exceed real tools.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

MAX_SPECS = 200
DEFAULT_TTL = 4 * 3600  # 4h


class TempSpecialistStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "specialists": []}
        self.data.setdefault("specialists", [])

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            self.data, indent=2, ensure_ascii=False, default=str))

    def _find(self, sid: str) -> dict | None:
        for s in self.data["specialists"]:
            if s.get("id") == sid:
                return s
        return None

    def spawn(self, name: str, *, task: str, domain: str = "custom",
              capabilities: list[str] | None = None,
              context_refs: list[str] | None = None,
              acceptance_criteria: list[str] | None = None,
              mission_id: str = "", ttl_seconds: float = DEFAULT_TTL,
              valid_capabilities: set[str] | None = None) -> dict:
        """Create a bounded task-scoped specialist. Capabilities are
        intersected with `valid_capabilities` when given — a temp
        specialist can never exceed real, existing tools."""
        caps = [str(c)[:60] for c in (capabilities or [])][:30]
        if valid_capabilities is not None:
            caps = [c for c in caps if c in valid_capabilities]
        now = time.time()
        row = {"id": f"tsp-{uuid.uuid4().hex[:10]}",
               "name": str(name)[:80],
               "domain": str(domain)[:40] or "custom",
               "task": str(task)[:500],
               "capabilities": caps,
               "context_refs": [str(c)[:200]
                                for c in (context_refs or [])][:30],
               "acceptance_criteria": [str(c)[:200] for c in
                                       (acceptance_criteria or [])][:20],
               "mission_id": str(mission_id)[:80],
               "status": "active",
               "created_at": now,
               "expires_at": now + ttl_seconds if ttl_seconds else 0.0,
               "result": ""}
        with self._lock:
            self.data["specialists"].append(row)
            self.data["specialists"] = \
                self.data["specialists"][-MAX_SPECS:]
            self._save()
        return dict(row)

    def _expired(self, row: dict) -> bool:
        exp = row.get("expires_at") or 0.0
        return bool(exp and exp < time.time())

    def dispatchable(self, sid: str) -> dict | None:
        """The scoped spec if still active+unexpired, else None."""
        row = self._find(sid)
        if row is None or row.get("status") != "active" \
                or self._expired(row):
            return None
        return dict(row)

    def complete(self, sid: str, *, result: str = "",
                 met_criteria: bool | None = None) -> dict | None:
        row = self._find(sid)
        if row is None or row.get("status") != "active":
            return None
        row["status"] = "completed"
        row["result"] = str(result)[:800]
        row["met_criteria"] = met_criteria
        row["ended_at"] = time.time()
        self._save()
        return dict(row)

    def retire(self, sid: str) -> dict | None:
        row = self._find(sid)
        if row is None or row.get("status") != "active":
            return None
        row["status"] = "retired"
        row["ended_at"] = time.time()
        self._save()
        return dict(row)

    def list(self, *, active_only: bool = False,
             mission_id: str = "") -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self.data["specialists"]
                    if (not mission_id or r.get("mission_id") == mission_id)]
        if active_only:
            rows = [r for r in rows if r.get("status") == "active"
                    and not self._expired(r)]
        return rows

    def reap_expired(self) -> int:
        """Mark expired actives as expired. Returns count."""
        n = 0
        with self._lock:
            for r in self.data["specialists"]:
                if r.get("status") == "active" and self._expired(r):
                    r["status"] = "expired"
                    r["ended_at"] = time.time()
                    n += 1
            if n:
                self._save()
        return n
