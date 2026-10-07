"""Execution sandboxes for generated/untrusted code.

``Sandbox.run()`` executes a command in a temporary workspace with:
- wall-clock timeout
- Windows Job Object limits (memory cap + kill-on-close) when available
- optional network denial (best-effort env isolation + flag)
- output caps
- artifact extraction into a caller-chosen directory
- guaranteed cleanup

This upgrades the lightweight ``tools/sandbox.py`` python_exec path into a
general multi-language sandbox; Docker remains the heavier isolation tier.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900
MAX_OUTPUT = 40000
DEFAULT_MEM_MB = 4096

# Windows Job Object flags (best-effort; ignored on other platforms).
_JOB_LIMIT_MEMORY = 0x00000200
_JOB_KILL_ON_CLOSE = 0x00002000


def _assign_job_limits(proc: subprocess.Popen, mem_mb: int) -> Any:
    """Put the child in a kill-on-close Job with a memory ceiling."""
    if os.name != "nt" or mem_mb <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount",
                         "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation",
                         JOBOBJECT_BASIC_LIMIT_INFORMATION),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_LIMIT_MEMORY | _JOB_KILL_ON_CLOSE)
        info.JobMemoryLimit = mem_mb * 1024 * 1024
        if not kernel.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel.CloseHandle(job)
            return None
        if not kernel.AssignProcessToJobObject(job, int(proc._handle)):
            kernel.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _clean_env(allow_network: bool) -> dict[str, str]:
    env = {"SystemRoot": os.environ.get("SystemRoot", ""),
           "WINDIR": os.environ.get("WINDIR", ""),
           "ComSpec": os.environ.get("ComSpec", ""),
           "PATH": os.environ.get("PATH", ""),
           "PYTHONIOENCODING": "utf-8",
           "TEMP": os.environ.get("TEMP", ""),
           "TMP": os.environ.get("TMP", "")}
    if not allow_network:
        # Best-effort: proxy vars redirect to a dead loopback endpoint so
        # libs honoring proxies fail fast. True net isolation needs a
        # firewall/container tier — the flag is recorded in the result.
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = "http://127.0.0.1:9"
        env["NO_PROXY"] = ""
    return {k: v for k, v in env.items() if v}


class Sandbox:
    """One temporary sandboxed workspace + bounded process execution."""

    def __init__(self, root: Path | None = None, *,
                 prefix: str = "nexus-sbx") -> None:
        self.root = Path(root) if root else Path(
            tempfile.mkdtemp(prefix=prefix))
        self.root.mkdir(parents=True, exist_ok=True)
        self.id = f"sbx-{uuid.uuid4().hex[:10]}"

    def run(self, argv: list[str], *, timeout: float = DEFAULT_TIMEOUT,
            mem_mb: int = DEFAULT_MEM_MB, allow_network: bool = False,
            stdin: str = "", cwd: Path | str | None = None) -> dict[str, Any]:
        """Run argv inside the sandbox workspace. Never raises on child
        failure — the result dict carries ok/timeout/exit/stdout/stderr.

        ``cwd`` may point at an external directory (e.g. the project repo)
        when the command needs a real working tree; the sandbox workspace
        itself remains the scratch root for artifacts and is always the
        thing cleaned up — never pass the workspace as ``root``.
        """
        timeout = max(1.0, min(float(timeout), MAX_TIMEOUT))
        started = time.time()
        job = None
        try:
            from ..procutil import no_window_flags
            proc = subprocess.Popen(
                [str(a) for a in argv], cwd=str(cwd or self.root),
                env=_clean_env(allow_network),
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=no_window_flags())
            job = _assign_job_limits(proc, mem_mb)
            try:
                out, err = proc.communicate(input=stdin or None,
                                            timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                timed_out = True
            return {
                "ok": proc.returncode == 0 and not timed_out,
                "exit": proc.returncode if not timed_out else -9,
                "timed_out": timed_out,
                "stdout": (out or "")[-MAX_OUTPUT:],
                "stderr": (err or "")[-MAX_OUTPUT:],
                "elapsed_s": round(time.time() - started, 3),
                "network_allowed": bool(allow_network),
                "mem_limit_mb": mem_mb,
                "job_limited": job is not None,
            }
        except OSError as exc:
            return {"ok": False, "exit": -1, "timed_out": False,
                    "stdout": "", "stderr": str(exc),
                    "elapsed_s": round(time.time() - started, 3),
                    "network_allowed": bool(allow_network),
                    "mem_limit_mb": mem_mb, "job_limited": False}
        finally:
            if job:
                try:
                    import ctypes
                    ctypes.windll.kernel32.CloseHandle(job)
                except Exception:
                    pass

    def run_python(self, code: str, **kw: Any) -> dict[str, Any]:
        script = self.root / "snippet.py"
        script.write_text(code, encoding="utf-8")
        return self.run([sys.executable, "-I", str(script)], **kw)

    def run_js(self, code: str, **kw: Any) -> dict[str, Any]:
        script = self.root / "snippet.js"
        script.write_text(code, encoding="utf-8")
        return self.run(["node", str(script)], **kw)

    def run_shell(self, command: str, **kw: Any) -> dict[str, Any]:
        if os.name == "nt":
            return self.run(["cmd.exe", "/d", "/c", command], **kw)
        return self.run(["/bin/sh", "-c", command], **kw)

    def artifacts(self, patterns: list[str] | None = None) -> list[dict]:
        """List files produced inside the workspace (for extraction)."""
        out = []
        for p in self.root.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(self.root))
                if patterns is None or any(
                        p.match(pat) or pat in rel for pat in patterns):
                    out.append({"path": rel, "size": p.stat().st_size})
        return sorted(out, key=lambda a: a["path"])[:200]

    def extract(self, dest: Path, patterns: list[str] | None = None) -> list[str]:
        """Copy workspace artifacts into dest; returns extracted paths."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        moved = []
        for art in self.artifacts(patterns):
            src = self.root / art["path"]
            tgt = dest / art["path"]
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tgt)
            moved.append(art["path"])
        return moved

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()
