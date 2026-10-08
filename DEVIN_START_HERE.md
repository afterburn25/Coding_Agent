# DEVIN START HERE — Nexus Core takeover

This file is the handoff entry point for Devin. **Do not reconstruct project state from chat history if the repository can answer it.**

## Source of truth

- Repository: `afterburn25/Coding_Agent`
- Branch to continue from: `main`
- Always pull the latest `main` before starting work; do not pin development to a stale documentation head.
- Devin takeover anchor commit: `21aa4fb0090d7fbd1ef119d5026b5289b6a81e10`
- Current development version: `0.31.11` (canonical `VERSION` file; `scripts/sync_version.py` derives all artifacts)
- Verified unit checkpoint: **1930+ passing** on `milestone/integrated-reliability-closeout` (2 environment skips; count drifts as tests are added — run the suite for the current number)
- v0.16.0 persona depth: behavior profiles per family, seriousness/topic scaling, effective-persona compiler, relationship + mood dynamics, overlays/modifiers/modes, NL persona commands, persona-aware notices, blending/coherence/versioning/import-export, consistency layer
- v0.17.0 persona social continuity: social-cue + context-aware sarcasm detection, bounded energy blending, long-session pacing taper, focus/topic-shift tracking, shared-history milestones + relevance-gated callbacks, stated-preference consistency, preferred address, expression saturation dampening, humor feedback adaptation, voice smoothing + gesture timing, self-description/compare/similarity QA (see docs/PERSONALITY.md)
- v0.18.0 voice polish: spoken notices for the full user-task lifecycle (queued → started → completed/failed/cancelled) via persona-aware `persona_notice`, a rate-limited notification listener that voices important/failure/approval/completion events + report-ready info rows, ~60 new phrase/stage-direction forms in the vocalization engine with Kokoro render forms + gestures, expanded feedback categories, comparative persona commands ("be nicer", "act calmer for the next hour", "you can call me Ash"), and token fixes ("of"/"off" no longer eaten by `oof`, "uh-oh" outranks bare "uh")
- v0.26.x Chatterbox Turbo engine: second `TTSEngine` as an isolated subprocess runtime (`runtime/voice/chatterbox`, CPython 3.12 + torch/CUDA + chatterbox-tts 0.1.7) over JSONL stdio; approved Isabella V6 clone reference packaged under `voice/chatterbox_voices/`; BS.1770 loudness normalization (−17 LUFS) before the limiter; native paralinguistic/emotion tags via a Chatterbox vocalization adapter; Kokoro fallback with voice-id remapping + separate cache keys; verified runtime+model provisioning; Voice Studio engine selector + Voice Lab tag auditions (see `docs/VOICE_SYSTEM.md`)
- v0.15.0 adds decomposed mission planning, project context/history, code-impact queries, provisioned STT/push-to-talk with first-class backend/model config, pinned code-intelligence/LSP host dependencies, operational state + return briefing, mission checkpoints, Knowledge Library, structured correction learning, hardened granular desktop control, the avatar animation/lip-sync foundation, managed skill/plugin lifecycle depth, verified/path-safe self-repair rollback gating, bounded long-duration approval/recovery/retention behavior, mission-owned verified model installs, a dedicated Real-ESRGAN post-process upscaler, and workstation-depth isolated selftest coverage (see PROJECT_STATUS.md)
- 2026-10-03 overnight dogfood (`docs/reports/OVERNIGHT_REPORT_2026-10-03.md`): fixed a real queue-wedge — `resume()` leaked `_finalize()`'s internal repair-round `None`, stranding tasks `running` forever with no driver; now a driver registry + watchdog reaper fails driverless active tasks after `stalled_task_grace_seconds`. Also: Answer Memory profile scoping (privacy), personality delivery cues, preset voice defaults, canary `PYTHONPATH` isolation, v2-schema self-heal.
- 2026-10-05 integrated-reliability closeout (`milestone/integrated-reliability-closeout`): packaged browser E2E via Playwright+Edge channel (`dc56c48d`), live ComfyUI dogfood (Qwen-Image-2.1 / FLUX.2 Klein / Juggernaut t2i + background-removal + cancel), InvokeAI↔ComfyUI fallback matrix A–E verified live, cross-backend GPU/RAM arbitration + crash-resubmit (`cc702cb2`), cinematic splash + Isabella narration (`ebc816db`)
- Latest local milestone commits: `91f9ccf` (permission-gated runtime/process controls + live resource usage), `2f4d874` (complete bundled workflow/toolchain release contract), `8889b26` (release dependency/package-contract hardening), `84b9380` (workstation-depth isolated validation + safe external-log decoding), `ec55090` (validated/pinned code-intelligence dependencies + MCP interop record), `2f591d2` (configurable/pinned local STT provisioning), `7ca1fac` (dedicated verified Real-ESRGAN post-process upscaler), `e506960` (mission-owned verified model installs + restart-safe partial cleanup), `a2425d5` (long-duration approval/recovery/retention), `6da328f` (verified/path-safe self-repair rollback), `8c4ef08` (managed skill/plugin lifecycle), `f580a9e` (avatar animation/lip-sync foundation), `e63a4b1` (permission-gated desktop control), `fb947d1` (structured correction learning), `8fec84b` (Knowledge Library), `1332ef6` (decomposed planning, projects UI/context, STT, operational state); earlier dogfood/hardening: `f0a46f5`/`16acf05` (mission-loop dogfood), `e3d68ff` (replan dead-end sweep), `5e74928` (real ledger node status), `6c684c1` (mission park lane release), `c4050e5` (queue-wedge fix)
- GitHub Actions: all-green on the last several pushes; windows-desktop job compiles the real Inno installer and smokes install→update-twice including a fake-process kill check

