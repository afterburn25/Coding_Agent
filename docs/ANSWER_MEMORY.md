# Nexus Answer Memory

Persistent, local-first learned Q&A. Answer Memory remembers questions Nexus
has already answered — and answers a trusted repeat **without loading or
calling any model**.

It is deliberately *not* a dumb string cache, and it does not blindly trust
everything a model ever said:

- Model answers are recorded as **experience** (observations).
- Only answers that earn trust (explicit `learn`, user corrections,
  verification, repetition + positive feedback) are promoted into the
  **trusted answers** table.
- Only `trusted`/`verified` rows may bypass inference. Everything below that
  stays advisory context for the fast model lane.

## Request pipeline

```
user question
  → tier-0 deterministic handlers (time/date/greetings/brain commands)
  → Answer Memory: exact normalized lookup
  → Answer Memory: semantic lookup (embeddings + conflict gates)
      trusted hit  → answer immediately; no hardware probe, no model load
      possible     → injected as advisory context for the utility model
      no_match     → normal routing
  → Nexus Brain knowledge lookup
  → utility model → coding/deep model → research/tools when fresh data needed
  → experience recorded → promotion when evidence warrants
```

A memory hit skips `runtime.refresh_hardware()`, model selection, provider
activation, and inference. The task record carries
`response_source="answer_memory"` and the timeline shows an **Answer Memory**
activity with match kind, similarity, trust state, and latency.

## Database

- Path: `data/nexus_brain/answer_memory.db` (private runtime data — never
  packaged, never deployed over, excluded by the installer).
- Engine: SQLite, WAL journal, `synchronous=NORMAL`, foreign keys on.
- Connections are **per-operation** and closed immediately — the DB file is
  never held open, which keeps it movable/back-up-able on Windows and makes
  hot upgrades safe. Read traffic is served from an in-memory snapshot of the
  `answers`/`aliases` tables, so lookups cost microseconds and do no disk I/O.
  Stat writes (use counts, hit metrics) are deferred and flushed in batches.
- Corruption on open → the file is quarantined to
  `answer_memory.db.corrupt-<timestamp>` (never deleted) and a fresh DB is
  created so chat keeps working.
- Migrations: `PRAGMA user_version` + `SCHEMA_VERSION` (currently **1**);
  a `pre-v<N>.bak` copy is made before any migration step.

### Schema (version 1)

| table | role |
|---|---|
| `experiences` | what happened — every completed exchange (unverified) |
| `answers` | canonical learned Q&A with trust state, freshness, scope |
| `aliases` | extra normalized phrasings → canonical answer |
| `metrics` | counters (hits, lookups, corrections, suppressed, …) |
| `answers_fts` | FTS5 index over question/answer text (when available) |

`answers` rows carry: canonical + normalized question, answer text, embedding
blob + `embedding_model`, `answer_type`, `confidence`, `trust_state`,
`source_type`, `created_at`/`updated_at`/`last_verified_at`/`expires_at`,
`use_count`/`successful_use_count`/`occurrence_count`/`correction_count`,
`invalidated` + `invalidation_reason`, `project_scope`/`project_id`,
`repository`/`git_commit`, `config_fingerprint`, `brain_revision`,
`content_hash`, `freshness`, `handler_key`, `last_used_at`.

## Trust model

| state | may bypass model? | how it gets there |
|---|---|---|
| `observed` | no | recorded model answer (default) |
| `candidate` | no (context only) | 3+ matching experiences, or promotion start |
| `trusted` | **yes** | explicit user learn/correction, 5+ occurrences, or 3+ occurrences with positive feedback |
| `verified` | **yes** | deterministic source / verified path |
| `stale` | no | TTL expired or dependency changed — needs revalidation |
| `invalidated` | no | corrected, forgotten, superseded |

Promotion ladder lives in `confidence.py`
(`PROMOTE_OCCURRENCES_CANDIDATE=3`, `PROMOTE_OCCURRENCES_TRUSTED=5`,
positive feedback accelerates one step). A correction typed by the user is
treated as an explicit instruction and stored directly as `trusted` with
`source_type="correction"`; the old answer is invalidated and its
`correction_count` incremented.

## Semantic matching

Pipeline: exact normalized match → alias table → semantic scoring → FTS
fallback for context.

