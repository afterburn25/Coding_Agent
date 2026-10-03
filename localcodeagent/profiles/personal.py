"""Profile personal memory — isolated per-profile notes/facts.

This is PROFILE memory, separate from device/global knowledge (chats,
models, Nexus Brain stay shared — the spec is explicit that huge shared
assets are never duplicated). Profile A can never read Profile B's
personal memory because each store only ever opens its own profile dir.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text

_MAX_ENTRIES = 5_000
_MAX_TEXT = 4_000


class PersonalMemory:
    """Simple durable list of personal memory entries for one profile."""

    def __init__(self, profile_dir: Path) -> None:
        self.dir = Path(profile_dir) / "memory"
        self.path = self.dir / "personal.json"

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return raw
        except Exception:
            pass
        return []

    def _save(self, items: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(items, indent=2,
                                               ensure_ascii=False))

    def list(self, *, limit: int = 500) -> list[dict]:
        return self._load()[-limit:]

    def remember(self, text: Any, *, kind: str = "note") -> dict:
        t = " ".join(str(text or "").split())[:_MAX_TEXT]
        if not t:
            raise ValueError("memory text required")
        items = self._load()[-_MAX_ENTRIES:]
        entry = {"id": uuid.uuid4().hex, "kind": str(kind or "note")[:40],
                 "text": t, "created_at": time.time()}
        items.append(entry)
        self._save(items)
        return entry

    def forget(self, entry_id: str) -> bool:
        items = self._load()
        kept = [e for e in items if e.get("id") != entry_id]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True
