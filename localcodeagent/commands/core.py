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


def _nl_control(env: dict, text: str, ctx: dict) -> Any:
    """Run a phrase through the deterministic self-knowledge control
    plane — still no model, just its typed resolver."""
    fn = env.get("nl_control")
    if not callable(fn):
        return None
    res = fn(text, ctx)
    if res is None:
        return None
    return getattr(res, "text", None) or (res if isinstance(res, str) else None)


def _subcommand_actions(table: dict, *, default=None):
    """Build a handler dispatching the first arg token to an action id.

    table values are action_id str or (action_id, param_fn(rest)->dict).
    """

    def handler(parsed: ParsedCommand, ctx: dict) -> CommandResult:
        env = ctx["env"]
        parts = parsed.raw_args.split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        if (not sub or sub == "status") and default is not None:
            return default(parsed, ctx)
        entry = table.get(sub)
        if entry is None:
            return CommandResult(
                False,
                f"Unknown '/{parsed.name} {sub}'. Try: " + ", ".join(table))
        action_id, param_fn = entry if isinstance(entry, tuple) else (entry, None)
        actions = env.get("actions")
        if actions is None:
            return CommandResult(False, "Action service unavailable.")
        params: dict[str, Any] = {"args": parsed.raw_args,
                                  "raw": parsed.raw_args}
        if callable(param_fn):
            params.update(param_fn(rest) or {})
        res = actions.execute(action_id, params,
                              confirmed=bool(ctx.get("confirmed")))
        text = res.message or res.detail or ("Done." if res.ok else "Failed.")
        if res.message == "needs_confirmation":
            text = (res.detail or
                    "This needs an explicit yes — append 'yes' to confirm.")
        return CommandResult(res.ok, text, detail=res.detail,
                             links=list(res.links or []))
    return handler


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