- **Embedder**: `hashed-ngram-v1` — deterministic word n-gram hashing with
  domain concept expansion (`checkpoint`→`model`, `normally`→`default`,
  `image generation`→`image create`, …). Zero dependencies, no model
  download, microseconds per embed, stable across restarts so stored vectors
  stay valid. The embedder id is recorded on every row; swapping embedders
  later is supported by `embedding_model` versioning + `rebuild_index()`.
- **Score**: blend of hashed-vector cosine + canonical-token Jaccard +
  stored-question coverage (`0.40·cos + 0.25·jac + 0.35·cov_stored −
  uncovered penalties`).
- **Hard gates return −1 before any score matters:**
  - *Discriminative tokens*: capitalized entities, digits, dotted/underscored
    identifiers must be mutually covered (France ≠ Italy, Qwen 14B ≠ 30B).
    Concept vocabulary is exempt from capitalization gating.
  - *Antonyms*: opposed concepts (start/stop, enable/disable, install/
    uninstall, min/max, …) never match.
  - *Symmetric canonical swap*: equal-size uncovered canonical sets on both
    sides mean a value substitution (image model ≠ voice model,
    password reset ≠ username change). Pure action-verb rewordings
    ("generate with" ↔ "use") are exempt.
- Thresholds (config): `answer_memory_semantic_threshold` 0.50 → bypass;
  `answer_memory_possible_threshold` 0.30 → context-only. Below that:
  `no_match`.
- Confirmed-paraphrase bridge: when a `possible` match is followed by a model
  answer whose normalized text equals the stored answer, the new phrasing is
  learned as an **alias** — the next equivalent question is an exact hit.
  Memory gets faster the more it is used.

Measured on this machine: **exact ≈ 0.08 ms, semantic ≈ 0.13 ms** with 200
stored answers — vs seconds for even the 4B utility lane.

## Freshness & invalidation

- `validation.classify_cacheability()` marks `live` questions (weather, news,
  prices, "latest", "right now"…) — they are recorded as experience but never
  produce bypassable answers.
- `ttl.infer_freshness()` assigns `static` / `user_defined` /
  `repository_dependent` / `config_dependent` / `application_state` /
  `model_dependent` / `live` classes with per-class TTLs.
- Dependency fingerprints are checked at lookup time:
  - `repository_dependent` answers go `stale` when the workspace HEAD moves.
  - `config_dependent` answers go `stale` when the config fingerprint changes.
  - Stale rows are demoted in place (`stale` + reason) — never silently
    served.
- Corrections invalidate immediately. `forget` removes a row entirely.
- `handler_key` answers (deterministic dynamic content) render fresh at hit
  time and skip staleness checks.

## Scoping

Every answer has `project_scope` (`global`/`repository`/`project`) +
`project_id`. Retrieval only considers `global` answers plus rows matching
the current workspace — answers learned inside one project never leak into
another.

Answers and experiences are additionally stamped with `profile_id` — the
profile that was active when they were recorded. Retrieval, learned-answer
lists, `forget`, corrections, and positive feedback only see unscoped
(`profile_id=''`) rows plus rows stamped with the *active* profile. A
personal answer learned under Profile A is therefore never served to or
mutated by Profile B — not through trusted bypass, not through
prompt-context hints, and not through the Learned Answers UI. Rows recorded
before profiles existed (or with no active profile) keep `profile_id=''`
and remain shared, so general technical knowledge still benefits everyone.

## Commands

Natural-language, parsed before model routing:

- `learn that <q> is <a>` / `learn: <q> → <a>` — store a trusted answer
- `learn this answer` — learn the previous exchange
- `forget the answer for <q>` — delete it
- `that's wrong — the answer is <a>` / thumbs-down — invalidate + learn the correction
- `what have you learned [about X]` — list trusted answers
- `did you answer that from memory` — explain the last hit

## HTTP API

| method | path | purpose |
|---|---|---|
| GET | `/api/answer-memory?q=&trust=` | list + stats |
| GET | `/api/answer-memory/export` | portable JSON export (no embeddings) |
| POST | `/api/answer-memory/learn` | `{question, answer, scope?, freshness?}` or `{message_id}` |
| POST | `/api/answer-memory/forget` | `{id}` or `{question}` / `{message_id}` |
| POST | `/api/answer-memory/mark-incorrect` | `{id\|question\|message_id, correction?}` |
| POST | `/api/answer-memory/update` | edit text/question/trust/freshness |
| POST | `/api/answer-memory/merge` | merge `from_id` into `into_id` |
| POST | `/api/answer-memory/refresh` | re-validate a stale answer |
| POST | `/api/answer-memory/clear` | wipe scope (`all`/`experiences`/…) |
| POST | `/api/answer-memory/rebuild-index` | re-embed all rows |
| POST | `/api/answer-memory/vacuum` | compact the DB |
| POST | `/api/answer-memory/import` | restore an export |

