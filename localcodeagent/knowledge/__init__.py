"""Relationship-aware knowledge graph on top of Nexus Brain (Part M).

Entities (projects, repos, files, people, tools, models, services,
decisions, tasks, artifacts) and typed edges stored in a small SQLite DB
at ``data/knowledge_graph.db``. Retrieval returns a bounded subgraph —
relevant context without stuffing all history into prompts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    attrs TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ent_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS ix_ent_name ON entities(name);
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    rel TEXT NOT NULL,
    attrs TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(src, dst, rel)
);
CREATE INDEX IF NOT EXISTS ix_edge_src ON edges(src);
CREATE INDEX IF NOT EXISTS ix_edge_dst ON edges(dst);
"""


class KnowledgeGraph:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Per-call connections keep no SQLite handle open between queries —
        # a persistent handle locks the file on Windows and blocks tempdir
        # cleanup / backup copies. Open+close per call is cheap for this DB.
        with self._conn() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        db = sqlite3.connect(str(self.path))
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # -- entities ---------------------------------------------------------

    def add_entity(self, kind: str, name: str, *,
                   attrs: dict | None = None,
                   entity_id: str = "") -> dict[str, Any]:
        eid = entity_id or f"{kind}:{name}"
        now = time.time()
        with self._lock, self._conn() as db:
            db.execute(
                "INSERT INTO entities(id,kind,name,attrs,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET attrs=?, updated_at=?",
                (eid, kind, name, json.dumps(attrs or {}), now, now,
                 json.dumps(attrs or {}), now))
        return {"id": eid, "kind": kind, "name": name}

    def get_entity(self, entity_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT id,kind,name,attrs FROM entities WHERE id=?",
                (entity_id,)).fetchone()
        if not row:
            return None
        return {"id": row[0], "kind": row[1], "name": row[2],
                "attrs": json.loads(row[3])}

    def find_entities(self, *, kind: str = "", name_like: str = "",
                      limit: int = 50) -> list[dict]:
        sql = "SELECT id,kind,name,attrs FROM entities WHERE 1=1"
        args: list[Any] = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if name_like:
            sql += " AND name LIKE ?"
            args.append(f"%{name_like}%")
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as db:
            rows = db.execute(sql, args).fetchall()
        return [{"id": r[0], "kind": r[1], "name": r[2],
                 "attrs": json.loads(r[3])} for r in rows]

    # -- edges --------------------------------------------------------------

    def _endpoint_id(self, ref: str) -> str:
        """Resolve an edge endpoint to a canonical entity id. Accepts an
        existing entity id, an entity name (kind-agnostic), or a bare name —
        unknown names become kind='entity' stubs so edges always join."""
        ref = str(ref or "").strip()
        if not ref:
            return ref
        if self.get_entity(ref) is not None:
            return ref
        with self._conn() as db:
            rows = db.execute(
                "SELECT id FROM entities WHERE name=? ORDER BY updated_at DESC"
                " LIMIT 1", (ref,)).fetchall()
        if rows:
            return rows[0][0]
        return self.add_entity("entity", ref)["id"]

    def link(self, src: str, dst: str, rel: str,
             attrs: dict | None = None) -> dict[str, Any]:
        eid = f"e-{uuid.uuid4().hex[:10]}"
        src_id = self._endpoint_id(src)
        dst_id = self._endpoint_id(dst)
        with self._lock, self._conn() as db:
            db.execute(
                "INSERT OR IGNORE INTO edges(id,src,dst,rel,attrs,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (eid, src_id, dst_id, rel, json.dumps(attrs or {}),
                 time.time()))
        return {"id": eid, "src": src_id, "dst": dst_id, "rel": rel}

    def neighbors(self, entity_id: str, *, rel: str = "",
                  depth: int = 1, limit: int = 60) -> list[dict]:
        """Bounded BFS around an entity — returns nodes + edges."""
        seen = {entity_id}
        seen_edges: set[str] = set()
        frontier = [entity_id]
        edges: list[dict] = []
        for _ in range(max(1, depth)):
            if not frontier:
                break
            # Match edges on entity ids AND names — links created before
            # endpoint canonicalization stored raw names.
            names = [e["name"] for e in
                     (self.get_entity(i) for i in frontier) if e]
            keys = list(dict.fromkeys([*frontier, *names]))
            marks = ",".join("?" * len(keys))
            sql = (f"SELECT id,src,dst,rel,attrs FROM edges "
                   f"WHERE (src IN ({marks}) OR dst IN ({marks}))")
            args: list[Any] = keys + keys
            if rel:
                sql += " AND rel=?"
                args.append(rel)
            sql += f" LIMIT {int(limit)}"
            nxt = []
            with self._conn() as db:
                rows = db.execute(sql, args).fetchall()
            for r in rows:
                if r[0] not in seen_edges:
                    seen_edges.add(r[0])
                    edges.append({"id": r[0], "src": r[1], "dst": r[2],
                                  "rel": r[3], "attrs": json.loads(r[4])})
                for other in (r[1], r[2]):
                    ent = self.get_entity(other)
                    key = ent["id"] if ent else other
                    if key not in seen:
                        seen.add(key)
                        nxt.append(key)
            frontier = nxt
        nodes = [e for e in (self.get_entity(i) for i in seen) if e]
        return {"nodes": nodes[:limit], "edges": edges[:limit]}

    def context_for(self, name: str, *, limit: int = 25) -> str:
        """Bounded textual context for prompts: entity + direct neighbors."""
        hits = self.find_entities(name_like=name, limit=3)
        if not hits:
            return ""
        lines = []
        for ent in hits:
            sub = self.neighbors(ent["id"], depth=1, limit=limit)
            for e in sub["edges"]:
                lines.append(f"{e['src']} --{e['rel']}--> {e['dst']}")
        return "\n".join(lines[:limit])

    def stats(self) -> dict[str, int]:
        with self._conn() as db:
            e = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            x = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return {"entities": e, "edges": x}

    def close(self) -> None:
        # Connections are per-call — nothing persistent to release.
        pass
