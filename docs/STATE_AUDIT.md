# State Audit — persistent store inventory & classification

Resilience milestone Phase A. Every durable Nexus store, its current
durability mechanism, its class, and its migration tier.

**Classes** (per the milestone taxonomy):

- `transactional` — real ACID writes (SQLite WAL)
- `atomic-file` — whole-doc `atomic_write_*` (tmp + os.replace); safe
  against torn files but no cross-store atomicity, no corruption
  detection beyond parse, and (pre-Foundation-1) several stores
  silently reset to defaults on any error
- `append-only` — JSONL logs; cap-rewrite is non-atomic but logs are
  rebuildable evidence, not authority
- `recoverable` — regenerable from live probes / other stores
- `non-critical` — caches, snapshots, tuning hints
- `legacy` — superseded formats kept for import only

## Tier 1 — migrated to `data/state.db` (Foundation 1, this change)

Truth lives in a WAL-mode `kv` row; a live shadow file is still
written on every save so file tooling and code downgrades keep
working; the pre-migration original is frozen as `<file>.migrated`.
Corrupt sources quarantine (`quarantine` table + `.corrupt-*` file)
and flag the store degraded — never silently empty.

| Store | File(s) | Why Tier-1 |
|---|---|---|
| Missions / workstreams | `data/autonomy/missions.json` | durable work truth; loss = orphaned/phantom work |
| Approvals | `data/autonomy/approvals.json` | permission state; pending rows must survive restart |
| Schedules | `data/autonomy/schedules.json` | mission materialization cadence |
| Standing goals / goals / triggers | `standing_goals.json`, `goals.json`, `triggers.json` | autonomous intent |
| Grants / notifications | `grants.json`, `notifications.json` | grants = session permission memory |
| Repairs / findings / procedures / control | `repairs.json`, `findings.json`, `procedures.json`, `control.json` | repair incidents resume mid-flight; control carries pause/stop |
| Action ledger | `data/action_ledger.json` | execution evidence — false-success gate; **previously no quarantine at all** |
| Identity | `data/identity.json` | account registry + credential refs; **previously silent reset on error** |
| Safe Mode | `data/safe_mode.json` | consecutive-failure counter; silent reset masked crash loops |
| Decisions | `data/decisions.json` | engineering decision ledger |
| Requirements | `data/requirements.json` | user-intent truth (spec ledger) |
| Hypotheses / causal memory | `hypotheses.json`, `causal_memory.json` | root-cause evidence feeding repairs |
| Learning (7 stores) | `data/learning/{lessons,procedures,strategies,competencies,study_sessions,mastery,skill_candidates}.json` | Phase B write-path truth — lessons→procedures→skill proposals |

## Tier 2 — next migration candidates (P1)

| Store | File(s) | Class | Notes |
|---|---|---|---|
| Conversation memory | `data/conversation_memory.json` | atomic-file | user-visible memory; migrate after Tier-1 soak |
| Conversations | `data/conversations.json` | atomic-file | full chat history — largest single doc; may want per-row layout instead of whole-doc kv |
| Knowledge memory | `data/knowledge_memory.json` | atomic-file | TTL'd sourced facts |
| Social | `data/social/*` (peers, consults, debates, journal, claims, moltbook_account) | atomic-file | peer trust state; consults pending across restarts |
| Projects | `data/projects/*/index.json` + per-project docs | atomic-file | project isolation (Phase H) wants per-project namespacing anyway |
| Skill registry state | `data/skills/_state.json` + backups | atomic-file + snapshots | already transactional via install/rollback protocol |
| Artifacts registry | `data/artifacts/registry.json` | atomic-file | metadata only; files stay files (verified hashes) |
| Jobs | `data/jobs.json` | atomic-file | JobManager ledger — orphan recovery exists |
| Tools state | `data/tools_state.json` | atomic-file | enabled/installed flags |
| Profiles | `data/profiles/index.json`, `migration.json` | atomic-file | user profile registry |
| Autonomy repair memory | `data/autonomy/repair_memory.json` | atomic-file | written by self-repair coordinator |
| Provisioning plan | `data/provisioning/plan.json` | atomic-file | install plans |

## Tier 3 — intentionally left as files

| Store | File(s) | Class | Why |
|---|---|---|---|
| Answer Memory | `data/nexus_brain/answer_memory.db` | transactional | already SQLite |
| Nexus Brain memory | `data/brain/memory.db` | transactional | already SQLite |
| Knowledge graph | `data/knowledge_graph.db` | transactional | already SQLite |
| Nexus Brain metadata | `data/nexus_brain.json` + `.auth.json` | atomic-file + signature | creator-signed; the `.auth.json` signature must remain a file-level artifact |
| Audit/receipts/activity/permission logs | `*.jsonl` streams | append-only | evidence streams; non-atomic cap acceptable (rebuildable) |
| Benchmarks / eval history | `data/benchmarks/*`, `eval/history.jsonl` | append-only/non-critical | measurement output |
| Crash history | `crash_history.jsonl` | append-only | post-mortem evidence |
| Health / twin / runtime tuning | `health.json`, `twin.json`, `runtime_tuning.json` | non-critical | live-derived snapshots |
| Session markers | `session-*.json` | non-critical | boot liveness only |
| Model files / artifacts / voice cache | `models/`, `data/artifacts/files/`, `data/voice/cache/` | files | binary payloads — DB stores verified references/hashes only, per spec |
| Golden config / LKG | `data/golden/`, `data/lkg/` | files + manifest | deployment snapshots — already hash-manifested |

## Defects fixed in Foundation 1

- `ActionLedger`, `IdentityManager`, `SafeModeStore`, `DecisionJournal`,
  `RequirementStore`, `HypothesisStore`, `CausalMemory`, and all 7
  learning stores fell back to **empty state on any load error with no
  quarantine** — a truncated or malformed file silently wiped the
  store. Every DocStore-backed store now quarantines + degrades loudly.
- No corruption detection anywhere in the JSON tier — boot now runs
  `PRAGMA quick_check` + a JSON parse probe over every migrated row
  (`/api/state/health`).
- No cross-store atomicity — `StateDB.txn()` provides the atomic unit
  (approval + ledger + event in one commit); cross-file-only stores
  still get durable intent via the `operations`/`events` groundwork
  tables.
- Migration safety: verified read-back before the original file is
  frozen as `.migrated`; `.migrated` is a recovery source if the DB
  is lost; migration is idempotent — the DB row always wins over the
  shadow file.
