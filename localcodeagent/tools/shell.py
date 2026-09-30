from __future__ import annotations

import subprocess
from pathlib import Path

from .base import ToolRegistry, ToolSpec


def register_shell_tools(registry: ToolRegistry, workspace: Path) -> None:
    def run_shell(args: dict) -> str:
        command = str(args["command"])
        timeout = max(1, min(int(args.get("timeout", 120)), 600))
        proc = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        output = (proc.stdout or "") + (("\nSTDERR:\n" + proc.stderr) if proc.stderr else "")
        return f"EXIT_CODE={proc.returncode}\n{output[-20000:]}"

    registry.register(ToolSpec("run_shell", "Run a shell command in the current workspace and return its exit code and output.", {
        "type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]
    }, "shell.execute", run_shell))
