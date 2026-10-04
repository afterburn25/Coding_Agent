# Nexus Autonomy — Persistent Mission Manager

Nexus Autonomy turns Nexus Core from a request/response assistant into a
persistent agent platform: durable missions, a dependency-aware task DAG,
a bounded event-driven supervisor, schedules/triggers, recovery playbooks,
policy gates, and restart-safe state.

## Storage

All autonomy state lives under `data/autonomy/` (schema version **1**):

| File | Contents |
|---|---|
| `missions.json` | Mission records (bounded history tail; live/actionable rows are never evicted) — objective, status, task graph, budgets, criteria, history |
| `standing_goals.json` | Persistent goals that spawn missions on schedule/trigger |
| `triggers.json` | Event triggers with conditions + debounce |
| `schedules.json` | once/interval/daily/weekly schedules (missed runs fire on next tick) |
| `grants.json` | Standing permission grants — scoped, expirable, revocable |
| `approvals.json` | Approval ledger — pending rows are preserved past the history bound and survive restart |
| `notifications.json` | Notification outbox (bounded 300) |
| `control.json` | Global autonomy stop/pause flags + resource mode |
| `receipts.jsonl` | Action receipts for idempotency/audit |
| `audit.jsonl` | Full audit stream of every autonomy decision |

Corrupt stores are quarantined to `*.corrupt-<ts>` instead of crashing.

## Mission lifecycle

```
draft → ready → planning → executing → evaluating → completed
                       ↑          │                    └ completed_with_warnings
                       └─ replanning ←──┘
paused / waiting_approval / waiting_dependency / blocked / failed / cancelled
```

Transitions are explicit and validated — a mission can never jump from
`draft` straight to `completed`.

## Supervisor

`AutonomousSupervisor.tick()` is the single bounded step: reclaim expired
leases → tick schedules → poll file watches → tick standing goals →
step each live mission. There is no uncontrolled `while` loop; the loop
thread wakes on the event bus and on a heartbeat.

Execution flow per mission:

```
observe → plan (MissionPlanner) → build DAG → claim runnable nodes
(with leases + resource locks, only when the interactive lane is free)
→ run via executor → verify → evaluate (MissionEvaluator)
→ complete / replan / escalate
```

Interactive user work always wins the agent lane — background missions
yield (`lane_free` gate).

## Task DAG

`TaskGraph` stores nodes with `deps`, `kind`, `retry`/`timeout` policy,
risk level, and lease records. Independent branches run concurrently;
failed dependencies mark dependents `blocked`; lease expiry requeues a
node (bounded by `max_task_retries`). Cycles are rejected.

`ResourceLocks` provides exclusive named lanes (`workspace_write`,
`agent_lane`, ...) so parallel nodes never collide on shared resources.

