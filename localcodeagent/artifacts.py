"""Unified artifact registry for Nexus-generated outputs.

Every output Nexus produces — code, reports, images, archives, datasets —
registers here with provenance (creator task/mission, inputs, tool). The
registry is a bounded JSON store at ``data/artifacts/registry.json``;
artifact bytes live wherever the producer wrote them (or under
``data/artifacts/files/`` when registered with ``store=True``).
"""
from __future__ import annotations

import hashlib
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

MAX_REGISTRY = 2000

_KIND_BY_EXT = {
    ".py": "code", ".js": "code", ".ts": "code", ".cs": "code",
    ".md": "report", ".txt": "report", ".html": "report",
    ".pdf": "pdf", ".xlsx": "spreadsheet", ".csv": "spreadsheet",
    ".pptx": "presentation",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".mp4": "video", ".webm": "video",
    ".wav": "audio", ".mp3": "audio", ".ogg": "audio",
    ".zip": "archive", ".7z": "archive", ".tar": "archive",
    ".exe": "installer", ".msi": "installer",
    ".jsonl": "dataset", ".parquet": "dataset",
}


class ArtifactManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.files_dir = self.root / "files"
        self.registry_path = self.root / "registry.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._rows: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            import json
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            self._rows = list(data.get("artifacts") or [])[-MAX_REGISTRY:]
        except (OSError, ValueError):
            self._rows = []

    def _save(self) -> None:
        import json
        atomic_write_text(self.registry_path, json.dumps(
            {"version": 1, "artifacts": self._rows[-MAX_REGISTRY:]},
            indent=2, ensure_ascii=False, default=str))

    @staticmethod
    def _sha256(path: Path) -> str:
        try:
            h = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""

    def register(self, path: str | Path, *, kind: str = "",
                 creator: str = "", mission_id: str = "",
                 task_id: str = "", tool: str = "",
                 metadata: dict | None = None,
                 provenance: dict | None = None,
                 store: bool = False) -> dict[str, Any]:
        """Register an existing file (or copy it into the artifact store
        when store=True). Returns the artifact record."""
        src = Path(path)
        rec_id = f"art-{uuid.uuid4().hex[:12]}"
        final = src
        if store:
            if not src.is_file():
                raise FileNotFoundError(src)
            final = self.files_dir / rec_id / src.name
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, final)
        kind = kind or _KIND_BY_EXT.get(final.suffix.lower(), "file")
        # Version = number of prior artifacts with the same filename.
        ver = 1 + sum(1 for r in self._rows
                      if Path(str(r.get("path", ""))).name == final.name)
        row = {
            "id": rec_id,
            "kind": kind,
            "path": str(final),
            "name": final.name,
            "size": final.stat().st_size if final.is_file() else 0,
            "sha256": self._sha256(final) if final.is_file() else "",
            "creator": str(creator),
            "mission_id": str(mission_id),
            "task_id": str(task_id),
            "tool": str(tool),
            "version": ver,
            "created_at": time.time(),
            "metadata": dict(metadata or {}),
            "provenance": dict(provenance or {}),
        }
        with self._lock:
            self._rows.append(row)
            self._save()
        return dict(row)

    def list(self, *, kind: str = "", mission_id: str = "",
             task_id: str = "", limit: int = 200) -> list[dict]:
        with self._lock:
            rows = [r for r in self._rows
                    if (not kind or r.get("kind") == kind)
                    and (not mission_id or r.get("mission_id") == mission_id)
                    and (not task_id or r.get("task_id") == task_id)]
        return [dict(r) for r in rows[-limit:]]

    def get(self, artifact_id: str) -> dict | None:
        with self._lock:
            for r in self._rows:
                if r.get("id") == artifact_id:
                    return dict(r)
        return None

    def verify(self, artifact_id: str) -> dict[str, Any]:
        """Confirm the artifact still exists and its hash matches."""
        row = self.get(artifact_id)
        if row is None:
            return {"ok": False, "reason": "unknown artifact"}
        p = Path(row["path"])
        if not p.is_file():
            return {"ok": False, "reason": "file missing"}
        if row.get("sha256") and self._sha256(p) != row["sha256"]:
            return {"ok": False, "reason": "hash mismatch (modified)"}
        return {"ok": True}
