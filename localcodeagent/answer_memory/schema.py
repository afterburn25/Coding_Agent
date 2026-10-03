"""SQLite schema for Nexus Answer Memory.

Two conceptual data areas live here:

- ``experiences`` — what happened (may contain unverified answers; NOT
  authoritative on its own).
- ``answers`` — trusted answer memory. Only rows in a usable trust state may
  bypass model inference.
- ``aliases`` — additional normalized phrasings pointing at a canonical answer.
- ``metrics`` — learning-quality counters.
"""

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS experiences (
    id                  TEXT PRIMARY KEY,
    conversation_id     TEXT NOT NULL DEFAULT '',
    response_id         TEXT NOT NULL DEFAULT '',
    ts                  REAL NOT NULL,
    raw_question        TEXT NOT NULL,
    normalized_question TEXT NOT NULL,
    embedding           BLOB,
    raw_answer          TEXT NOT NULL,
    model_id            TEXT NOT NULL DEFAULT '',
    model_role          TEXT NOT NULL DEFAULT '',
    inference_time_ms   REAL NOT NULL DEFAULT 0,
    tools_used          TEXT NOT NULL DEFAULT '[]',
    research_used       INTEGER NOT NULL DEFAULT 0,
    sources             TEXT NOT NULL DEFAULT '[]',
    project_id          TEXT NOT NULL DEFAULT '',
    repository          TEXT NOT NULL DEFAULT '',
    git_commit          TEXT NOT NULL DEFAULT '',
    brain_revision      TEXT NOT NULL DEFAULT '',
    user_feedback       TEXT NOT NULL DEFAULT '',
    corrected           INTEGER NOT NULL DEFAULT 0,
    correction_text     TEXT NOT NULL DEFAULT '',
    final_outcome       TEXT NOT NULL DEFAULT '',
    cacheability        TEXT NOT NULL DEFAULT 'reusable',
    profile_id          TEXT NOT NULL DEFAULT '',
    suppressed          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_exp_norm ON experiences(normalized_question);
CREATE INDEX IF NOT EXISTS idx_exp_conv ON experiences(conversation_id, ts);
CREATE INDEX IF NOT EXISTS idx_exp_ts ON experiences(ts);

CREATE TABLE IF NOT EXISTS answers (
    id                   TEXT PRIMARY KEY,
    canonical_question   TEXT NOT NULL,
    normalized_question  TEXT NOT NULL,
    answer_text          TEXT NOT NULL,
    embedding            BLOB,
    embedding_model      TEXT NOT NULL DEFAULT '',
    topic                TEXT NOT NULL DEFAULT '',
    answer_type          TEXT NOT NULL DEFAULT 'fact',
    confidence           REAL NOT NULL DEFAULT 0.5,
    trust_state          TEXT NOT NULL DEFAULT 'observed',
    source_type          TEXT NOT NULL DEFAULT 'model',
    source_metadata      TEXT NOT NULL DEFAULT '{}',
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    last_verified_at     REAL,
    expires_at           REAL,
    use_count            INTEGER NOT NULL DEFAULT 0,
    successful_use_count INTEGER NOT NULL DEFAULT 0,
    occurrence_count     INTEGER NOT NULL DEFAULT 1,
    correction_count     INTEGER NOT NULL DEFAULT 0,
    invalidated          INTEGER NOT NULL DEFAULT 0,
    invalidation_reason  TEXT NOT NULL DEFAULT '',
    project_scope        TEXT NOT NULL DEFAULT 'global',
    project_id           TEXT NOT NULL DEFAULT '',
    repository           TEXT NOT NULL DEFAULT '',
    git_commit           TEXT NOT NULL DEFAULT '',
    config_fingerprint   TEXT NOT NULL DEFAULT '',
    brain_revision       TEXT NOT NULL DEFAULT '',
    content_hash         TEXT NOT NULL DEFAULT '',
    freshness            TEXT NOT NULL DEFAULT 'static',
    handler_key          TEXT NOT NULL DEFAULT '',
    profile_id           TEXT NOT NULL DEFAULT '',
    last_used_at         REAL
);
CREATE INDEX IF NOT EXISTS idx_ans_norm ON answers(normalized_question);
CREATE INDEX IF NOT EXISTS idx_ans_trust ON answers(trust_state);
CREATE INDEX IF NOT EXISTS idx_ans_scope ON answers(project_scope, project_id);
CREATE INDEX IF NOT EXISTS idx_ans_profile ON answers(profile_id);

CREATE TABLE IF NOT EXISTS aliases (
    normalized_question TEXT PRIMARY KEY,
    answer_id           TEXT NOT NULL REFERENCES answers(id) ON DELETE CASCADE,
    use_count           INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alias_answer ON aliases(answer_id);

CREATE TABLE IF NOT EXISTS metrics (
    key   TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0
);
"""

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS answers_fts USING fts5(
    normalized_question,
    canonical_question,
    answer_text,
    content='answers',
    content_rowid='rowid'
);
"""

# Keep the FTS index in sync with the answers table.
FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS answers_fts_ins AFTER INSERT ON answers BEGIN
    INSERT INTO answers_fts(rowid, normalized_question, canonical_question, answer_text)
    VALUES (new.rowid, new.normalized_question, new.canonical_question, new.answer_text);
END;
CREATE TRIGGER IF NOT EXISTS answers_fts_del AFTER DELETE ON answers BEGIN
    INSERT INTO answers_fts(answers_fts, rowid, normalized_question, canonical_question, answer_text)
    VALUES ('delete', old.rowid, old.normalized_question, old.canonical_question, old.answer_text);
END;
CREATE TRIGGER IF NOT EXISTS answers_fts_upd AFTER UPDATE ON answers BEGIN
    INSERT INTO answers_fts(answers_fts, rowid, normalized_question, canonical_question, answer_text)
    VALUES ('delete', old.rowid, old.normalized_question, old.canonical_question, old.answer_text);
    INSERT INTO answers_fts(rowid, normalized_question, canonical_question, answer_text)
    VALUES (new.rowid, new.normalized_question, new.canonical_question, new.answer_text);
END;
"""
