# Overnight Hardening & Dogfood Report — 2026-10-03

Scope: reliability-focused dogfood of the real Windows backend against
`afterburn25/Coding_Agent` main, using Nexus Core on its own repository
(workspace `D:\Devin\chat-nexus`, config `D:\nexus-dogfood\config.json`,
backend `127.0.0.1:8901`). Real models on an RTX 3080 Ti (12 GB), real
approvals, real queue, real restarts — not just unit tests.

## Production defects found and fixed (4 commits)

### 1. Cross-profile memory leak through Answer Memory (privacy)
`43065f7` — Learned answers/experiences were global. Alice stored "my dog
is named Rex"; Bob asking the same question received Alice's answer —
non-trusted matches were injected into prompts as "possibly relevant
learned answers", so any personal fact one profile recorded could leak
into another profile's conversation. Schema v2 stamps `profile_id` on
`answers` and `experiences`; lookup/injection serve profile-stamped rows
only to the same profile (empty = pre-profile shared knowledge).

Verified live end-to-end: Bob → `no_match`, Alice → `exact`, and Bob
correctly answered "I don't know" to the dog's name after the fix.

### 2. Personality prompts were cosmetic
`60538d2` — Personality context reached the prompt as bare numbers
("Sassy, strength 100, sliders=…") the model ignored — identical prompts
produced identical responses across presets. Standout sliders now render
as prescriptive delivery cues ("tease with sharp playful sass"), keeping
the delivery-style-only boundary (facts/code/permissions untouched).
Verified live: sassy teases with metaphors while direct stays terse on
the same factual prompt.

### 3. Personality presets were inaudible
`5c01bd4` — 70 of 73 presets shipped `voice: {}` so switching
personalities changed nothing a listener could hear. Style families now
map to voice defaults inside intelligible bounds (speed 0.88–1.09,
±0.5 semis); explicit per-preset voices still win. Verified live:
identical phrase = 3.48s under Calm vs 3.05s under Playful (~14%).

### 4. Dead drive thread permanently wedged the task queue (the big one)
`c4050e5` — Found by live dogfood: a task sat `running/researching_failure`
for 6+ minutes with *no drive thread at all* (py-spy showed 5 idle
threads) and no model process, permanently blocking a queued 30B task.

Root cause: `resume()`'s verification branch returned
`_finalize(session)` directly. When `_finalize` detects a failed
verification it starts an auto-repair round and returns `None` —
"keep driving" inside `_drive`, but `resume` leaked it to the HTTP
handler, which died on `result.content`. Timeline confirmed against
task/activity logs.

Fixes:
- `resume()` re-enters `_drive_or_error` on the internal `None`.
- `AgentOrchestrator._drive_threads` registers the driver for every
  synchronous drive (`run` via `_drive_or_error`, plus `resume`/`recover`
  which do work outside it); `has_live_driver()` distinguishes live
  drives from stranded rows.
- `_reap_stalled_tasks()` runs in the watchdog before retry/dequeue:
  an active task (`running`/`verifying`/`reviewing`) with no live driver
  for `stalled_task_grace_seconds` (120s default, config-floored at 15s)
  is marked `error`, its activities closed, and the queue unblocks on
  the same tick. `waiting_approval` is a legitimate park — never reaped.

Regression tests: resume→failed-verification→repair re-drive;
reaper unwedges queue; reaper never touches live drives or parked
approvals.

### 5. Canary could validate the wrong code
`c4050e5` — The live suite failure `test_candidate_exit_reported`
("True is not false") was a real bug, not flake: `production_launcher`
*prepended* the worktree to an inherited `PYTHONPATH`, so a harness
exporting the repo path (the selftest runner does exactly this) let the
canary boot `localcodeagent` from stable-repo code and report healthy
for a worktree that can't boot. `PYTHONPATH` is now replaced with the
worktree — candidate resolution fully isolated.

## Live verification results

| Area | Result |
|---|---|
| Profiles (3) + isolation | Pass — per-profile personal memory, switching, avatars (512×512 WebP, real circular alpha) |
| Personality → real chat | Pass after fix — measurable style difference, facts preserved |
| Voice prosody mapping | Pass after fix — calm 3.48s vs playful 3.05s same phrase |
| Model ladder | 4B utility route ✓; 14B escalation evicts resident 4B (VRAM reclamation) ✓; 30B routing/benchmarks ✓; idle unload ✓ |
| Self-repair fault injection | Corrupt JSONL → quarantined+resolved; config/runtime faults → correctly `needs_human`; crash-storm correctly rejected for auto-repair |
| Autonomy supervisor | Healthy — ~5.2ms ticks, no busy loop; all 6 findings routed/resolved (2 auto, 4 needs_human) |
| Queue unwedge after fix | Stranded task → `interrupted` on restart; queued review task dequeued → `completed` via 14B; watchdog never touched the live multi-minute drive |
| Crash/restart continuity | `taskkill` mid-drive → restart → auto-resumed (`autonomous_resume_interrupted`) → drove real work to legitimate approval parks |
| Startup perf | 1.36s to health; AppState 0.47s (hardware detect ~0.13s, already parallelized) — healthy, no fix needed |
| UI sweep | 41 web assets — all pages 200, all JS parses (node --check), zero broken internal links, every UI-called API endpoint exists (404s were POST routes correctly rejecting GET) |
| Full test suite | **1199 tests, OK** (2 skipped — POSIX-only fake-executable tests, expected on Windows), ~230s |

