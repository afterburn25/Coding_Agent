# Architecture

> For the current Devin takeover checkpoint, security invariants, CI/artifacts, and next steps, read `DEVIN_START_HERE.md` first.

## Core layers

1. **UI** — local ChatGPT-style interface plus runtime and task workflow panels.
2. **Agent Core** — conversation/tool loop, durable task state, approval pauses, verification, and reviewer handoff.
3. **Workflow Services** — checkpoints, task ledger, project memory, persistent/scoped conversation memory, Conversation Manager, sourced knowledge memory, repository index, verification detection.
4. **Tool System** — a manifest-driven Tool Registry over permissioned filesystem, transactional patch, shell, Git, repository-index, research, web, and image tools, plus declarative plugin manifests and (planned) MCP tools.
5. **Permission Manager** — central action policy: `allow` / `session` / `ask` / `deny` levels plus named workspace profiles (safe, developer, power_user, offline, research_only).
6. **Job Manager** — one normalized asynchronous job ledger aggregating agent tasks, image jobs, and install/download jobs.
7. **Process Manager** — central registry of controllable services (llama.cpp runtimes, ComfyUI, future MCP servers) with live status and delegated start/stop/restart.
8. **Model Orchestrator** — maps task scope/phase to model roles and resource-fit candidates.
9. **Runtime Manager** — managed inference processes, health/recovery, hardware telemetry, and residency.
10. **Model Growth Lab** — review candidates, dataset export, offline training manifests, evaluation, promotion, and rollback.
11. **Nexus Brain** — model-independent creator-signed long-term memory, general/conversational learning, autobiographical continuity, signed subroutines, emotional profile, self-model, public read-only export/verification, and same-creator signed updates.
12. **Providers** — normalize OpenAI-compatible inference APIs.

```text
UI
 │
 ├──────────────── Task workflow / approvals / undo
 │
 ▼
Agent Core ──────────────── Tool Registry
 │                            │
 │                            ├─ Filesystem / apply_patch
 │                            ├─ Shell
 │                            ├─ Git
 │                            └─ Repository index
 │
 ├──────── Workflow Services
 │          ├─ Task Store
 │          ├─ Checkpoints
 │          ├─ Project Memory
 │          ├─ Repository Index
 │          └─ Verification Detector
 │
 ▼
Model Orchestrator
 │
 ▼
Runtime Manager
 │
 ├─ Hardware detector
 ├─ GGUF inventory
 ├─ Residency manager
 ├─ Health / recovery
 └─ llama.cpp launcher
 │
 ▼
OpenAI-compatible provider
 │
 ├─ managed llama-server
 └─ external local endpoint
```

## Modular tool/plugin layer (v0.7 foundation)

```text
Agent Orchestrator
  ↓
Tool Registry (manifests, capabilities, enabled state)
  ├─ built-in tools          localcodeagent/tools/*.py
  ├─ manifest tools          <runtime>/tools/manifests/*.json
  └─ MCP tools               planned
  ↓
PermissionManager            allow | session | ask | deny + profiles
  ↓
JobManager                   unified async job ledger
ProcessManager               managed service lifecycle
RuntimeManager               hardware telemetry, residency, VRAM arbitration
```

Every tool registers a `ToolSpec` manifest (id, category, capabilities,
required permissions, network/GPU requirements, install status, health check).
The agent receives OpenAI schemas only for enabled, callable tools; catalog-only
manifests stay visible in the Tool Manager without entering model context.
`interfaces.py` defines the structural tool-family contracts (executable,
model, image, browser, research, media, data, document, sandbox, VCS) so
providers are interchangeable. See `TOOLS.md` and `PERMISSIONS.md`.

## v0.3 task state machine

```text
planning
   ↓
working ────────────────┐
   │                    │
   ├─ gated tool ─→ waiting_approval
   │                    │ approve/deny
   │◀───────────────────┘
   ↓
verifying ── gated shell ─→ waiting_approval
   ↓
reviewing
   ↓
done
```

The orchestrator keeps the live model/tool continuation state in memory while `TaskStore` durably records task status, files, verification, review, and summary.

`ConversationMemory` is app-wide and stored under the preserved `data/` area. It keeps bounded recent chat plus explicit preferences/rules and correction examples. Prompt-time rules affect behavior immediately; model-weight training remains a separate explicit offline workflow.

`ConversationManager` owns durable conversation sessions, search/restore, summaries, personality, and feedback. `KnowledgeMemory` stores sourced learned answers with provenance and expiration/freshness metadata. `ModelGrowthLab` turns reviewed behavior/correction/knowledge candidates into versioned offline training datasets/jobs and only promotes evaluated artifacts explicitly.

