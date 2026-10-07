# Nexus Core Voice System

Local-first text-to-speech, voice playback, and voice-design subsystem.
All synthesis runs on-device; no cloud TTS, no browser `speechSynthesis`
primary path, no LLM-generated audio.

## Architecture

```
assistant token stream ──▶ SentenceStreamer ──▶ SpeechTextFilter
                                                        │ SPEAK / SUMMARIZE / SKIP
                                                        ▼
                                          VoiceManager queue (ordered,
                                          cancellable, stale-safe)
                                                        │
                                                        ▼
        TTSEngine (interface) ──▶ KokoroEngine ──▶ float32 PCM @ 24 kHz
                                                        │
                                                        ▼
                              preset DSP chain (pure numpy, float PCM):
                              pitch/tempo → EQ → exciter → compressor
                              → neural/glass/micro parallel layers
                              → stereo decorrelation → limiter
                                                        │
                                                        ▼
                          AudioCache (bounded LRU WAV) ──▶ /api/voice/audio/<id>
                                                        │
                                                        ▼
                                  web playback (voice_global.js)
```

Module layout (`localcodeagent/voice/`):

| file | role |
|---|---|
| `types.py` | `VoicePreset` schema v1 + `SpeechMode` |
| `engine.py` | `TTSEngine` provider interface + engine registry |
| `kokoro.py` | `KokoroEngine` — lazy load, CPU-first, metrics |
| `assets.py` | verified model/voice download (SHA-256 pinned) |
| `dsp.py` | numpy-only DSP: pitch/tempo phase vocoder, FFT EQ/bandpass, exciter, compressor, bitcrush, modulated delay, AM, stereo width, limiter, WAV encode |
| `speech_filter.py` | block classifier — SPEAK/SUMMARIZE/SKIP + prose sanitizer + vocalization canonicalizer |
| `vocalizations.py` | semantic vocalization/gesture engine — detect → policy → adapter render |
| `streamer.py` | fence-aware incremental sentence segmentation |
| `presets.py` | preset store: official seed, CRUD, import/export, quarantine |
| `cache.py` | LRU WAV cache keyed by text+engine+voice+preset+speed |
| `manager.py` | queue, mute/cancel, segment registry, export, metrics |
| `stt.py` | microphone enumeration, Vosk/faster-whisper engines, hands-free session |
| `tools.py` | ToolRegistry entries (`voice_*`, `audio.*` permissions) |
| `official/` | shipped presets (`nexus-synthetic-isabella.json`, `nexus-isabella-chatterbox.json`) |
| `chatterbox.py` | `ChatterboxEngine` — drives the isolated runtime over JSONL stdio |
| `chatterbox_worker.py` | self-contained worker for the isolated runtime (ships as data; run by the runtime's python, not the backend's) |
| `chatterbox_assets.py` | pinned turbo model manifest (SHA-256 verified, idempotent) |
| `chatterbox_runtime.py` | relocatable CPython runtime provisioning (pinned pbs, torch+CUDA, pip) |
| `chatterbox_voices/` | shipped clone voices (Isabella reference + metadata) |
| `loudness.py` | pure-numpy BS.1770 loudness measurement/normalization |
| `reference.py` | clone reference-audio validation (duration/clipping/silence/format) |

## Engine

- **Model**: Kokoro-82M (`hexgrad/Kokoro-82M`), ONNX v1.0 build.
- **Package**: `kokoro-onnx==0.6.1` + `onnxruntime==1.30.0` + `phonemizer==3.4.0` + `espeakng-loader==0.2.4` + `numpy==2.3.5`.
- **License**: Apache-2.0 (model). `kokoro-onnx` MIT.
- **Python 3.14 note**: `kokoro-onnx` declares `<3.14`; verified working on
  CPython 3.14 installed with `--ignore-requires-python`. The build script
  does this explicitly.
- **Assets** (SHA-256 verified from files exercised by this build):
  - `kokoro-v1.0.onnx` — `7d5df8ec…a6c5`, 325,532,387 bytes
  - `voices-v1.0.bin` — `bca610b8…bf7d`, 28,214,398 bytes
  - Source: `github.com/thewh1teagle/kokoro-onnx` `model-files-v1.0` release.
- Default device: **CPU** (`voice_device=cpu`) so TTS never evicts a coding
  model from VRAM. Model load ≈0.8 s, warm synthesis RTF ≈0.43 (measured).

