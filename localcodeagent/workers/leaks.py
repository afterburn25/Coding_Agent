"""Heavyweight-worker memory leak tracking (backlog §17).

For every managed worker (Chatterbox, ComfyUI, InvokeAI, STT, model
servers, trainers) record:

- RAM before / VRAM before   (free at spawn)
- peak usage during the run  (sampled while alive)
- RAM after / VRAM after     (free after stop)
- residual difference        (baseline_free - post_free)

A worker whose residual keeps growing run-over-run is leaking; the
tracker surfaces it as a recycle suspect so the owner can restart the
worker on a bounded cadence instead of silently ballooning.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class WorkerRun:
    worker: str
    started_at: float
    ram_free_before_gb: float = 0.0
    vram_free_before_gb: float = 0.0
    ram_used_peak_gb: float = 0.0
    vram_used_peak_gb: float = 0.0
    finished_at: float = 0.0
    ram_free_after_gb: float = 0.0
    vram_free_after_gb: float = 0.0
    samples: int = 0

    @property
    def residual_ram_gb(self) -> float:
        if not self.finished_at:
            return 0.0
        return round(self.ram_free_before_gb - self.ram_free_after_gb, 3)

    @property
    def residual_vram_gb(self) -> float:
        if not self.finished_at:
            return 0.0
        return round(self.vram_free_before_gb - self.vram_free_after_gb, 3)

    def as_dict(self) -> dict:
        return {
            "worker": self.worker,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ram_free_before_gb": self.ram_free_before_gb,
            "vram_free_before_gb": self.vram_free_before_gb,
            "ram_used_peak_gb": self.ram_used_peak_gb,
            "vram_used_peak_gb": self.vram_used_peak_gb,
            "ram_free_after_gb": self.ram_free_after_gb,
            "vram_free_after_gb": self.vram_free_after_gb,
            "residual_ram_gb": self.residual_ram_gb,
            "residual_vram_gb": self.residual_vram_gb,
            "samples": self.samples,
        }


class WorkerLeakTracker:
    """Bounded per-worker memory ledger.

    ``probe`` is a callable returning ``(free_ram_gb, free_vram_gb)`` —
    injected so the tracker works with any hardware snapshot source.
    """

    def __init__(self, probe: Callable[[], tuple[float, float]], *,
                 max_runs: int = 20,
                 residual_gb_threshold: float = 1.5) -> None:
        self._probe = probe
        self._max_runs = max(5, int(max_runs))
        self._threshold = float(residual_gb_threshold)
        self._lock = threading.RLock()
        self._open: dict[str, WorkerRun] = {}
        self._history: dict[str, list[WorkerRun]] = {}

    def _snapshot(self) -> tuple[float, float]:
        try:
            ram, vram = self._probe()
            return float(ram or 0.0), float(vram or 0.0)
        except Exception:
            return 0.0, 0.0

    def begin(self, worker: str) -> WorkerRun:
        ram_free, vram_free = self._snapshot()
        run = WorkerRun(
            worker=str(worker), started_at=time.time(),
            ram_free_before_gb=ram_free, vram_free_before_gb=vram_free)
        with self._lock:
            self._open[run.worker] = run
        return run

    def sample(self, worker: str) -> None:
        """Track peak usage while the worker is alive — cheap, call freely."""
        with self._lock:
            run = self._open.get(str(worker))
        if run is None:
            return
        ram_free, vram_free = self._snapshot()
        run.ram_used_peak_gb = max(
            run.ram_used_peak_gb,
            round(run.ram_free_before_gb - ram_free, 3))
        run.vram_used_peak_gb = max(
            run.vram_used_peak_gb,
            round(run.vram_free_before_gb - vram_free, 3))
        run.samples += 1

    def end(self, worker: str) -> WorkerRun | None:
        with self._lock:
            run = self._open.pop(str(worker), None)
        if run is None:
            return None
        ram_free, vram_free = self._snapshot()
        run.finished_at = time.time()
        run.ram_free_after_gb = ram_free
        run.vram_free_after_gb = vram_free
        with self._lock:
            hist = self._history.setdefault(run.worker, [])
            hist.append(run)
            del hist[:-self._max_runs]
        return run

    def leak_suspects(self) -> list[dict]:
        """Workers whose residual grows repeatedly — recycle candidates.

        A suspect needs >=3 completed runs and either (a) the last two
        residuals each exceed the threshold, or (b) a strictly rising
        residual trend across the last three runs.
        """
        out: list[dict] = []
        with self._lock:
            snapshot = {k: list(v) for k, v in self._history.items()}
        for worker, runs in snapshot.items():
            if len(runs) < 3:
                continue
            residuals = [r.residual_ram_gb + r.residual_vram_gb
                         for r in runs]
            last3 = residuals[-3:]
            repeated = all(r >= self._threshold for r in residuals[-2:])
            rising = last3[0] < last3[1] < last3[2]
            if repeated or rising:
                out.append({
                    "worker": worker,
                    "runs": len(runs),
                    "residual_gb_last": residuals[-1],
                    "residual_gb_trend": [round(r, 3) for r in last3],
                    "reason": "repeated_residual" if repeated else "rising_residual",
                })
        return out

    def history(self, worker: str) -> list[dict]:
        with self._lock:
            return [r.as_dict() for r in self._history.get(str(worker), [])]

    def summary(self) -> dict:
        with self._lock:
            return {
                "open": sorted(self._open),
                "tracked_workers": sorted(self._history),
                "suspects": self.leak_suspects(),
            }
