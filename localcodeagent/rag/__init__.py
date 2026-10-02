"""Persistent incremental repository index (Part D).

``RepoIndex`` keeps a SQLite index of files → symbols/chunks under
``.agent/rag_index.db``. Updates are incremental: only files whose mtime/
size/hash changed are re-parsed. Search returns symbol + chunk hits with
paths for the coding workflow — instead of re-scanning the tree every
time.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL,
    sha TEXT NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    file TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    line INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS ix_sym_file ON symbols(file);
CREATE TABLE IF NOT EXISTS chunks (
    file TEXT NOT NULL,
    ord INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunk_file ON chunks(file);
"""

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", "data", "models", ".agent/rag_index.db"}
INDEXABLE = {".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".txt",
             ".json", ".toml", ".cfg", ".iss", ".cs", ".ps1", ".yml",
             ".yaml", ".html", ".css"}
MAX_FILE = 512_000
CHUNK_LINES = 60


class RepoIndex:
    def __init__(self, workspace: Path, *,
                 db_path: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.db_path = Path(db_path) if db_path else (
            self.workspace / ".agent" / "rag_index.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _iter_files(self) -> list[Path]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.workspace):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            rel_dir = Path(dirpath).relative_to(self.workspace)
            if str(rel_dir).startswith("data") or ".agent" in rel_dir.parts:
                dirnames[:] = []
                continue
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in INDEXABLE:
                    out.append(p)
        return out[:4000]

    @staticmethod
    def _sha(path: Path) -> str:
        try:
            h = hashlib.sha1()
            with path.open("rb") as fh:
                h.update(fh.read(MAX_FILE))
            return h.hexdigest()[:16]
        except OSError:
            return ""

    def _index_file(self, path: Path, rel: str) -> None:
        from ..tools.codeintel import extract_symbols
        text = ""
        try:
            if path.stat().st_size <= MAX_FILE:
                text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self._db.execute("DELETE FROM symbols WHERE file=?", (rel,))
        self._db.execute("DELETE FROM chunks WHERE file=?", (rel,))
        for sym in extract_symbols(path).get("symbols", []):
            self._db.execute(
                "INSERT INTO symbols(file,kind,name,line) VALUES(?,?,?,?)",
                (rel, sym.get("kind", ""), sym.get("name", ""),
                 int(sym.get("line", 0) or 0)))
        if text:
            lines = text.splitlines()
            for i in range(0, len(lines), CHUNK_LINES):
                self._db.execute(
                    "INSERT INTO chunks(file,ord,text) VALUES(?,?,?)",
                    (rel, i // CHUNK_LINES,
                     "\n".join(lines[i:i + CHUNK_LINES])[:8000]))
        st = path.stat()
        self._db.execute(
            "INSERT OR REPLACE INTO files(path,mtime,size,sha,indexed_at)"
            " VALUES(?,?,?,?,?)",
            (rel, st.st_mtime, st.st_size, self._sha(path), time.time()))

    def update(self, *, force: bool = False) -> dict[str, int]:
        """Incremental update: only changed files are re-parsed; deleted
        files are dropped."""
        added = updated = removed = 0
        with self._lock:
            known = {r[0]: (r[1], r[2], r[3]) for r in self._db.execute(
                "SELECT path,mtime,size,sha FROM files").fetchall()}
            seen = set()
            for p in self._iter_files():
                rel = str(p.relative_to(self.workspace)).replace("\\", "/")
                seen.add(rel)
                try:
                    st = p.stat()
                except OSError:
                    continue
                old = known.get(rel)
                if not force and old and old[0] == st.st_mtime \
                        and old[1] == st.st_size:
                    continue  # unchanged fast-path
                sha = self._sha(p)
                if not force and old and old[2] == sha:
                    continue  # mtime moved but content identical
                self._index_file(p, rel)
                if old:
                    updated += 1
                else:
                    added += 1
            for rel in set(known) - seen:
                self._db.execute("DELETE FROM files WHERE path=?", (rel,))
                self._db.execute("DELETE FROM symbols WHERE file=?", (rel,))
                self._db.execute("DELETE FROM chunks WHERE file=?", (rel,))
                removed += 1
            self._db.commit()
        return {"added": added, "updated": updated, "removed": removed}

    def search(self, query: str, *, limit: int = 20) -> list[dict]:
        """Symbol + text search across the index."""
        q = query.strip()
        if not q:
            return []
        hits: list[dict] = []
        with self._lock:
            for r in self._db.execute(
                    "SELECT file,kind,name,line FROM symbols WHERE name LIKE ?"
                    " ORDER BY file LIMIT ?", (f"%{q}%", limit)).fetchall():
                hits.append({"type": "symbol", "file": r[0], "kind": r[1],
                             "name": r[2], "line": r[3]})
            if len(hits) < limit:
                for r in self._db.execute(
                        "SELECT file,ord,substr(text,1,400) FROM chunks"
                        " WHERE text LIKE ? LIMIT ?",
                        (f"%{q}%", limit - len(hits))).fetchall():
                    hits.append({"type": "chunk", "file": r[0],
                                 "ord": r[1], "snippet": r[2][:400]})
        return hits

    def stats(self) -> dict[str, int]:
        with self._lock:
            f = self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            s = self._db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            c = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"files": f, "symbols": s, "chunks": c}

    def close(self) -> None:
        self._db.close()
