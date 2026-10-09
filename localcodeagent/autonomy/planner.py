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

import re
import time
from typing import Any

from .task_graph import new_task

# Bounded domain vocabulary for deterministic decomposition. When an
# objective mentions two or more of these areas the planner fans out into
# parallel scoped lanes instead of one serial inspect→work chain. Each
# lane carries scope globs so downstream worktree isolation knows which
# parts of the tree the worker owns.
_DOMAIN_LANES: tuple[tuple[re.Pattern, str, list[str], str], ...] = tuple(
    (re.compile(p, re.I), t, list(g), r) for p, t, g, r in (
        (r"\b(backend|server|apis?|runtime|orchestrat\w*|models?\s+routing|router)\b",
         "Backend / runtime", ["localcodeagent/"], "coding"),
        (r"\b(frontend|ui|web|css|page|layout|interface)\b",
         "Frontend / UI", ["web/"], "coding"),
        (r"\b(installer|packag\w*|inno|setup|uninstall|desktop host|\.net|csharp|c#)\b",
         "Installer / packaging", ["installer/", "desktop/", "packaging/"], "coding"),
        (r"\b(tests?|coverage|regressions?|test suite)\b",
         "Tests", ["tests/"], "build_test"),
        (r"\b(docs?|documentation|readme|handbook|guide)\b",
         "Documentation", ["docs/", "*.md"], "coding"),
        (r"\b(security|permission|secrets?|vulnerabilit\w*|auth)\b",
         "Security", [], "reviewer"),
        (r"\b(performance|latency|throughput|benchmark|startup time)\b",
         "Performance", [], "diagnostics"),
        (r"\b(voice|tts|speech|kokoro|vocali[sz]ation)\b",
         "Voice", ["localcodeagent/voice/", "web/voice*"], "coding"),
        (r"\b(image|comfyui|diffusion|avatar|portrait)\b",
         "Image / ComfyUI", ["localcodeagent/image/", "web/image*"], "coding"),
        (r"\b(memory|answer memory|knowledge|recall)\b",
         "Memory", ["localcodeagent/answer_memory/", "localcodeagent/knowledge/"],
         "coding"),
    ))

MAX_LANES = 6


def _detect_domains(objective: str) -> list[dict]:
    """Match the objective against the bounded domain vocabulary."""
    lanes: list[dict] = []
    seen: set[str] = set()
    for pattern, title, globs, role in _DOMAIN_LANES:
        if title in seen:
            continue
        if pattern.search(objective):
            seen.add(title)
            lanes.append({"title": title, "scope": globs, "role": role})
    return lanes[:MAX_LANES]


def _explicit_lanes(mission: dict) -> list[dict]:
    """A caller (API, LLM planner assist, or the user) may attach explicit
    lanes — they win over keyword decomposition. Each lane needs at least
    a title or instruction. Lanes may carry ``depends`` (titles of lanes
    that must integrate first), ``priority`` (p0..p3), and ``acceptance``
    strings — the workstream contract."""
    lanes = []
    for raw in mission.get("decomposition") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("instruction") or "")[:90]
        if not title:
            continue
        lanes.append({
            "title": title,
            "instruction": str(raw.get("instruction") or "")[:2000],
            "scope": [str(s)[:200] for s in (raw.get("scope") or [])][:12],
            "role": str(raw.get("role") or "coding"),
            "depends": [str(d)[:90] for d in (raw.get("depends") or [])][:8],
            "priority": str(raw.get("priority") or "p2").lower()[:4],
            "acceptance": [str(a)[:300] for a in
                           (raw.get("acceptance") or [])][:12],
        })
    return lanes[:MAX_LANES]


