from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .comfyui import ComfyUIBackend
from ..fsutil import atomic_write_text
from ..procutil import no_window_flags


@dataclass(slots=True)
class ComfyRuntimeStatus:
    state: str = "external"
    pid: int | None = None
    managed: bool = False
    healthy: bool = False
    installed: bool = True
    error: str = ""
    log_path: str = ""
    restarts: int = 0
    started_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "pid": self.pid,
            "managed": self.managed,
            "healthy": self.healthy,
            "installed": self.installed,
            "error": self.error,
            "log_path": self.log_path,
            "restarts": self.restarts,
            "started_at": self.started_at,
        }


class ComfyUIRuntime:
    """Optionally owns a local ComfyUI process while also supporting external ComfyUI."""

    def __init__(self, *, base_dir: Path, backend: ComfyUIBackend, config, extra_model_paths_config: Path | None = None) -> None:
        self.base_dir = base_dir.resolve()
        self.backend = backend
        self.config = config
        self.extra_model_paths_config = extra_model_paths_config.resolve() if extra_model_paths_config else None
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._lock = threading.RLock()
        self.status = ComfyRuntimeStatus()
        if not getattr(config, "comfyui_auto_start", False):
            self.status.state = "external"

    def _resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p.resolve() if p.is_absolute() else (self.base_dir / p).resolve()

    def discover(self) -> tuple[Path | None, str | None]:
        configured = str(getattr(self.config, "comfyui_dir", "")).strip()
        dirs = []
        if configured:
            dirs.append(self._resolve(configured))
        dirs.extend([
            # Canonical layout: tool payloads live under {app}/tools/.
            self.base_dir / "tools" / "ComfyUI_windows_portable" / "ComfyUI",
            self.base_dir / "tools" / "ComfyUI",
            # Legacy layout — installs before the tools/ re-home.
            self.base_dir / "ComfyUI",
            self.base_dir / "comfyui",
            self.base_dir / "ComfyUI_windows_portable" / "ComfyUI",
        ])
        configured_python = str(getattr(self.config, "comfyui_python", "")).strip()
        for d in dirs:
            if not (d / "main.py").is_file():
                continue
            py = configured_python
            if not py:
                portable_python = d.parent / "python_embeded" / "python.exe"
                if portable_python.is_file():
                    py = str(portable_python.resolve())
                else:
                    py = shutil.which("python") or shutil.which("python3") or ""
            return d.resolve(), py or None
        return None, None

    def _command(self) -> tuple[list[str], Path]:
        directory, python = self.discover()
        if not directory or not python:
            raise RuntimeError("ComfyUI was not found. Set comfyui_dir to a ComfyUI checkout containing main.py.")
        parts = urlsplit(self.backend.endpoint)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 8188
        cmd = [python, "main.py", "--listen", host, "--port", str(port)]
        if self.extra_model_paths_config and self.extra_model_paths_config.is_file():
            cmd.extend(["--extra-model-paths-config", str(self.extra_model_paths_config)])
        extra = list(getattr(self.config, "comfyui_extra_args", []) or [])
        cmd.extend(str(x) for x in extra)
        return cmd, directory

    def _managed_marker_path(self) -> Path:
        return self._resolve(str(getattr(self.config, "comfyui_logs_dir", ".agent/runtime"))) / "comfyui-managed.json"

    def _orphaned_managed_pid(self) -> int | None:
        """PID of a ComfyUI this app spawned that outlived a dead backend.

        The marker records the exact python executable path so a PID that was
        reused by an unrelated process (or a user's own ComfyUI on the same
        port) is never mistaken for our orphan.
        """
        try:
            data = json.loads(self._managed_marker_path().read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            exe = str(data.get("exe") or "")
        except Exception:
            return None
        if pid <= 0 or not exe:
            return None
        return pid if self._pid_cmdline_matches(pid, exe) else None

    def _pid_cmdline_matches(self, pid: int, exe: str) -> bool:
        try:
            if os.name == "nt":
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=no_window_flags(),
                ).stdout
            else:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                out = raw.replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:
            return False
        return "main.py" in out and exe.lower() in out.lower()

    def _kill_orphan(self, pid: int) -> None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10,
                               creationflags=no_window_flags())
            else:
                os.kill(pid, 9)
        except Exception:
            pass

    def ensure_ready(self) -> None:
        with self._lock:
            healthy, detail = self.backend.health()
            if healthy and self._process is None:
                # A ComfyUI this app spawned may have survived a backend
                # restart — it would be adopted as "external" and escape idle
                # eviction forever while still holding VRAM. Reclaim it so the
                # managed lifecycle owns it again.
                orphan = self._orphaned_managed_pid()
                if orphan is not None:
                    self._kill_orphan(orphan)
                    try:
                        self._managed_marker_path().unlink(missing_ok=True)
                    except OSError:
                        pass
                    healthy, detail = self.backend.health()
            if healthy:
                self.status.healthy = True
                self.status.state = "running" if self._process else "external"
                return
            auto_start = bool(getattr(self.config, "comfyui_auto_start", False))
            start_on_request = bool(getattr(self.config, "comfyui_start_on_image_request", True))
            if not auto_start and not start_on_request:
                self.status.healthy = False
                self.status.state = "external_offline"
                self.status.error = detail[:300]
                raise RuntimeError(f"ComfyUI backend is offline: {detail}")

            if not auto_start and start_on_request:
                directory, python = self.discover()
                if not directory or not python:
                    self.status.healthy = False
                    self.status.state = "setup_required"
                    self.status.error = (
                        "ComfyUI is offline and no local ComfyUI checkout was found. "
                        "Install/configure ComfyUI or set comfyui_dir to a checkout containing main.py."
                    )
                    raise RuntimeError(self.status.error)

            self.start()

    def start(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                healthy, _ = self.backend.health()
                if healthy:
                    self.status.state = "running"
                    self.status.healthy = True
                    return
                # A half-booted ComfyUI keeps making progress — killing it on
                # every timed-out request just restarts the cold boot. Keep
                # waiting on the same process; only restart once it has
                # exceeded its total boot budget (2x the startup timeout).
                budget = 2 * max(10, int(getattr(self.config, "comfyui_startup_timeout", 180)))
                if time.time() - (self.status.started_at or time.time()) > budget:
                    self.stop()
            if not (self._process and self._process.poll() is None):
                self._spawn()
            self._wait_ready()

    def _spawn(self) -> None:
        cmd, cwd = self._command()
        logs = self._resolve(str(getattr(self.config, "comfyui_logs_dir", ".agent/runtime")))
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / "comfyui.log"
        try:
            # Bound the append-only process log for unattended runs.
            if log_path.exists() and log_path.stat().st_size > 8 * 1024 * 1024:
                log_path.write_bytes(log_path.read_bytes()[-4 * 1024 * 1024:])
        except OSError:
            pass
        self._log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
        flags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        self._process = subprocess.Popen(cmd, cwd=str(cwd), stdout=self._log_handle, stderr=subprocess.STDOUT, text=True, creationflags=flags)
        try:
            # Marker lets a restarted backend recognize this process as our
            # orphan (exe path ties the pid to this install's ComfyUI).
            marker = self._managed_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(marker, json.dumps({"pid": self._process.pid, "exe": cmd[0]}))
        except OSError:
            pass
        self.status = ComfyRuntimeStatus(state="loading", pid=self._process.pid, managed=True, healthy=False, log_path=str(log_path), restarts=self.status.restarts, started_at=time.time())

    def _wait_ready(self) -> None:
        # Wait out the full boot budget (2x startup timeout since spawn) so a
        # request that arrives during a cold boot attaches to it instead of
        # erroring while ComfyUI is still initializing.
        started = self.status.started_at or time.time()
        budget = 2 * max(10, int(getattr(self.config, "comfyui_startup_timeout", 180)))
        deadline = started + budget
        last = ""
        while time.time() < deadline:
            if self._process is None or self._process.poll() is not None:
                code = self._process.returncode if self._process is not None else "?"
                self.status.state = "error"
                self.status.error = f"ComfyUI exited with code {code}; see {self.status.log_path}"
                self.status.pid = None
                raise RuntimeError(self.status.error)
            healthy, last = self.backend.health()
            if healthy:
                self.status.state = "running"
                self.status.healthy = True
                return
            time.sleep(0.5)
        # The process is alive but not yet serving — leave it booting (state
        # stays "loading") so the next request attaches to the same cold boot
        # instead of restarting it. The boot budget in start() bounds how long
        # a genuinely stuck process can hold on.
        self.status.error = f"ComfyUI is still starting: {last[:300]}"
        raise TimeoutError(self.status.error)

    def stop(self) -> None:
        with self._lock:
            p = self._process
            self._process = None
            if p and p.poll() is None:
                if os.name == "nt":
                    # Kill the whole tree — the embedded python may re-exec a
                    # child that would otherwise survive and hold port 8188.
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True, timeout=15,
                                   creationflags=no_window_flags())
                else:
                    p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait(timeout=3)
            if self._log_handle:
                try: self._log_handle.close()
                except Exception: pass
                self._log_handle = None
            try:
                self._managed_marker_path().unlink(missing_ok=True)
            except OSError:
                pass
            self.status.state = "stopped"
            self.status.healthy = False
            self.status.pid = None

    def evict_if_managed(self) -> bool:
        """Stop this backend only when Nexus owns the resident process.

        Two resident image servers can exhaust VRAM/RAM together, so a
        Nexus-managed backend is eligible for arbitration eviction — the
        next request brings it back via start_on_image_request. A
        user-owned external ComfyUI is never touched.
        Returns True when a Nexus-owned process was stopped."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self.stop()
                return True
            orphan = self._orphaned_managed_pid()
            if orphan is None:
                return False
            self._kill_orphan(orphan)
            try:
                self._managed_marker_path().unlink(missing_ok=True)
            except OSError:
                pass
            self.status.state = "stopped"
            self.status.healthy = False
            self.status.pid = None
            return True

    def recover(self) -> None:
        with self._lock:
            self.status.restarts += 1
            self.stop()
            self.start()

    def probe(self) -> dict:
        healthy, detail = self.backend.health()
        self.status.healthy = healthy
        directory, _python = self.discover()
        self.status.installed = directory is not None
        if self._process and self._process.poll() is not None:
            self.status.state = "error"
            self.status.pid = None
            self.status.error = f"ComfyUI exited with code {self._process.returncode}"
        elif self._process:
            self.status.state = "running" if healthy else "loading"
        elif healthy:
            self.status.state = "external"
        else:
            self.status.state = "external_offline" if not getattr(self.config, "comfyui_auto_start", False) else "stopped"
            self.status.error = detail[:300]
        return self.status.as_dict()
