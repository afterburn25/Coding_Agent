# Profiles, Creator Identity, Personality & Onboarding

Nexus Core runs on durable human profiles. Every install boots into a
locked **Start Here** onboarding flow until a profile exists; all
protected APIs return `403 {error: "onboarding_required"}` server-side —
the UI redirect is convenience, not security.

## Architecture

```
data/profiles/
    index.json                  {"active": "<uuid>"}
    state.json                  app-level flags
    migration.json              idempotent legacy-association marker
    creator_credential.json     PBKDF2 verifier + rate-limit counters
    <uuid>/
        profile.json            identity record
        avatar.webp             512×512 normalized, circle in alpha
        personality.json        active preset/custom, strength, mood
        voice.json              profile-scoped voice selection
        settings                inside profile.json ("settings")
        memory/personal.json    isolated personal memories
```

Profiles are keyed by UUID — never by name. Identity fields
(`first_name`, `last_name`, `sex`, `birth_date`) are **immutable**:
`ProfileManager.patch` rejects them regardless of what the UI allows.
`is_creator`/`creator_*` are **protected**: produced only by the creator
verification path; a crafted create/patch payload containing them is
rejected outright. `age`/`is_adult` are **derived** from the immutable
birthdate on every read — nothing mutable can change eligibility.

## Onboarding & route lock

`ProfileManager.onboarding_required` (no valid profile dirs) drives the
gate in `Handler.do_GET/do_POST/do_PATCH`. Allowed while locked: static
assets/pages, `/api/onboarding/*`, `/api/profiles*`, `/api/creator/*`,
`/api/postal/*`, `/api/personalities`, `/api/health`, `/api/status`,
`/api/time`. `profiles_onboarding_gate` in config can disable it (test
harnesses only).

`web/profile.js` (loaded on every page) mirrors the gate: redirects to
`/start.html` while locked, and injects the profile switcher once
unlocked.

## Postal data

`localcodeagent/profiles/us_postal.json` bundles 51 jurisdictions → city
lists (~1.7k cities) for searchable offline dropdowns. **ZIP codes are
manual entry only** — validated `^\d{5}(-\d{4})?$` on both client and
server; nothing is auto-filled or guessed. The `PostalProvider`
protocol remains a swap point for a future geocoder.

## Creator authentication

The normalized full name `John Hamburn` is reserved (NFKC + casefold +
whitespace collapse — `john  hamburn` matches). Creating it without a
passcode returns `creator_verification_required`; a passcode on any
other name is rejected so no orphan credential exists.

- Verification: PBKDF2-HMAC-SHA256, 600 000 iterations, stdlib only.
- Bootstrap: the packaged verifier stores salt+digest — never the
  cleartext. First success **enrolls**: re-hashes under a fresh random
  salt into `data/creator_credential.json`, which then becomes
  authoritative.
- Rate limiting: `2^failures` seconds (cap 30 s), persisted — including
  pre-enrollment, so a restart can't reset guessing. Success resets the
  counter. Every failure returns the same neutral error — no oracle.
- The passcode is never stored in profile JSON, logged, sent to a model,
  or exposed by any API/diagnostic.
- `is_creator` does **not** widen tool permissions, approvals, or safety
  boundaries — it is a relationship/presentation flag only.

Creator profiles get private settings (`creator_address`,
`creator_title_*`) invisible in `public_profile` for ordinary profiles
and mutable only via `update_creator_settings`, which re-verifies the
passcode on every call.

## Personality

Presentation layer only — it shapes delivery, never facts, code
correctness, tool permissions, or safety state.

- **47 sliders** (`personality/schema.py::SLIDERS`) in 6 categories;
  7 are `adult_only`. Validation whitelists keys and clamps 0–100;
  adult sliders are *stripped* (not clamped) for minors.
- **73 built-in presets** (`presets.py`) — immutable; editing produces
  `Custom based on <preset>` with a stable UUID.
- **Personality Strength** (0–100) scales delivery away from neutral.
- **Mood** — one of 7 temporary states; small delivery nudges, never
  overrides profile settings.
- **10 voice controls** map onto real engine params
  (`voice_map.map_voice`): Kokoro `speed`, DSP `pitch_semitones`,
  `tempo`, `output_gain_db`. Breathiness/pause/emphasis/intensity have
  no direct engine equivalent → documented `preprocess` hints.

## Adult gating

`is_adult = compute_age(birth_date) >= 18`, derived from the immutable
birthdate — no editable checkbox. Enforced in `list_presets`
(exclusion, not flagging), `PersonalityStore.set_active/create_custom/
patch_custom` (rejection/stripping), `clean_traits`, and the greeting
service (flirty style → default). The UI hides the same controls.

## Greetings

`GreetingService` is the single structured place (no scattered strings):
inputs = profile + resolved personality + adult eligibility + health;
outputs = text + voice params.

- `has_completed_intro` false → the long once-per-profile introduction.
- Otherwise → personality-style returning template, rotated per profile
  via `greeting_seq.json`. Creators always hear their preferred address.
- Flirty templates degrade to default for minors; degraded health
  appends a short systems note.

## Isolation vs. sharing

Profile-scoped (isolated): personality, customs, strength, mood, voice
selection, avatar, settings, personal memories, greeting state.
Device/global (shared, never duplicated): chats, conversations, models,
ComfyUI, voice assets/presets, Nexus Brain, project knowledge.

## Migration

`migrate_legacy_state` runs on the first successful profile create:
writes `migration.json` (schema_version, timestamp, first-profile id,
preserved top-level dirs). Repeat runs return `already_migrated` —
nothing is moved, copied, or wiped; existing installs lose nothing.

## Security model

- Every rule enforced in the UI is re-enforced on the backend.
- Profile ids are validated before any filesystem access (no traversal,
  no dirs created by reads).
- Avatar uploads are fully decoded + `verify()`'d by Pillow, size-capped
  (25 MB), min-dimensioned, and normalized to a fixed 512 px WebP.
- `public_profile` is the only API-facing view — derived fields added,
  creator-only fields stripped for non-creators.
- Arbitrary personality JSON can only touch whitelisted keys — it can
  never override system behavior.
