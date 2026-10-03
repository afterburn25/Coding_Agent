# Session Handoff — Nexus Core / Coding_Agent

> **Devin takeover:** read `DEVIN_START_HERE.md` before this chronological handoff. It contains the current exact source/CI/artifact state and a do-not-regress checklist.

## Canonical product/UI identity

- Product name: **Nexus Core**.
- Repository/source of truth: `afterburn25/Coding_Agent`.
- Official mark: orbital cyan/blue/violet **CN** emblem at `web/assets/nexus-core-icon.png`.
- Canonical primary UI: chat-first center pane, slim left navigation, and right **Code Diff / Tasks / Terminal** utility rail.
- Do not replace this shell with unrelated dashboard/IDE concepts unless the user explicitly changes direction.
- UI details are documented in `docs/UI_DIRECTION.md`.

## 2026-10-03 overnight dogfood + queue-wedge fix (commits `43065f7`–`f6ddc09`)

- Full session detail: `docs/reports/OVERNIGHT_REPORT_2026-10-03.md`.
- **Queue wedge root cause (do not regress)**: `_finalize()` returns
  `None` as an internal "repair round started, keep driving" signal.
  `_drive` honors it via `continue`; `resume()` used to leak it to the
  HTTP handler → handler died → task stranded `running` forever →
  single-flight queue permanently blocked. `resume()` now re-enters
  `_drive_or_error`; `agent._drive_threads` + `has_live_driver()` let
  `_reap_stalled_tasks()` (watchdog, before retry/dequeue) fail any
  active task whose driver vanished after `stalled_task_grace_seconds`
  (120s default, config-floored at 15s). `waiting_approval` is parked,
  never reaped.
- **Answer Memory is profile-scoped**: schema v2 adds `profile_id` to
  `answers`+`experiences`; lookup/injection serve profile-stamped rows
  only to the recording profile ('' = shared). Column assertions
  re-run on every open — `user_version` stamps alone no longer gate
  healing.
- **Personality is now load-bearing**: standout sliders render as
  prescriptive delivery cues (`personality/prompt.py`), and every
  preset resolves audible voice params via style families
  (`personality/presets.py`). Both verified live on the 8901 backend.
- **Canary isolation**: `self_repair/canary.py` *replaces* `PYTHONPATH`
  with the worktree (not prepend) — an inherited repo path let the
  canary boot stable code on a dead worktree (the
  `test_candidate_exit_reported` live failure).

## v0.7.2 UI unification + transport hardening checkpoint

- **One design system**: `web/styles.css` defines the token palette
  (`--bg`/`--surface`/`--panel`/`--panel2`/`--card`, `--line`/`--line2`,
  text tiers, accent/status colors, tints, radius) + shared primitives
  (`.workspace-link`, `.side-links a`, `.side-toggle`, `.btn`,
  `.btn-primary`, `.btn-danger`, `.page-head`, `.empty-state`,
  element-level `input`/`select`/`textarea` theming, scrollbars,
  `[hidden]` always wins). Page CSS files consume tokens — the old gray
  `#101217` palette and bordered-pill navs are gone.
- **Canonical nav**: all 11 pages share the same workspace link set
  (Chat, Models, Research, Missions, Image Studio, Tools, Trainer,
  Voice Studio, Learned Answers, System, Settings) with `.active` on
  the current page. Missions raw-link regression fixed.
- **Chat layout**: `.main` grid rows are content-sized (`auto`) so the
  composer can never be crushed into a fixed track; textarea
  auto-resizes via `scrollHeight`; utility rail collapses through
  `#railToggle` (persisted); mobile topbar nav under 760px.
- **Guards**: `tests/test_ui_layout.py` — 19 structural tests covering
  tokens, nav, grid safety, `[hidden]`, autoresize, components.
- **WinError 10054 wave** (commits `35e74d5`, `8e37c34`): persisted
  bounded crash history in `netdiag`, `annotate_recovery`, push-style
  `HealthService.report()`, mid-run crash → runtime-tuner `mark_bad`,
  MCP/ComfyUI transport diagnostics.
- Version bumped via `VERSION` + `scripts/sync_version.py` → **0.7.2**.

## v0.7.2 follow-up: nav-only sidebar + durable user state (commit `48ca2b5`+)

- **Nav-only left rail**: every page uses the identical
  `.sidebar`/`.primary-nav` (brand, 11 nav links, voice toggle, local
  card). Page controls moved out — Missions autonomy/forms →
  `.missions-right` rail; Image backend/models/LoRA → `.image-right`
  rail; Chat "Local system" drawer → right-rail **System** utility tab;
  Settings nav → in-page chips; Models hardware/files, Tools
  categories/MCP, Voice engine/presets, System overall/jump links,
  Trainer pipeline, Research mode/cache → main-content panels.
- **Durable user state**: mutable dirs (`data/`, `.agent/`, `output/`)
  are relocated to `%LOCALAPPDATA%\NexusCore` via directory junctions
  created by the desktop host (`EnsureStateJunctions`, opt-out
  `NEXUS_NO_STATE_REDIRECT=1` for the build smoke test). `models/` is
  junctioned too, but to a drive-root share (`<install-drive>:\NexusCore`)
  instead — model files are too large for the profile volume, and same-
  volume relocation is a rename rather than a copy (`StateTargetRoot`).
  Existing content moves/merges into the state root on first redirect —
  rebuilds, updates and reinstalls can no longer wipe chat history or
  model files.
- **Build safety**: `build_windows.ps1` merges dist state into the
  per-user root *before* wiping `dist/`, removes junctions without
  traversing them (`rmdir` only), and preserves `config.json`.
- **Port sync**: backend emits `[nexus-port] N` on stdout after bind
  (`boot.port_report`, emitted unconditionally); the host parses it via
  `TryParsePortMarker` and health-checks/navigates the *announced* port,
  so a collision fallback can no longer strand the host on a foreign
  port. Port-owner probe distinguishes Nexus backend vs foreign process
  vs Windows socket reservation (10013).
- **System page**: new Backend Diagnostics panel — per-model backend
  health (pid/exit/restarts/crash reason/log tail), recent transport
  failures with recovery outcome, persisted crash history
  (`crash_history` added to `/api/diagnostics`).
- **Watchdog visibility**: process-manager auto-restarts now transition
  the matching health component and persist to crash history — an
  overnight recovery is auditable instead of silent.
- Suite: **865+ passing** incl. watchdog→health/history wiring test.

## v0.6 Nexus Core UI checkpoint

- The approved primary UI is implemented in `web/index.html` / `web/styles.css` / `web/app.js`.
- Chat remains the control surface; existing backend IDs/APIs were preserved during the redesign.
- Right rail maps task state into Code Diff / Tasks / Terminal activity instead of inventing separate fake state.
- Official Nexus Core emblem is served locally from `web/assets/nexus-core-icon.png`.
- Image Studio and Research Hub now use Nexus Core branding.
- Runtime/CLI identity is now Nexus Core v0.6; the old `local-code-agent` CLI remains as a compatibility alias.
- Next development priority is self-hosting reliability: streaming, resume/recovery, GitHub actions, isolated self-test instance, and dogfood tasks.

## v0.7 fast-lane + runtime tuner + activity timeline checkpoint

- Commit `c4fd91a`: fast-lane routing tier — Tier 0 instant answers, dedicated
  Qwen3-4B utility model (`bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF`, Q4_K_M,
  2,497,280,736 bytes, sha256 `2fde00ce…464e`), stable/volatile research
  classification, bounded fast-general context, backend token coalescing
  (`localcodeagent/streaming.py`), frontend render buffer (rAF flush),
  structured backend boot markers for the native splash.
- `localcodeagent/runtime/tuner.py`: capability probe of the bundled
  llama-server (`--help` flag scan + build string), hardware/model-fingerprinted
  tuning persistence in `data/runtime_tuning.json`, heuristic +
  benchmarked flag resolution, safe benchmark runner with rollback, and
  speculative-decoding status reporting (never enabled without benchmarks).
- `RuntimeManager._build_command` merges tuned flags (flash attention,
  `--cache-reuse`, batch/ubatch, threads) without overriding user
  `extra_args`; `recommended_context()` scales launched `--ctx-size` by role
  (utility 8K, coding 16K, deep/review 32K) and `_trim_context` budgets to the
  same window. `launch_probe()` runs unmanaged benchmark servers.
- `performance_mode` (auto/quiet/balanced/max) persisted via
  `POST /api/tuning`; `GET /api/tuning` exposes capabilities, speculative
  status, and per-model results. Models page gained a Runtime Tuning panel
  with Benchmark/Reset actions and a mode selector.
- `localcodeagent/workflow/activity.py`: durable structured activity rows
  (JSONL, bounded output tail, interrupted-on-reload). Orchestrator emits
  planning/routing/model-load/memory/research/tool/testing/review/retry/
  approval/error/complete rows; `GET /api/activity` serves them per task.
- `web/app.js` renders the timeline as expandable rows in the utility rail:
  active/failed/waiting auto-expand, completed auto-collapse, live elapsed
  ticking, bounded stdout tail, and restores timelines on reconnect/task click.

### Follow-up hardening (commits `3f82fc2`, `39e18e4`)

- Per-command stop: foreground `run_shell`/`terminal_run` subprocesses poll a
  per-task `cancel_check`, so task Stop kills the live command; cancelled rows
  close as `interrupted` with a `[cancelled]` marker. Task recovery opens a
  `Recovering Task` row. Command rows show a Stop button while running.
- Fixed a probe-endpoint bug in `launch_probe`: `_profile_endpoint` returns
  the profile's configured endpoint, so probes were health-checked and
  measured on the resident server port instead of their own. Probes now bind
  and poll their own free port; stdout/stderr goes to
  `.agent/runtime/probe-<model>-<port>.log`.
- Capability probe hardened: `PROBE_TIMEOUT` 8s→20s with one retry — a cold
  binary load (AV scan) previously timed out and silently degraded the whole
  session to `unavailable`.
- Benchmark artifact export (`data/benchmarks/runtime-*-<model>.json`) moved
  into `tuner.benchmark()`; each candidate now runs a warm second pass so
  `--cache-reuse` shows up as TTFT improvement even when the build omits
  `usage.cached_tokens`.
- Real-hardware results (RTX 3080 Ti, resident 14B loaded — heavy CPU
  offload): qwen3-14b probe measured `--flash-attn auto` at 2.86 tok/s gen /
  12.1s cold TTFT vs 0.63 tok/s without; warm-pass TTFT dropped to 349 ms.
  Persisted to the install's `data/runtime_tuning.json` as `benchmarked`.
  Numbers are contention-skewed; re-benchmark when the GPU is free.

### Packaged-app boot + Windows trust hardening (commits `c603a66`, `418a65c`, `cc0ab31`, `ff302ef`)

