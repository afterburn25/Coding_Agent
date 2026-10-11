# Production Surface Inventory — polish/QA pass

Machine-readable inventory of Nexus production surfaces (Part 1).
Owner module · primary entry points · endpoints · permissions ·
persistent state · tests · live-verification status. Status values:
`verified` (live-dogfooded this pass) · `partially_verified` (suite
coverage, not live) · `untested` · `broken` · `dead`.

API surface: ~300 route literals across `do_GET`/`do_POST`/`do_PATCH`
in `server.py`; the full extracted route table lives in git history of
this audit (see session notes) — endpoints below list the primary
families, not every suffix route.

| Surface | Owner module(s) | Primary endpoints | Permissions | Persistent state | Tests | Status |
|---|---|---|---|---|---|---|
| Core runtime | `server.py`, `runtime/` | `/api/status`, `/api/runtime*` | — | `data/` | many | partially_verified |
| Desktop host | `desktop/ChatNexus.Desktop/` | — (host shell) | — | — | ci smoke | partially_verified |
| Installer | `packaging/` | — | — | — | ci install/update | partially_verified |
| Updater/deployer | `lkg.py`, `update*` | `/api/update/*`, `/api/lkg*` | — | `data/` | suite + ci | partially_verified |
| Startup/splash/recovery | `boot.py`, `safemode.py`, `rc` routes | `/api/rc/*`, `/api/safemode/*` | — | `data/safe_mode.json` (db) | test_safemode | partially_verified |
| Chat | `server.do_POST /api/chat*`, `agent/orchestrator.py` | `/api/chat`, `/api/chat/stream`, `/api/chat/reset` | — | `data/conversations.json` | pack dogfood | verified |
| Conversation state | `context/state.py` + graph | `/api/conversations/*` | — | `data/conversations.json` | test_context*, soak | partially_verified |
| Memory | `conversation_memory`, `knowledge/` | `/api/conversation-memory*`, `/api/knowledge*` | — | `data/*memory*` | suite | partially_verified |
| Nexus Brain | `brain/` | `/api/brain/*`, `/api/nexus-brain*` | creator-gated sync/lock | `data/nexus_brain*` | test_brain | partially_verified |
| Answer Memory | `answer_memory/` | `/api/answer-memory*` | — | `data/nexus_brain/answer_memory.db` | test_answer_memory | partially_verified |
| Identity Manager | `identity_mgr.py` | `/api/identity*` | `identity.*` | `data/identity.json` (db) | test_identity | partially_verified |
| Capability Truth Graph | `capabilities.py` | `/api/capabilities*` | — | — | suite | partially_verified |
| Situation Model | `nexus_state.py` | `/api/situation`, `/api/nexus/state` | — | — | suite | partially_verified |
| Model Router | `models/router.py`, `brain/thalamus.py` | `/api/model-*` | — | `.agent/model_performance.json` | test_brain, router tests | partially_verified |
| llama.cpp runtime | `runtime/` + `models/` | `/api/runtime*`, `/api/models*` | `runtime.manage` | `.runtime/` | suite | partially_verified |
| Voice | `voice/`, `tts*` | `/api/voice*` | `audio.*` | voice presets/cache | test_voice* | partially_verified |
| Images | `image/` | `/api/image*` | `image.*` | image jobs/artifacts | image tests, live dogfood | partially_verified |
| InvokeAI | `image/invokeai_runtime.py` | via `/api/image/backend*` | `image.manage` | runtime venv | live matrix A–E | partially_verified |
| ComfyUI | `image/` comfyui backend | via `/api/image/backend*` | `image.manage` | runtime | live dogfood | partially_verified |
| Browser | `computer_use/`, browser tools | `/api/browser/*` | `browser.*` | — | suite | partially_verified |
| Computer Control | `computer_use/` | via tools | `desktop.*`/`mouse.*`/… | audit | suite | partially_verified |
| Filesystem | `server.py` fs routes + fs tools | `/api/fs/*` | `filesystem.*` | — | suite | partially_verified |
| Terminal | `terminal/` + tools | `/api/terminal/run` | `shell.execute` | — | suite | partially_verified |
| Git | tools `git` | `/api/git/*` | `git.execute`, `git.push` | — | suite | partially_verified |
| GitHub | `github_account.py`, connectors | `/api/github/*` | `github.*` | vault ref | suite | partially_verified |
| Research/Web | `research*`, `web` | `/api/research*` | `network.read` | research sessions | suite | partially_verified |
| MCP | `mcp.py` | `/api/mcp*` | `mcp.*` | server config | suite | partially_verified |
| Skills | `skills*`, `tools/plugins` | `/api/skills*` | `skills.manage` | skills dir + registry | suite | partially_verified |
| Projects | `projects*` | `/api/projects*` | — | `data/projects*` | test_workspace | partially_verified |
| Knowledge/RAG/LSP | `knowledge/`, `rag`, `lsp/` | `/api/knowledge*`, `/api/rag*`, `/api/lsp` | — | knowledge graph db | suite | partially_verified |
| Engineering Missions | `autonomy/` | `/api/missions*` | `autonomy.*` + per-tool | `data/autonomy/*.json` (db) | test_autonomy, test_missions | partially_verified |
| Workstreams | `autonomy/` (mission DAG) | via `/api/missions/:id` | — | db | suite | partially_verified |
| Scheduler | `autonomy/` schedules | `/api/schedules*`, `/api/triggers*` | — | db | suite | partially_verified |
| Approvals | `approvals.py` + `autonomy/` grants | `/api/approvals`, `/api/autonomy/approvals*`, `/api/autonomy/grants*` | decision surface | db | test_approvals | verified |
| Artifacts | `artifacts.py` | `/api/artifacts*` | — | `data/` registry | test_artifact_handoff | partially_verified |
| Self-repair | `self_repair/` | `/api/self-repair*` | `repair.manage` | findings/procedures (db) | suite | partially_verified |
| Moltbook | `connectors/moltbook.py` | via `/api/connectors*`, `/api/social*` | `social.*` | vault key | suite | partially_verified |
| Social Drive | `social/` | `/api/social*` | — | `data/social/*` (db) | test_social* | partially_verified |
| Epistemic Drive | `social/` backlog+claims | `/api/social/backlog*`, `/api/social/claims*` | — | db | suite | partially_verified |
| Peer Intelligence | `social/` peers/consults/debates | `/api/social/peer*`, `/api/social/consult*`, `/api/social/councils` | — | db | suite | partially_verified |
| Eval Lab | `eval/` | `/api/eval/*`, `/api/experiments*`, `/api/benchmarks*` | — | `data/eval/` | suite | partially_verified |
| Model Growth | `model_growth*` | `/api/model-growth*` | — | growth state | suite | partially_verified |
| Settings | `config.py` + registry | `/api/settings/*`, `/api/preferences*`, `/api/tuning` | — | `config.json` | suite | partially_verified |
| Permissions | `permissions.py` + `approvals.py` | `/api/permissions*` | the matrix itself | `config.json` perms | test_approvals | verified |
| Intelligence Center | `web/intel.html` | `/api/situation`, `/api/capabilities/graph`, `/api/identity*` | — | — | ui smoke | partially_verified |
| Cognitive governor | `governor/` | via `task.intel` + `intel` SSE | — | — | test_governor, test_cognitive | verified |
| Hypothesis engine | `hypotheses.py` | `/api/hypotheses*` | — | `data/hypotheses.json` (db) | test_hypotheses | verified |
| Causal memory | `causal.py` | `/api/causal-memory` | — | `data/causal_memory.json` (db) | test_hypotheses | verified |
| Decision journal | `decisions.py` | `/api/decisions*` | — | `data/decisions.json` (db) | test_hypotheses | verified |
| Assumption ledger | `assumptions.py` | `/api/assumptions*` | — | `data/assumptions.json` (db) | test_cognitive, test_api_errors | verified |
| Requirements compiler | `requirements.py` | `/api/requirements*` | — | `data/requirements.json` (db) | test_requirements, test_cognitive | verified |
| State DB | `state_db.py` | `/api/state/health` | — | `data/state.db` | test_state_db | verified |
| Backups/Recovery | `backups.py` | `/api/backups*` (create/list/restore incl. `paths=` selective, `/api/backups/restore_test`) | — | `data/backups/` | test_platform_foundations | verified |
| Primary UI pages | `web/` | static + api | — | — | e2e smokes | partially_verified |

