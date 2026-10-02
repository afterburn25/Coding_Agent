"""Nexus Digital Twin — measured resource model of the host machine.

Tracks real hardware state (via runtime.hardware) and historical
model-loading measurements, then predicts fit, VRAM/RAM use, first-token
latency, and tokens/sec. Routing uses measured reality, not guesses.

State: ``data/twin.json`` (bounded). Measurements are keyed by a hardware
fingerprint — a materially changed machine invalidates old numbers.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

MAX_SAMPLES = 400
MAX_MEASURES = 200


def _hw_fingerprint(hw: dict) -> str:
    gpus = ",".join(sorted(
        f"{g.get('name','?')}:{int(g.get('total_vram_mb',0))}" 
        for g in hw.get("gpus", [])))
    raw = f"{gpus}|{hw.get('ram_total_gb',0):.1f}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _default() -> dict:
    return {"version": 1, "fingerprint": "", "samples": [],
            "model_measures": []}


class DigitalTwin:
    def __init__(self, state_path: Path,
                 detect: Any = None) -> None:
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._detect = detect  # injectable for tests
        self._lock = threading.RLock()
        self.data = self._load()
        self._validate_fingerprint()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "samples" in raw:
                raw["samples"] = raw["samples"][-MAX_SAMPLES:]
                raw["model_measures"] = raw.get("model_measures",
                                                [])[-MAX_MEASURES:]
                return raw
        except (OSError, ValueError):
            pass
        return _default()

    def _save(self) -> None:
        self.data["samples"] = self.data["samples"][-MAX_SAMPLES:]
        self.data["model_measures"] = self.data["model_measures"][-MAX_MEASURES:]
        atomic_write_text(self.state_path, json.dumps(
            self.data, indent=2, default=str))

    def hardware(self) -> dict[str, Any]:
        if self._detect is not None:
            return self._detect()
        try:
            from .runtime.hardware import detect_hardware
            return detect_hardware().as_dict()
        except Exception:
            return {"gpus": [], "ram_total_gb": 0.0, "ram_free_gb": 0.0}

    def _validate_fingerprint(self) -> None:
        """Environment materially changed → drop stale measurements."""
        fp = _hw_fingerprint(self.hardware())
        if self.data.get("fingerprint") and self.data["fingerprint"] != fp:
            self.data["model_measures"] = []
            self.data["samples"] = []
        self.data["fingerprint"] = fp

    # -- sampling -------------------------------------------------------

    def sample(self) -> dict[str, Any]:
        hw = self.hardware()
        row = {"ts": time.time(),
               "ram_free_gb": hw.get("ram_free_gb", 0.0),
               "ram_total_gb": hw.get("ram_total_gb", 0.0),
               "vram_free_mb": sum(g.get("free_vram_mb", 0)
                                   for g in hw.get("gpus", [])),
               "vram_total_mb": sum(g.get("total_vram_mb", 0)
                                    for g in hw.get("gpus", [])),
               "gpu_names": [g.get("name", "") for g in hw.get("gpus", [])]}
        with self._lock:
            self.data["samples"].append(row)
            self._save()
        return row

    # -- model measurements ----------------------------------------------

    def record_model_measure(self, *, model_id: str, size_gb: float,
                             quant: str = "", context: int = 0,
                             load_s: float | None = None,
                             ttft_s: float | None = None,
                             tps: float | None = None,
                             ram_used_gb: float | None = None,
                             vram_used_mb: float | None = None,
                             source: str = "observed") -> dict:
        row = {"ts": time.time(), "model_id": str(model_id)[:120],
               "size_gb": float(size_gb), "quant": str(quant)[:40],
               "context": int(context), "load_s": load_s,
               "ttft_s": ttft_s, "tps": tps,
               "ram_used_gb": ram_used_gb, "vram_used_mb": vram_used_mb,
               "source": source}
        with self._lock:
            measures = [m for m in self.data["model_measures"]
                        if not (m["model_id"] == row["model_id"]
                                and m["source"] == source)]
            measures.append(row)
            self.data["model_measures"] = measures
            self._save()
        return row

    # -- prediction -------------------------------------------------------

    def predict_model(self, *, size_gb: float, quant: str = "",
                      context: int = 0,
                      prefer_gpu: bool = True) -> dict[str, Any]:
        """Predict whether a model of size_gb will fit, expected RAM/VRAM,
        ttft, tps — from measured history when available."""
        hw = self.hardware()
        free_ram = float(hw.get("ram_free_gb") or 0)
        free_vram_mb = sum(float(g.get("free_vram_mb") or 0)
                           for g in hw.get("gpus", []))
        free_vram_gb = free_vram_mb / 1024.0

        # Nearest measured model by size for calibration.
        measures = self.data.get("model_measures") or []
        near = None
        if measures:
            near = min(measures,
                       key=lambda m: abs(m["size_gb"] - size_gb)
                       + (0 if (not quant or m.get("quant") == quant) else 0.2))

        ram_need = size_gb * 1.15            # weights + runtime overhead
        vram_need_gb = size_gb * 1.10 if prefer_gpu else 0.0
        if near and near.get("ram_used_gb") and near.get("size_gb"):
            scale = size_gb / near["size_gb"]
            ram_need = near["ram_used_gb"] * scale
            if near.get("vram_used_mb"):
                vram_need_gb = near["vram_used_mb"] / 1024.0 * scale

        fits_ram = ram_need <= free_ram * 0.92
        fits_vram = vram_need_gb <= free_vram_gb * 0.95
        fits = fits_ram and (fits_vram or not prefer_gpu)

        est = {"fits": fits,
               "fits_ram": fits_ram,
               "fits_vram": fits_vram,
               "estimated_ram_gb": round(ram_need, 2),
               "estimated_vram_gb": round(vram_need_gb, 2),
               "free_ram_gb": round(free_ram, 2),
               "free_vram_gb": round(free_vram_gb, 2),
               "basis": "measured" if near else "heuristic"}
        if near:
            scale = size_gb / max(0.1, near["size_gb"])
            if near.get("ttft_s"):
                est["expected_ttft_s"] = round(near["ttft_s"] * scale, 2)
            if near.get("tps"):
                est["expected_tps"] = round(near["tps"] / max(0.4, scale), 1)
            if near.get("load_s"):
                est["expected_load_s"] = round(near["load_s"] * scale, 1)
        else:
            est["expected_load_s"] = round(4.0 + size_gb * 0.8, 1)
        return est

    def must_unload_first(self, *, size_gb: float,
                          resident: list[dict] | None = None,
                          prefer_gpu: bool = True) -> dict[str, Any]:
        """Predict whether a resident model must be evicted first."""
        pred = self.predict_model(size_gb=size_gb, quant="",
                                  prefer_gpu=prefer_gpu)
        if pred["fits"]:
            return {"needed": False}
        resident = resident or []
        reclaimable = sum(float(r.get("vram_mb", 0)) / 1024.0
                          + float(r.get("ram_gb", 0))
                          for r in resident)
        return {"needed": True, "reclaimable_gb": round(reclaimable, 2)}

    def status(self) -> dict[str, Any]:
        hw = self.hardware()
        return {"fingerprint": self.data.get("fingerprint", ""),
                "samples": len(self.data.get("samples") or []),
                "model_measures": len(self.data.get("model_measures") or []),
                "hardware": hw}
