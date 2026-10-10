from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from ..config import AgentConfig, ModelProfile
from ..procutil import no_window_flags
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
    exit_code: int | None = None
    crash_reason: str = ""
    last_crash_at: float | None = None

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
            "exit_code": self.exit_code,
            "crash_reason": self.crash_reason,
            "last_crash_at": self.last_crash_at,
        }


@dataclass(slots=True)
class _ManagedProcess:
    profile: ModelProfile
    process: subprocess.Popen
    endpoint: str
    log_handle: object
    status: RuntimeStatus


_EXE_MIN_BYTES = 4096
# PE machine types accepted on Windows: x86, x64, ARM32, ARM64.
_PE_MACHINES = {0x014C, 0x8664, 0x01C0, 0xAA64, 0x01C4}


def _validate_executable(path: str) -> str:
    """Return "" when *path* is a plausible spawnable binary, else a reason.

    Windows reports "unsupported 16-bit application" for truncated or
    malformed binaries (a partial download, or a placeholder file named
    like the real exe). Validating before CreateProcess turns that
    misleading dialog into a precise error naming the offending path.
    POSIX launchers are often small wrapper scripts, so the size floor
    and PE-header checks run on Windows only.
    """
    p = Path(path)
    if not p.is_file():
        return f"not a file: {p}"
    if os.name != "nt":
        return ""
    try:
        size = p.stat().st_size
    except OSError as exc:
        return f"cannot stat: {exc}"
    if size < _EXE_MIN_BYTES:
        return f"file is only {size} bytes — truncated or a placeholder, not a binary"
    try:
        with p.open("rb") as fh:
            head = fh.read(0x40)
            if len(head) < 0x40 or head[:2] != b"MZ":
                return "not a PE executable (missing MZ header)"
            e_lfanew = int.from_bytes(head[0x3C:0x40], "little")
            if e_lfanew <= 0 or e_lfanew + 6 > size:
                return f"truncated PE header (e_lfanew={e_lfanew}, size={size})"
            fh.seek(e_lfanew)
            sig = fh.read(6)
        if len(sig) < 6 or sig[:4] != b"PE\x00\x00":
            return "invalid PE signature — file is corrupt or not a Windows binary"
        machine = int.from_bytes(sig[4:6], "little")
        if machine not in _PE_MACHINES:
            return f"unsupported PE machine type 0x{machine:04X}"
    except OSError as exc:
        return f"unreadable: {exc}"
    return ""


