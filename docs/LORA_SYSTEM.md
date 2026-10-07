# Local LoRA System

LoRAs live under `models/image/loras/`. Nexus Core discovers supported weight files and optional JSON sidecars.

## Sidecar metadata

A LoRA can have a sibling `<weight-file>.json` containing fields such as:

```json
{
  "id": "alice-v2",
  "name": "Alice",
  "version": "2",
  "enabled": true,
  "strength": 0.8,
  "compatible_families": ["qwen-image-2.1"],
  "tags": ["character"]
}
```

Selections are resolved only against files actually present in the local LoRA library. Disabled LoRAs, incompatible declared families, unavailable requested versions, duplicate selections, excessive model-specific counts, and strengths outside -4..4 fail before image model loading.

## Workflow slots

A workflow that accepts selected LoRAs must expose template slots. For the first LoRA use:

- `${lora_1_name}`
- `${lora_1_strength}`

For multiple LoRAs, add numbered slots (`lora_2_name`, `lora_2_strength`, and so on). If a user selects more LoRAs than the imported workflow exposes, the job fails during workflow validation rather than silently ignoring the selection.

The substituted LoRA name is the path relative to `models/image/loras`, which matches the directory exported to ComfyUI through Nexus Core's extra-model-path configuration.

## Subject profiles

A subject profile may include `loras`, `reference_images`, `face_reference`, `body_reference`, `preferred_model`, and `generation_defaults`. Selecting that subject automatically merges those settings into the image request before model routing. Explicit request settings continue to take precedence over profile defaults.

## Subject profile compatibility

Subject profiles may use the current `loras` list or the simpler single-assignment fields `assigned_lora`, `lora_version`, and `lora_strength`. The image manager normalizes both forms into the same request-level LoRA selection list before routing and validation.

## Workflow slot contract

Every selected LoRA requires both `${lora_N_name}` and `${lora_N_strength}` placeholders in the imported ComfyUI API workflow. This prevents a workflow from silently ignoring a user-selected strength or applying an implementation-defined default.
