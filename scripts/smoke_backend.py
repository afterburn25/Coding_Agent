"""Post-build backend smoke test — catches late-init crashes.

The packaging handshake alone is insufficient: the 0.18.2 build answered
the early handshake then died in ``_start_source_sync`` (a NameError that
only ran at ~95% init). This script therefore launches the built exe,
waits for the ``[nexus-port]`` stdout marker, then keeps polling
``/api/nexus/state`` through a settle window and fails if the process
exits or the state endpoint stops answering.

Usage: python scripts/smoke_backend.py [path-to-ChatNexus.Backend.exe]
Env:   NEXUS_SMOKE_SETTLE_S (default 20)  NEXUS_SMOKE_PORT_WAIT_S (60)
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = ROOT / "dist" / "ChatNexus" / "backend" / "ChatNexus.Backend.exe"
PORT_MARKER = "[nexus-port] "

SETTLE_S = float(os.environ.get("NEXUS_SMOKE_SETTLE_S", "20"))
PORT_WAIT_S = float(os.environ.get("NEXUS_SMOKE_PORT_WAIT_S", "60"))


def _poll_state(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/nexus/state",
                timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def main() -> int:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_EXE
    if not exe.is_file():
        print(f"FAIL: backend exe not found: {exe}")
        return 2

    proc = subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1,
    )
    lines: queue.Queue[str] = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout or []:
                lines.put(line)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()

    try:
        # Phase 0 — wait for the [nexus-port] marker (emitted after bind,
        # before late init) or process exit.
        port = 0
        deadline = time.time() + PORT_WAIT_S
        while time.time() < deadline and not port:
            if proc.poll() is not None:
                print(f"FAIL: backend exited during startup "
                      f"(code {proc.returncode})")
                return 1
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line.startswith(PORT_MARKER):
                try:
                    port = int(line[len(PORT_MARKER):].strip())
                except ValueError:
                    pass
        if not port:
            print("FAIL: backend never emitted [nexus-port] marker")
            return 1

        # Phase 1 — API answers.
        deadline = time.time() + PORT_WAIT_S
        state = None
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"FAIL: backend exited before API ready "
                      f"(code {proc.returncode})")
                return 1
            state = _poll_state(port)
            if state is not None:
                break
            time.sleep(0.5)
        if state is None:
            print("FAIL: /api/nexus/state never answered")
            return 1
        print(f"backend up on :{port} — state={state.get('state')!r}")

        # Phase 2 — survive the settle window (late init runs here:
        # source sync, runtime restore, mission resume).
        deadline = time.time() + SETTLE_S
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"FAIL: backend exited during late init "
                      f"(code {proc.returncode})")
                return 1
            if _poll_state(port) is None:
                print("FAIL: /api/nexus/state stopped answering")
                return 1
            time.sleep(1.0)

        state = _poll_state(port) or {}
        print(f"PASS: backend stable after {SETTLE_S:.0f}s settle "
              f"(state={state.get('state')!r})")
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
