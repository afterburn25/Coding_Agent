"""Lesson extraction and store (Parts 8–9, 32).

After a meaningful task resolves, the extractor distills a STRUCTURED
lesson — problem class, which strategy worked/failed, useful evidence,
waste, and whether a reusable procedure emerged. Raw transcripts and
chain-of-thought are never stored; a lesson carries refs back to the
source records for provenance.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

from . import taxonomy as t

# problem_class detection — coarse buckets, same spirit as the governor's
# domain detection but tuned for learning bookkeeping.
_CLASS_SIGNALS = (
    ("debugging", ("error", "fail", "bug", "crash", "traceback",
                   "exception", "fix", "broken", "doesn't work")),
    ("configuration", ("install", "setup", "configure", "enable",
                       "disable", "setting", "connect", "deploy")),
    ("creation", ("create", "write", "build", "make", "generate",
                  "implement", "add ", "new ")),
    ("research", ("latest", "current", "version", "news", "price",
                  "what is", "explain", "compare", "search")),
    ("explanation", ("why", "how does", "what does", "difference",
                     "mean", "understand")),
)


def classify_problem(text: str, fallback: str = "general") -> str:
    low = (text or "").lower()
    for cls, signals in _CLASS_SIGNALS:
        if any(s in low for s in signals):
            return cls
    return fallback


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


def signature_for(text: str, problem_class: str) -> str:
    """Stable signature for dedupe/cluster: class + first normalized words."""
    words = _norm(text).split()[:8]
    return f"{problem_class}:{' '.join(words)}"


class LessonStore:
    """Append-mostly JSON store; records are never rewritten wholesale."""

    def __init__(self, path: Path, *, max_records: int = 2000,
                 db: Any = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_records = int(max_records)
        self._lock = threading.RLock()
        from ..state_db import DocStore
        self._doc = DocStore(db, self.path, domain="learning")
        self._data: dict = {"version": 1, "records": []}
        self._load()

    def _load(self) -> None:
        raw = self._doc.load_json(None)
        if isinstance(raw, dict) and isinstance(raw.get("records"), list):
            self._data.update(raw)

    def _save(self) -> None:
        self._doc.save_json(self._data)

    def add(self, record: dict) -> dict:
        with self._lock:
            record.setdefault("id", f"les-{uuid.uuid4().hex[:10]}")
            record.setdefault("created", time.time())
            self._data["records"].append(record)
            if len(self._data["records"]) > self.max_records:
                # Prune oldest LOW-VALUE records first; never silently drop
                # unresolved contradictions or trusted procedures.
                recs = self._data["records"]
                keep = [r for r in recs
                        if r.get("promotion") in (t.VERIFIED, t.TRUSTED, t.CONFLICTED)]
                rest = [r for r in recs if r not in keep]
                self._data["records"] = keep + rest[-max(0, self.max_records - len(keep)):]
            self._save()
            return record

    def update(self, rid: str, **fields) -> dict | None:
        with self._lock:
            for rec in self._data["records"]:
                if rec.get("id") == rid:
                    rec.update(fields)
                    rec["updated"] = time.time()
                    self._save()
                    return rec
            return None

    def get(self, rid: str) -> dict | None:
        with self._lock:
            for rec in self._data["records"]:
                if rec.get("id") == rid:
                    return rec
        return None

    def list(self, *, problem_class: str = "", signature: str = "",
             outcome: str = "", limit: int = 200) -> list[dict]:
        with self._lock:
            rows = list(self._data["records"])
        if problem_class:
            rows = [r for r in rows if r.get("problem_class") == problem_class]
        if signature:
            rows = [r for r in rows if r.get("signature") == signature]
        if outcome:
            rows = [r for r in rows if r.get("outcome") == outcome]
        return rows[-limit:]

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._data["records"][-limit:])

    def summary(self) -> dict:
        with self._lock:
            rows = list(self._data["records"])
        by_outcome: dict[str, int] = {}
        by_class: dict[str, int] = {}
        for r in rows:
            by_outcome[r.get("outcome") or "?"] = by_outcome.get(r.get("outcome") or "?", 0) + 1
            by_class[r.get("problem_class") or "?"] = by_class.get(r.get("problem_class") or "?", 0) + 1
        return {"total": len(rows), "by_outcome": by_outcome,
                "by_class": by_class}


class LessonExtractor:
    """Turns a resolved task/experience into a structured lesson."""

    # Strategies inferred from which lanes the turn actually used.
    def _strategies(self, task: dict) -> tuple[list[str], list[str]]:
        used: list[str] = []
        failed: list[str] = []
        intel = task.get("intel") or {}
        research = task.get("research") or {}
        model = task.get("model") or ""
        if research.get("session"):
            used.append("web_research")
        if intel.get("assessment"):
            used.append("governor_plan")
        if model:
            used.append(f"model:{model}")
        status = task.get("status") or ""
        if status in ("error", "failed", "cancelled"):
            failed.extend(used)
            used = []
        return used, failed

    def extract(self, task: dict, *, correction: str = "",
                user_feedback: str = "") -> dict:
        """Build a lesson record from a completed/failed task dict.

        `task` is the TaskRecord as_dict — goal, status, summary, model,
        research, intel. Only structured fields land in the record.
        """
        goal = str(task.get("goal") or task.get("prompt") or "")[:400]
        status = str(task.get("status") or "")
        outcome = ("correction" if correction else
                   "success" if status in ("completed", "completed_with_warnings")
                   else "failure")
        problem_class = classify_problem(goal)
        used, failed = self._strategies(task)
        refs: list[str] = []
        research = task.get("research") or {}
        for src in ((research.get("session") or {}).get("sources") or [])[:5]:
            if isinstance(src, dict) and src.get("url"):
                refs.append(str(src["url"]))
        if task.get("id"):
            refs.append(f"task:{task['id']}")

        lesson = {
            "memory_class": t.EPISODIC,
            "problem_class": problem_class,
            "signature": signature_for(goal, problem_class),
            "goal": goal,
            "outcome": outcome,
            "status": status,
            "successful_strategy": used[-1] if used else "",
            "strategies_used": used,
            "failed_strategies": failed,
            "evidence_refs": refs,
            "model": task.get("model") or "",
            "correction": correction[:300],
            "user_feedback": user_feedback[:300],
            # A regression candidate is suggested when a real failure was
            # verified — the eval subsystem decides whether to capture it.
            "regression_candidate": outcome == "failure" and bool(failed or used),
            "reusable_conditions": problem_class if outcome == "success" else "",
            "promotion": t.CANDIDATE,
            "confidence": 0.5,
        }
        return lesson
