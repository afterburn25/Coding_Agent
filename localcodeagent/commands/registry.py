"""CommandRegistry — the single source of truth for slash commands."""
from __future__ import annotations

import difflib
from typing import Any

from .types import CommandSpec


class CommandRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, CommandSpec] = {}
        self._alias: dict[str, str] = {}

    def register(self, spec: CommandSpec) -> CommandSpec:
        name = spec.name.lower()
        self._specs[name] = spec
        self._alias[name] = name
        for a in spec.aliases:
            self._alias[a.lower()] = name
        return spec

    def get(self, name: str) -> CommandSpec | None:
        return self._specs.get(self._alias.get(str(name or "").lower(), ""))

    def all(self, *, include_hidden: bool = False) -> list[CommandSpec]:
        rows = list(self._specs.values())
        if not include_hidden:
            rows = [s for s in rows if not s.hidden]
        return sorted(rows, key=lambda s: (s.category, s.name))

    def names(self, *, include_hidden: bool = False) -> list[str]:
        return [s.name for s in self.all(include_hidden=include_hidden)]

    def suggest(self, name: str, *, limit: int = 3) -> list[str]:
        """Closest registered names/aliases for an unknown command."""
        cand = difflib.get_close_matches(
            str(name or "").lower(), list(self._alias), n=limit, cutoff=0.6)
        out: list[str] = []
        for c in cand:
            canon = self._alias[c]
            if canon not in out:
                out.append(canon)
        return out

    def complete(self, prefix: str, *, limit: int = 10) -> list[str]:
        """Autocomplete candidates for a partial ``/prefix`` token."""
        p = str(prefix or "").lstrip("/").lower()
        seen: list[str] = []
        for key, canon in sorted(self._alias.items()):
            if key.startswith(p) and canon not in seen:
                spec = self._specs[canon]
                if not spec.hidden:
                    seen.append(canon)
            if len(seen) >= limit:
                break
        return seen

    def help_text(self, *, include_hidden: bool = False) -> str:
        """``/help`` output generated from the registry itself."""
        lines = ["Available commands:"]
        cat = None
        for spec in self.all(include_hidden=include_hidden):
            if spec.category != cat:
                cat = spec.category
                lines.append(f"\n{cat.replace('_', ' ').title()}:")
            usage = spec.usage or f"/{spec.name}"
            lines.append(f"  {usage:<34} {spec.description}")
        lines.append("\n/help <command> shows usage and examples.")
        return "\n".join(lines)

    def describe(self, name: str) -> str:
        spec = self.get(name)
        if spec is None:
            return ""
        parts = [f"/{spec.name} — {spec.description}",
                 f"Usage: {spec.usage or f'/{spec.name}'}"]
        if spec.aliases:
            parts.append("Aliases: " + ", ".join(f"/{a}" for a in spec.aliases))
        if spec.arguments:
            parts.append("Arguments: " + ", ".join(spec.arguments))
        if spec.examples:
            parts.append("Examples:\n  " + "\n  ".join(spec.examples))
        return "\n".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {s.name: s.as_dict() for s in self.all(include_hidden=True)}
