# System-wide gap audit — 2026-10-09 (v0.40.x closeout)

Status legend: complete | not-dogfooded | partial | broken | stale | missing | external

| Subsystem | Status | Evidence / notes |
|---|---|---|
| Conversation/context | partial | Whole-utterance adjudication solid (v36-37). New dogfood finds: 'what is moltbook' invented a service (FIXED service_info lane); 'molkbook' typo -> 'cookbook' (FIXED lexicon). Long-run 100+ turn dogfood (§36) not run. |
| Nexus Brain/memory | implemented but not dogfooded | PFC/hippocampus hooks wired into mission outcomes; no adversarial test. |
| Tool/action execution | partial | Work orders real now (files_changed observed). Gaps: worker can write OUTSIDE declared scope (types.py edited by a lane scoped to server.py — ownership reserves but doesn't confine). |
| Permissions | not-dogfooded | Gates exist; approval/resume path verified in tests. Live social.moderate/audit flows unexercised. |
| Computer control | not-dogfooded | Desktop control exists, permission-gated; unverified this session. |
| Git/GitHub | not-dogfooded | Worktree lifecycle verified (branches, merge_back, teardown). GitHub read lane works (earlier 404 = wrong routing, not connector). |
| Artifact delivery | not-dogfooded | |
| Voice | not-dogfooded | Chatterbox/Kokoro packaged; notices path live. |
| Models/router | partial | model_role→tier wired (thrash → deep). 14B emits malformed tool JSON on big write args (mitigated by prompt guidance). ~6-8 tok/s on offloaded tiers makes lanes 30-45min. Fallback for work_order committed. |
| Resource/VRAM | partial | 30B CPU-offload + orphan selftests starved RAM to 0.8GB (killed; kill-on-close jobs committed). VRAM eviction works; capacity nag cooldown landed. |
| Engineering Missions | partial | Full lifecycle verified BUT: lanes die step_limit (fixed: 48 budget, scoped verify, artifact-inheritance), 2 lanes blocked live, heartbeat missions starve user missions on agent_lane, zombie missions accumulate (13 active incl stale). |
| Self-repair | partial | Incidents generated + recovery playbooks fire (lease_expired reclaim observed). Cleanup of stale incidents lacking. |
| Moltbook | partial | Account ACTIVE (live /agents/status: claimed). Verified live: feed read (8 posts), anaphoric comments (35 replies), consult posted (posts_count:1 via /agents/me, consult pc-6e63f9e560 awaiting_response). Awaiting peer reply. |
| Social Drive | implemented but not dogfooded | Heartbeat missions run + replan (plan v5 observed — noisy). |
| Epistemic Drive | implemented but not dogfooded | Idle-learning bounded loop unverified. |
| Peer Intelligence | implemented but not dogfooded | Stores/engines exist; consult dispatch to real peers unproven. |
| Scheduler/background | partial | Social heartbeat works; flapped on RAM pressure; noisy replans. |
| Installer/updater/deploy | partial | deploy_local hardened (orphan reaper); still saw transient robocopy locks. |
| UI | not-dogfooded | Missions workstream view shipped; peer graph (§21) missing. |
| Restart/recovery | verified | Mission survived 2 backend restarts; 'recovery stop (CANCELLED)' cancels in-flight cleanly; durability test 13/13. |
| Security/secrets | not-dogfooded | Tagging/scans exist; no adversarial exfil test this session. |
| Performance | partial | API ~1s status; models slow (VRAM-bound); background work noisy. |
| Tests/CI | complete | 3057+ green; regressions added per dogfood find. |

## P0 (data loss / fabricated success / stuck)
- [open→hardened] TOTAL LIVE-STATE LOSS on deploy+restart (2026-10-09): after
  v0.40.0 rebuild+deploy, the whole per-user state root
  (%LOCALAPPDATA%\NexusCore\data — junctioned from install `data/`) was
  empty: missions.json (m-6541468d68c1), profiles, social store,
  approvals, secrets.vault, conversations all gone. `.agent`/`output`
  targets equally fresh. Worktree branches survived in Source/.git but
  held no WIP commits, so lane output is unrecoverable. Mechanism NOT
  conclusively isolated — proven NOT to be a plain /XD-protected
  robocopy /MIR pass (isolated repro: junction + target survive).
  Hardening landed: deploy `/XJ` (never traverse junction points),
  and MigrateDirectoryContents now PARKS conflicting source files under
  target\.conflicts\<stamp>\ instead of deleting them (Program.cs).
  Residual risk: unknown — watchdog for recurrence on next deploy.
- [closed] prose-as-success lanes -> work_order path + artifact evidence check (a61a5c44)
- [closed] replan workstream duplication (fdb0aafe)
- [closed] orphan selftest trees lock/RAM (c2126a47 kill-on-close; deploy reaper)
- [closed] internal missions got unsatisfiable verify_passed criterion -> infinite diagnose→replan treadmill starving agent_lane for HOURS. Live evidence: heartbeat m-0c12599a6caf replanned +3 nodes, user chat queued behind it. Fix: requirements.py skips verify_passed for internal: objectives; evaluator accepts completed internal checks for legacy rows (38297c9)
- [closed] step-limit retry wiped lane work — live mission m-6541468d68c1: every lane hit 48-step budget, re-dispatched cold, `git reset --hard` (reflog 'reset: moving to HEAD') destroyed uncommitted work each cycle. Fix: _wip_commit checkpoints dirty worktree on step-limit finish + retry instruction says inspect-and-continue, never reset (c0f27f41)
- [open] worker writes outside declared scope (advisory only — now surfaced to reviewer, not blocked)

## P1 (claims vs normal use)
- [closed] /api/social/status crashed — SocialStore.interests() didn't exist -> interest_graph() (096bd35f, verified live)
- [closed] 'comments on that post' anaphora -> GitHub 404 hijack. Fix: feed snapshot persisted in drive store; resolve_use_continuation binds title keywords/ordinals/bare 'it'; question-form use-intents execute (1d2c4e9f, verified live: 35 real replies on trust-chains post)
- [closed] consult EV gate vetoed the FIRST-EVER consult (cold relevance 0.15 → max 0.15 < 0.28 threshold) — peer network could never bootstrap. user_requested bypasses veto, gates still apply (c76e60b6)
- [closed] consult post_ref empty + reply-matching dead (2a2f730c _sent_ref + sole-open fallback)
- [closed] mission report endpoint + UI — GET /api/missions/<id>/report
  plain-English durable rollup (MissionStore.mission_report), Report
  panel on missions detail page, payload-shape tests; originally the
  m-6541468d68c1 objective — lane output lost in the state wipe,
  feature rebuilt directly
- [committed-not-deployed] internal-mission criteria fix (38297c9), consult matching (2a2f730c)
- [open] zombie mission/incident accumulation — retirement bounded for internals; ~9 stale heartbeat missions cancelled live
- [open] model quality on 14B: malformed tool JSON on >~4KB args
- [open] mission lane speed: 30-45min/lane at 6-8 tok/s — acceptable but slow
- [closed] ask_peer subject retained 'community:' prefix in posted question (2a2f730c)
- [closed] realvisxl-v5 hidden behind InvokeAI — fleet spec invisible
  pre-install, no lifecycle, no adult default. Fix: merged
  fleet_status() inventory (Missing/Downloading/Verifying/Installed/
  Failed + tags/progress), disk dedup → in-place registration (zero
  duplicate ~6.5 GB), image_adult_default_model config → deterministic
  adult pick after policy gates, fleet install/verify/remove through
  InvokeAI registry, capability-brief live model state (8bd0892)
- [closed] image_backend preference silently reset to auto on restart —
  persisted to config.json but never loaded back (8bd0892)
- [closed] .part downloads never resumed — init deleted partials,
  failures deleted them, downloads always restarted at byte 0. Fix:
  Range-resume + verify-then-promote (8bd0892)

## P2
- [closed] splash error/recovery animations never played — stray
  `mediaDuck = 1` (declaration removed in 3b4055fc) threw ReferenceError
  in the strict .mjs message handler on every stop-voice → renderer
  posted splash-error → host hid the WebView → static PNG + native
  fallback only. Live-observed 16:19:51 after backend exit -1.
  Fix: removed the dead assignment (efda36e8); all 4 clips
  (Startup/Error/Recovery/Recovery-Failed) reachable again.
- [closed] Peer graph UI (§21-22) — SocialStore.peer_graph() evidence-derived edges + /api/social/peer_graph + canvas graph with filters + click-through dossier (cc5597b8)
- [closed] Adversarial Review Council (§7-8) — red-team consult kind on the existing engine, council roster persistence wired (956cfa24, 026924a0)
- [closed] Replication aggregation (§9-10) — per-participant normalized-env measurements; replication_summary buckets by comparability signature, never merges incompatible envs; peer reporting registers interaction+edge (f7c75a10)
- [closed] Autonomous teach drive (§11-12) — teaching_candidates selects verified journal entries (tested + conf≥0.7), consider_teaching through the normal social.post gate, taught-marked (907fd11f)
- [closed] Idle epistemic loop (§14-15) — internal:epistemic_step scheduled mission; one bounded unit per tick (consult/experiment/utility-tier research node/journal collect); in-flight items never double-unit; off level = no activity (907fd11f)
- [closed] Community/submolt (§16-17) — API support probed live (GET /submolts 200, POST →401 auth-gated); discover/detail/create wired (b9da1e0b)
- [closed] Competitions/hackathons (§19-20) — probed live: endpoints 404 — NOT SUPPORTED by current Moltbook API; nothing invented
- [open] §18 social.moderate — community moderation not applicable (no moderate endpoint in API)
- NEXUS.md proposal present; write path gated — ok

## P3+
- Soak metrics, perf baselines, log noise, UI consistency

## Install debt (§46)
- active: D:\Nexus_Core
- LKG/rollback: D:\Nexus_Core-pre-0.31.1 (keep)
- stale (audit-only, not deleted): D:\NexusCore\data (1KB stub), D:\nexus-dogfood, D:\nexus-installer-dogfood
