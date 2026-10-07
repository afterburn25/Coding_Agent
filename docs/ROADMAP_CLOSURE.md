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
| `voice-concept-isabella` (f4a457fa) | **SUPERSEDED_BY_PRODUCTION_VOICE_SYSTEM** — one CI workflow generating ffmpeg-treated audition MP3s from the public Kokoro `bf_isabella` sample. No production code. The production voice system (Isabella preset, delivery plans, Speech Genome voice behavior) landed earlier. |
| `fix/full-core-glow` (147da018) | **SUPERSEDED** — glow renderer work is byte-identical in main (squashed via `e245aa53`); its test fix (`desktop_method_body` helper) is also already in main. Nothing unique remains. |
| `feature/cinematic-core-unlock-splash` (1374ebc9) | **SUPERSEDED_BY_PRODUCTION_SPLASH** — the 27 MB `prototypes/core_unlock_splash/` WinForms prototype exists only on that branch; curated handoff doc + manifest already live in production. Prototype binaries intentionally not carried into main. |
| local `feature/production-cinematic-splash-recovery` | Fully contained in this branch (its 3 commits = avatar + InvokeAI fix + handoff). Delete locally after landing. |

## Persona Speech Genome — completion matrix (v0.22.0 closeout)

All rows audited against real code + the soak harness
(`tests/soak_persona.py`) and the live install dogfood, not commit
messages.

