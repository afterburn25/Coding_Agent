from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..config import AgentConfig, ModelProfile
from .hardware import HardwareSnapshot, detect_hardware
from .catalog import CodingModelCatalogManager


@dataclass(slots=True)
class RuntimeStatus:
    model_id: str
    state: str
    endpoint: str
    pid: int | None = None
    managed: bool = False
    healthy: bool = False
    started_at: float | None = None
    restarts: int = 0
    error: str = ""
    log_path: str = ""

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "state": self.state,
            "endpoint": self.endpoint,
            "pid": self.pid,
            "managed": self.managed,
            "healthy": self.healthy,
            "started_at": self.started_at,
            "restarts": self.restarts,
            "error": self.error,
            "log_path": self.log_path,
        }


@dataclass(slots=True)
class _ManagedProcess:
    profile: ModelProfile
    process: subprocess.Popen
    endpoint: str
    log_handle: object
    status: RuntimeStatus


class RuntimeManager:
    """Owns local inference server lifecycles and exposes effective endpoints to the agent."""

    def __init__(self, config: AgentConfig, *, base_dir: Path | None = None) -> None:
        self.config = config
        self.base_dir = (base_dir or Path.cwd()).resolve()
        self.models_dir = self._resolve(config.models_dir)
        self.logs_dir = self._resolve(config.runtime_logs_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.model_catalog = CodingModelCatalogManager(self.models_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.hardware: HardwareSnapshot = detect_hardware()
        self._managed: dict[str, _ManagedProcess] = {}
        self._status: dict[str, RuntimeStatus] = {}
        self._lock = threading.RLock()
        self._last_used: dict[str, float] = {}
        for model in config.models:
            endpoint = self._profile_endpoint(model)
            self._status[model.id] = RuntimeStatus(
                model_id=model.id,
                state="external" if model.runtime == "external" else "stopped",
                endpoint=endpoint,
                managed=model.runtime != "external",
            )

    def _resolve(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (self.base_dir / path).resolve()

    @staticmethod
    def _port_from_endpoint(endpoint: str) -> int | None:
        if not endpoint:
            return None
        try:
            return urlsplit(endpoint).port
        except ValueError:
            return None

    def _profile_endpoint(self, profile: ModelProfile, port: int | None = None) -> str:
        if profile.endpoint:
            return profile.endpoint.rstrip("/")
        use_port = port or profile.port or 8080
        return f"http://{profile.host}:{use_port}/v1"

    @staticmethod
    def _health_url(endpoint: str) -> str:
        parts = urlsplit(endpoint)
        path = parts.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        return urlunsplit((parts.scheme, parts.netloc, f"{path}/health" or "/health", "", ""))

    @staticmethod
    def _find_free_port(host: str) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])

    def refresh_hardware(self) -> HardwareSnapshot:
        self.hardware = detect_hardware()
        return self.hardware

    def discover_llama_server(self, profile: ModelProfile | None = None) -> str | None:
        candidates: list[str] = []
        if profile and profile.executable:
            candidates.append(profile.executable)
        if self.config.llama_cpp_executable:
            candidates.append(self.config.llama_cpp_executable)
        bundled = self.base_dir / "runtime" / "llama"
        candidates.extend([
            str(bundled / "llama-server.exe"),
            str(bundled / "llama.exe"),
            str(bundled / "llama-server"),
            str(bundled / "llama"),
            "llama-server.exe", "llama-server", "llama.exe", "llama",
        ])
        for item in candidates:
            expanded = str(Path(item).expanduser())
            if os.path.isabs(expanded) and Path(expanded).is_file():
                return expanded
            found = shutil.which(item)
            if found:
                return found
            local = self.base_dir / item
            if local.is_file():
                return str(local.resolve())
        return None

    @staticmethod
    def _is_unified_llama(executable: str) -> bool:
        name = Path(executable).name.lower()
        return name in {"llama", "llama.exe"}

    def runtime_install_guidance(self) -> dict:
        installed = self.discover_llama_server()
        system = platform.system().lower()
        if system == "windows":
            commands = [{"label": "Winget", "command": "winget install llama.cpp"}, {"label": "Conda", "command": "conda install -c conda-forge llama.cpp"}]
        elif system == "darwin":
            commands = [{"label": "Homebrew", "command": "brew install llama.cpp"}, {"label": "Conda", "command": "conda install -c conda-forge llama.cpp"}]
        else:
            commands = [{"label": "Conda", "command": "conda install -c conda-forge llama.cpp"}, {"label": "Homebrew (Linuxbrew)", "command": "brew install llama.cpp"}]
        return {
            "installed": bool(installed),
            "executable": installed or "",
            "platform": platform.system(),
            "commands": commands,
            "note": "Install commands are shown for convenience and are never executed automatically by Chat Nexus.",
        }
    def inventory(self) -> list[dict]:
        rows: list[dict] = []
        if not self.models_dir.exists():
            return rows
        for path in sorted(self.models_dir.rglob("*.gguf")):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            rows.append({
                "name": path.name,
                "path": str(path),
                "size_gb": round(size / (1024 ** 3), 2),
            })
        return rows

    def resource_fit(self, profile: ModelProfile) -> tuple[bool, int, str]:
        """Return availability/resource fit for routing before a model is selected."""
        if profile.runtime == "llama_cpp":
            if not self.discover_llama_server(profile):
                return False, -200, "managed runtime executable is unavailable"
            if not profile.model_path:
                return False, -200, "managed GGUF path is not configured"
            model_path = self._resolve(profile.model_path)
            if not model_path.is_file():
                return False, -200, f"managed GGUF is not installed: {model_path.name}"
        h = self.hardware
        free_vram = h.free_vram_gb
        avail_ram = h.available_ram_gb
        required_vram = max(0.0, float(profile.estimated_vram_gb))
        required_ram = max(0.0, float(profile.estimated_ram_gb))

        # No estimates means we cannot reject it; let llama.cpp auto-fit decide.
        if required_vram <= 0 and required_ram <= 0:
            return True, 0, "no resource estimate; runtime auto-fit allowed"

        if required_ram > 0 and avail_ram > 0 and required_ram > avail_ram * 0.92:
            return False, -100, f"estimated RAM need {required_ram:.1f} GB exceeds available {avail_ram:.1f} GB"

        if required_vram > 0:
            if free_vram >= required_vram:
                return True, 25, f"fits free VRAM ({free_vram:.1f} GB)"
            if profile.allow_cpu_offload and (avail_ram <= 0 or required_ram <= avail_ram * 0.92):
                return True, -5, f"requires CPU offload; free VRAM {free_vram:.1f} GB"
            return False, -100, f"estimated VRAM need {required_vram:.1f} GB exceeds free {free_vram:.1f} GB"
        return True, 5, "fits available RAM"

    def _build_command(self, profile: ModelProfile, port: int) -> list[str]:
        exe = self.discover_llama_server(profile)
        if not exe:
            raise RuntimeError(
                "llama.cpp server was not found. Set llama_cpp_executable or the model profile executable, "
                "or add llama-server / the unified llama command to PATH."
            )
        if not profile.model_path:
            raise RuntimeError(f"Model profile '{profile.id}' has runtime=llama_cpp but no model_path.")
        model_path = self._resolve(profile.model_path)
        if not model_path.is_file():
            raise RuntimeError(f"Model file not found: {model_path}")

        cmd = [exe]
        if self._is_unified_llama(exe):
            cmd.append("serve")
        cmd.extend([
            "--model", str(model_path),
            "--host", profile.host,
            "--port", str(port),
            "--ctx-size", str(profile.context_window),
        ])
        if profile.gpu_layers:
            cmd.extend(["--gpu-layers", str(profile.gpu_layers)])
        if profile.threads > 0:
            cmd.extend(["--threads", str(profile.threads)])
        if profile.fit_target_mb > 0:
            cmd.extend(["--fit-target", str(profile.fit_target_mb)])
        cmd.extend(profile.extra_args)
        return cmd

    def _health(self, endpoint: str, timeout: float = 1.5) -> tuple[bool, str]:
        req = urllib.request.Request(self._health_url(endpoint), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status == 200, body
        except urllib.error.HTTPError as exc:
            # llama.cpp intentionally returns 503 while a model is loading.
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = str(exc)
            return False, body
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, str(exc)

    def _stop_managed(self, model_id: str) -> None:
        item = self._managed.pop(model_id, None)
        if not item:
            status = self._status.get(model_id)
            if status and status.managed:
                status.state = "stopped"
                status.healthy = False
                status.pid = None
            return
        process = item.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        try:
            item.log_handle.close()
        except Exception:
            pass
        item.status.state = "stopped"
        item.status.healthy = False
        item.status.pid = None

    def stop_model(self, model_id: str) -> RuntimeStatus:
        with self._lock:
            self._stop_managed(model_id)
            return self._status[model_id]

    def stop_all(self) -> None:
        with self._lock:
            for model_id in list(self._managed):
                self._stop_managed(model_id)

    def resident_model_ids(self) -> list[str]:
        with self._lock:
            return [mid for mid, item in self._managed.items() if item.process.poll() is None]

    def release_managed_models_for_vram(self, *, required_vram_gb: float, mode: str = "balanced") -> list[str]:
        """Stop managed LLM runtimes when an image job needs GPU memory.

        External runtimes are never terminated. The returned ids may be restored later.
        """
        with self._lock:
            self.refresh_hardware()
            if required_vram_gb <= 0 or self.hardware.free_vram_gb >= required_vram_gb:
                return []
            profiles = {m.id: m for m in self.config.models}
            active = [mid for mid in self.resident_model_ids() if not profiles.get(mid, self.config.models[0]).keep_loaded]
            active.sort(key=lambda mid: self._last_used.get(mid, 0.0))
            if str(mode).lower() in {"prefer image model", "prefer_image_model", "aggressive vram cleanup", "aggressive_vram_cleanup"}:
                victims = list(active)
            else:
                victims = active[:1]
            stopped: list[str] = []
            for mid in victims:
                self._stop_managed(mid)
                stopped.append(mid)
                self.refresh_hardware()
                if self.hardware.free_vram_gb >= required_vram_gb and str(mode).lower() not in {"aggressive vram cleanup", "aggressive_vram_cleanup"}:
                    break
            return stopped

    def restore_managed_models(self, model_ids: list[str]) -> list[str]:
        restored: list[str] = []
        profiles = {m.id: m for m in self.config.models}
        for model_id in model_ids:
            profile = profiles.get(model_id)
            if profile is None or profile.runtime != "llama_cpp":
                continue
            try:
                self.ensure_ready(profile)
                restored.append(model_id)
            except Exception:
                continue
        return restored

    def _enforce_residency(self, target: ModelProfile) -> None:
        max_resident = max(1, int(self.config.max_resident_models))
        active = [mid for mid, p in self._managed.items() if p.process.poll() is None and mid != target.id]
        if len(active) < max_resident:
            return
        profiles = {m.id: m for m in self.config.models}
        stoppable = [mid for mid in active if not profiles.get(mid, target).keep_loaded]
        stoppable.sort(key=lambda mid: self._last_used.get(mid, 0.0))
        while len(active) >= max_resident and stoppable:
            victim = stoppable.pop(0)
            self._stop_managed(victim)
            active.remove(victim)

    def _start_llama_cpp(self, profile: ModelProfile) -> str:
        existing = self._managed.get(profile.id)
        if existing and existing.process.poll() is None:
            healthy, _ = self._health(existing.endpoint)
            if healthy:
                existing.status.healthy = True
                existing.status.state = "running"
                return existing.endpoint
            self._stop_managed(profile.id)

        self._enforce_residency(profile)
        port = profile.port or self._port_from_endpoint(profile.endpoint) or self._find_free_port(profile.host)
        endpoint = self._profile_endpoint(profile, port)
        log_path = self.logs_dir / f"{profile.id}.log"
        log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
        command = self._build_command(profile, port)
        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(
            command,
            cwd=str(self.base_dir),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
        )
        status = self._status[profile.id]
        status.state = "loading"
        status.endpoint = endpoint
        status.pid = process.pid
        status.managed = True
        status.healthy = False
        status.started_at = time.time()
        status.error = ""
        status.log_path = str(log_path)
        self._managed[profile.id] = _ManagedProcess(profile, process, endpoint, log_handle, status)

        deadline = time.monotonic() + max(5, profile.startup_timeout)
        last_detail = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                status.state = "error"
                status.error = f"llama-server exited with code {process.returncode}; see {log_path}"
                status.pid = None
                self._managed.pop(profile.id, None)
                try:
                    log_handle.close()
                except Exception:
                    pass
                raise RuntimeError(status.error)
            healthy, detail = self._health(endpoint)
            last_detail = detail
            if healthy:
                status.state = "running"
                status.healthy = True
                self._last_used[profile.id] = time.time()
                return endpoint
            time.sleep(0.25)

        status.state = "error"
        status.error = f"Timed out waiting for model health: {last_detail[:300]}"
        self._stop_managed(profile.id)
        status.state = "error"
        raise TimeoutError(status.error)

    def ensure_ready(self, profile: ModelProfile) -> str:
        with self._lock:
            self._last_used[profile.id] = time.time()
            if profile.runtime == "external":
                endpoint = self._profile_endpoint(profile)
                healthy, detail = self._health(endpoint)
                status = self._status[profile.id]
                status.endpoint = endpoint
                status.healthy = healthy
                status.state = "running" if healthy else "external_unreachable"
                status.error = "" if healthy else detail[:300]
                # External runtimes are not ours to start or stop. Provider will surface a clearer request error.
                return endpoint
            if profile.runtime != "llama_cpp":
                raise RuntimeError(f"Unsupported runtime '{profile.runtime}' for model '{profile.id}'")
            if not self.config.runtime_auto_start:
                return self._profile_endpoint(profile)
            return self._start_llama_cpp(profile)

    def recover(self, profile: ModelProfile) -> str:
        with self._lock:
            status = self._status[profile.id]
            if profile.runtime == "external":
                return self.ensure_ready(profile)
            status.restarts += 1
            self._stop_managed(profile.id)
            return self._start_llama_cpp(profile)

    def statuses(self, *, probe_external: bool = False) -> list[dict]:
        with self._lock:
            for profile in self.config.models:
                status = self._status[profile.id]
                if profile.id in self._managed:
                    item = self._managed[profile.id]
                    if item.process.poll() is not None:
                        status.state = "error"
                        status.healthy = False
                        status.pid = None
                        status.error = f"runtime exited with code {item.process.returncode}"
                    else:
                        healthy, detail = self._health(item.endpoint, timeout=0.3)
                        status.healthy = healthy
                        status.state = "running" if healthy else "loading"
                        if not healthy and detail:
                            status.error = detail[:300]
                elif probe_external and profile.runtime == "external":
                    healthy, detail = self._health(status.endpoint, timeout=0.3)
                    status.healthy = healthy
                    status.state = "running" if healthy else "external_unreachable"
                    status.error = "" if healthy else detail[:300]
            return [self._status[m.id].as_dict() for m in self.config.models]

    def readiness(self, *, probe_external: bool = True) -> dict:
        """Describe whether configured coding models can actually serve agent work."""
        self.refresh_hardware()
        statuses = {row["model_id"]: row for row in self.statuses(probe_external=probe_external)}
        rows: list[dict] = []
        covered_roles: set[str] = set()
        recommendations: list[str] = []

        for profile in self.config.models:
            if not profile.enabled:
                continue
            status = statuses.get(profile.id, {})
            fits, _score, fit_reason = self.resource_fit(profile)
            issues: list[str] = []
            healthy = bool(status.get("healthy"))
            model_file = ""
            executable = ""
            if profile.runtime == "external":
                runnable = healthy
                if not profile.endpoint:
                    issues.append("endpoint is not configured")
                elif not healthy:
                    issues.append("external endpoint is not reachable")
            elif profile.runtime == "llama_cpp":
                executable = self.discover_llama_server(profile) or ""
                if not executable:
                    issues.append("llama.cpp server command was not found")
                if not profile.model_path:
                    issues.append("GGUF model path is not configured")
                    model_ok = False
                else:
                    path = self._resolve(profile.model_path)
                    model_file = str(path)
                    model_ok = path.is_file()
                    if not model_ok:
                        issues.append(f"GGUF model file is missing: {path}")
                if not fits and fit_reason:
                    # resource_fit also reports availability. Avoid repeating the same
                    # missing-model condition in a second, slightly different sentence.
                    if model_ok or "GGUF" not in fit_reason:
                        issues.append(fit_reason)
                runnable = bool(executable and model_ok and fits)
            else:
                runnable = False
                issues.append(f"unsupported runtime: {profile.runtime}")

            if healthy or runnable:
                covered_roles.update(profile.roles)
            rows.append({
                "id": profile.id,
                "runtime": profile.runtime,
                "roles": list(profile.roles),
                "healthy": healthy,
                "runnable": runnable,
                "state": status.get("state", ""),
                "endpoint": status.get("endpoint") or profile.endpoint,
                "model_file": model_file,
                "llama_server": executable,
                "resource_fit": fits,
                "resource_reason": fit_reason,
                "issues": issues,
            })

        coding_roles = {"fast_coder", "primary_coder", "deep_reasoner"}
        ready_to_code = any(row["runnable"] or row["healthy"] for row in rows) and bool(covered_roles & coding_roles)
        auto_routing_ready = {"primary_coder", "deep_reasoner", "reviewer"}.issubset(covered_roles)

        if not rows:
            recommendations.append("Configure at least one enabled coding model profile.")
        managed = [row for row in rows if row["runtime"] == "llama_cpp"]
        if managed and not any(row["llama_server"] for row in managed):
            recommendations.append("Install llama.cpp and put llama-server (or the unified llama command) on PATH, or set llama_cpp_executable.")
        if managed and not any(row["runnable"] or row["healthy"] for row in managed):
            recommendations.append("Point a managed model profile at an existing GGUF file in the models directory.")
        external = [row for row in rows if row["runtime"] == "external"]
        if external and not any(row["healthy"] for row in external):
            recommendations.append("Start the configured OpenAI-compatible local endpoint, or switch to a managed llama.cpp profile.")
        if ready_to_code and not auto_routing_ready:
            recommendations.append("Coding is available now; add dedicated deep-reasoner/reviewer roles later for stronger automatic routing.")

        return {
            "ready_to_code": ready_to_code,
            "auto_routing_ready": auto_routing_ready,
            "covered_roles": sorted(covered_roles),
            "models": rows,
            "hardware": self.hardware.as_dict(),
            "llama_server": self.discover_llama_server(),
            "models_dir": str(self.models_dir),
            "inventory": self.inventory(),
            "recommendations": recommendations,
            "runtime_install": self.runtime_install_guidance(),
        }

    def summary(self, *, probe_external: bool = False) -> dict:
        return {
            "hardware": self.hardware.as_dict(),
            "llama_server": self.discover_llama_server(),
            "models_dir": str(self.models_dir),
            "inventory": self.inventory(),
            "runtimes": self.statuses(probe_external=probe_external),
            "max_resident_models": self.config.max_resident_models,
            "runtime_auto_start": self.config.runtime_auto_start,
            "catalog": self.model_catalog.catalog(),
            "install_jobs": self.model_catalog.jobs(),
        }
