"""Bounded candidate-instance validation.

A real canary launches the repaired Nexus on an isolated port with a
temporary state dir and compares health against the stable instance.
That is heavyweight, so the launcher is *injected* — the coordinator
calls `canary.check(incident, worktree)`; production wiring supplies a
launcher that spawns `python -m localcodeagent.server` on a free port,
and tests supply a fake. With no launcher the stage reports `skipped`
so promotion gating can decide whether a skipped canary is acceptable
(configurable per repair kind).
"""
from __future__ import annotations

import json
import socket
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Canary:
    def __init__(self, launcher: Callable[..., Any] | None = None):
        self._launcher = launcher

    def check(self, incident: dict, worktree: Path, *,
              timeout_s: float = 60.0) -> dict[str, Any]:
        if self._launcher is None:
            return {"ok": None, "skipped": True,
                    "reason": "no candidate launcher configured"}
        try:
            return self._launcher(incident, Path(worktree),
                                  timeout_s=timeout_s)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:400]}


def http_health(url: str, timeout: float = 5.0) -> dict[str, Any]:
    """GET a health endpoint; used by the production launcher."""
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()[:4096]
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        return {"ok": True, "status": r.status, "payload": payload,
                "elapsed_s": round(time.time() - t0, 3)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300],
                "elapsed_s": round(time.time() - t0, 3)}
