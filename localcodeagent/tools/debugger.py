"""Headless debugger — breakpoints + post-mortem locals for Python scripts.

`debug_run` executes a workspace-bounded script in a subprocess under a
``bdb`` driver that records frame locals at each breakpoint hit and dumps
a full post-mortem frame walk on uncaught exceptions. No interactive
debugger required — the agent sets breakpoints, gets state back as JSON.

The driver source is embedded (``_RUNNER_SRC``) and written to a temp
file per invocation: under PyInstaller the package ships as .pyc in the
PYZ archive, so ``localcodeagent/*.py`` cannot be invoked by path.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time

from ..procutil import no_window_flags
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

_RUNNER_SRC = r'''
import bdb, json, runpy, sys, traceback
from pathlib import Path


def _fmt(v, limit=240):
    try:
        r = repr(v)
    except Exception as e:
        r = f"<unrepresentable: {e}>"
    return r if len(r) <= limit else r[:limit] + "…"


def _frame_locals(frame, cap=40):
    return {k: _fmt(v) for k, v in list(frame.f_locals.items())[:cap]}


def _stack(frame, depth=12):
    out = []
    while frame is not None and len(out) < depth:
        out.append({"file": frame.f_code.co_filename,
                    "line": frame.f_lineno,
                    "function": frame.f_code.co_name})
        frame = frame.f_back
    return out


class _Dbg(bdb.Bdb):
    def __init__(self, max_hits):
        super().__init__()
        self.hits = []
        self.max_hits = max_hits

    def user_line(self, frame):
        if self.break_here(frame) and len(self.hits) < self.max_hits:
            self.hits.append({
                "file": frame.f_code.co_filename,
                "line": frame.f_lineno,
                "function": frame.f_code.co_name,
                "locals": _frame_locals(frame),
                "stack": _stack(frame),
            })
        self.set_continue()


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--script", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--break", dest="breaks", action="append", default=[])
    p.add_argument("--max-hits", type=int, default=50)
    p.add_argument("args", nargs=argparse.REMAINDER)
    ns = p.parse_args()
    script = str(Path(ns.script).resolve())
    dbg = _Dbg(ns.max_hits)
    bps = []
    for spec in ns.breaks:
        f, _, line = spec.rpartition(":")
        try:
            err = dbg.set_break(str(Path(f).resolve()), int(line))
        except Exception as e:
            err = str(e)
        bps.append({"file": f, "line": int(line or 0),
                    "ok": err is None, "error": err})
    result = {"hits": dbg.hits, "breakpoints": bps,
              "exception": None, "exit": "ok"}
    extra = list(ns.args)
    if extra[:1] == ["--"]:
        extra = extra[1:]
    sys.argv = [script] + extra
    try:
        dbg.runctx(
            "runpy.run_path(script, run_name='__main__')",
            {"runpy": runpy, "script": script}, {})
    except SystemExit as e:
        result["exit"] = f"SystemExit({e.code})"
    except bdb.BdbQuit:
        result["exit"] = "bdb_quit"
    except BaseException:
        exc_type, exc, tb = sys.exc_info()
        frames = []
        for fr, ln in traceback.walk_tb(tb):
            frames.append({"file": fr.f_code.co_filename, "line": ln,
                           "function": fr.f_code.co_name,
                           "locals": _frame_locals(fr)})
        result["exception"] = {
            "type": getattr(exc_type, "__name__", str(exc_type)),
            "message": str(exc), "frames": frames}
        result["exit"] = "exception"
    finally:
        result["hits"] = dbg.hits
        Path(ns.out).write_text(json.dumps(result, default=str),
                                encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _truncate(text: str, limit: int = 12000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def register_debug_tools(registry: ToolRegistry, workspace: Path, *,
                         extra_roots=None) -> None:

    def _roots() -> list[Path]:
        roots = [workspace.resolve()]
        if extra_roots:
            try:
                roots += [Path(r).resolve() for r in extra_roots()]
            except Exception:
                pass
        return roots

    def _inside(path: Path) -> Path | None:
        try:
            resolved = path.resolve()
        except OSError:
            return None
        for root in _roots():
            try:
                if resolved.is_relative_to(root):
                    return resolved
            except OSError:
                continue
        return None

    def debug_run(args: dict[str, Any]) -> str:
        script = args.get("script") or ""
        target = _inside(workspace / script) if script else None
        if target is None or not target.is_file():
            return json.dumps(
                {"error": "script must be a file inside a registered workspace"})
        bps = []
        for bp in (args.get("breakpoints") or [])[:20]:
            bp_file, bp_line = bp.get("file"), bp.get("line")
            bp_path = _inside(workspace / bp_file) if bp_file else None
            if bp_path is None or not isinstance(bp_line, int) or bp_line < 1:
                return json.dumps(
                    {"error": f"invalid breakpoint {bp!r} — file must be "
                              "inside a workspace, line a positive int"})
            bps.append((bp_path, bp_line))
        timeout = min(int(args.get("timeout") or 60), 300)
        with tempfile.TemporaryDirectory(prefix="nexus-dbg-") as td:
            runner = Path(td) / "runner.py"
            out = Path(td) / "result.json"
            runner.write_text(_RUNNER_SRC, encoding="utf-8")
            cmd = [sys.executable, str(runner), "--script", str(target),
                   "--out", str(out)]
            for f, line in bps:
                cmd += ["--break", f"{f}:{line}"]
            cmd.append("--")
            cmd += [str(a) for a in (args.get("args") or [])]
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd, cwd=target.parent, capture_output=True, text=True,
                    timeout=timeout, creationflags=no_window_flags())
                timed_out = False
            except subprocess.TimeoutExpired as exc:
                proc = None
                timed_out = True
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
            if proc is not None:
                stdout, stderr = proc.stdout, proc.stderr
            payload: dict[str, Any] = {
                "script": str(target),
                "cwd": str(target.parent),
                "duration_seconds": round(time.monotonic() - started, 3),
                "timed_out": timed_out,
                "stdout": _truncate(stdout or ""),
                "stderr": _truncate(stderr or ""),
            }
            if proc is not None:
                payload["returncode"] = proc.returncode
            if out.is_file():
                try:
                    payload["debug"] = json.loads(
                        out.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload["debug"] = {"error": "runner result unreadable"}
            else:
                payload["debug"] = {
                    "error": "no result — process died before writing output"}
            return json.dumps(payload, ensure_ascii=False)

    registry.register(ToolSpec(
        "debug_run",
        "Run a Python script under a headless debugger: set breakpoints "
        "(file+line) to capture frame locals at each hit, and get a full "
        "post-mortem frame walk with locals on uncaught exceptions. "
        "Returns JSON with hits, exception frames, stdout/stderr.",
        {
            "type": "object",
            "properties": {
                "script": {"type": "string",
                           "description": "python file inside the workspace"},
                "args": {"type": "array", "items": {"type": "string"},
                         "description": "argv for the script"},
                "breakpoints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "line": {"type": "integer"},
                        },
                        "required": ["file", "line"],
                    },
                    "description": "breakpoints to capture locals at (max 20)"},
                "timeout": {"type": "integer",
                            "description": "seconds (default 60, max 300)"},
            },
            "required": ["script"],
        },
        "shell.execute",
        debug_run,
        category="coding",
        capabilities=["debug_run"],
    ))
