# Session Handoff — Chat Nexus / Coding_Agent

## Canonical product/UI identity

- Product name: **Chat Nexus**.
- Repository/source of truth: `afterburn25/Coding_Agent`.
- Official mark: orbital cyan/blue/violet **CN** emblem at `web/assets/chat-nexus-emblem.png`.
- Canonical primary UI: chat-first center pane, slim left navigation, and right **Code Diff / Tasks / Terminal** utility rail.
- Do not replace this shell with unrelated dashboard/IDE concepts unless the user explicitly changes direction.
- UI details are documented in `docs/UI_DIRECTION.md`.

## v0.6 Chat Nexus UI checkpoint

- The approved primary UI is implemented in `web/index.html` / `web/styles.css` / `web/app.js`.
- Chat remains the control surface; existing backend IDs/APIs were preserved during the redesign.
- Right rail maps task state into Code Diff / Tasks / Terminal activity instead of inventing separate fake state.
- Official Chat Nexus emblem is served locally from `web/assets/chat-nexus-emblem.png`.
- Image Studio and Research Hub now use Chat Nexus branding.
- Runtime/CLI identity is now Chat Nexus v0.6; the old `local-code-agent` CLI remains as a compatibility alias.
- Next development priority is self-hosting reliability: streaming, resume/recovery, GitHub actions, isolated self-test instance, and dogfood tasks.

## v0.6 restart recovery + CI checkpoint

- Durable task ledger now distinguishes genuine in-process work from tasks interrupted by an application restart.
- Persisted `waiting_approval` actions can be resumed after a full restart without the original in-memory agent session.
- `POST /api/tasks/recover` rebuilds a safe continuation context from the task, model role, checkpoint diff, project memory, repository index, research preflight, and prior verification results.
- The main Chat Nexus UI exposes a **Resume interrupted task** button in the Tasks rail.
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
- For the Chat Nexus source tree, `detect_verification_commands()` now selects one approval-gated selftest command that runs unit tests **and** an isolated second-instance smoke test.
- The second instance uses a temporary resource-safe config, a free 127.0.0.1 port, and never loads coding/image models.
- Smoke probes: `/api/status`, `/`, `/image.html`, `/research.html`.
- CI includes a real second-process regression test.
- Checkpoint: **78/78 tests passing**.
- Core self-hosting safeguards are now present. Next practical step is local model/setup readiness and then real dogfood development tasks inside Chat Nexus.

## v0.6 coding readiness/setup checkpoint

- `RuntimeManager.readiness()` evaluates the actual coding stack: endpoints, managed runtime executable/model files, RAM/VRAM fit, role coverage, and recommendations.
- `GET /api/readiness` adds self-hosting workspace/Git/selftest state.
- Main UI shows **Setup required**, **Ready to code**, or **Self-host ready** instead of a misleading generic GPU-ready label.
- `localcodeagent/runtime/setup.py` suggests role assignments from already-present GGUF files.
- `POST /api/readiness/configure` requires `apply=true`; the UI also asks for confirmation. It preserves non-model config and requires restart after changing model profiles.
- No coding model is silently downloaded or replaced.
- CI checkpoint: **86/86 tests passing**.
- Next practical blocker: an explicit model catalog/downloader, then first real Chat Nexus-on-Chat Nexus dogfood task.

## v0.6 coding model catalog checkpoint

- `localcodeagent/runtime/catalog.py` contains the explicit auditable coding-model catalog/downloader.
- Initial catalog: official Qwen3-14B Q4_K_M starter; community Qwen3-Coder-30B-A3B-Instruct Q4_K_M with the base/source distinction shown in UI.
- Exact expected file sizes/SHA256 values are embedded and tested.
- Download lifecycle: explicit confirmation → `.part` streaming → progress/cancel → exact size+SHA256 → atomic rename + trusted metadata.
- Existing unverified model files are not overwritten without explicit Repair.
- API: `GET /api/models/catalog`, `GET /api/models/install/<job>`, `POST /api/models/install`, `POST /api/models/install/cancel`, `POST /api/models/verify`.
- The readiness drawer renders model source/type/roles/hardware note and live install progress.
- CI checkpoint: **90/90 tests passing**.
- Next: first-run `llama-server` bootstrap guidance, then real Chat Nexus dogfood development.