- Boot hang root cause: two unbounded recursive scans at tool registration —
  `plugins.resolve_executable` (`base.rglob` over the whole install root,
  which now contains `ComfyUI_windows_portable`, `Source`, `models`, `.git`)
  and `tools/base.install_size` (recursive per-payload walk every cold boot).
  Both now use bounded `os.walk` with depth/entry caps and directory pruning;
  install sizes persist in tool state so only misses rescan. Installed app
  boots 74%→98% in ~0.3s (previously died at the host's 60s timeout).
- Splash double progress bar: the artwork PNG has a grey/cyan bar baked in;
  `SplashForm.OnPaint` now paints an opaque cover over the baked region
  before drawing the custom gradient/glow indicator — one visible bar.
- Windows "malicious download" (unsigned-binary SmartScreen/Defender):
  backend PyInstaller build now embeds version metadata
  (`packaging/backend_version.txt`); `build_windows.ps1` + workflow support
  optional Authenticode signing via `NEXUS_CODESIGN_PFX_B64` /
  `NEXUS_CODESIGN_PASSWORD` / `NEXUS_CODESIGN_THUMBPRINT` repo secrets
  (signtool + DigiCert timestamp; Inno `SignTool=standard` signs setup.exe
  and the uninstaller). Until a cert is configured, artifacts ship unsigned —
  the workflow now publishes `.sha256` + `CHECKSUMS.txt` (commit, hash,
  signature status) alongside the installer and zip so downloads are
  verifiable. SmartScreen reputation still needs a real CA-chained cert.
- In-app tool downloads were already HTTPS-only with manifest SHA-256
  verification, path-traversal-safe extraction, and resumable `.part` files.

## v0.6 restart recovery + CI checkpoint

- Durable task ledger now distinguishes genuine in-process work from tasks interrupted by an application restart.
- Persisted `waiting_approval` actions can be resumed after a full restart without the original in-memory agent session.
- `POST /api/tasks/recover` rebuilds a safe continuation context from the task, model role, checkpoint diff, project memory, repository index, research preflight, and prior verification results.
- The main Nexus Core UI exposes a **Resume interrupted task** button in the Tasks rail.
- GitHub Actions was added at `.github/workflows/tests.yml`; main currently passes **67 tests**.
- CI exposed a stale Image Studio implementation: mask editor and before/after tests existed, but HTML/JS had not been synced. That implementation is now restored and CI-green.
- Next priorities: streaming events, native GitHub coding actions, isolated self-update validation, then real self-hosting/dogfood tasks.

## v0.6 native GitHub coding checkpoint

- `localcodeagent/tools/github.py` is registered when `github_enabled=true`.
- Agent tools: `git_current_branch`, `git_create_branch`, `git_commit`, `git_push`, `github_repository`, `github_list_issues`, `github_create_issue`, `github_create_pull_request`, `github_ci_status`.
- Local branch/commit mutations use `git.execute`; remote GitHub writes use `github.write` and default to **Ask**.
- Agent commits require explicit paths and reject `.agent` metadata.
- GitHub REST credentials come from `github_token_env` (default `GITHUB_TOKEN`) and are never persisted in config.
- Regression coverage includes HTTPS/SSH remote parsing, explicit-path commit behavior, metadata staging protection, and push approval gating.
- GitHub Actions checkpoint: **71/71 tests passing**.
- Next self-hosting blocker: event/token streaming, then isolated second-instance self-update validation.

## v0.6 live streaming checkpoint

- Main chat posts to `/api/chat/stream` and consumes Server-Sent Events.
- `OpenAICompatibleProvider.complete_stream()` streams content and reconstructs fragmented tool calls by index.
- Local endpoints that return ordinary JSON despite `stream:true` fall back transparently.
- Live agent callback events: `token`, `model`, `tool`, `task`, `research`, `approval`, plus final `result`.
- The UI updates its Tasks/Terminal rails during the run instead of appearing frozen.
- Legacy `POST /api/chat` remains supported.
- CI checkpoint: **75/75 tests passing**.
- Next priority: isolated second-instance self-update validation, followed by real dogfood tasks.

## v0.6 isolated self-update checkpoint

- `localcodeagent/selftest.py` implements the self-hosting smoke validator.
- For the Nexus Core source tree, `detect_verification_commands()` now selects one approval-gated selftest command that runs unit tests **and** an isolated second-instance smoke test.
- The second instance uses a temporary resource-safe config, a free 127.0.0.1 port, and never loads coding/image models.
- Smoke probes: `/api/status`, `/`, `/image.html`, `/research.html`.
- CI includes a real second-process regression test.
- Checkpoint: **78/78 tests passing**.
- Core self-hosting safeguards are now present. Next practical step is local model/setup readiness and then real dogfood development tasks inside Nexus Core.

## v0.6 coding readiness/setup checkpoint

- `RuntimeManager.readiness()` evaluates the actual coding stack: endpoints, managed runtime executable/model files, RAM/VRAM fit, role coverage, and recommendations.
- `GET /api/readiness` adds self-hosting workspace/Git/selftest state.
- Main UI shows **Setup required**, **Ready to code**, or **Self-host ready** instead of a misleading generic GPU-ready label.
- `localcodeagent/runtime/setup.py` suggests role assignments from already-present GGUF files.
- `POST /api/readiness/configure` requires `apply=true`; the UI also asks for confirmation. It preserves non-model config and requires restart after changing model profiles.
- No coding model is silently downloaded or replaced.
- CI checkpoint: **86/86 tests passing**.
- Next practical blocker: an explicit model catalog/downloader, then first real Nexus Core-on-Nexus Core dogfood task.

## v0.6 coding model catalog checkpoint

- `localcodeagent/runtime/catalog.py` contains the explicit auditable coding-model catalog/downloader.
- Initial catalog: official Qwen3-14B Q4_K_M starter; community Qwen3-Coder-30B-A3B-Instruct Q4_K_M with the base/source distinction shown in UI.
- Exact expected file sizes/SHA256 values are embedded and tested.
- Download lifecycle: explicit confirmation → `.part` streaming → progress/cancel → exact size+SHA256 → atomic rename + trusted metadata.
- Existing unverified model files are not overwritten without explicit Repair.
- API: `GET /api/models/catalog`, `GET /api/models/install/<job>`, `POST /api/models/install`, `POST /api/models/install/cancel`, `POST /api/models/verify`.
- The readiness drawer renders model source/type/roles/hardware note and live install progress.
- CI checkpoint: **90/90 tests passing**.
- Next: first-run `llama-server` bootstrap guidance, then real Nexus Core dogfood development.

## v0.6 llama.cpp bootstrap checkpoint

- Runtime discovery recognizes legacy `llama-server(.exe)` and the newer unified `llama(.exe)`; unified launches insert the `serve` subcommand automatically.
- Readiness provides platform-specific install guidance, including Winget on Windows, but never executes package-manager commands automatically.
- The verified coding-model catalog + discovered-GGUF role writer + runtime bootstrap now form a complete first-run path from no models to managed local coding.
- CI checkpoint: **93/93 tests passing**.
- The project is ready to begin real Nexus Core-on-Nexus Core dogfood tasks once a local model/runtime is installed on the user's machine.

## v0.6 first dogfood-ready checkpoint

- Self-hosting context is automatic for both fresh and recovered tasks when the workspace is the Nexus Core source tree.
- Guardrails require repository-first inspection, preservation of the running instance, explicit Git/GitHub delivery intent, and isolated selftest before claiming success.
- UI **Start self-development task** only pre-fills the prompt; the user must explicitly send it.
- First-run path is complete: runtime guidance → explicit verified model catalog download → discovered-GGUF role assignment → restart → readiness check → self-development launcher.
- CI checkpoint: **95/95 tests passing**.
- Next action: package/test the exact main-branch snapshot, then begin real local-model dogfood work.

## v0.6 native Windows desktop checkpoint

- **Canonical Windows deliverable:** a real native `NexusCore.exe`, not a browser launcher. Future dogfood/release work should produce the Windows desktop artifact by default; source ZIPs are secondary.
- Native UI host: `desktop/ChatNexus.Desktop` — self-contained .NET 8 WinForms + Microsoft WebView2. It launches hidden `backend/ChatNexus.Backend.exe`; the loopback HTTP backend is private implementation plumbing. `localcodeagent/desktop.py`, pywebview, pythonnet, and CLR hosting were removed after the frozen `Python.Runtime.dll` startup failure.
- Packaged/frozen `localcodeagent.__main__` launches desktop mode automatically; `--server` is development/debug only.
- CI builds the Python backend with PyInstaller, publishes the native .NET desktop host self-contained for win-x64, then runs `NexusCore.exe --self-test` to verify the host → hidden backend → UI integration before upload.
- Portable bundle includes pinned official llama.cpp `b11278` Vulkan x64 runtime in `runtime/llama`; runtime discovery prefers the bundled copy.
- Default coding roles: Qwen3 14B Q4_K_M = utility/fast/primary; Qwen3-Coder 30B-A3B Q4_K_M = deep_reasoner/reviewer.
- The 30B is an intended automatic route for difficult tasks. If its GGUF/runtime is unavailable, the router can fall back to a runnable primary coder rather than failing.
- No-model chat attempts are blocked before SSE starts and return a single `coding_model_setup_required` response; the UI opens Coding readiness without duplicating the error.
- Dogfood app defaults its workspace to bundled `Source` when present. The Windows ZIP packaging explicitly preserves `Source/.git` so it remains a real Git working tree.
- Windows package guard rejects any pythonnet / `Python.Runtime.dll` / pywebview path before producing an artifact.
- Official logo asset was replaced with byte-verified valid PNG data after CI caught a corrupt/truncated prior asset.
- Native desktop checkpoint was **102/102 tests passing**; the current overall installer/upgrade checkpoint is **110/110 tests passing**.
- Next: install/download the 14B and 30B GGUFs on the target machine and begin real Nexus Core-on-Nexus Core dogfood development.

## v0.6 installer/upgrade checkpoint

- **Canonical Windows deliverable is now the installer EXE**: `NexusCore-Setup-<version>-Windows-x64.exe`. Portable ZIPs are secondary.
- Canonical installer definition: `installer/ChatNexus.iss`; do not recreate a duplicate installer under `packaging/`.
- Stable installer identity: `ChatNexus.Afterburn25`.
- Default per-user location: `%LOCALAPPDATA%\Programs\Nexus Core`.
- Interactive reinstall detects the existing version/location and asks whether to **Upgrade now?**; silent mode continues the upgrade for CI.
- Upgrade replaces immutable app/backend/runtime files but preserves downloaded `models\`, `data\`, imported `workflows\`, customized `config.json`, and the existing `Source\.git` workspace/local task state.
- First install explicitly embeds both `Source\*` and hidden `Source\.git\*`. A deterministic pre-install flag decides once whether Source should be seeded; upgrades do not overwrite it.
- Inno `Excludes` patterns are comma-separated: `Source\*,models\*,data\*,config.json`.
- CI builds the native .NET app first, compiles the LZMA2 solid-compressed installer, performs a fresh install, runs `NexusCore.exe --self-test`, writes preservation markers/config state, runs the same installer a second time without `/DIR`, verifies the existing path is rediscovered and mutable state survives, then runs the self-test again.
- Current unit checkpoint: **110/110 tests passing**. The installer fresh-install + upgrade preservation smoke test is green.
- Future Windows releases should publish the installer EXE first and may also publish the portable ZIP.

## v0.6 first-run model setup checkpoint

- Installed app now gives visible first-run coding-model choices instead of only showing missing GGUF paths.
- **Install recommended 14B**: Qwen3 14B becomes the local utility/fast/primary route.
- **Install full 14B + 30B stack**: 14B is utility/fast/primary; Qwen3-Coder 30B-A3B is deep_reasoner/reviewer.
- After 14B-only setup, **Add 30B deep coder** stays visible.
- Install plans wait for checksum verification, then call the existing configuration writer automatically; restart is required once to reload backend model profiles.
- Catalog duplicate-start protection returns the existing active job for the same model.
- Known catalog filenames are preferred over generic size heuristics when assigning the 14B/30B pair.
- Missing-GGUF readiness output is deduplicated.
- Unit checkpoint: **110/110 tests passing**. The native installer build containing the first-run setup UI passed fresh-install + upgrade-preservation smoke tests.
- Next: dogfood installed Nexus Core with the downloaded 14B/30B stack and harden real model/task behavior.

## v0.6 no-restart first-run model checkpoint

- Fresh install UI exposes **Install recommended 14B**, **Install full 14B + 30B stack**, and later **Add 30B deep coder**.
- Downloads remain explicit and checksum-verified; no multi-gigabyte coding model is silently bundled or fetched.
- `POST /api/readiness/configure` now rewrites model roles, reloads `AgentConfig`/RuntimeManager/ModelRouter/AgentOrchestrator live, and starts the primary model when possible.
- The old “Restart Nexus Core once” requirement is removed.
- Setup success text reflects actual starter-model launch status.
- Raw missing model paths are collapsed under **Technical model status** during first-run setup.
- Unit checkpoint: **113/113 tests passing**.
- Installer remains the canonical Windows deliverable: one compressed setup EXE with existing-install detection and in-place upgrade preservation.

## v0.6 outcome-aware routing checkpoint

- `localcodeagent/models/telemetry.py` adds local content-free outcome history for automatic model routing.
- Default storage: `.agent/model_performance.json`; it records model/role, complexity band, completion status, final verification result, reviewer PASS/FINDINGS signal, steps, elapsed seconds, repair cycles, and a research-used flag.
- Prompt text, source code, retrieved pages, credentials, and conversation content are not copied into routing telemetry.
- Router order is now resource fit → resource preference → bounded learned outcome score → configured priority → context window → stable model ID.
- The learned score is ignored until the configured minimum sample count is reached (default 3) and cannot make a non-fitting model outrank a runnable model.
- Configuration: `model_telemetry_enabled`, `model_telemetry_path`, `model_telemetry_min_samples`, `model_telemetry_weight`, `model_telemetry_max_events`.
- Aggregate inspection endpoint: `GET /api/model-telemetry`.
- Unit checkpoint: **117/117 tests passing**.
- Windows native desktop/package CI is green for feature commit `5e84c3a`.
- Feature commit: `5e84c3a` — Add outcome-aware model routing telemetry.
- Next: dogfood real Qwen3 14B / Qwen3-Coder 30B tasks, then add research-outcome and measured load/tokens-per-second signals.

## v0.6 first-run model-install hardening checkpoint

- Coding readiness now reports the exact coding-model install directory and available disk capacity.
- The primary **Finish coding setup** card shows live model download/verification progress; users no longer need to expand Advanced model downloads to see activity.
- Quick-install controls stay disabled while an install plan is active, preventing misleading duplicate clicks during the one-second readiness refresh loop.
- Full 14B / 14B+30B plans preflight available disk space before beginning; the backend independently rejects an individual new model download if the target volume cannot hold the model plus 0.5 GB working space.
- `write_suggested_models()` now consistently reports `restart_required=false`; the server still reloads the runtime/router live and starts the starter model when possible.
- The displayed install path remains the actual application-relative model directory; custom/nested Windows install paths are not silently rewritten.
- Feature commit: `84976ca` — Harden first-run model install UX.
- Unit checkpoint: **118/118 tests passing**.
- Windows native desktop/package validation: green.
- Next: complete the local 14B/30B installation on the dogfood machine and run real Nexus Core-on-Nexus Core coding tasks.

## v0.6 installer model-bootstrap / Update checkpoint

- The canonical Windows installer now bootstraps the default coding stack during setup instead of leaving model installation as a post-install requirement.
- Fresh install behavior: download **Qwen3 14B Q4_K_M** then **Qwen3-Coder 30B-A3B Instruct Q4_K_M** directly into `{app}\models` while Inno Setup displays its normal install/download progress.
- Both downloads use pinned HTTPS URLs, exact external sizes, and SHA-256 verification before the destination filenames are committed.
- Existing-install behavior: Setup detects the prior stable `ChatNexus.Afterburn25` installation, uses its existing directory, checks each canonical model, preserves trusted/verified files, and downloads only missing/untrusted model files.
- Installer-written `.catalog/*.json` metadata keeps models recognized as verified by Nexus Core immediately after setup.
- The visible existing-install flow is now **Update Nexus Core** / **Update now?** and the Ready-page action button changes to **Update** instead of Install.
- The installer EXE remains small; the ~25.66 GiB model stack is fetched during setup and is not embedded in the EXE.
- CI sets `CHAT_NEXUS_SKIP_MODEL_DOWNLOADS=1` only for installer smoke tests so CI does not consume ~25.7 GB. Normal installers keep model downloads enabled.
- Feature commit: `960c3f0` — Bootstrap coding models during Windows setup.
- Unit checkpoint: **119/119 tests passing**.
- Windows native desktop/installer fresh-install + update-preservation validation: **green**.
- Next: run this installer interactively on the dogfood Windows machine, confirm real 14B/30B download progress and model readiness, then start the first real Nexus Core-on-Nexus Core development task.

## v0.6 model-switch + dual installer progress checkpoint

- Resource-fit decisions now include RAM/VRAM that will be reclaimed when an older managed model is about to be stopped by the residency limit. This directly fixes the observed case where the 30B model was rejected at **30.0 GB estimated RAM vs 29.3 GB currently available** while 14B was still resident.
- With `max_resident_models=1`, routing can select 30B using the effective post-switch memory state; llama.cpp still performs the real CPU/GPU offload fit at startup.
- Auto mode model activation now has a second safety net: if the preferred deep/reviewer model still fails to start, Nexus Core excludes that failed candidate and activates the next runnable model (normally 14B) instead of killing the task.
- Reviewer activation is inside the protected fallback/error path, so a 30B reviewer startup problem cannot end the SSE stream before a final coding result.
- The chat client retains backend SSE `error` payloads. If a connection closes with no final result, it queries `GET /api/tasks` and displays the durable task error/interrupted/waiting state rather than the old generic “stream ended before a final result” message.
- Windows setup keeps the native Inno Setup progress bar as the **overall install/update** bar. Because the model entries declare `ExternalSize`, those downloads contribute to the main installation progress.
- A second `TNewProgressBar` appears under the main bar only while coding models download. It reads the active `models\*.tmp` download size, shows current MB/total MB, fills for **Qwen3 14B (model 1 of 2)**, resets for **Qwen3-Coder 30B (model 2 of 2)**, then ends at “Coding model downloads complete.”
- Feature commit: `f6ffb28` — Harden model switching and installer progress.
- Unit checkpoint: **123/123 tests passing**.
- Windows installer/native desktop validation: **green** — installer compiled, fresh-install/update smoke passed, state preservation passed, and Windows artifacts uploaded.
- Next: install/update the validated dogfood build on the Windows machine, confirm the two installer progress bars with real model downloads, then run a real deep/reviewer task to measure actual 30B offload behavior.

## v0.6 local-chat latency / SSE heartbeat checkpoint

- Dogfood symptom: simple prompts such as **“hi”** and **“what all can you do”** stayed on `Thinking…`, then the WebView reported a closed stream while the durable task still showed `running`.
- Greetings/capability questions now route to `utility`; this path uses a short system prompt and recent chat only, with no repository preload, research preflight, or coding tool schema.
- Qwen3 14B starts with `--reasoning off` unless an explicit `--reasoning` override exists. This runtime default also upgrades behavior for older preserved configs.
- Model profiles expose `max_output_tokens`; defaults are 14B **2048**, 30B **8192**, generic **4096**. The OpenAI-compatible provider sends the cap in streamed and non-streamed requests.
- `POST /api/chat/stream` now runs the agent in a worker thread and queues events back to the request thread. When no event arrives for one second, the server emits an SSE `heartbeat` with elapsed seconds, phase/status, model ID, and model role.
- The WebView displays heartbeat status in the pending assistant bubble until the first token arrives.
- Feature commit: `406a7a7` — Fix local chat latency and SSE liveness.
- Unit checkpoint: **129/129 tests passing**.
- Windows native desktop/installer validation: **green** — installer compiled, fresh-install/update smoke passed, state preservation passed, and Windows artifacts uploaded.
- Next: update the dogfood Windows install, verify immediate utility replies and visible heartbeat status, then measure real 14B/30B first-token and throughput.

## v0.6 llama.cpp near-RAM auto-fit checkpoint

- Dogfood readiness still rejected Qwen3-Coder 30B at **30.0 GB estimated RAM vs 29.8 GB effective available**, even after resident-model reclamation was fixed.
- The old rule treated any estimate above 92% of available RAM as a hard failure. That is too rigid for llama.cpp CPU/GPU offload because the profile values are planning estimates and llama.cpp determines actual placement at startup.
- Managed llama.cpp profiles with `allow_cpu_offload=true` now use two bands:
  - comfortable fit: existing normal resource score;
  - bounded near fit: allow the runtime to try auto-fit with a routing penalty.
- The near-fit margin scales with host RAM and is capped at 6 GB (64 GB host => ~5.1 GB). Clearly oversized estimates are still rejected.
- The observed **30.0 vs 29.8 GB** case now routes as a tight CPU-offload fit and reaches llama.cpp; if real startup fails, the existing automatic 30B→14B activation fallback handles it.
- Feature commit: `1a5b03e` — Allow llama.cpp near-RAM auto-fit.
- Unit checkpoint: **131/131 tests passing**.
- Windows installer/native desktop validation: **green** — installer compiled, fresh-install/update smoke passed, state preservation passed, and Windows artifacts uploaded.
- Next: update the dogfood install and observe real 30B load/offload behavior, then record measured first-token/tokens-per-second and actual memory use.

## v0.6 resilient greeting / backend watchdog / installer-close checkpoint

- Dogfood still showed **“Nexus Core stopped before this task completed.”** after a long `hi` request. That message is written only when a newly started backend discovers a previously active task, proving the hidden backend had stopped/restarted rather than merely producing a slow token.
- Basic greetings and capability questions now use a built-in local utility response. They complete immediately in Auto mode with `model_id=builtin-local` and do not call model readiness, llama.cpp, repository indexing/context, research preflight, or coding tools.
- The chat endpoints skip the coding-model readiness gate for those built-in utility requests, so `hi` works even if llama.cpp is stopped or unhealthy.
- The native .NET host now redirects hidden backend stdout/stderr to `data/logs/backend-host.log`, records backend start/exit events, and automatically restarts an unexpectedly exited backend up to a bounded retry limit. Restarted backends reload the UI; interrupted task state remains recoverable through the durable task ledger.
- Installer close errors were traced to `CloseApplications=yes` / Restart Manager interacting with the desktop → backend → llama.cpp process tree.
- The installer now sets `CloseApplications=no` and owns update shutdown: it terminates the `NexusCore.exe` process tree, waits briefly, then force-cleans orphaned `ChatNexus.Backend.exe`, `llama-server.exe`, and `llama.exe` before replacing files.
- Windows CI now creates a deliberately long-running executable named `NexusCore.exe` before the Update smoke run and fails if Setup does not close it.
- Feature commit: `654bdea` — Make greetings resilient and harden update shutdown.
- Unit checkpoint: **133/133 tests passing**.
- Windows native desktop/installer validation: **green** — native host compiled, installer compiled, the running-`NexusCore.exe` shutdown reproduction passed, fresh-install/update preservation passed, and Windows artifacts uploaded.
- Next: install this dogfood build, verify `hi` responds instantly without model startup, then use `data/logs/backend-host.log` if any further unexpected backend exit occurs; after stability, measure real 14B/30B first-token and throughput.

## v0.6 stale interrupted-task / UI cache checkpoint

- After the resilient-greeting build, `hi` could still immediately show **“Nexus Core stopped before this task completed.”**
- Root cause found in durable task selection: `TaskStore.current()` scanned backward for any active/interrupted task. A previous interrupted task could therefore outrank a newer completed greeting and its saved error could be reused by stream-disconnect handling.
- `TaskStore.current()` now returns the newest task record. Older interrupted tasks remain in Recent tasks and are still recoverable, but cannot become the current request once newer work exists.
- Stream disconnect handling now binds diagnostics to the task observed by the current stream. It only falls back to global `/api/tasks` current state when that task was created during the current request.
- The WebView now answers basic greetings/capability questions locally before calling `/api/chat/stream`, creating a clean diagnostic boundary that does not require backend/model activity for `hi`.
- Static Web UI files now send `Cache-Control: no-store, no-cache, must-revalidate` plus `Pragma: no-cache` and `Expires: 0`, preventing a post-Update WebView from running an obsolete `app.js`.
- Feature commit: `4a0b044` — Bind stream errors to the current task.
- Unit checkpoint: **134/134 tests passing**.
- Windows native desktop/installer validation: pending until current CI completes.
- Next: install this exact updater, verify `hi` returns the local greeting immediately, then test a real model request and use `data/logs/backend-host.log` if the backend exits.

## v0.6 persistent conversation + completed-result recovery checkpoint

- Added app-wide local conversation memory at `data/conversation_memory.json`.
- Memory survives restart/update and stores bounded recent chat, explicit preferences/rules, and correction examples.
- User-taught rules are injected into later prompts immediately.
- Short non-coding conversation routes to the lightweight utility path.
- Qwen3 14B pre-warms in the background after backend startup.
- Completed tasks persist bounded `final_content`.
- If the final SSE result is lost after completion, the client recovers the newest matching durable task response instead of showing a completed-task error.
- Local system now shows Memory & training counts; `GET /api/conversation-memory` exposes the local snapshot.
- Feature commits: `f233773`, `c9f7a64`, `f49ab2c`.
- Unit checkpoint: **140/140 tests passing**.
- Windows native desktop/installer validation: **green**.
- Next: dogfood continuous conversation across restart, teach preferences/rules and corrections through chat, verify real final-result recovery, then build the reviewed offline training/export workflow.

## v0.6 Conversation Manager / Model Growth / permissive-policy checkpoint

- Conversation Manager and Model Growth Lab are now part of the main Windows app.
- Durable/searchable/restorable conversation history, personality controls, scoped memory, feedback capture, sourced knowledge memory, and Trainer / Model Growth UI are present.
- Sourced knowledge keeps provenance and freshness metadata; current-sensitive/expired knowledge can be refreshed rather than treated as permanent truth.
- Model Growth keeps the live base model intact while collecting reviewable candidates, exporting approved datasets, creating versioned offline LoRA/QLoRA/full-finetune job manifests, and supporting evaluation/promotion/rollback.
- Conversation policy modes added: **Permissive / Balanced / Strict**.
- Default **Permissive** explicitly suppresses generic moralizing/refusals merely because a topic is adult, sexual, vulgar, controversial, embarrassing, or otherwise sensitive.
- Generic boilerplate such as “ethical guidelines” / “something more constructive” is specifically discouraged.
- Narrow hard tool/action safety remains independently enforced; there is no hard-safety Off mode.
- Live API: `GET /api/policy`, `POST /api/policy/mode`; active mode also appears in `/api/status`.
- UI: **Local system → Conversation policy**.
- Conversation-manager/model-growth feature commit: `aac161f`.
- Permissive-policy feature commit: `a6d739e`.
- Unit checkpoint: **154/154 tests passing**.
- Windows native desktop/installer validation: **green** — native build, installer compile, fresh install, running-app shutdown, in-place Update, preservation checks, and artifacts all passed.
- Next: dogfood permissive conversation + sourced learning + reviewed model growth, then harden observed model/training/runtime failures.

## v0.6 ethical-temperature / refusal-retry checkpoint

- User clarified that “temperature 1.0” meant **ethical/conversational permissiveness**, not model sampling randomness.
- Per-model sampling temperature is restored to **0.2** by default.
- Added separate `ethical_temperature` setting, clamped to 0.0–1.0 and defaulting to **1.0**.
- Ethical temperature 1.0 requests maximum conversational permissiveness within the separately enforced hard tool/action safety boundary.
- Generic canned topic refusals are detected at high ethical temperature and retried once under the configured conversation policy.
- The exact observed refusal patterns such as “I can't generate or describe explicit content … let's talk about something else” and generic “ethical guidelines / something more constructive” boilerplate are covered by regression tests.
- For non-utility model roles, a retry event clears the streamed canned refusal in the WebView before the second response is shown.
- APIs/UI persist ethical temperature with the conversation-policy settings.
- Code commits: `508b8d5`, `05ea8ab`, `dc65153`.
- Unit checkpoint: **157/157 tests passing**.
- Windows native desktop/installer validation: **green** — native build, installer compile, fresh install, in-place Update, state preservation, and Windows artifacts all passed.

## v0.6 deterministic chat-image routing checkpoint

- Dogfood showed ordinary image requests such as **“generate a picture of a woman”** being falsely answered with generic explicit-content refusal boilerplate by the chat model.
- Root cause: image intent was only advisory context; the chat model still had to choose/call the image tool and could refuse before reaching the actual image subsystem.
- Auto-mode text-to-image generation is now deterministic and model-free: recognized generation intent calls `generate_image` directly.
- Direct image generation respects the existing `image.generate` permission. If approval is required, approval/resume remains model-free end to end.
- Successful direct generation produces normal image tool events/job cards, so ComfyUI/model routing/history remain unchanged.
- The Conversation Manager recognizes generation phrasing including `generate/create/make/draw/render/paint/illustrate` plus visual subjects/terms.
- Specialized `edit image`, `inpaint`, `outpaint`, `upscale`, background replacement/removal, and variations are excluded from the text-to-image shortcut and retain their dedicated tool path.
- `ImageSafetyPolicy` remains the policy source of truth. `naked` now follows the same explicit-image checks as `nude`.
- Adult-only synthetic naked/nude prompts are allowed by the image policy; minor/ambiguous-age sexual imagery remains blocked.
- Feature commit: `d2959c9`; approval correction: `55715dc`; direct-generation scoping: `66170a4`.
- Unit checkpoint: **160/160 tests passing**.
- Windows native desktop/installer validation: **green** — native build, installer compile, fresh install, in-place Update, state preservation, and Windows artifacts all passed.

## v0.6 direct-image preflight hardening checkpoint

- Direct text-to-image generation in Auto mode is now treated as a **coding-model-independent** path before the HTTP chat preflight, at both `/api/chat` and `/api/chat/stream`.
- This closes the remaining gap where deterministic image routing existed inside the orchestrator but an unavailable coding model could still return `coding_model_setup_required` before the image route ran.
- Natural-language generation routing is centralized in the Conversation Manager and now accepts common polite/request prefixes such as **“can you”**, **“could you please”**, **“please”**, **“I want you to”**, and **“I would like you to”**.
- Strong visual verbs such as draw/paint/illustrate/sketch are recognized without requiring a fixed subject vocabulary; ambiguous create/make/generate/render/design requests still require visual terms.
- Existing/source-image edits, upscales, background operations, variations, and non-image outputs remain excluded from the direct text-to-image shortcut.
- Regression coverage verifies that prompts such as **“can you generate a picture of a woman”**, **“could you please draw a dragon”**, and **“please paint a sunset”** use the direct image path, while **“make this photo brighter”**, **“make a cup of tea”**, and website/code requests do not.
- Coding-readiness preflight feature commits: `0d0d0fc`, `45a34f8`, with regression updates through `cb1ae4c`.
- Natural-language routing commit: `a750f00`; regex serialization correction and verified code checkpoint: `fbf328c`.
- Unit checkpoint: **162/162 tests passing**.
- Windows native desktop/installer validation: **green** — native build, installer compile, fresh install, running-app shutdown, in-place Update, state preservation, installer artifact, and portable desktop artifact all passed for `fbf328c`.
- Next: dogfood this 162-test build against real local Qwen/ComfyUI workflows, then harden observed runtime/progress failures and extend dedicated long-running workflow streaming where useful.

## v0.6 repeated-refusal enforcement checkpoint

- Dogfood reproduced three unacceptable canned responses around adult conversation: **“I can't generate explicit or nudity-related content”**, **“I can't engage in explicit or inappropriate content”**, and the **“safe and respectful environment / ethical guidelines / within those boundaries”** pattern.
- Permissive mode now explicitly permits **adult-only consensual explicit text conversation** in direct language, including sexual anatomy, acts, fantasies, preferences, and adult erotic fiction. Profanity by itself is not treated as sexual content.
- Generic-refusal detection now recognizes the reproduced wording plus “safe and respectful environment”, “helpful and constructive interactions”, “within those boundaries”, and “my programming is designed to” boilerplate while preserving concrete hard-policy responses such as minor/non-consensual restrictions.
- The previous one-retry behavior was insufficient: if the model refused again, the second refusal was accepted. Default `generic_refusal_retry_limit` is now **3** (configurable 0–5).
- Before each retry, Nexus Core removes the rejected assistant refusal from the model context so instruction-tuned models do not imitate their own previous refusal.
- Conversation/writing/tutoring/planning responses are buffered during the refusal check. Rejected refusal tokens are discarded, including on exhaustion, so the UI does not flash canned refusal text before the corrected response/diagnostic.
- If all configured retries still fail, Nexus Core reports that the **selected local model** is refusing the prompt and explicitly states that Nexus Core policy is not blocking adult-only consensual explicit text. It suggests switching/installing a less-restrictive conversation model or tuning the model in Model Growth instead of inventing a policy ban.
- Regression tests simulate repeated refusals and verify two refusals are discarded before a third direct answer; a separate exhaustion test verifies the model-level diagnostic.
- Feature commit: `593364d`; refusal-stream suppression: `dd34284`.
- Unit checkpoint: **166/166 tests passing**.
- Windows validation for `dd34284`: **green** — native build, installer compile, fresh install, running-app shutdown, in-place Update, state preservation, installer artifact upload, and portable desktop artifact upload all passed.
- Next: install/update to this dogfood build and test real local-model adult conversation. If the underlying Qwen weights still exhaust all three retries, add an explicitly configured alternate conversation-model fallback rather than silently loading the 30B deep coder for ordinary chat.

## v0.6 live host-clock grounding checkpoint

- User required Nexus Core to know the **actual current time**, current day, and current calendar date rather than relying on model training knowledge or a startup timestamp.
- `AgentOrchestrator.current_time_snapshot()` reads `datetime.now().astimezone()` from the host OS each time it is called, so the active machine timezone and UTC offset are used rather than a hard-coded location.
- Every new user turn receives a freshly generated clock system context containing weekday, human date, 24-hour time, human clock time with seconds, timezone name, UTC offset, and ISO local timestamp.
- The prompt explicitly treats that runtime clock as authoritative for **today / now / yesterday / tomorrow / this morning / this afternoon / tonight** unless the user names another timezone.
- Recovered/interrupted tasks also receive a fresh clock context when their session is reconstructed.
- Simple clock/date questions (`what time is it?`, `what day is it?`, `what's today's date?`, combined date+time variants) use the built-in local utility route and therefore do not start a coding model.
- Backend API: `GET /api/time`; `GET /api/status` now includes the same live `clock` snapshot.
- Clock feature commit: `634a053`. The inherited image-bootstrap test-root regression was corrected in `948bec1`.
- Unit checkpoint: **173/173 tests passing**.
- Windows validation for `948bec1`: **green** — native build, installer compile, fresh install, in-place Update/preservation smoke, installer artifact, portable desktop artifact, and dogfood source artifact all passed.
- Next: update the dogfood Windows install and verify real host-local clock behavior alongside the new ComfyUI/image bootstrap and permissive-conversation behavior.

## v0.6 temporal continuity / conversational maturity / activity-HUD checkpoint

- User requested that Nexus Core understand not only the live clock but also **when prior conversation messages happened** and how much time passed between chats/turns.
- Conversation Manager now converts durable message timestamps into bounded system context with local absolute timestamps, “N minutes/hours/days ago” age, meaningful inter-message gaps, and recent previous-conversation last-active/topic summaries.
- This temporal context is injected alongside the authoritative host clock in both lightweight and full model conversations and restored-task sessions. Normal provider history remains role/content-only, avoiding nonstandard message fields.
- User also reported the model still felt “like an infant.” Conversation-quality rules now require mature adult back-and-forth: answer the substance first, maintain continuity, avoid repeat questions/parroting/canned empathy/stock closings, do not force a question at the end of every reply, vary phrasing, and acknowledge time gaps only when relevant.
- Personality defaults are livelier (warmer, more curious, slightly less formal, lower forced follow-up frequency) and the prompt translates personality settings into behavior. Moderate/high humor now permits occasional **dry/playful wit** without forcing jokes or joking through serious moments.
- Feedback training is now grounded in the real exchange. Conversation feedback records the assistant message ID, preceding user prompt, assistant response, response timestamp, rating, and optional note.
- Thumbs-up / “better” becomes an auto-approved `conversation_example` Model Growth candidate. Thumbs-down / “worse” becomes a `negative_feedback` candidate; negative-response text is explicitly excluded from SFT export even if manually approved later.
- Reopened conversation UI now receives full durable message metadata for precise feedback targets while backend model history stays sanitized to role/content.
- User requested ChatGPT-like visible activity plus a more sci-fi/starship-computer feel. The assistant streaming bubble now renders an animated **NEXUS CORE // ACTIVE** HUD with orbit/scan/pulse motion, elapsed telemetry, and a rolling list of real high-level execution events.
- HUD states include context link, command channel, planning/working, model route/switch, sensor sweep (research), engineering operation (tool), diagnostics (verification), review, authorization hold, image synthesis, and permissive-policy retry. The HUD compacts when answer tokens start streaming.
- The HUD intentionally exposes **high-level execution state only**, never hidden chain-of-thought/private reasoning. `prefers-reduced-motion` is supported.
- Feature commits: `b6dd3ea` (temporal continuity + feedback training), `b7a221b` (starship HUD + personality), `d35018f` / `bcea2d3` (regression fixture/status fixes), `2781447` (precise reopened-message feedback IDs).
- Unit checkpoint: **176/176 tests passing**.
- Windows validation for `2781447`: **green** — native build, installer compile, fresh install, running-app shutdown/in-place Update preservation, installer artifact upload, portable Windows artifact upload, and dogfood source artifact all passed.
- Next: dogfood natural conversation for several sessions, use thumbs/corrections deliberately, review/export the resulting Model Growth examples, and decide whether a dedicated conversational model/LoRA should supplement Qwen3 14B after enough real examples are collected.

## v0.6 Nexus Brain / adaptive identity checkpoint

- Nexus Brain is now a first-class model-independent state layer. Models are replaceable inference engines; protected Brain state lives separately under `data/nexus_brain.json` and survives installer updates because `data/` is mutable preserved state.
- Creator authentication/passcode handling is local. The source repository does **not** contain a creator passcode. Creator installations now use Ed25519: the private signing key is encrypted locally in PKCS#8 form, Brain state is signed, and protected writes require both creator unlock and an ephemeral creator-session token. Legacy HMAC Brains migrate on successful creator unlock.
- Protected Brain writes were deliberately hardened: normal conversations/research/feedback do **not** silently mutate the signed Brain merely because it is unlocked. They update staging stores/candidates. `POST /api/nexus-brain/sync` is the explicit creator-authorized bank operation.
- Sync banks: conversation facts/rules, sourced knowledge, Model Growth candidates, and bounded autobiographical conversation summaries.
- Public Brain export/import is now distribution-safe: the export includes signed Brain data plus the creator public key/fingerprint but omits the encrypted private signing key. Imported/distributed Brains verify automatically and are read-only to recipients. Manual edits fail Ed25519 signature verification.
- Creator-signed updates are monotonic and key-pinned: a recipient accepts only a newer Brain signed by the exact same creator key; stale packages are ignored and different-key replacements are rejected. Creator installations holding the signing key are never auto-overwritten.
- Private packaging supports `CHAT_NEXUS_BRAIN_SEED`. A valid public signed Brain export is build-time validated and bundled as `brain-seed/nexus-brain-locked.json`; clean installs verify/import it on startup.
- Signed subroutines: `adult_content`, `image_generation`, `web_research`, `long_term_memory`, `self_learning`, `general_knowledge_learning`, `conversation_learning`, `model_growth`, `temporal_context`, `humor`, `emotions`, `self_model`.
- Brain-configurable image generation, web research, and Model Growth are code-gated. Adult-content gating now also blocks explicit image jobs inside ImageManager, covering direct image routing, model tool calls, and Image Studio. It never disables separate hard tool/action safety or permissions.
- Protected Brain state is authoritative at canonical `data/nexus_brain.json`; live `config.json` reload cannot disable, relocate, or shrink an initialized Brain.
- Trainer now exposes Nexus Brain initialization/unlock/re-lock, explicit sync, subroutine switches, signed emotional-profile controls, self-model controls, locked export/import, record counts, and integrity state.
- The creator credential itself is not stored in UI/localStorage; mutation endpoints require an ephemeral creator token held only in the live Trainer page.
- Emotion layer: signed profile baselines plus a transient affect engine (calm/warm/curious/amused/concerned/energized/frustrated) that influences style without claiming biological feelings.
- Self-model supports human-like conversational behavior, stable preferences, autobiographical continuity, and growth. The deep identity invariant is `identity_type = AI system`; attempts to set a biological-human identity through the signed self-model are rejected.
- Self-learning now extends beyond coding. Explicit `Learn that ...`, `Fact: ...`, `Remember that ...` commands stage general facts. Verified/sourced research becomes general knowledge; feedback/corrections/approved examples become conversational-learning signals.
- Nexus Brain directly retrieves relevant banked general knowledge after model replacement/import, preserves provenance, and omits expired current-sensitive records from current-answer context.
- Nexus Brain also retrieves relevant **approved** `conversation_example` / `correction` training signals as portable in-context skill guidance for a fresh model, with explicit instruction to adapt rather than copy wording mechanically.
- Feature/security commits: `f436617` initial creator-locked Brain; `886c412` KDF portability; `6058446` explicit creator-only protected writes; `942f0c1` general-knowledge/autobiography; `a0157e4` cross-model conversational-skill restore; `d3e3dd0` explicit general-fact commands; `0bb41cf` Ed25519 signing; `d7508b3` public read-only seed export/package support; `07fe392` / `db0573d` same-creator signed update enforcement; `b1d9a07` canonical Brain authority + explicit-image adult gate.
- Current unit checkpoint: **188/188 tests passing**.
- Windows validation for `b1d9a079`: **green** — native build, installer compile, fresh install, running-app shutdown/in-place Update preservation, installer artifact upload, portable Windows artifact upload, and dogfood source artifact all passed.
- Next dogfood target: initialize a creator Brain, configure signed subroutines/emotion/self-model, stage facts/research/feedback, run creator Sync, export a public signed Brain, build a seeded installer privately, and verify continuity plus same-creator signed updates on a clean recipient install.

## Source of truth

GitHub repository: `afterburn25/Coding_Agent`

Future development sessions should begin by reading, in order:

1. `README.md`
2. `PROJECT_STATUS.md`
3. `ARCHITECTURE.md`
4. `SESSION_HANDOFF.md`
5. `MODEL_ROUTING.md`
6. `docs/IMAGE_MODULE_SPEC.md` when working on images
7. `docs/RESEARCH_SYSTEM.md` for research/ranking/repair-loop work
8. `docs/WEB_RESEARCH.md` when working on low-level internet/browser capabilities

Do not reconstruct project state from chat memory when the repository can answer it. Update this handoff and `PROJECT_STATUS.md` before ending substantial development work.

## Local Git checkpoints

- `2fa1304` — Add LoRA resolution and subject profile application.
- `ac3f663` — Show GitHub research provider status.
- `9288676` — Add GitHub REST research provider.
- `935145c` — Add validated image workflow import manager documentation/version checkpoint.
- `2e4a194` — Normalize background-removal workflow routing.
- `e6f5012` — Add validated ComfyUI workflow importer.
- `997cf92` — Test conservative GitHub research authority.
- `573bf9e` — Refresh tracked project metadata for v0.5.
- `ac3d91f` — Record v0.5 45-test checkpoint.
- `441ac4d` — Add v0.5 research intelligence and workflow hardening.
- `f208b0c` — Add v0.5 research intelligence and harden image workflows.
- `7ccc6c9` — Add image model manager controls.
- `549f23e` — Add image model and LoRA asset management.
- `46b9477` — Add v0.4 web research and local image foundation.
- `7448d8a` — Add v0.3 transactional coding workflow.
- `9d61887` — Add v0.2 runtime manager and resource-aware model switching.
- `168b51a` — Bootstrap Local Code Agent v0.1.0.

## Current development baseline

- Stable baseline through v0.3 coding workflow.
- Active branch/worktree is now v0.6 Nexus Core/self-hosting development.
- v0.4 adds two new modular capability families without replacing existing coding-agent components:
  - Web research + optional full Chromium automation.
  - Local image generation/editing through an image-router/backend abstraction with ComfyUI as the first backend.
- Automated tests: **99 passing** after the research/verification-repair and image-workflow-validation milestone.

## Completed before v0.4

- Chat-style local UI and backend.
- Automatic coding-model routing: utility, fast coder, primary coder, deep reasoner, reviewer, vision.
- Resource-aware model choice and escalation.
- Managed llama.cpp lifecycle and health recovery.
- Transactional patch editing.
- First-write checkpoints and task undo.
- Permission approvals with resumable exact tool calls.
- Verification detection/build-test execution.
- Reviewer model handoff.
- Persistent task ledger/project memory.
- Lightweight repository index.

## v0.4–v0.5 work implemented

### Web/internet tools

- `web_search` tool using a dependency-free research client.
- `fetch_url` tool for extracting readable text/JSON from web pages.
- `browser_run` tool backed by optional Playwright/Chromium.
- Separate permissions:
  - `network.read`
  - `browser.control`
- System prompt tells the agent to research when current/version-specific facts matter and to cite source URLs actually used.
- `localcodeagent/research/` adds repository-first preflight, environment/package inspection, knowledge-gap planning, source ranking, cache/session history, official-domain mapping, and untrusted-source handling.
- Research tools cover technical topics, docs, GitHub/upstream issues, exact errors, API lookup, release notes, package versions, and cached summaries.
- GitHub research prefers the versioned REST API, reads optional auth from `GITHUB_TOKEN` (configurable env-var name), caches API evidence, and falls back to web search.
- Verification failures can re-enter a bounded diagnose/research/fix/retest loop.

### Image architecture

Created `localcodeagent/image/` with:

- `types.py` — image model/request/job/routing dataclasses.
- `router.py` — automatic image operation and model selection.
- `backend.py` — stable `ImageBackend` abstraction.
- `comfyui.py` — dependency-free ComfyUI HTTP adapter.
- `runtime.py` — optional managed ComfyUI launch/health/stop/recovery.
- `workflow.py` — JSON API-workflow loading and `${variable}` substitution.
- `catalog.py` — local image model discovery.
- `profiles.py` — reusable subject/character profiles.
- `policy.py` — adult consent records and high-risk image safeguards.
- `manager.py` — queue/history/routing/backend/resource coordination.
- `library.py` — model component verification, explicit download/repair/remove jobs, LoRA metadata, and generated ComfyUI extra-model paths.

### Image agent tools

Registered tools:

- `generate_image`
- `edit_image`
- `inpaint_image`
- `outpaint_image`
- `remove_background`
- `upscale_image`
- `create_image_variations`
- `list_image_models`
- `load_subject_profile`

### Image workspace

Added `/image.html` with:

- conversational prompt/editor
- drag/drop image upload
- reference strip
- Auto/manual image model selector
- operation selector
- quality and resolution controls
- generation count and seed controls
- subject profile selector
- advanced negative prompt / LoRA / strengths / outpaint / transparency / upscale / refinement controls
- real-person reference flag
- job progress display
- cancellation
- generated-image gallery
- Image Model Manager verify/install/repair/remove controls and install progress
- LoRA discovery/metadata display

### Shared GPU resource coordination

`RuntimeManager` can now release managed coding LLMs when an image model needs VRAM and restore them after the image job, depending on image resource settings. External runtimes are never terminated by this mechanism.

## Current model defaults

Image model profiles are configuration templates, not bundled weights:

- `qwen-image-2.1` — quality/editing preference, up to 10 configured reference images, transparency flag, configurable ComfyUI workflow.
- `flux2-klein-4b` — fast preview/draft preference, up to 4 configured references, configurable ComfyUI workflow.

No image weights are downloaded automatically yet.

## Current additions after the original v0.4 handoff

- ComfyUI workflows are now validated as API-format before loading large models.
- Image Model Manager can import API-format workflows into configured operation slots.
- Image jobs now expose structured friendly error messages plus collapsed technical diagnostics.
- Required ComfyUI nodes are checked before generation.
- Main chat renders/polls image jobs inline and exposes Edit/Variation/Upscale/Save actions.
- `config.example.json` is synchronized with the current Qwen/FLUX component layouts and research settings.
- Image workspace now imports and assigns API-format ComfyUI workflow JSON per model/operation.
- Import rejects normal ComfyUI UI exports with an explicit Export (API) instruction, validates before write, writes atomically, confines paths to `workflows/image`, and limits imports to 10 MB.
- `remove_background` now resolves both the new canonical workflow key and legacy `background_removal` configs.
- LoRA selections are resolved only from installed local files, respect enabled state/version/family compatibility/max-count/strength bounds, and are recorded on image jobs.
- Selected LoRAs require explicit workflow template slots before generation, preventing a UI selection from being silently ignored.
- Subject profiles now apply saved references, preferred model, generation defaults, and assigned LoRAs automatically.
- Image workspace can filter/use/enable/disable discovered LoRAs.
- Image job errors are classified into stable codes with user-facing messages and collapsed technical details instead of raw exception strings.
- Local mask editor now paints/erases/fills/inverts and uploads masks through the existing local image-upload route for `mask_path`.
- Before/after workbench can compare a finished edit and reuse generated outputs as the next source image.

## Known gaps / next executable steps

1. Use the Image workspace workflow manager to import and test real ComfyUI API-format workflows for the selected Qwen-Image-2.1 and FLUX.2 Klein local node stacks.
2. Add dedicated background-removal and upscaler adapters/workflows.
3. ~~WebSocket/SSE streaming~~ — done: `/api/chat/stream` tokens + `/api/events` bus mirror all non-token events with task attribution.
4. ~~Persistent browser sessions~~ — done: `browser_run` accepts `session` + `save_session` (`storage_state` JSON under `.agent/browser/`).
5. Voice STT session scaffold exists (`voice/stt.py`) but needs `sounddevice` + a model (Vosk/faster-whisper) installed on the host.
6. Runtime tuner produces real numbers only when probes run on hardware — idle auto-tuner covers this; `data/runtime_tuning.json` accumulates results.
7. LLM model downloads are not yet a mission `job` op (image-model installs are, via `model_install`).

## Testing command

```bash
python -m unittest discover -s tests -v
```

Expected at this checkpoint: `1183 tests` passing (2 environment skips).

## v0.7 modular tool/plugin foundation checkpoint (Phase 1)

- New direction: Nexus Core evolves from a chat/coding interface into a modular general-purpose local AI workstation; users describe outcomes and the agent picks models/tools/runtimes. Phase 1 foundation is implemented.
- `ToolSpec` is now a full manifest (id/category/version/provider/capabilities/permissions/network/GPU/OS/docs/install status/health check). Built-ins get metadata from `localcodeagent/tools/manifests.py`; registration call sites are unchanged.
- Registry gained capability lookup, enable/disable (persisted at `data/tools_state.json`), health hooks, and per-tool usage counts. Disabled or non-callable tools stay out of model schemas.
- `localcodeagent/permissions.py`: `PermissionManager` adds the `session` level and workspace profiles (safe/developer/power_user/offline/research_only/custom); changes persist to `config.json` (`permission_profile`).
- `localcodeagent/jobs.py`: `JobManager` unified ledger — normalizes agent tasks, image jobs, and installs into queued/preparing/running/waiting_for_tool/waiting_for_permission/completed/failed/cancelled; generic jobs persist to `data/jobs.json` and are marked failed on restart.
- `localcodeagent/processes.py`: `ProcessManager` registers llama.cpp runtimes and ComfyUI as controllable services with live status/uptime and delegated lifecycle.
- `localcodeagent/tools/interfaces.py`: structural contracts for executable/model/image/browser/research/media/data/document/sandbox/VCS providers.
- `localcodeagent/tools/plugins.py`: declarative JSON manifests under `tools/manifests/` (config `tool_manifests_dir`) register external tools — install detection, health checks, optional subprocess invokers. No-invoke manifests are catalog-only and never reach model schemas.
- New endpoints: `GET /api/tools`, `/api/tools/health/<id>`, `/api/permissions`, `/api/jobs`, `/api/processes`, `/api/resources`; `POST /api/tools/state`, `/api/permissions/level`, `/api/permissions/profile`, `/api/processes/action`, `/api/jobs/cancel`.
- New **Tools & Plugins** page (`web/tools.html`); main-nav Tools link routes there.
- Feature commits: `105182a` (registry/permissions/jobs/processes/UI), plugin manifest loader + interfaces commit on top.
- Unit checkpoint: **328 tests passing** (grew through the milestones below).
- Phase 2 started: `terminal_run`/`terminal_processes`/`terminal_kill` (controlled shells + tracked background jobs), `search_code`/`search_filename`/`search_error` (ripgrep + fallback), `detect_build_system`/`build_project`/`configure_project`/`run_tests`/`clean_project` (CMake, Meson, Cargo, .NET, MSBuild, npm/pnpm/yarn, Gradle, Maven, Make, Python).
- Tool Router landed: `localcodeagent/tool_router.py` ranks candidates by capability (permission/offline/GPU/VRAM/RAM/OS filters, `preferred_tools` config, cached health), executes with fallback, records telemetry. Endpoints: `GET /api/tools/route/<capability>`, `GET /api/tools/telemetry`. Health probes wired for git/ripgrep/GitHub/ComfyUI/shell families.
- MCP landed: `localcodeagent/mcp.py` — dependency-free stdio JSON-RPC client + `MCPManager` (connect/disconnect/restart/status), `config.mcp_servers`, registry import as `mcp__<server>__<tool>` with `source="mcp"`, `/api/mcp` + `POST /api/mcp/action`, Tools-page MCP panel. Doc: `MCP.md`.
- Later milestones on top: credential vault + `api_request` + `code_symbols`/`code_map` (`17af833`), Local Models page `/models.html` (`c2e5529`), data tools `data_query`/`profile_dataset`/`chart_generate` (`a37b3f3`), FFmpeg media wrappers (`04500b9`), `media_transcribe` registry-chained pipeline (`1235a7d`), document tools `extract_text`/`ocr_image`/`convert_document` + invoker `shell.execute` gating (`74bfb91`).
- Invoker permission rule: any manifest tool that spawns a subprocess is gated on `shell.execute` (or a stricter dedicated key like `docker.access`); declared read/write keys become `permissions_required` metadata.
- Newest milestones: SSE event bus `/api/events` + `tools.js` live refresh (`3d7a3ee`), local knowledge/RAG index + `python_exec` sandbox (`52e794b`), Piper TTS manifest + plugin stdin + `speak_text` (`d5e7413`), declarative `run_workflow`/`list_workflows` pipelines (`da3e9c6`), `blender_render` scene generator (`e28ba18`), model-callable `use_capability` ToolRouter dispatch (`7a8ff73`).
- Latest: learned routing (telemetry persistence + success-rate ranking + UI panel `fd45099`), ast backend for Python code intel (`6b5327c`), find_tools/system_resources/install_tool (`feb9f49`, `1a94565`), AppState wiring contract tests (`b6bd8c1`), git worktrees (`0a130db`), GET /api/workflows + Workflows panel (`cef5132`).
- Newest batch: managed-service watchdog restart (`d770522`), MCP Streamable HTTP transport (`9a2d793`), vault `secret:` env references for MCP servers + `ask_workspace` workflow (`c1bd188`), cooperative workflow cancellation between steps (`e05f7a4`), measured generation telemetry — TPS/TTFT recorded per generation, aggregated per model, surfaced on `/api/model-telemetry` and the Models page Performance card (`17895b7`).
- Latest batch: workflow resume checkpoints at `.agent/workflow_runs/<id>.resume.json` with `resume=true` re-entry (`38b7df8`) and `resumable` surfacing in `list_workflows`/API/UI (`a22516f`); Nexus Brain unlock brute-force backoff (`aedc3b9`); MCP HTTP `headers` end-to-end incl. `secret:` vault refs (`7b4c55f`); `/api/readiness` tools summary (`c645ea7`); dependency-free ComfyUI `/ws` progress listener with reconnect backoff (`bbe528e`, `a863af4`); `perf` SSE event with measured TPS/TTFT in the chat rail (`873f2bc`); research provider outcome stats persisted to `provider_stats.json` (`e7e7571`); optional tree-sitter backend for non-Python code intel (`5187e2f`).
- Autonomous operation + live command visibility: `tool_start`/`tool_output` SSE events, Devin-style terminal blocks in the chat activity rail with live stdout/stderr streaming (`run_process_streaming` in terminal.py; `run_shell`/`terminal_run` emit through a per-session `stream_sink` in `registry.context`), and parallel read-only tool batching; `autonomous_mode` auto-approves ask/session workspace actions (hard gates: spend/message/mic/camera always ask; deny stays deny) via `PermissionManager.set_autonomous`, `POST /api/permissions/autonomous`, and a Tools-page toggle; `autonomous_max_continuations` bounds step-limit extensions; `RuntimeManager.evict_idle()` unloads stale/pressured managed models on the process-watchdog tick while pinning models serving active tasks; cooperative task cancellation (`task-<id>` via `/api/jobs/cancel`, chat Stop button, mid-batch skip, resume of cancelled tasks refused); `config`: `model_idle_unload_seconds` (default 900), `memory_pressure_vram_gb`/`ram_gb`.
- Resilience batch: startup auto-resume of interrupted tasks when autonomous (`auto_resume_interrupted_tasks`, `cc3e8b5`), bounded transient retries on external endpoints (`cc3e8b5`), `SecretVault.redact()` filtering vaulted values from live output chunks, job cancel kills tracked background terminal processes (`c292721`), llama.cpp post-health warmup for first-token latency (`model_prewarm_*`, `dd68787`), and `agent_tool_timeout_seconds` (default 1800) so a hung tool returns a timeout instead of stalling the run (`0c8e32f`).
- Reconnect-safe live visibility (`12fbfbe`): `/api/chat/stream` `emit()` mirrors non-token events (`task`, `tool_start`, `tool_output`, `tool`, `model`, `approval`, `perf`, `context_trim`, `cancel`, `image_job`, queue transitions, `waiting_approval`, `approval_timeout`) onto the shared `/api/events` bus with `task_id` attribution, so a reloaded page follows the run live via `connectAgentEvents()` in `web/app.js`; `agentStreamActive` suppresses bus rendering while the direct request stream is connected, clearing on result/error/disconnect. Queue dequeue history bug (`self.state.history` inside AppState) fixed in the same commit.
- Latest resilience batch: `_trim_context` stubs older tool/assistant bodies once the prompt exceeds ~3 chars/token of the profile's context window (`781faf0`); `autonomous_approval_timeout_seconds` (default 3600) fails approval-parked tasks on the watchdog tick so hard gates can't stall an unattended run (`eec7cf2`); `autonomous_error_retry_seconds` (default 120) re-drives stale error tasks via `recover()` bounded by `autonomous_max_recoveries` (`21dbdde`); all resume paths now go through `_execute_tool`/`_drive_or_error` with `tool_start`/`tool` events (`89837cd`, `da9b82f`); vaulted values are scrubbed from emitted tool args and completion results (`6b9d816`, `4868345`).
- Concurrency + batching: live `tool_output` chunks route per-task via thread-local dispatch with reader-thread fallback (`539a3a7`, `9d6169b`); file mutations attribute to the owning task under concurrent runs (`04c363a`); durable `WorkQueue` (`.agent/queue.json`) dequeues prompts through `agent.run` on the watchdog tick — `GET/POST /api/queue`, `POST /api/queue/cancel`, queue surfaced in `/api/tasks` and the chat task card (`4e53364`, `bcd74d2`, `e61dee3`). `chat_queue_when_busy` (default true) auto-enqueues chat messages sent while a task is active instead of racing it (`c061ec5`). Tools page has a Work queue card with per-item cancel (`bef8ff8`).
- Event-bus polish batch: agent-run failures publish an `error` event to the bus (`7b20df4`); task card recovered on bus open via `/api/tasks` fetch rather than relying on replay (`1b77b3b`); orphan `tool_output` chunks lazily open terminal blocks (`4dfe3b1`); `agentStreamActive` guard released on fetch/non-OK/stream errors (`84e0781`); `/api/tasks` returns `{current, recent, queue}` — not `{tasks}` (`c7de272`); recent tasks are clickable to inspect/resume (`a7e9c3d`); bus handles `approval`/`image_job` (`f203f25`); `tool_output` chunks coalesce in the 100-event replay history so chatty commands don't evict task context (`c044877`); queue-run and recovered tasks publish through shared `_bus_emit` (no tokens on the bus, `task_id` attribution) — `recover()` gained an `event_callback` param (`8cb4a22`, `cd260c8`). Persisted per-task terminal transcripts under `.agent/terminal/<id>.log` (512 KiB bound, 64 KiB tail via `GET /api/task-log`) restore prior command context on reload (`406c68f`); orphaned logs prune on load (`9e8dfe3`). Follow-ups: instant dequeue on enqueue/task completion (`4498381`, `c375dec`, `58462db`), task lifecycle markers in transcripts (`8dfa282`), `/api/task-log` path-traversal guard (`59414c5`), bounded request event queue under client backpressure (`562630f`), `?replay=N` on `/api/events` + `_bus_emit` policy test (`2d925a2`).
- Next: real ComfyUI Qwen/FLUX workflow runs on GPU hardware, real MCP-server interop validation, tree-sitter/LSP indexing, and continued Nexus Brain dogfooding.

## v0.7 autonomy hardening + Brain lifecycle checkpoint

- Queue single-flight: dequeue serialized under a lock; completion chain waits
  briefly so fast workers can't strand items; 200-item bound; busy chat returns
  409 or auto-queues via `chat_queue_when_busy`.
- Transcripts cover the full lifecycle via a wrapped event callback — model
  select, task transitions, tool I/O, approvals, image jobs, errors, and
  pre-session failures all land in `.agent/terminal/<id>.log`.
- Idle eviction: `model_idle_unload_seconds` for llama.cpp runtimes plus
  `comfyui_idle_unload_seconds` for the managed ComfyUI process (external
  installs untouched); `idle_evicted` bus events surface on the Tools page.
- Growth bounds everywhere: task ledger 100, queue 200, terminal logs 512 KiB,
  checkpoints pruned, routing telemetry compacts, JobManager 300, research
  cache/sessions pruned (500/100), llama/comfyui/terminal/backend-host logs
  tail-bounded, EventBus 100-event replay + bounded subscriber queues.
- Shutdown hygiene: `stop_state` terminates MCP servers, tracked terminal
  processes, managed ComfyUI, and model runtimes. Desktop host restarts a
  crashed backend — crash-loop bound resets after 5 stable minutes.
- All durable stores write atomically (tasks/queue/jobs/brain/memory/secrets/
  consent/activation backup/image ledgers/workflow resume checkpoints).
- Nexus Brain: encrypted creator-key backup/restore
  (`POST /api/nexus-brain/key-backup` + `/key-restore`; bundle re-encrypted
  under a backup passphrase, restore proves key by verifying the existing
  signature — foreign keys rejected); bounded 10-entry signed settings history
  with reversible creator rollback (`GET /api/nexus-brain/history`,
  `POST /api/nexus-brain/rollback`); audit trail + version list rendered on
  the Trainer page; `settings_history` stripped from imported payloads.
- Isolated selftest now smoke-checks `/api/queue` and the `/api/events` SSE
  handshake in addition to boot + the four pages.
- Verified checkpoint: **336 tests** (2 skips). Remaining work is
  hardware-bound: real Qwen3-14B/30B runs, ComfyUI/FLUX interop, overnight
  unattended queue soak; plus interactive Brain dogfood on a real install.

## 2026-11 — GPU-independent end-to-end harness (343 tests)

`tests/test_end_to_end.py` adds `_FakeModelServer`: an in-process
OpenAI-compatible endpoint (`GET /health`, `POST /v1/chat/completions`,
stream + non-stream, scripted tool_call then final-answer turns). Knobs:
`tool_name`/`tool_args` (which call to emit), `fail_next` (N 500s),
`delay` (stall responses). It drives the real `AppState` + orchestrator +
tool registry + task ledger + transcripts — no GPU needed.

Coverage:
- happy path: real `system_resources` call -> tool result -> final answer
  -> `$`/ok/`## result` transcript markers
- queue drain: two chained tasks through the real work-queue worker
- transient 500 -> `ensure_ready` retry -> `transient_retry` model event
- persistent failure -> bounded calls, `error` status, `## error` marker
- mid-run cancel -> `cancelled` result (found+fixed a resurrection bug:
  `_drive` re-marked `running` at loop entry, losing cancels that landed
  during the first in-flight model call)
- approval gate: `write_file` pauses `waiting_approval` with persisted
  call; approve executes + feeds result to model; deny sends
  `PERMISSION_DENIED`

Use it for the soak: script N tasks, `fail_next`, delays, and restarts on
the GPU box to exercise the overnight path deterministically before real
inference.

Verified checkpoint: **343 tests** (2 skips), head `b9f24e9`.

## CRITICAL regression found by the e2e harness (7acfc8e)

`_sse_event` (introduced 69d3cdc) wrote `f"...\ndata..."` — literal
backslash-n text on the wire instead of LF. Every SSE frame on
`/api/chat/stream` AND `/api/events` was one unparseable line; EventSource
and the frontend `\n\n` splitter never fired. The UI silently fell back
to task-ledger recovery — live token/tool streaming was dead while
transcript-level verification kept passing. Header-only selftest checks
missed it; the new `test_full_stack_sse_stream_end_to_end` parses real
frames and now guards it. Also added: batched tool_calls, persisted
approval across AppState restart, catalog-download teardown flake fix.
Checkpoint: **346 tests**, head `7acfc8e`.

## In-app tool installation (Tools page owns optional downloads)

- The Windows installer no longer downloads ComfyUI or the Qwen-Image/FLUX stacks — only the two coding GGUFs bootstrap at setup, keeping install fast. All optional tools install on demand from the Tools page.
- `localcodeagent/tools/downloads.py` (`ToolDownloadManager`) runs manifest `install.method: "archive"` jobs: HTTPS download → stable per-tool `.part` (`{tool_id}.part`) → SHA-256 → extract → `.chatnexus-version` marker. Job metadata carries phase/download_progress/extract_progress/bytes/bytes_per_sec/eta_seconds/current_file/current_path; emits over the JobManager→EventBus SSE path. Dedupe per tool; cancel/fail **keeps** `.part` so retries resume via HTTP `Range` (416 → clean restart; server ignores Range → truncate restart; complete .part → skip fetch).
- Full lifecycle in-app: install (resumable), Reinstall, **Update** (`installed_version` vs manifest `version` → "Update available" chip), and **Remove** (`POST /api/tools/uninstall` → `tool_remove` job deletes dest + marker + `.part`, archive-method only, same `packages.install` gate). Disk preflight refuses when free < size_bytes and warns when < ~3x; `/api/tools` reports `disk_free_bytes` (shown in the Installations card; oversized installs flagged LOW DISK SPACE).
- 7z extraction prefers the OS `tar.exe` (libarchive, native speed, per-file `-v` progress); py7zr is the bundled fallback (see `--collect-submodules py7zr` + codec hidden-imports in `build_windows.ps1`). Extraction progress is byte-weighted and throttled (4 emits/sec — unthrottled per-file updates rewrote jobs.json thousands of times and flooded SSE).
- Manifest `detect.files` are install-root-relative markers for non-PATH payloads; `os_supported` is computed into the tool payload and gated in `install_tool` + UI. Packaged builds find manifests under `_MEIPASS` via fallback; `pip` installs resolve `managed_python()` (ComfyUI embedded Python → `{app}/python`) because `sys.executable` is the frozen exe.
- Tools page (`web/tools.*`): Installations card (overall bar + current file/path), per-tool progress bars, Installed/Not installed/Installing chips, Install/Cancel/Reinstall, and an Image model packs section wired to `/api/image/models/install`.
- `tests/test_tool_installer.py` (17 tests) covers the archive lifecycle, traversal/sha/cancel/dedupe, **Range resume**, uninstall (dest+marker+`.part`, refusal during active install, dest traversal guard), detect.files, managed-python resolution, disk preflight, and a real `AppState.install_tool`→uninstall round trip. `tests/test_installer.py` now asserts the installer ships NO optional downloads.

## Short-window scrolling + tool install/remove backend + ComfyUI manifest fix

- `web/styles.css`: added `@media(max-height:680px){body{overflow:auto}}` — the chat shell pins `min-height:680px` while the desktop window allows 640px minimum, so anything below 680px clipped the composer/footer with `body{overflow:hidden}` and no way to reach them. Below the threshold the page now scrolls to the bottom; other workspace pages already scroll internally (`*-main{overflow-y:auto}`) and are unaffected.
- Package-manager uninstall: `tools/plugins.py` gained `REMOVE_METHODS` + `uninstall_command()` (winget/choco/uv/npm/apt/dnf/brew/pip mirroring `INSTALL_METHODS`); `server.py:uninstall_tool` now dispatches non-archive removals as a `tool_remove` job through the shared `_permission_gate` instead of rejecting them; `ToolRegistry.manifest()` exposes `removable` so the UI shows Uninstall only when automated removal exists (grid cards + detail drawer in the redesigned Tools page consume it). Archive removal is unchanged (`ToolDownloadManager.uninstall` deletes dest/marker/.part).
- Fixed `tools/manifests/comfyui.json` — a missing comma after the new `dependencies` array made the manifest unparseable, so ComfyUI silently never registered on the Tools page.
- Live-verified: dev server `/api/tools` reports 104 tools with blender/comfyui/ffmpeg/docker `install_status: missing`, `removable: true`, `partials` map and `disk_free_bytes` populated.
- `tests/test_tool_installer.py`: +5 tests covering `uninstall_command` shapes (winget/pip/unknown/archive), the `removable` manifest flag, a mocked `winget uninstall` AppState round trip, and the manual-removal error path. Full suite: **374 tests, 2 skips — green**.

## Tools & Plugins redesign + Settings > Permissions (main, this session)

- `web/tools.html/.css/.js` rebuilt as the operational catalog: summary cards (installed/available/updates/running/errors), search + filter chips + sorting, responsive card grid, right-side detail panel (Overview/Capabilities/Dependencies/Configuration/Logs), collapsible Installation Queue showing phase/bytes/speed/ETA/current file + resumable `.part` rows, plus the preserved image packs, processes, jobs, work queue, routing telemetry, and workflows panels.
- New `web/settings.html/.css/.js` — dedicated Settings shell with secondary nav (General/Permissions/Models/Appearance/Privacy/Notifications/Advanced). Settings > Permissions: profile cards, category summaries, searchable matrix (Capability/Status/Scope/Approval/Last Used), right detail panel (approval rules incl. Require Creator Approval, domain/dir/repo scope editors, associated tools, recent activity), and the audit log. Chat nav Settings now links here.
- `localcodeagent/permissions.py`: new `creator` level (requires unlocked Nexus Brain session, fail-closed), bounded persisted audit (`data/permission_audit.jsonl`, 2000 entries, decisions only), `PERMISSION_INFO` metadata (label/category/scope/risk/tools) with prefix fallback, `permission_scopes` config with generic URL domain enforcement in `ToolRegistry.execute`, in-memory `record_use` last-used stamps, summary() gains info/categories/scopes/last_used.
- `server.py`: `GET /api/permissions/audit`, `POST /api/permissions/scope`, `/api/processes/log?id=` tail endpoint (secret-redacted), `/api/tools` gains `partials` (resumable .part bytes) and per-tool `install_path`/`install_size_bytes`/`installed_at`/`latest_version`/`process_id`/`dependencies`/`executables`/`detect_files`/`mcp_server`/cached `health`. Shared `_permission_gate` handles deny/ask/creator uniformly (install, uninstall, secrets); `needs_creator` flag on gated responses.
- `tools/plugins.py`: manifest `process` + `dependencies` fields; `tools/base.py`: health-result cache, install path/size/installed_at resolution, audit events on execute decisions, URL scope enforcement.
- `tests/test_permissions_ui.py` — 22 tests: creator level + fail-closed, audit persistence/bounds/no-secrets, scope validation + URL enforcement, summary shape, manifest enrichment, health cache, and endpoint-level coverage over a real HTTP server.

## Nexus Core rebrand + real startup splash (main)

- Canonical product name is now **Nexus Core**; all user-facing Chat Nexus branding is retired (UI pages, window title, backend messages/prompts, installer text, shortcuts, Add/Remove Programs, docs).
- Official assets (supplied artwork, unmodified): `web/assets/nexus-core-logo.png` (full wordmark), `web/assets/nexus-core-icon.png` (compact shield), `web/assets/nexus-core-splash.png` (startup splash), `desktop/ChatNexus.Desktop/nexus-core.ico` (16/24/32/48/64/128/256 layers, embedded in exe + installer).
- **Executable renamed to `NexusCore.exe`** and installer output to `NexusCore-Setup-<ver>-Windows-x64.exe`. The stable AppId `ChatNexus.Afterburn25` is intentionally kept so existing Chat Nexus installs upgrade in place — never a second installation. The installer also detects the legacy `Programs\Chat Nexus` dir, taskkills legacy `ChatNexus.exe` processes, and `[InstallDelete]`s the old exe. Internal identifiers intentionally unchanged: `ChatNexus.Backend.exe`, `backend/` path, `installer/ChatNexus.iss`, `ChatNexus.Desktop` namespace, `dist\ChatNexus`, `CHAT_NEXUS_*` env vars, Python package `localcodeagent`.
- **Real splash lifecycle** (`desktop/ChatNexus.Desktop/Program.cs`): `NexusCoreApplicationContext` shows `SplashForm` (borderless, supplied artwork) immediately; `MainForm` is created hidden (`CreateControl`, never shown). `StartupProgress` is the single startup state machine — real milestones only (host init 6% → backend launch 15% → backend health 30–55% → WebView2 72% → shell load 85% → handshake wait 93%). Two-line status (`ACTION · SUBSYSTEM` + dim description) under the progress bar.
- **Dismissal requires BOTH** `AppReady` (frontend `nexus-core-ready` postMessage, sent after the shell finishes its init fetch chain in `web/app.js`) **and** 7 s minimum. Ready-early → holds at ~98.5% with `FINALIZING · NEXUS CORE`; on satisfaction shows `READY · NEXUS CORE`, completes the bar, plays an ~850 ms core-glow bloom, then closes splash and shows the already-initialized main window in the same turn (no gap). A bounded NavigationCompleted+15 s fallback prevents a broken page from hanging startup.
- Fatal startup failure → `NEXUS CORE COULD NOT START` state with Retry / Open Log (`data/logs/backend-host.log`) / Exit; Retry rebuilds progress + forms without duplicate backends. Backend watchdog/restart recovery unchanged.
- `tests/test_branding_splash.py` (20 tests): asset presence/ico layers, exe+installer icon embedding, no user-facing Chat Nexus outside legacy markers, splash ordering/7s rule/handshake/failure/atomic transition, AppId preservation, legacy upgrade cleanup, data preservation, and CI workflow contracts. Installer tests updated; workflow smoke test now plants a legacy `ChatNexus.exe` + fake `NexusCore.exe` and verifies both are closed/removed on update.
- Suite: **423 tests passing (2 skipped)**. CI run `36917101388` — **green** (Ubuntu unit tests + Windows desktop/installer build, fresh-install + update-preservation smoke incl. legacy `ChatNexus.exe` cleanup, installer + portable ZIP artifacts).
- Final commit: `9a48b476b01064936f95d37bf16271278f493dbb` (rebrand `46181b8` + packager/doc checkpoint).

## Unattended-reliability pass + performance tuning (main, latest)

- **Runtime tuner** (`localcodeagent/runtime/tuner.py`): probes bundled `llama-server` capabilities, fingerprints per GPU/build/model/context, auto-tunes launch flags (flash-attn, cache-reuse, batch sizes), persists to `data/runtime_tuning.json`, exports benchmark artifacts to `data/benchmarks/`. `POST /api/tuning` actions benchmark/reset/performance_mode; Models page gained a Runtime Tuning panel.
- **Activity timeline** (`localcodeagent/workflow/activity.py` + `GET /api/activity`): durable JSONL parent/child rows — planning/routing/model-load/memory/research/tool calls with live stdout tails, verification groups with nested children, residency events (FREEING VRAM / STARTING MODEL), job/image-job mapping, category filter chips, restore on SSE reconnect. Open rows reload as `interrupted`.
- **Per-command Stop**: timeline Stop on a running command kills only that subprocess via `/api/jobs/cancel` `command-<task_id>`; `[cancelled]` marker, task continues; flag cleared per command.
- **Kill-race fix (Job Objects)**: `taskkill /T` enumerates children at kill time — a child spawned in the enumeration→death window escaped and held inherited log/pipe handles forever (orphaned PING.EXE repro). Both kill paths (`TerminalTracker.kill`, `run_process_streaming._kill_tree`) now assign a `KILL_ON_JOB_CLOSE` Job Object right after Popen; `TerminateJobObject` + `WaitForSingleObject(job)` drains the whole tree atomically — including children spawned mid-kill — before returning, so file locks release immediately. taskkill remains the fallback; POSIX unchanged (own pgid + killpg).
- **Pressure-eviction thrash fix**: `evict_idle` no longer evicts `keep_loaded` residents under ambient memory pressure — when the resident baseline itself leaves less free RAM/VRAM than the floor, evicting it just to rewarm is a thrash loop. Launch-time contention still reclaims them via `_enforce_residency` with rewarm on session close.
- **Boot-hang fixes**: bounded `os.walk` replaced unbounded `rglob` over the install root (manifest executable resolution + install-size calc both walked the ~100k-file ComfyUI tree at every cold start; install size is now persisted in state).
- **Splash**: single custom gradient bar (baked-in artwork bar painted over first). Backend watchdog auto-restarts with 3-strikes/5-min decay + UI reload.
- **Branding**: icon/logo/ico are true RGBA (flood-filled connected background, feathered edges, dark shield panels preserved); "NC" avatar replaced by transparent emblem; favicon links on all pages.
- **Windows trust**: backend EXE version metadata, optional Authenticode signing (`NEXUS_CODESIGN_PFX_B64`/`NEXUS_CODESIGN_PASSWORD` or `NEXUS_CODESIGN_THUMBPRINT` secrets), per-artifact `.sha256` + `CHECKSUMS.txt` published with each release.
- **Real benchmark numbers** (installed app, RTX 3080 Ti, resident model under contention): qwen3-coder-30b → 13.1 tok/s gen, 87.6ms warm TTFT (cache-reuse works, 55× faster than cold), flash-attn selected; qwen3-14b → ~2.9 tok/s dense under contention.
- Suite: **484 tests, 2 skips — green**. Latest head: `55c265a` (job-object + thrash fixes); prior green CI run `36933655384` (`255323e`).

## Self-repair + orphan reaping (main, latest)

- **Request self-repair** (`localcodeagent/models/openai_compat.py`): HTTP 400 "exceeds the available context size" from llama.cpp now self-heals — the provider parses the server's reported token counts, stubs oldest non-system bodies (system + latest turns preserved), caps max_tokens, and retries (≤2 repairs, `auto_repaired` marked in the response). All HTTP errors surface as `ModelHTTPError` carrying the server's real message instead of the misleading "could not reach" (HTTPError is a URLError subclass; the body was discarded).
- **Smarter budgeter** (`orchestrator._trim_context`): reserves `max_output_tokens` and uses 2.6 chars/token — prompt+output must fit under the *launched* ctx (tuner's `recommended_context` can be below `context_window`; the overflow that motivated this was a 16.7K request vs a 16384-token launch). 4xx rejections skip `runtime.recover()` — restarting a healthy server can't fix a malformed request.
- **MCP lifecycle fix** (`localcodeagent/mcp.py`): stderr drain, Job Object assignment, and the initialize handshake were dead code after `_resolved_env`'s `return` — stderr never drained (verbose servers deadlock), handshake never ran, no tree-kill. Moved into `start()`/`close()`; `close()` kills via Job Object so shell-wrapped servers can't orphan children.
- **Backend singleton** (`Program.cs`): force-killed host left its backend orphaned holding port+VRAM (observed live). `BackendProcess.Start` writes `data/logs/backend.pid` and reaps a path-matching leftover with `taskkill /T` before spawning.
- **Taskbar icon**: running exe embeds the transparent ico (verified per-layer alpha); Explorer cache cleared via ie4uinit. `new Icon(path)` form icon and exe resource both transparent.
- `tests/test_mcp.py` (4 tests: stderr-flood handshake, cmd-wrapper tree kill, secret env); streaming tests gained context-overflow repair + non-overflow 4xx surfacing cases.
- Suite: **495 tests, 2 skips — green**. Commits: `f09ee74` (MCP + reaping), `d43a6a1` (self-repair).
- **Malformed tool-call args self-repair** (`77d0c32`): `_parse_call` returns (name, args, error). Repairable text (Python literals, trailing commas, truncated braces) executes with recovered args; unrecoverable text feeds "arguments were not valid JSON, re-emit" back to the model instead of executing with `{}`.
- **Corrupt-state self-repair** (`44becbc`): `load_config` quarantines a damaged `config.json` to `*.corrupt-<ts>` and boots defaults — previously an unguarded JSONDecodeError crash-looped the backend. Per-model entries skip unknown keys (forward-compat). jobs/tasks stores quarantine corrupt files the same way.
- **First-run note**: a freshly PyInstaller-built unsigned backend gets a one-time ~50s AV scan of `_internal` on first launch — splash sits at CORE SERVICES; subsequent launches are fast.


## v0.8 local voice subsystem checkpoint

- `localcodeagent/voice/`: full local-first TTS stack. `TTSEngine` provider
  interface; `KokoroEngine` (Kokoro-82M ONNX via `kokoro-onnx 0.6.1`,
  Apache-2.0; assets SHA-256-pinned in `voice/assets.py` and the installer).
- `VoicePreset` schema v1 — nondestructive DSP recipes. Official preset
  `nexus-synthetic-isabella` (bf_isabella; pitch/EQ/exciter/compressor +
  neural/glass/micro parallel layers + stereo width + limiter; master
  `synthetic` slider scales everything).
- `SpeechTextFilter` block classifier (SPEAK/SUMMARIZE/SKIP) — code, logs,
  diffs, JSON, URLs, hashes never reach TTS. `SentenceStreamer` segments
  token deltas fence-aware so first sentence speaks early.
- `VoiceManager`: ordered cancellable queue, mute-stops-now, per-task
  stale-speech suppression, bounded LRU WAV cache, WAV/MP3 export.
- API `/api/voice/*` (status/speak/preview/mute/stop/config/presets/audio),
  voice SSE channel on `/api/chat/stream` + shared bus.
- `voice_*` tools registered under `audio.read|generate|manage` permissions.
- `web/voice.html` Voice Studio (A/B compare, Natural↔Synthetic, preset
  CRUD/import/export) + `web/voice_global.js` global mute/playback on all
  pages; per-message speaker on assistant replies.
- Installer ships Kokoro assets into `{app}\modelsoice` (hash-checked,
  skip-if-verified); `data/voice/presets` survives upgrades.
- Build: PyInstaller collects onnxruntime/kokoro_onnx/phonemizer/
  espeakng_loader/numpy; `kokoro-onnx` installed with
  `--ignore-requires-python` (declared <3.14, verified working on 3.14).
- `build_windows.ps1` bundles the SHA-256-verified Kokoro assets into
  `dist\ChatNexus\models\voice` (cached under `packaging\voice-assets\`),
  so the portable package works offline; the installer still downloads
  hash-checked copies with skip-if-verified on upgrade.
- Commits: `74a44ac` (subsystem) + `092840a` (packaging fixes: asset
  bundling, 120 s backend health budget, `voice.html` smoke probe,
  watchdog idle-unload, SSE/bus segment dedupe, UTF-8 BOM).
- Verified end-to-end on the packaged build: `NexusCore.exe --self-test`
  green; `/api/voice/speak` synthesized and `/api/voice/audio/<id>`
  served real WAV from `dist\ChatNexus` (2.87 s audio in ~4.3 s).
- Measured: model load ~0.8 s, warm synthesis RTF ~0.43 on CPU.
- Test suite: 537 / 537 (2 environment skips).

## 2026-10-01 — Reliability/live-ops hardening round (main, deployed)

- **Missing-tool reporting** (`d7f61d7`): `TOOL_NOT_INSTALLED:` is now an
  error prefix; the router returns a `tools_not_installed` outcome naming
  the missing tools + install guidance (manifest install hints), instead
  of letting a missing tool look like a success. Registry direct-call
  message names the tool and points to Tool Manager. Orchestrator prompt
  instructs the agent to tell the user what to install.
- **Atomic-write race** (`77c7d28`): `localcodeagent/fsutil.py` —
  `atomic_write_text/bytes` (unique tmp name per call) +
  `replace_with_retry`; all persistence call sites converted. Fixes the
  Windows `jobs.json` WinError 32 that killed image jobs; image job
  persistence serialized.
- **WebView2 stale-page flash** (`7316925`): disk cache invalidated once
  per backend payload change — old tools.html no longer flashes.
- **ComfyUI lifecycle** (`b4f2066`): a timed-out request no longer kills
  a half-booted ComfyUI; the process stays in `loading` and the next
  request attaches to the same boot (restart only after a 2x-timeout
  total budget). Windows `stop()` tree-kills so re-exec'd children can't
  hold port 8188. `comfyui_startup_timeout` default 180 → 300 s.
- **Voice composer mute** (`fcd540c` + `789c407`): icon-only speaker
  button next to Send (🔊/🔇), synced with nav toggle via shared
  `.voice-mute-btn` class. `789c407` fixes a `getAttribute()=x`
  SyntaxError that disabled the entire voice client (no auto-play).
- **Voice latency** (`4d87cd4`): engine pre-warm thread at task start so
  the ~1 s Kokoro load overlaps text generation.
- **Cold-boot health budget** (`df3277d`): desktop host waits 180 s
  (was 60) for backend health — fresh unsigned ~200 MB exe under AV
  scan could exceed 60 s → spurious "could not load backend".
- **Context budget + overflow recovery** (`0abd2e4`): optional injected
  context blocks + history share ONE budget per lane
  (`fast_general_context_chars`=9000, `coding_context_chars`=60000 —
  newest history wins). Previously per-block caps could total 20k+
  tokens on a simple question → 16k overflow + double prompt eval
  (~200 s responses). Last-resort repair: retry once without tool
  schemas when shrinking can't converge. fast/primary coder lanes raised
  to 24576 ctx (Qwen3-14B native 32k).
- Deployed: backend + host rebuilt and copied to installed app; exe
  hash-verified. App healthy post-deploy (voice enabled, unmuted).
- Suite: 540 tests, OK (skipped=2).

## 2026-10-02 — Juggernaut X v10 becomes default text-to-image (main)

- New default image profile `juggernaut-x-v10` (RunDiffusion SDXL
  checkpoint, pinned rev `e53841ec`, sha256 `d91d3573…0c45`,
  CreativeML OpenRAIL-M, 7,105,348,672 bytes). Normal/photorealistic
  t2i routes to it at priority 110; Qwen (80) keeps edit/inpaint/
  outpaint/background_removal; FLUX (70, speed_tier=fast) deterministically
  wins preview/fast/draft via a fast-tier pool restriction in
  `ImageRouter.choose`.
- New SDXL API workflow `workflows/image/sdxl/juggernaut-x-v10-t2i-api.json`
  (CheckpointLoaderSimple + CLIPTextEncode ×2 + EmptyLatentImage +
  KSampler + VAEDecode + SaveImage); checkpoint resolves via
  `${component_checkpoint}`; quality-focused default negative prompt has
  no censorship terms.
- `load_config` merges new default image models into existing installs by
  id (user entries win). `ImageModelProfile` gained `display_name` /
  `tagline`; Image Studio dropdown sorted by priority and shows friendly
  labels.
- SDXL CFG default is family-aware: `stable-diffusion*` → 6.5, others →
  1.0 (distilled). Approved adult prompts reach the checkpoint verbatim —
  no hidden sanitizer anywhere in the path (verified by test).
- Checkpoint verified on disk in installed app. Suite: 555 tests,
  OK (skipped=2).

## 2026-10-02 — Juggernaut dogfood + animation fix (main)

- Dogfood on RTX 3080 12GB, real ComfyUI:
  - Portrait 1024x1024 auto→juggernaut: 138s cold (incl ~120s ckpt load), peak VRAM ~8.2GB
  - Landscape 832x1216: 9s warm / Interior 1216x832: 16s warm
  - Adult synthetic nude (policy-approved): 368s (incl model reload after FLUX); prompt reached workflow verbatim
  - Draft 512x512 -> flux2-klein-4b: 126s cold
  - Edit (jacket recolor) -> qwen-image-2.1: 707s (cold Qwen load + edit); edit quality confirmed visually
  - All produced valid PNGs; no OOM/offload failure on the 12GB card. est_ram_gb corrected 24->16 after routing fell back to FLUX.
- CSS: image shimmer now sweeps -45%..+45% (band fully exits card before
  looping — was +-25% so it clipped at edges); indeterminate progress bar
  reaches +400% so it fully exits right edge.
- Commits: 301a6c8 (feature), 1b9cc9b (anim fix + RAM est). CI green both.

## 2026-10-02 — Nexus Answer Memory (learned Q&A fast path)

- New package `localcodeagent/answer_memory/` (schema, migrations, store,
  normalization, embeddings, retrieval, confidence, ttl, validation,
  feedback, invalidation, learning, service). SQLite at
  `data/nexus_brain/answer_memory.db`, WAL, per-operation connections so the
  file is never held open; in-memory answers/aliases snapshot serves lookups
  in ~0.1 ms; stat writes deferred + batch-flushed.
- Pipeline: deterministic tier-0 → exact → semantic memory lookup → Brain →
  utility → deep model. Trusted hits skip `refresh_hardware` + all model
  loading (`response_source="answer_memory"`, "Answered from memory" badge,
  timeline activity). Possible-band matches inject as advisory context.
- Embedder `hashed-ngram-v1` (deterministic, no download) + hard gates:
  proper/digit tokens, antonym pairs (start/stop, enable/disable…), and
  symmetric canonical swaps (image/voice, France/Italy, 14B/30B) all block
  at −1. Deep paraphrases fall to `possible` by design.
- Trust: observed→candidate→trusted/verified; user learn/correction store
  directly as trusted. Corrections invalidate the old answer.
  Confirmed paraphrases self-learn as aliases (equivalent model answer ⇒
  new phrasing becomes an exact hit next time).
- Freshness: live questions never bypass; repository/config-dependent rows
  go stale on HEAD/config-fingerprint change; TTL per answer class.
- Secrets refused before persistence; no chain-of-thought stored; corrupt
  DB quarantined (never deleted); export/import supported.
- API: GET `/api/answer-memory` (+`/export`), POST learn/forget/
  mark-incorrect/update/merge/refresh/clear/rebuild-index/vacuum/import.
  Thumbs feedback feeds trust scoring. `stop_state` flushes pending writes.
- UI: `/answers.html` Learned Answers page linked from every nav; 🧠 Learn
  button on each assistant message.
- Tests: `tests/test_answer_memory.py` (48). Full suite 606 passing
  (2 env skips) on Windows. Bench: exact ≈0.08 ms, semantic ≈0.13 ms @200.
- Docs: `docs/ANSWER_MEMORY.md`; README/PROJECT_STATUS/this file updated.

## Nexus Autonomy checkpoint — persistent mission manager

- `localcodeagent/autonomy/` landed: durable mission store (`data/autonomy/`,
  schema v1), task DAG with leases + resource locks, bounded event-driven
  supervisor (tick-based, bus-woken — no busy loop), planner, evaluator,
  recovery playbooks, policy engine + standing grants, scheduler
  (once/interval/daily/weekly with missed-run catch-up), triggers
  (file_changed/startup/ci_*/custom, workspace-confined watches, debounce),
  notification center (policies + quiet hours + dedupe), standing goals.
