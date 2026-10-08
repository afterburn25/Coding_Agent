"""Unified artifact registry for Nexus-generated outputs.

Every output Nexus produces — code, reports, images, archives, datasets —
registers here with provenance (creator task/mission, inputs, tool). The
registry is a bounded JSON store at ``data/artifacts/registry.json``;
artifact bytes live wherever the producer wrote them (or under
``data/artifacts/files/`` when registered with ``store=True``).
"""
from __future__ import annotations

import hashlib
import mimetypes
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

MAX_REGISTRY = 2000

# Retention classes — cleanup must never treat user/release/pinned
# artifacts like scratch. Nothing deletes from the registry today, but
# every record declares its class so a future sweeper can distinguish.
RETENTION_CLASSES = {"temporary", "cached", "user", "release", "pinned"}

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
        # Optional LineageStore — set by AppState after construction.
        self.lineage = None
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
                 project_id: str = "",
                 requirement_ids: list[str] | None = None,
                 metadata: dict | None = None,
                 provenance: dict | None = None,
                 retention: str = "",
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
            "project_id": str(project_id),
            "requirement_ids": [str(r) for r in (requirement_ids or [])][:40],
            "version": ver,
            "created_at": time.time(),
            "metadata": dict(metadata or {}),
            "provenance": dict(provenance or {}),
            # Published remote copies — {provider, repository, kind,
            # release_id, asset_id, tag, web_url, download_url,
            # uploaded_at}. A local artifact and its GitHub copy stay
            # associated through this field.
            "remote": {},
            "retention": (retention if retention in RETENTION_CLASSES
                          else "user"),
            "pinned": retention == "pinned",
        }
        with self._lock:
            self._rows.append(row)
            self._save()
        # Provenance fields become a first-class lineage record when a
        # LineageStore is attached (set by AppState).
        if self.lineage is not None:
            try:
                contribs = []
                prov = dict(provenance or {})
                if tool:
                    contribs.append({"kind": "tool_output", "ref": tool})
                for src in list(prov.get("inputs") or [])[:40]:
                    contribs.append({"kind": "file", "ref": str(src)})
                for rq in row["requirement_ids"]:
                    contribs.append({"kind": "requirement", "ref": rq})
                if creator:
                    contribs.append({"kind": "worker", "ref": creator})
                self.lineage.record(
                    rec_id, target_kind="artifact",
                    contributors=contribs, project_id=project_id,
                    mission_id=mission_id, task_id=task_id)
            except Exception:
                pass
        return dict(row)

    def versions(self, name: str) -> list[dict]:
        """All registered versions of a filename, oldest → newest."""
        with self._lock:
            rows = [dict(r) for r in self._rows
                    if r.get("name") == name]
        return sorted(rows, key=lambda r: (r.get("version") or 0,
                                           r.get("created_at") or 0))

    def latest(self, name: str) -> dict | None:
        vs = self.versions(name)
        return vs[-1] if vs else None

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

    _REMOTE_KEYS = (
        "provider", "repository", "kind", "release_id", "release_url",
        "asset_id", "tag", "name", "size", "web_url", "download_url",
        "uploaded_at", "run_id", "artifact_name", "expires_at",
    )

    def attach_remote(self, artifact_id: str, remote: dict) -> dict | None:
        """Record a published remote copy on an existing artifact —
        e.g. the GitHub release asset produced by uploading it. Keeps
        only safe, non-secret fields; URLs must be https."""
        with self._lock:
            for row in self._rows:
                if row.get("id") != artifact_id:
                    continue
                clean: dict[str, Any] = {}
                for key in self._REMOTE_KEYS:
                    val = remote.get(key)
                    if val in (None, ""):
                        continue
                    if key.endswith("_url") or key == "web_url":
                        if not str(val).lower().startswith("https://"):
                            continue
                        val = str(val)
                    clean[key] = val
                merged = dict(row.get("remote") or {})
                merged.update(clean)
                row["remote"] = merged
                # A published copy is a release artifact — outlives
                # ordinary cleanup unless the user pinned it already.
                if not row.get("pinned"):
                    row["retention"] = "release"
                self._save()
                return dict(row)
        return None

    def pin(self, artifact_id: str, pinned: bool = True) -> dict | None:
        """Pin/unpin an artifact — pinned records are exempt from any
        retention sweep, present or future."""
        with self._lock:
            for row in self._rows:
                if row.get("id") != artifact_id:
                    continue
                row["pinned"] = bool(pinned)
                row["retention"] = "pinned" if pinned else "user"
                self._save()
                return dict(row)
        return None

    def find(self, name_or_id: str = "", *,
             task_id: str = "", limit: int = 12) -> list[dict]:
        """Resolve 'the installer', 'the zip', an id, or latest —
        name matching is case-insensitive substring on the filename."""
        key = str(name_or_id or "").strip().lower()
        with self._lock:
            rows = [r for r in self._rows
                    if not task_id or r.get("task_id") == task_id]
        if key.startswith("art-"):
            hit = [r for r in rows if r.get("id") == key]
            return [dict(r) for r in hit]
        if not key or key in ("file", "it", "that", "artifact",
                              "latest", "the file", "the artifact"):
            return [dict(r) for r in rows[-limit:]][::-1]
        matched = [r for r in rows if key in str(r.get("name", "")).lower()]
        if not matched:
            # Token-wise fallback: 'installer' should find
            # 'NexusCore-Setup.exe' via kind too.
            matched = [r for r in rows
                       if key == str(r.get("kind", "")).lower()]
        return [dict(r) for r in matched[-limit:]][::-1]

    def client_view(self, artifact) -> dict | None:
        """Safe client-facing projection — never exposes absolute
        filesystem paths. Accepts an id or a record dict."""
        row = self.get(artifact) if isinstance(artifact, str) \
            else dict(artifact)
        if not row:
            return None
        aid = str(row.get("id") or "")
        path = Path(str(row.get("path") or ""))
        exists = path.is_file()
        verified = bool(exists and self.verify(aid).get("ok"))
        return {
            "id": aid,
            "filename": str(row.get("name") or path.name),
            "kind": str(row.get("kind") or "file"),
            "size": int(row.get("size") or 0),
            "mime": mimetypes.guess_type(
                str(row.get("name") or path.name))[0]
                or "application/octet-stream",
            "sha256": str(row.get("sha256") or ""),
            "verified": verified,
            "available": exists,
            "version": int(row.get("version") or 1),
            "created_at": float(row.get("created_at") or 0),
            "task_id": str(row.get("task_id") or ""),
            "mission_id": str(row.get("mission_id") or ""),
            "creator": str(row.get("creator") or ""),
            "download_url": f"/api/artifacts/{aid}/download",
            "remote": dict(row.get("remote") or {}),
            "retention": str(row.get("retention") or "user"),
            "pinned": bool(row.get("pinned")),
        }