## v0.6 llama.cpp bootstrap checkpoint

- Runtime discovery recognizes legacy `llama-server(.exe)` and the newer unified `llama(.exe)`; unified launches insert the `serve` subcommand automatically.
- Readiness provides platform-specific install guidance, including Winget on Windows, but never executes package-manager commands automatically.
- The verified coding-model catalog + discovered-GGUF role writer + runtime bootstrap now form a complete first-run path from no models to managed local coding.
- CI checkpoint: **93/93 tests passing**.
- The project is ready to begin real Chat Nexus-on-Chat Nexus dogfood tasks once a local model/runtime is installed on the user's machine.

## v0.6 first dogfood-ready checkpoint

- Self-hosting context is automatic for both fresh and recovered tasks when the workspace is the Chat Nexus source tree.
- Guardrails require repository-first inspection, preservation of the running instance, explicit Git/GitHub delivery intent, and isolated selftest before claiming success.
- UI **Start self-development task** only pre-fills the prompt; the user must explicitly send it.
- First-run path is complete: runtime guidance → explicit verified model catalog download → discovered-GGUF role assignment → restart → readiness check → self-development launcher.
- CI checkpoint: **95/95 tests passing**.
- Next action: package/test the exact main-branch snapshot, then begin real local-model dogfood work.

## v0.6 native Windows desktop checkpoint

- **Canonical Windows deliverable:** a real native `ChatNexus.exe`, not a browser launcher. Future dogfood/release work should produce the Windows desktop artifact by default; source ZIPs are secondary.
- Native UI host: `desktop/ChatNexus.Desktop` — self-contained .NET 8 WinForms + Microsoft WebView2. It launches hidden `backend/ChatNexus.Backend.exe`; the loopback HTTP backend is private implementation plumbing. `localcodeagent/desktop.py`, pywebview, pythonnet, and CLR hosting were removed after the frozen `Python.Runtime.dll` startup failure.
- Packaged/frozen `localcodeagent.__main__` launches desktop mode automatically; `--server` is development/debug only.
- CI builds the Python backend with PyInstaller, publishes the native .NET desktop host self-contained for win-x64, then runs `ChatNexus.exe --self-test` to verify the host → hidden backend → UI integration before upload.
- Portable bundle includes pinned official llama.cpp `b11278` Vulkan x64 runtime in `runtime/llama`; runtime discovery prefers the bundled copy.
- Default coding roles: Qwen3 14B Q4_K_M = utility/fast/primary; Qwen3-Coder 30B-A3B Q4_K_M = deep_reasoner/reviewer.
- The 30B is an intended automatic route for difficult tasks. If its GGUF/runtime is unavailable, the router can fall back to a runnable primary coder rather than failing.
- No-model chat attempts are blocked before SSE starts and return a single `coding_model_setup_required` response; the UI opens Coding readiness without duplicating the error.
- Dogfood app defaults its workspace to bundled `Source` when present. The Windows ZIP packaging explicitly preserves `Source/.git` so it remains a real Git working tree.
- Windows package guard rejects any pythonnet / `Python.Runtime.dll` / pywebview path before producing an artifact.
- Official logo asset was replaced with byte-verified valid PNG data after CI caught a corrupt/truncated prior asset.
- Native desktop checkpoint was **102/102 tests passing**; the current overall installer/upgrade checkpoint is **110/110 tests passing**.
- Next: install/download the 14B and 30B GGUFs on the target machine and begin real Chat Nexus-on-Chat Nexus dogfood development.

## v0.6 installer/upgrade checkpoint

