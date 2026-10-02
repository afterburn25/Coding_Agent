"""Unified system health service (Part R).

Subsystems register a probe (returns healthy/degraded/crashed...) and an
optional bounded recover callback. ``HealthService.tick()`` evaluates all
components, transitions state, runs bounded recovery, and persists
history to ``data/health.json`` for diagnostics/UI.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_text

STATES = ("starting", "healthy", "degraded", "overloaded", "hung",
          "crashed", "restarting", "stopped")
BAD_STATES = {"crashed", "hung", "stopped"}


class Component:
    def __init__(self, name: str, probe: Callable[[], str],
                 recover: Callable[[], bool] | None = None,
                 max_recovery_attempts: int = 3,
                 cooldown_s: float = 60.0) -> None:
        self.name = name
        self.probe = probe
        self.recover = recover
        self.max_recovery_attempts = max_recovery_attempts
        self.cooldown_s = cooldown_s
        self.state = "starting"
        self.detail = ""
        self.last_check = 0.0
        self.last_recovery = 0.0
        self.recovery_attempts = 0


class HealthService:
    def __init__(self, state_path: Path | None = None) -> None:
        self.components: dict[str, Component] = {}
        self.state_path = Path(state_path) if state_path else None
        self._lock = threading.RLock()
        self.history: list[dict[str, Any]] = []

    def register(self, name: str, probe: Callable[[], str], *,
                 recover: Callable[[], bool] | None = None,
                 max_recovery_attempts: int = 3,
                 cooldown_s: float = 60.0) -> None:
        with self._lock:
            self.components[name] = Component(
                name, probe, recover, max_recovery_attempts, cooldown_s)

    def _transition(self, comp: Component, new: str, detail: str = "") -> None:
        if new != comp.state:
            self.history.append({"ts": time.time(), "component": comp.name,
                                 "from": comp.state, "to": new,
                                 "detail": detail[:300]})
            self.history = self.history[-500:]
        comp.state = new
        comp.detail = detail
        comp.last_check = time.time()

    def check(self, name: str) -> str:
        comp = self.components.get(name)
        if comp is None:
            return "unknown"
        with self._lock:
            try:
                state = comp.probe()
            except Exception as exc:
                state = "crashed"
                detail = f"probe raised {type(exc).__name__}: {exc}"
            else:
                detail = ""
            if state not in STATES:
                state, detail = "degraded", f"invalid probe result {state!r}"
            self._transition(comp, state, detail)
            if state in BAD_STATES and comp.recover is not None:
                self._try_recover(comp)
            return comp.state

    def _try_recover(self, comp: Component) -> None:
        now = time.time()
        if (comp.recovery_attempts >= comp.max_recovery_attempts
                or now - comp.last_recovery < comp.cooldown_s):
            return
        comp.recovery_attempts += 1
        comp.last_recovery = now
        self._transition(comp, "restarting",
                         f"recovery attempt {comp.recovery_attempts}")
        try:
            ok = bool(comp.recover())
        except Exception:
            ok = False
        if ok:
            try:
                state = comp.probe()
            except Exception:
                state = "degraded"
            self._transition(comp, state if state in STATES else "degraded",
                             "post-recovery probe")
            if comp.state not in BAD_STATES:
                comp.recovery_attempts = 0
        else:
            self._transition(comp, "crashed", "recovery returned failure")

    def tick(self) -> dict[str, str]:
        for name in list(self.components):
            self.check(name)
        self._persist()
        return self.states()

    def states(self) -> dict[str, str]:
        return {n: c.state for n, c in self.components.items()}

    def summary(self) -> dict[str, Any]:
        return {"states": self.states(),
                "overall": self.overall(),
                "history": self.history[-25:]}

    def overall(self) -> str:
        states = list(self.states().values())
        if not states:
            return "stopped"
        if all(s == "healthy" for s in states):
            return "healthy"
        if any(s in BAD_STATES for s in states):
            return "degraded"
        return "degraded" if any(s == "degraded" for s in states) else "starting"

    def _persist(self) -> None:
        if not self.state_path:
            return
        try:
            import json
            atomic_write_text(self.state_path, json.dumps(
                {"ts": time.time(), "states": self.states(),
                 "history": self.history[-50:]}, indent=2))
        except OSError:
            pass
