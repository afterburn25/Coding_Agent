"""Mission Planner — turns an objective + criteria into a task DAG.

The default planner is deterministic: it produces a bounded
inspect → work → verify skeleton, plus one verify node per verifiable
success criterion. Deterministic planning keeps unattended runs
predictable and unit-testable; the model is not required to plan before
any work can proceed.

Replanning appends a diagnosis → fix → verify path after a failed node —
descendants of the authorized mission only, never new top-level goals.
"""
from __future__ import annotations

import time
from typing import Any

from .task_graph import new_task


class MissionPlanner:
    """Builds the initial DAG and replan extensions for a mission."""

    def initial_plan(self, mission: dict) -> list[dict]:
        """Deterministic skeleton keyed off objective + success criteria."""
        objective = str(mission.get("objective") or "")
        scope = str(mission.get("scope") or "one_shot")
        tasks: list[dict] = []

        if scope == "maintenance":
            return self._maintenance_plan(mission)

        # Repository/workspace work should see a fresh code index before the
        # agent inspects anything — refresh it as an async job node first.
        index_node = None
        if scope in {"repository", "workspace"}:
            index_node = new_task(
                "Refresh repository index",
                "Incrementally update the code index before inspection.",
                kind="job", priority=5, verify="none", max_retries=0,
                metadata={"job": "rag_update"})

        inspect = new_task(
            "Inspect current state",
            ("Inspect the workspace relevant to this objective and report "
             "what exists and what must change. Objective: " + objective),
            kind="agent", priority=10, model_role="utility",
            verify="none", max_retries=1,
            deps=[index_node["id"]] if index_node else None,
        )
        work = new_task(
            f"Execute: {str(mission.get('title') or objective)[:100]}",
            ("Accomplish the authorized objective. Work only inside the "
             "mission's constraints. Objective: " + objective +
             self._constraints_text(mission)),
            kind="agent", deps=[inspect["id"]], priority=20,
            verify="none",
            max_retries=int((mission.get("budgets") or {}).get(
                "max_task_retries", 2)),
        )
        if index_node is not None:
            tasks.append(index_node)
        tasks += [inspect, work]

        verif_deps = [work["id"]]
        for crit in mission.get("success_criteria") or []:
            kind = str(crit.get("kind") or "")
            if kind in {"verify_passed", "all_tasks_completed"} or not kind:
                continue
            if kind in {"artifact_exists", "file_exists"}:
                node = new_task(
                    f"Check artifact: {crit.get('target', '')}",
                    f"internal:artifact_exists:{crit.get('target', '')}",
                    kind="internal", deps=verif_deps, priority=30,
                    verify="none", max_retries=0)
                tasks.append(node)
            elif kind == "metric":
                node = new_task(
                    f"Measure: {crit.get('key', 'metric')}",
                    f"Measure metric '{crit.get('key')}' and record it on the "
                    f"mission (target {crit.get('op', '<=')} {crit.get('target')}).",
                    kind="agent", deps=verif_deps, priority=30,
                    verify="none", max_retries=1)
                tasks.append(node)
        verify = new_task(
            "Verify work",
            ("Run the project's verification (tests/build as appropriate) "
             "and report pass/fail with the exact failing signal if any."),
            kind="verify", deps=verif_deps + [t["id"] for t in tasks
                                             if t["kind"] == "internal"],
            priority=40, verify="auto", max_retries=1,
        )
        tasks.append(verify)
        return tasks

    def _constraints_text(self, mission: dict) -> str:
        cons = [c for c in (mission.get("constraints") or []) if c]
        if not cons:
            return ""
        return "\nConstraints (must obey): " + "; ".join(cons[:10])

    def _maintenance_plan(self, mission: dict) -> list[dict]:
        return [new_task(
            "Run Nexus Core maintenance checks",
            "internal:maintenance",
            kind="internal", priority=50, verify="none", max_retries=1)]

    # -- replanning -------------------------------------------------------

    def replan(self, mission: dict, failed_node: dict | None,
               reason: str) -> list[dict]:
        """Append a bounded recovery path after failure — a DIFFERENT
        approach is required (plan diversity): inspect, then fix, then
        re-verify. Never just retries the same failed instruction."""
        mission["repair_loops"] = int(mission.get("repair_loops") or 0) + 1
        error = str((failed_node or {}).get("result", {}) or {})
        # The recovery path anchors on the failed node's *satisfied* deps,
        # not the failed node itself (which never completes) — otherwise the
        # graph deadlocks.
        deps = list((failed_node or {}).get("deps") or [])
        tasks: list[dict] = []

        diagnose = new_task(
            f"Diagnose failure ({mission['repair_loops']}): "
            f"{(failed_node or {}).get('title', 'task')[:60]}",
            ("A mission task failed. Diagnose the concrete cause using the "
             "error evidence and repository state; propose a DIFFERENT "
             "approach than the one that failed. Error: "
             + str(error)[:2000] + "\nFailure reason: " + str(reason)[:500]),
            kind="agent", deps=deps, priority=15,
            model_role="utility", verify="none", max_retries=1,
            created_by="replan",
        )
        fix = new_task(
            f"Recover: {(failed_node or {}).get('title', 'task')[:80]}",
            ("Apply the diagnosis to make progress on the mission objective. "
             "Objective: " + str(mission.get("objective") or "") +
             self._constraints_text(mission)),
            kind="agent", deps=[diagnose["id"]], priority=20,
            verify="none",
            max_retries=int((mission.get("budgets") or {}).get(
                "max_task_retries", 2)),
            created_by="replan",
        )
        verify = new_task(
            f"Re-verify ({mission['repair_loops']})",
            "Re-run verification for the recovered work and report pass/fail.",
            kind="verify", deps=[fix["id"]], priority=40,
            verify="auto", max_retries=1, created_by="replan",
        )
        tasks += [diagnose, fix, verify]
        mission.setdefault("history", []).append({
            "ts": time.time(), "event": "replan",
            "detail": f"+{len(tasks)} tasks after: {reason[:200]}",
        })
        return tasks

    def attach_subtask(self, mission: dict, title: str, instruction: str,
                       *, parent: dict | None = None, kind: str = "agent") -> dict:
        """Self-generated subtask — always a descendant of the authorized
        mission; the supervisor (not the model) owns top-level creation."""
        return new_task(
            title, instruction, kind=kind,
            deps=[parent["id"]] if parent else [], priority=25,
            created_by="subtask",
        )
