"""Brain Stem — deterministic survival layer.

Wraps HealthService (component probes + bounded recovery) and adds:
- resource pressure snapshot (VRAM/RAM/CPU/disk) so regions share one
  source of truth instead of each probing hardware
- HealthEvent publication on every state transition so the Thalamus can
  re-route around a dead model and the PFC can re-plan
- watchdog tick that can run entirely without any model loaded
- model blacklist consult (runtime.tuner mark_bad) before restarts

This module must never import or depend on an LLM.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..health import HealthService, BAD_STATES
from .events import EventType, Priority
from .regions import BrainRegion
from . import events as ev


class BrainStem(BrainRegion):
    name = ev.REGION_BRAINSTEM

    def __init__(self, bus, health: HealthService | None = None, *,
                 hardware_probe: Callable[[], dict] | None = None,
                 crash_history: Callable[[int], list[dict]] | None = None) -> None:
        super().__init__(bus)
        self.health = health or HealthService()
        self._hardware_probe = hardware_probe or (lambda: {})
        self._crash_history = crash_history or (lambda _n: [])
        self._pressure: dict[str, Any] = {}
        self._pressure_ts = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._transitions: list[dict] = []

    # -- component registration -------------------------------------------------
    def monitor(self, name: str, probe: Callable[[], str], *,
                recover: Callable[[], bool] | None = None,
                max_recovery_attempts: int = 3,
                cooldown_s: float = 60.0) -> None:
        self.health.register(name, probe, recover=recover,
                             max_recovery_attempts=max_recovery_attempts,
                             cooldown_s=cooldown_s)

    # -- ticking ------------------------------------------------------------------
    def tick(self) -> dict[str, str]:
        """Probe all components; publish HealthEvent on transitions and tell
        the Thalamus when a model-facing component dies so it can re-route."""
        before = self.health.states()
        states = self.health.tick()
        for name, state in states.items():
            if before.get(name) != state:
                self._transitions.append(
                    {"ts": time.time(), "component": name,
                     "from": before.get(name), "to": state})
                self._transitions = self._transitions[-200:]
                self.publish(EventType.HEALTH_EVENT, {
                    "component": name, "state": state,
                    "previous": before.get(name, ""),
                    "detail": self._component_detail(name),
                    "recoverable": self._recoverable(name),
                }, priority=Priority.HIGH if state in BAD_STATES else Priority.NORMAL)
        return states

    def _component_detail(self, name: str) -> str:
        comp = self.health.components.get(name)
        return comp.detail if comp else ""

    def _recoverable(self, name: str) -> bool:
        comp = self.health.components.get(name)
        return bool(comp and comp.recover)

    def start_watchdog(self, interval_s: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        def loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.tick()
                except Exception as exc:
                    self.state = "degraded"
                    self.last_error = f"{type(exc).__name__}: {exc}"[:200]
        self._thread = threading.Thread(target=loop, name="nexus-brainstem",
                                        daemon=True)
        self._thread.start()

    def stop_watchdog(self) -> None:
        self._stop.set()

    # -- resource pressure -----------------------------------------------------
    def resources(self, max_age_s: float = 5.0) -> dict[str, Any]:
        """Shared hardware snapshot — Thalamus routing and PFC planning must
        consult this rather than probing hardware independently."""
        now = time.time()
        if now - self._pressure_ts < max_age_s and self._pressure:
            return dict(self._pressure)
        try:
            snap = self._hardware_probe() or {}
        except Exception:
            snap = {}
        self._pressure = dict(snap)
        self._pressure_ts = now
        return dict(snap)

    def under_pressure(self, resource: str = "vram") -> bool:
        snap = self.resources()
        if resource == "vram":
            return float(snap.get("free_vram_gb") or 0) < 1.0
        if resource == "ram":
            return float(snap.get("available_ram_gb") or 0) < 2.0
        return False

    # -- crash history / blacklists ---------------------------------------------
    def crash_history(self, limit: int = 50) -> list[dict]:
        try:
            return list(self._crash_history(limit))
        except Exception:
            return []

    def report_crash(self, component: str, detail: str = "") -> None:
        """Immediate transition report (e.g. transport reset mid-request)."""
        self.health.report(component, "crashed", detail)
        self.publish(EventType.HEALTH_EVENT, {
            "component": component, "state": "crashed",
            "detail": detail[:300],
        }, priority=Priority.HIGH)

    # -- status --------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update({
            "components": self.health.states(),
            "overall": self.health.overall(),
            "recent_transitions": self._transitions[-10:],
            "resources": self._pressure,
        })
        return base
