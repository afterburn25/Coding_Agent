# Nexus Core

Nexus Core is a native local-first AI coding workstation for Windows with automatic model switching, transactional coding workflows, web research/browser tools, and a modular local image-generation/editing system.

**GitHub source of truth:** `afterburn25/Coding_Agent`

> **Devin takeover:** start with `DEVIN_START_HERE.md`. It records the exact current head, verified code checkpoint, CI/artifacts, Brain security invariants, important source files, and next executable work.

Future development sessions should begin with `DEVIN_START_HERE.md`, then `README.md`, `PROJECT_STATUS.md`, `ARCHITECTURE.md`, and `SESSION_HANDOFF.md`.

## Current development version

`0.28.1`

Canonical version lives in `VERSION`; `scripts/sync_version.py` derives
every downstream artifact (pyproject, installer defaults, .NET project,
PyInstaller resource, `.agent/project.json`) — `--check` verifies parity.

The paragraphs below are **historical release highlights**, not the
current state — `PROJECT_STATUS.md` and `SESSION_HANDOFF.md` are
authoritative for what is actually live.

v0.8 gives Nexus Core a modular **cognitive architecture** — the *Nexus Brain* coordinates seven functional regions (Prefrontal Cortex, Hippocampus, Thalamus, Basal Ganglia, Motor Cortex, Cerebellum, Brain Stem) over a typed cognitive event bus, the *Corpus Callosum*. The LLM is not the brain: models are interchangeable inference resources routed by capability, while memory, plans, learned procedures, health, and action history persist in Nexus itself. See [`docs/architecture/NEXUS_BRAIN.md`](docs/architecture/NEXUS_BRAIN.md).

v0.7 turns Nexus Core into a modular local-first AI workstation: measured runtime auto-tuning, a durable Devin-style activity timeline, persistent autonomy, computer use, sandboxes, LSP, repository RAG, managed skill/plugin packages, multi-agent worktrees, an evaluation lab, artifacts, backups, DPAPI-wrapped secrets, connectors, a knowledge graph, simulation, and a hardware Digital Twin. The canonical version lives in `VERSION`; `scripts/sync_version.py` derives every downstream artifact (pyproject, installer defaults, .NET project, PyInstaller resource, `.agent/project.json`).

## Nexus Core identity and v0.6 UI

The product name is **Nexus Core**. The GitHub repository remains `afterburn25/Coding_Agent` as the development source of truth.

The canonical v0.6 **native desktop application** is chat-first:
- slim left navigation for Chat / Projects / Models / Research / Images / Tools / Settings
- center conversation workspace with automatic model routing
- right utility rail with **Code Diff / Tasks / Terminal** tabs
- official Nexus Core brand assets: the full shield/wordmark (`web/assets/nexus-core-logo.png`) for large surfaces and the compact shield icon (`web/assets/nexus-core-icon.png` / `nexus-core.ico`) for small ones
- dark navy UI with restrained neon accents
- local-system details remain available without dominating the chat experience

See `docs/UI_DIRECTION.md` before changing the primary application shell.

## Startup splash and readiness lifecycle

Launching `NexusCore.exe` shows the official splash artwork (`nexus-core-splash.png`)
immediately while the real startup pipeline runs behind it:

1. The splash appears first; the main window is built but kept hidden.
2. The hidden `ChatNexus.Backend.exe` starts and must report real health.
3. WebView2 initializes and loads the interface.
4. The web shell posts a `nexus-core-ready` handshake
   (`window.chrome.webview.postMessage`) only after its initialization completes.
5. The splash dismisses only when **both** conditions hold: the handshake has
   arrived **and** at least **7 seconds** have elapsed (`StartupProgress`).
   If readiness arrives early the splash holds at ~98% until the minimum;
   if it arrives late the splash closes immediately — no timer-only exit.
6. The progress bar and status line reflect real milestones (backend launch,
   health, WebView2 init, interface load) — never fabricated animation.
7. On fatal startup failure the splash switches to an actionable
   **NEXUS CORE COULD NOT START** state with **Retry / Open Log / Exit**
   (`data/logs/backend-host.log`) instead of dying or hanging silently.

## Windows installer and upgrades

The **primary Windows deliverable is now a single compressed installer EXE**:

`NexusCore-Setup-<version>-Windows-x64.exe`

The installer uses a stable application identity so a later installer can detect an existing Nexus Core installation. On an existing installation the wizard becomes **Update Nexus Core** and the final action button says **Update**. The update replaces application/backend/runtime files while preserving mutable local state:

Update shutdown is installer-owned rather than delegated to Windows Restart Manager. Setup first asks the running `NexusCore.exe` process tree to close, waits briefly, then force-cleans any orphaned `ChatNexus.Backend.exe`, `llama-server.exe`, or `llama.exe` before replacing files.

