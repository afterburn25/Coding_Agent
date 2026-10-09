"""First-class Capability Registry — the honest answer to "can Nexus do X?"

Every important capability has a real probed state:

    verified | available | degraded | setup_required |
    unauthorized | unavailable | broken | experimental

and a user-facing *disposition* describing what may be claimed about it:

    can_do_now | setup_required | authorization_required |
    explanation_only | running | attempted_failed |
    completed_unverified | completed_verified

The truth gate already flags replies that claim executed actions with no
tools run; this registry adds the missing half — *pre-flight* knowledge.
Before Nexus says "I'll push that", the registry can answer whether the
GitHub path is credentialed; after tools ran it can flag a claim that
contradicts a hard-negative capability (claiming a push while GitHub is
``unauthorized`` is a fabrication regardless of tool events).

Probes are injected through ``env`` callables so the registry stays
decoupled from AppState and is cheap to test.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

STATES = (
    "verified", "available", "degraded", "setup_required",
    "unauthorized", "unavailable", "broken", "experimental",
)
DISPOSITIONS = (
    "can_do_now", "setup_required", "authorization_required",
    "explanation_only", "running", "attempted_failed",
    "completed_unverified", "completed_verified",
)
# States where the execution path does not exist right now — a claim of
# having used one of these is a contradiction, not just unverified.
HARD_NEGATIVE = {"unavailable", "broken", "unauthorized"}
ALL_NEGATIVE = HARD_NEGATIVE | {"setup_required"}

_STATE_DISPOSITION = {
    "verified": "can_do_now",
    "available": "can_do_now",
    "degraded": "can_do_now",
    "setup_required": "setup_required",
    "unauthorized": "authorization_required",
    "unavailable": "explanation_only",
    "broken": "explanation_only",
    "experimental": "explanation_only",
}

CACHE_TTL_S = 30.0

# Negation markers that turn a domain mention into a capability denial.
_DENIAL_NEG_RE = re.compile(
    r"\b(?:don'?t|do\s+not|didn'?t|can'?t|cannot|can\s+not|couldn'?t|"
    r"unable|not\s+able|never\s+able|no\s+(?:way|access|tools?)|"
    r"without|lack(?:ing)?|not\s+(?:set\s+up|configured|equipped)|"
    r"aren'?t\s+set\s+up|not\s+currently\s+able)\b",
    re.IGNORECASE)


@dataclass(slots=True)
class CapabilityReport:
    id: str
    name: str
    state: str = "unavailable"
    detail: str = ""
    requirements_met: list[str] = field(default_factory=list)
    requirements_unmet: list[str] = field(default_factory=list)

    @property
    def disposition(self) -> str:
        return _STATE_DISPOSITION.get(self.state, "explanation_only")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "disposition": self.disposition,
            "detail": self.detail,
            "requirements_met": list(self.requirements_met),
            "requirements_unmet": list(self.requirements_unmet),
        }


@dataclass(slots=True)
class CapabilitySpec:
    id: str
    name: str
    probe: Callable[[dict[str, Any], CapabilityReport], None]
    # Terms in generated text that would constitute a claim about this
    # capability — used to detect claims contradicting a negative state.
    claim_terms: tuple[str, ...] = ()
    # Domain phrases that, when negated, constitute a denial of this
    # capability — used to catch stale self-knowledge ("I don't have a
    # browser") contradicting a live positive state.
    denial_terms: tuple[str, ...] = ()


def _tool(env: dict[str, Any], name: str) -> dict[str, Any] | None:
    fn = env.get("tool_manifest")
    if fn is None:
        return None
    try:
        return fn(name)
    except Exception:
        return None


def _tool_ok(env: dict[str, Any], names: tuple[str, ...]) -> tuple[bool, str]:
    """True when any named tool is installed + enabled + callable."""
    for n in names:
        m = _tool(env, n)
        if m and m.get("enabled") and m.get("callable") \
                and m.get("install_status") != "missing":
            return True, n
    return False, ""


def _call(env: dict[str, Any], key: str, *args: Any, default: Any = None) -> Any:
    fn = env.get(key)
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception:
        return default


# -- probes ---------------------------------------------------------------

def _probe_filesystem(env, r: CapabilityReport) -> None:
    ws = _call(env, "workspace")
    if not ws or not Path(ws).is_dir():
        r.state, r.detail = "unavailable", "no workspace directory"
        r.requirements_unmet.append("workspace")
        return
    r.requirements_met.append("workspace")
    if _call(env, "workspace_writable", default=False):
        r.requirements_met.append("write_permission")
        r.state, r.detail = "verified", "workspace writable"
    else:
        r.requirements_unmet.append("write_permission")
        r.state, r.detail = "degraded", "workspace read-only"


def _probe_code_editing(env, r: CapabilityReport) -> None:
    _probe_filesystem(env, r)
    if r.state == "unavailable":
        return
    ok, name = _tool_ok(env, ("apply_patch", "write_file", "fs_write"))
    if ok:
        r.requirements_met.append("edit_tool")
        r.detail = f"editing via {name}"
    else:
        r.requirements_unmet.append("edit_tool")
        r.state, r.detail = "degraded", "no file-edit tool enabled"


def _probe_terminal(env, r: CapabilityReport) -> None:
    ok, name = _tool_ok(env, ("run_terminal", "terminal_run", "run_command",
                              "shell_exec", "terminal"))
    if ok:
        r.state, r.detail = "verified", f"terminal via {name}"
        r.requirements_met.append("terminal_tool")
    else:
        r.state, r.detail = "unavailable", "no terminal tool installed/enabled"
        r.requirements_unmet.append("terminal_tool")


def _probe_git(env, r: CapabilityReport) -> None:
    if not _call(env, "command", "git"):
        r.state = "setup_required"
        r.detail = "git executable not found"
        r.requirements_unmet.append("git_binary")
        return
    r.requirements_met.append("git_binary")
    ok, name = _tool_ok(env, ("git_status", "git_commit", "git_branch",
                              "git_push", "git_init"))
    if ok:
        r.state, r.detail = "verified", f"git available ({name})"
        r.requirements_met.append("git_tool")
    else:
        r.state, r.detail = "available", "git binary present, no git tools"
        r.requirements_unmet.append("git_tool")


def _probe_github(env, r: CapabilityReport) -> None:
    if not _call(env, "github_enabled", default=False):
        r.state, r.detail = "setup_required", "GitHub integration disabled"
        r.requirements_unmet.append("github_enabled")
        return
    r.requirements_met.append("github_enabled")
    if not _call(env, "command", "git"):
        r.requirements_unmet.append("git_binary")
    if _call(env, "github_authorized", default=False):
        r.requirements_met.append("authorization")
        r.state, r.detail = "verified", "GitHub authorized"
    else:
        r.requirements_unmet.append("authorization")
        r.state, r.detail = "unauthorized", "no GitHub credential configured"


def _probe_toolchains(env, r: CapabilityReport) -> None:
    found = [c for c in ("python", "node", "npm", "dotnet", "cmake",
                         "gcc", "clang", "cargo", "javac")
             if _call(env, "command", c)]
    if found:
        r.state, r.detail = "verified", "toolchains: " + ", ".join(found[:6])
        r.requirements_met.extend(f"toolchain:{c}" for c in found[:6])
    else:
        r.state, r.detail = "unavailable", "no build toolchain detected"
        r.requirements_unmet.append("toolchain")


def _probe_testing(env, r: CapabilityReport) -> None:
    runners = []
    if _call(env, "command", "python") or _call(env, "command", "pytest"):
        runners.append("python")
    if _call(env, "command", "npm") or _call(env, "command", "node"):
        runners.append("node")
    ws = _call(env, "workspace")
    if runners and ws:
        r.state, r.detail = "verified", "test runners: " + ", ".join(runners)
        r.requirements_met.extend(f"runner:{x}" for x in runners)
    elif runners:
        r.state, r.detail = "available", "runners exist; no workspace bound"
        r.requirements_met.extend(f"runner:{x}" for x in runners)
        r.requirements_unmet.append("workspace")
    else:
        r.state, r.detail = "unavailable", "no test runner detected"
        r.requirements_unmet.append("runner")


def _probe_browser_preview(env, r: CapabilityReport) -> None:
    # Real browser state beats manifest presence — a registered tool that
    # cannot launch a browser is not "available".
    state = str(_call(env, "browser_state", default="") or "")
    if state == "ready":
        r.state, r.detail = "verified", "browser automation ready"
        r.requirements_met.extend(("playwright", "browser"))
        return
    if state == "no_browser":
        r.state, r.detail = ("setup_required",
                             "playwright present; browser not provisioned")
        r.requirements_met.append("playwright")
        r.requirements_unmet.append("browser")
        return
    if state == "no_playwright":
        r.state, r.detail = ("setup_required",
                             "browser automation package not in this build")
        r.requirements_unmet.append("playwright")
        r.requirements_unmet.append("browser")
        return
    ok, name = _tool_ok(env, ("browser_run", "browser_preview",
                              "browser_navigate", "webview_open"))
    if ok:
        r.state, r.detail = "available", f"preview via {name}"
        r.requirements_met.append("browser_tool")
    else:
        r.state, r.detail = "setup_required", "no browser automation tool"
        r.requirements_unmet.append("browser_tool")


def _probe_web_access(env, r: CapabilityReport) -> None:
    """Outbound web reach — search + fetch. Offline policy and missing
    tools are different blockers and must be named separately."""
    if _call(env, "network_offline", default=False):
        r.state, r.detail = "unavailable", "offline policy mode active"
        r.requirements_unmet.append("network_policy")
        return
    ok, name = _tool_ok(env, ("web_search", "fetch_url"))
    if ok:
        r.state, r.detail = "verified", f"web access via {name}"
        r.requirements_met.append("web_tool")
        return
    r.state, r.detail = "setup_required", "no web search/fetch tool enabled"
    r.requirements_unmet.append("web_tool")


def _probe_deployment(env, r: CapabilityReport) -> None:
    if _call(env, "command", "docker"):
        r.state = "experimental"
        r.detail = "docker present; deploy adapters are experimental"
        r.requirements_met.append("docker")
    else:
        r.state, r.detail = "unavailable", "no deployment adapter installed"
        r.requirements_unmet.append("deploy_adapter")


def _probe_image_generation(env, r: CapabilityReport) -> None:
    if not _call(env, "image_enabled", default=False):
        r.state, r.detail = "unavailable", "image generation disabled"
        r.requirements_unmet.append("image_enabled")
        return
    r.requirements_met.append("image_enabled")
    backend = str(_call(env, "image_backend_state", default="") or "")
    if backend in ("running", "healthy", "starting", "loading"):
        r.state, r.detail = "verified", f"image backend {backend}"
        r.requirements_met.append("image_backend")
    elif backend in ("error", "crashed"):
        r.state, r.detail = "broken", f"image backend {backend}"
        r.requirements_unmet.append("image_backend")
    else:
        r.state, r.detail = "setup_required", "image backend not running"
        r.requirements_unmet.append("image_backend")


def _probe_stt(env, r: CapabilityReport) -> None:
    if not _call(env, "stt_enabled", default=False):
        r.state, r.detail = "unavailable", "speech input disabled"
        r.requirements_unmet.append("stt_enabled")
        return
    r.requirements_met.append("stt_enabled")
    eng = _call(env, "stt_engine_state", default="")
    if eng in ("ready", "running"):
        r.state, r.detail = "verified", "STT engine ready"
        r.requirements_met.append("stt_engine")
    elif eng in ("error", "missing"):
        r.state, r.detail = "setup_required", f"STT engine {eng or 'not provisioned'}"
        r.requirements_unmet.append("stt_engine")
    else:
        # Lazy engines report empty — importable but not yet loaded.
        r.state, r.detail = "available", "STT loads on first use"
        r.requirements_met.append("stt_engine")


def _probe_tts(env, r: CapabilityReport) -> None:
    if not _call(env, "voice_enabled", default=False):
        r.state, r.detail = "unavailable", "voice output disabled"
        r.requirements_unmet.append("voice_enabled")
        return
    r.requirements_met.append("voice_enabled")
    if _call(env, "voice_ready", default=False):
        r.state, r.detail = "verified", "TTS engine ready"
        r.requirements_met.append("voice_engine")
    elif _call(env, "voice_present", default=False):
        r.state, r.detail = "degraded", "voice subsystem loaded but not ready"
        r.requirements_unmet.append("voice_engine")
    else:
        r.state, r.detail = "setup_required", "voice assets not provisioned"
        r.requirements_unmet.append("voice_engine")


def _probe_desktop_control(env, r: CapabilityReport) -> None:
    ok, name = _tool_ok(env, ("computer_use", "desktop_control",
                              "screen_capture", "mouse_click"))
    if ok:
        r.state = "experimental"
        r.detail = f"desktop control via {name} (experimental)"
        r.requirements_met.append("desktop_tool")
    else:
        r.state, r.detail = "unavailable", "no desktop-control tool installed"
        r.requirements_unmet.append("desktop_tool")


def _probe_social(env, r: CapabilityReport) -> None:
    """Social/agent-network participation — grounded in the live
    connector state, including onboarding/claim status, never lore."""
    st = _call(env, "connector_state", "moltbook", default={}) or {}
    state = str(st.get("state") or "")
    account = str(st.get("account") or "")
    if state in ("missing", "disabled"):
        r.state, r.detail = "setup_required", "connector not configured"
        r.requirements_unmet.append("connector")
        return
    r.requirements_met.append("connector")
    if account == "active":
        r.state, r.detail = "verified", "agent identity verified"
        r.requirements_met.append("account")
        return
    if account == "awaiting_owner_verification":
        r.state, r.detail = ("setup_required",
                             "registered — awaiting owner claim")
        r.requirements_met.append("registered")
        r.requirements_unmet.append("owner_claim")
        return
    r.state, r.detail = ("setup_required",
                         "connector live — agent not yet registered")
    r.requirements_unmet.append("registration")


def _probe_coding_model(env, r: CapabilityReport) -> None:
    ready = _call(env, "llm_ready", default=None)
    if ready is True:
        r.state, r.detail = "verified", "coding model ready"
        r.requirements_met.append("model")
    elif ready is False:
        r.state, r.detail = "degraded", "model configured but runtime not ready"
        r.requirements_unmet.append("model_runtime")
    else:
        r.state, r.detail = "setup_required", "no coding model configured"
        r.requirements_unmet.append("model")


def _default_specs() -> list[CapabilitySpec]:
    return [
        CapabilitySpec("filesystem", "Local filesystem write",
                       _probe_filesystem,
                       ("file", "files", "wrote", "saved", "created",
                        "deleted", "edited", "folder", "directory"),
                       ("filesystem", "the file system")),
        CapabilitySpec("code_editing", "Code editing",
                       _probe_code_editing,
                       ("applied", "patched", "refactored", "edited",
                        "modified")),
        CapabilitySpec("terminal", "Terminal commands",
                       _probe_terminal,
                       ("command", "terminal", "ran", "executed", "shell"),
                       ("run commands", "the terminal", "a terminal",
                        "shell access")),
        CapabilitySpec("git", "Local Git", _probe_git,
                       ("git", "commit", "committed", "branch", "clone",
                        "cloned", "checkout", "merge", "staged"),
                       ("git",)),
        CapabilitySpec("github", "GitHub", _probe_github,
                       ("github", "pushed", "push", "pull request", "pr",
                        "remote", "synced", "ci"),
                       ("github",)),
        CapabilitySpec("compilation", "Build toolchain",
                       _probe_toolchains,
                       ("compiled", "build", "built", "packaged")),
        CapabilitySpec("testing", "Testing", _probe_testing,
                       ("tests", "test", "pytest", "unittest",
                        "test suite")),
        CapabilitySpec("web_access", "Web access", _probe_web_access,
                       ("searched", "fetched", "looked up", "the web"),
                       ("web access", "the web", "the internet",
                        "internet access", "websites", "a website",
                        "external sites", "external access",
                        "online", "web search", "fetch urls",
                        "download files", "download")),
        CapabilitySpec("browser_preview", "Browser preview",
                       _probe_browser_preview,
                       ("preview", "browser", "opened the app", "page"),
                       ("a browser", "a web browser", "browser",
                        "browse the web", "browsing")),
        CapabilitySpec("deployment", "Deployment", _probe_deployment,
                       ("deployed", "deploy", "released", "published")),
        CapabilitySpec("image_generation", "Image generation",
                       _probe_image_generation,
                       ("generated", "image", "picture", "render",
                        "artwork"),
                       ("generate images", "create images",
                        "image generation", "make images")),
        CapabilitySpec("stt", "Speech-to-text", _probe_stt,
                       ("transcribed", "heard you", "listened")),
        CapabilitySpec("tts", "Text-to-speech", _probe_tts,
                       ("spoke", "said aloud", "voice reply",
                        "narrated"),
                       ("a voice", "speak")),
        CapabilitySpec("desktop_control", "Desktop control",
                       _probe_desktop_control,
                       ("clicked", "moved the mouse", "typed into",
                        "screen"),
                       ("control your desktop", "see your screen",
                        "your screen")),
        CapabilitySpec("coding_model", "Coding model", _probe_coding_model,
                       ("model", "inference")),
        CapabilitySpec("moltbook", "Agent network", _probe_social,
                       ("moltbook",),
                       ("moltbook", "the agent community",
                        "an agent community", "ai community",
                        "the agent network", "a social network",
                        "social platforms", "an ai community")),
    ]


class CapabilityRegistry:
    """Probed, cached view of what the installation can actually do."""

    def __init__(self, env: dict[str, Callable] | None = None,
                 specs: list[CapabilitySpec] | None = None,
                 ttl_s: float = CACHE_TTL_S) -> None:
        self._env = dict(env or {})
        self._specs = list(specs or _default_specs())
        self._ttl = max(1.0, float(ttl_s))
        self._reports: dict[str, tuple[float, CapabilityReport]] = {}

    # -- evaluation --------------------------------------------------------

    def evaluate_one(self, cap_id: str, *, force: bool = False) -> CapabilityReport:
        now = time.time()
        cached = self._reports.get(cap_id)
        if cached and not force and now - cached[0] < self._ttl:
            return cached[1]
        spec = next((s for s in self._specs if s.id == cap_id), None)
        if spec is None:
            report = CapabilityReport(cap_id, cap_id, "unavailable",
                                      "unknown capability")
        else:
            report = CapabilityReport(spec.id, spec.name)
            try:
                spec.probe(self._env, report)
            except Exception as exc:
                report.state = "broken"
                report.detail = f"probe failed: {type(exc).__name__}"
        self._reports[cap_id] = (now, report)
        return report

    def evaluate(self, *, force: bool = False) -> dict[str, CapabilityReport]:
        return {s.id: self.evaluate_one(s.id, force=force)
                for s in self._specs}

    def summary(self, *, force: bool = False) -> dict[str, Any]:
        reports = self.evaluate(force=force)
        return {
            "time": time.time(),
            "capabilities": {k: r.as_dict() for k, r in reports.items()},
            "blocked": [k for k, r in reports.items()
                        if r.state in ALL_NEGATIVE],
            "states": list(STATES),
            "dispositions": list(DISPOSITIONS),
        }

    def invalidate(self, cap_id: str | None = None) -> None:
        """Drop cached verdicts — call after setup/auth state changes."""
        if cap_id is None:
            self._reports.clear()
        else:
            self._reports.pop(cap_id, None)

    # -- honesty helpers -----------------------------------------------------

    def blocked(self) -> dict[str, CapabilityReport]:
        return {k: r for k, r in self.evaluate().items()
                if r.state in ALL_NEGATIVE}

    def prompt_note(self) -> str:
        """Compact system-prompt line naming capabilities that cannot run
        right now — keeps the model from promising dead execution paths."""
        neg = self.blocked()
        if not neg:
            return ""
        bits = [f"{r.name} ({r.state})" for r in neg.values()]
        return ("Runtime capabilities currently unavailable: "
                + "; ".join(bits[:8])
                + ". Do not promise or claim actions that need them — "
                  "explain the missing requirement instead.")

    def contradicted(self, text: str) -> list[CapabilityReport]:
        """Capabilities in a hard-negative state that ``text`` claims to
        have used. A claim about a dead path is fabrication even when
        unrelated tools ran in the same reply."""
        low = " " + str(text or "").lower() + " "
        out: list[CapabilityReport] = []
        reports = self.evaluate()
        for spec in self._specs:
            report = reports.get(spec.id)
            if report is None or report.state not in HARD_NEGATIVE:
                continue
            if any(f" {t} " in low or low.startswith(f" {t}")
                   for t in spec.claim_terms):
                out.append(report)
        return out

    def denied(self, text: str) -> list[CapabilityReport]:
        """The inverse of ``contradicted``: capabilities in a *positive*
        state that ``text`` denies having ("I don't have a browser",
        "I can't access websites") — stale self-knowledge overriding
        live runtime truth. Runtime reality wins; these replies must be
        regenerated against the probed state."""
        low = str(text or "").lower()
        out: list[CapabilityReport] = []
        reports = self.evaluate()
        for spec in self._specs:
            if not spec.denial_terms:
                continue
            report = reports.get(spec.id)
            if report is None or report.state in ALL_NEGATIVE:
                # Denying a dead capability is truthful, not stale lore.
                continue
            for term in spec.denial_terms:
                pos = 0
                hit = False
                while True:
                    i = low.find(term, pos)
                    if i < 0:
                        break
                    pos = i + len(term)
                    # The denial must live in the same clause — a
                    # negation marker between the last sentence break
                    # and the domain phrase.
                    boundary = max(low.rfind(c, 0, i)
                                   for c in ".!?\n")
                    if _DENIAL_NEG_RE.search(low[boundary + 1:i]):
                        hit = True
                        break
                if hit:
                    out.append(report)
                    break
        return out

    def capability_brief(self) -> str:
        """Compact positive roster for utility/self-knowledge prompts —
        what the runtime can actually do right now, so ability answers
        come from probed state rather than remembered lore."""
        ups = [r.name for r in self.evaluate().values()
               if r.state in ("verified", "available", "degraded")]
        if not ups:
            return ""
        return ("Live capability check — currently available: "
                + "; ".join(ups[:14])
                + ". Answer ability questions from this list and the "
                  "unavailable list, never from memory.")