The `main` branch includes the verified code plus documentation/checkpoint metadata. **Clone/pull `main`; do not reset the project to the code checkpoint or takeover anchor.**

## Read these files before changing code

Read in this order:

1. `DEVIN_START_HERE.md` — this takeover briefing.
2. `README.md` — current user-facing architecture/features.
3. `PROJECT_STATUS.md` — implemented/current gaps.
4. `ARCHITECTURE.md` — component boundaries.
5. `SESSION_HANDOFF.md` — chronological implementation history and exact checkpoints.
6. `.agent/project.json` — machine-readable current project state.
7. `MODEL_ROUTING.md` when touching model selection/runtime.
8. `docs/UI_DIRECTION.md` before changing the primary UI.
9. `docs/IMAGE_MODULE_SPEC.md` for image work.
10. `docs/VOICE_SYSTEM.md` for the local TTS/voice subsystem (Kokoro, presets, Voice Studio).
10. `docs/RESEARCH_SYSTEM.md` and `docs/WEB_RESEARCH.md` for research/browser work.
11. `docs/ANSWER_MEMORY.md` for the learned-answer memory subsystem (trust states, semantic gates, freshness/invalidation, API/UI).
12. `docs/LEARNING.md` for structured correction learning (scoped preference candidates, prompt overlays, inspection/forget controls, protected-state guardrails).
13. `docs/AUTONOMY.md` for the persistent-mission autonomy subsystem (missions, DAG, supervisor, policy, scheduler, triggers, recovery).
14. `docs/DESKTOP_CONTROL.md` for permission-gated local desktop control, audit boundaries, and validation rules.
15. `docs/AVATAR.md` for the canonical portrait, avatar state channels, gesture bridge, and lip-sync boundaries.
16. `TOOLS.md` for plugin manifests and managed skill-package lifecycle.
17. `docs/ACTION_LANE.md` for the deterministic local-action lane — bounded grammar, verified execution, the ActionLedger evidence contract, compound sequencing, and the eval gate. Success language requires a `verified` ledger entry or `*_OK` tool result.

## Product direction that must be preserved

Nexus Core is a **native local-first Windows AI workstation**, not a browser-only demo.

Canonical delivery stack:

```text
NexusCore.exe
  .NET 8 WinForms + Microsoft WebView2
        |
        v
hidden ChatNexus.Backend.exe
  Python Agent Core over loopback
        |
        +-- Nexus Brain / conversation / knowledge / Model Growth
        +-- Research/browser modules
        +-- Image/ComfyUI modules
        +-- permissioned tool registry
        |
        v
automatic model router
        |
        v
bundled llama.cpp + local model files
```

The canonical v0.6 UI is chat-first:
- slim left navigation
- conversation workspace in the center
- right **Code Diff / Tasks / Terminal** rail
- dark sci-fi/starship-computer styling
- orbital CN emblem
- animated **NEXUS CORE // ACTIVE** activity HUD

Do not replace the approved shell with a generic dashboard/IDE redesign unless the user explicitly changes direction.

## Default coding models

Expected model stack:

- Qwen3 14B Q4_K_M — utility / fast / normal primary coding.
- Qwen3-Coder 30B-A3B Instruct Q4_K_M — deeper reasoning/reviewer work.

