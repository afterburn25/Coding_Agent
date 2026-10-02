# Project Status

> **Takeover note:** Devin should read `DEVIN_START_HERE.md` first. Verified suite: **698 / 698** (2 environment skips); see SESSION_HANDOFF.md for the autonomy and local voice checkpoints.

## Active version: 0.6.1-dev — Native Nexus Core Desktop Dogfood

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

#### Local voice

- Local-first TTS via Kokoro-82M ONNX (`kokoro-onnx 0.6.1`, CPU default, Apache-2.0).
- Provider abstraction (`TTSEngine`) — Kokoro is the first engine, not the only one.
- Official preset `Nexus Synthetic — Isabella`: `bf_isabella` shaped by a tunable
  parallel-layer DSP chain (neural/glass/micro + exciter + compressor + stereo + limiter).
- Block-level `SpeechTextFilter` — code/logs/diffs/JSON/URLs are never read aloud.
- Sentence-level streaming speech on `/api/chat/stream`; per-message replay; global
  mute stops playback instantly everywhere.
- Voice Studio (`/voice.html`): any base voice, Natural↔Synthetic slider, A/B compare,
  preset CRUD + import/export; user presets survive upgrades.
- `voice_*` ToolRegistry entries under `audio.*` permissions; bounded WAV cache;
  failure can never break text chat.

#### Local images

- Modular `ImageBackend` interface.
- ComfyUI HTTP adapter.
- Optional managed ComfyUI process runtime with health/start/stop/recovery.
- Image model profiles and local weight discovery.
- Automatic image operation/model router.
- Juggernaut X v10 (RunDiffusion SDXL, pinned rev e53841ec) default text-to-image profile.
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
- Generated ComfyUI `extra_model_paths.yaml` for Nexus Core model directories.
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
- Nexus Core UI surfaces a **Resume interrupted task** action.
- GitHub Actions test workflow runs on push/PR and currently passes all 362 tests.
- Previously documented Image Studio mask/before-after functionality is now synchronized with the actual shipped HTML/JS and covered by CI.
- Native GitHub coding/delivery tools are wired into the agent: branch, explicit-path commit, push, repository metadata, issue listing/creation, PR creation, and CI status.
- `github.write` defaults to approval-gated; `.agent` metadata is blocked from agent-created commits.
- Live SSE chat streaming now carries token/model/tool/task/research/approval events to the primary UI.
- OpenAI-compatible streamed tool calls are reassembled before execution; non-SSE JSON endpoints fall back safely.
- Isolated self-update validator runs the full tests and launches a second resource-safe Nexus Core process before self-changes are considered verified.
- CI regression coverage launches the isolated second instance and probes status + main/Image/Research UIs.
- Coding-readiness diagnostics classify the active stack as Setup required / Ready to code / Self-host ready.
- Readiness checks local endpoint health, `llama-server`, GGUF paths/inventory, role coverage, resource fit, Git/selftest availability.
- Local GGUF setup planner suggests role profiles from discovered models and writes them only after explicit user confirmation.
- Explicit checksummed coding-model catalog/downloader with progress, cancellation, repair, atomic install and no silent overwrite.
- Catalog distinguishes official source weights from community quantizations and records source/license/hardware notes.
- Runtime discovery supports both `llama-server` and the 2026 unified `llama serve` command.
- Coding-readiness UI gives safe copyable OS-specific install guidance; system package installers are never executed automatically.
- Fresh/recovered self-development tasks automatically receive Nexus Core self-hosting guardrails.
- Self-development launcher pre-fills a safe dogfood task but never starts edits automatically.
- Local model-performance telemetry now records content-free task outcomes and contributes a bounded routing score only after resource-fit filtering and a minimum sample threshold.
- `GET /api/model-telemetry` exposes aggregate model/role/complexity statistics; default local history is `.agent/model_performance.json`.
- First-run model setup now exposes the exact model install directory and free disk space, shows live progress in the primary setup card, keeps quick-install controls disabled while a plan is running, and preflights disk capacity before starting large downloads.
- Backend model installation also rejects a new download when the target volume cannot hold the model plus working space; manual/discovered-model configuration is now consistently marked no-restart.
- The canonical Windows installer is now a model bootstrapper: fresh installs download and SHA-256 verify Qwen3 14B + Qwen3-Coder 30B-A3B during setup, while updates skip verified existing models and fetch only missing/untrusted canonical model files.
- Existing-install UI now says **Update Nexus Core**, asks **Update now?**, and changes the Ready-page action from Install to **Update**. The setup EXE stays small because GGUF bytes are downloaded directly into the final `models` directory instead of being bundled.
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
- Windows Update no longer depends on Inno Restart Manager for the Nexus Core process tree. Setup owns shutdown/cleanup of the desktop, backend, and llama.cpp child processes before file replacement.
- CI now launches a deliberate long-running executable named `NexusCore.exe` before the second installer run and fails unless the updater closes it successfully.
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

