# Project Status

> **Takeover note:** Devin should read `DEVIN_START_HERE.md` first. Current repository head: `bfeb413`; verified suite: **338 / 338**; CI green on latest push.

## Active version: 0.6.0-dev — Native Chat Nexus Desktop Dogfood

### Stable capabilities retained from v0.1–v0.3

- ChatGPT-style local coding chat.
- Automatic coding-model routing and escalation.
- Resource-aware llama.cpp runtime lifecycle.
- GGUF inventory and RAM/VRAM telemetry.
- Transactional multi-file patching.
- Per-task checkpoints and undo.
- Approval-gated tool execution with exact-call resume.
- Automatic conservative build/test verification.
- Reviewer-model handoff.
- Persistent task ledger and project memory.
- Lightweight repository index.

### Added through v0.5 development

#### Internet/research

- Public `web_search` tool.
- `fetch_url` readable page/JSON extraction.
- Optional Playwright/Chromium `browser_run` automation.
- Separate `network.read` and `browser.control` permissions.
- Repository-first research coordinator with environment/package inspection.
- Knowledge-gap detection for current/version-sensitive/error-driven tasks.
- Ranked local/official/upstream/community evidence with provenance and local cache.
- Direct versioned GitHub REST research for repositories, issues/PRs, releases, and authenticated code search with cached web fallback.
- Prompt-injection-resistant untrusted-source wrapping and likely-secret query redaction.
- Automatic verification failure → diagnose/research/fix/retest loop with bounded repair cycles.

#### Local images

- Modular `ImageBackend` interface.
- ComfyUI HTTP adapter.
- Optional managed ComfyUI process runtime with health/start/stop/recovery.
- Image model profiles and local weight discovery.
- Automatic image operation/model router.
- Qwen-Image-2.1 quality/editing default profile.
- FLUX.2 Klein 4B fast-preview default profile.
- JSON workflow manager with typed variable substitution.
- Image jobs/history metadata persistence.
- Reference image storage.
- Subject/character profiles.
- Consent record storage and high-risk image safety gate.
- Shared VRAM coordination with coding LLM runtimes.
- Agent image tools for generate/edit/inpaint/outpaint/background removal/upscale/variations.
- Dedicated Image workspace (`/image.html`).
- Local image model/LoRA asset library with install verification and explicit repair/remove operations.
- Operation-specific ComfyUI workflow selection per image model.
- Safe upload of source/reference/mask images into ComfyUI input storage.
- Generated ComfyUI `extra_model_paths.yaml` for Chat Nexus model directories.
- Image Model Manager UI for verify/install/repair/remove plus install progress.
- LoRA discovery/metadata display foundation.
- ComfyUI API-workflow validation before model loading.
- Required ComfyUI node checks before generation.
- Safe Image Model Manager import of validated ComfyUI API workflows.
- Structured image error reporting with friendly messages and expandable technical details.
- Local canvas mask editor for inpaint workflows with paint/erase/fill/invert/save controls.
- Before/after comparison workbench and generated-output reuse as a new editing source.
- Main-chat inline image job cards with polling and Edit/Variation/Upscale/Save controls.
- Validated per-operation ComfyUI API-workflow import from the Image workspace.
- Atomic workflow replacement: invalid/UI-format imports never overwrite the last valid API workflow.
- Workflow import path confinement and a 10 MB workflow JSON limit.
- Backward-compatible `remove_background` / `background_removal` workflow aliasing.
- LoRA resolution against the local library with enabled/version/model-family/strength validation.
- Explicit multi-LoRA workflow slots (`${lora_N_name}` / `${lora_N_strength}`) with early validation before GPU loading.
- Subject profiles now automatically apply saved references, preferred model, generation defaults, and assigned LoRAs.
- Image workspace LoRA filter, one-click selection, and enable/disable controls.
- Structured image error classification with friendly messages and separate technical details for CUDA OOM, backend, model/VAE, LoRA, workflow, dependency, disk, checkpoint, and timeout failures.

### Tests

