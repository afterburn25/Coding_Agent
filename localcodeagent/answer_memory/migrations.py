"""Schema migrations for the Answer Memory database.

Migrations are idempotent and keyed off ``PRAGMA user_version``. An upgrade
never destroys an existing database — each step only adds or alters in a
forward-compatible way, and the store takes a file-level backup before
running any migration step on a pre-existing database.
"""

from __future__ import annotations

import sqlite3

from .schema import DDL, FTS_DDL, FTS_TRIGGERS, SCHEMA_VERSION


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    try:
        conn.executescript(FTS_DDL)
        conn.executescript(FTS_TRIGGERS)
    except sqlite3.DatabaseError:
        # FTS5 may be unavailable in a stripped sqlite build — lexical
        # retrieval still works without it.
        pass


def _add_column_if_missing(conn: sqlite3.Connection, table: str, ddl: str, column: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migration_2(conn: sqlite3.Connection) -> None:
    # Profile scoping: answers/experiences recorded while a profile is active
    # belong to that profile; only unscoped ('') rows are shared globally.
    _add_column_if_missing(
        conn, "answers", "profile_id TEXT NOT NULL DEFAULT ''", "profile_id"
    )
    _add_column_if_missing(
        conn, "experiences", "profile_id TEXT NOT NULL DEFAULT ''", "profile_id"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ans_profile ON answers(profile_id)"
    )


MIGRATIONS = {1: _migration_1, 2: _migration_2}


def fts_available(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='answers_fts'"
        ).fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        return False


def _heal_v2_columns(conn: sqlite3.Connection) -> None:
    """Re-assert migration-2 columns regardless of the stamped version.

    Migrations are keyed on PRAGMA user_version, so a database stamped v2
    without both profile_id columns — an interrupted migration, a manual
    stamp, or a partial restore — would report current while every
    profile-scoped write fails. These statements are idempotent, so
    asserting them on every open costs nothing and self-heals the gap.
    """
    _add_column_if_missing(
        conn, "answers", "profile_id TEXT NOT NULL DEFAULT ''", "profile_id"
    )
    _add_column_if_missing(
        conn, "experiences", "profile_id TEXT NOT NULL DEFAULT ''", "profile_id"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ans_profile ON answers(profile_id)"
    )


def apply(conn: sqlite3.Connection) -> int:
    """Bring the database to SCHEMA_VERSION. Returns the resulting version."""
    row = conn.execute("PRAGMA user_version").fetchone()
    version = int(row[0]) if row else 0
    for target in sorted(MIGRATIONS):
        if version < target:
            MIGRATIONS[target](conn)
            conn.execute(f"PRAGMA user_version = {target}")
            version = target
    if version > SCHEMA_VERSION:
        # Database is newer than this build — read-only compatible columns
        # still work; do not downgrade or destroy anything.
        return version
    if version >= 2:
        _heal_v2_columns(conn)
    return SCHEMA_VERSION