- Windows product delivery is now a real **`NexusCore.exe` desktop app**, not a browser-launch workflow.
- Native host is a self-contained .NET 8 WinForms application using Microsoft WebView2. It launches the Python agent as hidden `backend/ChatNexus.Backend.exe`; pywebview/pythonnet are no longer part of the desktop runtime.
- The internal backend binds to loopback and is owned/shut down by the desktop process.
- The Python agent backend is packaged with PyInstaller while the user-facing `NexusCore.exe` is the self-contained .NET 8 WinForms/WebView2 desktop host; no console window is shown on normal launch.
- Official Nexus Core logo is used for the app/UI and its repository bytes are verified.
- Windows CI builds and smoke-tests the packaged executable before artifact upload.
- The package bundles a pinned official llama.cpp **Vulkan x64** runtime under `runtime/llama`, so users no longer need a separate llama.cpp install for the portable build.
- The default model roles are **Qwen3 14B** for fast/general/primary coding and **Qwen3-Coder 30B-A3B** for deep reasoning/review.
- Availability-aware routing falls back from an unavailable 30B model to a runnable primary coder.
- Chat preflight returns one clean Setup required response when no coding model is usable instead of opening a doomed SSE stream.
- Dogfood package includes the project `Source` working copy and preserves its `.git` metadata.
- The Windows ZIP builder rejects legacy pythonnet / `Python.Runtime.dll` / pywebview paths so the CLR-loading crash cannot silently return.

### Autonomous missions checkpoint

- `localcodeagent/autonomy/` adds a persistent mission layer: `data/autonomy/` JSON stores (schema v1) for missions, standing goals, triggers, schedules, grants, approvals, notifications, plus append-only `receipts.jsonl`/`audit.jsonl`.
- `AutonomousSupervisor` runs bounded event-driven ticks (no uncontrolled loop): reclaim leases → fire schedules/triggers → plan → execute DAG nodes in worker threads → verify → evaluate → complete/replan/escalate.
- `TaskGraph` gives dependency-aware DAG execution with node leases, parallel independent branches, blocked/failed propagation, and cycle rejection; `ResourceLocks` provides exclusive named lanes; interactive chat always wins the agent lane.
- `MissionPlanner` decomposes objectives into plan/research/implement/verify steps; `MissionEvaluator` checks success criteria (`all_tasks_completed`, `verify_passed`, `artifact_exists`, metrics) before completion — tool success is never assumed equal to mission success.
- `RecoveryManager` classifies failures (CUDA OOM, WinError 10054, tool crash, approval-required, test failure…) into bounded playbooks — wait/retry/repair/replan/escalate — with per-mission budgets (`max_task_retries`, `max_repair_loops`, `max_same_failure_retries`, `max_runtime_s`); repeated identical failure signatures halt instead of looping.
- `AutonomyPolicy` profiles (`supervised`/`local_autonomous`/`extended_autonomous`/`custom`) sit on top of the existing PermissionManager and can only narrow it; sensitive actions (`git_push`, `create_pr`, `packages`, `delete_data`, `credentials`, `outbound_message`) require a standing grant (scoped/expirable/revocable) or a mission approval.
- Approvals pause a mission into `waiting_approval` and resume the exact suspended step; denial triggers replanning; pending approvals persist across restart.
- Durable scheduler (once/interval/daily/weekly — missed runs catch up after downtime) plus a trigger engine (file_changed, startup, ci_*, model_runtime_failed, custom, …) with conditions, debounce, and workspace-confined file watches; standing goals spawn recurring missions.
- `stop autonomy` / `resume autonomy` (chat command, API, or UI) immediately pauses all live missions and denies new autonomous work.
- Restart safety: `MissionStore._recover_orphans()` re-parks missions that were mid-execution when the process died — running nodes return to `ready`, completed work is never repeated.
- Missions UI at `web/missions.html` (list/detail, status, task graph, approvals, controls); chat commands `make this a mission`, `stop autonomy`, `resume autonomy` answer locally in both streaming and non-streaming chat.
- Autonomy status/mission/approval/goal/schedule/trigger/notification APIs under `/api/autonomy/*`; supervisor emits `mission`/`notification`/`autonomy` bus events; ActivityStore gained `autonomy`/`mission` categories.
- `tests/test_autonomy.py` adds 49 tests: persistence, restart recovery, corrupt-store quarantine, DAG/leases/locks, failure playbooks, policy/grants/stop, notifications, schedules, triggers, evaluator, approvals, denial→replan, stop-autonomy, lane arbitration, budgets, standing goals.
- Reference: `docs/AUTONOMY.md`.

