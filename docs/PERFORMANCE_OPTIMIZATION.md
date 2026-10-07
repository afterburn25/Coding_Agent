# Performance Optimization — Nexus Core

Milestone: "load only what is needed, when it is needed; reuse it
intelligently; release it when it is no longer worth keeping resident."

Baseline and measurement method: `docs/PERFORMANCE_BASELINE.md`
(installed build at `D:\Nexus_Core`, RTX 3080 Ti 12 GB / 64 GB RAM,
`scripts/benchmark.py`).

## What was wrong

| # | Bottleneck | Root cause |
|---|---|---|
| 1 | Generation dropped to **3.5 tok/s** (from ~165) | Chatterbox (~3.3 GB) + a duplicate InvokeAI pair stayed resident → llama.cpp WDDM-paged mid-generation |
| 2 | ~12 console flashes per typed message | `refresh_hardware()` spawned `nvidia-smi` on **every** chat turn; spawns lacked `CREATE_NO_WINDOW` |
| 3 | Chatterbox never unloaded | `_unload_idle_voice_engine` matched `eng._model` (Kokoro only); the worker engine tracks `_proc` — never matched |
| 4 | Chatterbox loaded on every message | `begin_task` prewarmed the engine before any speakable text existed |
| 5 | Idle lease never expired | `status`/`capabilities` worker calls bumped `_last_used` — any UI status poll reset the 120 s timer forever |
| 6 | InvokeAI resident at boot | installed config had `invokeai_auto_start: true` |
| 7 | No reverse-direction VRAM reclaim | image jobs could evict LLMs, but an LLM launch could not release idle image/voice consumers |

## What changed

### Voice lifecycle (largest win)
- `localcodeagent/voice/chatterbox.py` — T3 weights load **bf16**
  (~2290 MB allocated vs ~3.3 GB fp32, RTF unchanged ≈0.3–0.4);
  s3gen/ve stay fp32 for fidelity. `dtype` is a config option.
- `_request(..., touch=)` — only `load`/`prepare_voice`/`synthesize`
  extend the idle lease; `status`/`capabilities` are introspection and
  no longer reset the timer.
- `load()` stamps `_last_used`.
- `server._unload_idle_voice_engine` covers worker engines
  (`_loaded` + `_proc`), adds a **GPU-engine idle floor**
  (`voice_gpu_idle_unload_seconds`, default 120 s) vs the CPU-engine
  600 s, unloads under VRAM pressure (`min_free_vram_mb`), and reads
  hardware through the **15 s cached** `runtime.fresh_hardware()`.
