"""Mission Evaluator — objective verdict separate from the task executor.

The model that did the work never gets to declare its own mission a
success. The evaluator checks persisted evidence: task results,
verification runs, artifacts, and explicit success criteria.
"""
from __future__ import annotations

import time
from enum import Enum
from pathlib import Path
from typing import Any


class EvalVerdict(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    BLOCKED = "BLOCKED"
    NEEDS_REPLAN = "NEEDS_REPLAN"
    NEEDS_USER = "NEEDS_USER"


class MissionEvaluator:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    # -- criteria checks ------------------------------------------------

    def _check_criterion(self, crit: dict, mission: dict) -> dict:
        """Evaluate one success criterion against persisted evidence."""
        kind = str(crit.get("kind") or "custom")
        out = {"kind": kind, "met": False, "detail": ""}
        graph_nodes = (mission.get("graph") or {}).get("nodes", [])

        if kind == "all_tasks_completed":
            pending = [n for n in graph_nodes
                       if n.get("state") not in
                       {"completed", "skipped", "cancelled"}]
            failed = [n for n in graph_nodes if n.get("state") == "failed"]
            out["met"] = not pending and not failed and bool(graph_nodes)
            out["detail"] = (f"{sum(1 for n in graph_nodes if n.get('state') == 'completed')}"
                             f"/{len(graph_nodes)} tasks completed"
                             + (f", {len(failed)} failed" if failed else ""))

        elif kind == "verify_passed":
            verifs = mission.get("verification_history") or []
            ok = [v for v in verifs if v.get("ok")]
            out["met"] = bool(ok)
            out["detail"] = (f"{len(ok)} passing verification(s)"
                             if ok else "no passing verification recorded")

        elif kind in {"artifact_exists", "file_exists"}:
            raw = str(crit.get("target") or "")
            try:
                p = Path(raw)
                if not p.is_absolute():
                    p = self.workspace / raw
                p = p.resolve()
                p.relative_to(self.workspace.resolve())  # confine to workspace
                out["met"] = p.exists()
                out["detail"] = f"{raw}: {'exists' if out['met'] else 'missing'}"
            except (OSError, ValueError):
                out["met"] = False
                out["detail"] = f"{raw}: path outside workspace or invalid"

        elif kind == "metric":
            metrics = mission.get("metrics") or {}
            key = str(crit.get("key") or "")
            target = crit.get("target")
            actual = metrics.get(key)
            op = str(crit.get("op") or "<=")
            try:
                if op == "<=":
                    out["met"] = actual is not None and float(actual) <= float(target)
                elif op == ">=":
                    out["met"] = actual is not None and float(actual) >= float(target)
                else:
                    out["met"] = actual == target
                out["detail"] = f"{key}={actual} target {op} {target}"
            except (TypeError, ValueError):
                out["met"] = False
                out["detail"] = f"{key}: unmeasurable ({actual} vs {target})"

        elif kind == "no_failures":
            failed = [n for n in graph_nodes if n.get("state") == "failed"]
            out["met"] = not failed
            out["detail"] = f"{len(failed)} failed task(s)"

        else:  # custom — only satisfied by explicit evidence recorded
            evidence = str(crit.get("evidence") or "")
            out["met"] = bool(evidence)
            out["detail"] = evidence[:200] if evidence else \
                "custom criterion awaits recorded evidence"

        return out

    # -- verdict ----------------------------------------------------------

    def evaluate(self, mission: dict) -> dict:
        status = str(mission.get("status") or "")
        nodes = (mission.get("graph") or {}).get("nodes", [])

        if status in {"paused", "waiting_approval", "waiting_dependency",
                      "waiting_trigger"}:
            verdict = EvalVerdict.NEEDS_USER if status == "waiting_approval" \
                else EvalVerdict.INCOMPLETE
            result = {"verdict": verdict.value, "reasons": [f"mission is {status}"],
                      "criteria": [], "ts": time.time()}
            self._record(mission, result)
            return result

        failed_nodes = [n for n in nodes if n.get("state") == "failed"]
        running = [n for n in nodes if n.get("state") in
                   {"running", "verifying", "ready", "planned",
                    "waiting_dependency", "waiting_approval"}]

        criteria = [self._check_criterion(c, mission)
                    for c in (mission.get("success_criteria") or [])]
        unmet = [c for c in criteria if not c["met"]]

        reasons: list[str] = []
        if not nodes:
            verdict = EvalVerdict.NEEDS_REPLAN
            reasons.append("mission has no task graph")
        elif running:
            verdict = EvalVerdict.INCOMPLETE
            reasons.append(f"{len(running)} task(s) still open")
        elif unmet:
            verdict = EvalVerdict.NEEDS_REPLAN
            reasons += [f"unmet criterion: {c['kind']} — {c['detail']}"
                        for c in unmet[:5]]
        elif failed_nodes:
            verdict = EvalVerdict.NEEDS_REPLAN
            reasons.append(f"{len(failed_nodes)} task(s) failed")
        else:
            verdict = EvalVerdict.COMPLETE
            reasons.append("all success criteria satisfied")

        result = {"verdict": verdict.value, "reasons": reasons,
                  "criteria": criteria, "ts": time.time()}
        self._record(mission, result)
        return result

    def _record(self, mission: dict, result: dict) -> None:
        mission.setdefault("evaluator_history", []).append({
            "ts": result["ts"], "verdict": result["verdict"],
            "reasons": result["reasons"][:10],
        })
        mission["evaluator_history"] = mission["evaluator_history"][-50:]
