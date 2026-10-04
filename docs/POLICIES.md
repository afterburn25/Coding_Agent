# Resource Policies

`localcodeagent/policies.py` — the durable store
(`data/policies.json`) for resource modes, temporary policy overrides,
offline mode, and per-project egress rules. Wired into
`AutonomyPolicy.check` and the supervisor's admission loop.

## Resource modes

| Mode | max_workers | background_jobs | gpu_yield | warm_models | max_model_tier |
|---|---|---|---|---|---|
| performance | 8 | yes | no | yes | any |
| balanced | 4 | yes | no | yes | any |
| conservative | 2 | yes | yes | yes | any |
| quiet | 1 | no | yes | no | any |
| battery | 1 | no | yes | no | small |

`quiet` and `battery` yield the GPU to interactive use and pause
background work. `conservative` is retained as a low-concurrency mode
for backward compatibility. The supervisor reads `max_workers` from
`knobs()`; `BudgetManager.may_use_gpu` treats
`conservative`/`quiet`/`battery` as yield-when-interactive modes.

## Temporary overrides

Natural-language requests become **bounded** policies — never silent
permanent mutations:

```
"Pause background work for 30 minutes."
"For the next hour, prioritize coding and don't load 30B."
"Give image generation priority."
"Run quietly." / "battery" / "go offline tonight" / "back online"
```

`parse_request` extracts a TTL (`for N minutes/hours/days`, `tonight`
≈ 8 h) and emits override rows with `expires_at`. Active overrides are
what the scheduler consults; expired rows stay in the bounded history
but stop applying — original behavior returns automatically. `ttl=0`
means session-scoped.

Override keys the scheduler/policy consume:

- `background_jobs` = `false` → non-urgent missions take no admission
  slots until expiry.
- `priority` — mission bias hint surfaced in `summary()`.
- `model_deny` — model id fragment the router/scheduler should avoid
  while active.

## Offline mode

`set_offline(true)` persists a flag consulted by
`AutonomyPolicy.check`: every network action class (`research`,
`browser`, `git_push`, `create_pr`, `packages`, `outbound_message`)
returns `deny` while offline — outranking profile allowances and
standing grants. Local actions are unaffected.

## Project egress

`set_egress(project_id, mode)` — modes: `local_only`,
`network_read_allowed` (default for unset projects), `restricted`
(reads allowed; pushes/PRs/messages/package installs denied),
`custom` (reserved). Workers inherit via `policy.check(scope=...)`:
when the scope is a project with `local_only`/`restricted` egress,
network actions deny even under `extended_autonomous`.

## API

- `GET  /api/policies` — mode, knobs, offline flag, active overrides,
  egress map.
- `GET  /api/policies/egress/<project_id>` — effective egress for a
  project.
- `POST /api/policies/mode` `{mode}` — set resource mode.
- `POST /api/policies/offline` `{on}` — toggle offline mode.
- `POST /api/policies/request` `{text}` — translate a natural-language
  request into bounded overrides; returns what was applied.
- `POST /api/policies/override` `{key, value, ttl_seconds,
  description}` — add an explicit override.
- `POST /api/policies/revoke` `{id}` — revoke an override.
- `POST /api/policies/egress` `{project_id, mode, note}` — set project
  egress.
- `POST /api/autonomy/policy` `{resource_mode}` — legacy route; now
  accepts all five modes and delegates to the shared store.
