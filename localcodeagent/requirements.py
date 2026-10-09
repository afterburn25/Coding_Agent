"""First-class requirement tracking.

A requirement is a durable, inspectable entity — not a line in a task
list. Missions, projects and tasks link to requirements; the supervisor
and evaluator update their status as evidence arrives, so "what does
done mean" is a live object instead of a prompt guess.

Requirement fields (spec contract):
    id, description, source, priority, scope{type,id}, status,
    verification{kind,...}, dependencies, acceptance_criteria,
    evidence[], last_checked, owner{worker_id,task_id}, inferred

Sources mark where the requirement came from — inferred requirements
are always flagged so users can tell "Nexus derived this" from "the
user asked for this".
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

MAX_REQUIREMENTS = 2000
MAX_EVIDENCE = 30

REQUIREMENT_STATES = {
    "not_started", "planned", "in_progress", "implemented",
    "verified", "failed", "blocked", "deferred", "rejected",
}
ACTIVE_STATES = {"not_started", "planned", "in_progress", "implemented",
                 "failed", "blocked"}
REQUIREMENT_SOURCES = {
    "user", "spec", "inferred", "test_failure", "reviewer",
    "regression", "installer_constraint",
}
REQUIREMENT_PRIORITIES = {"critical", "high", "normal", "low"}

# Verification kinds understood by autonomy.evaluator.MissionEvaluator.
EVALUATOR_KINDS = {"all_tasks_completed", "verify_passed", "artifact_exists",
                   "file_exists", "artifact_verified", "metric",
                   "no_failures"}


def _rid() -> str:
    return f"req-{uuid.uuid4().hex[:10]}"


def new_requirement(description: str, *, source: str = "user",
                    inferred: bool = False, priority: str = "normal",
                    scope_type: str = "", scope_id: str = "",
                    verification: dict | None = None,
                    dependencies: list[str] | None = None,
                    acceptance_criteria: list[str] | None = None,
                    owner: dict | None = None) -> dict[str, Any]:
    now = time.time()
    ver = dict(verification or {})
    if not ver.get("kind"):
        ver["kind"] = "custom"
    return {
        "id": _rid(),
        "description": str(description)[:600],
        "source": source if source in REQUIREMENT_SOURCES else "user",
        "inferred": bool(inferred) or source == "inferred",
        "priority": priority if priority in REQUIREMENT_PRIORITIES else "normal",
        "scope": {"type": str(scope_type or "global"), "id": str(scope_id or "")},
        "status": "not_started",
        "verification": ver,
        "dependencies": [str(d) for d in (dependencies or [])][:20],
        "acceptance_criteria": [str(c)[:300] for c in
                                (acceptance_criteria or [])][:20],
        "evidence": [],
        "last_checked": None,
        "owner": {"worker_id": "", "task_id": "", **dict(owner or {})},
        "created_at": now,
        "updated_at": now,
    }


class RequirementStore:
    """Bounded JSON store at <runtime>/data/requirements.json."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._rows: list[dict[str, Any]] = self._load()

    # -- persistence --------------------------------------------------

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw.get("requirements", [])
            return [r for r in rows if isinstance(r, dict)
                    and r.get("id")][-MAX_REQUIREMENTS:]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            {"version": 1, "requirements": self._rows[-MAX_REQUIREMENTS:]},
            indent=2, ensure_ascii=False))

    # -- CRUD -----------------------------------------------------------

    def create(self, description: str, **kw: Any) -> dict[str, Any]:
        row = new_requirement(description, **kw)
        with self._lock:
            self._rows.append(row)
            if len(self._rows) > MAX_REQUIREMENTS:
                # Evict oldest terminal rows first; never drop live ones
                # silently — trim from the front only when forced.
                terminal = [r for r in self._rows
                            if r.get("status") not in ACTIVE_STATES]
                if terminal:
                    self._rows.remove(terminal[0])
                else:
                    self._rows = self._rows[-MAX_REQUIREMENTS:]
            self._save()
        return dict(row)

    def get(self, req_id: str) -> dict | None:
        with self._lock:
            for r in self._rows:
                if r.get("id") == req_id:
                    return dict(r)
        return None

    def list(self, *, scope_type: str = "", scope_id: str = "",
             status: str = "", limit: int = 200) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rows
                    if (not scope_type or r["scope"]["type"] == scope_type)
                    and (not scope_id or r["scope"]["id"] == scope_id)
                    and (not status or r.get("status") == status)]
        return rows[-limit:]

    def _mut(self, req_id: str) -> dict | None:
        for r in self._rows:
            if r.get("id") == req_id:
                return r
        return None

    def set_status(self, req_id: str, status: str, *,
                   evidence: str = "", detail: str = "") -> dict | None:
        if status not in REQUIREMENT_STATES:
            return None
        with self._lock:
            r = self._mut(req_id)
            if r is None:
                return None
            r["status"] = status
            r["last_checked"] = time.time()
            if evidence:
                self._add_evidence_locked(r, "status", evidence)
            r.setdefault("history", []).append(
                {"ts": time.time(), "status": status,
                 "detail": str(detail)[:200]})
            r["history"] = r["history"][-40:]
            r["updated_at"] = time.time()
            self._save()
            return dict(r)

    def _add_evidence_locked(self, r: dict, kind: str, detail: str,
                             ref: str = "") -> None:
        r.setdefault("evidence", []).append(
            {"ts": time.time(), "kind": str(kind)[:40],
             "detail": str(detail)[:300], "ref": str(ref)[:200]})
        r["evidence"] = r["evidence"][-MAX_EVIDENCE:]

    def add_evidence(self, req_id: str, kind: str, detail: str,
                     ref: str = "") -> dict | None:
        with self._lock:
            r = self._mut(req_id)
            if r is None:
                return None
            self._add_evidence_locked(r, kind, detail, ref)
            r["last_checked"] = time.time()
            r["updated_at"] = time.time()
            self._save()
            return dict(r)

    # -- mission wiring -------------------------------------------------

    def attach_mission(self, mission: dict) -> list[dict]:
        """Derive requirements for a mission that has none. Mutates the
        mission dict (requirement_ids + default success_criteria) — call
        before the mission row is persisted."""
        if mission.get("requirement_ids"):
            return []
        reqs = self.derive(
            str(mission.get("objective") or ""),
            scope_type="mission", scope_id=str(mission.get("id") or ""))
        mission["requirement_ids"] = [r["id"] for r in reqs]
        if not mission.get("success_criteria"):
            criteria = [dict(r["verification"]) for r in reqs
                        if r["verification"].get("kind") in EVALUATOR_KINDS]
            for crit, r in ((c, r) for c, r in
                            zip(criteria, reqs)):
                crit["requirement_id"] = r["id"]
                crit.setdefault("description", r["description"][:140])
            mission["success_criteria"] = criteria or [
                {"kind": "all_tasks_completed",
                 "description": "all tasks completed"}]
        return reqs

    def derive(self, objective: str, *, scope_type: str = "",
               scope_id: str = "", source: str = "inferred") -> list[dict]:
        """Create + persist derived requirements for a goal."""
        return self.create_specs(
            derive_requirement_specs(objective), scope_type=scope_type,
            scope_id=scope_id, source=source)

    def create_specs(self, specs: list[dict], *, scope_type: str = "",
                     scope_id: str = "",
                     source: str = "inferred") -> list[dict]:
        out = []
        for spec in specs:
            row = self.create(
                spec["description"], source=spec.get("source", source),
                inferred=True, priority=spec.get("priority", "normal"),
                scope_type=scope_type, scope_id=scope_id,
                verification=spec.get("verification"),
                acceptance_criteria=spec.get("acceptance_criteria"))
            out.append(row)
        return out

    def sync_mission(self, mission: dict,
                     criteria_results: list | None = None) -> None:
        """Push latest evaluator criteria + node state into linked
        requirement statuses — continuous status without user upkeep."""
        req_ids = set(mission.get("requirement_ids") or [])
        if not req_ids:
            return
        crit_by_rid: dict[str, dict] = {}
        # The evaluator's per-criterion outcomes carry requirement_id —
        # persisted on the mission row (criteria_results) and also passed
        # fresh at evaluation time.
        for c in (criteria_results
                  if criteria_results is not None
                  else mission.get("criteria_results") or []):
            rid = str(c.get("requirement_id") or "")
            if rid in req_ids:
                crit_by_rid[rid] = c
        nodes = (mission.get("graph") or {}).get("nodes", [])
        running = any(n.get("state") in {"running", "verifying", "ready"}
                      for n in nodes)
        with self._lock:
            for rid in req_ids:
                r = self._mut(rid)
                if r is None:
                    continue
                crit = crit_by_rid.get(rid)
                if crit is not None:
                    new = "verified" if crit.get("met") else "failed"
                    if r.get("status") != new:
                        r["status"] = new
                        r["updated_at"] = time.time()
                    r["last_checked"] = time.time()
                    self._add_evidence_locked(
                        r, "evaluation",
                        str(crit.get("detail") or crit.get("kind") or ""))
                elif running and r.get("status") in {
                        "not_started", "planned"}:
                    r["status"] = "in_progress"
                    r["updated_at"] = time.time()
            self._save()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            counts: dict[str, int] = {}
            for r in self._rows:
                counts[r.get("status", "?")] = counts.get(
                    r.get("status", "?"), 0) + 1
            return {"total": len(self._rows), "by_status": counts}


