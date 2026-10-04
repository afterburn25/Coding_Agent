"""Predictive health — trend analysis over recorded metric series.

`TrendAnalyzer` (`data/trends.json`) keeps bounded per-metric samples
and answers "is this getting worse?" with honest hedging: a prediction
is only emitted when there are enough points and the slope exceeds the
residual noise — otherwise the answer is "stable/insufficient data",
never fake precision.
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

MAX_POINTS = 500
MIN_POINTS = 6


class TrendAnalyzer:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "series": {}}
        self.data.setdefault("series", {})

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            self.data, indent=2, ensure_ascii=False, default=str))

    def record(self, metric: str, value: float,
               *, ts: float | None = None) -> None:
        with self._lock:
            pts = self.data["series"].setdefault(str(metric)[:80], [])
            pts.append([float(ts or time.time()), float(value)])
            del pts[:-MAX_POINTS]
            self._save()

    def analyze(self, metric: str, *, limit: float | None = None) -> dict:
        """Least-squares slope + honest trend label + optional ETA."""
        pts = list(self.data["series"].get(metric) or [])
        if len(pts) < MIN_POINTS:
            return {"metric": metric, "trend": "insufficient_data",
                    "points": len(pts),
                    "note": f"need at least {MIN_POINTS} samples"}
        t0 = pts[0][0]
        xs = [p[0] - t0 for p in pts]
        ys = [p[1] for p in pts]
        n = len(xs)
        sx, sy = sum(xs), sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sxx - sx * sx
        if denom == 0:
            return {"metric": metric, "trend": "stable",
                    "points": n, "note": "all samples at same instant"}
        slope = (n * sxy - sx * sy) / denom            # units per second
        intercept = (sy - slope * sx) / n
        resid = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
        noise = statistics.pstdev(resid) if n > 1 else 0.0
        span = xs[-1] - xs[0] or 1.0
        drift = abs(slope) * span                       # total move
        # Prediction only when the drift clearly exceeds the noise.
        if noise and drift < 2 * noise:
            label = "stable"
        elif drift < max(1e-9, 0.02 * (statistics.mean(
                [abs(y) for y in ys]) or 1.0)):
            label = "stable"
        else:
            label = "rising" if slope > 0 else "falling"
        out: dict[str, Any] = {
            "metric": metric, "trend": label, "points": n,
            "slope_per_hour": round(slope * 3600, 6),
            "noise": round(noise, 4),
            "last": ys[-1],
            "window_seconds": round(span, 1)}
        if limit is not None and label != "stable":
            # ETA to the limit with hedged language.
            target_dist = (limit - ys[-1]) if slope > 0 else (ys[-1] - limit)
            if target_dist > 0 and abs(slope) > 0:
                eta_h = target_dist / abs(slope) / 3600
                out["projection"] = (
                    f"{metric} is {label} steadily and may reach "
                    f"{limit} in roughly {eta_h:.1f}h if the current "
                    "trend continues.")
                out["eta_hours"] = round(eta_h, 2)
            else:
                out["projection"] = (
                    f"{metric} is {label} but already beyond the "
                    f"limit ({limit})." if target_dist <= 0 else
                    f"{metric} is {label}; insufficient slope data "
                    "for an ETA.")
        return out

    def metrics(self) -> list[str]:
        return sorted(self.data["series"])

    def report(self, limits: dict[str, float] | None = None) -> dict:
        """All series analyzed; limits produce projections."""
        lim = dict(limits or {})
        return {m: self.analyze(m, limit=lim.get(m))
                for m in self.metrics()}
