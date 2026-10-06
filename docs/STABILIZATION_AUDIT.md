# Nexus Core Stabilization Audit

Stabilization milestone wiring audit. Statuses are earned by real
evidence — unit tests alone do not mark a feature working. Packaged-app
dogfood results are recorded per feature.

Statuses: `WORKING_VERIFIED` | `WORKING_WITH_POLISH_NEEDED` |
`PARTIALLY_WIRED` | `BROKEN` | `NOT_INSTALLED` | `NOT_TESTED` |
`INTENTIONALLY_UNAVAILABLE`

Last updated: after commit `244af60` (GitHub vault-credential hydration).

## Verified dogfood evidence (this milestone)

| Evidence | Where |
|---|---|
| InvokeAI 6.14.2 discovered at `tools/InvokeAI`, `invokeai-web.exe` found, health green on :9090 | `localcodeagent/image/invokeai_runtime.py`, live probe |
| InvokeAI auto-route generated real 1024x1024 PNG; `job.backend == invokeai` | `data/image/generations/878199cb…png` |
| Stale completed-tool provisioning plan self-heals (tool re-verified on disk) | `localcodeagent/provisioning.py::_inventory` |
| GitHub: connected as `afterburn25` (vault token), repo list, branch → commit → header-auth push → remote verify → PR #6 created → read → closed → deleted | `localcodeagent/tools/github.py` |
| GitHub write bug found + fixed: `_ensure_token()` missing on issue/PR/read endpoints | commit `244af60` |
| Chat control plane live-verified on packaged backend 0.23.0: voice toggle, undo, worker ceiling, backend status | port-58333 live probe |
| Worker ceiling propagates to live `AdaptiveWorkerManager` | `self_knowledge` setter |
| `VERSION` bundled in packaged backend (was `0.0.0-dev` regression) | `packaging/build_windows.ps1` |

## GitHub — `WORKING_VERIFIED`

| Field | Value |
|---|---|
| UI surface | Settings → Connections |
| API endpoints | `/api/github/status|connect|disconnect|repos|test` |
| Backend service | `localcodeagent/github_account.py` (GitHubAccountService) |
| Tools | `github_*`, `git_*` in `localcodeagent/tools/github.py` |
| Credential | `SecretVault` key `github_token`, `D:\Nexus_Core\data\secrets.vault` |
| Permission | `github.read` (allow), `github.write` (ask), `git.push` (ask) |
| Verification | Real dogfood: connect state, repo list, branch/commit/push to `devin-stabilization-check`, remote verify, PR #6 create/read/close/delete |
| Dogfood result | PASS — full write cycle green; scopes returned: `gist notifications read:gpg_key read:org repo user workflow write:public_key` |
| Known residue | `github_repository` reported `authenticated:false` when called before token hydration — fixed via `_ensure_token()`; workspace-remote resolution via account service returns `null` and needs follow-up verification |

## Git — `WORKING_VERIFIED`

| Field | Value |
|---|---|
| Tools | `localcodeagent/tools/git.py` + git tools in `github.py` |
| Dogfood | `git_create_branch`, `git_commit`, `git_push` executed against real remote; push used transient `http.extraHeader` — token never in `.git/config` or remote URL |

## InvokeAI — `WORKING_VERIFIED`

| Field | Value |
|---|---|
| Manifest | `tools/manifests/invokeai.json` (pinned `invokeai==6.14.2`) |
| Runtime | `localcodeagent/image/invokeai_runtime.py` |
| Install root | `<runtime_root>/tools/InvokeAI`, launcher `Scripts/invokeai-web.exe` |
| Endpoint | `http://127.0.0.1:9090` |
| Installed version | 6.14.2 |
| Models (registry) | dreamshaper-8, CyberRealisticXLPlay_V9.0_FP16, RealVisXL_V5.0_fp16, Juggernaut-XL_v9 |
| Dogfood | Cold-start → healthy → auto-route selected InvokeAI → RealVisXL V5.0 → real PNG written (~120s) |
| Fixes this milestone | provisioning tool re-verification, `.dist-info` version parse bug, `installed` default truth, `invalidate_discovery()`, offline registry enumeration for auto-routing |

## ComfyUI — `WORKING_VERIFIED` (fallback role)

Endpoint `http://127.0.0.1:8188`; fallback routing tests green; remains the
advanced/custom-workflow backend. Not deleted, per requirement.

## Chat control plane / self-knowledge — `WORKING_VERIFIED`

`localcodeagent/self_knowledge/` + orchestrator lane. Live-verified on
deployed 0.23.0: capability catalog, truthful degraded-state reporting,
setting mutation with verify + undo, worker ceiling, nav cards.
31 self-knowledge tests + 187 focused regressions green.

## Workers / work queue — `WORKING_VERIFIED`

Live ceiling mutation verified (`0 active / 4 ceiling`); nested
`capacity.ceiling` probe bug fixed.

## Provisioning — `WORKING_WITH_POLISH_NEEDED`

Ground-truth re-verification now covers completed `tool`,
`invokeai_model`, `image_model`, `voice_assets` items; stale
`completed/verified` plans self-heal to `waiting`. Remaining: clean-install
and upgrade-migration dogfood still outstanding.

## Voice / TTS / STT / Speech Lab — `PARTIALLY_WIRED`

Settings mutations verified live (`turn voice off`, undo). Farewell voice
delivery was investigated earlier; inconsistent `voice_url` on deployed
build — suspected stale deployment, needs re-verification on the rebuilt
0.23.0 backend.

## Everything else — audit status

| Feature | Status | Notes |
|---|---|---|
| Nexus Brain | NOT_TESTED | service exists; needs learn/retrieve dogfood |
| Answer Memory | PARTIALLY_WIRED | 5 pre-existing SQLite-lock test failures on clean main (unrelated to this work); live persistence unverified |
| Missions / autonomy supervisor | NOT_TESTED | |
| Browser automation / Playwright | NOT_TESTED | Edge fallback path unverified |
| Web search / research | NOT_TESTED | |
| Computer use | NOT_TESTED | |
| Code intelligence / LSP | NOT_TESTED | |
| Debugger | NOT_TESTED | |
| Dev servers | NOT_TESTED | |
| Projects / workspaces | NOT_TESTED | workspace remote resolution returned `null` — needs investigation |
| Models (non-image) / model routing / Growth Lab | NOT_TESTED | |
| Personality / Speech Genome / persona | PARTIALLY_WIRED | persona replies live-verified; genome/farewell re-check pending |
| Notifications | NOT_TESTED | |
| Command Center | NOT_TESTED | |
| Permissions / profiles | PARTIALLY_WIRED | gates verified for github.* and git.*; full matrix pending |
| Safe Mode / LKG / rollback / self-update / self-repair / backups | NOT_TESTED | |
| Connectors / plugins / MCP / skills | NOT_TESTED | |
| Deployment / Docker / sandbox / coverage / dep-audit | NOT_TESTED | |
| Secrets vault | WORKING_VERIFIED | github_token round-trip verified, never printed |
| Health & diagnostics | WORKING_WITH_POLISH_NEEDED | probes fixed where found; global sweep pending |
| All UI pages | NOT_TESTED | packaged-app page sweep outstanding |

## Outstanding acceptance gaps

- Clean install dogfood and old-ComfyUI-install upgrade dogfood
- UI-level backend display + fallback-reason visibility in the app
- Full page/button/settings sweep on the packaged app
- Voice farewell re-verification on rebuilt backend
- Full unit suite run + separation of pre-existing failures
- Installer smoke test
