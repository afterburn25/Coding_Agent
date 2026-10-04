# Nexus Core splash animation handoff for Devin

**This branch contains a standalone cinematic Nexus Core splash prototype. It has NOT been integrated into production startup.**

Branch: `feature/cinematic-core-unlock-splash`.
Started from latest `main` at `a45c24cfe6f5b21c2cb8a78bce5aacc21377cfd5`.
Refreshed the branch onto the newer `main`,
`19aa13cc0cf295125af97bbb8b48e4ee18edd11d`, before delivery. The incoming changes
did not alter the source artwork, StartupProgress, or its readiness handshake.
All implementation, art, audio, preview and tests are under
`prototypes/core_unlock_splash/`. This document is the only change outside that
prototype folder. Do not merge or ship it automatically.

## Review and build first

From `prototypes/core_unlock_splash`, run `dotnet run -c Release`. Press Play
sequence. See its README for build, publish, verification and command-line modes.
Review the individual audio stems and the optional `audio/mixed_preview.wav`.
The recommended master volume is **0.45**, with headroom verified at volume 1.
The sounds are original deterministic synthesis; no movie/game recordings were
used. Provenance and source are committed.

The implementation reuses **WinForms + WebView2**, which production already
depends on. Canvas 2D has cached mechanical layers, bounded particle counts,
bounded backing resolution, and time-based transforms. Audio uses separate
stems scheduled on the Web Audio clock. There is no game engine, remote resource,
video, image-sequence animation, or morphing typography.

The online lighting now follows the brighter supplied reference: a white-hot
core, luminous orbital trails, cached blue haze, platform emission and floor
reflection, with deeper recessed shadows and metal bevels. These runtime layers
follow `charge` and remain at full intensity during a delayed readiness hold.
The original chamber, shield placement and wordmark remain the primary artwork.

## Exact integration surface

Integrate later with `desktop/ChatNexus.Desktop/Program.cs`, specifically
`SplashForm` and `NexusCoreApplicationContext.RunStartupAsync`, and with the
existing `StartupProgress` implementation in `StartupProgress.cs`.

The prototype is **not a replacement startup coordinator**. Keep these existing
authorities: `Report`, `Tick`, `DisplayedProgress`, `Primary`, `Secondary`,
`MarkAppReady`, `ReadyToDismiss`, and the existing error/retry path. Real backend
health and the interface's `nexus-core-ready` handshake remain required. The
prototype's `prototype-ready` message means only **renderer initialized**.
Never confuse it with application readiness.

1. Load the prototype assets into a local, restricted WebView2 origin or port
   its sampler/geometry to another renderer. Keep `SplashForm`'s static artwork
   visible while preparing it asynchronously; never await animation to start
   services. Reuse the existing WebView2 environment where practical instead
   of booting a second independent browser process tree.
2. Remove the development control chrome, or use a dedicated splash view. Keep
   the source image, its aspect ratio, wordmark placement and live progress region.
3. At time zero, close all six readiness gates. Release them from real startup
   signals using `window.preview.setGate(id, true)`. Marshal calls to the UI
   thread; serialize strings safely rather than interpolating untrusted text.
4. Independently call `setProgress(progress.DisplayedProgress, progress.Primary,
   progress.Secondary)` after `StartupProgress.Tick()`. The preview's nominal
   percentage is only a demo readout. Never substitute it for real/predictive
   startup progress, readiness, or the existing learned timing profile.
5. Apply the user's settings before `play()`. On closure/failure invoke
   `dispose()` and dispose the renderer so no timer or audio remains active.

The JS API also exposes `pause()`, `play()`, `replay()`, `setAudioEnabled(bool)`,
`setVolume(0..1)`, and `setReducedMotion(bool)`. `capture()` and `verifyPlayback()`
are development-only helpers. Gate closure after crossing a boundary is rejected;
close all gates **before** starting. Replay rewinds time but preserves gate state;
on a retry reset the readiness policy explicitly or recreate the preview.

For example, `setProgress(0.37, "LOADING · MODELS", "Preparing the local model")`
shows 37% and those two exact text lines without advancing the animation or
releasing readiness. Replace these example strings and the fraction with actual
StartupProgress values. The native verification explicitly checks this separation.

## Connect phases to real startup

Nominal timing is **12.6 seconds**, extended at the user's request for a longer
four-second power-up, more core glow, a louder hum, dry tumbler/gear sounds, and
a sustained full-power plateau that can extend for delayed loading. At
2× preview speed it lasts 6.3 seconds. Timing is not a readiness signal. The machine-readable
manifest is authoritative for seconds, durations, event ordering, audio paths,
gains, loop ends, and stereo placement.

