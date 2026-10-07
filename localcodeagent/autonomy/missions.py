"""Mission model + durable MissionStore.

A mission is a user-authorized objective that can outlive a chat turn or an
application session. Missions are plain dicts inside AutonomyStore.missions
so older records tolerate new fields without migrations.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any, Callable

from .state import AutonomyStore

MISSION_STATUSES = {
    "draft", "ready", "active", "planning", "executing", "verifying",
    "evaluating", "replanning", "waiting_dependency", "waiting_trigger",
    "waiting_approval", "paused", "blocked", "completed",
    "completed_with_warnings", "failed", "cancelled", "archived",
}
TERMINAL_MISSION_STATUSES = {"completed", "completed_with_warnings", "failed", "cancelled"}
# States that mean "still owned by the supervisor, must recover on restart".
LIVE_MISSION_STATUSES = {
    "active", "planning", "executing", "verifying", "evaluating",
    "replanning", "waiting_dependency",
}

MISSION_SCOPES = {
    "one_shot", "workspace", "repository", "standing",
    "maintenance", "self_development",
}

MISSION_PRIORITIES = {"urgent", "interactive", "normal", "background", "maintenance"}
_PRIORITY_RANK = {"urgent": 0, "interactive": 1, "normal": 2, "background": 3, "maintenance": 4}

# Priority aging — background/maintenance work that has waited long enough
# is promoted toward (never past) `normal` so low-priority missions can't
# starve behind a steady stream of newer work. Interactive and urgent
# already outrank it; aging can never cross that boundary.
AGE_PROMOTE_AFTER_S = (4 * 3600, 24 * 3600)  # wait → extra rank per tier
_AGE_FLOOR = _PRIORITY_RANK["normal"]        # never outrank interactive

# Retention — terminal missions stay visible for 30 days, then quiet
# completions/cancels auto-archive so unattended installs stay bounded.
MISSION_RETENTION_S = 30 * 86400


def effective_rank(mission: dict, now: float | None = None) -> int:
    """Priority rank after aging. Background-tier missions gain one rank
    per aging tier waited; everything at/above `normal` is unaffected."""
    rank = _PRIORITY_RANK.get(str(mission.get("priority")), 9)
    if rank <= _AGE_FLOOR:
        return rank
    now = now or time.time()
    age = now - float(mission.get("created_at") or now)
    boost = sum(age > t for t in AGE_PROMOTE_AFTER_S)
    return max(_AGE_FLOOR, rank - boost)

DEFAULT_BUDGETS = {
    "max_runtime_s": 4 * 3600,
    "max_repair_loops": 5,
    "max_task_retries": 3,
    "max_same_failure_retries": 3,
    "max_tool_failures": 8,
    "max_network_bytes": 0,       # 0 = unbounded
    "max_research_loops": 3,
    "max_approval_retries": 2,
}

DEFAULT_NOTIFICATION_POLICY = "important"  # all|important|failures|completion|silent


def new_mission(objective: str, *, title: str = "", user_request: str = "",
                scope: str = "one_shot", priority: str = "normal",
                success_criteria: list[dict] | None = None,
                constraints: list[str] | None = None,
                autonomy_profile: str = "local_autonomous",
                budgets: dict | None = None,
                notification_policy: str = DEFAULT_NOTIFICATION_POLICY,
                source: str = "api", source_id: str = "",
                workspace: str = "", project_id: str = "",
                repository: str = "", branch: str = "",
                created_by: str = "user", max_runtime_s: float = 0.0,
                decomposition: list[dict] | None = None,
                pipeline: str = "", pipeline_options: dict | None = None,
                ) -> dict[str, Any]:
    now = time.time()
    merged_budgets = dict(DEFAULT_BUDGETS)
    if budgets:
        for k, v in budgets.items():
            if k in merged_budgets:
                try:
                    merged_budgets[k] = float(v)
                except (TypeError, ValueError):
                    pass
    if max_runtime_s:
        merged_budgets["max_runtime_s"] = float(max_runtime_s)
    return {
        "id": f"m-{uuid.uuid4().hex[:12]}",
        "title": (title or objective or "mission")[:140],
        "objective": str(objective)[:4000],
        "user_request": str(user_request or objective)[:4000],
        "status": "draft",
        "phase": "draft",
        "scope": scope if scope in MISSION_SCOPES else "one_shot",
        "priority": priority if priority in MISSION_PRIORITIES else "normal",
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "workspace": workspace,
        "project_id": project_id,
        "repository": repository,
        "branch": branch,
        "success_criteria": list(success_criteria or []),
        "requirement_ids": [],
        "constraints": [str(c)[:500] for c in (constraints or [])][:40],
        "autonomy_profile": autonomy_profile,
        "graph": {"nodes": []},
        "plan_version": 0,
        "plan_history": [],
        "decomposition": [d for d in (decomposition or [])
                          if isinstance(d, dict)][:8],
        "pipeline": str(pipeline or "")[:40],
        "pipeline_options": dict(pipeline_options or {}),
        "attempts": 0,
        "repair_loops": 0,
        "failure_history": [],
        "research_history": [],
        "verification_history": [],
        "evaluator_history": [],
        "artifacts": [],
        "checkpoints": [],
        "learned_lessons": [],
        "blocked_reason": "",
        "waiting_for": "",
        "next_review_at": None,
        "runtime_deadline": None,
        "budgets": merged_budgets,
        "notification_policy": notification_policy,
        "created_by": created_by,
        "source": source,
        "source_id": source_id,
        "stop_requested": False,
        "pending_approval": None,
        "approval_retries": 0,
        "completion": None,
        "revision": 1,
        "history": [],          # mission-level state transition log
        "conversation": [],     # bounded mission Q&A thread
        "lease": None,          # {"owner", "expires"} — crash-detection token
    }


class MissionStore:
    """CRUD + transition guard over the missions JSON store."""

    VALID_TRANSITIONS: dict[str, set[str]] = {
        "draft": {"ready", "cancelled"},
        "ready": {"active", "planning", "waiting_trigger", "paused", "cancelled"},
        "active": {"planning", "executing", "verifying", "evaluating",
                   "waiting_approval", "waiting_dependency", "paused",
                   "blocked", "cancelled"},
        "planning": {"executing", "waiting_approval", "waiting_dependency",
                     "paused", "blocked", "cancelled", "failed"},
        "executing": {"verifying", "evaluating", "replanning",
                      "waiting_approval", "waiting_dependency", "paused",
                      "blocked", "completed", "completed_with_warnings",
                      "failed", "cancelled"},
        "verifying": {"evaluating", "executing", "replanning", "paused",
                      "blocked", "completed", "completed_with_warnings",
                      "failed", "cancelled"},
        "evaluating": {"completed", "completed_with_warnings", "replanning",
                       "executing", "blocked", "failed", "paused", "cancelled"},
        "replanning": {"planning", "executing", "paused", "blocked",
                       "cancelled", "failed"},
        "waiting_dependency": {"executing", "planning", "paused", "blocked",
                               "cancelled"},
        "waiting_trigger": {"ready", "paused", "cancelled"},
        "waiting_approval": {"executing", "replanning", "paused", "blocked",
                             "cancelled", "failed"},
        "paused": {"ready", "active", "planning", "executing", "cancelled"},
        "blocked": {"replanning", "paused", "cancelled", "failed"},
        "completed": {"archived"},
        "completed_with_warnings": {"archived"},
        "failed": {"archived", "ready"},
        "cancelled": {"archived", "ready"},
        "archived": set(),
    }

    def __init__(self, store: AutonomyStore,
                 on_change: Callable[[dict], None] | None = None,
                 derive_hook: Callable[[dict], None] | None = None) -> None:
        self._store = store
        self._lock = threading.RLock()
        self.on_change = on_change
        # Requirement derivation — called on every create() so every
        # mission carries explicit requirement entities regardless of
        # which entry point spawned it.
        self._derive_hook = derive_hook
        self._recover_orphans()

    # -- persistence helpers -------------------------------------------

    def _rows(self) -> list[dict]:
        return self._store.missions.data.setdefault("missions", [])

    def _save(self) -> None:
        self._store.missions.save()

    def _emit(self, mission: dict, event: str = "mission_updated") -> None:
        if self.on_change is not None:
            try:
                self.on_change({"type": event, "mission": self._public(mission)})
            except Exception:
                pass

    @staticmethod
    def _public(m: dict) -> dict:
        out = dict(m)
        return out

    def _recover_orphans(self) -> None:
        """Restart recovery: a mission that was mid-work when the process
        died is parked back into a recoverable state — never left claiming
        active work it is not doing."""
        changed = False
        with self._lock:
            for m in self._rows():
                if m.get("status") in {"planning", "executing", "verifying",
                                       "evaluating", "replanning"}:
                    m["status"] = "active"
                    m["phase"] = "recovered"
                    m["lease"] = None
                    m.setdefault("history", []).append({
                        "ts": time.time(), "event": "recovered",
                        "detail": "process restarted while mission was active",
                    })
                    changed = True
                for node in (m.get("graph") or {}).get("nodes", []):
                    if node.get("state") in {"running", "verifying"}:
                        node["state"] = "ready"
                        node["lease"] = None
                        changed = True
            if changed:
                self._save()

    # -- CRUD ----------------------------------------------------------

    def create(self, **fields: Any) -> dict:
        mission = new_mission(**fields)
        mission["history"].append({
            "ts": time.time(), "event": "created",
            "detail": f"source={mission['source']}",
        })
        with self._lock:
            self._rows().append(mission)
            if self._derive_hook is not None:
                try:
                    self._derive_hook(mission)
                except Exception:
                    pass  # derivation must never block mission creation
            self._save()
        self._emit(mission, "mission_created")
        return self._public(mission)

    def get(self, mission_id: str) -> dict | None:
        with self._lock:
            for m in self._rows():
                if m.get("id") == mission_id:
                    return self._public(m)
        return None

    def _get_mut(self, mission_id: str) -> dict | None:
        for m in self._rows():
            if m.get("id") == mission_id:
                return m
        return None

    def list(self, *, include_archived: bool = False) -> list[dict]:
        with self._lock:
            rows = [self._public(m) for m in self._rows()
                    if include_archived or m.get("status") != "archived"]
        now = time.time()
        rows.sort(key=lambda m: (
            effective_rank(m, now),
            -(m.get("updated_at") or 0)))
        return rows

    def active(self) -> list[dict]:
        with self._lock:
            return [self._public(m) for m in self._rows()
                    if m.get("status") in
                    {"ready", "active", "planning", "executing", "verifying",
                     "evaluating", "replanning", "waiting_dependency",
                     "waiting_approval"}]

    def transition(self, mission_id: str, new_status: str, *,
                   detail: str = "", phase: str | None = None) -> dict | None:
        """Guarded status change. Illegal transitions are rejected so the
        UI can never claim ACTIVE work for a mission that is really blocked."""
        with self._lock:
            m = self._get_mut(mission_id)
            if m is None or new_status not in MISSION_STATUSES:
                return None
            cur = str(m.get("status"))
            if cur != new_status and new_status not in self.VALID_TRANSITIONS.get(cur, set()):
                return None
            m["status"] = new_status
            if phase is not None:
                m["phase"] = phase
            elif new_status != cur:
                m["phase"] = new_status
            m["updated_at"] = time.time()
            if new_status in {"active", "planning"} and not m.get("started_at"):
                m["started_at"] = time.time()
                if m["budgets"].get("max_runtime_s"):
                    m["runtime_deadline"] = time.time() + m["budgets"]["max_runtime_s"]
            if new_status in TERMINAL_MISSION_STATUSES:
                m["completed_at"] = m.get("completed_at") or time.time()
                m["lease"] = None
                self._auto_archive_old()
            m.setdefault("history", []).append({
                "ts": time.time(), "event": "transition",
                "detail": f"{cur} -> {new_status}" + (f" · {detail}" if detail else "")[:400],
            })
            m["history"] = m["history"][-200:]
            self._save()
        self._emit(m, "mission_updated")
        return self._public(m)

    def _auto_archive_old(self) -> None:
        """Long-running installs accumulate terminal missions forever;
        archive quiet successes/cancels older than RETENTION_S so the
        resident store stays bounded. Failed missions are never
        auto-archived — they need operator attention. Caller holds _lock."""
        cutoff = time.time() - MISSION_RETENTION_S
        for old in self._rows():
            if old.get("status") in {"completed", "completed_with_warnings",
                                     "cancelled"}:
                try:
                    done = float(old.get("completed_at") or 0)
                except (TypeError, ValueError):
                    continue
                if done and done < cutoff:
                    old["status"] = "archived"
                    old["updated_at"] = time.time()

    def update(self, mission_id: str, **fields: Any) -> dict | None:
        """Direct field update for bookkeeping (histories, graph, flags)."""
        with self._lock:
            m = self._get_mut(mission_id)
            if m is None:
                return None
            for k, v in fields.items():
                m[k] = v
            m["updated_at"] = time.time()
            self._save()
        self._emit(m, "mission_updated")
        return self._public(m)

    def mutate(self, mission_id: str, fn: Callable[[dict], None]) -> dict | None:
        """Atomic read-modify-write against the live mission row."""
        with self._lock:
            m = self._get_mut(mission_id)
            if m is None:
                return None
            fn(m)
            m["updated_at"] = time.time()
            self._save()
        self._emit(m, "mission_updated")
        return self._public(m)

    def append_history(self, mission_id: str, event: str, detail: str = "") -> None:
        def _fn(m: dict) -> None:
            m.setdefault("history", []).append(
                {"ts": time.time(), "event": event, "detail": str(detail)[:400]})
            m["history"] = m["history"][-200:]
        self.mutate(mission_id, _fn)

    def log_activity(self, mission_id: str, category: str, detail: str) -> None:
        self.append_history(mission_id, category, detail)

    def next_runnable(self) -> list[dict]:
        """Missions the supervisor may drive this tick, priority-ordered."""
        return self.list()

    def flag_requirement_change(self, superseded: list[str]) -> dict:
        """Requirement-change propagation: when conversation memory
        supersedes a fact ('store uses PostgreSQL' -> 'store uses
        SQLite'), in-flight mission nodes whose instructions still
        reference the old value get flagged `stale_requirement` so the
        planner/replan path re-derives instead of executing on dead
        information. Terminal nodes and completed work stay untouched —
        history records what was believed at the time."""
        flagged: list[dict] = []
        needles: list[str] = []
        for text in superseded or []:
            t = str(text or "").strip()
            if not t:
                continue
            # Canonical fact shape is "subject uses value" — the value
            # half is the stale requirement; the subject stays valid.
            m = re.match(
                r"^.+?\s+(?:uses?|is|are|was|runs? on|prefers?|should"
                r" (?:stay|be|remain|use)|must be|will be)\s+(.+?)[.!?]?$",
                t, flags=re.IGNORECASE)
            needle = str(m.group(1) if m else t).strip().lower()
            if len(needle) >= 3:
                needles.append(needle)
        if not needles:
            return {"flagged": flagged}
        from .task_graph import TERMINAL_TASK_STATES
        for mission in self.active():
            hits: list[tuple[dict, list[str]]] = []
            for node in (mission.get("graph") or {}).get("nodes", []):
                if (node.get("state") in TERMINAL_TASK_STATES
                        or node.get("stale_requirement")):
                    continue
                hay = (str(node.get("title") or "") + " "
                       + str(node.get("instruction") or "")).lower()
                matched = [n for n in needles if n in hay]
                if matched:
                    hits.append((node, matched))
            if not hits:
                continue
            hit_map = {str(n.get("id") or ""): matched
                       for n, matched in hits}

            def _flag(row: dict, _map: dict = hit_map) -> None:
                stale = sorted({h for ms in _map.values() for h in ms})
                live = {
                    str(n.get("id") or ""): n
                    for n in (row.get("graph") or {}).get("nodes", [])
                }
                for nid, matched in _map.items():
                    node = live.get(nid)
                    if node is None:
                        continue
                    node["stale_requirement"] = True
                    node.setdefault("metadata", {}).setdefault(
                        "superseded_requirements", []).extend(matched)
                row.setdefault("history", []).append({
                    "ts": time.time(),
                    "event": "requirement_changed",
                    "detail": (
                        f"{len(_map)} node(s) reference superseded "
                        f"requirement(s): "
                        + "; ".join(stale))[:400],
                })
                row["history"] = row["history"][-200:]
            updated = self.mutate(str(mission.get("id") or ""), _flag)
            if updated is not None:
                flagged.append({
                    "mission_id": mission.get("id"),
                    "nodes": [n.get("id") for n, _ in hits],
                })
        return {"flagged": flagged}