### Platform workstation expansion (v0.7.0 line)

- Canonical `VERSION` file + `localcodeagent/version.py` + `scripts/sync_version.py`: pyproject, Inno defaults, .NET project, PyInstaller backend resource, and `.agent/project.json` all derive from one source; `--check` runs in tests.
- **Computer Use**: `localcodeagent/computer_use/` (screenshots via PIL/PowerShell fallback, window enum/focus, mouse/keyboard/clipboard via SendInput) exposed as `computer_*` tools behind new `computer.observe`/`computer.control` permissions; audited to the activity timeline.
- **Sandbox**: `localcodeagent/sandbox/` — temp-workspace python/JS/shell exec with timeout, Windows Job Object memory+kill limits, network-deny, artifact extraction; `sandbox_run` tool.
- **LSP**: `localcodeagent/lsp/` stdio JSON-RPC client (definitions/references/document+workspace symbols/hover/diagnostics), pooled per server, graceful without installed servers.
- **Repo RAG**: `localcodeagent/rag/` incremental SQLite index (files/symbols/chunks, mtime+sha incremental) — `search()` for coding workflows.
- **Skills**: `localcodeagent/skills/` — skill.json/SKILL.md packages with capabilities/tools/permissions/instructions; install/enable/disable persisted under `data/skills/`.
- **Multi-agent worktrees**: `localcodeagent/multiagent.py` — role-scoped agents (planner/coder/reviewer/tester/debugger/security) in isolated git worktrees; controlled merge-back aborts cleanly on conflict.
- **Eval Lab + Experiments**: `localcodeagent/eval/` — suite runs with history/compare; experiment records (hypothesis→arms→metrics→conclusion).
- **Artifacts**: `localcodeagent/artifacts.py` — provenance registry (creator/mission/task, sha256, versioning, managed store).
- **Backups**: `localcodeagent/backups.py` — versioned manifest-verified backups of Brain/Answer Memory/autonomy/config/skills; verified restore with pre-restore stash; last valid never pruned.
- **Vault hardening**: secret store key DPAPI-wrapped per Windows user; pattern redaction for API keys/tokens/private-key blocks.
- **Connectors**: `localcodeagent/connectors/` — capability/permission/rate-limit/health/reconnect/audit framework.
- **Knowledge graph**: `localcodeagent/knowledge/` — SQLite entity+edge store with bounded BFS context (`context_for`) for prompt injection.
- **Simulation**: `localcodeagent/simulate.py` + `/api/simulate` — dry-run steps/files/permissions/verdicts/failure points; never executes.
- **Digital Twin**: `localcodeagent/twin.py` — measured hardware samples + model measures; predicts RAM/VRAM fit, load/TTFT/TPS; hardware-fingerprint invalidation.
- **Health**: `localcodeagent/health.py` — component probe+recover, bounded attempts, persisted history; autonomy/voice registered.
- **Two-way voice scaffold**: `localcodeagent/voice/stt.py` — mic capture (sounddevice, optional), Vosk/faster-whisper engines, barge-in interrupt, latency metrics.
- Server: `/api/health` `/api/twin` `/api/artifacts` `/api/skills` `/api/connectors` `/api/knowledge` `/api/rag` `/api/lsp` `/api/eval/history` `/api/experiments` `/api/backups` `/api/simulate`.
- Current automated checkpoint: **830 tests passing** (2 environment skips).