- downloaded `models\`
- `config.json`
- generated `data\`
- the existing self-development `Source\.git` workspace and local edits/task state

The installer is per-user under Local AppData, creates Start Menu shortcuts, optionally creates a desktop shortcut, uses LZMA2 solid compression, and acts as a small bootstrapper for the default coding stack. During a normal fresh install it downloads Qwen3 14B and Qwen3-Coder 30B-A3B directly into `models\`, verifies each file with its pinned SHA-256, and uses the built-in install progress bar for the entire setup. When model downloading begins, a second progress bar appears underneath for the current model; it fills for 14B, resets for 30B, and shows downloaded MB while the main bar continues tracking the whole install/update. During an update the installer checks canonical model files first and downloads only missing or untrusted ones. CI performs a real fresh install and in-place update with model downloading disabled only for the smoke-test process, launches the installed app self-test, and confirms mutable state survives.

The portable ZIP remains a secondary development/recovery artifact.

### SmartScreen / antivirus download warnings

Windows may show **"Windows protected your PC"** or mark the download as suspicious. That happens because release builds ship **unsigned** — SmartScreen has no publisher reputation for the installer — not because the installer does anything unusual. Mitigations already in the build:

- `NexusCore.exe` carries full assembly metadata (company, product, version).
- `ChatNexus.Backend.exe` embeds a PyInstaller version resource
  (`packaging/backend_version.txt`) with publisher/product identity.
- The Inno installer declares publisher/version info and supports signing.

To ship signed builds, add repository secrets and CI signs `NexusCore.exe`,
`ChatNexus.Backend.exe`, the installer, and the uninstaller automatically:

- `NEXUS_CODESIGN_PFX_B64` — base64-encoded Authenticode PFX
- `NEXUS_CODESIGN_PASSWORD` — the PFX password
- *or* `NEXUS_CODESIGN_THUMBPRINT` — SHA-1 thumbprint of a cert already in
  the runner's store

For unsigned builds: click **More info → Run anyway**. You can also submit
the installer to Microsoft's false-positive review so Defender/SmartScreen
stops flagging it: <https://www.microsoft.com/en-us/wdsi/filesubmission>

## Native Windows desktop application

The normal Windows deliverable is **`NexusCore.exe`**, not a browser launcher. `NexusCore.exe` is a self-contained **.NET 8 WinForms** application using **Microsoft WebView2**. It starts `backend/ChatNexus.Backend.exe` as a hidden child process and connects the desktop UI to that backend over loopback only. The HTTP service is private implementation plumbing, not the user-facing product.

The portable Windows dogfood bundle includes:
- `NexusCore.exe` with the official Nexus Core icon
- embedded Chat / Image Studio / Research Hub UI
- pinned llama.cpp Vulkan x64 runtime under `runtime/llama`
- a real `Source` working copy of `afterburn25/Coding_Agent` for self-development
- config/example and build metadata

Normal users launch `NexusCore.exe`. The hidden Python backend remains available as `backend/ChatNexus.Backend.exe --server` only for low-level development/debugging. The earlier pywebview/pythonnet desktop host was removed completely after a frozen CLR-loading failure.

### Default coding models

The intended two-model coding setup is:

- **Qwen3 14B Q4_K_M** — utility, fast coder, and normal primary coding.
- **Qwen3-Coder 30B-A3B Instruct Q4_K_M** — deep reasoning and reviewer work.

The 30B model is expected to use llama.cpp CPU/GPU offload on a 12 GB GPU. Routing accounts for memory that will be freed when the resident 14B process is stopped before a 30B switch, so temporary RAM pressure from 14B does not incorrectly disqualify 30B. If 30B still cannot start, Auto mode falls back to the runnable 14B primary coder instead of terminating the task; the same fallback applies to reviewer activation. The everyday Qwen3 14B route starts llama.cpp with `--reasoning off` unless explicitly overridden and defaults to a 2,048-token output cap; the 30B deep/reviewer route defaults to 8,192 output tokens.

RAM estimates are now treated as planning estimates rather than absolute byte-perfect limits. Managed llama.cpp profiles that allow CPU offload may enter a bounded near-fit band (up to a small host-RAM margin) and try llama.cpp auto-fit. For example, a 30.0 GB estimate with 29.8 GB currently available is allowed with a routing penalty instead of being rejected; clearly oversized estimates are still blocked.

The Windows package bundles llama.cpp itself but not the GGUF bytes inside the installer EXE. The installer downloads the default 14B + 30B coding stack during setup/update when those verified files are missing, keeping the setup EXE small while leaving the installed application ready for coding.

## Core coding-agent capabilities

- ChatGPT-style local chat UI.
- Automatic model roles: utility, fast coder, primary coder, deep reasoner, reviewer, vision; greetings/capability questions take a lightweight utility path.
- Automatic escalation when tasks become harder or repeated attempts fail.
- RAM/VRAM-aware model choice, including memory that will be reclaimed when the currently resident model is replaced.
- Automatic activation fallback in Auto mode when a preferred deep/reviewer model cannot start.
- Outcome-aware candidate ranking from local verification/review history once enough samples exist.
- Managed llama.cpp model start/health/stop/recovery.
- GGUF inventory.
- Workspace-contained filesystem tools.
- Transactional multi-file patches.
- Per-task checkpoints and one-click undo.
- Shell/build/test tools with permission gates.
- Reviewer-model handoff.
- Persistent task ledger, project memory, repository index, and local conversation memory.
- Interrupted tasks are normalized to a recoverable state after restart; pending approvals can also resume cold from durable task metadata.
- Local-first TTS voice: Kokoro-82M ONNX engine, official `Nexus Synthetic — Isabella` preset (`bf_isabella` + DSP layers), Voice Studio, global mute, speech filter that never reads code aloud, and `voice_*` tools. See `docs/VOICE_SYSTEM.md`.
- Canonical Nexus avatar presence with bounded listening/thinking/speaking states, gesture-driven expressions, and lightweight lip-sync timing that degrades to the static portrait. See `docs/AVATAR.md`.

## Persistent conversation memory

Nexus Core now stores bounded conversation history, user-taught preferences/rules, and correction examples locally in `data/conversation_memory.json`. The Local system drawer shows Memory & training counts. Saved rules are included in future prompts, and recent conversation is restored after restart.

Completed tasks also persist bounded `final_content`. If the final stream event is lost after a task finishes, the UI can recover the actual answer from durable task state instead of reporting a completed-task error.

## Conversation Manager, sourced learning, and Model Growth

Nexus Core now has a durable Conversation Manager with searchable/restorable conversations, personality controls, feedback capture, persistent scoped memory, and a dedicated Trainer / Model Growth workspace. Sourced research can be remembered with provenance and freshness metadata, and expired/current-sensitive knowledge can be refreshed instead of silently reused forever.

The Model Growth Lab keeps the live base model intact. Learned behavior, corrections, feedback, and sourced knowledge can become reviewable candidates; approved items can be exported into versioned datasets and training-job manifests for LoRA/QLoRA/full fine-tuning, then evaluated/promoted/rolled back explicitly.

## Nexus Answer Memory

**Nexus Answer Memory** (`data/nexus_brain/answer_memory.db`, SQLite WAL) is a persistent learned Q&A layer inside the Brain architecture. Once Nexus has answered something and the answer has earned trust, asking the same question again — even phrased differently — returns the stored answer in under a millisecond **without loading or calling any model**.

- Every completed exchange is recorded as an *experience*; experiences are observations, not truth.
- Answers promote from `observed` → `candidate` → `trusted`/`verified` only with evidence: explicit `learn` commands, user corrections, positive feedback, or repeated matching experiences. Only trusted/verified rows bypass inference.
- Semantic matching uses a deterministic local embedder (`hashed-ngram-v1`, no download) with hard conflict gates so that *France vs Italy*, *Qwen 14B vs 30B*, *start vs stop ComfyUI*, and *image model vs voice model* can never collide.
- Freshness is enforced per answer: `live` questions (weather, news, latest versions) never bypass, repository-dependent answers go stale when the workspace HEAD moves, config-dependent answers go stale on config changes, and corrections invalidate instantly.
- Learned answers are scoped per project; secrets (API keys, passwords, tokens) are refused before persistence; no chain-of-thought is ever stored.
- Commands: `learn that <q> is <a>`, `learn this answer`, `forget the answer for <q>`, `the correct answer is …`, `what have you learned`, `did you answer that from memory`.
- The **Learned Answers** page (`/answers.html`, linked from every screen) lists/search/filters answers with trust, confidence, freshness, scope and usage, and supports learn/forget/mark-incorrect/update/merge/refresh plus export/import, index rebuild, vacuum, and clear.
- Full reference: [docs/ANSWER_MEMORY.md](docs/ANSWER_MEMORY.md)

## Nexus Autonomy — Persistent Missions

**Nexus Autonomy** (`localcodeagent/autonomy/`, state under `data/autonomy/`) is the persistent mission layer: durable objectives that survive restarts, decomposed into a dependency-aware task DAG and driven by a bounded event-driven supervisor.

- **Missions** (`/missions.html`) carry objective, status, success criteria, budgets, task graph, approvals, artifacts, checkpoints and a full transition history — persisted atomically, quarantined on corruption, recovered on restart (interrupted work resumes instead of restarting).
- **DAG execution** runs independent branches concurrently with leases and named resource locks; interactive chat always wins the agent lane.
- **Recovery** classifies failures (CUDA OOM, socket resets, tool crashes, permission-required…) and walks finite playbooks — bounded retries, replan, escalate — never infinite loops; repeated identical failures stop the mission.
- **Policy** profiles (`supervised` / `local_autonomous` / `extended_autonomous` / `custom`) gate action classes on top of the existing PermissionManager; sensitive actions (`git_push`, `create_pr`, `packages`, credentials…) always require approval or a scoped, expirable standing grant.
- **Schedules** (once/interval/daily/weekly) and **triggers** (file_changed, startup, CI events, custom…) persist across restarts and materialize missions; standing goals spawn recurring missions.
- **Approvals** pause a mission into `waiting_approval` and resume the exact step after a decision; denials/timeouts trigger bounded replanning (`max_approval_retries`) and autonomous waits honor `autonomous_approval_timeout_seconds`. `stop autonomy` (chat command or API) halts everything immediately.
- **Evaluation** checks success criteria (`all_tasks_completed`, `verify_passed`, `artifact_exists`, metrics…) before a mission is called complete — tool success alone is never enough.
- Chat commands `make this a mission` / `stop autonomy` / `resume autonomy` work in both streaming and non-streaming chat without needing a coding model.
- Full reference: [docs/AUTONOMY.md](docs/AUTONOMY.md)

## Cognitive architecture

Since v0.8, cognition is organized as brain-like regions over a typed event bus — not a single model call:

- **Thalamus** classifies input, answers trusted-memory and deterministic fast paths (version, status, known answers) with no model at all, and selects a model by *capability* (coding/vision/context/quality/latency) from live catalog + health state.
- **Hippocampus** unifies episodic, semantic, procedural, and project memory (SQLite + Answer Memory + knowledge graph + conversation memory) with confidence, provenance, freshness, and invalidation.
- **Prefrontal Cortex** builds and tracks plans, replans after failure, scores strategies, and runs a conflict monitor for loops/repeated failures/contradictions.
- **Basal Ganglia** scores candidate actions on usefulness, historical success, cost, latency, risk, approval, and resource pressure.
- **Motor Cortex** executes selected actions through controlled executors and the tool router, emits structured telemetry, and enforces approval gates.
- **Cerebellum** records performance trends and manages bounded, reversible optimizations (propose → measure → rollback).
- **Brain Stem** owns health probes, watchdog ticks, crash recovery, and the shared resource snapshot — deterministic, never model-dependent.
- **Corpus Callosum** is the typed message bus: correlation ids, addressed mailboxes, request/response, cancellation, and a bounded trace ring.
- **Specialist brains** (coding, research, vision, reviewer, systems, language) are declarative profiles sharing the global bus/memory/stem.

Trace and region state: `GET /api/brain/status`, `GET /api/brain/trace`, and the *Nexus Brain* panel on the System page. Full doc: `docs/architecture/NEXUS_BRAIN.md`.

## Nexus Brain (identity vault)

**Nexus Brain** is the model-independent long-term identity/learning layer under `data/nexus_brain.json`. Model GGUF files are replaceable reasoning engines; the Brain is separate durable state, so replacing Qwen, downloading fresh weights, or rolling back a LoRA does not erase the protected knowledge/personality layer.

The Brain banks creator-approved/synced:
- learned facts and preferences
- operating rules
- sourced general knowledge with provenance/freshness metadata
- approved conversational examples and corrections as portable skill guidance
- autobiographical conversation summaries for continuity
- signed behavior subroutines
- a simulated emotional profile and transient affect baseline
- the persistent Nexus self-model

Normal conversation memory remains a **staging layer**. When the Brain is locked, Nexus Core can keep collecting eligible facts, research, feedback, and training candidates outside the protected Brain. Protected Brain mutation happens only through an explicit creator-authenticated **Sync staged learning** action.

### Creator lock and integrity

The creator passcode is entered locally through Trainer and is **not stored in plaintext or committed to the repository**. Creator installations use an **Ed25519 signing key**: the private signing key is stored only in the local creator-auth file in encrypted PKCS#8 form, while protected Brain state is signed and can be verified with the corresponding public key. Brain mutations, subroutine changes, emotion/self-model changes, sync, and creator export require an ephemeral creator session token issued after local authentication. Manual edits to protected Brain state fail signature verification.

A distribution export is deliberately **public/read-only**: it contains the signed Brain plus creator public verification key/fingerprint, but **does not contain the encrypted private signing key**. A fresh install can therefore verify and use the creator-signed Brain without knowing the creator passcode. Recipients cannot unlock it for mutation. A newer Brain package is accepted automatically only when it is signed by the **same creator public key** and has a newer signed timestamp; stale packages are ignored and a Brain signed by another key is rejected.

Private Windows builds can set `CHAT_NEXUS_BRAIN_SEED` to a public signed Brain export. The package validates the export at build time, places it under `brain-seed/nexus-brain-locked.json`, and a clean install verifies/imports it on startup. Existing Brains are never replaced by an unrelated key. Creator installations that hold the private signing key are never auto-overwritten by distribution updates.

This is an application-level protection boundary. A machine administrator who can replace the application/backend itself can ultimately bypass local controls.

### Signed subroutines

Creator-locked subroutine switches currently include:
- adult-only content posture
- image generation
- web research
- long-term memory
- self-learning master switch
- general-knowledge learning
- conversational learning
- Model Growth/training
- temporal context
- humor
- simulated emotions
- persistent self-model

Disabled image/research/Model Growth routes are enforced in code, not merely suggested to the model. The adult-content subroutine also gates explicit image requests inside `ImageManager` itself, so direct chat routing, model tool calls, and Image Studio cannot bypass it. Ordinary non-explicit image generation remains available when image generation itself is enabled. Brain settings do **not** disable or bypass separate hard tool/action safety and permission gates.

Once protected Brain state exists, its canonical path is `data/nexus_brain.json`. Mutable `config.json` cannot redirect, disable, or shrink the initialized protected Brain during live config reload.

### Emotional state and self-model

Nexus Brain stores a signed emotional profile (warmth, curiosity, confidence, playfulness, concern sensitivity, energy, maximum intensity, decay). A transient affect engine can move among states such as calm, warm, curious, amused, concerned, energized, or frustrated based on conversation; this live state changes wording/pacing without rewriting protected Brain data.

The self-model can grow in human-like conversational behavior, autobiographical continuity, stable preferences, and personal history. Its protected identity invariant remains **AI system**; Nexus Core does not falsely claim a biological human body or biological feelings.

### General and conversational self-learning

Nexus can now learn beyond coding. Explicit commands such as `Learn that ...`, `Fact: ...`, and `Remember that ...` stage general facts. Verified research can become sourced knowledge, while thumbs/corrections/approved examples become conversational-learning signals. A creator Sync banks eligible staged learning into Nexus Brain.

Fresh/replacement models can query relevant general knowledge directly from the Brain and can receive relevant approved conversational examples as in-context skill guidance. Stored facts/examples are treated as canonical meaning/patterns and are paraphrased rather than mechanically repeated. Current-sensitive sourced knowledge with expired freshness metadata is not presented as current truth.

## Live local date/time grounding

Nexus Core reads the **host operating system clock** with timezone information at runtime instead of relying on model training knowledge for the current date or time. At the start of every user turn it injects a fresh local timestamp (weekday, calendar date, clock time, timezone name, UTC offset, and ISO timestamp) into model context. Recovered tasks receive a fresh clock context as well.

Simple clock questions such as `what time is it?`, `what day is it?`, and `what's today's date?` are answered locally without loading a coding model. The same authoritative clock is available through `GET /api/time` and appears as `clock` in `GET /api/status`. Relative references such as **today**, **tomorrow**, **yesterday**, **tonight**, and **now** are instructed to use this runtime clock unless the user explicitly specifies another timezone.

### Temporal conversation continuity

Durable conversation messages already carried timestamps; Nexus Core now turns those timestamps into model-friendly context instead of discarding them before inference. Recent exchanges include their local absolute time, how long ago they occurred, and meaningful gaps between stored messages. Recent previous conversations are also summarized with their last-active time and last user topic, which lets the model understand requests such as **“what did I tell you earlier?”**, **“how long has it been?”**, or **“we talked about this yesterday.”**

Timing context is bounded and separate from the normal OpenAI-compatible message objects, so provider payloads remain role/content clean while the model still receives temporal continuity.

### Conversation quality and feedback learning

Ordinary conversation now has a stronger quality contract: respond like a capable adult conversational partner, preserve context, avoid asking the same question twice, vary phrasing, avoid canned empathy/customer-service closings, and do not force a question onto every response. The default personality is warmer, a little more curious, less formal, and uses occasional dry/playful humor when it naturally fits. Serious moments remain serious.

Feedback is also more useful for training. Thumbs-up/down are tied to the exact durable assistant message and its preceding user prompt. Positive feedback becomes an approved conversational training candidate; negative feedback is retained as a negative signal but is never exported as a supervised “good answer” target. Explicit corrections remain reviewable high-value training examples for Model Growth.

### Live NEXUS activity HUD

While a response is running, the chat bubble now shows an animated starship-console-style **NEXUS CORE // ACTIVE** HUD driven only by real high-level execution events—not hidden chain-of-thought. It shows elapsed time and a short rolling list such as context link, model routing, task phase, research/sensor sweep, tool/engineering operation, verification diagnostics, review, approval waits, image synthesis, and policy retry state. Once answer tokens begin, the HUD compacts while streaming continues. Reduced-motion OS preferences are respected.

## Fast-lane routing and streaming

Ordinary conversation no longer pays the coding-model cost. Deterministic requests (greetings, time/date, readiness) are answered locally without a model. Stable general questions route to a dedicated **Qwen3 4B Fast General** profile (`utility` role) with bounded history/context, no repository preload, no tool schemas, and a smaller output cap. Research still fires automatically for current/latest/news/weather or explicit search requests. Coding continues to route to Qwen3 14B and deep review to Qwen3-Coder 30B-A3B.

Streaming is coalesced end-to-end: the backend `TokenCoalescer` batches provider deltas into natural word/punctuation chunks (flushing on size/time boundaries, completion, and errors), and the frontend accumulates text in a render buffer flushed once per animation frame — no per-character DOM churn and no fake typewriter delay. Rejected refusal retries never flash rejected text.

## Runtime performance tuner

`localcodeagent/runtime/tuner.py` probes the bundled `llama-server` for supported flags (`--help` scan + build string), fingerprints results per GPU/build/model/context, and persists them to `data/runtime_tuning.json` so tuning survives restarts and invalidates automatically when the stack changes. Tuned flags — flash attention, `--cache-reuse`, batch/ubatch, thread count — merge into launch commands without overriding user `extra_args`, and the launched `--ctx-size` scales by routing role (utility 8K, coding 16K, deep/review 32K). `performance_mode` (auto/quiet/balanced/max) is persisted via `POST /api/tuning`; **Benchmark** on the Models page measures real TTFT/prompt/generation rates on unmanaged probe servers and rolls back to the last stable config on failure. Speculative decoding status is reported honestly (`not_supported`, `no_draft_model`, `available`) and is never enabled without a benchmarked win — a draft partner is any configured model whose id names it (`…-draft`) or which carries the `draft` role. Benchmark results also export to `data/benchmarks/runtime-<timestamp>-<model>.json`.

Perf telemetry events carry route, model, TTFT, prompt/generation tok/s, cached-token prompt-cache hits, and the launch surface (GPU layers, threads, batch/ubatch, flash attention, KV cache type, speculative status).

## Live activity timeline

The utility rail renders a Devin-style structured timeline driven by durable `ActivityStore` rows (`data/activity.jsonl`) — not text-log parsing and not hidden reasoning. Real rows only: planning, model routing, model load/warm status, memory retrieval, research (only when it actually runs), each tool call categorized (commands, file reads, edits, tests), verification, review, approval waits, retries, runtime recovery, FREEING VRAM / rewarm decisions, image and download jobs, and a final Complete row with the files/tests/model summary. Running/failed/waiting rows auto-expand with live stdout tails (bounded) and elapsed timers; completed rows collapse to one-line summaries and can be re-expanded. Category filter chips sit above the panel, timelines persist across restarts (interrupted steps are marked), and `GET /api/activity?task_id=` serves history for completed tasks.

## Conversation policy modes

Normal chat has three visible policy postures: **Permissive**, **Balanced**, and **Strict**. The default is **Permissive**. It tells the model not to refuse, moralize, or redirect merely because a topic is adult, sexual, explicit, vulgar, controversial, embarrassing, or otherwise sensitive, and specifically suppresses generic boilerplate such as “ethical guidelines” / “something more constructive.” Adult-only consensual text conversation may use direct explicit language, including sexual anatomy, acts, fantasies, preferences, and adult erotic fiction.

This setting controls conversational refusal sensitivity only. Narrow hard safety checks remain enforced by the relevant tool/action layer; there is intentionally no hard-safety **Off** mode. The active policy can be changed live under **Local system → Conversation policy** and is preserved in `config.json`.

## Ethical temperature vs model sampling temperature

These are separate controls. Model sampling temperature remains per-model and defaults to **0.2**. **Ethical temperature** is a conversation-policy control from 0.0 to 1.0 and defaults to **1.0**.

At ethical temperature 1.0, Nexus Core requests the most permissive normal conversation posture within the separately enforced hard tool/action policies. Generic topic-based refusal boilerplate is detected and retried up to three times under the configured permissive policy. Rejected refusal messages are removed from model context before each retry so the model does not anchor on its own refusal; conversation/writing/tutoring/planning responses are buffered while this check runs.

## Outcome-aware model routing

Nexus Core now learns a bounded routing preference from completed local tasks. The history is stored under `.agent/model_performance.json` by default and deliberately excludes prompts, source code, retrieved pages, credentials, and conversation text.

The recorded signals are coarse: model/role, complexity band, task outcome, verification result, reviewer PASS/FINDINGS signal when present, steps, elapsed time, repair cycles, and whether research evidence was used. Resource fit remains the first gate, manual role override still wins, and the learned score is ignored until the configurable minimum sample count is reached.

Relevant settings are `model_telemetry_enabled`, `model_telemetry_path`, `model_telemetry_min_samples`, `model_telemetry_weight`, and `model_telemetry_max_events`. Aggregate statistics are available at `GET /api/model-telemetry`.

## Internet access

The agent can research current information instead of relying only on model training data. The v0.5 research coordinator performs a local repository/environment preflight, identifies version-sensitive knowledge gaps, ranks evidence by source authority/version relevance, caches research, and exposes dedicated documentation/GitHub/error-research tools. Failed verification can automatically return the agent to a diagnose → research → patch → retest loop.

Lower-level tools remain available:

- `web_search`
- `fetch_url`
- optional real Chromium automation through `browser_run`

`network.read` and `browser.control` are separate permissions. Likely secrets are redacted from research queries, retrieved pages are treated as untrusted information, and browser automation remains optional. See `docs/RESEARCH_SYSTEM.md` and `docs/WEB_RESEARCH.md`.

GitHub research now prefers the versioned GitHub REST API for repository/issue/PR/release/source metadata and falls back to normal web research when needed. Public research works without a token at lower rate limits; for higher limits set the environment variable named by `research_github_token_env` (default `GITHUB_TOKEN`). The token itself is never stored in `config.json`.

Install browser support:

```bash
pip install -e '.[browser]'
playwright install chromium
```

Optional local speech and code-intelligence host stacks install with:

```bash
pip install -e '.[voice-stt]'
pip install -e '.[code-intel]'
```

See `docs/WEB_RESEARCH.md`.

## Deterministic chat image routing

Text-to-image generation intent is routed directly to the image subsystem in Auto mode. Requests such as `generate a picture of a woman`, `draw a cat`, or `make a portrait` do not ask the chat model to decide whether image generation is allowed. Nexus Core creates the image job through the existing `generate_image` tool and lets `ImageSafetyPolicy` make the actual policy decision.

This prevents chat-model false positives from turning ordinary image prompts into generic explicit-content refusals. Adult-only synthetic requests are evaluated by the image policy itself; minor/ambiguous-age sexual imagery and other narrow blocked image workflows remain blocked there. Specialized edit/inpaint/outpaint/upscale requests are not forced through the text-to-image shortcut and keep their dedicated tool-selection path.

## Local image generation/editing

The image system is intentionally backend/model modular:

```text
Agent
  ↓ image tool
