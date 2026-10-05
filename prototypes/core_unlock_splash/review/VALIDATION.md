# Prototype verification

Verified on Windows on 2026-10-04. Production startup was not launched or modified.
Normal startup remains 12.6 seconds with its full-power readiness hold. Failure
now lasts **5.6 seconds**, including roughly 2.5 seconds of visible instability
before power collapse. The iris starts closing at 3.5 seconds. Recovery work is
independent of cosmetic timing; immediate controls and Safe Mode bypass it.

## Results

- **46 Node tests and 7 Python tests passed.** Coverage includes UTF-8 assets, unchanged normal
  transforms/audio, every startup phase as a fault origin, exact snapshot
  continuity, monotonic half-open iris closure, sequence/pin order, final lock,
  supplied attempt counts, duplicate errors, recovery success with closed
  readiness gates, rollback, Safe Mode, pause/replay, missing stems/devices,
  muted/silent audio, outgoing gain ramps, loop seams, hashes and mix headroom.
- **Release build and Windows x64 publish passed**, with zero warnings.
- **14 native normal-playback checks passed**, with and without sound. The
  charged hum/core hold, progress/text separation and normal ONLINE state remain
  functional. Silent playback created no AudioContext and decoded no stems.
- **26 native failure/recovery checks passed**, with and without sound. Faults
  were injected at normal times 0, 1.9, 2.8, 5.15, 7.8 and 11.79 seconds. The
  real DOM, Canvas and Web Audio confirmed continuity, visible instability,
  frozen progress, containment, recovery panel, actual audio output, mute,
  supplied counters, all six recovery states and successful reauthorization.
- **Renderer exception and stalled presentation checks passed.** Both exposed
  native static recovery controls. The independent watchdog is six seconds;
  repeated errors cannot extend it. These deliberate faults exit with 1 as
  expected. Static mode and missing-manifest recovery checks also passed.
- The native fallback was additionally inspected through the visible Windows
  UI and accessibility tree: its Details, Open Log, Retry, Rollback, Safe Mode
  and Exit controls remained available independently of WebView2.
- Compared the previous revision: the normal manifest phases/events/gates are
  semantically identical; all original 16 WAV stems are byte-identical; locked,
  full-power hold and online captures are byte-identical. Source art is unchanged.
- Visually inspected instability, emergency iris closure, contained state,
  recovery panel and Safe Mode. The panel leaves the original wordmark visible.

`playback-report.json` and `silent-report.json` record the final normal path.
`failure-report.json` records the **published Windows x64** failure/recovery
preview; `failure-silent-report.json` records its silent counterpart. All reports
use the extended 5.6-second failure sequence. Fallback reports record the native
exception, six-second deadline, static mode and missing-manifest exercises.
Process IDs were removed from saved reports.

## Measured performance and limits

The renderer uses a fixed 1024x576 backing surface. Mechanical and light sprites
are cached; procedural noise is computed once. Cinematic motion targets 60 Hz.
The contained diagnostic scanner runs at 15 Hz, and Safe Mode is static.

| Metric | Normal preview | Published failure/recovery test |
| --- | ---: | ---: |
| Test duration | 16.58 s | 13.13 s |
| Median / p95 frame interval | 16.7 / 16.8 ms | Mixed cinematic and idle states; no aggregate FPS claim |
| Median / p95 draw submission | 0.3 / 0.4 ms | Not separately sampled |
| Renderer initialization | 2473 ms | 610 ms |
| Host / browser CPU | 0.063 / 4.297 CPU-seconds | 0.125 / 3.844 CPU-seconds |
| Host / aggregate browser working set | 60.4 / 474.4 MiB | 65.1 / 482.4 MiB |
| Decoded stems | 28 | 28 |
| Digital output peak at default volume | 0.277 full scale | 0.294 full scale |

Initialization varies between launches; native static artwork covers preparation.
CPU averages roughly 0.26–0.30 of one logical core across these test processes,
not a machine-wide percentage. Summed working sets include shared pages and are
not private committed memory. Draw timing measures command submission, not GPU
completion. The normal report retains the most recent 600 frame samples.

Offline nominal normal mix peaks at 0.712 full scale at master 100% (-9.89 dBFS
at default 45%). Standalone failure peaks at 0.573; conservative mixes at nine
interruption times never exceed 0.712. They include all failure cues even when
the runtime would suppress motion sounds for already locked components. No
clipping was detected. Digital measurements verify signal scheduling/output,
not calibrated acoustic loudness from physical speakers.

These are desktop observations, not a guarantee for integrated GPUs, RDP,
battery operation or simultaneous model initialization/diagnostics/rollback.
WebView2 has a substantial process/memory baseline. Devin must reuse the existing
environment where practical and profile the integrated application on target
hardware. Actual recovery workers and final overlapping-window transitions
remain integration work; they are not implemented by this prototype.

## Captured states

| Timeline | Seconds | Preview |
| --- | ---: | --- |
| Normal | 0.00 | [Sealed](01-locked.png) |
| Normal | 2.25 | [Authorized](02-authorized.png) |
| Normal | 3.80 | [Pins released](03-pins.png) |
| Normal | 5.15 | [Iris opening](04-opening.png) |
| Normal | 7.80 | [Core charging](05-core.png) |
| Normal | 10.50 | [Full-power hold](08-charged-hold.png) |
| Normal | 12.60 | [Online](06-online.png) / [reduced motion](07-reduced.png) |
| Failure, from normal 9.60 | 1.50 | [Visible instability](09-instability.png) |
| Failure, from normal 9.60 | 3.18 | [Power collapse](10-power-drop.png) |
| Failure, from normal 9.60 | 3.78 | [Emergency iris closure](11-emergency-closure.png) |
| Failure, from normal 9.60 | 5.25 | [Fault contained](12-contained.png) |
| Failure, from normal 9.60 | 5.60 | [Recovery](13-recovery.png) / [rollback](14-rollback.png) |
| Recovery | Immediate | [Safe Mode](15-safe-mode.png) |
| Recovery | 5.60 | [Supplied repair counter](16-repair-attempt.png) |
| Failure, from normal 5.15 | 0.01 | [Half-open origin preserved](17-half-open-fault.png) |
| Failure, reduced motion | 5.60 | [Reduced-motion recovery](18-reduced-fault.png) |

Reproduce with `./tools/verify.ps1 -Native` from the prototype folder. The script
does not start Nexus, contact a backend, change global audio settings or alter
production startup. Fault tests use a disposable distribution copy; no source
assets are deleted.

## Full-core glow follow-up — 2026-10-05

The sphere and its plasma/light layers now expand with charge to fill the
reactor aperture. Updated captures 05–10 show charging, online, reduced motion,
full-power hold, instability and power collapse. The other twelve captures
remain byte-identical, including closed mechanics and contained recovery.

Validation rerun for this renderer-only change: all 46 Node tests passed;
Release build and Windows x64 publish succeeded with no warnings or errors;
native silent normal playback passed 14 checks and native silent failure/recovery
passed 26 checks. Online and instability captures were visually reviewed. Audio
sources, timing, gates and host behavior were not edited; audible playback and
the full fallback matrix were not rerun for this visual change.
