"""Minimal Language Server Protocol client over stdio (Part C).

``LspClient`` speaks Content-Length-framed JSON-RPC: initialize,
didOpen, definition, references, documentSymbol, workspace/symbol,
hover (type info), and publishDiagnostics collection. The pool maps
file extensions to server commands — Python via ``pylsp`` when
installed; others can be registered. When no server exists for a
language, callers get ``ok: False`` and fall back to the codeintel
regex/AST path — nothing pretends.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# Language → candidate server commands (first found wins).
SERVER_CANDIDATES = {
    ".py": [["pylsp"], ["python", "-m", "pylsp"]],
    ".js": [["typescript-language-server", "--stdio"]],
    ".ts": [["typescript-language-server", "--stdio"]],
    ".cs": [["csharp-ls"]],
}
DID_OPEN_WAIT = 0.15


def _which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


class LspClient:
    """One stdio JSON-RPC session to a language server."""

    def __init__(self, cmd: list[str], root: Path, *, timeout: float = 15.0):
        self.cmd = cmd
        self.root = Path(root)
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._diag: dict[str, list] = {}
        self._reader: threading.Thread | None = None
        self._alive = False

    # -- transport --------------------------------------------------------

    def start(self) -> bool:
        try:
            self._proc = subprocess.Popen(
                self.cmd, cwd=self.root, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return False
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop,
                                        daemon=True)
        try:
            self._reader.start()
        except Exception:
            # No reader means the child blocks on a full pipe — kill it and
            # report the client as down instead of hanging in _initialize.
            self._alive = False
            try:
                self._proc.kill()
            except Exception:
                pass
            return False
        return self._initialize()

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self._proc.stdin.flush()

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc and proc.stdout
        buf = proc.stdout
        while self._alive:
            try:
                headers = {}
                while True:
                    line = buf.readline()
                    if not line:
                        self._alive = False
                        return
                    line = line.strip()
                    if not line:
                        break
                    k, _, v = line.decode("ascii", "replace").partition(":")
                    headers[k.strip().lower()] = v.strip()
                n = int(headers.get("content-length", 0))
                if n <= 0 or n > 16_000_000:
                    continue
                msg = json.loads(buf.read(n).decode("utf-8"))
            except Exception:
                self._alive = False
                return
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._lock:
                slot = self._pending.pop(msg["id"], None)
                if slot is not None:
                    slot["msg"] = msg
                    slot["event"].set()
        elif msg.get("method") == "textDocument/publishDiagnostics":
            uri = msg["params"].get("uri", "")
            self._diag[uri] = msg["params"].get("diagnostics", [])

    def _request(self, method: str, params: dict) -> dict:
        if not self._alive:
            return {"ok": False, "error": "server not running"}
        self._id += 1
        slot: dict = {"event": threading.Event(), "msg": None}
        with self._lock:
            self._pending[self._id] = slot
        self._send({"jsonrpc": "2.0", "id": self._id,
                    "method": method, "params": params})
        if not slot["event"].wait(self.timeout):
            return {"ok": False, "error": f"{method} timed out"}
        msg = slot["msg"] or {}
        if "error" in msg:
            return {"ok": False, "error": str(msg["error"])[:300]}
        return {"ok": True, "result": msg.get("result")}

    def _notify(self, method: str, params: dict) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method,
                        "params": params})
        except Exception:
            pass

    # -- protocol ----------------------------------------------------------

    def _initialize(self) -> bool:
        out = self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": self.root.as_uri(),
            "capabilities": {
                "textDocument": {
                    "definition": {}, "references": {},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "hover": {}, "publishDiagnostics": {},
                },
                "workspace": {"symbol": {}},
            },
        })
        if not out.get("ok"):
            return False
        self._notify("initialized", {})
        return True

    def did_open(self, path: Path) -> None:
        text = ""
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": Path(path).resolve().as_uri(),
                "languageId": path.suffix.lstrip("."),
                "version": 1, "text": text}})
        time.sleep(DID_OPEN_WAIT)

    def _pos(self, path: Path, line: int, char: int) -> dict:
        return {"textDocument": {"uri": Path(path).resolve().as_uri()},
                "position": {"line": max(0, line - 1),
                             "character": max(0, char)}}

    def definition(self, path: Path, line: int, char: int = 0) -> dict:
        return self._request("textDocument/definition",
                             self._pos(path, line, char))

    def references(self, path: Path, line: int, char: int = 0) -> dict:
        p = self._pos(path, line, char)
        p["context"] = {"includeDeclaration": True}
        return self._request("textDocument/references", p)

    def document_symbols(self, path: Path) -> dict:
        return self._request("textDocument/documentSymbol", {
            "textDocument": {"uri": Path(path).resolve().as_uri()}})

    def workspace_symbols(self, query: str) -> dict:
        return self._request("workspace/symbol", {"query": query})

    def hover(self, path: Path, line: int, char: int = 0) -> dict:
        return self._request("textDocument/hover",
                             self._pos(path, line, char))

    def diagnostics(self, path: Path) -> dict:
        uri = Path(path).resolve().as_uri()
        return {"ok": True, "diagnostics": self._diag.get(uri, [])}

    def shutdown(self) -> None:
        try:
            if self._alive:
                self._request("shutdown", {})
                self._notify("exit", {})
        except Exception:
            pass
        self._alive = False
        try:
            if self._proc:
                self._proc.terminate()
                self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        try:
            if self._proc:
                for stream in (self._proc.stdin, self._proc.stdout):
                    try:
                        stream.close()
                    except Exception:
                        pass
        except Exception:
            pass


class LspPool:
    """One client per language server command, lazily started."""

    def __init__(self, workspace: Path,
                 server_map: dict[str, list[list[str]]] | None = None) -> None:
        self.workspace = Path(workspace)
        self.server_map = dict(SERVER_CANDIDATES)
        if server_map:
            self.server_map.update(server_map)
        self._clients: dict[str, LspClient] = {}
        self._lock = threading.Lock()

    def _command_for(self, ext: str) -> list[str] | None:
        for cand in self.server_map.get(ext, []):
            if _which(cand[0]):
                return cand
        return None

    def client_for(self, path: Path) -> LspClient | None:
        ext = Path(path).suffix.lower()
        cmd = self._command_for(ext)
        if cmd is None:
            return None
        key = " ".join(cmd)
        with self._lock:
            client = self._clients.get(key)
            if client is None or not client._alive:
                client = LspClient(cmd, self.workspace)
                if not client.start():
                    return None
                self._clients[key] = client
            return client

    def supported(self, path: Path) -> bool:
        return self._command_for(Path(path).suffix.lower()) is not None

    def status(self) -> dict[str, Any]:
        return {"servers": {k: c._alive for k, c in self._clients.items()},
                "candidates": {ext: [c[0] for c in cmds]
                               for ext, cmds in self.server_map.items()}}

    def shutdown_all(self) -> None:
        for c in self._clients.values():
            c.shutdown()
