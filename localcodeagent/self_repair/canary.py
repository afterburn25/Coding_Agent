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
import os
import socket
import subprocess
import sys
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


def production_launcher(*, state_parent: Path,
                        python_exe: str | None = None) -> Callable:
    """Build a launcher that runs the *candidate* Nexus from the incident
    worktree — `python -m localcodeagent` with PYTHONPATH pointed at the
    worktree, a free port, and a scratch config so all runtime state
    (data/, brain, autonomy stores) lands under
    `state_parent/canary/<incident>/` — never the live state.

    Verification is bounded: health endpoint, an optional replay of the
    original failing request path (when the incident recorded one), then
    the process is terminated. The candidate never shares the stable
    instance's port, workspace, or state.
    """
    exe = python_exe or sys.executable

    def _launch(incident: dict, worktree: Path,
                timeout_s: float = 60.0) -> dict[str, Any]:
        port = free_port()
        iid = str(incident.get("id") or "unknown")
        state_dir = Path(state_parent) / "canary" / iid
        state_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = state_dir / "config.json"
        if not cfg_path.exists():
            # Minimal config → defaults resolve under the scratch root.
            cfg_path.write_text("{}", encoding="utf-8")
        env = dict(os.environ)
        # Replace — not prepend — PYTHONPATH: the candidate must resolve
        # localcodeagent only from the worktree. An inherited entry pointing
        # at the stable repo would silently boot the wrong code and the
        # canary would validate nothing (also breaks any test harness that
        # exports PYTHONPATH to run the suite, e.g. selftest).
        env["PYTHONPATH"] = str(worktree)
        kwargs: dict[str, Any] = {}
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            [exe, "-m", "localcodeagent",
             "--workspace", str(worktree),
             "--config", str(cfg_path),
             "--port", str(port)],
            cwd=str(worktree), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **kwargs)
        try:
            deadline = time.time() + timeout_s
            health: dict[str, Any] = {}
            while time.time() < deadline:
                if proc.poll() is not None:
                    return {"ok": False, "port": port,
                            "error": "candidate process exited",
                            "exit_code": proc.returncode}
                health = http_health(
                    f"http://127.0.0.1:{port}/api/health", timeout=2.0)
                if health.get("ok"):
                    break
                time.sleep(0.5)
            checks = {"health": health}
            # Replay the original failing request when the incident
            # recorded one — the strongest promotion signal.
            replay_path = str(
                (incident.get("reproduction") or {}).get("path") or "")
            if health.get("ok") and replay_path.startswith("/api/"):
                checks["replay"] = http_health(
                    f"http://127.0.0.1:{port}{replay_path}", timeout=5.0)
            ok = bool(health.get("ok")) and all(
                c.get("ok") for c in checks.values())
            return {"ok": ok, "port": port, "checks": checks,
                    "isolated_state": str(state_dir)}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    return _launch