#### v0.7.1 — performance + timeline

- **Tuner sweep**: bounded benchmark candidates now vary batch/ubatch, threads,
  flash-attention, KV-cache q8_0, and speculative decoding (when a draft model
  exists); probe failures classified oom/crash/timeout; `mark_bad` blacklist
  ensures configs that crash a real launch are never re-picked while the
  fingerprint holds.
- **Launch fallback**: tuned → heuristic-safe → bare flag ladder in
  `_launch_with_fallback`; bad tuning cannot brick startup.
- **Context classes**: `context_class()` small/medium/large/xlarge; orchestrator
  passes `min_context` to `ensure_ready`, which relaunches the resident server
  at a bigger window when a task needs it (never silently truncates).
- **Cold/warm telemetry**: per-generation records carry `cold`, `context`,
  `cached_tokens`, and launch surface; `generation_summary` reports cold vs
  warm TPS + prompt-cache hits; measurements feed the Digital Twin.
- **Timeline**: rows carry `mission_id`/`progress`; `/api/activity` supports
  `mission_id` filter and returns a per-task rollup (tools/models/errors/
  retries/elapsed); mission nodes appear as `task_graph` rows; auto-resume
  opens a `RECOVERING TASK` row; UI shows mission chip + progress bar.
- **Idle auto-tuner**: `AppState._auto_tune_worker` benchmarks untuned
  resident models while the machine is idle (no active tasks/queues) and
  persists winning configs — tuning no longer requires a manual
  `/api/tuning` call.
- **Sandboxed verify**: generated node verify commands run through
  `Sandbox.run` (repo cwd, limits, cleanup); detected project commands
  still run on the host.
- **RAG as tool**: `repo_search` registered in the ToolRegistry against
  the lazy incremental index — agents can query symbols/chunks directly.
- **CI**: worktree merges carry explicit git identity (fixes Linux-runner
  `empty ident` failures); the Windows package workflow resolves
  `VERSION` into `APP_VERSION`/`APP_NUMERIC_VERSION` for Inno defines and
  artifact names instead of hardcoded `0.6.0-dev`. GitHub Actions run `36982678215` green
  on `a44905c` including the windows-desktop package + installer smoke
  job.
- Final commits this wave: `6986514` (0.7.1 perf+timeline), `55c7d66`
  (auto-tuner + CI identity fix), `2df6870` (sandboxed verify + RAG
  tool), `b6e2572` + `a44905c` (workflow version resolution +
  contract-test fix), `8e477c8` (System page), `17fdaea` (job nodes).

#### v0.7.2 — UI unification + transport-failure hardening

- **Shared design system**: `styles.css` owns the token palette
  (`--bg`/`--surface`/`--panel`/`--panel2`, `--line`, `--text`,
  `--accent`, status colors + tints) plus shared primitives
  (`.workspace-link`, `.side-toggle`, `.btn` variants, `.page-head`,
  `.empty-state`, element-level themed form controls, scrollbars).
  All page stylesheets migrated off divergent gray/navy hexes.
- **Canonical nav**: every secondary page renders the same 11-link
  workspace nav with an `.active` marker; voice toggle and jump links
  styled consistently.
- **Chat fixes**: `.main` grid no longer crushes the composer onto a
  fixed track; `[hidden]` beats class `display`; textarea auto-resizes;
  collapsible utility rail; mobile topbar nav below 760px; fixed
  `grid-template-colums` typo and the undefined `--purple` reference.
- **UI regression tests**: `tests/test_ui_layout.py` (19 tests) guards
  tokens, canonical nav, grid safety, composer autoresize, and shared
  components across all 11 pages.
