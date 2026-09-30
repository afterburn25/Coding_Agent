# Local Code Agent

A local-first ChatGPT-style coding workstation with automatic model switching, transactional coding workflows, web research/browser tools, and a modular local image-generation/editing system.

**GitHub source of truth:** `afterburn25/Coding_Agent`

Future development sessions should begin with `README.md`, `PROJECT_STATUS.md`, `ARCHITECTURE.md`, and `SESSION_HANDOFF.md`.

## Current development version

`0.4.0-dev`

The stable v0.3 coding workflow remains intact while v0.4 adds internet and local-image capabilities.

## Core coding-agent capabilities

- ChatGPT-style local chat UI.
- Automatic model roles: utility, fast coder, primary coder, deep reasoner, reviewer, vision.
- Automatic escalation when tasks become harder or repeated attempts fail.
- RAM/VRAM-aware model choice.
- Managed llama.cpp model start/health/stop/recovery.
- GGUF inventory.
- Workspace-contained filesystem tools.
- Transactional multi-file patches.
- Per-task checkpoints and one-click undo.
- Shell/build/test tools with permission gates.
- Reviewer-model handoff.
- Persistent task ledger, project memory, and repository index.

## Internet access

The agent can research current information instead of relying only on model training data:

- `web_search`
- `fetch_url`
- optional real Chromium automation through `browser_run`

`network.read` and `browser.control` are separate permissions. Browser automation is optional so the core remains lightweight.

Install browser support:

```bash
pip install -e '.[browser]'
playwright install chromium
```

See `docs/WEB_RESEARCH.md`.

## Local image generation/editing

The image system is intentionally backend/model modular:

```text
Agent
  ↓ image tool
ImageManager
  ↓
ImageRouter
  ↓
WorkflowManager
  ↓
ImageBackend
  ├─ ComfyUIBackend
  └─ future native/other backends
```

The initial model profiles are:

- **Qwen-Image-2.1** — preferred quality/editing route.
- **FLUX.2 Klein 4B** — preferred fast preview/draft route.

Weights are not bundled or silently downloaded. Configure installed paths/workflows in `config.json`.

Supported tool surfaces already include:

- generate image
- edit image
- inpaint
- outpaint
- background removal workflow
- upscale workflow
- variations
- subject/character profile loading
- image model listing

The dedicated workspace is available at:

```text
http://127.0.0.1:8765/image.html
```

It provides drag/drop references, conversational prompting, Auto/manual model selection, operation/quality/resolution/count/seed controls, advanced edit controls, queue progress/cancellation, and an image gallery.

The complete requested image specification is preserved at `docs/IMAGE_MODULE_SPEC.md`.

## Shared GPU management

On systems such as an RTX 3080 12 GB, coding LLMs and image models cannot always remain resident together. Before an image job, the resource manager can stop agent-owned llama.cpp runtimes to free VRAM and optionally restore them when generation completes.

Configurable image resource policy values are designed around:

- Prefer Chat Model
- Balanced
- Prefer Image Model
- Aggressive VRAM Cleanup

The current implementation accepts these semantics through `image_resource_mode`; UI presets are still being completed.

## Image safety/consent records

The local image subsystem permits lawful adult-only synthetic content while implementing strict gates for minor/ambiguous-age sexual imagery, non-consensual intimate imagery, and explicit real-person edits without an active adult consent record. Fully synthetic adults do not require a consent record.

Consent records and subject profiles remain local under `data/image/` unless the user explicitly configures something else.

## Requirements

- Python 3.11+
- Recent `llama-server` for managed coding models
- Local GGUF coding model(s)
- Optional ComfyUI checkout for image generation
- Installed local image model/node stack and API-format workflows
- NVIDIA drivers / `nvidia-smi` for NVIDIA-aware routing (application still runs without it)
- Optional Playwright + Chromium for full browser automation

## Windows quick start

1. Clone/extract the project.
2. Copy `config.example.json` to `config.json`.
3. Point coding model profiles at real GGUF files/endpoints.
4. If using images, configure `comfyui_dir`, installed image weights, and workflow JSON paths.
5. Start:

```bat
start_windows.bat C:\path\to\workspace-you-want-the-agent-to-edit
```

Then open:

```text
http://127.0.0.1:8765
```

Image workspace:

```text
http://127.0.0.1:8765/image.html
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Current expected result: **27 tests passing**.

## API highlights

```text
GET  /api/status
GET  /api/models
GET  /api/runtime
GET  /api/tasks
GET  /api/index
GET  /api/image
GET  /api/image/history
POST /api/chat
POST /api/tasks/resume
POST /api/tasks/undo
POST /api/runtime/start
POST /api/runtime/stop
POST /api/index/rebuild
POST /api/image/upload
POST /api/image/generate
POST /api/image/cancel
POST /api/image/profile
POST /api/image/consent
POST /api/image/backend/start
POST /api/image/backend/stop
POST /api/image/backend/inspect
```

## Development state

See `PROJECT_STATUS.md` for implemented/remaining features and `SESSION_HANDOFF.md` for the exact handoff point for the next development session.