def derive_acceptance_criteria(mission: dict) -> list[str]:
    """Turn the raw request into explicit completion criteria BEFORE
    implementation starts (§2). Deterministic extraction — criteria
    come from what the user actually asked for, never model prose:
      • every success_criteria row becomes a criterion sentence
      • imperative fragments of the objective become criteria
      • constraints are always criteria
      • tests-green is implied when the objective says test/fix
    Existing acceptance_criteria win — a caller-authored contract is
    never overwritten."""
    existing = [str(c).strip() for c in
                (mission.get("acceptance_criteria") or []) if str(c).strip()]
    if existing:
        return existing[:16]
    out: list[str] = []
    for crit in mission.get("success_criteria") or []:
        kind = str(crit.get("kind") or "")
        target = str(crit.get("target") or "")
        if kind in {"artifact_exists", "file_exists",
                    "artifact_verified"} and target:
            out.append(f"Deliverable exists and verifies: {target}")
        elif kind == "verify_passed":
            out.append("Project verification passes")
        elif kind == "all_tasks_completed":
            out.append("All planned work completes")
        elif kind == "metric":
            out.append(f"{crit.get('key', 'metric')} "
                       f"{crit.get('op', '<=')} {crit.get('target')}")
        elif kind == "custom" and target:
            out.append(target)
    # "…, write the tests, fix regressions, build the installer, and
    # give me the artifact" — each imperative is a criterion.
    obj = str(mission.get("objective") or "")
    frag_verbs = re.compile(
        r"\b(?:write|add|update|migrate|fix|build|test|verify|create|"
        r"implement|refactor|deliver|produce|document|deploy)\b", re.I)
    for frag in re.split(r"[,;]|\band\b", obj):
        frag = frag.strip(" .")
        if frag and frag_verbs.search(frag) and len(frag) >= 12:
            out.append("Requested: " + frag[:160])
    for c in mission.get("constraints") or []:
        c = str(c).strip()
        if c:
            out.append("Constraint: " + c[:160])
    if re.search(r"\btest", obj, re.I) and not any(
            "test" in c.lower() for c in out):
        out.append("Tests exist and pass for the changed area")
    # Dedupe preserving order.
    seen: set[str] = set()
    deduped = []
    for c in out:
        key = c.lower()[:80]
        if key not in seen:
            seen.add(key)
            deduped.append(c)
    return deduped[:16]


