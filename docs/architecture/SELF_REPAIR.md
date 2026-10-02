# Self-Repair Architecture

`localcodeagent/self_repair/` — Nexus Core's autonomous failure pipeline.
The goal: when something breaks, Nexus detects it, proves a cause,
repairs it in isolation, verifies the repair, promotes or rolls it back,
learns the procedure, and resumes whatever was interrupted — all inside
existing permission boundaries.

## Pipeline

```
STABLE NEXUS
    │
    ▼  failure signal (exception, crash, mission node, API report)
detected ──► collecting ──► localizing ──► diagnosing ──► planning
    │                                                        │
    │          ┌─────────────────────────────────────────────┤
    │          ▼ operational kind                            ▼ code kind
    │     fixer (restart/quarantine/     worktree patch (repair mission
    │     evict/probe)                    inside `.repair-worktrees/` — the
    │          │                          stable tree is never edited first)
    │          ▼                                       │
    │     health check                     testing ──► reviewing ──► canary
    │          │                                       │
    │          └───────────────► promoting ◄───────────┘
    │                               │
    │                    resolved ──┴── rolled_back ──► needs_human
    ▼
interrupted mission resumes · procedure stored in repair_memory.json
```

State transitions are guarded (`models.VALID_TRANSITIONS`) — an incident
cannot jump `detected → promoting`. `resolved → rolled_back` and
`resolved → detected` exist so post-promotion regressions reopen or
restore cleanly.

## Incident record

`data/autonomy/repairs.json` (`AutonomyStore.repairs`, bounded 200 rows).
Each `RepairIncident` carries: signature, severity, error class/message,
redacted stack trace, process/exit code, mission/task/request ids,
evidence snapshots (hardware/runtime/logs/crash history/recent commits),
ranked suspects, ranked hypotheses, repair kind, plan, patch files,
regression test, verification results, review verdict, promotion and
rollback manifests, the procedure actually executed, the interrupted
operation to resume, per-incident budgets, and a stage history.

Secrets are redacted at intake (`coordinator.redact`) before any field
is persisted or handed to a model.

## Detection (`detector.py`)

- `normalize_signature` strips volatile fragments (paths, PIDs, ports,
  addresses, sizes, numbers) so a recurring fault maps to one incident.
- `classify_error` maps raw text to `(error_class, subsystem)`:
  WinError 10054 → TransportError, bind failures → PortCollision,
  OOM/VRAM → OOM, llama.cpp/ComfyUI/MCP exits → ProcessCrash, Inno/ISCC →
  InstallerFailure, JSON corruption → CorruptionError, sqlite →
  DatabaseError, …
- Severity = class base + critical-subsystem bonus + recurrence bonus.
  First-time low-signal classes (timeouts, transport noise) are
  suppressed — recurrences escalate automatically via dedupe, which
  bumps `occurrences` instead of forking parallel repairs.

## Localization (`localizer.py`)

Traceback frames resolve to repo-relative files (innermost first,
confidence decays with frame depth), plus failing-test-name → test-file
mapping and recent-commit correlation (`git log --name-only`). A bare
frame tops out at 0.85; frame + fresh commits approaches 0.97. No repo
frames → empty suspects → diagnosis favors environmental causes.

## Diagnosis (`diagnosis.py`)

Deterministic rules map evidence to ranked hypotheses across the spec's
taxonomy (source_bug, config_bug, runtime_bug, stale_process,
corrupt_store, port_collision, hardware_pressure, bad_model_config,
corrupt_model, network_failure, external_service, permission,
recent_regression, race_condition, invalid_user_data, environment,
dependency_incompatible, unknown). Each hypothesis carries evidence and
a repair kind: `operational` or `code`. `unknown` stays unknown — the
coordinator escalates instead of guessing.

## Repair paths

**Operational** — registered fixers (wired in `server.py` over the same
`runtime_hooks` the recovery playbooks use): restart+probe for stale
process / port collision / transport resets, `evict_idle` for memory
pressure, store-health verification for corruption, `stop_models` for
bad model configs. Fixers return structured `{ok, detail}`; failures are
recorded to memory so a repeatedly-failing procedure escalates instead
of looping (`RepairMemory.known_bad` ≥ 2 fails).

**Code** — `Patcher` creates `.repair-worktrees/<incident>` (git
worktree on branch `repair/<id>`; falls back to a copy sandbox without
git). Patch generation is a bounded repair **mission** running inside
the worktree (`_spawn_repair_patch_mission`) — the existing audited
executor writes the patch, while the incident parks in `patching` until
the mission terminates. The generator contract also accepts synchronous
patchers for tests.

