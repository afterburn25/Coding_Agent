# ROADMAP CLOSURE — canonical matrix

> Audit of every requirement from the major Nexus Core roadmap prompts
> against **current code and live evidence** on
> `milestone/integrated-reliability-closeout` (36 commits over
> `origin/main` 516942f9). Statuses: `LANDED_VERIFIED` (code + live
> dogfood evidence), `LANDED_NEEDS_DOGFOOD` (code + tests, no live
> proof), `PARTIAL`, `NOT_LANDED`, `INTENTIONALLY_DEFERRED`,
> `SUPERSEDED`.
>
> Evidence abbreviations: LIVE = verified on the real install
> (`D:\Nexus_Core`, RTX 3080 Ti); TEST = suite coverage; CODE = present
> but unverified.

## Stale branches — disposition

| Branch | Verdict |
|---|---|
| `feature/cinematic-core-unlock-splash` (f344901b) | **SUPERSEDED** — standalone `prototypes/core_unlock_splash/` WinForms prototype. Fully ported into production by `ebc816db` as a WebView2 layer: all 23 audio stems carried over byte-identical (prototype `mix_report.json`/`preview.wav` dev artifacts intentionally not shipped), `animation_manifest.json` identical, `docs/SPLASH_ANIMATION_HANDOFF.md` is the prototype's own handoff. Nothing left to port. |
| `voice-concept-isabella` (f4a457fa) | **SUPERSEDED** — one CI workflow generating ffmpeg-treated audition MP3s from the public Kokoro `bf_isabella` sample. No production code. The production voice is `nexus-synthetic-isabella` (default preset, DSP layers, Voice Studio UI) — landed earlier on main. |
| local `feature/production-cinematic-splash-recovery` | Fully contained in this branch (its 3 commits = avatar + InvokeAI fix + handoff). Delete locally after landing. |

## Phase 2 — Cinematic splash

| Requirement | Status | Evidence |
|---|---|---|
| 15-phase unlock sequence (lock→core→auth→key→pins→rings→iris→charge→orbitals→illumination→stable→ONLINE→UI) | LANDED_VERIFIED | `desktop/ChatNexus.Desktop/splash/web/` renderer.mjs (10-blade iris, 4 rings, 4 pins, crackle arcs), timeline.mjs state machine, animation_manifest.json 6 gates; `tests/test_branding_splash.py` contract suite; installer ships `splash/` |
| Deterministic time-based ~60fps, component state (lockAngle/ringAngle/pins/iris/core), easing | LANDED_VERIFIED | renderer.mjs canvas state machine — no generated-frame morphing; ramp/ease fns in timeline.mjs |
| Real StartupProgress mapping; never ONLINE early; hold on slow start | LANDED_VERIFIED | `StartupProgress` 3-layer model (Real/Predicted/Displayed), EMA profile, monotonic; `PumpCinematic` feeds `progress.RealProgress` → gates; `Program.cs:298-315` |
| Audio layers (hum/chirps/lock/tumbler/servo/iris/charge/swell/stable/online) | LANDED_VERIFIED | 23 WAV stems 48kHz, `audio.mjs` WebAudio crossfades + one-shots; `splash_audio_enabled`/`splash_volume`/`reduced_motion` config; audio failure → static fallback, never blocks start |
| Failure breakdown sequence (FAULT_DETECTED→…→LOCKDOWN→recovery UI), ~1.5-2.5s | LANDED_VERIFIED | manifest `failure` timeline (instability arcs/sparks, emergency iris close, pin re-engage, ring lock, containment); `trigger-fault` host msg; failure WAV stems |
| Recovery visual states (ANALYZING/REPAIR/RESTARTING/ROLLBACK/SAFE_MODE/HUMAN) | LANDED_VERIFIED (rendering) | `RECOVERY_STATES` in controller.mjs; `recovery-state`/`repair-success` host msgs; recovery panel buttons |
| Recovery actions wired to host | LANDED_NEEDS_DOGFOOD | All 5 actions wired: rollback→`rollback.flag`→`ApplyLkgFlags` swap on next boot, safe-mode→`safe_mode.json` (SafeModeStore schema, history preserved)→suppressed heavy paths; watchdog consumes `recoveryWatchdogMs:6000`→HUMAN_INTERVENTION_REQUIRED; `--test-fault` injects a real fault for on-demand dogfood. LIVE: fault→narration→containment→panel verified; user Retry clicked→clean boot |
| Fallback on renderer/audio/manifest failure | LANDED_VERIFIED | Dual-layer SplashForm: WebView2 cinematic over GDI+ static artwork+bar; `splash-error` keeps fallback; missing manifest/asset tolerated |
| Duplicate-fault protection | LANDED_NEEDS_DOGFOOD | fault mode latched; verify live |

