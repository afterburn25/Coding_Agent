# Nexus Image System

One unified image system with a backend router — never two parallel
pipelines.

```
Nexus Image System
    ↓
Image Backend Router  (operation + capability + availability aware)
    ├── InvokeAI  — preferred primary backend for standard generation/editing
    └── ComfyUI   — advanced/custom workflows, specialized nodes, fallback
```

## Architecture

- **`image/backend.py`** — the `ImageBackend` contract: `health`, `inspect`,
  `submit`, `status`, `fetch_outputs`, `cancel`, plus `capabilities()` and
  `models()`. `submit()` takes a backend-native payload (a rendered ComfyUI
  API workflow, or a normalized spec dict InvokeAI translates to a queue
  graph).
- **`image/comfyui.py`** — `ComfyUIBackend` (port 8188, `/prompt`,
  `/history`, `/ws` progress).
- **`image/invokeai.py`** — `InvokeAIBackend` (port 9090): health via
  `/api/v1/app/version`, model discovery via `/api/v2/models/` (v1
  fallback), uploads via `/api/v1/images/upload` (multipart file +
  query params), jobs via `POST /api/v1/queue/default/enqueue_batch`
  with a nested `{"batch": {graph, runs}}` body, per-item polling at
  `/i/{item_id}`, cancellation via `PUT /i/{item_id}/cancel`, downloads
  via `/api/v1/images/i/{name}/full`. Variations submit one batch per
  seed — `runs` on a single batch would reuse the same seed.
- **`image/runtime.py` / `invokeai_runtime.py`** — managed process lifecycle
  (discover, start, stop, restart, orphan reclaim, idle eviction) for each
  engine. Both register as `ManagedService` rows in the Process Manager.
- **`image/router.py`** — `ImageRouter.choose(request, backend=...)` filters
  the model pool per engine; `ImageManager._select_backend` implements the
  Auto preference (InvokeAI → ComfyUI → honest failure).
- **`image/manager.py`** — one job ledger for both engines; `ImageJob.backend`
  records which engine ran. Safety policy, resource scheduling, artifact
  history and persistence are shared.
- **`image/catalog.py`** — `classify_model()` assigns `capability_class`
  (general/photoreal/illustration/inpainting/upscale/background_removal/lora)
  and `restriction_status` (adult_capable, restricted_by_model,
  restricted_by_provider, local_unfiltered_model, unknown_capability).
- **`image/sampling.py`** — `SamplingAdvisor` keyed by `backend|family|op`
  for InvokeAI; ComfyUI keeps the legacy `family|op` key so learned stats
  carry forward.
- **`image/errors.py`** — normalized error codes shared by both engines
  (`backend_not_installed`, `backend_offline`, `model_missing`,
  `model_load_failed`, `operation_unsupported`, `safety_rejected_by_model`,
  `out_of_memory`, `timeout`, …).

## Backend routing

`image_backend` config (or per-request `backend_override`) selects:

- **`auto`** (default, recommended) — InvokeAI when it advertises the
  operation and is healthy/installed; ComfyUI for custom-workflow ops
  (outpaint, background removal, imported node graphs, transparency) or as
  fallback when InvokeAI is unavailable. Every routing decision records its
  reasons on the job.
- **`invokeai` / `comfyui`** — hard pin; a missing engine errors honestly
  instead of silently rerouting.

InvokeAI supports `text_to_image`, `edit_image`/img2img, `inpaint`,
`variation`, and `upscale` (spandrel models). Ops it can't express —
outpaint/canvas edits, custom node graphs, background removal workflows —
route to ComfyUI.

## Models

- ComfyUI models are the configured `image_models` profiles (weights +
  API workflows + components verified by the asset library).
- InvokeAI models are discovered live from `/api/v2/models/` and merged
  into the router pool as `invokeai:<key>` profiles carrying the
  ModelIdentifier needed to build graph nodes. They dedupe on refresh and
  get the same capability-classification metadata.
