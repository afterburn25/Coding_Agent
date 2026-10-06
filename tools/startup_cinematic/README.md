# Normal startup cinematic authoring

Offline tools only. Nothing here launches Nexus, changes startup state,
invokes TTS, edits recovery clips or installs into the local application.
See [the delivery and timing guide](../../docs/STARTUP_CINEMATIC_CAPTIONS.md).

## Inputs and outputs

The single authoring specification is
`desktop/ChatNexus.Desktop/splash/startup_caption_timeline.json`.
`author.mjs` imports the current production renderer/timeline and samples the
existing mechanisms at their original timestamps. The current-main source
plate is preserved; see the provenance explanation in the delivery guide.

`author.css` and `author.mjs` are the reference for Devin's future live caption
overlay. They provide two-line dissolves, progress demonstration and the green
success pulse. They are not loaded by the installed app.

## Rebuild

Dependencies: Node 22+, Playwright 1.62.1, Python 3.10+ with NumPy 2.3.5,
ffmpeg/ffprobe with H.264/AAC, Windows Segoe UI and Microsoft Edge.
Windows playback verification also uses .NET 8, WebView2 and Windows Media Player.

From this folder:

```powershell
npm install
python -m pip install numpy==2.3.5
node export.cjs
python media.py
python verify_media.py
dotnet run --project PlaybackProbe.csproj -c Release -- ../..
```

`NEXUS_PLAYWRIGHT_MODULE` can point to an existing Playwright installation.
`node export.cjs --stills` runs deterministic layout/timeline/seam checks
without encoding. `--reference-only` regenerates the captioned movie when
only the readout changes; it requires an existing clean render in `work/`.
Use a full render whenever visual/tail/source geometry changes.

`media.py` builds one audio stream from existing cinematic PCM stems and copies
it into both movies, then extracts the review PNGs from the final encoded
reference. No speech track is accepted. Files in `work/` are ignored scratch
outputs. Keep the committed review JSON and PNGs with the two MP4s and timeline.

Both movies are rendered at `frame / 30`, using the same scene sample for each
pair of frames. Capture stays at the existing 1024 × 576 composition budget,
then uses the original Lanczos upscale and CRF 18 export at 1280 × 720.
The authoring adapter only changes the loop's final 0.7 seconds to make its
moving energy/particles/hum repeat cleanly. It never changes physical scale.

## Verification

- `export.cjs`: all 900 caption/progress frames, seven optional caption bounds,
  exact pre-encode loop endpoint equality, pulse low/peak and pause-safe hold.
- `verify_media.py`: full H.264/AAC decoding, 30 s / 900-frame metadata,
  identical audio in both copies, all-frame scene alignment, original-main
  image/audio comparison, encoded loop boundary deltas, 42-frame pulse,
  authored milestone frames and unchanged production/recovery files.
- `PlaybackProbe.cs`: opens and plays both MP4s with the standard Windows
  Media Player engine, then decodes/seeks/plays key phases and repeats the
  tail boundary twice in a real WebView2 host. It has an invisible test window
  and isolated disposable profile under `work/`; it does not launch Nexus.

The baseline preservation check intentionally pins the fetched source commit
`2e29fd8edbda980e7be8c007a607675c3b1b969d`. Future deliberate renderer changes
need a newly reviewed baseline. Do not weaken comparisons to hide a redesign.
