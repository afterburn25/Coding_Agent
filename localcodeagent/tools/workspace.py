"""Workspace tools — the agent-callable surface of the WorkspaceManager.

`workspace_open` is how "Work in D:\\Projects\\MyApp" becomes a real
execution path: inspect + register + mark active in one bounded call.
File tools then accept paths inside that root via the extra_roots hook.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import ToolRegistry, ToolSpec


def register_workspace_tools(registry: ToolRegistry, manager) -> None:

    def inspect(args: dict) -> str:
        path = str(args.get("path") or "").strip()
        if not path:
            return "ERROR: 'path' is required"
        try:
            report = manager.inspect(path)
        except Exception as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"
        return json.dumps(report, ensure_ascii=False, default=str)

    def open_workspace(args: dict) -> str:
        path = str(args.get("path") or "").strip()
        if not path:
            return "ERROR: 'path' is required"
        try:
            rec = manager.open(path, create=bool(args.get("create", False)))
        except FileNotFoundError:
            return json.dumps({
                "error": "path_not_found",
                "detail": f"{path} does not exist — pass create=true to "
                          "create and adopt it",
            })
        except ValueError as exc:
            return f"ERROR: {exc}"
        return json.dumps({"workspace": rec}, ensure_ascii=False,
                          default=str)

    def list_workspaces(args: dict) -> str:
        return json.dumps({
            "active": manager.active(),
            "workspaces": manager.list(),
        }, ensure_ascii=False, default=str)

    def active(args: dict) -> str:
        return json.dumps({"active": manager.active()},
                          ensure_ascii=False, default=str)

    registry.register(ToolSpec(
        "workspace_inspect",
        "Inspect a directory: detect project type, toolchain, build system, "
        "test/build/dev commands, git branch/remote/ahead-behind and "
        "writable state. Read-only — does not adopt the directory.",
        {"type": "object",
         "properties": {"path": {"type": "string"}},
         "required": ["path"]},
        "filesystem.read", inspect, category="workspace",
        capabilities=["workspace_inspect", "project_detection"]))

    registry.register(ToolSpec(
        "workspace_open",
        "Adopt a directory as a registered workspace: inspects it, persists "
        "its detected toolchain/commands/git state, marks it active, and "
        "allows file/shell tools to operate inside it. Pass create=true to "
        "create a missing directory first.",
        {"type": "object",
         "properties": {
             "path": {"type": "string"},
             "create": {"type": "boolean", "default": False},
         },
         "required": ["path"]},
        "filesystem.write", open_workspace, category="workspace",
        capabilities=["workspace_open", "adopt_workspace"]))

    registry.register(ToolSpec(
        "workspace_list",
        "List registered workspaces and the active one (recent first).",
        {"type": "object", "properties": {}},
        "filesystem.read", list_workspaces, category="workspace",
        capabilities=["workspace_list", "recent_workspaces"]))

    registry.register(ToolSpec(
        "workspace_active",
        "Show the currently active workspace record.",
        {"type": "object", "properties": {}},
        "filesystem.read", active, category="workspace",
        capabilities=["workspace_status"]))
