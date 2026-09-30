# Image API Workflows

Place ComfyUI **API-format** workflow JSON files here and reference them from an `image_models` profile in `config.json`.

Workflow strings can contain variables such as:

- `${prompt}`
- `${negative_prompt}`
- `${width}`
- `${height}`
- `${count}`
- `${seed}`
- `${model_path}`
- `${source_image}`
- `${mask_path}`

A string that consists only of a token preserves the variable's native type. For example, `${seed}` becomes an integer when the request seed is an integer.

Real model-specific workflow templates are intentionally not guessed. They must match the installed ComfyUI nodes/model loader stack and will be added/tested as the supported local stacks are finalized.
