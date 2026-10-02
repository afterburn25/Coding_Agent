"""Hippocampus — persistent memory: retrieval, consolidation, learning.

Separates memory into four kinds and queries them through one interface:

- episodic   "what happened"  — mission outcomes, prior actions, exchanges
- semantic   "what I know"    — answer memory, knowledge graph, locked vault
- procedural "how to do it"   — recorded successful action sequences
- project    "this repo"      — per-project facts, decisions, conventions

Existing stores are queried in place (no data copied): Answer Memory,
knowledge graph, conversation memory, mission/activity history, and the
creator-locked vault. The new durable piece is a small SQLite store for
episodic events, procedures, and project facts — versioned with
``PRAGMA user_version`` migrations and never destructive.

Every recall result carries confidence, provenance, and freshness so the
PFC can weigh trusted memory against live model inference — and so a
trusted memory hit can answer without invoking a large model at all.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .events import CognitiveEvent, EventType
from .regions import BrainRegion
from . import events as ev

SCHEMA_VERSION = 1

_MIGRATIONS = {
    1: """
    CREATE TABLE IF NOT EXISTS episodes (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,            -- mission | task | exchange | event
        summary TEXT NOT NULL,
        detail TEXT DEFAULT '',
        mission_id TEXT DEFAULT '',
        task_id TEXT DEFAULT '',
        project_id TEXT DEFAULT '',
        confidence REAL DEFAULT 0.5,
        ts REAL NOT NULL,
        invalidated INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project_id, ts);
    CREATE INDEX IF NOT EXISTS idx_episodes_kind ON episodes(kind, ts);

    CREATE TABLE IF NOT EXISTS procedures (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        trigger_text TEXT DEFAULT '',
        steps TEXT NOT NULL,           -- JSON list of action dicts
        success_count INTEGER DEFAULT 1,
        failure_count INTEGER DEFAULT 0,
        confidence REAL DEFAULT 0.5,
        project_id TEXT DEFAULT '',
        last_used REAL DEFAULT 0,
        created REAL NOT NULL,
        invalidated INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_procedures_name ON procedures(name);

    CREATE TABLE IF NOT EXISTS project_facts (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        category TEXT DEFAULT 'fact',  -- architecture|decision|issue|dependency|convention|status|fix|build
        key TEXT DEFAULT '',
        value TEXT NOT NULL,
        confidence REAL DEFAULT 0.7,
        provenance TEXT DEFAULT '',
        ts REAL NOT NULL,
        invalidated INTEGER DEFAULT 0,
        UNIQUE(project_id, category, key)
    );
    CREATE INDEX IF NOT EXISTS idx_facts_project ON project_facts(project_id, category);
    """,
}


@dataclass(slots=True)
class MemoryEntry:
    kind: str                       # episodic|semantic|procedural|project
    text: str
    confidence: float = 0.5
    provenance: str = ""            # which store produced it
    freshness: str = ""             # fresh|aging|stale
    ts: float = 0.0
    source_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text,
                "confidence": self.confidence, "provenance": self.provenance,
                "freshness": self.freshness, "ts": self.ts,
                "source_id": self.source_id}


@dataclass(slots=True)
class RecallResult:
    entries: list[MemoryEntry] = field(default_factory=list)
    trusted_answer: str = ""        # set when a trusted semantic hit exists
    latency_ms: float = 0.0


def _freshness(ts: float) -> str:
    age = time.time() - float(ts or 0)
    if age < 86400:
        return "fresh"
    if age < 30 * 86400:
        return "aging"
    return "stale"


class Hippocampus(BrainRegion):
    name = ev.REGION_HIPPOCAMPUS

    def __init__(self, bus, db_path: Path | None = None, *,
                 answer_memory=None, knowledge_graph=None,
                 knowledge_memory=None, conversation_memory=None,
                 locked_vault=None, activity_source: Callable[[int], list[dict]] | None = None,
                 embedder=None) -> None:
        super().__init__(bus)
        self.answer_memory = answer_memory
        self.knowledge_graph = knowledge_graph
        self.knowledge_memory = knowledge_memory
        self.conversation_memory = conversation_memory
        self.locked_vault = locked_vault
        self._activity_source = activity_source
        self._embedder = embedder
        self._lock = threading.RLock()
        self.db_path = Path(db_path) if db_path else None
        self._migrated = False

    # -- connections -------------------------------------------------------------
    # Short-lived connections per operation — same convention as
    # answer_memory.store and knowledge.graph. Holding a persistent handle
    # would lock the file for the process lifetime (Windows).
    def _conn(self) -> sqlite3.Connection | None:
        if self.db_path is None:
            return None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=15.0)
            conn.row_factory = sqlite3.Row
            if not self._migrated:
                self._migrate(conn)
                self._migrated = True
            return conn
        except sqlite3.Error:
            return None

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for v in range(version + 1, SCHEMA_VERSION + 1):
            if v in _MIGRATIONS:
                conn.executescript(_MIGRATIONS[v])
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()

    def close(self) -> None:
        """Nothing persistent to close — connections are per-operation."""

    def _write(self, fn) -> Any:
        conn = self._conn()
        if conn is None:
            return None
        try:
            with self._lock:
                out = fn(conn)
                conn.commit()
                return out
        finally:
            conn.close()

    def _read(self, fn) -> Any:
        conn = self._conn()
        if conn is None:
            return []
        try:
            with self._lock:
                return fn(conn)
        finally:
            conn.close()

    # -- recording ------------------------------------------------------------------
    def record_episode(self, kind: str, summary: str, *, detail: str = "",
                       mission_id: str = "", task_id: str = "",
                       project_id: str = "", confidence: float = 0.5) -> str:
        """Episodic write — what happened. Safe no-op without a DB."""
        eid = uuid.uuid4().hex[:16]
        self._write(lambda c: c.execute(
            "INSERT INTO episodes (id,kind,summary,detail,mission_id,task_id,"
            "project_id,confidence,ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, kind, summary[:2000], detail[:8000], mission_id, task_id,
             project_id, confidence, time.time())))
        return eid

    def record_procedure(self, name: str, steps: list[dict], *,
                         trigger_text: str = "", project_id: str = "",
                         success: bool = True) -> str:
        """Procedural write — successful/failed action sequences become
        candidate habits. Repeat success raises confidence; failure lowers it."""
        def go(c):
            row = c.execute(
                "SELECT * FROM procedures WHERE name=? AND invalidated=0",
                (name,)).fetchone()
            if row is not None:
                wins = row["success_count"] + (1 if success else 0)
                losses = row["failure_count"] + (0 if success else 1)
                conf = round(0.3 + 0.7 * wins / max(1, wins + losses), 3)
                c.execute(
                    "UPDATE procedures SET steps=?, success_count=?, failure_count=?,"
                    " confidence=?, last_used=? WHERE id=?",
                    (json.dumps(steps), wins, losses, conf, time.time(), row["id"]))
                return row["id"]
            pid = uuid.uuid4().hex[:16]
            c.execute(
                "INSERT INTO procedures (id,name,trigger_text,steps,confidence,"
                "project_id,last_used,created) VALUES (?,?,?,?,?,?,?,?)",
                (pid, name, trigger_text[:500], json.dumps(steps),
                 0.6 if success else 0.2, project_id, time.time(), time.time()))
            return pid
        return self._write(go) or ""

    def learn_project_fact(self, project_id: str, key: str, value: str, *,
                           category: str = "fact", confidence: float = 0.7,
                           provenance: str = "") -> str:
        """Project memory — namespaced facts about THIS repository."""
        fid = uuid.uuid4().hex[:16]
        self._write(lambda c: c.execute(
            "INSERT INTO project_facts (id,project_id,category,key,value,"
            "confidence,provenance,ts) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id,category,key) DO UPDATE SET "
            "value=excluded.value, confidence=excluded.confidence, "
            "provenance=excluded.provenance, ts=excluded.ts, invalidated=0",
            (fid, project_id, category, key[:200], value[:4000],
             confidence, provenance[:200], time.time())))
        return fid

    def invalidate(self, source_id: str) -> bool:
        """Invalidate an entry in the local store — mirrors Answer Memory's
        invalidation contract for episodes/procedures/facts."""
        def go(c):
            total = 0
            for table in ("episodes", "procedures", "project_facts"):
                total += c.execute(
                    f"UPDATE {table} SET invalidated=1 WHERE id=?",
                    (source_id,)).rowcount
            return total
        return bool(self._write(go))

    # -- recall ---------------------------------------------------------------------
    def recall(self, query: str, *, kinds: set[str] | None = None,
               project_id: str = "", limit: int = 8,
               min_confidence: float = 0.0) -> RecallResult:
        """Fan out across memory systems. Returns entries tagged with kind,
        confidence, provenance, freshness — plus a trusted_answer when
        Answer Memory or a locked fact can respond without a model."""
        started = time.monotonic()
        kinds = kinds or {"episodic", "semantic", "procedural", "project"}
        entries: list[MemoryEntry] = []
        trusted = ""

        if "semantic" in kinds:
            entries.extend(self._semantic(query, project_id, limit))
            trusted = self._trusted_answer(query, project_id)
        if "episodic" in kinds:
            entries.extend(self._episodic(query, project_id, limit))
        if "procedural" in kinds:
            entries.extend(self._procedural(query, project_id, limit))
        if "project" in kinds and project_id:
            entries.extend(self._project(project_id, limit))

        entries = [e for e in entries if e.confidence >= min_confidence]
        entries.sort(key=lambda e: (e.confidence, e.ts), reverse=True)
        entries = entries[:limit]
        return RecallResult(
            entries=entries, trusted_answer=trusted,
            latency_ms=round((time.monotonic() - started) * 1000, 1))

    def _trusted_answer(self, query: str, project_id: str) -> str:
        """A trusted semantic hit that can answer without model inference."""
        am = self.answer_memory
        if am is not None:
            try:
                # record=False — recall is a peek; hit stats belong to the
                # orchestrator's real answer-memory path.
                match = am.lookup(query, project_id=project_id, record=False)
                if match is not None and match.hit:
                    row = match.answer or {}
                    return str(row.get("answer_text") or "")
            except Exception:
                pass
        return ""

    def _semantic(self, query: str, project_id: str, limit: int) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        am = self.answer_memory
        if am is not None:
            try:
                match = am.lookup(query, project_id=project_id, record=False)
                rows = []
                if match is not None:
                    if match.answer:
                        rows.append(match.answer)
                    rows.extend(match.context_answers or [])
                for row in rows[:limit]:
                    text = str(row.get("answer_text") or "")
                    if text:
                        out.append(MemoryEntry(
                            kind="semantic", text=text,
                            confidence=float(row.get("confidence") or 0.5),
                            provenance="answer_memory",
                            freshness=str(row.get("freshness") or ""),
                            ts=float(row.get("updated_at") or row.get("ts") or 0),
                            source_id=str(row.get("id") or "")))
            except Exception:
                pass
        if self.knowledge_memory is not None:
            try:
                ctx = self.knowledge_memory.prompt_context(query)
                if ctx:
                    out.append(MemoryEntry(
                        kind="semantic", text=ctx[:1500], confidence=0.6,
                        provenance="knowledge_memory",
                        freshness="aging", ts=time.time()))
            except Exception:
                pass
        # Knowledge graph accepts either a live object or a lazy resolver —
        # the SQLite store must not open during construction.
        kg = self.knowledge_graph() if callable(self.knowledge_graph) else self.knowledge_graph
        if kg is not None:
            try:
                ctx = kg.context_for(query, limit=limit) if hasattr(kg, "context_for") else ""
                if ctx:
                    out.append(MemoryEntry(
                        kind="semantic", text=ctx[:1500], confidence=0.6,
                        provenance="knowledge_graph", freshness="aging",
                        ts=time.time()))
            except Exception:
                pass
        if self.locked_vault is not None:
            try:
                if getattr(self.locked_vault, "unlocked", False):
                    ctx = self.locked_vault.prompt_context(
                        project_id=project_id, conversation_id="")
                    if ctx:
                        out.append(MemoryEntry(
                            kind="semantic", text=ctx[:1500], confidence=0.9,
                            provenance="locked_vault", freshness="fresh",
                            ts=time.time()))
            except Exception:
                pass
        return out

    def _episodic(self, query: str, project_id: str, limit: int) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        qtokens = {t for t in query.lower().split() if len(t) > 2}
        sql = ("SELECT * FROM episodes WHERE invalidated=0 "
               + ("AND project_id=? " if project_id else "")
               + "ORDER BY ts DESC LIMIT 200")
        args = (project_id,) if project_id else ()
        rows = self._read(lambda c: c.execute(sql, args).fetchall())
        for r in rows:
            text = f"{r['kind']}: {r['summary']}"
            overlap = len(qtokens & set(text.lower().split()))
            conf = min(0.95, float(r["confidence"]) + 0.1 * overlap)
            out.append(MemoryEntry(
                kind="episodic", text=text, confidence=conf,
                provenance="episodes", freshness=_freshness(r["ts"]),
                ts=r["ts"], source_id=r["id"]))
        if self._activity_source is not None:
            try:
                for row in (self._activity_source(50) or []):
                    text = str(row.get("detail") or row.get("summary") or "")
                    if not text:
                        continue
                    overlap = len(qtokens & set(text.lower().split()))
                    if overlap:
                        out.append(MemoryEntry(
                            kind="episodic", text=text[:500],
                            confidence=min(0.9, 0.4 + 0.1 * overlap),
                            provenance="activity_log",
                            freshness=_freshness(row.get("ts") or 0),
                            ts=float(row.get("ts") or 0),
                            source_id=str(row.get("id") or "")))
            except Exception:
                pass
        return out

    def _procedural(self, query: str, project_id: str, limit: int) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        qtokens = {t for t in query.lower().split() if len(t) > 2}
        rows = self._read(lambda c: c.execute(
            "SELECT * FROM procedures WHERE invalidated=0 "
            "ORDER BY confidence DESC LIMIT 100").fetchall())
        for r in rows:
            hay = f"{r['name']} {r['trigger_text']}".lower()
            overlap = len(qtokens & set(hay.split()))
            if not overlap and query.lower() not in hay:
                continue
            try:
                steps = json.loads(r["steps"])
            except (ValueError, TypeError):
                steps = []
            out.append(MemoryEntry(
                kind="procedural",
                text=f"{r['name']}: {len(steps)} steps "
                     f"({r['success_count']}×ok/{r['failure_count']}×fail)",
                confidence=float(r["confidence"]),
                provenance="procedures", freshness=_freshness(r["last_used"]),
                ts=float(r["last_used"] or r["created"]),
                source_id=r["id"]))
        return out[:limit]

    def _project(self, project_id: str, limit: int) -> list[MemoryEntry]:
        out: list[MemoryEntry] = []
        rows = self._read(lambda c: c.execute(
            "SELECT * FROM project_facts WHERE project_id=? AND invalidated=0 "
            "ORDER BY confidence DESC, ts DESC LIMIT ?",
            (project_id, limit * 2)).fetchall())
        for r in rows:
            key = f"{r['category']}/{r['key']}" if r["key"] else r["category"]
            out.append(MemoryEntry(
                kind="project", text=f"{key}: {r['value']}",
                confidence=float(r["confidence"]),
                provenance=f"project:{r['provenance'] or 'local'}",
                freshness=_freshness(r["ts"]), ts=r["ts"], source_id=r["id"]))
        return out[:limit]

    # -- event handling ---------------------------------------------------------------
    def handle(self, event: CognitiveEvent) -> None:
        if event.type == EventType.MEMORY_QUERY:
            q = str(event.content.get("query") or "")
            result = self.recall(
                q, kinds=set(event.content.get("kinds") or []) or None,
                project_id=str(event.content.get("project_id") or ""),
                limit=int(event.content.get("limit") or 8))
            self.respond(event, EventType.MEMORY_RESULT, {
                "entries": [e.as_dict() for e in result.entries],
                "trusted_answer": result.trusted_answer,
                "hits": len(result.entries),
                "latency_ms": result.latency_ms,
            })
        elif event.type == EventType.LEARNING_EVENT:
            c = event.content
            kind = str(c.get("kind") or "event")
            if kind == "procedure":
                self.record_procedure(
                    str(c.get("name") or "procedure"),
                    list(c.get("steps") or []),
                    trigger_text=str(c.get("trigger_text") or ""),
                    project_id=event.mission_id or str(c.get("project_id") or ""),
                    success=bool(c.get("success", True)))
            else:
                self.record_episode(
                    kind, str(c.get("summary") or ""),
                    detail=str(c.get("detail") or ""),
                    mission_id=event.mission_id, task_id=event.task_id,
                    project_id=str(c.get("project_id") or ""))

    def status(self) -> dict[str, Any]:
        base = super().status()
        counts: dict[str, int] = {}
        conn = self._conn()
        if conn is not None:
            try:
                with self._lock:
                    for table, kind in (("episodes", "episodic"),
                                        ("procedures", "procedural"),
                                        ("project_facts", "project")):
                        try:
                            counts[kind] = conn.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE invalidated=0"
                            ).fetchone()[0]
                        except sqlite3.Error:
                            counts[kind] = -1
            finally:
                conn.close()
        base["stores"] = counts
        base["sources"] = {
            "answer_memory": self.answer_memory is not None,
            "knowledge_graph": self.knowledge_graph is not None,
            "knowledge_memory": self.knowledge_memory is not None,
            "conversation_memory": self.conversation_memory is not None,
            "locked_vault": self.locked_vault is not None,
        }
        return base
