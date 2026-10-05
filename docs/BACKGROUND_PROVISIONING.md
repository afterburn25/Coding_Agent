# Background Provisioning

Nexus installs its core application fast, launches, and then finishes the
rest of the workstation stack in the background while the user works.
`ProvisioningManager` (`localcodeagent/provisioning.py`) owns this.

## Flow

```
Nexus installer → app launches → UI usable immediately
              → ProvisioningManager.start()
              → plan built from the declared stack
              → components install progressively (bounded parallelism)
              → each item verifies before it reports ready
              → Capability Registry invalidates → feature lights up
```

The user is told once — a notification: *"Nexus is finishing workstation
setup in the background…"* — and optionally spoken again the moment the
voice stack itself verifies.

## Declared stack (PLAN_VERSION = 1)

| Item | Kind | Provides | Depends on |
|---|---|---|---|
| `voice-assets` | `voice_assets` | `tts` | — |
| `invokeai` | `tool` (venv manifest) | `image_generation` | — |
| `model-juggernaut-xl-v9` | `invokeai_model` | `image_model` | `invokeai` |
| `whisper-stt` | `tool` (archive manifest) | `stt` | — |
| `model-cyberrealistic-xl-v9` | `invokeai_model` | `image_model` | `invokeai` |
| `model-realvisxl-v5` | `invokeai_model` | `image_model` | `invokeai` |
| `comfyui` | `tool` (archive manifest) | `image_generation` | — |

Sizes and checksums come from the tool manifests and the verified fleet
specs in `image/fleet.py` — nothing is invented. Fleet models install
through InvokeAI's own `/api/v2/models/install` job API, so downloads
resume at the HTTP layer and get upstream hash-checking.

## Item lifecycle

`waiting → queued → running → verifying → completed`

also `failed`, `cancelled`, `skipped` (dependency failed / declined), and
`waiting` with `blocked_reason` when disk space is insufficient.

`completed` is only written **after verification** — voice assets are
re-hashed, fleet models must be enumerable by the backend, and a tool
install must have a completed job record.

## Disk planning

Before dispatch, each item's `est_disk_bytes` plus an 8 GB reserve
(`provisioning_disk_reserve_bytes`) must fit in free space, else the item
stays `waiting` with a `blocked_reason` shown in the UI. `status()`
reports `total_download_bytes`, `remaining_download_bytes`, and
`free_disk_bytes`.

## Retry & failure honesty

`classify_setup_error` maps failures onto stable codes:
`network_failure`, `disk_full`, `checksum_failed`, `unsupported_python`,
`install_script_failed`, `model_download_failed`, `backend_health_failed`,
`permission_denied`, `file_lock`, `dependency_conflict`,
`unsupported_hardware`.

Transient codes (`network_failure`, `connection_reset`, `server_busy`,
`backend_health_failed`) retry with bounded exponential backoff
(30 s × 4ⁿ, capped 10 min, max 3 attempts, `provisioning_auto_retry`).
Permanent failures mark the item `failed`, emit one notification, and
speak **one** short line — `"Juggernaut XL v9 failed — the model download
failed"` — never stack traces, never one line per retry.

## Persistence & resume

Plan state lives in `data/provisioning/plan.json`. On restart,
mid-flight items requeue (`resumed after restart`), completed items stay
completed — upgrades never redownload healthy components. Voice assets
are re-verified on boot and honestly requeued if files went missing.

## Capability integration

Each item carries `provides` (e.g. `image_generation`). On completion the
Capability Registry cache is invalidated and waiters wake. Requests that
need a capability still installing can be deferred —
`waiting_for_capability` — and auto-resume when the provider verifies
(the `/api/image/generate` path does this). Deferred queues are bounded
(20 per capability) and cancellable.

## API

```
GET  /api/provisioning             status(): items, progress, disk plan
POST /api/provisioning/pause       pause the scheduler
POST /api/provisioning/resume      resume
POST /api/provisioning/cancel      {id} — cancel one item
POST /api/provisioning/retry       {id} — requeue failed/cancelled/skipped
POST /api/provisioning/config      {provisioning_enabled, ...} persisted
```

SSE: `provision_update` events carry item state for live UI.

## UI

- **Command Center → Background setup**: `Finishing setup · N of M
  components ready`, per-item state badges, bytes/speed/ETA, progress
  bars, retry/cancel, global pause/resume.
- **Settings → Setup**: the same summary plus the `provisioning_enabled`,
  `provisioning_auto_retry`, `provisioning_voice_notifications` toggles.

## Config

| Key | Default | Meaning |
|---|---|---|
| `provisioning_enabled` | `true` | master switch |
| `provisioning_dev_enable` | `false` | auto-start in a non-frozen dev checkout — off by default so tests/dev runs never trigger multi-GB installs; frozen (installed) builds auto-start whenever `provisioning_enabled` is on |
| `provisioning_parallel` | `2` | max concurrent items (heavy installs still serialize — only one heavy at a time) |
| `provisioning_auto_retry` | `true` | retry transient failures |
| `provisioning_voice_notifications` | `true` | spoken failure/completion summaries |
| `provisioning_disk_reserve_bytes` | `8 GB` | free-space reserve kept while installing |

## Honesty rules enforced

- Nothing is reported `installed`/`ready` that hasn't verified.
- "All ready" is never claimed while items failed — partial completion is
  reported as partial (`"Core setup is complete, but N optional
  components still need attention"`).
- Provisioning never blocks the foreground task bar; setup is background
  work surfaced in the dedicated card and notifications.
- Runtime-installed payloads live outside the frozen bundle — PyInstaller
  ships only `tools/manifests`, never `tools/` trees or model weights.
