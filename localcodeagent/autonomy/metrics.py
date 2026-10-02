"""Metric registry — named, measurable signals for goal evaluation.

Every metric is a zero-arg callable supplied by live wiring (server.py) that
returns a number. The registry adds no measurement of its own: a metric that
cannot be measured returns ok=False and the goal evaluator treats it as
"unknown" — telemetry is advisory evidence, never fabricated.
"""
from __future__ import annotations

import time
from typing import Any, Callable


class MetricRegistry:
    def __init__(self) -> None:
        # key -> (callable, description, unit)
        self._providers: dict[str, tuple[Callable[[], Any], str, str]] = {}

    def register(self, key: str, fn: Callable[[], Any], *,
                 description: str = "", unit: str = "") -> None:
        self._providers[str(key)] = (fn, description, unit)

    def keys(self) -> list[str]:
        return sorted(self._providers)

    def available(self) -> list[dict[str, str]]:
        return [{"key": k, "description": d, "unit": u}
                for k, (_, d, u) in sorted(self._providers.items())]

    def measure(self, key: str) -> dict[str, Any]:
        entry = self._providers.get(str(key))
        if entry is None:
            return {"key": key, "ok": False, "value": None,
                    "detail": "no provider registered"}
        fn, _desc, unit = entry
        try:
            value = fn()
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return {"key": key, "ok": False, "value": None, "unit": unit,
                        "detail": f"provider returned non-numeric {value!r}"}
            return {"key": key, "ok": True, "value": float(value),
                    "unit": unit, "detail": f"{float(value):g} {unit}".strip()}
        except Exception as exc:
            return {"key": key, "ok": False, "value": None, "unit": unit,
                    "detail": f"measure failed: {exc}"[:200]}

    def measure_many(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        return {k: self.measure(k) for k in keys}
