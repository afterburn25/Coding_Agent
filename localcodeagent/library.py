"""Managed Knowledge Library — documents Nexus owns, not just files she
can see.

The library imports documents (pdf/md/txt/html/csv/code and anything
``extract_document_text`` handles) into a durable catalog with checksums
and origin paths. Entries index into the shared ``KnowledgeIndex`` under
``lib:<doc_id>`` with project ownership — searches can scope to a project
plus global, and a changed source re-extracts while a vanished one is
marked stale instead of silently serving dead chunks.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text
from .tools.documents import extract_document_text
from .tools.knowledge import KnowledgeIndex

MAX_DOC_TEXT = 400_000


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class KnowledgeLibrary:
    """Catalog + ingestion pipeline over the shared KnowledgeIndex."""

    def __init__(self, data_dir: Path, index: KnowledgeIndex) -> None:
        self.root = Path(data_dir) / "library"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = index
        self._path = self.root / "docs.json"
        self._lock = threading.RLock()
        self._docs: dict[str, dict[str, Any]] = {}
        self._load()

    # -- persistence -----------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._docs = {d["id"]: d for d in raw.get("docs", [])
                          if isinstance(d, dict) and d.get("id")}
        except (OSError, ValueError):
            self._docs = {}

    def _save(self) -> None:
        atomic_write_text(self._path, json.dumps(
            {"version": 1, "docs": list(self._docs.values())}, indent=2))

    # -- ingestion ---------------------------------------------------------

    def import_document(self, path: Path | str, *, project_id: str = "",
                        shared_with: list[str] | None = None,
                        title: str = "") -> dict[str, Any]:
        """Import an external document: extract → checksum → catalog →
        index. The source path is user-supplied (explicit import), so it
        may live outside the workspace."""
        src = Path(path).expanduser().resolve()
        if not src.is_file():
            return {"ok": False, "error": f"file not found: {src}"}
        extracted = extract_document_text(src, max_chars=MAX_DOC_TEXT)
        text = str(extracted.get("text") or "")
        if not text.strip():
            return {"ok": False, "error": "no extractable text"}
        checksum = _sha256(src)
        stat = src.stat()
        # Re-import of the same origin updates the existing doc in place.
        existing = next((d for d in self._docs.values()
                         if d.get("origin") == str(src)), None)
        doc_id = existing["id"] if existing else f"doc-{uuid.uuid4().hex[:10]}"
        doc = {
            "id": doc_id,
            "title": str(title or (existing or {}).get("title")
                         or src.name)[:160],
            "origin": str(src),
            "project": str(project_id or ""),
            "shared_with": [str(s) for s in (shared_with or [])][:20],
            "kind": extracted.get("kind", ""),
            "checksum": checksum,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "stale": False,
            "imported_at": (existing or {}).get("imported_at")
                           or time.time(),
            "indexed_at": time.time(),
        }
        self.index.index_document(
            doc_id, text, title=doc["title"], project=doc["project"],
            shared_with=doc["shared_with"], origin=doc["origin"],
            checksum=checksum)
        with self._lock:
            self._docs[doc_id] = doc
            self._save()
        return {"ok": True, "doc": doc,
                "chunks": len(self.index.entries.get(
                    f"lib:{doc_id}", {}).get("chunks", []))}

    # -- refresh / staleness ------------------------------------------------

    def refresh(self) -> dict[str, Any]:
        """Re-check every document's origin: changed → re-extract +
        reindex; missing → stale (chunks stop matching but the record and
        provenance stay)."""
        stats = {"checked": 0, "reindexed": 0, "stale": 0,
                 "unchanged": 0, "errors": 0}
        for doc_id, doc in list(self._docs.items()):
            stats["checked"] += 1
            origin = Path(str(doc.get("origin") or ""))
            if not origin.is_file():
                if not doc.get("stale"):
                    doc["stale"] = True
                    self.index.mark_stale(doc_id)
                stats["stale"] += 1
                continue
            stat = origin.stat()
            if (stat.st_mtime == doc.get("mtime")
                    and stat.st_size == doc.get("size")
                    and not doc.get("stale")):
                stats["unchanged"] += 1
                continue
            try:
                extracted = extract_document_text(
                    origin, max_chars=MAX_DOC_TEXT)
                checksum = _sha256(origin)
                doc.update({"checksum": checksum, "size": stat.st_size,
                            "mtime": stat.st_mtime, "stale": False,
                            "indexed_at": time.time()})
                self.index.index_document(
                    doc_id, str(extracted.get("text") or ""),
                    title=doc.get("title", ""), project=doc.get("project", ""),
                    shared_with=doc.get("shared_with"),
                    origin=doc.get("origin"), checksum=checksum)
                stats["reindexed"] += 1
            except Exception:
                doc["stale"] = True
                self.index.mark_stale(doc_id)
                stats["errors"] += 1
        self._save()
        return stats

    # -- queries -------------------------------------------------------------

    def list(self, *, project: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            docs = [dict(d) for d in self._docs.values()]
        if project is not None:
            docs = [d for d in docs
                    if not d.get("project") or d.get("project") == project
                    or project in (d.get("shared_with") or [])]
        return sorted(docs, key=lambda d: d.get("indexed_at") or 0,
                      reverse=True)

    def get(self, doc_id: str) -> dict | None:
        with self._lock:
            d = self._docs.get(doc_id)
            return dict(d) if d else None

    def remove(self, doc_id: str) -> bool:
        with self._lock:
            doc = self._docs.pop(doc_id, None)
            if doc is None:
                return False
            self._save()
        self.index.forget(f"lib:{doc_id}")
        return True

    def set_project(self, doc_id: str, project_id: str,
                    *, shared_with: list[str] | None = None) -> bool:
        """Reassign project ownership — rescopes search visibility."""
        with self._lock:
            doc = self._docs.get(doc_id)
            if doc is None:
                return False
            doc["project"] = str(project_id or "")
            if shared_with is not None:
                doc["shared_with"] = [str(s) for s in shared_with][:20]
            self._save()
        entry = self.index.entries.get(f"lib:{doc_id}")
        if entry is not None:
            entry["project"] = doc["project"]
            entry["shared_with"] = doc.get("shared_with", [])
            self.index.save()
        return True

    def stats(self) -> dict[str, Any]:
        with self._lock:
            docs = list(self._docs.values())
        return {"documents": len(docs),
                "stale": sum(1 for d in docs if d.get("stale")),
                "projects": sorted({d.get("project") or ""
                                    for d in docs} - {""})}