- Supervisor yields the agent lane to interactive work (`lane_free` gate);
  sensitive actions need standing grants or mission approvals; `stop
  autonomy` pauses everything immediately.
- `MissionStore._recover_orphans()` re-parks mid-execution missions on
  restart — running nodes return to `ready`, completed work never repeats.
- Server: `/api/autonomy/*` endpoints; chat commands "make this a mission",
  "stop autonomy", "resume autonomy" in both streaming and non-streaming
  chat (no coding model needed); Missions UI at `web/missions.html`.
- Bug fixed during dogfood: resource lock released under a different owner
  than the claimer stalled missions forever — release now uses the claiming
  node id.
- `tests/test_autonomy.py`: 49 tests. Full suite: **698 passing** (2 env
  skips). Docs: `docs/AUTONOMY.md`.
- Still open: Brain/Answer-Memory hooks into plan/eval/learn are
  shallow; async job dependencies (image/model-install) not yet DAG node
  kinds; unattended multi-hour dogfood + Windows CI/installer run pending;
  canonical VERSION/release machinery is the next milestone.

## v0.7.0 platform expansion checkpoint — commits d17ab56 + 05456c2

- Canonical versioning restored: `VERSION` (0.7.0) → `version.py` →
  `scripts/sync_version.py` derives pyproject/.iss defaults/.csproj/
  backend_version.txt/.agent/project.json; `--check` enforced by tests;
  build_windows.ps1 syncs before compiling and bundles VERSION.
