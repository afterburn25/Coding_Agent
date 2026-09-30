from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .comfyui import ComfyUIBackend


@dataclass(slots=True)
class ComfyRuntimeStatus:
    state: str = "external"
    pid: int | None = None
    managed: bool = False
    healthy: bool = False
    error: str = ""
    log_path: str = ""
    restarts: int = 0

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "pid": self.pid,
            "managed": self.managed,
            "healthy": self.healthy,
            "error": self.error,
            "log_path": self.log_path,
            "restarts": self.restarts,
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
        dirs.extend([self.base_dir / "ComfyUI", self.base_dir / "comfyui"])
        for d in dirs:
            if (d / "main.py").is_file():
                py = str(getattr(self.config, "comfyui_python", "")).strip() or shutil.which("python") or shutil.which("python3")
                return d.resolve(), py
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

    def ensure_ready(self) -> None:
        with self._lock:
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
                    return
                self.stop()
            cmd, cwd = self._command()
            logs = self._resolve(str(getattr(self.config, "comfyui_logs_dir", ".agent/runtime")))
            logs.mkdir(parents=True, exist_ok=True)
            log_path = logs / "comfyui.log"
            self._log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
            flags = 0
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags = subprocess.CREATE_NO_WINDOW
            self._process = subprocess.Popen(cmd, cwd=str(cwd), stdout=self._log_handle, stderr=subprocess.STDOUT, text=True, creationflags=flags)
            self.status = ComfyRuntimeStatus(state="loading", pid=self._process.pid, managed=True, healthy=False, log_path=str(log_path), restarts=self.status.restarts)
            deadline = time.monotonic() + max(10, int(getattr(self.config, "comfyui_startup_timeout", 180)))
            last = ""
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    self.status.state = "error"
                    self.status.error = f"ComfyUI exited with code {self._process.returncode}; see {log_path}"
                    self.status.pid = None
                    raise RuntimeError(self.status.error)
                healthy, last = self.backend.health()
                if healthy:
                    self.status.state = "running"
                    self.status.healthy = True
                    return
                time.sleep(0.5)
            self.status.state = "error"
            self.status.error = f"Timed out waiting for ComfyUI: {last[:300]}"
            self.stop()
            raise TimeoutError(self.status.error)

    def stop(self) -> None:
        with self._lock:
            p = self._process
            self._process = None
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait(timeout=3)
            if self._log_handle:
                try: self._log_handle.close()
                except Exception: pass
                self._log_handle = None
            self.status.state = "stopped"
            self.status.healthy = False
            self.status.pid = None

    def recover(self) -> None:
        with self._lock:
            self.status.restarts += 1
            self.stop()
            self.start()

    def probe(self) -> dict:
        healthy, detail = self.backend.health()
        self.status.healthy = healthy
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
