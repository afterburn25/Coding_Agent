# Nexus Core: startup, error, recovery and recovery-failed splash sequences

## Scope and branch

Repository: `afterburn25/Coding_Agent`.
Handoff branch: `feature/cinematic-core-unlock-splash`.
Updated from main `af467e4` on October 5, 2026, without changing production files
relative to that baseline. Do not replace current main with an older prototype
checkout. The normal cinematic and glow fix are already integrated; Devin's
remaining work is to port these new presentation states into that runtime.

All four videos, caption timings, audio sources and rendering code are in
`prototypes/core_unlock_splash/`. This package does not implement backend repair,
automatically merge itself into main, or update the installed application.

## Review assets

| State | File under `review/videos/` | Duration | Intended transition |
|---|---|---:|---|
| Startup | `NexusCore-Startup-Glow-Only.mp4` | 17 s | Existing normal startup, unchanged |
| Error | `NexusCore-Error-Red-Continuation.mp4` | 12 s | Continue from startup's fully powered final frame; stop at intervention |
| Recovery | `NexusCore-Recovery.mp4` | 27 s | Begin only after the user selects Retry |
| Recovery failed | `NexusCore-Recovery-Failed.mp4` | 13 s | Alternative branch at recovery's 8.8 s reconstruction stage |

`sequence_manifest.json` records paths, SHA-256 hashes, codec metadata and
transition rules. `review/videos/index.html` plays the separate files with sound.
All MP4s are 1280×720, 30 fps, H.264, with 48 kHz stereo AAC. They are review
references, with captions/progress/recovery controls baked into the pixels.
For a live splash, port the source animation and use real DOM/native controls;
an MP4's recorded buttons cannot receive clicks and its captions cannot change.

The exact startup SHA-256 remains
`bcc3ecc6e9032c95b1c0244d79071b81663743dd64a415151b18e26ee4b63955`.
Keep the current production startup flow, progress authority, voice behavior,
readiness convergence and dismissal fixes. The legacy review startup caption is
cyan `NEXUS CORE ONLINE`; production's current green `CORE SYSTEMS · ONLINE`
behavior remains appropriate and must not be reverted to match that old caption.

## Required interaction flow: user-controlled Retry

```text
STARTUP
  -> first failure -> ERROR / CONTAINMENT
  -> USER INTERVENTION REQUIRED (wait indefinitely)
       -> Retry clicked -> RECOVERY ATTEMPT
            -> confirmed success/readiness -> CORE SYSTEMS ONLINE
            -> attempt fails -> RECOVERY FAILED
                 -> USER INTERVENTION REQUIRED (wait indefinitely)
```

The first failure must stop with **USER INTERVENTION · REQUIRED** and the
existing Retry, Open Log, Rollback, Safe Mode and Exit options visible. Do not
automatically begin recovery, restart loading, or play the recovery movie when
the error movie ends or a timer expires. The waiting state may keep the red
status pulse and quiet emergency hum, but the core remains sealed. Passive
diagnostics/logging may run; actual repair/relaunch starts only after Retry.

Retry must invoke the real backend retry/recovery operation once, then show the
recovery presentation. Disable/coalesce duplicate Retry clicks while that
attempt runs. Keep other appropriate recovery actions reachable. Drive the
recovery captions from that attempt's real milestones. If it fails at any point,
interrupt recovery and play the fourth sequence from the current visual state;
never finish the green successful ending first. Return to intervention and wait
for another explicit action. A further retry is user initiated, not an automatic
loop. Keep these states separate from a renderer failure/static fallback.

## Error: continue instead of replaying startup

The first frame matches startup frame 509 at 30 fps (16.9667 s): iris fully
open, fixed sphere geometry, full emitted core glow, completed bar. After a
0.5 s online lead, the full bar pulses red with a 1.2 s period. Status is red,
with these exact labels:

| Clip time | Status |
|---:|---|
| 0.5 s | WARNING · SYSTEM INSTABILITY |
| 2.3 s | CORE SYNCHRONIZATION · FAILING |
| 4.1 s | DESTABILIZATION CASCADE · DETECTED |
| 5.9 s | CORE DESTABILIZATION · IMMINENT |

Power drops at 7.1 s; the iris begins closing at 7.8 s. Pins and rings engage
in sequence, with synchronized mechanical sounds. Containment completes at
9.52 s; status changes to **USER INTERVENTION · REQUIRED** and the recovery
options appear. Stop at this endpoint until Retry or another option is chosen.
The reference MP4 is finite; a live host must retain its final scene/controls
indefinitely instead of unloading the splash or autoplaying the next clip.
No startup replay or automatic recovery attempt occurs.

`tools/video_export/error-continuation.json` owns these timings. The export
adapter adds 3.8 s of visible instability to the original fault timeline while
moving power-down/closure/audio cues together. In live use, capture the actual
current geometry, orbit and ambient phase: an early startup fault must start
from its current partial state, not jump ahead to the fully powered review
case. The review's full red bar applies to faults after a completed startup;
capture actual partial progress when a fault occurs earlier.

## Successful recovery

| Clip time | Exact status | Presentation |
|---:|---|---|
| 0.0 s | EMERGENCY CONTAINMENT · ENGAGED | Closed core, full red bar |
| 2.2 s | NONESSENTIAL SYSTEMS · ISOLATED | Containment remains engaged |
| 4.4 s | RECOVERY MATRIX · INITIALIZING | Diagnostic scan |
| 6.6 s | FAULT SOURCE · LOCATED | Diagnostic scan |
| 8.8 s | CORE RECONSTRUCTION · IN PROGRESS | Begin restoring mechanism |
| 12.6 s | STABILITY THRESHOLD · RECOVERING | Start a 1.8 s color transition |
| 15.8 s | CONTAINMENT · RELEASED | Iris open; core charging |
| 18.6 s | CORE INTEGRITY · VERIFIED | Full charge and stable glow |
| 21.4 s | CORE SYSTEMS · ONLINE | Green status pulse; steady core/hum |

At 12.6–14.4 s, stop the red flashing smoothly, fade the bar to its original
blue/cyan gradient, and fade the red text to white. The panel also fades away.
At 21.4 s, change the status to green and pulse it gently with a 2.4 s period.
Hold the stable full-power state until real readiness permits dismissal.
The physical sphere never grows: only its emitted glow expands with charge.

`tools/video_export/recovery.json` defines the labels, transition window and
piecewise mapping into the existing normal mechanism sampler. Individual
lock, pin, gear, iris and reactor stems are synchronized by
`mix_recovery.py`; the iris/charge stems are retimed without changing pitch.

The displayed stage must follow real evidence. The preview advances on a
clock only to make a reviewable movie. Production must not claim that systems
were isolated, a fault was located, integrity was verified, or the core is
online just because time elapsed. Hold the last truthful stage if the backend
has not confirmed the next one. Unavailable automatic repair goes to human
intervention rather than inventing progress or repair attempts.

## Recovery failed: alternate ending

This is the failure branch of a user-requested Retry, not a clip to append after
successful ONLINE or to play automatically on the first startup fault.
The reference begins at recovery time 8.8 s with the iris sealed and the core
unpowered. It retains red warning color and never shows green or online.

| Clip time | Exact status |
|---:|---|
| 0.5 s | RECOVERY ATTEMPT · FAILED |
| 3.2 s | AUTOMATIC RECOVERY · HALTED |
| 5.9 s | CORE CONTAINMENT · MAINTAINED |
| 8.6 s | USER INTERVENTION · REQUIRED |

The final state is contained and stable, with a quiet emergency hum and live
recovery controls. Repeated fault notifications update details, never restart
the animation or trigger an automatic retry loop. A failure arriving after the
iris has begun opening must first use current-state containment; do not snap
back to this reference's already-closed frame.