- **Transport hardening (WinError 10054)**: `netdiag` failure history
  persists to bounded JSONL with recovery-outcome annotation;
  `HealthService.report()` push API for immediate state transitions;
  mid-run llama.cpp crashes feed `mark_bad()` so crashing configs are
  never re-picked; MCP stdio/HTTP failures carry structured diagnostics;
  ComfyUI transport errors enter the shared failure history.
- Found + documented a real port-collision case: a stale dev backend
  (`python -m localcodeagent` from an earlier session) was squatting on
  :8765 serving `0.6.0-dev` while the packaged app ran on a dynamically
  chosen port — the exact class of stale-process issue this hardening
  targets.

#### Verified benchmark — user hardware (2026-10-02)

RTX 3080 Ti 12 GB (10.36 GB free), 64 GB RAM, 24 logical cores,
llama.cpp build 11278, Qwen3-14B Q4_K_M at ctx 8192 with `gpu_layers
auto` + `fit_target 1024` (full GPU offload). Six bounded candidates,
each measured cold + warm via a real streamed request:

| Candidate | Prompt t/s | Gen t/s | TTFT ms | Warm TTFT |
|---|---|---|---|---|
| FA, batch 512/ubatch 256, 12t | 589 | 20.9 | 576 | 44.9 |
| FA, batch 1024/512, 12t | 600 | 19.1 | 422 | 27.2 |
| FA, batch 256/128, 12t | 1286 | 26.7 | 196 | 27.1 |
| FA, batch 512/256, 23t | **2011** | **78.8** | **146** | 26.9 |
| FA+KV q8_0, batch 512/256, 12t | 193 | 76.9 | 1239 | 22.2 |
| no-FA, batch 512/256, 12t | 2004 | 78.7 | 125 | 34.9 |

Winner persisted: `--flash-attn auto --cache-reuse 256 --batch-size 512
--ubatch-size 256 --threads 23`. Measured finding: the stable ceiling on
this GPU is ~78-79 t/s, but the first three candidates measured 19-27
t/s — consistent with transient VRAM contention during the early probes
(only 10.36 GB was free for a ~10 GB model+KV at ctx 8192). That is
exactly the class of result static tables miss: the tuner picked the
config that measured best under real conditions rather than assuming
more threads or more batch helps. `--cache-reuse` reported
`cached_tokens=0` on this build; flash-attention was neutral, not a
regression.



- Model files are not bundled and large image models are not silently downloaded.
- Real Qwen/FLUX ComfyUI API workflows still need to be exported/imported and tested against the chosen local node implementations; the Image workspace now provides a validated Import API workflow action.
- Image jobs require a configured/running ComfyUI backend or `comfyui_auto_start` with a valid local checkout.
- Image progress refines the coarse /history poll with a dependency-free ComfyUI `/ws` listener (`image/ws.py`) that reports real per-node/step progress.
- LoRA file import and richer version/compatibility metadata editing, dedicated upscaler/background-removal adapters, and richer comparison controls remain upcoming.
- Browser automation is optional and requires Playwright + Chromium.

## v0.6 UI checkpoint

- Product renamed to **Nexus Core** while preserving the existing Python package/repository structure for compatibility.
- Official orbital CN emblem is stored locally under `web/assets/`.
- Approved chat-first shell is implemented: slim left rail, center conversation/composer, right Code Diff / Tasks / Terminal utility rail.
- Existing task approvals, undo, runtime controls, research/image links, model routing, image-job cards, and repository-index controls remain wired to their existing APIs.
- `chat-nexus` CLI entry point added while retaining `local-code-agent` as a compatibility alias.

- Persistent local conversation memory now stores bounded chat history, explicit preferences/rules, and correction examples under `data/conversation_memory.json`.
- Completed tasks now persist `final_content`, and the UI can recover the finished response from durable task state if the final SSE event is lost.
- Short non-coding conversation uses the lightweight utility route; the primary 14B model pre-warms in the background.
- Local system now exposes Memory & training counts.
- Chat composer `+` attachments: menu-driven file/image picker, removable chips with thumbnails, text/code files inlined into the model's user turn (bounded), images persisted under `data/attachments` and routed into image workflows, attachment-only messages allowed, and queued requests retain attached context.

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
- If the selected local model still returns a generic topic refusal after the configured retry limit, Nexus Core reports a **model-level refusal** rather than falsely claiming Nexus Core policy blocks the topic. Default `generic_refusal_retry_limit` is 3 (bounded 0–5).