| Requirement | Status | Evidence |
|---|---|---|
| Context envelope / ActiveContext / reference resolution / follow-up merge | LANDED_VERIFIED | `context/` package; TEST; orchestrator consumes envelope before lanes |
| SemanticResponse → PersonaRenderer → RenderedReply pipeline | LANDED_VERIFIED | `context/realize.py`; `_BUILTIN_RENDERER` wired in orchestrator; LIVE dogfood (3× "Who created you?" → 3 distinct in-character renders) |
| SpeechDeliveryPlan → real TTS | LANDED_VERIFIED | `voice/manager.py`: pace→speed, energy/warmth→bounded pitch/gain, seriousness calms delivery, technical/formal register dampens pace, pause_hint→bounded clause-boundary ellipsis in speech text only; `voice/vocalizations.py`: `nonverbal_rate` scales keep-probability (0 = silent); TEST ×5 |
| emphasis_spans | PARTIAL (intentional) | Carried as span-protection metadata; Kokoro path has no per-word emphasis — documented honestly, not faked |
| Answer Memory persona rendering | LANDED_VERIFIED | `render_semantic` + `wrap()` on canonical text; plan carried on `AgentResult.delivery`; TEST |
| Built-in response variation | LANDED_VERIFIED | ledger fingerprint + cursor rotation + repeat-ack pool (10); LIVE: repeat asks render differently |
| Identity fact immutability | LANDED_VERIFIED | `identity.py` canonical facts stay deterministic under persona; 20× ask soak → 0 drift, 17 unique surfaces |
| Repeat evolution | LANDED_VERIFIED | repeat index per semantic_id: rep≥1 adds honest ack, rep≥3 compresses to load-bearing fact; soak output verified |
| Opening/closing repetition control | LANDED_VERIFIED | `PhraseCooldowns` per-category pools; openings/closings banned-recent selection; soak top-opening share ~7% |
| Forms of address policy | LANDED_VERIFIED | policy none/formal/first/literal + frequency + act weights + creator boost + 4-turn cooldown; literal persona → 23% use with term rotation; plain personas → 0% |
| Humor categories affect surface | LANDED_VERIFIED | 12 `_HUMOR_QUIPS` pools; `_humor_bias` feedback multipliers; register + seriousness gates; saturation damp |
| Emotional continuity | LANDED_VERIFIED | `personality/dynamics.py` mood engine: event→mood transitions, 0.72 decay, intensity floor, persisted `persona_state.json` |
| Correction / repair style | LANDED_VERIFIED | `correct`/`acknowledge` speech acts + social-cue detection + persona-keyed repair phrasing (`calm`/`sassy`/`rude` families) |
| Confidence language by evidence | LANDED_VERIFIED | confidence stems emitted ONLY for non-verified; verified facts never hedge |
| Storytelling/explanation style | LANDED_VERIFIED | `storytell`/`teach`/`explain`/`summarize` acts; result-first vs scene-setting opening families weighted per genome |
| Question behavior | LANDED_VERIFIED | `question` closing fires only when genome `questions.frequency` > 0.4 and real next_options exist — never fabricated |
| Relationship speech | LANDED_VERIFIED | stages new/familiar/trusted/long_term + familiarity drive context; creator boost; LIVE Speech Lab showed `long_term` at 82 turns |
| Relevant memory callbacks | PARTIAL | `context_callback` opening family fires on topic return; irrelevant callbacks structurally impossible (no random-history injection); depth limited to opening phrase |
| Micro-reactions | LANDED_VERIFIED | act→category map, persona pools, cooldown 6 turns, serious ×0.25, saturation damp; soak: 16.5% rate, 0 serious leaks |
| Natural-language persona tuning | LANDED_VERIFIED | `personality/commands.py`: overlays ±20, modifiers w/ TTL+scope, modes, address set/stop, voice rate/gain/mute, reset paths — bounded, confirmed in-chat |
| Speech Lab | LANDED_VERIFIED | persona/register/situation/renders selectors + delivery-plan display + voice preview + genome summary; LIVE battery verified on install |
| Speech Lab simple/advanced split | INTENTIONALLY_DEFERRED | sliders already sectioned (Social/Humor/Intelligence/Energy/Response/Voice/Vocal); explicit simple-mode toggle is cosmetic |
| Import/export/migration | LANDED_VERIFIED | `customize.py` export/import packages + provenance; `migrate_genome` fills missing, preserves unknown keys |
| Frontend canned replies vs backend | PARTIAL | `web/app.js builtinClientReply` keeps a small local pool for the no-persona fast path only (`!personaActive`); backend semantic+renderer is canonical when a persona is on |
| 500+ render soak | LANDED_VERIFIED | 612 renders / 4 personas / 9 lanes: 0 fact drift, 0 exact-span corruption, 0 serious humor leaks, 0.3% near-dup pairs |
| 100+ turn multi-turn soak | LANDED_VERIFIED | 120-turn scripted session (chat/debug/success/failure/correction/uncertain/alert): 29 repeats across reused micro-pools, all canned-lane convergent |
| Real TTS dogfood | LANDED_VERIFIED | `/api/voice/preview` → real 24 kHz stereo WAV (3.61 s) on live install; delivery overlay applied |
| Windows app dogfood | LANDED_VERIFIED | Frozen backend + desktop host deployed to `D:\Nexus_Core`; launch → video splash → verify gate → "Nexus Core" main window; chat, persona, Speech Lab, GitHub (`afterburn25` connected) all live |

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
| Pipeline to create other authorized voice profiles | LANDED | presets.py save/save_as/rename/duplicate/delete + Voice Studio UI (DSP authoring). **Sample-import resolved**: `ChatterboxEngine.import_voice` + `POST /api/voice/voice/import` + Voice Studio "Clone a voice" card — Chatterbox is zero-shot, so "training" is registering a validated reference (validate→canonicalize→`voices/<id>/`); no fine-tuning pipeline exists to build. Presets remain DSP recipes over base voices by design (`types.py`). TEST ×4 |
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
| Per-page E2E over all Nexus pages | LANDED | `tests/test_browser_e2e.py` — real Playwright/Edge sweep of 18 pages (load + title + per-page selector + console/failed-request assertions), skips cleanly without Playwright; LIVE: 18/18 pass |
| Bounded visual regression | LANDED | `tests/visual_baseline/*.png` per-page baselines + bounded pixel-diff compare (`VISUAL_BASELINE=record|compare`); strict palette-class diff, PIL-gated |

## Phases 8–15 — workstation surfaces

