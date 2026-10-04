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

`playback-report.json` is the published Windows build's playback measurement.
The most recent small follow-up fixes mute-before-Play; its dedicated Node test
passes. It does not change the captured visual states or the unmuted playback path.
`silent-report.json` records the silent/failure verification run. Its larger
backing buffer predates the final rendering optimization; use the normal report
for final performance figures. The fallback reports record the injected-fault
and static recovery routes. Process IDs were removed from saved reports.

## Measured performance and limits

The final renderer uses a fixed **1024×576** backing surface, matching production
artwork. Mechanical textures are cached at 2×. Unchanged accessibility labels
and status text are no longer rewritten every frame; the bar uses a transform
instead of relayout. That reduced measured host/browser CPU cost substantially.

| Metric | Published preview measurement |
| --- | ---: |
| Median frame interval | 16.7 ms (about 60 FPS) |
| 95th-percentile frame interval | 16.8 ms |
| Median / 95th-percentile draw submission | 0.2 / 0.3 ms |
| Renderer initialization | 483 ms |
| Host CPU / WebView2 CPU during 16.56 s test | 0.125 / 3.313 CPU-seconds |
| Host / aggregate WebView2 working set | 63.6 / 470.8 MiB |
| Active decoded stems | 16 |
| Playback measured digital peak, default volume | 0.299 full scale |
| Offline nominal mix peak, master at 100% | 0.712 full scale |
| Offline nominal mix peak, default 45% | -9.89 dBFS |

CPU is roughly 0.21 of one logical core across the measured processes, not a
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