def _doctor(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    text = _nl_control(ctx["env"], "self diagnose", ctx)
    return CommandResult(bool(text), text or "Diagnostics unavailable.")


def _undo(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    text = _nl_control(ctx["env"], "undo that", ctx)
    return CommandResult(bool(text),
                         text or "The last change can't be undone from chat.")


def _workspace(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    ws = ctx["env"].get("workspace")
    ws = ws() if callable(ws) else ws
    return CommandResult(bool(ws), f"Workspace: {ws}" if ws
                         else "Workspace unavailable.")


def _why(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    out = _env_call(ctx["env"], "why", ctx)
    return CommandResult(bool(out), str(out) if out
                         else "I don't have a recent decision to explain.")


def _confidence(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    conv = str(ctx.get("conversation_id") or "")
    row = _env_call(ctx["env"], "last_research", conv)
    session = (row or {}).get("session") or {}
    ev = session.get("evidence") or {}
    if not ev:
        return CommandResult(
            True, "No recent evidence-based session — nothing to score.")
    lines = [f"Evidence confidence: {ev.get('confidence', '?')}"]
    if ev.get("corroboration"):
        lines.append(f"Corroboration: {str(ev['corroboration']).replace('_', ' ')}")
    for r in (ev.get("reasons") or [])[:5]:
        lines.append(f"  • {r}")
    return CommandResult(True, "\n".join(lines), data={"evidence": ev})


def _evidence(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    conv = str(ctx.get("conversation_id") or "")
    row = _env_call(ctx["env"], "last_research", conv)
    session = (row or {}).get("session") or {}
    ev = session.get("evidence") or {}
    if not ev:
        return CommandResult(
            True, "No recent research session — run /research first.")
    lines = [
        f"Confidence: {ev.get('confidence', '?')} | "
        f"Corroboration: {str(ev.get('corroboration', '?')).replace('_', ' ')}"]
    groups = ev.get("independent_groups")
    if groups is not None:
        lines.append(f"Independent source groups: {groups}")
    for c in (ev.get("conflicts") or [])[:4]:
        lines.append(f"  conflict: {c}")
    claims = (ev.get("claims") or [])[:5]
    for cl in claims:
        lines.append(
            f"  • {str(cl.get('claim') or '')[:110]}"
            f" [{cl.get('support', '?')}]")
    return CommandResult(True, "\n".join(lines), data={"evidence": ev})


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
    reg(CommandSpec(
        "doctor", aliases=("diagnose", "health"),
        description="Run real subsystem diagnostics.",
        usage="/doctor", category="general", handler=_doctor))
    reg(CommandSpec(
        "undo", description="Undo the last reversible change.",
        usage="/undo", category="control", handler=_undo,
        supports_undo=True))
    reg(CommandSpec(
        "workspace", description="Show the active workspace path.",
        usage="/workspace", category="general", handler=_workspace))
    reg(CommandSpec(
        "why", description="Explain the last decision Nexus made.",
        usage="/why", category="intelligence", handler=_why))
    reg(CommandSpec(
        "confidence", description="Show evidence confidence for the last "
        "researched answer.", usage="/confidence", category="intelligence",
        handler=_confidence))
    reg(CommandSpec(
        "evidence", description="Show the evidence board for the last "
        "research session.", usage="/evidence", category="intelligence",
        handler=_evidence))
    reg(CommandSpec(
        "github", description="GitHub connection: /github test|connect|"
        "disconnect.", usage="/github <subcommand>",
        category="integrations",
        examples=("/github test", "/github status"),
        handler=_subcommand_actions(
            {"test": "github.test",
             "connect": "github.connect",
             "disconnect": "github.disconnect"},
            default=lambda p, c: CommandResult(
                True, _nl_control(c["env"], "github status", c)
                or "Usage: /github test|connect|disconnect"))))
    reg(CommandSpec(
        "voice", description="Voice control: /voice on|off|mute|unmute|"
        "stop|preset <name>|volume <0-100>.",
        usage="/voice <subcommand>", category="control",
        handler=_subcommand_actions(
            {"on": "voice.enable", "enable": "voice.enable",
             "off": "voice.disable", "disable": "voice.disable",
             "mute": "voice.mute", "unmute": "voice.unmute",
             "stop": "voice.stop",
             "preset": ("voice.set_preset", lambda r: {"value": r}),
             "volume": ("voice.set_volume",
                        lambda r: {"value": r})},
            default=lambda p, c: CommandResult(
                True, _nl_control(c["env"], "is voice on", c)
                or "Usage: /voice on|off|mute|unmute|stop|preset|volume"))))
    reg(CommandSpec(
        "workers", description="Show or set the background worker limit.",
        usage="/workers [n]", category="control",
        handler=_workers))
    reg(CommandSpec(
        "image", description="Image backend: /image start|stop|install|"
        "backend <name>.", usage="/image <subcommand>",
        category="integrations",
        handler=_subcommand_actions(
            {"start": "image.backend.start",
             "stop": "image.backend.stop",
             "install": "image.backend.install",
             "backend": ("image.backend.set", lambda r: {"value": r})},
            default=lambda p, c: CommandResult(
                True, _nl_control(c["env"], "image backend status", c)
                or "Usage: /image start|stop|install|backend"))))
    return registry


def _workers(parsed: ParsedCommand, ctx: dict) -> CommandResult:
    arg = parsed.raw_args.strip()
    if not arg:
        text = _nl_control(ctx["env"], "how many workers", ctx)
        return CommandResult(True, text or "Usage: /workers <count>")
    if not arg.isdigit() or not (1 <= int(arg) <= 16):
        return CommandResult(False, "Usage: /workers <1-16>")
    actions = ctx["env"].get("actions")
    if actions is None:
        return CommandResult(False, "Action service unavailable.")
    res = actions.execute("workers.set_ceiling",
                          {"value": int(arg), "args": arg},
                          confirmed=bool(ctx.get("confirmed")))
    return CommandResult(res.ok, res.message or res.detail or "Done.",
                         detail=res.detail)