`330` automated tests passing.

### v0.6 self-hosting progress

- Durable task recovery across application restarts.
- Active tasks that were running/verifying/reviewing are marked `interrupted` on startup instead of being left falsely active.
- Pending approval tasks survive restart and can execute/deny the exact persisted action before the agent re-enters the task loop.
- Interrupted/error tasks can rebuild model/context state from the durable task ledger, checkpoint diff, repository index, project memory, and prior verification results.
- Chat Nexus UI surfaces a **Resume interrupted task** action.
- GitHub Actions test workflow runs on push/PR and currently passes all 338 tests.
- Previously documented Image Studio mask/before-after functionality is now synchronized with the actual shipped HTML/JS and covered by CI.
- Native GitHub coding/delivery tools are wired into the agent: branch, explicit-path commit, push, repository metadata, issue listing/creation, PR creation, and CI status.
- `github.write` defaults to approval-gated; `.agent` metadata is blocked from agent-created commits.
- Live SSE chat streaming now carries token/model/tool/task/research/approval events to the primary UI.
- OpenAI-compatible streamed tool calls are reassembled before execution; non-SSE JSON endpoints fall back safely.
- Isolated self-update validator runs the full tests and launches a second resource-safe Chat Nexus process before self-changes are considered verified.
- CI regression coverage launches the isolated second instance and probes status + main/Image/Research UIs.
- Coding-readiness diagnostics classify the active stack as Setup required / Ready to code / Self-host ready.
- Readiness checks local endpoint health, `llama-server`, GGUF paths/inventory, role coverage, resource fit, Git/selftest availability.
- Local GGUF setup planner suggests role profiles from discovered models and writes them only after explicit user confirmation.
- Explicit checksummed coding-model catalog/downloader with progress, cancellation, repair, atomic install and no silent overwrite.
- Catalog distinguishes official source weights from community quantizations and records source/license/hardware notes.
- Runtime discovery supports both `llama-server` and the 2026 unified `llama serve` command.
- Coding-readiness UI gives safe copyable OS-specific install guidance; system package installers are never executed automatically.
- Fresh/recovered self-development tasks automatically receive Chat Nexus self-hosting guardrails.
- Self-development launcher pre-fills a safe dogfood task but never starts edits automatically.
- Local model-performance telemetry now records content-free task outcomes and contributes a bounded routing score only after resource-fit filtering and a minimum sample threshold.
- `GET /api/model-telemetry` exposes aggregate model/role/complexity statistics; default local history is `.agent/model_performance.json`.
- First-run model setup now exposes the exact model install directory and free disk space, shows live progress in the primary setup card, keeps quick-install controls disabled while a plan is running, and preflights disk capacity before starting large downloads.
- Backend model installation also rejects a new download when the target volume cannot hold the model plus working space; manual/discovered-model configuration is now consistently marked no-restart.
- The canonical Windows installer is now a model bootstrapper: fresh installs download and SHA-256 verify Qwen3 14B + Qwen3-Coder 30B-A3B during setup, while updates skip verified existing models and fetch only missing/untrusted canonical model files.
- Existing-install UI now says **Update Chat Nexus**, asks **Update now?**, and changes the Ready-page action from Install to **Update**. The setup EXE stays small because GGUF bytes are downloaded directly into the final `models` directory instead of being bundled.
- The installer now shows two progress tracks when models are needed: the built-in bar remains the overall install/update progress, while a second model bar appears beneath it, reports the current model and MB downloaded, fills for 14B, then resets for 30B.
- Runtime fit now accounts for reclaimable resident-model RAM/VRAM before a `max_resident_models=1` switch, preventing the loaded 14B model from making the 30B candidate look artificially unavailable.
- Auto mode now falls back to another runnable model if initial/deep/reviewer activation fails; reviewer startup failure no longer terminates an otherwise completed coding task.
- SSE errors are preserved in the chat bubble, and unexpected stream closure consults the durable task ledger for the actual error/interrupted/approval state instead of showing a generic final-result error.
- `/api/chat/stream` now runs model/agent work in a daemon worker and emits queued events plus one-second liveness heartbeats from the request thread, preventing silent idle periods during model startup/first-token latency.
- The UI renders heartbeat phase/model/elapsed time while waiting for the first token.
- Greetings and capability questions use the lightweight `utility` route and skip repository preload, research preflight, and the coding tool schema.
- Qwen3 14B defaults to llama.cpp `--reasoning off` unless explicitly overridden, including preserved older configs; provider requests also carry bounded output-token limits.
- Managed llama.cpp RAM gating now has a bounded near-fit auto-fit band: small estimate gaps (including the observed 30.0 GB estimate vs 29.8 GB available) are allowed to try runtime CPU/GPU placement, while clearly oversized models remain rejected.
- Auto-mode greetings/capability questions now complete through a built-in local utility response without touching model readiness or starting llama.cpp.
- The native desktop captures hidden backend output to `data/logs/backend-host.log` and performs a bounded automatic backend restart on unexpected backend exit.
- Windows Update no longer depends on Inno Restart Manager for the Chat Nexus process tree. Setup owns shutdown/cleanup of the desktop, backend, and llama.cpp child processes before file replacement.
- CI now launches a deliberate long-running executable named `ChatNexus.exe` before the second installer run and fails unless the updater closes it successfully.
- `TaskStore.current()` now returns the newest record rather than searching backward for any interrupted task; old interrupted work remains in Recent tasks but cannot replace a newer completed request.
- Stream-close diagnosis is request-scoped: it prefers the task event observed by the current stream and only accepts global task state if its creation time matches the current request.
- The WebView short-circuits the most basic greeting/capability prompts locally before SSE, and the backend serves UI assets with `Cache-Control: no-store` to eliminate stale post-Update JavaScript.
- Runtime clock grounding now reads the host-local timezone-aware clock on every user turn and injects weekday/date/time/timezone/UTC-offset context into both lightweight and full model prompts; recovered tasks receive a fresh timestamp too.
- Basic date/time/day questions are answered by the built-in local utility path without loading Qwen. `GET /api/time` exposes the same clock and `/api/status` includes a live `clock` snapshot.
- Durable conversation timestamps are converted into bounded temporal-continuity context: recent message times, elapsed age, meaningful inter-message gaps, and recent previous-conversation recency/topic are injected alongside the live clock.
- Conversation-quality prompting now targets mature natural back-and-forth: continuity, non-repetition, varied phrasing, fewer forced follow-up questions, natural contractions, and occasional light dry humor when appropriate.
- Explicit feedback is contextual: thumbs-up/down resolve the exact assistant message and preceding user prompt. Positive pairs become approved Model Growth conversation examples; negative pairs remain negative signals and are excluded from SFT dataset targets.
- Reopened conversation history preserves durable message IDs/timestamps for precise feedback while model history stays role/content-only.
- Main chat streaming now includes an animated **NEXUS CORE // ACTIVE** starship-console HUD showing only real high-level execution state (phase/model/research/tools/verification/review/approval/image/policy retry) plus elapsed time; it never exposes private reasoning.

