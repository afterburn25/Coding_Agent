"""Model Context Protocol (MCP) integration.

Stdio JSON-RPC 2.0 client + server lifecycle manager. MCP tools are imported
into the central ToolRegistry with ``source="mcp"`` so they coexist with native
tools under the same permission, enable/disable, and manifest surfaces.

Transport: newline-delimited JSON-RPC messages over the server process' stdin/
stdout, per the MCP stdio transport spec.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools.base import ToolRegistry, ToolSpec
from .tools.terminal import _assign_kill_job, _terminate_kill_job

MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass(slots=True)
class MCPServerConfig:
    id: str
    name: str = ""
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"  # stdio | http
    enabled: bool = True
    auto_start: bool = True
    permission: str = "shell.execute"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MCPServerConfig":
        command = raw.get("command", [])
        if isinstance(command, str):
            command = [command]
        url = str(raw.get("url") or raw.get("endpoint") or "").strip()
        transport = str(raw.get("transport") or ("http" if url else "stdio")).strip().lower()
        # HTTP servers call remote services — default to the external_api gate.
        default_permission = "external_api.call" if transport == "http" else "shell.execute"
        return cls(
            id=str(raw.get("id") or raw.get("name") or "").strip(),
            name=str(raw.get("name") or raw.get("id") or "").strip(),
            command=[str(c) for c in command],
            env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
            cwd=str(raw.get("cwd") or ""),
            url=url,
            headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
            transport=transport,
            enabled=bool(raw.get("enabled", True)),
            auto_start=bool(raw.get("auto_start", True)),
            permission=str(raw.get("permission") or default_permission),
        )


class MCPError(Exception):
    pass


def _conn_diag(exc: BaseException, config: "MCPServerConfig", *,
               process: subprocess.Popen | None = None,
               method: str = "") -> "object":
    """Wrap an MCP transport failure in BackendConnectionError so it lands in
    the diagnostics ring with server/pid context instead of a bare socket
    error. Returns the wrapped exception; callers decide whether to raise it
    or keep the MCPError surface."""
    from .netdiag import BackendConnectionError
    peer = config.url or (config.command[0] if config.command else config.id)
    proc = None
    if process is not None:
        proc = {"pid": process.pid,
                "exit_code": process.poll(),
                "command": " ".join(config.command)[:200]}
    return BackendConnectionError(
        exc, subsystem="mcp", url=peer, model_id=config.id,
        method=method or "POST", process=proc)


class MCPClient:
    """Synchronous stdio JSON-RPC client for one MCP server process."""

    def __init__(self, config: MCPServerConfig, *, timeout: float = 30.0,
                 env_resolver: Any = None) -> None:
        self.config = config
        self.timeout = timeout
        self._env_resolver = env_resolver
        self._proc: subprocess.Popen | None = None
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._reader: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._stderr_tail: str = ""
        server_label = config.name or config.id
        self._log_prefix = f"[mcp:{server_label}]"

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self.is_alive():
            return
        if not self.config.command:
            raise MCPError(f"MCP server '{self.config.id}' has no command")
        env = dict(os.environ)
        env.update(self._resolved_env())
        cwd = self.config.cwd or None
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        try:
            self._proc = subprocess.Popen(
                self.config.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=cwd,
                creationflags=flags,
            )
        except OSError as exc:
            self._proc = None
            raise MCPError(f"failed to launch MCP server '{self.config.id}': {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        # Job Object lets close() kill the whole tree — an MCP server whose
        # command is a shell wrapper would otherwise orphan its child holding
        # our pipes, and a crashed backend would leave the server running.
        self._kill_job = _assign_kill_job(self._proc)
        # MCP handshake
        self.request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "chat-nexus", "version": "0.7.0"},
        })
        self.notify("notifications/initialized", {})

    def _resolved_env(self) -> dict[str, str]:
        """Resolve ``secret:<name>`` env values through the credential vault.

        Unresolvable references fail the launch rather than leaking the
        literal placeholder to the server.
        """
        resolved: dict[str, str] = {}
        for key, value in self.config.env.items():
            if str(value).startswith("secret:"):
                name = str(value)[7:].strip()
                secret = self._env_resolver(name) if self._env_resolver else None
                if secret is None:
                    raise MCPError(
                        f"env '{key}' references unresolved secret '{name}' — set it in the credential vault")
                resolved[key] = secret
            else:
                resolved[key] = value
        return resolved

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        job = getattr(self, "_kill_job", 0)
        self._kill_job = 0
        if proc is None:
            if job:
                _terminate_kill_job(job)
            return
        if job:
            _terminate_kill_job(job)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        elif proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass

    # -- protocol -------------------------------------------------------------

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self.is_alive():
            from .netdiag import record_failure
            proc = self._proc
            exited = proc is not None and proc.poll() is not None
            diag = _conn_diag(
                BrokenPipeError(f"MCP server '{self.config.id}' exited mid-request"
                                if exited else f"MCP server '{self.config.id}' is not running"),
                self.config, process=proc, method=method)
            record_failure(diag)
            raise MCPError(diag.friendly) from diag
        with self._send_lock:
            self._next_id += 1
            request_id = self._next_id
            q: queue.Queue = queue.Queue(maxsize=1)
            self._pending[request_id] = q
            try:
                self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            except Exception:
                self._pending.pop(request_id, None)
                raise
        try:
            message = q.get(timeout=self.timeout)
        except queue.Empty as exc:
            self._pending.pop(request_id, None)
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                # The server died while we waited — surface that, not a bare
                # timeout. Recorded so diagnostics/watchdog see the crash.
                from .netdiag import record_failure
                diag = _conn_diag(BrokenPipeError("process exited"), self.config,
                                  process=proc, method=method)
                record_failure(diag)
                raise MCPError(diag.friendly) from diag
            raise MCPError(f"MCP request '{method}' timed out after {self.timeout}s") from exc
        if "error" in message:
            err = message["error"]
            raise MCPError(f"MCP error {err.get('code')}: {err.get('message')}")
        return message.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.is_alive():
            try:
                self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})
            except Exception:
                pass

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        return list((result or {}).get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    # -- io -------------------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                request_id = message.get("id")
                pending = self._pending.pop(request_id, None)
                if pending is not None:
                    pending.put(message)
        except (OSError, ValueError):
            pass
        finally:
            for pending in self._pending.values():
                try:
                    pending.put({"error": {"code": -1, "message": "server closed stdout"}})
                except Exception:
                    pass
            self._pending.clear()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            tail = []
            for line in proc.stderr:
                tail.append(line.rstrip())
                tail = tail[-5:]
            self._stderr_tail = "\n".join(tail)
        except Exception:
            pass


class MCPHTTPClient:
    """Streamable-HTTP MCP client: POSTs JSON-RPC, tolerates plain-JSON or SSE
    (``data:``) responses, and tracks the ``Mcp-Session-Id`` header."""

    def __init__(self, config: MCPServerConfig, *, timeout: float = 30.0,
                 header_resolver: Any = None) -> None:
        self.config = config
        self.timeout = timeout
        self._header_resolver = header_resolver
        self._next_id = 0
        self._session_id = ""
        self._connected = False
        self._send_lock = threading.Lock()

    def _resolved_headers(self) -> dict[str, str]:
        """Resolve ``secret:<name>`` header values through the credential vault."""
        resolved: dict[str, str] = {}
        for key, value in self.config.headers.items():
            if str(value).startswith("secret:"):
                name = str(value)[7:].strip()
                secret = self._header_resolver(name) if self._header_resolver else None
                if secret is None:
                    raise MCPError(
                        f"header '{key}' references unresolved secret '{name}' — set it in the credential vault")
                resolved[key] = secret
            else:
                resolved[key] = value
        return resolved

    def start(self) -> None:
        if self._connected:
            return
        if not self.config.url:
            raise MCPError(f"MCP server '{self.config.id}' has no url")
        self._connected = True

    def is_alive(self) -> bool:
        return self._connected

    def close(self) -> None:
        self._connected = False
        self._session_id = ""

    def _post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        import urllib.request
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        headers.update(self._resolved_headers())
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self.config.url, data=json.dumps(message).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
                if sid:
                    self._session_id = sid
                if resp.status == 202 or resp.status == 204:
                    return None
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise MCPError(f"MCP HTTP {exc.code}: {exc.read()[:300]!r}") from exc
        except OSError as exc:
            from .netdiag import record_failure
            diag = _conn_diag(exc, self.config)
            record_failure(diag)
            raise MCPError(diag.friendly) from diag
        # Streamable HTTP may answer with SSE frames — take the last data: line.
        payload = ""
        for line in body.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
        if not payload:
            payload = body.strip()
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError as exc:
            raise MCPError(f"invalid MCP response: {payload[:200]}") from exc

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self.is_alive():
            raise MCPError(f"MCP server '{self.config.id}' is not connected")
        with self._send_lock:
            self._next_id += 1
            message = self._post({"jsonrpc": "2.0", "id": self._next_id,
                                  "method": method, "params": params or {}})
        if message is None:
            raise MCPError(f"MCP request '{method}' returned no body")
        if "error" in message:
            err = message["error"]
            raise MCPError(f"MCP error {err.get('code')}: {err.get('message')}")
        return message.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.is_alive():
            try:
                self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})
            except Exception:
                pass

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        return list((result or {}).get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})


class MCPManager:
    """Lifecycle manager for configured MCP servers; imports tools into the registry."""

    def __init__(self, registry: ToolRegistry, configs: list[MCPServerConfig] | None = None, *,
                 timeout: float = 30.0, vault: Any = None) -> None:
        self.registry = registry
        self._vault = vault
        self._configs = {c.id: c for c in (configs or []) if c.id}
        self._clients: dict[str, MCPClient] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._timeout = timeout
        self._lock = threading.RLock()

    def configure(self, configs: list[MCPServerConfig]) -> None:
        with self._lock:
            self._configs = {c.id: c for c in configs if c.id}

    # -- lifecycle ------------------------------------------------------------

    def connect(self, server_id: str) -> dict[str, Any]:
        with self._lock:
            config = self._configs.get(server_id)
            if config is None:
                raise KeyError(f"unknown MCP server '{server_id}'")
            client = self._clients.get(server_id)
            if client is not None and client.is_alive():
                return self._status_row(server_id)
            if client is not None:
                client.close()
            resolver = self._vault.get if self._vault is not None else None
            client = (MCPHTTPClient if config.transport == "http" else MCPClient)(
                config, timeout=self._timeout,
                **({"header_resolver": resolver} if config.transport == "http"
                   else {"env_resolver": resolver}))
            try:
                client.start()
            except Exception as exc:
                self._status[server_id] = {"state": "error", "error": f"{type(exc).__name__}: {exc}", "tools": 0}
                client.close()
                return self._status_row(server_id)
            self._clients[server_id] = client
            try:
                tools = client.list_tools()
            except Exception as exc:
                client.close()
                self._clients.pop(server_id, None)
                self._status[server_id] = {"state": "error", "error": f"tools/list failed: {exc}", "tools": 0}
                return self._status_row(server_id)
            imported = self._import_tools(server_id, client, tools)
            pid = getattr(client, "_proc", None)
            self._status[server_id] = {"state": "connected", "error": "", "tools": imported,
                                       "pid": pid.pid if pid else None}
            return self._status_row(server_id)

    def disconnect(self, server_id: str) -> dict[str, Any]:
        with self._lock:
            client = self._clients.pop(server_id, None)
            if client is not None:
                client.close()
            self._status[server_id] = {"state": "disconnected", "error": "", "tools": 0}
            return self._status_row(server_id)

    def restart(self, server_id: str) -> dict[str, Any]:
        self.disconnect(server_id)
        return self.connect(server_id)

    def connect_all(self) -> list[dict[str, Any]]:
        rows = []
        for server_id, config in self._configs.items():
            if config.enabled and config.auto_start:
                rows.append(self.connect(server_id))
        return rows

    def shutdown(self) -> None:
        for server_id in list(self._clients):
            self.disconnect(server_id)

    # -- status ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"servers": [self._status_row(sid) for sid in self._configs]}

    def status_row(self, server_id: str) -> dict[str, Any]:
        """Single-server status for the ProcessManager describe hook."""
        with self._lock:
            return self._status_row(server_id)

    def _status_row(self, server_id: str) -> dict[str, Any]:
        config = self._configs.get(server_id)
        client = self._clients.get(server_id)
        st = dict(self._status.get(server_id) or {"state": "disconnected", "error": "", "tools": 0})
        if client is not None and not client.is_alive() and st.get("state") == "connected":
            st.update({"state": "error", "error": "process exited"})
        row = {
            "id": server_id,
            "name": config.name if config else server_id,
            "enabled": config.enabled if config else False,
            "command": list(config.command) if config else [],
            "transport": config.transport if config else "stdio",
            "url": config.url if config else "",
            "state": st.get("state", "disconnected"),
            "error": st.get("error", ""),
            "tools": st.get("tools", 0),
            "pid": st.get("pid"),
            "alive": bool(client and client.is_alive()),
        }
        return row

    # -- tool import ----------------------------------------------------------

    def _import_tools(self, server_id: str, client: MCPClient, tools: list[dict[str, Any]]) -> int:
        config = self._configs[server_id]
        imported = 0
        for tool in tools:
            tname = str(tool.get("name") or "").strip()
            if not tname:
                continue
            reg_name = f"mcp__{server_id}__{tname}"
            description = str(tool.get("description") or f"MCP tool {tname} on {config.name or server_id}")
            schema = tool.get("inputSchema") or {"type": "object"}

            def make_handler(cid: str = server_id, t: str = tname):
                def handler(args: dict[str, Any]) -> str:
                    c = self._clients.get(cid)
                    if c is None or not c.is_alive():
                        raise MCPError(f"MCP server '{cid}' is not connected")
                    result = c.call_tool(t, args)
                    return _format_tool_result(result)
                return handler

            spec = ToolSpec(
                reg_name,
                f"[MCP {config.name or server_id}] {description}",
                schema,
                config.permission,
                make_handler(),
                tool_id=f"{server_id}:{tname}",
                category="mcp",
                version=str(tool.get("version") or "mcp"),
                provider=server_id,
                capabilities=[f"mcp_{tname}", tname],
                source="mcp",
                health_check=None,
            )
            self.registry.register(spec)
            self.registry._plugin_meta[reg_name] = {"invocable": True, "mcp_server": server_id, "mcp_tool": tname}
            imported += 1
        return imported


def _format_tool_result(result: Any) -> str:
    """Render a MCP tools/call result into agent-readable text."""
    if isinstance(result, dict):
        if result.get("isError"):
            content = result.get("content") or []
            text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            return f"ERROR: {text or 'tool call failed'}"
        content = result.get("content")
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(str(c.get("text", "")))
                elif isinstance(c, dict):
                    parts.append(json.dumps(c))
            if parts:
                return "\n".join(parts)
        return json.dumps(result, ensure_ascii=False)
    return str(result)


def load_mcp_configs(raw_list: Any) -> list[MCPServerConfig]:
    configs = []
    for raw in raw_list or []:
        if isinstance(raw, dict):
            cfg = MCPServerConfig.from_dict(raw)
            if cfg.id:
                configs.append(cfg)
    return configs
