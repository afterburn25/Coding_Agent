from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any


CURRENT_SIGNALS = (
    "latest", "current", "today", "this week", "this month", "right now",
    "price", "weather", "news", "release", "version", "available now",
)


class KnowledgeMemory:
    """Local sourced knowledge learned from research.

    Records are deliberately provenance-bearing and expire. Web-derived knowledge is
    never treated as a hidden permanent fact without its sources/freshness metadata.
    """

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        default_ttl_hours: int = 720,
        current_ttl_hours: int = 12,
        max_records: int = 1000,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enabled = bool(enabled)
        self.default_ttl = max(1, int(default_ttl_hours)) * 3600
        self.current_ttl = max(1, int(current_ttl_hours)) * 3600
        self.max_records = max(50, int(max_records))
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "records": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("records"), list):
                self._data.update(raw)
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        if not self.enabled:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _normalize(text: str) -> str:
        text = re.sub(r"\s+", " ", str(text or "").strip().lower())
        return re.sub(r"[^a-z0-9_+.# -]", "", text)

    @classmethod
    def is_current_sensitive(cls, query: str) -> bool:
        q = cls._normalize(query)
        return any(signal in q for signal in CURRENT_SIGNALS)

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {
            t for t in re.findall(r"[a-z0-9_+.#-]{2,}", text.lower())
            if t not in {"the", "and", "for", "with", "that", "this", "what", "how", "are", "was"}
        }

    def remember_research(
        self,
        query: str,
        answer: str,
        sources: list[dict[str, Any]],
        *,
        current_sensitive: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        clean_query = str(query or "").strip()[:4000]
        clean_answer = str(answer or "").strip()[:16000]
        if not clean_query or not clean_answer:
            return None

        now = time.time()
        sensitive = self.is_current_sensitive(clean_query) if current_sensitive is None else bool(current_sensitive)
        ttl = self.current_ttl if sensitive else self.default_ttl
        source_rows = []
        for source in sources[:20]:
            if not isinstance(source, dict):
                continue
            source_rows.append({
                "id": str(source.get("id", ""))[:80],
                "title": str(source.get("title", ""))[:500],
                "url": str(source.get("url", ""))[:2000],
                "provider": str(source.get("provider", ""))[:80],
                "reliability": str(source.get("reliability", ""))[:80],
                "retrieved_at": float(source.get("retrieved_at") or now),
            })

        record = {
            "id": uuid.uuid4().hex[:12],
            "query": clean_query,
            "normalized_query": self._normalize(clean_query),
            "answer": clean_answer,
            "sources": source_rows,
            "learned_at": now,
            "expires_at": now + ttl,
            "current_sensitive": sensitive,
            "metadata": dict(metadata or {}),
            "use_count": 0,
            "last_used_at": 0.0,
        }

        with self._lock:
            records = self._data.setdefault("records", [])
            normalized = record["normalized_query"]
            # Replace an older answer to the same normalized question rather than
            # accumulating contradictory versions indefinitely.
            records = [
                row for row in records
                if str(row.get("normalized_query", "")) != normalized
            ]
            records.append(record)
            self._data["records"] = records[-self.max_records:]
            self._save()
        return dict(record)

    def lookup(self, query: str, *, allow_expired: bool = False) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        q_norm = self._normalize(query)
        q_terms = self._terms(q_norm)
        now = time.time()
        best: tuple[float, dict[str, Any]] | None = None
        with self._lock:
            for row in self._data.get("records", []):
                if not isinstance(row, dict):
                    continue
                expired = float(row.get("expires_at", 0)) <= now
                if expired and not allow_expired:
                    continue
                candidate = str(row.get("normalized_query", ""))
                score = 0.0
                if candidate == q_norm:
                    score = 100.0
                elif q_norm and (q_norm in candidate or candidate in q_norm):
                    score = 70.0
                else:
                    c_terms = self._terms(candidate)
                    if q_terms and c_terms:
                        overlap = len(q_terms & c_terms)
                        union = len(q_terms | c_terms)
                        score = (overlap / max(1, union)) * 50.0
                if score < 12.0:
                    continue
                if best is None or score > best[0]:
                    best = (score, row)

            if best is None:
                return None
            row = best[1]
            row["use_count"] = int(row.get("use_count", 0)) + 1
            row["last_used_at"] = now
            self._save()
            result = dict(row)
            result["match_score"] = best[0]
            result["expired"] = float(row.get("expires_at", 0)) <= now
            return result

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q_terms = self._terms(self._normalize(query))
        now = time.time()
        hits: list[tuple[float, dict[str, Any]]] = []
        with self._lock:
            for row in self._data.get("records", []):
                if not isinstance(row, dict):
                    continue
                candidate = self._terms(str(row.get("normalized_query", "")) + " " + str(row.get("answer", "")))
                overlap = len(q_terms & candidate)
                if not overlap:
                    continue
                score = overlap / max(1, len(q_terms))
                copy = dict(row)
                copy["expired"] = float(row.get("expires_at", 0)) <= now
                hits.append((score, copy))
        hits.sort(key=lambda x: (x[0], float(x[1].get("learned_at", 0))), reverse=True)
        return [row for _score, row in hits[:max(1, int(limit))]]

    def prompt_context(self, query: str) -> str:
        row = self.lookup(query)
        if not row:
            return ""
        sources = [s for s in row.get("sources", []) if s.get("url")]
        lines = [
            "Previously researched sourced knowledge (refresh if the user asks for current/latest information):",
            f"Question: {row.get('query', '')}",
            f"Learned answer: {row.get('answer', '')}",
        ]
        if sources:
            lines.append("Sources:")
            lines.extend(f"- {s.get('title')}: {s.get('url')}" for s in sources[:8])
        return "\n".join(lines)[:20000]

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            records = list(self._data.get("records", []))
        return {
            "enabled": self.enabled,
            "path": str(self.path),
            "records": len(records),
            "fresh_records": sum(1 for row in records if float(row.get("expires_at", 0)) > now),
            "expired_records": sum(1 for row in records if float(row.get("expires_at", 0)) <= now),
            "recent": sorted(records, key=lambda x: float(x.get("learned_at", 0)), reverse=True)[:20],
        }