Installer behavior:
- downloads missing verified model weights during install/update
- has overall install progress plus per-model download progress
- skips already-valid model files
- preserves local mutable state during Update

## Windows installer / update invariants

Canonical installer: `installer/ChatNexus.iss`

An update must preserve:
- `models/`
- `data/`
- `config.json`
- `workflows/`
- the bundled `Source` Git working copy and task state

The installer owns process shutdown before replacing files. CI already smoke-tests:
- fresh install
- installed application self-test
- running-app shutdown during update
- in-place update
- preservation of model/data/config/workflow/source/task-state markers

Do not remove these checks.

## Current verified artifacts

From GitHub Actions run `36796088939`:

- Artifact `11133807708`
  - `NexusCore-Setup-0.6.0-dev-Windows-x64`
  - 75,555,341 bytes
- Artifact `11133708132`
  - `Chat-Nexus-v0.6.0-dev-Windows-x64`
  - 118,918,524 bytes
- Artifact `11134055863`
  - `chat-nexus-v0.6-dogfood`
  - 346,477 bytes

These were not expired at handoff.

## Nexus Brain — current architecture

The protected Brain is intentionally **separate from model weights**.

Canonical protected state:
- `data/nexus_brain.json`
- `data/nexus_brain.auth.json`

Once initialized, the Brain location is canonical. Mutable `config.json` is not allowed to redirect, disable, or shrink an initialized protected Brain during live configuration reload.

The goal is:

```text
replace model weights
        |
        v
same Nexus Brain
        |
        +-- learned facts/preferences
        +-- sourced general knowledge
        +-- conversational skill examples
        +-- autobiographical continuity
        +-- signed subroutines
        +-- emotional profile
        +-- self-model
```

### Staging vs protected Brain

Normal conversation/research/training stores are staging layers:
- `data/conversation_memory.json`
- Conversation Manager data
- Knowledge Memory data
- Model Growth candidates

Normal chat activity must **not silently mutate the signed Brain**.

Protected Brain writes occur through an explicit creator-authenticated Sync. The current sync banks:
- facts/preferences
- behavior rules
- sourced knowledge
- approved/reviewable training signals
- bounded autobiographical conversation summaries

## Nexus Brain creator security

### Do not put creator credentials in source

The creator passcode is intentionally **not in GitHub, config.example.json, installer source, tests, docs, or exported public Brain packages**.

Do not hardcode a passcode.

Creator authentication is local through Trainer.

### Ed25519 design

Current Brain integrity/signing uses **Ed25519**.

Creator installation:
- creates an Ed25519 signing key pair
- keeps the private signing key only in local creator-auth state
- private key is encrypted in PKCS#8 form using the local creator passcode
- signs the Brain payload
- exposes only a public-key fingerprint in normal status

Legacy HMAC/scrypt Brain state has a migration path: a successful creator unlock migrates the Brain to Ed25519.

### Public Brain export

A distribution export is read-only and contains:
- signed Brain data
- creator public verification key
- creator public-key fingerprint

It **does not contain the encrypted creator private signing key**.

A recipient can verify and use that Brain without knowing the creator passcode, but cannot unlock it for mutation/re-signing.

### Creator-signed updates

A distributed read-only Brain accepts an update only when:
1. the incoming Brain verifies successfully,
2. it has the exact same creator public-key fingerprint,
3. its signed `updated_at` is newer.

A stale Brain is ignored.

A Brain signed by another key is rejected.

A creator installation that actually holds the private signing key is never automatically overwritten by a distribution Brain.

## Shipping a trained Brain

Private packaging supports:

```text
CHAT_NEXUS_BRAIN_SEED=<path to public signed Brain export>
```

`packaging/build_windows.ps1` validates the export before copying it to:

```text
brain-seed/nexus-brain-locked.json
```

Validation requires:
- expected Brain export format
- Ed25519 metadata
- public verification key
- public-key fingerprint
- signed Brain data
- **no encrypted private signing key**

On first startup:
- if no Brain exists, the bundled seed is public-key verified and installed
- if a read-only Brain from the same creator exists, a newer signed seed may update it
- stale seeds do nothing
- another creator key cannot replace it
- creator installations are not auto-overwritten

This lets a trained Brain ship independently of the model weights.

## Signed Brain subroutines

Current protected switches:

- `adult_content`
- `image_generation`
- `web_research`
- `long_term_memory`
- `self_learning`
- `general_knowledge_learning`
- `conversation_learning`
- `model_growth`
- `temporal_context`
- `humor`
- `emotions`
- `self_model`

