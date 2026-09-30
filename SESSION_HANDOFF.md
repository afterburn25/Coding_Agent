# Session Handoff — Coding_Agent

## Source of truth
GitHub repository: `afterburn25/Coding_Agent`.

Future sessions should begin by reading:
1. `README.md`
2. `PROJECT_STATUS.md`
3. `ARCHITECTURE.md`
4. `SESSION_HANDOFF.md`
5. `MODEL_ROUTING.md`
6. `docs/IMAGE_MODULE_SPEC.md` for image work
7. `docs/WEB_RESEARCH.md` for internet/browser work

Do not reconstruct project state from chat memory when the repository can answer it. Update this file and `PROJECT_STATUS.md` before ending substantial development work.

## Local commit history at this checkpoint
- `46b9477` — Add v0.4 web research and local image foundation
- `7448d8a` — Add v0.3 transactional coding workflow
- `9d61887` — Add v0.2 runtime manager and resource-aware model switching
- `168b51a` — Bootstrap Local Code Agent v0.1.0

## Current baseline
- Stable coding workflow through v0.3.
- v0.4 development adds web research/browser tools and local image generation/editing.
- Automated tests: **27 passing**.
- Server smoke test passed for `/api/status`, `/api/image`, and `/image.html`.

## v0.4 implemented
### Web
- `web_search`
- `fetch_url`
- optional Playwright `browser_run`
- separate network/browser permissions

### Image system
- `localcodeagent/image/` modular architecture
- independent image router
- ComfyUI backend abstraction and optional managed runtime
- workflow manager
- image model discovery
- job/history persistence
- subject profiles
- adult consent records and safety gate
- shared LLM/image VRAM coordination
- Image workspace with uploads, model/operation/quality/resolution/seed controls, advanced controls, jobs, cancellation and gallery
- image tools exposed to chatbot

## Next executable steps
1. Add tested ComfyUI API workflows for Qwen-Image-2.1 and FLUX.2 Klein 4B.
2. Add image model download/install/verify/remove/repair manager.
3. Build LoRA library and compatibility/version controls.
4. Add mask editor and proper before/after viewer.
5. Add dedicated background-removal/upscale adapters.
6. Render finished image jobs inline in main chat.
7. Add SSE/WebSocket streaming for chat/tool/image progress.
8. Add persistent browser sessions and richer browser snapshots.
9. Add agent-native GitHub integration.
10. Add per-model performance telemetry to improve routing.
