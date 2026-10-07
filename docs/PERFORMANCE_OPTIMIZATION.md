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

## Measured results (installed build)

| Metric | Before | After |
|---|---|---|
| Chatterbox resident VRAM | ~3.3 GB fp32, held forever | **2290 MB bf16, releases at 120 s idle** |
| Chatterbox warm RTF | 0.62 | 0.39 |
| Cold TTFA | 6.9 s | 11.7 s (includes full 8.3 s cold load — the idle-unload trade-off) |
| Idle VRAM (llama + desktop, voice unloaded) | ~4.5 GB + 3.3 GB silent tax | **~4.5 GB, voice at 0** |
| Per-message nvidia-smi spawn | 1/msg | 0 (15 s cache) |
| Backend RAM over 30 turns | — | +339 MB then **flat at ~641 MB** |
| Mean simple-chat turn | — | 1.15 s (voice enabled) |
| Idle process count | 3 | 3 (NexusCore, backend, llama-server) |

## Verified live on the installed build
- Engine `loaded:false, worker_alive:false` 120 s after last speech.
- VRAM drops ~2.3 GB when the worker exits.
- Status polls no longer extend the idle lease.
- Chat works while voice is cold; voice works while llama is resident
  (7.9 GB total during synth — fits the 12 GB card).
- No InvokeAI/ComfyUI processes at idle; on-demand only.

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
- Startup ~15 s backend boot is dominated by PyInstaller unpack +
  subsystem init; not yet stage-profiled.
- 50-turn soak and a full image-generation VRAM round-trip were not run
  end-to-end on the install; 30-turn chat soak was flat.