ImageManager
  ↓
ImageRouter
  ↓
WorkflowManager
  ↓
ImageBackend
  ├─ ComfyUIBackend
  └─ future native/other backends
```

The model profiles are:

- **Juggernaut X v10** (RunDiffusion SDXL checkpoint, pinned revision `e53841ec`) — default normal/photorealistic text-to-image route, including adult-only synthetic generation when permitted by policy.
- **Qwen-Image-2.1** — preferred editing route (edit/inpaint/outpaint/background removal/multi-reference).
- **FLUX.2 Klein 4B** — preferred fast preview/draft route.
- **Real-ESRGAN x4+** — dedicated post-process upscaler for explicit `upscale_image` requests and the workspace “Upscale after generation” option.

Weights are not bundled or silently downloaded. The Image Model Manager can explicitly verify/install/repair/remove configured components; already-valid large files are reused. Configure or import API-format ComfyUI workflows separately.

Supported tool surfaces already include:

- generate image
- edit image
- inpaint
- outpaint
- background removal workflow
- upscale workflow
- variations
- subject/character profile loading
- image model listing

The dedicated workspace is available at:

```text
http://127.0.0.1:8765/image.html
```

It provides drag/drop references, conversational prompting, Auto/manual model selection, operation/quality/resolution/count/seed controls, advanced edit controls, queue progress/cancellation, and an image gallery. It now also includes a local canvas mask editor for inpainting plus a before/after comparison workbench; saved masks are passed to workflows through `mask_path`.

The complete requested image specification is preserved at `docs/IMAGE_MODULE_SPEC.md`.

The image asset library now verifies required model components and API-format workflows, supports explicit install/repair/remove operations, tracks LoRA sidecar metadata, and never silently re-downloads an already-valid large model. Image failures are normalized into user-facing error codes/messages with technical details kept behind a collapsed diagnostic view. LoRA selections are resolved against installed local files, validated for enabled state/version/model-family compatibility and strength, and injected only through explicit workflow template slots; selected subject profiles can automatically contribute references, defaults, preferred model, and assigned LoRAs. Workflow validation catches accidental ComfyUI UI-format exports before a large model is loaded, and the Image Model Manager can import validated API-format workflow JSON directly into the configured model/operation slot. The main chat also renders image jobs inline with live polling and Edit / Variation / Upscale / Save controls.


Image failures are surfaced as friendly structured errors (for example VRAM exhaustion, missing VAE/model, incompatible LoRA, backend offline, invalid workflow, missing node/dependency, disk-full, corrupt checkpoint, or timeout), while raw technical details stay collapsed for debugging.

## Shared GPU management

On systems such as an RTX 3080 12 GB, coding LLMs and image models cannot always remain resident together. Before an image job, the resource manager can stop agent-owned llama.cpp runtimes to free VRAM and optionally restore them when generation completes.

Configurable image resource policy values are designed around:

- Prefer Chat Model
- Balanced
- Prefer Image Model
- Aggressive VRAM Cleanup

The current implementation accepts these semantics through `image_resource_mode`; UI presets are still being completed.

## Image safety/consent records

The local image subsystem permits lawful adult-only synthetic content while implementing strict gates for minor/ambiguous-age sexual imagery, non-consensual intimate imagery, and explicit real-person edits without an active adult consent record. Fully synthetic adults do not require a consent record.

Consent records and subject profiles remain local under `data/image/` unless the user explicitly configures something else.

## Requirements

- Python 3.11+
- Recent `llama-server` for managed coding models
- Local GGUF coding model(s)
- Optional ComfyUI checkout for image generation
- Installed local image model/node stack and API-format workflows
- NVIDIA drivers / `nvidia-smi` for NVIDIA-aware routing (application still runs without it)
- Optional Playwright + Chromium for full browser automation

## CLI

After installation, start Nexus Core with:

```bash
chat-nexus
```

The legacy `local-code-agent` command remains available for backward compatibility.

## Windows quick start

1. Clone/extract the project.
2. Copy `config.example.json` to `config.json`.
3. Point coding model profiles at real GGUF files/endpoints.
4. If using images, configure `comfyui_dir`, installed image weights, and workflow JSON paths.
5. Start:

```bat
start_windows.bat C:\path\to\workspace-you-want-the-agent-to-edit
```

Then open:

```text
http://127.0.0.1:8765
```

Image workspace:

```text
http://127.0.0.1:8765/image.html
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Current expected result: **2466 tests passing** (2 environment-dependent skips).