## Honest gaps this inventory exposes

- `verified` rows = exercised live this pass (chat lane, error mapping,
  assumptions API, approvals/permissions invariants, StateDB health,
  cognitive stores). Everything else is suite-only — the mandate's
  live-dogfood column is the real gap, not unit coverage.
- Live GET sweep (this pass, round 2): all 104 literal no-param GET
  routes probed against the running backend — **zero 500s/errors**;
  every handler returns a well-formed response (200/400/404 as
  appropriate). Two slow endpoints: `/api/diagnostics` ~2.2s,
  `/api/storage/audit` ~6.2s (full recursive JSON scan of a bloated
  dogfood runtime root; scratch dirs now skipped). 8 primary UI pages
  all serve 200.
- `?`-method routes above are dispatched inside handler helpers whose
  method detection the extractor could not attribute — most are GET+POST
  pairs in the shared dispatch helpers (`_handle_*_post`, profile API).
- Live POST fuzz (this pass, round 3): all 134 literal POST routes
  probed with empty `{}` bodies — 6 handlers leaked service exceptions
  as raw 500s → BUG-021 (fixed, mapped to 400/409/503). Re-probe plus
  69 safe routes × 3 wrong-typed bodies: zero 500s.
- UI↔API consistency sweep (this pass): 317 fetch/api call sites in
  `web/*.js` all resolve to real routes; all inline handlers defined;
  all JS-referenced DOM ids exist (one genuinely dead feature found →
  BUG-023, `#nexusPresence` never in markup — fixed); SSE client
  subscriptions all map to real emitters. Remaining UI gap is only the
  interactive click-path (needs a browser session).