Node kinds: `agent` (executor on the agent lane), `verify`
(project-detected or generated checks — generated commands run inside a
Sandbox), `research` (the real ResearchCoordinator — repo/local-docs
first, web per policy), `internal` (server-side helpers), `wait`, and
`job` — asynchronous platform work dispatched via the wired job runner.
`job` metadata selects the operation: `sandbox` (bounded command with
the workspace as cwd), `backup` (versioned state backup), `rag_update`
(incremental index refresh; works standalone via a transient
`RepoIndex`), `image` (submit an `ImageRequest` and await the job's
terminal state — produced files are registered as artifacts with
knowledge-graph provenance), and `model_install` (download a model —
an LLM catalog id via `runtime.model_catalog`, else an image-model
profile's download job — and run it to completion), and `worktree`
(provision an isolated git worktree via `WorktreeAgent`, run the
sandboxed `command`/`argv` inside it, commit, and merge back — a
nonzero exit or merge conflict never touches the base tree; conflicts
keep the agent branch for manual resolution). Every `job` op is
recorded in the unified `JobManager` ledger (`kind: mission_job`,
carrying `mission_id`/`node_id`) so `/api/jobs` and the Jobs UI show
mission work alongside other platform jobs. Unknown node kinds fail
loudly rather than completing silently. Repository/workspace missions
automatically prepend a `rag_update` job node so inspection sees a
fresh index. `integrate` and `review` are convergence kinds —
`integrator`/`reviewer` worker roles executed through the agent lane.

### Decomposition + plan versioning

When an objective spans multiple domains (backend/UI/installer/tests/
docs/security/performance/voice/image/memory) — or the caller attaches
explicit `decomposition` lanes — the planner fans out into parallel
scoped lane nodes that converge:

```
lane A ─┐
lane B ─┼→ integrate → review → verify
lane C ─┘
```

Each lane carries `metadata.lane` + `metadata.scope` globs (used by
worktree isolation) and `worker_role` for resource admission. Lanes are
independently admitted by the Adaptive Worker Manager — hardware decides
how many actually run concurrently. Shared files (VERSION, CHANGELOG,
lockfiles, central routers) are integration-owned by convention.

Every plan build and replan bumps `mission.plan_version` and appends a
`plan_history` entry (version, reason, node count, lanes) — "why does the
plan look like this" is always answerable. Replanning still appends the
bounded diagnose→recover→re-verify path.

Project-linked missions (`project_id`) receive a bounded
`project_context` digest — active goals, decisions, known issues,
blockers, next steps from `ProjectStore.context_digest` — injected into
work instructions. Finished nodes record themselves on the project's
worker history; mission completion logs to project activity.

## Recovery

`classify_failure(text)` maps failures to classes (CUDA OOM, network
resets, tool crashes, permission-required, test failures, ...).
`RecoveryManager` walks a finite playbook per class — wait/retry,
repair, replan, escalate — with hard caps:

- `max_task_retries`, `max_repair_loops`, `max_same_failure_retries`,
  `max_tool_failures`, `max_approval_retries`, `max_runtime_s`,
  `max_budget_usd` (mission budgets)

A repeated identical failure signature stops the mission instead of
looping forever.

Inside a single task drive the same anti-loop protections apply at the
tool-call level: a call whose `(name, arguments)` signature already
returned an error is fed back as an error without executing — no
approval round-trip, no step burned — and the signatures reset after
any successful mutating call so retry-after-state-change still works.
When repeated failures would escalate to a larger model but none
resolves (the current model is already the top tier), a one-time
corrective hint is injected into the conversation instead of the
escalation silently no-oping.

A driverless task can never wedge the queue: every synchronous drive
(`run`, `resume`, `recover`) registers its thread in
`agent._drive_threads`, and the watchdog's `_reap_stalled_tasks()` —
running before retry/dequeue each tick — marks any task stuck in
`running`/`verifying`/`reviewing` with no live driver as `error` after
`stalled_task_grace_seconds` (120s default, floored at 15s). Reaped
tasks flow through normal error retry, so the single-flight queue
always drains. `waiting_approval` is a legitimate parked state and is
never reaped (in autonomous mode it instead expires via
`autonomous_approval_timeout_seconds`).

Drive registration is also the single-flight gate: `_claim_drive()`
refuses when another live thread already drives the task, so two
simultaneous `resume()`/`recover()` calls (double-clicked approval, UI
retry racing the watchdog) cannot both consume the pending approval and
execute it twice — the approval itself is claimed under the same lock,
and every resume branch flips the ledger to `running` before executing
so expiry never sees a stale `waiting_approval` mid-action.

The supervisor also pauses all mission execution under RAM pressure —
when free host RAM drops below ``min(2 GB, 5% of total)`` the tick skips
dispatching nodes entirely (existing work finishes or re-parks on
restart) and resumes when memory frees, so unattended missions cannot
OOM the host.

## Policy

`AutonomyPolicy` is a gate **on top of** the existing PermissionManager —
it can only narrow, never widen.

- Profiles: `supervised`, `local_autonomous`, `extended_autonomous`, `custom`
- Sensitive actions (`git_push`, `create_pr`, `packages`, `delete_data`,
  `credentials`, `outbound_message`, ...) always require a standing
  grant or an approval
- `STOP AUTONOMY` (`stop_autonomy`) pauses every live mission and denies
  all new autonomous work until resumed
- File watches are confined to the workspace; artifact criteria are
  confined to the workspace

### Resource modes

`resource_mode` (`conservative` / `balanced` / `performance`) caps
mission parallelism (1/2/3 in-flight nodes) and gates GPU-heavy mission
work: in `conservative` mode, `job` nodes whose op uses the GPU
(`image`) stay `ready` while the interactive agent lane is busy —
background missions never compete with the user's foreground request
for VRAM. `balanced`/`performance` always allow them.

### Approval persistence + timeout

A gated node parks its mission in `waiting_approval` and writes a durable
row in `approvals.json`; approving resumes the exact node, while denial
replans around it. In autonomous mode the same
`autonomous_approval_timeout_seconds` bound applies to mission approvals:
an expired gate is recorded `timed_out` and the mission replans rather
than parking forever. Denials/timeouts/missing approval rows count against
`max_approval_retries` (default 2) so request → timeout → replan cannot
loop unattended all night; exhausting the bound blocks the mission for a
human.

## Restart hygiene

Boot recovery re-parks missions mid-flight (``MissionStore._recover_orphans``)
and sweeps orphaned ``.agent/worktrees`` dirs left by a killed process.
Catalog/image model managers likewise remove their stale ``*.part``
downloads on startup so a killed install resumes cleanly instead of
leaking large partial files. ``nexus-agent/*`` branches are never
deleted — a merge conflict preserves work on them — and kept branches
surface as a notification.

## Notifications

Policy-filtered outbox (`all`/`important`/`failures`/`completion`/`silent`),
30-minute dedupe, and quiet hours that still let failures/approvals
through.

## Evaluated goals (GoalManager)

Distinct from *standing goals* (schedule/trigger-bound recurring runs),
evaluated goals are durable desired-state records persisted to
`data/autonomy/goals.json` and managed by
`localcodeagent/autonomy/goals.py`:

- Schema: id (`g-…`), title, description, owner, type (reliability,
  performance, security, maintenance, capability_growth, cost_reduction,
  test_coverage, code_quality, model_quality, knowledge_freshness,
  infrastructure_health), priority, status, metrics + thresholds +
  optional warn bands, constraints, budget, linked missions, health,
  confidence, escalation policy, autonomy profile, review interval,
  mission cooldown.
- Evaluation measures every metric through `MetricRegistry`
  (`autonomy/metrics.py`) — providers are wired in `server.py` against
  real telemetry (crash history, mission failure rate, disk, Answer
  Memory stats, model-call failure rate, startup timing, corrupt-store
  count, notification backlog, pending approvals). A metric that cannot
  be measured returns `ok=False` → the goal reports `unknown`, never a
  fabricated healthy value.
- Health: `healthy`/`satisfied` (terminal goals), `degrading` (warn band
  or a failed linked repair), `violated` (hard breach), `blocked`
  (remediation wanted but autonomy stopped/paused or spawn failed),
  `unknown`.
- Degrading/violated goals may self-generate a repair *mission*
  (`source="goal"`, `created_by="goal_manager"`) carrying
  `trigger_evidence` — the measured values that justified the work.
  Spawns are deduplicated (one live mission per goal) and cooldown-bounded
  (default 30 min, floor 60 s). `escalation_policy` selects `mission` /
  `notify` / `observe`.
- Terminal mission outcomes feed back through `tick()` → confidence EMA
  (success +0.25 toward 1, failure −35%) and `mission_outcomes` counters.
- First run seeds a system-owned health-floor goal (crash-free backend,
  bounded mission failures, disk headroom, intact stores).
- API: `GET /api/goals` (goals + available metric specs + summary),
  `POST /api/goals` (create), `POST /api/goals/{id}/evaluate|enable|
  disable|archive`. UI: Goals panel on `web/missions.html`.

## Signal detection (SignalScanner)

`localcodeagent/autonomy/detectors.py` runs a bounded periodic scan
(default 120 s, one pass per `tick()` window) of small pure detectors
over injected telemetry sources — wired in `server.py` against crash
history, mission history, Answer Memory stats, model telemetry, disk,
startup profile, pending approvals, and repair-incident history. A
detector that cannot measure its signal returns nothing rather than
guessing.

Each *finding* (`data/autonomy/findings.json`) carries kind, severity,
evidence, confidence, a stable `signature`, and a `route`:

- `repair` → `SelfRepairCoordinator.report_failure` (deduped upstream by
  the repair detector's own signature normalization)
- `mission` → a bounded investigation mission (`source="detector"`,
  priority mapped from severity: critical→urgent, high→normal,
  else background), deduplicated — one live mission per signature
- `suggestion` → stays in the store for the UI; nothing is started

Findings dedupe by signature: repeat sightings refresh the row
(sightings++, confidence max) instead of re-firing, and re-routing an
acted-on finding requires a 1 h cooldown. `POST /api/findings/scan`
forces a pass; `POST /api/findings/{id}/dismiss` closes a row.

**Lifecycle**: every tick, `reconcile` marks an `acted` finding
`resolved` once its routed target finishes (repair incident terminal,
or the investigation mission terminal — a missing target also counts);
a `resolved` signature that fires again re-opens and re-routes fresh,
while `dismissed` stays silent. Closed rows older than 30 d are pruned
and the store is hard-capped at 500 rows.

**Procedural memory** (`autonomy/procedures.py`,
`data/autonomy/procedures.json`, 300-row cap) — terminal outcomes of
source-keyed missions (`detector:<signature>`, `goal:<id>`,
`schedule:<id>`) are recorded idempotently every tick. Recurring
signatures get two things:

- *Recall*: the next occurrence's objective carries a bounded
  "Prior runs" line (count, last status, duration, task completion),
  so an investigation doesn't redo identical work.
- *Suppression*: when a signature's last 2 runs all ended
  failed/cancelled, auto-respawn backs off 6 h instead of re-spawning
  on every cooldown — detector missions return `"suppressed"` and goal
  repair missions record `suppressed: prior_runs_failing` in goal
  history (the goal stays degraded, never mis-marked blocked). User
  schedules are never suppressed; they get recall context only.

Detector-generated repair incidents carry the measured cause in
`error_class`, and the diagnoser maps those kinds straight onto
operational hypotheses instead of guessing: `ram_pressure` → evict
idle models, `model_failure_rate` → stop models to safe defaults,
`disk_pressure` → reclaim orphaned repair worktrees + quarantined
stores (Nexus-managed scratch only — never user data).
Built-in detectors: crash storms, mission failure rate, model-call
failures, disk pressure, RAM pressure (twin hardware sample), repair
thrash (same signature ≥3 incidents), answer-memory decay, startup
regression vs the learned profile, stale approval backlog, orphaned
repair worktrees (≥3 dead `.repair-worktrees` candidates → suggestion),
and **CI failures** — when `gh` is installed and authed, failed runs
(`gh run list --status failure`, cached 10 min) open one repair
incident per failed-run signature. The source also attaches a bounded
`--log-failed` tail to each run, which the incident consumes as its
stack trace — the localizer suffix-matches CI-runner paths
(`/home/runner/work/<repo>/<repo>/…`) onto repo files so CI tracebacks
localize like local ones.

## Priority scheduling & fairness

- `MissionStore.list()` orders by priority rank
  (urgent < interactive < normal < background < maintenance) then
  recency. `effective_rank()` adds **aging**: background/maintenance
  missions gain one rank after 4 h waiting and another after 24 h,
  capped at `normal` — they can never outrank interactive or urgent
  work, so self-generated work can't starve but also can't preempt
  the user.
- Lane arbitration: agent nodes run only when the interactive lane is
  free — *except* `urgent` missions, which may claim the lane between
  user turns (critical recovery must not wait behind chat).
- GPU-bound image jobs yield to the interactive lane in conservative
  resource mode via `budgets.may_use_gpu`.

## Integration

- Server APIs: `/api/autonomy/*` (status, missions CRUD, approve/deny,
  pause/resume/cancel/replan, standing goals, schedules, triggers,
  notifications, stop/resume autonomy), `/api/goals*` (evaluated goals),
  `/api/findings*` (detector signals), `/api/self-repair*` (incidents)
- Chat commands: "make this a mission", "stop autonomy", "resume autonomy"
  answered locally without needing a coding model
- Missions UI: `web/missions.html`
- WorkQueue: queued prompts attribute to an active mission lane
- ActivityStore: `autonomy`/`mission` categories for audit rows
- EventBus: supervisor publishes `mission`/`notification`/`autonomy`
  events; bus events map onto trigger signals (`BUS_SIGNAL_MAP`)

## Tests

`tests/test_autonomy.py` — 49 tests covering persistence, restart
recovery, corrupt-store quarantine, DAG ordering/parallelism/leases,
failure classification + bounded playbooks, policy profiles/grants/stop,
notification policies + quiet hours + dedupe, schedules/triggers/
debounce/file watches, evaluator verdicts, approval pause→resume,
denial→replan, stop-autonomy, lane arbitration, budgets, standing
goals, daily summary, mission Q&A.

`tests/test_goals.py` — evaluated-goal coverage: metric registry
(measured/unknown/non-numeric/failing providers), goal schema +
validation + persistence roundtrip, seed defaults, evaluation states
(healthy/satisfied/violated/degrading/unknown), evidence-carrying repair
spawns, live-mission dedupe, cooldown suppression, notify/observe
policies, stop/pause blocking, confidence feedback from mission outcomes,
and tick integration.
