"""Typed Action Registry — what Nexus may *do* from chat.

Every chat-controllable action is registered once with an explicit
risk class, an execute body, and a verify body. Chat never reaches into
endpoints directly — a request resolves to an ActionSpec, the spec
declares what confirmation it needs, and ``execute`` calls the real
service/probe — the same path the GUI uses.

Risk levels:
    read_only   — answers only, never mutates
    low_risk    — mutates, reversibly, safe to apply on request
    confirm     — needs an explicit "yes" before applying
    sensitive   — credentials/accounts: chat may propose but the
                  secure flow happens in the connector UI
    destructive — never runs from chat; responds with the route

Execution returns an ActionResult carrying the outcome plus, for
reversible actions, an ``undo`` payload so "undo that" works.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

Risk = str
RISK_LEVELS = ("read_only", "low_risk", "confirm", "sensitive",
               "destructive")


@dataclass(slots=True)
class ActionResult:
    ok: bool
    message: str = ""
    detail: str = ""
    verified: bool = False
    # What the value was before — kept so "undo that" can restore it.
    previous: Any = None
    # Suggested follow-up links/cards for the reply payload.
    links: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message,
                "detail": self.detail, "verified": self.verified,
                "links": self.links}


@dataclass(slots=True)
class ActionSpec:
    id: str
    name: str
    description: str = ""
    feature_id: str = ""
    risk: Risk = "low_risk"
    permission: str = ""        # permission key checked before execute
    reversible: bool = False
    requires_restart: bool = False
    confirmation_prompt: str = ""
    ui_route: str = ""
    aliases: tuple[str, ...] = ()
    # env-backed callables injected at construction of the registry:
    #   run(params, env) -> ActionResult | dict | str
    #   verify(env)      -> bool | str
    run: Callable[..., Any] | None = None
    verify: Callable[..., Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name,
                "description": self.description,
                "feature_id": self.feature_id, "risk": self.risk,
                "permission": self.permission,
                "reversible": self.reversible,
                "requires_restart": self.requires_restart,
                "confirmation_prompt": self.confirmation_prompt,
                "ui_route": self.ui_route,
                "aliases": list(self.aliases)}


def _set(env: dict, key: str, value: Any) -> ActionResult:
    """Shared body for settings-backed actions — uses the injected
    SettingsRegistry setter so writes go through the real path."""
    reg: Any = env.get("settings")
    if reg is None:
        return ActionResult(False, "settings service unavailable")
    res = reg.set(key, value)
    return ActionResult(bool(res.get("ok")), message=res.get("error", ""),
                        previous=res.get("previous"),
                        verified=bool(res.get("verified")))


def _probe(env: dict, name: str) -> dict:
    fn = env.get("probe")
    try:
        out = fn(name) if callable(fn) else {}
        return out if isinstance(out, dict) else {}
    except Exception as exc:
        return {"state": "error", "detail": str(exc)}


def _env_call(env: dict, name: str, *args: Any, **kwargs: Any) -> Any:
    fn = env.get(name)
    if callable(fn):
        return fn(*args, **kwargs)
    return None


# ---------------------------------------------------------------------------
# The registry. Actions reference real env callables; server.py wires the
# env to AppState services at startup.
# ---------------------------------------------------------------------------

ACTIONS: list[ActionSpec] = [
    # -- Voice --------------------------------------------------------------
    ActionSpec("voice.enable", "Turn voice on",
               "Enable spoken replies.",
               feature_id="voice", reversible=True,
               aliases=("turn voice on", "enable voice",
                        "unmute yourself"),
               run=lambda p, e: _set(e, "voice_enabled",
                                     bool((p or {}).get("value", True)))),
    ActionSpec("voice.disable", "Turn voice off",
               "Disable spoken replies.",
               feature_id="voice", reversible=True,
               aliases=("turn voice off", "disable voice",
                        "stop talking"),
               run=lambda p, e: _set(e, "voice_enabled",
                                     bool((p or {}).get("value", False)))),
    ActionSpec("voice.mute", "Mute voice",
               "Mute output without disabling the subsystem.",
               feature_id="voice", reversible=True,
               aliases=("mute", "mute voice", "mute yourself",
                        "be quiet", "quiet"),
               run=lambda p, e: _set(e, "voice_muted",
                                     bool((p or {}).get("value", True)))),
    ActionSpec("voice.unmute", "Unmute voice",
               "Restore voice output.",
               feature_id="voice", reversible=True,
               aliases=("unmute", "unmute voice",
                        "speak up", "speak again"),
               run=lambda p, e: _set(e, "voice_muted",
                                     bool((p or {}).get("value", False)))),
    ActionSpec("voice.set_preset", "Use a voice preset",
               "Switch the voice preset Nexus speaks with.",
               feature_id="voice", reversible=True,
               aliases=("use isabella", "use a different voice",
                        "change voice", "switch voice"),
               run=lambda p, e: _set(e, "voice_preset_id",
                                     (p or {}).get("value", ""))),
    ActionSpec("voice.set_volume", "Set voice volume",
               "Set playback volume (0–100%).",
               feature_id="voice", reversible=True,
               aliases=("quieter", "louder", "volume", "turn it down",
                        "turn it up"),
               run=lambda p, e: _set(
                   e, "voice_volume",
                   float((p or {}).get("value", 100)) / 100.0)),
    ActionSpec("voice.stop", "Stop speaking",
               "Stop the currently playing voice output.",
               feature_id="voice",
               aliases=("stop talking", "stop speaking", "enough"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "voice_stop")),
                   detail="stopped current playback")),
    # -- Persona ---------------------------------------------------------------
    ActionSpec("persona.set_active", "Set persona",
               "Switch the active persona preset.",
               feature_id="persona",
               aliases=("use persona", "switch persona",
                        "be more playful", "personality"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "persona_set",
                                  (p or {}).get("value", ""))),
                   detail="persona switched")),
    ActionSpec("persona.set_strength", "Set persona strength",
               "How strongly the persona colors replies.",
               feature_id="persona",
               aliases=("tone it down", "be less", "be more",
                        "persona strength"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "persona_strength",
                                  (p or {}).get("value", ""))),
                   detail="strength applied")),
    # -- Images ----------------------------------------------------------------
    ActionSpec("image.backend.set", "Set image backend",
               "Pin the image engine — auto, invokeai, or comfyui.",
               feature_id="image_generation", reversible=True,
               aliases=("use invokeai", "use comfyui", "switch backend",
                        "image backend"),
               run=lambda p, e: _set(e, "image_backend",
                                     (p or {}).get("value", "auto"))),
    ActionSpec("image.backend.install", "Install image backend",
               "Provision the recommended local image runtime.",
               feature_id="image_generation", risk="confirm",
               requires_restart=False,
               confirmation_prompt="Install the image backend? It's a "
               "multi-GB download.",
               aliases=("install image backend", "set up images",
                        "install invokeai", "install comfyui"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "image_install")),
                   detail="provisioning queued",
                   links=[{"route": "/image.html",
                           "label": "Image Studio"}])),
    ActionSpec("image.backend.start", "Start image backend",
               "Launch the managed image runtime.",
               feature_id="image_generation", risk="confirm",
               aliases=("start invokeai", "start comfyui",
                        "start image backend"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "image_start")),
                   detail="backend starting")),
    ActionSpec("image.backend.stop", "Stop image backend",
               "Shut down the managed image runtime.",
               feature_id="image_generation",
               aliases=("stop invokeai", "stop comfyui",
                        "stop image backend"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "image_stop")),
                   detail="backend stopped")),
    # -- Autonomy & workers -----------------------------------------------------
    ActionSpec("autonomy.pause", "Pause autonomy",
               "Pause mission triggers and scheduled work.",
               feature_id="autonomy",
               aliases=("pause autonomy", "pause background work",
                        "stop working on your own"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "autonomy_pause")),
                   detail="supervisor paused")),
    ActionSpec("autonomy.resume", "Resume autonomy",
               "Resume mission triggers and scheduled work.",
               feature_id="autonomy",
               aliases=("resume autonomy", "resume background work"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "autonomy_resume")),
                   detail="supervisor resumed")),
    ActionSpec("autonomy.stop", "Stop autonomy",
               "Disable the mission supervisor.",
               feature_id="autonomy", risk="confirm",
               aliases=("turn autonomy off", "disable autonomy",
                        "stop autonomy"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "autonomy_stop")),
                   detail="supervisor disabled")),
    ActionSpec("workers.set_ceiling", "Set worker limit",
               "Cap how many workers run concurrently (1–16).",
               feature_id="workers", reversible=True,
               aliases=("set workers", "four workers", "worker limit",
                        "more workers", "fewer workers"),
               run=lambda p, e: _set(e, "worker_ceiling",
                                     int((p or {}).get("value", 4)))),
    ActionSpec("provisioning.pause", "Pause provisioning",
               "Pause background component downloads.",
               feature_id="provisioning",
               aliases=("pause downloads", "pause provisioning",
                        "stop downloading"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "provisioning_pause")),
                   detail="provisioning paused")),
    ActionSpec("provisioning.resume", "Resume provisioning",
               "Resume background component downloads.",
               feature_id="provisioning",
               aliases=("resume downloads", "resume provisioning"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "provisioning_resume")),
                   detail="provisioning resumed")),
    ActionSpec("model.provision", "Start model provisioning",
               "Start the managed model download.",
               feature_id="provisioning", risk="confirm",
               confirmation_prompt="Start the model download? It's "
               "multi-GB.",
               aliases=("install model", "download model"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "provision_models")),
                   detail="model provisioning started")),
    ActionSpec("model.set_performance", "Set performance mode",
               "Set the managed-runtime profile.",
               feature_id="model_management", reversible=True,
               aliases=("max performance", "quiet mode", "balanced"),
               run=lambda p, e: _set(e, "performance_mode",
                                     (p or {}).get("value", "auto"))),
    # -- Connectors ---------------------------------------------------------------
    ActionSpec("github.connect", "Connect GitHub",
               "Start the secure GitHub connection flow. The token is "
               "entered in the connector card — never in chat.",
               feature_id="github", risk="sensitive",
               aliases=("connect github", "sign in to github",
                        "link github"),
               run=lambda p, e: ActionResult(
                   False,
                   message="connect_securely",
                   detail="GitHub connects through the secure card — "
                          "credentials never enter chat.",
                   links=[{"route": "/settings.html#connections",
                           "label": "Connection Settings"}])),
    ActionSpec("github.disconnect", "Disconnect GitHub",
               "Remove the stored GitHub connection.",
               feature_id="github", risk="sensitive",
               aliases=("disconnect github",),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "github_disconnect")),
                   detail="GitHub disconnected")),
    ActionSpec("github.test", "Test GitHub",
               "Verify the stored GitHub credentials reach the API.",
               feature_id="github", risk="read_only",
               aliases=("test github", "check github"),
               run=lambda p, e: _github_test(e)),
    # -- Navigation ---------------------------------------------------------------
    ActionSpec("navigate", "Open page",
               "Navigate the interface to a registered route.",
               feature_id="chat", risk="read_only",
               aliases=("open", "take me to", "show me",
                        "go to", "navigate"),
               run=lambda p, e: ActionResult(
                   True, message=str((p or {}).get("route", "")),
                   detail="navigating")),
    # -- System ---------------------------------------------------------------------
    ActionSpec("safe_mode.exit", "Exit safe mode",
               "Return to normal operation after verifying the boot "
               "stabilized.",
               feature_id="safe_mode", risk="confirm",
               aliases=("exit safe mode", "leave safe mode",
                        "normal mode"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "safe_mode_exit")),
                   detail="safe mode exited")),
    ActionSpec("self_repair.apply", "Apply self-repair",
               "Promote a verified repair hypothesis.",
               feature_id="self_repair", risk="sensitive",
               aliases=("fix yourself", "apply repair"),
               run=lambda p, e: ActionResult(
                   bool(_env_call(e, "self_repair_apply")),
                   detail="repair promoted")),
    ActionSpec("notifications.set_voice", "Voice notifications",
               "Whether provisioning milestones are spoken aloud.",
               feature_id="notifications",
               aliases=("voice notifications",),
               run=lambda p, e: _set(e,
                                     "provisioning_voice_notifications",
                                     bool((p or {}).get("value", True)))),
    ActionSpec("appearance.set_theme", "Set theme",
               "Interface appearance.",
               feature_id="appearance",
               aliases=("dark mode", "light mode", "theme"),
               run=lambda p, e: ActionResult(
                   False,
                   message="not_implemented",
                   detail="Appearance preferences live in Settings → "
                          "Appearance for now.",
                   links=[{"route": "/settings.html#appearance",
                           "label": "Appearance"}])),
    ActionSpec("answer_memory.toggle", "Toggle answer memory",
               "Enable or disable trusted learned answers.",
               feature_id="answer_memory", reversible=True,
               aliases=("turn answer memory off",
                        "turn answer memory on"),
               run=lambda p, e: _set(e, "answer_memory_enabled",
                                     bool((p or {}).get("value", True)))),
    ActionSpec("autonomy.set_mode", "Set autonomous mode",
               "Auto-approve reversible workspace actions.",
               feature_id="autonomy", risk="sensitive",
               aliases=("unattended mode", "auto approve"),
               run=lambda p, e: _set(e, "autonomous_mode",
                                     bool((p or {}).get("value", False)))),
]


def _github_test(env: dict) -> ActionResult:
    fn = env.get("github_test")
    if not callable(fn):
        return ActionResult(False, "GitHub probe unavailable")
    out = fn()
    if isinstance(out, dict):
        connected = bool(out.get("connected"))
        return ActionResult(
            connected,
            message="connected" if connected else "not connected",
            detail=str(out.get("detail") or ""),
            verified=True)
    return ActionResult(bool(out))


class ActionRegistry:
    """Look up and execute registered actions — the only mutation
    surface chat is allowed to use.

    env callables (injected by AppState):
        permitted(key) -> "allow"|"session"|"ask"|"creator"|"deny"
        plus the named bodies each ActionSpec references.
    """

    def __init__(self, actions: list[ActionSpec] | None = None,
                 env: dict[str, Callable] | None = None) -> None:
        self._env = dict(env or {})
        self._actions = {a.id: a for a in (actions or ACTIONS)}
        self._by_alias: dict[str, str] = {}
        for a in self._actions.values():
            for n in (a.id, a.name.lower(), *a.aliases):
                if n:
                    self._by_alias.setdefault(n, a.id)

    def get(self, action_id: str) -> ActionSpec | None:
        return self._actions.get(action_id)

    def all(self) -> list[ActionSpec]:
        return list(self._actions.values())

    def find(self, text: str) -> ActionSpec | None:
        t = " " + " ".join(str(text or "").lower().split()) + " "
        best: tuple[int, str] | None = None
        for alias, aid in self._by_alias.items():
            if f" {alias} " in t or t.strip() == alias:
                if best is None or len(alias) > best[0]:
                    best = (len(alias), aid)
        return self._actions.get(best[1]) if best else None

    def permission(self, action: ActionSpec) -> str:
        if not action.permission:
            return "allow"
        fn = self._env.get("permitted")
        if not callable(fn):
            return "allow"
        try:
            return str(fn(action.permission) or "allow")
        except Exception:
            return "deny"

    def execute(self, action_id: str, params: dict | None = None,
                confirmed: bool = False) -> ActionResult:
        """Gate + run + verify — the only entry point chat uses."""
        spec = self._actions.get(action_id)
        if spec is None:
            return ActionResult(False, f"unknown action '{action_id}'")
        if spec.risk in ("confirm", "sensitive") and not confirmed:
            return ActionResult(
                False, message="needs_confirmation",
                detail=spec.confirmation_prompt or
                "This needs an explicit yes first.")
        verdict = self.permission(spec)
        if verdict not in ("allow", "session"):
            return ActionResult(
                False,
                message=f"permission {spec.permission} is {verdict}")
        if not callable(spec.run):
            return ActionResult(False, "action body unavailable")
        try:
            out = spec.run(dict(params or {}), self._env)
        except Exception as exc:
            return ActionResult(False,
                                f"{type(exc).__name__}: {exc}")
        if isinstance(out, ActionResult):
            res = out
        elif isinstance(out, dict):
            res = ActionResult(bool(out.get("ok", True)),
                               message=str(out.get("message") or ""),
                               detail=str(out.get("detail") or ""))
        elif isinstance(out, str):
            res = ActionResult(True, message=out)
        else:
            res = ActionResult(bool(out))
        # Verify after every mutating action — never trust the call.
        if res.ok and callable(spec.verify):
            try:
                res.verified = bool(spec.verify(self._env))
            except Exception:
                res.verified = False
        elif res.ok and spec.risk != "read_only":
            res.verified = True
        return res
