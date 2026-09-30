# Chat Nexus

Chat Nexus is a local-first ChatGPT-style coding workstation with automatic model switching, transactional coding workflows, web research/browser tools, and a modular local image-generation/editing system.

**GitHub source of truth:** `afterburn25/Coding_Agent`

Future development sessions should begin with `README.md`, `PROJECT_STATUS.md`, `ARCHITECTURE.md`, and `SESSION_HANDOFF.md`.

## Current development version

`0.5.0-dev`

The stable coding workflow remains intact while v0.5 adds repository-first research intelligence, automatic verification repair/retest cycles, and hardens the local-image workflow.

## Chat Nexus identity and v0.6 UI

The product name is **Chat Nexus**. The GitHub repository remains `afterburn25/Coding_Agent` as the development source of truth.

The canonical v0.6 desktop/web shell is chat-first:
- slim left navigation for Chat / Projects / Models / Research / Images / Tools / Settings
- center conversation workspace with automatic model routing
- right utility rail with **Code Diff / Tasks / Terminal** tabs
- official cyan → blue → violet orbital **CN** emblem
- dark navy UI with restrained neon accents
- local-system details remain available without dominating the chat experience

See `docs/UI_DIRECTION.md` before changing the primary application shell.

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

The agent can research current information instead of relying only on model training data. The v0.5 research coordinator performs a local repository/environment preflight, identifies version-sensitive knowledge gaps, ranks evidence by source authority/version relevance, caches research, and exposes dedicated documentation/GitHub/error-research tools. Failed verification can automatically return the agent to a diagnose → research → patch → retest loop.

Lower-level tools remain available:

- `web_search`
- `fetch_url`
- optional real Chromium automation through `browser_run`

`network.read` and `browser.control` are separate permissions. Likely secrets are redacted from research queries, retrieved pages are treated as untrusted information, and browser automation remains optional. See `docs/RESEARCH_SYSTEM.md` and `docs/WEB_RESEARCH.md`.

GitHub research now prefers the versioned GitHub REST API for repository/issue/PR/release/source metadata and falls back to normal web research when needed. Public research works without a token at lower rate limits; for higher limits set the environment variable named by `research_github_token_env` (default `GITHUB_TOKEN`). The token itself is never stored in `config.json`.

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

Weights are not bundled or silently downloaded. The Image Model Manager can explicitly verify/install/repair/remove configured components; already-valid large files are reused. Configure or import API-format ComfyUI workflows separately.

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

It provides drag/drop references, conversational prompting, Auto/manual model selection, operation/quality/resolution/count/seed controls, advanced edit controls, queue progress/cancellation, and an image gallery. It now also includes a local canvas mask editor for inpainting plus a before/after comparison workbench; saved masks are passed to workflows through `mask_path`.

The complete requested image specification is preserved at `docs/IMAGE_MODULE_SPEC.md`.

The image asset library now verifies required model components and API-format workflows, supports explicit install/repair/remove operations, tracks LoRA sidecar metadata, and never silently re-downloads an already-valid large model. Image failures are normalized into user-facing error codes/messages with technical details kept behind a collapsed diagnostic view. LoRA selections are resolved against installed local files, validated for enabled state/version/model-family compatibility and strength, and injected only through explicit workflow template slots; selected subject profiles can automatically contribute references, defaults, preferred model, and assigned LoRAs. Workflow validation catches accidental ComfyUI UI-format exports before a large model is loaded, and the Image Model Manager can import validated API-format workflow JSON directly into the configured model/operation slot. The main chat also renders image jobs inline with live polling and Edit / Variation / Upscale / Save controls.


Image failures are surfaced as friendly structured errors (for example VRAM exhaustion, missing VAE/model, incompatible LoRA, backend offline, invalid workflow, missing node/dependency, disk-full, corrupt checkpoint, or timeout), while raw technical details stay collapsed for debugging.\n\n\n## Shared GPU management

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

Current expected result: **64 tests passing**.

## API highlights

```text
GET  /api/status
GET  /api/models
GET  /api/runtime
GET  /api/tasks
GET  /api/index
GET  /api/research
GET  /api/image
GET  /api/image/history
GET  /api/image/job/<id>
POST /api/chat
POST /api/tasks/resume
POST /api/tasks/undo
POST /api/runtime/start
POST /api/runtime/stop
POST /api/index/rebuild
POST /api/research/plan
POST /api/research/run
POST /api/image/upload
POST /api/image/generate
POST /api/image/cancel
POST /api/image/profile
POST /api/image/consent
POST /api/image/backend/start
POST /api/image/backend/stop
POST /api/image/backend/inspect
POST /api/image/workflows/import
POST /api/image/models/verify
POST /api/image/models/install
POST /api/image/models/remove
POST /api/image/loras/metadata
```

## Development state

See `PROJECT_STATUS.md` for implemented/remaining features and `SESSION_HANDOFF.md` for the exact handoff point for the next development session.
