# Nexus Core startup cinematic: 30-second visual handoff

This is an **asset and presentation handoff for Devin**, based on current main
`2e29fd8edbda980e7be8c007a607675c3b1b969d` (fetched before authoring).
It does not wire milestones, alter readiness/recovery, or change Isabella.

## Deliverables

- [Clean runtime master](../desktop/ChatNexus.Desktop/splash/assets/NexusCore-Startup-Glow-Only.mp4)
- [Captioned reference master](reference/NexusCore-Startup-Captioned-Reference.mp4)
- [Authoritative caption/tail timeline](../desktop/ChatNexus.Desktop/splash/startup_caption_timeline.json)
- [Review frames and verification](review/startup-milestones/)
- [Reproducible authoring tools](../tools/startup_cinematic/README.md)

Both movies are exactly **30.000 seconds, 900 frames, 1280 × 720, 30 fps**, H.264
High/yuv420p with AAC stereo at 48 kHz. The clean copy has **no baked milestone
text or progress bar**. Its existing small dark readout plate remains blank,
ready for the live DOM overlay. The reference adds only the readout. Branding
and decorative chamber lettering remain part of the approved artwork.

The current-main runtime movie was still 17 seconds. This export carries
forward the user's explicitly approved **30-second extension**, with the same
startup motion and a living fully powered ending. The core sphere, aperture,
shield, rings, iris, lighting and glow sizes are unchanged. All mechanical
events and SFX retain their original timestamps. No narration is in either file.

## Exact authored boundaries

Times below are **transition starts**, not real-work completion estimates.
They are specified before rendering, used directly for frame generation, and
verified against the rendered frame log. A 0.2-second dissolve follows each
boundary; the initial caption is already readable on frame zero.

| Frame | Seconds | Semantic gate | Primary | Secondary |
|---:|---:|---:|---|---|
| 0 | 0.0 | 0% | INITIALIZING · NEXUS CORE | Establishing core startup environment |
| 30 | 1.0 | 6% | CORE CONTROL · ESTABLISHED | Loading configuration and protected system state |
| 60 | 2.0 | 15% | STARTING · CORE SERVICES | Launching Nexus agent and service runtime |
| 90 | 3.0 | 30% | VERIFYING · CORE INTEGRITY | Confirming backend health and authorization |
| 120 | 4.0 | 55% | SYNCHRONIZING · NEXUS BRAIN | Restoring memory, models and system continuity |
| 150 | 5.0 | 72% | OPENING · COMMAND INTERFACE | Initializing the Nexus control environment |
| 180 | 6.0 | 85% | LOADING · NEXUS WORKSPACE | Connecting tools, profiles and workspace services |
| 255 | 8.5 | 93% | SYNCHRONIZING · CORE INTERFACE | Establishing communication with core systems |
| 300 | 10.0 | 98% visual hold | FINALIZING · NEXUS CORE | Verifying interface and system readiness |
| 366 | 12.2 | 100% | CORE SYSTEMS · ONLINE | Nexus Core ready |

The early captions have a one-second dwell (0.2-second dissolve and 0.8 seconds
fully legible), preserving the user's existing fast mechanical opening.
Workspace charging, synchronization and finalizing have longer dwells.
These associations span the existing visual phases; they do not retime the
mechanisms to manufacture backend milestones. Original landmarks remain:
security 0.7 s, authorization 1.6 s, lock turn 2.35 s, pins 3.15–3.69 s, rings
3.95 s, iris 4.75 s, core charge 5.65–9.65 s, stabilization 11.65 s, online
12.2 s. Devin should let real work determine live caption dwell.

## Readout presentation and safe holds

The readout stays in the existing lower centered region (left 28%, width 44%,
bottom 3.7%). At the original 1024 × 576 composition size, primary text is
12 px, weight 500, tracking .25 em; secondary is 9 px with .10 em tracking.
These scale to 15 px / 11.25 px in the 720p export. Use Segoe UI with Arial
fallback. Both lines are centered without an added subtitle box.

Normal colors: primary `#AEDFFF`, secondary `#7690AE`. At 12.2 s both lines
begin the same 200 ms cyan-to-green transition as their caption dissolve:
primary `#59FFA0`, secondary `#3DD77F`. Pulse phase starts there too. Use
`p = (1 - cos(2π × elapsed / 1.4)) / 2`, primary opacity `.45 + .55p`, secondary
opacity `.75 + .25p`, with a smoothly expanding green halo. It is not a blink.
The chamber and core keep their existing blue/cyan/violet treatment.

**Pause-safe FINALIZING:** hold frame **348 (11.6 s)**. It has full charge, open
iris, cyan text and a visibly incomplete 98% bar. The fully dissolved finalizing
caption is available from 10.2 s; the last pre-ONLINE frame is 365. The reference
bar stays at 98% throughout the hold, then eases to full across 12.0–12.2 s.
It never reaches 100% before ONLINE. These are visual values; live progress
must remain the host's value and must not be inferred from movie time.

**Online loop:** repeat **[24.4, 30.0)**, frames **732–899 inclusive**. This is
5.6 seconds / 168 frames / exactly four 1.4-second pulses. Frame 900 is not
encoded: its mathematically rendered picture equals frame 732 pixel-for-pixel.
The final 0.7 s dissolves only the settled moving light/particle layers to the
loop's incoming phase, keeping rigid geometry fixed. Premultiplied alpha is
blended correctly so translucent mist does not double in brightness. The
caption continues its normal pulse throughout. Hum uses the same loop phase
dissolve with **no end fade to silence**. Export forces keyframes at ONLINE
and loop start. No freeze, reverse playback or repeated startup is used.

For later integration, use these exact bounds instead of the old hardcoded
13.8-second tail start in the host page. Gate the transition to ONLINE on real
readiness, let the live caption overlay own both lines and the progress bar,
and continue the tail while narration/quiet-buffer/host transition completes.
Neither this timeline nor reaching EOF authorizes dismissal. The application
code is deliberately unchanged in this asset branch.

All seven optional real-work captions are in `optionalCaptions` in the JSON.
They are verified to fit this readout but are **not inserted into either movie**.
Only show them when Nexus actually performs that work.

## Current-main visual and audio provenance

The export imports `desktop/ChatNexus.Desktop/splash/web/renderer.mjs` and
`timeline.mjs` unchanged, with the production animation manifest and PCM audio
stems. The chamber plate is the one already tracked on current main at
`prototypes/core_unlock_splash/assets/nexus-core-splash.png`; it is the source
of main's actual startup MP4. Main's prototype-reference and production MP4s
were byte-identical (`bcc3ecc6…63955`). The production fallback PNG already
contains rendered mechanism artwork, so compositing the renderer over that
would duplicate the housing. This export uses the movie's correct source
plate and verifies the result against the current-main production video.

Review reports record full decode, all-frame scene alignment, original-main
scene similarity, original soundtrack correlation, codec metadata, hashes,
caption fit, pulse period, loop seam and Windows playback. Error and recovery
movies are unchanged byte-for-byte. All runtime code, startup gates and
narration files match the baseline Git objects (accounting for Windows line
endings); none is changed by this task.
