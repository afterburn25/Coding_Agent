# Original procedural sound assets

All WAVs in this directory were synthesized specifically for this prototype by
`tools/generate_audio.py`. They use oscillators, deterministic seeded noise,
filters and envelopes. They contain no recordings, music, speech, external
samples, model-generated imitations or franchise material.

The new sound assets and their synthesis source may be used, modified and
redistributed with Nexus Core. No third-party sample attribution is required.
This statement does not change the rights in existing Nexus artwork.

Format: 48 kHz, stereo, 16-bit PCM. Four separate pin releases support later
re-timing. `ambient_hum.wav`, `charged_hum.wav` and `stable_hum.wav` are four-second periodic loops;
the runtime adds gain fades. `mixed_preview.wav` is a 17-second listening preview
at the recommended 45% master volume, including a preview-only final fade.
Runtime playback always uses the individual stems.

Mechanical audio uses millisecond broadband impacts, short inharmonic metal
resonances, case knocks and spring returns. `tumbler_clicks.wav` contains 15
distinct dry contacts. Ring tooth contacts follow the same 72/60/48-tooth counts,
rotation angles and smooth velocity curve as the visual rings, layered with
bearing drag and metal friction. These are procedural mechanical effects, not
recorded samples or electronic chirps. `charged_hum.wav` maintains the louder
full-power reactor spectrum throughout an arbitrarily long readiness hold.

`mix_report.json` records duration, peak, RMS and SHA-256 for every stem and the
maximum-level nominal mix. The default master volume is 0.45; start at 0.3 on
small speakers. The mix has headroom before its protective -3 dB compressor.
Muting and silent startup do not alter animation time.

To regenerate with Python 3.10+ and NumPy: `python tools/generate_audio.py`.
All synthesis parameters and seeds are checked in. Different NumPy/platform
floating-point implementations may change least-significant PCM bits; use the
committed WAVs for byte-exact reproduction.
