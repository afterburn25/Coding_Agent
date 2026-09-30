from __future__ import annotations

import subprocess
from pathlib import Path

from .base import ToolRegistry, ToolSpec


def register_git_tools(registry: ToolRegistry, workspace: Path) -> None:
    def git_status(_: dict) -> str:
        proc = subprocess.run(["git", "status", "--short", "--branch"], cwd=workspace, text=True, capture_output=True)
        return (proc.stdout + proc.stderr).strip() or "clean"

    def git_diff(args: dict) -> str:
        cmd = ["git", "diff"]
        if args.get("staged"):
            cmd.append("--staged")
        proc = subprocess.run(cmd, cwd=workspace, text=True, capture_output=True)
        return (proc.stdout + proc.stderr)[-30000:] or "no diff"

    registry.register(ToolSpec("git_status", "Show repository branch and changed files.", {
        "type": "object", "properties": {}
    }, "filesystem.read", git_status))
    registry.register(ToolSpec("git_diff", "Show the current Git diff.", {
        "type": "object", "properties": {"staged": {"type": "boolean"}}
    }, "filesystem.read", git_diff))
