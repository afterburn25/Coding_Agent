"""Core slash commands — deterministic paths into existing services.

Every handler receives ``(parsed, ctx)`` where ``ctx["env"]`` carries
callables wired by the orchestrator (status, research, think mode, …).
Handlers return text/CommandResult; no model is ever invoked.
"""
from __future__ import annotations

from typing import Any

from .registry import CommandRegistry
from .types import CommandResult, CommandSpec, ParsedCommand

THINK_MODES = ("fast", "normal", "deep", "exhaustive", "auto")


def _env_call(env: dict, name: str, *args: Any, **kwargs: Any) -> Any:
    fn = env.get(name)
    if callable(fn):
        return fn(*args, **kwargs)
    return None


def _help(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    reg: CommandRegistry = ctx["env"]["registry"]
    topic = parsed.raw_args.strip().lstrip("/").lower()
    if topic:
        text = reg.describe(topic)
        if not text:
            hints = reg.suggest(topic)
            text = f"No command named /{topic}."
            if hints:
                text += " Did you mean: " + ", ".join(f"/{h}" for h in hints)
            return CommandResult(False, text)
        return CommandResult(True, text)
    return CommandResult(True, reg.help_text())


def _status(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    env = ctx["env"]
    info = _env_call(env, "status") or {}
    if isinstance(info, str):
        return CommandResult(True, info)
    if not isinstance(info, dict) or not info:
        return CommandResult(False, "Status is unavailable right now.")
    lines = [f"Nexus Core {info.get('version', '?')}"]
    if info.get("model"):
        lines.append(f"Model: {info['model']}")
    if info.get("think_mode"):
        lines.append(f"Think mode: {info['think_mode']}")
    if info.get("backend"):
        lines.append(f"Backend: {info['backend']}")
    if info.get("research_mode"):
        lines.append(f"Research: {info['research_mode']}")
    if info.get("health"):
        lines.append(f"Health: {info['health']}")
    for k in ("queue", "workers", "voice", "images"):
        if info.get(k) not in (None, ""):
            lines.append(f"{k.title()}: {info[k]}")
    return CommandResult(True, "\n".join(lines), data=info)


def _version(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    ver = _env_call(ctx["env"], "version")
    return CommandResult(True, f"Nexus Core {ver}" if ver else
                         "Version unavailable.")


_THINK_HELP = (
    "Think modes: fast — minimal extra work; normal — balanced; "
    "deep — hypotheses + critic allowed; exhaustive — bounded specialist "
    "investigation; auto — Nexus decides.")


def _think(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    env = ctx["env"]
    conv = str(ctx.get("conversation_id") or "")
    arg = parsed.raw_args.strip().lower()
    if not arg:
        cur = _env_call(env, "think_get", conv) or "auto"
        return CommandResult(True, f"Think mode: {cur}\n\n{_THINK_HELP}")
    if arg not in THINK_MODES:
        return CommandResult(
            False, f"Unknown think mode '{arg}'.\n\n{_THINK_HELP}")
    _env_call(env, "think_set", conv, arg)
    return CommandResult(True, f"Think mode set to {arg}.")


def _research(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    env = ctx["env"]
    query = parsed.raw_args.strip()
    if not query:
        return CommandResult(False, "Usage: /research <question or topic>")
    session = _env_call(env, "run_research", query, ctx)
    if not isinstance(session, dict):
        return CommandResult(False, "Research is unavailable right now.")
    if session.get("error"):
        return CommandResult(False, f"Research failed: {session['error']}")
    ev = session.get("evidence") or {}
    conf = ev.get("confidence") or "?"
    corr = ev.get("corroboration") or ""
    sources = session.get("sources") or []
    lines = [session.get("summary") or "No summary produced."]
    lines.append(
        f"\nEvidence: {conf}" + (f" ({corr.replace('_', ' ')})" if corr else ""))
    if sources:
        lines.append(f"Sources ({len(sources)}):")
        for s in sources[:6]:
            badges = " ".join(f"[{b}]" for b in (s.get("badges") or []))
            lines.append(
                f"  • {s.get('title') or s.get('url')} — {s.get('domain') or ''}"
                + (f" {badges}" if badges else ""))
    return CommandResult(True, "\n".join(lines),
                         data={"research": session})


def _sources(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    env = ctx["env"]
    conv = str(ctx.get("conversation_id") or "")
    session = _env_call(env, "last_research", conv)
    fmt = env.get("format_sources")
    if callable(fmt) and isinstance(session, dict):
        text = fmt(session.get("session") or session)
    elif isinstance(session, dict):
        text = "Recent research had no sources to list."
    else:
        text = "I don't have a recent research session with sources to show you."
    return CommandResult(True, text)


def _model(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    env = ctx["env"]
    info = _env_call(env, "model_info") or {}
    if isinstance(info, str):
        return CommandResult(True, info)
    if not isinstance(info, dict) or not info:
        return CommandResult(False, "Model information unavailable.")
    lines = []
    if info.get("active"):
        lines.append(f"Active: {info['active']}")
    for role, mid in (info.get("roles") or {}).items():
        lines.append(f"  {role}: {mid}")
    if info.get("note"):
        lines.append(str(info["note"]))
    if not lines:
        lines.append("No model details available.")
    return CommandResult(True, "\n".join(lines), data=info)


def _stop(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    env = ctx["env"]
    out = _env_call(env, "stop_active", str(ctx.get("task_id") or ""))
    if out is True or (isinstance(out, str) and out and not out.startswith("no ")):
        return CommandResult(True, out if isinstance(out, str) else
                             "Stopped the active work.")
    return CommandResult(True, "Nothing is running right now.")


def _shutdown(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    # Executor already confirmed ("/shutdown yes"); call the host hook.
    fn = ctx["env"].get("shutdown")
    if not callable(fn):
        return CommandResult(False, "Shutdown isn't wired in this host.")
    fn(restart=parsed.name == "restart")
    return CommandResult(
        True, "Restarting…" if parsed.name == "restart" else "Shutting down…")


def register_core_commands(registry: CommandRegistry) -> CommandRegistry:
    reg = registry.register
    reg(CommandSpec(
        "help", aliases=("commands", "?"),
        description="List commands or explain one command.",
        usage="/help [command]", category="general",
        examples=("/help", "/help research"), handler=_help))
    reg(CommandSpec(
        "status", description="Show live system status — model, health, "
        "think mode, queue.", usage="/status", category="general",
        handler=_status))
    reg(CommandSpec(
        "version", aliases=("ver",), description="Show Nexus Core version.",
        usage="/version", category="general", handler=_version))
    reg(CommandSpec(
        "think", description="Show or set how hard Nexus reasons "
        "(fast|normal|deep|exhaustive|auto).",
        usage="/think [mode]", category="intelligence",
        arguments=("mode",), examples=("/think deep", "/think auto"),
        handler=_think))
    reg(CommandSpec(
        "research", aliases=("search", "web"),
        description="Run web research on a question and show evidence.",
        usage="/research <question>", category="intelligence",
        arguments=("question",),
        examples=("/research latest stable Python version",),
        handler=_research))
    reg(CommandSpec(
        "sources", description="List sources from the last research "
        "session.", usage="/sources", category="intelligence",
        handler=_sources))
    reg(CommandSpec(
        "model", description="Show the active model and role assignments.",
        usage="/model", category="intelligence", handler=_model))
    reg(CommandSpec(
        "stop", aliases=("cancel",), risk="low_risk",
        description="Cancel the currently running work.",
        usage="/stop", category="control", handler=_stop))
    reg(CommandSpec(
        "shutdown", risk="confirm", desktop_only=True,
        description="Shut Nexus down (asks for confirmation).",
        usage="/shutdown yes", category="control", handler=_shutdown))
    reg(CommandSpec(
        "exit", aliases=("quit",), risk="confirm", desktop_only=True,
        description="Exit Nexus (asks for confirmation).",
        usage="/exit yes", category="control", handler=_shutdown))
    reg(CommandSpec(
        "restart", risk="confirm", desktop_only=True,
        description="Restart Nexus (asks for confirmation).",
        usage="/restart yes", category="control", handler=_shutdown))
    return registry
