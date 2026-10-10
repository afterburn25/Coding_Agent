"""Project environment manifests.

`EnvironmentStore` (`data/environment.json`) records what a project
needs — runtime/toolchain components with version requirements — and
verifies the live host against them. `detect()` probes the real host
(python, node, dotnet, git, plus arbitrary tools via an injectable
probe); `verify(project_id)` reports missing/mismatched components so
"recreate this environment" becomes a checklist, not guesswork.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_text
from .procutil import no_window_flags

MAX_COMPONENTS = 200

# name → (binary, version-args, regex) for built-in probes.
_BUILTIN_PROBES = {
    "python": (None, None, None),          # handled specially
    "node": ("node", ["--version"], r"v?(\d+\.\d+\.\d+)"),
    "dotnet": ("dotnet", ["--version"], r"(\d+\.\d+\.\d+)"),
    "git": ("git", ["--version"], r"(\d+\.\d+\.\d+)"),
    "npm": ("npm", ["--version"], r"(\d+\.\d+\.\d+)"),
    "pip": ("pip", ["--version"], r"pip (\d+\.\d+(?:\.\d+)?)"),
}


def _probe_builtin(name: str, timeout: float = 5.0) -> dict:
    """Detect one component. Returns {installed, version, path}."""
    if name == "python":
        return {"installed": True,
                "version": platform.python_version(),
                "path": sys.executable}
    spec = _BUILTIN_PROBES.get(name)
    binary = spec[0] if spec else name
    args = spec[1] if spec else ["--version"]
    pattern = spec[2] if spec else r"(\d+\.\d+[\d.]*)"
    exe = shutil.which(binary)
    if not exe:
        return {"installed": False, "version": "", "path": ""}
    try:
        out = subprocess.run(
            [exe] + list(args), capture_output=True, text=True,
            timeout=timeout, creationflags=no_window_flags(), encoding="utf-8", errors="replace")
        text = (out.stdout or "") + (out.stderr or "")
        m = re.search(pattern, text)
        return {"installed": True,
                "version": m.group(1) if m else text.strip()[:60],
                "path": exe}
    except (OSError, subprocess.SubprocessError):
        return {"installed": True, "version": "", "path": exe}


def _satisfies(installed: str, requirement: str) -> bool:
    """Requirement forms: '', '>=1.2', '==1.2.3', '1.x' prefix."""
    req = str(requirement or "").strip()
    if not req:
        return True
    if not installed:
        return False
    from .dependencies import _ver_tuple
    iv = _ver_tuple(installed)
    if not iv:
        return False
    if req.startswith(">="):
        rv = _ver_tuple(req[2:])
    elif req.startswith("=="):
        rv = _ver_tuple(req[2:])
        n = len(rv)
        return iv[:n] == rv
    elif req.endswith(".x") or req.endswith(".*"):
        rv = _ver_tuple(req[:-2])
        return bool(rv) and iv[:len(rv)] == rv
    else:
        rv = _ver_tuple(req)
        n = len(rv)
        return iv[:n] == rv
    return bool(rv) and iv >= rv


class EnvironmentStore:
    def __init__(self, path: Path,
                 probe: Callable[[str], dict] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._probe = probe or _probe_builtin
        self._lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "manifests": {}}
        self.data.setdefault("manifests", {})

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            self.data, indent=2, ensure_ascii=False, default=str))

    # -- declared manifests ---------------------------------------------------
    def declare(self, project_id: str, components: list[dict]) -> dict:
        """Store a project's required environment.
        Each component: {name, requirement, kind}."""
        rows = []
        for c in list(components or [])[:MAX_COMPONENTS]:
            rows.append({
                "name": str(c.get("name") or "")[:80],
                "requirement": str(c.get("requirement") or "")[:60],
                "kind": str(c.get("kind") or "tool")[:40]})
        self.data["manifests"][str(project_id)] = {
            "components": rows, "declared_at": time.time()}
        self._save()
        return dict(self.data["manifests"][str(project_id)],
                    project_id=project_id)

    def manifest(self, project_id: str) -> dict | None:
        m = self.data["manifests"].get(str(project_id))
        return dict(m, project_id=project_id) if m else None

    # -- live detection ----------------------------------------------------------
    def detect(self, names: list[str] | None = None) -> dict:
        """Probe the host for the requested (or built-in) components.

        Probes run concurrently: each is an independent subprocess with
        its own timeout, and serial execution stacks worst case to
        len(targets) × probe timeout — beyond typical client timeouts.
        """
        targets = names or sorted(_BUILTIN_PROBES)
        out = {}

        def _one(name: str) -> dict:
            try:
                return self._probe(name)
            except Exception:
                return {"installed": False, "version": "", "path": ""}

        with cf.ThreadPoolExecutor(max_workers=4) as pool:
            for name, det in zip(targets[:50],
                                 pool.map(_one, targets[:50])):
                out[name] = det
        return {"detected_at": time.time(), "components": out,
                "os": platform.platform()}

    # -- verification --------------------------------------------------------------
    def verify(self, project_id: str) -> dict:
        m = self.data["manifests"].get(str(project_id))
        if m is None:
            return {"ok": False, "reason": "no manifest declared",
                    "project_id": project_id}
        missing, mismatched, ok_rows = [], [], []
        for c in m.get("components") or []:
            name = c.get("name") or ""
            req = c.get("requirement") or ""
            try:
                det = self._probe(name)
            except Exception:
                det = {"installed": False, "version": "", "path": ""}
            if not det.get("installed"):
                missing.append(name)
            elif not _satisfies(str(det.get("version") or ""), req):
                mismatched.append({"name": name, "required": req,
                                   "installed": det.get("version")})
            else:
                ok_rows.append({"name": name,
                                "version": det.get("version")})
        return {"ok": not missing and not mismatched,
                "project_id": str(project_id),
                "satisfied": ok_rows, "missing": missing,
                "mismatched": mismatched,
                "verified_at": time.time()}
