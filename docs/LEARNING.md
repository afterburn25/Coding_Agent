# Learning from Corrections

Nexus Core treats explicit user corrections as structured preference
candidates, not silent permanent rules. A message such as "stop asking me to
confirm" or "always test the installer first" is detected by
`localcodeagent/preferences.py`, persisted under
`data/preferences.json`, and only becomes active after repeated evidence or
explicit user activation.

## Lifecycle

```text
user correction
      ↓
candidate preference (inactive, evidence counted)
      ↓ repeated / user activates
active overlay
      ↓
bounded prompt context for chat or project missions
```

Users can inspect and control rules in **Settings → Profile → Learned
Preferences**. Every row can be activated, paused, edited, re-scoped, or
forgotten. Duplicate edits conflict explicitly rather than silently merging.

## Scopes

- `global` — applies to all profiles/projects.
- `profile:<profile_id>` — visible only while that profile is active.
- `project:<workspace-or-project-id>` — applies to the current workspace in
  chat and to a linked project in mission planning.

The API filters list/edit/forget operations to scopes visible to the active
profile, so one profile cannot inspect or mutate another profile's private
rules.

## Safety boundary

A learned preference is an overlay only. It can steer style and workflow
("use shorter responses", "run targeted tests first"), but it cannot override:

- creator-locked identity facts (`identity.locked_topic`)
- permissions, approvals, safety policy, authentication, or verification
- secrets or credentials
- factual correctness

Unsafe or secret-looking corrections are rejected before storage. Active
overlays are rendered as a bounded list — never raw transcript history — and
are capped before entering prompt context.

## Interfaces

- `PreferenceStore.observe(text, profile_id, project_id)` — capture a
  correction candidate from a user turn.
- `PreferenceStore.overlay_text(profile_id, project_id)` — bounded active
  rules for prompt/mission context.
- `GET /api/preferences` — visible rules.
- `POST /api/preferences/add` — explicit rule creation.
- `POST /api/preferences/update` — edit text/scope/active state.
- `POST /api/preferences/toggle` — activate or pause.
- `POST /api/preferences/forget` — delete permanently.

Answer Memory remains the store for corrected question/answer facts;
`PreferenceStore` is for reusable behavior guidance. Conversation Memory and
Nexus Brain keep their existing trust and creator-authenticated boundaries.
