# Nexus Core — containment protocol

Standalone cinematic startup prototype for .NET 8 / Windows / WinForms.
**Production startup is not integrated or modified.** Devin's integration guide
is [`../../docs/SPLASH_ANIMATION_HANDOFF.md`](../../docs/SPLASH_ANIMATION_HANDOFF.md).

## Run

Requirements: Windows 10 1809+ or Windows 11, .NET 8 Desktop Runtime and SDK 8+
for building, and Microsoft Edge WebView2 Runtime (already used by Nexus).
The pinned WebView2 NuGet package is the same version as the production desktop.

From this folder:

```powershell
dotnet run --project CoreUnlockSplash.csproj -c Release
```

Press **Play sequence** to see and hear the 12.6-second unlock, including the
requested four-second reactor charge, stronger core glow/hum, and 15 individually
synthesized tumbler contacts. The original 6–8-second fast-start range remains
available at 2× speed (6.3 seconds). Replay, pause /
resume, mute, volume, 0.25–2× speed, scrubbing, phase jumps, reduced motion and
readiness holds are available. Scrubbing/jumping pauses playback; press Resume
to hear stems from the selected position. Selecting a hold rewinds and pauses;
Play, then Release hold, demonstrates a slow startup. Replay preserves the
chosen gates. The preview never starts the Nexus backend or models.

```powershell
# Automatic demo, silent demo, reduced motion, and recovery preview
dotnet run -c Release -- --autoplay
dotnet run -c Release -- --autoplay --silent
dotnet run -c Release -- --reduced-motion
dotnet run -c Release -- --static
```

To distribute a framework-dependent Windows x64 build:

```powershell
dotnet publish CoreUnlockSplash.csproj -c Release -r win-x64 --self-contained false -o bin/preview
```

Distribute the **entire** publish folder, including `web`, `assets`, `audio`,
manifest and WebView2 loader. Launch `NexusCore.SplashPreview.exe`. Neither a
Node installation nor Python is required for playback. Builds require NuGet
access once; playback is local and has no network or remote asset dependency.

## Design

The original production image is immutable. A six-leaf iris, four solenoid
clamps, three concentric rings and a key cylinder form a layered mechanism
inside the existing shield. Geometry is fixed; only rigid transforms and
opacity change. The cylinder withdraws on the top carriage. The core has
procedural plasma, a white-hot center, luminous cyan/violet orbitals, particles
and a restrained halo. Cached blue nebula haze, light on the housing, a bright
platform ring and a vertical floor reflection bring the fully powered state
closer to the supplied online reference. Recessed ring shadows, dark plate
seams and bright metal bevels keep the mechanism readable inside the glow.
This full lighting state persists while the charged core waits for readiness.
The surrounding chamber and lettering remain in their original positions.

The renderer uses cached Canvas 2D layers hosted by the desktop's existing
WebView2 technology. It caps the backing buffer at 1024×576. It uses no game
engine, frame animation, generated-image crossfade, or downloaded font.
Reduced motion uses a short opacity reveal of the same procedural geometry,
small pin travel, fixed rings/orbits, and no free particles or activation pulse.

`performance.now()` supplies monotonic elapsed time. `requestAnimationFrame`
requests presentation at up to 60 Hz. `animation_manifest.json` is the timing
and sound-event source; `web/timeline.mjs` samples state directly at time t.
Pause, speed changes, seeks and readiness holds all re-anchor the clock.
Waiting time is discarded at a gate; releasing it does not jump ahead.

Web Audio schedules individual stereo PCM stems on its audio clock. On a
transport change it stops stale sources and reschedules active/future stems
with correct offsets and playback rate. A held gate sustains a quiet hum.
At full charge, a two-second plateau precedes stabilization. A readiness hold
extends the bright core, slow orbital motion and full-power hum indefinitely;
the hum does not drop back to dormant ambience. Pause stops sounds; Resume
reconstructs them. Preview speed also changes pitch.
Silent mode creates no AudioContext and fetches/decodes no stems.

## Source map

| File | Responsibility |
| --- | --- |
| `Program.cs` | Independent WinForms host, local asset mapping, static fallback, native verification |
| `animation_manifest.json` | Phases, gates, durations, ordered events, sound mapping and gains |
| `web/timeline.mjs` | Pure animation sampler and monotonic transport |
| `web/renderer.mjs` | All editable art geometry, texture generation and render layers |
| `web/audio.mjs` | Stem decode, clock scheduling, fades, pan, mute and failure isolation |
| `web/app.mjs` | Preview controls and future integration API |
| `assets/` | Byte-identical production splash and provenance |
| `audio/` | 16 original stems, optional mixed preview, mix measurements and provenance |
| `tools/generate_audio.py` | Reproducible original synthesis; optional NumPy dependency |
| `tests/` | Node transport/audio tests and Python asset/distribution tests |
| `review/` | Verified phase stills and measured playback results |

## Verification

Node 22+ and Python 3.10+ are needed only for development tests. The asset tests
use Python's standard library; NumPy is needed only to regenerate sounds.

```powershell
./tools/verify.ps1 -Native
# Or run individual checks:
node --test tests/*.test.mjs
python -m unittest discover -s tests -p test_*.py
dotnet build CoreUnlockSplash.csproj -c Release
dotnet bin/Release/net8.0-windows10.0.17763.0/NexusCore.SplashPreview.dll --verify verification-local/normal
dotnet bin/Release/net8.0-windows10.0.17763.0/NexusCore.SplashPreview.dll --silent --verify verification-local/silent
dotnet bin/Release/net8.0-windows10.0.17763.0/NexusCore.SplashPreview.dll --static --verify verification-local/static
```

The native verification plays actual audio through Web Audio, checks nonzero
post-master samples, tests the transport and readiness gate, records render
timing and process resource use, captures eight exact timeline states, then
exits with status 0 only when checks pass. Run on an interactive Windows desktop.
It checks the digital playback path; human listening and display review remain
necessary on target speakers/displays. `--static --verify` checks the independent
fallback route. Test a missing manifest or unavailable renderer in a disposable
copy; native fallback must remain visible and verification must exit with 1.

The offline tests cover missing stems, unavailable audio devices, silent mode,
gate ordering, replay, paused async initialization, sample scheduling, deterministic
geometry, loop seams, stereo format, mix headroom and byte-identical source art.
No production tests or startup code are replaced by these checks.
