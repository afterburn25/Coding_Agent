# Permissions

Nexus Core routes every tool/action through `PermissionManager`
(`localcodeagent/permissions.py`). The manager wraps the `permissions` map in
`config.json`, so existing configs keep working.

## Permission keys

Core keys plus risk-sensitive keys shared across profiles:

`filesystem.read` · `filesystem.write` · `filesystem.delete` ·
`shell.execute` · `git.execute` · `git.push` · `packages.install` ·
`skills.manage` · `repair.manage` · `runtime.manage` · `docker.access` · `credentials.use` · `network.read` · `browser.control` ·
`browser.submit` · `external_api.call` · `image.read` · `image.generate` ·
`image.manage` · `github.read` · `github.write` · `message.send` ·
`microphone.use` · `camera.use` · `spend.money` · `tasks.queue` ·
`desktop.view` · `screen.capture` · `desktop.control` · `mouse.control` ·
`keyboard.control` · `clipboard.read` · `clipboard.write` ·
`application.launch`

Computer Use's older broad `computer.observe` and `computer.control` levels
remain aliases when a granular key has not been configured explicitly.

High-impact actions (spending money, sending messages, mic/camera, desktop
observation/control) default
to `deny` in every shipped profile; installs/deletes/pushes/submissions
default to `ask`. Manifest tools may introduce additional keys — unknown
keys default to `ask`.

## Levels

| level | behavior |
| --- | --- |
| `allow` | run without asking |
| `session` | ask once; on approval, allow for the rest of the session |
| `ask` | require explicit approval every time (default for unknown keys) |
| `creator` | like `ask`, but approvals also require an unlocked Nexus Brain creator session (fail-closed without it) |
| `deny` | never run |

## Known permission keys

`filesystem.read`, `filesystem.write`, `filesystem.delete`,
`shell.execute`, `git.execute`, `git.push`, `packages.install`,
`skills.manage`, `repair.manage`, `runtime.manage`,
`image.read`, `image.generate`, `image.manage`,
`network.read`, `browser.control`, `browser.submit`, `external_api.call`,
`docker.access`, `credentials.use`, `github.read`, `github.write`,
`tasks.queue`, `desktop.view`, `screen.capture`, `desktop.control`,
`mouse.control`, `keyboard.control`, `clipboard.read`, `clipboard.write`,
`application.launch`, `microphone.use`, `camera.use`, `message.send`,
`spend.money`.

Tools may introduce additional keys; any key not configured defaults to `ask`.

## Workspace profiles

Applying a profile rewrites the permission map and records the profile name in
`config.json` (`permission_profile`). Editing any single level switches the
profile back to `custom`.

| profile | intent |
| --- | --- |
| `safe` | reads allowed; writes/shell/git/network ask |
| `developer` | current defaults (coding workflow, remote writes ask) |
| `power_user` | writes/git allowed; shell and remote writes session-gated |
| `offline` | developer baseline with network/browser/GitHub denied |
| `research_only` | reads + research allowed; writes/shell/git denied |
| `custom` | the stored map is authoritative; never auto-applied |

## Boundaries that do not move

- The permission system gates tool **actions**. It is separate from
  conversation-policy posture (`permissive`/`balanced`/`strict`) and from the
  signed Nexus Brain subroutines.
- Narrow hard safety checks (for example image safety gates inside
  `ImageManager`) remain enforced regardless of permission levels.
- Manifest/plugin tools default to `shell.execute` unless they declare a
  narrower key.

## Autonomous mode

`autonomous_mode` (config key, or the Tools page toggle) lets the agent run
unattended: permission levels `ask` and `session` are auto-approved for
reversible workspace actions and recorded in `session_grants` for visibility.

It never bypasses:

- `deny` levels — denied stays denied.
- Hard gates (`AUTONOMY_NEVER_AUTO` in `permissions.py`): `spend.money`,
  `message.send`, `microphone.use`, `camera.use`, `skills.manage`,
  `repair.manage`, `runtime.manage`, and every desktop observation/control
  key always require an explicit human approval
  regardless of mode. (`runtime.manage` gates the HTTP start/stop/restart
  surface only — autonomous memory management uses `RuntimeManager`'s
  internal busy-aware eviction path, which never goes through the API.)

Two companion settings bound autonomous runs:

- `autonomous_max_continuations` (default 5) — how many times the agent may
  extend past `max_agent_steps` without finishing.
- `autonomous_approval_timeout_seconds` (default 3600) — a task parked on a
  hard-gated approval is failed after this wait, while a mission approval
  is marked `timed_out` and replans around the suspended node; repeated
  mission denials/timeouts are bounded by `max_approval_retries` so an
  unattended run cannot stall or re-ask forever (0 waits forever; ignored
  when autonomous mode is off).
- `agent_tool_timeout_seconds` (default 1800) — hard cap per tool call; a
  hung tool returns a timeout error instead of blocking the run.
- `model_idle_unload_seconds` (default 900) plus `memory_pressure_vram_gb` /
  `memory_pressure_ram_gb` — idle or pressured managed models are unloaded by
  the process watchdog tick; models pinned by a running task are never
  evicted, and `ensure_ready()` transparently restarts them on resume.

## Scopes

`permission_scopes` in `config.json` attaches per-key boundaries:

```json
{"permission_scopes": {"network.read": {"allowed_domains": ["api.github.com"],
                                        "blocked_domains": []},
                       "filesystem.write": {"allowed_dirs": ["src/"],
                                            "workspace_only": true},
                       "github.write": {"allowed_repos": ["me/repo"]}}}
```

Domain scopes are enforced at `ToolRegistry.execute` — any tool argument whose
name contains `url` is checked against the tool's permission scope. Editable
per permission in Settings → Permissions.

## Audit log

Permission decisions and policy changes are appended to
`data/permission_audit.jsonl` (bounded to 2000 entries): level/profile/scope
changes, session grants, autonomy toggles, approvals, denials, scope denials,
creator-approval requirements, and MCP connect/disconnect/restart. Only event
names, permission keys, and short sanitized labels are recorded — never tool
arguments, secrets, tokens, or payloads. Routine executions update an
in-memory `last_used` stamp (shown in the matrix) without file writes.

## API

```text
GET  /api/permissions             profile, levels, effective map, session grants,
                                  categories, per-key info/scopes/last_used, autonomy state
GET  /api/permissions/audit       {"entries": [...]}  (bounded, newest first)
POST /api/permissions/level       {"permission": "shell.execute", "level": "session"}
POST /api/permissions/profile     {"profile": "offline"}
POST /api/permissions/autonomous  {"enabled": true}
POST /api/permissions/scope       {"permission": "network.read", "scope": {"allowed_domains": [...]}}
```

Changes persist into `config.json` atomically. Session grants are intentionally
in-memory only and clear on restart or profile change.

Other gated endpoints respond `{"needs_approval": true, "permission": ...}` the
same way: skill lifecycle routes (`skills.manage`), self-repair actions
(`repair.manage`), and runtime/process lifecycle
(`POST /api/processes/action`, `/api/runtime/start`, `/api/runtime/stop` →
`runtime.manage`). The Tools and Models pages resend with `approve: true`
after a confirmation prompt.

## UI

Permissions live under **Settings → Permissions** (`/settings.html#permissions`):
profile cards, category summaries, a searchable permission matrix, per-key
detail panel (scope editors, approval rules, recent activity), and the bounded
activity log. The Tools page is operational only — install/health/runtime
management — and links to Settings for authorization.