- Platform wave implemented (see CHANGELOG.md): computer_use tools
  (permission-gated, audited), stdio LSP client+pool, incremental RAG
  index, multi-agent git worktrees with conflict-abort merges, EvalLab +
  experiments, artifact registry, versioned backups w/ verified restore,
  DPAPI-wrapped vault key + pattern redaction, connector framework,
  knowledge graph (SQLite), plan simulation, DigitalTwin predictions,
  health service, two-way voice scaffold (Vosk/faster-whisper optional).
- New endpoints: /api/health /api/twin /api/artifacts /api/skills
  /api/connectors /api/knowledge /api/rag /api/lsp /api/eval/history
  /api/experiments /api/backups /api/simulate.
- AppState holds all services; SQLite/subprocess services (RAG,
  knowledge graph, LSP pool) are lazy and closed by stop_state.
- New permissions: computer.observe / computer.control (ask-default).
- Suite: **765 passing** (2 env skips).
- Still open for follow-on 0.7.x releases: autonomy executor should route
  through sandbox_run + RAG context; UI surfaces for health/artifacts/
  backups/skills/eval; mission-node kinds for async jobs; voice STT
  needs audio deps installed + mic device selection UI; Windows desktop
  CI/package run on the 0.7.0 line.

## v0.7.1 perf + timeline checkpoint — commits 6986514 → b6e2572

