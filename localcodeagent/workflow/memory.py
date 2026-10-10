from __future__ import annotations

import json
import re
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

    def remember_task(self, *, task_id: str, prompt: str, summary: str, files_changed: list[str], review: str = "", mission: bool = False) -> None:
        with self._lock:
            self._data.setdefault("task_history", []).append({
                "task_id": task_id,
                "timestamp": time.time(),
                "prompt": prompt[:1200],
                "summary": summary[:4000],
                "files_changed": files_changed[:100],
                "review": review[:2000],
                "mission": bool(mission),
            })
            self._data["task_history"] = self._data["task_history"][-50:]
            self._save()

    def context(self, limit: int = 6) -> str:
        with self._lock:
            items = self._data.get("task_history", [])[-max(1, limit):]
        if not items:
            return "No previous completed tasks are recorded for this workspace."
        return self._format(items)

    # Tokens too generic to carry recall signal — an overlap on these
    # alone would call every past task "relevant".
    _QUERY_GENERIC = frozenset({
        "what", "when", "where", "which", "that", "this", "these", "those",
        "have", "has", "had", "with", "from", "your", "yours", "would",
        "could", "should", "there", "their", "them", "then", "than",
        "about", "does", "did", "into", "been", "were", "they", "will",
        "shall", "each", "such", "like", "just", "make", "made", "more",
        "some", "also", "only", "very", "much", "many", "same", "here",
        "take", "over", "used", "uses", "work", "task", "fix", "fixed",
        "fixes", "thing", "things", "something", "anything", "everything",
        "tell", "know", "want", "need", "please", "thanks", "thank",
        "deleted", "delete", "open", "close", "change", "changed",
    })

    @staticmethod
    def _tokens(text: str) -> set:
        return set(re.findall(r"[a-z0-9_]{4,}", str(text).lower()))

    def context_for(self, query: str, *, limit: int = 6,
                    max_items: int = 3,
                    exclude_task_ids: set | None = None) -> str:
        """Relevance-gated recall for the conversation lane: only task
        records that share a content token with the query are injected.
        Returns "" when nothing matches — a chat turn about an unrelated
        topic must not get six verbatim mission/workstream summaries
        dumped into its context (the model reliably echoes them).
        Mission/work-order records are excluded entirely — machine-
        authored work prose is not conversational context. ``exclude_task_ids``
        catches legacy rows recorded before the ``mission`` flag existed
        (the row's stored task id resolves against the live ledger)."""
        qt = self._tokens(query) - self._QUERY_GENERIC
        if not qt:
            return ""
        exclude = set(exclude_task_ids or ())
        with self._lock:
            items = list(self._data.get("task_history", [])[-max(1, limit):])
        scored = []
        for item in reversed(items):  # newest first
            if (item.get("mission")
                    or str(item.get("task_id") or "") in exclude):
                continue
            hay = self._tokens(
                f"{item.get('prompt','')} {item.get('summary','')} "
                f"{' '.join(map(str, item.get('files_changed', [])))}")
            if qt & hay:
                scored.append(item)
            if len(scored) >= max_items:
                break
        scored.reverse()
        return self._format(scored) if scored else ""

    @staticmethod
    def _format(items: list) -> str:
        lines = ["Recent project work remembered locally:"]
        for item in items:
            changed = ", ".join(item.get("files_changed", [])[:8]) or "no files recorded"
            lines.append(f"- {item.get('summary') or item.get('prompt', '')} [files: {changed}]")
        return "\n".join(lines)
