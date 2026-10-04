"""Autonomous Supervisor — bounded, event-driven mission engine.

One daemon thread. Each tick is bounded and terminal: fire due triggers and
schedules, reclaim dead leases, step each live mission a bounded amount,
persist after transitions, then sleep on a condition event. There is no
uncontrolled while-loop over work — mission progress is explicit state.

Interactive priority: the supervisor only claims the agent lane when the
foreground chat lane is idle (checked via the injected lane probe), so user
requests always outrank background work.
"""
from __future__ import annotations

import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .budgets import BudgetManager
from .evaluator import EvalVerdict, MissionEvaluator
from .goals import GoalManager, _PRIORITY_TO_MISSION
from .metrics import MetricRegistry
from .missions import MissionStore, TERMINAL_MISSION_STATUSES, DEFAULT_BUDGETS
from .notifications import NotificationCenter
from .planner import MissionPlanner
from .policy import AutonomyPolicy
from .recovery import RecoveryManager, FailureClass
from .scheduler import Scheduler
from .state import AutonomyStore
from .task_graph import ResourceLocks, TaskGraph, new_task
from .triggers import TriggerEngine
from ..self_repair.models import TERMINAL_REPAIR_STATES


class AutonomousSupervisor:
    """Owns all autonomy subsystems; drives missions through bounded ticks."""

    TICK_SECONDS = 5.0

    def __init__(
        self,
        *,
        workspace: Path,
        store_root: Path,
        emit: Callable[[str, dict], None] | None = None,
        bus: Any = None,                     # EventBus for event triggers
        executor: Callable[[dict, dict, Callable | None], dict] | None = None,
        verify_runner: Callable[[dict, dict], dict] | None = None,
        internal_runner: Callable[[dict, dict], dict] | None = None,
        job_runner: Callable[[dict, dict], dict] | None = None,
        research_runner: Callable[[dict, dict], dict] | None = None,
        lane_free: Callable[[], bool] | None = None,
        permission_manager=None,
        activities=None,                   # workflow.ActivityStore — timeline rows
        runtime_hooks: dict[str, Callable] | None = None,
        resources: Callable[[], dict] | None = None,
        quiet_hours: tuple[int, int] | None = None,
        enabled: bool = True,
        metrics: MetricRegistry | None = None,
        repair: Any = None,
        signal_sources: dict | None = None,
        worker_manager: Any = None,
        projects: Any = None,                # ProjectStore — context + history
        preferences: Any = None,             # PreferenceStore — learned overlays
        approval_timeout_seconds: float | Callable[[], float] = 0.0,
    ) -> None:
        self.workspace = Path(workspace)
        self.store = AutonomyStore(store_root)
        # Adaptive Worker Manager — measured-capacity admission control for
        # every mission node (and, via the shared instance, ad-hoc user
        # work). Injected by the server so the queue can reach voice/UI;
        # default-constructed for tests/embedded use.
        if worker_manager is not None:
            self.workers = worker_manager
        else:
            from ..workers import AdaptiveWorkerManager
            self.workers = AdaptiveWorkerManager(
                self.workspace,
                on_queue_event=lambda t, p: self._emit("worker_event",
                                                     {"event": t, **p}))
        self._emit_bus = emit or (lambda t, p: None)
        self.enabled = enabled
        self.projects = projects
        self.preferences = preferences

        self.missions = MissionStore(
            self.store,
            on_change=lambda p: self._emit("mission", p))
        self.locks = ResourceLocks()
        self.recovery = RecoveryManager()
        self.evaluator = MissionEvaluator(self.workspace)
        self.planner = MissionPlanner()
        # Optional cognitive-architecture hook — set by AppState once the
        # Nexus Brain is constructed (supervisor builds first).
        self.pfc = None
        self.budgets = BudgetManager(self.workspace, resources=resources)
        self.policy = AutonomyPolicy(self.store, permission_manager)
        self.notifications = NotificationCenter(
            self.store,
            publish=self._publish_notification,
            quiet_hours=quiet_hours)
        self.scheduler = Scheduler(self.store, on_fire=self._on_schedule_fired)
        self.triggers = TriggerEngine(
            self.store, on_fire=self._on_trigger_fired, workspace=self.workspace)
        # Persistent Goal Manager — durable desired-state layer that
        # evaluates measured metrics on each tick and generates repair
        # missions when a goal degrades (deduped + cooldown-bounded).
        self.metric_registry = metrics or MetricRegistry()
        self.goal_manager = GoalManager(
            self.store, self.metric_registry,
            missions=self.missions,
            spawn_mission=self._spawn_goal_repair_mission,
            notify=lambda level, title, detail: self.notifications.notify(
                title, level=level, detail=detail),
            audit=self._audit,
            emit=lambda p: self._emit("goal", p),
            is_blocked=lambda: self.policy.is_stopped() or
            self.policy.is_paused())
        # Self-repair coordinator — wired by AppState. When a mission's
        # recovery playbook is exhausted the failure becomes a repair
        # incident; on resolution the interrupted mission resumes.
        self.repair = repair
        # Signal scanner — evidence-based problem/opportunity detection.
        # Sources are injected (telemetry is measured, never guessed);
        # routable findings become repair incidents or investigation
        # missions, the rest queue as suggestions.
        from .detectors import SignalScanner
        self.scanner = SignalScanner(
            self.store, sources=dict(signal_sources or {}),
            route=self._route_finding)
        # Procedural memory — terminal outcomes of source-keyed missions
        # feed recall (prior-run context) and suppression (known-failing
        # signatures back off instead of looping on every cooldown).
        from .procedures import ProcedureMemory
        self.procedures = ProcedureMemory(self.store)

        self._executor = executor
        self._verify_runner = verify_runner or self._default_verify
        self._internal_runner = internal_runner or self._default_internal
        self._job_runner = job_runner or self._default_job
        self._research_runner = research_runner or self._default_internal
        self._lane_free = lane_free or (lambda: True)
        self.activities = activities
        self._node_rows: dict[str, str] = {}  # node_id -> activity row id
        self._hooks = dict(runtime_hooks or {})

        self._wake = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None
        self._bus_thread: threading.Thread | None = None
        self._bus_queue = None
        self._bus = bus
        self._workers: dict[str, threading.Thread] = {}  # node_id -> thread
        self._heartbeat = {"ts": 0.0, "tick_ms": 0.0}
        self._started_once = False
        self._gate_lock = threading.RLock()
        self._approval_timeout_seconds = approval_timeout_seconds
        # WorkQueue attribution hook set by the server wiring.
        self._lane_mission: str | None = None

    # ------------------------------------------------------------------
    # lifecycle

    def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="nexus-supervisor", daemon=True)
        self._thread.start()
        if self._bus is not None:
            try:
                self._bus_queue = self._bus.subscribe(replay=0)
                self._bus_thread = threading.Thread(
                    target=self._bus_pump, name="nexus-trigger-pump", daemon=True)
                self._bus_thread.start()
            except Exception:
                self._bus_queue = None
        # Startup triggers fire once per process boot.
        self._started_once = True
        try:
            self.triggers.fire("startup", {"ts": time.time()})
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._bus is not None and self._bus_queue is not None:
            try:
                self._bus.unsubscribe(self._bus_queue)
            except Exception:
                pass
        # Join owned threads so teardown is deterministic — a tick or
        # worker writing after the workspace is deleted is a real bug.
        for t in (self._thread, self._bus_thread,
                  *self._workers.values()):
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=5.0)

    def wake(self) -> None:
        self._wake.set()

    def _bus_pump(self) -> None:
        q = self._bus_queue
        while self._running and q is not None:
            try:
                event = q.get(timeout=2.0)
            except Exception:
                continue
            try:
                before = {t["id"] for t in self.triggers.list()}
                fired = []
                self.triggers.handle_bus_event(event)
                fired = [t for t in self.triggers.list()
                         if t.get("last_fired") and t["id"] in before]
                if fired:
                    self.wake()
            except Exception:
                pass

    def _loop(self) -> None:
        while self._running:
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                pass
            self._heartbeat = {
                "ts": time.time(),
                "tick_ms": round((time.monotonic() - started) * 1000, 1),
            }
            # Wait for the next scheduled deadline or an external wake —
            # never a hot polling loop.
            delay = self.TICK_SECONDS
            try:
                nxt = self.scheduler.next_due()
                if nxt and nxt.get("next_run"):
                    delay = max(1.0, min(self.TICK_SECONDS,
                                         float(nxt["next_run"]) - time.time() + 0.5))
            except Exception:
                pass
            self._wake.wait(timeout=delay)
            self._wake.clear()

    # ------------------------------------------------------------------
    # emission / audit

    def _emit(self, event_type: str, payload: dict) -> None:
        try:
            self._emit_bus(event_type, payload)
        except Exception:
            pass

    def _audit(self, kind: str, **fields: Any) -> None:
        try:
            self.store.audit.append({"ts": time.time(), "kind": kind, **fields})
        except Exception:
            pass

    def _publish_notification(self, payload: dict) -> None:
        """Push a notification onto the event bus and mirror it onto the
        activity timeline so it persists for reloads and mission views."""
        self._emit("notification", payload)
        store = self.activities
        if store is None:
            return
        n = (payload or {}).get("notification") or {}
        mission_id = str(n.get("mission_id") or "") or None
        task_id = f"mission:{mission_id}" if mission_id else "mission:global"
        level = str(n.get("level") or "info")
        try:
            row = store.open(
                task_id, "notification",
                str(n.get("title") or "Notification"),
                str(n.get("message") or "")[:80],
                details={"level": level, "detail": n.get("detail")},
                mission_id=mission_id)
            store.update(task_id, row["id"],
                         state="failed" if level == "failure" else "completed",
                         summary=str(n.get("message") or "")[:200])
        except Exception:
            pass

    def _receipt(self, mission_id: str, node_id: str, action: str,
                 result: dict | None = None) -> None:
        """Action receipt — persisted so recovery/retries can tell whether a
        side effect already landed (idempotency)."""
        try:
            self.store.receipts.append({
                "ts": time.time(), "mission": mission_id, "task": node_id,
                "action": action, "ok": bool((result or {}).get("ok")),
                "detail": str((result or {}).get("output") or "")[:500],
            })
        except Exception:
            pass

    # ------------------------------------------------------------------
    # public control

    def create_mission(self, **fields: Any) -> dict:
        mission = self.missions.create(**fields)
        self.wake()
        return mission

    def start_mission(self, mission_id: str) -> dict | None:
        m = self.missions.transition(mission_id, "ready", detail="started")
        self.wake()
        return m

    def pause_mission(self, mission_id: str, *, reason: str = "user pause") -> dict | None:
        m = self.missions.transition(mission_id, "paused", detail=reason)
        if m is not None:
            self._audit("mission_paused", mission=mission_id, reason=reason)
        self.wake()
        return m

    def resume_mission(self, mission_id: str) -> dict | None:
        m = self.missions.get(mission_id)
        if m is None:
            return None
        target = "active" if (m.get("graph") or {}).get("nodes") else "ready"
        if str(m.get("status")) == "blocked":
            target = "replanning"
        m = self.missions.transition(mission_id, target, detail="resumed")
        self.wake()
        return m

    def cancel_mission(self, mission_id: str) -> dict | None:
        def _fn(m: dict) -> None:
            for n in (m.get("graph") or {}).get("nodes", []):
                if n.get("state") in {"planned", "ready", "waiting_dependency",
                                      "waiting_approval", "blocked"}:
                    n["state"] = "cancelled"
        self.missions.mutate(mission_id, _fn)
        self._close_pending_approvals(mission_id, "cancelled")
        m = self.missions.transition(mission_id, "cancelled", detail="cancelled")
        self._audit("mission_cancelled", mission=mission_id)
        self.wake()
        return m

    def replan_mission(self, mission_id: str, reason: str = "user requested replan") -> dict | None:
        m = self.missions.get(mission_id)
        if m is None:
            return None
        def _fn(row: dict) -> None:
            self._do_replan(row, reason)
        self.missions.mutate(mission_id, _fn)
        m = self.missions.transition(mission_id, "executing", detail="replan")
        self.wake()
        return m

    def stop_autonomy(self) -> dict:
        """Global emergency stop: no new autonomous work; live missions are
        cooperatively paused (in-flight tasks finish their bounded step)."""
        self.policy.set_stopped(True)
        paused = []
        for m in self.missions.list():
            if m.get("status") in {"ready", "active", "planning", "executing",
                                   "verifying", "evaluating", "replanning",
                                   "waiting_dependency"}:
                self.missions.transition(m["id"], "paused",
                                         detail="STOP AUTONOMY")
                paused.append(m["id"])
        self._emit("autonomy", {"event": "stopped", "paused_missions": paused})
        self._audit("autonomy_stop", paused=paused)
        self.wake()
        return {"stopped": True, "paused_missions": paused}

    def resume_autonomy(self) -> dict:
        self.policy.set_stopped(False)
        self.policy.set_paused(False)
        self._emit("autonomy", {"event": "resumed"})
        self.wake()
        return {"stopped": False}

    # ------------------------------------------------------------------
    # standing goals

    def add_standing_goal(self, objective: str, *, trigger: dict | None = None,
                          schedule: dict | None = None,
                          scope: str = "standing",
                          allowed_actions: list[str] | None = None,
                          notification_policy: str = "important",
                          enabled: bool = True,
                          success_criteria: list[dict] | None = None,
                          constraints: list[str] | None = None) -> dict:
        row = {
            "id": f"sg-{uuid.uuid4().hex[:10]}",
            "objective": str(objective)[:2000],
            "enabled": bool(enabled),
            "scope": scope,
            "trigger": dict(trigger or {}),
            "schedule": dict(schedule or {}),      # {kind, interval_s, hour, ...}
            "allowed_actions": list(allowed_actions or []),
            "notification_policy": notification_policy,
            "success_criteria": list(success_criteria or []),
            "constraints": list(constraints or []),
            "last_run": None, "next_run": None, "last_result": None,
            "created_at": time.time(),
        }
        if schedule:
            row["next_run"] = self._goal_next_run(row, after=time.time() - 1)
        self.store.standing_goals.data.setdefault("goals", []).append(row)
        self.store.standing_goals.save()
        # Bind a live trigger record for event-driven goals.
        trig_spec = row["trigger"]
        if enabled and trig_spec.get("event"):
            self.triggers.add(
                f"goal:{row['id']}", trig_spec["event"],
                conditions=trig_spec.get("conditions"),
                action={"kind": "standing_goal", "goal_id": row["id"]},
                debounce_s=float(trig_spec.get("debounce_s", 300)),
                created_by="standing_goal")
        self.wake()
        return dict(row)

    def _goal_next_run(self, goal: dict, *, after: float) -> float | None:
        spec = goal.get("schedule") or {}
        kind = spec.get("kind")
        if kind == "interval":
            return after + float(spec.get("interval_s") or 86400)
        if kind == "daily":
            from .scheduler import _next_daily
            return _next_daily(int(spec.get("hour", 3)), int(spec.get("minute", 0)),
                               after=after)
        if kind == "weekly":
            from .scheduler import _next_daily
            return _next_daily(int(spec.get("hour", 3)), int(spec.get("minute", 0)),
                               weekday=int(spec.get("weekday") or 6), after=after)
        return None

    def set_goal_enabled(self, goal_id: str, enabled: bool) -> bool:
        for g in self.store.standing_goals.data.setdefault("goals", []):
            if g.get("id") == goal_id:
                g["enabled"] = bool(enabled)
                g["next_run"] = self._goal_next_run(g, after=time.time()) if enabled else None
                self.store.standing_goals.save()
                return True
        return False

    def standing_goals(self) -> list[dict]:
        return self.store.standing_goals.rows()

    def _spawn_goal_mission(self, goal: dict, *, origin: str) -> dict:
        mission = self.missions.create(
            objective=goal["objective"],
            title=f"[{goal.get('id')}] {goal['objective'][:90]}",
            scope="standing" if goal.get("scope") == "standing" else goal.get("scope", "one_shot"),
            priority="maintenance",
            success_criteria=goal.get("success_criteria") or
                [{"kind": "all_tasks_completed", "description": "checks complete"}],
            constraints=goal.get("constraints"),
            autonomy_profile="local_autonomous",
            notification_policy=goal.get("notification_policy", "important"),
            source=origin, source_id=goal["id"],
            workspace=str(self.workspace))
        self.missions.transition(mission["id"], "ready",
                                 detail=f"spawned by {origin}")
        return mission

    def _spawn_goal_repair_mission(self, goal: dict, evidence: dict) -> dict | None:
        """Self-generated repair mission for a degraded/violated goal.

        The mission carries the evaluation evidence so every generated unit
        of work is auditable back to the real measured trigger."""
        gid = str(goal.get("id"))
        if self.procedures.failing("goal", gid):
            # Generated repairs keep failing identically — back off
            # rather than re-spawning the same doomed mission each
            # cooldown; the goal evaluation continues reporting health.
            self._audit("goal_mission_suppressed", goal=gid,
                        reason="prior_runs_failing")
            self._emit("goal", {"type": "mission_suppressed",
                                "goal_id": gid})
            return {"suppressed": "prior_runs_failing"}
        metrics_text = "; ".join(
            f"{r['key']}={r.get('value'):g} (limit {r['op']} {r['target']})"
            for r in (evidence.get("metrics") or [])
            if r.get("value") is not None)[:500]
        prior = self.procedures.summary({"source": "goal",
                                         "source_id": gid})
        objective = (
            f"Restore goal '{goal.get('title')}'. "
            f"Health: {evidence.get('health')}. "
            f"Evidence: {metrics_text or evidence.get('detail')}. "
            "Investigate the cause, apply the minimal safe repair, and "
            "verify the fix. "
            + (f"Goal description: {str(goal.get('description'))[:600]}"
               if goal.get("description") else "")
            + (f" {prior}" if prior else ""))
        mission = self.missions.create(
            objective=objective[:3900],
            title=f"[{goal.get('id')}] {str(goal.get('title'))[:80]}",
            scope="one_shot",
            priority=_PRIORITY_TO_MISSION.get(
                str(goal.get("priority") or "normal"), "background"),
            success_criteria=[
                {"kind": "all_tasks_completed",
                 "description": "repair work finished"},
                {"kind": "verify_passed",
                 "description": "verification run passed"}],
            constraints=goal.get("constraints"),
            autonomy_profile=str(goal.get("autonomy_profile")
                                 or "local_autonomous"),
            notification_policy="important",
            source="goal", source_id=str(goal.get("id")),
            created_by="goal_manager",
            workspace=str(self.workspace))
        self.missions.update(mission["id"], goal_id=str(goal.get("id")),
                             trigger_evidence=evidence)
        self.missions.transition(
            mission["id"], "ready",
            detail=f"goal:{goal.get('id')} {evidence.get('health')}")
        return mission

    def _spawn_repair_patch_mission(self, incident: dict,
                                    worktree) -> dict:
        """Code-repair generation: run the normal mission executor with
        the incident's worktree as workspace so the agent patches the
        candidate copy, never the stable tree. The coordinator's own
        verify/review/promote gates then apply to the worktree diff."""
        top = (incident.get("hypotheses") or [{}])[0]
        suspects = "; ".join(
            f"{s['path']}:{s.get('line')}" for s in
            (incident.get("suspects") or [])[:4])
        objective = (
            f"Self-repair incident {incident['id']} — produce a minimal, "
            f"verified fix in THIS workspace (an isolated repair worktree; "
            f"do not touch anything outside it).\n"
            f"Failure: {incident.get('error_class')} in "
            f"{incident.get('subsystem')}\n"
            f"Message: {str(incident.get('error_message'))[:800]}\n"
            f"Hypothesis: {top.get('kind')} — {top.get('detail')}\n"
            f"Suspects: {suspects or 'unlocalized'}\n"
            f"Stack (tail): {str(incident.get('stack_trace'))[-1500:]}\n"
            "Requirements: add or update a regression test that fails "
            "without the fix, apply the minimal patch, run the targeted "
            "test(s). Record what you changed.")
        mission = self.missions.create(
            objective=objective[:3900],
            title=f"Repair {incident['id']}: "
                  f"{str(incident.get('error_class'))[:60]}",
            scope="repository",
            priority="urgent" if incident.get("severity") == "critical"
            else "normal",
            success_criteria=[
                {"kind": "all_tasks_completed",
                 "description": "patch applied in worktree"},
                {"kind": "verify_passed",
                 "description": "targeted tests pass"}],
            autonomy_profile="local_autonomous",
            notification_policy="silent",
            source="self_repair", source_id=str(incident.get("id")),
            created_by="self_repair",
            workspace=str(worktree))
        self.missions.transition(mission["id"], "ready",
                                 detail=f"self-repair:{incident.get('id')}")
        return mission

    # ------------------------------------------------------------------
    # triggers + schedules

    def _on_schedule_fired(self, schedule: dict) -> None:
        self._emit("schedule", {"type": "schedule_fired", "schedule": schedule})
        self._audit("schedule_fired", schedule=schedule.get("id"),
                    name=schedule.get("name"))
        action = schedule.get("action") or {}
        self._materialize_action(action, origin=f"schedule:{schedule.get('id')}")
        self.triggers.fire("schedule_due",
                           {"schedule_id": schedule.get("id"),
                            "name": schedule.get("name", "")})
        self.wake()

    def _on_trigger_fired(self, trigger: dict, payload: dict) -> None:
        self._emit("trigger", {"type": "trigger_fired", "trigger": trigger,
                               "payload": {k: v for k, v in payload.items()
                                           if isinstance(v, (str, int, float, bool))}})
        self._audit("trigger_fired", trigger=trigger.get("id"),
                    signal=payload.get("signal"))
        self._materialize_action(trigger.get("action") or {},
                                 origin=f"trigger:{trigger.get('id')}")
        self.wake()

    def _materialize_action(self, action: dict, *, origin: str) -> None:
        kind = action.get("kind", "mission")
        if self.policy.is_stopped():
            self._audit("materialize_blocked", origin=origin, reason="autonomy stopped")
            return
        if kind == "standing_goal":
            goal = next((g for g in self.standing_goals()
                         if g.get("id") == action.get("goal_id")), None)
            if goal is not None and goal.get("enabled"):
                self._spawn_goal_mission(goal, origin=origin)
            return
        if kind == "mission":
            objective = str(action.get("objective") or "").strip()
            if not objective:
                return
            prior = self.procedures.summary(
                {"source": origin.split(":")[0], "source_id": origin})
            mission = self.missions.create(
                objective=(objective + (f" {prior}" if prior else ""))[:3900],
                title=action.get("title") or objective[:90],
                scope=str(action.get("scope") or "one_shot"),
                priority=str(action.get("priority") or "normal"),
                success_criteria=action.get("success_criteria"),
                constraints=action.get("constraints"),
                autonomy_profile=str(action.get("autonomy_profile") or "local_autonomous"),
                budgets=action.get("budgets"),
                notification_policy=str(action.get("notification_policy") or "important"),
                source=origin.split(":")[0], source_id=origin,
                workspace=str(self.workspace))
            self.missions.transition(mission["id"], "ready", detail=origin)

    # ------------------------------------------------------------------
    # main tick — bounded, terminal, persists after each transition

    def tick(self) -> None:
        if not self.enabled:
            return
        now = time.time()

        # 1. schedules + file watches
        self.scheduler.tick()
        self.triggers.check_watches()

        # 2. standing goals whose schedule came due
        self._tick_goals(now)

        # 2b. evaluated goals — outcome feedback + due reviews; a degraded
        # goal generates a repair mission (deduped, cooldown-bounded)
        self.goal_manager.tick(now)

        # 2c. self-repair — each open incident advances at most one stage
        # per tick so a heavy repair cannot stall the supervisor.
        if self.repair is not None:
            try:
                self.repair.tick(now)
            except Exception:
                pass

        # 2d. signal detection — problems/opportunities from telemetry;
        # routable findings feed repair + missions, the rest surface as
        # suggestions in the findings store.
        try:
            self.scanner.tick(now)
            self.scanner.reconcile(self._finding_done)
        except Exception:
            pass

        # 2e. procedural learning — terminal source-keyed missions teach
        # the next occurrence; idempotent on mission id. Runs even while
        # paused so outcomes that landed mid-pause aren't lost.
        try:
            known = self.procedures.recorded_ids()
            for m in self.missions.list():
                if str(m.get("status")) in TERMINAL_MISSION_STATUSES \
                        and str(m.get("id")) not in known:
                    self.procedures.record_terminal(m)
        except Exception:
            pass

        # 3. reclaim expired leases (worker died mid-task)
        for m in self.missions.list():
            graph = TaskGraph(m)
            for node in graph.reclaim_expired():
                self.missions.append_history(
                    m["id"], "lease_expired",
                    f"task '{node.get('title')}' worker lost — requeued")
                wid = str((node.get("metadata") or {}).get("worker_id") or "")
                if wid:
                    self.workers.release(
                        wid, outcome="interrupted",
                        result={"error": "lease expired — worker lost"})

        # 3b. worker manager housekeeping — reap dead heartbeats, then drain
        # the durable queue into whatever capacity freed up this tick.
        try:
            self.workers.reconcile()
            self.workers.tick()
        except Exception:
            pass

        # 3c. approval housekeeping — expired/missing approval records must
        # not leave a mission parked forever while nobody is watching.
        self._reconcile_approvals(now)

        # 4. drive live missions
        if self.policy.is_stopped() or self.policy.is_paused():
            return
        for m in self.missions.list():
            status = str(m.get("status"))
            if status in TERMINAL_MISSION_STATUSES or status in {
                    "draft", "paused", "blocked", "waiting_trigger",
                    "waiting_approval", "archived"}:
                continue
            try:
                self._step_mission(m["id"])
            except Exception:
                pass  # one mission's fault must not kill the supervisor

    # ------------------------------------------------------------------
    # per-mission step

    def _step_mission(self, mission_id: str) -> None:
        m = self.missions.get(mission_id)
        if m is None or m.get("stop_requested"):
            return
        status = str(m.get("status"))

        # Budget gate first — overspend pauses with a notification.
        budget = self.budgets.check(m)
        if not budget["ok"]:
            self.missions.transition(mission_id, "paused",
                                     detail="; ".join(budget["violations"]))
            self.notifications.notify(
                f"Mission '{m.get('title')}' paused — "
                + "; ".join(budget["violations"]),
                level="important", policy=m.get("notification_policy", "important"),
                mission_id=mission_id, title="Budget exceeded",
                actions=["resume", "edit budget"])
            return

        if status in {"ready", "active"}:
            graph = TaskGraph(m)
            if not graph.nodes:
                self.missions.transition(mission_id, "planning")
                self._build_plan(mission_id)
                return
            self.missions.transition(mission_id, "executing")
            status = "executing"

        if status in {"planning"}:
            self._build_plan(mission_id)
            return

        if status == "executing":
            self._step_executing(mission_id)
            return

        if status == "evaluating":
            self._step_evaluating(mission_id)
            return

        if status == "replanning":
            m = self.missions.get(mission_id)
            if m is not None:
                def _fn(row: dict) -> None:
                    last_fail = next(
                        (n for n in (row.get("graph") or {}).get("nodes", [])
                         if n.get("state") == "failed"), None)
                    self._do_replan(row, "replanning phase", failed_node=last_fail)
                self.missions.mutate(mission_id, _fn)
                self.missions.transition(mission_id, "executing")
            return

        if status == "waiting_dependency":
            # Dependencies resolve into ready on refresh — nothing else waits
            # on an external signal right now.
            self.missions.transition(mission_id, "executing")
            return

    def _build_plan(self, mission_id: str) -> None:
        m = self.missions.get(mission_id)
        if m is None:
            return
        def _fn(row: dict) -> None:
            if (row.get("graph") or {}).get("nodes"):
                return
            # Project-linked missions get a bounded context digest — goals,
            # decisions, blockers — before the planner builds the DAG.
            pid = str(row.get("project_id") or "")
            if pid and self.projects is not None:
                try:
                    ctx = self.projects.context_digest(pid)
                    if self.preferences is not None:
                        overlay = self.preferences.overlay_text(
                            project_id=pid)
                        if overlay:
                            ctx = (ctx + "\n\n" + overlay).strip()
                    row["project_context"] = ctx
                except Exception:
                    row["project_context"] = ""
            tasks = self.planner.initial_plan(row)
            graph = TaskGraph(row)
            for t in tasks:
                graph.add(t)
            row.setdefault("history", []).append({
                "ts": time.time(), "event": "plan",
                "detail": f"{len(tasks)} tasks planned",
            })
            # The PFC mirrors the mission DAG as a cognitive plan — tracked
            # for completion assessment and conflict monitoring.
            if self.pfc is not None:
                try:
                    p = self.pfc.plan(str(row.get("objective") or ""),
                                      mission={**row, "id": mission_id},
                                      project_id=str(self.workspace))
                    row.setdefault("history", []).append({
                        "ts": time.time(), "event": "cognitive_plan",
                        "detail": f"pfc plan {p.plan_id} "
                                  f"confidence={p.confidence}"})
                    row["cognitive_plan_id"] = p.plan_id
                except Exception:
                    pass
            row["attempts"] = int(row.get("attempts") or 0) + 1
        self.missions.mutate(mission_id, _fn)
        self._emit("task_graph", {"type": "task_graph_updated",
                                  "mission_id": mission_id,
                                  "graph": (self.missions.get(mission_id) or {}).get("graph")})
        self.missions.transition(mission_id, "executing", detail="plan built")

    def _step_executing(self, mission_id: str) -> None:
        m = self.missions.get(mission_id)
        if m is None:
            return
        graph = TaskGraph(m)

        # Honor retry cooldowns.
        now = time.time()
        for n in graph.nodes:
            if n.get("state") == "ready" and n.get("retry_after", 0) > now:
                n["state"] = "waiting_dependency"   # parked until cooldown
            elif n.get("state") == "waiting_dependency" \
                    and n.get("retry_after", 0) <= now:
                n["state"] = "ready"

        # Detect newly-failed dependencies → mark failed node's dependents.
        for n in graph.running():
            continue  # running workers update their own state

        runnable = graph.runnable(limit=8)
        started = 0
        # Admission is hardware-measured, not a fixed mode→count map: each
        # node requests a reservation sized by role and the manager admits
        # only when it fits live schedulable capacity. A small per-mission
        # sprawl bound still applies; resource_mode biases it.
        mission_cap = {"conservative": 2, "balanced": 4,
                       "performance": 6}.get(self.policy.resource_mode(), 4)
        in_flight = len(graph.running())
        budget_parallel = max(0, mission_cap - in_flight)

        for node in runnable:
            if started >= budget_parallel:
                break
            kind = node.get("kind", "agent")
            meta = node.get("metadata") or {}
            # Cheap structural gates first — lane/locks/GPU-yield — so a
            # node that can't start never takes a reservation.
            if kind in {"agent", "integrate", "review"}:
                # The agent lane is exclusive and interactive work outranks
                # background missions — except urgent (critical recovery)
                # work, which may interleave between user turns.
                if not self._lane_free() and \
                        str(m.get("priority")) != "urgent":
                    node["queue_reason"] = "waiting_for_worker"
                    node["queue_detail"] = "interactive lane is busy"
                    break
                if not self.locks.acquire("agent_lane", node["id"]):
                    node["queue_reason"] = "waiting_for_worker"
                    node["queue_detail"] = "agent lane in use"
                    break
            elif kind == "job" and str(meta.get("job") or "") == "image" \
                    and not self.budgets.may_use_gpu(
                        m, foreground_busy=not self._lane_free(),
                        resource_mode=self.policy.resource_mode()):
                # GPU-bound background work yields to the interactive lane
                # in conservative mode — stays ready for the next tick.
                node["queue_reason"] = "waiting_for_gpu"
                node["queue_detail"] = "yielding to interactive use"
                continue
            else:
                lock = node.get("lock") or ""
                if lock and not self.locks.acquire(lock, node["id"]):
                    node["queue_reason"] = "waiting_for_worker"
                    node["queue_detail"] = f"resource lock: {lock}"
                    continue
            # Adaptive admission — reserve before start so several nodes
            # can never observe the same free RAM/VRAM and overcommit.
            worker, w_reason, w_detail = self.workers.admit_node(
                str(node.get("id") or ""),
                str(node.get("title") or node.get("instruction") or ""),
                role=str(meta.get("worker_role") or ""),
                kind=kind,
                text=str(node.get("instruction") or ""),
                priority=int(node.get("priority", 50)),
                mission_id=str(m.get("id") or ""),
                project_id=str(meta.get("project_id") or ""),
                profile_id=str(m.get("profile_id") or ""),
                estimate_overrides=meta.get("estimate"))
            if worker is None:
                node["queue_reason"] = w_reason
                node["queue_detail"] = w_detail
                if node.get("lock") or kind in {"agent", "integrate", "review"}:
                    self.locks.release(
                        node.get("lock") or "agent_lane", node["id"])
                continue
            node.pop("queue_reason", None)
            node.pop("queue_detail", None)
            meta["worker_id"] = worker.id
            node["metadata"] = meta
            if not graph.claim(node["id"], owner=f"supervisor"):
                if node.get("lock") or kind in {"agent", "integrate", "review"}:
                    self.locks.release(
                        node.get("lock") or "agent_lane", node["id"])
                self.workers.release(worker.id, outcome="cancelled")
                continue
            started += 1
            self._spawn_worker(mission_id, node["id"], worker_id=worker.id)

        if started:
            self.missions.update(mission_id, graph=graph.graph)
            self._emit("task_graph", {"type": "task_graph_updated",
                                      "mission_id": mission_id,
                                      "graph": graph.graph})
        elif not runnable and not graph.running():
            # Nothing left to run — evaluate goal progress, or recover
            # nodes stranded behind a failed dependency.
            if graph.is_done():
                self.missions.transition(mission_id, "evaluating")
            else:
                failed = [n for n in graph.nodes if n.get("state") == "failed"]
                stuck = [n for n in graph.nodes
                         if n.get("state") in {"planned", "blocked"}]
                if failed and stuck:
                    # Replan around the failed dep — repoints the stranded
                    # nodes onto a fresh recovery path.
                    self.missions.transition(mission_id, "replanning",
                                             detail="dependency failed")
                elif stuck:
                    self.missions.transition(mission_id, "blocked",
                                             detail="tasks stuck on failed dependencies")
                    self.notifications.notify(
                        f"Mission '{m.get('title')}' blocked — dependencies failed.",
                        level="failure",
                        policy=m.get("notification_policy", "important"),
                        mission_id=mission_id, title="Mission blocked",
                        actions=["replan", "resume"])
                else:
                    self.missions.transition(mission_id, "evaluating")

    def _step_evaluating(self, mission_id: str) -> None:
        m = self.missions.get(mission_id)
        if m is None:
            return
        result = self.evaluator.evaluate(m)
        verdict = result["verdict"]
        if verdict == EvalVerdict.COMPLETE.value:
            self._complete_mission(mission_id, warnings=False)
        elif verdict == EvalVerdict.NEEDS_USER.value:
            self.missions.transition(mission_id, "waiting_approval")
            self._create_approval(
                mission_id, "",
                {"name": "mission_decision", "kind": "mission",
                 "detail": "; ".join(result.get("reasons") or
                                      ["mission needs user direction"])})
        elif verdict == EvalVerdict.NEEDS_REPLAN.value:
            reason = self.recovery.budgets_exceeded(m)
            if reason:
                self.missions.transition(mission_id, "blocked", detail=reason)
                self.notifications.notify(
                    f"Mission '{m.get('title')}' blocked: {reason}",
                    level="failure",
                    policy=m.get("notification_policy", "important"),
                    mission_id=mission_id, title="Mission blocked",
                    actions=["replan"])
            else:
                def _fn(row: dict) -> None:
                    last_fail = next(
                        (n for n in (row.get("graph") or {}).get("nodes", [])
                         if n.get("state") == "failed"), None)
                    self._do_replan(row, "; ".join(result["reasons"]),
                                    failed_node=last_fail)
                self.missions.mutate(mission_id, _fn)
                self.missions.transition(mission_id, "executing",
                                         detail="auto-replan")
        else:
            self.missions.transition(mission_id, "executing")

    def _do_replan(self, mission: dict, reason: str,
                   failed_node: dict | None = None) -> None:
        graph = TaskGraph(mission)
        if failed_node is None:
            failed_node = next(
                (n for n in graph.nodes if n.get("state") == "failed"), None)
        tasks = self.planner.replan(mission, failed_node, reason)
        # Mirror the replan in the PFC's working plan when one was recorded.
        if self.pfc is not None and mission.get("cognitive_plan_id"):
            try:
                self.pfc.replan(str(mission["cognitive_plan_id"]),
                                reason=reason[:300])
            except Exception:
                pass
        new_ids = []
        for t in tasks:
            graph.add(t)
            new_ids.append(t["id"])
        if failed_node is not None:
            # Repoint the failed node's dependents onto the new verify tail
            # and skip the dead node so the graph can progress.
            tail = new_ids[-1] if new_ids else failed_node["id"]
            for n in graph.dependents(failed_node["id"]):
                n["deps"] = [tail]
                if n.get("state") == "blocked":
                    n["state"] = "planned"
            failed_node["state"] = "skipped"
        # Supersede dead-end nodes the new plan replaces — a blocked node
        # can never reschedule (nothing clears it outside this repoint
        # path), and pending nodes on dead deps can never become ready.
        # Left in place they hold all_tasks_completed unmet forever — the
        # mission can never complete even when the new plan succeeds.
        dead_states = {"failed", "cancelled", "blocked", "skipped"}
        for n in graph.nodes:
            if n["id"] in new_ids or n.get("state") == "running":
                continue
            state = n.get("state")
            if state == "blocked":
                n["state"] = "skipped"
            elif state in {"planned", "ready", "waiting_dependency",
                           "waiting_approval"} and any(
                    (dep := graph.get(d)) is not None
                    and dep.get("state") in dead_states
                    for d in (n.get("deps") or [])):
                n["state"] = "skipped"
        graph.refresh()

    def _complete_mission(self, mission_id: str, *, warnings: bool) -> None:
        m = self.missions.get(mission_id)
        if m is None:
            return
        nodes = (m.get("graph") or {}).get("nodes", [])
        completion = {
            "mission": mission_id,
            "state": "completed_with_warnings" if warnings else "completed",
            "criteria": (self.evaluator.evaluate(m) or {}).get("criteria", []),
            "tasks": [{"id": n["id"], "title": n.get("title"),
                       "state": n.get("state")} for n in nodes],
            "changes": [a for a in m.get("artifacts", [])][:50],
            "elapsed_s": round(time.time() - (m.get("started_at") or
                                              m.get("created_at") or 0), 1),
            "finished_at": time.time(),
        }
        def _fn(row: dict) -> None:
            row["completion"] = completion
            if self.pfc is not None and row.get("cognitive_plan_id"):
                try:
                    completion["cognitive"] = self.pfc.assess_completion(
                        str(row["cognitive_plan_id"]))
                except Exception:
                    pass
        self.missions.mutate(mission_id, _fn)
        self.missions.transition(
            mission_id,
            "completed_with_warnings" if warnings else "completed",
            detail="success criteria satisfied")
        self._receipt(mission_id, "mission", "mission_completed",
                      {"ok": True, "output": "criteria satisfied"})
        self._audit("mission_completed", mission=mission_id,
                    warnings=warnings)
        pid = str(m.get("project_id") or "")
        if pid and self.projects is not None:
            try:
                self.projects.log_activity(
                    pid, "mission_completed",
                    detail=(f"{m.get('title')} — "
                            + ("with warnings" if warnings else "clean")))
            except Exception:
                pass
        self.notifications.notify(
            f"Mission complete: {m.get('title')}",
            level="completion",
            policy=m.get("notification_policy", "important"),
            mission_id=mission_id, title="Mission completed")
        self._learn_from_mission(mission_id)
        self._emit("mission", {"type": "mission_completed",
                               "mission": self.missions.get(mission_id)})

    def _learn_from_mission(self, mission_id: str) -> None:
        """Feed durable lessons: structured mission outcomes only, never
        hidden reasoning."""
        m = self.missions.get(mission_id)
        if m is None:
            return
        lesson = {
            "ts": time.time(), "mission": mission_id,
            "objective": str(m.get("objective"))[:300],
            "outcome": m.get("status"),
            "failures": len(m.get("failure_history") or []),
            "repair_loops": m.get("repair_loops", 0),
            "tasks": len((m.get("graph") or {}).get("nodes", [])),
        }
        try:
            self.store.lessons.append(lesson)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # worker execution

    def _spawn_worker(self, mission_id: str, node_id: str,
                      worker_id: str | None = None) -> None:
        if worker_id:
            self.workers.worker_started(worker_id)

        def work() -> None:
            try:
                self._run_node(mission_id, node_id)
            except Exception as exc:
                self._finish_node(mission_id, node_id,
                                  {"ok": False,
                                   "output": f"{type(exc).__name__}: {exc}"})
            finally:
                self._workers.pop(node_id, None)
                self.wake()
        t = threading.Thread(target=work, name=f"mission-{node_id}", daemon=True)
        self._workers[node_id] = t
        try:
            t.start()
        except Exception as exc:
            # Never leak a dead entry — _workers is the live-worker census
            # and a stranded id would misreport forever. The node still gets
            # a terminal result so the mission can recover/replan.
            self._workers.pop(node_id, None)
            self._finish_node(mission_id, node_id,
                              {"ok": False,
                               "output": f"worker spawn failed: {type(exc).__name__}: {exc}"})

    def _run_node(self, mission_id: str, node_id: str) -> None:
        m = self.missions.get(mission_id)
        node = TaskGraph(m).get(node_id) if m else None
        if m is None or node is None:
            return
        kind = node.get("kind", "agent")
        owner = node_id  # same identity used by acquire() in _step_executing

        def emit(event: dict) -> None:
            try:
                wid = str((node.get("metadata") or {}).get("worker_id") or "")
                if wid:
                    self.workers.heartbeat(
                        wid, phase=str(event.get("phase")
                                       or event.get("title") or ""))
                e = dict(event)
                e.setdefault("mission_id", mission_id)
                e.setdefault("mission_node", node_id)
                self._emit_bus(str(e.pop("type", "task") or "task"), e)
            except Exception:
                pass

        self._node_activity_open(m, node)
        try:
            if kind in {"agent", "integrate", "review"}:
                if self._executor is None:
                    self._finish_node(mission_id, node_id,
                                      {"ok": False, "output": "no executor configured"})
                    return
                # Mark the mission as owning the agent lane while the run is
                # in-flight (queue_task enrichment hooks read this).
                self._lane_mission = mission_id
                try:
                    result = self._executor(m, node, emit)
                finally:
                    self._lane_mission = None
            elif kind == "verify":
                result = self._verify_runner(m, node)
            elif kind == "internal":
                result = self._internal_runner(m, node)
            elif kind == "research":
                result = self._research_runner(m, node)
            elif kind == "job":
                result = self._job_runner(m, node)
            elif kind == "wait":
                result = self._default_wait(m, node)
            else:
                # Unknown kind — fail loudly so recovery/replanning engages
                # rather than reporting a phantom success.
                result = {"ok": False,
                          "output": f"unknown node kind: {kind}",
                          "error": f"unknown node kind: {kind}"}
        finally:
            lock = node.get("lock") or ("agent_lane" if kind == "agent" else "")
            if lock:
                self.locks.release(lock, owner)
        self._finish_node(mission_id, node_id, result or {"ok": False})

    def _node_activity_open(self, mission: dict, node: dict) -> None:
        """Mirror a mission node onto the shared task timeline (Devin-style)."""
        store = self.activities
        if store is None:
            return
        try:
            mtitle = str(mission.get("title") or mission.get("objective") or "mission")
            row = store.open(
                f"mission:{mission.get('id')}", "task_graph",
                str(node.get("title") or "task"),
                f"{mtitle[:80]} · {node.get('kind', 'agent')}",
                details={"mission_id": mission.get("id"),
                         "node_id": node.get("id"),
                         "kind": node.get("kind")},
                mission_id=mission.get("id"),
            )
            self._node_rows[str(node.get("id"))] = row["id"]
        except Exception:
            pass

    def _node_activity_close(self, mission_id: str, node_id: str, result: dict) -> None:
        store = self.activities
        row_id = self._node_rows.pop(str(node_id), None)
        if store is None or row_id is None:
            return
        try:
            task_id = f"mission:{mission_id}"
            if result.get("pending_approval"):
                store.update(
                    task_id, row_id, state="waiting",
                    summary="Waiting for approval",
                    details={"pending_approval": result.get("pending_approval")})
            elif result.get("ok"):
                store.update(task_id, row_id, state="completed",
                             summary=str(result.get("output") or "")[:200])
            else:
                store.update(task_id, row_id, state="failed",
                             summary=str(result.get("output") or result.get("error") or "failed")[:200])
        except Exception:
            pass

    def _finish_node(self, mission_id: str, node_id: str, result: dict) -> None:
        self._node_activity_close(mission_id, node_id, result)
        m = self.missions.get(mission_id)
        if m is None:
            return
        # Release the worker's resource reservation — the run ended (even
        # when the node will retry, the retry re-admits fresh). A node
        # parked on approval frees its slot; resumption re-admits too.
        wid = str((TaskGraph(m).get(node_id) or {})
                  .get("metadata", {}).get("worker_id") or "")
        if wid:
            obs = (result or {}).get("observed") or {}
            self.workers.release(
                wid,
                outcome=("waiting_for_permission" if result.get(
                    "pending_approval") else
                    "completed" if result.get("ok") else "failed"),
                result={"error": str((result or {}).get("error")
                                     or (result or {}).get("output") or "")[:300]},
                observed=obs)
        ok = bool(result.get("ok"))
        pending_approval = result.get("pending_approval")
        # Project-linked missions record each finished node on the
        # project's worker history — "what did Nexus do on this project".
        pid = str(m.get("project_id") or "")
        if pid and self.projects is not None and not pending_approval:
            try:
                meta = node.get("metadata") or {}
                self.projects.record_task(pid, {
                    "task_id": node.get("id"), "worker_id": wid,
                    "title": node.get("title"),
                    "status": "completed" if ok else "failed",
                    "branch": str(meta.get("branch") or ""),
                    "files": list(meta.get("files") or [])})
            except Exception:
                pass

        def _fn(row: dict) -> None:
            graph = TaskGraph(row)
            node = graph.get(node_id)
            if node is None:
                return
            if pending_approval:
                node["state"] = "waiting_approval"
                node["result"] = result
                row["pending_approval"] = pending_approval
                row["waiting_for"] = "approval"
                return
            node["result"] = {
                "ok": ok,
                "output": str(result.get("output") or "")[:4000],
                "task_id": str(result.get("task_id") or ""),
                "artifacts": list(result.get("artifacts") or [])[:20],
                "finished_at": time.time(),
            }
            if ok:
                node["state"] = "completed"
                if node.get("kind") == "verify":
                    row.setdefault("verification_history", []).append({
                        "ts": time.time(), "ok": True,
                        "task": node.get("title"),
                        "output": str(result.get("output") or "")[:800]})
            else:
                node["retries"] = int(node.get("retries") or 0) + 1
                if node["retries"] >= int(node.get("max_retries") or 0):
                    node["state"] = "failed"
                else:
                    node["state"] = "ready"
                    # Bounded backoff — no instant hot retry.
                    node["retry_after"] = time.time() + min(
                        120.0, 10.0 * node["retries"])
                if node["state"] == "failed":
                    self.recovery.record_failure(
                        row, node.get("title", ""),
                        str(result.get("output") or "task failed"))
                if node.get("kind") == "verify":
                    row.setdefault("verification_history", []).append({
                        "ts": time.time(), "ok": False,
                        "task": node.get("title"),
                        "output": str(result.get("output") or "")[:800]})
            # Checkpoint — a bounded progress trail so a restart mid-mission
            # leaves forensic state: which node finished, when, and how.
            cps = row.setdefault("checkpoints", [])
            cps.append({"ts": time.time(), "node": node.get("id"),
                        "kind": node.get("kind"),
                        "state": node.get("state"),
                        "title": str(node.get("title") or "")[:120],
                        "plan_version": row.get("plan_version")})
            del cps[:-80]
            graph.refresh()

        self.missions.mutate(mission_id, _fn)
        self._receipt(mission_id, node_id, "task_finished", result)

        if pending_approval:
            self.missions.transition(mission_id, "waiting_approval")
            self._create_approval(mission_id, node_id, pending_approval)
            return

        m2 = self.missions.get(mission_id)
        node2 = TaskGraph(m2).get(node_id) if m2 else None
        if not ok and node2 is not None and node2.get("state") == "failed":
            # Node exhausted its retries — run the recovery playbook.
            self._recover_node(mission_id, node_id)
        self.wake()

    def _recover_node(self, mission_id: str, node_id: str) -> None:
        m = self.missions.get(mission_id)
        if m is None:
            return
        failures = m.get("failure_history") or []
        failure = failures[-1] if failures else None
        if failure is None:
            return
        reason = self.recovery.budgets_exceeded(m)
        if reason:
            self.missions.transition(mission_id, "blocked", detail=reason)
            self.notifications.notify(
                f"Mission '{m.get('title')}' blocked: {reason}",
                level="failure",
                policy=m.get("notification_policy", "important"),
                mission_id=mission_id, title="Mission blocked",
                actions=["replan", "resume"])
            return
        step = self.recovery.next_step(m, failure)
        if step is None or step.get("action") == "escalate":
            self.missions.transition(mission_id, "blocked",
                                     detail=f"recovery playbook exhausted ({failure.get('class')})")
            self.notifications.notify(
                f"Mission '{m.get('title')}' needs help — recovery exhausted for "
                f"{failure.get('class')}. Last error: {str(failure.get('error'))[:200]}",
                level="failure",
                policy=m.get("notification_policy", "important"),
                mission_id=mission_id, title="Mission blocked — needs user",
                actions=["replan", "cancel"])
            self._audit("recovery_exhausted", mission=mission_id,
                        failure_class=failure.get("class"))
            # Hand the failure to self-repair: deterministic playbooks are
            # exhausted, so the incident pipeline localizes, repairs, and
            # — on success — resumes this mission instead of leaving it
            # permanently blocked.
            if self.repair is not None:
                try:
                    inc, _disp = self.repair.report_failure(
                        source="mission", subsystem="mission",
                        exc_type=str(failure.get("class") or ""),
                        error_message=str(failure.get("error") or
                                          f"{failure.get('class')} in mission"),
                        mission_id=mission_id)
                    if inc is not None:
                        inc["interrupted_operation"] = {
                            "kind": "mission", "mission_id": mission_id}
                        self.repair._save()
                except Exception:
                    pass  # repair intake must never break recovery
            return
        action = step.get("action")
        self._audit("recovery_step", mission=mission_id, node=node_id,
                    action=action, failure_class=failure.get("class"))
        self._emit("recovery", {"type": "recovery_started",
                                "mission_id": mission_id, "node": node_id,
                                "action": action})
        self._apply_recovery_step(m, node_id, step, failure)
        self.missions.update(mission_id)  # persist playbook cursor

    def _apply_recovery_step(self, m: dict, node_id: str,
                             step: dict, failure: dict) -> None:
        action = str(step.get("action"))
        mission_id = m["id"]

        def requeue(delay: float = 0.0) -> None:
            def _fn(row: dict) -> None:
                graph = TaskGraph(row)
                node = graph.get(node_id)
                if node is not None and node.get("state") == "failed":
                    node["state"] = "ready"
                    node["retry_after"] = time.time() + max(0.0, delay)
                    node["retries"] = 0  # playbook retry resets the per-node counter
            self.missions.mutate(mission_id, _fn)

        if action == "retry":
            requeue(float(step.get("delay") or 0))
        elif action == "subtask_fix":
            self.missions.mutate(
                mission_id,
                lambda row: self._do_replan(
                    row, f"playbook fix for {failure.get('class')}",
                    failed_node=TaskGraph(row).get(node_id)))
        elif action == "request_approval":
            pend = {"kind": "autonomy", "mission_id": mission_id,
                    "node_id": node_id,
                    "detail": failure.get("error", "")[:500]}
            self._create_approval(mission_id, node_id, pend)
            self.missions.transition(mission_id, "waiting_approval")
        elif action in {"pause"}:
            self.missions.transition(mission_id, "paused",
                                     detail=f"recovery: {failure.get('class')}")
        elif action in {"stop"}:
            self.missions.transition(mission_id, "cancelled",
                                     detail=f"recovery stop ({failure.get('class')})")
        elif action == "notify":
            self.notifications.notify(
                f"Mission '{m.get('title')}': {failure.get('error', '')[:300]}",
                level="important",
                policy=m.get("notification_policy", "important"),
                mission_id=mission_id)
        elif action == "wait_connectivity":
            requeue(120.0)
        elif action in self._hooks:
            try:
                outcome = self._hooks[action]()
                self._audit("recovery_hook", mission=mission_id,
                            action=action, result=str(outcome)[:200])
            except Exception as exc:
                self._audit("recovery_hook_failed", mission=mission_id,
                            action=action, error=f"{type(exc).__name__}: {exc}")
            # Hooks don't requeue on their own — advance to next step on next tick
        elif action in {"inspect_failure", "inspect_logs", "fetch_logs",
                        "research"}:
            # Diagnostic steps funnel into a fix subtask — the agent does
            # the actual diagnosis inside the replan path.
            self.missions.mutate(
                mission_id,
                lambda row: self._do_replan(
                    row, f"{action} for {failure.get('class')}",
                    failed_node=TaskGraph(row).get(node_id)))
        else:
            # Unknown/hook-less step (fallback_model, redownload, …) —
            # treat as escalate to stay bounded.
            self.missions.transition(mission_id, "blocked",
                                     detail=f"no handler for recovery step '{action}'")

    def _route_finding(self, finding: dict) -> str:
        """Send a routable detector finding to its sink; returns the
        created object's id (or '' when nothing was created)."""
        route = str(finding.get("route") or "suggestion")
        if route == "repair" and self.repair is not None:
            ev = dict(finding.get("evidence") or {})
            # Long blob evidence (CI log tails) feeds the localizer via
            # stack_trace instead of being dumped into the message.
            stack = str(ev.pop("log_tail", "") or "")
            inc, _ = self.repair.report_failure(
                source=f"detector:{finding.get('kind')}",
                subsystem=str(finding.get("kind") or "system"),
                exc_type=str(finding.get("kind") or ""),
                error_message=f"{finding.get('title')}. "
                              f"{finding.get('detail') or ''} "
                              f"evidence={ev}",
                stack_trace=stack)
            if inc:
                self._emit("finding", {"type": "finding_routed",
                                       "finding": finding.get("id"),
                                       "route": "repair",
                                       "target": inc.get("id")})
            return str(inc.get("id")) if inc else ""
        if route == "mission":
            # Dedupe: one live investigation mission per signature.
            sig = str(finding.get("signature") or "")
            for m in self.missions.list():
                if str(m.get("source")) == "detector" and \
                        str(m.get("source_id")) == sig and \
                        str(m.get("status")) not in TERMINAL_MISSION_STATUSES:
                    return str(m.get("id"))
            if self.procedures.failing("detector", sig):
                # Same signature keeps failing identically — a 1 h
                # cooldown re-spawn is a loop, not recovery. Back off;
                # the finding reconciles away and the next sighting
                # re-evaluates after the back-off.
                self._audit("finding_suppressed",
                            finding=finding.get("id"), signature=sig,
                            reason="prior_runs_failing")
                self._emit("finding", {"type": "finding_suppressed",
                                       "finding": finding.get("id")})
                return "suppressed"
            ev = "; ".join(f"{k}={v}" for k, v in
                           (finding.get("evidence") or {}).items()
                           if not isinstance(v, (dict, list)))[:600]
            priority = {"critical": "urgent", "high": "normal"}.get(
                str(finding.get("severity")), "background")
            prior = self.procedures.summary(
                {"source": "detector", "source_id": sig})
            mission = self.missions.create(
                objective=(
                    f"Investigate: {finding.get('title')}. "
                    f"Evidence: {ev or finding.get('detail')}. "
                    "Find the root cause, fix it if safely fixable, and "
                    "verify. If it needs a decision or external action, "
                    "report findings instead of acting."
                    + (f" {prior}" if prior else ""))[:3900],
                title=f"[{finding.get('kind')}] "
                      f"{str(finding.get('title'))[:80]}",
                scope="one_shot", priority=priority,
                success_criteria=[{"kind": "all_tasks_completed",
                                   "description": "investigation done"}],
                autonomy_profile="local_autonomous",
                notification_policy="important",
                source="detector", source_id=sig,
                created_by="signal_scanner",
                workspace=str(self.workspace))
            self.missions.transition(
                mission["id"], "ready",
                detail=f"detector:{finding.get('kind')}")
            self._audit("finding_routed", finding=finding.get("id"),
                        mission=mission["id"])
            self._emit("finding", {"type": "finding_routed",
                                   "finding": finding.get("id"),
                                   "route": "mission",
                                   "target": mission["id"]})
            return str(mission["id"])
        return ""

    def _finding_done(self, target_id: str) -> bool:
        """True when a finding's routed target reached a terminal state —
        repair incident resolved/rolled back/abandoned, or the
        investigation mission finished. Missing targets count as done so
        their findings can close instead of lingering 'acted' forever."""
        if self.repair is not None:
            inc = self.repair.get(target_id)
            if inc is not None:
                return str(inc.get("state")) in TERMINAL_REPAIR_STATES
        m = self.missions.get(target_id)
        if m is not None:
            return str(m.get("status")) in TERMINAL_MISSION_STATUSES
        return True

    def resume_interrupted(self, op: dict) -> None:
        """Self-repair resolved → restart the work it interrupted."""
        if str(op.get("kind")) != "mission":
            return
        mid = str(op.get("mission_id") or "")
        m = self.missions.get(mid)
        if m and str(m.get("status")) in {"blocked", "failed"}:
            self.missions.transition(
                mid, "replanning",
                detail="self-repair resolved — resuming mission")
            self.wake()

    # ------------------------------------------------------------------
    # approvals (autonomy-level)

    def _create_approval(self, mission_id: str, node_id: str,
                       pending: dict) -> dict:
        now = time.time()
        row = {
            "id": f"ap-{uuid.uuid4().hex[:10]}",
            "mission_id": mission_id,
            "node_id": node_id,
            "action": str(pending.get("name") or pending.get("kind") or "action"),
            "detail": str(pending.get("detail") or "")[:800],
            "state": "pending",
            "created_at": now,
            "resolved_at": None,
        }
        # One actionable gate per mission — a newer request supersedes an
        # orphaned prior row instead of leaving two divergent decisions.
        with self.store.approvals._lock:
            for old in self.store.approvals.data.setdefault("approvals", []):
                if (old.get("mission_id") == mission_id
                        and old.get("state") == "pending"):
                    old["state"] = "superseded"
                    old["resolved_at"] = now
            self.store.approvals.data["approvals"].append(row)
            self.store.approvals.save()
        self.notifications.notify(
            f"Approval needed — {row['action']}: {row['detail'][:200]}",
            level="approval", policy="all",
            mission_id=mission_id, title="Approval required",
            actions=["approve", "deny"])
        self._emit("mission", {"type": "mission_blocked",
                               "mission_id": mission_id,
                               "reason": "approval_required"})
        return row

    def approvals(self, *, pending_only: bool = False) -> list[dict]:
        rows = self.store.approvals.rows()
        if pending_only:
            rows = [r for r in rows if r.get("state") == "pending"]
        return rows[::-1]

    def _close_pending_approvals(self, mission_id: str,
                                 state: str = "closed") -> None:
        """Resolve stale gate rows when the owning mission ends/cancels."""
        try:
            now = time.time()
            with self.store.approvals._lock:
                changed = False
                for row in self.store.approvals.data.setdefault(
                        "approvals", []):
                    if (row.get("mission_id") == mission_id
                            and row.get("state") == "pending"):
                        row["state"] = state
                        row["resolved_at"] = now
                        changed = True
                if changed:
                    self.store.approvals.save()
        except Exception:
            pass

    def _approval_timeout(self) -> float:
        raw = self._approval_timeout_seconds
        if callable(raw):
            try:
                raw = raw()
            except Exception:
                raw = 0.0
        try:
            return max(0.0, float(raw or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _pending_approval_for(self, mission_id: str) -> dict | None:
        def created(row: dict) -> float:
            try:
                return float(row.get("created_at") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        rows = [r for r in self.store.approvals.rows()
                if r.get("mission_id") == mission_id
                and r.get("state") == "pending"]
        return max(rows, key=created, default=None)

    def _approval_retry_limit(self, mission: dict) -> int:
        try:
            return max(0, int((mission.get("budgets") or {}).get(
                "max_approval_retries", 2)))
        except (TypeError, ValueError):
            return 2

    def _reconcile_approvals(self, now: float) -> None:
        """Expire or unstick approval-parked missions.

        ``waiting_approval`` is durable, but the approval row must exist and
        unattended runs need a bound. A missing row (older retention,
        corrupt store, interrupted write) is treated as an unresolvable gate
        and replans around the suspended node rather than silently freezing
        the mission.
        """
        try:
            timeout = self._approval_timeout()
            for m in self.missions.list():
                if str(m.get("status")) != "waiting_approval":
                    continue
                pending = self._pending_approval_for(str(m.get("id") or ""))
                if pending is None:
                    node = next(
                        (n for n in (m.get("graph") or {}).get("nodes", [])
                         if n.get("state") == "waiting_approval"),
                        None)
                    self._approval_replan_or_block(
                        str(m.get("id") or ""),
                        str((node or {}).get("id") or ""),
                        "approval record missing — resuming via replan",
                        event="approval_missing")
                    continue
                try:
                    created = float(pending.get("created_at") or now)
                except (TypeError, ValueError):
                    created = now
                if timeout > 0 and now - created >= timeout:
                    pending["state"] = "timed_out"
                    pending["resolved_at"] = now
                    self.store.approvals.save()
                    self._approval_replan_or_block(
                        str(m.get("id") or ""),
                        str(pending.get("node_id") or ""),
                        f"approval timed out after {int(timeout)}s",
                        event="approval_timeout")
        except Exception:
            pass

    def _approval_replan_or_block(self, mission_id: str, node_id: str,
                                  reason: str, *, event: str) -> None:
        """Resolve a denied/expired gate by replanning, bounded per mission."""
        m = self.missions.get(mission_id)
        if m is None:
            return
        limit = self._approval_retry_limit(m)
        exhausted = False

        def _fn(row: dict) -> None:
            nonlocal exhausted
            row["approval_retries"] = int(row.get("approval_retries") or 0) + 1
            row["pending_approval"] = None
            row["waiting_for"] = ""
            graph = TaskGraph(row)
            node = graph.get(node_id) if node_id else next(
                (n for n in graph.nodes
                 if n.get("state") == "waiting_approval"), None)
            exhausted = row["approval_retries"] >= max(1, limit)
            if exhausted:
                if node is not None:
                    node["state"] = "blocked"
                row["blocked_reason"] = (
                    f"approval retry budget exhausted ({limit}) — {reason}")
                graph.refresh()
            else:
                self._do_replan(row, reason, failed_node=node)

        self.missions.mutate(mission_id, _fn)
        self._audit(event, mission=mission_id, node=node_id, reason=reason)
        if exhausted:
            self.missions.transition(mission_id, "blocked",
                                     detail=reason)
            self.notifications.notify(
                f"Mission '{m.get('title')}' blocked: {reason}",
                level="failure",
                policy=m.get("notification_policy", "important"),
                mission_id=mission_id, title="Mission approval stalled",
                actions=["replan", "resume"])
        else:
            self.missions.transition(mission_id, "executing",
                                     detail=f"{reason} — replanning")
            self.notifications.notify(
                f"Mission '{m.get('title')}' needs another route: {reason}",
                level="important",
                policy=m.get("notification_policy", "important"),
                mission_id=mission_id, title="Approval not granted")
        self.wake()

    def resolve_approval(self, approval_id: str, approve: bool) -> dict | None:
        with self.store.approvals._lock:
            target = None
            for r in self.store.approvals.data.setdefault("approvals", []):
                if r.get("id") == approval_id:
                    target = r
                    break
            if target is None or target.get("state") != "pending":
                return None
            target["state"] = "approved" if approve else "denied"
            target["resolved_at"] = time.time()
            self.store.approvals.save()
        mission_id = target.get("mission_id")
        node_id = target.get("node_id")
        if approve:
            def _fn(row: dict) -> None:
                graph = TaskGraph(row)
                node = graph.get(node_id)
                if node is not None and node.get("state") == "waiting_approval":
                    node["state"] = "ready"   # resume exact action
                row["pending_approval"] = None
                row["waiting_for"] = ""
            self.missions.mutate(mission_id, _fn)
            self.missions.transition(mission_id, "executing",
                                     detail="approval granted")
        else:
            self._approval_replan_or_block(
                str(mission_id or ""), str(node_id or ""),
                "approval denied", event="approval_denied")
        self._audit("approval_resolved", approval=approval_id,
                    approved=approve, mission=mission_id)
        self.wake()
        return dict(target)

    # ------------------------------------------------------------------
    # verify/internal runners

    def _default_verify(self, mission: dict, node: dict) -> dict:
        """Run verification — detected project checks, or the node's
        explicit command. Permission-gated via the policy engine."""
        from ..workflow.verify import detect_verification_commands
        decision = self.policy.check(
            "run_tests", profile=str(mission.get("autonomy_profile") or "local_autonomous"),
            scope=str(mission.get("workspace") or ""))
        if decision == "deny":
            return {"ok": False, "output": "shell verification denied by policy"}
        if decision == "ask":
            return {"ok": False,
                    "pending_approval": {"name": "run_tests",
                                         "kind": "autonomy",
                                         "detail": node.get("instruction", "")[:300]}}
        command = str(node.get("metadata", {}).get("command") or "")
        name = "verify"
        generated_cmd = bool(command)
        if not command:
            cmds = detect_verification_commands(self.workspace)
            if not cmds:
                return {"ok": True, "output": "no verification commands detected"}
            command = cmds[0]["command"]
            name = cmds[0]["name"]
        if generated_cmd or node.get("metadata", {}).get("sandbox"):
            # Node-supplied commands are generated code — run them inside the
            # sandbox (Job memory cap + kill-on-close + timeout + clean env)
            # with the repo as cwd, not a raw host shell.
            try:
                from ..sandbox import Sandbox
                with Sandbox() as sb:
                    res = sb.run_shell(
                        command, timeout=900,
                        allow_network=bool(node.get("metadata", {}).get("network")),
                        cwd=self.workspace)
                tail = (res.get("stdout") or "")[-3000:] + (res.get("stderr") or "")[-1500:]
                if res.get("timed_out"):
                    return {"ok": False, "output": f"{name}: timed out (sandboxed)"}
                return {"ok": bool(res.get("ok")),
                        "output": f"{name} [sandboxed]: rc={res.get('exit')}\n{tail}"}
            except Exception as exc:
                return {"ok": False, "output": f"{name}: sandbox failed — {exc}"}
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(self.workspace),
                capture_output=True, text=True, timeout=900,
                errors="replace")
            ok = proc.returncode == 0
            tail = (proc.stdout or "")[-3000:] + (proc.stderr or "")[-1500:]
            return {"ok": ok, "output": f"{name}: rc={proc.returncode}\n{tail}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": f"{name}: timed out after 900s"}
        except OSError as exc:
            return {"ok": False, "output": f"{name}: {exc}"}

    def _default_internal(self, mission: dict, node: dict) -> dict:
        instr = str(node.get("instruction") or "")
        if instr.startswith("internal:artifact_exists:"):
            raw = instr.split("internal:artifact_exists:", 1)[1].strip()
            p = Path(raw)
            if not p.is_absolute():
                p = self.workspace / raw
            try:
                p = p.resolve()
                p.relative_to(self.workspace.resolve())
                ok = p.exists()
            except (OSError, ValueError):
                ok = False
            return {"ok": ok, "output": f"{raw}: {'exists' if ok else 'missing'}"}
        if instr.startswith("internal:maintenance"):
            try:
                from .maintenance import run_light_maintenance
                return run_light_maintenance(self.workspace, self.store)
            except Exception as exc:
                return {"ok": False, "output": f"maintenance: {exc}"}
        return {"ok": True, "output": "internal task acknowledged"}

    def _default_job(self, mission: dict, node: dict) -> dict:
        op = str((node.get("metadata") or {}).get("job") or "")
        if op == "rag_update":
            # Standalone-capable op — the server's runner reuses the shared
            # index instance; without one, build a transient index handle.
            try:
                from ..rag import RepoIndex
                idx = RepoIndex(self.workspace,
                                db_path=self.workspace / ".agent"
                                / "rag_index.db")
                try:
                    r = idx.update()
                    return {"ok": True,
                            "output": f"index: +{r.get('added', 0)} added, "
                                      f"{r.get('updated', 0)} updated, "
                                      f"{r.get('removed', 0)} removed"}
                finally:
                    idx.close()
            except Exception as exc:
                return {"ok": False, "output": f"rag_update failed: {exc}"}
        return {"ok": False,
                "output": f"job node has no runner wired (metadata.job={op!r})"}

    _ACTIVE_MISSION_STATES = frozenset(
        {"active", "planning", "executing", "verifying", "evaluating",
         "replanning", "waiting_dependency"})

    def _default_wait(self, mission: dict, node: dict) -> dict:
        """Bounded delay node — sleeps metadata.seconds (or until an
        epoch in metadata.until), capped at 1h, and aborts early when the
        mission leaves an executing-family state (pause/cancel/stop)."""
        meta = dict(node.get("metadata") or {})
        secs = float(meta.get("seconds") or 0)
        until = float(meta.get("until") or 0)
        if until > 0:
            secs = max(0.0, until - time.time())
        secs = max(0.0, min(secs, 3600.0))
        end = time.time() + secs
        while time.time() < end and self._running:
            cur = self.missions.get(str(mission.get("id") or ""))
            if cur and cur.get("status") not in self._ACTIVE_MISSION_STATES:
                return {"ok": False,
                        "output": f"wait aborted — mission {cur['status']}"}
            time.sleep(min(1.0, end - time.time()))
        waited = round(secs - max(0.0, end - time.time()))
        return {"ok": True, "output": f"waited {waited}s"}

    # ------------------------------------------------------------------
    # standing-goal ticking

    def _tick_goals(self, now: float) -> None:
        changed = False
        for g in self.store.standing_goals.data.setdefault("goals", []):
            if not g.get("enabled"):
                continue
            nxt = g.get("next_run")
            if nxt is None:
                continue
            if now >= float(nxt):
                g["last_run"] = now
                g["next_run"] = self._goal_next_run(g, after=now)
                changed = True
                try:
                    self._spawn_goal_mission(g, origin=f"goal-schedule:{g['id']}")
                except Exception:
                    pass
        if changed:
            self.store.standing_goals.save()

    # ------------------------------------------------------------------
    # status / reporting

    def status(self) -> dict:
        missions = self.missions.list()
        live = [m for m in missions if m.get("status") not in
                TERMINAL_MISSION_STATUSES | {"archived", "draft"}]
        heartbeat_age = time.time() - self._heartbeat.get("ts", 0.0) \
            if self._heartbeat.get("ts") else None
        return {
            "enabled": self.enabled,
            "running": self._running and self._thread is not None
            and self._thread.is_alive(),
            "stopped": self.policy.is_stopped(),
            "paused": self.policy.is_paused(),
            "resource_mode": self.policy.resource_mode(),
            "heartbeat_age_s": round(heartbeat_age, 1) if heartbeat_age else None,
            "tick_ms": self._heartbeat.get("tick_ms"),
            "active_missions": len(live),
            "missions": [ {"id": m["id"], "title": m.get("title"),
                           "status": m.get("status"), "phase": m.get("phase")}
                          for m in live[:20] ],
            "pending_approvals": len(self.approvals(pending_only=True)),
            "unread_notifications": self.notifications.pending_count(),
            "locks": self.locks.snapshot(),
            "workers": len(self._workers),
            "worker_manager": (self.workers.status()
                               if self.workers is not None else {}),
            "next_schedule": self.scheduler.next_due(),
            "store": self.store.health(),
        }

    def daily_summary(self, *, hours: float = 24.0) -> dict:
        cutoff = time.time() - hours * 3600
        missions = self.missions.list(include_archived=False)
        recent = [m for m in missions if (m.get("updated_at") or 0) >= cutoff]
        return {
            "window_hours": hours,
            "missions_completed": sum(1 for m in recent
                                      if m.get("status") in
                                      {"completed", "completed_with_warnings"}),
            "missions_failed": sum(1 for m in recent if m.get("status") == "failed"),
            "missions_blocked": [m["title"] for m in missions
                                 if m.get("status") == "blocked"][:10],
            "tasks_completed": sum(
                1 for m in recent
                for n in (m.get("graph") or {}).get("nodes", [])
                if n.get("state") == "completed"),
            "pending_approvals": [a["action"] for a in
                                  self.approvals(pending_only=True)][:10],
            "next_scheduled": self.scheduler.next_due(),
            "notifications": self.notifications.pending_count(),
        }

    def answer_about_mission(self, mission_id: str, question: str) -> str:
        """Mission conversation: answers come from persisted state, never
        guesses."""
        m = self.missions.get(mission_id)
        if m is None:
            return "Mission not found."
        q = str(question or "").lower()
        nodes = (m.get("graph") or {}).get("nodes", [])
        if any(w in q for w in ("why", "blocked", "stuck")):
            if m.get("status") == "blocked":
                return (f"Mission is blocked: {m.get('blocked_reason') or ''} "
                        + "; ".join(str(f.get('error'))[:150] for f in
                                    (m.get('failure_history') or [])[-2:]))
            fails = (m.get("failure_history") or [])[-3:]
            return ("Not currently blocked. " +
                    (f"Recent failures: " + "; ".join(
                        str(f.get('error'))[:120] for f in fails) if fails
                     else "No recorded failures."))
        if any(w in q for w in ("tried", "attempted", "what have")):
            hist = [h for h in (m.get("history") or [])[-12:]]
            lines = [f"{h.get('event')}: {h.get('detail')}" for h in hist]
            return "Mission activity:\n" + "\n".join(lines)
        if any(w in q for w in ("left", "remaining", "next")):
            open_nodes = [n for n in nodes if n.get("state") in
                          {"planned", "ready", "running", "waiting_dependency"}]
            if not open_nodes:
                return "No open tasks — awaiting evaluation."
            return "Remaining work:\n" + "\n".join(
                f"- [{n.get('state')}] {n.get('title')}" for n in open_nodes[:10])
        return (f"Mission '{m.get('title')}' is {m.get('status')} "
                f"(phase {m.get('phase')}). "
                f"{sum(1 for n in nodes if n.get('state') == 'completed')}"
                f"/{len(nodes)} tasks completed.")
