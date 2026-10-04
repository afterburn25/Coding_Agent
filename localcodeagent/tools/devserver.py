"""Dev-server agent tools — start/stop/restart/list/probe managed
development servers inside registered workspaces."""

from __future__ import annotations

import json
from typing import Any

from .base import ToolRegistry, ToolSpec


def register_devserver_tools(registry: ToolRegistry, manager) -> None:
    def dev_server_start(args: dict[str, Any]) -> str:
        res = manager.start(
            str(args.get("path") or ""),
            str(args.get("command") or ""),
            name=str(args.get("name") or ""))
        if res.get("error"):
            return f"ERROR: {res['error']}" + (
                f" ({res['server']['id']})" if res.get("server") else "")
        if bool(args.get("wait", True)):
            wait = manager.wait_for_url(
                res["server"]["id"],
                timeout=max(1.0, min(float(args.get("timeout", 45)), 180)))
            res["wait"] = wait
        return json.dumps(res, ensure_ascii=False, default=str)

    def dev_server_list(args: dict[str, Any]) -> str:
        return json.dumps({"servers": manager.list()},
                          ensure_ascii=False, default=str)

    def _by_id(args: dict[str, Any], fn) -> str:
        sid = str(args.get("id") or "").strip()
        if not sid:
            return "ERROR: 'id' is required"
        return json.dumps(fn(sid), ensure_ascii=False, default=str)

    registry.register(ToolSpec(
        "dev_server_start",
        "Start a development server inside a registered workspace. Detects "
        "the bound URL/port from the process log and (with wait=true, "
        "default) blocks until it actually answers HTTP — a server is only "
        "reported as serving when a real probe succeeds.",
        {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "workspace root directory"},
            "command": {"type": "string",
                        "description": "server command, e.g. 'npm run dev'"},
            "name": {"type": "string"},
            "wait": {"type": "boolean"},
            "timeout": {"type": "number"}},
         "required": ["path", "command"]},
        "shell.execute", dev_server_start, category="workspace",
        capabilities=["dev_server", "terminal"]))

    registry.register(ToolSpec(
        "dev_server_list",
        "List managed dev servers with state (starting/serving/stopped), "
        "detected URL/port, and a live health probe.",
        {"type": "object", "properties": {}},
        "filesystem.read", dev_server_list, category="workspace",
        capabilities=["dev_server"]))

    registry.register(ToolSpec(
        "dev_server_stop",
        "Stop a managed dev server by id.",
        {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
        "shell.execute",
        lambda a: _by_id(a, manager.stop), category="workspace",
        capabilities=["dev_server"]))

    registry.register(ToolSpec(
        "dev_server_restart",
        "Restart a managed dev server by id (same command and root).",
        {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
        "shell.execute",
        lambda a: _by_id(a, manager.restart), category="workspace",
        capabilities=["dev_server"]))

    registry.register(ToolSpec(
        "dev_server_logs",
        "Return the bounded log tail of a managed dev server.",
        {"type": "object", "properties": {
            "id": {"type": "string"},
            "limit": {"type": "number"}}, "required": ["id"]},
        "filesystem.read",
        lambda a: _by_id(
            a, lambda sid: manager.logs(
                sid, int(a.get("limit", 80)))), category="workspace",
        capabilities=["dev_server"]))
