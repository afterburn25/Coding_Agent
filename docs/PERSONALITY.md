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

## Versioning

Every preset carries `behavior_version` (`presets.BEHAVIOR_VERSION`,
currently 2). Customs and imported packages record the version they
were created against so future behavior-profile changes can migrate
rather than silently shifting.

## API

`POST /api/profiles/<id>/personality` actions:

- existing: `set_active`, `set_strength`, `set_mood`,
  `set_vocalizations`, `create_custom`, `patch_custom`,
  `delete_custom`, `reset`, `preview`
- new: `adjust_overlay` {trait_offsets, note}, `reset_overlay`,
  `add_modifier` {trait_offsets, note, ttl_seconds, scope, mode},
  `clear_modifiers` {scope}, `set_mode` {mode},
  `coherence` {traits, voice}, `blend` {blend, name},
  `export` {personality_id}, `import` {package}

`GET /api/profiles/<id>/personality/effective?text=...` — the compiled
debug card (no private memory).

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