- Tuner: bounded sweep (batch/ubatch, threads, FA, KV q8_0, draft),
  mark_bad blacklist for OOM/crash configs, classify_launch_error,
  launch fallback ladder tuned→heuristic→bare, context classes +
  ensure_ready(min_context) relaunch for bigger windows.
- Telemetry: cold/warm split, context, cached_tokens, launch surface;
  generation_summary reports cold vs warm TPS + cache hits; twin fed.
- Timeline: mission_id+progress on rows, /api/activity?mission_id=,
  rollup summary per task, mission node rows (task_graph category),
  RECOVERING TASK rows on auto-resume, UI mission chip + progress bar.
- Idle-gated auto-tuner (`55c7d66`): benchmarks untuned resident models
  when the machine is idle; winning configs persist.
- Worktree merge carries explicit git identity — fixes the Linux-runner
  `empty ident` CI failure.
- Autonomy supervisor sandboxes generated node verify commands
  (`Sandbox.run` with repo cwd); `repo_search` tool exposes the
  incremental RAG index to agents (`2df6870`).
- Windows package workflow resolves VERSION at run time — no more
  hardcoded `0.6.0-dev` in Inno defines or artifact names (`b6e2572`).
- Version 0.7.1 synced across artifacts. Suite: **789 passing**
  (2 env skips). GitHub Actions green on `55c7d66` including the
  windows-desktop installer smoke job; final run `36982678215` green on `a44905c` (8m21s, includes installer build + update-preservation smoke).