Conversation-policy posture is prompt-layer configuration (`permissive`, `balanced`, `strict`). It changes refusal sensitivity/tone but does not replace the narrow hard policies enforced by specific tool/action modules.

Text-to-image generation is a deterministic intent route in Auto mode: recognized generation requests bypass the chat model and execute the registered `generate_image` tool directly. This keeps the image subsystem's router, permissions, queue/history, and `ImageSafetyPolicy` authoritative. Edit/inpaint/outpaint/upscale operations remain on specialized image-tool selection paths.

## Transactional mutation model

Filesystem edits are tracked per task. On the first mutation of a path, `CheckpointManager` records whether it existed and stores the original bytes if necessary.

`apply_patch` stages and validates every requested file change before it mutates disk. Exact replacement counts must match. Only after the full patchset validates are checkpoints created and files atomically replaced.

Undo replays the checkpoint in reverse depth order, restoring originals and deleting task-created files.

## Verification and review

After a changed task reaches a no-tool final response:

1. `detect_verification_commands()` identifies conservative project tests/builds.
2. Commands pass through the existing `shell.execute` permission.
3. The task moves to `reviewing`.
4. Model routing is called with `phase="review"`.
5. The reviewer receives the original task, changed files, and checkpoint-derived unified diff.
6. Task summary/review are written to local project memory and the repository index is refreshed.

## Repository intelligence

v0.3 ships a dependency-free index containing:

- source/text file paths
- size and mtime
- basic class/function symbols
- compact beginning-of-file preview

Search scoring prioritizes path matches, then symbols, then preview text. Future versions can replace or augment this with language servers, tree-sitter, embeddings, and reference graphs without changing the agent tool interface.

## Runtime lifecycle

For a managed `llama_cpp` profile:

```text
Route task/phase
  ↓
Check current RAM / VRAM
  ↓
Choose a fitting model profile
  ↓
Already healthy?
  ├─ yes → reuse
  └─ no
      ↓
Enforce residency limit
      ↓
Launch llama-server
      ↓
Poll /health until ready
      ↓
Execute agent request
```

A later workflow phase (for example review) can route to a different model, causing Runtime Manager to perform the corresponding switch.

## Autonomous operation layer

```text
POST /api/queue (durable .agent/queue.json, FIFO, 200-item bound)
  ↓ dequeue only when no task is active — triggered on enqueue,
  task completion, and resume/recover (lock-serialized single-flight)
agent.run / recover (single-flight)
  ↓
live terminal: tool_start → tool_output chunks → tool completion
  ↓ (post-redaction)
persisted transcript .agent/terminal/<task>.log (512 KiB bound)
  ↓
bounded execution: agent_tool_timeout_seconds, context trimming,
  step-limit continuations (autonomous_max_continuations)
  ↓
failure → autonomous_error_retry_seconds backoff, bounded by
  autonomous_max_recoveries; crash → startup auto-resume
```

- `EventBus` (`events.py`) fans events to `/api/events` SSE subscribers with a
  100-event replay buffer; consecutive `tool_output` chunks for the same
  tool+task coalesce in history so replay keeps task context. All agent-event
  producers (chat stream, queue worker, auto-resume, error retry) share
  `AppState._bus_emit` — tokens/results stay chat-only, everything else gets
  `task_id` attribution.
- The chat page keeps its direct `/api/chat/stream` connection authoritative
  (`agentStreamActive` suppresses bus duplicates); on reload the bus
  subscription + `/api/tasks` + `/api/task-log` restore the live view, and
  the transcript re-fetches on every EventSource reconnect so output emitted
  during a disconnect gap is recovered in place.
- Transcript markers cover the whole run: `$ tool args`, live output,
  `· tool ok|failed|timeout`, `## task <status/phase>` transitions,
  `## approval required`, `## image <op/state/stage>` (state-change deduped),
  `## model`/`## research`/`## perf` markers, `## error`, and the final
  `## result` block (4 KiB preview) on terminal status — plus crash lines
  for failures before the session loop begins.
- `PermissionManager.set_autonomous` auto-approves `ask`/`session` workspace
  actions; hard gates (spend/message/mic/camera) and `deny` are never touched.
  `autonomous_approval_timeout_seconds` bounds hard-gate waits.
- `RuntimeManager.evict_idle` on the watchdog tick unloads models idle past
  `model_idle_unload_seconds` or under `memory_pressure_*` floors, while
  pinning models serving active tasks — `ensure_ready` restarts them
  transparently. Managed ComfyUI likewise stops after
  `comfyui_idle_unload_seconds` with no active image jobs (external installs
  are never touched); image-job events stream over `image_job` bus events.
