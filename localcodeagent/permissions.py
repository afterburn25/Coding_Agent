from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

# Permission levels ordered from most to least permissive.
# allow   - run without asking
# session - ask once, then allow for the rest of the session
# ask     - require an explicit approval every time
# creator - like ask, but the approval also requires an unlocked Nexus Brain
#           creator session (fail-closed: without a verifier it can never pass)
# deny    - never run
LEVELS = ("allow", "session", "ask", "creator", "deny")

# Workspace permission profiles. Each maps known permission keys to a level.
# "custom" means the stored per-key map is authoritative and is never applied
# automatically.
# Risk-sensitive keys shared by every profile. High-impact actions default to
# confirmation or denial even for permissive profiles.
_RISK_DEFAULTS = {
    "filesystem.delete": "ask",
    "packages.install": "ask",
    "skills.manage": "ask",
    "docker.access": "ask",
    "credentials.use": "ask",
    "git.push": "ask",
    "browser.submit": "ask",
    "external_api.call": "ask",
    "message.send": "ask",
    "tasks.queue": "ask",
    "spend.money": "deny",
    "microphone.use": "deny",
    "camera.use": "deny",
    # Computer Use — observation, screen capture, input channels, clipboard,
    # and process launch are separately gated. The original broad keys remain
    # compatibility aliases for older configs.
    "computer.observe": "ask",
    "computer.control": "ask",
    "desktop.view": "ask",
    "desktop.control": "ask",
    "screen.capture": "ask",
    "mouse.control": "ask",
    "keyboard.control": "ask",
    "clipboard.read": "ask",
    "clipboard.write": "ask",
    "application.launch": "ask",
}

# Granular permissions inherit the older broad Computer Use keys when the new
# key has no explicit configured level. Once a granular key is set it wins.
_PERMISSION_ALIASES = {
    "desktop.view": ("computer.observe",),
    "desktop.control": ("computer.control",),
    "screen.capture": ("computer.observe",),
    "mouse.control": ("computer.control",),
    "keyboard.control": ("computer.control",),
    "clipboard.read": ("computer.observe",),
    "clipboard.write": ("computer.control",),
    "application.launch": ("computer.control",),
}

PROFILES: dict[str, dict[str, str]] = {
    "safe": {
        **_RISK_DEFAULTS,
        "filesystem.read": "allow",
        "filesystem.write": "ask",
        "shell.execute": "ask",
        "git.execute": "ask",
        "image.read": "allow",
        "image.generate": "ask",
        "image.manage": "ask",
        "audio.read": "allow",
        "audio.generate": "ask",
        "audio.manage": "ask",
        "network.read": "ask",
        "browser.control": "ask",
        "github.read": "allow",
        "github.write": "ask",
        "docker.access": "deny",
    },
    "developer": {
        **_RISK_DEFAULTS,
        "filesystem.read": "allow",
        "filesystem.write": "ask",
        "shell.execute": "ask",
        "git.execute": "ask",
        "image.read": "allow",
        "image.generate": "allow",
        "image.manage": "ask",
        "audio.read": "allow",
        "audio.generate": "allow",
        "audio.manage": "ask",
        "network.read": "allow",
        "browser.control": "ask",
        "github.read": "allow",
        "github.write": "ask",
    },
    "power_user": {
        **_RISK_DEFAULTS,
        "filesystem.read": "allow",
        "filesystem.write": "allow",
        "filesystem.delete": "session",
        "shell.execute": "ask",
        "git.execute": "allow",
        "git.push": "session",
        "image.read": "allow",
        "image.generate": "allow",
        "image.manage": "allow",
        "audio.read": "allow",
        "audio.generate": "allow",
        "audio.manage": "session",
        "network.read": "allow",
        "browser.control": "session",
        "browser.submit": "session",
        "packages.install": "session",
        "docker.access": "session",
        "credentials.use": "session",
        "external_api.call": "session",
        "microphone.use": "ask",
        "camera.use": "ask",
        "github.read": "allow",
        "github.write": "session",
    },
    "offline": {
        **_RISK_DEFAULTS,
        "filesystem.read": "allow",
        "filesystem.write": "ask",
        "shell.execute": "ask",
        "git.execute": "ask",
        "image.read": "allow",
        "image.generate": "allow",
        "image.manage": "ask",
        "audio.read": "allow",
        "audio.generate": "allow",
        "audio.manage": "ask",
        "network.read": "deny",
        "browser.control": "deny",
        "browser.submit": "deny",
        "external_api.call": "deny",
        "message.send": "deny",
        "github.read": "deny",
        "github.write": "deny",
        "git.push": "deny",
    },
    "research_only": {
        **_RISK_DEFAULTS,
        "filesystem.read": "allow",
        "filesystem.write": "deny",
        "filesystem.delete": "deny",
        "shell.execute": "deny",
        "git.execute": "deny",
        "git.push": "deny",
        "packages.install": "deny",
        "docker.access": "deny",
        "credentials.use": "deny",
        "image.read": "allow",
        "image.generate": "deny",
        "image.manage": "deny",
        "audio.read": "allow",
        "audio.generate": "deny",
        "audio.manage": "deny",
        "network.read": "allow",
        "browser.control": "ask",
        "github.read": "allow",
        "github.write": "deny",
    },
}

