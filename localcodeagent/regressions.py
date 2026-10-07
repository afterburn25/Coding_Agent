"""Regression memory + performance baselines.

Two related stores:

`RegressionStore` — *verified behaviors* ("startup worked at commit X",
"voice greeting worked at commit Y"). `record()` marks a behavior
verified at a head; `check()` compares a fresh observation and reports a
regression when a previously-verified behavior now fails. First failure
per behavior opens a regression entry; a later pass clears it.

`BaselineStore` — numeric performance baselines (startup time, TTFT,
TPS, RAM, VRAM, build/test duration). `check()` flags a regression only
when the new value exceeds `max(mean·(1+slack), mean+3σ)` over a minimum
sample count — tiny noise never raises an alarm.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

_BOUND = 400


class RegressionStore:
    """Verified behaviors → regression detection."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "behaviors": {}, "events": []}
        self.data.setdefault("behaviors", {})
        self.data.setdefault("events", [])

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False,
                       default=str))

    def record(self, behavior: str, *, ok: bool, head: str = "",
               detail: str = "", context: dict | None = None) -> dict:
        """Observe a behavior. First failure after a verified pass opens
        a regression event; a pass after a regression clears it."""
        name = str(behavior)[:200]
        now = time.time()
        with self._lock:
            b = self.data["behaviors"].setdefault(name, {
                "behavior": name, "verified_at": 0.0, "verified_head": "",
                "last_ok": None, "regression_open": None,
                "checks": 0, "first_seen": now})
            b["checks"] += 1
            was_verified = bool(b["verified_at"])
            event = None
            if ok and b.get("regression_open"):
                event = {"id": f"reg-{uuid.uuid4().hex[:8]}", "ts": now,
                         "behavior": name, "type": "regression_cleared",
                         "head": str(head)[:40],
                         "detail": str(detail)[:300]}
                b["regression_open"] = None
            elif not ok and was_verified and not b.get("regression_open"):
                event = {"id": f"reg-{uuid.uuid4().hex[:8]}", "ts": now,
                         "behavior": name, "type": "regression_detected",
                         "head": str(head)[:40],
                         "known_good_head": b["verified_head"],
                         "detail": str(detail)[:300]}
                b["regression_open"] = event["id"]
            if ok:
                b["verified_at"] = now
                b["verified_head"] = str(head)[:40]
            b["last_ok"] = bool(ok)
            if event:
                self.data["events"].append(event)
                self.data["events"] = self.data["events"][-_BOUND:]
            self._save()
            return dict(b, event=event)

    def check(self, behavior: str) -> dict:
        b = self.data["behaviors"].get(str(behavior))
        if not b:
            return {"behavior": behavior, "known": False,
                    "regression": False}
        return {"behavior": b["behavior"], "known": True,
                "last_ok": b["last_ok"],
                "verified_head": b["verified_head"],
                "regression": bool(b.get("regression_open"))}

    def open_regressions(self) -> list[dict]:
        return [e for e in self.data["events"]
                if e["type"] == "regression_detected"
                and (self.data["behaviors"].get(e["behavior"]) or {})
                .get("regression_open") == e["id"]]

    def summary(self) -> dict:
        return {"behaviors": len(self.data["behaviors"]),
                "open_regressions": len(self.open_regressions()),
                "events": len(self.data["events"])}


class BaselineStore:
    """Numeric performance baselines with noise-tolerant thresholds."""

    MIN_SAMPLES = 5
    SLACK = 0.25          # 25% over mean is suspicious
    SIGMA = 3.0           # or 3σ — whichever bound is looser

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "metrics": {}}
        self.data.setdefault("metrics", {})

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False,
                       default=str))

    def record(self, metric: str, value: float, *,
               context: dict | None = None) -> dict:
        name = str(metric)[:120]
        v = float(value)
        with self._lock:
            m = self.data["metrics"].setdefault(name, {
                "metric": name, "n": 0, "mean": 0.0, "m2": 0.0,
                "min": None, "max": None, "last": None,
                "contexts": {}, "updated_at": 0.0})
            # Welford online mean/variance — no history list needed.
            m["n"] += 1
            delta = v - m["mean"]
            m["mean"] += delta / m["n"]
            m["m2"] += delta * (v - m["mean"])
            m["min"] = v if m["min"] is None else min(m["min"], v)
            m["max"] = v if m["max"] is None else max(m["max"], v)
            m["last"] = v
            m["updated_at"] = time.time()
            if context:
                ctx = m["contexts"]
                key = json.dumps(context, sort_keys=True)[:120]
                ctx[key] = ctx.get(key, 0) + 1
                if len(ctx) > 20:
                    del ctx[sorted(ctx)[:1][0]]
            self._save()
            return dict(m)

    def baseline(self, metric: str) -> dict | None:
        m = self.data["metrics"].get(str(metric))
        if not m or m["n"] < self.MIN_SAMPLES:
            return None
        var = m["m2"] / max(m["n"] - 1, 1)
        std = math.sqrt(var)
        return {"metric": m["metric"], "n": m["n"],
                "mean": round(m["mean"], 4),
                "std": round(std, 4),
                "threshold": round(max(m["mean"] * (1 + self.SLACK),
                                       m["mean"] + self.SIGMA * std), 4)}

    def check(self, metric: str, value: float, *,
              direction: str = "higher") -> dict:
        """Regression = new value drifts past the bounded threshold in the
        bad direction — 'higher' for latency/memory, 'lower' for
        throughput. Below MIN_SAMPLES the answer is honest
        'insufficient data'."""
        b = self.baseline(metric)
        v = float(value)
        if b is None:
            return {"metric": metric, "value": v,
                    "regression": False, "confidence": "insufficient"}
        if str(direction).lower() == "lower":
            floor = min(b["mean"] * (1 - self.SLACK),
                        b["mean"] - self.SIGMA * math.sqrt(
                            self.data["metrics"][str(metric)]["m2"]
                            / max(self.data["metrics"][str(metric)]["n"] - 1, 1)))
            reg = v < floor
            result = {"floor": round(floor, 4)}
        else:
            reg = v > b["threshold"]
            result = {}
        return {"metric": metric, "value": v, "regression": reg,
                "direction": str(direction).lower(),
                "baseline_mean": b["mean"], "threshold": b["threshold"],
                **result,
                "ratio": round(v / b["mean"], 3) if b["mean"] else 0.0,
                "confidence": "high" if b["n"] >= 20 else "moderate"}

    def summary(self) -> dict:
        return {"metrics": len(self.data["metrics"]),
                "established": sum(
                    1 for m in self.data["metrics"].values()
                    if m["n"] >= self.MIN_SAMPLES)}
