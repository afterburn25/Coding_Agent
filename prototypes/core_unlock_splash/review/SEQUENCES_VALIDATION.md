# Four-sequence package validation — October 5, 2026

Validated locally on Windows with Edge/Playwright, Node, Python/NumPy and ffmpeg.

- All four files decoded completely without ffmpeg errors.
- Each has H.264 1280×720 video at 30 fps, plus AAC 48 kHz stereo audio.
- Durations/frame counts: startup 30 s/900, error 12 s/360, recovery 27 s/810,
  recovery-failed 13 s/390.
- All asset hashes and file sizes match `sequence_manifest.json`.
- Startup keeps the original opening/charge and extends the moving online state
  to 30 seconds total, as requested. The prior 17-second file is superseded.
- At 12.6, 17, 24 and 29.9667 seconds the core remains fully charged and the
  iris/rings/pins/cylinder stay in their settled positions. Rendered frames
  differ and orbital phase advances at every checked time: the hold is animated.
- Digital RMS remains about 0.035 during 17–20, 23–26 and 27–29 seconds. The
  stable hum continues through the extension, with only a final one-second fade.
- Browser checks pass for the fully powered error start, complete progress bar,
  first-failure intervention endpoint,
  exact warning/recovery caption order, captions fitting the readout, white
  stability state, green online state, containment and failed-recovery branch.
- Sampled geometry/orbit/ambient values match across startup → error,
  error → recovery, and recovery at 8.8 s → recovery-failed.
- Error and successful recovery are re-exported against the 30-second startup
  endpoint. All four sampled recovery-failed PNGs remain byte-identical, so that
  contained clip requires no re-encode.
- Frames visually reviewed for red warning text/bar, opening/charge, white
  recovery status, green online status, and the contained intervention endpoint.
- Existing prototype tests: 46 Node tests and 7 Python asset tests passed.
- Standalone .NET preview build passed with zero warnings/errors.
- Production `desktop/` and `web/` have no changes relative to main baseline
  `af467e4`; the new behavior has not been wired into the installed app.

Audio mixes preserve digital headroom; peak absolute full-scale values at the
recommended master volume: error 0.246, recovery 0.332 (rounded upward), failed
recovery 0.077 (rounded upward). The mechanical cues are aligned to the exported
event schedule. This is digital/render verification, not a claim of new human
listening tests on the user's speakers.

Run `python tools/video_export/verify_package.py --media` from the prototype
directory to recheck the distribution. Run the exporter's
`--recovery-failed --stills` mode to reproduce the reference transition checks.
See the handoff for required production lifecycle and backend-event tests after
Devin integrates the presentation.