## Verification & gating

- `verifier.run_unittest` runs targeted tests (regression test added by
  the repair first, then tests matching suspect modules) inside the
  worktree, bounded by timeout; an optional `regression_suite` gate runs
  the broader suite.
- `_default_review` is a deterministic baseline review (rejects
  `except: pass` error-hiding, path escapes, empty patches); a separate
  reviewer callable can be injected for model-based review.
- `Canary` runs a candidate launcher (isolated port/state). Production
  wiring uses `production_launcher`: boots `python -m localcodeagent`
  from the incident worktree (PYTHONPATH-scoped code, free port, scratch
  config under `data/canary/<incident>/` — all candidate state isolated
  from the live instance), waits for `/api/health`, replays the original
  failing request path when the incident recorded one, then terminates
  the process. `self_repair_canary_enabled=false` falls back to the
  honest `skipped` report, and `canary_required_for` subsystems refuse
  promotion without a real canary either way.
- Promotion requires: confidence ≥ `min_promote_confidence`, targeted
  tests green, regression suite green when configured, review pass,
  canary pass/not-required — **and** `auto_promote` enabled
  (`self_repair_auto_promote` config, default **off** — verified
  candidates wait at `needs_human` with all evidence attached).
- Before promotion, `Rollback.snapshot` preserves every file the
  candidate touches byte-for-byte under `lkg/<incident>/`; restore also
  removes files the candidate added. Post-promotion regressions reopen
  the incident or restore last-known-good.
- After promotion, `Patcher.commit_promotion` commits exactly the
  promoted paths with incident/root-cause/test-evidence metadata
  (`Self-repair: …`, identity `Nexus Self-Repair` when the repo has no
  configured identity) — auditable and revertible, never sweeping
  unrelated staged work, never pushing. Disable with
  `self_repair_commit_on_promote=false` (default **on**).

## Budgets

Per incident: `max_diagnosis_attempts`, `max_patch_attempts`,
`max_same_patch_failures`, `max_replans`, `max_runtime_minutes`,
`max_model_cost`. Any breach → `needs_human`, never an infinite loop.

## Memory & learning

`RepairMemory` (`repair_memory.json`) stores every attempted procedure
with outcome, steps, confidence, and duration. Resolution recalls exact-
signature fixes first (fuzzy token-overlap for near matches, ≥0.6);
successful repairs raise confidence by +0.15; known-bad kinds are
suppressed. A resolved incident feeds its procedure back as learned
procedural memory — the same signature next time recalls it before
diagnosing fresh.

## Integration

- **Supervisor**: `tick()` → `repair.tick()` (each open incident
  advances ≤1 stage per tick); exhausted recovery playbooks open an
  incident with `interrupted_operation = {"kind": "mission", …}` and
  `resume_interrupted` puts the mission back to `replanning` on success.
- **Server**: coordinator built in `_build_self_repair` with live
  collectors (hardware/runtime/crash history/log tail) and fixers.
- **Nexus Brain**: a `diagnostics_brain` specialist (domain
  `diagnostics`, coding+reasoning requirement, repo/test/git/system
  capabilities) subscribes to `HEALTH_EVENT` on the corpus callosum and
  records repair incidents as `diagnostics:outcome` episodic memory, so
  prior incidents inform future diagnosis. The supervisor `emit` bridge
  republishes `repair`/`finding` events onto the cognitive bus as
  structural `HEALTH_EVENT`/`MISSION_EVENT`s — no raw evidence crosses.
  `diagnostics`/`repair` intents route to `diagnostics_brain`.
- **API**: `GET /api/self-repair`, `GET /api/self-repair/{id}`,
  `POST /api/self-repair/report`, `POST /api/self-repair/{id}/retry |
  process | rollback`.
- **UI**: Self Repair panel on Mission Control — state, severity,
  confidence, recurrence count, top hypothesis, Retry for needs_human.
- **Safety**: incidents never widen permissions; `is_blocked` honors
  autonomy stop/pause; secrets are redacted at intake; stable tree is
  only touched at promotion, and only after every gate passes.

## Tests

`tests/test_self_repair.py` — 27 tests including the five controlled
fault-injection scenarios: injected code bug repaired end-to-end
(git worktree → failing-then-passing regression test → review → canary →
promote → memory), bad patch rejected with stable tree byte-identical,
rollback restoring last-known-good, operational repair with no code
path, and mid-repair restart resuming persisted state.
