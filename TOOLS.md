# Tools & Plugins

Chat Nexus exposes capabilities through a central **Tool Registry**. The agent
queries the registry instead of hard-coding tool knowledge, so tools can be
installed, removed, enabled, disabled, and replaced independently.

## Registry anatomy

```text
Agent Orchestrator
  ↓
Tool Registry  (localcodeagent/tools/base.py)
  ├─ built-in tools (filesystem, shell, terminal, search, build,
  │                  git, github, research, web, image)
  ├─ manifest tools  (tools/manifests/*.json)
  └─ MCP tools       (planned; same registry surface)
  ↓
PermissionManager  (localcodeagent/permissions.py)
```

## Built-in tool families

- **Terminal** (`localcodeagent/tools/terminal.py`): `terminal_run` executes
  commands through a chosen shell (powershell/pwsh/cmd/bash/sh/auto) with
  env vars, workspace-bounded cwd, timeout, and optional background mode.
  Background processes are tracked by `TerminalTracker`, surfaced as Job
  Manager jobs, listed by `terminal_processes`, and killed by
  `terminal_kill`. All gated by `shell.execute`.
- **Search** (`localcodeagent/tools/search.py`): `search_code`,
  `search_filename`, `search_error` use ripgrep when on PATH with a Python
  fallback scanner; results are structured JSON `{path, line, text}`.
- **Build** (`localcodeagent/tools/buildsys.py`): `detect_build_system`,
  `build_project`, `configure_project`, `run_tests`, `clean_project` detect
  CMake/Meson/Cargo/.NET/MSBuild/npm/pnpm/yarn/Gradle/Maven/Make/Python and
  run the right command through the permission gate.
- **Media** (`localcodeagent/tools/media.py`): high-level FFmpeg wrappers —
  `media_probe` (ffprobe JSON), `extract_audio`, `trim_video`,
  `convert_video`, `create_thumbnail`, `normalize_audio`, `add_subtitles`
  (mux or burn). All build validated argv behind `shell.execute` and report
  clean installable errors when ffmpeg is missing. `media_transcribe` is a
  Media Agent recipe that chains `extract_audio` → `whisper` through the
  registry as a tracked Job Manager job.
- **Data** (`localcodeagent/tools/data.py`): `data_query`,
  `profile_dataset`, `chart_generate` — DuckDB-preferred engine with
  SQLite/CSV fallback and dependency-free SVG charts under
  `.agent/data/charts/`.
- **Documents** (`localcodeagent/tools/documents.py`): `extract_text`
  (txt/md/html/csv/json/pdf), `ocr_image` (delegates to the Tesseract
  manifest), `convert_document` (delegates to Pandoc).
- Existing filesystem, shell (`run_shell`), git, github, research, web, and
  image families are unchanged.

Every registered tool has a manifest:

| field | meaning |
| --- | --- |
| `id` | stable tool id (defaults to function name) |
| `display_name` | UI label |
| `category` | ai_models / coding / browsers / research / images / audio / video / documents / data / devops / git / 3d / utilities / external_apis / mcp |
| `version` | provider/manifest version |
| `provider` | e.g. `chat-nexus`, `comfyui`, `playwright`, `github` |
| `capabilities` | semantic tags used for capability lookup |
| `permissions_required` | permission keys the tool needs |
| `requires_network` / `requires_gpu` | resource gates |
| `requirements` | `{ram_mb, vram_mb, ...}` estimates |
| `supported_os` | empty = all |
| `install_status` | installed / missing / not_applicable |
| `source` | builtin / manifest / mcp |
| `enabled` | runtime toggle, persisted in `data/tools_state.json` |
| `callable` | false when a manifest has no invoker (catalog entry only) |

## Workflows

`workflows/*.json` (config `workflows_dir`) define declarative multi-step
pipelines: each step calls a registered tool by name, so permission gates,
install checks, and Job Manager tracking apply per step. Steps can pass data
via `{params.name}` and `{steps.save_as.field}` placeholders (parsed from JSON
step output). `list_workflows` / `run_workflow` expose them to the agent —
e.g. the bundled `clip_transcribe` chains thumbnail → audio extract →
whisper transcription, and `ask_workspace` indexes then searches local docs.

Workflow jobs are `cancellable`: `POST /api/jobs/cancel` (or the Cancel
button in the Tools UI) stops the pipeline at the next step boundary —
in-flight steps finish, remaining steps are skipped and the job lands in
`cancelled` state.

Failed or cancelled runs write a resume checkpoint to
`.agent/workflow_runs/<id>.resume.json` (merged params, completed-step
outputs, and the step index to re-enter at). Pass `{"resume": true}` to
`run_workflow` to continue from the failed step — caller params override
the saved ones, and the checkpoint is cleared on success.

## Tool interfaces

