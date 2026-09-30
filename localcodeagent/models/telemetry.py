from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..config import ModelProfile


def complexity_band(complexity: int) -> str:
    if complexity <= 1:
        return "low"
    if complexity >= 6:
        return "high"
    return "medium"


class ModelPerformanceTelemetry:
    """Local, content-free outcome history used as a bounded routing signal."""

    def __init__(
        self,
        workspace: Path,
        *,
        enabled: bool = True,
        storage_path: str = ".agent/model_performance.json",
        min_samples: int = 3,
        weight: int = 20,
        max_events: int = 500,
    ) -> None:
        self.workspace = workspace.resolve()
        configured = Path(storage_path).expanduser()
        self.path = configured if configured.is_absolute() else self.workspace / configured
        self.enabled = bool(enabled)
        self.min_samples = max(1, int(min_samples))
        self.weight = max(0, min(100, int(weight)))
        self.max_events = max(20, int(max_events))
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.enabled or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            events = raw.get("events", [])
            if isinstance(events, list):
                self._events = [dict(item) for item in events if isinstance(item, dict)][-self.max_events:]
        except (OSError, ValueError, TypeError):
            self._events = []

    def _save(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "events": self._events[-self.max_events:]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def record(
        self,
        *,
        model_id: str,
        role: str,
        complexity: int,
        status: str,
        verification_passed: bool | None = None,
        review_passed: bool | None = None,
        steps: int = 0,
        elapsed_seconds: float = 0.0,
        research_used: bool = False,
        repair_cycles: int = 0,
    ) -> None:
        if not self.enabled or not model_id:
            return
        event = {
            "timestamp": round(time.time(), 3),
            "model_id": str(model_id),
            "role": str(role),
            "complexity_band": complexity_band(int(complexity)),
            "status": str(status),
            "verification_passed": verification_passed,
            "review_passed": review_passed,
            "steps": max(0, int(steps)),
            "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
            "research_used": bool(research_used),
            "repair_cycles": max(0, int(repair_cycles)),
        }
        with self._lock:
            self._events.append(event)
            self._events = self._events[-self.max_events:]
            try:
                self._save()
            except OSError:
                # Telemetry is advisory; a read-only/busy disk must never block the agent.
                pass

    @staticmethod
    def _centered(values: list[float]) -> float:
        if not values:
            return 0.0
        return (2.0 * (sum(values) / len(values))) - 1.0

    @staticmethod
    def _outcome_value(status: str) -> float:
        if status == "completed":
            return 1.0
        if status == "completed_with_warnings":
            return 0.65
        return 0.0

    def _matching(self, model_id: str, role: str, band: str) -> tuple[list[dict[str, Any]], str]:
        same_band = [
            event for event in self._events
            if event.get("model_id") == model_id
            and event.get("role") == role
            and event.get("complexity_band") == band
        ]
        if len(same_band) >= self.min_samples:
            return same_band, band
        same_role = [
            event for event in self._events
            if event.get("model_id") == model_id and event.get("role") == role
        ]
        if len(same_role) >= self.min_samples:
            return same_role, "all-complexity"
        return [], band

    def score(self, model: ModelProfile, role: str, complexity: int) -> tuple[int, str]:
        if not self.enabled or self.weight <= 0:
            return 0, ""
        with self._lock:
            samples, scope = self._matching(model.id, role, complexity_band(complexity))
            if not samples:
                return 0, ""

            outcomes = [self._outcome_value(str(event.get("status", ""))) for event in samples]
            verification = [
                1.0 if event["verification_passed"] else 0.0
                for event in samples if isinstance(event.get("verification_passed"), bool)
            ]
            reviews = [
                1.0 if event["review_passed"] else 0.0
                for event in samples if isinstance(event.get("review_passed"), bool)
            ]

            weighted = [(0.65, self._centered(outcomes))]
            if verification:
                weighted.append((0.25, self._centered(verification)))
            if reviews:
                weighted.append((0.10, self._centered(reviews)))
            total_weight = sum(part for part, _ in weighted)
            signal = sum(part * value for part, value in weighted) / total_weight
            learned_score = int(round(max(-1.0, min(1.0, signal)) * self.weight))
            clean = sum(1 for event in samples if event.get("status") == "completed")
            reason = (
                f"{model.id}: learned {scope} {role} outcome score {learned_score:+d} "
                f"from {len(samples)} samples ({clean} clean completions)"
            )
            return learned_score, reason

    def summary(self) -> dict[str, Any]:
        with self._lock:
            grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
            for event in self._events:
                key = (
                    str(event.get("model_id", "")),
                    str(event.get("role", "")),
                    str(event.get("complexity_band", "")),
                )
                row = grouped.setdefault(key, {
                    "model_id": key[0],
                    "role": key[1],
                    "complexity_band": key[2],
                    "samples": 0,
                    "clean_completions": 0,
                    "warnings": 0,
                    "failures": 0,
                    "verification_passes": 0,
                    "verification_failures": 0,
                    "review_passes": 0,
                    "review_findings": 0,
                    "research_tasks": 0,
                    "total_steps": 0,
                    "total_elapsed_seconds": 0.0,
                })
                row["samples"] += 1
                status = str(event.get("status", ""))
                if status == "completed":
                    row["clean_completions"] += 1
                elif status == "completed_with_warnings":
                    row["warnings"] += 1
                else:
                    row["failures"] += 1
                if event.get("verification_passed") is True:
                    row["verification_passes"] += 1
                elif event.get("verification_passed") is False:
                    row["verification_failures"] += 1
                if event.get("review_passed") is True:
                    row["review_passes"] += 1
                elif event.get("review_passed") is False:
                    row["review_findings"] += 1
                if event.get("research_used"):
                    row["research_tasks"] += 1
                row["total_steps"] += int(event.get("steps", 0) or 0)
                row["total_elapsed_seconds"] += float(event.get("elapsed_seconds", 0.0) or 0.0)

            rows = []
            for row in grouped.values():
                samples = max(1, int(row["samples"]))
                row = dict(row)
                row["average_steps"] = round(row.pop("total_steps") / samples, 2)
                row["average_elapsed_seconds"] = round(row.pop("total_elapsed_seconds") / samples, 2)
                rows.append(row)
            rows.sort(key=lambda item: (item["model_id"], item["role"], item["complexity_band"]))
            return {
                "enabled": self.enabled,
                "path": str(self.path),
                "event_count": len(self._events),
                "min_samples": self.min_samples,
                "weight": self.weight,
                "max_events": self.max_events,
                "groups": rows,
            }
