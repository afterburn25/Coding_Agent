# MCP Integration

Nexus Core can connect to Model Context Protocol servers and import their tools
into the central Tool Registry, so MCP tools coexist with native tools under
the same permission, enable/disable, and manifest model. Nexus Core does **not**
depend exclusively on MCP — native tools remain first-class.

## Transport

Two transports are supported:

- **stdio** (default): newline-delimited JSON-RPC 2.0 over a spawned child
  process' stdin/stdout, per the MCP stdio transport spec.
- **http** (Streamable HTTP): JSON-RPC POSTs to a configured `url` —
  plain-JSON or `text/event-stream` (`data:`) responses are handled and the
  `Mcp-Session-Id` header is tracked automatically.

Both support `initialize`/`notifications/initialized`,
`tools/list` schema import, and `tools/call` execution.

## Configuration

Add servers to `config.json` (`mcp_servers`):

```json
"mcp_servers": [
  {
    "id": "fs",
    "name": "Filesystem tools",
    "command": ["python", "servers/fs_server.py"],
    "env": {"API_KEY": "secret:mcp_fs_key"},
    "cwd": "",
    "enabled": true,
    "auto_start": true,
    "permission": "shell.execute"
  }
]
```

| field | meaning |
| --- | --- |
| `id` | stable server id used in tool names and API calls |
| `command` | argv launched as a subprocess (stdio transport) |
| `url` | HTTP endpoint for the `http` transport (Streamable HTTP) |
| `headers` | extra HTTP request headers for the `http` transport (e.g. `Authorization`); `secret:<name>` values resolve through the credential vault |
| `transport` | `stdio` (default) or `http` — auto-set when `url` is present |
| `env` | extra environment variables; values of the form `secret:<name>` are resolved through the credential vault at launch (unresolved references fail the launch instead of leaking the placeholder) |
| `enabled` / `auto_start` | whether the server may run / is connected at backend startup |
| `permission` | permission key applied to every imported tool (default `shell.execute` for stdio, `external_api.call` for http) |

## Imported tools

Each `tools/list` entry registers as `mcp__<server_id>__<tool_name>` with:

- `source: "mcp"`, `category: "mcp"`, `provider: <server id>`
- capabilities `[<tool_name>, mcp_<tool_name>]`
- the server's declared `inputSchema` exposed as the tool schema
- `tools/call` results rendered to text for the agent

Imported tools respect everything native tools respect: permission levels and
profiles (offline mode blocks network-permissioned MCP tools), per-tool
enable/disable in the Tool Manager, usage counting, and routing.

## Management

```text
GET  /api/mcp                    server states (connected/disconnected/error, pid, tool count)
POST /api/mcp/action             {"id": "fs", "action": "connect|disconnect|restart"}
```

The **Tools & Plugins** page (`/tools.html`) shows an "MCP servers" panel with
connect/disconnect/restart controls. Connection failures are isolated: a
broken MCP server cannot prevent backend startup and does not affect native
tools.

## Security notes

- MCP responses are untrusted external content; they become tool output and
  never override system policies or user intent.
- Every imported tool goes through the permission system. Use a dedicated
  permission key (or `ask` level) for risky servers.
- Keep credentials out of `mcp_servers.env` in shared configs; use
  `secret:<name>` references resolved through the encrypted credential vault
  (`localcodeagent/secrets.py`) so the raw value never appears in config.
- Downloaded/community MCP servers are third-party executables — only run
  servers from sources you trust.

## Validation status

The client has been exercised against official MCP reference packages:

- `@modelcontextprotocol/server-filesystem@2026.8.31` over **stdio** —
  initialize, `tools/list` (14 tools), and `read_file` succeeded.
- `@modelcontextprotocol/server-everything@2026.8.31` over **Streamable
  HTTP** — initialize, `tools/list` (13 tools), and `echo` succeeded.