## Known remaining rough edges (not blocking)

- `waiting_approval` parks accumulate in "ask" permission mode — by
  design (a pending human decision shouldn't be silently skipped), but
  unattended use wants `autonomous_mode` or `session`-scoped grants for
  shell/filesystem. In autonomous mode they expire via
  `autonomous_approval_timeout_seconds`.
- `debounce.py`/`test_memoization.py` dogfood artifacts deleted from
  repo root (the debounce write missed `import threading` — itself the
  verification failure that triggered the repair round exposing the
  wedge).

## Follow-on fixes (post-report)

- `f6ddc09` — Answer Memory self-heals partial v2 stamps: column/index
  assertions re-run idempotently on every open, closing the
  `user_version`-keyed migration gap flagged above.
- `167f2c9` — Identical-failing-call dedup + escalation nudge: the live
  task's terminal log showed the 14B re-emitting the same failing
  `apply_patch` ~5×, each burning an approval + step to `step_limit`.
  Identical `(name, args)` calls now fail fast without executing
  (signatures reset on any successful mutation), and when repeated
  failures can't escalate to a bigger model the corrective hint fires
  once instead of silently no-oping. Verified live with a fresh task
  (clamp utility): 14B under 0.8GB free VRAM survived the full drive —
  write → test → selftest (ran the new dedup test itself) → `completed`.
- `351f601` — **14B disconnect root cause found and fixed**: the
  recurring `connection_reset` on 127.0.0.1:8081 was not a crash or
  VRAM contention — memory-pressure eviction (`memory_pressure_vram_gb:
  1.0` floor, reached while the 14B served a 17k-token prompt) evicted
  the *serving* runtime mid-request because the recovered task's ledger
  row had `model_id=""`. `_drive_or_error` now restamps
  `model_id`/`model_role` at every drive start (run/resume/recover all
  funnel through it), and the eviction busy-set pins *all* resident
  models when a live driver's model can't be identified. Live-verified:
  same task, same 17k prompt, VRAM below the floor — server survived,
  drive continued to a legitimate `step_limit`. Also fixed: the
  step-limit outcome path passed `conversation_manager` kwargs to
  `ConversationMemory.record_exchange` (a 2-arg method) — TypeError
  flipped `step_limit` tasks to `error`. Regression tests cover all
  three fixes.
- `04d2ca3` — Reaper check-and-claim is atomic under `agent._drive_lock`
  (RLock) — a drive registering between the liveness check and the
  terminal mark can no longer be clobbered mid-drive.
- `830b5e1` — Single-flight holds past the ledger window: dequeue and
  error-retry now consult the driver registry as the authoritative
  liveness check (a task aged past `recent()` can't let a second drive
  start); reaper scans the same wider window.
- `f205c69` — One `_agent_lane_active()` helper (ledger + queue/retry
  markers + driver registry) now backs mission dispatch (`lane_free`)
  and auto-tune benchmarking (`_lane_busy`); idle-model eviction pins
  live-driver models so a deep-ledger task's runtime can't be unloaded
  mid-drive.
- `7eab984`/`bf0dc30` — handoff/architecture/autonomy docs updated.
- `33b1241` — `_agent_lane_active` treats an absent agent as
  no-drivers rather than busy (suite stub fixture regression).
- **Definitive live closure**: the originally-wedged task
  (`a47765e9395f`, `running/researching_failure` with no driver for
  6+ min on old code) was resumed on the new code — tool approval →
  verification approval → suite re-ran → `EXIT_CODE=0` → `completed`.
  The suite passing also re-verified the canary `PYTHONPATH` fix live:
  `test_candidate_exit_reported` correctly fails dead worktrees under
  selftest's exported `PYTHONPATH` now. Dogfood ledger ends fully
  terminal: 2 completed, 1 recoverable error, queue empty.

## Commits this session

```
f205c69 Queue: lane checks share one authoritative liveness helper
830b5e1 Queue: driver registry is the authoritative single-flight check
04d2ca3 Queue: make reap check-and-claim atomic against driver registration
bf0dc30 docs: driver-liveness reaper in AUTONOMY recovery + ARCHITECTURE flow
7eab984 docs: handoff + start-here for overnight queue-wedge and dogfood fixes
f6ddc09 Answer Memory: self-heal partially stamped v2 schemas
2b6a058 docs: overnight hardening + dogfood report 2026-10-03
c4050e5 Queue: dead drive threads can no longer wedge task processing
5c01bd4 Personality: per-style voice defaults make presets audible
60538d2 Personality: render standout sliders as delivery-style cues
43065f7 Answer Memory: scope learned answers to the recording profile
```

## Follow-on rounds (post-report hardening sweep)

After the ledger drained, a systematic audit of every failure class that
could degrade an unattended multi-day run landed the following fixes —
all unit-tested, several live-verified on the dogfood backend:

**Concurrency & approvals**

- `95a4124` — `resume()` claimed `session.pending_approval`
  non-atomically: two simultaneous approvals executed the action twice
  and spawned competing drives on one session. New `_claim_drive()`
  single-flights all drive entries (run/resume/recover, nested re-entry
  allowed); the approval claim happens under `_drive_lock`.
- `f909222` / `8ce18dc` / `884440d` — every resume branch (session tool,
  persisted tool/verification, direct image) left the row at
  `waiting_approval` while the approved action ran — approval-expiry
  could reap mid-execution, and the UI showed a stale park. The ledger
  now flips to `running`/`working` at claim time.
- `edc3011` — `/api/jobs/cancel` on a parked task left the session in
  `_sessions` (leaked + resumable stale state) and kept
  `pending_approval` in the row; the endpoint now closes the session and
  clears the field.
- `04a88ca` — `current_mission_id` was a mutable orchestrator global;
  parallel lanes (mission node + chat) mis-stamped activities.
  `run(mission_id=…)` registers per-task now.

**Marker-then-spawn wedges** (a failed `Thread.start()` left permanent
stuck state): `584d177` queue/retry sets (+ lost popped item re-queued),
`41c8e07` mission `_workers` + image jobs + model installs marked
failed, `3c810db` MCP/LSP PIPE'd children killed when reader threads
fail to spawn.

**Unbounded resources**

- Logs: `3f8a74a` permission audit JSONL (2×AUDIT_LIMIT compaction),
  `97bab0b` eval history + cerebellum opt log (512KiB tail bound;
  `history()` also re-read the whole file per call), `ca75984` probe log
  handle leak + `probe-*.log` count prune.
- Memory: `98a3be2` TaskStore in-memory rows evict to the persisted set
  (previously every row ever created stayed in RAM), `0e2d001` image
  `_jobs` same divergence, `7445b1e` install `_cancel` flags popped on
  worker exit.
- `6cf18b1` — `_log_buffers` leaked on every early-return terminal path
  (brain fast-path, builtin, direct image, answer memory, activate
  failure); terminal status transitions now flush unconditionally.

**Verified, no fix needed**: lock ordering (`_dequeue_lock` →
`_drive_lock` → `tasks._lock`, never reversed — no ABBA), `agent_lane`
resource lock enforces `_lane_mission` single-ownership, ActivityStore
bounds, brain stores locked/sqlite, EventBus slow-consumer drops, SSE
unsubscribe, all process waits timeout-guarded or cancelable,
TaskStore atomic writes + corrupt quarantine, restart storm cap
(3 restarts / 10 min), schedule catch-up fires once (no storm),
research cache TTL+cap, voice task map bound.

**Live verification**: fresh attended task `429985175318` (fib utility)
on qwen3-14b — write_file → run_shell → verification selftest →
`completed`, all through approval gates on final code.

**Unattended verification**: autonomous mode enabled live via
`/api/permissions/autonomous`; task `a8b351e1ad2f` (countdown util)
drove write_file → run_shell → repo selftest → review → `completed`
with zero approval parks — the full unattended path on the real 14B.
Autonomous mode restored to off after the test.

Final suite: **1207 tests green / 2 POSIX skips**.

**Sustained-unattended soak**: autonomous mode enabled live; three
tasks submitted back-to-back (two queued while the first drove). All
drained in order — flatten_once → `completed`, kebab → `completed`
(including a real verify→repair→re-verify round unattended),
clamp_range → `completed` — zero approval parks across the entire
soak. During the run the llama runtime crashed twice (connection
refused, then mid-stream reset); the watchdog auto-restarted it both
times inside the 3-per-10min cap and the affected drive recovered and
finished. End state: all rows terminal, queue empty, runtime
idle-stopped, no leaked drivers. Autonomous mode restored to off.
