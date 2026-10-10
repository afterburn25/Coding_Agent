"""Active learning priority + curriculum + study sessions (Parts 13–21).

ActiveLearningPlanner: which competency is worth studying next —
value × weakness × usage × uncertainty ÷ cost. Never studies what Nexus
already scores well on.

CurriculumManager: a bounded progression (foundation → … → mastery) per
competency, plus StudySession records so a session has a defined
objective, source/time budget, and evaluation plan — study ≠ research
(Part 19): research answers a question; study builds durable competency.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

from . import competencies as comp

CURRICULUM_LEVELS = (
    "foundation", "basic_application", "intermediate",
    "advanced", "adversarial", "real_world", "mastery",
)

# Rough per-level stage descriptors used when generating a curriculum.
_STAGE_TEMPLATES = {
    "foundation": "core definitions, terminology, and mental model for {topic}",
    "basic_application": "apply {topic} on a guided, small example",
    "intermediate": "solve unguided {topic} problems with edge cases",
    "advanced": "handle {topic} in realistic, messy contexts",
    "adversarial": "diagnose deliberately broken/subtle {topic} cases",
    "real_world": "use {topic} on a real task with verification",
    "mastery": "closed-book {topic} evaluation + retention check",
}

# Importance hints per domain — frequency of use in a coding workstation.
_DOMAIN_VALUE = {
    "programming": 1.0, "debugging": 1.0, "research": 0.9,
    "tools": 0.8, "system": 0.8, "math": 0.7, "writing": 0.6,
    "general": 0.5,
}


class ActiveLearningPlanner:
    """Ranks competencies by learning value (Part 13)."""

    def __init__(self, competency_map, *, cost_per_attempt_s: float = 60.0) -> None:
        self.map = competency_map
        self.cost_per_attempt_s = float(cost_per_attempt_s)

    def priority(self, row: dict) -> float:
        """value × weakness × usage × uncertainty ÷ cost."""
        status = row.get("status")
        if status in (comp.STATUS_MASTERED, comp.STATUS_UNTESTED, comp.STATUS_STALE) \
                and not (row.get("usage_count") or 0):
            return 0.0
        domain = str(row.get("domain") or row.get("id") or "general").partition(".")[0]
        value = _DOMAIN_VALUE.get(domain, 0.5)
        rate = row.get("success_rate")
        weakness = 1.0 - (rate if rate is not None else 0.5)
        usage = min(1.0, ((row.get("attempts") or 0) + (row.get("usage_count") or 0)) / 20.0)
        # Low evidence → high uncertainty → worth studying, but only when
        # the area is actually used (usage gate above).
        uncertainty = 1.0 - float(row.get("confidence") or 0.0)
        cost = max(0.2, self.cost_per_attempt_s / 120.0)
        return round(value * weakness * max(0.1, usage) * max(0.1, uncertainty) / cost, 4)

    def priorities(self, *, limit: int = 10) -> list[dict]:
        rows = []
        for row in self.map.all():
            p = self.priority(row)
            if p <= 0:
                continue
            r = dict(row)
            r["priority"] = p
            rows.append(r)
        rows.sort(key=lambda r: -r["priority"])
        return rows[:limit]


class CurriculumManager:
    """Generates a bounded staged curriculum for a competency."""

    def build(self, topic: str, *, max_levels: int = 7) -> dict:
        topic = re.sub(r"\s+", " ", str(topic or "")).strip()[:120] or "general study"
        levels = []
        for i, level in enumerate(CURRICULUM_LEVELS[:max(1, min(max_levels, 7))]):
            levels.append({
                "level": level,
                "stage": i + 1,
                "objective": _STAGE_TEMPLATES[level].format(topic=topic),
            })
        return {"topic": topic, "levels": levels, "created": time.time()}


class StudySessionStore:
    def __init__(self, path: Path, *, max_sessions: int = 300,
                 db: Any = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_sessions = int(max_sessions)
        self._lock = threading.RLock()
        from ..state_db import DocStore
        self._doc = DocStore(db, self.path, domain="learning")
        self._data: dict = {"version": 1, "sessions": []}
        self._load()

    def _load(self) -> None:
        raw = self._doc.load_json(None)
        if isinstance(raw, dict) and isinstance(raw.get("sessions"), list):
            self._data["sessions"] = raw["sessions"]

    def _save(self) -> None:
        self._doc.save_json(self._data)

    def open(self, topic: str, *, competency_ids: list[str] | None = None,
             curriculum: dict | None = None, budget: dict | None = None) -> dict:
        with self._lock:
            sess = {
                "id": f"study-{uuid.uuid4().hex[:10]}",
                "topic": topic[:200],
                "competency_ids": list(competency_ids or []),
                "curriculum": curriculum or {},
                "objectives": [],
                "sources": [], "concepts": [], "procedures": [],
                "questions": [], "exercises": [], "scores": [],
                "started_at": time.time(), "ended_at": 0.0,
                "budget": dict(budget or {"sources": 8, "seconds": 600}),
                "status": "active",
            }
            self._data["sessions"].append(sess)
            self._data["sessions"] = self._data["sessions"][-self.max_sessions:]
            self._save()
            return dict(sess)

    def _row(self, sid: str) -> dict | None:
        for s in self._data["sessions"]:
            if s.get("id") == sid:
                return s
        return None

    def update(self, sid: str, **fields) -> dict | None:
        with self._lock:
            s = self._row(sid)
            if not s:
                return None
            for k, v in fields.items():
                if isinstance(v, list) and isinstance(s.get(k), list):
                    s[k].extend(v)
                else:
                    s[k] = v
            self._save()
            return dict(s)

    def get(self, sid: str) -> dict | None:
        with self._lock:
            s = self._row(sid)
            return dict(s) if s else None

    def active(self) -> dict | None:
        with self._lock:
            for s in reversed(self._data["sessions"]):
                if s.get("status") == "active":
                    return dict(s)
        return None

    def close(self, sid: str, *, status: str = "completed") -> dict | None:
        with self._lock:
            s = self._row(sid)
            if not s:
                return None
            s["status"] = status
            s["ended_at"] = time.time()
            self._save()
            return dict(s)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._data["sessions"][-limit:]]

    def summary(self) -> dict:
        with self._lock:
            rows = list(self._data["sessions"])
        out = {"total": len(rows),
               "active": sum(1 for s in rows if s.get("status") == "active"),
               "completed": sum(1 for s in rows if s.get("status") == "completed")}
        act = next((s for s in reversed(rows)
                    if s.get("status") == "active"), None)
        if act:
            out["active_session"] = dict(act)
        return out
