"""Cerebellum — execution optimization, prediction, error correction.

Coordinates the Digital Twin, runtime tuner, model telemetry, and tool
performance stats into one optimization surface. Rules:

- optimizations must be measurable, reversible, logged, and bounded
- no live model weight changes — tuning stays a separate explicit workflow
- every optimization is a scored comparison with a rollback path
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .events import CognitiveEvent, EventType
from .regions import BrainRegion
from . import events as ev


class Cerebellum(BrainRegion):
    name = ev.REGION_CEREBELLUM

    def __init__(self, bus, *, twin=None, tool_stats: Callable[[], dict] | None = None,
                 model_telemetry: Callable[[], dict] | None = None,
                 opt_path: Path | None = None) -> None:
        super().__init__(bus)
        self.twin = twin
        self._tool_stats = tool_stats or (lambda: {})
        self._model_telemetry = model_telemetry or (lambda: {})
        self.opt_path = Path(opt_path) if opt_path else None
        self._lock = threading.RLock()
        self._metrics: deque[dict] = deque(maxlen=2000)
        self._optimizations: list[dict] = []
        if self.opt_path and self.opt_path.is_file():
            try:
                self._optimizations = [
                    json.loads(l) for l in
                    self.opt_path.read_text(encoding="utf-8").splitlines()[-500:]
                    if l.strip()]
            except (OSError, ValueError):
                self._optimizations = []

    # -- telemetry ---------------------------------------------------------------------
    def record_metric(self, kind: str, subject: str, value: float,
                      *, unit: str = "", context: dict | None = None) -> None:
        """kind: inference|tool|scan|build|rag|memory ..."""
        entry = {"ts": time.time(), "kind": kind, "subject": subject,
                 "value": float(value), "unit": unit,
                 "context": dict(context or {})}
        with self._lock:
            self._metrics.append(entry)
        if self.twin is not None and kind == "inference":
            try:
                ctx = dict(context or {})
                self.twin.record_model_measure(
                    model_id=subject, size_gb=float(ctx.get("size_gb") or 0),
                    load_s=float(ctx.get("load_s") or 0),
                    tps=float(ctx.get("tps") or value), ttft_s=float(ctx.get("ttft_s") or 0))
            except Exception:
                pass

    def trend(self, kind: str, subject: str, window: int = 10) -> dict[str, Any]:
        """Improvement trend for repeated operations — e.g. repo scan
        42s → 30s → 12s."""
        with self._lock:
            vals = [m["value"] for m in self._metrics
                    if m["kind"] == kind and m["subject"] == subject][-window:]
        if not vals:
            return {"samples": 0}
        return {"samples": len(vals), "first": vals[0], "last": vals[-1],
                "best": min(vals), "improved": vals[-1] < vals[0],
                "delta": round(vals[-1] - vals[0], 3)}

    # -- bounded optimization ------------------------------------------------------------
    def propose_optimization(self, subject: str, change: dict, *,
                             expected_gain: str = "",
                             rollback: dict | None = None) -> dict:
        """Register an optimization candidate. Nothing applies until
        apply_optimization() marks it measured — keeping self-modification
        bounded and reversible."""
        opt = {"id": uuid.uuid4().hex[:12], "ts": time.time(),
               "subject": subject, "change": dict(change),
               "expected_gain": expected_gain,
               "rollback": dict(rollback or {}),
               "state": "proposed"}
        with self._lock:
            self._optimizations.append(opt)
        self._persist_opts()
        self.publish(EventType.LEARNING_EVENT, {
            "kind": "optimization_proposed", "subject": subject,
            "opt_id": opt["id"]})
        return opt

    def apply_optimization(self, opt_id: str, measured_value: float) -> dict | None:
        with self._lock:
            opt = next((o for o in self._optimizations if o["id"] == opt_id), None)
            if opt is None:
                return None
            opt["state"] = "applied"
            opt["measured"] = measured_value
            opt["applied_at"] = time.time()
        self._persist_opts()
        return opt

    def rollback(self, opt_id: str) -> dict | None:
        """Reversible: returns the rollback payload the caller applies."""
        with self._lock:
            opt = next((o for o in self._optimizations if o["id"] == opt_id), None)
            if opt is None:
                return None
            opt["state"] = "rolled_back"
            opt["rolled_back_at"] = time.time()
        self._persist_opts()
        self.publish(EventType.LEARNING_EVENT, {
            "kind": "optimization_rolled_back", "subject": opt["subject"],
            "opt_id": opt_id})
        return opt

    def optimizations(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._optimizations)[-limit:]

    def _persist_opts(self) -> None:
        if not self.opt_path:
            return
        try:
            self.opt_path.parent.mkdir(parents=True, exist_ok=True)
            with self.opt_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(self._optimizations[-1],
                                    ensure_ascii=False) + "\n")
            # Load only ever feeds the last-500 tail — rewrite the file to
            # that window when it doubles, so the log can't grow forever
            # across long unattended sessions.
            if self.opt_path.stat().st_size > 512 * 1024:
                lines = self.opt_path.read_text(encoding="utf-8").splitlines()[-500:]
                tmp = self.opt_path.with_suffix(self.opt_path.suffix + ".tmp")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                tmp.replace(self.opt_path)
        except OSError:
            pass

    # -- aggregate stats ------------------------------------------------------------------
    def performance_summary(self) -> dict[str, Any]:
        try:
            tools = self._tool_stats() or {}
        except Exception:
            tools = {}
        try:
            models = self._model_telemetry() or {}
        except Exception:
            models = {}
        return {"tools": tools, "models": models,
                "metric_samples": len(self._metrics),
                "optimizations": len(self._optimizations)}

    def handle(self, event: CognitiveEvent) -> None:
        if event.type == EventType.EXECUTION_RESULT:
            c = event.content
            if c.get("action"):
                self.record_metric("tool", str(c["action"]),
                                   float(c.get("duration_ms") or 0),
                                   unit="ms",
                                   context={"ok": c.get("ok"), "tool": c.get("tool")})
        elif event.type == EventType.MODEL_RESULT:
            c = event.content
            if c.get("model_id"):
                self.record_metric("inference", str(c["model_id"]),
                                   float(c.get("latency_ms") or 0), unit="ms",
                                   context={"tps": c.get("tps"),
                                            "ttft_s": c.get("ttft_s")})

    def status(self) -> dict[str, Any]:
        base = super().status()
        base["performance"] = self.performance_summary()
        return base
