"""Coding-agent pipeline — canonical software-development mission plan.

Builds the Understand → Plan → Implement → Dependencies → Build →
Test → Review → Launch/Verify → Document → Commit task DAG as real
mission nodes. Every evidence stage runs a real tool through the
registry (permissions enforced); failures flow through the
supervisor's bounded retries and recovery playbook — there are no
narrated stages.

Stage states are derivable for UI via ``stage_view(mission)`` —
ordered readable rows the Command Center / missions UI can render
without inventing progress.
"""

from __future__ import annotations

from typing import Any

from .autonomy.task_graph import new_task

STAGES = [
    "understand", "plan", "implement", "dependencies", "build",
    "test", "review", "launch_verify", "document", "commit",
]

_STAGE_TITLES = {
    "understand": "Understand project",
    "plan": "Plan implementation",
    "implement": "Generate code",
    "dependencies": "Resolve dependencies",
    "build": "Build project",
    "test": "Run tests",
    "review": "Review changes",
    "launch_verify": "Launch + verify app",
    "document": "Document project",
    "commit": "Commit changes",
}


def _tool_job(title: str, stage: str, tool: str, tool_args: dict,
              *, deps: list[str], priority: int, retries: int) -> dict:
    return new_task(
        title,
        f"[{stage}] run {tool}({tool_args}) — a real tool invocation "
        "through the permission-enforced registry.",
        kind="job", deps=deps, priority=priority, verify="none",
        max_retries=retries,
        metadata={"job": "tool", "tool": tool, "tool_args": tool_args,
                  "pipeline_stage": stage})


def build_coding_tasks(mission: dict) -> list[dict]:
    """Serial coding DAG for a mission with ``pipeline == "coding"``.

    ``mission["pipeline_options"]`` carries:
      root        workspace root (defaults to mission workspace)
      server_cmd  launch command — adds a serve+verify stage
      commit      whether to add the git-commit stage
    """
    opts = dict(mission.get("pipeline_options") or {})
    root = str(opts.get("root") or mission.get("workspace") or "")
    objective = str(mission.get("objective") or "")
    retries = int((mission.get("budgets") or {}).get(
        "max_task_retries", 2))
    tasks: list[dict] = []
    prev: list[str] = []

    understand = _tool_job(
        "Understand project", "understand",
        "workspace_inspect", {"path": root},
        deps=prev, priority=10, retries=0)
    tasks.append(understand)
    prev = [understand["id"]]

    # Pre-flight: surface uncommitted user work before agent edits so
    # nothing silently overwrites it.
    preflight = _tool_job(
        "Check for uncommitted user changes", "understand",
        "git_user_changes", {"path": root},
        deps=prev, priority=11, retries=0)
    tasks.append(preflight)
    prev = [preflight["id"]]

    plan = new_task(
        "Plan implementation",
        ("Produce a concrete implementation plan for the workspace at "
         f"{root}: architecture choice, files to create/modify, "
         "dependency changes, and verification approach. Do not write "
         "project files yet — output the plan.\nObjective: " + objective),
        kind="agent", deps=prev, priority=20, model_role="utility",
        verify="none", max_retries=1,
        metadata={"pipeline_stage": "plan", "root": root})
    tasks.append(plan)
    prev = [plan["id"]]

    implement = new_task(
        f"Implement: {str(mission.get('title') or objective)[:90]}",
        ("Implement the plan inside workspace " + root + " using the "
         "filesystem/shell tools. Work only inside the workspace; "
         "preserve any existing user changes. Objective: " + objective),
        kind="agent", deps=prev, priority=25, verify="none",
        max_retries=retries,
        metadata={"pipeline_stage": "implement", "root": root})
    tasks.append(implement)
    prev = [implement["id"]]

    for stage, tool, extra in (
            ("dependencies", "project_setup", {}),
            ("build", "build_project", {}),
            ("test", "run_tests", {})):
        node = _tool_job(
            _STAGE_TITLES[stage], stage, tool,
            {"path": root, **extra},
            deps=prev, priority=30, retries=retries)
        tasks.append(node)
        prev = [node["id"]]

    review = new_task(
        "Review changes",
        ("Independently review the implemented changes in " + root +
         " against the objective. Check correctness, obvious defects, "
         "and scope creep. Report findings — do not rewrite code.\n"
         "Objective: " + objective),
        kind="review", deps=prev, priority=40, verify="none",
        max_retries=0,
        metadata={"pipeline_stage": "review", "root": root,
                  "worker_role": "reviewer"})
    tasks.append(review)
    prev = [review["id"]]

    server_cmd = str(opts.get("server_cmd") or "").strip()
    if server_cmd:
        serve = new_task(
            "Launch + verify app",
            "[launch_verify] start the dev server and verify it answers "
            "over HTTP — the app only counts as launched when a real "
            "probe succeeds.",
            kind="job", deps=prev, priority=45, verify="none",
            max_retries=1,
            metadata={"job": "serve_check", "command": server_cmd,
                      "path": root, "pipeline_stage": "launch_verify",
                      "timeout": 60})
        tasks.append(serve)
        prev = [serve["id"]]

    document = new_task(
        "Document project",
        ("Write or update project documentation in " + root +
         " (README/setup/run/test instructions that match what actually "
         "exists). Keep it accurate — document real behavior only."),
        kind="agent", deps=prev, priority=48, verify="none",
        max_retries=1,
        metadata={"pipeline_stage": "document", "root": root})
    tasks.append(document)
    prev = [document["id"]]

    if opts.get("commit"):
        message = str(opts.get("commit_message") or
                      f"Nexus: {str(mission.get('title') or objective)[:70]}")
        commit = _tool_job(
            "Commit changes", "commit",
            "git_commit",
            {"path": root, "message": message,
             "add_all": bool(opts.get("commit_all", True))},
            deps=prev, priority=50, retries=0)
        tasks.append(commit)

    return tasks


def stage_view(mission: dict) -> list[dict[str, Any]]:
    """Ordered readable stage rows for a coding-pipeline mission —
    only stages that exist in the real DAG, with their live states."""
    nodes = (mission.get("graph") or {}).get("nodes") or []
    rows = []
    order = 0
    for node in nodes:
        stage = str((node.get("metadata") or {}).get(
            "pipeline_stage") or "")
        if not stage:
            continue
        order += 1
        rows.append({
            "order": order,
            "stage": stage,
            "title": node.get("title"),
            "state": node.get("state"),
            "kind": node.get("kind"),
            "retries": node.get("retries", 0),
            "started_at": node.get("started_at"),
            "completed_at": node.get("completed_at"),
            "result_ok": (node.get("result") or {}).get("ok"),
            "output": str((node.get("result") or {}).get(
                "output") or "")[:600],
        })
    return rows