- `VoiceManager.enqueue` warms the engine only when the utterance is
  a **cache miss** (`_cache_probe` shares `_synth_key` with
  `_synthesize`, so the probe can't drift from the real key).
  `begin_task` no longer warms — verified live: engine stays cold
  until speech actually enqueues, then self-unloads at 120 s idle.

### VRAM brokerage (RuntimeManager extension)
- `RuntimeManager.vram_releasers` — app-registered callbacks invoked by
  `_enforce_residency` when an incoming launch is short on VRAM,
  **before** evicting a resident keep_loaded model.
- `AppState._release_idle_gpu_consumers` — stops managed
  InvokeAI/ComfyUI (no active image jobs) and unloads idle CUDA voice
  engines (skipped while `_active_job`/queue is busy — never cuts
  mid-utterance).
- `_enforce_residency` no longer returns early when no other LLM is
  resident — external reclaim runs whenever the target is short.

### Per-message hot path
- `orchestrator`: `refresh_hardware()` → cached `fresh_hardware()`
  (one nvidia-smi spawn per message eliminated).
- Pipeline order verified: slash/local commands → research follow-ups →
  staged answer-memory (exact → alias → semantic → FTS) → model routing.
  Cached-answer hits never touch the model.

### Process / polling hygiene
- All subprocess spawns funnel through `procutil` `CREATE_NO_WINDOW`
  (deployed) — the per-message console flashes are gone.
- `web/task_bar.js`: 4 s → 15 s poll (EventSource already gives instant
  updates; the poll is a fallback).
- Installed config: `invokeai_auto_start` → false; image backends are
  on-demand (`/api/image/backend/*` confirmed: nothing on :8188 at idle)
  and idle-evict at 900 s.

### Startup (second pass — measured with `[nexus-init]` step timers)
Boot showed a **9.4 s gap** between the 94% and 98% markers, all inside
`AppState.__init__`'s tail. Step timing pinned it to
`_start_provisioning` — `ProvisioningManager.__init__` → `_inventory()`
re-verified every completed item **synchronously, before the socket
bound**:

- `chatterbox_runtime` → `runtime_status` spawned the embedded Python
  and **imported torch+chatterbox** (~5 s)
- `tool` items → `self._tool_installed` called
  `tools.refresh_install_status()` — a **full-registry** manifest probe
  (shutil.which + install-root dir scans) once **per item**
- `chatterbox_model` → `model_ready` **sha256'd ~3.3 GB of weights**
- `voice-assets` → `asset_status` hashed ~400 MB of ONNX assets

Fixes (all measured, semantics preserved):
- `runtime_status(..., deep=False)` — marker + `python.exe` +
  site-packages dirs; the deep import probe still runs inside
  `ensure_runtime` before the marker is written and stays the default
  for explicit verification callers.
- `refresh_install_status(name)` — scoped single-manifest refresh;
  `_tool_installed` probes only its own manifest.
- `verified` caches (`.nexus-model-verified.json`,
  `.nexus-assets-verified.json`) — a file whose (size, mtime_ns, sha256)
  signature matches a previously-hashed entry reports verified without
  re-hashing; any changed/unknown file is still hashed honestly.
- Permanent `[nexus-init] <step> <ms>` boot-stage lines + per-item
  `inventory/<id>` lines when a probe exceeds 100 ms — init regressions
  are now diagnosable from `backend-host.log` alone.

Measured: provisioning init **8350 ms → 65 ms**; AppState tail
**9.9 s → 1.4 s**; `backend_health` **15.3 s → 4.3 s**; total startup
**~21.4 s → ~7.6 s** (installed build, warm disk cache).

### `/api/status` hot path (third pass — `[nexus-slow]` section timers)
The endpoint aggregates ~10 subsystem summaries and doubles as the
host's boot health probe (polled every 150 ms) and the frontend's
post-turn refresh. Section timing showed **`images.summary()` =
~800 ms steady, ~2.8 s periodic** — every call ran a live HTTP health
`probe()` per image backend, and on this machine a connect to a dead
localhost port takes ~2 s before being refused (filter/AV quirk), so
each probe burned its full timeout.

Fixes:
- `ImageManager.summary` caches the two `probe()` results for **10 s**
  (`_SUMMARY_PROBE_TTL`), bypassed whenever a backend `_process` is
  active so job state transitions stay fresh.
- The two probes run **in parallel** (independent objects) — serialized
  dead-endpoint timeouts no longer stack.
- InvokeAI `_health_uncached` bounds its probe at **1 s** (was the full
  4 s request timeout); ComfyUI already caps at 0.75 s.
- `/api/status` emits `[nexus-slow] <section> <ms>` lines when total
  exceeds 500 ms — same always-on diagnostics as `[nexus-init]`.

Measured: steady-state `/api/status` **~800 ms → ~20 ms** (cached
probes); first/boot call 2.8 s → ~1.05 s.

## Measured results (installed build)

| Metric | Before | After |
|---|---|---|
| Chatterbox resident VRAM | ~3.3 GB fp32, held forever | **2290 MB bf16, releases at 120 s idle** |
| Chatterbox warm RTF | 0.62 | 0.39 |
| Cold TTFA | 6.9 s | 11.7 s (includes full 8.3 s cold load — the idle-unload trade-off) |
| Idle VRAM (llama + desktop, voice unloaded) | ~4.5 GB + 3.3 GB silent tax | **~4.5 GB, voice at 0** |
| Per-message nvidia-smi spawn | 1/msg | 0 (15 s cache) |
| Backend RAM over 30 turns | — | +339 MB then **flat at ~641 MB** |
| Backend RAM over 50 turns | — | warm-up to ~6.1 GB (llama ctx + voice), then **flat through turn 50** |
| Mean simple-chat turn | — | 1.15 s (voice enabled) |
| Idle process count | 3 | 3 (NexusCore, backend, llama-server) |
| Startup: provisioning init | ~8.4 s | **65 ms** |
| Startup: /api/status steady-state | ~800 ms | **~20 ms** (10 s probe cache) |
| Startup: backend_health | ~15.3 s | **~4.3 s** |
| Startup: total to ready | ~21.4 s | **~7.6 s** |

## Verified live on the installed build
- Engine `loaded:false, worker_alive:false` 120 s after last speech.
- VRAM drops ~2.3 GB when the worker exits.
- Status polls no longer extend the idle lease.
- Chat works while voice is cold; voice works while llama is resident
  (7.9 GB total during synth — fits the 12 GB card).
- No InvokeAI/ComfyUI processes at idle; on-demand only.
- **Image round-trip verified end-to-end**: a 512² text-to-image job
  auto-started InvokeAI, generated (real PNG on disk), and released —
  `vram_before 4.1 GB → vram_after 7.34 GB`, the ~20 GB model process
  exited, llama-server stayed resident throughout.

## Not changed (measured first — judged acceptable)
- Runtime auto-tune: fingerprint-gated, once per boot, waits for an idle
  agent lane, sleeps 10 min between passes.
- `avatar.js` 200 ms tick: DOM-only, no network.
- `operational_state()`/`/api/nexus/state` 20 s poll: doesn't touch
  voice/hardware probes.
- Learning (`learn_from_user`): synchronous but heuristic — no LLM call.
- Repository index: cached after first build; no per-message rescan.

## Remaining known costs
- Cold TTFA is higher by design (~8–10 s worker spawn + model load);
  mitigations would be pre-warm on voice toggle or a smaller warm pool —
  deliberately **not** done (that's the residency tax this milestone
  removed).
- Startup residual (~7.6 s total): ~1.5 s frozen-exe spawn → first
  marker (PyInstaller + imports), ~1.4 s subsystem init (boot 4→94%),
  ~1 s first status probe, ~2.4 s WebView2 interface_ready.
- Machine quirk: refused localhost connects take ~2 s here (filter/AV).
  Dead image-backend probes are bounded (≤1 s, parallel, 10 s-cached)
  so the quirk no longer taxes boot or status polls.
- 50-turn soak: **done** — flat at ~6.1 GB; no growth turns 20–50.
- Image generation runs mostly through WDDM shared memory on a busy
  12 GB card — slow but correct, and memory returns afterward.
