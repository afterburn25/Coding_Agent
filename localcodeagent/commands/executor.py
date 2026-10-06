"""CommandExecutor — parse-resolved commands run here, never in a model.

Flow: registry lookup → availability/permission gates → ActionRegistry
(for ``action_id`` specs) or direct handler → structured CommandResult.
Every execution is reported to the audit callback.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .registry import CommandRegistry
from .types import CommandResult, CommandSpec, ParsedCommand

_NEEDS_CONFIRM = ("confirm", "sensitive")


class CommandExecutor:
    def __init__(
        self,
        registry: CommandRegistry,
        *,
        env: dict[str, Any] | None = None,
        audit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.registry = registry
        self.env = dict(env or {})
        self._audit = audit

    def _permitted(self, spec: CommandSpec) -> str:
        if not spec.permission:
            return "allow"
        fn = self.env.get("permitted")
        if not callable(fn):
            return "allow"
        try:
            return str(fn(spec.permission) or "allow")
        except Exception:
            return "deny"

    def execute(
        self,
        parsed: ParsedCommand,
        *,
        ctx: dict[str, Any] | None = None,
    ) -> CommandResult:
        ctx = dict(ctx or {})
        spec = self.registry.get(parsed.name)
        started = time.time()
        if spec is None:
            hints = self.registry.suggest(parsed.name)
            text = f"Unknown command: /{parsed.name}"
            if hints:
                text += "\n\nDid you mean:\n" + "\n".join(
                    f"/{h}" for h in hints)
            result = CommandResult(False, text)
            self._log(parsed, spec, result, started)
            return result
        try:
            result = self._dispatch(spec, parsed, ctx)
        except Exception as exc:
            result = CommandResult(
                False, f"/{spec.name} failed: {type(exc).__name__}: {exc}")
        self._log(parsed, spec, result, started)
        return result

    def _dispatch(
        self,
        spec: CommandSpec,
        parsed: ParsedCommand,
        ctx: dict[str, Any],
    ) -> CommandResult:
        if spec.desktop_only and not self.env.get("desktop"):
            return CommandResult(
                False, f"/{spec.name} is only available in the desktop app.")
        if spec.available_when is not None:
            try:
                if not spec.available_when(self.env):
                    return CommandResult(
                        False, f"/{spec.name} isn't available right now.")
            except Exception:
                return CommandResult(
                    False, f"/{spec.name} isn't available right now.")
        confirmed = bool(ctx.get("confirmed")) or (
            parsed.raw_args.strip().lower() in {"yes", "confirm", "now"})
        if spec.action_id:
            actions = self.env.get("actions")
            if actions is None:
                return CommandResult(
                    False, f"/{spec.name} has no action backend wired.")
            params: dict[str, Any] = {
                "args": parsed.raw_args, "raw": parsed.raw_args}
            if callable(spec.map_params):
                try:
                    mapped = spec.map_params(parsed.raw_args)
                    if isinstance(mapped, dict):
                        params.update(mapped)
                except Exception:
                    pass
            res = actions.execute(
                spec.action_id, params, confirmed=confirmed)
            text = res.message or res.detail or (
                "Done." if res.ok else "Failed.")
            if res.message == "needs_confirmation":
                text = (res.detail
                        or "This needs an explicit yes — run it again with "
                           "'yes' appended to confirm.")
            return CommandResult(res.ok, text, detail=res.detail,
                                 links=list(res.links or []),
                                 data={"action_id": spec.action_id,
                                       "verified": res.verified})
        verdict = self._permitted(spec)
        if verdict not in ("allow", "session"):
            return CommandResult(
                False, f"/{spec.name} is blocked by permissions "
                       f"({spec.permission}: {verdict}).")
        if spec.risk in _NEEDS_CONFIRM and not confirmed:
            return CommandResult(
                False,
                f"/{spec.name} is {spec.risk} — append 'yes' to confirm.",
                data={"needs_confirmation": True})
        ctx["confirmed"] = confirmed
        if not callable(spec.handler):
            return CommandResult(
                False, f"/{spec.name} has no handler wired.")
        out = spec.handler(parsed, {**ctx, "env": self.env})
        return self._coerce(out)

    @staticmethod
    def _coerce(out: Any) -> CommandResult:
        if isinstance(out, CommandResult):
            return out
        if isinstance(out, dict):
            return CommandResult(
                bool(out.get("ok", True)),
                str(out.get("text") or out.get("message") or "Done."),
                detail=str(out.get("detail") or ""),
                links=list(out.get("links") or []),
                data=dict(out.get("data") or {}))
        return CommandResult(True, str(out))

    def _log(
        self,
        parsed: ParsedCommand,
        spec: CommandSpec | None,
        result: CommandResult,
        started: float,
    ) -> None:
        if not callable(self._audit):
            return
        try:
            self._audit({
                "ts": started,
                "elapsed_ms": round((time.time() - started) * 1000, 1),
                "raw": parsed.raw,
                "name": parsed.name,
                "matched": spec.name if spec else "",
                "ok": result.ok,
                "risk": spec.risk if spec else "",
            })
        except Exception:
            pass
