# Nexus Self-Knowledge

Nexus's answers about Nexus Core are generated from a canonical feature
catalog composed against live runtime state — never from static prose.
This document covers the *knowledge* half; `CHAT_CONTROL_PLANE.md`
covers the mutation half.

## Architecture

```
localcodeagent/self_knowledge/
  catalog.py   — FeatureSpec + FeatureCatalog  (what exists + dev status)
  pages.py     — PageSpec + PageRegistry       (every UI route + deep links)
  settings.py  — SettingSpec + SettingsRegistry (chat-mutable settings)
  actions.py   — ActionSpec + ActionRegistry   (typed risk-classed actions)
  service.py   — SelfKnowledgeService          (NL → resolution → reply)
```

The service is built in `AppState._build_self_knowledge()` with a lazy
env of callables into the real subsystems. It is handed to the
orchestrator as `self_knowledge=lambda: self.self_knowledge` so wiring
order never matters; a build failure leaves `self_knowledge = None` and
the lane silently no-ops.

## Two kinds of truth — never conflated

| Field | Meaning | Values |
|---|---|---|
| `development_status` | What the code supports | implemented, partial, experimental, planned, not_implemented, deprecated |
| `runtime_state` | What works right now | verified, available, degraded, setup_required, authorization_required, unavailable, broken, disabled |

Runtime state comes from the **Capability Registry**
(`localcodeagent/capabilities.py`) via `FeatureSpec.capability_id`, or
from a custom `probe` key resolved through the injected env
(`env["probe"](name) -> {"state": ..., "detail": ...}`). The catalog
never re-implements a probe.

## The lane

`AgentOrchestrator._self_knowledge_reply()` runs in Tier 0 alongside the
GitHub-status lane — before `suppresses_canned()` so control requests
that classify as ACTION intents still resolve locally. It:

1. Calls `SelfKnowledgeService.respond(user_text)` → `Resolution`.
2. Renders `Resolution.text` through the persona genome
   (`PersonaRenderer.render_semantic`) — persona controls wording, never
   truth.
3. Attaches `Resolution.actions / links / controls` to
   `RenderedReply.ui`, which flows through `AgentResult.ui` →
   `_agent_payload()["ui"]` → the chat renderer.

`can_answer_self_knowledge()` runs `service.would_answer()` — a dry run
that resolves the turn without mutating — so "turn voice off" doesn't
409 when no coding model is resident.

## What the service resolves

In `respond()` order:

1. **Follow-ups** — `do it` / `yes` → pending proposal; `undo that` /
   `turn it back on` → undo of last executed action; `open it` → last
   deep link.
2. **Diagnostics** — `what can you do` (live catalog grouped by
   category), `what's broken`, `what needs setup`, `what can't you do`,
   `what version`.
3. **Control** — `turn voice off`, `mute yourself`, `use isabella`,
   `use comfyui`, `set workers to 4`, `turn the volume to 30%`,
   `make it quieter`. Imperative-only: a registered action fires only on
   an imperative verb; a bare alias in a question never mutates.
4. **Navigation** — `where is X` → deep link; `open X` → navigate card.
   Section links like `nexus://` are expressed as `/page.html#section`.
5. **Status / explain** — `can you X`, `what is X`, `why can't you X`,
   `how do I X`, `X isn't working`, `what's the Y`, `how many Z`.

Anything unrecognized returns `None` and falls through to the
tools/model lane — the lane only claims turns it actually resolved.

## Provenance

Every `Resolution.truth` carries `{kind, key|id|action|route}` so a
reply about Nexus functionality is traceable to a feature id, capability
probe, setting key, or action id — internal structured metadata, not
exposed chain-of-thought.

## API surface (read-only)

- `GET /api/features` — catalog summary + live states
- `GET /api/features/{id}` — one feature's spec + composed state
- `GET /api/pages` — the page registry
- `GET /api/actions` — the action registry
- `GET /api/settings/registry` — chat-mutable settings with live values
- `POST /api/actions/execute` — `{id, params, confirmed}` — validated,
  risk-gated, verified mutation (see CHAT_CONTROL_PLANE.md)

## Adding a feature

One obvious place: add a `FeatureSpec` to `catalog.FEATURES` with the
fields that apply — `capability_id` or `probe` for runtime truth,
`ui_route` for deep links, `action_ids`/`settings_keys` for chat
control. The help lane, "what can you do", diagnostics, and the
`/api/features` surface all pick it up automatically.

If the feature needs a chat action, register an `ActionSpec` in
`actions.ACTIONS` (execute through env callables — never direct
endpoint pokes). If it needs a chat-mutable setting, add a
`SettingSpec` in `settings.SETTINGS`. If it gets a UI page or section,
add it to `pages.PAGES`.