Important distinction: Brain subroutines configure product behavior, but do not disable separate hard tool/action safety and permission gates.

Current code-enforced routes include:
- image-generation enable/disable
- web-research enable/disable
- Model Growth enable/disable
- adult/explicit image generation enable/disable

The adult-content gate for images is enforced in `ImageManager.create_job()`, not just via prompt instructions. Therefore direct image routing, agent tool calls, and Image Studio all hit the same gate.

## Image safety invariant

Local image policy intentionally supports lawful adult-only synthetic generation while retaining narrow hard blocks such as:
- sexual imagery involving minors/ambiguous-age subjects
- non-consensual intimate imagery
- explicit real-person edits without an active adult consent record

Do not replace the image safety layer with a generic chat-model refusal.

The deterministic text-to-image route intentionally bypasses chat-model judgment and uses the image policy/tool layer.

## Conversation behavior / personality

Recent work deliberately makes Nexus less like a basic assistant.

Conversation quality behavior now includes:
- mature adult conversational tone
- better continuity
- fewer repeated questions
- fewer canned customer-service endings
- natural contractions and varied sentence rhythm
- occasional dry/playful humor when appropriate
- no forced question at the end of every response
- real temporal continuity using stored message timestamps
- live host-local date/time grounding

Remembered facts are **canonical meaning, not canned output**. Nexus should paraphrase learned facts naturally on recall while preserving exact values such as names, dates, numbers, code, identifiers, and URLs. Exact wording is only used when the user asks for a quote/verbatim recall.

## NEXUS CORE activity HUD

The main chat shows a sci-fi/starship-style live activity HUD while a request is running.

It is driven by real high-level events:
- context loading
- planning/working
- model routing/switching
- research
- tool execution
- verification
- review
- approvals
- image generation
- policy retry
- elapsed time

It must **not expose hidden chain-of-thought/private reasoning**.

## Time awareness

Nexus Core uses the host OS clock via timezone-aware runtime timestamps.

It understands:
- current date
- current time
- weekday
- timezone/UTC offset
- relative words such as today/tomorrow/yesterday/tonight

Simple time/date questions are answered locally without loading Qwen.

Conversation Manager also turns stored timestamps into:
- absolute local times
- elapsed-time descriptions
- meaningful inter-message gaps
- recent previous-conversation recency

## Self-learning beyond coding

The previous “I only learn coding” behavior is obsolete.

Nexus can stage/learn:
- code/development patterns
- explicit user-taught facts/preferences
- general knowledge
- sourced research with provenance/freshness
- conversational style
- corrections
- positive/negative feedback
- approved conversation examples
- autobiographical continuity

Explicit local fact commands include:
- `Remember that ...`
- `Learn that ...`
- `Fact: ...`

Sourced/current-sensitive knowledge keeps freshness metadata; expired current-sensitive knowledge must not be presented as current truth.

## Conversational skill portability

Approved `conversation_example` and `correction` training signals can be stored in the Brain.

A fresh/replacement model can retrieve relevant examples as **in-context skill guidance**.

The model is explicitly told to adapt the pattern rather than copy the stored response mechanically.

This is how conversational learning can survive a model replacement even before a new LoRA is trained.

## Simulated emotional state

Nexus has a signed emotional profile including:
- warmth
- curiosity
- confidence
- playfulness
- concern sensitivity
- energy
- maximum affect intensity
- decay

Runtime affect is transient and may move among states such as:
- calm
- warm
- curious
- amused
- concerned
- energized
- frustrated

The transient state influences wording/pacing/tone without rewriting protected Brain data every turn.

These are simulated affective states. Nexus must not falsely claim biological sensations or a biological human body.

## Persistent self-model

The signed self-model supports:
- Nexus name
- human-like conversational behavior
- autobiographical continuity
- stable preferences
- self-model growth

The protected identity invariant remains:

```text
identity_type = "AI system"
claim_biological_human = false
```

Do not change this into a false claim of biological humanity.

## Model Growth

The live base model is not silently mutated.

Model Growth flow remains:
1. collect review candidates
2. review/approve/reject
3. export dataset
4. create versioned offline LoRA/QLoRA/full-finetune job
5. evaluate
6. promote explicitly
7. retain rollback

Positive feedback can produce approved conversation examples.

Negative feedback is retained as a negative signal and must not be exported as a supervised “good answer” target.

## Key source files for Nexus Brain work

