from __future__ import annotations

import difflib
import json
import shutil
import threading
import time
from pathlib import Path

from ..fsutil import atomic_write_text
from typing import Any


class CheckpointManager:
    """Per-task first-write snapshots with deterministic restore."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".agent" / "checkpoints"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _task_dir(self, task_id: str) -> Path:
        if not task_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in task_id):
            raise ValueError("invalid task id")
        return self.root / task_id

    def _relative(self, path: Path) -> Path:
        resolved = path.resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise ValueError("path escapes the selected workspace")
        rel = resolved.relative_to(self.workspace)
        if rel.parts and rel.parts[0] == ".agent":
            raise ValueError("agent metadata cannot be edited through workspace tools")
        return rel

    def _manifest_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "manifest.json"

    def _load_manifest(self, task_id: str) -> dict[str, Any]:
        path = self._manifest_path(task_id)
        if not path.exists():
            return {"task_id": task_id, "created_at": time.time(), "files": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_manifest(self, task_id: str, manifest: dict[str, Any]) -> None:
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        path = self._manifest_path(task_id)
        atomic_write_text(path, json.dumps(manifest, indent=2))

    def snapshot(self, task_id: str, path: Path) -> None:
        with self._lock:
            rel = self._relative(path)
            key = rel.as_posix()
            manifest = self._load_manifest(task_id)
            if key in manifest["files"]:
                return
            target = self.workspace / rel
            entry: dict[str, Any] = {"existed": target.exists(), "is_dir": target.is_dir() if target.exists() else False}
            if target.exists() and target.is_file():
                data = target.read_bytes()
                # Use a file copy for normal files and keep metadata compact.
                backup = self._task_dir(task_id) / "files" / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(data)
                entry["backup"] = str(Path("files") / rel)
                entry["size"] = len(data)
            manifest["files"][key] = entry
            self._save_manifest(task_id, manifest)

    def changed_files(self, task_id: str) -> list[str]:
        with self._lock:
            return sorted(self._load_manifest(task_id).get("files", {}).keys())

    def diff(self, task_id: str, max_chars: int = 20000) -> str:
        with self._lock:
            manifest = self._load_manifest(task_id)
            lines: list[str] = []
            for key, entry in sorted(manifest.get("files", {}).items()):
                target = self.workspace / key
                if entry.get("existed") and not entry.get("is_dir"):
                    backup = self._task_dir(task_id) / entry["backup"]
                    old = backup.read_text(encoding="utf-8", errors="replace") if backup.exists() else ""
                else:
                    old = ""
                new = target.read_text(encoding="utf-8", errors="replace") if target.exists() and target.is_file() else ""
                lines.extend(difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"a/{key}",
                    tofile=f"b/{key}",
                ))
                if sum(len(x) for x in lines) >= max_chars:
                    break
            diff = "".join(lines)
            return diff[:max_chars] + ("\n...diff truncated..." if len(diff) > max_chars else "")

    def prune_orphans(self, keep: set[str]) -> int:
        """Remove checkpoint dirs whose task fell out of the task ledger —
        task records are capped but snapshots would otherwise grow forever."""
        removed = 0
        with self._lock:
            try:
                for path in self.root.iterdir():
                    if path.is_dir() and path.name not in keep:
                        shutil.rmtree(path, ignore_errors=True)
                        removed += 1
            except OSError:
                pass
        return removed

    def restore(self, task_id: str) -> list[str]:
        with self._lock:
            manifest = self._load_manifest(task_id)
            restored: list[str] = []
            # Restore deepest paths first in case future versions snapshot directories.
            for key, entry in sorted(manifest.get("files", {}).items(), key=lambda item: item[0].count("/"), reverse=True):
                rel = Path(key)
                target = self.workspace / rel
                if entry.get("existed"):
                    if entry.get("is_dir"):
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        backup = self._task_dir(task_id) / entry["backup"]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup, target)
                else:
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                restored.append(key)
            return restored
