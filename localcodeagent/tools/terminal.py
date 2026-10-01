from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

MAX_OUTPUT = 20000


def resolve_shell(name: str) -> tuple[str, list[str]]:
    """Resolve a shell name to (shell_id, argv_prefix)."""
    name = str(name or "auto").strip().lower()
    on_windows = sys.platform.startswith("win")
    if name == "auto":
        name = "powershell" if on_windows and shutil.which("powershell") else "cmd" if on_windows else "bash"
    table = {
        "powershell": (["powershell", "-NoProfile", "-NonInteractive", "-Command"], "powershell"),
        "pwsh": (["pwsh", "-NoProfile", "-NonInteractive", "-Command"], "powershell"),
        "cmd": (["cmd", "/c"], "cmd"),
        "bash": (["bash", "-c"], "bash"),
        "sh": (["sh", "-c"], "bash"),
    }
    if name not in table:
        raise ValueError(f"unknown shell '{name}'; use auto|powershell|cmd|bash")
    argv, shell_id = table[name]
    if not shutil.which(argv[0]) and name != "cmd":
        raise FileNotFoundError(f"shell executable not found: {argv[0]}")
    return shell_id, argv


class TerminalTracker:
    """Tracks background terminal processes with log capture and kill support."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._prune_logs()

    def _prune_logs(self, keep: int = 200) -> None:
        """Each background job writes terminal-<job>.log; keep only the
        newest handful so long-running deployments do not accumulate files."""
        try:
            logs = sorted(
                self.log_dir.glob("terminal-*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in logs[keep:]:
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def spawn(self, argv: list[str], *, cwd: Path, env: dict[str, str] | None, job_id: str, command: str) -> dict[str, Any]:
        log_path = self.log_dir / f"terminal-{job_id}.log"
        handle = open(log_path, "a", encoding="utf-8", buffering=1, errors="replace")
        merged_env = dict(os.environ)
        if env:
            merged_env.update({str(k): str(v) for k, v in env.items()})
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        proc = subprocess.Popen(argv, cwd=str(cwd), env=merged_env, stdout=handle, stderr=subprocess.STDOUT, text=True, creationflags=flags)
        with self._lock:
            self._procs[job_id] = {
                "job_id": job_id,
                "pid": proc.pid,
                "command": command,
                "argv": argv,
                "started_at": time.time(),
                "log_path": str(log_path),
                "process": proc,
                "handle": handle,
            }
        return {"job_id": job_id, "pid": proc.pid, "log_path": str(log_path)}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for job_id, entry in self._procs.items():
                proc: subprocess.Popen = entry["process"]
                code = proc.poll()
                running = code is None
                if not running and entry["handle"]:
                    try:
                        entry["handle"].close()
                    except Exception:
                        pass
                    entry["handle"] = None
                rows.append({
                    "job_id": job_id,
                    "pid": entry["pid"],
                    "command": entry["command"],
                    "state": "running" if running else "finished",
                    "exit_code": code,
                    "started_at": entry["started_at"],
                    "uptime_seconds": int(time.time() - entry["started_at"]),
                    "log_path": entry["log_path"],
                })
            return sorted(rows, key=lambda r: r["started_at"], reverse=True)

    def kill(self, job_id_or_pid: str) -> dict[str, Any]:
        with self._lock:
            entry = self._procs.get(job_id_or_pid)
            if entry is None:
                entry = next((e for e in self._procs.values() if str(e["pid"]) == str(job_id_or_pid)), None)
            if entry is None:
                raise KeyError(f"unknown terminal process '{job_id_or_pid}'")
            proc: subprocess.Popen = entry["process"]
            if proc.poll() is None:
                # Kill the whole tree — a stopped job's children must not be
                # orphaned holding inherited pipes/handles open.
                if sys.platform.startswith("win"):
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True, timeout=10,
                        )
                    except Exception:
                        pass
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            if entry["handle"]:
                try:
                    entry["handle"].close()
                except Exception:
                    pass
                entry["handle"] = None
            return {"job_id": entry["job_id"], "pid": entry["pid"], "exit_code": proc.returncode}

    def shutdown(self) -> None:
        """Terminate all tracked background processes — they are children of
        this app and should not outlive it."""
        for job_id in list(self._procs):
            try:
                self.kill(job_id)
            except Exception:
                pass


def run_process_streaming(
    argv,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 120,
    shell: bool = False,
    sink=None,
    cancel_check=None,
    creationflags: int = 0,
) -> tuple[int | None, str, str, bool]:
    """Run a process and stream stdout/stderr chunks to sink(which, chunk).

    Returns (exit_code, stdout, stderr, timed_out). The full output is always
    returned; sink receives the same text incrementally while the process runs
    so callers can render a live terminal without waiting for exit.
    """
    out_parts: list[str] = []
    err_parts: list[str] = []

    def _reader(stream, parts, which) -> None:
        while True:
            chunk = stream.read(1024)
            if not chunk:
                break
            parts.append(chunk)
            if sink is not None:
                try:
                    sink(which, chunk)
                except Exception:
                    pass

    def _kill_tree() -> None:
        # shell=True wraps the command in a shell — killing only the wrapper
        # orphans the real child and leaves it holding our pipes open, so the
        # cancel path hangs until the child exits on its own. Windows uses
        # taskkill /T; POSIX runs the child in its own process group so the
        # whole tree dies with one killpg.
        if sys.platform.startswith("win"):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:
            pass

    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env, shell=shell, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags,
        # Own process group on POSIX so _kill_tree can killpg the wrapper and
        # every child it spawned.
        start_new_session=not sys.platform.startswith("win"),
    )
    threads = [
        threading.Thread(target=_reader, args=(proc.stdout, out_parts, "stdout"), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, err_parts, "stderr"), daemon=True),
    ]
    for t in threads:
        t.start()
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancel_check is not None:
            try:
                if cancel_check():
                    cancelled = True
                    _kill_tree()
                    break
            except Exception:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _kill_tree()
            break
        time.sleep(min(0.15, remaining))
    timed_out = timed_out or cancelled
    # Close our read ends first — a killed child's orphan may hold the write
    # side open, and closing unblocks the reader threads' read() immediately
    # instead of waiting out the join timeout.
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except Exception:
            pass
    for t in threads:
        t.join(timeout=5)
    return proc.returncode, "".join(out_parts), "".join(err_parts), timed_out


def _resolve_cwd(workspace: Path, raw: str) -> Path:
    cwd = (workspace / raw).resolve() if raw else workspace
    if not cwd.is_relative_to(workspace.resolve()):
        raise ValueError("cwd must stay inside the workspace")
    if not cwd.is_dir():
        raise FileNotFoundError(f"directory does not exist: {cwd}")
    return cwd


def register_terminal_tools(
    registry: ToolRegistry,
    workspace: Path,
    *,
    jobs=None,
    log_dir: Path | None = None,
    default_timeout: int = 120,
) -> TerminalTracker:
    tracker = TerminalTracker(log_dir or (workspace / ".agent" / "runtime"))

    def terminal_run(args: dict[str, Any]) -> str:
        command = str(args["command"])
        try:
            shell_id, prefix = resolve_shell(str(args.get("shell", "auto")))
        except (ValueError, FileNotFoundError) as exc:
            return f"ERROR: {exc}"
        try:
            cwd = _resolve_cwd(workspace, str(args.get("cwd", "") or ""))
        except (ValueError, FileNotFoundError) as exc:
            return f"ERROR: {exc}"
        env = args.get("env")
        merged_env = dict(os.environ)
        if isinstance(env, dict):
            merged_env.update({str(k): str(v) for k, v in env.items()})
        timeout = max(1, min(int(args.get("timeout_seconds", default_timeout)), 3600))
        argv = [*prefix, command]

        if bool(args.get("background", False)):
            if jobs is None:
                return "ERROR: background execution requires the Job Manager"
            job = jobs.submit("terminal", command[:140], metadata={"shell": shell_id, "cwd": str(cwd)})
            try:
                info = tracker.spawn(argv, cwd=cwd, env=env, job_id=job.id, command=command)
            except Exception as exc:
                jobs.update(job.id, state="failed", error=f"{type(exc).__name__}: {exc}")
                return f"ERROR: {type(exc).__name__}: {exc}"
            jobs.update(job.id, state="running", detail=f"pid {info['pid']}")
            return json.dumps({"ok": True, "background": True, "shell": shell_id, **info, "state": "running"}, ensure_ascii=False)

        started = time.time()
        sink = registry.context.get("stream_sink")

        def _emit_chunk(which: str, chunk: str) -> None:
            if sink is None:
                return
            try:
                sink("terminal_run", chunk)
            except Exception:
                pass

        checks = registry.context.get("cancel_checks") or {}
        tls = registry.context.get("task_tls")
        tid = str(getattr(tls, "task_id", "") or registry.context.get("task_id") or "")
        cancel_check = checks.get(tid)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        code, stdout, stderr, timed_out = run_process_streaming(
            argv, cwd=cwd, env=merged_env, timeout=timeout, sink=_emit_chunk,
            cancel_check=cancel_check, creationflags=flags
        )
        cancelled = False
        if timed_out:
            try:
                cancelled = bool(cancel_check and cancel_check())
            except Exception:
                pass
        payload = {
            "exit_code": code,
            "stdout": stdout[-MAX_OUTPUT:],
            "stderr": stderr[-8000:],
            "timed_out": timed_out and not cancelled,
            "cancelled": cancelled,
            "elapsed_seconds": round(time.time() - started, 3),
            "shell": shell_id,
        }
        return json.dumps(payload, ensure_ascii=False)

    def terminal_processes(args: dict[str, Any]) -> str:
        rows = tracker.list()
        if jobs is not None:
            for row in rows:
                if row["state"] == "finished":
                    try:
                        jobs.update(row["job_id"], state="completed" if row["exit_code"] == 0 else "failed",
                                    detail=f"exit {row['exit_code']}")
                    except KeyError:
                        pass
        return json.dumps({"processes": rows}, ensure_ascii=False)

    def terminal_kill(args: dict[str, Any]) -> str:
        target = str(args.get("pid") or args.get("job_id") or "")
        if not target:
            return "ERROR: pid or job_id is required"
        try:
            result = tracker.kill(target)
        except KeyError as exc:
            return f"ERROR: {exc}"
        if jobs is not None:
            try:
                jobs.update(result["job_id"], state="cancelled", detail="killed by user/agent")
            except KeyError:
                pass
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    registry.register(ToolSpec(
        "terminal_run",
        "Run a command in a controlled terminal (powershell/cmd/bash). Returns JSON with exit_code, stdout, stderr, elapsed time. Supports env vars, workspace-relative cwd, timeouts, and background mode tracked by the Job Manager.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "shell": {"type": "string", "enum": ["auto", "powershell", "pwsh", "cmd", "bash", "sh"], "default": "auto"},
                "cwd": {"type": "string", "description": "workspace-relative working directory"},
                "env": {"type": "object"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                "background": {"type": "boolean", "default": False},
            },
            "required": ["command"],
        },
        "shell.execute",
        terminal_run,
        category="devops",
        capabilities=["terminal_run", "terminal", "execute_process"],
    ))
    registry.register(ToolSpec(
        "terminal_processes",
        "List tracked background terminal processes with state, exit code, uptime, and log path.",
        {"type": "object", "properties": {}},
        "shell.execute",
        terminal_processes,
        category="devops",
        capabilities=["terminal_processes", "terminal"],
    ))
    registry.register(ToolSpec(
        "terminal_kill",
        "Terminate a tracked background terminal process by job_id or pid.",
        {"type": "object", "properties": {"job_id": {"type": "string"}, "pid": {"type": "integer"}}},
        "shell.execute",
        terminal_kill,
        category="devops",
        capabilities=["terminal_kill", "terminal"],
    ))
    return tracker