# Union of every permission key known to the built-in profiles. Tool manifests
# may introduce additional keys; unknown keys default to "ask".
KNOWN_PERMISSIONS = sorted({key for profile in PROFILES.values() for key in profile})

# Actions with irreversible real-world side effects. Autonomous mode
# auto-approves "ask"/"session" workspace actions, but these always still
# require an explicit human approval. "deny" stays denied regardless.
AUTONOMY_NEVER_AUTO = frozenset({
    "spend.money",
    "message.send",
    # Skill/plugin lifecycle changes can alter future agent instructions and
    # available tools; autonomous missions must not silently widen themselves.
    "skills.manage",
    "microphone.use",
    "camera.use",
    # Desktop observation/control can expose the user's live screen or inject
    # input into unrelated applications; autonomous missions must ask a human.
    "computer.observe",
    "computer.control",
    "desktop.view",
    "desktop.control",
    "screen.capture",
    "mouse.control",
    "keyboard.control",
    "clipboard.read",
    "clipboard.write",
    "application.launch",
})

# Audit log bound (entries). Older entries are dropped once exceeded; the log
# is decisions/changes only, never tool arguments or secrets.
AUDIT_LIMIT = 2000

# Display/enforcement metadata for the permission matrix. Categories map to
# the Settings > Permissions category cards. `scope` is the default scope
# label; config.json `permission_scopes` may override per key.
PERMISSION_INFO: dict[str, dict[str, Any]] = {
    "filesystem.read":   {"label": "Read files", "category": "Filesystem", "scope": "Workspace", "risk": "low",
                          "blurb": "Read files inside the workspace directory.", "tools": ["read_file", "list_files", "search_files"]},
    "filesystem.write":  {"label": "Write files", "category": "Filesystem", "scope": "Workspace", "risk": "medium",
                          "blurb": "Create or modify files, including applying code patches.", "tools": ["write_file", "apply_patch"]},
    "filesystem.delete": {"label": "Delete files", "category": "Filesystem", "scope": "Workspace", "risk": "high",
                          "blurb": "Delete files or directories. Irreversible.", "tools": ["delete_path"]},
    "shell.execute":     {"label": "Execute terminal commands", "category": "Terminal", "scope": "Workspace", "risk": "high",
                          "blurb": "Run shell commands (PowerShell, CMD, bash) and spawned tools.", "tools": ["shell", "terminal", "run_process"]},
    "git.execute":       {"label": "Git commands", "category": "Git / GitHub", "scope": "Workspace repository", "risk": "medium",
                          "blurb": "Run local git commands: status, diff, commit, branch.", "tools": ["git"]},
    "git.push":          {"label": "Git push", "category": "Git / GitHub", "scope": "Configured remotes", "risk": "high",
                          "blurb": "Push commits to remote repositories. Publishes work publicly.", "tools": ["git"]},
    "github.read":       {"label": "GitHub read", "category": "Git / GitHub", "scope": "Configured repos", "risk": "low",
                          "blurb": "Read issues, pull requests, and repository data via the GitHub API.", "tools": ["github"]},
    "github.write":      {"label": "GitHub write", "category": "Git / GitHub", "scope": "Configured repos", "risk": "high",
                          "blurb": "Create/edit issues, pull requests, and comments on GitHub.", "tools": ["github"]},
    "packages.install":  {"label": "Install tools & packages", "category": "Tool Installation", "scope": "App tool directories", "risk": "high",
                          "blurb": "Download, install, update, or remove tools and packages (ComfyUI, Blender, pip packages).", "tools": ["tool_installer", "package_installer"]},
    "skills.manage":     {"label": "Manage skills & plugins", "category": "Tool Installation", "scope": "App skill directories", "risk": "high",
                          "blurb": "Install, update, enable, disable, roll back, or remove reusable skill/plugin packages.", "tools": ["skill_registry"]},
    "docker.access":     {"label": "Docker / sandbox", "category": "Docker / Sandbox", "scope": "Local daemon", "risk": "high",
                          "blurb": "Start containers and run code inside the local Docker sandbox.", "tools": ["docker", "sandbox"]},
    "credentials.use":   {"label": "Use credentials", "category": "Credentials", "scope": "Secret vault", "risk": "high",
                          "blurb": "Read stored secrets for authenticated API calls. Values are never shown.", "tools": ["secrets", "api_request"]},
    "network.read":      {"label": "Web requests", "category": "Network", "scope": "Any domain", "risk": "medium",
                          "blurb": "Fetch web pages and call HTTP endpoints.", "tools": ["fetch_url", "web_search"]},
    "external_api.call": {"label": "External API calls", "category": "Network", "scope": "Any domain", "risk": "medium",
                          "blurb": "Call third-party APIs from the api_request tool.", "tools": ["api_request"]},
    "browser.control":   {"label": "Browser automation", "category": "Browser Automation", "scope": "Any domain", "risk": "medium",
                          "blurb": "Drive a real browser: navigate, read pages, click, and fill forms.", "tools": ["browser_navigate", "browser_action"]},
    "browser.submit":    {"label": "Browser form submit", "category": "Browser Automation", "scope": "Any domain", "risk": "high",
                          "blurb": "Submit forms or perform logged-in actions in the browser.", "tools": ["browser_action"]},
    "image.read":        {"label": "Read images", "category": "Images", "scope": "Workspace", "risk": "low",
                          "blurb": "Load and inspect image files.", "tools": ["image_read"]},
    "image.generate":    {"label": "Image generation", "category": "Images", "scope": "Local runtime", "risk": "medium",
                          "blurb": "Generate or edit images with ComfyUI / local diffusion backends.", "tools": ["generate_image", "edit_image"]},
    "image.manage":      {"label": "Manage image models", "category": "Images", "scope": "App model dirs", "risk": "medium",
                          "blurb": "Download or remove diffusion model packs and workflows.", "tools": ["image_models"]},
    "message.send":      {"label": "Send messages", "category": "Communication", "scope": "Configured channels", "risk": "critical",
                          "blurb": "Send messages on your behalf. Always requires explicit approval.", "tools": []},
    "spend.money":       {"label": "Spend money", "category": "Communication", "scope": "Configured services", "risk": "critical",
                          "blurb": "Actions that can incur cost. Always requires explicit approval.", "tools": []},
    "microphone.use":    {"label": "Microphone", "category": "Devices", "scope": "Local device", "risk": "critical",
                          "blurb": "Access the microphone. Always requires explicit approval.", "tools": []},
    "camera.use":        {"label": "Camera", "category": "Devices", "scope": "Local device", "risk": "critical",
                          "blurb": "Access the camera. Always requires explicit approval.", "tools": []},
    "computer.observe":  {"label": "Legacy desktop observe", "category": "Desktop", "scope": "Local desktop", "risk": "high",
                          "blurb": "Legacy broad observation permission. Prefer desktop.view, screen.capture, or clipboard.read.", "tools": []},
    "computer.control":  {"label": "Legacy desktop control", "category": "Desktop", "scope": "Local desktop", "risk": "high",
                          "blurb": "Legacy broad input-control permission. Prefer the granular desktop/mouse/keyboard/clipboard/application keys.", "tools": []},
    "desktop.view":      {"label": "View desktop windows", "category": "Desktop", "scope": "Local desktop", "risk": "medium",
                          "blurb": "List visible windows and identify the foreground application.", "tools": ["computer_windows", "computer_active_window"]},
    "screen.capture":    {"label": "Capture screen", "category": "Desktop", "scope": "Local display", "risk": "high",
                          "blurb": "Take screenshots of the visible desktop, including private content on screen.", "tools": ["computer_screenshot"]},
    "desktop.control":   {"label": "Focus desktop windows", "category": "Desktop", "scope": "Local desktop", "risk": "high",
                          "blurb": "Switch or restore foreground windows.", "tools": ["computer_focus"]},
    "mouse.control":     {"label": "Mouse control", "category": "Desktop", "scope": "Local desktop", "risk": "high",
                          "blurb": "Move the pointer, click, and scroll in the active desktop session.", "tools": ["computer_click", "computer_scroll"]},
    "keyboard.control":  {"label": "Keyboard control", "category": "Desktop", "scope": "Local desktop", "risk": "high",
                          "blurb": "Inject keystrokes and shortcuts into the focused application. Typed content is not stored in the audit timeline.", "tools": ["computer_type", "computer_keys"]},
    "clipboard.read":    {"label": "Read clipboard", "category": "Desktop", "scope": "Local clipboard", "risk": "high",
                          "blurb": "Read current clipboard text, which may contain private data.", "tools": ["computer_clipboard_get"]},
    "clipboard.write":   {"label": "Write clipboard", "category": "Desktop", "scope": "Local clipboard", "risk": "medium",
                          "blurb": "Replace clipboard text. Audit records only the character count.", "tools": ["computer_clipboard_set"]},
    "application.launch": {"label": "Launch applications", "category": "Desktop", "scope": "Local machine", "risk": "high",
                           "blurb": "Start a local executable directly, without invoking a shell.", "tools": ["computer_launch"]},
    "tasks.queue":       {"label": "Queue background tasks", "category": "Automation", "scope": "Job manager", "risk": "medium",
                          "blurb": "Queue multi-step background work (installs, research, workflows).", "tools": ["task_queue"]},
}

