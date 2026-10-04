"""Autonomy Policy Engine.

Profiles are grouped *policy settings* — the existing PermissionManager and
ToolRegistry remain authoritative. This layer decides, per mission/profile:

  - which action classes an autonomous task may take without asking
  - which must pause the mission into waiting_approval
  - which are denied outright (never silently widened)

Standing grants are explicit, scoped, expirable, revocable user
authorizations — e.g. "may create local commits in this repo, never push".
"""
from __future__ import annotations

import fnmatch
import time
import uuid
from typing import Any, Callable

from ..policies import NETWORK_ACTIONS

AUTONOMY_PROFILES = ("supervised", "local_autonomous", "extended_autonomous", "custom")

# Action classes → the existing permission id that governs them.
ACTION_PERMISSION = {
    "read_files": "filesystem.read",
    "write_workspace": "filesystem.write",
    "run_shell": "shell.execute",
    "run_tests": "shell.execute",
    "build": "shell.execute",
    "research": "network.read",
    "browser": "browser.control",
    "git_commit": "git.write",
    "git_push": "github.write",
    "create_pr": "github.write",
    "packages": "packages.install",
    "image_generate": "image.generate",
    "restart_service": "services.control",
    "self_development": "self.modify",
    "notify": "notifications.local",
}

# High-impact actions that ALWAYS ask unless a valid standing grant covers
# them, regardless of profile.
SENSITIVE_ACTIONS = {
    "git_push", "create_pr", "packages", "delete_data", "deploy",
    "credentials", "outbound_message", "purchase", "subscription",
}

# What each profile may do autonomously (action classes).
PROFILE_ALLOW = {
    "supervised": {"read_files", "run_tests", "notify"},
    "local_autonomous": {
        "read_files", "write_workspace", "run_shell", "run_tests", "build",
        "research", "git_commit", "restart_service", "image_generate",
        "notify",
    },
    "extended_autonomous": {
        "read_files", "write_workspace", "run_shell", "run_tests", "build",
        "research", "browser", "git_commit", "git_push", "create_pr",
        "restart_service", "image_generate", "notify",
    },
    "custom": set(),  # falls through to raw permission checks
}


class AutonomyPolicy:
    def __init__(self, store, permission_manager=None,
                 policies=None) -> None:
        self._store = store
        self._pm = permission_manager
        # ResourcePolicies — offline mode and per-project egress deny
        # network action classes regardless of profile allowances.
        self._policies = policies

    # -- global control ---------------------------------------------------

    def is_stopped(self) -> bool:
        return bool(self._store.control.data.get("stop"))

    def is_paused(self) -> bool:
        return bool(self._store.control.data.get("paused"))

    def set_stopped(self, on: bool) -> None:
        self._store.control.data["stop"] = bool(on)
        if on:
            self._store.control.data["paused"] = True
        self._store.control.save()

    def set_paused(self, on: bool) -> None:
        self._store.control.data["paused"] = bool(on)
        if not on:
            self._store.control.data["stop"] = False
        self._store.control.save()

    def resource_mode(self) -> str:
        if self._policies is not None:
            return self._policies.mode()
        return str(self._store.control.data.get("resource_mode") or "balanced")

    def set_resource_mode(self, mode: str) -> None:
        if self._policies is not None:
            if self._policies.set_mode(mode):
                # Mirror into control data for consumers that only read
                # the autonomy store.
                self._store.control.data["resource_mode"] = mode
                self._store.control.save()
            return
        if mode in {"conservative", "balanced", "performance"}:
            self._store.control.data["resource_mode"] = mode
            self._store.control.save()

    # -- action checks -----------------------------------------------------

    def check(self, action: str, *, profile: str = "local_autonomous",
              scope: str = "") -> str:
        """Returns 'allow' | 'ask' | 'deny' for an action class.

        Order: global stop → standing grant → profile allow-list →
        underlying permission mode → sensitive-action gate.
        """
        if self.is_stopped():
            return "deny"
        # Offline/egress gates outrank profile allowances and grants —
        # a standing grant can never open the network in offline mode.
        if self._policies is not None and action in NETWORK_ACTIONS:
            try:
                allowed, _why = self._policies.network_allowed(
                    project_id=scope, action=action)
                if not allowed:
                    return "deny"
            except Exception:
                pass
        grant = self.matching_grant(action, scope=scope)
        if grant is not None:
            return "allow" if grant.get("effect", "allow") == "allow" else "deny"

        allowed = PROFILE_ALLOW.get(profile, set()) or set()
        profile_ok = action in allowed

        perm = ACTION_PERMISSION.get(action, "")
        perm_mode = ""
        if perm and self._pm is not None:
            try:
                perm_mode = str(self._pm.effective(perm))
            except Exception:
                perm_mode = ""

        if action in SENSITIVE_ACTIONS:
            # Standing grant was checked above; without one, sensitive
            # actions always escalate in autonomous context.
            return "ask"

        if perm_mode == "deny":
            return "deny"
        if not profile_ok:
            return "ask" if perm_mode != "deny" else "deny"
        if perm_mode == "ask":
            return "ask"
        return "allow"

    # -- standing grants -----------------------------------------------------

    def grants(self) -> list[dict]:
        return self._store.grants.rows()

    def grant(self, action: str, *, scope: str = "", expires_in_s: float = 0,
              created_by: str = "user", note: str = "") -> dict:
        row = {
            "id": f"g-{uuid.uuid4().hex[:10]}",
            "action": str(action),
            "scope": str(scope),
            "effect": "allow",
            "created_by": created_by,
            "created_at": time.time(),
            "expires_at": time.time() + expires_in_s if expires_in_s else None,
            "revoked": False,
            "note": str(note)[:300],
        }
        data = self._store.grants.data.setdefault("grants", [])
        data.append(row)
        self._store.grants.save()
        return dict(row)

    def revoke(self, grant_id: str) -> bool:
        for g in self._store.grants.data.setdefault("grants", []):
            if g.get("id") == grant_id:
                g["revoked"] = True
                self._store.grants.save()
                return True
        return False

    def matching_grant(self, action: str, *, scope: str = "") -> dict | None:
        now = time.time()
        for g in self.grants():
            if g.get("revoked") or g.get("action") != action:
                continue
            if g.get("expires_at") and float(g["expires_at"]) < now:
                continue
            gscope = str(g.get("scope") or "")
            if gscope and scope and not fnmatch.fnmatch(scope, gscope):
                continue
            if gscope and not scope:
                continue  # scoped grant cannot cover an unscoped request
            return g
        return None
