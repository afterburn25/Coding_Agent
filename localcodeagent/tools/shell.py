from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .base import ToolRegistry, ToolSpec
from .terminal import run_process_streaming


def register_shell_tools(registry: ToolRegistry, workspace: Path) -> None:
    def run_shell(args: dict) -> str:
        command = str(args["command"])
        timeout = max(1, min(int(args.get("timeout", 120)), 600))
        sink = registry.context.get("stream_sink")

        def _emit_chunk(which: str, chunk: str) -> None:
            if sink is None:
                return
            try:
                sink("run_shell", chunk)
            except Exception:
                pass

        checks = registry.context.get("cancel_checks") or {}
        tls = registry.context.get("task_tls")
        tid = str(getattr(tls, "task_id", "") or registry.context.get("task_id") or "")
        cancel_check = checks.get(tid)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        code, stdout, stderr, timed_out = run_process_streaming(
            command, cwd=workspace, timeout=timeout, shell=True, sink=_emit_chunk,
            cancel_check=cancel_check, creationflags=flags,
        )
        output = stdout + (("\nSTDERR:\n" + stderr) if stderr else "")
        if timed_out:
            cancelled = False
            try:
                cancelled = bool(cancel_check and cancel_check())
            except Exception:
                pass
            output += "\n[cancelled]" if cancelled else "\n[timed out]"
        return f"EXIT_CODE={code}\n{output[-20000:]}"

    registry.register(ToolSpec("run_shell", "Run a shell command in the current workspace and return its exit code and output.", {
        "type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]
    }, "shell.execute", run_shell))
