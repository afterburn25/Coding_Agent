from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(slots=True)
class ManagedService:
    """A controllable local service/process known to Chat Nexus.

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


class ProcessManager:
    """Central registry of Chat Nexus-managed and attached services."""

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
                       max_restarts: int = 3) -> None:
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_running = True
        restarts: dict[str, list[float]] = {}

        def loop() -> None:
            while self._watchdog_running:
                time.sleep(max(0.05, interval))
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
