# Nexus Avatar and Lip-Sync Foundation

Nexus uses the canonical portrait resolved by `localcodeagent/nexus_avatar.py`.
`web/assets/nexus-portrait.{webp,png,jpg}` remains the identity source; when it
is absent the CN emblem is used. The animation layer must never substitute a
different face or character.

## Event flow

```text
VoiceManager queued/segment/stop
  → AppState._voice_publish
  → NexusAvatar activity state + SSE voice events
  → web/voice_global.js playback
  → web/avatar.js presence state

VocalizationEngine gesture
  → gesture SSE event
  → document.body[data-gesture]
  → subtle CSS motion + semantic expression hint

/api/nexus/state
  → web/avatar.js operational fallback
  → idle / focused / concerned presentation
```

The frontend polls actual `NexusVoice.current` playback, so speech is never
delayed to animate the portrait. When phoneme/viseme timing is unavailable,
utterance duration plus a stable segment-derived phase drives a bounded
viseme-like mouth cue; audio samples and speech text are not consumed.

## States

`NexusAvatar` exposes two bounded state channels:

- `expression`: semantic expression (`neutral`, `friendly`, `happy`,
  `focused`, `concerned`, `playful`, `confident`) driven by gesture events.
- `activity`: observable runtime state (`idle`, `listening`, `thinking`,
  `speaking`) driven by voice lifecycle events and client playback/STT state.

Both expire back to neutral/idle so stale events cannot leave Nexus animated
forever. `/api/nexus/avatar/status` reports the active state for diagnostics
and future renderers.

## Renderer contract

`web/avatar.js` owns the small top-bar `#nexusPresence` portrait on the chat
page. Precedence is:

```text
speaking > listening > thinking > operational state
```

CSS provides the initial effects only: breathing, attention ring, small nods,
head turns/tilts, blink/alert cues, and a bounded speaking mouth indicator.
All animation honors `prefers-reduced-motion` and uses no image generation,
large DOM graph, WebGL, model inference, or high-frequency worker.

## Safety and failure boundaries

- No speech text, microphone audio, or conversation payload is sent to the
  avatar layer; only lifecycle metadata and semantic gesture names are used.
- Missing JavaScript, CSS, portrait derivatives, or voice playback leaves the
  static portrait and text chat fully functional.
- Voice/TTS failures emit normal voice errors and cannot block chat,
  missions, or model execution.
- Operational state is a measured presentation hint, not a claim of
  consciousness.