- `localcodeagent/workflow/nexus_brain.py` — protected Brain, signing, export/import/update, knowledge/skills/self-model/emotions
- `localcodeagent/workflow/conversation_memory.py` — staging facts/rules/corrections
- `localcodeagent/workflow/conversation_manager.py` — durable sessions/personality/timing/feedback
- `localcodeagent/workflow/knowledge_memory.py` — sourced/freshness-aware staging knowledge
- `localcodeagent/training/model_growth.py` — candidates/datasets/training-job metadata
- `localcodeagent/agent/orchestrator.py` — context injection, routing, Brain subroutine enforcement
- `localcodeagent/server.py` — Brain lifecycle/APIs/seed loading/config reload authority
- `web/trainer.html`, `web/trainer.js`, `web/trainer.css` — creator Brain control surface
- `localcodeagent/image/manager.py` — image queue plus signed adult-content gate
- `localcodeagent/image/policy.py` — narrow image safety/consent checks
- `packaging/build_windows.ps1` — native package plus optional signed Brain seed
- `installer/ChatNexus.iss` — update-aware Windows installer
- `.github/workflows/tests.yml` — Linux unit + Windows build/installer/update validation

## Recent Brain/security commit sequence

These commits explain the design progression:

- `f436617` — initial creator-locked adaptive Nexus Brain
- `886c412` — KDF portability fix
- `6058446` — explicit creator-only protected writes
- `942f0c1` — Brain general knowledge + autobiography
- `a0157e4` — restore conversational skills from Brain
- `d3e3dd0` — explicit general-fact commands
- `0bb41cf` — Ed25519 Brain signing
- `d7508b3` — public read-only signed Brain exports + installer seed support
- `07fe392` / `db0573d` — same-creator signed update enforcement/regression fix
- `b1d9a07` — protected Brain canonical authority + explicit-image adult-content gate
- `f19fc58` — documentation/source-of-truth checkpoint for the verified 188-test build

## Testing

Run:

```bash
python -m unittest discover -s tests -v
```

Expected at takeover: **188 tests passing**.

CI also installs the required `cryptography` package for Ed25519 tests/builds.

Windows packaging is validated by `.github/workflows/tests.yml`.

## Build / local developer commands

Python package:

```bash
python -m unittest discover -s tests -v
```

Windows desktop package:

```powershell
./packaging/build_windows.ps1
```

Optional private signed Brain seed:

```powershell
$env:CHAT_NEXUS_BRAIN_SEED = "C:\path\to\chat-nexus-brain-locked.json"
./packaging/build_windows.ps1
```

Do not put a creator-private Brain/auth file into the public repository.

## Do-not-regress checklist

Before accepting a substantial change, verify these still hold:

- `main` remains the source of truth.
- Native `NexusCore.exe` still launches the hidden backend.
- Installer still preserves `models/`, `data/`, `config.json`, `workflows/`, and `Source/`.
- Unit suite remains green.
- Windows build/installer/update smoke remains green.
- Protected Brain remains canonical under `data/`.
- Config reload cannot redirect/disable an initialized Brain.
- Public Brain export contains no private signing key.
- Brain signature verifies without creator passcode.
- Different creator keys cannot update a distributed Brain.
- Creator Brain is not auto-overwritten.
- Protected Brain writes require creator authentication.
- No passcode is committed or logged.
- Adult-content Brain setting reaches chat behavior and explicit-image queueing.
- Hard tool/action/image safety remains separate from configurable Brain behavior.
- Model replacement does not erase Brain knowledge/personality.
- Learned facts are paraphrased rather than copied mechanically.
- Time/date comes from the runtime clock.
- NEXUS activity HUD shows high-level status only, not hidden reasoning.
- Negative feedback cannot become a positive SFT target.
- Success language for a local action requires a `verified` ActionLedger
  entry or a `*_OK` tool result — a plan, a permission grant, or a queued
  op is not evidence (v0.31.0 invariant).
- Action-shaped capability phrasings ("can you create a folder X") must
  not be answered as capability questions — they reach the deterministic
  `action_ops` lane or the model lane (regression: `D:\Nexus`).
- Outside-workspace targets named by the user always require approval,
  regardless of configured filesystem permission level.
- The deterministic action lane must never intercept mission turns or
  genuine capability questions ("can you generate images").

## Immediate next work for Devin

The repository records the next milestone as real end-to-end Brain dogfooding plus operational hardening.

Recommended order:

