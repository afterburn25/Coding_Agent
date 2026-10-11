"""Thread-safe SQLite store for Nexus Answer Memory.

- WAL journal, NORMAL synchronous, foreign keys on.
- All SQL is parameterized — no interpolation of user input.
- Corruption at open → the file is quarantined (renamed, never deleted) and a
  fresh database is created so Nexus keeps working.
- Connections are opened per operation and closed immediately after. Holding
  no long-lived connection keeps the DB file unlocked between calls, which
  matters on Windows (file deletion/backup while Nexus is running).
- Read hot path uses an in-memory snapshot of the answers/aliases tables, so
  lookups cost microseconds and never touch disk.
- Cheap stat writes (use counts, metrics) are deferred and piggyback on the
  next connection instead of paying a WAL checkpoint per bump.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import migrations

_CACHED_TABLES = ("answers", "aliases")


class AnswerMemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.fts = False
        self.corrupt_quarantine = ""
        self.integrity_ok = True
        self.flush_interval = 5.0
        self.flush_batch = 64
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._pending: list[tuple[str, tuple[Any, ...]]] = []
        self._last_flush = time.monotonic()
        self._open()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            conn.close()
            raise
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _open(self) -> None:
        # A previous background integrity check may have flagged this file —
        # quarantine it now, while no connection holds it open.
        try:
            pending = self.path.with_name(self.path.name + ".corrupt-pending")
            if pending.exists():
                pending.unlink()
                quarantine = self.path.with_name(
                    f"{self.path.name}.corrupt-{int(time.time())}")
                try:
                    shutil.move(str(self.path), str(quarantine))
                    self.corrupt_quarantine = str(quarantine)
                except OSError:
                    pass
        except OSError:
            pass
        existed = self.path.exists() and self.path.stat().st_size > 0
        # A non-empty -wal means the previous close was a crash — SQLite
        # replays it on connect, which is exactly when integrity must be
        # verified before use. On a clean close, quick_check(1) still scans
        # every index (~15s on a 12MB store — it stalled app boot past the
        # port bind); defer that to a background pass instead.
        wal_pending = False
        try:
            wal = self.path.with_name(self.path.name + "-wal")
            wal_pending = wal.exists() and wal.stat().st_size > 0
        except OSError:
            pass
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            if existed and wal_pending:
                conn.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.DatabaseError:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            quarantine = self.path.with_name(
                f"{self.path.name}.corrupt-{int(time.time())}"
            )
            try:
                shutil.move(str(self.path), str(quarantine))
                self.corrupt_quarantine = str(quarantine)
            except OSError:
                pass
            conn = self._connect()
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            version = int(row[0]) if row else 0
            if existed and version > 0 and version < migrations.SCHEMA_VERSION:
                # Backup before destructive-capable migration steps.
                backup = self.path.with_name(f"{self.path.name}.pre-v{version}.bak")
                try:
                    conn.commit()
                    shutil.copy2(self.path, backup)
                except OSError:
                    pass
            migrations.apply(conn)
            conn.commit()
            self.fts = migrations.fts_available(conn)
        finally:
            conn.close()
        if existed and not wal_pending:
            self._schedule_integrity_check()

    # -- integrity ----------------------------------------------------------

    _INTEGRITY_RECHECK_S = 86400.0

    def _schedule_integrity_check(self) -> None:
        """Clean-close path: re-verify at most once a day, off the boot
        path. On failure the DB is flagged via a `.corrupt-pending` marker —
        moving a live file out from under open connections is unsafe on
        Windows, so the next _open() performs the quarantine."""
        marker = self.path.with_name(self.path.name + ".integrity-ok")
        try:
            if marker.exists() and (
                    time.time() - marker.stat().st_mtime
                    < self._INTEGRITY_RECHECK_S):
                return
        except OSError:
            pass
        self._integrity_thread = threading.Thread(
            target=self._integrity_check_once,
            name="answer-memory-integrity",
            daemon=True)
        self._integrity_thread.start()

    def _integrity_check_once(self) -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("PRAGMA quick_check(1)").fetchone()
            self.path.with_name(
                self.path.name + ".integrity-ok").touch()
        except sqlite3.DatabaseError:
            self.integrity_ok = False
            try:
                self.path.with_name(
                    self.path.name + ".corrupt-pending").touch()
            except OSError:
                pass
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # -- generic helpers -----------------------------------------------------

    def _invalidate(self, sql: str) -> None:
        low = sql.lower()
        for table in _CACHED_TABLES:
            if table in low:
                self._cache.pop(table, None)

    def _apply_pending(self, conn: sqlite3.Connection) -> None:
        if not self._pending:
            return
        for sql, params in self._pending:
            conn.execute(sql, params)
        self._pending.clear()
        self._last_flush = time.monotonic()

    def _maybe_flush(self) -> None:
        if self._pending and (
            len(self._pending) >= self.flush_batch
            or time.monotonic() - self._last_flush >= self.flush_interval
        ):
            conn = self._connect()
            try:
                self._apply_pending(conn)
                conn.commit()
            finally:
                conn.close()

    def defer(self, sql: str, params: Iterable[Any] = (), *, invalidate: bool = True) -> None:
        """Queue a stat write to ride the next connection (or a timed flush).

        ``invalidate=False`` for pure stat bumps (use counts) — the in-memory
        snapshot may lag on those fields until the next structural write.
        """
        with self._lock:
            if invalidate:
                self._invalidate(sql)
            self._pending.append((sql, tuple(params)))
            self._maybe_flush()

    def flush(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                self._apply_pending(conn)
                conn.commit()
            finally:
                conn.close()

    def cached(self, table: str) -> list[dict[str, Any]]:
        """In-memory snapshot of a small table (answers/aliases)."""
        if table not in _CACHED_TABLES:
            raise ValueError(f"uncacheable table: {table}")
        with self._lock:
            self._maybe_flush()
            rows = self._cache.get(table)
            if rows is None:
                rows = self.query(f"SELECT * FROM {table}")
                self._cache[table] = rows
            return [dict(r) for r in rows]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._invalidate(sql)
            conn = self._connect()
            try:
                self._apply_pending(conn)
                conn.execute(sql, tuple(params))
                conn.commit()
            finally:
                conn.close()

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._invalidate(sql)
            conn = self._connect()
            try:
                self._apply_pending(conn)
                conn.executemany(sql, seq)
                conn.commit()
            finally:
                conn.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                self._apply_pending(conn)
                cur = conn.execute(sql, tuple(params))
                rows = [dict(r) for r in cur.fetchall()]
                conn.commit()
                return rows
            finally:
                conn.close()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def bump(self, key: str, amount: float = 1.0) -> None:
        self.defer(
            "INSERT INTO metrics(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = value + ?",
            (key, amount, amount),
        )

    def metric(self, key: str) -> float:
        row = self.query_one("SELECT value FROM metrics WHERE key=?", (key,))
        return float(row["value"]) if row else 0.0

    def metrics(self) -> dict[str, float]:
        return {r["key"]: float(r["value"]) for r in self.query("SELECT key, value FROM metrics")}

    def vacuum(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("VACUUM")
                conn.execute("PRAGMA optimize")
            finally:
                conn.close()

    def rebuild_fts(self) -> bool:
        if not self.fts:
            return False
        self.execute("INSERT INTO answers_fts(answers_fts) VALUES('rebuild')")
        return True

    def integrity_check(self) -> bool:
        try:
            row = self.query_one("PRAGMA integrity_check(1)")
            return bool(row) and str(row.get("integrity_check", "")).startswith("ok")
        except sqlite3.DatabaseError:
            return False

    def size_bytes(self) -> int:
        try:
            return int(self.path.stat().st_size)
        except OSError:
            return 0

    def close(self) -> None:
        # No long-lived connection; close flushes deferred stat writes.
        # Join the background integrity checker first — its side
        # connection still touches the DB file (and WAL sidecars), so a
        # teardown that deletes the store's directory can race it.
        checker = getattr(self, "_integrity_thread", None)
        if checker is not None:
            try:
                checker.join(timeout=10.0)
            except Exception:
                pass
        try:
            self.flush()
        except Exception:
            pass