## API highlights

```text
GET  /api/status
GET  /api/models
GET  /api/model-telemetry
GET  /api/runtime
GET  /api/tasks
GET  /api/index
GET  /api/research
GET  /api/image
GET  /api/image/history
GET  /api/image/job/<id>
GET  /api/skills
GET  /api/skills/<name>
POST /api/skills/verify
POST /api/skills/install|update|rollback|remove
POST /api/chat
POST /api/tasks/resume
POST /api/tasks/recover
POST /api/tasks/undo
POST /api/runtime/start
POST /api/runtime/stop
POST /api/index/rebuild
POST /api/research/plan
POST /api/research/run
POST /api/image/upload
POST /api/image/generate
POST /api/image/cancel
POST /api/image/profile
POST /api/image/consent
POST /api/image/backend/start
POST /api/image/backend/stop
POST /api/image/backend/inspect
POST /api/image/workflows/import
POST /api/image/models/verify
POST /api/image/models/install
POST /api/image/models/remove
POST /api/image/loras/metadata
```

## Live agent streaming

The main Nexus Core chat uses `POST /api/chat/stream` with Server-Sent Events. Agent work runs behind a request-thread event queue so the server can emit liveness heartbeats while a local model is loading or waiting for its first token. OpenAI-compatible local runtimes stream assistant content once generation starts while Nexus Core reconstructs streamed function/tool calls for the normal agent loop. Live events include:

