"""Last-known-good snapshots and restore.

Before a promoted repair touches the stable tree, every file it will
overwrite (and every file it will delete) is snapshotted byte-for-byte
under `data/self_repair/lkg/<incident>/`. Restoring copies them back —
works with or without git and never depends on the LLM.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text


class Rollback:
    def __init__(self, repo_root: Path, state_root: Path):
        self.repo_root = Path(repo_root)
        self.lkg_root = Path(state_root) / "lkg"
        self.lkg_root.mkdir(parents=True, exist_ok=True)

    def _hash(self, p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]

    def snapshot(self, incident_id: str, files: list[str]) -> dict[str, Any]:
        """Preserve the pre-repair content of every file the candidate
        will touch. Returns the manifest that `restore()` consumes."""
        dest = self.lkg_root / incident_id
        dest.mkdir(parents=True, exist_ok=True)
        manifest = {"incident": incident_id, "ts": time.time(),
                    "files": []}
        for rel in files:
            src = self.repo_root / rel
            entry = {"path": rel, "existed": src.is_file()}
            if src.is_file():
                entry["sha256"] = self._hash(src)
                dst = dest / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            manifest["files"].append(entry)
        atomic_write_text(dest / "manifest.json",
                          json.dumps(manifest, indent=2))
        return manifest

    def restore(self, incident_id: str) -> dict[str, Any]:
        """Restore the snapshot; files the candidate *added* are removed
        so the stable tree returns exactly to last-known-good."""
        dest = self.lkg_root / incident_id
        manifest_path = dest / "manifest.json"
        if not manifest_path.is_file():
            return {"ok": False, "error": "no snapshot"}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            return {"ok": False, "error": "corrupt snapshot manifest"}
        restored, removed, errors = 0, 0, []
        for entry in manifest.get("files", []):
            rel = entry["path"]
            target = self.repo_root / rel
            if entry.get("existed"):
                src = dest / rel
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
                    restored += 1
                except OSError as exc:
                    errors.append(f"{rel}: {exc}")
            else:
                try:
                    if target.is_file():
                        target.unlink()
                        removed += 1
                except OSError as exc:
                    errors.append(f"{rel}: {exc}")
        return {"ok": not errors, "restored": restored,
                "removed_added": removed, "errors": errors,
                "incident": incident_id}