class _OrphanProcess:
    """Duck-typed stand-in for a llama-server this manager didn't spawn —
    lets a healthy survivor of a previous backend be adopted into
    ``_managed`` (poll/terminate/kill/wait all act on the real pid)."""

    __slots__ = ("pid", "_mgr")

    def __init__(self, pid: int, mgr: "RuntimeManager") -> None:
        self.pid = pid
        self._mgr = mgr

    def poll(self) -> int | None:
        return None if self._mgr._pid_alive(self.pid) else 1

    def terminate(self) -> None:
        self._mgr._kill_pid(self.pid)

    kill = terminate

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.time() + (timeout if timeout is not None else 0)
        while self.poll() is None:
            if timeout is not None and time.time() > deadline:
                raise subprocess.TimeoutExpired("orphan", timeout)
            time.sleep(0.05)
        return 1


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
        self._hw_ts: float = time.time()
        self._managed: dict[str, _ManagedProcess] = {}
        self._status: dict[str, RuntimeStatus] = {}
        self._lock = threading.RLock()
        # In-flight model launches. A llama-server load takes up to
        # startup_timeout seconds and must NOT hold _lock — every status
        # reader (including /api/status, the desktop host health check)
        # would block for the whole load. Claiming here serializes
        # duplicate starts while the wait itself runs lock-free.
        self._starting: set[str] = set()
        self._launch_cond = threading.Condition(self._lock)
        self._last_used: dict[str, float] = {}
        self.twin: Any = None  # optional DigitalTwin, attached by AppState
        self._launch_ctx: dict[str, int] = {}
        self._launch_tuning: dict[str, list[str]] = {}
        self._pending_rewarm: set[str] = set()
        self._rewarm_lock = threading.Lock()
        # model_id -> (timestamp, resource) for models stopped by the demand-
        # driven release path (a queued node needed the memory). Relaunching
        # such a model while the freed resource is still short just re-evicts
        # it next tick — the evict/launch ping-pong churns a 14B through
        # repeated multi-minute loads. ensure_ready defers these launches
        # until the window lapses or the model would fit again.
        self._demand_evicted: dict[str, tuple[float, str]] = {}
        # Optional residency observer: called with {"action", "model_id",
        # "reason"} when a managed runtime is reclaimed or rewarmed so the UI
        # timeline can show FREEING VRAM / REWARMING steps.
        self.on_residency_event: Callable[[dict[str, Any]], None] | None = None
        # External VRAM releasers: registered by the app shell for consumers
        # the runtime doesn't own (idle image backends, idle GPU voice
        # workers). Called — best-effort — when a managed model launch needs
        # VRAM and reclaiming them is cheaper than evicting a resident LLM.
        self.vram_releasers: list[Callable[[], None]] = []
        # §17 — per-worker RAM/VRAM ledger (before/peak/after + residual).
        # Image backends wire begin/sample/end around their managed
        # processes; leak_suspects() surfaces repeat-leakers to recycle.
        from ..workers.leaks import WorkerLeakTracker
        self.leaks = WorkerLeakTracker(self._leak_probe)
        from .tuner import RuntimeTuner
        self.tuner = RuntimeTuner(self.base_dir, config, runtime=self)
        for model in config.models:
            endpoint = self._profile_endpoint(model)
            self._status[model.id] = RuntimeStatus(
                model_id=model.id,
                state="external" if model.runtime == "external" else "stopped",
                endpoint=endpoint,
                managed=model.runtime != "external",
            )

    def reconfigure_models(self, config: AgentConfig) -> None:
        """Apply a new coding-model configuration without restarting Nexus Core."""
        with self._lock:
            for model_id in list(self._managed):
                self._stop_managed(model_id)

            self.config = config
            new_models_dir = self._resolve(config.models_dir)
            new_logs_dir = self._resolve(config.runtime_logs_dir)
            new_models_dir.mkdir(parents=True, exist_ok=True)
            new_logs_dir.mkdir(parents=True, exist_ok=True)

            if new_models_dir != self.models_dir:
                self.models_dir = new_models_dir
                self.model_catalog = CodingModelCatalogManager(self.models_dir)
            self.logs_dir = new_logs_dir

            self._status = {}
            self._last_used = {}
            self._pending_rewarm = set()
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

    def _leak_probe(self) -> tuple[float, float]:
        # TTL-bounded probe — a _wait_ready loop samples every 0.5s and
        # must not spawn nvidia-smi on every tick.
        hw = self.fresh_hardware(max_age_s=2.0)
        return hw.available_ram_gb, hw.free_vram_gb

    def refresh_hardware(self) -> HardwareSnapshot:
        self.hardware = detect_hardware()
        self._hw_ts = time.time()
        return self.hardware

    def fresh_hardware(self, max_age_s: float = 15.0) -> HardwareSnapshot:
        """Return the hardware snapshot, re-probing when it's older than
        ``max_age_s``. Callers that gate decisions on live pressure (budget
        checks, admission) must not read a minutes-old RAM dip forever —
        but re-probing on every caller tick would spawn nvidia-smi in a
        loop, so the refresh is TTL-bounded."""
        if time.time() - getattr(self, "_hw_ts", 0.0) > max_age_s:
            return self.refresh_hardware()
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
            "note": "Install commands are shown for convenience and are never executed automatically by Nexus Core.",
        }
    def model_storage(self) -> dict:
        """Return install-path and disk-capacity information for local coding models."""
        try:
            usage = shutil.disk_usage(self.models_dir)
            return {
                "path": str(self.models_dir),
                "total_bytes": int(usage.total),
                "used_bytes": int(usage.used),
                "free_bytes": int(usage.free),
                "available": True,
                "error": "",
            }
        except OSError as exc:
            return {
                "path": str(self.models_dir),
                "total_bytes": 0,
                "used_bytes": 0,
                "free_bytes": 0,
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
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

    def _reclaimable_resources(self, target: ModelProfile) -> tuple[float, float, list[str]]:
        """Estimate RAM/VRAM released before a managed model switch.

        Routing happens before ensure_ready(), while the currently resident model may
        still own most of the memory. With a residency limit, _enforce_residency()
        will stop older managed models before the target starts. Account for those
        imminent releases so Auto routing does not reject the next model using stale
        memory pressure from the model it is about to replace.
        """
        if target.runtime != "llama_cpp":
            return 0.0, 0.0, []

        active = [model_id for model_id in self.resident_model_ids() if model_id != target.id]
        max_resident = max(1, int(self.config.max_resident_models))
        if len(active) < max_resident:
            return 0.0, 0.0, []

        profiles = {model.id: model for model in self.config.models}
        stoppable = [
            model_id for model_id in active
            if model_id in profiles and not profiles[model_id].keep_loaded
        ]
        stoppable.sort(key=lambda model_id: self._last_used.get(model_id, 0.0))

        reclaim_ram = 0.0
        reclaim_vram = 0.0
        victims: list[str] = []
        remaining = len(active)
        while remaining >= max_resident and stoppable:
            victim = stoppable.pop(0)
            victim_profile = profiles[victim]
            victims.append(victim)
            reclaim_ram += max(0.0, float(victim_profile.estimated_ram_gb))
            reclaim_vram += max(0.0, float(victim_profile.estimated_vram_gb))
            remaining -= 1
        return reclaim_ram, reclaim_vram, victims

    def resource_fit(self, profile: ModelProfile) -> tuple[bool, int, str]:
        """Return availability/resource fit for routing before a model is selected."""
        if profile.runtime == "llama_cpp":
            exe = self.discover_llama_server(profile)
            if not exe:
                return False, -200, "managed runtime executable is unavailable"
            bad = _validate_executable(exe)
            if bad:
                return False, -200, f"managed runtime executable invalid: {bad} ({exe})"
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

        # Digital Twin calibration: when this hardware has measured the model's
        # real footprint, prefer those numbers over static profile estimates.
        twin = getattr(self, "twin", None)
        if twin is not None and profile.model_path:
            try:
                mp = self._resolve(profile.model_path)
                if mp.is_file():
                    pred = twin.predict_model(
                        model_id=profile.id,
                        size_gb=mp.stat().st_size / 1e9,
                        prefer_gpu=required_vram > 0)
                    if pred.get("basis") == "measured":
                        required_ram = float(pred["estimated_ram_gb"])
                        required_vram = float(pred["estimated_vram_gb"])
            except Exception:
                pass

        reclaim_ram, reclaim_vram, victims = self._reclaimable_resources(profile)
        if reclaim_ram > 0 and h.total_ram_gb > 0:
            avail_ram = min(h.total_ram_gb, avail_ram + reclaim_ram)
        elif reclaim_ram > 0:
            avail_ram += reclaim_ram
        if reclaim_vram > 0 and h.total_vram_gb > 0:
            free_vram = min(h.total_vram_gb, free_vram + reclaim_vram)
        elif reclaim_vram > 0:
            free_vram += reclaim_vram
        switch_note = (
            f" after releasing resident {', '.join(victims)}"
            if victims else ""
        )

        # No estimates means we cannot reject it; let llama.cpp auto-fit decide.
        if required_vram <= 0 and required_ram <= 0:
            return True, 0, "no resource estimate; runtime auto-fit allowed"

        # RAM estimates are planning guidance, not exact runtime allocations.
        # llama.cpp can change GPU/CPU placement and KV/cache residency at launch.
        # Keep a strict comfortable band, but allow a modest near-fit margin for
        # managed models that explicitly permit CPU offload. The real runtime is
        # still authoritative: activation fallback handles an actual load failure.
        ram_comfortable = (
            required_ram <= 0
            or avail_ram <= 0
            or required_ram <= avail_ram * 0.92
        )
        if h.total_ram_gb > 0:
            ram_near_margin = max(2.0, min(6.0, h.total_ram_gb * 0.08))
        else:
            ram_near_margin = 4.0
        ram_near_fit = (
            required_ram > 0
            and avail_ram > 0
            and required_ram <= avail_ram + ram_near_margin
        )
        ram_runtime_fit = ram_comfortable or (
            profile.runtime == "llama_cpp"
            and profile.allow_cpu_offload
            and ram_near_fit
        )

        if required_ram > 0 and avail_ram > 0 and not ram_runtime_fit:
            return False, -100, (
                f"estimated RAM need {required_ram:.1f} GB exceeds effective available "
                f"{avail_ram:.1f} GB plus {ram_near_margin:.1f} GB auto-fit margin"
                f"{switch_note}"
            )

        ram_note = ""
        ram_penalty = 0
        if not ram_comfortable and ram_near_fit:
            ram_note = (
                f"; tight estimated RAM fit {required_ram:.1f} GB vs "
                f"{avail_ram:.1f} GB available, runtime auto-fit allowed"
            )
            ram_penalty = -10

        if required_vram > 0:
            if free_vram >= required_vram:
                return True, 25 + ram_penalty, (
                    f"fits effective free VRAM ({free_vram:.1f} GB){switch_note}{ram_note}"
                )
            if profile.allow_cpu_offload and ram_runtime_fit:
                return True, -5 + ram_penalty, (
                    f"requires CPU offload; effective free VRAM {free_vram:.1f} GB"
                    f"{switch_note}{ram_note}"
                )
            return False, -100, (
                f"estimated VRAM need {required_vram:.1f} GB exceeds effective free "
                f"{free_vram:.1f} GB{switch_note}"
            )
        return True, 5 + ram_penalty, (
            f"fits effective available RAM{switch_note}{ram_note}"
        )

    def _build_command(self, profile: ModelProfile, port: int, *, apply_tuning: bool = True, extra_args: list[str] | None = None, ctx_override: int | None = None) -> list[str]:
        exe = self.discover_llama_server(profile)
        if not exe:
            raise RuntimeError(
                "llama.cpp server was not found. Set llama_cpp_executable or the model profile executable, "
                "or add llama-server / the unified llama command to PATH."
            )
        bad = _validate_executable(exe)
        if bad:
            raise RuntimeError(
                f"llama.cpp executable is not a valid program: {exe} — {bad}. "
                "Reinstall the runtime or point llama_cpp_executable at a working binary."
            )
        if not profile.model_path:
            raise RuntimeError(f"Model profile '{profile.id}' has runtime=llama_cpp but no model_path.")
        model_path = self._resolve(profile.model_path)
        if not model_path.is_file():
            raise RuntimeError(f"Model file not found: {model_path}")

        cmd = [exe]
        if self._is_unified_llama(exe):
            cmd.append("serve")
        ctx = profile.context_window
        if apply_tuning and getattr(self.config, "runtime_dynamic_context", True):
            try:
                from .tuner import recommended_context
                ctx = recommended_context(profile)
            except Exception:
                ctx = profile.context_window
        if ctx_override:
            # A task can demand a larger window than the role default — never
            # shrink below the role recommendation though.
            ctx = max(ctx or 0, int(ctx_override)) or ctx_override
        cmd.extend([
            "--model", str(model_path),
            "--host", profile.host,
            "--port", str(port),
            "--ctx-size", str(ctx),
        ])
        if profile.mmproj_path:
            mmproj = self._resolve(profile.mmproj_path)
            if not mmproj.is_file():
                raise RuntimeError(f"mmproj projector file not found: {mmproj}")
            cmd.extend(["--mmproj", str(mmproj)])
        if profile.gpu_layers:
            cmd.extend(["--gpu-layers", str(profile.gpu_layers)])
        if profile.threads > 0:
            cmd.extend(["--threads", str(profile.threads)])
        if profile.fit_target_mb > 0:
            cmd.extend(["--fit-target", str(profile.fit_target_mb)])

        # Qwen3 14B is the low-latency everyday route. Existing user configs from
        # earlier installers may not have an explicit reasoning flag, so inject
        # non-thinking mode unless the user already supplied a --reasoning override.
        has_reasoning_override = any(
            str(arg) == "--reasoning" or str(arg).startswith("--reasoning=")
            for arg in profile.extra_args
        )
        qwen14 = (
            profile.id.lower() == "qwen3-14b"
            or profile.model.lower().startswith("qwen3-14b")
        )
        if qwen14 and not has_reasoning_override:
            cmd.extend(["--reasoning", "off"])

        # Tool calling requires --jinja so llama.cpp renders the request's
        # `tools` block through the chat template and parses tool_calls back
        # out. Without it the server silently ignores tool schemas and the
        # model can only narrate actions it never performs.
        user_args = [*profile.extra_args, *(extra_args or [])]
        has_jinja_override = any(
            str(arg) in ("--jinja", "--no-jinja") for arg in user_args
        )
        if getattr(profile, "tool_calling", False) and not has_jinja_override:
            cmd.append("--jinja")

        # Tuned flags: persisted benchmark results or capability-gated
        # heuristics. User-supplied extra_args always win on conflicts.
        try:
            tuned = self.tuner.tuned_flags(
                profile, mode=str(getattr(self.config, "performance_mode", "auto"))) if apply_tuning else []
        except Exception:
            tuned = []
        if tuned:
            present = {
                str(a).split("=")[0]
                for a in [*cmd, *profile.extra_args]
                if str(a).startswith("-")
            }
            idx = 0
            while idx < len(tuned):
                flag = tuned[idx]
                if str(flag).startswith("-") and str(flag).split("=")[0] not in present:
                    cmd.append(flag)
                    if idx + 1 < len(tuned) and not str(tuned[idx + 1]).startswith("-"):
                        cmd.append(tuned[idx + 1])
                        idx += 2
                        continue
                idx += 1

        cmd.extend(profile.extra_args)
        if extra_args:
            cmd.extend(extra_args)
        return cmd

    @staticmethod
    def _friendly_health_detail(body: str) -> str:
        """Translate a runtime's raw health-probe body into display text.

        llama.cpp answers 503 with a JSON envelope while a model loads —
        showing that verbatim in the status row is unreadable, so map known
        shapes to short phrases and otherwise prefer the error message over
        raw markup.
        """
        text = (body or "").strip()
        if not text:
            return text
        try:
            payload = json.loads(text)
        except Exception:
            return text
        if not isinstance(payload, dict):
            return text
        if payload.get("status") == "ok":
            return "ok"
        err = payload.get("error")
        if not isinstance(err, dict):
            return text
        message = str(err.get("message") or "").strip()
        if "loading" in message.lower() or str(err.get("type") or "") == "unavailable_error":
            return "model is loading"
        return message or text

    def _health(self, endpoint: str, timeout: float = 1.5) -> tuple[bool, str]:
        req = urllib.request.Request(self._health_url(endpoint), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status == 200, self._friendly_health_detail(body)
        except urllib.error.HTTPError as exc:
            try:
                # llama.cpp intentionally returns 503 while a model is loading.
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    body = str(exc)
                return False, self._friendly_health_detail(body)
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, str(exc)

    def _reclaim_orphaned_port(self, port: int) -> None:
        """Kill orphaned runtime processes squatting on a model's port.

        A previous backend that died without reaping this model's server
        leaves an orphan holding the port and its VRAM — the next spawn
        would fail to bind (or double-load the model). Only kills listeners
        whose image looks like our runtime, and never pids still owned by
        ``self._managed`` (a port collision between profiles must not kill
        a healthy sibling).
        """
        managed_pids = {item.process.pid for item in self._managed.values()}
        for pid in self._listening_pids(port):
            if pid in managed_pids:
                continue
            name = self._process_image_name(pid)
            if name and "llama" in name.lower():
                self._kill_pid(pid)

    def _pid_alive(self, pid: int) -> bool:
        try:
            return bool(self._process_image_name(pid))
        except Exception:
            return False

    def _orphan_serves_model(self, endpoint: str, profile: ModelProfile) -> bool:
        """True when the server on ``endpoint`` has this profile's model file
        loaded — adoption must never silently serve a different checkpoint."""
        want = Path(profile.model_path).name.lower() if profile.model_path else ""
        try:
            req = urllib.request.Request(f"{endpoint.rstrip('/')}/v1/models")
            with urllib.request.urlopen(req, timeout=3) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return False
        names = [str(m.get("name") or m.get("id") or "")
                 for m in (payload.get("data") or payload.get("models") or [])]
        if not names:
            return False
        if not want:
            return True
        return any(want == Path(n).name.lower() for n in names)

    def _orphan_context(self, endpoint: str) -> int:
        """Best-effort read of the orphan's configured context window."""
        try:
            req = urllib.request.Request(f"{endpoint.rstrip('/')}/props")
            with urllib.request.urlopen(req, timeout=3) as resp:
                props = json.loads(resp.read().decode("utf-8", errors="replace"))
            dgs = props.get("default_generation_settings") or {}
            return int(dgs.get("n_ctx") or props.get("n_ctx") or 0)
        except Exception:
            return 0

    def _adopt_healthy_orphan(self, profile: ModelProfile, port: int,
                              endpoint: str, ctx_override: int | None) -> bool:
        """Adopt a surviving llama-server on the target port instead of
        killing it and reloading multi-GB weights.

        A backend crash/restart orphans the server process — killing a
        *healthy* orphan just to reload the same checkpoint wastes tens of
        seconds and churns VRAM. Adopt only when the orphan is healthy,
        serves this profile's model file, and (when a bigger window was
        requested) already runs enough context. Otherwise the caller's
        reclaim path kills it and launches fresh as before.
        """
        needed_ctx = max(int(ctx_override or 0),
                         int(getattr(profile, "context_window", 0) or 0))
        try:
            listeners = self._listening_pids(port)
        except Exception:
            return False
        with self._lock:
            managed_pids = {getattr(item.process, "pid", None)
                            for item in self._managed.values()} - {None}
        for pid in listeners - managed_pids:
            name = self._process_image_name(pid)
            if not name or "llama" not in name.lower():
                continue
            healthy, _ = self._health(endpoint)
            if not healthy or not self._orphan_serves_model(endpoint, profile):
                continue
            orphan_ctx = self._orphan_context(endpoint)
            if needed_ctx and (not orphan_ctx or orphan_ctx < needed_ctx):
                continue
            with self._lock:
                status = self._status[profile.id]
                status.state = "running"
                status.endpoint = endpoint
                status.healthy = True
                status.managed = True
                status.pid = pid
                status.error = ""
                status.started_at = time.time()
                self._managed[profile.id] = _ManagedProcess(
                    profile, _OrphanProcess(pid, self), endpoint, None, status)
            self._emit_residency(
                "adopted", profile.id,
                f"adopted surviving llama-server (pid {pid}) — no reload needed")
            return True
        return False

    def _listening_pids(self, port: int) -> set[int]:
        """PIDs holding a TCP LISTEN on ``port`` — best-effort, empty on
        failure so callers never block a launch on a probe hiccup."""
        pids: set[int] = set()
        try:
            if os.name == "nt":
                out = subprocess.run(
                    ["netstat", "-ano", "-p", "tcp"],
                    capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace").stdout
                for line in out.splitlines():
                    parts = line.split()
                    if (len(parts) >= 5 and parts[0].upper() == "TCP"
                            and parts[3].upper() == "LISTENING"
                            and parts[1].rsplit(":", 1)[-1] == str(port)):
                        pids.add(int(parts[-1]))
            else:
                out = subprocess.run(
                    ["lsof", "-nP", "-ti", f":{port}", "-sTCP:LISTEN"],
                    capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace").stdout
                for line in out.splitlines():
                    if line.strip().isdigit():
                        pids.add(int(line.strip()))
        except Exception:
            pass
        return pids

    def _runtime_exe_candidates(self) -> set[str]:
        """Resolved paths of llama executables this manager could have
        spawned — the orphan sweep only touches processes whose image is
        one of these, so a foreign llama-server install is never killed."""
        cands: set[str] = set()
        try:
            found = self.discover_llama_server()
            if found:
                cands.add(self._norm_exe(found))
        except Exception:
            pass
        try:
            raw = self.config.llama_cpp_executable
            if raw:
                cands.add(self._norm_exe(str(self._resolve(raw))))
        except Exception:
            pass
        for model in getattr(self.config, "models", []) or []:
            try:
                exe = getattr(model, "executable", "") or ""
                if exe:
                    cands.add(self._norm_exe(str(self._resolve(exe))))
            except Exception:
                continue
        bundled = self.base_dir / "runtime" / "llama"
        for name in ("llama-server.exe", "llama.exe",
                     "llama-server", "llama"):
            p = bundled / name
            if p.is_file():
                cands.add(self._norm_exe(str(p)))
        return cands

    @staticmethod
    def _norm_exe(path: str) -> str:
        try:
            return os.path.normcase(str(Path(path).expanduser().resolve()))
        except Exception:
            return os.path.normcase(path)

    def _exe_is_ours(self, exe: str, cmdline: str) -> bool:
        """True only when the process image is one of this install's own
        llama executables — either a discovered/configured path or any
        binary under ``<base_dir>/runtime``. A foreign llama-server on the
        machine is never treated as ours."""
        if not exe:
            return False
        normed = self._norm_exe(exe)
        if normed in self._runtime_exe_candidates():
            return True
        runtime_root = self._norm_exe(str(self.base_dir / "runtime"))
        return normed.startswith(runtime_root + os.sep)

    def _our_runtime_processes(self) -> list[dict]:
        """Enumerate running llama processes whose image is our managed
        runtime — {pid, exe, cmdline}. Best-effort; empty on any probe
        failure so boot never blocks on process enumeration."""
        out: list[dict] = []
        try:
            if os.name == "nt":
                ps = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive",
                     "-Command",
                     "Get-CimInstance Win32_Process -Filter "
                     "\"Name like 'llama%'\" | Select-Object ProcessId,"
                     "ExecutablePath,CommandLine | ConvertTo-Json -Compress"],
                    capture_output=True, text=True, timeout=30,
                    creationflags=no_window_flags(), encoding="utf-8", errors="replace")
                rows = json.loads(ps.stdout.strip() or "[]")
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows or []:
                    pid = int(row.get("ProcessId") or 0)
                    exe = str(row.get("ExecutablePath") or "")
                    cmd = str(row.get("CommandLine") or "")
                    if pid and exe and self._exe_is_ours(exe, cmd):
                        out.append({"pid": pid, "exe": exe,
                                    "cmdline": cmd})
            else:
                for ent in Path("/proc").iterdir():
                    if not ent.name.isdigit():
                        continue
                    try:
                        exe = os.readlink(ent / "exe")
                        if "llama" not in Path(exe).name.lower():
                            continue
                        cmd = (ent / "cmdline").read_bytes() \
                            .replace(b"\x00", b" ") \
                            .decode("utf-8", "replace").strip()
                        if self._exe_is_ours(exe, cmd):
                            out.append({"pid": int(ent.name), "exe": exe,
                                        "cmdline": cmd})
                    except Exception:
                        continue
        except Exception:
            pass
        return out

    @staticmethod
    def _orphan_cmdline_model_port(cmdline: str) -> tuple[str, int | None]:
        """Parse --model / --port out of a llama-server command line."""
        model, port = "", None
        m = re.search(r"--model\s+(\"([^\"]+)\"|(\S+))", cmdline)
        if m:
            model = m.group(2) or m.group(3) or ""
        m = re.search(r"--port\s+(\d+)", cmdline)
        if m:
            try:
                port = int(m.group(1))
            except ValueError:
                port = None
        return model, port

    def _orphan_profile(self, model_path: str) -> ModelProfile | None:
        """The configured managed profile whose model file the orphan
        serves — adoption must never bind an orphan to the wrong
        checkpoint."""
        want = ""
        try:
            want = self._norm_exe(model_path)
            want_name = Path(model_path).name.lower()
        except Exception:
            want_name = Path(model_path).name.lower() if model_path else ""
        for profile in getattr(self.config, "models", []) or []:
            if getattr(profile, "runtime", "") == "external":
                continue
            ppath = getattr(profile, "model_path", "") or ""
            if not ppath:
                continue
            try:
                resolved = self._norm_exe(str(self._resolve(ppath)))
                if want and resolved == want:
                    return profile
            except Exception:
                pass
            if want_name and Path(ppath).name.lower() == want_name:
                return profile
        return None

    def sweep_orphan_runtimes(self) -> dict[str, list[str]]:
        """Reclaim llama-server processes a previous Nexus backend left
        running. Launch ports are chosen at random, so an orphan on a
        random port is invisible to the per-spawn port-collision adoption
        path — it pins VRAM and RAM forever, invisible to every capacity
        probe (observed: four orphaned 8B servers left <2 GB free VRAM,
        pushing the Chatterbox voice worker to CPU where synthesis timed
        out for four minutes before kokoro fallback).

        Runs at backend boot: a healthy orphan serving a configured
        profile's checkpoint is adopted (no multi-GB reload); anything
        else carrying our runtime image is killed. Only processes whose
        executable resolves to our managed runtime are touched — foreign
        llama-server installs are left alone.
        """
        adopted: list[str] = []
        killed: list[str] = []
        for proc in self._our_runtime_processes():
            pid = int(proc.get("pid") or 0)
            if not pid or pid == os.getpid():
                continue
            with self._lock:
                # Re-read under the lock on every pid: _our_runtime_processes()
                # spawns a slow process-list probe, so a snapshot taken before
                # it returns goes stale when a launch/prewarm registers a new
                # managed server mid-sweep — it would read as an orphan and be
                # killed below. Membership is checked fresh each iteration.
                owned = pid in {
                    getattr(item.process, "pid", None)
                    for item in self._managed.values()}
            if owned:
                continue
            model_path, port = self._orphan_cmdline_model_port(
                str(proc.get("cmdline") or ""))
            profile = self._orphan_profile(model_path) if model_path else None
            adopted_profile = False
            if profile is not None and port:
                with self._lock:
                    already = profile.id in self._managed
                if not already:
                    endpoint = self._profile_endpoint(profile, port)
                    try:
                        adopted_profile = self._adopt_healthy_orphan(
                            profile, port, endpoint, None)
                    except Exception:
                        adopted_profile = False
            if adopted_profile and profile is not None:
                adopted.append(profile.id)
            else:
                self._kill_pid(pid)
                killed.append(str(pid))
        if adopted or killed:
            self._emit_residency(
                "orphan_sweep", "",
                f"adopted={','.join(adopted) or 'none'} "
                f"killed={','.join(killed) or 'none'}")
        return {"adopted": adopted, "killed": killed}

        """PIDs holding a TCP LISTEN on ``port`` — best-effort, empty on
        failure so callers never block a launch on a probe hiccup."""
        pids: set[int] = set()
        try:
            if os.name == "nt":
                out = subprocess.run(
                    ["netstat", "-ano", "-p", "tcp"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=no_window_flags(),
                encoding="utf-8", errors="replace").stdout
                for line in out.splitlines():
                    parts = line.split()
                    if (len(parts) >= 5 and parts[0].upper() == "TCP"
                            and parts[3].upper() == "LISTENING"
                            and parts[1].rsplit(":", 1)[-1] == str(port)):
                        pids.add(int(parts[-1]))
            else:
                out = subprocess.run(
                    ["lsof", "-nP", "-ti", f":{port}", "-sTCP:LISTEN"],
                    capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace").stdout
                for line in out.splitlines():
                    if line.strip().isdigit():
                        pids.add(int(line.strip()))
        except Exception:
            pass
        return pids

    def _process_image_name(self, pid: int) -> str:
        """Best-effort process image name, used to confirm an orphan is
        actually a llama-server before killing it."""
        try:
            if os.name == "nt":
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=no_window_flags(),
                encoding="utf-8", errors="replace").stdout.strip()
                if out.startswith('"'):
                    return out.split('","')[0].strip('"')
            else:
                return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return ""

    def _kill_pid(self, pid: int) -> None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                    creationflags=no_window_flags(),
                )
            else:
                os.kill(pid, 9)
        except Exception:
            pass

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

    def restart_unhealthy_managed(self) -> list[str]:
        """Stop managed runtimes that are dead or failed their last
        health probe, so the next request cold-starts them fresh via
        the single-flight launch path. Returns the stopped model ids."""
        stopped: list[str] = []
        with self._lock:
            for model_id, item in list(self._managed.items()):
                status = self._status.get(model_id)
                dead = item.process.poll() is not None
                if dead or not (status and status.healthy):
                    self._stop_managed(model_id)
                    stopped.append(model_id)
        return stopped

    def reap_zombie_listeners(self) -> list[str]:
        """Stop managed runtimes whose process is alive but whose listen
        socket is gone — the llama-server zombie state observed in soak
        (process running, no listener, every request stalls until the
        next ensure_ready cold-start). Reaped proactively so the next
        user request doesn't eat the stall+restart.

        Only the missing *socket* counts: a busy model that answers a
        health probe slowly still owns its listener and is never
        touched, and a model still inside its startup grace is left
        alone (llama binds the socket only after weights load)."""
        stopped: list[str] = []
        now = time.time()
        with self._lock:
            for model_id, item in list(self._managed.items()):
                if item.process.poll() is not None:
                    continue
                status = self._status.get(model_id)
                started = float(
                    getattr(status, "started_at", None) or now)
                grace = max(60.0, float(
                    getattr(item.profile, "startup_timeout", 60) or 60))
                if now - started < grace:
                    continue
                port = self._port_from_endpoint(item.endpoint)
                if not port or self._listening_pids(port):
                    continue
                status = status or item.status
                status.state = "error"
                status.healthy = False
                status.error = (
                    "llama-server is alive but no longer listening on "
                    f"{item.endpoint} — reaped zombie process")
                self._stop_managed(model_id)
                stopped.append(model_id)
        return stopped

    def resident_model_ids(self) -> list[str]:
        with self._lock:
            return [mid for mid, item in self._managed.items() if item.process.poll() is None]

    def measure_resident(self, model_id: str) -> dict[str, Any]:
        """Best-effort RSS/VRAM/model-size footprint of a resident model.

        Feeds DigitalTwin.model_measures so predict_model can calibrate on
        observed numbers instead of static profile estimates. Returns {} when
        the model is not resident; every field is independently optional.
        """
        with self._lock:
            item = self._managed.get(model_id)
        if item is None or item.process.poll() is not None:
            return {}
        out: dict[str, Any] = {}
        try:
            mp = self._resolve(item.profile.model_path) if item.profile.model_path else None
            if mp is not None and mp.is_file():
                out["model_size_gb"] = round(mp.stat().st_size / 1e9, 3)
        except OSError:
            pass
        pid = item.process.pid
        try:
            if os.name == "nt":
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-Process -Id {pid}).WorkingSet64"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=no_window_flags(), encoding="utf-8", errors="replace")
                if r.returncode == 0 and r.stdout.strip().isdigit():
                    out["ram_used_gb"] = round(int(r.stdout.strip()) / 1e9, 2)
            else:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        out["ram_used_gb"] = round(int(line.split()[1]) / 1048576, 2)
                        break
        except Exception:
            pass
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                creationflags=no_window_flags(), encoding="utf-8", errors="replace")
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) == 2 and parts[0] == str(pid):
                        out["vram_used_mb"] = float(parts[1])
                        break
        except Exception:
            pass
        return out

    def _emit_residency(self, action: str, model_id: str, reason: str = "") -> None:
        hook = self.on_residency_event
        if hook is None:
            return
        try:
            hook({"action": action, "model_id": model_id, "reason": reason})
        except Exception:
            pass

    def _adopt_orphans_for_pressure(self) -> None:
        """Adopt surviving llama-server orphans so pressure eviction sees them.

        A backend restart leaves previously-launched servers running but
        absent from ``_managed`` — invisible to ``resident_model_ids()``
        until a request happens to adopt them. On a saturated card that
        blind spot means the image arbiter finds nothing to evict and the
        job proceeds into an OOM. Sweep only runs when memory is already
        short, so the per-port health probes never sit on a hot path.
        """
        for profile in self.config.models:
            if getattr(profile, "runtime", "") != "llama_cpp":
                continue
            with self._lock:
                item = self._managed.get(profile.id)
                if item is not None and item.process.poll() is None:
                    continue
                endpoint = self._profile_endpoint(profile)
                port = int(getattr(profile, "port", 0) or 0)
            if port <= 0:
                try:
                    port = int(urlsplit(endpoint).port or 0)
                except Exception:
                    port = 0
            if not port:
                continue
            try:
                self._adopt_healthy_orphan(profile, port, endpoint, None)
            except Exception:
                pass

    def release_managed_models_for_vram(self, *, required_vram_gb: float, mode: str = "balanced",
                                        busy_models: set[str] | None = None) -> list[str]:
        """Stop managed LLM runtimes when an image job needs GPU memory.

        External runtimes are never terminated. The returned ids may be restored later.
        Models in ``busy_models`` are serving in-flight work and are never touched.
        """
        busy = set(busy_models or ())
        with self._lock:
            self.refresh_hardware()
            if required_vram_gb <= 0 or self.hardware.free_vram_gb >= required_vram_gb:
                return []
            # Claim restart-orphaned servers before deciding what to evict —
            # they hold VRAM but are not yet in _managed.
            self._adopt_orphans_for_pressure()
            self.refresh_hardware()
            if self.hardware.free_vram_gb >= required_vram_gb:
                return []
            profiles = {m.id: m for m in self.config.models}
            resident = [mid for mid in self.resident_model_ids() if mid not in busy]
            active = [mid for mid in resident if not profiles.get(mid, self.config.models[0]).keep_loaded]
            # keep_loaded models are a last resort: still evictable when an
            # image job genuinely cannot fit — otherwise the generation crawls
            # on CPU offload for far longer than a reload would cost.
            baseline = [mid for mid in resident if mid not in active]
            key = lambda mid: self._last_used.get(mid, 0.0)
            active.sort(key=key); baseline.sort(key=key)
            aggressive = str(mode).lower() in {"prefer image model", "prefer_image_model", "aggressive vram cleanup", "aggressive_vram_cleanup"}
            stopped: list[str] = []
            for mid in active + baseline:
                if not aggressive and self.hardware.free_vram_gb >= required_vram_gb:
                    break
                self._stop_managed(mid)
                stopped.append(mid)
                self._demand_evicted[mid] = (time.time(), "vram")
                self._emit_residency("evict", mid, f"freeing VRAM ({mode})")
                self.refresh_hardware()
            return stopped

    def release_managed_models_for_ram(self, *, required_ram_gb: float,
                                       busy_models: set[str] | None = None) -> list[str]:
        """Stop managed LLM runtimes when an image job needs system RAM.

        ``ullAvailPhys`` only counts *free* pages — a resident LLM holding
        20 GB is invisible to it even though its memory is instantly
        reclaimable by stopping the process. Mirrors the VRAM release:
        non-keep_loaded models go first (least-recently-used), keep_loaded
        residents are a last resort so a genuinely oversized job still runs.
        Busy models serving a task are never touched; external runtimes are
        never terminated.
        """
        busy = set(busy_models or ())
        with self._lock:
            self.refresh_hardware()
            if required_ram_gb <= 0 or self.hardware.available_ram_gb >= required_ram_gb:
                return []
            self._adopt_orphans_for_pressure()
            self.refresh_hardware()
            if self.hardware.available_ram_gb >= required_ram_gb:
                return []
            profiles = {m.id: m for m in self.config.models}
            resident = [mid for mid in self.resident_model_ids() if mid not in busy]
            if not resident:
                return []
            key = lambda mid: self._last_used.get(mid, 0.0)
            optional = sorted(
                (mid for mid in resident
                 if not profiles.get(mid, self.config.models[0]).keep_loaded),
                key=key)
            baseline = sorted(
                (mid for mid in resident
                 if profiles.get(mid, self.config.models[0]).keep_loaded),
                key=key)
            stopped: list[str] = []
            for mid in optional + baseline:
                if self.hardware.available_ram_gb >= required_ram_gb:
                    break
                self._stop_managed(mid)
                stopped.append(mid)
                self._demand_evicted[mid] = (time.time(), "ram")
                self._emit_residency("evict", mid, "freeing RAM for image job")
                self.refresh_hardware()
            return stopped

    def evict_idle(self, *, busy_models: set[str] | None = None) -> list[str]:
        """Stop managed runtimes that are idle or under memory pressure.

        Models listed in ``busy_models`` are currently serving a task and are
        never evicted. ``keep_loaded`` and external runtimes are untouched.
        Idle eviction honors ``model_idle_unload_seconds``; pressure eviction
        honors ``memory_pressure_vram_gb`` / ``memory_pressure_ram_gb`` and can
        run even while tasks are active (it still skips busy models).
        Returns the ids that were stopped.
        """
        busy = set(busy_models or ())
        idle_seconds = float(getattr(self.config, "model_idle_unload_seconds", 0.0) or 0.0)
        vram_floor = float(getattr(self.config, "memory_pressure_vram_gb", 0.0) or 0.0)
        ram_floor = float(getattr(self.config, "memory_pressure_ram_gb", 0.0) or 0.0)
        stopped: list[str] = []
        with self._lock:
            profiles = {m.id: m for m in self.config.models}
            if idle_seconds > 0:
                now = time.time()
                for mid, item in list(self._managed.items()):
                    if mid in busy or item.process.poll() is not None:
                        continue
                    profile = profiles.get(mid)
                    if profile is None or profile.keep_loaded:
                        continue
                    if now - self._last_used.get(mid, 0.0) >= idle_seconds:
                        self._stop_managed(mid)
                        stopped.append(mid)
            if vram_floor <= 0 and ram_floor <= 0:
                return stopped
            self.refresh_hardware()
            while (self.hardware.free_vram_gb < vram_floor
                   or self.hardware.available_ram_gb < ram_floor):
                # keep_loaded models are exempt from ambient pressure eviction:
                # they are the declared baseline — if the floor is below what
                # the baseline leaves free, evicting the resident just to evict
                # it back on rewarm is a thrash loop. Incoming-model contention
                # is handled separately by _enforce_residency at launch time.
                candidates = [
                    mid for mid, item in self._managed.items()
                    if mid not in busy
                    and item.process.poll() is None
                    and mid in profiles
                    and not profiles[mid].keep_loaded
                ]
                if not candidates:
                    break
                victim = min(candidates, key=lambda m: self._last_used.get(m, 0.0))
                self._stop_managed(victim)
                stopped.append(victim)
                self._emit_residency("evict", victim, "memory pressure")
                self.refresh_hardware()
        return stopped

    def shrink_oversized_context(self, *, busy_models: set[str] | None = None) -> list[str]:
        """Relaunch idle residents whose launched context far exceeds the
        role recommendation — a big task can grow the window, and once the
        work is done the oversized KV cache should not pin VRAM forever.

        A model is only shrunk when it is not busy, has been idle for
        ``context_shrink_idle_seconds`` (default 600), and its launched
        window exceeds ``context_shrink_factor`` × ``recommended_context``
        (default 1.5). This is a relaunch, not an eviction — the model
        stays warm, just at its normal window. If the relaunch fails the
        model is simply left stopped (launch fallback already ran).
        """
        busy = set(busy_models or ())
        grace = float(getattr(self.config, "context_shrink_idle_seconds", 600.0) or 0.0)
        factor = float(getattr(self.config, "context_shrink_factor", 1.5) or 1.5)
        if grace <= 0:
            return []
        shrunk: list[str] = []
        targets: list[ModelProfile] = []
        with self._lock:
            now = time.time()
            profiles = {m.id: m for m in self.config.models}
            for mid, item in list(self._managed.items()):
                if mid in busy or item.process.poll() is not None:
                    continue
                profile = profiles.get(mid)
                if profile is None or profile.runtime != "llama_cpp":
                    continue
                launched = int(self._launch_ctx.get(mid, 0))
                if not launched:
                    continue
                try:
                    from .tuner import recommended_context
                    want = int(recommended_context(profile))
                except Exception:
                    want = int(profile.context_window or 0)
                if not want or launched <= want * factor:
                    continue
                if now - self._last_used.get(mid, 0.0) < grace:
                    continue
                self._emit_residency(
                    "relaunch", mid,
                    f"shrinking idle context {launched}→{want}")
                self._stop_managed(mid)
                targets.append(profile)
        # Restarts run off the lock — each ensure_ready claims its own
        # single-flight launch and waits lock-free for the model load.
        for profile in targets:
            try:
                self.ensure_ready(profile)
            except Exception as exc:
                self._emit_residency(
                    "evict", profile.id, f"context shrink failed: {exc}")
                continue
            shrunk.append(profile.id)
        return shrunk

    def rewarm_keep_loaded(self) -> list[str]:
        """Restart keep_loaded models that were reclaimed under memory pressure.

        No-op when nothing is pending or the model is already running. Each
        restart runs on a daemon thread so callers (watchdog, session close)
        never block on a model load.
        """
        with self._lock:
            pending = [
                mid for mid in self._pending_rewarm
                if mid in {m.id: m for m in self.config.models}
            ]
        restarted: list[str] = []
        profiles = {m.id: m for m in self.config.models}
        for mid in pending:
            profile = profiles.get(mid)
            if profile is None or not profile.keep_loaded or profile.runtime != "llama_cpp":
                self._pending_rewarm.discard(mid)
                continue
            with self._lock:
                current = self._managed.get(mid)
                if current is not None and current.process.poll() is None:
                    self._pending_rewarm.discard(mid)
                    continue

            self._pending_rewarm.discard(mid)

            def _warm(p: ModelProfile = profile) -> None:
                try:
                    self.ensure_ready(p)
                except Exception:
                    # Requeue so a later rewarm pass retries once pressure eases.
                    self._pending_rewarm.add(p.id)
                    return
            threading.Thread(target=_warm, name=f"chat-nexus-rewarm-{mid}", daemon=True).start()
            self._emit_residency("rewarm", mid, "resources free — restoring keep-loaded model")
            restarted.append(mid)
        return restarted

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
        profiles = {m.id: m for m in self.config.models}
        if len(active) >= max_resident:
            stoppable = [mid for mid in active if not profiles.get(mid, target).keep_loaded]
            stoppable.sort(key=lambda mid: self._last_used.get(mid, 0.0))
            while len(active) >= max_resident and stoppable:
                victim = stoppable.pop(0)
                self._stop_managed(victim)
                active.remove(victim)
        # Memory-based reclaim: keep_loaded models are still evictable when the
        # incoming target genuinely cannot fit alongside them. They are marked
        # for rewarm once the heavy operation releases VRAM/RAM again.
        if target.runtime != "llama_cpp":
            return
        total_vram = float(getattr(self.hardware, "total_vram_gb", 0.0) or 0.0)
        if total_vram <= 0:
            return
        self.refresh_hardware()
        resident_vram = sum(max(0.0, float(profiles.get(mid).estimated_vram_gb)) if profiles.get(mid) else 0.0 for mid in active)
        free_vram = float(self.hardware.free_vram_gb)
        needed = max(0.0, float(target.estimated_vram_gb)) - free_vram
        if needed <= 0 and resident_vram + float(target.estimated_vram_gb) <= total_vram * 0.92:
            return
        # Idle external GPU consumers (image backends, GPU voice workers)
        # release before we evict a resident LLM — restarting them on next
        # use is far cheaper than reloading a multi-GB model mid-session.
        if needed > 0 and self.vram_releasers:
            for release in list(self.vram_releasers):
                try:
                    release()
                except Exception:
                    pass
            self.refresh_hardware()
            free_vram = float(self.hardware.free_vram_gb)
        if not active:
            return
        keep_loaded_residents = [mid for mid in active if profiles.get(mid, target).keep_loaded]
        keep_loaded_residents.sort(key=lambda mid: self._last_used.get(mid, 0.0))
        for victim in keep_loaded_residents:
            if free_vram >= float(target.estimated_vram_gb):
                break
            self._pending_rewarm.add(victim)
            self._stop_managed(victim)
            self._emit_residency("evict", victim, f"making room for {target.id}")
            active.remove(victim)
            victim_vram = max(0.0, float(profiles[victim].estimated_vram_gb))
            free_vram += victim_vram
            self.refresh_hardware()
            free_vram = float(self.hardware.free_vram_gb)

    def _start_llama_cpp(self, profile: ModelProfile, ctx_override: int | None = None) -> str:
        # Called WITHOUT self._lock — the caller claimed profile.id in
        # self._starting, so at most one launch per model is in flight.
        # Shared-state reads/writes take the lock in short sections only;
        # network probes and the load wait stay lock-free.
        with self._lock:
            existing = self._managed.get(profile.id)
        if existing and existing.process.poll() is None:
            healthy, _ = self._health(existing.endpoint)
            with self._lock:
                launched_ctx = self._launch_ctx.get(profile.id, 0)
            ctx_ok = not ctx_override or launched_ctx >= ctx_override or not launched_ctx
            if healthy and ctx_ok:
                with self._lock:
                    existing.status.healthy = True
                    existing.status.state = "running"
                return existing.endpoint
            if healthy and not ctx_ok:
                # Task needs a bigger window than the resident server was
                # launched with — restart at the larger context rather than
                # silently truncating the prompt.
                self._emit_residency(
                    "relaunch", profile.id,
                    f"expanding context {launched_ctx}→{ctx_override}")
            with self._lock:
                self._stop_managed(profile.id)

        with self._lock:
            self._enforce_residency(profile)
        port = profile.port or self._port_from_endpoint(profile.endpoint) or self._find_free_port(profile.host)
        endpoint = self._profile_endpoint(profile, port)
        try:
            adopted = self._adopt_healthy_orphan(
                profile, port, endpoint, ctx_override)
        except Exception:
            adopted = False
        if not adopted:
            with self._lock:
                self._reclaim_orphaned_port(port)
            return self._launch_with_fallback(profile, port, endpoint, ctx_override)
        return endpoint

    def _launch_with_fallback(
        self, profile: ModelProfile, port: int, endpoint: str,
        ctx_override: int | None = None,
    ) -> str:
        """Launch with tuned flags; on provable tuned-config failure, drop to
        heuristic-safe flags, then to a bare launch. A bad benchmark result
        must never leave the model unable to start."""
        tuned: list[str] = []
        try:
            with self._lock:
                tuned = self.tuner.tuned_flags(
                    profile, mode=str(getattr(self.config, "performance_mode", "auto")))
        except Exception:
            pass
        attempts = [
            ("tuned", True, []),
            ("heuristic", False, self.tuner.tuned_flags_heuristic_safe()),
            ("bare", False, []),
        ]
        # Skip redundant middle attempt when tuned==heuristic or when no
        # tuning was going to be applied anyway.
        if not tuned:
            attempts = [("heuristic", False, attempts[1][2]), ("bare", False, [])]
        elif tuned == attempts[1][2]:
            attempts = [("tuned", True, []), ("bare", False, [])]

        def _try(extra_ctx: int | None) -> str:
            last_exc_inner: Exception | None = None
            for label, apply_tuning, extra in attempts:
                try:
                    return self._spawn_and_wait(
                        profile, port, endpoint,
                        apply_tuning=apply_tuning, extra_args=extra,
                        ctx_override=extra_ctx)
                except Exception as exc:
                    last_exc_inner = exc
                    if label == "tuned" and tuned:
                        try:
                            with self._lock:
                                self.tuner.mark_bad(profile, tuned, str(exc))
                        except Exception:
                            pass
                    continue
            raise last_exc_inner or RuntimeError("llama-server launch failed")

        try:
            return _try(ctx_override)
        except Exception as exc:
            # A request can force a context larger than memory allows — every
            # attempt then fails identically on KV-cache allocation. Retry
            # once at half the window instead of declaring the model dead.
            tail = self._log_tail(self.logs_dir / f"{profile.id}.log")
            alloc_fail = any(s in tail.lower() for s in
                             ("failed to allocate", "out of memory",
                              "kv cache", "cuda error"))
            if not (ctx_override and ctx_override > 8192 and alloc_fail):
                raise
            reduced = max(8192, ctx_override // 2)
            try:
                with self._lock:
                    self._status[profile.id].error = (
                        f"context {ctx_override} exceeded memory — "
                        f"retrying at {reduced}")
            except Exception:
                pass
            return _try(reduced)

    def _spawn_and_wait(
        self, profile: ModelProfile, port: int, endpoint: str, *,
        apply_tuning: bool = True, extra_args: list[str] | None = None,
        ctx_override: int | None = None,
    ) -> str:
        log_path = self.logs_dir / f"{profile.id}.log"
        try:
            # The per-model log appends on every start — bound it so months of
            # unattended restarts cannot grow it without limit.
            if log_path.exists() and log_path.stat().st_size > 8 * 1024 * 1024:
                log_path.write_bytes(log_path.read_bytes()[-4 * 1024 * 1024:])
        except OSError:
            pass
        command = self._build_command(
            profile, port, apply_tuning=apply_tuning,
            extra_args=extra_args, ctx_override=ctx_override)
        log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.base_dir),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creationflags,
                encoding="utf-8", errors="replace")
        except OSError as exc:
            try:
                log_handle.close()
            except Exception:
                pass
            winerr = getattr(exc, "winerror", None)
            detail = (f"WinError {winerr}" if winerr is not None
                      else f"{type(exc).__name__}: {exc}")
            raise RuntimeError(
                f"Windows refused to start {command[0]} ({detail}). "
                "The binary may be corrupt or for the wrong architecture — "
                "reinstall the managed runtime."
            ) from exc
        with self._lock:
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
            # Remember the tuned arg-set this process launched with — if it dies
            # mid-run the recovery path can mark exactly this config bad instead
            # of relaunching it identically forever.
            try:
                self._launch_tuning[profile.id] = list(
                    self.tuner.tuned_flags(
                        profile, mode=str(getattr(self.config, "performance_mode", "auto")))
                ) if apply_tuning else []
            except Exception:
                self._launch_tuning[profile.id] = []

        # The load wait deliberately runs WITHOUT self._lock: it can last
        # startup_timeout seconds and /api/status must keep answering.
        deadline = time.monotonic() + max(5, profile.startup_timeout)
        last_detail = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                with self._lock:
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
                with self._lock:
                    status.state = "running"
                    status.healthy = True
                    self._last_used[profile.id] = time.time()
                    self._launch_ctx[profile.id] = self._ctx_from_command(command)
                self._warmup(profile, endpoint)
                return endpoint
            time.sleep(0.25)

        with self._lock:
            status.state = "error"
            status.error = f"Timed out waiting for model health: {last_detail[:300]}"
            self._stop_managed(profile.id)
        raise TimeoutError(status.error)

    @staticmethod
    def _ctx_from_command(command: list[str]) -> int:
        try:
            idx = command.index("--ctx-size")
            return int(command[idx + 1])
        except (ValueError, IndexError):
            return 0

    def _warmup(self, profile: ModelProfile, endpoint: str) -> None:
        """Fire a 1-token completion so the first real request isn't cold.

        Runs in a daemon thread; failures are irrelevant (the next real request
        exercises the same path). Skipped when model_warmup is disabled.
        """
        if not getattr(self.config, "model_warmup", True):
            return

        def _ping() -> None:
            try:
                req = urllib.request.Request(
                    endpoint.rstrip("/") + "/chat/completions",
                    data=json.dumps({
                        "model": profile.model or profile.id,
                        "messages": [{"role": "user", "content": "ok"}],
                        "max_tokens": 1,
                        "temperature": 0,
                        "stream": False,
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp.read()
            except Exception:
                pass

        threading.Thread(target=_ping, name=f"warmup-{profile.id}", daemon=True).start()

    class _Probe:
        """Unmanaged llama-server process used by the tuner benchmark."""

        def __init__(self, process, endpoint: str, log_file=None):
            self.process = process
            self.endpoint = endpoint
            self.log_file = log_file

        def stop(self) -> None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            try:
                if self.log_file is not None and self.log_file is not subprocess.DEVNULL:
                    self.log_file.close()
            except Exception:
                pass

    def launch_probe(self, profile: ModelProfile, port: int, extra_args: list[str]) -> "RuntimeManager._Probe":
        """Start an unmanaged llama-server for candidate benchmarking.

        Tuned flags are NOT applied — the candidate list is the thing under
        test. Returns once healthy or raises on crash/timeout.
        """
        probe_port = port or self._find_free_port(profile.host)
        command = self._build_command(profile, probe_port, apply_tuning=False, extra_args=extra_args)
        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        log_path = self.logs_dir / f"probe-{profile.id}-{probe_port}.log"
        try:
            log_file = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            log_file = subprocess.DEVNULL
        try:
            # Probe logs are per-launch diagnostics — prune to the newest few
            # so a benchmark grid can't accumulate files forever.
            probe_logs = sorted(
                self.logs_dir.glob("probe-*.log"),
                key=lambda p: p.stat().st_mtime)
            for stale in probe_logs[:-20]:
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        process = subprocess.Popen(
            command,
            cwd=str(self.base_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
            encoding="utf-8", errors="replace")
        # The probe MUST use its own port — _profile_endpoint would return the
        # profile's configured endpoint, silently measuring a resident server
        # (or polling a dead port) instead of the candidate under test.
        endpoint = f"http://{profile.host}:{probe_port}/v1"

        def _close_log() -> None:
            try:
                if log_file is not subprocess.DEVNULL:
                    log_file.close()
            except Exception:
                pass

        deadline = time.monotonic() + max(5, profile.startup_timeout)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _close_log()
                raise RuntimeError(
                    f"probe llama-server exited with code {process.returncode} "
                    f"(log: {log_path})")
            healthy, _ = self._health(endpoint)
            if healthy:
                return self._Probe(process, endpoint, log_file=log_file)
            time.sleep(0.25)
        process.kill()
        _close_log()
        raise TimeoutError(
            f"probe llama-server did not become healthy within "
            f"{max(5, profile.startup_timeout)}s (log: {log_path})")

    def launched_context(self, model_id: str) -> int:
        """ctx size the resident server launched with (0 when unmanaged)."""
        with self._lock:
            return int(self._launch_ctx.get(model_id, 0) or 0)

    def ensure_ready(self, profile: ModelProfile, min_context: int | None = None) -> str:
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
            evicted = self._demand_evicted.get(profile.id)
            if evicted:
                evicted_at, resource = evicted
                cooldown = float(getattr(
                    self.config, "demand_eviction_cooldown_s", 120.0))
                if time.time() - evicted_at < cooldown:
                    self.refresh_hardware()
                    short = (
                        self.hardware.free_vram_gb < float(profile.estimated_vram_gb)
                        if resource == "vram"
                        else self.hardware.available_ram_gb < float(profile.estimated_ram_gb)
                    )
                    if short:
                        raise RuntimeError(
                            f"model '{profile.id}' was evicted to free {resource.upper()} "
                            f"for queued work and still would not fit — launch deferred "
                            f"until pressure clears")
            # Single-flight launch: if another thread is already starting
            # this model, wait on the condition (which releases _lock) so
            # status readers keep working during the load — then take the
            # claim for ourselves if the launch is finished.
            while profile.id in self._starting:
                self._launch_cond.wait(timeout=30)
            self._starting.add(profile.id)
        try:
            return self._start_llama_cpp(profile, ctx_override=min_context)
        finally:
            with self._lock:
                self._starting.discard(profile.id)
                self._launch_cond.notify_all()

    def recover(self, profile: ModelProfile) -> str:
        if profile.runtime == "external":
            return self.ensure_ready(profile)
        with self._lock:
            status = self._status[profile.id]
            status.restarts += 1
            # If this exact tuned configuration keeps dying mid-run, mark it
            # bad so the next launch falls back instead of looping on the same
            # crashing config. Only blacklist on a real process exit with a
            # crash signature — a refused connection to a still-alive server
            # is a stale socket, not a bad config.
            item = self._managed.get(profile.id)
            died = item is not None and item.process.poll() is not None
            if died and status.restarts >= 2:
                tail = self._log_tail(status.log_path) if status.log_path else ""
                cause = ""
                for pattern, label in self._CRASH_SIGNATURES:
                    if pattern.lower() in tail.lower():
                        cause = label
                        break
                tuned = self._launch_tuning.get(profile.id) or []
                if tuned:
                    code = item.process.poll()
                    try:
                        self.tuner.mark_bad(
                            profile, tuned,
                            cause or f"llama-server exited with code {code}")
                    except Exception:
                        pass
                status.crash_reason = cause or status.crash_reason
            self._stop_managed(profile.id)
        # The relaunch itself runs through ensure_ready — the wait for the
        # model load happens off _lock so status readers stay live.
        return self.ensure_ready(profile)

    def statuses(self, *, probe_external: bool = False) -> list[dict]:
        # Bounded acquire: stop/reclaim paths can hold the lock for a few
        # seconds (process waits). The status endpoint must always answer —
        # fall back to the last-known snapshot rather than stall.
        if not self._lock.acquire(timeout=1.0):
            return [self._status[m.id].as_dict() for m in self.config.models]
        try:
            for profile in self.config.models:
                status = self._status[profile.id]
                if profile.id in self._managed:
                    item = self._managed[profile.id]
                    if item.process.poll() is not None:
                        status.state = "error"
                        status.healthy = False
                        status.pid = None
                        status.exit_code = item.process.returncode
                        status.crash_reason = (
                            f"llama-server exited with code {item.process.returncode}"
                        )
                        status.last_crash_at = time.time()
                        status.error = status.crash_reason
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
        finally:
            self._lock.release()

    _CRASH_SIGNATURES = (
        # (pattern, human-readable cause) — first match wins. These turn a
        # bare socket reset into the real reason the backend died.
        ("out of memory", "VRAM/RAM exhausted while loading or generating"),
        ("cuda error", "CUDA failure"),
        ("cudaMalloc failed", "VRAM allocation failed"),
        ("failed to allocate", "memory allocation failed"),
        ("ggml_backend_cuda", "CUDA backend failure"),
        ("access violation", "backend crashed (access violation)"),
        ("assertion failed", "backend crashed (assertion failure)"),
        ("error loading model", "model file failed to load"),
        ("failed to load model", "model file failed to load"),
        ("bind", "port bind failure — the port was already in use"),
        ("address already in use", "port bind failure — the port was already in use"),
    )

    def _log_tail(self, log_path: str, limit: int = 6000) -> str:
        """Last `limit` bytes of a backend log — the crash's stderr lives
        here because llama-server runs with stderr→stdout→file."""
        try:
            path = Path(log_path)
            if not path.is_file():
                return ""
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > limit:
                    fh.seek(-limit, 2)
                return fh.read().decode("utf-8", errors="replace")[-limit:]
        except Exception:
            return ""

    def backend_health(self, model_id: str) -> dict:
        """On-demand diagnostic snapshot for a failed model request —
        process state, exit code, crash signature, log tail and the memory
        picture, so a bare WinError 10054 is never all the user gets."""
        status = self._status.get(model_id)
        profile = next((m for m in self.config.models if m.id == model_id), None)
        item = self._managed.get(model_id)
        proc_alive = bool(item and item.process.poll() is None)
        exit_code = (
            item.process.returncode
            if item is not None and item.process.poll() is not None
            else (status.exit_code if status else None)
        )
        tail = self._log_tail(status.log_path) if status and status.log_path else ""
        crash_hint = ""
        if exit_code is not None or not proc_alive:
            low = tail.lower()
            for pattern, cause in self._CRASH_SIGNATURES:
                if pattern.lower() in low:
                    crash_hint = cause
                    break
        hw = self.hardware
        return {
            "model_id": model_id,
            "runtime": profile.runtime if profile else "",
            "managed": bool(status.managed) if status else False,
            "state": status.state if status else "unknown",
            "healthy": bool(status.healthy) if status else False,
            "endpoint": status.endpoint if status else "",
            "port": self._port_from_endpoint(status.endpoint) if status else None,
            "pid": (item.process.pid if proc_alive else None),
            "exit_code": exit_code,
            "restarts": status.restarts if status else 0,
            "started_at": status.started_at if status else None,
            "error": status.error if status else "",
            "crash_reason": crash_hint or (status.crash_reason if status else ""),
            "log_path": status.log_path if status else "",
            "log_tail": tail[-2000:] if tail else "",
            "free_vram_gb": getattr(hw, "free_vram_gb", None),
            "total_vram_gb": getattr(hw, "total_vram_gb", None),
            "available_ram_gb": getattr(hw, "available_ram_gb", None),
            "estimated_model_vram_gb": float(getattr(profile, "estimated_vram_gb", 0.0) or 0.0) if profile else None,
        }

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
            "model_storage": self.model_storage(),
            "inventory": self.inventory(),
            "recommendations": recommendations,
            "runtime_install": self.runtime_install_guidance(),
            "tuning": self.tuner.status(),
            "performance_mode": getattr(self.config, "performance_mode", "auto"),
        }

    def summary(self, *, probe_external: bool = False) -> dict:
        return {
            "hardware": self.hardware.as_dict(),
            "llama_server": self.discover_llama_server(),
            "models_dir": str(self.models_dir),
            "model_storage": self.model_storage(),
            "inventory": self.inventory(),
            "runtimes": self.statuses(probe_external=probe_external),
            "max_resident_models": self.config.max_resident_models,
            "runtime_auto_start": self.config.runtime_auto_start,
            "catalog": self.model_catalog.catalog(),
            "install_jobs": self.model_catalog.jobs(),
        }