### Native desktop dogfood checkpoint

- Windows product delivery is now a real **`ChatNexus.exe` desktop app**, not a browser-launch workflow.
- Native host is a self-contained .NET 8 WinForms application using Microsoft WebView2. It launches the Python agent as hidden `backend/ChatNexus.Backend.exe`; pywebview/pythonnet are no longer part of the desktop runtime.
- The internal backend binds to loopback and is owned/shut down by the desktop process.
- The Python agent backend is packaged with PyInstaller while the user-facing `ChatNexus.exe` is the self-contained .NET 8 WinForms/WebView2 desktop host; no console window is shown on normal launch.
- Official Chat Nexus logo is used for the app/UI and its repository bytes are verified.
- Windows CI builds and smoke-tests the packaged executable before artifact upload.
- The package bundles a pinned official llama.cpp **Vulkan x64** runtime under `runtime/llama`, so users no longer need a separate llama.cpp install for the portable build.
- The default model roles are **Qwen3 14B** for fast/general/primary coding and **Qwen3-Coder 30B-A3B** for deep reasoning/review.
- Availability-aware routing falls back from an unavailable 30B model to a runnable primary coder.
- Chat preflight returns one clean Setup required response when no coding model is usable instead of opening a doomed SSE stream.
- Dogfood package includes the project `Source` working copy and preserves its `.git` metadata.
- The Windows ZIP builder rejects legacy pythonnet / `Python.Runtime.dll` / pywebview paths so the CLR-loading crash cannot silently return.
- Current automated checkpoint: **338 tests passing**.