## v0.7 modular tool/plugin system — Phase 1 (foundation)

Direction: expand Nexus Core into a general-purpose local AI workstation where
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

Latest UI restructure (current main):

- **Tools & Plugins** is now the operational catalog/runtime surface: summary
  cards (installed/available/updates/running), search + filter chips + sort,
  responsive card grid, per-tool detail panel (Overview/Capabilities/
  Dependencies/Configuration/Logs), collapsible Installation Queue with
  resumable-download state, speed/ETA, current file/path. Image packs,
  processes, jobs, work queue, routing telemetry, workflows preserved.
- **Permissions moved to Settings → Permissions** (`/settings.html`):
  profiles, category summaries, searchable matrix, per-key detail panel with
  approval rules (incl. new `creator` level — requires unlocked Nexus Brain),
  domain/dir/repo scope editors, and a persisted bounded audit log
  (`data/permission_audit.jsonl`). Tool installation and permission control
  are intentionally separate surfaces.

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

Unit checkpoint: **362 tests passing**.

- Ethical temperature is now a separate 0.0–1.0 conversation control; default **1.0** requests maximum conversational permissiveness within the existing hard tool/action safety boundary.
- Model sampling temperature remains a separate per-model setting and defaults back to **0.2**.
- At high ethical temperature, generic topic-based refusal responses are detected and retried up to three times. Rejected refusals are removed from context before retrying, and conversational output is buffered so canned refusal text is not flashed to the user.

- Auto-mode text-to-image generation now bypasses the chat model and calls the image subsystem directly, preventing normal image prompts from being mislabeled as explicit by the chat model.
- Direct generation respects the existing `image.generate` permission and supports approval without constructing a chat-model session.
- `ImageSafetyPolicy` is the policy source of truth for direct generation; `naked` is treated consistently with `nude` by the explicit-image policy.
- Specialized edit/inpaint/outpaint/upscale requests remain on their dedicated image-tool flow instead of being forced through text-to-image generation.
- Direct text-to-image requests in Auto mode now bypass coding-model readiness at both `/api/chat` and `/api/chat/stream`, so an unavailable coding model cannot block a model-free image job before it reaches the image subsystem.
- Direct-generation intent is centralized and recognizes natural request prefixes such as “can you”, “could you please”, “please”, and “I would like you to” while keeping source-image edits and non-image outputs out of the shortcut.

## Nexus Answer Memory — learned Q&A fast path

- Persistent SQLite (`data/nexus_brain/answer_memory.db`, WAL, schema v1)
  stores *experiences* (every exchange, unverified) separately from *trusted
  answers* — model output is never auto-trusted.
- Tiered request pipeline: deterministic handlers → exact/semantic Answer
  Memory lookup → Brain knowledge → utility model → deep model. Trusted hits
  skip hardware probing and all model loading (measured ≈0.08 ms exact,
  ≈0.13 ms semantic at 200 answers vs multi-second model inference).
- Semantic matching uses `hashed-ngram-v1` (deterministic local embedder,
  no download) with proper-token, antonym, and symmetric-swap conflict gates;
  precision is favored over recall — borderline matches inject as context
  instead of bypassing.
- Trust promotion requires evidence (explicit learn, user correction,
  repeated confirmation + positive feedback). Corrections invalidate the old
  answer and store the replacement as trusted user input.
- Freshness: `live` questions never bypass; `repository_dependent` /
  `config_dependent` answers go stale automatically when the HEAD or config
  fingerprint changes.
- Secrets are refused before persistence; no chain-of-thought is stored;
  corruption quarantines the DB instead of deleting it; connections are
  per-operation so the file is never held open (Windows-safe).
- Confirmed paraphrases self-learn as aliases — verified-equivalent model
  answers teach the memory new phrasings over time.
