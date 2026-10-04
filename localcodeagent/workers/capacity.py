"""Live capacity sampling for the Adaptive Worker Manager.

Wraps runtime.hardware detection with a short-lived cache so admission
decisions never spawn nvidia-smi per call, and adds CPU utilization sampling
(psutil when present, GetSystemTimes on Windows, /proc/stat on Linux) plus
disk pressure for the workspace drive.

The manager subtracts reservations and safety reserves from these live
readings to get schedulable capacity — never trust a "free" number alone,
because several admissions could observe the same free VRAM/RAM and
overcommit it.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..runtime.hardware import detect_hardware


# ---------------------------------------------------------------------------
# CPU utilization
# ---------------------------------------------------------------------------

class _CpuSampler:
    """Delta-based total CPU utilization (0..1). First call establishes the
    baseline and returns the system's since-boot average where available."""

    def __init__(self) -> None:
        self._prev: tuple[float, float] | None = None  # (idle, total)
        self._psutil = None
        try:
            import psutil  # optional dependency — not bundled everywhere
            psutil.cpu_percent(interval=None)  # prime the delta counter
            self._psutil = psutil
        except Exception:
            self._psutil = None

    def sample(self) -> float | None:
        if self._psutil is not None:
            try:
                v = float(self._psutil.cpu_percent(interval=None))
                return max(0.0, min(1.0, v / 100.0))
            except Exception:
                pass
        if os.name == "nt":
            return self._sample_windows()
        return self._sample_proc_stat()

    def _sample_windows(self) -> float | None:
        try:
            import ctypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                            ("dwHighDateTime", ctypes.c_ulong)]

            def _ft() -> int:
                f = FILETIME()
                return f.dwLowDateTime | (f.dwHighDateTime << 32)

            idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            i = idle.dwLowDateTime | (idle.dwHighDateTime << 32)
            k = kernel.dwLowDateTime | (kernel.dwHighDateTime << 32)
            u = user.dwLowDateTime | (user.dwHighDateTime << 32)
            return self._delta(i, k + u)
        except Exception:
            return None

    def _sample_proc_stat(self) -> float | None:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as f:
                parts = f.readline().split()
            if parts[0] != "cpu":
                return None
            vals = [float(x) for x in parts[1:8]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)
            return self._delta(idle, sum(vals))
        except Exception:
            return None

    def _delta(self, idle: float, total: float) -> float | None:
        prev = self._prev
        self._prev = (idle, total)
        if prev is None:
            return None
        di, dt = idle - prev[0], total - prev[1]
        if dt <= 0:
            return None
        return max(0.0, min(1.0, 1.0 - di / dt))


# ---------------------------------------------------------------------------
# Capacity snapshot
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CapacitySnapshot:
    """One coherent view of measured machine headroom."""
    ts: float
    cpu_logical: int
    cpu_util: float | None          # 0..1; None when unmeasurable
    ram_total_mb: int
    ram_free_mb: int
    vram_total_mb: int
    vram_free_mb: int
    gpu_util: float | None          # 0..1 primary GPU
    disk_free_gb: float
    hardware_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Reserves:
    """Safety margin — capacity that is never schedulable for workers, so the
    OS and the interactive user lane always keep headroom.

    Values are conservative defaults; the learner may tighten them (never
    below the floors) as measurements accumulate on this hardware."""
    ram_mb: int = 4096              # OS + interactive lane headroom
    vram_mb: int = 1024             # desktop compositor + UI + safety
    cpu_frac: float = 0.25          # fraction of logical cores kept free
    disk_gb: float = 8.0            # workspace drive breathing room
    interactive_ram_mb: int = 2048  # extra RAM kept for user-foreground work
    interactive_cpu_frac: float = 0.15
    # Floors the learner may not cross.
    RAM_FLOOR_MB: int = field(default=1536, repr=False)
    VRAM_FLOOR_MB: int = field(default=512, repr=False)


