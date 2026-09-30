# Project Status

## Active version: 0.5.0-dev — Research Intelligence + Local Image Workflow

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
- Generated ComfyUI `extra_model_paths.yaml` for Local Code Agent model directories.
- Image Model Manager UI for verify/install/repair/remove plus install progress.
- LoRA discovery/metadata display foundation.
- ComfyUI API-workflow validation before model loading.
- Required ComfyUI node checks before generation.
- Safe Image Model Manager import of validated ComfyUI API workflows.
- Main-chat inline image job cards with polling and Edit/Variation/Upscale/Save controls.
- Validated per-operation ComfyUI API-workflow import from the Image workspace.
- Atomic workflow replacement: invalid/UI-format imports never overwrite the last valid API workflow.
- Workflow import path confinement and a 10 MB workflow JSON limit.
- Backward-compatible `remove_background` / `background_removal` workflow aliasing.

### Tests

`49` automated tests passing.

### Important current limitations

- Model files are not bundled and large image models are not silently downloaded.
- Real Qwen/FLUX ComfyUI API workflows still need to be exported/imported and tested against the chosen local node implementations; the Image workspace now provides a validated Import API workflow action.
- Image jobs require a configured/running ComfyUI backend or `comfyui_auto_start` with a valid local checkout.
- Image progress is currently polling-based; ComfyUI WebSocket progress events are not wired into the app yet.
- Mask painting UI, advanced before/after viewer, full LoRA import/version/compatibility management, and dedicated upscaler/background-removal adapters remain upcoming.
- Browser automation is optional and requires Playwright + Chromium.

## Next milestone

Complete v0.5 by importing/testing real Qwen/FLUX API workflows through the new workflow manager, completing LoRA injection/management, adding richer progress streaming, mask/before-after UX, persistent browser sessions, and research telemetry/source presentation in the UI.
