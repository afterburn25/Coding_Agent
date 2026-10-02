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
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    # -- entities ---------------------------------------------------------

    def add_entity(self, kind: str, name: str, *,
                   attrs: dict | None = None,
                   entity_id: str = "") -> dict[str, Any]:
        eid = entity_id or f"{kind}:{name}"
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO entities(id,kind,name,attrs,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET attrs=?, updated_at=?",
                (eid, kind, name, json.dumps(attrs or {}), now, now,
                 json.dumps(attrs or {}), now))
            self._db.commit()
        return {"id": eid, "kind": kind, "name": name}

    def get_entity(self, entity_id: str) -> dict | None:
        row = self._db.execute(
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
        return [{"id": r[0], "kind": r[1], "name": r[2],
                 "attrs": json.loads(r[3])}
                for r in self._db.execute(sql, args).fetchall()]

    # -- edges --------------------------------------------------------------

    def link(self, src: str, dst: str, rel: str,
             attrs: dict | None = None) -> dict[str, Any]:
        eid = f"e-{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO edges(id,src,dst,rel,attrs,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (eid, src, dst, rel, json.dumps(attrs or {}), time.time()))
            self._db.commit()
        return {"id": eid, "src": src, "dst": dst, "rel": rel}

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
            marks = ",".join("?" * len(frontier))
            sql = (f"SELECT id,src,dst,rel,attrs FROM edges "
                   f"WHERE (src IN ({marks}) OR dst IN ({marks}))")
            args: list[Any] = list(frontier) + list(frontier)
            if rel:
                sql += " AND rel=?"
                args.append(rel)
            sql += f" LIMIT {int(limit)}"
            nxt = []
            for r in self._db.execute(sql, args).fetchall():
                if r[0] not in seen_edges:
                    seen_edges.add(r[0])
                    edges.append({"id": r[0], "src": r[1], "dst": r[2],
                                  "rel": r[3], "attrs": json.loads(r[4])})
                for other in (r[1], r[2]):
                    if other not in seen:
                        seen.add(other)
                        nxt.append(other)
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
        e = self._db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        x = self._db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return {"entities": e, "edges": x}

    def close(self) -> None:
        self._db.close()
