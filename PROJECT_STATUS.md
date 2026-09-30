# Project Status

## Active version: 0.6.0-dev — Chat Nexus Self-Hosting UI

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

`64` automated tests passing.

### Important current limitations

- Model files are not bundled and large image models are not silently downloaded.
- Real Qwen/FLUX ComfyUI API workflows still need to be exported/imported and tested against the chosen local node implementations; the Image workspace now provides a validated Import API workflow action.
- Image jobs require a configured/running ComfyUI backend or `comfyui_auto_start` with a valid local checkout.
- Image progress is currently polling-based; ComfyUI WebSocket progress events are not wired into the app yet.
- LoRA file import and richer version/compatibility metadata editing, dedicated upscaler/background-removal adapters, and richer comparison controls remain upcoming.
- Browser automation is optional and requires Playwright + Chromium.

## v0.6 UI checkpoint

- Product renamed to **Chat Nexus** while preserving the existing Python package/repository structure for compatibility.
- Official orbital CN emblem is stored locally under `web/assets/`.
- Approved chat-first shell is implemented: slim left rail, center conversation/composer, right Code Diff / Tasks / Terminal utility rail.
- Existing task approvals, undo, runtime controls, research/image links, model routing, image-job cards, and repository-index controls remain wired to their existing APIs.
- `chat-nexus` CLI entry point added while retaining `local-code-agent` as a compatibility alias.

## Next milestone

Move directly toward **v0.6 self-hosting/dogfooding**:
1. add SSE/WebSocket streaming for model tokens, tool output, task/research/image progress
2. add persistent task recovery/resume after process restart or model failure
3. add first-class GitHub coding actions (branch/commit/push/PR/CI) behind explicit permissions
4. launch an isolated second Chat Nexus instance for self-update smoke tests before accepting self-modifications
5. run real dogfood tasks against the Chat Nexus repository and harden failures found there
6. continue testing real Qwen/FLUX ComfyUI API workflows in parallel without blocking self-hosting
