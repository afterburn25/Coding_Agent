"""Motor Cortex — controlled execution of selected actions.

Reasoning regions never execute directly. A Basal-Ganglia selection becomes
an ExecutionRequest here; the Motor Cortex dispatches through registered
executors (tool router capability, sandboxed shell, file ops, git, tests,
MCP, browser, computer use) and always emits a structured ExecutionResult
with full telemetry — duration, exit status, provenance, mission/task ids,
retry count.

Approval gates are preserved: executors may flag an action as requiring
approval; the request must arrive approved or the result is a refusal,
never an execution.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .events import CognitiveEvent, EventType
from .regions import BrainRegion
from . import events as ev


@dataclass(slots=True)
class ExecutionResult:
    action: str
    ok: bool
    output: str = ""
    error: str = ""
    exit_status: str = ""            # ok|error|cancelled|denied|approval_required
    duration_ms: float = 0.0
    tool: str = ""
    attempts: int = 0
    execution_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def as_dict(self) -> dict[str, Any]:
        return {"execution_id": self.execution_id, "action": self.action,
                "ok": self.ok, "output": self.output[:2000],
                "error": self.error[:500], "exit_status": self.exit_status,
                "duration_ms": self.duration_ms, "tool": self.tool,
                "attempts": self.attempts}


# capability name → executors are registered callables
#   fn(arguments, approved) -> (ok: bool, output: str)
class MotorCortex(BrainRegion):
    name = ev.REGION_MOTOR

    def __init__(self, bus, *, tool_router=None, sandbox=None) -> None:
        super().__init__(bus)
        self.tool_router = tool_router
        self.sandbox = sandbox
        self._executors: dict[str, Callable[[dict, bool], tuple[bool, str]]] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.RLock()
        self._telemetry: list[dict] = []

    def register_executor(self, action: str,
                          fn: Callable[[dict, bool], tuple[bool, str]],
                          *, requires_approval: bool = False) -> None:
        self._executors[action] = fn
        if requires_approval:
            # Marked so execute() refuses without approved=True even if the
            # executor itself forgets to check.
            setattr(self, f"_approval_required_{action}", True)

    def cancel(self, execution_id: str) -> None:
        with self._lock:
            self._cancelled.add(execution_id)

    def execute(self, action: str, arguments: dict[str, Any] | None = None, *,
                approved: bool = False, tool: str = "",
                correlation_id: str = "", mission_id: str = "",
                task_id: str = "", timeout_s: float = 120.0) -> ExecutionResult:
        started = time.monotonic()
        arguments = dict(arguments or {})
        req_id = uuid.uuid4().hex[:12]
        self.publish(EventType.EXECUTION_REQUEST, {
            "execution_id": req_id, "action": action, "tool": tool,
            "args_preview": str(arguments)[:200], "approved": approved,
        }, correlation_id=correlation_id, mission_id=mission_id,
            task_id=task_id, provenance=self.name)

        with self._lock:
            cancelled = req_id in self._cancelled
        if cancelled:
            result = ExecutionResult(action, False, exit_status="cancelled",
                                     execution_id=req_id)
            return self._finish(result, started, correlation_id, mission_id, task_id)

        if getattr(self, f"_approval_required_{action}", False) and not approved:
            result = ExecutionResult(action, False,
                                     exit_status="approval_required",
                                     error="action requires approval",
                                     execution_id=req_id)
            return self._finish(result, started, correlation_id, mission_id, task_id)

        result = self._dispatch(action, arguments, approved=approved,
                                tool=tool, req_id=req_id)
        return self._finish(result, started, correlation_id, mission_id, task_id)

    def _dispatch(self, action: str, arguments: dict, *, approved: bool,
                  tool: str, req_id: str) -> ExecutionResult:
        # 1) a registered executor takes precedence
        fn = self._executors.get(action)
        if fn is not None:
            try:
                ok, out = fn(arguments, approved)
                return ExecutionResult(action, ok, output=str(out)[:8000],
                                       exit_status="ok" if ok else "error",
                                       tool=tool or action,
                                       error="" if ok else str(out)[:400],
                                       execution_id=req_id)
            except Exception as exc:
                return ExecutionResult(action, False,
                                       error=f"{type(exc).__name__}: {exc}"[:400],
                                       exit_status="error",
                                       tool=tool or action, execution_id=req_id)
        # 2) capability route through the tool router
        if self.tool_router is not None:
            try:
                r = self.tool_router.execute(action, arguments, approved=approved)
            except Exception as exc:
                return ExecutionResult(action, False,
                                       error=f"{type(exc).__name__}: {exc}"[:400],
                                       exit_status="error", execution_id=req_id)
            if r.get("ok"):
                return ExecutionResult(action, True, output=str(r.get("result"))[:8000],
                                       exit_status="ok", tool=r.get("tool", ""),
                                       attempts=len(r.get("attempts") or []),
                                       execution_id=req_id)
            err = str(r.get("error") or "failed")
            return ExecutionResult(action, False, error=err[:400],
                                   exit_status=err, tool=str(r.get("tool") or ""),
                                   attempts=len(r.get("attempts") or []),
                                   execution_id=req_id)
        return ExecutionResult(action, False, error="no executor registered",
                               exit_status="error", execution_id=req_id)

    def _finish(self, result: ExecutionResult, started: float,
                correlation_id: str, mission_id: str, task_id: str) -> ExecutionResult:
        result.duration_ms = round((time.monotonic() - started) * 1000, 1)
        self.publish(EventType.EXECUTION_RESULT, {
            **result.as_dict(),
            "mission_id": mission_id, "task_id": task_id,
        }, correlation_id=correlation_id, mission_id=mission_id,
            task_id=task_id, provenance=self.name)
        with self._lock:
            self._telemetry.append({
                "ts": time.time(), "action": result.action, "ok": result.ok,
                "duration_ms": result.duration_ms, "tool": result.tool,
                "exit_status": result.exit_status, "mission_id": mission_id,
                "task_id": task_id})
            self._telemetry = self._telemetry[-500:]
        return result

    def telemetry(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._telemetry)[-limit:]

    def status(self) -> dict[str, Any]:
        base = super().status()
        base["executors"] = sorted(self._executors)
        base["recent_executions"] = self._telemetry[-5:]
        return base
