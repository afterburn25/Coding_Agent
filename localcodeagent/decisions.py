"""Decision journal — significant autonomous choices stay explainable.

Every consequential decision records the problem, the alternatives that
were considered, the evidence behind the choice, what was *expected* to
happen, and — once known — what actually happened plus lessons learned.
This is the feed into procedural learning (Cerebellum) and the answer to
"why did Nexus do that?".
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

_HISTORY_BOUND = 500


class DecisionJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "decisions": []}
        self.data.setdefault("decisions", [])

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False,
                       default=str))

    # ------------------------------------------------------------------
    def record(self, problem: str, *, alternatives: list,
               evidence: list | None = None, decision: str = "",
               expected_outcome: str = "", actor: str = "nexus",
               context: dict | None = None) -> dict:
        now = time.time()
        row = {
            "id": f"dec-{uuid.uuid4().hex[:10]}",
            "problem": str(problem)[:500],
            "alternatives": [str(a)[:300] for a in alternatives][:12],
            "evidence": [str(e)[:300] for e in (evidence or [])][:20],
            "decision": str(decision)[:400],
            "expected_outcome": str(expected_outcome)[:400],
            "actual_outcome": "",
            "reviewer_result": "",
            "lessons": "",
            "actor": str(actor)[:60],
            "context": dict(context) if isinstance(context, dict) else {},
            "status": "open",           # open → closed (outcome recorded)
            "created_at": now, "updated_at": now,
        }
        with self._lock:
            self.data["decisions"].append(row)
            self.data["decisions"] = self.data["decisions"][
                -_HISTORY_BOUND:]
            self._save()
        return dict(row)

    def outcome(self, did: str, actual: str, *,
                reviewer_result: str = "",
                lessons: str = "") -> dict | None:
        with self._lock:
            for d in self.data["decisions"]:
                if d.get("id") == did:
                    d["actual_outcome"] = str(actual)[:500]
                    d["reviewer_result"] = str(reviewer_result)[:300]
                    d["lessons"] = str(lessons)[:500]
                    d["status"] = "closed"
                    d["updated_at"] = time.time()
                    self._save()
                    return dict(d)
        return None

    def get(self, did: str) -> dict | None:
        for d in self.data["decisions"]:
            if d.get("id") == did:
                return dict(d)
        return None

    def list(self, *, status: str = "", actor: str = "",
             limit: int = 50) -> list[dict]:
        rows = [d for d in self.data["decisions"]
                if (not status or d.get("status") == status)
                and (not actor or d.get("actor") == actor)]
        return [dict(d) for d in rows[-limit:]]

    def summary(self) -> dict:
        rows = self.data["decisions"]
        return {"total": len(rows),
                "open": sum(1 for d in rows if d.get("status") == "open"),
                "closed": sum(1 for d in rows
                              if d.get("status") == "closed")}