`POST /api/conversations/feedback` (the thumbs buttons) also forwards
up/down signals into Answer Memory trust scoring.

## UI

`web/answers.html` — **Learned Answers** page: search/filter by trust state,
learn/forget/mark-incorrect/update/merge/refresh, usage + confidence +
freshness + scope columns, stats strip, export/import, rebuild/vacuum/clear.
Linked from every page's nav.

In chat, every assistant message gets a 🧠 *Learn* button next to the
existing 👍/👎/🔊 controls. Memory-served messages display an
"◈ Answered from memory · N ms" badge.

## Security & privacy

- `validation.contains_secret()` scans both question and answer before
  persistence — API keys, passwords (`password is …`), tokens, cookies,
  connection strings, private keys are refused (learn) or suppressed
  (experience); nothing secret is written to the DB or FTS index.
- No chain-of-thought is ever stored — only final answers, model/tool
  metadata, feedback, and corrections.
- Everything stays on disk locally; export/import is explicit and
  user-driven.
- **Creator-locked identity facts** (`localcodeagent/identity.py`):
  Nexus Core's birthday (September 30th, 2026) and her father/creator
  (John Hamburn) are hard-coded constants. Identity questions are answered
  deterministically at tier-0 — before Answer Memory — and every memory
  write path (`learn`, `forget`, `mark_incorrect`, `edit`, `import_`,
  `record_exchange`, aliases, conversation-memory facts) refuses content
  targeting those topics. On startup `_enforce_locked_identity()`
  invalidates any pre-existing stored answers that contradict them.

## Failure modes

- DB missing/corrupt/locked → service reports `available=False`, chat falls
  back to normal model routing. Nothing raises into the request path.
- All public service methods are exception-guarded; a failure mid-write can
  never take down a chat turn.
- `answer_memory_enabled=false` disables the subsystem entirely.

## Maintenance & lifecycle

- `data/` is excluded from the installer payload and deploy sync, so upgrades
  preserve learned knowledge. The DB is also re-created cleanly if the file
  vanishes.
- `_maybe_prune()` enforces `answer_memory_max_experiences`,
  `answer_memory_experience_retention_days`, and `answer_memory_max_db_mb`.
- `rebuild_index()` re-embeds every answer (used after embedder upgrades);
  `vacuum()` compacts; `integrity_check()` is exposed via stats.

## Config reference

```json
"answer_memory_enabled": true,
"answer_memory_path": "data/nexus_brain/answer_memory.db",
"answer_memory_semantic_enabled": true,
"answer_memory_auto_learn": true,
"answer_memory_auto_promote": true,
"answer_memory_semantic_threshold": 0.50,
"answer_memory_possible_threshold": 0.30,
"answer_memory_experience_retention_days": 90,
"answer_memory_max_experiences": 20000,
"answer_memory_max_db_mb": 256
```

## Nexus Brain integration

Answer Memory is one subsystem of the Brain's larger memory architecture and
is gated by the creator-locked `answer_memory` Brain subroutine. Precedence:

1. explicit current user instruction/correction
2. verified deterministic state
3. verified Nexus Brain knowledge
4. trusted Answer Memory
5. candidate/observed answers (context only)
6. ordinary model answer

## Limitations & future work

- `hashed-ngram-v1` is lexical. Very deep paraphrases land in the `possible`
  band rather than bypassing — by design (precision over recall). A local
  sentence-transformer embedder can be dropped in behind the embedder
  interface later; `embedding_model` versioning + `rebuild_index()` already
  support migration.
- Negation inside a question ("what can it NOT do") is not specially
  modeled — it may match the affirmative's canonical form. Prefer
  corrections in that case.
- Experience rows are the seed for a future offline distillation/fine-tune
  path (model-growth export) — not implemented yet.
- Stat bumps are deferred by up to `flush_interval` (5 s); a hard kill can
  lose a few seconds of use-count accuracy. Answers themselves are always
  written synchronously.