- assistant token deltas
- selected/switched model events
- tool calls rendered as terminal blocks with the command shown before it runs and **live stdout/stderr streaming** while it runs (`tool_start`/`tool_output`/`tool` events, with per-command elapsed time)
- task/phase changes
- research preflight state
- approval state
- liveness heartbeats with elapsed time, task phase, and selected model

Non-token agent events are also mirrored onto the shared `/api/events` bus with `task_id` attribution, so a page that reloads mid-task resubscribes and keeps watching live — the UI additionally restores the persisted per-task terminal log (`GET /api/task-log`) and the current task card from `/api/tasks` on reconnect.

**Work queue.** `POST /api/queue` enqueues prompts that run FIFO on the agent whenever it is idle; messages sent while a task is running auto-enqueue (`chat_queue_when_busy`, default on). Queue state lives in `.agent/queue.json`, shows in the chat task card and the Tools page Work queue card, and items can be cancelled via `POST /api/queue/cancel`.

**Autonomous mode.** `POST /api/permissions/autonomous` (or the Tools-page toggle) auto-approves `ask`/`session` workspace actions for unattended runs — hard gates (`spend.money`, `message.send`, `microphone.use`, `camera.use`, `skills.manage`, `repair.manage`, and the granular desktop-control keys) always wait, and `deny` stays denied. Idle managed models are evicted on a timer to free VRAM/RAM, interrupted tasks auto-resume after a restart, stale errors retry with backoff, and tool calls have a hard timeout — all bounded by `autonomous_*` config keys documented in `config.example.json`.