## Phase 3 — Isabella voice

| Requirement | Status | Evidence |
|---|---|---|
| Distinctive production default voice on Isabella concept | LANDED_VERIFIED | `nexus-synthetic-isabella` official preset (Kokoro `bf_isabella` + Neural/Glass/Micro DSP layers); LIVE: startup narration speaks |
| Voice-profile metadata (id/name/engine/base/pitch/tempo/formant/EQ/compression/layers/blend/output) | LANDED_VERIFIED | `VoicePreset` now carries provenance/version/cadence/energy/warmth/formality/emotion_range + `pronunciation_overrides` (word-boundary rewrites applied in `_synthesize`, cache-key coherent); official presets refresh from shipped JSON every boot. TEST ×4 |
| Pipeline to create other authorized voice profiles | PARTIAL | presets.py save/save_as/rename/duplicate/delete + Voice Studio UI (DSP authoring). **Import→preprocess→segment→train-from-samples is not built** — presets are DSP recipes over base voices, by design (`types.py:2-5`). Sample-import/clone training: INTENTIONALLY_DEFERRED? — decide |
| Preserve Kokoro fallback/queue/persona controls/notices/TTS-safe text/vocalizations/gesture timing | LANDED_VERIFIED | docs/VOICE_SYSTEM.md; LIVE overnight dogfood; `speech_filter.py`, `vocalizations.py`, `voice_map.py` |
| Versioned voice assets, rollback, non-destructive | LANDED_VERIFIED | preset store versions JSON, official presets copy-on-write, SHA-256-verified packaged assets |

## Phase 4 — Background provisioning

| Requirement | Status | Evidence |
|---|---|---|
| Declared plan (voice→InvokeAI→fleet→Whisper→ComfyUI→image models) | LANDED_VERIFIED | `provisioning.py:_declared_plan`; LIVE: plan reconciled `invokeai→completed` today after manifest fix |
| Full recommended workstation stack (ripgrep, ffmpeg, ffprobe, tesseract, pandoc, duckdb, piper, docker, blender, code-intel extras, Playwright/Chromium, …) | **PARTIAL — real gap** | 11 bundled manifests exist in `tools/manifests/` but only voice/invokeai/whisper/fleet/comfyui/image-models are in the plan. No Playwright item (browser assets install via `/api/browser/install` ad-hoc, not the planner). |
| Install profiles (Core / Recommended / Complete Workstation / Custom) | **NOT_LANDED** | No profile concept in `provisioning.py`/`config.py`. |
| Approval gate for privileged/large/licensed items (WAITING_FOR_APPROVAL) | PARTIAL | `packages.install` permission exists; no per-item approval state in the plan for OS-level tools |
| Progress (bytes/%/speed/ETA/stage), pause/resume/cancel/retry, restart persistence, disk reserve, checksum, healthy-skip, AV-retry, failure classification, waiting_for_capability | LANDED_VERIFIED | provisioning.py + Command Center/Settings UI; LIVE reconcile after deploy fix |
| Spoken meaningful failures, deduped, no spam | LANDED_NEEDS_DOGFOOD | `_speak_notice` once-per-incident; LIVE not fully exercised for every class |
| Background UX: chat usable during provisioning + one-time notice | LANDED_NEEDS_DOGFOOD | `provisioning` runs async; verify the spoken/text notice exists |

## Phase 5 — Clean-install dogfood

| Requirement | Status | Evidence |
|---|---|---|
| Actual frozen product: installer→first launch→provisioning→chat/voice/STT/image/coding usable→mid-restart resume→completion | PARTIAL | CI windows-desktop job smokes install→update→running-app-kill; LIVE box has the real install but never a true clean-scratch full-stack run. **Needs the dedicated clean dogfood.** |
| Fault injection (network cut, resume, Defender lock, disk shortage, crash, restart, cancel+retry, preinstalled, corrupt partial, checksum fail) | LANDED_NEEDS_DOGFOOD | Retry/classification paths tested; live-fault matrix not run |

## Phase 6 — Live image backends

| Requirement | Status | Evidence |
|---|---|---|
| ComfyUI: health/discovery/standard+custom workflow/bg-removal/upscale/import/progress/cancel/artifacts | LANDED_VERIFIED | LIVE 2026-10-05: qwen-image-2.1/flux2-klein/juggernaut t2i, bg-removal, mid-flight cancel, real PNGs, history+backend_job_id+vram persisted |
| InvokeAI live | LANDED_VERIFIED | LIVE today: managed start on :9090, 4 models, real 512² PNG through `/api/image/generate`; fleet dogfood 2026-10-05 (Juggernaut/CyberRealistic/RealVisXL) |
| Auto fallback InvokeAI→ComfyUI with real output + recorded reason | LANDED_VERIFIED | LIVE matrix A–E incl. honest 400 on impossible pin |
| LLM+image VRAM contention (release→generate→restore, vram before/during/after) | LANDED_VERIFIED | LIVE: `cc702cb2` evidence + `21b7120d` demand eviction + `ad267afd` mid-request protection + `9b8710c3` CUDA_OOM classification; `vram_before/after` persisted per job |

