# Performance Baseline — Nexus Core

Measured on the **installed build** at `D:\Nexus_Core` (RTX 3080 Ti 12 GB /
64 GB RAM), via `scripts/benchmark.py`. Re-run with:

```
python scripts/benchmark.py baseline     # full suite (idle/chat/voice/memory)
python scripts/benchmark.py idle|chat|voice|memory|processes
```

Results are written to `docs/benchmark-*.json`.

## Baseline: v0.26.1 + post-release perf fixes (2026-10-07)

The baseline was captured *after* the first round of fixes (console-window
suppression, Chatterbox idle/VRAM-pressure unload, InvokeAI auto-start off).

### Startup (warm machine, llama-server prewarm daemon)

| Metric | Value |
|---|---|
| Cold boot → backend `/api/status` ready | 14.8 s |
| Cold boot → first chat answer | 18.7 s |
| Restart → backend ready (2nd run) | 14.7–17.1 s |
| Processes at idle | 3 (NexusCore, backend, llama-server) |
| Idle VRAM — desktop only | 0.9 GB |
| Idle VRAM — app resident | ~4.5 GB |
| Idle RAM — full tree | ~3.4 GB |
| Idle CPU | ~0.4 % total |

### Chat (installed backend, local qwen3-4b)

| Probe | TTFT | Total |
|---|---|---|
| Simple ("2+2") | 1.66 s | 1.0 s |
| Normal (541-char answer) | 0.8 s | 2.1–2.3 s |
| Simple turn, steady state | — | ~1.0 s |

llama.cpp reference throughput on this GPU: ~165 tok/s healthy; 3.5 tok/s
was observed under WDDM paging with Chatterbox + duplicate InvokeAI resident
(pre-fix evidence of the contention class this milestone targets).

### Voice (Chatterbox Turbo, active engine)

| Metric | Before fixes | After |
|---|---|---|
| Cold TTFA (model load + synth) | 6.9 s | — |
| Warm TTFA | 1.18 s | — |
| Warm RTF | 0.62 | 0.28–0.31 (bf16) |
| Resident VRAM (torch allocated) | ~3.3 GB fp32 | ~2.3 GB bf16 |
| Idle unload | none — held forever | 120 s (GPU) / VRAM-pressure |
| Load trigger | every task begin | first speakable sentence only |

### Memory soak (10 turns, simple prompts)

| Metric | Value |
|---|---|
| RAM at turn 0 | 5.89 GB |
| RAM at turn 10 | 5.92 GB (+34 MB) |
| Turn latency | ~1.0 s each |

Stable — no growth observed at 10 turns.

## Regressions found during measurement

| Symptom | Root cause | Fix |
|---|---|---|
| 3.5 tok/s generation | Chatterbox + duplicate InvokeAI resident → WDDM paging | idle/pressure unload + `invokeai_auto_start:false` |
| ~12 console flashes per message | `refresh_hardware()` → `nvidia-smi` on every chat turn | cached `fresh_hardware(15s)`; all spawns `CREATE_NO_WINDOW` |
| Chatterbox 3.3 GB forever | engine tracked `_proc` not `_model` — never matched idle-unload | per-engine unload incl. GPU floor (120 s) |
| Chatterbox loads at every msg | `begin_task` prewarmed unconditionally | warm only when first sentence is enqueued |
| Backend CPU 0.84% idle | watchdog + probes | acceptable; nvidia-smi now cached |

## Known remaining costs (not yet measured/fixed)

- Startup ~15 s is dominated by backend boot (PyInstaller unpack, model
  autodetect, subsystem init) — not yet profiled stage-by-stage.
- `avatar.js` 200 ms tick + `/api/nexus/state` 20 s poll — light but
  always-on while the main page is open.
- `task_bar` polls `/api/tasks` every 15 s (was 4 s) as an EventSource
  fallback.
- No long-session (50-turn) or image-generation VRAM round-trip measured
  yet — `memory`, `voice` subcommands are reusable for that.
