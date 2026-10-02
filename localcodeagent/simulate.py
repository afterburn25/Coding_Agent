"""Simulation / dry-run mode (Part N).

``simulate_plan`` renders a mission/task plan into an explicit step list —
tools that would be called, files likely touched, permissions required,
estimated resources, and failure points — without executing anything.

Simulation NEVER runs a tool: it consults the tool registry for schemas
and the policy engine for permission verdicts, and reports what *would*
happen. Results are labeled ``"simulated": true`` everywhere.
"""
from __future__ import annotations

import time
from typing import Any

# Rough per-tool cost model — overridden by measured data when the twin
# has observed the tool before.
_TOOL_COSTS = {
    "shell.execute": {"risk": "high", "duration_s": 30},
    "filesystem.write": {"risk": "medium", "duration_s": 2},
    "git.write": {"risk": "high", "duration_s": 5},
    "github.write": {"risk": "high", "duration_s": 10},
    "packages.install": {"risk": "high", "duration_s": 120},
    "browser.control": {"risk": "medium", "duration_s": 20},
    "image.generate": {"risk": "medium", "duration_s": 60},
    "network.read": {"risk": "low", "duration_s": 8},
    "filesystem.read": {"risk": "low", "duration_s": 1},
}


def simulate_plan(plan: dict[str, Any], *, policy: Any = None,
                  permission_map: dict[str, str] | None = None,
                  twin: Any = None) -> dict[str, Any]:
    """Dry-run a plan dict with 'steps': [{title, tool, action, files}].

    A mission-DAG dict with 'nodes' (id/title/kind/deps/metadata) is
    translated into steps first, so dry-runs cover real mission graphs.

    Returns the plan's expected trajectory — never executes.
    """
    plan = _nodes_to_steps(plan)
    perm_map = permission_map or {}
    steps = []
    required_perms: set[str] = set()
    total_s = 0.0
    risks: list[str] = []

    for i, step in enumerate(plan.get("steps") or []):
        tool = str(step.get("tool") or "")
        action = str(step.get("action") or "")
        files = [str(f) for f in (step.get("files") or [])]
        perm = perm_map.get(tool, tool)  # tool names ARE permission/action ids
        verdict = "allow"
        if perm:
            required_perms.add(perm)
            if policy is not None:
                try:
                    verdict = policy.check(_action_for(perm))
                except Exception:
                    verdict = "ask"
        cost = _TOOL_COSTS.get(perm, {"risk": "low", "duration_s": 5})
        total_s += float(cost["duration_s"])
        if cost["risk"] == "high":
            risks.append(f"step {i + 1}: {tool or action} is high-impact")
        if verdict == "ask":
            risks.append(f"step {i + 1}: '{perm}' will pause for approval")
        elif verdict == "deny":
            risks.append(f"step {i + 1}: '{perm}' would be denied")
        steps.append({
            "index": i,
            "title": str(step.get("title") or f"step {i + 1}"),
            "tool": tool,
            "action": action,
            "files_affected": files,
            "permission": perm,
            "verdict": verdict,
            "estimated_s": cost["duration_s"],
            "risk": cost["risk"],
            "simulated": True,
        })

    resource_note = ""
    if twin is not None:
        try:
            hw = twin.hardware()
            resource_note = (f"host: {hw.get('ram_free_gb', 0):.0f}GB RAM free, "
                             f"{sum(g.get('free_vram_mb', 0) for g in hw.get('gpus', [])) / 1024:.0f}GB VRAM free")
        except Exception:
            pass

    return {
        "simulated": True,
        "step_count": len(steps),
        "steps": steps,
        "permissions_required": sorted(required_perms),
        "approvals_expected": sum(1 for s in steps if s["verdict"] == "ask"),
        "denied": [s["title"] for s in steps if s["verdict"] == "deny"],
        "estimated_duration_s": total_s,
        "failure_points": risks,
        "resource_note": resource_note,
        "ts": time.time(),
    }


# Mission-DAG node kind → representative tool/permission for the dry run.
_NODE_KIND_PERMS = {
    "agent": "shell.execute",
    "verify": "shell.execute",
    "research": "network.read",
    "internal": "filesystem.read",
    "wait": "",
}
_JOB_OP_PERMS = {
    "sandbox": "shell.execute",
    "backup": "filesystem.write",
    "rag_update": "filesystem.read",
    "image": "image.generate",
    "model_install": "packages.install",
}


def _nodes_to_steps(plan: dict[str, Any]) -> dict[str, Any]:
    """Translate mission-DAG 'nodes' into generic simulation steps."""
    nodes = plan.get("nodes") or []
    if not nodes or plan.get("steps"):
        return plan
    steps = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        kind = str(n.get("kind") or "agent")
        meta = dict(n.get("metadata") or {})
        if kind == "job":
            tool = _JOB_OP_PERMS.get(str(meta.get("job") or ""), "")
            action = f"job:{meta.get('job') or '?'}"
        else:
            tool = _NODE_KIND_PERMS.get(kind, "")
            action = f"node:{kind}"
        steps.append({
            "title": n.get("title") or n.get("id") or "node",
            "tool": tool,
            "action": action,
            "files": meta.get("files") or [],
        })
    out = dict(plan)
    out["steps"] = steps
    return out


def _action_for(perm: str) -> str:
    """Map a permission id to an autonomy action class when possible."""
    return {
        "filesystem.write": "write_workspace",
        "filesystem.read": "read_files",
        "shell.execute": "run_shell",
        "git.write": "git_commit",
        "github.write": "git_push",
        "packages.install": "packages",
        "browser.control": "browser",
        "network.read": "research",
    }.get(perm, perm)
