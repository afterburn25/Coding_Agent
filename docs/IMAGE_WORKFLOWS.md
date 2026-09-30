# ComfyUI API Workflow Management

Local Code Agent executes image workflows through ComfyUI's prompt API. The runnable artifact is therefore an **API-format prompt graph**, not the normal ComfyUI UI/workflow JSON.

## Import a workflow

1. Open or build the desired workflow in ComfyUI.
2. Export the workflow in **API format** (`Workflow → Export (API)` in current ComfyUI builds).
3. Open Local Code Agent → **Image workspace**.
4. Expand the target image model's **Workflows** section.
5. Choose **Import API** beside the exact operation (`text_to_image`, `edit_image`, `inpaint`, and so on).
6. Select the exported JSON file.

The server validates the graph before saving it. A valid prompt graph is a mapping of node IDs to objects that provide at least `class_type` and `inputs`. A normal UI export containing a top-level `nodes` list is rejected with a conversion/export instruction.

## Safety and reliability rules

- The destination filename comes from the configured model profile; the browser cannot choose an arbitrary server path.
- Resolved workflow destinations must stay below `workflows/image/`.
- Imported workflow JSON is limited to 10 MB.
- Saving is atomic. An invalid replacement cannot destroy the previous valid workflow.
- The validator reports node count, class types, template variables, and validation errors.
- Generation validates the workflow again before loading large model components.
- When ComfyUI is online, required node implementations are checked before the prompt is submitted.

## Template variables

Local Code Agent can substitute `${...}` values before execution. Common variables include:

- `${prompt}` / `${negative_prompt}`
- `${seed}`
- `${width}` / `${height}`
- `${steps}` / `${guidance}`
- `${source_image}` / `${mask_path}`
- `${reference_1}`, `${reference_2}`, ...
- component filenames such as `${diffusion_model}`, `${text_encoder}`, `${vae}`
- LoRA values such as `${lora_1_name}` / `${lora_1_strength}`

Unresolved variables cause the job to fail before it is submitted to ComfyUI.

## Current Qwen / FLUX workflow targets

The default profiles reserve operation-specific paths under:

- `workflows/image/qwen/`
- `workflows/image/flux/`

Weights are managed independently under `models/image/`; importing a workflow does not download or overwrite model weights.
