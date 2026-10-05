# Four splash reference videos: editable source and export

The committed MP4s need no build step. Open `../../review/videos/index.html`
through a local server or play each video directly. For production integration,
read `docs/SPLASH_SEQUENCES_DEVIN_HANDOFF.md` at the repository root.

## Source map

| File | Purpose |
|---|---|
| `error-continuation.json` | Startup endpoint, longer visible instability, four red warnings |
| `recovery.json` | Nine recovery captions, mechanism mapping, red-to-normal fade and green pulse |
| `recovery-failed.json` | Contained recovery branch and four failure captions |
| `export-error-continuation.cjs` | Deterministic `exportStartupFrame`, `exportErrorFrame`, `exportRecoveryFrame`, `exportFailedFrame` adapters, browser verification and 30 fps capture |
| `mix_audio.py` | Original stereo mixing primitives and unchanged startup mix |
| `mix_error_continuation.py` | Continued online hum, extended instability, synchronized containment audio |
| `mix_recovery.py` | Recovery event schedule and pitch-preserving iris/charge retiming |
| `mix_recovery_failed.py` | Quiet contained-state hum and warning cues |
| `build_video.py` | Render and mux one selected MP4; refresh its manifest hash |
| `verify_package.py` | Verify all four hashes, durations, codecs, frame counts and decoding |

The adapter uses the existing `../../web/` renderer/controller and the original
`../../assets/` and `../../audio/` assets. It serves files locally and injects
review-frame methods into the standalone preview in memory. It does not modify
production splash files, fetch third-party artwork, start Nexus or run repairs.
Capture progress is deterministic at `frame / fps`; the underlying renderer
remains time based. Live production animation should retain its 60 Hz target.

The exported 17/12/27/13-second movies are presentation examples. Actual loading,
fault, recovery milestones, buttons and readiness remain host-owned. Use
separate original audio stems for live holds, muting, voice ducking and reliable
interruption; the mixed MP4 tracks fade at their fixed end for standalone review.
First failure holds at intervention. The host must wait for Retry before starting
recovery; a failed retry plays recovery-failed and returns to the same waiting
state. No MP4 ending is authorization to start a retry or dismiss the splash.
The old interactive prototype remains available at `../../web/index.html`.
Its legacy failure text/timing is superseded by these export adapters/configs.

## Development prerequisites

- Node.js 22+ and the pinned Playwright development dependency.
- Python 3.10+ with NumPy (validated with 2.3.5).
- `ffmpeg` and `ffprobe` on PATH, including H.264/AAC encoders.
- On Windows, Microsoft Edge; on other platforms, Playwright Chromium.

From this directory:

```powershell
npm install
python -m pip install numpy==2.3.5
# Non-Windows only, unless Chromium is already installed:
npx playwright install chromium
```

`NEXUS_PLAYWRIGHT_MODULE` may point at an existing Playwright package to reuse
an already provisioned development environment. No absolute workstation paths
are committed. Node/Playwright/Python/ffmpeg are export tools, not runtime Nexus
dependencies, and none belongs in the installed app.

## Verify and rebuild

From this directory:

```powershell
python verify_package.py --media
node export-error-continuation.cjs --recovery-failed --stills
python build_video.py --clip error
python build_video.py --clip recovery
python build_video.py --clip recovery-failed
```

`--stills` verifies caption order, containment, color transitions and geometry
continuity without encoding a full movie. It writes review PNGs and a report.
Reports, temporary WAVs, silent MP4s, PNGs and node_modules are ignored by Git.

Keep the approved startup movie byte-for-byte unchanged. An explicit
`python build_video.py --clip startup` is available for intentional future
authoring, but is not needed for this handoff and will invalidate the preserved
startup checksum until a new startup is deliberately approved.

After editing a clip, rebuild it, run `verify_package.py --media`, visually
review it, then commit the MP4 and updated `../../sequence_manifest.json`
together. Encoder/browser versions may produce different MP4 hashes even when
the source frames are equivalent. `--mux-only` combines an already rendered
silent MP4 and corresponding WAV without re-running the renderer.

The existing prototype checks still apply, from the prototype directory:

```powershell
node --test tests/*.test.mjs
python -m unittest discover -s tests -p test_*.py
dotnet build CoreUnlockSplash.csproj -c Release
```
