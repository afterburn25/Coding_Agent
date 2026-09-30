# Architecture

## Core layers

1. **UI** — local ChatGPT-style interface plus runtime and task workflow panels.
2. **Agent Core** — conversation/tool loop, durable task state, approval pauses, verification, and reviewer handoff.
3. **Workflow Services** — checkpoints, task ledger, project memory, repository index, verification detection.
4. **Tool System** — permissioned filesystem, transactional patch, shell, Git, and repository-index tools.
5. **Model Orchestrator** — maps task scope/phase to model roles and resource-fit candidates.
6. **Runtime Manager** — managed inference processes, health/recovery, hardware telemetry, and residency.
7. **Providers** — normalize OpenAI-compatible inference APIs.

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
