"""MCP lifecycle tests: handshake, stderr draining, and tree kill."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from localcodeagent.mcp import MCPClient, MCPServerConfig, MCPError


# A fake MCP server that floods stderr continuously — without the client's
# stderr drain thread the ~300KB of noise fills the OS pipe buffer (~64KB)
# and the server deadlocks before answering requests.
_SERVER = textwrap.dedent(
    """
    import sys, json, threading
    def noise():
        for i in range(20000):
            try:
                print("noise line %d" % i, file=sys.stderr)
                sys.stderr.flush()
            except Exception:
                return
    threading.Thread(target=noise, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        if mid is None:
            continue
        method = msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {},
                      "serverInfo": {"name": "fake", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "echo",
                                 "inputSchema": {"type": "object"}}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\\n")
        sys.stdout.flush()
    """
)

# Wrapper-spawned server: writes the real server's pid to a file so the test
# can verify the *grandchild* (not just the shell) dies on close().
_PIDFILE_SERVER = textwrap.dedent(
    """
    import sys, json, os
    open(sys.argv[1], "w").write(str(os.getpid()))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        if mid is None:
            continue
        result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "f", "version": "0"}} \\
            if msg.get("method") == "initialize" else {"tools": []} if msg.get("method") == "tools/list" else {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\\n")
        sys.stdout.flush()
    """
)


def _pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], text=True)
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class MCPClientTests(unittest.TestCase):
    def test_handshake_and_tools_despite_stderr_flood(self) -> None:
        client = MCPClient(MCPServerConfig(
            id="fake",
            command=[sys.executable, "-c", _SERVER]), timeout=30.0)
        try:
            client.start()
            self.assertTrue(client.is_alive())
            tools = client.list_tools()
            self.assertEqual([t["name"] for t in tools], ["echo"])
        finally:
            client.close()
        self.assertFalse(client.is_alive())

    def test_close_kills_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = str(Path(tmp) / "child.pid")
            server_py = Path(tmp) / "server.py"
            server_py.write_text(_PIDFILE_SERVER)
            # Paths used here (sys.executable, tempdir) contain no spaces, so
            # cmd /c can take them unquoted — avoids cmd's outer-quote stripping.
            inner = f"{sys.executable} {server_py} {pidfile}"
            if sys.platform.startswith("win"):
                command = ["cmd.exe", "/c", inner]
            else:
                command = ["sh", "-c", inner]
            client = MCPClient(MCPServerConfig(id="tree", command=command), timeout=30.0)
            client.start()
            for _ in range(50):
                if Path(pidfile).exists():
                    break
                import time
                time.sleep(0.1)
            child_pid = int(Path(pidfile).read_text().strip())
            self.assertTrue(_pid_alive(child_pid))
            client.close()
            for _ in range(50):
                if not _pid_alive(child_pid):
                    break
                import time
                time.sleep(0.1)
            self.assertFalse(
                _pid_alive(child_pid),
                "shell-wrapped MCP child survived close() — orphaned process")

    def test_secret_env_requires_resolver(self) -> None:
        client = MCPClient(MCPServerConfig(
            id="sec",
            command=[sys.executable, "-c", "pass"],
            env={"API_KEY": "secret:missing"}))
        with self.assertRaises(MCPError):
            client.start()

    def test_secret_env_resolves_through_vault(self) -> None:
        client = MCPClient(MCPServerConfig(
            id="sec",
            command=[sys.executable, "-c", "pass"],
            env={"API_KEY": "secret:k"}),
            env_resolver=lambda name: "resolved-value" if name == "k" else None)
        self.assertEqual(client._resolved_env()["API_KEY"], "resolved-value")


if __name__ == "__main__":
    unittest.main()
