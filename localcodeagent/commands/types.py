"""Slash-command data types.

A recognised command never reaches a model — it parses to a
:class:`ParsedCommand`, resolves to a :class:`CommandSpec`, and executes
through either a direct handler or an ``ActionRegistry`` action id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(slots=True)
class ParsedCommand:
    """Raw parse output — valid command *syntax*; the registry decides
    whether the name is real."""
    name: str            # lower-cased command token without the slash
    raw_args: str = ""   # everything after the first whitespace run
    raw: str = ""        # the stripped input as typed


@dataclass
class CommandSpec:
    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    usage: str = ""
    category: str = "general"
    arguments: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    # ActionRegistry-compatible risk class; handlers with mutation risk
    # may still require explicit confirmation via ``confirmed``.
    risk: str = "read_only"
    permission: str = ""
    # When set, execution delegates to ActionRegistry.execute(action_id)
    # instead of a local handler — the same path the GUI uses.
    action_id: str = ""
    # map_params(raw_args) -> dict — typed argument mapping merged into
    # the params handed to the action (e.g. "4" -> {"value": 4}).
    map_params: Any = None
    # handler(parsed, ctx) -> CommandResult | str | dict
    handler: Callable[..., Any] | None = None
    # available_when(env) -> bool — gates commands on real capability.
    available_when: Callable[[dict], bool] | None = None
    desktop_only: bool = False
    hidden: bool = False
    supports_undo: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "description": self.description,
            "usage": self.usage or f"/{self.name}",
            "category": self.category,
            "arguments": list(self.arguments),
            "examples": list(self.examples),
            "risk": self.risk,
            "desktop_only": self.desktop_only,
            "hidden": self.hidden,
            "supports_undo": self.supports_undo,
        }


@dataclass
class CommandResult:
    ok: bool
    text: str
    detail: str = ""
    links: list[dict[str, str]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "text": self.text, "detail": self.detail,
                "links": self.links, "data": self.data}
