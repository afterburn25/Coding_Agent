# Startup milestone cinematic validation

Validated locally on Windows, 2026-10-05. Baseline main:
`2e29fd8edbda980e7be8c007a607675c3b1b969d`.

| Check | Result |
|---|---|
| Both finished MP4s | 30.000 s, 900 frames, 1280 × 720, 30 fps |
| Encoding | H.264 High/yuv420p, AAC LC stereo 48 kHz, fast-start MP4 |
| Full frame/audio decode | Both passed without corruption/errors |
| Windows Media Player engine | Both opened, reported 30 s, entered playing state and advanced with zero errors |
| Real WebView2 host | Both decoded and played at 0, 11.6, 12.2, 24.4 and 29.5 s; EOF → loop-start seeking repeated twice |
| WebView2 version | 154.0.4258.53 |
| Clean/reference source alignment | Scene pixels identical at nine representative timestamps; shared deterministic time samples |
| Encoded scene alignment, all 900 frames | SSIM 0.997921 (independent lossy encoding of text/clean versions) |
| Main's original scene, first 510 frames | SSIM 0.993754; original source renderer, geometry and timing unchanged |
| Main's original audio, first 15.5 s | Decoded correlation 1.0; original SFX schedule and mix preserved |
| Audio between new masters | Compressed audio streams identical |
| FINALIZING hold | Frame 348 / 11.6 s: full core, open iris, cyan text, 98% bar |
| ONLINE boundary | Frame 366 / 12.2 s; 200 ms caption/color dissolve |
| Pulse measured in final encoded caption | 42 frames / 1.4 s; repeat correlation 0.995926 |
| Loop | [24.4, 30.0), 168 frames, four pulse cycles |
| Unencoded loop endpoints | Exact pixel equality, including captions and translucent effects |
| Decoded seam | Mean change 0.729 clean / 0.804 reference on an 8-bit 0–255 scale; sub-one-level compression difference at seekable keyframe |
| Hum seam | Continuous source waveform; no end fade or silence |
| Caption overflow | Zero in 900 authored frames; all seven optional captions fit |
| Progress | No frame reaches 100% before ONLINE |
| Narration | No speech input; audio source-stem manifest recorded |
| Error/recovery assets and production source | Same Git objects as baseline (binary media unchanged byte-for-byte) |
| Existing branding/splash unit suite | 38 tests passed |
| Desktop packaging build | Succeeded, zero errors; existing WindowsBase/WebView2 WPF reference warning MSB3277 remains |
| Standalone playback-probe build | Succeeded, zero warnings/errors |

The 13 PNGs are extracted from the **finished captioned MP4**, not only from
the authoring page. Open [the review gallery](index.html) or compare the
individual images. JSON reports provide exact hashes, frame numbers, playback
results, source audio paths and file sizes.

This verification covers the visual assets and playback. Wiring actual loading
milestones, readiness, live captions, narration and host loop boundaries is
Devin's subsequent task; those application behaviors were not changed here.
