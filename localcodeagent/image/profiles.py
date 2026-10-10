from __future__ import annotations

import json
from pathlib import Path
from time import time
from typing import Any

from ..fsutil import atomic_write_text


class SubjectProfileStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, profile_id: str) -> Path:
        safe = "".join(c for c in profile_id if c.isalnum() or c in "-_ ").strip().replace(" ", "_")
        if not safe:
            raise ValueError("profile id is required")
        return self.root / f"{safe}.json"

    def list(self) -> list[dict[str, Any]]:
        rows=[]
        for path in sorted(self.root.glob("*.json")):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return rows

    def get(self, profile_id: str) -> dict[str, Any]:
        path=self._path(profile_id)
        if not path.exists():
            raise KeyError(profile_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, profile: dict[str, Any]) -> dict[str, Any]:
        profile_id=str(profile.get("id") or profile.get("display_name") or "").strip()
        if not profile_id:
            raise ValueError("profile id or display_name is required")
        existing={}
        try:
            existing=self.get(profile_id)
        except KeyError:
            pass
        merged={**existing, **profile, "id": profile_id, "updated_at": time()}
        merged.setdefault("created_at", time())
        merged.setdefault("reference_images", [])
        merged.setdefault("saved_prompts", [])
        merged.setdefault("loras", [])
        atomic_write_text(self._path(profile_id), json.dumps(merged, indent=2), encoding="utf-8")
        return merged
