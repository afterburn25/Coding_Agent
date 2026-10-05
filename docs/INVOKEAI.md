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

## Verified dogfood (InvokeAI 6.14.2, real server, RTX 3080 Ti)

Performed against a live `invokeai-web` on this machine (`tools/InvokeAI`
venv, torch 2.14.1+cu126 — pip defaults to CPU torch on Windows, upgrade
to the `+cu126` wheel for GPU):

| Operation | Result |
|---|---|
| Health/version probe | `InvokeAI 6.14.2` healthy |
| Model install (`Lykon/dreamshaper-8`, 5.5GB) | completed via `/api/v2/models/install` |
| Text-to-image (`cinematic futuristic city at night`) | finished — 358KB PNG, ~2s |
| img2img edit (change lighting/background, denoise 0.6) | finished — real output |
| Inpaint (bounded mask region) | finished — `create_denoise_mask` needs `image`+`mask` fields |
| Variation (count=3) | finished — **3 distinct outputs**; submit fans out per-seed batches because `runs` reuses one seed |
| Cancel mid-flight | items transition to `canceled` |
| Auto routing | chose InvokeAI; Nexus job persisted output + mirrored copy |
| Explicit pin + dead endpoint | honest `not installed or running` error — no silent swap |

Not dogfooded: GPU/LLM-resident resource contention (no LLM was loaded on
this dev box) and live ComfyUI fallback (ComfyUI not installed here — the
fallback path is unit-tested in `tests/test_invokeai.py`).

**Closed 2026-10-05 (live, same box):** both gaps are now verified on real
processes. With `llama-server` resident (~11 GB VRAM) and a managed
InvokeAI + managed ComfyUI all running, a ComfyUI Qwen job released the
coding LLM (llama-server count 0 during the job), parked the managed
InvokeAI (`resource arbitration: parked Nexus-managed InvokeAI` recorded
in `job.routing_reasons`), generated real output, and restored the LLM
(`llama-server` back to 1 after completion). Mid-job InvokeAI death was
also observed live: the job failed honestly once, and the new bounded
restart+resubmit path now covers `BackendConnectionError` crashes on both
engines. External (user-owned) servers are never evicted —
`evict_if_managed()` only touches Nexus-owned processes.

## Managed model installs (fleet)

The adapter wraps InvokeAI's model-manager install API (v6 verified):

- `POST /api/v2/models/install?source=<src>` — accepts a HF repo id, a
  `repo::file.safetensors` pin, a URL, or a local path. Returns a
  `ModelInstallJob` (`id`, `status`, `bytes`/`total_bytes`).
- `GET /api/v2/models/install` / `/{id}` — job list / single job polling.
- `DELETE /api/v2/models/install/{id}` — cancel.

`ProvisioningManager` uses these for the photoreal fleet: submit → poll
bytes for progress → on `completed`, verify the model is enumerable via
`GET /api/v2/models/` before marking the item done. InvokeAI itself
resumes interrupted downloads (huggingface_hub caching) and hash-checks
upstream metadata, so a 7 GB checkpoint never restarts from zero after a
restart.

Fleet install dogfood (2026-10-05, live 6.14.2): all three fleet sources
downloaded complete (real `total_bytes` matching the verified spec
sizes). **Correction found by dogfooding:** the `repo::file` pin form
downloads into a folder that InvokeAI's model identifier cannot classify
— models registered as `type: unknown` named `tmpinstall_*` and are
unusable. Single-file checkpoints must use the direct
`https://huggingface.co/<repo>/resolve/main/<file>` URL form, which
registers as a proper `main` checkpoint. The fleet specs carry the URL
form; `repo::file` remains valid only for multi-file diffusers repos.

All three fleet models registered `main`/`sdxl` and a real Juggernaut
generation completed through `InvokeAIBackend.submit` (1024², 20
steps, ~32 s on the 3080 Ti).

### Upstream bug: spandrel probe segfault on large checkpoints

InvokeAI's model classifier runs *every* candidate config class during
install, including `Spandrel_Checkpoint_Config`, which fully loads the
state dict via `safetensors.torch.load_file`. On this box (torch
2.14.1+cu126, Windows) that segfaults — access violation, no traceback —
killing `invokeai-web` mid-install for multi-GB files. Observed on all
three ~7 GB fleet checkpoints; repro standalone:

```text
ModelConfigFactory.from_model_on_disk(<7GB safetensors>)
→ spandrel.py:_validate_spandrel_loads_model → access violation
```

Local workaround applied to `tools/InvokeAI/Lib/site-packages/
invokeai/backend/model_manager/configs/spandrel.py` (marked
`NEXUS PATCH`): `_validate_spandrel_loads_model` raises `NotAMatchError`
for files >2 GiB — spandrel image-to-image nets are far smaller. With
the patch all three fleet installs register cleanly. **Caveat**: the
patch lives in the installed venv, so an InvokeAI reinstall or upgrade
removes it and large-checkpoint installs will crash the server again
until re-applied or fixed upstream. Serialise model installs — two
concurrent hash+probe cycles also OOMed this box mid-install.
