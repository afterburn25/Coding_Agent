"""Versioned transactional state layer — SQLite (WAL) backing for
critical Nexus stores.

Resilience Foundation 1: critical whole-document stores migrate from
rewrite-the-whole-file JSON into a single WAL-mode database so saves
are crash-safe commits, corruption is detectable at boot, and
multi-store updates can share one transaction. Large/binary payloads
(models, generated artifacts) stay files; this DB holds state,
metadata, and verified references only.

Two pieces:

- ``StateDB`` — the database. One file at ``data/state.db``: WAL mode,
  ``foreign_keys=ON``, bounded ``busy_timeout``, a
  ``schema_migrations`` table, domain-keyed ``kv`` rows for migrated
  document stores, a ``quarantine`` table for payloads that fail to
  parse, ``operations`` rows for exactly-once external actions, and an
  ``events`` stream for audit replay. ``txn()`` gives callers a real
  multi-row atomic unit.

- ``DocStore`` — transparent per-file adapter. A store keeps its
  load/mutate/save shape; DocStore moves the bytes: DB row when a
  ``StateDB`` is provided, legacy atomic file otherwise. A valid
  legacy file imports once and is renamed ``<file>.migrated`` (never
  deleted); a live shadow file is still written on every save so
  downgrades and file-level tools keep working. Corrupt sources are
  quarantined and flagged — never silently reset.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .fsutil import atomic_write_text

SCHEMA_VERSION = 1


class StateDB:
    """Single WAL-mode SQLite state store for critical Nexus state."""

    def __init__(self, path: Path | str, *, busy_ms: int = 5000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_ms = int(busy_ms)
        self._lock = threading.RLock()
        self.degraded: list[dict[str, str]] = []
        # No persistent connection — every operation opens, works, and
        # closes. WAL persists in the database file once set here, so
        # each open only re-applies the per-connection pragmas. A closed
        # handle can never hold the file across a teardown, and crash
        # safety improves: there is no dangling connection to corrupt.
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate(conn)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=self._busy_ms / 1000.0,
            check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self._busy_ms}")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple = ()) -> list:
        """Convenience read/write for one-shot statements."""
        with self._conn() as conn:
            return conn.execute(sql, params).fetchall()

    # -- schema -----------------------------------------------------------

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply pending schema versions in order. Idempotent: each
        version runs once, recorded in schema_migrations inside the same
        transaction as its DDL so a crash mid-migration retries cleanly."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version INTEGER PRIMARY KEY,"
            "applied_at REAL NOT NULL)")
        applied = {
            r[0] for r in
            conn.execute("SELECT version FROM schema_migrations")}
        if 1 not in applied:
                # Per-statement execute inside one transaction —
                # executescript would implicitly COMMIT first and break
                # atomicity. All DDL + the version record share it.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for ddl in (
                            "CREATE TABLE IF NOT EXISTS kv("
                            "domain TEXT NOT NULL,"
                            "key TEXT NOT NULL,"
                            "value TEXT NOT NULL,"
                            "updated_at REAL NOT NULL,"
                            "PRIMARY KEY(domain, key))",
                            "CREATE TABLE IF NOT EXISTS quarantine("
                            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                            "domain TEXT NOT NULL,"
                            "key TEXT NOT NULL,"
                            "reason TEXT NOT NULL,"
                            "payload TEXT,"
                            "quarantined_at REAL NOT NULL)",
                            "CREATE TABLE IF NOT EXISTS operations("
                            "operation_id TEXT PRIMARY KEY,"
                            "intent_hash TEXT NOT NULL DEFAULT '',"
                            "target TEXT NOT NULL DEFAULT '',"
                            "state TEXT NOT NULL,"
                            "started_at REAL NOT NULL,"
                            "result TEXT,"
                            "verification TEXT)",
                            "CREATE TABLE IF NOT EXISTS events("
                            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                            "ts REAL NOT NULL,"
                            "domain TEXT NOT NULL,"
                            "action TEXT NOT NULL,"
                            "detail TEXT NOT NULL DEFAULT '')",
                            "CREATE INDEX IF NOT EXISTS idx_events_ts"
                            " ON events(ts)",
                            "CREATE INDEX IF NOT EXISTS idx_events_domain"
                            " ON events(domain)",
                    ):
                        conn.execute(ddl)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at)"
                        " VALUES(?, ?)", (1, time.time()))
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")

    @property
    def schema_version(self) -> int:
        row = self.execute(
            "SELECT MAX(version) FROM schema_migrations")
        return int(row[0][0] or 0) if row else 0

    # -- transactions ------------------------------------------------------

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """Atomic multi-write unit: BEGIN IMMEDIATE → COMMIT, or ROLLBACK
        on any error. Crash between begin and commit leaves no partial
        state — SQLite restores the pre-transaction image."""
        with self._lock:
            conn = self._open()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
            finally:
                conn.close()

    # -- kv domain ----------------------------------------------------------

    def kv_get(self, domain: str, key: str) -> str | None:
        rows = self.execute(
            "SELECT value FROM kv WHERE domain=? AND key=?",
            (domain, key))
        return rows[0][0] if rows else None

    def kv_put(self, domain: str, key: str, value: str) -> None:
        with self._lock:
            self.execute(
                "INSERT INTO kv(domain, key, value, updated_at)"
                " VALUES(?, ?, ?, ?)"
                " ON CONFLICT(domain, key)"
                " DO UPDATE SET value=excluded.value,"
                "              updated_at=excluded.updated_at",
                (domain, key, value, time.time()))

    def kv_keys(self, domain: str) -> list[str]:
        return [r[0] for r in self.execute(
            "SELECT key FROM kv WHERE domain=? ORDER BY key", (domain,))]

    # -- quarantine -----------------------------------------------------------

    def quarantine(self, domain: str, key: str, reason: str,
                   payload: str | None) -> None:
        """Move a payload that failed to parse out of the live path —
        preserved for inspection/recovery, never deleted."""
        with self._lock:
            self.execute(
                "INSERT INTO quarantine(domain, key, reason, payload,"
                " quarantined_at) VALUES(?, ?, ?, ?, ?)",
                (domain, key, str(reason)[:300],
                 payload if payload is None else payload[:1_000_000],
                 time.time()))

    # -- events (audit-replay groundwork) ------------------------------------

    def record_event(self, domain: str, action: str,
                     detail: str | dict[str, Any] = "") -> None:
        if isinstance(detail, dict):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        with self._lock:
            self.execute(
                "INSERT INTO events(ts, domain, action, detail)"
                " VALUES(?, ?, ?, ?)",
                (time.time(), domain, str(action)[:120],
                 str(detail)[:4000]))

    # -- integrity ------------------------------------------------------------

    def integrity(self, *, parse_probe: bool = True) -> dict[str, Any]:
        """Bounded boot-time check: SQLite quick_check plus a JSON parse
        probe over every kv row (kv payloads are all JSON documents).
        Deep verification is scheduled work, not a boot blocker."""
        report: dict[str, Any] = {
            "path": str(self.path), "ok": True,
            "schema_version": self.schema_version,
            "quick_check": "", "bad_rows": [],
            "kv_rows": 0, "quarantined": 0,
            "degraded": list(self.degraded)}
        try:
            with self._conn() as conn:
                row = conn.execute("PRAGMA quick_check").fetchone()
                report["quick_check"] = str(row[0] if row else "")
                if report["quick_check"] != "ok":
                    report["ok"] = False
                if parse_probe:
                    for domain, key, value in conn.execute(
                            "SELECT domain, key, value FROM kv"):
                        report["kv_rows"] += 1
                        try:
                            json.loads(value)
                        except ValueError:
                            report["ok"] = False
                            report["bad_rows"].append(f"{domain}/{key}")
                row = conn.execute(
                    "SELECT COUNT(*) FROM quarantine").fetchone()
                report["quarantined"] = int(row[0] if row else 0)
        except sqlite3.Error as exc:
            report["ok"] = False
            report["quick_check"] = f"{type(exc).__name__}: {exc}"
        return report

    def close(self) -> None:
        """Kept for lifecycle symmetry — connections are per-operation,
        so nothing persists to close."""
        return None


class DocStore:
    """Persistence for one whole-document store, DB-backed or file-backed.

    ``load_json`` resolution order:
      1. live DB row — parse; on failure the raw value moves to
         ``quarantine`` and the file fallbacks below are tried;
      2. live ``<file>`` — parse; on success imports into the DB and
         renames itself ``<file>.migrated`` (kept, never deleted);
      3. ``<file>.migrated`` — recovery path when the DB was lost but
         the frozen migration copy survives;
      4. caller's default.

    Every corrupt source is copied/quarantined and recorded in
    ``issues`` — a damaged store boots degraded, never silently empty.
    ``save_json`` writes the DB row first (crash-safe WAL commit), then
    a live file shadow so file-level tooling and code downgrades still
    see current state.
    """

    def __init__(self, db: StateDB | None, path: Path | str,
                 *, domain: str | None = None) -> None:
        self.db = db
        self.path = Path(path)
        self.domain = domain or self.path.stem
        self.key = self.path.name
        self.issues: list[str] = []
        self.migrated = False

    # -- read --------------------------------------------------------------

    def load_json(self, default: Any,
                  migrate: Callable[[Any], Any] | None = None) -> Any:
        migrate = migrate or (lambda raw: raw)
        if self.db is not None:
            raw = self.db.kv_get(self.domain, self.key)
            if raw is not None:
                try:
                    return migrate(json.loads(raw))
                except (ValueError, TypeError) as exc:
                    self._flag(f"db row corrupt: {exc}")
                    self.db.quarantine(
                        self.domain, self.key, f"parse: {exc}", raw)
                    self.db.execute(
                        "DELETE FROM kv WHERE domain=? AND key=?",
                        (self.domain, self.key))
        for candidate in (self.path,
                          self.path.with_name(self.path.name + ".migrated")):
            if not candidate.is_file():
                continue
            try:
                obj = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                self._flag(f"{candidate.name} corrupt: {exc}")
                self._quarantine_file(candidate, exc)
                continue
            try:
                obj = migrate(obj)
            except Exception as exc:
                self._flag(f"{candidate.name} migrate failed: {exc}")
                self._quarantine_file(candidate, exc)
                continue
            if self.db is not None:
                self._import(obj, candidate)
            return obj
        return default

    def _import(self, obj: Any, source: Path) -> None:
        """Write the imported doc to the DB, then freeze the source as
        ``<file>.migrated``. Verification: the row must read back and
        parse equal before the rename — the original is never removed
        until migrated state verifies."""
        self.db.kv_put(self.domain, self.key, json.dumps(
            obj, ensure_ascii=False, default=str))
        back = self.db.kv_get(self.domain, self.key)
        if json.loads(back) != obj:
            self._flag(f"{source.name} import verify failed")
            self.db.execute(
                "DELETE FROM kv WHERE domain=? AND key=?",
                (self.domain, self.key))
            return
        if source == self.path:
            try:
                shutil.copy2(source, source.with_name(
                    source.name + ".migrated"))
                self.migrated = True
            except OSError as exc:
                self._flag(f"migration backup failed: {exc}")

    def _quarantine_file(self, path: Path, exc: Exception) -> None:
        payload = None
        try:
            payload = path.read_text(encoding="utf-8", errors="replace")
            shutil.copy2(path, path.with_name(
                path.name + f".corrupt-{int(time.time())}"))
        except OSError:
            pass
        if self.db is not None:
            self.db.quarantine(
                self.domain, self.key, f"file parse: {exc}", payload)

    def _flag(self, issue: str) -> None:
        self.issues.append(issue)
        if self.db is not None:
            self.db.degraded.append(
                {"domain": self.domain, "key": self.key, "issue": issue})

    # -- write ---------------------------------------------------------------

    def save_json(self, obj: Any, *, indent: int | None = 2) -> None:
        text = json.dumps(obj, indent=indent, ensure_ascii=False,
                          default=str)
        if self.db is not None:
            self.db.kv_put(self.domain, self.key, text)
        # Live shadow file: keeps file-level tools and pre-DB code
        # downgrades working; a failed shadow never loses truth (DB row
        # already committed).
        try:
            atomic_write_text(self.path, text)
        except OSError:
            if self.db is None:
                raise

    @property
    def degraded(self) -> bool:
        return bool(self.issues)
