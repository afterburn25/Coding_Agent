"""Canonical Nexus Core version.

The single source of truth is the top-level ``VERSION`` file. Everything
else — pyproject, installer, .NET project, backend version resource,
status API — derives from it via ``scripts/sync_version.py``.
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"

_DEV_FALLBACK = "0.0.0-dev"


def _read_version_file() -> str:
    """Locate VERSION — beside the repo in dev, in _MEIPASS when frozen."""
    candidates = [VERSION_FILE]
    base = getattr(sys, "_MEIPASS", None)  # PyInstaller bundle
    if base:
        candidates.append(Path(base) / "VERSION")
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


@lru_cache(maxsize=1)
def version() -> str:
    """Read the canonical version string (e.g. ``0.7.0``)."""
    raw = _read_version_file()
    if re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?", raw):
        return raw
    return _DEV_FALLBACK


@lru_cache(maxsize=1)
def version_tuple() -> tuple[int, int, int, int]:
    """Numeric 4-tuple for Windows version resources (e.g. (0,7,0,0))."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", version())
    if not m:
        return (0, 0, 0, 0)
    return (int(m[1]), int(m[2]), int(m[3]), 0)


def user_agent() -> str:
    return f"NexusCore/{version()}"