## Second engine: Chatterbox Turbo (v0.26.x)

Chatterbox is a **cloned-voice engine**, not a bundled dependency: torch +
`chatterbox-tts==0.1.7` are far too heavy for the frozen backend, so the
provider spawns a persistent worker (`chatterbox_worker.py`) inside an
isolated runtime at `runtime/voice/chatterbox` (relocatable CPython 3.12 +
torch 2.6.0+CU124, provisioned by `chatterbox_runtime.py` — no system
Python required) and talks **JSONL over stdin/stdout**. Commands: `ping`,
`load`, `prepare_voice`, `synthesize`, `capabilities`, `status`, `unload`,
`shutdown`; failures return `{"ok": false, "error": ...}` and stderr is
pumped separately so diagnostics can never corrupt protocol lines.

- **Model**: `ResembleAI/chatterbox-turbo` rev `749d1c1a` — 9 pinned files,
  SHA-256 verified (`chatterbox_assets.MODEL_FILES`).
- **Device policy**: `voice_chatterbox_device=auto` picks CUDA only when
  ≥ `voice_chatterbox_min_free_vram_mb` (default 3200 MB) is free — the
  LLM/image lanes own the GPU first. Measured on RTX 3080 Ti: ~8.5 s cold
  load, ~2.8 GB VRAM resident, RTF ≈0.3.
- **Voices**: the preset's `base_voice` selects a clone voice directory.
  Official voices ship in `localcodeagent/voice/chatterbox_voices/<id>/`
  (voice.json + reference.wav, like official presets); user-installed
  voices under `models/voice/chatterbox/voices/<id>/` merge and shadow.
  Reference audio must validate (`reference.py`: >5 s, no clipping, no
  silence-dominated or corrupt files). Turbo's conditionals are global —
  the worker re-prepares on every voice switch.
- **Turbo caveats**: `exaggeration`/`cfg`/`min_p` are ignored by the model
  (`emotion_adv=False`); emotion delivery is tokenizer tags + temperature.
  `[laugh] [chuckle] [sigh] [gasp] [cough] [groan] [sniff] [shush]
  [clear throat]` plus emotion/delivery tokens are probed per-runtime —
  a tag only counts when the tokenizer encodes it as a dedicated single
  token.
- **Loudness**: raw turbo output is quiet (~−27 LUFS after its reference
  conditioning). `loudness.py` applies BS.1770 normalization to a target
  (default −17 LUFS, `voice_target_lufs`) after output gain and before
  the limiter. Presets opt in via `normalize_loudness`; the master switch
  is `voice_normalize_loudness`.
