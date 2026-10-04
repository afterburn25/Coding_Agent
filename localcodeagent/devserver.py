"""Development-server lifecycle manager.

Dev servers are long-running background processes the agent or the
workspace UI starts inside a registered workspace. This manager keeps a
dedicated registry (separate from ad-hoc terminal jobs) so each server
carries:

- which workspace root it belongs to,
- the URL/port it bound (detected from its own log output),
- a liveness/health probe result,
- bounded log tail for diagnostics.

Only claims that are actually observed are reported: a server is
``serving`` only after its log produced a URL *and* a real HTTP probe
answered.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_text
from .tools.terminal import TerminalTracker

# Common dev-server "we are listening" announcements. Order matters:
# the first match wins, preferring a full URL over a bare port.
_URL_RES = [
    # "Local:   http://localhost:5173/" (vite), "ready on http://...",
    # "Available on http://127.0.0.1:5000"
    re.compile(r"(https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?(?:/[\w\-./]*)?)", re.I),
    # "listening on port 3000" / "serving at port 8080"
    re.compile(r"(?:listening|serving|started|running)[^\d]{0,30}port[:\s]+(\d{2,5})", re.I),
    # "port 3000" / "PORT=3000" bare announcements
    re.compile(r"\bport[:\s=]+(\d{2,5})\b", re.I),
]
_READY_HINTS = re.compile(
    r"ready|listening|serving|compiled|started server|application startup complete|"
    r"now listening|development server", re.I)
_FAIL_HINTS = re.compile(
    r"EADDRINUSE|address already in use|permission denied|"
    r"error: cannot find|traceback \(most recent call last\)", re.I)


class DevServerManager:
    """Tracks dev servers across registered workspaces.

    ``roots_fn`` returns the set of workspace roots a server may run in
    (primary workspace + WorkspaceManager-registered roots).
    """

    MAX_SERVERS = 8

    def __init__(
        self,
        tracker: TerminalTracker,
        meta_path: Path,
        roots_fn: Callable[[], list[Path]] | None = None,
    ) -> None:
        self.tracker = tracker
        self.meta_path = Path(meta_path)
        self._roots_fn = roots_fn or (lambda: [])
        self._lock = threading.RLock()
        self._servers: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------
    # persistence
    def _load(self) -> None:
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, dict) and v.get("job_id"):
                        self._servers[k] = v
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        atomic_write_text(
            self.meta_path,
            json.dumps(self._servers, indent=2, sort_keys=True),
        )

    # ------------------------------------------------------------------
    # helpers
    def _allowed_root(self, raw: str) -> Path:
        root = Path(raw).expanduser().resolve()
        allowed = [Path(r).resolve() for r in self._roots_fn()]
        if not any(root == a or a in root.parents for a in allowed):
            raise ValueError("dev servers must run inside a registered workspace")
        if not root.is_dir():
            raise ValueError(f"workspace root does not exist: {root}")
        return root

    def _log_tail(self, log_path: str, limit: int = 40) -> list[str]:
        try:
            lines = Path(log_path).read_text(
                encoding="utf-8", errors="replace").splitlines()
            return lines[-limit:]
        except OSError:
            return []

    def _detect_url(self, log_path: str) -> tuple[str, int | None]:
        """Parse recent log lines for a bound URL or port."""
        for line in reversed(self._log_tail(log_path, 120)):
            m = _URL_RES[0].search(line)
            if m:
                url = m.group(1).rstrip(".,);'\"")
                host = "127.0.0.1" if "0.0.0.0" in url or "[::1]" in url else None
                if host:
                    url = re.sub(r"(0\.0\.0\.0|\[::1\])", host, url)
                pm = re.search(r":(\d+)(?:/|$)", url)
                return url, int(pm.group(1)) if pm else None
            m = _URL_RES[1].search(line) or _URL_RES[2].search(line)
            if m:
                port = int(m.group(1))
                return f"http://127.0.0.1:{port}/", port
        return "", None

    @staticmethod
    def probe(url: str, timeout: float = 3.0) -> dict[str, Any]:
        """Real HTTP probe — a server only counts as serving if it
        answers. Never fabricate this."""
        if not url:
            return {"ok": False, "reason": "no url detected"}
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read(256)
                return {"ok": True, "status": r.status}
        except Exception as exc:  # noqa: BLE001 — report real error
            return {"ok": False, "reason": str(exc)[:200]}

    # ------------------------------------------------------------------
    # lifecycle
    def start(self, root: str, command: str, *, name: str = "") -> dict[str, Any]:
        try:
            root_p = self._allowed_root(root)
        except ValueError as exc:
            return {"error": str(exc)}
        command = str(command).strip()
        if not command:
            return {"error": "command is required"}
        with self._lock:
            if len([s for s in self._servers.values() if s["alive"]]) >= self.MAX_SERVERS:
                return {"error": f"dev-server limit reached ({self.MAX_SERVERS})"}
            for sid, s in self._servers.items():
                if s["root"] == str(root_p) and s["command"] == command and s.get("alive"):
                    return {"error": "server already running",
                            "server": self._row(sid)}
            sid = f"dev-{int(time.time() * 1000) % 10**10}"
            argv = _shell_argv(command)
            info = self.tracker.spawn(
                argv, cwd=root_p, env=None, job_id=sid, command=command)
            self._servers[sid] = {
                "job_id": info["job_id"], "pid": info["pid"],
                "root": str(root_p), "command": command,
                "name": name or root_p.name,
                "started_at": time.time(), "log_path": info["log_path"],
                "alive": True,
            }
            self._save()
            return {"ok": True, "server": self._row(sid)}

    def _refresh(self, sid: str) -> dict[str, Any]:
        s = self._servers[sid]
        for row in self.tracker.list():
            if row["job_id"] == s["job_id"]:
                s["alive"] = row["state"] == "running"
                s["exit_code"] = row["exit_code"]
                break
        else:
            s["alive"] = False
        url, port = self._detect_url(s["log_path"])
        s["url"] = url
        s["port"] = port
        tail = self._log_tail(s["log_path"], 30)
        if any(_FAIL_HINTS.search(l) for l in tail):
            s["hint"] = "failure signature detected in log"
        return s

    def _row(self, sid: str) -> dict[str, Any]:
        s = dict(self._servers[sid])
        s["id"] = sid
        if s.get("alive") and s.get("url"):
            s["health"] = self.probe(s["url"])
            s["state"] = "serving" if s["health"].get("ok") else "starting"
        elif s.get("alive"):
            s["state"] = "starting"
            s["health"] = {"ok": False, "reason": "url not detected yet"}
        else:
            s["state"] = "stopped"
            s["health"] = {"ok": False, "reason": "process exited"}
        return s

    def list(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            for sid in self._servers:
                if refresh:
                    self._refresh(sid)
            return [self._row(sid) for sid in
                    sorted(self._servers, key=lambda k: -self._servers[k]["started_at"])]

    def stop(self, sid: str) -> dict[str, Any]:
        with self._lock:
            if sid not in self._servers:
                return {"error": f"unknown dev server '{sid}'"}
            job = self._servers[sid]["job_id"]
            try:
                res = self.tracker.kill(job)
            except KeyError:
                res = {"job_id": job, "exit_code": None}
            self._servers[sid]["alive"] = False
            self._save()
            return {"ok": True, "server": self._row(sid), "stopped": res}

    def restart(self, sid: str) -> dict[str, Any]:
        with self._lock:
            if sid not in self._servers:
                return {"error": f"unknown dev server '{sid}'"}
            s = self._servers[sid]
            if s.get("alive"):
                try:
                    self.tracker.kill(s["job_id"])
                except KeyError:
                    pass
            new = f"dev-{int(time.time() * 1000) % 10**10}"
            argv = _shell_argv(s["command"])
            info = self.tracker.spawn(
                argv, cwd=Path(s["root"]), env=None,
                job_id=new, command=s["command"])
            s.update({"job_id": info["job_id"], "pid": info["pid"],
                      "log_path": info["log_path"], "started_at": time.time(),
                      "alive": True})
            self._servers[new] = dict(s)
            del self._servers[sid]
            self._save()
            return {"ok": True, "server": self._row(new)}

    def logs(self, sid: str, limit: int = 80) -> dict[str, Any]:
        with self._lock:
            if sid not in self._servers:
                return {"error": f"unknown dev server '{sid}'"}
            s = self._servers[sid]
            return {"id": sid, "lines": self._log_tail(s["log_path"], limit)}

    def wait_for_url(self, sid: str, timeout: float = 30.0) -> dict[str, Any]:
        """Block until the server announces a URL and answers a probe,
        the process dies, or the deadline passes. Honest result."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if sid not in self._servers:
                    return {"ok": False, "error": "server disappeared"}
                s = self._refresh(sid)
            if not s.get("alive"):
                return {"ok": False, "error": "process exited",
                        "log_tail": self._log_tail(s["log_path"], 15)}
            if s.get("url"):
                probe = self.probe(s["url"])
                if probe.get("ok"):
                    return {"ok": True, "url": s["url"],
                            "port": s["port"], "status": probe.get("status")}
            time.sleep(0.5)
        return {"ok": False, "error": "timed out waiting for server",
                "log_tail": self._log_tail(self._servers[sid]["log_path"], 15)}

    def shutdown(self) -> None:
        with self._lock:
            for sid, s in self._servers.items():
                if s.get("alive"):
                    try:
                        self.tracker.kill(s["job_id"])
                    except KeyError:
                        pass
            self._save()


def _is_windows() -> bool:
    import sys
    return sys.platform.startswith("win")


def _shell_argv(command: str):
    """Shell invocation as a raw command-line string. Popen uses a
    string verbatim on Windows, which avoids list2cmdline
    double-quoting a command that itself starts with a quoted path
    (e.g. '"C:\\python.exe" -m http.server')."""
    if _is_windows():
        return f'cmd /s /c "{command}"'
    return ["sh", "-c", command]
