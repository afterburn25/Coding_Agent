"""Procedural memory (Parts 30–32, 34–37, 65–68).

HOW Nexus solves recurring problem classes. Procedures are discovered
from repeated verified experience (never one lucky run), carry
success/failure statistics, verification steps, environment scoping,
and versions. A repeatedly failing procedure is marked STALE → retested
or deprecated — not silently kept.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

from . import taxonomy as t

# A procedure candidate must succeed this many times across distinct
# episodes before it earns "verified" — one run proves nothing (Part 31).
_VERIFY_SUCCESS_THRESHOLD = 3
# Consecutive failures that retire a procedure as STALE (Part 65).
_STALE_FAILURES = 2

STATUS_CANDIDATE = "candidate"
STATUS_VERIFIED = "verified"
STATUS_STALE = "stale"
STATUS_DEPRECATED = "deprecated"


def _norm_steps(steps: list[str]) -> list[str]:
    out = []
    for s in steps or []:
        txt = re.sub(r"\s+", " ", str(s)).strip()[:300]
        if txt:
            out.append(txt)
    return out[:40]


class ProceduralMemory:
    def __init__(self, path: Path, *, max_procedures: int = 500,
                 db: Any = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_procedures = int(max_procedures)
        self._lock = threading.RLock()
        from ..state_db import DocStore
        self._doc = DocStore(db, self.path, domain="learning")
        self._data: dict = {"version": 1, "procedures": [], "candidates": []}
        self._load()

    def _load(self) -> None:
        raw = self._doc.load_json(None)
        if isinstance(raw, dict):
            if isinstance(raw.get("procedures"), list):
                self._data["procedures"] = raw["procedures"]
            if isinstance(raw.get("candidates"), list):
                self._data["candidates"] = raw["candidates"]

    def _save(self) -> None:
        self._doc.save_json(self._data)

    # -- procedures -----------------------------------------------------------

    def add(self, name: str, *, problem_signature: str,
            steps: list[str], preconditions: list[str] | None = None,
            tools: list[str] | None = None,
            verification: list[str] | None = None,
            contexts: list[str] | None = None,
            version: int = 1,
            source_lesson_ids: list[str] | None = None) -> dict:
        with self._lock:
            proc = {
                "id": f"proc-{uuid.uuid4().hex[:10]}",
                "name": name, "version": int(version),
                "problem_signature": problem_signature,
                "problem_class": problem_signature.split(":", 1)[0]
                if ":" in problem_signature else "general",
                "preconditions": list(preconditions or []),
                "steps": _norm_steps(steps),
                "tools": list(tools or []),
                "verification": list(verification or []),
                "contexts": list(contexts or []),   # env scoping: windows,
                                                  # nvidia, pkg==1.2, …
                "success_count": 0, "failure_count": 0, "partial_count": 0,
                "mean_duration_s": 0.0, "mean_steps": 0.0,
                "last_used": 0.0, "last_verified": 0.0,
                "confidence": 0.0,
                "status": STATUS_CANDIDATE,
                "source_lesson_ids": list(source_lesson_ids or []),
                "created": time.time(),
            }
            self._data["procedures"].append(proc)
            if len(self._data["procedures"]) > self.max_procedures:
                self._data["procedures"] = self._data["procedures"][-self.max_procedures:]
            self._save()
            return proc

    def _row(self, pid: str) -> dict | None:
        for p in self._data["procedures"]:
            if p.get("id") == pid:
                return p
        return None

    def get(self, pid: str) -> dict | None:
        with self._lock:
            row = self._row(pid)
            return dict(row) if row else None

    def record_outcome(self, pid: str, outcome: str, *,
                       duration_s: float = 0.0, steps_used: int = 0) -> dict | None:
        """outcome: success | partial | failure."""
        with self._lock:
            p = self._row(pid)
            if not p:
                return None
            p["last_used"] = time.time()
            total = p["success_count"] + p["failure_count"] + p["partial_count"]
            if outcome == "success":
                p["success_count"] += 1
                p["consecutive_failures"] = 0
                p["last_verified"] = time.time()
            elif outcome == "partial":
                p["partial_count"] += 1
            else:
                p["failure_count"] += 1
                p["consecutive_failures"] = int(p.get("consecutive_failures") or 0) + 1
            n = max(1, total + 1)
            if duration_s:
                p["mean_duration_s"] = round(
                    p["mean_duration_s"] + (duration_s - p["mean_duration_s"]) / n, 2)
            if steps_used:
                p["mean_steps"] = round(
                    p["mean_steps"] + (steps_used - p["mean_steps"]) / n, 2)
            p["confidence"] = round(min(1.0, n / 10.0), 3)
            # Promotion/retirement driven by outcomes only.
            if (p["status"] == STATUS_CANDIDATE
                    and p["success_count"] >= _VERIFY_SUCCESS_THRESHOLD):
                p["status"] = STATUS_VERIFIED
            elif (p["consecutive_failures"] >= _STALE_FAILURES
                  and p["status"] == STATUS_VERIFIED):
                p["status"] = STATUS_STALE
            self._save()
            return dict(p)

    def adapt(self, pid: str, *, extra_steps: list[str] | None = None,
              note: str = "") -> dict | None:
        """Create a versioned revision when a successful procedure needed
        an unexpected step (Part 35). Old version stays for history."""
        with self._lock:
            old = self._row(pid)
            if not old:
                return None
            new = dict(old)
            new["id"] = f"proc-{uuid.uuid4().hex[:10]}"
            new["version"] = int(old.get("version") or 1) + 1
            new["steps"] = _norm_steps(list(old.get("steps") or []) + list(extra_steps or []))
            new["status"] = STATUS_CANDIDATE
            new["success_count"] = new["failure_count"] = new["partial_count"] = 0
            new["confidence"] = 0.0
            new["derived_from"] = old["id"]
            new["adapt_note"] = note[:300]
            new["created"] = time.time()
            self._data["procedures"].append(new)
            self._save()
            return dict(new)

    def deprecate(self, pid: str, *, reason: str = "") -> dict | None:
        with self._lock:
            p = self._row(pid)
            if not p:
                return None
            p["status"] = STATUS_DEPRECATED
            p["deprecated_reason"] = reason[:300]
            self._save()
            return dict(p)

    def match(self, text: str, *, context: str = "",
              statuses: tuple = (STATUS_VERIFIED, STATUS_CANDIDATE)) -> list[dict]:
        """Context-matched procedure lookup — signature words + env fit."""
        low = (text or "").lower()
        out = []
        with self._lock:
            rows = [dict(p) for p in self._data["procedures"]
                    if p.get("status") in statuses]
        for p in rows:
            sig = str(p.get("problem_signature") or "").lower()
            terms = [w for w in re.split(r"[^a-z0-9]+", sig) if len(w) > 3]
            score = sum(1 for w in terms if w in low)
            if score <= 0:
                continue
            # Environment scope: a procedure tagged "windows" loses score
            # in a non-windows context rather than confidently applying.
            env = str(p.get("contexts") or "")
            if env and context and context.lower() not in env:
                score -= 1
            p["_match_score"] = score
            out.append(p)
        out.sort(key=lambda p: (-p["_match_score"], -p.get("success_count", 0)))
        return out

    def list(self, *, status: str = "", limit: int = 100) -> list[dict]:
        with self._lock:
            rows = [dict(p) for p in self._data["procedures"]]
        if status:
            rows = [p for p in rows if p.get("status") == status]
        rows.sort(key=lambda p: -(p.get("last_used") or p.get("created") or 0))
        return rows[:limit]

    # -- candidates -----------------------------------------------------------

    def propose_candidate(self, problem_signature: str, *,
                          lesson_id: str, steps: list[str] | None = None) -> dict:
        """Track a repeated successful pattern; promote to Procedure when
        it recurs (Part 31)."""
        with self._lock:
            for c in self._data["candidates"]:
                if c.get("problem_signature") == problem_signature:
                    c["count"] = int(c.get("count") or 1) + 1
                    c.setdefault("lesson_ids", []).append(lesson_id)
                    if steps:
                        c["steps"] = _norm_steps(steps)
                    c["updated"] = time.time()
                    promoted = c["count"] >= _VERIFY_SUCCESS_THRESHOLD
                    if promoted and not c.get("promoted"):
                        c["promoted"] = True
                        self.add(
                            f"{problem_signature[:48]}-v1",
                            problem_signature=problem_signature,
                            steps=c.get("steps") or [],
                            source_lesson_ids=c.get("lesson_ids") or [],
                        )
                    self._save()
                    return dict(c)
            cand = {
                "id": f"pcand-{uuid.uuid4().hex[:8]}",
                "problem_signature": problem_signature,
                "count": 1, "lesson_ids": [lesson_id],
                "steps": _norm_steps(steps or []),
                "promoted": False, "created": time.time(),
                "updated": time.time(),
            }
            self._data["candidates"].append(cand)
            self._save()
            return dict(cand)

    def candidates(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._data["candidates"][-limit:]]

    def summary(self) -> dict:
        with self._lock:
            procs = list(self._data["procedures"])
            cands = list(self._data["candidates"])
        by_status: dict[str, int] = {}
        for p in procs:
            by_status[p.get("status") or "?"] = by_status.get(p.get("status") or "?", 0) + 1
        return {"procedures": len(procs), "by_status": by_status,
                "candidates": len(cands)}