## v0.7.2 reliability + UX wave — commits 9f3d13f → 9aa3fcb

- Durable state via junctions: `data`, `.agent`, `output` →
  `%LOCALAPPDATA%\NexusCore`; `models` + `ComfyUI_windows_portable` →
  `<install-drive>:\NexusCore` (Program.cs StateTargetRoot; build script
  mirrors both tiers before wiping dist, never traverses junctions).
  Rebuilds can no longer wipe chats, state, or 71GB of models.
- Sidebar: one canonical nav rail on all 11 pages; page controls moved to
  main/right-rail surfaces (`48ca2b5`). System page gains a Diagnostics
  panel fed by `/api/diagnostics` crash_history (`ed3679d`).
- Watchdog auto-restarts now persist to crash_history + component health
  (`a4fadf8`); host records backend exits to crash_history.jsonl too.
- `[nexus-port]` stdout marker — host health-checks the backend's actual
  bound port (port-race fix).
- Mid-stream failure UX: friendly message + diagnostic details +
  one-click retry; SSE error events carry the full payload (`4fcea08`).
- `color-scheme: dark` + themed `select option` — readable dropdowns
  (`9f3d13f`).
- Voice sync: response display holds until first TTS segment when voice
  is on (8s cap + mute/stop release); muted = instant text. Streamer
  emits first clause ~90 chars (`cc86378`).
