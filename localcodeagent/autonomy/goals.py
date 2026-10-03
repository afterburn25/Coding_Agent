"""Persistent Goal Manager — durable desired-state layer above missions.

A Goal is a long-lived desired state ("keep CI green", "keep crash-free",
"keep latency under X"). A Mission is a finite attempt to advance a Goal.

The manager periodically evaluates each enabled goal against real measured
metrics (MetricRegistry — server wiring supplies the providers) and derives
a health state:

    satisfied — terminal goal whose metrics all pass (evaluation stops)
    healthy   — continuous goal, all metrics within bounds
    degrading — a warn threshold crossed, or a linked repair mission failed
    violated  — a hard threshold breached
    blocked   — remediation wanted but the escalation path can't act
    unknown   — nothing measurable yet

A degrading/violated goal may generate a repair mission — evidence-based,
deduplicated (one live mission per goal), and cooldown-bounded so a broken
metric can never spam the queue. Every evaluation and spawn decision is
recorded for auditability.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from .metrics import MetricRegistry
from .missions import TERMINAL_MISSION_STATUSES

GOAL_TYPES = {
    "reliability", "performance", "security", "maintenance",
    "capability_growth", "cost_reduction", "test_coverage",
    "code_quality", "model_quality", "knowledge_freshness",
    "infrastructure_health",
}
GOAL_STATUSES = {"active", "paused", "completed", "archived"}
GOAL_HEALTH = {"satisfied", "healthy", "degrading", "violated",
               "blocked", "unknown"}
GOAL_PRIORITIES = {"critical", "high", "normal", "low"}
# goal priority -> mission priority (MISSION_PRIORITIES)
_PRIORITY_TO_MISSION = {
    "critical": "urgent", "high": "normal",
    "normal": "background", "low": "maintenance",
}
ESCALATION_POLICIES = {"mission", "notify", "observe"}

DEFAULT_REVIEW_INTERVAL_S = 300.0
DEFAULT_MISSION_COOLDOWN_S = 1800.0

# Confidence updates from linked-mission outcomes — bounded EMA-style.
_CONF_OK_STEP = 0.25
_CONF_FAIL_STEP = 0.35

_OPS = ("<=", ">=", "==", "!=", "<", ">")


def _metric_state(spec: dict, measured: dict) -> str:
    """ok | warn | breach | unknown for one metric spec."""
    if not measured.get("ok"):
        return "unknown"
    value = float(measured["value"])
    op = str(spec.get("op") or "<=")
    try:
        target = float(spec.get("target"))
    except (TypeError, ValueError):
        return "unknown"
    breach = {
        "<=": value > target, ">=": value < target,
        "<": value >= target, ">": value <= target,
        "==": value != target, "!=": value == target,
    }.get(op, False)
    if breach:
        return "breach"
    warn = spec.get("warn")
    if warn is not None:
        try:
            w = float(warn)
            # For "<=" goals the warn band sits inside the target
            # (warn < target); for ">=" it sits above (warn > target).
            if op in {"<=", "<"} and warn < target and value > warn:
                return "warn"
            if op in {">=", ">"} and warn > target and value < warn:
                return "warn"
        except (TypeError, ValueError):
            pass
    return "ok"


class GoalManager:
    """Evaluates durable goals; turns degraded goals into missions."""

    def __init__(self, store, metrics: MetricRegistry, *,
                 missions=None,
                 spawn_mission: Callable[[dict, dict], dict | None] | None = None,
                 notify: Callable[[str, str, str], None] | None = None,
                 audit: Callable[[str, dict], None] | None = None,
                 emit: Callable[[dict], None] | None = None,
                 is_blocked: Callable[[], bool] | None = None) -> None:
        self._store = store              # AutonomyStore
        self._metrics = metrics
        self._missions = missions        # MissionStore (live-status lookups)
        self._spawn_mission = spawn_mission
        self._notify = notify
        self._audit = audit or (lambda kind, **kw: None)
        self._emit = emit or (lambda payload: None)
        self._is_blocked = is_blocked or (lambda: False)
        self._lock = threading.RLock()
        self.seed_defaults()

    # ------------------------------------------------------------------
    # CRUD

    def _rows(self) -> list[dict]:
        return self._store.goals.data.setdefault("goals", [])

    def _save(self) -> None:
        self._store.goals.save()

    def _find(self, goal_id: str) -> dict | None:
        for g in self._rows():
            if g.get("id") == goal_id:
                return g
        return None

    def list(self, *, include_archived: bool = False) -> list[dict]:
        with self._lock:
            return [dict(g) for g in self._rows()
                    if include_archived or g.get("status") != "archived"]

    def get(self, goal_id: str) -> dict | None:
        with self._lock:
            g = self._find(goal_id)
            return dict(g) if g else None

    def add(self, title: str, *, description: str = "",
            type: str = "reliability", priority: str = "normal",
            metrics: list[dict] | None = None,
            constraints: list[str] | None = None,
            budget: dict | None = None,
            escalation_policy: str = "mission",
            autonomy_profile: str = "local_autonomous",
            review_interval_s: float = DEFAULT_REVIEW_INTERVAL_S,
            mission_cooldown_s: float = DEFAULT_MISSION_COOLDOWN_S,
            continuous: bool = True,
            enabled: bool = True,
            owner: str = "user") -> dict:
        title = str(title or "").strip()
        if not title:
            raise ValueError("goal title required")
        if type not in GOAL_TYPES:
            raise ValueError(f"unknown goal type '{type}'")
        if priority not in GOAL_PRIORITIES:
            raise ValueError(f"unknown goal priority '{priority}'")
        if escalation_policy not in ESCALATION_POLICIES:
            raise ValueError(f"unknown escalation policy '{escalation_policy}'")
        metric_rows = []
        for spec in metrics or []:
            key = str(spec.get("key") or "")
            op = str(spec.get("op") or "<=")
            if not key or op not in _OPS:
                continue
            try:
                target = float(spec.get("target"))
            except (TypeError, ValueError):
                continue
            row: dict[str, Any] = {"key": key, "op": op, "target": target,
                                   "weight": max(0.1, float(spec.get("weight") or 1.0))}
            if spec.get("warn") is not None:
                try:
                    row["warn"] = float(spec["warn"])
                except (TypeError, ValueError):
                    pass
            metric_rows.append(row)
        now = time.time()
        goal = {
            "id": f"g-{uuid.uuid4().hex[:10]}",
            "title": title[:140],
            "description": str(description)[:4000],
            "owner": owner,
            "type": type,
            "priority": priority,
            "status": "active" if enabled else "paused",
            "continuous": bool(continuous),
            "created_at": now,
            "last_evaluated_at": None,
            "next_review_at": now if enabled else None,
            "review_interval_s": max(60.0, float(review_interval_s)),
            "metrics": metric_rows,
            "constraints": [str(c)[:500] for c in (constraints or [])][:20],
            "budget": dict(budget or {}),
            "linked_missions": [],
            "health": "unknown",
            "health_detail": "not yet evaluated",
            "confidence": 0.6,
            "escalation_policy": escalation_policy,
            "autonomy_profile": autonomy_profile,
            "mission_cooldown_s": max(60.0, float(mission_cooldown_s)),
            "last_mission_at": None,
            "last_mission_id": "",
            "evaluations": 0,
            "health_history": [],
            "history": [{"ts": now, "event": "created",
                         "detail": f"owner={owner} type={type}"}],
        }
        with self._lock:
            self._rows().append(goal)
            self._save()
        self._audit("goal_created", goal=goal["id"], title=goal["title"],
                    owner=owner)
        self._emit({"type": "goal_created", "goal": dict(goal)})
        return dict(goal)

    def set_enabled(self, goal_id: str, enabled: bool) -> bool:
        with self._lock:
            g = self._find(goal_id)
            if g is None or g.get("status") == "archived":
                return False
            g["status"] = "active" if enabled else "paused"
            g["next_review_at"] = time.time() if enabled else None
            self._history(g, "enabled" if enabled else "paused")
            self._save()
        return True

    def archive(self, goal_id: str) -> bool:
        with self._lock:
            g = self._find(goal_id)
            if g is None:
                return False
            g["status"] = "archived"
            g["next_review_at"] = None
            self._history(g, "archived")
            self._save()
        self._audit("goal_archived", goal=goal_id)
        return True

    def available_metrics(self) -> list[dict]:
        return self._metrics.available()

    # ------------------------------------------------------------------
    # evaluation

    def evaluate(self, goal_id: str, *, now: float | None = None) -> dict | None:
        """Measure a goal's metrics and update health; may spawn a mission."""
        with self._lock:
            g = self._find(goal_id)
            if g is None:
                return None
        return self._evaluate_one(dict(g), now=now)

    def evaluate_all(self, *, now: float | None = None) -> list[dict]:
        out = []
        for g in self.list():
            if g.get("status") == "active":
                r = self._evaluate_one(g, now=now)
                if r:
                    out.append(r)
        return out

    def _evaluate_one(self, goal: dict, *, now: float | None = None) -> dict:
        now = now or time.time()
        specs = goal.get("metrics") or []
        measured = self._metrics.measure_many(
            [s.get("key", "") for s in specs]) if specs else {}

        metric_results: list[dict] = []
        states: list[str] = []
        for spec in specs:
            m = measured.get(str(spec.get("key")), {"ok": False})
            state = _metric_state(spec, m)
            states.append(state)
            metric_results.append({
                "key": spec.get("key"), "state": state,
                "value": m.get("value"), "op": spec.get("op"),
                "target": spec.get("target"),
                "detail": m.get("detail", "")[:160]})

        live = self._live_linked(goal)
        last_failed = self._last_linked_failed(goal)

        if not specs or all(s == "unknown" for s in states):
            health = "unknown"
            detail = "no measurable metrics" if specs else "no metrics defined"
        elif any(s == "breach" for s in states):
            health = "violated"
            bad = [r["key"] for r in metric_results if r["state"] == "breach"]
            detail = "breached: " + ", ".join(
                f"{r['key']}={r['value']:g} (target {r['op']} {r['target']})"
                for r in metric_results if r["state"] == "breach" and r["value"] is not None) or ", ".join(bad)
        elif any(s == "warn" for s in states) or last_failed:
            health = "degrading"
            warn = [r["key"] for r in metric_results if r["state"] == "warn"]
            parts = [f"warn: {', '.join(warn)}"] if warn else []
            if last_failed:
                parts.append(f"last repair mission failed ({last_failed})")
            detail = "; ".join(parts) or "degrading"
        elif not goal.get("continuous", True):
            health = "satisfied"
            detail = "all metrics satisfied"
        else:
            health = "healthy"
            detail = "all metrics within bounds"

        evaluation = {"ts": now, "health": health, "detail": detail,
                      "metrics": metric_results}
        spawned = None
        suppressed = None
        if health in {"degrading", "violated"}:
            spawned, suppressed = self._escalate(goal, evaluation)

        with self._lock:
            g = self._find(goal["id"])
            if g is None:
                return evaluation
            g["last_evaluated_at"] = now
            g["evaluations"] = int(g.get("evaluations") or 0) + 1
            # "blocked" set during escalation wins over the measured health:
            # the goal is still breached, but remediation cannot act.
            if evaluation.get("blocked"):
                g["health"] = "blocked"
                g["health_detail"] = str(evaluation["blocked"])[:400]
            else:
                g["health"] = health
                g["health_detail"] = detail[:400]
            if health == "satisfied":
                g["status"] = "completed"
                g["next_review_at"] = None
            elif g.get("status") == "active":
                g["next_review_at"] = now + float(
                    g.get("review_interval_s") or DEFAULT_REVIEW_INTERVAL_S)
            hist = g.setdefault("health_history", [])
            hist.append({"ts": now, "health": health, "detail": detail[:200],
                         "metrics": {r["key"]: r["value"] for r in metric_results}})
            del hist[:-30]
            if spawned:
                g["linked_missions"].append(
                    {"id": spawned["id"], "spawned_at": now,
                     "status": spawned.get("status", "ready")})
                g["linked_missions"] = g["linked_missions"][-50:]
                g["last_mission_at"] = now
                g["last_mission_id"] = spawned["id"]
            if suppressed:
                self._history(g, "mission_suppressed", suppressed)
            self._save()
        self._emit({"type": "goal_evaluated", "goal_id": goal["id"],
                    "health": health})
        return evaluation

    # ------------------------------------------------------------------
    # escalation → self-generated repair missions

    def _escalate(self, goal: dict, evaluation: dict) -> tuple[dict | None, str | None]:
        """Spawn a repair mission per the goal's escalation policy.
        Returns (mission_or_none, suppressed_reason_or_none)."""
        policy = str(goal.get("escalation_policy") or "mission")
        if policy == "observe":
            return None, "escalation_policy=observe"
        if policy == "notify":
            self._notify_once(
                goal, "important",
                f"Goal degrading: {goal['title']}",
                evaluation.get("detail") or "")
            return None, "escalation_policy=notify"
        if self._is_blocked():
            self._set_blocked(goal, "autonomy is stopped/paused")
            evaluation["blocked"] = "autonomy is stopped/paused"
            return None, "autonomy stopped"
        live = self._live_linked(goal)
        if live:
            return None, f"mission already live: {live}"
        cooldown = float(goal.get("mission_cooldown_s")
                         or DEFAULT_MISSION_COOLDOWN_S)
        last_at = float(goal.get("last_mission_at") or 0)
        if last_at and time.time() - last_at < cooldown:
            return None, f"cooldown {int(cooldown - (time.time() - last_at))}s left"
        if self._spawn_mission is None:
            return None, "no mission spawner wired"

        evidence = [r for r in evaluation.get("metrics", [])
                    if r["state"] in {"breach", "warn"}]
        ev_text = "; ".join(
            f"{r['key']}={r['value']:g} (limit {r['op']} {r['target']})"
            for r in evidence if r["value"] is not None)[:500]
        mission = self._spawn_mission(goal, {
            "health": evaluation["health"],
            "detail": evaluation.get("detail") or "",
            "metrics": evidence,
            "confidence": float(goal.get("confidence") or 0.5),
        })
        if mission is None:
            self._set_blocked(goal, "mission spawn failed")
            evaluation["blocked"] = "mission spawn failed"
            return None, "spawn failed"
        if mission.get("suppressed"):
            # The spawner refused on learned-failure grounds — the goal
            # stays degraded (not blocked) and history records why.
            return None, f"suppressed: {str(mission['suppressed'])[:120]}"
        self._audit("goal_mission_spawned", goal=goal["id"],
                    mission=mission.get("id"), health=evaluation["health"],
                    evidence=ev_text)
        return mission, None

    def _set_blocked(self, goal: dict, reason: str) -> None:
        with self._lock:
            g = self._find(goal["id"])
            if g is None:
                return
            if g.get("health") != "blocked":
                self._history(g, "blocked", reason[:200])
            g["health"] = "blocked"
            g["health_detail"] = reason[:400]

    def _notify_once(self, goal: dict, level: str, title: str,
                     detail: str) -> None:
        # At most one notification per goal per cooldown window.
        last = self._last_event_ts(goal, "notified")
        now = time.time()
        if last and now - last < float(
                goal.get("mission_cooldown_s") or DEFAULT_MISSION_COOLDOWN_S):
            return
        if self._notify is not None:
            try:
                self._notify(level, title, detail)
            except Exception:
                pass
        with self._lock:
            g = self._find(goal["id"])
            if g is not None:
                self._history(g, "notified", f"{level}: {title}")
                self._save()

    def _last_event_ts(self, goal: dict, event: str) -> float:
        for h in reversed(goal.get("history") or []):
            if h.get("event") == event:
                return float(h.get("ts") or 0)
        return 0.0

    # ------------------------------------------------------------------
    # linked mission feedback

    def _live_linked(self, goal: dict) -> str:
        if self._missions is None:
            return ""
        for link in goal.get("linked_missions") or []:
            m = self._missions.get(str(link.get("id") or ""))
            if m and str(m.get("status")) not in TERMINAL_MISSION_STATUSES:
                return str(m["id"])
        return ""

    def _last_linked_failed(self, goal: dict) -> str:
        if self._missions is None:
            return ""
        links = goal.get("linked_missions") or []
        if not links:
            return ""
        m = self._missions.get(str(links[-1].get("id") or ""))
        if m and str(m.get("status")) == "failed":
            return str(m["id"])
        return ""

    def _resolve_missions(self) -> None:
        """Feed terminal mission outcomes back into goal confidence."""
        if self._missions is None:
            return
        now = time.time()
        with self._lock:
            changed = False
            for g in self._rows():
                for link in g.get("linked_missions") or []:
                    if link.get("resolved"):
                        continue
                    m = self._missions.get(str(link.get("id") or ""))
                    if m is None:
                        link["resolved"] = True
                        link["status"] = "missing"
                        changed = True
                        continue
                    status = str(m.get("status"))
                    if status not in TERMINAL_MISSION_STATUSES:
                        link["status"] = status
                        changed = True
                        continue
                    link["resolved"] = True
                    link["status"] = status
                    link["completed_at"] = m.get("completed_at") or now
                    ok = status in {"completed", "completed_with_warnings"}
                    conf = float(g.get("confidence") or 0.5)
                    g["confidence"] = round(min(1.0, max(0.05,
                        conf + (1 - conf) * _CONF_OK_STEP if ok
                        else conf - conf * _CONF_FAIL_STEP)), 3)
                    outcomes = g.setdefault("mission_outcomes",
                                            {"success": 0, "failure": 0})
                    outcomes["success" if ok else "failure"] += 1
                    self._history(
                        g, "mission_completed" if ok else "mission_failed",
                        f"{link['id']} → {status}")
                    changed = True
            if changed:
                self._save()

    # ------------------------------------------------------------------
    # tick

    def tick(self, now: float | None = None) -> None:
        """Bounded supervisor-tick step: resolve mission outcomes, then
        evaluate goals whose review is due."""
        now = now or time.time()
        try:
            self._resolve_missions()
        except Exception:
            pass
        due = []
        with self._lock:
            for g in self._rows():
                if g.get("status") != "active":
                    continue
                nxt = g.get("next_review_at")
                if nxt is None or float(nxt) <= now:
                    due.append(dict(g))
        for g in due:
            try:
                self._evaluate_one(g, now=now)
            except Exception:
                pass  # one goal's fault must not stall the supervisor

    # ------------------------------------------------------------------
    # helpers

    @staticmethod
    def _history(goal: dict, event: str, detail: str = "") -> None:
        hist = goal.setdefault("history", [])
        hist.append({"ts": time.time(), "event": event,
                     "detail": str(detail)[:300]})
        del hist[:-50]

    def summary(self) -> dict[str, Any]:
        goals = self.list()
        by_health: dict[str, int] = {}
        for g in goals:
            h = str(g.get("health") or "unknown")
            by_health[h] = by_health.get(h, 0) + 1
        return {"goals": len(goals), "by_health": by_health,
                "active": sum(1 for g in goals if g.get("status") == "active")}

    # ------------------------------------------------------------------
    # defaults

    def seed_defaults(self) -> None:
        """First-run builtin goals — the system's own health floor.
        Seeding is initialization, not a runtime event: it must not emit
        bus traffic during AppState construction."""
        with self._lock:
            if self._rows():
                return
        emit, audit = self._emit, self._audit
        self._emit, self._audit = lambda p: None, lambda k, **f: None
        try:
            self.add(
            "Keep Nexus Core healthy during unattended operation",
            description=("Self-managing health floor: crash-free backend, "
                         "bounded mission failures, disk headroom, intact "
                         "state stores."),
            type="infrastructure_health", priority="high",
            owner="system",
            metrics=[
                {"key": "backend_incidents_24h", "op": "<=",
                 "target": 2, "warn": 1},
                {"key": "mission_failure_rate", "op": "<=",
                 "target": 0.5, "warn": 0.3},
                {"key": "disk_free_gb", "op": ">=", "target": 1.0,
                 "warn": 3.0},
                {"key": "corrupt_store_files", "op": "<=", "target": 0},
            ],
            escalation_policy="mission",
            review_interval_s=300.0,
            mission_cooldown_s=1800.0,
        )
        finally:
            self._emit, self._audit = emit, audit
