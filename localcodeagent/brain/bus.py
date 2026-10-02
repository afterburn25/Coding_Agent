"""Corpus Callosum — the cognitive event bus.

Strongly typed delivery between regions. Properties:

- addressed delivery: ``destination`` routes to that region's mailbox;
  empty destination broadcasts to all subscribers of the event type
- request/response: ``request()`` blocks on a reply with the same
  correlation_id, with timeout
- failure isolation: a region's handler exception is caught, recorded as a
  trace error, and never propagates to the publisher or sibling regions
- tracing: every event is appended to a bounded trace ring so developers
  can see how an input moved through Nexus (structural events only —
  never hidden model reasoning tokens)
- cancellation: a correlation can be cancelled; pending requesters wake
  with None and delivery of further events under it is suppressed
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable

from .events import CognitiveEvent, EventType, Priority


class CorpusCallosum:
    def __init__(self, *, trace_limit: int = 2000, mailbox_size: int = 500) -> None:
        self._lock = threading.RLock()
        self._mailboxes: dict[str, queue.Queue] = {}
        self._type_subs: dict[str, set[Callable[[CognitiveEvent], None]]] = {}
        self._region_handlers: dict[str, Callable[[CognitiveEvent], Any]] = {}
        self._pending: dict[str, tuple[threading.Event, list[CognitiveEvent], str]] = {}
        self._cancelled: set[str] = set()
        self._trace: deque[dict[str, Any]] = deque(maxlen=trace_limit)
        self._mailbox_size = max(50, mailbox_size)

    # -- region wiring --------------------------------------------------------
    def attach_region(self, name: str,
                      handler: Callable[[CognitiveEvent], Any] | None = None
                      ) -> queue.Queue:
        """Give a region an addressed mailbox and optional inline handler.
        The mailbox is for poll-style consumers; the handler runs inline on
        publish (failures isolated)."""
        with self._lock:
            q = self._mailboxes.setdefault(name, queue.Queue(maxsize=self._mailbox_size))
            if handler is not None:
                self._region_handlers[name] = handler
            return q

    def detach_region(self, name: str) -> None:
        with self._lock:
            self._region_handlers.pop(name, None)
            self._mailboxes.pop(name, None)

    def subscribe_type(self, event_type: str,
                       fn: Callable[[CognitiveEvent], None]) -> None:
        with self._lock:
            self._type_subs.setdefault(event_type, set()).add(fn)

    def unsubscribe_type(self, event_type: str,
                         fn: Callable[[CognitiveEvent], None]) -> None:
        with self._lock:
            self._type_subs.get(event_type, set()).discard(fn)

    # -- delivery --------------------------------------------------------------
    def publish(self, event: CognitiveEvent) -> CognitiveEvent:
        if not event.id:
            event.id = uuid.uuid4().hex[:16]
        if not event.ts:
            event.ts = time.time()
        with self._lock:
            cancelled = (event.correlation_id in self._cancelled
                         or (event.trace_id and event.trace_id in self._cancelled))
            self._trace.append(dict(event.as_dict()))
            handlers = ([self._region_handlers[event.destination]]
                        if event.destination and event.destination in self._region_handlers
                        else [])
            type_subs = list(self._type_subs.get(event.type, set()))
            mailbox = self._mailboxes.get(event.destination) if event.destination else None
            # Wake a blocked requester when a reply with this correlation lands.
            waiter = self._pending.get(event.correlation_id)
        if cancelled:
            return event
        if mailbox is not None:
            try:
                mailbox.put_nowait(event)
            except queue.Full:
                pass  # slow region — structural trace still records the event
        if waiter is not None and event.id != waiter[2]:
            # A reply is any event sharing the correlation that isn't the
            # request itself — the request also passes through publish().
            waiter[1].append(event)
            waiter[0].set()
        for fn in type_subs:
            self._deliver_guarded(fn, event)
        for h in handlers:
            self._deliver_guarded(h, event)
        return event

    def _deliver_guarded(self, fn: Callable, event: CognitiveEvent) -> Any:
        try:
            return fn(event)
        except Exception as exc:
            self._record_trace_error(event, exc)
            return None

    def _record_trace_error(self, event: CognitiveEvent, exc: Exception) -> None:
        with self._lock:
            self._trace.append({
                "id": uuid.uuid4().hex[:16], "type": "trace_error",
                "source": "corpus_callosum", "destination": event.destination,
                "correlation_id": event.correlation_id, "ts": time.time(),
                "content": {"for_type": event.type,
                            "error": f"{type(exc).__name__}: {exc}"[:300]},
            })

    # -- request / response ------------------------------------------------------
    def request(self, event: CognitiveEvent, *, timeout: float = 10.0
                ) -> CognitiveEvent | None:
        """Publish and wait for a reply sharing the correlation_id."""
        if not event.correlation_id:
            event.correlation_id = uuid.uuid4().hex[:16]
        done = threading.Event()
        replies: list[CognitiveEvent] = []
        if not event.id:
            event.id = uuid.uuid4().hex[:16]
        with self._lock:
            self._pending[event.correlation_id] = (done, replies, event.id)
        try:
            self.publish(event)
            done.wait(timeout)
            return replies[0] if replies else None
        finally:
            with self._lock:
                self._pending.pop(event.correlation_id, None)

    def respond(self, request: CognitiveEvent, type_: str,
                content: dict[str, Any]) -> CognitiveEvent:
        return self.publish(request.reply(type_, content))

    # -- cancellation -----------------------------------------------------------
    def cancel(self, correlation_id: str) -> None:
        """Suppress further delivery for a correlation and wake waiters."""
        with self._lock:
            self._cancelled.add(correlation_id)
            if len(self._cancelled) > 5000:
                self._cancelled = set(list(self._cancelled)[-2500:])
            waiter = self._pending.pop(correlation_id, None)
        if waiter is not None:
            waiter[0].set()

    # -- observability -------------------------------------------------------------
    def trace(self, *, correlation_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._trace)
        if correlation_id:
            items = [e for e in items if e.get("correlation_id") == correlation_id]
        return items[-limit:]

    def trace_summary(self, correlation_id: str) -> dict[str, Any]:
        """Collapsed hop list: source → type → destination for the dev view."""
        hops = []
        for e in self.trace(correlation_id=correlation_id, limit=500):
            hops.append({
                "ts": e.get("ts"), "source": e.get("source"),
                "type": e.get("type"), "destination": e.get("destination"),
                "confidence": e.get("confidence"),
                "summary": _hop_summary(e),
            })
        return {"correlation_id": correlation_id, "hops": hops}


def _hop_summary(e: dict[str, Any]) -> str:
    c = e.get("content") or {}
    t = e.get("type", "")
    if t == EventType.ATTENTION:
        return f"classified → {c.get('route', c.get('region', ''))}"
    if t == EventType.MEMORY_RESULT:
        return f"{c.get('hits', 0)} memories ({c.get('latency_ms', 0)}ms)"
    if t == EventType.PLAN:
        return f"{c.get('steps', 0)} steps"
    if t == EventType.ACTION_SELECTION:
        return f"selected {c.get('action', '')}"
    if t == EventType.EXECUTION_RESULT:
        return f"{c.get('action', '')} → {'ok' if c.get('ok') else 'failed'} ({c.get('duration_ms', 0)}ms)"
    if t == EventType.HEALTH_EVENT:
        return f"{c.get('component', '')} {c.get('state', '')}"
    if t == EventType.MODEL_RESULT:
        return f"{c.get('model_id', '')} ({c.get('latency_ms', 0)}ms)"
    if t == EventType.CONFLICT_DETECTED:
        return str(c.get("kind", "conflict"))
    return str(c.get("summary", c.get("event", "")))[:120]
