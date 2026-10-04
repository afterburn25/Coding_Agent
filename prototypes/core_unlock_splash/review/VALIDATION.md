# Prototype verification

Verified on Windows on 2026-10-04. Production startup was not launched or modified.
The final sequence is 12.6 seconds: a four-second charge followed by a full-power
plateau, with an unbounded charged-hum/bright-core hold when readiness is delayed.

## Results

- **28 Node tests passed**: deterministic transforms, phase and event ordering,
  gates, long waits, idle motion, replay/pause/speed/seek, sound scheduling, missing
  stems, unavailable devices, silent mode, mute before initialization, and charged
  hum persistence/release.
- **5 Python tests passed**: all sound formats and headroom, loop seam slopes,
  asset hashes, byte-identical production art, and standalone host isolation.
- **.NET Release build and Windows x64 publish passed**, with zero warnings.
- **14 native playback checks passed**, including the actual Web Audio output
  path and loading text/progress independent from animation time. All 16 stems
  decoded, output was nonzero and unclipped, and a delayed readiness gate kept
  the charged hum audible and the core at full brightness.
- **Silent native playback passed**: zero decoded stems, no AudioContext, zero
  output, and animation completed normally.
- **Static recovery and missing-manifest exercises passed**: the native artwork
  fallback remained available; a missing manifest returned the expected test
  failure status rather than aborting without a splash.
- Reviewed the visible native controls and captured locked, authorized, pin
  release, opening, charging, full-power hold, online and reduced-motion states.
  The wordmark stays unchanged and the six iris leaves keep their geometry.
- The reference-lighting revision adds a brighter core and orbitals, blue
  chamber haze, platform/floor light, and deeper mechanical recess shadows.
  The full-power hold retains this lighting without repeating the online pulse.

`playback-report.json` records the updated Release preview with audio enabled.
`silent-report.json` records the corresponding published Windows x64 preview
with audio disabled. Both include the reference-lighting revision and the
mute-before-Play fix. The fallback reports record the previously verified,
unchanged injected-fault and static recovery routes. Process IDs were removed
from saved reports.

## Measured performance and limits

The final renderer uses a fixed **1024×576** backing surface, matching production
artwork. Mechanical textures and light sprites are cached at 2×; the procedural
nebula density field is computed once and cached at 384×384. Unchanged accessibility labels
and status text are no longer rewritten every frame; the bar uses a transform
instead of relayout. That reduced measured host/browser CPU cost substantially.

| Metric | Updated Release preview measurement |
| --- | ---: |
| Median frame interval | 16.7 ms (about 60 FPS) |
| 95th-percentile frame interval | 16.8 ms |
| Median / 95th-percentile draw submission | 0.3 / 0.4 ms |
| Renderer initialization | 565 ms |
| Host CPU / WebView2 CPU during 16.57 s test | 0.094 / 3.641 CPU-seconds |
| Host / aggregate WebView2 working set | 63.0 / 459.8 MiB |
| Active decoded stems | 16 |
| Playback measured digital peak, default volume | 0.277 full scale |
| Offline nominal mix peak, master at 100% | 0.712 full scale |
| Offline nominal mix peak, default 45% | -9.89 dBFS |

The published silent preview also measured a 16.7 ms median frame interval and
16.8 ms p95, with 0.3 / 0.5 ms draw submission and 2.47 s initialization. Startup
initialization varies between launches; the native static artwork covers it.

CPU is roughly 0.23 of one logical core across the measured processes, not a
machine-wide percentage. Working sets include shared pages; summed working sets
are not private committed memory. Draw timing measures command submission, not
GPU completion. Frame statistics retain the most recent 600 samples. The digital
audio measurement verifies scheduling/output, not a calibrated loudness level
from physical speakers. No clipping was detected in the offline mix.

These are observations on this desktop, not a guarantee for integrated GPUs,
RDP, battery mode or simultaneous model initialization. A separate WebView2
environment carries a substantial memory baseline. Devin must measure/reuse the
existing application environment where practical and retain static fallback.
Actual production loading impact and the final overlapping-window transition
remain integration work; they cannot be validated in an isolated prototype.

## Exact captured states

| Time | Preview |
| ---: | --- |
| 0.00 | [Sealed](01-locked.png) |
| 2.25 | [Authorized](02-authorized.png) |
| 3.80 | [Pins released](03-pins.png) |
| 5.15 | [Iris opening](04-opening.png) |
| 7.80 | [Core charging](05-core.png) |
| 10.50 | [Full-power plateau](08-charged-hold.png) |
| 12.60 | [Online](06-online.png) |
| 12.60, reduced motion | [Reduced motion](07-reduced.png) |

Reproduce with `./tools/verify.ps1 -Native` from the prototype folder. It does
not start Nexus, contact a backend, alter global audio settings, or change the
production splash. The fault exercise creates a disposable copy, without
deleting any source assets.