| Requirement | Status | Evidence |
|---|---|---|
| Env editor per project (key/value/source/scope/inherited/validation) | LANDED | System page Environment panel — list/set/delete via `/api/environment*`; `web/system.html` + `system.js` |
| Secrets editor (name/scope/status/last-updated, never reveals values) | LANDED | System page Secrets panel — metadata-only list + set/delete; values never leave the vault |
| DB migration awareness (Alembic/Django/Prisma/EF/Flyway detect/status/generate/preview/apply/verify) | LANDED | `tools/migrations.py` — file-based detection, per-system status parsing (alembic heads/current, django showmigrations, prisma/ef/flyway), `migrations_apply` gated on `shell.execute` approval; TEST |
| Docker-aware dev (Dockerfile/compose detect, build/run/stop/logs/health/ps/ports, dev-server discovery) | LANDED | `docker_tool.py` extended — compose file detection, ps/logs/stop/start/rm, dev-server-in-container discovery (published ports ↔ dev port map); TEST |
| Unified Quality UI (tests/coverage/lint/type/audit/profile/bench/regressions) | LANDED | System page Quality panel + `lint_run`/`profile_run` adapters in buildsys + `GET /api/quality` + `POST /api/quality/{test,coverage,lint,profile}`; TEST |
| External file-change watcher (detect→mark→no silent overwrite→reload/compare/merge; self-write immunity) | LANDED | `POST /api/files/changed` mtime_ns scan + workspace poll; extern-change banner (Reload/Compare), tab markers, self-write immunity via write-response mtime |
| Autonomous Git→PR→CI closure loop (branch/worktree→implement→test→review→PR→CI inspect→repair→merge-ready; no auto-merge) | LANDED | github tools emit `ci`/`pull_request` bus events (dedupe per run+conclusion) → `autonomy/triggers.py` maps to ci_failed/ci_completed/pull_request_updated signals; AUTONOMY.md; LIVE dogfood: real `ci failure` event captured for run 37588751795 |
| Scaffold templates build/test/launch/health-check | LANDED | 15 templates (added flask_service, django_app, vue_vite, dotnet_console, dotnet_webapi, go_cli, rust_cli, python_lib); all render-verified, emitted Python compiles |
| Transactional locked-file/deferred replacement | LANDED | `LkgStore.stage_swap` + `data/lkg/swaps/<id>` staging + generic deferred-swap applier in desktop host — arbitrary relative targets, path validation, transactional apply, consumed-flag safety |

## Phases 16–20 — audits, cohesion, release, soak

| Requirement | Status | Evidence |
|---|---|---|
| Brain closure audit (export/sign/update/stale/foreign/model-replacement continuity/backup-restore/audit/paraphrase/plan-eval-learn/multi-hour) | LANDED | `export_creator_key_backup`/`restore_creator_key_backup` + `/api/nexus-brain/key-backup|key-restore` + trainer UI + TEST ×18; project.json `next_security_upgrade` corrected; multi-hour unattended dogfood pending |
| MCP live validation + code-intel dogfood | LANDED_VERIFIED | `mcp.py` stdio+HTTP clients; LIVE 2026-10-07: real `@modelcontextprotocol/server-filesystem` via stdio — connect, 14 tools imported (`mcp__filesystem__*`), `list_directory`/`read_text_file` returned real data. Note: npx.cmd shim mangles `D:\n...` launch args (upstream, reproduced via raw Popen — not the client) |
| UI cohesion pass on every page | PARTIAL | `b600e60b4` sweep found+fixed 3 defects; systematic all-page polish pending Phase 18 work |
| Version/release metadata agreement | LANDED_VERIFIED | VERSION 0.27.0 (released, tag `v0.27.0`); `sync_version.py --check` passes; all derived surfaces (project.json, csproj, .iss, pyproject, backend_version.txt) agree |
| Long-run soak (hours; mixed workload) | PARTIAL | `scripts/mission_soak.py` + `scripts/selftest` soak tier + nightly CI workflow; **bounded run 2026-10-07**: kill-mid-mission → restart → mission resumes → approvals re-granted; multi-hour run still open |

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

**Resolved (2026-10-07):** version floor + coherence landed — snapshots
record their VERSION, `latest.txt`/`floor.json` ratchet only on proven
clean sessions, rollback refuses any snapshot older than the installed
`VERSION`, and the host-side `ApplyLkgFlags` re-validates floor +
web-bundle coherence before applying (`lkg.py`, `Program.cs`; TEST).

