# Permissions

Chat Nexus routes every tool/action through `PermissionManager`
(`localcodeagent/permissions.py`). The manager wraps the `permissions` map in
`config.json`, so existing configs keep working.

## Levels

| level | behavior |
| --- | --- |
| `allow` | run without asking |
| `session` | ask once; on approval, allow for the rest of the session |
| `ask` | require explicit approval every time (default for unknown keys) |
| `deny` | never run |

## Known permission keys

`filesystem.read`, `filesystem.write`, `shell.execute`, `git.execute`,
`image.read`, `image.generate`, `image.manage`, `network.read`,
`browser.control`, `github.read`, `github.write`.

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

## API

```text
GET  /api/permissions            profile, levels, effective map, session grants
POST /api/permissions/level      {"permission": "shell.execute", "level": "session"}
POST /api/permissions/profile    {"profile": "offline"}
```

Changes persist into `config.json` atomically. Session grants are intentionally
in-memory only and clear on restart or profile change.