## Phase 7 — Playwright / browser E2E

| Requirement | Status | Evidence |
|---|---|---|
| Playwright in packaged app (Edge channel, managed Chromium fallback, post-install provisioning) | LANDED_VERIFIED | `dc56c48d`; LIVE headless Edge verify + screenshot; `/api/browser/install`; frozen selftest check |
| Actions: open/click/type/navigate/console/network/sessions/storage-state/screenshot/close | LANDED_VERIFIED | `webtools/browser.py` 11 actions + `browser_verify` |
| Per-page E2E over all Nexus pages | **PARTIAL** | One-time headless UI sweep found+fixed 3 defects; no repeatable per-page spec (Chat/Command/Workspace/Projects/Missions/Image/Models/Knowledge/Tools/Settings/Trainer…) |
| Bounded visual regression | **NOT_LANDED** | No baseline/tolerance machinery |

## Phases 8–15 — workstation surfaces

| Requirement | Status | Evidence |
|---|---|---|
| Env editor per project (key/value/source/scope/inherited/validation) | **NOT_LANDED** | `EnvironmentStore` + `/api/environment*` exist; **zero web UI references** |
| Secrets editor (name/scope/status/last-updated, never reveals values) | **NOT_LANDED** | vault + `/api/secrets[/set|delete]` exist; **zero web UI references** |
| DB migration awareness (Alembic/Django/Prisma/EF/Flyway detect/status/generate/preview/apply/verify) | **NOT_LANDED** | no migration tooling in `localcodeagent/tools/` |
| Docker-aware dev (Dockerfile/compose detect, build/run/stop/logs/health/ps/ports, dev-server discovery) | **PARTIAL** | `docker_tool.py` = docker_run + docker_images only; no compose detection, container mgmt, or dev-server-in-container discovery |
| Unified Quality UI (tests/coverage/lint/type/audit/profile/bench/regressions) | **PARTIAL** | tools exist (`run_tests`,`coverage_report`,`dep_list`,`project_audit`,`debug_run`,benchmark lab) — no coherent Quality page; lint/profiling adapters minimal |
| External file-change watcher (detect→mark→no silent overwrite→reload/compare/merge; self-write immunity) | **NOT_LANDED** | editor has hash-based stale-save 409 only |
| Autonomous Git→PR→CI closure loop (branch/worktree→implement→test→review→PR→CI inspect→repair→merge-ready; no auto-merge) | **PARTIAL** | all primitives verified (git tools, github connector, worktrees, missions, self-repair, CI rerun); end-to-end wired loop not documented/live-verified |
| Scaffold templates build/test/launch/health-check | PARTIAL | 7 templates (static_site, python_app, python_cli, fastapi_service, react_vite, node_api, cpp_cmake); prompt lists ~15 — add only what's CI-testable |
| Transactional locked-file/deferred replacement | PARTIAL | LKG `update.flag`/`rollback.flag` host-swap covers backend binary; generic staged-manifest deferred replacement for arbitrary locked files not generalized |

## Phases 16–20 — audits, cohesion, release, soak

| Requirement | Status | Evidence |
|---|---|---|
| Brain closure audit (export/sign/update/stale/foreign/model-replacement continuity/backup-restore/audit/paraphrase/plan-eval-learn/multi-hour) | PARTIAL | Docs + tests verify signing/export/stale/foreign/audit-log; **`next_security_upgrade` in project.json still lists creator private-key backup/recovery**; multi-hour unattended dogfood pending |
| MCP live validation + code-intel dogfood | PARTIAL | `mcp.py` stdio+HTTP clients + `ec55090` "validated/pinned code-intelligence dependencies + MCP interop record"; live real-MCP-server dogfood not recorded |
| UI cohesion pass on every page | PARTIAL | `b600e60b4` sweep found+fixed 3 defects; systematic all-page polish pending Phase 18 work |
| Version/release metadata agreement | PENDING | PROJECT_STATUS says 1963 tests; suite is 1965; VERSION 0.21.0; head/test-count fields stale in project.json (1705) |
| Long-run soak (hours; mixed workload) | PARTIAL | `scripts/mission_soak.py` + `scripts/selftest` soak tier + nightly CI workflow; multi-hour run not yet completed |

