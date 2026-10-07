"""Update discovery for manifest tools.

Probes the real package source each tool came from — winget upgrade
listings, pip index, or the latest GitHub release for archive installs —
and caches results at ``<install_root>/.agent/update_check.json`` so the
tools payload stays fast between checks.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from ..fsutil import atomic_write_text
from ..procutil import no_window_flags
from typing import Any

log = logging.getLogger("chatnexus.updates")

_GH_RELEASE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/", re.I)


class ToolUpdateChecker:
    """Runs per-tool update probes and persists what they found."""

    def __init__(self, install_root: Path) -> None:
        self.cache_path = Path(install_root) / ".agent" / "update_check.json"
        self._cache: dict[str, dict] = {}
        self.load()

    # ------------------------------------------------------------- cache --
    def load(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._cache = {k: v for k, v in data.items()
                               if isinstance(v, dict)}
        except (OSError, ValueError):
            self._cache = {}

    def save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.cache_path, json.dumps(self._cache, indent=1))
        except OSError as exc:
            log.warning("update cache write failed: %s", exc)

    def info(self, tool_id: str) -> dict:
        """Latest cached probe result for a tool, or {} if never checked."""
        return dict(self._cache.get(tool_id) or {})

    # ------------------------------------------------------------ probes --
    @staticmethod
    def _winget_latest(package: str) -> str | None:
        out = subprocess.run(
            ["winget", "upgrade", "--id", package, "-e",
             "--accept-source-agreements"],
            capture_output=True, text=True, timeout=120,
            creationflags=no_window_flags())
        text = f"{out.stdout}\n{out.stderr}"
        if "No applicable upgrade" in text or out.returncode in (-1978335189,):
            return "current"
        # Table columns: Name  Id  Version  Available  Source — find the
        # row whose Id column matches the package.
        for line in text.splitlines():
            if package.lower() in line.lower():
                cols = line.split()
                try:
                    idx = cols.index(package)
                except ValueError:
                    idx = next((i for i, c in enumerate(cols)
                                if c.lower() == package.lower()), -1)
                if idx >= 0 and len(cols) > idx + 2:
                    return cols[idx + 2]
        return None

    @staticmethod
    def _pip_latest(package: str, python: str | None) -> str | None:
        if not python:
            return None
        out = subprocess.run(
            [python, "-m", "pip", "index", "versions", package],
            capture_output=True, text=True, timeout=120,
            creationflags=no_window_flags())
        m = re.search(r"\(([^)]+)\)", out.stdout or "")
        return m.group(1) if m else None

    @staticmethod
    def _github_latest(url: str) -> str | None:
        m = _GH_RELEASE.match(url or "")
        if not m:
            return None
        owner, repo, _tag = m.groups()
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
            headers={"User-Agent": "chat-nexus", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "")
        return tag or None

    def probe(self, tool_id: str, install: dict[str, Any] | None,
              *, python: str | None = None) -> dict:
        """Run the probe for one tool; returns and caches the result."""
        install = install or {}
        method = str(install.get("method") or "").strip().lower()
        package = str(install.get("package") or "").strip()
        result: dict[str, Any] = {"checked_at": time.time(), "source": method}
        try:
            if method == "winget" and package:
                latest = self._winget_latest(package)
            elif method in {"pip", "uv"} and package:
                latest = self._pip_latest(package, python)
            elif method == "archive":
                latest = self._github_latest(str(install.get("url") or ""))
            else:
                result["status"] = "unavailable"
                latest = None
            if latest == "current":
                result.update(status="current", latest="")
            elif latest:
                result.update(status="checked", latest=latest)
            elif "status" not in result:
                result["status"] = "unavailable"
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            result.update(status="error", error=str(exc)[:200])
        self._cache[tool_id] = result
        return result
