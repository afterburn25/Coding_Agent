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

The October 5 follow-up expands only the emitted glow with `charge`, filling
the reactor opening with diffuse cyan/blue light. The sphere, plasma texture,
surface currents and cavity retain their original dimensions. Full-charge
holds sustain that light; emergency power loss contracts and dims the glow
without shrinking the sphere. The renderer adds one cached `coreLight` sprite
and adjusts only light-layer sizes in `drawCore`. Apply both changes from
`prototypes/core_unlock_splash/web/renderer.mjs` if production keeps a separate
copy. Gates, progress, audio, mechanical geometry and failure timing are unchanged.

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

### Fatal startup failure: presentation is not the recovery coordinator

The prototype now includes a **5.6-second emergency containment path**, separate
from the unchanged normal-startup timeline. `web/controller.mjs` captures current
lock angle, three ring angles, four pin positions, six iris positions, core
brightness, orbital position and reveal amount. Failure interpolates from those
values. A 53%-open iris closes from 53%; it never jumps fully open first.

The failure path is:

`ANY_STARTUP_STATE → FAULT_DETECTED → CORE_INSTABILITY → EMERGENCY_POWER_DROP → EMERGENCY_CONTAINMENT → LOCKDOWN → FAULT_CONTAINED → RECOVERY_UI`.

| Failure-relative seconds | Presentation / sound |
| ---: | --- |
| 0.00 | Freeze progress; snapshot current state; crossfade startup audio into unstable reactor harmonics over 160 ms |
| 0.10–2.80 | Sustained visible instability: one amber indicator, a small diagnostic glyph, uneven core/platform light, violet/cyan imbalance, orbital desynchronization and a small inner-ring shudder |
| 0.30 / 0.65 | Quiet electrical texture / one restrained double warning |
| 2.80–3.50 | Smooth power drop, contracting halo, slower orbitals and reduced particles; descending power tone |
| 3.50–4.10 | Decisive iris closure from current positions; armored plate/servo sound only if the iris was open |
| 3.85–4.41 | Inner, outer, then middle ring realign with controlled easing |
| 4.38 / 4.55 / 4.72 / 4.89 | Bottom / left / right / top pins engage; each impact lands 130 ms later at its end stop |
| 4.99–5.21 | Cylinder returns to secure position with a slight end-stop recoil |
| 5.22 | FAULT CONTAINED, restrained thump and quiet emergency hum |
| 5.36–5.60 | Matching recovery panel fades in; contained core stays dim and locked |

Red is confined to a critical indicator; the chamber remains blue. Instability
is visibly distinct before shutdown, without lightning, explosions or a full
screen flash. Reduced motion removes shudder, flicker and orbital drift.
Instability gets roughly 2.5 seconds of viewing time before power collapse, and
the iris does not begin closing until 3.5 seconds after the fault. Extending
this presentation never postpones actual recovery work or immediate controls.
The normal source artwork and original sixteen audio stems remain unchanged.
Twelve separate failure stems, timings and event gains are in the manifest.
The failure mix and outgoing crossfade are measured at nine interruption points.

**Do not replace the current `ShowFailure(...)` call with an awaited animation.**
The current fatal catch in `NexusCoreApplicationContext.RunStartupAsync()` in
`desktop/ChatNexus.Desktop/Program.cs` still reaches `SplashForm.ShowFailure(...)`;
this branch has not changed it. For integration, introduce a host-level
`TriggerStartupFault(exception)` wrapper with this order:

1. Record the exception and diagnostic context immediately.
2. Start/notify the existing recovery analysis, rollback or restart preparation
   independently; observe worker exceptions using the recovery coordinator.
3. Best-effort signal the splash with `window.preview.triggerFault(...)` on the
   UI thread. The call starts presentation and returns a boolean immediately.
   It does not return an animation-completion task to await.
4. Show the recovery view as containment settles. Expose recovery controls
   immediately when needed, including an explicit skip and Safe Mode.
5. On any rendering, bridge or audio failure, retain real recovery state and
   show the existing static `ShowFailure(...)`/recovery controls. Logging,
   diagnostics and recovery workers must continue regardless of presentation.

Never wait for a renderer-ready callback, decoder, animation completion, fade,
or this 5.6-second sequence before logging, diagnostics, rollback, Safe Mode,
recovery workers or restart preparation. The standalone prototype does not
implement any of those workers. Its recovery labels and buttons are previews.

