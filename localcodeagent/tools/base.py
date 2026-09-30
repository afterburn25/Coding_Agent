from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    permission: str
    handler: Callable[[dict[str, Any]], str]

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, permissions: dict[str, str]) -> None:
        self.permissions = permissions
        self._tools: dict[str, ToolSpec] = {}
        # Mutable execution context is set by the orchestrator before tool calls.
        self.context: dict[str, Any] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def schemas(self) -> list[dict[str, Any]]:
        return [t.openai_schema() for t in self._tools.values()]

    def permission_for(self, name: str) -> tuple[str, str]:
        tool = self._tools.get(name)
        if tool is None:
            return "", "deny"
        return tool.permission, self.permissions.get(tool.permission, "ask")

    def requires_approval(self, name: str) -> tuple[bool, str]:
        permission, mode = self.permission_for(name)
        return mode == "ask", permission

    def execute(self, name: str, arguments: dict[str, Any], *, approved: bool = False) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"ERROR: unknown tool '{name}'"
        mode = self.permissions.get(tool.permission, "ask")
        if mode == "deny":
            return f"PERMISSION_DENIED: {tool.permission} is disabled"
        if mode == "ask" and not approved:
            return f"APPROVAL_REQUIRED: permission '{tool.permission}' must be approved by the user before running {name}"
        try:
            return tool.handler(arguments)
        except Exception as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"