class MissionPlanner:
    """Builds the initial DAG and replan extensions for a mission."""

    def initial_plan(self, mission: dict) -> list[dict]:
        """Deterministic skeleton keyed off objective + success criteria."""
        objective = str(mission.get("objective") or "")
        scope = str(mission.get("scope") or "one_shot")
        tasks: list[dict] = []

        # §2 — acceptance criteria exist before implementation starts.
        # Written as a side effect on the mission row (the caller's
        # mutate() persists it); never overwrites an authored contract.
        if not mission.get("acceptance_criteria"):
            mission["acceptance_criteria"] = derive_acceptance_criteria(
                mission)

        if scope == "maintenance":
            return self._record_plan(mission, self._maintenance_plan(mission),
                                     "maintenance")

        if objective.strip().startswith("internal:"):
            # Fully-internal mission — the supervisor's built-in runner
            # owns the instruction (heartbeat checks, maintenance).
            return self._record_plan(
                mission,
                [new_task("Run internal check", objective.strip(),
                          kind="internal", priority=50,
                          verify="none", max_retries=1)],
                "internal")

        if str(mission.get("pipeline") or "") == "coding":
            from ..coding_pipeline import build_coding_tasks
            tasks = build_coding_tasks(mission)
            return self._record_plan(mission, tasks, "coding pipeline")

        # Multi-domain decomposition — explicit lanes first, then the
        # bounded keyword vocabulary. A single-domain objective keeps the
        # serial inspect→work→verify skeleton.
        lanes = _explicit_lanes(mission) or (
            _detect_domains(objective)
            if scope in {"repository", "workspace", "one_shot"} else [])
        if len(lanes) >= 2:
            return self._record_plan(
                mission, self._decomposed_plan(mission, lanes),
                f"decomposed into {len(lanes)} lanes")

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
             self._deliverables_text(mission) +
             self._constraints_text(mission) +
             self._project_context(mission)),
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
        return self._record_plan(mission, tasks, "serial skeleton")

    # -- multi-lane decomposition ---------------------------------------

    def _decomposed_plan(self, mission: dict, lanes: list[dict]) -> list[dict]:
        """Fan a multi-domain objective into parallel scoped lanes that
        converge on Integrator → Reviewer → Verify.

            lane A ─┐
            lane B ─┼→ integrate → review → verify
            lane C ─┘

        Lanes are admitted independently by the worker manager — the
        hardware decides how many actually run at once; the rest queue on
        the DAG and drain automatically.
        """
        objective = str(mission.get("objective") or "")
        scope = str(mission.get("scope") or "one_shot")
        tasks: list[dict] = []

        index_node = None
        if scope in {"repository", "workspace"}:
            index_node = new_task(
                "Refresh repository index",
                "Incrementally update the code index before inspection.",
                kind="job", priority=5, verify="none", max_retries=0,
                metadata={"job": "rag_update"})
            tasks.append(index_node)
        index_dep = [index_node["id"]] if index_node else []

        lane_ids: list[str] = []
        retries = int((mission.get("budgets") or {}).get(
            "max_task_retries", 2))
        # Durable workstream records — Mission → Workstream → Task is a
        # real hierarchy, not prompt prose. Nodes carry the workstream id
        # so rollups, ownership, and steering can find them after restart.
        ws_ids: dict[str, str] = {}   # lane title → workstream id
        import uuid as _uuid
        lane_ws: list[dict] = []
        for i, lane in enumerate(lanes):
            ws = {
                "id": f"ws-{_uuid.uuid4().hex[:10]}",
                "title": str(lane["title"]),
                "objective": str(lane.get("instruction") or
                                 lane["title"])[:2000],
                "status": "planned",
                "priority": lane.get("priority") or "p2",
                "role": str(lane.get("role") or "coding"),
                "scope": list(lane.get("scope") or []),
                "depends_on": list(lane.get("depends") or []),
                "node_ids": [],
                "worktree": None,
                "acceptance": list(lane.get("acceptance") or []),
                "blocker": "",
                "result": "",
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            lane_ws.append(ws)
            ws_ids[lane["title"]] = ws["id"]
        mission.setdefault("workstreams", []).extend(lane_ws)
        mission["workstreams"] = mission["workstreams"][-40:]
        # Workstream-level dependencies: a lane whose "depends" names
        # another lane's title waits on that lane's final node.
        lane_dep_ids: dict[str, list[str]] = {}
        for i, lane in enumerate(lanes):
            globs = " ".join(lane.get("scope") or [])
            ws_id = ws_ids[lane["title"]]
            # Priority band: p0 → 10, p1 → 16, p2 → 20, p3 → 26 + lane idx
            band = {"p0": 10, "p1": 16, "p2": 20,
                    "p3": 26}.get(lane.get("priority"), 20)
            dep_node_ids = [dep
                            for dep_title in (lane.get("depends") or [])
                            for dep in lane_dep_ids.get(dep_title, [])]
            node = new_task(
                f"[{lane['title']}] {str(mission.get('title') or objective)[:80]}",
                ("Work the scoped lane of this mission. Stay inside your "
                 "scope; leave shared files (VERSION, CHANGELOG, lockfiles, "
                 "schemas, central routers) for the Integrator unless your "
                 "scope explicitly includes them.\n"
                 f"Lane: {lane['title']}\nScope: {globs or 'unscoped'}\n"
                 + str(lane.get("instruction") or "")
                 + "\nObjective: " + objective
                 + self._constraints_text(mission)
                 + self._project_context(mission)
                 + "\nReturn: summary, files changed, tests run + results, "
                   "unresolved issues, risks."),
                kind="agent", deps=(dep_node_ids or index_dep),
                priority=band + i,
                verify="none", max_retries=retries,
                metadata={"worker_role": lane.get("role") or "coding",
                          "lane": lane["title"],
                          "workstream": ws_id,
                          "scope": list(lane.get("scope") or [])})
            tasks.append(node)
            lane_ids.append(node["id"])
            lane_dep_ids[lane["title"]] = [node["id"]]
            lane_ws[i]["node_ids"] = [node["id"]]

        integrate = new_task(
            "Integrate lane outputs",
            ("Collect every lane's result. Reconcile overlapping changes, "
             "detect conflicts, apply compatible work, and leave shared/"
             "integration-owned files consistent. Do NOT blindly merge — "
             "record conflicts you could not resolve in the output.\n"
             "Objective: " + objective
             + self._deliverables_text(mission)),
            kind="integrate", deps=lane_ids, priority=38,
            verify="none", max_retries=1,
            metadata={"worker_role": "integrator"})
        review = new_task(
            "Independent review",
            ("Independently review the integrated result against the "
             "original objective — do not echo the workers' reasoning. "
             "Check correctness, regressions, architecture consistency, "
             "unhandled edge cases, security/permission boundaries, state "
             "preservation and test quality. Return verdict + findings.\n"
             "Objective: " + objective),
            kind="review", deps=[integrate["id"]], priority=39,
            verify="none", max_retries=1,
            model_role="deep_reasoner",
            metadata={"worker_role": "reviewer"})
        tasks += [integrate, review]

        verif_deps = [review["id"]]
        for crit in mission.get("success_criteria") or []:
            kind = str(crit.get("kind") or "")
            if kind in {"verify_passed", "all_tasks_completed"} or not kind:
                continue
            if kind in {"artifact_exists", "file_exists"}:
                tasks.append(new_task(
                    f"Check artifact: {crit.get('target', '')}",
                    f"internal:artifact_exists:{crit.get('target', '')}",
                    kind="internal", deps=verif_deps, priority=30,
                    verify="none", max_retries=0))
        verify = new_task(
            "Verify integrated work",
            ("Run the project's verification (tests/build as appropriate) "
             "on the integrated result and report pass/fail with the exact "
             "failing signal if any."),
            kind="verify", deps=verif_deps, priority=40,
            verify="auto", max_retries=1)
        tasks.append(verify)
        return tasks

    def _record_plan(self, mission: dict, tasks: list[dict],
                     reason: str) -> list[dict]:
        """Plan versioning — every plan build/rebuild records why it
        changed so 'why does the plan look like this' is answerable."""
        version = int(mission.get("plan_version") or 0) + 1
        mission["plan_version"] = version
        mission.setdefault("plan_history", []).append({
            "ts": time.time(), "version": version, "reason": reason[:300],
            "nodes": len(tasks),
            "lanes": [str((n.get("metadata") or {}).get("lane") or "")
                      for n in tasks
                      if (n.get("metadata") or {}).get("lane")]})
        return tasks

    def _deliverables_text(self, mission: dict) -> str:
        """Declared artifact_exists/file_exists criteria are hard
        deliverables — the first executor pass must know prose does not
        satisfy them (bounded-soak finding: missions burned a full
        fail→replan cycle before the recovery instruction named the
        target). Name every required artifact and demand a real write."""
        targets = [
            str(c.get("target") or "").strip()
            for c in (mission.get("success_criteria") or [])
            if str(c.get("kind") or "") in {"artifact_exists",
                                           "file_exists"}
            and str(c.get("target") or "").strip()]
        if not targets:
            return ""
        named = "; ".join(targets[:8])
        return (
            f"\nRequired deliverable(s): {named}. The mission is NOT "
            "complete until each exists on disk — invoke the file-write "
            "tool with complete, concrete content. A prose summary or a "
            "description of the file does not count.")

    def _constraints_text(self, mission: dict) -> str:
        cons = [c for c in (mission.get("constraints") or []) if c]
        if not cons:
            return ""
        return "\nConstraints (must obey): " + "; ".join(cons[:10])

    @staticmethod
    def _project_context(mission: dict) -> str:
        ctx = str(mission.get("project_context") or "").strip()
        return ("\nProject context (durable project memory — respect its "
                "decisions and constraints):\n" + ctx) if ctx else ""

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

        # A failed artifact_exists criterion needs a recovery path that
        # actually produces the file — the generic "apply the diagnosis"
        # instruction let prose-only agent output sail past the missing
        # artifact (bounded-soak finding: every mission timed out in that
        # loop). Name the target explicitly and re-check it after the fix
        # so the criterion is never retired unverified.
        artifact_target = ""
        failed_instr = str((failed_node or {}).get("instruction") or "")
        if failed_instr.startswith("internal:artifact_exists:"):
            artifact_target = failed_instr.split(
                "internal:artifact_exists:", 1)[1].strip()

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
        if artifact_target:
            fix_instruction = (
                f"The declared mission artifact does not exist: "
                f"{artifact_target}. Produce it for real — call the "
                "file-write tool with complete, concrete content. A prose "
                "summary or a description of the file does NOT satisfy "
                "this; the artifact must exist on disk. Objective: "
                + str(mission.get("objective") or "")
                + self._constraints_text(mission))
        else:
            fix_instruction = (
                "Apply the diagnosis to make progress on the mission "
                "objective. Objective: "
                + str(mission.get("objective") or "")
                + self._constraints_text(mission))
        fix = new_task(
            f"Recover: {(failed_node or {}).get('title', 'task')[:80]}",
            fix_instruction,
            kind="agent", deps=[diagnose["id"]], priority=20,
            verify="none",
            max_retries=int((mission.get("budgets") or {}).get(
                "max_task_retries", 2)),
            created_by="replan",
        )
        tasks += [diagnose, fix]
        verify_deps = [fix["id"]]
        if artifact_target:
            recheck = new_task(
                f"Check artifact: {artifact_target}",
                f"internal:artifact_exists:{artifact_target}",
                kind="internal", deps=[fix["id"]], priority=30,
                verify="none", max_retries=0, created_by="replan")
            tasks.append(recheck)
            verify_deps.append(recheck["id"])
        verify = new_task(
            f"Re-verify ({mission['repair_loops']})",
            "Re-run verification for the recovered work and report pass/fail.",
            kind="verify", deps=verify_deps, priority=40,
            verify="auto", max_retries=1, created_by="replan",
        )
        tasks.append(verify)
        version = int(mission.get("plan_version") or 0) + 1
        mission["plan_version"] = version
        mission.setdefault("plan_history", []).append({
            "ts": time.time(), "version": version,
            "reason": f"replan: {reason[:240]}",
            "nodes": len(tasks), "lanes": []})
        mission.setdefault("history", []).append({
            "ts": time.time(), "event": "replan",
            "detail": f"plan v{version}: +{len(tasks)} tasks after: {reason[:200]}",
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
