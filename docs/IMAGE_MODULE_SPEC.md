Add a complete **local image-generation and image-editing module** to the existing local AI/coding-agent project.

The module should feel like ChatGPT image generation from the user’s point of view: the user describes what they want conversationally, the agent determines the required image workflow automatically, selects the appropriate installed model/tools, performs the generation or edit, and returns the finished image inside the chat UI.

## Core goals

Build the image system as a modular tool that can later support additional image models without redesigning the application.

The system must run locally whenever possible and should not depend on a hosted image-generation API.

Primary hardware target:

- NVIDIA RTX 3080
- 12 GB VRAM
- 64 GB system RAM
- Windows and Linux support where practical

Use GPU/CPU offloading, quantization, tiled processing, and memory-efficient attention where useful.

## Primary image models

Add support for:

### Juggernaut X v10 (default text-to-image)

RunDiffusion Juggernaut-X-v10 (full SDXL checkpoint, pinned revision
`e53841ec`) is the default general/photorealistic text-to-image model:

- normal and photorealistic text-to-image
- adult-only synthetic generation when the creator-locked Nexus Brain
  adult-content setting and `ImageSafetyPolicy` permit it

Weights land under `models/image/stable-diffusion/checkpoints/` and are
exposed to ComfyUI through the generated `extra_model_paths` file. The
bundled workflow is `workflows/image/sdxl/juggernaut-x-v10-t2i-api.json`
(standard `CheckpointLoaderSimple` + `CLIPTextEncode` + `EmptyLatentImage`
+ `KSampler` + `VAEDecode` + `SaveImage`). The default negative prompt
targets quality defects only — it does not inject censorship terms into
requests that already passed the image safety policy.

### Qwen-Image-2.1

Use Qwen-Image-2.1 as the preferred quality model for:

- image-to-image
- conversational image editing
- subject-preserving edits
- background replacement
- clothing changes
- inpainting
- outpainting
- multiple reference images
- consistent-character generation
- transparent-background images where supported

(Qwen can also produce photorealistic output, but default/photorealistic
text-to-image routes to Juggernaut X v10.)

Support GGUF/quantized versions suitable for 12 GB VRAM.

### FLUX.2 Klein 4B

Use FLUX.2 Klein 4B as a fast secondary model for:

- rapid previews
- fast/draft text-to-image (it always wins `preview`/`fast`/`draft`
  quality requests regardless of global model priority)
- lightweight edits
- draft generations
- workflows where speed matters more than maximum quality

Design the backend so additional Stable Diffusion, FLUX, Qwen, or future image models can be added later as plugins.

## Automatic image model routing

Integrate image generation with the agent’s existing automatic model router.

The agent should inspect the request and automatically determine:

- text-to-image vs image editing
- speed vs maximum quality
- amount of available VRAM
- desired resolution
- number of reference images
- identity consistency requirements
- whether masks are required
- whether inpainting/outpainting is required
- whether an upscaler is needed
- whether background removal is required

Examples:

Fast preview request:
→ FLUX.2 Klein 4B

High-quality photorealistic request:
→ Qwen-Image-2.1

Edit uploaded photograph:
→ Qwen-Image-2.1 image-editing workflow

Background replacement:
→ segmentation/background-removal tool
→ Qwen edit/inpainting
→ optional upscaler

Large final render:
→ generate
→ tiled upscale
→ face/detail refinement if requested

Always allow a manual model override.

## Adult-content support

This is a locally controlled image system and should support lawful **consensual adult-only imagery**, including:

- adult boudoir
- lingerie
- artistic nudity
- explicit consensual adult imagery
- synthetic adult characters
- consensual adult couples
- adult creator photography
- editing photographs supplied by a consenting adult
- adult character LoRAs

Do not add arbitrary blanket NSFW blocking that prevents lawful consensual adult usage.

However, implement strict safeguards against:

- minors or characters represented as minors in sexual contexts
- ambiguous-age sexual subjects
- non-consensual intimate imagery
- sexual deepfakes of real people without documented consent
- coercive sexual imagery
- secretly generated intimate imagery of identifiable real people

For real-person explicit editing, create a configurable **consent record** associated with the subject/profile.

A consent record should support:

- subject name or internal alias
- confirmation that the subject is 18+
- confirmation of consent for image generation/editing
- date consent was recorded
- optional consent scope
- optional reference/source notes
- revocation status

Do not require these records for fully synthetic adults.

## Image workspace

Add an Image workspace/page to the UI.

It should contain:

- conversation/prompt field
- drag-and-drop image upload
- reference-image area
- generated-image gallery
- before/after viewer
- model selector
- Auto model option
- resolution selector
- aspect-ratio selector
- generation count
- seed control
- random seed button
- quality selector
- guidance/steps advanced controls
- negative prompt field
- LoRA selector
- LoRA strength
- image strength
- denoise strength
- mask editor
- inpainting controls
- outpainting controls
- background-removal control
- upscale control
- face/detail refinement control
- save/export controls

Keep advanced settings collapsed by default so normal users can simply type requests conversationally.

## Conversational editing

Allow workflows such as:

“Remove the background and put this person in a luxury hotel suite.”

“Keep her face, body proportions, pose and hairstyle the same, but change the outfit.”

“Create five additional images of the same character in different locations.”

“Remove the person behind me.”

“Extend this image to 16:9.”

