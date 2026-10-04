# Requirements & Acceptance Criteria

Nexus tracks *what "done" means* as durable first-class data, not as an
implied reading of a chat message. A requirement row is created for every
explicit ask and — critically — for every criterion Nexus *derives* from a
vague goal like "fix startup" or "make this work". Derived rows are marked
`inferred` so users can always tell Nexus's interpretation from their own
words.

## Storage

All requirements live in `data/requirements.json` (schema version **1**,
bounded history, atomic writes) under the runtime workspace.

## Row shape

```json
{
  "id": "req-…",
  "description": "App reaches the health endpoint",
  "source": "user|inferred|spec|test_failure|review|regression|runtime",
  "inferred": true,
  "priority": "normal",
  "scope_type": "mission|project|task|conversation|global",
  "scope_id": "…",
  "status": "in_progress",
  "verification": {"kind": "verify_passed|health_check|custom", "detail": "…"},
  "acceptance_criteria": ["…"],
  "evidence": [{"kind": "…", "detail": "…", "ref": "…", "at": …}],
  "last_checked": 0.0,
  "created_at": 0.0,
  "updated_at": 0.0
}
```

## Lifecycle

`not_started → planned → in_progress → implemented → verified`, with
`failed`, `blocked`, `deferred`, and `rejected` reachable from active
states. Unknown statuses are refused; terminal states accept no further
status writes (evidence may still be appended — regressions can flip a
`verified` row back to `failed` on new counter-evidence via
`sync_mission`).

## Derivation

`derive_requirement_specs(objective)` is deterministic: repair-intent asks
("fix", "broken", "make this work", "won't start"…) yield verified
criteria such as health check, no stale process, bounded startup timeout,
and restart stability; feature asks yield verify-passed and no-failures
criteria. Derivation happens **before** work starts:

- `POST /api/chat` + `/api/chat/stream` — repair-intent messages derive
  specs up front; the stream emits a `requirements` SSE event live, and
  the final result carries `requirements` + `acceptance_criteria`. Rows
  persist scoped to the task (falling back to conversation/global).
- Mission creation (`MissionStore` derive hook) — specs become
  requirement rows scoped to the mission *and* `success_criteria`
  entries carrying `requirement_id`, so evaluation feeds status.

## Evaluation sync

`MissionEvaluator` persists `criteria_results` (with `requirement_id`) in
mission history each evaluation pass. `RequirementStore.sync_mission`
maps met→`verified`, unmet→`failed`, running nodes→`in_progress` —
status is driven by persisted evidence, never by the acting model's own
claim.

## API

| Route | Purpose |
|---|---|
| `GET /api/requirements` | list; filter `scope_type`, `scope_id`, `status` |
| `POST /api/requirements` | create explicit row |
| `POST /api/requirements/status` | transition `{id, status, evidence?}` |
| `POST /api/requirements/evidence` | append evidence `{id, kind, detail, ref?}` |

## UI

The mission detail page shows a **Requirements** checklist (✓ verified,
~ in progress, ✕ failed/blocked, · pending) above success criteria, with
an `inferred` badge on derived rows.
