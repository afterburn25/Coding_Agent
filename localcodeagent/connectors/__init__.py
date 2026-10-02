"""Connector framework for external services (Part L).

A Connector declares capabilities, an auth strategy, permission needs,
and standard operations (health/call/reconnect). The registry tracks
state, rate limits, and audit history per connector so integrations are
uniformly permission-gated and observable — not hardcoded into core.

Subclass ``Connector`` and register an instance with
``ConnectorRegistry.register(...)``. MCP servers stay available under
``mcp.py``; this framework covers first-class named services.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..fsutil import atomic_write_text


class Connector:
    """Base connector interface."""

    name = "connector"
    capabilities: tuple[str, ...] = ()
    permission = "network.read"          # permission id governing calls
    rate_limit_per_min = 60

    def authenticate(self, vault: Any) -> bool:
        return True

    def health(self) -> dict[str, Any]:
        return {"ok": True}

    def call(self, capability: str, **params: Any) -> dict[str, Any]:
        raise NotImplementedError

    def reconnect(self) -> bool:
        return True


class ConnectorRegistry:
    def __init__(self, state_path: Path | None = None,
                 vault: Any = None,
                 permission_check: Callable[[str], str] | None = None) -> None:
        self.vault = vault
        self._perm = permission_check or (lambda perm: "allow")
        self._state_path = Path(state_path) if state_path else None
        self._lock = threading.RLock()
        self.connectors: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []

    def register(self, conn: Connector, *, enabled: bool = True) -> None:
        with self._lock:
            self.connectors[conn.name] = {
                "conn": conn, "enabled": enabled,
                "calls_window": [], "consecutive_errors": 0,
                "last_health": None, "authed": False}

    def _audit(self, name: str, event: str, detail: str = "") -> None:
        self.audit.append({"ts": time.time(), "connector": name,
                           "event": event, "detail": detail[:300]})
        self.audit = self.audit[-1000:]

    def _rate_ok(self, rec: dict) -> bool:
        conn: Connector = rec["conn"]
        now = time.time()
        rec["calls_window"] = [t for t in rec["calls_window"]
                               if now - t < 60.0]
        if len(rec["calls_window"]) >= conn.rate_limit_per_min:
            return False
        rec["calls_window"].append(now)
        return True

    def call(self, name: str, capability: str, **params: Any) -> dict[str, Any]:
        rec = self.connectors.get(name)
        if rec is None:
            return {"ok": False, "error": f"unknown connector '{name}'"}
        conn: Connector = rec["conn"]
        if not rec["enabled"]:
            return {"ok": False, "error": "connector disabled"}
        if capability not in conn.capabilities:
            return {"ok": False, "error": f"connector lacks '{capability}'"}
        if self._perm(conn.permission) != "allow":
            self._audit(name, "permission_denied", conn.permission)
            return {"ok": False, "error": f"permission denied: {conn.permission}"}
        if not self._rate_ok(rec):
            self._audit(name, "rate_limited")
            return {"ok": False, "error": "rate limit exceeded"}
        if not rec["authed"]:
            try:
                rec["authed"] = bool(conn.authenticate(self.vault))
            except Exception as exc:
                self._audit(name, "auth_error", str(exc))
                rec["authed"] = False
            if not rec["authed"]:
                return {"ok": False, "error": "authentication failed"}
        try:
            out = conn.call(capability, **params)
            rec["consecutive_errors"] = 0
            self._audit(name, "call", capability)
            return out if isinstance(out, dict) else {"ok": True, "result": out}
        except Exception as exc:
            rec["consecutive_errors"] += 1
            self._audit(name, "error", f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def health(self, name: str) -> dict[str, Any]:
        rec = self.connectors.get(name)
        if rec is None:
            return {"ok": False, "error": "unknown connector"}
        try:
            h = rec["conn"].health()
        except Exception as exc:
            h = {"ok": False, "error": str(exc)}
        rec["last_health"] = h
        if not h.get("ok") and rec["consecutive_errors"] >= 3:
            # bounded reconnect attempt
            try:
                h["reconnected"] = bool(rec["conn"].reconnect())
            except Exception:
                h["reconnected"] = False
            if h.get("reconnected"):
                rec["consecutive_errors"] = 0
        return h

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": n, "enabled": r["enabled"],
                     "capabilities": list(r["conn"].capabilities),
                     "permission": r["conn"].permission,
                     "authed": r["authed"],
                     "errors": r["consecutive_errors"],
                     "last_health": r["last_health"]}
                    for n, r in sorted(self.connectors.items())]

    def set_enabled(self, name: str, enabled: bool) -> bool:
        rec = self.connectors.get(name)
        if rec is None:
            return False
        rec["enabled"] = bool(enabled)
        self._audit(name, "enabled" if enabled else "disabled")
        return True

    def persist_audit(self) -> None:
        if not self._state_path:
            return
        import json
        atomic_write_text(self._state_path, json.dumps(
            {"audit": self.audit[-500:]}, indent=2))
