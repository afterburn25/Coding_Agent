# Automatic Model Routing

The normal user-facing setting is `Auto`.

The orchestrator routes by capability role rather than hard-coded model name:

- `utility` — lightweight classification/summaries/background work
- `fast_coder` — small edits and low-complexity coding
- `primary_coder` — normal feature implementation and repository work
- `deep_reasoner` — architecture, difficult debugging, complex refactors
- `reviewer` — post-change independent diff/regression review
- `vision` — future screenshot/visual UI work

A model profile can advertise several roles.

## Task-phase routing

v0.3 uses routing more than once during a task. The implementation model is selected for the work phase, while changed tasks make a second routing decision with `phase="review"`.

```text
request
  ↓
implementation role/model
  ↓
changes + verification
  ↓
reviewer role/model
```

Repeated tool failures can still trigger an in-task escalation toward a `deep_reasoner` model before implementation finishes.

## Resource-aware ranking

After role selection, candidates are ranked using:

1. Configured RAM/VRAM fit against current machine state.
2. Resource preference (full-VRAM fit ahead of CPU offload).
3. Bounded local outcome score when enough comparable samples exist.
4. Profile priority.
5. Context window.
6. Stable model ID ordering.

Outcome telemetry can only rank candidates that already pass the resource-fit stage. It cannot make an oversized model outrank a runnable model, and manual role override still bypasses role classification.

If configured estimates say no candidate fits, the router returns the best fallback and records the resource warning instead of silently failing to choose a model.

## Runtime switching

Runtime Manager translates routing decisions into actual local runtime state. With `max_resident_models: 1`, a switch can stop the least-recently-used managed model before starting the newly selected model and waiting for `/health`.

This applies to:

- initial task selection
- repeated-failure escalation
- post-change reviewer handoff

Manual role override remains available in the UI and bypasses task-role classification while preserving runtime/resource checks.

## Local outcome telemetry (v0.6)

Nexus Core keeps a small local record at `.agent/model_performance.json` by default. It stores no prompt text, source code, retrieved pages, credentials, or conversation transcript.

Each event contains only coarse routing/outcome fields such as:

- model ID and routed role
- low / medium / high complexity band
- completed / warning / error / step-limit outcome
- final verification pass/fail when verification ran
- reviewer PASS/FINDINGS signal when available
- step count, elapsed seconds, repair-cycle count
- whether external research evidence was used

The router requires a configurable minimum sample count (default 3) before the history affects selection. The learned score is bounded (default weight 20), is evaluated only after resource fit, and falls back to the existing deterministic priority/context ordering when data is sparse. `GET /api/model-telemetry` exposes aggregate statistics without exposing task content.

## Planned routing improvements

- repository language and size
- retrieved-context token budget
- measured model load time and tokens/second
- model-specific language/framework strengths
- finer task categories beyond role + complexity band
- KV/cache residency and switch cost
- reviewer quality and test-fix success history

## Image model routing (v0.4)

Image routing is separate from coding/chat routing. The main agent calls an image tool; `ImageRouter` then chooses the image operation and image model from request features and current resources.

Signals include:

- generate vs edit vs inpaint vs outpaint
- background removal / upscale intent
- fast preview vs maximum quality
- source/reference count
- transparency requirement
- configured model capabilities
- current free VRAM/RAM
- manual image-model override

Default policy templates:

```text
Fast preview/draft       → FLUX.2 Klein 4B
High-quality generation  → Qwen-Image-2.1
General image edit       → Qwen-Image-2.1
Inpaint/outpaint         → capability-matched Qwen workflow
5+ references            → model configured for the required reference count
```

Image model profile estimates are advisory. The real workflow/node stack may consume different VRAM depending on quantization, resolution, attention implementation, VAE behavior, reference count, and offload settings. Users can override estimates per installed profile.