1. **Dogfood creator Brain lifecycle**
   - initialize creator Brain locally
   - configure signed subroutines/emotion/self-model
   - stage facts, sourced research, feedback, and corrections
   - creator Sync
   - verify record counts and prompt behavior

2. **Verify model replacement continuity**
   - stop/remove/swap a model
   - load a fresh compatible model
   - prove facts, relevant general knowledge, autobiographical continuity, personality, and approved conversation skills still come from Nexus Brain

3. **Verify distribution**
   - export public read-only Brain
   - confirm export has public key/fingerprint and no private signing key
   - private-build an installer with `CHAT_NEXUS_BRAIN_SEED`
   - clean-install it
   - confirm public signature verification and automatic Brain activation
   - confirm recipient cannot unlock/mutate the Brain

4. **Verify signed updates**
   - produce a newer creator Brain export
   - confirm recipient accepts it
   - confirm old/stale export is ignored
   - confirm a different creator key is rejected

5. **Creator-key backup/recovery** — DONE (`eb5aa70`)
   - `POST /api/nexus-brain/key-backup` exports the creator signing key as an
     encrypted bundle; requires creator session + passcode re-authentication;
     the key is re-encrypted under a separate backup passphrase
   - `POST /api/nexus-brain/key-restore` recovers a lost auth sidecar; the
     restored key must verify the Brain's existing signature and match the
     pinned fingerprint, so foreign/different-key backups are rejected
   - never included in public Brain exports; Trainer page has Back up /
     Restore creator key buttons

6. **Brain audit/history** — DONE (fc0db5e, e5e0d92)
   - Trainer page renders the tamper-evident audit log (events + metadata)
   - bounded 10-version signed settings history with per-version signatures
   - creator-only rollback via POST /api/nexus-brain/rollback; rollback is
     itself a signed save, so it is reversible

7. **Conversation dogfood**
   - use thumbs/corrections in real sessions
   - inspect whether recall paraphrases naturally
   - identify repetitive/infantile patterns
   - only then decide whether to train a dedicated Conversation LoRA

## v0.7 direction — modular local AI workstation

User direction (2026-09-30): expand Nexus Core into a modular, general-purpose
local AI workstation with a unified tool/plugin system. Users describe the
outcome; the agent chooses the models, tools, runtimes, and external
applications automatically. Do not build it as one tightly coupled subsystem —
tools install/remove/update/enable/disable/replace independently through the
Tool Registry.

Phase plan (see `TOOLS.md` / `PERMISSIONS.md` / `ARCHITECTURE.md`):

1. **Foundation** — Tool Registry + manifests, tool interfaces, permission
   levels/profiles, Process Manager, Job Manager, resource awareness, Tool
   Manager UI. **Done**: commits `105182a`, `d55bf52` (249 tests).
2. **Developer tools** — terminal adapters (PowerShell/CMD/Bash), Git/GitHub
   depth, ripgrep search, build-system adapters (CMake/MSBuild/npm/cargo/...),
   test runners, Tree-sitter, LSP.
3. **Research & browser** — Research Agent on the tool system, Playwright
   backend, browser testing mode.
4. **Local AI** — llama.cpp model manager page (roles: chat/coding/research/
   planning/vision/fast-router/review), GPU policies.
5. **Image** — ComfyUI manager on the registry, workflow templates, gallery.
6. **Media** — FFmpeg, whisper.cpp transcription, modular TTS, Media Agent.
7. **Documents** — OCR (Tesseract), Pandoc, PDF tools, Document Agent.
8. **Data** — DuckDB/SQLite, Data Agent, chart generation.
9. **Sandboxing & packages** — Docker sandbox, dependency/package managers.
10. **Additional** — Blender, generic API tools + OpenAPI import, local
    RAG/indexing with vector-backend abstraction, more MCP integrations.

Brain hardening (the "Immediate next work" list above) remains a parallel
track; dogfood the creator Brain lifecycle alongside Phase 2+.

## Important security boundary

Nexus Brain protection is application-level cryptographic integrity/access control. A person with full administrator control who can replace the executable/backend can ultimately change program behavior. Do not document or market the design as protection against an OS administrator controlling the entire machine.

## Handoff rule

Before ending a substantial Devin development session:

1. run the tests
2. run/inspect Windows CI for executable changes
3. update `PROJECT_STATUS.md`
4. update `SESSION_HANDOFF.md`
5. update `.agent/project.json`
6. update this file when takeover-critical architecture/next steps change
7. record the exact verified code checkpoint and CI run

