# Adaptive Worker Manager

`localcodeagent/workers/` — hardware-aware admission control for every unit
of work Nexus runs. Workers are **logical execution units**, not
permanently-resident model processes; the safe worker count is computed
from live hardware, never configured by hand.

## Pipeline

```
request → classify_role() → ResourceEstimate
        → AdaptiveWorkerManager.admit / submit
          ├─ fits → reservation + WorkerRecord → run
          └─ doesn't fit → durable queue + reason code
completions → release reservation → queue re-evaluated → next fit starts
```

## Capacity model

`capacity.ResourceMonitor` samples live hardware (cached ~3 s):
CPU logical cores + utilization, RAM total/free, GPU VRAM total/free +
utilization, workspace disk free. nvidia-smi is used when present; Windows
registry fallback supplies dedicated VRAM otherwise. CPU% comes from
psutil when importable, else GetSystemTimes (Windows) / `/proc/stat`
(Linux) — no hard psutil dependency.

Schedulable capacity per dimension:

```
free − already-reserved − OS reserve − interactive reserve (when busy)
```

Reserves (`capacity.Reserves`) are conservative defaults: 4 GB RAM,
1 GB VRAM, 25% of CPU, 8 GB disk, plus an interactive-lane top-up
(+2 GB RAM, +15% CPU) applied to background admissions while the user is
active. Learner adjustments never go below the floors.

## Roles and estimates

`roles.ROLE_PROFILES` maps each role to a ResourceEstimate
(cpu_cores, ram_mb, vram_mb, gpu, io, model_tier, model_slot):

| role         | tier | notes                                    |
|--------------|------|------------------------------------------|
| coding       | 14B  | primary workhorse                        |
| reasoning    | 14B  | reasoning/planning-heavy                 |
| research     | 8B   | light model + IO                         |
| diagnostics  | 8B   | light                                    |
| build_test   | —    | CPU/IO heavy, no model slot              |
| tool         | —    | light CPU/IO                             |
| image        | —    | ComfyUI VRAM-heavy                       |
| self_repair  | 14B  | repair workload                          |
| reviewer     | 30B  | deep review — biggest footprint          |
| integrator   | 8B   | merges + conflict detection              |
| maintenance  | —    | cheap background upkeep                  |
| planning     | 8B   | decomposition                            |

`classify_role()` makes a deterministic first pass (keywords → role);
planner metadata (`worker_role`) overrides it. **An LLM may assist with
semantics but code + telemetry make the final admission decision.**

## Admission

`submit()` → `admitted` (reservation held) or `queued` with a reason:
`waiting_for_cpu` / `waiting_for_ram` / `waiting_for_vram` /
`waiting_for_gpu` / `waiting_for_worker` / `waiting_for_model` /
`waiting_for_dependency` / `waiting_for_permission` / `ceiling`.
Each reason carries a human message ("Waiting for GPU memory") surfaced
via `/api/workers/explain?id=…`.

`admit_node()` is the mission-graph path: the mission DAG is itself
durable, so nodes aren't duplicated into the worker queue — the reason is
stamped on the node (`queue_reason` / `queue_detail`).

### Model slots

`model_capacity` maps tier → concurrent inference slots (llama.cpp-class
backends serialize — default `4B:2, 8B:1, 14B:1, 30B:1`; configurable via
`worker_model_slots`). A worker needing a slot that is in use queues as
`waiting_for_model`.

### Queue behavior

- Durable: `~/.agent/worker_queue.json` — survives restart; reservations
  never do.
- Fit-aware: a queued whale never blocks a small task that fits.
- Priority + aging: lower `priority` runs first; every 5 min queued
  grants +1 effective priority so heavy work can't starve forever.
- Capped: `MAX_QUEUE = 200`.
- `tick()` drains admitted entries; `on_admit` dispatches them for
  execution (server routes prompt payloads into the normal work queue).

## Lifecycle and failure safety