- **Fallback**: a `VoiceEngineError` from Chatterbox (missing runtime,
  dead worker, VRAM floor) degrades to Kokoro rather than dropping speech.
  The Kokoro voice is remapped (`isabella` → `bf_isabella`, the approved
  reference's own source) and cached under a separate engine key so it
  never masquerades as Chatterbox output.
- **Packaging**: `chatterbox_worker.py` ships via explicit `--add-data`
  (.py files otherwise vanish into the PYZ and the worker can't spawn —
  the v0.26.1 fix). Official voices ride the same mechanism.
- **Vocalizations**: `ChatterboxVocalizationAdapter` maps the planner's
  semantic styles to native tags where the runtime confirms them,
  natural text forms otherwise; the adapter follows the active preset's
  engine. `/api/voice/capabilities` exposes the per-engine tag table;
  `?probe=1` forces the live probe (loads the model, ~15 s cold).
- **Voice Studio**: engine selector, per-engine voice list, loudness +
  limiter controls, and Voice Lab (tag audition buttons gated by the
  runtime capability probe).

## Speech-to-text

Push-to-talk uses the browser microphone/`MediaRecorder` and posts audio to
`/api/stt/transcribe`; faster-whisper decodes browser audio locally through
PyAV and returns text for the composer. `stt_auto_submit` controls whether a
successful transcript submits immediately or remains editable. Hands-free
sessions use `VoiceSession`; Vosk supplies streaming partials when
`vosk_model_path` is configured, while faster-whisper supplies non-streaming
file/chunk transcription.

Install the optional host stack with:

```bash
python -m pip install ".[voice-stt]"
```

The `voice-stt` extra pins `sounddevice==0.5.6`, `faster-whisper==1.2.1`,
and `av==18.1.0`. PyAV 19.x removed an `av.open()` keyword consumed by
faster-whisper 1.2.1, so the compatibility boundary is explicit rather than
floating. On this host the `base` faster-whisper model has been initialized
for CPU/int8 and a WAV smoke transcription completed successfully.

## Official preset: `nexus-synthetic-isabella`

Base voice `bf_isabella` (British female). Character: natural female voice
first, clearly synthetic intelligence underneath — not robotic/vocoder/
telephone. Initial DSP recipe (all preset-tunable):

- pitch +1.25 st, tempo 0.98, light formant pull-back
- EQ: −3 dB @220 Hz, +5.8 dB @3.4 kHz, +3.2 dB @7.2 kHz
- exciter 0.44; 3:1 fast compressor
- **Neural layer**: 700–6900 Hz band, 9-bit crush (wet .78, aa .58,
  sr-reduce 1.55), 1.8 ms modulated delay (decay .28, rate .48 Hz),
  38 Hz AM @0.16 depth, mixed at 24%
- **Glass layer**: ×1.245 pitch, formant-preserved, 1900–9200 Hz, 8 ms
  echo, 8.5% mix
- **Micro layer**: ×0.965 pitch, 900–4800 Hz, 12-bit light texture,
  15 ms reflection, 6% mix
- stereo width 1.5 (layers decorrelated, main centered; mono-safe)
- limiter ceiling 0.89

`synthetic` (0..1) scales layer mixes + exciter + EQ depth — 0 ≈ raw base
voice, 0.8 = the approved Nexus character. It is **not** a single wet/dry
knob.

## SpeechTextFilter

Block-level classifier — never dictates code. SPEAK: prose, headings,
simple bullets. SKIP: code fences, commands, diffs, stack traces, logs,
JSON-ish lines, base64, hashes, UUIDs, URL-dense lines. SUMMARIZE: tables.
Skipped content earns a short spoken substitution ("I've included the code
in the response."). Inline cleanup strips markdown, expands paths to their
basename words, shortens identifiers.

## Vocalization Engine (`vocalizations.py`)

Non-verbal reactions (hums, sighs, chuckles, laughs, gasps, scoffs, yawns,
throat-clears…) as a semantic layer between prose filtering and synthesis —
the model writes `Mmm…` or `*sighs*`, the engine decides whether/how it is
voiced. Raw letter-spelling (`MMM` → "em em em") can never reach the
synthesizer.

```
streamer / direct text
   → SpeechTextFilter.canonicalize()   CAPS tokens → lowercase canon
   → VocalizationEngine.resolve()      detect → policy → render
   → speech_text (TTS input) + display_text (unchanged) + gesture events
```

- **Detection**: canonical token classes (agreement `mm-hmm`, thinking
  `hmm`, pleasure `mmm`, realization `ahh`, surprise `ooh`, laughs
  `haha/hehe/heh`, frustration `ugh`, sympathy `aww`, scoffs/tsk, yawns,
  sighs, etc.) plus `*stage directions*` (`*laughs*`, `*sighs*` → semantic
  acts, never spoken literally).
- **Semantics**: each hit resolves to `{category, style, intensity,
  duration}` — e.g. `ugh` → `frustration/exhale`.
- **Policy**: profile-scoped level `off|minimal|natural|expressive`
  (persisted in `<profile>/personality.json`, default `natural`), persona
  frequency bias (professional ≈ restrained … playful ≈ generous), mood
  allow-lists (concerned mutes amusement, focused drops most), personality
  strength scaling (0 ≈ neutral), per-response caps (natural 2, expressive
  3), ≥2-sentence spacing, same-style repeat suppression, minimal-mode
  intensity ceiling, adult render variants gated on 18+ profiles.
- **Priming**: `begin_task(user_text)` seeds a *possible* leading reaction
  ("that finally worked" → satisfied exhale / relieved sigh) — applied only
  if the model didn't already open with a reaction, so it can stack with
  but never duplicates the model's own.
- **Render**: engine adapters translate semantics to TTS-safe text
  (`mmm`→`mmmm…`, `mmhmm`→`mm-hmm`, `haha`→`ha ha`, `heh`→`heh`,
  `tsk`→`tsk`). Adapters are per-engine (`adapter_for(engine_id)`) — no
  Kokoro-specific hacks in the semantic layer; a bad rendering falls back
  to a milder form or silence, never breaks the response.
- **display vs speech**: `resolve()` returns `display_text` untouched and
  `speech_text` rewritten — chat shows `Mmm…`, Kokoro hears `mmmm…`.
- **Gestures**: kept vocalizations emit paired semantic gestures
  (`mm-hmm`→`small_nod`, `hmm`→`head_tilt`, chuckle→`small_smile`,
  gasp→`eyebrow_raise`, sigh→`exhale`). They ride `voice` segment events
  (`gestures[]` + `utterance_id` = segment id, for future lip-sync) and a
  standalone `gesture` SSE channel; `voice_global.js` sets
  `document.body.dataset.gesture` and notifies `web/avatar.js`, which maps
  them onto the canonical portrait's bounded expression/motion hooks.
  Gesture count is capped per response — no constant bobbing.
- **Telemetry**: `vocalization_resolved` stats deque (category/style/kept,
  no text content) + `record_feedback(profile_id, "fewer sighs")`
  suppresses a category per profile.
- Code/JSON/structured output never enters this path — the fence-aware
  streamer and filter drop it upstream.

## Spoken notices (v0.18.0)

User-visible system activity speaks through the same persona layer:

- **Worker lifecycle** — user-initiated queued work announces
  `queued → started → completed / failed / cancelled` via
  `persona_notice` (`_speak_notice`), once per event, mute-respecting
  (muted jobs are dropped in the speech queue). Background/mission
  worker churn never speaks.
- **Notifications** — a daemon (`nexus-voice-notices`) drains the event
  bus; `important`/`failure`/`approval`/`completion` rows — plus
  info-level report/briefing rows — become short persona-flavored
  spoken notices. Rate-limited (20s gap, 5s for failure/approval) so a
  noisy incident cannot monologue; deduplicated by notification id.
- **Vocabulary** — the engine knows ~18 additional casual token forms
  (yep/mkay/gotcha/nope, uh-oh, welp, yikes, whew, bwahaha…), ~25 more
  stage directions (smirks, nods, winces, perks up, rolls eyes,
  whispers, sighs happily…), render forms for every style, and
  feedback categories for giggles/gasps/moans/yawns/whispers/groans.
  Multi-word forms outrank their components ("uh-oh" ≠ "uh"), and
  prose words ("of"/"off", "a gasp", "she scoffed") aren't vocalized.
- **Capacity events** — `worker_capacity_reduced` /
  `worker_capacity_restored` speak once per distinct ceiling;
  oscillation re-speaks by clearing the opposite direction's dedup id.
- **Queue rotation** — each persona style has two queue-announcement
  phrasings that alternate via a per-style cursor.
- **Voice commands** — "stop talking"/"be quiet"/"voice off" mute
  (with a spoken farewell *before* the mute lands);
  "speak again"/"unmute"/"voice on" restore; "speak faster"/"slow
  down"/"normal speed" adjust the profile's `voice.json` speed bias
  (0.6–1.5×, persisted, applied on top of the persona delivery map);
  "speak louder"/"talk softer"/"normal volume" adjust a `gain_db` bias
  (±6 dB, additive with the persona gain delta);
  "say that again"/"repeat it"/"what did you say" replay the last
  spoken utterance (`repeat_last`).
- **Burst collapse** — 3+ user tasks queued inside a 30 s window speak
  one "More tasks joined the queue." line per window.
- **Greetings** — the profile greeting speaks once per process per
  profile via `speak_greeting` (mute-aware, vocalization-safe).
- **Reason-aware queue notices** — dependency/approval/model waits
  append the real reason rather than blaming worker availability.

Personality Studio gains a *Natural Vocalizations* section: level select
(profile-scoped) + per-style preview buttons hitting
`GET /api/voice/vocalizations` (style catalog) and `POST /api/voice/preview`.

## Queue / cancellation

- Per-task `SentenceStreamer` (fence-aware; never emits half words).
- `begin_task(new_id)` cancels queued+active speech from the prior response.
- `voice_muted=true` → immediate stop + queue clear (client stops the
  `Audio` element instantly, server cancels pending synthesis).
- Segment ids are unique per synthesis; replay hits the LRU cache.
- TTS failure emits a `voice` error event; the text response is unaffected.

## API

```
GET  /api/voice/status            engine+queue+cache+metrics
GET  /api/voice/presets           all presets
GET  /api/voice/preset/<id>       one preset
GET  /api/voice/preset/<id>/export
GET  /api/voice/voices            installed base voices
GET  /api/voice/vocalizations     style catalog + levels (Personality Studio)
GET  /api/voice/audio/<seg_id>    WAV bytes
POST /api/voice/speak             {text, preset_id?, speed?, auto_filter?}
POST /api/voice/preview           {preset|preset_id, text?, raw?, base_voice?}
POST /api/voice/mute              {muted}
POST /api/voice/stop              {reason?}
POST /api/voice/config            {voice_* fields}
POST /api/voice/preset/save|duplicate|rename|delete|import
POST /api/voice/export            {segment_id, format: wav|mp3, name}
POST /api/voice/assets/install    verified asset download (job, progress events)
GET  /api/stt                     backend/device/session status
POST /api/stt/transcribe          {audio(base64), format} → transcript
POST /api/stt/session             {action: start|status|stop, device?}
```

During `/api/chat/stream`, token deltas feed the streamer and `voice` SSE
events (`segment`, `stop`, `error`, `muted`) reach the requesting client and
the shared event bus.

## Tools

`voice_synthesize` (audio.generate), `voice_preview` (audio.generate),
`voice_list`, `voice_preset_list/get/export`, `voice_status` (audio.read),
`voice_preset_save/duplicate/delete/import` (audio.manage). Preset
mutations honor permission levels; official presets are protected.

## Config (`config.json`)

`voice_enabled`, `voice_muted`, `voice_engine`, `voice_preset_id`,
`voice_mode` (off|responses|responses_activity|manual), `voice_volume`,
`voice_speed`, `voice_output_device`, `voice_assets_dir` (`models/voice`),
`voice_presets_dir` (`data/voice/presets`), `voice_cache_dir`,
`voice_cache_max_mb`, `voice_device`, `voice_max_concurrency`,
`voice_idle_unload_seconds`, `stt_backend`, `stt_model`, `vosk_model_path`,
`stt_auto_submit`.

Chatterbox (v0.26.x): `voice_chatterbox_runtime_dir`
(`runtime/voice/chatterbox`), `voice_chatterbox_device` (auto|cpu|cuda),
`voice_chatterbox_min_free_vram_mb` (3200),
`voice_chatterbox_synth_timeout_s` (240). Loudness:
`voice_normalize_loudness`, `voice_target_lufs` (−17),
`voice_limiter_enabled`.

## Installer / upgrades

Setup downloads `kokoro-v1.0.onnx` + `voices-v1.0.bin` into
`{app}\models\voice` with SHA-256 verification (`ShouldDownloadKokoro*`
checks skip already-trusted files; `SkipModelDownloads` flag honored).
`data\` is excluded from install targets, so custom presets, cache and
voice settings survive upgrades. In-app fallback: Voice Studio → “Setup
engine”.

## Frontend

- `voice_global.js` on every page: global mute button, ordered playback
  queue, `/api/events` voice-channel listener.
- `index.html`/`app.js`: 🔊 control on every assistant message (replay uses
  stored text — never re-runs the model); new submissions stop prior speech.
- `voice.html`/`voice.js`/`voice.css`: Voice Studio — preset list, A/B
  compare, Natural↔Synthetic slider, character + advanced DSP controls,
  CRUD, import/export, diagnostics.

## Troubleshooting

- "voice assets missing or unverified" → Voice Studio → Setup engine, or
  rerun the installer.
- "kokoro-onnx is not installed" → build env lacks the voice deps; see
  `packaging/build_windows.ps1` install block.
- MP3 export unavailable → install ffmpeg (Tool Manager). WAV always works.
- Engine cold ≈0.8 s first-load delay on first utterance only.

## Known limitations

- Output-device selection is stored but the WebView2 page plays through the
  default audio device (no `setSinkId` enumeration yet).
- `voice_device=gpu` is accepted but ONNX Runtime GPU providers aren't
  bundled in the build — CPU policy is the tested path.
- Phase-vocoder pitch shifting adds mild spectral smearing at extreme
  semitone offsets; the ±6 st UI bound keeps it clean.