`localcodeagent/tools/interfaces.py` defines structural (duck-typed) contracts:
`ITool`, `IExecutableTool`, `IModelBackend`, `IImageBackend`,
`IBrowserBackend`, `IResearchProvider`, `IMediaTool`, `IDataTool`,
`IDocumentTool`, `ISandboxProvider`, `IVersionControlProvider`. Providers do
not need to inherit; the interfaces document the expected surface.

## Plugin manifests

Drop JSON files into `tools/manifests/` (configurable via
`tool_manifests_dir`). Format:

```json
{
  "id": "ffmpeg",
  "name": "FFmpeg",
  "version": "1.0.0",
  "category": "video",
  "provider": "ffmpeg-project",
  "executables": ["ffmpeg", "ffprobe"],
  "capabilities": ["convert_video", "trim_video"],
  "permissions": ["shell.execute"],
  "requires_network": false,
  "requires_gpu": false,
  "requirements": {"ram_mb": 512},
  "supported_os": ["windows"],
  "docs": "https://ffmpeg.org/documentation.html",
  "install": {"method": "winget", "package": "Gyan.FFmpeg"},
  "health_check": {"command": ["ffmpeg", "-version"]},
  "invoke": {
    "command": ["ffmpeg", "-hide_banner", "{args}"],
    "input_schema": {"type": "object", "properties": {"args": {"type": "array"}}},
    "timeout_seconds": 300
  }
}
```

- `executables` — names on PATH or absolute paths; missing files mark the tool
  `install_status: "missing"`.
- `health_check.command` — run with a 15 s timeout; exit 0 = healthy.
- `invoke.command` — argv template; `{name}` placeholders are substituted from
  the call arguments (a list value splices into the argv). Invocations run in
  the workspace with a timeout and return `exit_code`, `stdout`, `stderr`.
- A manifest without `invoke` is a **catalog entry**: visible and manageable in
  the Tool Manager, hidden from model tool schemas.
- Manifest tools default to the `shell.execute` permission unless they declare
  their own key in `permissions`.

## API

```text
GET  /api/tools                    full registry manifest + plugin load report
GET  /api/tools/health/<id>        run a tool's health check
GET  /api/tools/route/<capability> ranked routing candidates + exclusion reasons
GET  /api/tools/telemetry          recent routing decisions
POST /api/tools/state              {"tool": "read_file", "enabled": false}
GET  /api/permissions              profile + levels + session grants
POST /api/permissions/level        {"permission": "shell.execute", "level": "session"}
POST /api/permissions/profile      {"profile": "offline"}
GET  /api/processes                managed services (llama.cpp, ComfyUI, ...)
POST /api/processes/action         {"id": "comfyui", "action": "restart"}
GET  /api/jobs                     unified job queue
POST /api/jobs/cancel              {"job_id": "image-..."} (kind-aware)
GET  /api/queue                    durable prompt work queue (FIFO, capped at 200 items)
POST /api/queue                    {"prompt": "...", "mode": "auto"} — 429 when the queue is full
POST /api/queue/cancel             {"id": "<queue item id>"}
GET  /api/task-log?task_id=<id>    per-task terminal transcript tail
GET  /api/resources                hardware, residency, image resource mode
```

## Tool Router

`localcodeagent/tool_router.py` selects tools by capability instead of name.
`explain(capability)` returns ranked candidates and exclusion reasons
(disabled, not_callable, not_installed, permission_denied, offline, no_gpu,
insufficient_vram/ram, unsupported_os); `execute(capability, args)` walks the
ranked list with automatic fallback and records routing telemetry (chosen
tool, attempts, elapsed). Ranking uses `preferred_tools` config, permission
strength, and cached health checks. The registry remains the execution gate —
the router only picks, it never bypasses permissions.

## Job Manager

`localcodeagent/jobs.py` is the central asynchronous job ledger. It normalizes
agent tasks, image jobs, and model/image installs into one state vocabulary:
`queued`, `preparing`, `running`, `waiting_for_tool`,
`waiting_for_permission`, `completed`, `failed`, `cancelled`. Generic tool
providers can submit their own job records; persisted jobs that were active at
restart are marked failed on load.

## Process Manager

`localcodeagent/processes.py` registers controllable services (llama.cpp model
runtimes, ComfyUI, future MCP servers). Each service supplies live describe +
optional start/stop/restart delegates owned by the launching subsystem — the
manager lists status/PID/uptime/port and routes control actions without
knowing process internals.

## UI

`web/tools.html` — permission profile switcher (incl. the autonomous-mode
toggle), per-permission levels, category-filtered registry with
enable/disable + health checks, process controls, the unified job list, and
the Work queue card (enqueue form, list, per-item cancel).

The agent can also enqueue follow-up work itself via the `queue_task`,
`queue_list`, and `queue_cancel` tools (`localcodeagent/tools/queue.py`,
permission key `tasks.queue`).