# Map a key prefix to a category for keys not listed in PERMISSION_INFO
# (manifest tools may introduce new keys).
_PREFIX_CATEGORY = [
    ("filesystem", "Filesystem"), ("shell", "Terminal"), ("terminal", "Terminal"),
    ("process", "Terminal"), ("network", "Network"), ("external_api", "Network"),
    ("browser", "Browser Automation"), ("git", "Git / GitHub"), ("github", "Git / GitHub"),
    ("image", "Images"), ("research", "Research"), ("rag", "Research"),
    ("knowledge", "Research"), ("mcp", "MCP / External Tools"),
    ("docker", "Docker / Sandbox"), ("sandbox", "Docker / Sandbox"),
    ("packages", "Tool Installation"), ("tools", "Tool Installation"),
    ("skills", "Tool Installation"), ("plugins", "Tool Installation"),
    ("credentials", "Credentials"), ("secrets", "Credentials"),
    ("message", "Communication"), ("spend", "Communication"),
    ("desktop", "Desktop"), ("screen", "Desktop"), ("mouse", "Desktop"),
    ("keyboard", "Desktop"), ("clipboard", "Desktop"),
    ("application", "Desktop"), ("computer", "Desktop"),
    ("microphone", "Devices"), ("camera", "Devices"), ("tasks", "Automation"),
]