- Growth bounds for unattended runs: task ledger keeps 100 records; orphaned
  terminal logs and `.agent/checkpoints/` snapshots are pruned on startup;
  queue is capped at 200; `routing_telemetry.jsonl` compacts past 512 KiB
  (in-memory deque maxlen 500); JobManager trims at 300 records; request-local
  SSE queues drop token/output chunks under backpressure while task, tool,
  and result events always land. Per-model llama.cpp logs, `terminal-*.log`
  job output, `comfyui.log`, and the desktop `backend-host.log` all retain a
  bounded tail instead of appending forever. On exit, `stop_state` shuts down
  MCP server subprocesses, tracked background terminal processes, the managed
  ComfyUI process, and all model runtimes; the desktop host restarts a crashed
  backend (crash-loop bound of 3 restarts inside 5 minutes).

## v0.4 Web + Image capability layers

The core agent remains responsible for conversation/task planning. Web and image functions are tools, not hard-wired into the chat model.

```text
Chat / Coding UI
      │
      ▼
Agent Core ───────────────────────────────┐
  │                                      │
  ├─ Coding tools                        ├─ Web tools
  │   ├─ filesystem / patch              │   ├─ web_search
  │   ├─ shell / tests                   │   ├─ fetch_url
  │   └─ git / repository index          │   └─ browser_run (optional Playwright)
  │                                      │
  └──────────────────────────────────────┤
                                         ▼
                                   Image tools
                                         │
                                   ImageManager
                                         │
                ┌────────────────────────┼─────────────────────────┐
                ▼                        ▼                         ▼
           ImageRouter            WorkflowManager           Subject/Consent
                │                        │                         │
                └────────────────────────┼─────────────────────────┘
                                         ▼
                                  ImageBackend interface
                                         │
                          ┌──────────────┴──────────────┐
                          ▼                             ▼
                    ComfyUIBackend                Future backend
                          │
                    ComfyUIRuntime
                          │
                 local model/node stack
```

### Independent routing planes

There are now two model-routing planes:

1. `ModelRouter` chooses the conversational/coding/reasoning model.
2. `ImageRouter` chooses the image model/workflow for an image tool call.

This prevents the chat LLM from needing to know low-level image runtime details. A small/fast chat model can still call `generate_image`, while the image router independently chooses a large quality model.

### GPU arbitration

Image generation and LLM inference share a global hardware view. Before starting an image job, `ImageManager` compares the selected image model's configured VRAM estimate with current free VRAM. `RuntimeManager.release_managed_models_for_vram()` may stop one or more agent-owned llama.cpp runtimes according to the configured image resource mode. External model servers are never terminated. When configured, stopped coding models are restored after the image job.

### Image persistence

Default structure:

```text
models/image/
  qwen/
  flux/
  stable-diffusion/
  loras/
  vae/
  controlnet/
  upscalers/

data/image/
  generations/
  references/
  characters/
  consent_records.json
  history.json
  jobs.json
workflows/image/
```

Each generation output receives adjacent JSON metadata recording the request, route, model, workflow/job metadata, timestamps, and output path.

### ComfyUI boundary

The main application talks only to the `ImageBackend` interface. `ComfyUIBackend` currently implements `/system_stats`, `/object_info`, `/prompt`, `/history/{prompt_id}`, `/view`, and `/interrupt` interactions. `ComfyUIRuntime` optionally owns the local ComfyUI process. This keeps ComfyUI replaceable rather than making it part of application-domain logic.

### Internet boundary

Public research and interactive browsing use separate permissions. `web_search` and `fetch_url` are read-only research tools under `network.read`. Full Chromium automation is exposed as `browser_run` under the stronger `browser.control` permission and is optional at install time.

## Profile identity layer (v0.12)

Nexus Core runs on durable human profiles — `localcodeagent/profiles/`
(UUID-keyed dirs under `data/profiles/<uuid>/`) and
`localcodeagent/personality/` (presets, slider whitelist, strength,
moods, voice map, greetings). While `ProfileManager.onboarding_required`
the HTTP handler returns 403 for every protected API; onboarding-safe
endpoints and static assets pass. Identity fields are immutable,
protected fields (`is_creator`, `creator_*`) are produced only by the
Creator verification path (PBKDF2-HMAC-SHA256, bootstrap→enrollment
credential file, persisted rate limiting), and adult eligibility is
always derived from the immutable birthdate — enforced in the services,
not the UI. Personality is a presentation-only layer over reasoning:
whitelisted sliders/presets shape text style and voice delivery
(`voice_map` → Kokoro speed + DSP pitch/tempo/gain, documented prosody
hints for unsupported acoustics); facts, code correctness, tool
permissions, and safety state are untouched. Personal memory is isolated
per profile; chats/models/Brain stay device-global. See
`docs/PROFILES.md` for the full model.