- UI: Learned Answers page (`/answers.html`) with search/filter/learn/
  forget/mark-incorrect/update/merge/refresh plus export/import, rebuild,
  vacuum; 🧠 per-message Learn button in chat; "Answered from memory" badge
  on memory-served responses; Answer Memory activity in the task timeline.
- HTTP API: `/api/answer-memory` list/stats, `/export`, `/learn`,
  `/forget`, `/mark-incorrect`, `/update`, `/merge`, `/refresh`, `/clear`,
  `/rebuild-index`, `/vacuum`, `/import`. Thumbs feedback feeds trust scoring.
- Reference: `docs/ANSWER_MEMORY.md`. Tests: `tests/test_answer_memory.py`
  (48 cases); full suite **606 passing** (2 environment skips).

## Backend connection reliability — WinError 10054 recovery (v0.6.1)

- **Root cause:** a local backend (llama.cpp/llama-server, ComfyUI) closing
  a socket surfaced as a bare `ConnectionResetError`/`IncompleteRead`. The
  SSE reader iterated the raw response and only caught `URLError`, and the
  orchestrator's recovery loop only caught `RuntimeError` — so `OSError`
  transport failures escaped recovery entirely and reached the user raw.
- `localcodeagent/netdiag.py` classifies every transport failure
  (reset/aborted/refused/broken-pipe/incomplete-read/timeout) into a
  `BackendConnectionError` carrying subsystem, redacted endpoint, host,
  port, request id, model, phase (connect/read/stream), elapsed time,
  chunk/partial-output counts, and a `friendly` user message.
- `RuntimeManager.backend_health(model_id)` snapshots process state, PID,
  exit code, restart count, log tail and RAM/VRAM, and recognises crash
  signatures (VRAM/RAM exhaustion, CUDA failure, access violation, model
  load failure, port bind failure) so a backend crash reports the real
  cause instead of a socket error.
- Recovery loop now catches `OSError`/`http.client` transport failures,
  attaches backend health, emits a `backend_failure` model event, and
  retries through a fresh connection after `runtime.recover()`. Retries
  are bounded by `runtime_recovery_attempts` and are **never** attempted
  once tokens were already delivered (no duplicated partial responses);
  4xx rejections are still never retried.
- `/api/diagnostics` aggregates per-model backend health, managed-service
  state, hardware picture and a bounded ring of the last 25 transport
  failures with their recovery outcome; the Models sidebar gains a
  **Diagnostics** button that copies a full crash report.
- ComfyUI calls (`submit`/`status`/`cancel`/upload/download) classify the
  same way; non-streaming `/api/chat` 500s map to the friendly message
  with the diagnostic attached.
- Tests: `tests/test_netdiag.py` (19 cases — socket reset mid-stream,
  connect-phase refusal, truncated bodies, bounded retries, no-retry
  after delivered output, crash-signature health reports); full suite
  **643 passing**.

## Next milestone

**Modular workstation core + Answer Memory are in place** (643 tests). Priorities:
1. run real 14B/30B dogfood tasks against the Nexus Core repository and harden failures found there
2. continue testing real Qwen/FLUX ComfyUI API workflows in parallel without blocking self-hosting
3. validate MCP Streamable HTTP against real MCP servers (local fake-server tests pass)
4. richer resource-manager extraction and runtime controls
5. tree-sitter/LSP indexing upgrade for code intelligence

Done since this list was written: measured generation telemetry (TPS/TTFT in `ModelPerformanceTelemetry.record_generation`, surfaced via `/api/model-telemetry` and the Models page Performance card — `17895b7`), learned routing statistics in the Tools UI and Models UI, SSE event bus for tool/job updates, MCP Streamable HTTP transport (`9a2d793`), vault `secret:` env references for MCP (`c1bd188`), cooperative workflow cancellation (`e05f7a4`), durable work queue with agent-scheduled follow-ups (`queue_task`), busy-chat auto-queueing, per-task live-output routing, and reconnect-safe agent-event mirroring onto `/api/events` so page reloads keep live terminal/task visibility (`12fbfbe`).
