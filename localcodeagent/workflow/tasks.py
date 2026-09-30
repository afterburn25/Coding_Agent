from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TaskRecord:
    id: str
    prompt: str
    mode: str
    status: str = "running"
    phase: str = "planning"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    model_id: str = ""
    model_role: str = ""
    steps: int = 0
    files_changed: list[str] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    review: str = ""
    pending_approval: dict[str, Any] | None = None
    summary: str = ""
    error: str = ""
    research: dict[str, Any] = field(default_factory=dict)
    reverted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskStore:
    """Small durable task ledger kept under .agent/tasks.json."""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace.resolve() / ".agent"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.json"
        self._lock = threading.RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._order: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("tasks", []):
                task = TaskRecord(**item)
                self._tasks[task.id] = task
                self._order.append(task.id)
        except (OSError, ValueError, TypeError):
            # Do not prevent the coding agent from starting because old task state is damaged.
            self._tasks = {}
            self._order = []

    def _save(self) -> None:
        payload = {"version": 1, "tasks": [self._tasks[i].as_dict() for i in self._order[-100:]]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def create(self, prompt: str, mode: str) -> TaskRecord:
        with self._lock:
            task = TaskRecord(id=uuid.uuid4().hex[:12], prompt=prompt, mode=mode)
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._save()
            return task

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            if task_id not in self._tasks:
                raise KeyError(task_id)
            return self._tasks[task_id]

    def update(self, task_id: str, **changes: Any) -> TaskRecord:
        with self._lock:
            task = self.get(task_id)
            for key, value in changes.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            task.updated_at = time.time()
            self._save()
            return task

    def add_changed_file(self, task_id: str, path: str) -> TaskRecord:
        with self._lock:
            task = self.get(task_id)
            if path not in task.files_changed:
                task.files_changed.append(path)
            task.updated_at = time.time()
            self._save()
            return task

    def current(self) -> TaskRecord | None:
        with self._lock:
            for task_id in reversed(self._order):
                task = self._tasks[task_id]
                if task.status in {"running", "waiting_approval", "reviewing", "verifying"}:
                    return task
            return self._tasks[self._order[-1]] if self._order else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [self._tasks[i].as_dict() for i in self._order[-max(1, limit):]][::-1]
