# Nexus Core Stabilization Audit

Stabilization milestone wiring audit. Statuses are earned by real
evidence — unit tests alone do not mark a feature working. Packaged-app
dogfood results are recorded per feature.

Statuses: `WORKING_VERIFIED` | `WORKING_WITH_POLISH_NEEDED` |
`PARTIALLY_WIRED` | `BROKEN` | `NOT_INSTALLED` | `NOT_TESTED` |
`INTENTIONALLY_UNAVAILABLE`

Last updated: after packaged-backend dogfood on the `244af60`+ build
(deployed to `D:\Nexus_Core\backend`, served on port 58341 with
`--workspace D:\Nexus_Core\Source`).

## Verified dogfood evidence (this milestone)

| Evidence | Where |
|---|---|
| InvokeAI 6.14.2 discovered at `tools/InvokeAI`, `invokeai-web.exe` found, health green on :9090 | `localcodeagent/image/invokeai_runtime.py`, live probe |
| InvokeAI auto-route generated real 1024x1024 PNG; `job.backend == invokeai` | `data/image/generations/878199cb…png` |
| **Packaged app**: POST `/api/image/generate` (auto) → job `81408629` finished, `backend=invokeai`, `backend_job_id=34`, real PNG, routing reasons recorded | packaged backend :58341 |
| Stale completed-tool provisioning plan self-heals (tool re-verified on disk) | `localcodeagent/provisioning.py::_inventory` |
| GitHub: connected as `afterburn25` (vault token), repo list, branch → commit → header-auth push → remote verify → PR #6 created → read → closed → deleted | `localcodeagent/tools/github.py` |
| **Packaged app**: `/api/github/status` → `state=connected`, `workspace_repo=afterburn25/Coding_Agent` resolves | packaged backend :58341 |
| GitHub write bug found + fixed: `_ensure_token()` missing on issue/PR/read endpoints | commit `244af60` |
| **Packaged app**: TTS speak → real 173KB WAV; `/api/profiles/{id}/farewell` → `voice_url` + real 503KB WAV (earlier farewell failure was stale deploy) | packaged backend :58341 |
| Image job cards now render `routing_reasons` (fallback reason visible) | `web/image.js` |
| Chat control plane live-verified on packaged backend 0.23.0: voice toggle, undo, worker ceiling, backend status | port-58333 live probe |
| Worker ceiling propagates to live `AdaptiveWorkerManager` | `self_knowledge` setter |
| `VERSION` bundled in packaged backend (was `0.0.0-dev` regression) | `packaging/build_windows.ps1` |
| Permission matrix verified: `git.push` deny→`PERMISSION_DENIED: git.push`, ask→`APPROVAL_REQUIRED` naming the right key; `git_push` was wrongly gated on `github.write` — fixed | `localcodeagent/tools/github.py` |
| STT on packaged app: faster-whisper `base` available, 24 input devices enumerated | `/api/stt` on :58341 |
| All 17 UI pages serve 200 HTML on packaged backend | page sweep :58341 |
| Full unit suite: **2175 passed / 7 failed** — 5 pre-existing `answer_memory` SQLite-lock flakes (repro on clean main), 2 doc-version staleness (fixed in `78f72ec`) | `pytest tests` |

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
| Known residue | `github_repository` reported `authenticated:false` before token hydration — fixed via `_ensure_token()`; workspace remote **verified** on packaged app (`workspace_repo` resolves when `--workspace` points at the git repo, which the desktop host does via `Source/`) |

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

## Voice / TTS / Speech Lab — `WORKING_VERIFIED`

Settings mutations verified live (`turn voice off`, undo). Packaged-app
dogfood: `voice.status` reports kokoro 0.6.1 with both assets present +
verified; `/api/voice/speak` returned a real 173KB WAV; the desktop
farewell chain (`/api/profiles/{id}/farewell` → `voice_url`) returned a
real 503KB persona-styled WAV. The earlier "farewell isn't firing" report
was a stale deployed backend — resolved by the rebuilt 0.23.0 deployment.
STT itself still `NOT_TESTED` (no mic dogfood yet).

## Everything else — audit status

| Feature | Status | Notes |
|---|---|---|
| Nexus Brain | NOT_TESTED | service exists; needs learn/retrieve dogfood |
| Answer Memory | BROKEN (pre-existing) | 5 SQLite-lock failures repro on clean main — `learn()` writes are not persisting (export/stats/reopen all see 0 rows). Real defect, filed for repair |
| Speech input (STT) | WORKING_WITH_POLISH_NEEDED | engine + device enumeration verified live; no real-mic transcription dogfood |
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