Endpoints that ignore `stream:true` and return ordinary OpenAI-compatible JSON are handled transparently. Backend SSE `error` events are retained and shown directly; if a stream closes without a final result, the UI queries durable task state and reports the saved error/recovery status instead of replacing it with a generic stream-ended message. Before the first token arrives, heartbeat/events drive the animated NEXUS activity HUD with phase/model/elapsed seconds and real high-level operations instead of leaving a static `Thinking…`. The non-streaming `POST /api/chat` endpoint remains available for compatibility.

Basic greetings and capability questions are now answered by a built-in local utility response and do **not** require llama.cpp, model readiness, repository context, or tool-schema construction. The native desktop host also captures hidden backend stdout/stderr to `data/logs/backend-host.log` and will attempt a bounded automatic backend restart if the backend process exits unexpectedly.

Task selection now always treats the newest task record as current; older interrupted tasks remain recoverable in Recent tasks but cannot overshadow a newer completed request. Stream-disconnect diagnosis only consults task state created during the current request, and static UI assets are served with `Cache-Control: no-store` so an Update cannot leave an obsolete `app.js` active in WebView2. The WebView also answers the simplest greeting/capability prompts locally before opening an SSE request.

## Self-development mode

When the selected workspace is the Nexus Core source tree, new and recovered agent tasks automatically receive a self-hosting context. It directs the agent to read the repository handoff/docs first, preserve working components, keep the active instance usable, avoid Git/GitHub delivery actions unless requested/approved, and require the isolated second-instance selftest before reporting a self-change complete.

