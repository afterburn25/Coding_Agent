"""AdaptiveWorkerManager — hardware-aware admission control for workers.

Workers are logical execution units, not permanently resident processes.
The manager measures live capacity (ResourceMonitor), subtracts safety
reserves and outstanding reservations, and admits work only when every
required dimension fits:

    schedulable = free − reserved − os_reserve − interactive_reserve

Work that does not fit is placed on a durable queue with a machine +
human-readable reason; completed workers release reservations and the queue
is re-evaluated — no manual start action. Priority aging keeps cheap
runnable tasks from starving behind a heavy job that cannot fit yet
(head-of-line blocking is avoided: a small task may pass a queued whale).

Cost learning: observed usage per (hardware, role) feeds a bounded EMA so
estimates converge on reality. Resource failures (OOM, backend crash, GPU
error, severe contention) lower the admission ceiling; sustained clean
completions raise it back — never instantly.

The LLM may suggest classification, but code + measured telemetry make the
final admission decision. Never ask a model "how many workers".
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..fsutil import atomic_write_text
from .capacity import CapacitySnapshot, ResourceMonitor
from .roles import PRIORITY, ResourceEstimate, classify_role, estimate_for


WORKER_STATES = {
    "idle", "reserved", "starting", "running", "waiting_for_tool",
    "waiting_for_permission", "waiting_for_model", "waiting_for_dependency",
    "paused", "cancelling", "completed", "failed", "interrupted",
    "cancelled",
}
TERMINAL = {"completed", "failed", "interrupted", "cancelled"}

QUEUE_REASONS = {
    "waiting_for_cpu", "waiting_for_ram", "waiting_for_vram",
    "waiting_for_gpu", "waiting_for_worker", "waiting_for_model",
    "waiting_for_dependency", "waiting_for_permission", "ceiling",
}

REASON_TEXT = {
    "waiting_for_cpu": "Waiting for CPU capacity",
    "waiting_for_ram": "Waiting for free memory",
    "waiting_for_vram": "Waiting for GPU memory",
    "waiting_for_gpu": "Waiting for GPU availability",
    "waiting_for_worker": "Waiting for a worker",
    "waiting_for_model": "Waiting for model capacity",
    "waiting_for_dependency": "Waiting for another task to finish",
    "waiting_for_permission": "Waiting for approval",
    "ceiling": "Waiting — worker ceiling reached",
}

# Bounded queues/records — a workstation never accrues unbounded state.
MAX_QUEUE = 200
MAX_HISTORY = 400
# Manager reaping is the backstop only — mission-node liveness is owned by
# the graph lease (300s), and model inference can run silent for minutes.
HEARTBEAT_TIMEOUT_S = 900.0       # worker silent this long → suspect
AGING_GRANT_S = 300.0             # every 5 min queued → +1 effective priority


@dataclass(slots=True)
class QueueEntry:
    """A task awaiting resources. Durable — survives restart."""
    id: str
    title: str
    role: str
    priority: int
    estimate: dict[str, Any]
    reason: str = "waiting_for_worker"
    reason_detail: str = ""
    user_initiated: bool = False
    profile_id: str = ""
    project_id: str = ""
    mission_id: str = ""
    task_id: str = ""
    deps: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.time)
    announced: bool = False         # voice notice spoken for this entry

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkerRecord:
    id: str
    role: str
    task_id: str = ""
    mission_id: str = ""
    project_id: str = ""
    profile_id: str = ""
    title: str = ""
    status: str = "reserved"
    priority: int = 50
    model_tier: str = ""
    exec_mode: str = "local"
    estimate: dict[str, Any] = field(default_factory=dict)
    worktree: str = ""
    branch: str = ""
    phase: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    heartbeat_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    user_initiated: bool = False

    @property
    def elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return round((self.finished_at or time.time()) - self.started_at, 1)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["elapsed_s"] = self.elapsed_s
        return d


class _CostLearner:
    """Bounded EMA of observed resource use per (hardware, role).

    Persisted at data/workers/costs.json. Converges estimates toward
    measured reality without ever shrinking below a safe fraction of the
    static profile (a lucky cheap run must not zero the reservation)."""

    ALPHA = 0.3
    MIN_FRACTION = 0.4

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, float]] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = {k: v for k, v in raw.items()
                          if isinstance(v, dict)}
        except (OSError, ValueError):
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, json.dumps(self._data, indent=1))
        except OSError:
            pass

    def _key(self, hw: str, role: str) -> str:
        return f"{hw or 'unknown'}:{role}"

    def record(self, hw: str, role: str, *, ram_mb: float = 0,
               vram_mb: float = 0, cpu_cores: float = 0,
               duration_s: float = 0) -> None:
        if not any((ram_mb, vram_mb, cpu_cores, duration_s)):
            return
        with self._lock:
            row = self._data.setdefault(self._key(hw, role), {})
            for k, v in (("ram_mb", ram_mb), ("vram_mb", vram_mb),
                         ("cpu_cores", cpu_cores), ("duration_s", duration_s)):
                if v <= 0:
                    continue
                row[k] = (v if k not in row
                          else row[k] * (1 - self.ALPHA) + v * self.ALPHA)
            row["samples"] = row.get("samples", 0) + 1
            self._save()

    def learned(self, hw: str, role: str,
                default: ResourceEstimate) -> ResourceEstimate | None:
        with self._lock:
            row = self._data.get(self._key(hw, role))
            if not row or row.get("samples", 0) < 2:
                return None
        est = ResourceEstimate(**asdict(default))
        if row.get("ram_mb"):
            est.ram_mb = int(max(default.ram_mb * self.MIN_FRACTION,
                                 row["ram_mb"]))
        if row.get("vram_mb"):
            est.vram_mb = int(max(default.vram_mb * self.MIN_FRACTION,
                                  row["vram_mb"]))
        if row.get("cpu_cores"):
            est.cpu_cores = float(max(default.cpu_cores * self.MIN_FRACTION,
                                      row["cpu_cores"]))
        return est


class AdaptiveWorkerManager:
    """Central admission controller. Thread-safe; pure scheduling logic —
    the caller (supervisor/server) owns actual execution.

    ``on_queue_event`` receives semantic events
    (``task_queued`` / ``queued_task_started``) for the voice/activity bus.
    ``model_capacity`` maps tier → concurrent inference slots
    (llama.cpp is typically 1 per resident model)."""

    def __init__(
        self,
        workspace: Path,
        *,
        store_dir: Path | None = None,
        monitor: ResourceMonitor | None = None,
        max_workers: int = 8,                    # absolute safety ceiling
        model_capacity: dict[str, int] | None = None,
        on_queue_event: Callable[[str, dict], None] | None = None,
        interactive_probe: Callable[[], bool] | None = None,
        on_admit: Callable[[WorkerRecord], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        root = Path(store_dir) if store_dir else self.workspace / ".agent"
        root.mkdir(parents=True, exist_ok=True)
        self._queue_path = root / "worker_queue.json"
        self.monitor = monitor or ResourceMonitor(self.workspace)
        self.max_workers = max(1, int(max_workers))
        # llama.cpp class backends serialize inference — default 1 slot for
        # resident tiers, refined by config/measurement.
        self.model_capacity = {"4B": 2, "8B": 1, "14B": 1, "30B": 1}
        self.model_capacity.update(model_capacity or {})
        self._emit = on_queue_event or (lambda t, p: None)
        self._interactive = interactive_probe or (lambda: False)
        # Dispatched when a durable-queue entry is admitted — the server
        # turns the worker's payload into real execution (agent run, job…).
        self.on_admit = on_admit
        self._learner = _CostLearner(root / "worker_costs.json")
        self._lock = threading.RLock()
        self._workers: dict[str, WorkerRecord] = {}
        self._queue: list[QueueEntry] = []
        self._history: list[dict] = []
        # Failure backoff — resource incidents reduce the effective ceiling;
        # clean completions restore it gradually.
        self._ceiling = self.max_workers
        self._fail_streak = 0
        self._clean_streak = 0
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _save_queue(self) -> None:
        try:
            atomic_write_text(self._queue_path, json.dumps(
                {"version": 1,
                 "queue": [e.as_dict() for e in self._queue]}, indent=2))
        except OSError:
            pass

    def _load(self) -> None:
        """Restart reconciliation — queued entries survive; nothing is
        silently dropped. Reservations never survive a process restart."""
        try:
            raw = json.loads(self._queue_path.read_text(encoding="utf-8"))
            entries = [QueueEntry(**{k: v for k, v in e.items()
                                     if k in QueueEntry.__dataclass_fields__})
                       for e in raw.get("queue", [])]
        except (OSError, ValueError, TypeError):
            entries = []
        self._queue = entries[:MAX_QUEUE]

    # ------------------------------------------------------------------
    # reservation accounting
    # ------------------------------------------------------------------

    def _reserved(self) -> dict[str, float]:
        cpu = ram = vram = 0.0
        gpu = 0.0
        with self._lock:
            for w in self._workers.values():
                if w.status in TERMINAL:
                    continue
                e = w.estimate or {}
                cpu += float(e.get("cpu_cores") or 0)
                ram += float(e.get("ram_mb") or 0)
                vram += float(e.get("vram_mb") or 0)
                gpu += float(e.get("gpu") or 0)
        return {"cpu_cores": cpu, "ram_mb": ram, "vram_mb": vram, "gpu": gpu}

    def _model_slots_used(self) -> dict[str, int]:
        used: dict[str, int] = {}
        with self._lock:
            for w in self._workers.values():
                if w.status in TERMINAL or not w.model_tier:
                    continue
                need = int((w.estimate or {}).get("model_slot") or 0)
                if need:
                    used[w.model_tier] = used.get(w.model_tier, 0) + need
        return used

    # ------------------------------------------------------------------
    # admission
    # ------------------------------------------------------------------

    def _fits(self, est: ResourceEstimate, sched: dict[str, float],
              slots_used: dict[str, int]) -> tuple[bool, str, str]:
        """Does this estimate fit schedulable capacity? Returns
        (ok, reason_code, human_detail)."""
        tier = est.model_tier
        if tier and int(est.model_slot or 0):
            cap = int(self.model_capacity.get(tier, 1))
            if slots_used.get(tier, 0) + int(est.model_slot) > cap:
                return (False, "waiting_for_model",
                        f"{tier} inference capacity in use "
                        f"({slots_used.get(tier, 0)}/{cap} slots)")
        if est.cpu_cores > sched.get("cpu_cores", 0):
            return (False, "waiting_for_cpu",
                    f"needs {est.cpu_cores:.1f} CPU cores, "
                    f"{sched.get('cpu_cores', 0):.1f} schedulable")
        if est.ram_mb > sched.get("ram_mb", 0):
            return (False, "waiting_for_ram",
                    f"needs {est.ram_mb/1024:.1f} GB RAM, "
                    f"{sched.get('ram_mb', 0)/1024:.1f} GB schedulable")
        if est.vram_mb > sched.get("vram_mb", 0):
            return (False, "waiting_for_vram",
                    f"needs {est.vram_mb/1024:.1f} GB VRAM, "
                    f"{sched.get('vram_mb', 0)/1024:.1f} GB schedulable")
        if est.gpu > sched.get("gpu", 0):
            return (False, "waiting_for_gpu",
                    f"needs {est.gpu:.0%} GPU, "
                    f"{sched.get('gpu', 0):.0%} schedulable")
        return True, "", ""

    def _estimate(self, role: str, overrides: dict | None) -> ResourceEstimate:
        snap = self.monitor.snapshot()
        default = estimate_for(role)
        learned = self._learner.learned(snap.hardware_id, role, default)
        return estimate_for(role, overrides=overrides, learned=learned
                            or default)

    def submit(self, title: str, *, role: str = "", kind: str = "",
               text: str = "", priority: int | None = None,
               priority_class: str = "mission",
               estimate_overrides: dict | None = None,
               user_initiated: bool = False, profile_id: str = "",
               project_id: str = "", mission_id: str = "",
               task_id: str = "", deps: list[str] | None = None,
               payload: dict | None = None) -> dict[str, Any]:
        """Admit a unit of work or queue it with a reason.

        Returns {"status": "admitted"|"queued", "worker": ...} or
        {"status": "queued", "entry": ..., "reason": ..., "message": ...}."""
        role = classify_role(text or title, kind=kind, hinted=role)
        est = self._estimate(role, estimate_overrides)
        pri = int(priority if priority is not None
                  else PRIORITY.get(priority_class, PRIORITY["mission"]))
        entry = QueueEntry(
            id=f"w-{uuid.uuid4().hex[:10]}", title=str(title)[:140],
            role=role, priority=pri, estimate=est.as_dict(),
            user_initiated=user_initiated, profile_id=profile_id,
            project_id=project_id, mission_id=mission_id,
            task_id=task_id, deps=list(deps or []),
            payload=dict(payload or {}))
        decision = self._try_admit_entry(entry)
        if decision is not None:
            return {"status": "admitted", "worker": decision.as_dict()}
        with self._lock:
            if len(self._queue) >= MAX_QUEUE:
                return {"status": "rejected",
                        "reason": "queue_full",
                        "message": f"Worker queue is full ({MAX_QUEUE})"}
            self._queue.append(entry)
            self._save_queue()
        self._emit("task_queued", {"entry": entry.as_dict(),
                                   "reason": entry.reason,
                                   "message": REASON_TEXT.get(
                                       entry.reason, "Queued"),
                                   "queue_position": self.position(entry.id),
                                   "user_initiated": user_initiated})
        return {"status": "queued", "entry": entry.as_dict(),
                "reason": entry.reason,
                "message": REASON_TEXT.get(entry.reason, "Queued"),
                "queue_position": self.position(entry.id)}

    def _try_admit_entry(self, entry: QueueEntry) -> WorkerRecord | None:
        """Attempt admission; on failure stamp the entry's reason."""
        with self._lock:
            live = [w for w in self._workers.values()
                    if w.status not in TERMINAL]
            # Dependency gate — a queued entry waits until every named task
            # has completed (completed workers live in _history).
            if entry.deps:
                done_ids = {r.get("task_id") for r in self._history
                            if r.get("status") == "completed"}
                pending = [d for d in entry.deps if d not in done_ids]
                if pending:
                    entry.reason, entry.reason_detail = (
                        "waiting_for_dependency",
                        f"waiting on {', '.join(pending[:3])}")
                    return None
            if len(live) >= self._ceiling:
                entry.reason, entry.reason_detail = (
                    "ceiling",
                    f"{len(live)} workers active (safe ceiling "
                    f"{self._ceiling})")
                return None
            res = self._reserved()
            slots = self._model_slots_used()
        sched = self.monitor.schedulable(
            reserved_cpu=res["cpu_cores"], reserved_ram_mb=res["ram_mb"],
            reserved_vram_mb=res["vram_mb"], reserved_gpu=res["gpu"],
            interactive=(self._interactive()
                         and not entry.user_initiated))
        est = ResourceEstimate(**{k: v for k, v in entry.estimate.items()
                                  if k in ResourceEstimate.__dataclass_fields__})
        ok, reason, detail = self._fits(est, sched, slots)
        if not ok:
            entry.reason, entry.reason_detail = reason, detail
            return None
        worker = WorkerRecord(
            id=entry.id, role=entry.role, task_id=entry.task_id,
            mission_id=entry.mission_id, project_id=entry.project_id,
            profile_id=entry.profile_id, title=entry.title,
            status="reserved", priority=entry.priority,
            model_tier=est.model_tier, estimate=est.as_dict(),
            user_initiated=entry.user_initiated)
        with self._lock:
            self._workers[worker.id] = worker
        return worker

    def mark_announced(self, entry_id: str) -> None:
        with self._lock:
            for e in self._queue:
                if e.id == entry_id:
                    e.announced = True
                    self._save_queue()
                    return

    # ------------------------------------------------------------------
    # mission-node admission — the mission DAG is itself durable, so nodes
    # never enter worker_queue.json; they re-present every tick and the
    # manager stamps a queue reason the UI can surface.
    # ------------------------------------------------------------------

    def admit_node(self, task_id: str, title: str, *, role: str = "",
                   kind: str = "", text: str = "", priority: int = 50,
                   mission_id: str = "", project_id: str = "",
                   profile_id: str = "",
                   estimate_overrides: dict | None = None,
                   user_initiated: bool = False) -> tuple[WorkerRecord | None, str, str]:
        """Reserve resources for a mission graph node.

        Returns (worker, reason_code, detail). worker is None when the node
        does not currently fit — reason/detail explain why for the UI."""
        role = classify_role(text or title, kind=kind, hinted=role)
        est = self._estimate(role, estimate_overrides)
        with self._lock:
            live = [w for w in self._workers.values()
                    if w.status not in TERMINAL]
            if len(live) >= self._ceiling:
                return (None, "ceiling",
                        f"{len(live)} workers active (safe ceiling "
                        f"{self._ceiling})")
            res = self._reserved()
            slots = self._model_slots_used()
        sched = self.monitor.schedulable(
            reserved_cpu=res["cpu_cores"], reserved_ram_mb=res["ram_mb"],
            reserved_vram_mb=res["vram_mb"], reserved_gpu=res["gpu"],
            interactive=self._interactive() and not user_initiated)
        ok, reason, detail = self._fits(est, sched, slots)
        if not ok:
            return None, reason, detail
        worker = WorkerRecord(
            id=f"w-{uuid.uuid4().hex[:10]}", role=role, task_id=task_id,
            mission_id=mission_id, project_id=project_id,
            profile_id=profile_id, title=str(title)[:140],
            status="reserved", priority=int(priority),
            model_tier=est.model_tier, estimate=est.as_dict(),
            user_initiated=user_initiated)
        with self._lock:
            self._workers[worker.id] = worker
        return worker, "", ""

    # ------------------------------------------------------------------
    # queue drain — call from supervisor tick or a worker release
    # ------------------------------------------------------------------

    def tick(self) -> list[WorkerRecord]:
        """Admit everything that currently fits, in aged-priority order.
        Head-of-line blocking is avoided: an entry that doesn't fit stays
        queued while a smaller entry behind it may start."""
        admitted: list[WorkerRecord] = []
        while True:
            with self._lock:
                now = time.time()
                ranked = sorted(
                    self._queue,
                    key=lambda e: (e.priority
                                   - int((now - e.enqueued_at)
                                         / AGING_GRANT_S),
                                   e.enqueued_at))
                candidate = None
                for e in ranked:
                    w = self._try_admit_entry(e)
                    if w is not None:
                        candidate = (e, w)
                        break
                if candidate is None:
                    if ranked:
                        # cheapest reason to surface first
                        self._save_queue()
                    return admitted
                entry, worker = candidate
                self._queue.remove(entry)
                self._save_queue()
            admitted.append(worker)
            self._emit("queued_task_started",
                       {"worker": worker.as_dict(),
                        "title": worker.title,
                        "user_initiated": worker.user_initiated})
            if self.on_admit is not None:
                try:
                    self.on_admit(worker)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # worker lifecycle
    # ------------------------------------------------------------------

    def worker_started(self, worker_id: str, *, phase: str = "") -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.status = "running"
                w.started_at = w.started_at or time.time()
                w.heartbeat_at = time.time()
                if phase:
                    w.phase = phase

    def heartbeat(self, worker_id: str, *, phase: str = "") -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w and w.status not in TERMINAL:
                w.heartbeat_at = time.time()
                if phase:
                    w.phase = phase

    def set_status(self, worker_id: str, status: str,
                   *, phase: str = "") -> None:
        if status not in WORKER_STATES:
            return
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.status = status
                if phase:
                    w.phase = phase

    def set_worktree(self, worker_id: str, path: str, branch: str) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.worktree, w.branch = str(path), str(branch)

    def release(self, worker_id: str, *, outcome: str = "completed",
                result: dict | None = None,
                observed: dict | None = None) -> None:
        """Release the reservation; feed the cost learner; re-evaluate the
        queue implicitly via the caller's next tick."""
        with self._lock:
            w = self._workers.get(worker_id)
            if w is None:
                return
            w.status = outcome if outcome in WORKER_STATES else "completed"
            w.finished_at = time.time()
            w.result = result
            row = w.as_dict()
            self._history.append(row)
            self._history = self._history[-MAX_HISTORY:]
            del self._workers[worker_id]
        obs = observed or {}
        never_started = w.started_at is None
        if not never_started:
            self._learner.record(
                self.monitor.snapshot().hardware_id, w.role,
                ram_mb=float(obs.get("ram_mb") or 0),
                vram_mb=float(obs.get("vram_mb") or 0),
                cpu_cores=float(obs.get("cpu_cores") or 0),
                duration_s=float(w.elapsed_s or 0))
        if never_started:
            pass  # released before launch — no telemetry, no streaks
        elif outcome in {"failed", "interrupted"}:
            err = str((result or {}).get("error") or "")
            if any(k in err.lower() for k in
                   ("oom", "out of memory", "vram", "crash", "cuda",
                    "resource")):
                self.record_resource_failure()
            else:
                self._fail_streak += 1
                self._clean_streak = 0
        else:
            self._clean_streak += 1
            self._fail_streak = 0
            # Restore ceiling slowly — one clean streak of 5 → +1.
            if self._ceiling < self.max_workers and self._clean_streak >= 5:
                self._ceiling += 1
                self._clean_streak = 0

    def record_resource_failure(self) -> None:
        """OOM / backend crash / GPU error → immediately reduce safe
        concurrency; recovery is earned through clean completions."""
        with self._lock:
            self._ceiling = max(1, self._ceiling - 1)
            self._clean_streak = 0
        self._emit("worker_capacity_reduced",
                   {"ceiling": self._ceiling})

    def cancel(self, worker_id: str) -> bool:
        with self._lock:
            w = self._workers.get(worker_id)
            if w is None:
                for e in self._queue:
                    if e.id == worker_id:
                        self._queue.remove(e)
                        self._save_queue()
                        return True
                return False
            if w.started_at is None:
                # Reserved but never launched — release the reservation now
                # so a cancelled worker can't hold capacity forever.
                pass
            else:
                w.status = "cancelling"
                return True
        self.release(worker_id, outcome="cancelled")
        return True

    def reconcile(self) -> dict[str, int]:
        """Reap workers whose heartbeat died (crashed/stuck), releasing
        their reservations so queued work can proceed."""
        now = time.time()
        reaped = []
        with self._lock:
            for wid, w in list(self._workers.items()):
                if w.status in TERMINAL:
                    continue
                if w.started_at and now - w.heartbeat_at > HEARTBEAT_TIMEOUT_S:
                    reaped.append(wid)
        for wid in reaped:
            self.release(wid, outcome="failed",
                         result={"error": "worker heartbeat lost"})
        return {"reaped": len(reaped)}

    # ------------------------------------------------------------------
    # introspection — activity view + "why is my task queued"
    # ------------------------------------------------------------------

    def position(self, entry_id: str) -> int:
        with self._lock:
            for i, e in enumerate(self._queue):
                if e.id == entry_id:
                    return i + 1
        return 0

    def explain(self, entry_id: str) -> dict[str, Any]:
        """Human + machine answer to 'why is my task queued?'."""
        with self._lock:
            for e in self._queue:
                if e.id == entry_id:
                    return {"id": e.id, "reason": e.reason,
                            "detail": e.reason_detail,
                            "message": REASON_TEXT.get(e.reason, "Queued"),
                            "queue_position": self.position(e.id),
                            "estimate": e.estimate,
                            "schedulable": self.schedulable_report()}
        return {"id": entry_id, "reason": "not_queued",
                "message": "That task is not in the queue."}

    def schedulable_report(self) -> dict[str, float]:
        res = self._reserved()
        sched = self.monitor.schedulable(
            reserved_cpu=res["cpu_cores"], reserved_ram_mb=res["ram_mb"],
            reserved_vram_mb=res["vram_mb"], reserved_gpu=res["gpu"],
            interactive=self._interactive())
        return {k: round(v, 2) for k, v in sched.items()
                if k != "snapshot"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            live = [w.as_dict() for w in self._workers.values()
                    if w.status not in TERMINAL]
            queue = [dict(e.as_dict(),
                          message=REASON_TEXT.get(e.reason, "Queued"),
                          position=i + 1)
                     for i, e in enumerate(sorted(
                         self._queue,
                         key=lambda e: (e.priority, e.enqueued_at)))]
        snap: CapacitySnapshot = self.monitor.snapshot()
        return {
            "capacity": {
                "ceiling": self._ceiling,
                "max_workers": self.max_workers,
                "active": len(live),
                "safe_now": self._ceiling - len(live),
                "hardware_id": snap.hardware_id,
            },
            "hardware": snap.as_dict(),
            "schedulable": self.schedulable_report(),
            "model_slots": {
                t: {"used": self._model_slots_used().get(t, 0),
                    "capacity": c}
                for t, c in self.model_capacity.items()},
            "workers": live,
            "queue": queue,
            "recent": self._history[-25:],
        }
