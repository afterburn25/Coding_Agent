# Persona Depth — Behavior Profiles, Dynamics & Effective Personas

The persona system has two layers. `personality.json` (per profile)
holds the *surface* — active preset/custom, sliders, strength, mood,
vocalization level. `persona_state.json` (per profile) holds the
*dynamics* — familiarity, mood decay, overlays, temporary modifiers,
mode, recent phrasing. Both are presentation-layer only: they steer
**how** Nexus talks, never **what** is true, permitted, or computed.

## Architecture

```
built-in preset / custom persona      (sliders + family + voice)
        + learned overlay             (per-profile trait offsets)
        + temporary modifiers         (expiring deltas / modes)
        + mode overlay                (coding/debugging/serious/…)
        + mood                        (dynamics engine or manual)
        + relationship                (familiarity stage)
        + seriousness + topic         (from the user message)
        ──▶ effective.py: compile_effective()  →  effective card
        ──▶ prompt.py: card_guidance()         →  ~14 prompt lines
```

## Behavior profiles (`behavior.py`)

Each greeting-style family (default, professional, warm, playful,
nerdy, calm, sassy, rude, flirty, raunchy) carries a stable profile:

- **motivations** — what the persona steers toward (never overrides
  the user's request)
- **aversions** — non-safety style dislikes (guides style, not refusal)
- **rhythm** — compact|measured|conversational|energetic|formal|
  fragmented|narrative|analytical
- **signature** — probabilistic habits: opener/closer style, analogy /
  rhetorical-question / teasing / summary / callback tendencies,
  preferred answer structure. Rendered as *tendencies*, never
  catchphrases.
- **styles** — humor_type, question_style, teaching_style,
  challenge_style, praise/criticism styles, decision_style,
  plan_style, confidence phrasing, error-admission and tool-failure
  phrasing, turn-taking, interruption posture, silence discipline
- **vocal_prefer / vocal_bias / gesture_prefer** — feed the
  VocalizationEngine policy and gesture intensity
- **warmup_rate / baseline_affect** — relationship speed + resting mood

`PRESET_BEHAVIOR` overrides per preset id (taskmaster, minimalist,
mentor, researcher, engineer, strategist, critical-reviewer, deadpan,
mysterious, …) merge over the family defaults. Built-ins are never
mutated.

## Seriousness + topic (`seriousness.py`)

`classify_seriousness(text)` → casual|neutral|focused|serious|critical
with severity precedence (critical outranks casual markers).
`classify_topic(text)` → casual|technical|diagnostics|personal|review|
operational|discovery|creative for `topic_shift` cues.

Seriousness scales persona expression: focused lightens color,
serious/critical drop humor, teasing, vocalizations, and flourishes
entirely. Facts and task behavior are untouched.

## Dynamics (`dynamics.py`)

`PersonaDynamics(profile_dir)` persists `persona_state.json`:

- **Relationship** — `familiarity` 0–100 grows asymptotically
  (warmup_rate per persona: playful/warm fast, professional/formal
  slow, mysterious slowest). Stages: new → familiar (15) →
  trusted (45) → long_term (80). This is interaction history, not
  simulated attachment.
- **Mood** — events from user text (`_MOOD_EVENTS`) transition the
  held mood; each turn without input decays intensity toward the
  persona's baseline affect. A manual mood (personality.json) always
  wins while set.
- **Overlay** — learned per-profile trait offsets + notes
  ("less sarcasm"), adjusted via commands or API.
- **Modifiers** — temporary `{trait_offsets, note, expires_at, scope}`
  records; expire by time or scope reset (conversation/task). Never
  silently permanent.
- **Mode** — `MODE_OVERLAYS` key (coding/debugging/teaching/
  brainstorming/serious/casual/reviewer/research).
- **recent** — bounded opener/closer history feeding the
  repetition feed-forward hints.

## Commands (`commands.py`)

Strict NL patterns handled without a model call:

| Say | Result |
|---|---|
| "be less sarcastic" | persistent overlay offset |
| "be more playful for the next hour" | timed modifier |
| "tone down the sass tonight" | timed modifier |
| "be more concise for this task" | task-scoped modifier |
| "use serious mode" / "be in debugging mode" | mode overlay |
| "reset personality" | clear modifiers + overlay + mode |
| "back to normal" | clear temporary modifiers only |

Ambiguous phrasing falls through to the agent unchanged.

Additional commands (v0.17.0): "call me Ash" / "don't use my name"
(preferred address, independent of Creator titles), "what are you
like?" (honest self-description from the resolved card), "reset the
learned adaptations" (clears continuity/adaptation state, keeps
familiarity and memory).

