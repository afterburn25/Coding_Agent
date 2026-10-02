from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .fsutil import atomic_write_text
from typing import Any


class WorkQueue:
    """Durable FIFO of pending prompts, persisted at .agent/queue.json.

    Lets a user enqueue a batch of tasks; the watchdog dequeues the next
    prompt whenever no task is active, so unattended operation can work
    through a list instead of a single request.
    """

    MAX_ITEMS = 200

    def __init__(self, workspace: Path) -> None:
        self.root = workspace.resolve() / ".agent"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "queue.json"
        self._lock = threading.RLock()
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [i for i in raw.get("items", []) if i.get("status") == "queued"]
        except (OSError, ValueError, TypeError):
            self._items = []

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps({"version": 1, "items": self._items}, indent=2, ensure_ascii=False))

    def enqueue(self, prompt: str, *, mode: str = "auto") -> dict[str, Any]:
        with self._lock:
            if len(self._items) >= self.MAX_ITEMS:
                raise ValueError(f"Work queue is full ({self.MAX_ITEMS} items); wait for tasks to finish or cancel queued items")
            item = {
                "id": uuid.uuid4().hex[:12],
                "prompt": prompt,
                "mode": mode,
                "status": "queued",
                "enqueued_at": time.time(),
            }
            self._items.append(item)
            self._save()
            return dict(item)

    def peek(self) -> dict[str, Any] | None:
        """Return the oldest queued item without removing it."""
        with self._lock:
            return dict(self._items[0]) if self._items else None

    def pop(self) -> dict[str, Any] | None:
        """Remove and return the oldest queued item, if any."""
        with self._lock:
            if not self._items:
                return None
            item = self._items.pop(0)
            self._save()
            return dict(item)

    def remove(self, item_id: str) -> bool:
        with self._lock:
            for i, item in enumerate(self._items):
                if item.get("id") == item_id:
                    self._items.pop(i)
                    self._save()
                    return True
            return False

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(i) for i in self._items]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
