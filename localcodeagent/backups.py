"""Versioned backups of critical Nexus state (Part J).

Backs up: Nexus Brain, Answer Memory DB, missions/autonomy stores,
config.json, skills, settings, indexes. Each backup is a timestamped
directory under ``data/backups/`` with a manifest (files + sha256).
Restore verifies manifest hashes before copying and NEVER deletes the
last valid backup — the oldest are pruned only after newer verified
backups exist.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

KEEP_BACKUPS = 10

DEFAULT_SOURCES = (
    "data/nexus_brain.json",
    "data/nexus_brain/answer_memory.db",
    "data/autonomy",
    "data/conversation_memory.json",
    "data/skills",
    "config.json",
    ".agent/project.json",
    ".agent/model_performance.json",
)


class BackupService:
    def __init__(self, workspace: Path, *,
                 backup_root: Path | None = None,
                 keep: int = KEEP_BACKUPS) -> None:
        self.workspace = Path(workspace).resolve()
        self.backup_root = (Path(backup_root) if backup_root
                            else self.workspace / "data" / "backups")
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.keep = max(2, int(keep))
        self._lock = threading.RLock()

    @staticmethod
    def _sha(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _sources(self, extra: list[str] | None) -> list[Path]:
        out: list[Path] = []
        for rel in list(DEFAULT_SOURCES) + list(extra or []):
            p = (self.workspace / rel).resolve()
            try:
                p.relative_to(self.workspace)
            except ValueError:
                continue  # never back up outside the workspace
            if p.exists():
                out.append(p)
        return out

    def create(self, *, extra: list[str] | None = None,
               label: str = "") -> dict[str, Any]:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.backup_root / f"backup-{stamp}"
        n = 1
        while dest.exists():
            n += 1
            dest = self.backup_root / f"backup-{stamp}-{n}"
        dest.mkdir(parents=True)
        manifest: list[dict[str, Any]] = []
        with self._lock:
            for src in self._sources(extra):
                rel = src.relative_to(self.workspace)
                files = sorted(src.rglob("*")) if src.is_dir() else [src]
                for f in files:
                    if not f.is_file():
                        continue
                    frel = f.relative_to(self.workspace)
                    tgt = dest / "files" / frel
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(f, tgt)
                        manifest.append({"path": str(frel).replace("\\", "/"),
                                         "size": f.stat().st_size,
                                         "sha256": self._sha(f)})
                    except OSError:
                        pass
        meta = {"created_at": time.time(), "label": label,
                "files": len(manifest)}
        atomic_write_text(dest / "manifest.json", json.dumps(
            {"meta": meta, "manifest": manifest}, indent=2))
        self._prune()
        return {"ok": True, "backup": dest.name, "files": len(manifest),
                "path": str(dest)}

    def list(self) -> list[dict[str, Any]]:
        out = []
        for d in sorted(self.backup_root.glob("backup-*")):
            try:
                meta = json.loads(
                    (d / "manifest.json").read_text(encoding="utf-8"))
                out.append({"name": d.name,
                            "created_at": meta["meta"]["created_at"],
                            "label": meta["meta"].get("label", ""),
                            "files": meta["meta"]["files"]})
            except (OSError, ValueError, KeyError):
                out.append({"name": d.name, "corrupt": True})
        return out

    def _verify_dir(self, d: Path) -> tuple[bool, dict | None]:
        try:
            meta = json.loads((d / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError):
            return False, None
        for row in meta.get("manifest", []):
            f = d / "files" / row["path"]
            if not f.is_file() or self._sha(f) != row["sha256"]:
                return False, meta
        return True, meta

    def restore(self, backup_name: str, *, dry_run: bool = False) -> dict:
        """Restore a backup after verifying every file's manifest hash.
        Current live files are stashed under data/backups/pre-restore-<ts>
        first so a bad restore never destroys working state."""
        d = (self.backup_root / backup_name).resolve()
        if not d.is_dir() or d.parent != self.backup_root.resolve():
            return {"ok": False, "error": "unknown backup"}
        ok, meta = self._verify_dir(d)
        if not ok or meta is None:
            return {"ok": False, "error": "backup failed hash verification"}
        if dry_run:
            return {"ok": True, "would_restore": len(meta["manifest"])}
        stash = self.backup_root / f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}"
        restored = 0
        for row in meta["manifest"]:
            src = d / "files" / row["path"]
            tgt = (self.workspace / row["path"]).resolve()
            try:
                tgt.relative_to(self.workspace)
            except ValueError:
                continue
            if tgt.is_file():
                s = stash / row["path"]
                s.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tgt, s)
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tgt)
            restored += 1
        return {"ok": True, "restored": restored,
                "stash": str(stash)}

    def _created_at(self, d: Path) -> float:
        try:
            return float(json.loads(
                (d / "manifest.json").read_text("utf-8"))["meta"]["created_at"])
        except (OSError, ValueError, KeyError):
            try:
                return d.stat().st_mtime
            except OSError:
                return 0.0

    def _prune(self) -> None:
        """Delete oldest backups beyond `keep` — but only ones that verify
        or corrupt ones when verified newer ones exist. The last remaining
        backup is never deleted."""
        backups = sorted(self.backup_root.glob("backup-*"),
                         key=self._created_at)
        while len(backups) > self.keep:
            victim = backups[0]
            ok, _ = self._verify_dir(victim)
            remaining = backups[1:]
            if not ok and not any(self._verify_dir(b)[0] for b in remaining):
                break  # never destroy the last valid backup
            if len(remaining) < 1:
                break
            shutil.rmtree(victim, ignore_errors=True)
            backups = remaining
