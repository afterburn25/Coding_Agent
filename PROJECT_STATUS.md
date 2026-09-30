# Project Status

## Active version: 0.4.0-dev — Web + Local Image Foundation

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

### Added in v0.4 development

#### Internet/research

- Public `web_search` tool.
- `fetch_url` readable page/JSON extraction.
- Optional Playwright/Chromium `browser_run` automation.
- Separate `network.read` and `browser.control` permissions.

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

### Tests

`27` automated tests passing.

### Important current limitations

- Model files are not bundled and large image models are not silently downloaded.
- Real Qwen/FLUX ComfyUI API workflow templates still need to be added/tested against the chosen local node implementations.
- Image jobs require a configured/running ComfyUI backend or `comfyui_auto_start` with a valid local checkout.
- Image progress is currently polling-based; ComfyUI WebSocket progress events are not wired into the app yet.
- Main chat can call image tools, but finished images are not yet automatically rendered inline in chat.
- Mask painting UI, advanced before/after viewer, LoRA library manager, model download/repair UI, and dedicated upscaler/background-removal adapters remain upcoming.
- Browser automation is optional and requires Playwright + Chromium.

## Next milestone

Complete v0.4 by implementing and testing real image workflows, model/LoRA management, inline chat image results, richer progress streaming, mask/before-after UX, and persistent browser sessions.
