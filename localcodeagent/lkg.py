"""Last-known-good application snapshots and rollback coordination.

Distinct from ``safemode.GoldenConfigStore`` — that covers whitelisted
*configuration* files. This store snapshots the *application itself*:
``backend/`` (exe + _internal), ``config.json``, and ``VERSION``, so a
bad self-update can roll the install back wholesale.

Two durable flags coordinate with the desktop host, because a running
frozen exe cannot overwrite itself on Windows:

- ``rollback.flag`` — the backend (or repeated-failure watchdog) asks the
  host to restore a verified snapshot before launching the backend.
- ``update.flag`` — a staged backend build waits in ``backend-new/``;
  the host swaps it in at launch.
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

KEEP_SNAPSHOTS = 3
# Consecutive unclean boots before the watchdog requests an automatic
# rollback — same threshold Safe Mode uses for its own offer.
AUTO_ROLLBACK_THRESHOLD = 3
SNAP_PATHS = ["backend", "config.json", "VERSION"]


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_stats(root: Path) -> tuple[int, int]:
    files = bytes_ = 0
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                files += 1
                try:
                    bytes_ += p.stat().st_size
                except OSError:
                    pass
    return files, bytes_


class LkgStore:
    def __init__(self, app_dir: Path, store_dir: Path) -> None:
        self.app_dir = Path(app_dir)
        self.root = Path(store_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def rollback_flag(self) -> Path:
        return self.root / "rollback.flag"

    @property
    def update_flag(self) -> Path:
        return self.root / "update.flag"

    def _manifest(self, snap: Path) -> dict:
        try:
            return json.loads(
                (snap / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------
    def snapshot(self, *, label: str = "") -> dict:
        """Copy backend/ + config.json + VERSION into a timestamped snap."""
        with self._lock:
            snap = self.root / f"snap-{int(time.time() * 1000)}"
            n = 0
            while snap.exists():
                n += 1
                snap = self.root / f"snap-{int(time.time() * 1000)}-{n}"
            snap.mkdir(parents=True, exist_ok=True)
            copied = []
            total_bytes = 0
            for rel in SNAP_PATHS:
                src = self.app_dir / rel
                dst = snap / rel
                if src.is_dir():
                    shutil.copytree(src, dst)
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                else:
                    continue
                files, bytes_ = _dir_stats(dst) if dst.is_dir() \
                    else (1, dst.stat().st_size)
                total_bytes += bytes_
                copied.append({"path": rel, "files": files,
                               "bytes": bytes_})
            exe = snap / "backend" / "ChatNexus.Backend.exe"
            version = ""
            try:
                version = (snap / "VERSION").read_text().strip()
            except OSError:
                pass
            manifest = {
                "label": str(label)[:160],
                "created_at": time.time(),
                "version": version,
                "paths": copied,
                "bytes": total_bytes,
                "exe_sha256": _sha(exe) if exe.is_file() else "",
            }
            atomic_write_text(snap / "manifest.json",
                              json.dumps(manifest, indent=2))
            atomic_write_text(self.root / "latest.txt", snap.name)
            self._prune()
            return {"ok": True, "name": snap.name, "version": version,
                    "paths": len(copied),
                    "bytes": total_bytes}

    def _prune(self) -> None:
        snaps = sorted(self.root.glob("snap-*"))
        while len(snaps) > KEEP_SNAPSHOTS:
            old = snaps.pop(0)
            try:
                shutil.rmtree(old)
            except OSError:
                break

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.glob("snap-*"), reverse=True):
            m = self._manifest(d)
            out.append({"name": d.name,
                        "label": m.get("label") or "",
                        "version": m.get("version") or "",
                        "created_at": m.get("created_at") or 0,
                        "bytes": m.get("bytes") or 0,
                        "paths": len(m.get("paths") or [])})
        return out

    def latest(self) -> str:
        try:
            return (self.root / "latest.txt").read_text().strip()
        except OSError:
            return ""

    def verify(self, name: str) -> dict:
        snap = self.root / name
        if not snap.is_dir():
            return {"ok": False, "reason": "unknown snapshot"}
        m = self._manifest(snap)
        if not m:
            return {"ok": False, "reason": "manifest missing or unreadable"}
        for entry in m.get("paths") or []:
            p = snap / str(entry.get("path") or "")
            if not p.exists():
                return {"ok": False,
                        "reason": f"missing path: {entry.get('path')}"}
        exe_sha = str(m.get("exe_sha256") or "")
        exe = snap / "backend" / "ChatNexus.Backend.exe"
        if exe_sha:
            if not exe.is_file():
                return {"ok": False, "reason": "backend exe missing"}
            if _sha(exe) != exe_sha:
                return {"ok": False, "reason": "backend exe hash mismatch"}
        return {"ok": True}

    # ------------------------------------------------------------------
    # rollback coordination
    # ------------------------------------------------------------------
    def request_rollback(self, name: str = "", *, reason: str = "") -> dict:
        """Ask the host to restore a verified snapshot at next launch."""
        name = name or self.latest()
        v = self.verify(name) if name else {"ok": False,
                                            "reason": "no snapshots"}
        if not v.get("ok"):
            return {"ok": False,
                    "reason": f"snapshot not restorable: {v.get('reason')}"}
        with self._lock:
            atomic_write_text(self.rollback_flag, json.dumps({
                "name": name, "reason": str(reason)[:300],
                "requested_at": time.time()}))
        return {"ok": True, "name": name, "staged": True,
                "detail": ("Rollback applies the next time Nexus Core "
                           "starts — the running exe cannot replace itself.")}

    def consume_rollback(self) -> dict | None:
        """Host-side: read + clear the rollback request. Returns the flag
        payload or None. Clearing first guarantees a failed restore cannot
        loop forever."""
        with self._lock:
            try:
                data = json.loads(
                    self.rollback_flag.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            try:
                self.rollback_flag.unlink()
            except OSError:
                pass
            return data

    def apply_rollback(self, name: str) -> dict:
        """Copy a verified snapshot back over the install. Safe to call only
        when the backend is NOT running (host, dev mode, or tests)."""
        snap = self.root / name
        v = self.verify(name)
        if not v.get("ok"):
            return {"ok": False, "reason": f"snapshot corrupt: {v.get('reason')}"}
        restored = []
        for entry in (self._manifest(snap).get("paths") or []):
            rel = str(entry.get("path") or "")
            src = snap / rel
            dst = self.app_dir / rel
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            else:
                continue
            restored.append(rel)
        return {"ok": True, "name": name, "restored": restored}

    # ------------------------------------------------------------------
    # staged-update coordination
    # ------------------------------------------------------------------
    def request_update(self, staged_dir: str = "backend-new",
                       *, version: str = "") -> dict:
        """Tell the host a rebuilt backend waits in ``staged_dir``."""
        staged = self.app_dir / staged_dir
        if not (staged / "ChatNexus.Backend.exe").is_file():
            return {"ok": False,
                    "reason": f"{staged_dir}/ChatNexus.Backend.exe missing"}
        with self._lock:
            atomic_write_text(self.update_flag, json.dumps({
                "staged_dir": staged_dir, "version": str(version),
                "requested_at": time.time()}))
        return {"ok": True, "staged_dir": staged_dir,
                "version": str(version)}

    def consume_update(self) -> dict | None:
        with self._lock:
            try:
                data = json.loads(
                    self.update_flag.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            try:
                self.update_flag.unlink()
            except OSError:
                pass
            return data

    def status(self) -> dict:
        pending_rb = pending_up = None
        try:
            pending_rb = json.loads(
                self.rollback_flag.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            pending_up = json.loads(
                self.update_flag.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return {"snapshots": self.list(), "latest": self.latest(),
                "pending_rollback": pending_rb,
                "pending_update": pending_up}
