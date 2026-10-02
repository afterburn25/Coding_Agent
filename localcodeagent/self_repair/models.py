"""Durable RepairIncident schema.

An incident is the persistent record of one detected failure and every
attempt to repair it. It survives restarts; `state` is a guarded
lifecycle so a crash mid-repair resumes instead of repeating finished
work.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

REPAIR_STATES = {
    "detected",        # recorded, diagnostics not yet collected
    "collecting",      # gathering evidence snapshot
    "localizing",      # narrowing to suspect files/functions
    "diagnosing",      # ranking root-cause hypotheses
    "planning",        # building bounded repair plan
    "patching",        # worktree candidate being built
    "testing",         # targeted/regression verification running
    "reviewing",       # independent review of the candidate
    "canary",          # candidate instance validation
    "promoting",       # applying fix to stable
    "resolved",        # verified fixed
    "rolled_back",     # promoted repair regressed; stable restored
    "needs_human",     # evidence/confidence/permission insufficient
    "abandoned",       # budgets exhausted or superseded
}
TERMINAL_REPAIR_STATES = {"resolved", "rolled_back", "needs_human",
                          "abandoned"}
OPEN_REPAIR_STATES = REPAIR_STATES - TERMINAL_REPAIR_STATES

SEVERITIES = {"critical", "high", "normal", "low"}

# Guarded lifecycle — prevents impossible jumps (e.g. detected->promoted)
# and makes the audit trail trustworthy.
VALID_TRANSITIONS = {
    "detected": {"collecting", "localizing", "diagnosing", "needs_human",
                 "abandoned"},
    "collecting": {"localizing", "diagnosing", "detected", "needs_human",
                   "abandoned"},
    "localizing": {"diagnosing", "planning", "needs_human", "abandoned"},
    "diagnosing": {"planning", "localizing", "needs_human", "abandoned"},
    "planning": {"patching", "testing", "diagnosing", "needs_human",
                 "abandoned"},
    "patching": {"testing", "planning", "needs_human", "abandoned"},
    "testing": {"reviewing", "patching", "planning", "promoting",
                "needs_human", "abandoned"},
    "reviewing": {"canary", "promoting", "patching", "needs_human",
                  "abandoned"},
    "canary": {"promoting", "patching", "needs_human", "abandoned"},
    "promoting": {"resolved", "rolled_back", "needs_human"},
    "resolved": {"detected", "rolled_back"},  # regression → reopen or
                                             # restore last-known-good
    "rolled_back": {"detected", "planning", "needs_human"},
    "needs_human": {"detected", "abandoned"},   # operator may retry
    "abandoned": {"detected"},
}


class IncidentBudgets:
    """Per-incident anti-loop bounds. Persisted on the incident."""

    DEFAULTS = {"max_diagnosis_attempts": 3, "max_patch_attempts": 3,
                "max_same_patch_failures": 2, "max_replans": 2,
                "max_runtime_minutes": 45.0, "max_model_cost": 0.0}

    def __init__(self, **kw):
        cfg = dict(self.DEFAULTS)
        for k, v in kw.items():
            if k in cfg:
                try:
                    cfg[k] = float(v) if k == "max_runtime_minutes" else int(v)
                except (TypeError, ValueError):
                    pass
        self.values = cfg

    def as_dict(self) -> dict:
        return dict(self.values)


def new_incident(*, source: str, subsystem: str, error_class: str,
                 error_message: str, signature: str, severity: str = "normal",
                 stack_trace: str = "", mission_id: str = "",
                 task_id: str = "", request_id: str = "",
                 model_id: str = "", tool_id: str = "",
                 process_id: int = 0, exit_code: int | None = None,
                 budgets: dict | None = None) -> dict[str, Any]:
    now = time.time()
    return {
        "id": f"ri-{uuid.uuid4().hex[:10]}",
        "created_at": now,
        "updated_at": now,
        # --- what happened ------------------------------------------------
        "source": str(source)[:80],             # exception|crash|test|ci|…
        "subsystem": str(subsystem)[:80],
        "severity": severity if severity in SEVERITIES else "normal",
        "error_class": str(error_class)[:120],
        "error_message": str(error_message)[:2000],
        "signature": str(signature)[:200],
        "stack_trace": str(stack_trace)[:12000],
        "process_id": int(process_id or 0),
        "exit_code": exit_code,
        "mission_id": str(mission_id or "")[:60],
        "task_id": str(task_id or "")[:60],
        "request_id": str(request_id or "")[:60],
        "model_id": str(model_id or "")[:80],
        "tool_id": str(tool_id or "")[:80],
        # --- evidence captured at detect time ------------------------------
        "affected_files": [],
        "recent_commits": [],
        "hardware_snapshot": {},
        "runtime_snapshot": {},
        "logs": "",                   # bounded excerpt, redacted
        "reproduction": {"reproducible": None, "command": "", "output": ""},
        # --- analysis ------------------------------------------------------
        "suspects": [],               # [{path, function, line, confidence}]
        "hypotheses": [],             # [{kind, detail, confidence, evidence}]
        "research": [],               # external evidence gathered when
                                      # diagnosis is weak (bounded, ≤3)
        "repair_kind": "",            # operational|code|""
        "plan": [],                   # bounded step list
        # --- execution -----------------------------------------------------
        "state": "detected",
        "confidence": 0.0,            # calibrated root-cause confidence
        "worktree": "",               # repair/<id> worktree path
        "patch_mission": "",          # async repair mission generating the patch
        "patch_files": [],            # files changed by the candidate
        "regression_test": "",        # added test path
        "verification": {},           # {targeted, regression, candidate…}
        "review": {},
        "promotion": {},
        "rollback": {},
        "repair_procedure": [],       # steps actually taken (for memory)
        "interrupted_operation": {},  # what to resume after success
        # --- bookkeeping -----------------------------------------------------
        "occurrences": 1,
        "first_seen": now,
        "last_seen": now,
        "attempts": {"diagnosis": 0, "patch": 0, "same_patch_failures": 0,
                     "replans": 0},
        "budgets": IncidentBudgets(**(budgets or {})).as_dict(),
        "history": [{"ts": now, "event": "detected",
                     "detail": f"{subsystem}: {error_class}"}],
        "needs_human_reason": "",
    }


def transition(incident: dict, new_state: str, *, detail: str = "") -> bool:
    """Guarded state change; returns False for illegal transitions."""
    if new_state not in REPAIR_STATES:
        return False
    cur = str(incident.get("state"))
    if cur != new_state and new_state not in VALID_TRANSITIONS.get(cur, set()):
        return False
    incident["state"] = new_state
    incident["updated_at"] = time.time()
    if cur != new_state:
        hist = incident.setdefault("history", [])
        hist.append({"ts": incident["updated_at"], "event": new_state,
                     "detail": str(detail)[:300]})
        del hist[:-80]
    return True


def budget_exceeded(incident: dict) -> str:
    """Return the exceeded budget name, or ''."""
    b = incident.get("budgets") or IncidentBudgets.DEFAULTS
    a = incident.get("attempts") or {}
    for key, attempt in (("max_diagnosis_attempts", "diagnosis"),
                         ("max_patch_attempts", "patch"),
                         ("max_same_patch_failures", "same_patch_failures"),
                         ("max_replans", "replans")):
        if int(a.get(attempt) or 0) >= int(b.get(key) or 0):
            return key
    if b.get("max_runtime_minutes"):
        mins = (time.time() - float(incident.get("created_at") or 0)) / 60.0
        if mins > float(b["max_runtime_minutes"]):
            return "max_runtime_minutes"
    return ""