class ResourceMonitor:
    """Cached sampler. ``SAMPLE_TTL`` bounds nvidia-smi/exec frequency."""

    SAMPLE_TTL = 3.0

    def __init__(self, workspace: Path, *, sample_ttl: float | None = None,
                 reserves: Reserves | None = None,
                 sampler: Any = None) -> None:
        self.workspace = Path(workspace)
        self.sample_ttl = float(sample_ttl if sample_ttl is not None
                                else self.SAMPLE_TTL)
        self.reserves = reserves or Reserves()
        self._sampler = sampler              # injectable for tests
        self._cpu = _CpuSampler()
        self._lock = threading.RLock()
        self._cache: CapacitySnapshot | None = None

    # -- sampling ----------------------------------------------------------

    def snapshot(self, *, fresh: bool = False) -> CapacitySnapshot:
        with self._lock:
            if (not fresh and self._cache is not None
                    and time.time() - self._cache.ts < self.sample_ttl):
                return self._cache
            snap = self._sample()
            self._cache = snap
            return snap

    def _sample(self) -> CapacitySnapshot:
        if self._sampler is not None:
            return self._sampler()
        try:
            hw = detect_hardware()
        except Exception:
            hw = None
        gpu = hw.gpus[0] if hw and hw.gpus else None
        util = None
        if gpu and gpu.utilization_percent is not None:
            util = max(0.0, min(1.0, gpu.utilization_percent / 100.0))
        disk_gb = 0.0
        try:
            disk_gb = round(shutil.disk_usage(self.workspace).free
                            / (1024 ** 3), 2)
        except OSError:
            pass
        hw_id = ""
        if hw:
            try:
                from ..runtime.hardware import hardware_fingerprint
                hw_id = hardware_fingerprint(hw)
            except Exception:
                hw_id = ""
        return CapacitySnapshot(
            ts=time.time(),
            cpu_logical=os.cpu_count() or 4,
            cpu_util=self._cpu.sample(),
            ram_total_mb=int((hw.total_ram_gb if hw else 0) * 1024),
            ram_free_mb=int((hw.available_ram_gb if hw else 0) * 1024),
            vram_total_mb=gpu.total_vram_mb if gpu else 0,
            vram_free_mb=gpu.free_vram_mb if gpu else 0,
            gpu_util=util,
            disk_free_gb=disk_gb,
            hardware_id=hw_id,
        )

    # -- schedulable capacity ----------------------------------------------

    def schedulable(self, *, reserved_cpu: float = 0.0,
                    reserved_ram_mb: int = 0, reserved_vram_mb: int = 0,
                    reserved_gpu: float = 0.0,
                    interactive: bool = False) -> dict[str, float]:
        """Free capacity minus live reservations minus safety reserves.

        ``interactive=True`` additionally holds back the interactive lane
        reserve (used when admitting background work while the user is
        active — user-facing requests pass interactive=False so they see
        the fuller capacity)."""
        snap = self.snapshot()
        r = self.reserves
        cpu_total = float(max(1, snap.cpu_logical))
        busy_cpu = cpu_total * (snap.cpu_util or 0.0)
        cpu_reserve = cpu_total * r.cpu_frac
        ram_reserve = float(r.ram_mb)
        if interactive:
            cpu_reserve += cpu_total * r.interactive_cpu_frac
            ram_reserve += float(r.interactive_ram_mb)
        return {
            "cpu_cores": max(0.0, cpu_total - busy_cpu - cpu_reserve
                             - reserved_cpu),
            "ram_mb": max(0.0, snap.ram_free_mb - ram_reserve
                          - float(reserved_ram_mb)),
            "vram_mb": max(0.0, snap.vram_free_mb - float(r.vram_mb)
                           - float(reserved_vram_mb)),
            "gpu": max(0.0, 1.0 - (snap.gpu_util or 0.0) - 0.10
                       - reserved_gpu),
            "disk_gb": max(0.0, snap.disk_free_gb - float(r.disk_gb)),
            "snapshot": snap,
        }
