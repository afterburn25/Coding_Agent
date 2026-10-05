# InvokeAI — primary image backend

InvokeAI is Nexus Core's preferred image engine for standard generation and
editing. It runs alongside ComfyUI, which remains the advanced/custom-
workflow engine. Both are managed services; Auto routing picks per job.

## Install

InvokeAI installs into a dedicated virtualenv — `{app}/tools/InvokeAI` — via
the `invokeai` tool manifest (`method: "venv"`), so its pinned torch stack
never contaminates ComfyUI's embedded Python:

- Image workspace → Backends → **Install** (or `/api/image/backend/install`).
- Tools page → InvokeAI → Install (gated by `packages.install`).
- Manual: `python -m venv tools/InvokeAI && tools/InvokeAI/Scripts/pip install invokeai`.

The install job reports progress through the normal job ledger; a disk
check (~6 GB) runs before anything downloads. Models are a separate step —
Nexus never silently multi-GB-downloads checkpoints; import/download them
in InvokeAI's own UI (`http://127.0.0.1:9090`) or drop files into the
InvokeAI models dir.

## Config

```json
"invokeai_endpoint": "http://127.0.0.1:9090",
"invokeai_dir": "",                       // venv root containing Scripts/bin
"invokeai_python": "",                    // interpreter with invokeai installed
"invokeai_auto_start": false,
"invokeai_start_on_image_request": true,  // boot it when a job arrives
"invokeai_extra_args": [],                // extra invokeai-web args
"invokeai_logs_dir": ".agent/runtime",
"invokeai_startup_timeout": 300,
"invokeai_idle_unload_seconds": 900,
"image_backend": "auto"                   // auto | invokeai | comfyui
```

An external InvokeAI on the configured endpoint is used as-is; managed
lifecycle only applies to installs Nexus spawned (marker file → orphan
reclaim → idle eviction).

## What runs on InvokeAI

| Operation | InvokeAI | Notes |
|---|---|---|
| text_to_image | yes | SD1.5/SD2/SDXL checkpoint graphs |
| edit_image (img2img) | yes | `denoise_strength` → `denoising_start` |
| inpaint | yes | `create_denoise_mask` node from the uploaded mask |
| variation | yes | seed fan-out / img2img |
| upscale | yes | when a spandrel upscale model is installed |
| outpaint / canvas | no → ComfyUI | InvokeAI canvas isn't exposed via stable API |
| remove_background | no → ComfyUI | uses ComfyUI's configured custom workflow |
| custom node graphs | no → ComfyUI | imported API workflows stay ComfyUI-only |
| FLUX / SD3 bases | no → ComfyUI | different node plumbing; reported as `operation_unsupported` |

LoRA support: SDXL/main loras from InvokeAI's registry attach as
`lora_loader`/`sdxl_lora_loader` nodes. Reference images upload through
`/api/v1/images/upload` before submission.

## Routing behavior

- Auto prefers InvokeAI whenever it can serve the operation.
- If InvokeAI is missing/offline, Auto falls back to ComfyUI and records
  `auto fallback: preferred backend unavailable` in `routing_reasons`.
- Explicit `backend_override` pins the engine; unavailable or unsupported
  produces an honest error, never a silent swap.
- Never claims fallback/success without a real accepted job.

## Sampler mapping

InvokeAI merges ComfyUI's sampler+scheduler pair into one `scheduler` enum.
The adapter maps canonical names (`euler_ancestral` → `euler_a`,
`dpmpp_2m`+`karras` → `dpmpp_2m_k`, …) and passes InvokeAI-native names
through. Explicit user choices always win; unknown names fall back to
`dpmpp_2m_k`.

## Limitations

- Progress is queue-item status polling (no socket.io dependency); the job
  bar ramps conservatively between polls.
- InvokeAI model metadata (license, VRAM estimates) isn't surfaced by the
  API — those fields stay honest `unknown_capability` / `0` rather than
  invented.
- FLUX/SD3 graph plumbing is not built yet — those models route to ComfyUI.

## License note

InvokeAI itself is Apache-2.0; model weights carry their own licenses —
Nexus records license metadata per model but never implies a license that
wasn't declared.
