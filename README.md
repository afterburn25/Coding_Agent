# Chat Nexus

Chat Nexus is a native local-first AI coding workstation for Windows with automatic model switching, transactional coding workflows, web research/browser tools, and a modular local image-generation/editing system.

**GitHub source of truth:** `afterburn25/Coding_Agent`

Future development sessions should begin with `README.md`, `PROJECT_STATUS.md`, `ARCHITECTURE.md`, and `SESSION_HANDOFF.md`.

## Current development version

`0.6.0-dev`

v0.6 keeps the stable coding/research/image workflow and moves Chat Nexus toward self-hosting: the approved chat-first shell is now the primary UI while runtime recovery, streaming, GitHub actions, and dogfood reliability are the next focus.

## Chat Nexus identity and v0.6 UI

The product name is **Chat Nexus**. The GitHub repository remains `afterburn25/Coding_Agent` as the development source of truth.

The canonical v0.6 **native desktop application** is chat-first:
- slim left navigation for Chat / Projects / Models / Research / Images / Tools / Settings
- center conversation workspace with automatic model routing
- right utility rail with **Code Diff / Tasks / Terminal** tabs
- official cyan → blue → violet orbital **CN** emblem
- dark navy UI with restrained neon accents
- local-system details remain available without dominating the chat experience

See `docs/UI_DIRECTION.md` before changing the primary application shell.

## Windows installer and upgrades

The **primary Windows deliverable is now a single compressed installer EXE**:

`Chat-Nexus-Setup-<version>-Windows-x64.exe`

The installer uses a stable application identity so a later installer can detect an existing Chat Nexus installation. On an interactive upgrade it shows the installed version and new version and asks whether to upgrade. The upgrade replaces application/backend/runtime files while preserving mutable local state:

- downloaded `models\`
- `config.json`
- generated `data\`
- the existing self-development `Source\.git` workspace and local edits/task state

The installer is per-user under Local AppData, creates Start Menu shortcuts, optionally creates a desktop shortcut, uses LZMA2 solid compression, and extracts the application during setup. CI performs a real fresh install, launches the installed app self-test, writes preservation markers, runs the **same installer a second time without an install path override**, verifies the stable AppId rediscovers the previous install, and confirms all mutable state survives the upgrade.

The portable ZIP remains a secondary development/recovery artifact.

## Native Windows desktop application

The normal Windows deliverable is **`ChatNexus.exe`**, not a browser launcher. `ChatNexus.exe` is a self-contained **.NET 8 WinForms** application using **Microsoft WebView2**. It starts `backend/ChatNexus.Backend.exe` as a hidden child process and connects the desktop UI to that backend over loopback only. The HTTP service is private implementation plumbing, not the user-facing product.

The portable Windows dogfood bundle includes:
- `ChatNexus.exe` with the official Chat Nexus icon
- embedded Chat / Image Studio / Research Hub UI
- pinned llama.cpp Vulkan x64 runtime under `runtime/llama`
- a real `Source` working copy of `afterburn25/Coding_Agent` for self-development
- config/example and build metadata

Normal users launch `ChatNexus.exe`. The hidden Python backend remains available as `backend/ChatNexus.Backend.exe --server` only for low-level development/debugging. The earlier pywebview/pythonnet desktop host was removed completely after a frozen CLR-loading failure.

### Default coding models

The intended two-model coding setup is:

- **Qwen3 14B Q4_K_M** — utility, fast coder, and normal primary coding.
- **Qwen3-Coder 30B-A3B Instruct Q4_K_M** — deep reasoning and reviewer work.

The 30B model is expected to use llama.cpp CPU/GPU offload on a 12 GB GPU. If the 30B GGUF is not installed or cannot run, deep tasks can fall back to the runnable 14B primary coder instead of failing the task.

The Windows package bundles llama.cpp itself; large GGUF model files remain explicit downloads from the Coding readiness/model catalog so the application package does not silently ship or download tens of gigabytes.

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
- Interrupted tasks are normalized to a recoverable state after restart; pending approvals can also resume cold from durable task metadata.

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


Image failures are surfaced as friendly structured errors (for example VRAM exhaustion, missing VAE/model, incompatible LoRA, backend offline, invalid workflow, missing node/dependency, disk-full, corrupt checkpoint, or timeout), while raw technical details stay collapsed for debugging.

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

## CLI

After installation, start Chat Nexus with:

```bash
chat-nexus
```

The legacy `local-code-agent` command remains available for backward compatibility.

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

Current expected result: **110 tests passing**.

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
POST /api/tasks/recover
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

## Live agent streaming

The main Chat Nexus chat now uses `POST /api/chat/stream` with Server-Sent Events. OpenAI-compatible local runtimes stream assistant content immediately while Chat Nexus reconstructs streamed function/tool calls for the normal agent loop. Live events include:

- assistant token deltas
- selected/switched model events
- tool calls and bounded output
- task/phase changes
- research preflight state
- approval state

Endpoints that ignore `stream:true` and return ordinary OpenAI-compatible JSON are handled transparently. The non-streaming `POST /api/chat` endpoint remains available for compatibility.

## Self-development mode

When the selected workspace is the Chat Nexus source tree, new and recovered agent tasks automatically receive a self-hosting context. It directs the agent to read the repository handoff/docs first, preserve working components, keep the active instance usable, avoid Git/GitHub delivery actions unless requested/approved, and require the isolated second-instance selftest before reporting a self-change complete.

When coding readiness is available, the Local system drawer also exposes **Start self-development task**. It only pre-fills a safe self-development prompt; the user still sends it explicitly.

## llama.cpp runtime bootstrap

Chat Nexus recognizes both the traditional `llama-server` executable and the newer unified `llama serve` command.

When no managed llama.cpp runtime is found, Coding readiness shows platform-appropriate install commands that can be copied. Chat Nexus **does not execute package-manager installers automatically**. Current guidance includes Winget on Windows and supported Conda/Homebrew options on other platforms.

After installation, Refresh re-runs discovery. Download/install a catalog GGUF, click **Use discovered models**, restart Chat Nexus, and the managed runtime can auto-launch that model when the router needs it.

## Explicit coding-model catalog

The Local system drawer now contains a small auditable coding-model catalog. Downloads are never automatic.

Initial entries:

- **Qwen3 14B Q4_K_M** — official Qwen GGUF, 9.0 GB, Apache-2.0; starter roles for utility/fast/general coding.
- **Qwen3-Coder 30B-A3B Instruct Q4_K_M** — community GGUF quantization of the Apache-2.0 Qwen base, 18.56 GB; deep-reasoner/reviewer roles and expected CPU/GPU offload on 12 GB VRAM.

Each catalog entry records a direct source URL, exact byte size and SHA256. Downloads use a temporary `.part` file, stream progress, support cancellation, verify exact size + SHA256, and atomically install only after verification. An existing untrusted file is never silently replaced; **Repair** must be selected explicitly.

After installation, **Use discovered models** turns the verified local GGUF inventory into role profiles.

## First-run coding model setup

A fresh installed Chat Nexus no longer leaves users at raw missing-GGUF paths. **Coding readiness** now exposes direct setup actions:

- **Install recommended 14B** — downloads and checksum-verifies Qwen3 14B for utility/fast/primary coding, then writes the discovered model routing config.
- **Install full 14B + 30B stack** — installs both Qwen3 14B and Qwen3-Coder 30B-A3B, verifies them, and configures 14B for everyday coding plus 30B for deep reasoning/review.
- If 14B is installed first, **Add 30B deep coder** remains visible until the full stack is available.

Install plans reuse an already-running download job for the same model instead of starting duplicate multi-gigabyte transfers. After model configuration changes, Chat Nexus asks for one restart so the backend/router reload the new profiles.

Missing-model readiness diagnostics are deduplicated: each unavailable GGUF is reported once instead of as two near-identical runtime errors.

## Coding-model readiness and local GGUF setup

`GET /api/readiness` reports whether Chat Nexus can actually perform coding work, rather than merely whether the web app is running. It checks:

- local endpoint health
- `llama-server` discovery
- configured GGUF paths
- local GGUF inventory
- estimated RAM/VRAM fit and CPU-offload allowance
- coding-role coverage
- whether the current workspace is the Chat Nexus source tree with Git + isolated selftest available

The main UI distinguishes **Setup required**, **Ready to code**, and **Self-host ready**.

If GGUF files already exist locally, Chat Nexus proposes conservative role assignments. Clicking **Use discovered models** explicitly writes those model profiles into the selected config and preserves unrelated settings; a restart is required. No model is downloaded or replaced silently.

## Isolated self-update validation

When the selected workspace is the Chat Nexus source tree, automatic verification upgrades from a plain unit-test command to:

```bash
python -m localcodeagent.selftest --workspace . --json
```

That validator:

1. runs the full unit suite
2. creates a resource-safe temporary configuration
3. launches a **second Chat Nexus process** from the edited working tree on a free loopback port
4. probes `/api/status`, the main Chat UI, Image Studio, and Research Hub
5. terminates the second process
6. fails verification if any stage is unhealthy

The currently running Chat Nexus instance is never replaced during this validation.

## Native GitHub delivery tools

Chat Nexus can now use the current workspace's GitHub remote as part of an agent workflow:

- inspect current branch and repository metadata
- create/switch local feature branches
- create commits from **explicit file paths** only
- push branches
- list issues / pull requests
- create issues
- open pull requests
- read recent GitHub Actions status

Remote writes use the `github.write` permission, which defaults to **Ask**. GitHub REST writes read credentials from the configured environment variable (default `GITHUB_TOKEN`); the token is never written into project configuration. Agent-created Git commits refuse to stage `.agent` metadata.

## Continuous verification

GitHub Actions now runs the unit suite on every push and pull request. The current main-branch checkpoint is **110 passing tests**.

## Development state

See `PROJECT_STATUS.md` for implemented/remaining features and `SESSION_HANDOFF.md` for the exact handoff point for the next development session.
