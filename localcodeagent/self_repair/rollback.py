"""Last-known-good snapshots and restore.

Before a promoted repair touches the stable tree, every file it will
overwrite (and every file it will delete) is snapshotted byte-for-byte
under `data/self_repair/lkg/<incident>/`. Restoring copies them back —
works with or without git and never depends on the LLM.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text, replace_with_retry


class Rollback:
    """Byte-verified rollback snapshots rooted below the repair state dir.

    Snapshot manifests are treated as untrusted input on restore: incident
    names, repo-relative file paths, expected digests, and added-file
    removals are all validated before the stable tree is touched.
    """

    MAX_SNAPSHOTS = 16
    MAX_FILES = 500
    MAX_MANIFEST_BYTES = 1024 * 1024
    _INCIDENT_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
    _PROTECTED_PREFIXES = (".git", ".repair-worktrees")

    def __init__(self, repo_root: Path, state_root: Path,
                 max_snapshots: int | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.lkg_root = Path(state_root) / "lkg"
        self.lkg_root.mkdir(parents=True, exist_ok=True)
        self.max_snapshots = max(
            1, int(max_snapshots or self.MAX_SNAPSHOTS))
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # path and digest safety

    def _incident_dir(self, incident_id: str) -> Path | None:
        raw = str(incident_id or "")
        if not self._INCIDENT_RX.fullmatch(raw):
            return None
        root = self.lkg_root.resolve()
        dest = (root / raw).resolve()
        return dest if dest.parent == root else None

    def _inside(self, root: Path, path: Path) -> Path | None:
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(Path(root).resolve())
            return resolved
        except (OSError, ValueError):
            return None

    @classmethod
    def _safe_rel(cls, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or len(raw) > 512 or "\x00" in raw:
            return ""
        norm = raw.replace("\\", "/")
        if norm.startswith(("/", "~")):
            return ""
        parts = [p for p in norm.split("/") if p]
        if not parts or any(p in {".", ".."} for p in parts):
            return ""
        if re.fullmatch(r"[A-Za-z]:", parts[0]):
            return ""
        if parts[0] in cls._PROTECTED_PREFIXES:
            return ""
        return "/".join(parts)

    def _repo_target(self, rel: str) -> Path | None:
        return self._inside(self.repo_root, self.repo_root / rel)

    def _snapshot_source(self, root: Path, rel: str) -> Path | None:
        return self._inside(root, root / rel)

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _hash_matches(self, path: Path, expected: str) -> bool:
        expected = str(expected or "").lower()
        if not re.fullmatch(r"[0-9a-f]{16,64}", expected):
            return False
        return self._hash(path).startswith(expected)

    def _copy_verified(self, src: Path, dst: Path) -> None:
        """Copy to a same-directory tmp file, then replace atomically."""
        tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tmp)
            if self._hash(tmp) != self._hash(src):
                raise OSError("copied file digest mismatch")
            replace_with_retry(tmp, dst)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _remove_tree(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # bounded snapshot lifecycle

    def _snapshot_time(self, path: Path) -> float:
        try:
            manifest = path / "manifest.json"
            if manifest.is_file():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                return float(data.get("ts") or path.stat().st_mtime)
            return path.stat().st_mtime
        except (OSError, ValueError, TypeError):
            return 0.0

    def _prune(self, keep: str = "") -> None:
        """Retain only the newest snapshots; stale staged dirs don't count."""
        rows: list[tuple[float, Path]] = []
        try:
            for child in self.lkg_root.iterdir():
                if (child.is_dir()
                        and self._INCIDENT_RX.fullmatch(child.name)):
                    rows.append((self._snapshot_time(child), child))
        except OSError:
            return
        rows.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        capacity = self.max_snapshots - int(
            bool(keep) and any(path.name == keep for _, path in rows))
        kept = 0
        for _ts, path in rows:
            if path.name == keep:
                continue
            kept += 1
            if kept > capacity:
                try:
                    self._remove_tree(path)
                except OSError:
                    pass

    def snapshot(self, incident_id: str, files: list[str]) -> dict[str, Any]:
        """Preserve pre-repair content transactionally.

        Files are copied into a sibling staging directory first. Only when
        every requested path is valid and every copied digest verifies does
        the staged snapshot replace the incident's previous snapshot.
        """
        with self._lock:
            dest = self._incident_dir(incident_id)
            if dest is None:
                return {"ok": False, "error": "invalid incident id",
                        "files": []}
            if len(files or []) > self.MAX_FILES:
                return {"ok": False, "error": "too many rollback files",
                        "files": []}

            rels: list[str] = []
            errors: list[str] = []
            for raw in files or []:
                rel = self._safe_rel(raw)
                if not rel:
                    errors.append(f"unsafe rollback path: {raw!r}")
                elif rel in rels:
                    errors.append(f"duplicate rollback path: {rel}")
                else:
                    rels.append(rel)
            if errors:
                return {"ok": False, "error": "; ".join(errors[:4]),
                        "files": []}

            stage = self.lkg_root / (
                f".{incident_id}.stage-{uuid.uuid4().hex[:8]}")
            manifest: dict[str, Any] = {
                "ok": True, "schema": 1, "incident": incident_id,
                "ts": time.time(), "files": [],
            }
            try:
                stage.mkdir(parents=True, exist_ok=False)
                for rel in rels:
                    src = self._repo_target(rel)
                    if src is None or self._inside(self.lkg_root, src):
                        raise OSError(f"unsafe repository path: {rel}")
                    entry = {"path": rel,
                             "existed": bool(src and src.is_file())}
                    if src.is_file():
                        entry["sha256"] = self._hash(src)
                        entry["size"] = src.stat().st_size
                        dst = self._snapshot_source(stage, rel)
                        if dst is None:
                            raise OSError(f"unsafe snapshot path: {rel}")
                        self._copy_verified(src, dst)
                    manifest["files"].append(entry)
                manifest["file_count"] = len(manifest["files"])
                manifest["bytes"] = sum(int(e.get("size") or 0)
                                        for e in manifest["files"])
                atomic_write_text(stage / "manifest.json",
                                  json.dumps(manifest, indent=2))

                previous = self.lkg_root / (
                    f".{incident_id}.old-{uuid.uuid4().hex[:8]}")
                moved_previous = False
                try:
                    if dest.exists() or dest.is_symlink():
                        dest.rename(previous)
                        moved_previous = True
                    stage.rename(dest)
                except OSError:
                    if moved_previous and previous.exists() and not dest.exists():
                        try:
                            previous.rename(dest)
                        except OSError:
                            pass
                    raise
                if previous.exists():
                    self._remove_tree(previous)
                manifest["snapshot"] = str(dest)
                self._prune(keep=dest.name)
                return manifest
            except OSError as exc:
                try:
                    self._remove_tree(stage)
                except OSError:
                    pass
                return {"ok": False, "error": str(exc)[:300],
                        "files": []}
            finally:
                for leftover in self.lkg_root.glob(
                        f".{incident_id}.old-*"):
                    try:
                        self._remove_tree(leftover)
                    except OSError:
                        pass

    # ------------------------------------------------------------------
    # manifest verification + restore

    def _manifest_records(self, dest: Path) -> tuple[list[dict], list[str]]:
        manifest_path = dest / "manifest.json"
        try:
            if (not manifest_path.is_file()
                    or manifest_path.stat().st_size > self.MAX_MANIFEST_BYTES):
                return [], ["missing or oversized snapshot manifest"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return [], ["corrupt snapshot manifest"]
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(manifest, dict) or manifest.get("ok") is False:
            return [], ["snapshot manifest is invalid"]
        if manifest.get("schema") not in (None, 1):
            return [], ["unsupported snapshot manifest schema"]
        if str(manifest.get("incident") or "") != dest.name:
            return [], ["snapshot manifest incident mismatch"]
        if not isinstance(files, list) or not files:
            return [], ["snapshot manifest has no files"]
        if len(files) > self.MAX_FILES:
            return [], ["snapshot manifest exceeds file limit"]

        records: list[dict] = []
        errors: list[str] = []
        seen: set[str] = set()
        for item in files:
            if not isinstance(item, dict):
                errors.append("snapshot manifest has a malformed file entry")
                continue
            rel = self._safe_rel(item.get("path"))
            if not rel:
                errors.append(f"unsafe rollback path: {item.get('path')!r}")
                continue
            if rel in seen:
                errors.append(f"duplicate rollback path: {rel}")
                continue
            seen.add(rel)
            target = self._repo_target(rel)
            snapshot_src = self._snapshot_source(dest, rel)
            if target is None or snapshot_src is None:
                errors.append(f"rollback path escapes its root: {rel}")
                continue
            if type(item.get("existed")) is not bool:
                errors.append(f"malformed rollback existence flag: {rel}")
                continue
            existed = bool(item.get("existed"))
            expected = str(item.get("sha256") or "").lower()
            if existed:
                if not re.fullmatch(r"[0-9a-f]{16,64}", expected):
                    errors.append(f"missing rollback digest: {rel}")
                    continue
                if not snapshot_src.is_file():
                    errors.append(f"missing snapshot file: {rel}")
                    continue
                if not self._hash_matches(snapshot_src, expected):
                    errors.append(f"rollback digest mismatch: {rel}")
                    continue
            elif snapshot_src.exists():
                errors.append(f"unexpected snapshot payload: {rel}")
                continue
            records.append({"path": rel, "existed": existed,
                            "expected": expected, "src": snapshot_src,
                            "target": target})
        return records, errors

    def restore(self, incident_id: str) -> dict[str, Any]:
        """Restore the snapshot; files the candidate *added* are removed
        so the stable tree returns exactly to last-known-good."""
        with self._lock:
            dest = self._incident_dir(incident_id)
            if dest is None or not dest.is_dir():
                return {"ok": False, "error": "no snapshot",
                        "incident": incident_id}
            records, errors = self._manifest_records(dest)
            if errors:
                return {"ok": False, "error": "invalid rollback snapshot",
                        "errors": errors[:12], "incident": incident_id,
                        "validated": False}

            restored, removed = 0, 0
            apply_errors: list[str] = []
            for rec in records:
                rel = rec["path"]
                target = rec["target"]
                try:
                    if rec["existed"]:
                        if target.is_dir() and not target.is_symlink():
                            raise OSError("target is a directory")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        self._copy_verified(rec["src"], target)
                        restored += 1
                    elif target.is_file() or target.is_symlink():
                        target.unlink()
                        removed += 1
                    elif target.exists():
                        raise OSError("target is not a file")
                except OSError as exc:
                    apply_errors.append(f"{rel}: {exc}")

            verify_errors: list[str] = []
            for rec in records:
                rel = rec["path"]
                target = rec["target"]
                try:
                    if rec["existed"]:
                        if (not target.is_file()
                                or not self._hash_matches(
                                    target, rec["expected"])):
                            verify_errors.append(f"restored digest mismatch: {rel}")
                    elif target.exists():
                        verify_errors.append(f"added file still present: {rel}")
                except OSError as exc:
                    verify_errors.append(f"{rel}: {exc}")

            errors = apply_errors + verify_errors
            return {"ok": not errors, "restored": restored,
                    "removed_added": removed, "errors": errors[:12],
                    "validated": not errors, "incident": incident_id}