### Failure and recovery API

```javascript
// Serialize real values through the host bridge; never interpolate raw exception text.
window.preview.triggerFault({
  message: 'Core initialization could not complete.',
  detail: 'User-readable diagnostic context; keep full logs in the host.'
});
window.preview.setRecoveryState('RECOVERY_ANALYZING');
window.preview.setRecoveryState('REPAIR_ATTEMPT', { attempt: 2, total: 3 });
window.preview.setRecoveryState('ROLLBACK');
window.preview.setRecoveryState('RESTARTING');
window.preview.setRecoveryState('HUMAN_INTERVENTION_REQUIRED');
window.preview.setRecoveryState('SAFE_MODE'); // Immediate locked, static, silent view.
window.preview.showRecoveryImmediately();   // Skip presentation; keep recovery policy.
window.preview.repairSuccess();             // Presentation only; real worker reports success.
```

`triggerFault` accepts faults from any normal phase and a reauthorization retry.
While one fault is active, subsequent calls update bounded diagnostic strings
and a count but return `false`, retaining the same snapshot and elapsed time.
They never restart containment or replay audio. Failure status overrides normal
loading labels, and the bar freezes at the last supplied real fraction even if
late progress messages arrive. Diagnostic details appear only in the panel's
expandable Details area; text uses `textContent`, never injected HTML.

Recovery states require an active fault. The caller supplies any attempt count;
there is no automatic retry counter in production-facing behavior. `ROLLBACK`
uses a reverse outer diagnostic scan while all actual containment hardware
stays locked. `RESTARTING` and `HUMAN_INTERVENTION_REQUIRED` keep the lock closed.
`SAFE_MODE` bypasses the cinematic immediately, pauses motion and schedules no
audio. It cannot accidentally launch a recovery-success power-up; explicit
normal-start presentation is needed for a later real startup attempt.

`repairSuccess()` during closure queues the presentation transition until the
core is secured. Then `REPAIR_SUCCESS → REAUTHORIZATION → UNLOCK → CORE_CHARGE →
ONLINE` clears amber/red indicators, plays authentication, and resumes the normal
sequence at authorization. **It does not release any readiness gate.** All
remaining gates, real StartupProgress values and the app-ready handshake remain
host-owned. Early normal gates also remain authoritative; the sampler resumes
no later than the earliest closed boundary. A new failure during this retry can
start a new containment sequence. Only explicit success/retry ends the active
fault episode; duplicate errors alone cannot create a failure loop.

Panel actions dispatch `nexus-recovery-action` on the window and a WebView2
`recovery-action` message, with `action` equal to `open-log`, `retry`, `rollback`,
`safe-mode` or `exit`. Details expands locally. Wire those requests to existing
host recovery commands later. The current preview displays an acknowledgement;
it never starts repair, restarts, opens logs, rolls back, or exits production.
The native static fallback has independent buttons; its Exit closes only this
standalone preview.

### Recovery fallback and resource limits

The preview host receives `startup-fault` immediately. A renderer exception or
WebView2 process failure after that disposes the renderer/audio and reveals a
native WinForms recovery panel. An independent **6-second native watchdog**
also shows those controls if `recovery-visible` never arrives, including a
stalled/paused presentation. Repeated errors do not restart the watchdog. A new
normal attempt invalidates the previous watchdog. Carry this independent bound
into production, with the real recovery view model retained outside WebView2.
Do not make a web page, animation timer or audio device the sole route to recovery.

After containment the optional diagnostic scanner runs at 15 Hz; the short
cinematic targets 60 Hz. Safe Mode draws once. Noise/lighting textures are cached,
the backing surface remains 1024×576, and background playback pauses. The host
must still profile concurrent diagnostics/rollback on target hardware; the
WebView2 memory baseline remains significant. No production performance claim
is implied by this isolated preview.

Native verification covers faults from locked, authorization, lock turn,
half-open iris, power-up and pre-ONLINE states; frozen progress; duplicate
errors; output/mute/silent audio; supplied attempt counts; rollback, restarting,
human intervention, Safe Mode and successful recovery through a closed readiness
gate. Injected renderer failure and a deliberately stalled presentation must
both expose native static recovery controls. See the saved review reports and
`tools/verify.ps1 -Native`.

### Successful startup handoff

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