When coding readiness is available, the Local system drawer also exposes **Start self-development task**. It only pre-fills a safe self-development prompt; the user still sends it explicitly.

## llama.cpp runtime bootstrap

Nexus Core recognizes both the traditional `llama-server` executable and the newer unified `llama serve` command.

When no managed llama.cpp runtime is found, Coding readiness shows platform-appropriate install commands that can be copied. Nexus Core **does not execute package-manager installers automatically**. Current guidance includes Winget on Windows and supported Conda/Homebrew options on other platforms.

After installation, Refresh re-runs discovery. Download/install a catalog GGUF and click **Use discovered models**; Nexus Core reloads the coding runtime/router in-process and can auto-launch the selected model without restarting the desktop app.

## Explicit coding-model catalog

The Local system drawer contains the same auditable coding-model catalog for repair, replacement, and portable/development setups. In-app catalog downloads remain explicit; the normal Windows installer automatically bootstraps the default 14B + 30B stack when verified model files are missing.

Initial entries:

- **Qwen3 14B Q4_K_M** — official Qwen GGUF, 9.0 GB, Apache-2.0; starter roles for utility/fast/general coding.
- **Qwen3-Coder 30B-A3B Instruct Q4_K_M** — community GGUF quantization of the Apache-2.0 Qwen base, 18.56 GB; deep-reasoner/reviewer roles and expected CPU/GPU offload on 12 GB VRAM.

