# Risk Classification & Promotion Pipeline

Changes earn a risk tier *before* they run, and real-state mutations
must clear a gated stage chain before they may land. No stage can be
skipped; a failed required stage blocks promotion permanently for that
candidate (it must be rejected or a new candidate opened).

## Risk tiers (`localcodeagent/risk.py`)

| Tier | Examples | Required stages |
|---|---|---|
| `low` | read, search, status, probe | none |
| `medium` | code change in isolation, config edit, sandbox run, network request | simulate → targeted_tests → review |
| `high` | installer, migration, runtime config, dependency upgrade, desktop automation, update, destructive | simulate → targeted_tests → regression_tests → review → canary |

`classify_action(action, target, context)` returns `{risk, reasons,
required_stages}` — deterministic and explainable. Context can only
*raise* a tier (`production`, `touches_user_data`, `network` egress
floors at medium); `isolated` downgrades a risky *experiment* to
medium because staging an upgrade is itself bounded work — but
irreversible kinds (`destructive`, `secret_access`, `self_modify`,
`promote`) never downgrade.

## Promotion pipeline (`localcodeagent/promotion.py`)

Durable candidates in `data/promotions.json`:

```
candidate → simulate → targeted_tests → regression_tests → review
          → canary → promote            (or → rejected)
```

- `create(description, action, target, context)` — risk auto-classified
  unless explicitly set; the required stage list is stamped on the row.
- `record_stage(id, stage, ok, detail, evidence)` — required stages must
  pass **in order**; a `review` write is refused while `targeted_tests`
  is unpassed. Non-required stages are always allowed as extra evidence.
- `ready(id)` → `(can_promote, missing_stages)`; `promote()` refuses
  while anything is missing — no partial promotions.
- `reject(id, reason)` — terminal.

Execution layers (sandbox, self-repair worktree pipeline, missions) run
the actual checks; this store is the durable gate they must satisfy.

## API

| Route | Purpose |
|---|---|
| `GET /api/promotions` | list + status summary |
| `GET /api/risk?action=…&target=…` | preview the classification |
| `POST /api/promotions` | create a candidate |
| `POST /api/promotions/stage` | `{id, stage, ok, detail, evidence}` (409 when out of order) |
| `POST /api/promotions/promote` | `{id}` → 409 while stages unpassed |
| `POST /api/promotions/reject` | `{id, reason}` |