# ---------------------------------------------------------------------
# Acceptance-criteria derivation — deterministic rules over the
# objective text. Everything produced here is marked inferred; explicit
# user-supplied criteria always win over derived ones.

_REPAIR_RE = re.compile(
    r"\b(fix|repair|broken|broke|doesn'?t work|not working|won'?t|fails?|"
    r"failing|crash(?:es|ing)?|error|hang(?:s|ing)?|stuck|regression|"
    r"can'?t|cannot)\b"
    r"|\b(?:make|get)\s+(?:this|it|that|the\s+\w+)\s+"
    r"(?:work(?:ing)?|run(?:ning)?|start(?:ing)?)\b", re.I)
_FEATURE_RE = re.compile(
    r"\b(add|implement|create|build|introduce|support|make|write)\b", re.I)

# Domain rules: (pattern, [(description, verification dict)])
_DOMAIN_RULES: list[tuple[re.Pattern, list[dict[str, Any]]]] = [
    (re.compile(r"\b(startup|start ?up|launch|boot|won'?t start|"
                r"doesn'?t start|not starting)\b", re.I), [
        {"description": "Backend health endpoint responds after start",
         "verification": {"kind": "custom", "check": "health_endpoint"}},
        {"description": "Startup completes within the configured timeout",
         "verification": {"kind": "custom", "check": "startup_timeout"}},
        {"description": "No stale backend process remains after repair",
         "verification": {"kind": "custom", "check": "no_stale_process"}},
        {"description": "Existing user data and profiles remain intact",
         "verification": {"kind": "custom", "check": "state_preserved"}},
        {"description": "A fresh restart also succeeds",
         "verification": {"kind": "custom", "check": "restart_stable"}},
    ]),
    (re.compile(r"\b(installer|install|update|upgrade|setup)\b", re.I), [
        {"description": "Installer/update completes without error",
         "verification": {"kind": "custom", "check": "installer_ok"}},
        {"description": "Existing models, data and config are preserved",
         "verification": {"kind": "custom", "check": "install_preserved"}},
    ]),
    (re.compile(r"\b(model|llama|runtime|inference|gpu|vram)\b", re.I), [
        {"description": "Target model loads and answers a health probe",
         "verification": {"kind": "custom", "check": "model_health"}},
        {"description": "Model stays within the RAM/VRAM budget",
         "verification": {"kind": "custom", "check": "resource_budget"}},
    ]),
    (re.compile(r"\b(voice|tts|audio|speech|greeting|speak)\b", re.I), [
        {"description": "Voice output produces valid audible audio",
         "verification": {"kind": "custom", "check": "audio_ok"}},
    ]),
    (re.compile(r"\b(image|picture|photo|comfyui|render|upscale)\b", re.I), [
        {"description": "Image job completes and produces an artifact",
         "verification": {"kind": "artifact_exists"}},
    ]),
    (re.compile(r"\b(tests?|unittest|pytest|regression suite)\b", re.I), [
        {"description": "Target test suite passes",
         "verification": {"kind": "verify_passed"}},
    ]),
    (re.compile(r"\b(build|compile|package|bundle|ci)\b", re.I), [
        {"description": "Build completes and produces artifacts",
         "verification": {"kind": "verify_passed"}},
    ]),
    (re.compile(r"\b(dependenc|package|library|pip|npm|nuget)\b", re.I), [
        {"description": "Change verified in an isolated environment first",
         "verification": {"kind": "custom", "check": "isolated_verify"}},
        {"description": "Regression tests pass with the change",
         "verification": {"kind": "verify_passed"}},
    ]),
    (re.compile(r"\b(ui|page|button|layout|display|render|css|"
                r"frontend)\b", re.I), [
        {"description": "Affected UI surface loads without errors",
         "verification": {"kind": "custom", "check": "ui_loads"}},
    ]),
]


