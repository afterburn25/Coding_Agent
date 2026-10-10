"""Competency map (Parts 10–12).

Evidence-backed estimates of what Nexus is good at — never fake
precision. Scores come from attempts/verified outcomes with explicit
sample counts; a competency with 2 tries reports weak confidence, not a
confident percentage.

Competencies form a hierarchy via dotted ids ("programming",
"programming.python", "programming.python.debugging"); parents aggregate
children on read.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


STATUS_MASTERED = "mastered"
STATUS_STRONG = "strong"
STATUS_DEVELOPING = "developing"
STATUS_WEAK = "weak"
STATUS_UNTESTED = "untested"
STATUS_STALE = "stale"

# Sample-count gating: no status stronger than DEVELOPING below this many
# attempts — "83.427% CUDA expertise from 2 examples" is exactly what the
# spec forbids.
_MIN_ATTEMPTS_STRONG = 8
_MIN_ATTEMPTS_MASTERED = 15
_STALE_AFTER = 90 * 24 * 3600


def _status_for(row: dict, *, now: float | None = None) -> str:
    attempts = int(row.get("attempts") or 0)
    if attempts <= 0:
        return STATUS_UNTESTED
    if (now or time.time()) - float(row.get("last_evaluated") or 0) > _STALE_AFTER:
        return STATUS_STALE
    succ = int(row.get("verified_successes") or 0)
    fails = int(row.get("verified_failures") or 0)
    partial = int(row.get("partial_successes") or 0)
    rate = (succ + 0.5 * partial) / max(1, attempts)
    if rate < 0.4:
        return STATUS_WEAK
    if attempts < _MIN_ATTEMPTS_STRONG:
        return STATUS_DEVELOPING
    if rate >= 0.9 and attempts >= _MIN_ATTEMPTS_MASTERED:
        return STATUS_MASTERED
    if rate >= 0.7:
        return STATUS_STRONG
    return STATUS_DEVELOPING


def _confidence(attempts: int) -> float:
    """Evidence confidence = coverage, not skill. 0..1, saturates ~25."""
    return min(1.0, attempts / 25.0)


def _success_rate(row: dict) -> float:
    attempts = int(row.get("attempts") or 0)
    if attempts <= 0:
        return 0.0
    succ = int(row.get("verified_successes") or 0)
    partial = int(row.get("partial_successes") or 0)
    return (succ + 0.5 * partial) / attempts


def _trend(row: dict) -> str:
    hist = row.get("history") or []
    if len(hist) < 4:
        return "insufficient evidence"
    half = len(hist) // 2
    old = sum(1 for h in hist[:half] if h.get("ok")) / max(1, half)
    new = sum(1 for h in hist[half:] if h.get("ok")) / max(1, len(hist) - half)
    if new - old > 0.10:
        return "better"
    if old - new > 0.10:
        return "regressed"
    return "unchanged"


class CompetencyMap:
    def __init__(self, path: Path, *, history_keep: int = 60,
                 db: Any = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.history_keep = int(history_keep)
        self._lock = threading.RLock()
        from ..state_db import DocStore
        self._doc = DocStore(db, self.path, domain="learning")
        self._data: dict = {"version": 1, "competencies": {}}
        self._load()

    def _load(self) -> None:
        raw = self._doc.load_json(None)
        if isinstance(raw, dict) and isinstance(raw.get("competencies"), dict):
            self._data.update(raw)

    def _save(self) -> None:
        self._doc.save_json(self._data)

    def _row(self, cid: str) -> dict:
        comps = self._data["competencies"]
        if cid not in comps:
            parent = cid.rpartition(".")[0]
            comps[cid] = {
                "id": cid, "name": cid.rpartition(".")[2] or cid,
                "domain": cid.partition(".")[0], "parent_id": parent or None,
                "attempts": 0, "verified_successes": 0,
                "verified_failures": 0, "partial_successes": 0,
                "mean_quality": 0.0, "last_evaluated": 0.0,
                "model_breakdown": {}, "tool_breakdown": {},
                "usage_count": 0, "history": [], "created": time.time(),
            }
        return comps[cid]

    def record(self, competency: str, outcome: str, *,
               quality: float = 0.5, model: str = "", tool: str = "",
               now: float | None = None) -> dict:
        """Record one evaluated attempt.

        outcome: "success" | "failure" | "partial" | "attempt" (attempt
        bumps usage without affecting verified counts — e.g. an
        unverified try still signals frequency for learning priority).
        """
        ts = now or time.time()
        with self._lock:
            row = self._row(competency)
            ok = outcome == "success"
            if outcome in ("success", "failure", "partial"):
                row["attempts"] += 1
                if outcome == "success":
                    row["verified_successes"] += 1
                elif outcome == "failure":
                    row["verified_failures"] += 1
                else:
                    row["partial_successes"] += 1
                n = row["attempts"]
                row["mean_quality"] = round(
                    row["mean_quality"] + (quality - row["mean_quality"]) / n, 4)
                row["history"].append({"ok": ok, "ts": ts, "model": model})
                row["history"] = row["history"][-self.history_keep:]
                row["last_evaluated"] = ts
                mb = row["model_breakdown"].setdefault(
                    model or "unknown", {"attempts": 0, "successes": 0})
                mb["attempts"] += 1
                mb["successes"] += 1 if ok else 0
                if tool:
                    tb = row["tool_breakdown"].setdefault(
                        tool, {"attempts": 0, "successes": 0})
                    tb["attempts"] += 1
                    tb["successes"] += 1 if ok else 0
            else:
                row["usage_count"] += 1
            # Bubble activity up the dotted hierarchy as descendant
            # evidence — parents aggregate, they don't accrue their own
            # usage (which would make them fake learning targets).
            parent = row.get("parent_id")
            while parent:
                prow = self._row(parent)
                prow["descendant_attempts"] = prow.get("descendant_attempts", 0) + 1
                parent = prow.get("parent_id")
            self._save()
            return self.present(competency)

    def present(self, cid: str) -> dict:
        """Computed view — adds status/confidence/trend, no fake precision."""
        with self._lock:
            raw = dict(self._data["competencies"].get(cid) or {})
        if not raw:
            return {"id": cid, "status": STATUS_UNTESTED, "attempts": 0,
                    "success_rate": None, "confidence": 0.0,
                    "trend": "insufficient evidence"}
        raw["success_rate"] = round(_success_rate(raw), 3)
        raw["confidence"] = round(_confidence(raw["attempts"]), 3)
        raw["status"] = _status_for(raw)
        raw["trend"] = _trend(raw)
        raw.pop("history", None)
        return raw

    def all(self) -> list[dict]:
        with self._lock:
            ids = list(self._data["competencies"].keys())
        return [self.present(c) for c in sorted(ids)]

    def weaknesses(self, *, min_usage: int = 1, limit: int = 10) -> list[dict]:
        """Weakest evidence-backed competencies that actually get used."""
        rows = [r for r in self.all()
                if r.get("status") in (STATUS_WEAK, STATUS_DEVELOPING, STATUS_STALE)
                and (r.get("attempts") or 0) + (r.get("usage_count") or 0) >= min_usage]
        rows.sort(key=lambda r: (r.get("success_rate") or 0,
                                 -(r.get("usage_count") or 0)))
        return rows[:limit]

    def summary(self) -> dict:
        rows = self.all()
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        return {"total": len(rows), "by_status": by_status}
