"""Safe Mode + golden-configuration snapshots.

Two durable pieces:

`SafeModeStore` (`data/safe_mode.json`) — counts consecutive unclean
boots; after OFFER_THRESHOLD it recommends Safe Mode. Entering Safe
Mode disables heavy startup paths (model autostart, plugins,
autonomous workers, watchers, scheduled jobs) while keeping the core
UI, diagnostics, logs, config, and repair tools reachable. It never
deletes or resets user data.

`GoldenConfigStore` (`data/golden/`) — snapshots of whitelisted
config/state files (config.json, autonomy control, resource policies)
with a sha256 manifest. `restore` copies a verified snapshot back;
tampered snapshots fail restore rather than applying corrupt config.
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

OFFER_THRESHOLD = 3

# Startup paths Safe Mode suppresses. Core UI, diagnostics, logs,
# config inspection, and repair tooling always stay available.
SAFE_DISABLED = [
    "model_autostart", "comfyui", "plugins", "autonomous_workers",
    "background_watchers", "scheduled_jobs",
]
SAFE_AVAILABLE = [
    "core_ui", "diagnostics", "logs", "config", "repair_tools",
]

# Files golden snapshots cover — relative to the workspace root.
GOLDEN_PATHS = [
    "config.json",
    "data/policies.json",
    "data/autonomy/control.json",
]


class SafeModeStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "active": False, "reason": "",
                         "since": 0.0, "consecutive_failures": 0,
                         "history": []}

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            self.data, indent=2, ensure_ascii=False, default=str))

    def record_boot(self, *, previous_clean: bool) -> None:
        """Called at startup after reading the prior session marker."""
        with self._lock:
            if previous_clean:
                self.data["consecutive_failures"] = 0
            else:
                self.data["consecutive_failures"] = \
                    int(self.data.get("consecutive_failures") or 0) + 1
            self._save()

    @property
    def consecutive_failures(self) -> int:
        return int(self.data.get("consecutive_failures") or 0)

    def should_offer(self) -> bool:
        """Repeated startup failures → offer Safe Mode (never auto-erase)."""
        return self.consecutive_failures >= OFFER_THRESHOLD

    def is_active(self) -> bool:
        return bool(self.data.get("active"))

    def enter(self, reason: str = "") -> dict:
        with self._lock:
            self.data["active"] = True
            self.data["reason"] = str(reason)[:300]
            self.data["since"] = time.time()
            self.data["history"].append({
                "event": "enter", "reason": str(reason)[:300],
                "time": time.time()})
            self.data["history"] = self.data["history"][-50:]
            self._save()
        return self.status()

    def exit(self) -> dict:
        with self._lock:
            self.data["active"] = False
            self.data["consecutive_failures"] = 0
            self.data["history"].append({"event": "exit",
                                         "time": time.time()})
            self.data["history"] = self.data["history"][-50:]
            self._save()
        return self.status()

    def disabled(self, feature: str) -> bool:
        return self.is_active() and feature in SAFE_DISABLED

    def status(self) -> dict:
        return {"active": self.is_active(),
                "reason": self.data.get("reason") or "",
                "since": self.data.get("since") or 0.0,
                "consecutive_failures": self.consecutive_failures,
                "should_offer": self.should_offer(),
                "disabled": SAFE_DISABLED if self.is_active() else [],
                "available": SAFE_AVAILABLE}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class GoldenConfigStore:
    """Verified snapshots of the whitelisted configuration set."""

    def __init__(self, root: Path, workspace: Path) -> None:
        self.root = Path(root)
        self.workspace = Path(workspace)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _manifest(self, snap_dir: Path) -> dict:
        try:
            return json.loads(
                (snap_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def snapshot(self, *, label: str = "",
                 extra_paths: list[str] | None = None) -> dict:
        """Copy the whitelisted config files into a timestamped dir."""
        paths = list(GOLDEN_PATHS) + [p for p in (extra_paths or [])
                                      if p not in GOLDEN_PATHS]
        snap = self.root / f"snap-{int(time.time() * 1000)}"
        n = 0
        while snap.exists():
            n += 1
            snap = self.root / f"snap-{int(time.time() * 1000)}-{n}"
        snap.mkdir(parents=True, exist_ok=True)
        files = {}
        for rel in paths:
            src = self.workspace / rel
            if not src.is_file():
                continue
            dst = snap / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            files[rel] = {"sha256": _sha(dst),
                          "size": dst.stat().st_size}
        manifest = {"label": str(label)[:120],
                    "created_at": time.time(), "files": files}
        atomic_write_text(snap / "manifest.json",
                          json.dumps(manifest, indent=2))
        atomic_write_text(self.root / "latest.txt", snap.name)
        return {"ok": True, "name": snap.name, "files": len(files)}

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.glob("snap-*"), reverse=True):
            m = self._manifest(d)
            out.append({"name": d.name,
                        "label": m.get("label") or "",
                        "created_at": m.get("created_at") or 0,
                        "files": len(m.get("files") or {})})
        return out[:50]

    def _verify_snapshot(self, snap: Path) -> tuple[bool, str]:
        m = self._manifest(snap)
        if not m:
            return False, "manifest missing or unreadable"
        for rel, info in (m.get("files") or {}).items():
            p = snap / rel
            if not p.is_file():
                return False, f"missing file: {rel}"
            if _sha(p) != info.get("sha256"):
                return False, f"hash mismatch: {rel}"
        return True, ""

    def verify(self, name: str) -> dict:
        snap = self.root / name
        ok, why = self._verify_snapshot(snap) if snap.is_dir() \
            else (False, "unknown snapshot")
        return {"ok": ok, "reason": why}

    def restore(self, name: str) -> dict:
        """Copy a verified snapshot back over the live config files."""
        snap = self.root / name
        if not snap.is_dir():
            return {"ok": False, "reason": "unknown snapshot"}
        ok, why = self._verify_snapshot(snap)
        if not ok:
            return {"ok": False, "reason": f"snapshot corrupt: {why}"}
        m = self._manifest(snap)
        restored = []
        for rel in m.get("files") or {}:
            dst = self.workspace / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snap / rel, dst)
            restored.append(rel)
        return {"ok": True, "restored": restored}
