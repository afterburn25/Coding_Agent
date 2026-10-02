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


MIGRATIONS = {1: _migration_1}


def fts_available(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='answers_fts'"
        ).fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        return False


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
    return SCHEMA_VERSION