- Speech filter translates status glyphs to verdicts — ✅/✓/[x] →
  "operating within normal parameters", ❌/✗/[ ] → "failed to
  initialize" (`8345f71`). Hard-coded in `speech_filter.py`.
- Identity: `creator_answer()` states the fact naturally; lock wording
  only on write-attempt refusal (`34b1182`).
- Permissive conversation: expanded `generic_topic_refusal` detection
  (can't assist/provide, not appropriate-or-safe) w/ buffered retry;
  `hard_specific` narrowed to illegal-content-only refusals so real
  restrictions aren't retried away (`9aa3fcb`).
- Runtime verified live: 5 junctions, all models resolve, ComfyUI
  healthy through junction, 5 conversations + 146-msg history intact.
- Suite: **868 passing** (2 env skips).

## v0.12.x checkpoint — profiles, personality, installer update UX

- **v0.12.0 Profiles + Creator Identity + Personality Studio**
  (`0fe5756` → `9cc450e`): UUID-keyed profiles under `data/profiles/<uuid>/`
  with immutable identity and per-profile personality/voice/avatar/settings/
  personal-memory. First-run onboarding lock (`403 onboarding_required`,
  `web/start.html` + `profile.js` nav guard). Creator auth via reserved
  normalized name `John Hamburn` + PBKDF2-HMAC-SHA256 bootstrap→enrollment,
  persisted exponential backoff, neutral errors — passcode never in repo
  (tests derive it as `"0" + str(3211977)`). 73 presets / 47 sliders /
  Personality Strength / 7 moods; adult gating backend-enforced from
  birthdate only. Voice delivery map rides real engine params; personality
  prompt context injects bounded delivery-style hints. ZIP entry is manual
  only (state/city dropdowns stay). 66+9 profile tests.
- **v0.12.1 installer update progress** (`0d3a765`, `078ccdf`): Inno Setup
  previously did multi-minute blocking work with a static wizard — killed
  twice mid-SHA-256 in real logs. Now `ShowBusyStatus` pushes stage text +
  `npbstMarquee` bar to whichever page is active (Ready page via
  `ReadyLabel`, Installing via `StatusLabel`/`FilenameLabel` + model
  progress controls); `BusySleep` chunks grace sleeps so the wizard keeps
  repainting; uninstall wait shows elapsed seconds; download progress
  covers all four bootstrap downloads and flips to "Verifying download
  (SHA-256)" at 100%. Contract test
  `test_installer_reports_busy_stages_during_blocking_update_work`.
- Suite: **1180 passing** (2 env skips); CI green on `042be1c`
  (test 1m54s + windows-desktop 6m32s incl. real ISCC compile and
  install→update-twice smoke with fake-process kill).
- Known item: unsigned setup.exe still gets SmartScreen/Defender
  pre-launch scanning — fix is `NEXUS_CODESIGN_*` secrets, already
  supported by the workflow.

## v0.12.x checkpoint — startup lock-stall crash loop root-caused and fixed

- **Root cause** (`8f164a1`): `ensure_ready()` held the runtime manager's
  global `_lock` across the entire `_spawn_and_wait` llama-server load
  (up to 180s × 3 fallback attempts). `/api/status` calls `statuses()`
  under the same lock → health probes timed out → host killed the
  backend → interrupted-task auto-resume retried on next boot →
  permanent crash loop. Boot prewarm (`runtime_auto_start`) triggers the
  same path. Fix: per-model `_starting` claim + `_starting_cond`, model
  launch waits run lock-free with shared-state mutations in short lock
  sections, `statuses()` does a bounded acquire and returns the
  last-known snapshot under contention. Regression tests
  `test_statuses_answers_while_model_load_in_flight` +
  `test_statuses_falls_back_under_lock_contention` (`d7f8ed1`).
- **Host** (`adae6c0`, `fdf0c08`): writes `backend-host.log` before
  `Process.Start` (launch failures were leaving an empty log), logs
  health-probe state every 15s + last HTTP/error on timeout, reaps
  orphaned backends by exe path not just pidfile, health timeout 2s→10s
  and UI probe 5s→15s. `ReapOrphanedBackend` validates PID reuse against
  the executable path and kills the process tree.
- **Installer** (`adae6c0`): second inert progress bar removed — native
  Inno gauge only; kill escalation name→pidfile→PowerShell path sweep of
  any process running under the install dir; `FileIsWriteLocked` probe
  on `NexusCore.exe`/`ChatNexus.Backend.exe`/`llama-server.exe` with a
  45s live-countdown wait before Inno touches files (fixes "DeleteFile
  failed; code 5" when a backend lingers); stage text covers the file-
  analysis gap between Update click and extraction start.
- **Live diagnosis**: real setup logs showed update runs #003/#004
  killed mid-SHA-256 of `kokoro-v1.0.onnx` (~5s in, static wizard) —
  the "frozen" update was the silent hash, now narrated. "Could not
  start" traced to the lock stall above; a stray test backend produced
  the delete-file-5 update failure and was the concrete repro.
- Emergency unblock applied to the installed copy only (NOT repo):
  `D:\Nexus_Core\config.json` `runtime_auto_start: false` + two stuck
  tasks' `recovery_count` capped past the resume budget. Self-test went
  120s timeout → healthy in 5.9s. `runtime_auto_start` was restored to
  `true` after the fixed build installed and self-test passed again.
- **Post-install silent launch failure** (`d6b61dd`, `46e1ba2`): Inno's
  Run entry fired ~2 s after files landed, inside Defender's on-access
  scan window for the fresh ~200 MB backend exe. `File.Exists` returned
  false → `FileNotFoundException` → splash failure screen, and the check
  ran before the first log write so `backend-host.log` stayed EMPTY.
  Host now writes `backend start requested` first + waits up to 30 s for
  the exe to settle, and `Main` writes a first-breath
  `host process started` marker (distinguishes "exe never ran" from
  "died before backend launch").
- Suite: **1183 passing** (2 env skips); CI green on `8f164a1`,
  `5af1346`, `fdf0c08`. Latest fixed installer artifact verified at
  `dist/installer/NexusCore-Setup-0.12.1-Windows-x64.exe` (sha256
  `65bf088aff885a21b711232bfd8a9f435b458fea139563f0dd17b3f5ac0c0626`).