- LoRA name→model resolution for InvokeAI goes through its own registry;
  the shared `list_loras` library path remains ComfyUI's.

### Photoreal fleet

`image/fleet.py` declares the managed photoreal fleet — curated SDXL
checkpoints with verified Hugging Face sources, real sizes, SHA-256
digests, per-model licenses, and trait weights:

- **Juggernaut XL v9** (`general_photoreal`) — full scenes, people in
  environments, architecture with people, outdoor realism; the general
  photoreal fallback.
- **CyberRealistic XL** (`portrait_photoreal`) — portraits, close-ups,
  headshots, beauty/editorial, identity-focused work.
- **RealVisXL V5.0** (`glamour_photoreal`) — glamour/boudoir-style
  adult-only imagery, skin detail, body-focused studio photography.
  `adult_capable`; adult routing only fires on clearly-adult requests —
  ambiguous-age prompts never land on it.

Fleet specs match installed InvokeAI models by name/source/path markers;
matched profiles get `fleet_id`, `fleet_role`, license, and per-model
`sampling` defaults in `metadata` (`steps` 30, `guidance` 5.0,
`dpmpp_2m_sde`/`karras` for all three).

**Routing**: `classify_request_traits()` maps prompts to traits
(portrait/face/scene/environment/glamour/adult/…). `ImageRouter` scores
fleet-tagged pool members (`score_fleet_model`) before generic priority
sorting; the winner's `fleet_id` and traits land in `routing_reasons`.
Fallback honors `resource_fit` — a winner that can't run skips to the
next scored model. No photoreal signal → generic routing unchanged.
Manual override accepts either the `invokeai:<key>` id or the fleet id
(e.g. `realvisxl-v5`); a fleet model pinned against the wrong backend
errors honestly.

**Learning**: `SamplingAdvisor` keys stats per backend **and** per
`model_scope` (fleet id), so thumbs feedback on Juggernaut never drifts
CyberRealistic. Model-declared sampling defaults sit below learned
outcomes and explicit request values.

**Installs**: the provisioning manager drives `/api/v2/models/install`
with each spec's `invokeai_source` (`repo::file`), polls the install job
for byte-level progress, and verifies by re-enumerating models — never by
download completion alone.

## Safety

Unchanged. `ImageSafetyPolicy` gates every request before any backend call:
minors and ambiguous-age explicit content are blocked, non-consensual
intimate imagery is blocked, explicit real-person edits require active
consent, and the creator-locked Brain flag gates explicit generation.
`adult_capable`/`local_unfiltered_model` are descriptive metadata only.
Model-level safety filters (e.g. provider-restricted checkpoints) surface
as `safety_rejected_by_model` — Nexus never retries past them.

## Truth rules

- "Generating" is only reported after a backend accepted a real job.
- "Completed" only after output files exist and the job is `finished`.
- Auto fallback is recorded in `routing_reasons`; a fallback claim without
  a real backend job is a bug.

## Endpoints

- `GET /api/image` — summary incl. `backends.invokeai`/`backends.comfyui`
  health, capabilities, and live InvokeAI model rows.
- `POST /api/image/generate` — `backend_override` accepted.
- `POST /api/image/backend/start|stop|inspect` — `{"backend": "invokeai"|"comfyui"}`.
- `POST /api/image/backend/install` — permission-gated install through the
  tool manifest (`invokeai` → dedicated venv; `comfyui` → archive).
- `POST /api/image/backend/preference` — persist `image_backend`.
- All existing job/history/cancel/output endpoints unchanged.

## Resource management

Both engines register in the Process Manager (`invokeai`, `comfyui`) and
idle-evict after `invokeai_idle_unload_seconds` /
`comfyui_idle_unload_seconds` (managed processes only). Before a large job
the manager releases resident LLMs under `image_resource_mode` and restores
them when `image_restore_chat_model` is set — the same flow for both
backends.
