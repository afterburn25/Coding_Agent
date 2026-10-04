"""Risk classification — every action earns a tier before it runs.

Tiers drive how much verification an action must clear before its
result may touch real state:

    low    — observe-only; no gate
    medium — bounded mutation in isolation; targeted tests + review
    high   — real-state mutation; full pipeline
             simulate → targeted → regression → review → canary

Classification is deterministic and explainable — each result lists
the reasons that set the tier. Nothing here *blocks* an action; it
produces the requirement set `PromotionPipeline` enforces.
"""
from __future__ import annotations

import re
from typing import Any

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISKS = {RISK_LOW, RISK_MEDIUM, RISK_HIGH}

# Ordered promotion stages; the terminal stage is promotion itself and
# is implied, not stored.
STAGES = ("simulate", "targeted_tests", "regression_tests",
          "review", "canary")

REQUIRED_STAGES = {
    RISK_LOW: (),
    RISK_MEDIUM: ("simulate", "targeted_tests", "review"),
    RISK_HIGH: STAGES,
}

_OBSERVE_RE = re.compile(
    r"^(read|list|search|scan|observe|status|probe|inspect|describe|"
    r"get|show|check|measure|find|query|lookup|preview)(?=_|\b)", re.I)

_HIGH_KINDS = {
    "installer", "migration", "runtime_config", "dependency_upgrade",
    "desktop_automation", "update", "promote", "production_change",
    "secret_access", "destructive", "delete_data", "uninstall",
    "self_modify", "credential", "registry",
}
_MEDIUM_KINDS = {
    "code_change", "worktree_change", "config_change", "sandbox_run",
    "file_write", "package_install", "test_run", "network_request",
    "shell_command", "service_restart", "model_load",
}


def classify_action(action: str, *, target: str = "",
                    context: dict | None = None) -> dict[str, Any]:
    """Deterministic tier + reasons. `action` is a short kind string
    ('installer', 'code_change', 'shell_command'…); free-form text falls
    back to conservative keyword matching."""
    ctx = dict(context or {})
    kind = str(action or "").strip().lower()
    text = f"{kind} {target}".lower()
    reasons: list[str] = []

    if kind in _HIGH_KINDS or any(
            k.replace("_", " ") in text or k in text
            for k in _HIGH_KINDS):
        reasons.append(f"high-impact action kind: {kind or 'matched'}")
        tier = RISK_HIGH
    elif kind in _MEDIUM_KINDS or any(
            k.replace("_", " ") in text or k in text
            for k in _MEDIUM_KINDS):
        reasons.append(f"mutating action kind: {kind or 'matched'}")
        tier = RISK_MEDIUM
    elif _OBSERVE_RE.match(kind):
        reasons.append("observe-only action")
        tier = RISK_LOW
    else:
        # Unknown mutation surface — conservative default, not a guess.
        reasons.append("unclassified action — defaulting to medium")
        tier = RISK_MEDIUM

    # Context can only ever *raise* the tier, never lower it.
    if ctx.get("production") or ctx.get("touches_user_data"):
        tier = RISK_HIGH
        reasons.append("touches production or user data")
    if ctx.get("network") and tier == RISK_LOW:
        tier = RISK_MEDIUM
        reasons.append("network egress — medium minimum")
    if ctx.get("isolated") and tier == RISK_HIGH and \
            not ctx.get("production") and kind not in {
                "secret_access", "destructive", "delete_data",
                "self_modify", "credential", "promote"}:
        # Staging a risky change inside an isolated environment is a
        # medium-risk experiment; applying it for real is a separate
        # high-risk candidate. Irreversible kinds never downgrade.
        tier = RISK_MEDIUM
        reasons.append("isolated environment — downgraded to medium")

    return {"risk": tier, "action": kind, "reasons": reasons,
            "required_stages": list(REQUIRED_STAGES[tier])}
