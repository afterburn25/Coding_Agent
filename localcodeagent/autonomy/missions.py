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
from pathlib import Path
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

# Workstream lifecycle — the durable middle tier between Mission and
# graph Task. A workstream owns a scoped slice of the objective, a set
# of node ids, an optional worktree, and its own acceptance criteria.
WORKSTREAM_STATES = {
    "planned", "ready", "active", "awaiting_review",
    "integration_ready", "integrated", "abandoned", "failed",
    "paused", "blocked",
}
LIVE_WORKSTREAM_STATES = {"ready", "active", "awaiting_review"}
TERMINAL_WORKSTREAM_STATES = {"integrated", "abandoned", "failed"}
# Node states that count as "done" inside a workstream progress rollup.
TERMINAL_WORKSTREAM_NODE_STATES = {"completed", "skipped", "cancelled"}

# Dynamic priority ladder (§20). Lower rank = sooner.
WORKSTREAM_PRIORITIES = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}

# Engineering decision + capsule bounds — history stays raw on the
# mission; these are the compact durable layers.
MAX_DECISIONS = 60
MAX_CAPSULE_ITEMS = 24

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
        # ---- engineering-mission layer ------------------------------
        # Formal hierarchy: Mission → workstreams → graph nodes.
        # Workstreams are durable records (not prompt text) so the
        # hierarchy survives restart and context compaction.
        "workstreams": [],
        # Explicit completion criteria authored before implementation —
        # distinct from success_criteria *kinds* (which the evaluator
        # checks mechanically); these are the user-facing contract.
        "acceptance_criteria": [],
        # Durable engineering decisions — workers inherit these.
        "decisions": [],
        "superseded_requirements": [],
        # Compact mission context — the state a fresh worker/model needs
        # without reading full history. Rebuilt by capsule refresh;
        # raw history stays intact on the mission.
        "context_capsule": {},
        # Path/symbol reservations: {id, owner(node/worker), patterns,
        # state, claimed_at, lease_expires} — parallel workers never
        # silently overwrite the same code.
        "ownership": [],
        # Git checkpoints: {id, label, ref, commit, at} — rollback points
        # at baseline / per-workstream-complete / integrated / final.
        "git_checkpoints": [],
        "unresolved_questions": [],
        # Mission quality metrics (§44) — counters accumulate across the
        # whole run for post-hoc scheduling improvement.
        "metrics": {
            "repair_cycles": 0, "replans": 0, "escalations": 0,
            "compactions": 0, "worker_crashes": 0, "ownership_conflicts": 0,
            "approval_wait_s": 0.0, "verification_failures": 0,
            "duplicate_work": 0, "model_calls": 0, "tokens_est": 0,
            "checkpoints": 0, "restart_recoveries": 0,
        },
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
                    elif node.get("state") == "waiting_approval":
                        # Park interrupted mid-flight: the mission stayed
                        # in a drive state, so the gate is unowned — the
                        # approval row may never have been written. Requeue
                        # the node to re-run and re-ask; a surviving pending
                        # row is superseded when the node parks again.
                        node["state"] = "ready"
                        changed = True
                if m.get("pending_approval") or \
                        m.get("waiting_for") == "approval":
                    m["pending_approval"] = None
                    m["waiting_for"] = ""
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
                # §18 — propagate beyond the task tier. The mission's
                # superseded list is the audit trail; workstream
                # acceptance rows and durable decisions that still
                # assert the old value stop being treated as live.
                sup = row.setdefault("superseded_requirements", [])
                for s in stale:
                    if s not in sup:
                        sup.append(s)
                row["superseded_requirements"] = sup[-20:]
                crit = row.get("acceptance_criteria") or []
                if crit:
                    keep = [c for c in crit
                            if not any(n in str(c).lower()
                                       for n in stale)]
                    if len(keep) != len(crit):
                        row["acceptance_criteria"] = keep
                for ws in (row.get("workstreams") or []):
                    hay = (str(ws.get("title") or "") + " "
                           + " ".join(str(a) for a in
                                      ws.get("acceptance") or [])).lower()
                    hit = [n for n in stale if n in hay]
                    if hit:
                        ws.setdefault("requirement_flags", []).append({
                            "ts": time.time(),
                            "superseded": hit[:4]})
                        ws["updated_at"] = time.time()
                for d in (row.get("decisions") or []):
                    if d.get("superseded"):
                        continue
                    if any(n in str(d.get("decision") or "").lower()
                           for n in stale):
                        d["superseded"] = True
                        d["superseded_by"] = "requirement change"
                        d["superseded_at"] = time.time()
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

    # -- workstreams (durable hierarchy tier) ---------------------------

    def add_workstream(self, mission_id: str, title: str, *,
                       objective: str = "", scope: list[str] | None = None,
                       role: str = "", priority: str = "p2",
                       depends_on: list[str] | None = None,
                       acceptance: list[str] | None = None,
                       node_ids: list[str] | None = None) -> dict | None:
        """Attach a durable workstream record. Nodes link back via
        metadata["workstream"]; the workstream's own status is derived
        by ``workstream_rollup`` plus explicit lifecycle transitions."""
        ws = {
            "id": f"ws-{uuid.uuid4().hex[:10]}",
            "title": str(title or "workstream")[:140],
            "objective": str(objective)[:2000],
            "status": "planned",
            "priority": priority if priority in WORKSTREAM_PRIORITIES
                        else "p2",
            "role": str(role or "coding")[:40],
            "scope": [str(s)[:200] for s in (scope or [])][:16],
            "depends_on": [str(d) for d in (depends_on or [])][:16],
            "node_ids": [str(n) for n in (node_ids or [])][:64],
            "worktree": None,   # {"path", "branch", "state"}
            "acceptance": [str(a)[:300] for a in (acceptance or [])][:16],
            "blocker": "",
            "result": "",
            "created_at": time.time(),
            "updated_at": time.time(),
        }

        def _fn(m: dict) -> None:
            m.setdefault("workstreams", []).append(ws)
            m["workstreams"] = m["workstreams"][-40:]
        return self.mutate(mission_id, _fn) and ws or None

    def _ws_mut(self, m: dict, ws_id: str) -> dict | None:
        for ws in m.get("workstreams") or []:
            if ws.get("id") == ws_id:
                return ws
        return None

    def update_workstream(self, mission_id: str, ws_id: str,
                          **fields: Any) -> dict | None:
        def _fn(m: dict) -> None:
            ws = self._ws_mut(m, ws_id)
            if ws is not None:
                for k, v in fields.items():
                    ws[k] = v
                ws["updated_at"] = time.time()
        return self.mutate(mission_id, _fn)

    def workstream_status(self, mission_id: str, ws_id: str,
                          status: str, *, detail: str = "") -> dict | None:
        if status not in WORKSTREAM_STATES:
            return None
        out = self.update_workstream(
            mission_id, ws_id, status=status,
            blocker=detail if status in {"blocked", "failed"} else "")
        if out is not None:
            self.append_history(mission_id, "workstream",
                                f"{ws_id} -> {status}"
                                + (f" · {detail}" if detail else ""))
        return out

    def workstream_rollup(self, mission: dict) -> list[dict]:
        """Workstream rows with live node progress folded in — the UI's
        default mission view (§34). Node counts come from metadata
        ["workstream"] links plus explicit node_ids."""
        nodes = (mission.get("graph") or {}).get("nodes") or []
        rows = []
        for ws in mission.get("workstreams") or []:
            ids = set(ws.get("node_ids") or [])
            linked = [n for n in nodes
                      if n.get("id") in ids
                      or (n.get("metadata") or {}).get("workstream")
                      == ws.get("id")]
            states = [str(n.get("state") or "") for n in linked]
            done = sum(s in TERMINAL_WORKSTREAM_NODE_STATES
                       for s in states)
            running = sum(s in {"running", "verifying"} for s in states)
            failed = sum(s == "failed" for s in states)
            rows.append({
                **{k: ws.get(k) for k in
                   ("id", "title", "objective", "status", "priority",
                    "role", "scope", "depends_on", "worktree",
                    "acceptance", "blocker", "result")},
                "tasks": len(linked), "tasks_done": done,
                "tasks_running": running, "tasks_failed": failed,
                "node_ids": [n.get("id") for n in linked],
                "progress": round(done / len(linked), 2)
                            if linked else 0.0,
            })
        return rows

    # -- engineering decisions -------------------------------------------

    def record_decision(self, mission_id: str, decision: str, *,
                        reason: str = "", source: str = "",
                        supersedes: str = "") -> dict | None:
        """Durable project decision — workers inherit active decisions.
        ``supersedes`` retires an earlier decision id."""
        rec = {
            "id": f"dec-{uuid.uuid4().hex[:8]}",
            "decision": str(decision)[:600],
            "reason": str(reason)[:600],
            "source": str(source or "mission")[:60],
            "superseded": False,
            "ts": time.time(),
        }

        def _fn(m: dict) -> None:
            if supersedes:
                for d in m.get("decisions") or []:
                    if d.get("id") == supersedes:
                        d["superseded"] = True
            m.setdefault("decisions", []).append(rec)
            m["decisions"] = m["decisions"][-MAX_DECISIONS:]
        out = self.mutate(mission_id, _fn)
        return rec if out is not None else None

    def active_decisions(self, mission: dict) -> list[dict]:
        return [d for d in (mission.get("decisions") or [])
                if not d.get("superseded")]

    # -- ownership reservations -------------------------------------------

    OWNERSHIP_LEASE_S = 600.0

    def ownership_conflicts(self, mission: dict,
                            patterns: list[str]) -> list[dict]:
        """Live reservations overlapping `patterns` — a glob overlaps
        when either pattern prefixes the other or they share a path
        literal. Owner-agnostic: callers exclude themselves."""
        hits: list[dict] = []
        now = time.time()
        for res in mission.get("ownership") or []:
            if res.get("state") != "held":
                continue
            if float(res.get("lease_expires") or 0) < now:
                continue
            for have in res.get("patterns") or []:
                for want in patterns or []:
                    h, w = str(have).rstrip("*"), str(want).rstrip("*")
                    if h == w or h.startswith(w) or w.startswith(h):
                        hits.append(res)
                        break
        return hits

    def reserve_paths(self, mission_id: str, owner: str,
                      patterns: list[str], *,
                      lease_s: float = 0.0) -> dict:
        """Reserve path patterns for a node/worker. Returns
        {"ok": True, "reservation": rec} or {"ok": False,
        "conflicts": [...]} — callers park/wait, never overwrite."""
        pats = [str(p)[:200] for p in (patterns or []) if str(p).strip()]
        if not pats:
            return {"ok": True, "reservation": None}
        lease = time.time() + (lease_s or self.OWNERSHIP_LEASE_S)
        rec = {
            "id": f"own-{uuid.uuid4().hex[:8]}",
            "owner": str(owner),
            "patterns": pats[:16],
            "state": "held",
            "claimed_at": time.time(),
            "lease_expires": lease,
        }
        with self._lock:
            m = self._get_mut(mission_id)
            if m is None:
                return {"ok": False, "conflicts": []}
            conflicts = self.ownership_conflicts(m, pats)
            conflicts = [c for c in conflicts
                         if c.get("owner") != owner]
            if conflicts:
                return {"ok": False, "conflicts": conflicts}
            # Same owner renewing/re-widening is fine — drop their stale
            # holds for these patterns and record the fresh claim.
            m.setdefault("ownership", [])
            m["ownership"] = [
                r for r in m["ownership"]
                if not (r.get("owner") == owner and r.get("state") == "held"
                        and set(r.get("patterns") or []) & set(pats))]
            m["ownership"].append(rec)
            m["ownership"] = m["ownership"][-80:]
            m["updated_at"] = time.time()
            self._save()
        self._emit(m, "mission_updated")
        return {"ok": True, "reservation": rec}

    def release_paths(self, mission_id: str, owner: str = "",
                      reservation_id: str = "") -> None:
        def _fn(m: dict) -> None:
            for r in m.get("ownership") or []:
                if r.get("state") != "held":
                    continue
                if (reservation_id and r.get("id") == reservation_id) \
                        or (owner and r.get("owner") == owner):
                    r["state"] = "released"
                    r["released_at"] = time.time()
        self.mutate(mission_id, _fn)

    def sweep_ownership(self, mission_id: str) -> list[str]:
        """Expire dead leases — a crashed worker's claim must not fence
        off its files forever (§12). Returns owners released."""
        released: list[str] = []
        now = time.time()

        def _fn(m: dict) -> None:
            for r in m.get("ownership") or []:
                if r.get("state") == "held" and float(
                        r.get("lease_expires") or 0) < now:
                    r["state"] = "expired"
                    released.append(str(r.get("owner") or ""))
        self.mutate(mission_id, _fn)
        return released

    # -- checkpoints + metrics --------------------------------------------

    def record_checkpoint(self, mission_id: str, label: str, *,
                          commit: str = "", ref: str = "",
                          detail: str = "") -> dict | None:
        rec = {
            "id": f"ckpt-{uuid.uuid4().hex[:8]}",
            "label": str(label)[:80],
            "commit": str(commit)[:80],
            "ref": str(ref)[:160],
            "detail": str(detail)[:300],
            "ts": time.time(),
        }

        def _fn(m: dict) -> None:
            m.setdefault("git_checkpoints", []).append(rec)
            m["git_checkpoints"] = m["git_checkpoints"][-60:]
            met = m.setdefault("metrics", {})
            met["checkpoints"] = int(met.get("checkpoints") or 0) + 1
        out = self.mutate(mission_id, _fn)
        return rec if out is not None else None

    def bump_metric(self, mission_id: str, key: str,
                    delta: float = 1.0) -> None:
        def _fn(m: dict) -> None:
            met = m.setdefault("metrics", {})
            met[key] = round(float(met.get(key) or 0) + delta, 3)
        self.mutate(mission_id, _fn)

    # -- unresolved questions / steering ------------------------------------

    def add_question(self, mission_id: str, question: str,
                     *, source: str = "") -> dict | None:
        rec = {"id": f"q-{uuid.uuid4().hex[:8]}",
               "question": str(question)[:600],
               "source": str(source)[:60],
               "resolved": False, "ts": time.time()}

        def _fn(m: dict) -> None:
            m.setdefault("unresolved_questions", []).append(rec)
            m["unresolved_questions"] = \
                m["unresolved_questions"][-30:]
        out = self.mutate(mission_id, _fn)
        return rec if out is not None else None

    # -- mission context (§4-§7) ------------------------------------------
    #
    # The capsule is the compact durable layer: raw history/evidence stay
    # on the mission untouched; the capsule is what a fresh worker/model
    # reads instead of the whole record. ``refresh_capsule`` rebuilds it
    # from live state; ``maybe_compact`` is the same rebuild triggered by
    # growth, plus durable-fact extraction.

    def refresh_capsule(self, mission_id: str) -> dict | None:
        """Rebuild ``context_capsule`` from live mission state — the
        state a worker needs without reading full history (§4)."""
        m = self.get(mission_id)
        if m is None:
            return None
        nodes = (m.get("graph") or {}).get("nodes") or []
        done_titles = [str(n.get("title") or "")[:90]
                       for n in nodes
                       if n.get("state") in TERMINAL_WORKSTREAM_NODE_STATES]
        running = [str(n.get("title") or "")[:90]
                   for n in nodes
                   if n.get("state") in {"running", "verifying", "ready"}]
        ws_rows = self.workstream_rollup(m)
        files: list[str] = []
        for n in nodes:
            meta = n.get("metadata") or {}
            for p in (meta.get("scope") or meta.get("paths") or []):
                p = str(p)
                if p and p not in files:
                    files.append(p)
        for a in m.get("artifacts") or []:
            p = str((a or {}).get("path") or (a or {}).get("name") or "")
            if p and p not in files:
                files.append(p)
        failures = [str(f.get("detail") or f.get("error") or "")[:160]
                    for f in (m.get("failure_history") or [])[-6:]]
        open_q = [str(q.get("question") or "")[:200]
                  for q in (m.get("unresolved_questions") or [])
                  if not q.get("resolved")]
        cap = {
            "objective": str(m.get("objective") or "")[:800],
            "acceptance_criteria":
                [str(c)[:200] for c in
                 (m.get("acceptance_criteria") or [])][:12],
            "constraints": [str(c)[:200] for c in
                            (m.get("constraints") or [])][:10],
            "decisions": [str(d.get("decision") or "")[:240]
                          for d in self.active_decisions(m)][:10],
            "workstreams": [
                {"id": w.get("id"), "title": w.get("title"),
                 "status": w.get("status"), "progress": w.get("progress")}
                for w in ws_rows],
            "tasks_done": done_titles[-MAX_CAPSULE_ITEMS:],
            "tasks_active": running[:MAX_CAPSULE_ITEMS],
            "known_failures": [f for f in failures if f],
            "blockers": ([str(m.get("blocked_reason") or "")]
                         if m.get("blocked_reason") else [])
                        + ([f"waiting: {m.get('waiting_for')}"]
                           if m.get("waiting_for") else [])
                        + open_q,
            "relevant_files": files[:MAX_CAPSULE_ITEMS],
            "superseded_requirements": [
                str(s)[:160] for s in
                (m.get("superseded_requirements") or [])][-8:],
            "built_at": time.time(),
            "version": int((m.get("context_capsule") or {})
                           .get("version") or 0) + 1,
        }

        def _fn(row: dict) -> None:
            row["context_capsule"] = cap
        self.mutate(mission_id, _fn)
        return cap

    def maybe_compact(self, mission_id: str) -> bool:
        """§5 auto-compaction — when the raw record grows past bounds,
        extract durable facts into the capsule and record the event.
        Raw history/evidence is NEVER deleted; the capsule is simply
        what workers read next."""
        m = self.get(mission_id)
        if m is None:
            return False
        big = (len(m.get("history") or []) >= 180
               or len(m.get("conversation") or []) >= 50
               or len(m.get("failure_history") or []) >= 12)
        if not big:
            return False
        cap = self.refresh_capsule(mission_id)
        if cap is None:
            return False
        self.bump_metric(mission_id, "compactions")
        self.append_history(
            mission_id, "compact",
            f"context capsule v{cap.get('version')} rebuilt")
        return True

    def context_package(self, mission: dict, node: dict,
                        *, nexus_md: str = "") -> str:
        """Per-worker context package (§6) — only what this node needs:
        objective, its workstream contract, in-force decisions, scope,
        recent same-workstream failures. Rendered as a bounded text
        block the executor prepends to the instruction."""
        cap = mission.get("context_capsule") or {}
        meta = node.get("metadata") or {}
        ws_id = str(meta.get("workstream") or "")
        ws = next((w for w in self.workstream_rollup(mission)
                   if w.get("id") == ws_id), None)
        lines: list[str] = ["[Mission context]"]
        obj = str(mission.get("objective") or "").strip()
        if obj:
            lines.append("Objective: " + obj[:400])
        crit = cap.get("acceptance_criteria") or \
            mission.get("acceptance_criteria") or []
        if crit:
            lines.append("Acceptance criteria:")
            lines += ["- " + str(c)[:160] for c in crit[:8]]
        if ws is not None:
            lines.append(
                f"Workstream: {ws.get('title')} "
                f"(status {ws.get('status')}, "
                f"{int(float(ws.get('progress') or 0) * 100)}% done)")
            if ws.get("objective"):
                lines.append("Workstream goal: "
                             + str(ws["objective"])[:240])
            if ws.get("acceptance"):
                lines.append("Workstream acceptance:")
                lines += ["- " + str(a)[:160]
                          for a in (ws.get("acceptance") or [])[:6]]
        decisions = cap.get("decisions") or [
            str(d.get("decision") or "")[:200]
            for d in self.active_decisions(mission)][:8]
        if decisions:
            lines.append("Decisions in force (do not revisit):")
            lines += ["- " + str(d)[:160] for d in decisions[:8]]
        cons = mission.get("constraints") or []
        if cons:
            lines.append("Constraints:")
            lines += ["- " + str(c)[:160] for c in cons[:6]]
        scope = meta.get("scope") or (ws or {}).get("scope") or []
        if scope:
            lines.append("Your scope: "
                         + ", ".join(str(s)[:80] for s in scope[:8]))
        ws_failures: list[str] = []
        for n in (mission.get("graph") or {}).get("nodes") or []:
            if ws_id and (n.get("metadata") or {}).get(
                    "workstream") != ws_id:
                continue
            res = n.get("result") or {}
            err = str(res.get("error") or "")
            if n.get("state") == "failed" and err:
                ws_failures.append(
                    f"{str(n.get('title') or '')[:60]}: {err[:120]}")
        if ws_failures:
            lines.append("Known failures in this workstream:")
            lines += ["- " + f for f in ws_failures[-4:]]
        # §23-24 — review/integration nodes get the evidence packet, not
        # a blind instruction: what each predecessor produced, which
        # files it touched, and which lane it came from. The reviewer
        # is a fresh worker — this is what makes the review independent
        # in practice instead of the implementer grading itself.
        if str(node.get("kind") or "") in {"review", "integrate"}:
            impl = []
            for n in (mission.get("graph") or {}).get("nodes") or []:
                if n.get("kind") != "agent" \
                        or n.get("state") != "completed":
                    continue
                res = n.get("result") or {}
                arts = [str(a) for a in (res.get("artifacts") or [])]
                lane = str(((n.get("metadata") or {})
                            .get("workstream")) or "")[:14]
                impl.append(
                    f"- {str(n.get('title') or '')[:70]}"
                    + (f" [{lane}]" if lane else "")
                    + ": files "
                    + (", ".join(arts[:8]) if arts else "not recorded"))
            if impl:
                lines.append(
                    "Work under review (you did NOT implement this — "
                    "audit it against the acceptance criteria):")
                lines += impl[:12]
            roll = self.workstream_rollup(mission)
            if roll:
                lines.append("Lane status:")
                lines += [
                    f"- {str(w.get('title') or '')[:60]}: "
                    f"{w.get('status')} "
                    f"({w.get('tasks_done', 0)}/{w.get('tasks', 0)})"
                    for w in roll[:8]]
        if nexus_md:
            lines.append("Repository guide (NEXUS.md, excerpt):")
            lines.append(nexus_md[:1200])
        return "\n".join(lines)[:3000]


    # -- live steering (§19-§20) ---------------------------------------------
    #
    # The user redirects a running mission without restarting it:
    # "pause the frontend", "make installer reliability the priority",
    # "forget the image work". These are durable record edits — the
    # supervisor reads workstream state every dispatch cycle.

    def pause_workstream(self, mission_id: str, ws_id: str,
                         *, reason: str = "user") -> dict | None:
        """Freeze one workstream — its pending nodes stop dispatching;
        running nodes finish the current step. Other workstreams
        continue (§29)."""
        def _fn(m: dict) -> None:
            ws = self._ws_mut(m, ws_id)
            if ws is None:
                return
            ws["status"] = "paused"
            ws["blocker"] = f"paused by {reason}"
            ws["updated_at"] = time.time()
            ws_nodes = set(ws.get("node_ids") or [])
            for n in (m.get("graph") or {}).get("nodes") or []:
                if n.get("id") in ws_nodes or \
                        (n.get("metadata") or {}).get("workstream") == ws_id:
                    if n.get("state") in {"planned", "ready",
                                          "waiting_dependency"}:
                        n["state"] = "blocked"
                        n["queue_reason"] = "workstream_paused"
                        n["queue_detail"] = "paused by user"
        out = self.mutate(mission_id, _fn)
        if out is not None:
            self.append_history(mission_id, "steer",
                                f"workstream {ws_id} paused ({reason})")
        return out

    def resume_workstream(self, mission_id: str, ws_id: str) -> dict | None:
        def _fn(m: dict) -> None:
            ws = self._ws_mut(m, ws_id)
            if ws is None:
                return
            ws["status"] = "ready"
            ws["blocker"] = ""
            ws["updated_at"] = time.time()
            ws_nodes = set(ws.get("node_ids") or [])
            for n in (m.get("graph") or {}).get("nodes") or []:
                if (n.get("id") in ws_nodes or
                        (n.get("metadata") or {}).get("workstream")
                        == ws_id) and n.get("state") == "blocked" \
                        and n.get("queue_reason") == "workstream_paused":
                    n["state"] = "ready"
                    n.pop("queue_reason", None)
                    n.pop("queue_detail", None)
        out = self.mutate(mission_id, _fn)
        if out is not None:
            self.append_history(mission_id, "steer",
                                f"workstream {ws_id} resumed")
        return out

    def reprioritize_workstream(self, mission_id: str, ws_id: str,
                                priority: str) -> dict | None:
        """Dynamic priority (§20) — p0..p3 maps onto node priorities so
        the scheduler naturally picks the raised work first."""
        priority = str(priority or "").lower()
        if priority not in WORKSTREAM_PRIORITIES:
            return None
        band = {"p0": 10, "p1": 16, "p2": 20, "p3": 26}[priority]

        def _fn(m: dict) -> None:
            ws = self._ws_mut(m, ws_id)
            if ws is None:
                return
            ws["priority"] = priority
            ws["updated_at"] = time.time()
            ws_nodes = set(ws.get("node_ids") or [])
            for n in (m.get("graph") or {}).get("nodes") or []:
                if (n.get("id") in ws_nodes or
                        (n.get("metadata") or {}).get("workstream")
                        == ws_id) and n.get("state") in \
                        {"planned", "ready", "waiting_dependency",
                         "blocked"}:
                    n["priority"] = band
        out = self.mutate(mission_id, _fn)
        if out is not None:
            self.append_history(mission_id, "steer",
                                f"workstream {ws_id} -> {priority}")
        return out

    def drop_workstream(self, mission_id: str, ws_id: str,
                        *, reason: str = "user") -> dict | None:
        """Abandon a workstream — pending nodes cancelled; running nodes
        finish their current step but dependents no longer wait on this
        scope. Completed work stays in history."""
        def _fn(m: dict) -> None:
            ws = self._ws_mut(m, ws_id)
            if ws is None:
                return
            ws["status"] = "abandoned"
            ws["blocker"] = f"dropped by {reason}"
            ws["updated_at"] = time.time()
            ws_nodes = set(ws.get("node_ids") or [])
            for n in (m.get("graph") or {}).get("nodes") or []:
                if n.get("id") in ws_nodes or \
                        (n.get("metadata") or {}).get("workstream") == ws_id:
                    if n.get("state") in {"planned", "ready",
                                          "waiting_dependency",
                                          "blocked"}:
                        n["state"] = "cancelled"
                        n["queue_reason"] = "workstream_dropped"
            # Dependents that only waited on cancelled work must not
            # hang — repoint them onto the cancelled nodes' remaining
            # deps (a cancelled dep counts as satisfied-with-warnings).
            cancelled = {n.get("id") for n in
                         (m.get("graph") or {}).get("nodes") or []
                         if n.get("state") == "cancelled"}
            for n in (m.get("graph") or {}).get("nodes") or []:
                deps = [d for d in (n.get("deps") or [])
                        if d not in cancelled]
                if deps != list(n.get("deps") or []):
                    n["deps"] = deps
        out = self.mutate(mission_id, _fn)
        if out is not None:
            self.append_history(mission_id, "steer",
                                f"workstream {ws_id} dropped ({reason})")
        return out

    def find_workstream(self, mission: dict, needle: str) -> dict | None:
        """Steering-language lookup — 'the frontend', 'installer',
        'voice' resolve against workstream titles/objectives. Fuzzy but
        conservative: unique substring match only."""
        t = str(needle or "").strip().lower()
        if not t or len(t) < 3:
            return None
        hits = []
        for ws in self.workstream_rollup(mission):
            hay = (str(ws.get("title") or "") + " "
                   + str(ws.get("objective") or "")).lower()
            if t in hay:
                hits.append(ws)
        return hits[0] if len(hits) == 1 else None


def discover_nexus_md(root: Any) -> str:
    """Repository-local operating instructions (§15) — NEXUS.md at the
    workspace root, read once per mission and injected into worker
    context. Project guidance only: it can inform work, it cannot relax
    permissions, safety, or credential rules."""
    try:
        p = Path(str(root)) / "NEXUS.md"
        if p.is_file():
            return p.read_text(encoding="utf-8",
                               errors="replace")[:4000]
    except Exception:
        pass
    return ""