- **Canonical Windows deliverable is now the installer EXE**: `Chat-Nexus-Setup-<version>-Windows-x64.exe`. Portable ZIPs are secondary.
- Canonical installer definition: `installer/ChatNexus.iss`; do not recreate a duplicate installer under `packaging/`.
- Stable installer identity: `ChatNexus.Afterburn25`.
- Default per-user location: `%LOCALAPPDATA%\Programs\Chat Nexus`.
- Interactive reinstall detects the existing version/location and asks whether to **Upgrade now?**; silent mode continues the upgrade for CI.
- Upgrade replaces immutable app/backend/runtime files but preserves downloaded `models\`, `data\`, imported `workflows\`, customized `config.json`, and the existing `Source\.git` workspace/local task state.
- First install explicitly embeds both `Source\*` and hidden `Source\.git\*`. A deterministic pre-install flag decides once whether Source should be seeded; upgrades do not overwrite it.
- Inno `Excludes` patterns are comma-separated: `Source\*,models\*,data\*,config.json`.
- CI builds the native .NET app first, compiles the LZMA2 solid-compressed installer, performs a fresh install, runs `ChatNexus.exe --self-test`, writes preservation markers/config state, runs the same installer a second time without `/DIR`, verifies the existing path is rediscovered and mutable state survives, then runs the self-test again.
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
- Next: dogfood installed Chat Nexus with the downloaded 14B/30B stack and harden real model/task behavior.

## v0.6 no-restart first-run model checkpoint

- Fresh install UI exposes **Install recommended 14B**, **Install full 14B + 30B stack**, and later **Add 30B deep coder**.
- Downloads remain explicit and checksum-verified; no multi-gigabyte coding model is silently bundled or fetched.
- `POST /api/readiness/configure` now rewrites model roles, reloads `AgentConfig`/RuntimeManager/ModelRouter/AgentOrchestrator live, and starts the primary model when possible.
- The old “Restart Chat Nexus once” requirement is removed.
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
- Next: complete the local 14B/30B installation on the dogfood machine and run real Chat Nexus-on-Chat Nexus coding tasks.

## v0.6 installer model-bootstrap / Update checkpoint

- The canonical Windows installer now bootstraps the default coding stack during setup instead of leaving model installation as a post-install requirement.
- Fresh install behavior: download **Qwen3 14B Q4_K_M** then **Qwen3-Coder 30B-A3B Instruct Q4_K_M** directly into `{app}\models` while Inno Setup displays its normal install/download progress.
- Both downloads use pinned HTTPS URLs, exact external sizes, and SHA-256 verification before the destination filenames are committed.
- Existing-install behavior: Setup detects the prior stable `ChatNexus.Afterburn25` installation, uses its existing directory, checks each canonical model, preserves trusted/verified files, and downloads only missing/untrusted model files.
- Installer-written `.catalog/*.json` metadata keeps models recognized as verified by Chat Nexus immediately after setup.
- The visible existing-install flow is now **Update Chat Nexus** / **Update now?** and the Ready-page action button changes to **Update** instead of Install.
- The installer EXE remains small; the ~25.66 GiB model stack is fetched during setup and is not embedded in the EXE.
- CI sets `CHAT_NEXUS_SKIP_MODEL_DOWNLOADS=1` only for installer smoke tests so CI does not consume ~25.7 GB. Normal installers keep model downloads enabled.
- Feature commit: `960c3f0` — Bootstrap coding models during Windows setup.
- Unit checkpoint: **119/119 tests passing**.
- Windows native desktop/installer fresh-install + update-preservation validation: **green**.
- Next: run this installer interactively on the dogfood Windows machine, confirm real 14B/30B download progress and model readiness, then start the first real Chat Nexus-on-Chat Nexus development task.

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
- Active branch/worktree is now v0.6 Chat Nexus/self-hosting development.
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
3. Add WebSocket/SSE streaming for chat tokens, tool events, research events, and image-generation progress.
4. Add persistent browser sessions and richer browser selectors/snapshots.
6. Add model-performance and research-outcome telemetry to improve automatic routing.

## Testing command

```bash
python -m unittest discover -s tests -v
```

Expected at this checkpoint: `119 tests` passing.
