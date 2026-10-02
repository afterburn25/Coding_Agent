# Nexus Autonomy — Persistent Mission Manager

Nexus Autonomy turns Nexus Core from a request/response assistant into a
persistent agent platform: durable missions, a dependency-aware task DAG,
a bounded event-driven supervisor, schedules/triggers, recovery playbooks,
policy gates, and restart-safe state.

## Storage

All autonomy state lives under `data/autonomy/` (schema version **1**):

| File | Contents |
|---|---|
| `missions.json` | Mission records (bounded 200) — objective, status, task graph, budgets, criteria, history |
| `standing_goals.json` | Persistent goals that spawn missions on schedule/trigger |
| `triggers.json` | Event triggers with conditions + debounce |
| `schedules.json` | once/interval/daily/weekly schedules (missed runs fire on next tick) |
| `grants.json` | Standing permission grants — scoped, expirable, revocable |
| `approvals.json` | Approval ledger — pending approvals survive restart |
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
Sandbox), `research`/`internal` (server-side helpers), `wait`, and
`job` — asynchronous platform work dispatched via the wired job runner.
`job` metadata selects the operation: `sandbox` (bounded command with
the workspace as cwd), `backup` (versioned state backup), `rag_update`
(incremental index refresh; works standalone via a transient
`RepoIndex`), and `image` (submit an `ImageRequest` and await the job's
terminal state). Repository/workspace missions automatically prepend a
`rag_update` job node so inspection sees a fresh index.

## Recovery

`classify_failure(text)` maps failures to classes (CUDA OOM, network
resets, tool crashes, permission-required, test failures, ...).
`RecoveryManager` walks a finite playbook per class — wait/retry,
repair, replan, escalate — with hard caps:

- `max_task_retries`, `max_repair_loops`, `max_same_failure_retries`,
  `max_tool_failures`, `max_runtime_s`, `max_budget_usd` (mission budgets)

A repeated identical failure signature stops the mission instead of
looping forever.

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

## Notifications

Policy-filtered outbox (`all`/`important`/`failures`/`completion`/`silent`),
30-minute dedupe, and quiet hours that still let failures/approvals
through.

## Integration

- Server APIs: `/api/autonomy/*` (status, missions CRUD, approve/deny,
  pause/resume/cancel/replan, standing goals, schedules, triggers,
  notifications, stop/resume autonomy)
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