WorkerRecord exposes id/role/task/project/profile/status/priority/
model_tier/estimate/worktree/branch/phase/heartbeat/elapsed.

- `worker_started` / `heartbeat` / `set_status` / `release`
- Release happens on completion, failure, cancel, lease-expiry reclaim,
  and restart reconciliation — a reservation can never outlive its worker.
- `reconcile()` reaps workers whose heartbeat died (900 s backstop; the
  mission-graph lease at 300 s is the primary liveness check).
- `record_resource_failure()` (OOM / backend crash / GPU error) lowers the
  safe ceiling by one immediately; five clean completions earn it back.
  Never auto-restores instantly.

## Cost learning

`_CostLearner` (`~/.agent/worker_costs.json`) keeps a bounded EMA of
observed ram_mb/vram_mb/cpu_cores/duration per (hardware_fingerprint,
role). Estimates converge on measured reality but never shrink below 40%
of the static profile — a lucky cheap run can't zero a reservation.
Keyed by hardware fingerprint so numbers never leak across machines.

## Mission integration

`AutonomousSupervisor._step_executing` admits each runnable DAG node
through the shared manager before claiming it. Structural gates (agent
lane, resource locks, conservative-mode GPU yield) still apply first so a
node that can't start never holds a reservation. `self.workers.status()`
is surfaced in `/api/autonomy/status` (`worker_manager`) and `/api/workers`.

## Worktree isolation

`workers/worktree.py` wraps `multiagent.WorktreeAgent`: each coding worker
gets `.nexus/worktrees/<worker-id>` on a traceable
`nexus/<worker-id>/<slug>` branch — parallel coding workers never share a
checkout and never commit to main directly. `SHARED_HOTSPOTS` (VERSION,
CHANGELOG, lockfiles, router, installer) flags integration-owned files;
`detect_scope_overlap` pre-flags workers claiming the same paths;
`reconcile_worktrees` sweeps stranded checkouts at boot while keeping
surviving branches for recovery.

## Voice + events

Semantic events on the `worker` SSE channel: `task_queued`,
`queued_task_started`, `worker_capacity_reduced`. User-initiated queueing
speaks a persona-styled notice **once per item** through
`voice.enqueue` (mute-respecting); queue polling, retries and background
admissions are silent.

## API

- `GET /api/workers` — capacity, live hardware, schedulable, model slots,
  active workers, queue with reasons, recent history
- `GET /api/workers/queue` — queue entries + positions + messages
- `GET /api/workers/explain?id=…` — "why is my task queued?"
- `POST /api/workers/submit` — user-initiated work (`{prompt, mode}`)
- `POST /api/workers/cancel` — `{id}` cancels a queued entry or worker
- `GET /api/projects` / `POST /api/projects` — durable project store
- `GET /api/projects/<id>` (`?summary`) — detail + structured memory
  summary; `POST /api/projects/<id>/goal` and `/remember`
- `GET /api/nexus/avatar` (`?size=`) — canonical portrait derivative;
  `/api/nexus/avatar/status` — identity + expression state

## Projects

`projects.ProjectStore` — durable per-profile projects with goals,
milestones, decisions, blockers, structured project memory
(`architecture / decisions / completed_work / known_issues / blockers /
next_steps`), worker history and activity. Profile-scoped: Profile B
never sees Profile A's projects.

## Config

- `worker_ceiling` (default 8) — absolute safety ceiling, not a target
- `worker_model_slots` — per-tier inference concurrency overrides

## Deliberate non-goals (this milestone)

- `agent`-kind mission nodes still serialize on the agent lane — the
  shared orchestrator isn't provably multi-session safe; parallel coding
  uses worktree `job` nodes or future per-worker sessions.
- Integrator/reviewer are admitted worker *roles*; full merge semantics
  land on top of `merge_back`/`detect_scope_overlap`.
- Self-update pipeline is a documented flow, not implemented execution.
