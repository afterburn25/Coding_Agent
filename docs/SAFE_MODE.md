# Safe Mode & Golden Configuration

`localcodeagent/safemode.py` — startup-failure detection, a minimal
Safe Mode, and verified config snapshots.

## Failure detection

AppState writes the session marker dirty at boot and flips it clean on
graceful shutdown. On the next launch a dirty prior record calls
`safemode.record_boot(previous_clean=False)` — three consecutive
unclean boots set `should_offer`, surfaced via `GET /api/safemode`.
Safe Mode is offered, never forced, and never deletes data.

## Safe Mode

`enter`/`exit` toggle a persisted flag. While active:

- **Suppressed:** `model_autostart` (the primary prewarm returns
  early), `comfyui`, `plugins`, `autonomous_workers`,
  `background_watchers`, `scheduled_jobs` — `AutonomyPolicy.is_stopped`
  returns true so every autonomous action class denies.
- **Available:** `core_ui`, `diagnostics`, `logs`, `config`,
  `repair_tools`.

## Golden config

`GoldenConfigStore` snapshots the whitelisted set — `config.json`,
`data/policies.json`, `data/autonomy/control.json` (plus optional
`extra_paths`) — into `data/golden/snap-<ms>/` with a sha256
`manifest.json`, updating `latest.txt`.

- `verify(name)` → manifest + per-file hash check.
- `restore(name)` → refuses to apply a snapshot that fails
  verification (`snapshot corrupt: hash mismatch`), then copies files
  back over live config.
- `list()` → up to 50 snapshots newest-first.

## API

- `GET  /api/safemode` — active/reason/failure count/should_offer +
  disabled/available lists
- `POST /api/safemode/enter` `{reason}` / `POST /api/safemode/exit`
- `GET  /api/golden` — snapshot list
- `GET  /api/golden/verify/<name>` — integrity check
- `POST /api/golden/snapshot` `{label, extra_paths}`
- `POST /api/golden/restore` `{name}` — verified restore
