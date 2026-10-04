"""Benchmark lab — measured runs for installed capabilities.

`BenchmarkLab` times a callable (or accepts manual results), records
latency/throughput/memory/VRAM/success/quality into `BaselineStore` —
so benchmark data feeds the same regression detection as everything
else — and keeps a bounded result log per benchmark name.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_text
from .regressions import BaselineStore

MAX_RESULTS = 50  # per benchmark


class BenchmarkLab:
    def __init__(self, path: Path,
                 baselines: BaselineStore | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.baselines = baselines
        self._lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "results": {}}
        self.data.setdefault("results", {})

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            self.data, indent=2, ensure_ascii=False, default=str))

    def _store_result(self, name: str, row: dict) -> dict:
        with self._lock:
            rows = self.data["results"].setdefault(name, [])
            rows.append(row)
            del rows[:-MAX_RESULTS]
            self._save()
        # Feed the shared regression detector.
        if self.baselines is not None:
            try:
                if row.get("success"):
                    self.baselines.record(
                        f"bench.{name}.latency_ms",
                        float(row["latency_ms"]))
                    if row.get("throughput"):
                        self.baselines.record(
                            f"bench.{name}.throughput",
                            float(row["throughput"]))
            except Exception:
                pass
        return dict(row)

    def run(self, name: str, fn: Callable[[], Any], *,
            iterations: int = 1, detail: str = "") -> dict:
        """Time a callable. latency is the mean across iterations;
        a raised exception is a failed run, not a crash."""
        latencies = []
        ok = True
        err = ""
        for _ in range(max(1, int(iterations))):
            t0 = time.perf_counter()
            try:
                fn()
            except Exception as exc:
                ok = False
                err = str(exc)[:200]
                break
            latencies.append((time.perf_counter() - t0) * 1000)
        row = {"id": f"bench-{uuid.uuid4().hex[:10]}",
               "name": str(name)[:80],
               "success": ok,
               "error": err,
               "latency_ms": round(
                   sum(latencies) / len(latencies), 3)
                   if latencies else 0.0,
               "iterations": len(latencies),
               "detail": str(detail)[:200],
               "at": time.time()}
        return self._store_result(str(name)[:80], row)

    def record_result(self, name: str, *, latency_ms: float,
                      throughput: float = 0.0, memory_mb: float = 0.0,
                      vram_mb: float = 0.0, success: bool = True,
                      quality: float | None = None,
                      detail: str = "") -> dict:
        """Manual result entry (e.g. model TPS measured elsewhere)."""
        row = {"id": f"bench-{uuid.uuid4().hex[:10]}",
               "name": str(name)[:80], "success": bool(success),
               "latency_ms": float(latency_ms),
               "throughput": float(throughput),
               "memory_mb": float(memory_mb), "vram_mb": float(vram_mb),
               "quality": quality, "detail": str(detail)[:200],
               "at": time.time()}
        return self._store_result(str(name)[:80], row)

    def results(self, name: str) -> list[dict]:
        return [dict(r) for r in self.data["results"].get(name, [])]

    def summary(self) -> dict:
        out = {}
        for name, rows in self.data["results"].items():
            ok = [r for r in rows if r.get("success")]
            lat = [r["latency_ms"] for r in ok]
            out[name] = {
                "runs": len(rows), "successes": len(ok),
                "last_latency_ms": rows[-1]["latency_ms"],
                "mean_latency_ms": round(sum(lat) / len(lat), 3)
                if lat else None}
        return {"benchmarks": out}
