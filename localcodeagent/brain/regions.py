"""BrainRegion — base contract for cognitive regions.

A region owns a name, a mailbox on the Corpus Callosum, and a health
marker. Regions never call each other directly for cross-cutting work —
they publish typed events. A region that raises during ``handle`` is
isolated by the bus (the error is traced, not propagated), and regions may
enter a degraded state that callers can check before depending on them.
"""
from __future__ import annotations

import queue
import time
from typing import Any, Callable

from .events import CognitiveEvent


class BrainRegion:
    name: str = "region"

    def __init__(self, bus) -> None:
        self.bus = bus
        self.state = "healthy"          # healthy | degraded | isolated
        self.last_error = ""
        self._handled = 0
        self._errors = 0
        self.mailbox = bus.attach_region(self.name, self._guarded_handle)

    # -- bus plumbing --------------------------------------------------------
    def publish(self, type_: str, content: dict[str, Any], *,
                destination: str = "", correlation_id: str = "",
                mission_id: str = "", task_id: str = "",
                confidence: float = 1.0,
                priority: int = 50, provenance: str = "") -> CognitiveEvent:
        return self.bus.publish(CognitiveEvent(
            type=type_, source=self.name, destination=destination,
            content=content, correlation_id=correlation_id,
            mission_id=mission_id, task_id=task_id, confidence=confidence,
            priority=priority, provenance=provenance or self.name))

    def respond(self, request: CognitiveEvent, type_: str,
                content: dict[str, Any], **kw) -> CognitiveEvent:
        # reply() already sources the response from this region — the
        # request's destination — so don't pass source twice.
        return self.bus.publish(request.reply(type_, content, **kw))

    def _guarded_handle(self, event: CognitiveEvent) -> None:
        try:
            self.handle(event)
            self._handled += 1
        except Exception as exc:
            self._errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self.state = "degraded"
            raise  # bus catches + traces; keep the failure visible there

    def handle(self, event: CognitiveEvent) -> None:
        """Override to consume addressed events. Default: drop."""

    def poll(self, timeout: float = 0.0) -> CognitiveEvent | None:
        try:
            return self.mailbox.get(timeout=timeout)
        except queue.Empty:
            return None

    # -- status ----------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state,
                "handled": self._handled, "errors": self._errors,
                "last_error": self.last_error}

    def mark_isolated(self, reason: str) -> None:
        """Take the region off the bus (its own services keep running)."""
        self.state = "isolated"
        self.last_error = reason[:200]
        self.bus.detach_region(self.name)
