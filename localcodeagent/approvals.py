"""Chat permission cards — the presentation + decision layer for
interactive in-chat authorization.

One permission decision surface serves chat and the Tasks panel: the
backend builds a card view from the durable pending_approval record, the
browser renders exactly the decisions the backend authorizes, and the
decision resolves the SAME parked task — session grants, persistent
levels, and audits all go through PermissionManager, so Settings stays
the single source of truth.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from .permissions import AUTONOMY_NEVER_AUTO, permission_info

# Explicit decision vocabulary. `approved: bool` stays accepted on the
# legacy resume endpoint but every new surface carries one of these.
DECISION_SESSION = "session"   # approve now + grant for this process
DECISION_DENY = "deny"         # refuse; do not execute
DECISION_ALWAYS = "always"     # approve now + persist level=allow
DECISION_ONCE = "once"         # approve this call only (Tasks panel)

DECISIONS = (DECISION_SESSION, DECISION_DENY, DECISION_ALWAYS,
             DECISION_ONCE)

DECISION_LABELS = {
    DECISION_SESSION: "Allow this session",
    DECISION_DENY: "Deny",
    DECISION_ALWAYS: "Always allow",
    DECISION_ONCE: "Approve",
}

STALE_MESSAGE = "This authorization request is no longer active."
DISABLED_MESSAGE = "This permission is disabled in Settings."


class ApprovalError(Exception):
    """Decision-path rejection. `status` is the HTTP code the API maps
    it to; `stale` marks the no-longer-active class (double click,
    duplicate event, restart, cancel, already-resumed)."""

    def __init__(self, message: str, *, status: int = 409,
                 stale: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.stale = stale


def new_approval_id() -> str:
    return f"ap-{uuid.uuid4().hex[:12]}"


def stamp_pending(task_id: str, pending: dict[str, Any]) -> dict[str, Any]:
    """Identity + timestamps every parked approval record gets. Called by
    each park site before pending_approval is persisted on the task."""
    pending.setdefault("id", new_approval_id())
    pending.setdefault("task_id", task_id)
    pending.setdefault("created_at", time.time())
    return pending


def decision_options(permission: str, level: str, *,
                     creator_ok: bool = True) -> list[str]:
    """Decisions the backend will honor for this permission, in display
    order. The hard gates live here so no surface can offer more:

    - level `deny`     → no decisions at all; the user cannot override a
                         policy denial from chat.
    - AUTONOMY_NEVER_AUTO / creator-gated
                       → never `always` — these must keep asking a human
                         (or an unlocked creator session) every time.
    """
    level = str(level or "ask").strip().lower()
    if level == "deny":
        return []
    options = [DECISION_SESSION, DECISION_DENY]
    never_persist = permission in AUTONOMY_NEVER_AUTO or level == "creator"
    if level == "creator" and not creator_ok:
        # An approval the user is not currently entitled to give — the
        # card still renders but every decision stays disabled until the
        # creator session unlocks.
        return []
    if not never_persist:
        options.append(DECISION_ALWAYS)
    return options


def _action_summary(pending: dict[str, Any]) -> str:
    """Human-readable 'Nexus wants to X' — the local-action plan carries
    a curated action_text; tool calls fall back to the tool name plus a
    short sanitized argument hint."""
    plan = pending.get("plan") or {}
    for key in ("detail",):
        text = str(pending.get(key) or "").strip()
        if text:
            return text[:240]
    for key in ("action_text", "display"):
        text = str(plan.get(key) or "").strip()
        if text:
            return text[:240]
    name = str(pending.get("name") or "action").strip()
    args = pending.get("arguments") or {}
    hint = ""
    for k in ("path", "target", "command", "url", "query", "src", "dst"):
        v = str(args.get(k) or "").strip()
        if v:
            hint = f" — {v[:120]}"
            break
    return f"Run {name}{hint}"


def approval_card(pending: dict[str, Any], task_id: str,
                  manager) -> dict[str, Any]:
    """Backend-authoritative card payload. `manager` is the live
    PermissionManager; the browser renders exactly what this returns."""
    key = str(pending.get("permission") or "")
    info = permission_info(key)
    level = manager.level(key) if manager is not None else "ask"
    creator_ok = bool(manager.creator_ok()) if manager is not None else True
    options = decision_options(key, level, creator_ok=creator_ok)
    plan = pending.get("plan") or {}
    args = {
        str(k): str(v)[:200]
        for k, v in list((pending.get("arguments") or {}).items())[:8]
    }
    card = {
        "id": str(pending.get("id") or ""),
        "task_id": str(pending.get("task_id") or task_id or ""),
        "permission": key,
        "level": level,
        "label": info["label"],
        "category": info["category"],
        "title": f"{info['category']} · {info['label']}",
        "action": _action_summary(pending),
        "detail": str(pending.get("detail") or "")[:300],
        "target": str(plan.get("resolved", {}).get("target") or ""),
        "outside_workspace": bool(plan.get("outside_root")),
        "scope": info["scope"],
        "risk": info["risk"],
        "blurb": info["blurb"],
        "tool": str(pending.get("name") or ""),
        "arguments": args,
        "decisions": options,
        "decision_labels": {d: DECISION_LABELS[d] for d in options},
        "creator_required": level == "creator" and not creator_ok,
        "disabled_reason": DISABLED_MESSAGE if not options else "",
        "created_at": float(pending.get("created_at") or 0.0),
        "expires_at": None,
        "status": "pending",
    }
    return card


def resolution_row(card: dict[str, Any], decision: str,
                   status: str = "resolved") -> dict[str, Any]:
    """Durable resolved-state snapshot stored on the task row — reload
    and restart render the historical card instead of a live control."""
    return {
        "id": card.get("id") or "",
        "task_id": card.get("task_id") or "",
        "permission": card.get("permission") or "",
        "title": card.get("title") or "",
        "action": card.get("action") or "",
        "decision": decision,
        "decision_label": DECISION_LABELS.get(decision, decision),
        "status": status,
        "resolved_at": time.time(),
    }