## Social layer (`social.py`, v0.17.0)

Deterministic, bounded — no model call:

- `classify_social(text)` → joking | sarcasm | frustration |
  confusion | excitement | disappointment | celebration | venting |
  uncertainty | casual | serious | neutral, with confidence.
- `detect_sarcasm(text, context_failed)` — positive surface wording
  over a negative event or explicit markers ("yeah right", "what a
  surprise", "went perfectly" after a failure). **Understanding ≠
  generation**: a persona may detect sarcasm it would never produce.
- `user_energy(text, cue)` → low|medium|high; `effective_energy`
  blends persona baseline + user energy + mood + seriousness +
  session length (long sessions cap energy; serious contexts cap at
  medium or below).
- `pacing_factor(turns)` — long-session taper: expression eases off
  over prolonged work (fewer jokes/vocals/filler); emotional turns
  (celebration, venting) get a pass.
- `tag_focus` / `topic_shifted` — current conversational focus and
  real topic-shift detection, so transitions bridge ("that covers
  the installer — on the UI issue…") instead of restarting.
- `recovery_cue(family)` — per-family phrasing for owning a
  misunderstanding.
- `confidence_delivery(level)` — verified → steady cadence;
  uncertain → measured with honestly-flagged hesitation. No random
  "hmm" on solid answers.

## Continuity (`continuity.py`, v0.17.0)

Stored inside `persona_state.json` — profile-scoped, bounded:

- **Milestones** — notable shared events only (project done,
  recurring issue, hard bug, success, learned preference, shared
  joke): `{type: shared_milestone, kind, event, importance, ts}`.
  Trivial exchanges never create one; near-duplicates refresh.
- **Callbacks** — `relevant_callback` picks a milestone when
  token-overlap × recency × importance clears the gate, and a
  `last_referenced` cooldown prevents nostalgic repetition. Old
  memories need much stronger relevance than recent ones.
- **Stated preferences** — when the persona states a subjective
  preference, `record_stated_preference` keeps it;
  `preference_consistency` re-surfaces it on-topic so the persona
  doesn't reverse itself.
- **Preferred address** — `set_address` stores a profile-level name
  (independent of Creator titles); `address_hint` scales usage
  frequency by persona family and gates it behind familiarity — new
  profiles never get heavy name use.
- **Saturation** — `note_expression` accumulates humor/sarcasm/
  vocal/gesture/intensity counters (half-life ~20 turns); high
  saturation drops humor a register (sarcastic → dry) and dampens
  `expression_scale` — strong personas get neutral moments instead
  of caricaturing.
- **Pattern memory** — `note_pattern`/`pattern_fresh` track recent
  analogy/humor structure tags so the same one isn't re-run too soon.
- **Humor adaptation** — `humor_feedback(style, ±)` accumulates into
  a bounded profile-level nudge ("user responds poorly to sarcasm —
  keep it rare") rendered into the card.

Per-family extras merged into every behavior profile: `curiosity`
(low/medium/high), `noticing` (which cues it picks up first), and
`stable_prefs` (standing subjective preferences, e.g. "prefers
concise technical explanations").

## Voice & gesture continuity

- `map_voice(..., previous=...)` — per-parameter step clamps
  (speed/pitch/gain) so delivery interpolates instead of jumping
  between adjacent utterances; a persona *switch* still applies at
  once.
- Vocalization engine context gains `hesitation_ok` (thinking-type
  vocals suppressed in serious/uncertain-wrong contexts) and
  `gesture_bias` + persona saturation dampening on `vocal_bias`.
- Gesture events carry `timing: "pre"|"with"` so avatar expressions
  align to the speech beat (a smile lands *as* the laugh starts).

## Introspection & QA (`introspect.py`)

- `describe_persona(card)` — first-person self-description built only
  from resolved card fields; never invents traits.
- `compare_personas(store, ids, text)` — Personality Studio
  side-by-side: effective summary, voice params, deterministic
  preview line for 2–3 personas on the same prompt.
- `persona_similarity(a, b)` / `similarity_audit` — 0..1 distance over
  character fields; ≥0.9 flags collapsed personas.
- `behavior_report(metrics)` — QA flags (high question rate, name
  overuse, humor-rate creep, callback overuse) over the aggregate
  `PersonaDynamics.metrics()` counters.

## Consistency (`consistency.py`)

- `check_repetition` — repeated opener/closer n-grams → flags + score
- `detect_drift` — surface violations vs the effective card
  (slang drift for professional, energy drift for calm, verbosity
  drift for compact personas, humor in serious contexts, color loss
  for expressive personas)
- `feedforward_hints` — "vary your opener — recently used: …"
  injected into the next prompt
- `consistency_report` — debug/eval roll-up

## Customization (`customize.py`)

- `coherence_check(traits)` — conflicts like "formality + slang both
  max" → `coherence: high|mixed|low` + notes. Informative only.
- `blend_presets` / `create_blend` — weighted merge ("70%
  Professional + 30% Playful") into a new custom. Numeric fields
  merge weighted; categorical fields follow the heaviest contributor.
- `export_custom` / `import_custom` — portable `nexus-persona`
  packages. Name, base preset, traits, voice, blend metadata, version
  — **never** personal memory or relationship state.

## Speech genome (`genome.py`)

The Persona Speech Genome is the versioned, structured identity of HOW
a persona talks — downstream of meaning (intent/tools/facts), upstream
of surface text. `derive_genome(personality)` layers:

```
neutral defaults → behavior-family genome → trait-slider nudges
→ explicit speech_genome overrides (deep-merge)
```

Sections: `vocabulary` (per-family acknowledgement/success/error/
disagreement/transition/interjection/signature pools), `syntax`
(sentence length/variance, fragment + one-word rates, dash usage, list
preference, answer-first, technical density, elaboration, rhetorical
rate), `cadence`, `pragmatics` (opening/closing family weights),
`humor` (12 categories with strength+frequency + allowed registers +
serious suppression), `disagreement`, `storytelling`, `questions`,
`repair`, `relationship` (familiarity/teasing/openness/callbacks),
`address` (policy: none/first/formal/literal-term + context-weighted
frequency + cooldown), `boundaries` (graded slang/profanity/emoji/
internet-speak ceilings), `vocal` (pace/energy/warmth/emphasis/
nonverbal biases), `micro_reactions` (per-context pools + cooldown),
`confidence` (verified/likely/inferred/uncertain phrase stems from the
behavior family), `repetition` (opening/closing/phrase cooldowns).

`migrate_genome()` fills missing/corrupt fields from defaults,
preserves unknown keys (forward-compatible import), and pins
`speech_genome_version` — old persona files upgrade without data loss.
Customs inherit their base preset's family genome and can carry a
`speech_genome` override dict via `create_custom`/`patch_custom`.

### Surface realization (`context/realize.py`)

`SemanticResponse` carries WHAT must be communicated — facts,
warnings, conclusions, uncertainty, evidence, completed/failed
actions, next steps, `confidence` (verified/likely/inferred/uncertain
set upstream), `exact_spans` (identifiers/numbers/canonical text that
must survive verbatim), `speech_act`, `semantic_id`, `register`.
`classify_speech_act()` maps intent + outcome + seriousness + social
cue to one of 34 acts; canned lanes keep their own act.

`PersonaRenderer.render_semantic(sem, genome, ctx)` realizes:

- micro-reaction prefix (genome pools, per-category cooldown,
  suppressed when serious)
- opening family (weighted by genome pragmatics, masked by act fit —
  a failure never opens "reaction"; `result_first` only on outcome
  acts so success terms never precede a plain answer)
- address term (policy + context-weighted + cooled — `first` uses the
  user's preferred name, `formal`/`none` stay name-free)
- body: facts/conclusions/warnings verbatim + confidence stem ONLY
  for non-verified content; `canonical=` bodies (built-ins, Answer
  Memory, identity facts) pass through untouched with honest repeat
  acknowledgements on re-asks
- closing family (hard_stop dominates; `next_step` only when
  next_options exist; `question` respects question frequency;
  `light_comment` pulls from the persona's own humor-category quips
  gated by allowed registers)
- `SpeechDeliveryPlan`: pace (genome vocal bias + tempo − serious
  slowdown), energy (mirrored user energy), warmth, emphasis_spans
  (= exact_spans), pause density, seriousness, act, register,
  nonverbal rate, sarcasm — handed to `voice.finish_task(delivery=)`
  which applies `pace` today; the rest are documented hints until the
  engine exposes controls.

`RenderedReply` reports `opening_family`/`closing_family`/
`micro_reaction`/`used_address`/`repeat_index`/`genome_rendered` for
observability. `PhraseCooldowns` keeps per-category recently-used
pools turn-bounded; the ledger still fingerprints whole replies.

### Wiring

`server._speech_context(user_text)` resolves the active profile's
`(genome, RenderContext)` — dynamics mood/relationship, seriousness +
topic classification, social cue + sarcasm + user energy, preferred
address. The orchestrator's `speech_context=` resolver feeds the
builtin lanes (`builtin_semantic` → `_builtin_reply`) and the Answer
Memory path; when a genome renders, canned replies are no longer
suppressed under an active persona — they answer in character without
a model call.

## Versioning

## API

`POST /api/profiles/<id>/personality` actions:

- existing: `set_active`, `set_strength`, `set_mood`,
  `set_vocalizations`, `create_custom`, `patch_custom`,
  `delete_custom`, `reset`, `preview`
- new: `adjust_overlay` {trait_offsets, note}, `reset_overlay`,
  `add_modifier` {trait_offsets, note, ttl_seconds, scope, mode},
  `clear_modifiers` {scope}, `set_mode` {mode},
  `coherence` {traits, voice}, `blend` {blend, name},
  `export` {personality_id}, `import` {package},
  `set_address` {address}, `reset_adaptations`,
  `adaptations` (inspect learned overlays/modifiers/continuity —
  counts and stances, no raw memory), `metrics`,
  `compare` {personas, text}, `describe`, `similarity` {a, b}

`GET /api/profiles/<id>/personality/effective?text=...` — the compiled
debug card (no private memory).

`GET /api/profiles/<id>/personality/speech-preview?target=…&register=…&seriousness=0-3&turns=1-4`
— renders an 8-act battery (greet, answer, success, failure, disagree,
warn, uncertainty, farewell) through any resolvable persona's genome:
text + delivery plan + opening/closing family + genome summary. Backs
the Personality Studio **Speech Lab** panel.

## Invariants

- Personality never alters facts, calculations, code correctness,
  tool arguments, permissions, safety, citations, or structured
  outputs — the boundary line is in every persona prompt.
- Built-in presets are immutable; every adjustment lands on the
  profile's dynamics or a custom copy.
- Everything is per-profile: overlays, modifiers, relationship, mood
  state cannot leak across profiles.
- Compilation is deterministic and cheap — a handful of dict merges
  and regex classifiers; no extra model call.
