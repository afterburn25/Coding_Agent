# Chat Control Plane

Chat is the primary natural-language interface to Nexus Core. Mutation
requests resolve to **typed registered actions** — never arbitrary
endpoint execution — and every mutating action is verified by read-back
before Nexus reports "done".

## ActionSpec

```python
ActionSpec(
    "voice.disable",
    name="Turn voice off",
    feature_id="voice",
    risk="low_risk",            # see risk ladder below
    permission="",              # PermissionManager key, if any
    reversible=True,            # carries previous value for "undo that"
    run=lambda params, env: ...,  # real env callable — the GUI path
    verify=lambda env: ...,       # post-mutation check
)
```

`run` receives `(params, env)` where `env` is the same lazy callable
table the service uses — so a chat write and a Settings-page write take
the same code path. Return `ActionResult`, a dict, a string, or a bool.

## Risk ladder

| Level | Behavior |
|---|---|
| `read_only` | Never mutates; answers only |
| `low_risk` | Executes immediately on request; verified; reversible |
| `confirm` | Responds with a proposal card; executes only on explicit yes / `confirmed=True` |
| `sensitive` | Never executes the sensitive part inline — returns `connect_securely`/`not_implemented` + the route to the secure surface (credentials never enter chat, history, model context, or logs) |
| `destructive` | Never runs from chat; responds with the UI route |

Permission checks call `env["permitted"](key)` →
`PermissionManager.effective()` — `allow`/`session` proceed, everything
else (`ask`, `creator`, `deny`) refuses. Chat cannot widen permissions.

## Execute → verify → report

`ActionRegistry.execute(id, params, confirmed)`:

1. Unknown id → reject.
2. `confirm`/`sensitive` without `confirmed` → `needs_confirmation`.
3. Permission verdict not in `allow`/`session` → refuse.
4. `run()` → `ActionResult`.
5. `verify()` when defined, else `verified=True` on success.

The reply says "applied **and verified**" only when the read-back
confirmed the target state. A 200 from the call alone is never enough.

## Follow-up context

`SelfKnowledgeService._context` keeps:

- `proposed` — the action/setting awaiting `do it` / `yes` /
  `fix it` / `install it` (set by every proposal and every "X isn't
  working → [Fix it]" answer).
- `last_executed` — `{action, params, undo, route}` where `undo` holds
  the previous value so `undo that` / `turn it back on` restores it.

## Chat UI contract

`AgentResult.ui` → payload `ui` → `renderChatUI()` in `web/app.js`:

```json
{"ui": {
  "actions": [{"id": "voice.disable", "label": "Turn Voice Off",
               "kind": "execute", "params": {}}],
  "links":   [{"route": "/settings.html#voice", "label": "Voice"}],
  "controls":[{"kind": "toggle", "key": "voice_enabled",
               "label": "Voice", "value": true}]
}}
```

- `kind: "execute"` buttons POST `/api/actions/execute` with
  `{id, params, confirmed: true}` — the backend re-validates the id.
- `kind: "navigate"` carries a registry `route` — `location.href`.
- `controls` render inline toggles/selects/sliders for safe settings;
  they POST `_set` with `{key, value}` — the same `SettingsRegistry.set`
  path chat commands use.

## Truth guarantees

- "Can you X" answers from the live `feature_state` — `setup_required`,
  `authorization_required`, `broken`, `degraded` are surfaced, never
  smoothed over.
- `experimental`/`partial` dev status is named in the reply.
- `planned`/`not_implemented` → "isn't implemented in this build yet" —
  never a capability claim.
- A question containing an imperative verb (`push`, `run`, `edit`, …)
  that isn't a resolved control falls through to the tools/model lane
  untouched.

## Universal change journal

`localcodeagent/changes.py` — a bounded (500-row) durable JSONL ledger at
`data/changes.jsonl`. Every significant mutation lane records a typed
ChangeRecord: id, ts, task/mission/conversation, actor, action_type,
subject, before, after, files, reversible, undo descriptor, risk,
verification, checkpoint_id, undone.

Currently journaled:

- **File writes** — `write_file`/`apply_patch` record per task via
  `record_file_mutation` (upserts one record per task); undo restores
  the task checkpoint (`checkpoint_restore`) and verifies the diff is
  clean.
- **Settings** — `SettingsRegistry.set` fires an `on_change` hook, so
  every registry-mediated write (chat commands, action bodies, inline
  controls) lands once; undo sets the previous value back through the
  same verified path.
- **Git** — `git switch -c`/`git_create_branch` journal
  `git_branch_create` (undo switches back then deletes the branch);
  `git_commit` journals `git_reset_soft` (soft reset, work kept staged,
  refuses if HEAD moved); `git_push` journals an explicitly
  **irreversible** record — remote side effects are never silently
  claimed undoable.

Undo dispatch is injected (`kind → handler`) so the ledger never imports
the subsystems it orchestrates. `undo()` marks the record undone and
refuses repeats; irreversible records answer honestly instead of
pretending remote effects can be recalled.

Chat surface: when a turn has no session-scoped `last_executed` undo,
`"undo that"` falls back to `journal.undo()` — file/git/setting changes
from tools, missions, and earlier sessions are all reachable.
`"what did you change"` / `"what have you done"` list the newest
records with undo state. API: `GET /api/changes?limit=N`,
`POST /api/changes/undo {id?}`.
