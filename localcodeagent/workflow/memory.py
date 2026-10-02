from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..fsutil import atomic_write_text
from typing import Any


class ProjectMemory:
    """Durable, local project notes generated from completed agent work."""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace.resolve() / ".agent"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "memory.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "task_history": [], "notes": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data.update(raw)
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(self._data, indent=2, ensure_ascii=False))

    def remember_task(self, *, task_id: str, prompt: str, summary: str, files_changed: list[str], review: str = "") -> None:
        with self._lock:
            self._data.setdefault("task_history", []).append({
                "task_id": task_id,
                "timestamp": time.time(),
                "prompt": prompt[:1200],
                "summary": summary[:4000],
                "files_changed": files_changed[:100],
                "review": review[:2000],
            })
            self._data["task_history"] = self._data["task_history"][-50:]
            self._save()

    def context(self, limit: int = 6) -> str:
        with self._lock:
            items = self._data.get("task_history", [])[-max(1, limit):]
        if not items:
            return "No previous completed tasks are recorded for this workspace."
        lines = ["Recent project work remembered locally:"]
        for item in items:
            changed = ", ".join(item.get("files_changed", [])[:8]) or "no files recorded"
            lines.append(f"- {item.get('summary') or item.get('prompt', '')} [files: {changed}]")
        return "\n".join(lines)
