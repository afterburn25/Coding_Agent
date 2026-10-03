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
| Full test suite | **1197 tests, OK** (2 skipped — POSIX-only fake-executable tests, expected on Windows), ~230s |

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
- `04d2ca3` — Reaper check-and-claim is atomic under `agent._drive_lock`
  (RLock) — a drive registering between the liveness check and the
  terminal mark can no longer be clobbered mid-drive.
- `7eab984`/`bf0dc30` — handoff/architecture/autonomy docs updated.

## Commits this session

```
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