## Live incident — packaged LKG rollback (2026-10-05, resolved)

**Symptoms:** user-reported avatar regression (square portrait + logo
overlay — mixed-version web bundle: `app.js` emitted `has-avatar`, old
`styles.css` lacked the round/suppression rules) and voice fully dead
(rolled-back backend lacked bundled `kokoro_onnx`/`onnxruntime`).

**Root cause chain:** dev/soak backends shared `D:\Nexus_Core`'s
`data/session.json` + `data/safe_mode.json`; every hard-killed test
process wrote a dirty marker counted against the *production* install.
18 phantom "unclean boots" tripped `AUTO_ROLLBACK_THRESHOLD` → host
restored the oldest LKG snapshot → `backend/` regressed ~2 days.

**Fixes landed this session:**
- Restored the newer build (`backend-replaced/` → `backend/`); old
  build preserved as `backend-lkg-oct4/`; LIVE re-verified voice
  synthesis (2.35 s WAV), avatar CSS rules, clean `safe_mode.json`.
- **Session markers now scoped per install**
  (`data/session-<owner>.json`, owner = sha1 of frozen exe / main
  module path): a foreign backend sharing the data dir can no longer
  inject phantom crashes, erase real crash evidence, or mask a true
  crash loop. Legacy `session.json` migrates once. TEST ×4.
- **Greeting voice race fixed:** `speak_greeting` is now synchronous —
  the `/greeting` response carries `voice_url`/`voice_segment_id` and
  the page plays it through `NexusVoice.enqueue` (segment-id dedupes
  the same segment arriving on the bus). Previously the greeting
  published only an ephemeral bus segment that fired before the page's
  EventSource attached on boot — permanently lost, then deduped for the
  process lifetime. TEST ×2.

**Still open:** rollback snapshots can restore a build *older than the
last-known-good* because snapshot selection = `latest.txt` written by
whatever ran last; add a version floor (never roll back to a VERSION
older than the currently-installed one) and validate snapshot coherence
(web bundle version == exe version) before applying.

## Confirmed stale documentation to reconcile (Phase 1)

- `.agent/project.json`: `test_checkpoint`/`verified_tests`/`test_count` = 1705 (actual ~1965), `head` = 5d1dcd2, `next_milestone` describes landed P1 work.
- `docs/AUTONOMY.md`: says 49 autonomy tests — actual 105.
- `docs/architecture/SELF_REPAIR.md`: says 27 tests — actual 52.
- `docs/WEB_RESEARCH.md`: browser section superseded by `docs/BROWSER_E2E.md`; lists shipped features as "next work".
- `docs/ANSWER_MEMORY.md`: schema "currently 1" — code is v2.
- `docs/LORA_SYSTEM.md`, `docs/IMAGE_WORKFLOWS.md`: "Local Code Agent" naming.
- `docs/WORKERS.md`: "self-update pipeline is a documented flow, not implemented" — STALE (selfupdate.py + flags landed); agent-lane serialization non-goal may still hold.
- `docs/IMAGE_MODULE_SPEC.md`: `face/detail refinement` — zero code hits (NOT_LANDED); mask editor, queue reorder, per-item regenerate unverified.
- `docs/POLICIES.md`: `custom` egress "reserved" (declared, not implemented).
- `docs/VOICE_SYSTEM.md`: no `setSinkId` device selection; GPU TTS providers unbundled (documented limitations).
- `SESSION_HANDOFF.md` "Remaining milestone work" — soak-round notes are historical; current work is this closure matrix.

## Priority-ordered real work remaining

1. ~~Splash: wire rollback/safe-mode recovery actions to host; consume recoveryWatchdogMs.~~ **DONE** (69894e4a + earlier; live fault dogfood verified)
2. ~~Voice: extend VoicePreset metadata (provenance/version/cadence/energy/warmth/formality/emotion_range/pronunciation_overrides).~~ **DONE** (03ddc8af)
3. Provisioning: full-stack plan + Core/Recommended/Complete/Custom profiles + per-item approval states. (medium)
4. Env + Secrets editor UI on existing APIs. (medium)
5. External file watcher for workspace editor. (medium)
6. Migration awareness tools. (medium)
7. Docker-aware workflows (detect/compose/ps/logs/health/dev-server). (medium)
8. Quality surface + lint/profiling adapters. (medium)
9. Per-page browser E2E + bounded visual regression. (medium)
10. Autonomous Git/PR/CI closure loop wiring + dogfood. (medium)
11. Brain backup/restore + MCP/code-intel live dogfood. (small-medium)
12. Clean-install dogfood + long soak + release metadata. (gate)
