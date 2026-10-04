"""Local knowledge index — dependency-free keyword RAG over the workspace.

`knowledge_index` chunks indexable files (docs/code/data) into overlapping
blocks and stores token frequencies in a JSON index. `knowledge_search`
scores chunks with TF-IDF-style weighting plus filename/phrase bonuses.
No vector DB required — the index is a plain JSON document that survives
restarts and can be rebuilt incrementally (mtime-tracked).
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from pathlib import Path

from ..fsutil import atomic_write_text
from typing import Any

from .base import ToolRegistry, ToolSpec

INDEXABLE_SUFFIXES = {
    ".md", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".html", ".css", ".sql",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".java", ".go", ".rs", ".sh",
    ".ps1", ".bat", ".csv",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", ".agent", ".idea", ".vscode"}
MAX_FILE_BYTES = 512 * 1024
CHUNK_CHARS = 1400
CHUNK_OVERLAP = 200
MAX_FILES = 4000

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def _chunk_text(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text] if text.strip() else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        piece = text[start:end]
        if piece.strip():
            chunks.append(piece)
        start = end - CHUNK_OVERLAP if end < len(text) else len(text)
    return chunks


class KnowledgeIndex:
    """Workspace keyword index persisted as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = {k: v for k, v in raw.get("files", {}).items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.entries = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated_at": time.time(), "files": self.entries}
        atomic_write_text(self.path, json.dumps(payload))

    def index_workspace(self, workspace: Path, *, subpath: str = "") -> dict[str, Any]:
        root = (workspace / subpath).resolve()
        if not root.is_relative_to(workspace) or not root.exists():
            raise ValueError("index path must stay inside the workspace")
        scanned = indexed = skipped = 0
        files = [root] if root.is_file() else sorted(
            p for p in root.rglob("*")
            if p.is_file()
            and p.suffix.lower() in INDEXABLE_SUFFIXES
            and not (set(p.parts) & SKIP_DIRS)
        )
        for path in files[:MAX_FILES]:
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            rel = str(path.relative_to(workspace)).replace("\\", "/")
            scanned += 1
            existing = self.entries.get(rel)
            if existing and existing.get("mtime") == stat.st_mtime and existing.get("size") == stat.st_size:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped += 1
                continue
            chunks = [{"index": i, "text": c, "tokens": dict(Counter(_tokens(c)))}
                      for i, c in enumerate(_chunk_text(text))]
            self.entries[rel] = {"mtime": stat.st_mtime, "size": stat.st_size, "chunks": chunks}
            indexed += 1
        self.save()
        return {"files_scanned": scanned, "files_indexed": indexed, "files_skipped": skipped,
                "total_indexed": len(self.entries), "index_file": str(self.path)}

    def index_document(self, doc_id: str, text: str, *, title: str = "",
                       project: str = "", shared_with: list[str] | None = None,
                       origin: str = "", checksum: str = "") -> dict[str, Any]:
        """Index a managed library document under ``lib:<doc_id>``.

        Unlike workspace entries these carry project ownership, an origin
        path, and a checksum so the library can detect source drift.
        """
        key = f"lib:{doc_id}"
        chunks = [{"index": i, "text": c, "tokens": dict(Counter(_tokens(c)))}
                  for i, c in enumerate(_chunk_text(text))]
        self.entries[key] = {
            "kind": "library", "doc_id": doc_id,
            "title": str(title or doc_id)[:160],
            "project": str(project or ""),
            "shared_with": [str(s) for s in (shared_with or [])][:20],
            "origin": str(origin or "")[:500],
            "checksum": checksum,
            "indexed_at": time.time(),
            "chunks": chunks}
        self.save()
        return {"doc_id": doc_id, "chunks": len(chunks)}

    def mark_stale(self, doc_id: str, *, stale: bool = True) -> None:
        """A stale library doc keeps its record (provenance) but its chunks
        stop matching searches — stale knowledge is never silently served."""
        key = f"lib:{doc_id}"
        entry = self.entries.get(key)
        if entry is None:
            return
        entry["stale"] = bool(stale)
        if stale:
            entry["chunks"] = []
        self.save()

    def forget(self, rel: str) -> int:
        removed = 1 if self.entries.pop(rel, None) is not None else 0
        if removed:
            self.save()
        return removed

    def search(self, query: str, *, limit: int = 8,
               project: str | None = None) -> list[dict[str, Any]]:
        """Ranked excerpt search. ``project`` scopes library docs to the
        project + global; workspace (repo) chunks are always in scope.
        Stale library docs are skipped."""
        q_tokens = _tokens(query)
        if not q_tokens:
            return []
        q_counts = Counter(q_tokens)
        entries = {k: v for k, v in self.entries.items()
                   if not v.get("stale")}
        if project is not None:
            entries = {
                k: v for k, v in entries.items()
                if v.get("kind") != "library"
                or not v.get("project")
                or v.get("project") == project
                or project in (v.get("shared_with") or [])}
        n_docs = max(len(entries), 1)
        # document frequency per term (file-level)
        df: Counter[str] = Counter()
        for entry in entries.values():
            seen = set()
            for chunk in entry.get("chunks", []):
                seen.update(chunk.get("tokens", {}))
            df.update(seen)

        scored: list[tuple[float, str, dict[str, Any]]] = []
        for rel, entry in entries.items():
            name_tokens = set(_tokens(rel)) | set(
                _tokens(str(entry.get("title", ""))))
            name_bonus = sum(2.0 for t in q_tokens if t in name_tokens)
            for chunk in entry.get("chunks", []):
                tf: dict[str, int] = chunk.get("tokens", {})
                score = name_bonus
                for term, qc in q_counts.items():
                    freq = tf.get(term, 0)
                    if freq:
                        idf = math.log(1 + n_docs / (1 + df.get(term, 0)))
                        score += (1 + math.log(freq)) * idf * min(qc, 2)
                if score > 0:
                    text = chunk.get("text", "")
                    if query.lower() in text.lower():
                        score += 5.0  # exact phrase bonus
                    scored.append((score, rel, chunk, entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [{"file": rel, "chunk": chunk["index"],
                 "score": round(score, 3),
                 "doc": entry.get("doc_id"), "title": entry.get("title"),
                 "origin": entry.get("origin"),
                 "project": entry.get("project"),
                 "indexed_at": entry.get("indexed_at"),
                 "excerpt": chunk["text"][:600]}
                for score, rel, chunk, entry in scored[:limit]]

    def stats(self) -> dict[str, Any]:
        chunks = sum(len(e.get("chunks", [])) for e in self.entries.values())
        return {"files": len(self.entries), "chunks": chunks, "index_file": str(self.path)}


def register_knowledge_tools(registry: ToolRegistry, workspace: Path, *, index_path: Path | None = None) -> KnowledgeIndex:
    index = KnowledgeIndex(index_path or workspace / ".agent" / "knowledge_index.json")

    def knowledge_index(args: dict[str, Any]) -> str:
        try:
            return json.dumps(index.index_workspace(workspace, subpath=str(args.get("path", ""))), ensure_ascii=False)
        except ValueError as exc:
            return f"ERROR: {exc}"

    def knowledge_search(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "ERROR: 'query' is required"
        limit = max(1, min(int(args.get("limit", 8)), 20))
        project = str(args.get("project", ""))
        results = index.search(query, limit=limit,
                               project=project or None)
        if not results:
            return "ERROR: no matches — run knowledge_index first to build the index"
        return json.dumps({"query": query, "results": results, "index": index.stats()}, ensure_ascii=False)

    def knowledge_forget(args: dict[str, Any]) -> str:
        rel = str(args.get("file", "")).replace("\\", "/")
        if index.forget(rel):
            return json.dumps({"removed": rel})
        return f"ERROR: '{rel}' is not in the index"

    registry.register(ToolSpec(
        "knowledge_index",
        "Build/refresh the local knowledge index over workspace documents and code. Incremental (mtime-tracked); indexes md/txt/code/data files into .agent/knowledge_index.json.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "optional subpath to index", "default": ""}},
        },
        "filesystem.read", knowledge_index,
        category="research",
        capabilities=["index_knowledge", "build_rag_index", "embed_documents"],
    ))
    registry.register(ToolSpec(
        "knowledge_search",
        "Search the local knowledge index (keyword TF-IDF ranking). Returns ranked excerpts with file paths — use for local RAG over docs/code without a vector DB.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 8},
                "project": {"type": "string",
                            "description": "scope library docs to this project + global"},
            },
            "required": ["query"],
        },
        "filesystem.read", knowledge_search,
        category="research",
        capabilities=["search_knowledge", "local_rag", "recall_documents"],
    ))
    registry.register(ToolSpec(
        "knowledge_forget",
        "Remove a file from the local knowledge index.",
        {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]},
        "filesystem.write", knowledge_forget,
        category="research",
        capabilities=["forget_knowledge", "manage_index"],
    ))
    return index
