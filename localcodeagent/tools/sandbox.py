"""Sandboxed script execution — run Python snippets in an isolated interpreter.

`python_exec` writes code to a temp file under .agent/sandbox/ and runs it via
``sys.executable -I`` (isolated mode: no user site-packages, no env PYTHONPATH,
cwd-bound) with a timeout and output caps. Gated by `shell.execute`; heavier
isolation belongs to the Docker manifest tool.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ..procutil import no_window_flags
from .base import ToolRegistry, ToolSpec

MAX_CODE_CHARS = 50000
MAX_OUTPUT = 20000
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600


def _sandbox_env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "SystemRoot": os.environ.get("SystemRoot", "")}
    for key in ("PYTHONIOENCODING", "TEMP", "TMP", "USERPROFILE", "HOME", "SystemDrive", "WINDIR", "ComSpec"):
        if key in os.environ:
            env[key] = os.environ[key]
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_python(code: str, sandbox_dir: Path, *, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    if len(code) > MAX_CODE_CHARS:
        raise ValueError(f"code exceeds {MAX_CODE_CHARS} characters")
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    script = sandbox_dir / f"exec_{uuid.uuid4().hex[:12]}.py"
    script.write_text(code, encoding="utf-8")
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-u", str(script)],
            cwd=str(sandbox_dir), env=_sandbox_env(),
            capture_output=True, text=True, timeout=timeout,
            creationflags=no_window_flags(),
            encoding="utf-8", errors="replace")
        return {
            "exit_code": proc.returncode,
            "elapsed_seconds": round(time.time() - started, 3),
            "stdout": (proc.stdout or "")[-MAX_OUTPUT:],
            "stderr": (proc.stderr or "")[-MAX_OUTPUT:],
            "ok": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "elapsed_seconds": timeout, "stdout": "",
                "stderr": f"execution timed out after {timeout}s", "ok": False}
    finally:
        try:
            script.unlink(missing_ok=True)
        except OSError:
            pass


def register_sandbox_tools(registry: ToolRegistry, workspace: Path) -> None:
    sandbox_dir = workspace / ".agent" / "sandbox"

    def python_exec(args: dict[str, Any]) -> str:
        code = str(args.get("code", ""))
        if not code.strip():
            return "ERROR: 'code' is required"
        timeout = max(1, min(int(args.get("timeout_seconds", DEFAULT_TIMEOUT)), MAX_TIMEOUT))
        try:
            return json.dumps(run_python(code, sandbox_dir, timeout=timeout), ensure_ascii=False)
        except ValueError as exc:
            return f"ERROR: {exc}"

    registry.register(ToolSpec(
        "python_exec",
        "Run a Python snippet in an isolated interpreter (-I mode) inside .agent/sandbox/ with a timeout and captured stdout/stderr. For data analysis, quick computations, and code validation.",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute"},
                "timeout_seconds": {"type": "integer", "default": DEFAULT_TIMEOUT},
            },
            "required": ["code"],
        },
        "shell.execute",
        python_exec,
        category="utilities",
        capabilities=["execute_code", "run_python", "sandbox_eval"],
    ))

    def sandbox_run(args: dict[str, Any]) -> str:
        from ..sandbox import Sandbox
        lang = str(args.get("language", "python")).lower()
        code = str(args.get("code") or args.get("command") or "")
        if not code.strip():
            return "ERROR: 'code' (or 'command') is required"
        kw = {
            "timeout": max(1, min(int(args.get("timeout_seconds", 120)), 900)),
            "mem_mb": max(64, min(int(args.get("memory_mb", 4096)), 16384)),
            "allow_network": bool(args.get("allow_network", False)),
        }
        with Sandbox(root=sandbox_dir / f"job-{id(args) & 0xffff:x}") as sb:
            if lang == "python":
                out = sb.run_python(code, **kw)
            elif lang in {"js", "javascript", "node"}:
                out = sb.run_js(code, **kw)
            else:
                out = sb.run_shell(code, **kw)
            out["artifacts"] = sb.artifacts()
        return json.dumps(out, ensure_ascii=False)

    registry.register(ToolSpec(
        "sandbox_run",
        "Run generated code/commands in a temporary sandbox with wall-clock timeout, a Windows Job Object memory cap + kill-on-close, optional network denial, and artifact listing. languages: python, javascript/node, shell. Prefer this over shell.execute for unknown generated code.",
        {
            "type": "object",
            "properties": {
                "language": {"type": "string",
                             "enum": ["python", "javascript", "node", "shell"],
                             "default": "python"},
                "code": {"type": "string", "description": "Source/command to run"},
                "timeout_seconds": {"type": "integer", "default": 120},
                "memory_mb": {"type": "integer", "default": 4096},
                "allow_network": {"type": "boolean", "default": False},
            },
            "required": ["code"],
        },
        "shell.execute",
        sandbox_run,
        category="utilities",
        capabilities=["execute_code", "sandbox", "test_runner", "isolation"],
    ))
