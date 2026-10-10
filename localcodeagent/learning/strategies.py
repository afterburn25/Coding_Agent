"""Strategy evaluation + failure memory (Parts 32–34).

For each problem class, track which strategies worked, which failed, and
at what cost. The CognitiveScheduler consults this so a repeatedly
failing approach loses priority under matching conditions — failure
memory is as important as success memory.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path



class StrategyEvaluator:
    def __init__(self, path: Path, db: Any = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        from ..state_db import DocStore
        self._doc = DocStore(db, self.path, domain="learning")
        self._data: dict = {"version": 1, "strategies": {}}
        self._load()

    def _load(self) -> None:
        raw = self._doc.load_json(None)
        if isinstance(raw, dict) and isinstance(raw.get("strategies"), dict):
            self._data["strategies"] = raw["strategies"]

    def _save(self) -> None:
        self._doc.save_json(self._data)

    def _key(self, problem_class: str, strategy: str) -> str:
        return f"{problem_class}|{strategy}"

    def record(self, problem_class: str, strategy: str, *,
               ok: bool, duration_s: float = 0.0, steps: int = 0,
               retries: int = 0, corrected: bool = False) -> dict:
        with self._lock:
            key = self._key(problem_class, strategy)
            row = self._data["strategies"].setdefault(key, {
                "problem_class": problem_class, "strategy": strategy,
                "attempts": 0, "successes": 0, "failures": 0,
                "total_duration_s": 0.0, "total_steps": 0,
                "total_retries": 0, "corrections": 0,
                "first_seen": time.time(), "last_seen": 0.0,
            })
            row["attempts"] += 1
            row["successes"] += 1 if ok else 0
            row["failures"] += 0 if ok else 1
            row["total_duration_s"] += float(duration_s or 0)
            row["total_steps"] += int(steps or 0)
            row["total_retries"] += int(retries or 0)
            row["corrections"] += 1 if corrected else 0
            row["last_seen"] = time.time()
            self._save()
            return self.present(problem_class, strategy)

    def present(self, problem_class: str, strategy: str) -> dict:
        with self._lock:
            row = dict(self._data["strategies"].get(
                self._key(problem_class, strategy)) or {})
        if not row:
            return {"problem_class": problem_class, "strategy": strategy,
                    "attempts": 0, "success_rate": None, "confidence": 0.0}
        n = row["attempts"]
        row["success_rate"] = round(row["successes"] / n, 3)
        row["confidence"] = round(min(1.0, n / 15.0), 3)
        row["mean_duration_s"] = round(row["total_duration_s"] / n, 2)
        row["mean_steps"] = round(row["total_steps"] / n, 2)
        row["mean_retries"] = round(row["total_retries"] / n, 2)
        return row

    def for_problem(self, problem_class: str) -> list[dict]:
        with self._lock:
            keys = [k for k in self._data["strategies"]
                    if k.startswith(f"{problem_class}|")]
        rows = [self.present(r["problem_class"], r["strategy"])
                for r in (self._data["strategies"][k] for k in keys)]
        # Prefer proven strategies; cheap ones break ties.
        rows.sort(key=lambda r: (-(r.get("success_rate") or 0),
                                 r.get("mean_steps") or 0))
        return rows

    def recommend(self, problem_class: str, *, min_attempts: int = 2) -> dict | None:
        """Best evidence-backed strategy for the scheduler — None when
        nothing has enough data (scheduler keeps its default order)."""
        rows = [r for r in self.for_problem(problem_class)
                if (r.get("attempts") or 0) >= min_attempts]
        return rows[0] if rows else None

    def avoid(self, problem_class: str, *, min_attempts: int = 2,
              max_success_rate: float = 0.3) -> list[dict]:
        """Strategies the scheduler should deprioritize — proven bad."""
        return [r for r in self.for_problem(problem_class)
                if (r.get("attempts") or 0) >= min_attempts
                and (r.get("success_rate") or 0) <= max_success_rate]

    def summary(self) -> dict:
        with self._lock:
            rows = list(self._data["strategies"].values())
        classes = sorted({r["problem_class"] for r in rows})
        return {"tracked": len(rows), "problem_classes": classes}
