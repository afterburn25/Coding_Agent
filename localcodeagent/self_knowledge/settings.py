"""Canonical Settings Registry — every chat-mutable setting, once.

A SettingSpec names the config key, the kind of value, who may change
it, and where it lives in the UI. Mutation goes through the same setters
the pages call — env callables are injected so the registry is testable
without AppState and never duplicates a write path.

    get(name)           -> current value
    set(name, value)    -> validated write through the real setter
    spec(name)          -> SettingSpec for UI/chat rendering
    chat_mutable()      -> specs safe to change from chat
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

SETTING_TYPES = ("bool", "int", "float", "choice", "text")


@dataclass(slots=True)
class SettingSpec:
    """One user-facing setting."""
    key: str                    # AgentConfig attribute name
    name: str
    description: str = ""
    category: str = "general"
    type: str = "bool"          # bool|int|float|choice|text
    allowed_values: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    aliases: tuple[str, ...] = ()
    # Risk classification mirrors the action registry's Risk levels.
    risk: str = "low_risk"      # read_only|low_risk|confirm|sensitive|destructive
    permission: str = ""        # permission key checked before mutation
    chat_mutable: bool = True
    profile_specific: bool = False
    requires_restart: bool = False
    ui_route: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "name": self.name,
            "description": self.description, "category": self.category,
            "type": self.type,
            "allowed_values": list(self.allowed_values),
            "min": self.minimum, "max": self.maximum,
            "aliases": list(self.aliases), "risk": self.risk,
            "permission": self.permission,
            "chat_mutable": self.chat_mutable,
            "profile_specific": self.profile_specific,
            "requires_restart": self.requires_restart,
            "ui_route": self.ui_route,
        }


SETTINGS: list[SettingSpec] = [
    # -- Voice --------------------------------------------------------------
    SettingSpec("voice_enabled", "Voice",
                "Spoken replies through the local TTS engine.",
                category="voice",
                aliases=("voice", "speech", "talking", "spoken replies"),
                ui_route="/settings.html#voice"),
    SettingSpec("voice_muted", "Voice mute",
                "Mute voice output without disabling the subsystem.",
                category="voice", aliases=("mute", "muted", "quiet",
                                           "silence", "be quiet",
                                           "shut up"),
                ui_route="/settings.html#voice"),
    SettingSpec("voice_mode", "Voice mode",
                "When Nexus speaks — responses only, responses plus "
                "activity, manual trigger, or off.",
                category="voice", type="choice",
                allowed_values=("off", "responses", "responses_activity",
                                "manual"),
                aliases=("voice mode", "speak mode"),
                ui_route="/settings.html#voice"),
    SettingSpec("voice_preset_id", "Voice preset",
                "Which voice preset Nexus speaks with.",
                category="voice", type="choice",
                aliases=("voice preset", "voice name", "voice identity",
                         "isabella"),
                ui_route="/voice.html",
                # allowed_values resolved live from the preset store.
                ),
    SettingSpec("voice_volume", "Voice volume",
                "Playback volume for spoken replies (0.0–1.0).",
                category="voice", type="float", minimum=0.0, maximum=1.0,
                aliases=("volume", "louder", "quieter",
                         "turn it down", "turn it up"),
                ui_route="/settings.html#voice"),
    SettingSpec("voice_speed", "Voice speed",
                "Speaking rate multiplier (0.5–2.0).",
                category="voice", type="float", minimum=0.5, maximum=2.0,
                aliases=("talk faster", "talk slower", "speech rate",
                         "speaking speed"),
                ui_route="/settings.html#voice"),
    SettingSpec("voice_output_device", "Voice output device",
                "Audio output device for voice playback (empty = system "
                "default).",
                category="voice", type="text",
                aliases=("output device", "speakers"),
                ui_route="/settings.html#voice"),
    SettingSpec("startup_narration", "Startup narration",
                "Spoken narration during the startup cinematic.",
                category="voice",
                aliases=("startup narration", "boot voice"),
                ui_route="/settings.html#general"),
    SettingSpec("silent_startup", "Silent startup",
                "Skip splash audio and narration entirely.",
                category="voice",
                aliases=("silent startup", "quiet boot", "silent boot"),
                ui_route="/settings.html#general"),
    SettingSpec("splash_audio_enabled", "Splash audio",
                "Audio track during the startup cinematic.",
                category="voice",
                aliases=("splash audio", "splash sound"),
                ui_route="/settings.html#general"),
    SettingSpec("splash_volume", "Splash volume",
                "Volume of the startup cinematic audio (0.0–1.0).",
                category="voice", type="float", minimum=0.0, maximum=1.0,
                aliases=("splash volume",),
                ui_route="/settings.html#general"),
    # -- Speech input ---------------------------------------------------------
    SettingSpec("stt_backend", "Speech-to-text backend",
                "Which STT engine handles dictation — auto, "
                "faster-whisper, vosk, or off.",
                category="voice", type="choice",
                allowed_values=("auto", "faster-whisper", "whisper",
                                "vosk", "off"),
                aliases=("stt", "speech to text", "dictation",
                         "microphone input", "voice input"),
                ui_route="/settings.html#voice"),
    SettingSpec("stt_model", "STT model",
                "Which faster-whisper model size to use for dictation.",
                category="voice", type="choice",
                allowed_values=("tiny", "base", "small", "medium",
                                "large-v3"),
                aliases=("stt model", "whisper model"),
                ui_route="/settings.html#voice"),
    SettingSpec("stt_auto_submit", "STT auto-submit",
                "Submit dictated text automatically when speech ends.",
                category="voice",
                aliases=("auto submit", "send automatically"),
                ui_route="/settings.html#voice"),
    # -- Persona ---------------------------------------------------------------
    SettingSpec("conversation_policy_mode", "Conversation policy",
                "How permissive Nexus's conversational stance is — "
                "permissive, balanced, or strict.",
                category="persona", type="choice",
                allowed_values=("permissive", "balanced", "strict"),
                aliases=("policy mode", "conversation policy"),
                risk="confirm",
                ui_route="/settings.html#advanced"),
    # -- Images ----------------------------------------------------------------
    SettingSpec("image_enabled", "Image generation",
                "Whether the image subsystem is on.",
                category="images",
                aliases=("image generation", "image gen", "images"),
                ui_route="/image.html#backends"),
    SettingSpec("image_backend", "Image backend",
                "Which image engine jobs use — auto picks per request; "
                "invokeai or comfyui pins every job.",
                category="images", type="choice",
                allowed_values=("auto", "invokeai", "comfyui"),
                aliases=("image backend", "invokeai", "comfyui",
                         "image engine", "image system"),
                ui_route="/image.html#backends"),
    SettingSpec("image_resource_mode", "Image resource mode",
                "How much hardware image jobs may use.",
                category="images", type="choice",
                allowed_values=("light", "balanced", "performance"),
                aliases=("image performance",),
                ui_route="/image.html#backends"),
    SettingSpec("image_restore_chat_model", "Restore chat model",
                "Reload the coding model after an image job that "
                "displaced it.",
                category="images",
                ui_route="/image.html#backends"),
    # -- Autonomy & workers ------------------------------------------------------
    SettingSpec("autonomy_enabled", "Autonomy",
                "Whether the mission supervisor runs at all.",
                category="automation",
                aliases=("autonomy", "autonomous work",
                         "background missions"),
                risk="confirm",
                ui_route="/command.html#autonomy"),
    SettingSpec("autonomous_mode", "Autonomous mode",
                "Auto-approve reversible workspace actions so unattended "
                "work can proceed; hard-gated permissions still apply.",
                category="automation",
                aliases=("autonomous mode", "unattended mode",
                         "auto approve"),
                risk="sensitive",
                ui_route="/settings.html#advanced"),
    SettingSpec("worker_ceiling", "Worker ceiling",
                "Absolute cap on concurrent workers (1–16).",
                category="automation", type="int", minimum=1, maximum=16,
                aliases=("workers", "worker count", "worker limit",
                         "parallel workers", "how many workers"),
                ui_route="/command.html#workers"),
    SettingSpec("chat_queue_when_busy", "Queue while busy",
                "Queue a chat message instead of interrupting the "
                "running task.",
                category="automation",
                aliases=("queue messages",),
                ui_route="/settings.html#advanced"),
    # -- Models -----------------------------------------------------------------
    SettingSpec("max_resident_models", "Resident models",
                "How many model runtimes may stay loaded at once (1–3).",
                category="models", type="int", minimum=1, maximum=3,
                aliases=("resident models", "loaded models"),
                ui_route="/models.html#runtime"),
    SettingSpec("performance_mode", "Performance mode",
                "Managed-runtime performance profile — auto, quiet, "
                "balanced, or max.",
                category="models", type="choice",
                allowed_values=("auto", "quiet", "balanced", "max"),
                aliases=("performance mode", "speed", "max performance"),
                ui_route="/models.html#runtime"),
    SettingSpec("model_warmup", "Model warmup",
                "Prime a managed model with a 1-token request right "
                "after it reports healthy.",
                category="models",
                ui_route="/models.html#runtime"),
    SettingSpec("model_idle_unload_seconds", "Model idle unload",
                "Seconds an idle resident model may sit before being "
                "unloaded (0 disables).",
                category="models", type="int", minimum=0, maximum=86400,
                aliases=("unload models", "idle unload"),
                ui_route="/models.html#runtime"),
    SettingSpec("runtime_auto_start", "Runtime auto-start",
                "Start the model runtimes Nexus needs automatically.",
                category="models",
                aliases=("auto start models",),
                ui_route="/models.html#runtime"),
    SettingSpec("runtime_auto_tune", "Runtime auto-tune",
                "Benchmark managed models on this hardware while idle so "
                "launch flags are measured, not guessed.",
                category="models",
                aliases=("auto tune", "benchmarking"),
                ui_route="/models.html#runtime"),
    SettingSpec("runtime_dynamic_context", "Dynamic context",
                "Size each model's context window to its routing role "
                "instead of the profile maximum.",
                category="models",
                ui_route="/models.html#runtime"),
    # -- Research -----------------------------------------------------------------
    SettingSpec("research_enabled", "Research",
                "Whether Nexus may run web research.",
                category="research",
                aliases=("research", "web research"),
                ui_route="/research.html"),
    SettingSpec("research_mode", "Research mode",
                "How research runs — auto, local only, official sources, "
                "balanced, deep, offline, or none.",
                category="research", type="choice",
                allowed_values=("auto", "local_only", "official",
                                "balanced", "deep", "offline", "none"),
                aliases=("research mode", "web search", "search"),
                ui_route="/research.html"),
    SettingSpec("auto_research_unknown", "Auto-research unknowns",
                "Research answers on the fly when Nexus doesn't know "
                "something.",
                category="research",
                aliases=("auto research", "look things up"),
                ui_route="/research.html"),
    # -- Memory -------------------------------------------------------------------
    SettingSpec("conversation_memory_enabled", "Conversation memory",
                "Remember facts, preferences, and patterns across "
                "conversations.",
                category="memory",
                aliases=("conversation memory", "remember me",
                         "remembering"),
                ui_route="/knowledge.html"),
    SettingSpec("answer_memory_enabled", "Answer Memory",
                "Let trusted learned answers skip model inference.",
                category="memory",
                aliases=("answer memory", "learned answers"),
                ui_route="/answers.html"),
    SettingSpec("knowledge_memory_enabled", "Knowledge memory",
                "Retain sourced knowledge with provenance and TTL.",
                category="memory",
                aliases=("knowledge memory",),
                ui_route="/knowledge.html"),
    SettingSpec("nexus_brain_enabled", "Nexus Brain",
                "The persistent model-independent memory — disabling it "
                "stops new records; stored knowledge is untouched.",
                category="memory",
                aliases=("nexus brain", "brain"),
                risk="confirm",
                ui_route="/knowledge.html#brain"),
    # -- Provisioning ------------------------------------------------------------
    SettingSpec("provisioning_enabled", "Provisioning",
                "Background installation of components after launch.",
                category="system",
                aliases=("provisioning", "background installs",
                         "background provisioning"),
                ui_route="/settings.html#setup"),
    SettingSpec("provisioning_voice_notifications", "Provisioning voice "
                "notifications",
                "Speak provisioning milestones aloud.",
                category="system",
                aliases=("install notifications", "voice notices",
                         "notifications"),
                ui_route="/settings.html#notifications"),
    SettingSpec("autonomy_quiet_hours", "Quiet hours",
                "Hours during which noncritical notifications are muted.",
                category="system", type="text",
                aliases=("quiet hours", "do not disturb"),
                ui_route="/settings.html#notifications"),
    # -- System ---------------------------------------------------------------------
    SettingSpec("self_repair_auto_promote", "Self-repair auto-promote",
                "Promote verified code repairs without human review.",
                category="system",
                aliases=("auto promote repairs",),
                risk="sensitive",
                ui_route="/system.html"),
    SettingSpec("github_enabled", "GitHub integration",
                "Whether GitHub connectivity is enabled at all.",
                category="connectors",
                aliases=("github integration", "github"),
                ui_route="/settings.html#connections"),
    SettingSpec("process_watchdog", "Process watchdog",
                "Watch managed processes and mark stalled tasks failed "
                "so the queue keeps moving.",
                category="system",
                aliases=("watchdog",),
                ui_route="/settings.html#advanced"),
    SettingSpec("permission_profile", "Permission profile",
                "The named authorization posture — safe, research_only, "
                "developer, power_user, offline, or custom.",
                category="system", type="choice",
                allowed_values=("safe", "research_only", "developer",
                                "power_user", "offline", "custom"),
                aliases=("permission profile", "permissions profile"),
                risk="sensitive",
                ui_route="/settings.html#permissions"),
]


class SettingsRegistry:
    """Canonical settings metadata + validated chat mutation.

    env callables (injected by AppState):
        get(key)            -> current value
        set(key, value)     -> validated write + persist (returns value
                             actually applied, or raises)
        choices(key)        -> live allowed values (e.g. voice presets)
        permitted(perm)     -> "allow" | "session" | "ask" | "creator" | "deny"
    """

    def __init__(self, settings: list[SettingSpec] | None = None,
                 env: dict[str, Callable] | None = None) -> None:
        self._env = dict(env or {})
        self._specs = {s.key: s for s in (settings or SETTINGS)}
        self._by_alias: dict[str, str] = {}
        for s in self._specs.values():
            names = {s.key, s.name.lower(),
                     *(a.lower() for a in s.aliases)}
            for n in names:
                if n:
                    self._by_alias.setdefault(n, s.key)

    # -- lookup -----------------------------------------------------------------

    def get(self, key: str) -> SettingSpec | None:
        return self._specs.get(key)

    def all(self) -> list[SettingSpec]:
        return list(self._specs.values())

    def chat_mutable(self) -> list[SettingSpec]:
        return [s for s in self._specs.values() if s.chat_mutable]

    def find(self, text: str) -> SettingSpec | None:
        t = " " + " ".join(str(text or "").lower().split()) + " "
        best: tuple[int, int, str] | None = None
        for alias, key in self._by_alias.items():
            pos = t.find(f" {alias} ")
            if pos < 0 and t.strip() == alias:
                pos = 0
            if pos < 0:
                continue
            # Longest alias wins; ties go to the one appearing LAST —
            # 'is voice muted' should resolve voice_muted, not voice_enabled.
            score = (len(alias), pos)
            if best is None or score > (best[0], best[1]):
                best = (len(alias), pos, key)
        return self._specs.get(best[2]) if best else None

    def allowed_values(self, key: str) -> list[str]:
        """Live allowed values — spec list, or env-provided choices for
        dynamic sets like voice presets."""
        spec = self._specs.get(key)
        if spec is None:
            return []
        fn = self._env.get("choices")
        if callable(fn):
            try:
                live = fn(key)
                if live:
                    return list(live)
            except Exception:
                pass
        return list(spec.allowed_values)

    # -- value access ------------------------------------------------------------

    def current(self, key: str) -> Any:
        fn = self._env.get("get")
        return fn(key) if callable(fn) else None

    def describe(self, key: str) -> dict[str, Any]:
        spec = self._specs.get(key)
        if spec is None:
            return {}
        return {**spec.as_dict(), "current": self.current(key),
                "allowed_values": self.allowed_values(key)}

    # -- mutation ------------------------------------------------------------------

    def can_change(self, key: str) -> tuple[bool, str]:
        """Gate: is this setting chat-mutable right now?"""
        spec = self._specs.get(key)
        if spec is None:
            return False, "unknown setting"
        if not spec.chat_mutable:
            return False, "not chat-mutable"
        perm_fn = self._env.get("permitted")
        if spec.permission and callable(perm_fn):
            verdict = perm_fn(spec.permission)
            if verdict not in ("allow", "session"):
                return False, f"permission '{spec.permission}' is {verdict}"
        return True, ""

    def set(self, key: str, value: Any) -> dict[str, Any]:
        """Validated write through the real setter — same path the
        Settings UI uses. Returns the applied value + a verification
        read-back."""
        spec = self._specs.get(key)
        if spec is None:
            return {"ok": False, "error": f"unknown setting '{key}'"}
        ok, why = self.can_change(key)
        if not ok:
            return {"ok": False, "error": f"cannot change {key}: {why}"}
        try:
            value = self._coerce(spec, value)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"invalid value: {exc}"}
        previous = self.current(key)
        fn = self._env.get("set")
        if not callable(fn):
            return {"ok": False, "error": "no setter wired"}
        try:
            fn(key, value)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        verify = self.current(key)
        return {
            "ok": verify == value or verify is not None,
            "key": key, "value": verify, "previous": previous,
            "verified": verify == value,
            "requires_restart": spec.requires_restart,
        }

    def _coerce(self, spec: SettingSpec, value: Any) -> Any:
        if spec.type == "bool":
            if isinstance(value, bool):
                return value
            v = str(value).strip().lower()
            if v in ("on", "true", "yes", "enable", "enabled", "1"):
                return True
            if v in ("off", "false", "no", "disable", "disabled", "0"):
                return False
            raise ValueError(f"{spec.name} needs on/off")
        if spec.type == "int":
            v = int(float(str(value).strip()))
            if spec.minimum is not None:
                v = max(int(spec.minimum), v)
            if spec.maximum is not None:
                v = min(int(spec.maximum), v)
            return v
        if spec.type == "float":
            v = float(str(value).strip())
            if spec.minimum is not None:
                v = max(float(spec.minimum), v)
            if spec.maximum is not None:
                v = min(float(spec.maximum), v)
            return v
        if spec.type == "choice":
            v = str(value).strip().lower()
            allowed = self.allowed_values(spec.key)
            if allowed and v not in [str(a).lower() for a in allowed]:
                # tolerate fuzzy preset names — "isabella" matches the
                # preset id containing it
                match = [a for a in allowed if v in str(a).lower()]
                if len(match) == 1:
                    return match[0]
                raise ValueError(
                    f"{spec.name} must be one of: {', '.join(map(str, allowed))}")
            return v
        return value