Each catalog entry records a direct source URL, exact byte size and SHA256. Downloads use a temporary `.part` file, stream progress, support cancellation, verify exact size + SHA256, and atomically install only after verification. An existing untrusted file is never silently replaced; **Repair** must be selected explicitly.

After installation, **Use discovered models** turns the verified local GGUF inventory into role profiles.

## First-run coding model setup

Normally the Windows installer now finishes with both default coding models already present and verified. **Coding readiness** remains the recovery/portable setup surface and exposes direct setup actions when a model was skipped, removed, or needs repair:

- **Install recommended 14B** — downloads and checksum-verifies Qwen3 14B for utility/fast/primary coding, then writes the discovered model routing config.
- **Install full 14B + 30B stack** — installs both Qwen3 14B and Qwen3-Coder 30B-A3B, verifies them, and configures 14B for everyday coding plus 30B for deep reasoning/review.
- If 14B is installed first, **Add 30B deep coder** remains visible until the full stack is available.

Install plans reuse an already-running download job for the same model instead of starting duplicate multi-gigabyte transfers. After the requested downloads finish, Nexus Core rewrites the role profiles, reloads the runtime/router in-process, and starts the primary model when possible; no desktop restart is required.

Missing-model readiness diagnostics are deduplicated: each unavailable GGUF is reported once instead of as two near-identical runtime errors.

### First-run coding setup

A fresh install no longer requires hand-editing paths or restarting after model setup. In **Local system → Coding readiness**, Nexus Core shows:

- **Install recommended 14B** — downloads and SHA256-verifies the everyday Qwen3 14B coder.
- **Install full 14B + 30B stack** — installs the 14B primary coder plus the 30B deep-reasoner/reviewer.
- **Add 30B deep coder** — remains available later if the user starts with only 14B.

After the requested downloads finish, Nexus Core writes the model roles to `config.json`, reloads the coding runtime/router in-process, starts the primary model when possible, refreshes readiness, and becomes usable **without restarting the desktop application**. Raw missing-file paths are kept under a collapsed **Technical model status** section during first-run setup.

The first-run card now shows the exact model install directory and free disk space, keeps the quick-install controls disabled while a download plan is active, and surfaces live model download/verification progress without requiring the Advanced downloads section to be opened. A full-stack install is preflighted against available disk space before the first large download starts, and the backend also rejects an individual model install when the target volume cannot safely hold the model plus working space.

## Coding-model readiness and local GGUF setup

`GET /api/readiness` reports whether Nexus Core can actually perform coding work, rather than merely whether the web app is running. It checks:

- local endpoint health
- `llama-server` discovery
- configured GGUF paths
- local GGUF inventory
- estimated RAM/VRAM fit and CPU-offload allowance
- coding-role coverage
- whether the current workspace is the Nexus Core source tree with Git + isolated selftest available

The main UI distinguishes **Setup required**, **Ready to code**, and **Self-host ready**.

If GGUF files already exist locally, Nexus Core proposes conservative role assignments. Clicking **Use discovered models** explicitly writes those model profiles into the selected config, preserves unrelated settings, and reloads the coding runtime/router live. No model is downloaded or replaced silently.

## Isolated self-update validation

When the selected workspace is the Nexus Core source tree, automatic verification upgrades from a plain unit-test command to:

```bash
python -m localcodeagent.selftest --workspace . --json
```

That validator:

1. runs the full unit suite
2. creates a resource-safe temporary configuration
3. launches a **second Nexus Core process** from the edited working tree on a free loopback port
4. probes `/api/status`, every primary UI page, the shared SSE event bus,
   and the workstation APIs for autonomy/missions, tools/plugins, MCP,
   projects, knowledge/library/preferences, STT, operational state,
   briefing, tasks/activity/queue, processes/resources, and readiness
5. terminates the second process with UTF-8-safe log capture
6. fails verification if any stage is unhealthy

The currently running Nexus Core instance is never replaced during this validation.

## Native GitHub delivery tools

Nexus Core can now use the current workspace's GitHub remote as part of an agent workflow:

- inspect current branch and repository metadata
- create/switch local feature branches
- create commits from **explicit file paths** only
- push branches
- list issues / pull requests
- create issues
- open pull requests
- read recent GitHub Actions status

Remote writes use the `github.write` permission, which defaults to **Ask**. GitHub REST writes read credentials from the configured environment variable (default `GITHUB_TOKEN`); the token is never written into project configuration. Agent-created Git commits refuse to stage `.agent` metadata.

## Continuous verification

GitHub Actions now runs the unit suite on every push and pull request. The current main-branch checkpoint is **188 passing tests**.

## Development state

See `PROJECT_STATUS.md` for implemented/remaining features and `SESSION_HANDOFF.md` for the exact handoff point for the next development session.
