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
    final_content: str = ""
    error: str = ""
    research: dict[str, Any] = field(default_factory=dict)
    reverted: bool = False
    interrupted_from: str = ""
    recovery_count: int = 0

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
        self._prune_logs()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            normalized = False
            for item in raw.get("tasks", []):
                task = TaskRecord(**item)
                # In-memory model/tool-call state cannot survive a process restart. Mark
                # formerly active tasks as recoverable instead of pretending they are
                # still running. Approval-gated tasks keep their persisted approval
                # payload and can be resumed cold by the orchestrator.
                if task.status in {"running", "verifying", "reviewing"}:
                    task.interrupted_from = task.phase or task.status
                    task.status = "interrupted"
                    task.phase = "interrupted"
                    task.error = task.error or "Chat Nexus stopped before this task completed."
                    task.updated_at = time.time()
                    normalized = True
                self._tasks[task.id] = task
                self._order.append(task.id)
            if normalized:
                self._save()
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

    def _prune_logs(self) -> None:
        """Drop terminal transcripts whose task fell out of the ledger."""
        terminal = self.root / "terminal"
        try:
            keep = set(self._tasks)
            for path in terminal.glob("*.log"):
                if path.stem not in keep:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _log_path(root: Path, task_id: str) -> Path | None:
        # task ids are generated hex, but the endpoint accepts arbitrary
        # strings — refuse anything that could escape the terminal dir.
        if not task_id or not task_id.replace("-", "").replace("_", "").isalnum():
            return None
        return root / "terminal" / f"{task_id}.log"

    def append_log(self, task_id: str, text: str) -> None:
        """Append to the task's terminal transcript under .agent/terminal/.

        Bounded at ~512 KiB with a 256 KiB tail kept; failures never break
        the agent loop.
        """
        path = self._log_path(self.root, task_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
            if path.stat().st_size > 512 * 1024:
                path.write_bytes(path.read_bytes()[-256 * 1024:])
        except OSError:
            pass

    def read_log(self, task_id: str, *, max_bytes: int = 64 * 1024) -> str:
        """Return the tail of a task's terminal transcript."""
        path = self._log_path(self.root, task_id)
        if path is None:
            return ""
        try:
            if not path.exists():
                return ""
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > max_bytes:
                    fh.seek(-max_bytes, 2)
                return fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def current(self) -> TaskRecord | None:
        """Return the newest task record, regardless of status.

        Chat Nexus runs one foreground chat task at a time. Older interrupted tasks
        remain recoverable in recent history, but they must not overshadow a newer
        completed/error task and get misreported as the current request.
        """
        with self._lock:
            return self._tasks[self._order[-1]] if self._order else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [self._tasks[i].as_dict() for i in self._order[-max(1, limit):]][::-1]
