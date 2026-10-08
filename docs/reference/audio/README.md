# Isabella V7 Golden Reference

This folder contains the **user-approved Isabella V7 sound target** for Nexus Core.

## Golden audio

`isabella-v7-approved-golden-2s.mp3`

This is an exact excerpt from the user-approved V7 audition. Use it for
A/B listening, spectral/loudness comparison, and regression evaluation.

**Do not use this file as Chatterbox conditioning audio.**

Chatterbox Isabella must continue to condition from the dry source:

`localcodeagent/voice/chatterbox_voices/isabella/reference-source.mp3`

The intended pipeline is:

`dry bf_isabella -> Chatterbox Turbo -> V7/V6-style Nexus DSP once -> loudness/limiter`

The failure we are preventing is:

`already-synthetic Isabella -> Chatterbox clones DSP coloration -> DSP applied again`

That double-processing produced the hollow / "inside a barrel" sound.

## Locked target

Preset signature:

`approved-v7-v6-character-louder`

Preset file:

`localcodeagent/voice/official/nexus-isabella-chatterbox.json`

See `isabella-v7-approved-golden.json` for hashes and measurement metadata.