- Model files are not bundled and large image models are not silently downloaded.
- Real Qwen/FLUX ComfyUI API workflows still need to be exported/imported and tested against the chosen local node implementations; the Image workspace now provides a validated Import API workflow action.
- Image jobs require a configured/running ComfyUI backend or `comfyui_auto_start` with a valid local checkout.
- Image progress refines the coarse /history poll with a dependency-free ComfyUI `/ws` listener (`image/ws.py`) that reports real per-node/step progress.
- LoRA file import and richer version/compatibility metadata editing, dedicated upscaler/background-removal adapters, and richer comparison controls remain upcoming.
- Browser automation is optional and requires Playwright + Chromium.

## v0.6 UI checkpoint

- Product renamed to **Chat Nexus** while preserving the existing Python package/repository structure for compatibility.
- Official orbital CN emblem is stored locally under `web/assets/`.
- Approved chat-first shell is implemented: slim left rail, center conversation/composer, right Code Diff / Tasks / Terminal utility rail.
- Existing task approvals, undo, runtime controls, research/image links, model routing, image-job cards, and repository-index controls remain wired to their existing APIs.
- `chat-nexus` CLI entry point added while retaining `local-code-agent` as a compatibility alias.

- Persistent local conversation memory now stores bounded chat history, explicit preferences/rules, and correction examples under `data/conversation_memory.json`.
- Completed tasks now persist `final_content`, and the UI can recover the finished response from durable task state if the final SSE event is lost.
- Short non-coding conversation uses the lightweight utility route; the primary 14B model pre-warms in the background.
- Local system now exposes Memory & training counts.

- Conversation Manager now supports durable/searchable/restorable chats, personality controls, scoped memory, feedback, and the Trainer / Model Growth UI.
- Sourced knowledge memory stores researched answers with provenance/freshness and can refresh current-sensitive knowledge.
- Model Growth Lab supports review candidates, dataset export, versioned offline training-job manifests, evaluation, promotion, and rollback without mutating the live base model in-place.
- Nexus Brain is implemented as protected model-independent state under `data/nexus_brain.json` plus creator-auth metadata. Replacing/downloading model weights does not replace the Brain.
- Brain protected writes are explicit creator actions: staging memories/research/feedback can accumulate while locked, but facts/rules/knowledge/training/autobiography enter the protected Brain only through creator-authenticated Sync.
- Creator passcodes are never stored in plaintext; creator installations keep an encrypted Ed25519 private signing key locally, protected writes require an ephemeral in-memory creator token, and manual protected-data edits fail public signature verification.
- Signed Brain subroutines gate adult-content posture, image generation, web research, long-term memory, self-learning, general-knowledge learning, conversation learning, Model Growth, temporal context, humor, emotions, and the self-model. Image/research/Model Growth routes are code-gated, and adult-content off now blocks explicit image jobs inside ImageManager itself.
- Brain emotions are simulated affective state, with signed baselines plus transient runtime mood; Nexus may be human-like/autobiographically continuous but retains a non-editable `AI system` identity invariant.
- General knowledge can be staged with `Learn that ...` / `Fact: ...`, sourced knowledge is banked with freshness/provenance, and fresh models can retrieve relevant Brain knowledge directly.
- Approved conversation examples/corrections are banked as training signals and can be retrieved as portable in-context conversational skill guidance after a model replacement/export-import cycle.
- Brain distribution is Ed25519 public-key verified. Public Brain exports omit the creator private signing key, activate read-only without the passcode, and accept updates only from the same creator key when the signed Brain is newer.
- Private builds can bundle a signed Brain seed with `CHAT_NEXUS_BRAIN_SEED`; clean installs verify/import it automatically, while existing unrelated/creator Brains are not overwritten.
- Initialized protected Brain state is canonical at `data/nexus_brain.json`; mutable config reload cannot redirect or disable it.
- Conversation policy modes are live-configurable: Permissive / Balanced / Strict. Default Permissive suppresses generic adult/sensitive-topic moralizing; narrow hard tool/action safety remains separate and enforced.
- Policy mode is persisted in `config.json`, surfaced in `/api/status`, and changeable through `POST /api/policy/mode`.
- Permissive adult-only consensual text conversation explicitly allows direct sexual language, including anatomy, acts, fantasies, preferences, and adult erotic fiction; profanity alone is never treated as sexual content.
- If the selected local model still returns a generic topic refusal after the configured retry limit, Chat Nexus reports a **model-level refusal** rather than falsely claiming Chat Nexus policy blocks the topic. Default `generic_refusal_retry_limit` is 3 (bounded 0–5).