`tools/video_export/recovery-failed.json` and `mix_recovery_failed.py` define this
clip. Map the terminal live state to existing `HUMAN_INTERVENTION_REQUIRED`.

## Where Devin should wire it

Current production paths:

- `desktop/ChatNexus.Desktop/Program.cs`: `SplashForm`, startup/fault reporting,
  `NexusCoreApplicationContext.RunStartupAsync`, native fallback and readiness.
- `desktop/ChatNexus.Desktop/StartupProgress.cs`: truthful progress, status,
  readiness and dismissal authority.
- `desktop/ChatNexus.Desktop/splash/web/splash.mjs`: host messages, status/bar,
  recovery panel, sound/voice and completion notification.
- `desktop/ChatNexus.Desktop/splash/web/controller.mjs`: current-state fault
  snapshot, containment and recovery state transitions.
- `desktop/ChatNexus.Desktop/splash/web/timeline.mjs`, `renderer.mjs`,
  `audio.mjs` and `splash/animation_manifest.json`: deterministic presentation.

Existing host messages include `set-progress`, `set-gate`, `trigger-fault`,
`recovery-state`, `repair-success`, `show-recovery`, `set-reduced`, `set-audio`
and `dispose`. Extend that contract deliberately for confirmed recovery
milestones; do not route nine new labels through unsupported controller enums.
The review source has explicit `exportErrorFrame`, `exportRecoveryFrame` and
`exportFailedFrame` adapters in `tools/video_export/export-error-continuation.cjs`.
These are deterministic presentation/reference helpers, not a replacement for
the host's startup or recovery coordinator. Extract their interpolation and
color logic into the live controller/runtime when integrating.

For error/failure states, clear stale `.online` classes from status/detail;
normal `externalProgress.primary` may still contain the old ONLINE label.
All UI labels and error details should remain text content, never HTML.

Keep recovery actions accessible immediately. Record/log a startup exception
and permit passive diagnostics before playing the cosmetic sequence; never await
the 12/27/13 s videos before exposing fallback controls or acting on a user
request. Never start actual repair/relaunch before the user's Retry action.
Update the presentation watchdog policy for the longer sequence while keeping
the independent native recovery fallback available promptly. Do not merely
increase the old six-second fallback timeout or block recovery on the animation.

Keep `nexus-core-ready`, `MarkAppReady`, `ReadyToDismiss`, `sequence-complete`
and the readiness-tail convergence introduced on main. A preview/movie ending
must not dismiss the splash, imply backend health or signal app readiness.
Hold completed visuals and loop the relevant original hum while waiting.
Stop/fade audio on dismissal, honor mute/volume and voice ducking, and avoid
replaying a fault on duplicate messages. Reduced motion must suppress flashing,
orbital/particle movement and pulses while retaining readable static status.

## Implementation checklist and verification

1. Pull current main and inspect this branch; port only the new presentation
   and required assets, retaining the existing production lifecycle fixes.
2. Watch all four reference MP4s and read the JSON timings. Keep normal startup
   unchanged. Separate live labels/progress/controls from artwork.
3. Connect error and confirmed recovery stages to the real host messages.
   First failure waits at intervention; Retry starts exactly one real attempt;
   only a failed attempt enters the recovery-failed presentation.
   Preserve snapshots for mid-animation interruption and early failure.
4. Implement full-power and contained holds, no repeated-fault loop, failed
   repair terminal state, silent/reduced-motion paths and immediate fallback.
5. Run prototype tests and `tools/video_export/verify_package.py`, then the
   production StartupProgress/lifecycle tests after integration. Verify fast
   readiness, long loading, early/late fault, indefinite first-failure hold,
   single/double Retry clicks, recovery success/failure, repeated
   errors, unavailable renderer/audio and cancellation on the actual app.
6. Publish integration in its own reviewed change. A branch push here does not
   update the installed local application; build/install and verify that
   separately if the user requests it.

Commands for reproducing the references and package validation are in
`prototypes/core_unlock_splash/tools/video_export/README.md`.
