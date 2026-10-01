"""In-process event bus for live UI streaming (jobs, tools, processes).

Subscribers get a queue; the bus keeps a bounded history so late subscribers
(SSE clients) can replay recent events. Producers never block on consumers —
a full subscriber queue is dropped rather than stalling tool execution.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, *, history: int = 100, subscriber_queue: int = 200) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=max(10, history))
        self._subscribers: set[queue.Queue] = set()
        self._queue_size = max(10, subscriber_queue)
        self._lock = threading.RLock()

    def publish(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {"type": event_type, "ts": time.time(), **dict(payload)}
        with self._lock:
            self._history.append(event)
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(dict(event))
            except queue.Full:
                pass  # slow consumer — events for it collapse to heartbeats
        return event

    def subscribe(self, *, replay: int = 20) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            for event in list(self._history)[-max(0, replay):]:
                try:
                    q.put_nowait(dict(event))
                except queue.Full:
                    break
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


def make_emitter(bus: EventBus, event_type: str):
    """Return a callback that publishes payloads of one event type."""
    def emit(payload: dict[str, Any]) -> None:
        bus.publish(event_type, payload)
    return emit
