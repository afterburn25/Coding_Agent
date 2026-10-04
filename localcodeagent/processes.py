from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class ManagedService:
    """A controllable local service/process known to Nexus Core.

    `describe` returns live status (state, pid, health, extra fields). The
    start/stop callables delegate to the owning subsystem (RuntimeManager,
    ComfyUIRuntime, future MCP supervisors) so process control stays with the
    module that launched the process.
    """

    id: str
    name: str
    kind: str  # llm_runtime | image_backend | browser | mcp_server | internal
    source: str = "chat-nexus"
    port: int = 0
    log_path: str = ""
    describe: Callable[[], dict[str, Any]] | None = None
    start: Callable[[], Any] | None = None
    stop: Callable[[], Any] | None = None
    restart: Callable[[], Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


FAILED_STATES = {"crashed", "error", "exited", "failed", "dead"}


def _process_usage_windows(pid: int) -> dict[str, Any]:
    """Working-set + kernel/user CPU for a live PID via Win32 (no psutil)."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return {}
    try:
        usage: dict[str, Any] = {}

        class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        # Classic export lives in psapi.dll; kernel32 carries the K32 name.
        get_mem = getattr(ctypes.windll.psapi, "GetProcessMemoryInfo", None)
        if get_mem is None:
            get_mem = getattr(ctypes.windll.kernel32,
                              "K32GetProcessMemoryInfo", None)
        if get_mem is not None and get_mem(
                handle, ctypes.byref(counters), counters.cb):
            usage["rss_mb"] = round(counters.WorkingSetSize / (1024 ** 2), 1)
            usage["commit_mb"] = round(counters.PrivateUsage / (1024 ** 2), 1)

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                        ("dwHighDateTime", ctypes.c_ulong)]

        def _ticks(ft: FILETIME) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        creation, exit_, kernel, user = (
            FILETIME(), FILETIME(), FILETIME(), FILETIME())
        if ctypes.windll.kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(exit_),
                ctypes.byref(kernel), ctypes.byref(user)):
            usage["cpu_seconds"] = round(
                (_ticks(kernel) + _ticks(user)) / 10_000_000, 1)
        return usage
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _process_usage_procfs(pid: int) -> dict[str, Any]:
    """RSS + utime/stime from /proc (Linux). Empty dict elsewhere."""
    proc = Path(f"/proc/{pid}")
    if not proc.is_dir():
        return {}
    usage: dict[str, Any] = {}
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        resident_pages = int(
            (proc / "statm").read_text(encoding="utf-8").split()[1])
        usage["rss_mb"] = round(resident_pages * page_size / (1024 ** 2), 1)
    except (OSError, IndexError, ValueError):
        pass
    try:
        stat = (proc / "stat").read_text(encoding="utf-8")
        # comm is wrapped in parens and may itself contain spaces/parens.
        fields = stat[stat.rfind(")") + 2:].split()
        ticks = os.sysconf("SC_CLK_TCK")
        # fields[11]=utime (field 14), fields[12]=stime (field 15) — index 0
        # here is `state` (field 3).
        usage["cpu_seconds"] = round(
            (int(fields[11]) + int(fields[12])) / ticks, 1)
    except (OSError, IndexError, ValueError):
        pass
    return usage


def process_usage(pid: Any) -> dict[str, Any]:
    """Best-effort per-process usage: ``rss_mb`` / ``commit_mb`` /
    ``cpu_seconds`` (cumulative). Returns {} for dead/foreign/unsupported
    PIDs — never raises."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {}
    if pid <= 0:
        return {}
    try:
        if os.name == "nt":
            return _process_usage_windows(pid)
        return _process_usage_procfs(pid)
    except (OSError, ValueError, AttributeError):
        return {}


class ProcessManager:
    """Central registry of Nexus Core-managed and attached services."""

    def __init__(self) -> None:
        self._services: dict[str, ManagedService] = {}
        self._lock = threading.RLock()
        # Optional event callback — wired to the server EventBus.
        self.on_event: Callable[[dict[str, Any]], None] | None = None
        self._watchdog: threading.Thread | None = None
        self._watchdog_running = False

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(payload)
        except Exception:
            pass

    # -- auto-restart watchdog ------------------------------------------------
    #
    # Services opt in via metadata["auto_restart"] = True. The watchdog only
    # restarts services whose describe() reports a crash-class state — a
    # user-requested "stopped" is never restarted.

    def start_watchdog(self, *, interval: float = 30.0, window_seconds: float = 600.0,
                       max_restarts: int = 3, on_tick: Callable[[], None] | None = None) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_running = True
        restarts: dict[str, list[float]] = {}

        def loop() -> None:
            while self._watchdog_running:
                time.sleep(max(0.05, interval))
                if on_tick is not None:
                    try:
                        on_tick()
                    except Exception as exc:
                        self._emit({"service": "", "event": "watchdog_tick_error",
                                    "error": f"{type(exc).__name__}: {exc}"})
                with self._lock:
                    services = list(self._services.values())
                for service in services:
                    if not service.metadata.get("auto_restart"):
                        continue
                    try:
                        status = service.describe() if service.describe else {}
                    except Exception:
                        status = {"state": "error"}
                    state = str(status.get("state") or "")
                    if state not in FAILED_STATES:
                        continue
                    recent = [t for t in restarts.get(service.id, []) if time.time() - t < window_seconds]
                    if len(recent) >= max_restarts:
                        continue
                    action = service.restart or service.start
                    if action is None:
                        continue
                    try:
                        result = action()
                        recent.append(time.time())
                        restarts[service.id] = recent
                        self._emit({"service": service.id, "event": "auto_restart",
                                    "state": state, "attempt": len(recent)})
                    except Exception as exc:
                        self._emit({"service": service.id, "event": "auto_restart_failed",
                                    "error": f"{type(exc).__name__}: {exc}"})

        self._watchdog = threading.Thread(target=loop, name="process-watchdog", daemon=True)
        self._watchdog.start()

    def stop_watchdog(self) -> None:
        self._watchdog_running = False

    def register(self, service: ManagedService) -> None:
        with self._lock:
            self._services[service.id] = service

    def unregister(self, service_id: str) -> None:
        with self._lock:
            self._services.pop(service_id, None)

    def get(self, service_id: str) -> ManagedService | None:
        return self._services.get(service_id)

    def service_ids(self, *, prefix: str = "") -> list[str]:
        with self._lock:
            return sorted(sid for sid in self._services if sid.startswith(prefix))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            services = list(self._services.values())
        rows: list[dict[str, Any]] = []
        for service in services:
            status: dict[str, Any] = {}
            if service.describe is not None:
                try:
                    status = dict(service.describe() or {})
                except Exception as exc:
                    status = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
            started_at = status.get("started_at")
            rows.append({
                "id": service.id,
                "name": service.name,
                "kind": service.kind,
                "source": service.source,
                "port": service.port or status.get("port") or 0,
                "log_path": service.log_path or str(status.get("log_path") or ""),
                "state": str(status.get("state") or "unknown"),
                "pid": status.get("pid"),
                "healthy": bool(status.get("healthy")),
                "started_at": started_at,
                "uptime_seconds": max(0, int(time.time() - started_at)) if started_at else 0,
                "error": str(status.get("error") or ""),
                "resource": process_usage(status.get("pid")),
                "can_start": service.start is not None,
                "can_stop": service.stop is not None,
                "can_restart": service.restart is not None or (service.start is not None and service.stop is not None),
                "detail": {k: v for k, v in status.items() if k not in {"state", "pid", "healthy", "started_at", "error", "port", "log_path"}},
                "metadata": dict(service.metadata),
            })
        return sorted(rows, key=lambda r: (r["kind"], r["name"]))

    def action(self, service_id: str, action: str) -> dict[str, Any]:
        service = self._services.get(service_id)
        if service is None:
            raise KeyError(service_id)
        action = str(action or "").strip().lower()
        callback = {"start": service.start, "stop": service.stop, "restart": service.restart}.get(action)
        if callback is None and action == "restart" and service.start and service.stop:
            def callback() -> Any:  # type: ignore[no-redef]
                service.stop()
                return service.start()
        if callback is None:
            raise ValueError(f"service '{service_id}' does not support '{action}'")
        result = callback()
        return {"ok": True, "service": service_id, "action": action, "result": result if isinstance(result, dict) else str(result or "")}