def is_repair_intent(text: str) -> bool:
    return bool(_REPAIR_RE.search(str(text or "")))


def derive_requirement_specs(objective: str) -> list[dict[str, Any]]:
    """Turn a vague goal into explicit requirement specs. Always returns
    at least a verification + no-regression pair so 'done' is never
    implicit. Marked inferred — the user can reject any row."""
    text = str(objective or "").strip()
    specs: list[dict[str, Any]] = []
    repair = is_repair_intent(text)
    feature = bool(_FEATURE_RE.search(text))

    seen: set[str] = set()

    def add(description: str, *, verification: dict | None = None,
            priority: str = "normal", source: str = "inferred") -> None:
        key = description.lower()
        if key in seen:
            return
        seen.add(key)
        specs.append({
            "description": description,
            "verification": dict(verification or {"kind": "custom"}),
            "acceptance_criteria": [description],
            "priority": priority, "source": source,
        })

    if repair:
        add("The reported failure symptom no longer reproduces",
            verification={"kind": "custom", "check": "symptom_gone"},
            priority="high")
        for pattern, rules in _DOMAIN_RULES:
            if pattern.search(text):
                for rule in rules:
                    add(rule["description"],
                        verification=rule.get("verification"))
    elif feature:
        add("The requested behavior works as specified",
            verification={"kind": "custom", "check": "behavior_verified"},
            priority="high")

    # Universal completion gates — cheap, always meaningful. Internal
    # missions (heartbeats, maintenance) have no verification node, so
    # 'verify_passed' is unsatisfiable for them and forces an infinite
    # diagnose→replan treadmill; 'no_failures' is their honest gate.
    internal = text.startswith("internal:")
    add("Planned work completes without failed tasks",
        verification={"kind": "no_failures"})
    if not internal:
        add("At least one verification run passes",
            verification={"kind": "verify_passed"})
    if (repair or feature) and not internal:
        add("No regressions: existing checks still pass",
            verification={"kind": "verify_passed"})
    return specs