def permission_info(key: str) -> dict[str, Any]:
    """Display metadata for a permission key; synthesized for unknown keys."""
    info = PERMISSION_INFO.get(key)
    if info:
        return dict(info)
    prefix = key.split(".", 1)[0]
    category = next((c for p, c in _PREFIX_CATEGORY if prefix == p or key.startswith(p + ".")), "Other")
    label = key.replace(".", " ").replace("_", " ").title()
    return {"label": label, "category": category, "scope": "Global", "risk": "medium",
            "blurb": f"Controls '{key}' actions used by tools and plugins.", "tools": []}


def _host_allowed(host: str, scope: dict[str, Any]) -> str | None:
    """Return a denial reason if `host` violates the configured scope."""
    host = (host or "").lower().split(":", 1)[0]
    blocked = [str(d).lower() for d in scope.get("blocked_domains") or []]
    allowed = [str(d).lower() for d in scope.get("allowed_domains") or []]
    if any(host == d or host.endswith("." + d) for d in blocked):
        return f"domain '{host}' is on the blocked list for this permission"
    if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
        return f"domain '{host}' is not on the allowed list for this permission"
    return None


class PermissionManager:
    """Central permission policy for tool/action execution.

    Wraps the mutable `config.permissions` dict so existing configuration keeps
    working. Adds the `session` level, named workspace profiles, and an
    effective-mode query used by the tool registry.
    """

    def __init__(self, permissions: dict[str, str], *, profile: str = "custom",
                 autonomous: bool = False, scopes: dict[str, dict] | None = None,
                 audit_path: str | Path | None = None) -> None:
        self.permissions = permissions
        self.scopes: dict[str, dict] = scopes if scopes is not None else {}
        self.profile = profile if profile in PROFILES or profile == "custom" else "custom"
        self._autonomous = bool(autonomous)
        self._session_grants: set[str] = set()
        self._lock = threading.RLock()
        # Optional callable returning True when the Nexus Brain creator session
        # is unlocked. Wired by AppState; fail-closed without it.
        self.creator_verified: Callable[[], bool] | None = None
        # Session-only "last used" timestamps (decision-time, per key).
        self._last_used: dict[str, float] = {}
        # Bounded decision/change audit, persisted as JSONL.
        self._audit_path = Path(audit_path) if audit_path else None
        self._audit: list[dict[str, Any]] = []
        # Lines currently believed to be in the JSONL file — the file has no
        # natural bound, so it is compacted once it outlives 2x AUDIT_LIMIT.
        # Seeded from the real line count by _load_audit.
        self._audit_appends = 0
        self._load_audit()

    def creator_ok(self) -> bool:
        return bool(self.creator_verified and self.creator_verified())

    # ------------------------------------------------------------------ audit

    def _load_audit(self) -> None:
        if not self._audit_path or not self._audit_path.is_file():
            return
        try:
            lines = self._audit_path.read_text(encoding="utf-8").splitlines()
            self._audit_appends = len(lines)
            for line in lines[-AUDIT_LIMIT:]:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and "ts" in entry and "event" in entry:
                    self._audit.append(entry)
                    key = entry.get("permission")
                    if key:
                        self._last_used[key] = max(float(entry["ts"]), self._last_used.get(key, 0.0))
        except OSError:
            pass

    def record_event(self, event: str, permission: str = "", detail: str = "") -> None:
        """Append a bounded audit entry. Never pass arguments/secrets here —
        `detail` should be a short sanitized label (tool id, profile name...)."""
        entry = {"ts": time.time(), "event": str(event)[:64],
                 "permission": str(permission)[:80], "detail": str(detail)[:160]}
        with self._lock:
            self._audit.append(entry)
            if permission:
                self._last_used[permission] = entry["ts"]
            if len(self._audit) > AUDIT_LIMIT:
                self._audit = self._audit[-AUDIT_LIMIT:]
        if self._audit_path:
            try:
                self._audit_path.parent.mkdir(parents=True, exist_ok=True)
                with self._audit_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._audit_appends += 1
                if self._audit_appends > AUDIT_LIMIT * 2:
                    self._compact_audit()
            except OSError:
                pass

    def _compact_audit(self) -> None:
        """Rewrite the JSONL audit log with the bounded in-memory tail.

        The log is append-only, so without periodic compaction it grows a
        line per permission event forever — over long unattended sessions
        that is an unbounded disk leak. The in-memory audit is already
        capped at AUDIT_LIMIT, so a rewrite is the whole fix.
        """
        try:
            with self._lock:
                rows = [json.dumps(row, ensure_ascii=False) for row in self._audit]
            tmp = self._audit_path.with_suffix(self._audit_path.suffix + ".tmp")
            tmp.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
            tmp.replace(self._audit_path)
            self._audit_appends = len(rows)
        except OSError:
            pass

    def audit_entries(self, limit: int = 200, permission: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._audit
            if permission:
                rows = [e for e in rows if e.get("permission") == permission]
            return list(reversed(rows[-int(limit):]))

    def last_used(self, permission: str) -> float | None:
        ts = self._last_used.get(permission)
        return ts or None

    def record_use(self, permission: str) -> None:
        """In-memory 'last used' stamp for routine executions — not persisted
        to the audit log (decisions/changes only)."""
        with self._lock:
            self._last_used[permission] = time.time()

    # ------------------------------------------------------------------ scope

    def scope(self, permission: str) -> dict[str, Any]:
        return dict(self.scopes.get(permission) or {})

    def set_scope(self, permission: str, scope: dict[str, Any]) -> None:
        permission = str(permission or "").strip()
        if not permission:
            raise ValueError("permission is required")
        if not isinstance(scope, dict):
            raise ValueError("scope must be an object")
        clean: dict[str, Any] = {}
        for field in ("allowed_domains", "blocked_domains", "allowed_dirs",
                      "allowed_repos"):
            value = scope.get(field)
            if value is None:
                continue
            if not isinstance(value, list):
                raise ValueError(f"{field} must be a list")
            clean[field] = [str(v)[:300] for v in value[:64]]
        for field in ("workspace_only", "read_only"):
            if field in scope:
                clean[field] = bool(scope[field])
        self.scopes[permission] = clean
        self.record_event("scope_changed", permission)

    def check_url(self, permission: str, url: str) -> str | None:
        """Return a denial reason when `url` violates the permission's domain
        scope, else None. Generic enforcement for any tool taking a URL arg."""
        host = urlparse(str(url)).hostname or ""
        return _host_allowed(host, self.scopes.get(permission) or {})

    # ------------------------------------------------------------------ state

    def level(self, permission: str) -> str:
        """Raw configured level for a permission key, honoring legacy aliases."""
        permission = str(permission or "").strip()
        raw = self.permissions.get(permission)
        if raw is None:
            for alias in _PERMISSION_ALIASES.get(permission, ()):  # legacy config compatibility
                raw = self.permissions.get(alias)
                if raw is not None:
                    break
        mode = str(raw or "ask").strip().lower()
        return mode if mode in LEVELS else "ask"

    def effective(self, permission: str) -> str:
        """Effective decision for this call: allow / session / ask / creator / deny."""
        mode = self.level(permission)
        if mode == "deny":
            return "deny"
        if mode == "creator":
            # Creator-gated: requires approval AND an unlocked creator session.
            # Once verified it degrades to a normal per-action approval.
            return "ask" if self.creator_ok() else "creator"
        if self._autonomous and permission not in AUTONOMY_NEVER_AUTO:
            if mode in {"ask", "session"}:
                # Record the auto-grant so it is visible in summary()/audit UI.
                first = permission not in self._session_grants
                with self._lock:
                    self._session_grants.add(permission)
                if first:
                    self.record_event("auto_granted", permission, "autonomous mode")
                return "allow"
        if mode == "session":
            return "allow" if permission in self._session_grants else "ask"
        return mode

    @property
    def autonomous(self) -> bool:
        return self._autonomous

    def set_autonomous(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled != self._autonomous:
            self._autonomous = enabled
            self.record_event("autonomous_on" if enabled else "autonomous_off")
        else:
            self._autonomous = enabled

    def grant_session(self, permission: str) -> None:
        with self._lock:
            self._session_grants.add(permission)
        self.record_event("session_granted", permission)

    def clear_session(self) -> None:
        with self._lock:
            self._session_grants.clear()

    def set_level(self, permission: str, level: str) -> str:
        level = str(level or "").strip().lower()
        if level not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")
        permission = str(permission or "").strip()
        if not permission:
            raise ValueError("permission is required")
        self.permissions[permission] = level
        self.profile = "custom"
        if level != "session":
            with self._lock:
                self._session_grants.discard(permission)
        self.record_event("level_changed", permission, level)
        return level

    def apply_profile(self, name: str) -> str:
        name = str(name or "").strip().lower()
        if name not in PROFILES:
            raise ValueError(f"unknown permission profile '{name}'")
        self.permissions.update(PROFILES[name])
        self.profile = name
        self.clear_session()
        self.record_event("profile_changed", detail=name)
        return name

    def summary(self) -> dict[str, Any]:
        keys = sorted(set(KNOWN_PERMISSIONS) | set(self.permissions))
        infos = {key: permission_info(key) for key in keys}
        categories: dict[str, dict[str, Any]] = {}
        for key in keys:
            cat = infos[key]["category"]
            entry = categories.setdefault(cat, {"category": cat, "total": 0, "counts": {}})
            entry["total"] += 1
            eff = self.effective(key)
            entry["counts"][eff] = entry["counts"].get(eff, 0) + 1
        return {
            "profile": self.profile,
            "available_profiles": sorted(PROFILES) + ["custom"],
            "levels": list(LEVELS),
            "autonomous": self._autonomous,
            "autonomy_hard_gates": sorted(AUTONOMY_NEVER_AUTO),
            "permissions": {key: self.level(key) for key in keys},
            "session_grants": sorted(self._session_grants),
            "info": infos,
            "categories": sorted(categories.values(), key=lambda c: c["category"]),
            "scopes": {key: self.scope(key) for key in keys if self.scope(key)},
            "last_used": {key: ts for key, ts in sorted(self._last_used.items())},
        }