| Visual state / event | Nominal seconds | Suggested real signal / gate release |
| --- | ---: | --- |
| Dormant / sealed | 0.00 | Initial environment preparation; all gates closed |
| Security activation | 0.70 | Core services starting (`Report(0.15)` / `0.30`); release `services` |
| Authorization | 1.60 | Backend initialization markers (`BootPhase`, current 0.30–0.54 band); release `authorization` |
| Tumbler contacts | 1.83 | Fifteen distinct mechanical contacts over 1.3 seconds, through the cylinder turn |
| Cylinder turn | 2.35 | Services becoming available / backend healthy (`Report(0.55)`); release `unlock` |
| Top / right / bottom / left pins | 3.15 / 3.33 / 3.51 / 3.69 | Continue the authorized mechanical sequence |
| Counter-rotating ring alignment | 3.95 | Continue; rings settle after 0.8 s |
| Six-leaf iris opening | 4.75 | Interface nearing readiness (`Report(0.85)` or `0.93`); release `open` |
| Core visible | 5.35 | A visual reveal, never a new readiness authority |
| Core charge | 5.65 | `PrepareAsync` completes / real `MarkAppReady`; release `charge` |
| Full-power plateau | 9.65 | Sustain charged reactor hum and bright core for at least two seconds |
| Stabilization | 11.65 | Smoothly settle energy and orbital velocity |
| Localized online pulse | 11.80 | Only once the existing `ReadyToDismiss` gate is satisfied; release `ready` |
| Stable online | 12.20 | Quiet looping reactor hum and slow orbitals |
| Nominal handoff available | 12.60 | Short settle, then coordinated splash-to-main reveal |

The source currently reports coarse backend bands. Do not claim a model is loaded
just because a timer expired. Add richer real events later if needed. Multiple
gate releases may arrive early; that makes them eligible, but does not skip the
remaining mechanics. Slow startup holds immediately before the next protected
boundary. Before charging it sustains quiet ambience; **after full charge the
bright core, slow orbitals, and louder `charged_hum.wav` continue indefinitely**
until readiness is released. This prevents an audible power-down during slow
loading. Held wall time is discarded for sequence events, so release
does not fast-forward. Do not make this an uninterruptible movie.

Each pin event maps to its own `pin_01..04.wav`; other mapped sounds are
`ambient_hum`, `security_activate`, `authorization`, `tumbler_clicks`, `lock_turn`, `ring_rotation`,
`iris_open`, `core_charge`, `energy_swell`, `charged_hum`, `core_online`, `stable_hum`.
See `animation_manifest.json` for exact mapping. Transport speed scales audio
offsets/rates with motion. Gate changes cancel obsolete scheduled sources before
rebuilding the permitted portion. Do not play `mixed_preview.wav` in production.

## Final handoff and recovery

After genuine readiness: online pulse → short settle → splash fade → main window.
Prepare the main window behind the splash, give it a rendered frame, then overlap
the windows during the fade. Keep an opaque chamber underneath until the main
window can cover it. Do not expose the desktop, a black frame, or a white flash.
Coordinate with `BeginCompletion` / `CompletionFinished`; avoid stacking the old
850 ms completion glow and the new pulse unintentionally. Animation completion
can add a bounded presentation settle, but can never mark the app ready.

Repeated boot failures and Safe Mode must bypass the full sequence: use static
or simplified art, minimal or no sound, and immediate diagnostics. Preserve
`ShowFailure`, Retry / Open Log / Exit, backend-failure handling and bounded
readiness fallback behavior. This prototype is not a reason to delay recovery.

## Failure isolation

The native preview loads the static image first. Renderer initialization is
asynchronous with a 10-second watchdog. A process failure, manifest failure,
script error or initialization exception disposes the animation and retains
static art. Missing source art falls back to branded text. An individual missing
sound is skipped; unavailable audio leaves motion running. Silent mode creates
no audio context and does no audio fetch/decode. Production must use the same
best-effort principle, with a tighter watchdog if appropriate; never abort or
block backend/model initialization for a splash failure.

Do not let exceptions from the JS bridge escape into real startup. Catch and log
them once, switch to `SplashForm`'s existing static drawing path, and let real
startup proceed. The preview test harness exits nonzero on a fault; that behavior
belongs to **verification only**, not to a future production startup coordinator.

## Settings and resource budget

Connect **Splash sounds enabled**, **Splash sound volume**, **Silent startup**,
and **reduced motion**. Honor existing global audio preferences if present.
Reduced motion defaults to the OS preference and removes orbit motion, free
particles and pulse, shortens cylinder travel, and substitutes a brief opacity
reveal for iris travel. Silent mode never affects animation timing.

Rendering targets 60 Hz, caps backing resolution at 1024×576, caches mechanical
art/glows, and draws only a small moving overlay. Browser presentation may use
GPU acceleration; don't assume acceleration is available in every environment.
The hidden preview pauses animation/audio to avoid unnecessary background work.
The sampler is independent of frame counts, so skipped frames do not alter phase
ordering or geometry. See `prototypes/core_unlock_splash/review/VALIDATION.md`
for measured performance and its limits.

WebView2 adds process/memory overhead even though the SDK is already present.
Before production, measure cold startup, integrated GPU, high DPI, battery/RDP
and simultaneous model loading. Share an environment if feasible; reduce render
resolution/rate or select static fallback under contention. This branch does not
claim negligible model-startup impact without that integration measurement.

## Review gates before production

Run the isolated Node/Python tests, native build, live audio/visual verification,
silent preview, missing-asset/device failure exercises and recovery preview.
Then run production StartupProgress tests after integration. Check early/slow
readiness, a failed backend, retry, repeated boot failure, focus changes, resize,
mute/volume persistence and the final overlapping-window transition on target
hardware. Confirm no audio continues after dismissal. Keep this prototype branch
unmerged until its visual, sound and integration review is complete.

API references used in implementation: [WebView2 local content](https://learn.microsoft.com/microsoft-edge/webview2/concepts/working-with-local-content)
and [Web Audio buffer scheduling](https://developer.mozilla.org/en-US/docs/Web/API/AudioBufferSourceNode/start).