## Stale documentation reconciliation (2026-10-07 — done)

All rows below were reconciled against the code; the list is kept as
the record of what drifted and how it was resolved.

- `.agent/project.json`: `test_checkpoint`/`verified_tests`/`test_count` → 2466 collected; `next_milestone` now reflects only the remaining dogfood gates.
- `docs/AUTONOMY.md`: 49 → 106 autonomy tests.
- `docs/architecture/SELF_REPAIR.md`: 27 → 48 tests.
- `docs/WEB_RESEARCH.md`: browser section rewritten — Edge-channel/managed-Chromium resolution, full action set, persistent sessions (`storage_state`), honest status, BROWSER_E2E reference; shipped items removed from "next work".
- `docs/ANSWER_MEMORY.md`: schema version corrected to **2**.
- `docs/LORA_SYSTEM.md`, `docs/IMAGE_WORKFLOWS.md`: "Local Code Agent" → "Nexus Core".
- `docs/WORKERS.md`: self-update no longer "documented flow" — `selfupdate.py` + deferred-swap flags landed.
- `docs/IMAGE_MODULE_SPEC.md`: face/detail refinement annotated LANDED (`refine_details` low-denoise variation pass).
- `docs/POLICIES.md` + `policies.py`: `custom` egress now **fails closed** (denies all network actions) until per-action rules exist — was declared-but-ignored, silently allowing everything.
- `docs/VOICE_SYSTEM.md`: voice-clone import documented; `emphasis_spans` limitation already honest; `setSinkId`/GPU-provider limitations stand as documented.
- `SESSION_HANDOFF.md`: chronological log; earlier "remaining" entries are historical by design — current state lives in the newest entry.

## Priority-ordered real work remaining

1. ~~Splash: wire rollback/safe-mode recovery actions to host; consume recoveryWatchdogMs.~~ **DONE** (69894e4a + earlier; live fault dogfood verified)
2. ~~Voice: extend VoicePreset metadata (provenance/version/cadence/energy/warmth/formality/emotion_range/pronunciation_overrides).~~ **DONE** (03ddc8af)
3. ~~Provisioning: full-stack plan + Core/Recommended/Complete/Custom profiles + per-item approval states.~~ **DONE** (browser-runtime plan item, licensed-weights approval gates, live replan, profile UI — 6d5ba836)
4. ~~Env + Secrets editor UI on existing APIs.~~ **DONE** (6942973… System page panels)
5. ~~External file watcher for workspace editor.~~ **DONE** (`/api/files/changed` + banner + self-write immunity)
6. ~~Migration awareness tools.~~ **DONE** (`tools/migrations.py`)
7. ~~Docker-aware workflows (detect/compose/ps/logs/health/dev-server).~~ **DONE** (`docker_tool.py` extension)
8. ~~Quality surface + lint/profiling adapters.~~ **DONE** (bc59fc7b + `/api/quality` + System panel)
9. ~~Per-page browser E2E + bounded visual regression.~~ **DONE** (b7744ef7 — 18 pages, Edge, baselines)
10. ~~Autonomous Git/PR/CI closure loop wiring + dogfood.~~ **DONE** (7b03f77a wiring + live `ci failure` event dogfood)
11. Brain backup/restore — **DONE** (already landed; metadata fixed); MCP/code-intel live dogfood still open.
12. **Gate status 2026-10-07:** clean-install dogfood **DONE** — real `NexusCore-Setup-0.27.0` artifact, checksum-verified, silent-installed to `D:\Nexus_Core_Fresh`, backend booted, `/api/health`+`/api/status` served v0.27.0 on real GPU hardware, `--self-test` passed, uninstalled clean. **Found+fixed a real defect**: `/VERYSILENT` uninstall hung on the unguarded `AskUninstallScope` modal (`UninstallSilent()` guard added). CI green on `213b97ca` (test + windows-desktop incl. installer compile + fresh-install/update smoke). Bounded soak in flight (kill→restart→resume verified). Still open: multi-hour mixed soak, live fault matrix (network cut/Defender lock/disk shortage — crash/kill/resume already exercised).
