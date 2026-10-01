from __future__ import annotations

import threading
from typing import Any

# Permission levels ordered from most to least permissive.
# allow   - run without asking
# session - ask once, then allow for the rest of the session
# ask     - require an explicit approval every time
# deny    - never run
LEVELS = ("allow", "session", "ask", "deny")

# Workspace permission profiles. Each maps known permission keys to a level.
# "custom" means the stored per-key map is authoritative and is never applied
# automatically.
# Risk-sensitive keys shared by every profile. High-impact actions default to
# confirmation or denial even for permissive profiles.
_RISK_DEFAULTS = {
    "filesystem.delete": "ask",
    "packages.install": "ask",
    "docker.access": "ask",
    "credentials.use": "ask",
    "git.push": "ask",
    "browser.submit": "ask",
    "external_api.call": "ask",
    "message.send": "ask",
    "spend.money": "deny",
    "microphone.use": "deny",
    "camera.use": "deny",
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
        "network.read": "allow",
        "browser.control": "ask",
        "github.read": "allow",
        "github.write": "deny",
    },
}

# Union of every permission key known to the built-in profiles. Tool manifests
# may introduce additional keys; unknown keys default to "ask".
KNOWN_PERMISSIONS = sorted({key for profile in PROFILES.values() for key in profile})


class PermissionManager:
    """Central permission policy for tool/action execution.

    Wraps the mutable `config.permissions` dict so existing configuration keeps
    working. Adds the `session` level, named workspace profiles, and an
    effective-mode query used by the tool registry.
    """

    def __init__(self, permissions: dict[str, str], *, profile: str = "custom") -> None:
        self.permissions = permissions
        self.profile = profile if profile in PROFILES or profile == "custom" else "custom"
        self._session_grants: set[str] = set()
        self._lock = threading.RLock()

    def level(self, permission: str) -> str:
        """Raw configured level for a permission key."""
        mode = str(self.permissions.get(permission, "ask")).strip().lower()
        return mode if mode in LEVELS else "ask"

    def effective(self, permission: str) -> str:
        """Effective decision for this call: allow / ask / deny."""
        mode = self.level(permission)
        if mode == "session":
            return "allow" if permission in self._session_grants else "ask"
        return mode

    def grant_session(self, permission: str) -> None:
        with self._lock:
            self._session_grants.add(permission)

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
        return level

    def apply_profile(self, name: str) -> str:
        name = str(name or "").strip().lower()
        if name not in PROFILES:
            raise ValueError(f"unknown permission profile '{name}'")
        self.permissions.update(PROFILES[name])
        self.profile = name
        self.clear_session()
        return name

    def summary(self) -> dict[str, Any]:
        keys = sorted(set(KNOWN_PERMISSIONS) | set(self.permissions))
        return {
            "profile": self.profile,
            "available_profiles": sorted(PROFILES) + ["custom"],
            "levels": list(LEVELS),
            "permissions": {key: self.level(key) for key in keys},
            "session_grants": sorted(self._session_grants),
        }
