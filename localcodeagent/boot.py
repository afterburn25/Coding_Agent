"""Backend startup-phase markers consumed by the Nexus Core desktop splash.

When the native host launches the backend it sets ``NEXUS_BOOT_MARKERS=1`` and
reads stdout. AppState emits ``[nexus-boot] {json}`` lines at real
initialization milestones; the host feeds them into the splash progress
coordinator, so the status text reflects genuine backend work instead of a
fabricated sequence.
"""

from __future__ import annotations

import json
import os
from typing import Callable

BootReporter = Callable[[float, str, str], None]

MARKER_PREFIX = "[nexus-boot] "
PORT_MARKER_PREFIX = "[nexus-port] "
ENV_FLAG = "NEXUS_BOOT_MARKERS"


def boot_report(pct: float, primary: str, secondary: str) -> None:
    """Emit one structured startup-phase marker on stdout."""
    payload = {
        "pct": max(0.0, min(100.0, float(pct))),
        "primary": primary,
        "secondary": secondary,
    }
    # ASCII-safe JSON: backend stdout may be decoded under the console OEM
    # codepage by the host, so non-ASCII characters must stay escaped.
    print(f"{MARKER_PREFIX}{json.dumps(payload, ensure_ascii=True)}", flush=True)


def port_report(port: int) -> None:
    """Emit the actual bound port on stdout.

    The host picks a candidate port before launching the backend, but the
    backend may fall back to a different port if the candidate became
    unavailable in the meantime. Without this marker the host would
    health-check a port the backend never bound — or worse, a foreign
    service squatting on it. Emitted unconditionally: unlike boot markers,
    any launcher (not just the desktop host) benefits from knowing where
    the server actually landed.
    """
    print(f"{PORT_MARKER_PREFIX}{int(port)}", flush=True)


def reporter_from_env() -> BootReporter | None:
    return boot_report if os.environ.get(ENV_FLAG) == "1" else None
