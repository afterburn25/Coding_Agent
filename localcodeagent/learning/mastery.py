"""Mastery evaluation + retention (Parts 21–25).

"Study completed" proves nothing. The MasteryEvaluator runs a staged
pipeline — closed-book test → practical → adversarial → independent
verification — and schedules spaced revalidation. Scores and sample
counts persist; retention decays and refresh is scheduled.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from ..fsutil import atomic_write_text

STAGES = ("closed_book", "practical", "adversarial", "verified")
PASS_SCORE = 0.7

# Spaced revalidation schedule (Part 24) — seconds after last mastery.
RETENTION_INTERVALS = (86400, 7 * 86400, 30 * 86400)


class MasteryEvaluator:
    def __init__(self, path: Path, *, competency_map=None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.competency_map = competency_map
        self._lock = threading.RLock()
        self._data: dict = {"version": 1, "evaluations": [], "retention": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if isinstance(raw.get("evaluations"), list):
                    self._data["evaluations"] = raw["evaluations"]
                if isinstance(raw.get("retention"), dict):
                    self._data["retention"] = raw["retention"]
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            self._data, indent=2, ensure_ascii=False))

    # -- evaluations ---------------------------------------------------------

    def record(self, competency: str, stage: str, score: float, *,
               total: int = 0, passed: int = 0, evidence: str = "",
               difficulty: int = 1) -> dict:
        """Record one stage result. `score` in [0,1] from verified grading,
        never from model self-assessment."""
        with self._lock:
            ev = {
                "id": f"mast-{uuid.uuid4().hex[:10]}",
                "competency": competency[:120],
                "stage": stage if stage in STAGES else "closed_book",
                "score": round(max(0.0, min(1.0, float(score))), 3),
                "total": int(total), "passed": int(passed),
                "difficulty": int(difficulty),
                "evidence": evidence[:300],
                "ts": time.time(),
            }
            self._data["evaluations"].append(ev)
            self._data["evaluations"] = self._data["evaluations"][-500:]
            # Retention bookkeeping per competency.
            ret = self._data["retention"].setdefault(competency, {
                "competency": competency, "best_score": 0.0,
                "last_score": 0.0, "last_evaluated": 0.0,
                "interval_index": 0, "next_check": 0.0,
                "checks": [], "initial_mastery": 0.0,
            })
            ret["last_score"] = ev["score"]
            ret["best_score"] = max(ret["best_score"], ev["score"])
            if not ret["initial_mastery"] and stage == "verified":
                ret["initial_mastery"] = ev["score"]
            ret["last_evaluated"] = ev["ts"]
            if stage == "verified" and ev["score"] >= PASS_SCORE:
                idx = min(ret["interval_index"], len(RETENTION_INTERVALS) - 1)
                ret["next_check"] = ev["ts"] + RETENTION_INTERVALS[idx]
            self._save()
            if self.competency_map is not None:
                self.competency_map.record(
                    competency,
                    "success" if ev["score"] >= PASS_SCORE else "failure",
                    quality=ev["score"])
            return dict(ev)

    def stage_results(self, competency: str, stage: str) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._data["evaluations"]
                    if e.get("competency") == competency and e.get("stage") == stage]

    def next_difficulty(self, competency: str, stage: str) -> int:
        """Difficulty adaptation (Part 23): easy pass → harder; repeated
        fail → step back toward prerequisites."""
        rows = self.stage_results(competency, stage)
        if not rows:
            return 1
        last = rows[-1]
        if last["score"] >= 0.9:
            return min(5, last["difficulty"] + 1)
        recent_fail = sum(1 for r in rows[-3:] if r["score"] < PASS_SCORE)
        if recent_fail >= 2:
            return max(1, last["difficulty"] - 1)
        return last["difficulty"]

    def mastery_level(self, competency: str) -> dict:
        """Current mastery picture: per-stage best + whether the full
        pipeline has been passed."""
        with self._lock:
            rows = [e for e in self._data["evaluations"]
                    if e.get("competency") == competency]
        stages: dict[str, dict] = {}
        for e in rows:
            cur = stages.get(e["stage"])
            if cur is None or e["score"] > cur["score"]:
                stages[e["stage"]] = dict(e)
        passed = all(s in stages and stages[s]["score"] >= PASS_SCORE
                     for s in STAGES)
        return {
            "competency": competency,
            "stages": stages,
            "mastered": passed,
            "evaluations": len(rows),
            "retention": dict(self._data["retention"].get(competency) or {}),
        }

    # -- retention ------------------------------------------------------------

    def due_for_retention(self, *, now: float | None = None,
                          limit: int = 20) -> list[dict]:
        ts = now or time.time()
        with self._lock:
            due = [dict(r) for r in self._data["retention"].values()
                   if r.get("next_check") and r["next_check"] <= ts]
        due.sort(key=lambda r: r["next_check"])
        return due[:limit]

    def record_retention_check(self, competency: str, score: float) -> dict:
        """A later closed-book check — may show decay; refresh improves."""
        with self._lock:
            ret = self._data["retention"].setdefault(competency, {
                "competency": competency, "best_score": 0.0,
                "last_score": 0.0, "last_evaluated": 0.0,
                "interval_index": 0, "next_check": 0.0,
                "checks": [], "initial_mastery": 0.0,
            })
            ret["checks"].append({"score": round(float(score), 3),
                                  "ts": time.time()})
            ret["checks"] = ret["checks"][-40:]
            ret["last_score"] = round(float(score), 3)
            ret["last_evaluated"] = time.time()
            # Pass → advance interval; fail → decay + sooner recheck.
            if score >= PASS_SCORE:
                ret["interval_index"] = min(ret["interval_index"] + 1,
                                            len(RETENTION_INTERVALS) - 1)
            else:
                ret["interval_index"] = 0
            ret["next_check"] = time.time() + RETENTION_INTERVALS[ret["interval_index"]]
            self._save()
            return dict(ret)

    def summary(self) -> dict:
        with self._lock:
            rows = list(self._data["evaluations"])
            rets = list(self._data["retention"].values())
        comps = sorted({e["competency"] for e in rows})
        return {"evaluations": len(rows), "competencies": comps,
                "due_for_retention": len(self.due_for_retention()),
                "tracked_retention": len(rets)}