## v0.7 modular tool/plugin system — Phase 1 (foundation)

Direction: expand Chat Nexus into a general-purpose local AI workstation where
the agent chooses models/tools/runtimes automatically. Phase 1 delivers the
foundation the later phases build on:

- **Unified Tool Registry.** `ToolSpec` now carries a full manifest (id,
  display name, category, version, provider, capabilities, required
  permissions, network/GPU requirements, supported OS, docs, install status,
  health check). Built-in tools are annotated via a central manifest map; the
  registry is queryable by capability and persists enable/disable state under
  `data/tools_state.json`. Disabled tools leave the model schema and are
  blocked at execution.
- **Tool interfaces.** `localcodeagent/tools/interfaces.py` defines the
  structural contracts (`ITool`, `IExecutableTool`, `IModelBackend`,
  `IImageBackend`, `IBrowserBackend`, `IResearchProvider`, `IMediaTool`,
  `IDataTool`, `IDocumentTool`, `ISandboxProvider`, `IVersionControlProvider`).
- **Plugin manifests.** Declarative JSON manifests under `tools/manifests/`
  register external tools with install detection, health checks, and optional
  subprocess invokers. Non-invocable manifests are catalog entries only and
  never enter model schemas.
- **Permission Manager.** `allow`/`session`/`ask`/`deny` levels plus named
  workspace profiles (`safe`, `developer`, `power_user`, `offline`,
  `research_only`, `custom`), persisted to `config.json`.
- **Job Manager.** One normalized async job ledger aggregating agent tasks,
  image jobs, and model/image installs; state vocabulary matches the Phase 1
  spec (queued → running → waiting_for_tool/permission → completed/failed/
  cancelled). Generic provider jobs persist to `data/jobs.json` and are marked
  failed across restarts.
- **Process Manager.** Central service registry for llama.cpp runtimes and
  ComfyUI with live status/uptime and delegated start/stop/restart.
- **APIs.** `GET /api/tools`, `/api/tools/health/<id>`, `/api/permissions`,
  `/api/jobs`, `/api/processes`, `/api/resources`; `POST /api/tools/state`,
  `/api/permissions/level`, `/api/permissions/profile`, `/api/processes/action`,
  `/api/jobs/cancel`.
- **UI.** New **Tools & Plugins** page (`/tools.html`): permission profile
  switcher, per-permission levels, category-filtered tool registry with
  enable/disable + health checks, process controls, unified job list. Main
  nav Tools link now points at it.
- Docs: `TOOLS.md`, `PERMISSIONS.md`.

Phase 2 kickoff (developer tools):