“Make this look like professional studio photography.”

The agent should translate normal language into the underlying workflow automatically.

## Identity and reference consistency

Create a reusable **Subject Profile / Character Profile** system.

A profile can contain:

- display name
- reference images
- face reference
- body reference
- appearance description
- preferred model
- assigned LoRA
- LoRA version
- generation defaults
- consent record when applicable
- saved prompts/styles

Allow several reference photographs to be attached to one profile.

The system should automatically use those references when the profile is selected.

## LoRA support

Support:

- loading LoRAs
- enabling multiple LoRAs
- per-LoRA strength
- assigning LoRAs to characters
- versioning LoRAs
- enabling/disabling LoRAs
- metadata
- preview image
- model compatibility
- searching/filtering the LoRA library

Architect this so LoRA training can be added later.

## ComfyUI integration

Use ComfyUI as a supported backend where practical.

Create an abstraction layer so the main application does not depend directly on ComfyUI internals.

Example architecture:

ImageAgent
    ↓
ImageRouter
    ↓
WorkflowManager
    ↓
ImageBackend interface
       ├── ComfyUIBackend
       ├── NativeQwenBackend
       └── FutureBackend

The app should be capable of:

- launching ComfyUI automatically
- detecting whether it is running
- checking installed nodes/models
- submitting workflows
- monitoring generation progress
- retrieving generated images
- cancelling jobs
- reporting errors cleanly
- restarting the backend if it crashes

The user should not normally need to open the ComfyUI interface.

## Model management

Add an Image Models manager showing:

- installed models
- available models
- model family
- disk size
- VRAM requirement
- quantization
- supported functions
- path
- enabled/disabled status
- default role
- update status

Allow:

- download
- install
- remove
- change model directory
- verify files
- repair installation
- select defaults

Never silently redownload a large model if it is already installed and passes verification.

## Storage

Use a structured directory such as:

models/
    image/
        qwen/
        flux/
        stable-diffusion/
        loras/
        vae/
        controlnet/
        upscalers/

data/
    image/
        generations/
        projects/
        references/
        characters/
        masks/
        thumbnails/

workflows/
    image/

Store metadata with each generated image including:

- prompt
- negative prompt
- model
- model version
- LoRAs
- seed
- dimensions
- steps
- guidance
- source/reference images
- workflow
- generation timestamp

## Generation history

Add searchable image history.

Allow filtering by:

- date
- model
- character
- project
- seed
- LoRA
- tag

Clicking an old image should allow:

- regenerate
- edit
- create variations
- reuse seed
- reuse settings
- upscale
- open containing project

## Job queue

Create an image-generation queue with:

- queued
- loading model
- generating
- refining
- upscaling
- finished
- failed
- cancelled

Display:

- progress percentage
- active model
- elapsed time
- estimated stage
- GPU VRAM usage

Allow queue reorder and cancellation.

## Resource management

Since the target machine has 12 GB VRAM, prevent image models and coding LLMs from fighting over GPU memory.

Integrate with the global model manager.

Before loading an image model:

1. check free VRAM
2. determine whether the current LLM must unload or partially offload
3. unload inactive models if necessary
4. load the image model
5. run the job
6. optionally restore the prior coding/chat model afterward

Add configurable modes:

- Prefer Chat Model
- Balanced
- Prefer Image Model
- Aggressive VRAM Cleanup

## API/tool exposure

Expose image capabilities to the main agent as tools similar to:

generate_image()

edit_image()

inpaint_image()

outpaint_image()

remove_background()

upscale_image()

create_variations()

load_character_profile()

list_image_models()

switch_image_model()

The chatbot should call these tools automatically when appropriate.

## UI behavior

The chatbot should understand requests such as:

“Make an image…”

“Edit this picture…”

“Replace the background…”

“Generate more pictures of Samantha…”

“Upscale this…”

and automatically transition into the image workflow without requiring the user to manually select tools.

Show progress directly inside the conversation.

When complete, display the generated image inline with controls for:

- Edit
- Variation
- Upscale
- Save
- Open Image Workspace
- View settings

## Error handling

Do not expose raw Python stack traces to normal users.

Provide useful messages for:

- CUDA out of memory
- missing model
- missing VAE
- incompatible LoRA
- backend offline
- corrupt checkpoint
- unsupported workflow
- failed dependency
- insufficient disk space

Include a technical-details expander for debugging.

## Logging

Log:

- requested operation
- selected workflow
- selected model
- routing reason
- VRAM before/after
- generation duration
- backend errors

Do not store uploaded private images outside the local machine unless the user explicitly configures remote storage.

## Development approach

Implement this incrementally without breaking the existing coding-agent functions.

First inspect the existing repository and architecture before modifying anything.

Then:

1. create the image module architecture
2. create model/backend interfaces
3. integrate ComfyUI
4. implement model discovery
5. implement automatic image routing
6. add Qwen-Image-2.1
7. add FLUX.2 Klein 4B
8. build the Image workspace
9. add reference/character profiles
10. add generation history
11. add LoRA management
12. integrate GPU/VRAM management
13. expose image tools to the chatbot
14. test complete conversational workflows
15. document installation and usage

Maintain backward compatibility with the rest of the application.

Do not replace functional components unnecessarily.

Commit work in logical stages with descriptive commit messages.

Update the project README and development/status documentation as features are implemented.