- **Terminal tools.** `terminal_run` executes through powershell/pwsh/cmd/bash/sh
  with env vars, workspace-bounded cwd, timeouts, and background mode; a
  `TerminalTracker` ties background processes to Job Manager jobs with
  `terminal_processes` / `terminal_kill`.
- **Ripgrep search.** `search_code`, `search_filename`, `search_error` return
  structured JSON matches and fall back to a built-in scanner when `rg` is absent.
- **Build adapters.** `detect_build_system`, `build_project`,
  `configure_project`, `run_tests`, `clean_project` cover CMake, Meson, Cargo,
  .NET, MSBuild, npm/pnpm/yarn, Gradle, Maven, Make, and Python.

- **Media tools.** `media_probe`/`extract_audio`/`trim_video`/`convert_video`/
  `create_thumbnail`/`normalize_audio`/`add_subtitles` wrap FFmpeg with
  validated argv behind `shell.execute`; `media_transcribe` chains
  extract→whisper→subtitles through the registry as a tracked job.
- **Document tools.** `extract_text` (txt/md/html/csv/json/pdf),
  `ocr_image` → Tesseract manifest, `convert_document` → Pandoc manifest.
- **Data tools.** `data_query`/`profile_dataset`/`chart_generate` —
  DuckDB-preferred engine, SQLite/CSV fallback, pure-Python SVG charts.
- **Permission hardening.** Manifest invokers that spawn subprocesses are
  gated on `shell.execute` (or a stricter dedicated key such as
  `docker.access`) regardless of declared read/write keys.

Unit checkpoint: **338 tests passing**.

- Ethical temperature is now a separate 0.0–1.0 conversation control; default **1.0** requests maximum conversational permissiveness within the existing hard tool/action safety boundary.
- Model sampling temperature remains a separate per-model setting and defaults back to **0.2**.
- At high ethical temperature, generic topic-based refusal responses are detected and retried up to three times. Rejected refusals are removed from context before retrying, and conversational output is buffered so canned refusal text is not flashed to the user.

- Auto-mode text-to-image generation now bypasses the chat model and calls the image subsystem directly, preventing normal image prompts from being mislabeled as explicit by the chat model.
- Direct generation respects the existing `image.generate` permission and supports approval without constructing a chat-model session.
- `ImageSafetyPolicy` is the policy source of truth for direct generation; `naked` is treated consistently with `nude` by the explicit-image policy.
- Specialized edit/inpaint/outpaint/upscale requests remain on their dedicated image-tool flow instead of being forced through text-to-image generation.
- Direct text-to-image requests in Auto mode now bypass coding-model readiness at both `/api/chat` and `/api/chat/stream`, so an unavailable coding model cannot block a model-free image job before it reaches the image subsystem.
- Direct-generation intent is centralized and recognizes natural request prefixes such as “can you”, “could you please”, “please”, and “I would like you to” while keeping source-image edits and non-image outputs out of the shortcut.

## Next milestone

**Modular workstation core is in place** (338 tests). Priorities:
1. run real 14B/30B dogfood tasks against the Chat Nexus repository and harden failures found there
2. continue testing real Qwen/FLUX ComfyUI API workflows in parallel without blocking self-hosting
3. validate MCP Streamable HTTP against real MCP servers (local fake-server tests pass)
4. richer resource-manager extraction and runtime controls
5. tree-sitter/LSP indexing upgrade for code intelligence

Done since this list was written: measured generation telemetry (TPS/TTFT in `ModelPerformanceTelemetry.record_generation`, surfaced via `/api/model-telemetry` and the Models page Performance card — `17895b7`), learned routing statistics in the Tools UI and Models UI, SSE event bus for tool/job updates, MCP Streamable HTTP transport (`9a2d793`), vault `secret:` env references for MCP (`c1bd188`), cooperative workflow cancellation (`e05f7a4`), durable work queue with agent-scheduled follow-ups (`queue_task`), busy-chat auto-queueing, per-task live-output routing, and reconnect-safe agent-event mirroring onto `/api/events` so page reloads keep live terminal/task visibility (`12fbfbe`).
