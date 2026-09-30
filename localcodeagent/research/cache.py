from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class ResearchCache:
    def __init__(self, root: Path, *, ttl_hours: int = 168) -> None:
        self.root = root.resolve()
        self.cache_dir = self.root / "cache"
        self.sessions_dir = self.root / "sessions"
        self.sources_dir = self.root / "sources"
        self.summaries_dir = self.root / "summaries"
        for p in (self.cache_dir, self.sessions_dir, self.sources_dir, self.summaries_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.ttl = max(1, ttl_hours) * 3600

    @staticmethod
    def key(provider: str, query: str, version: str = "") -> str:
        raw = f"{provider}\0{query.strip().lower()}\0{version.strip().lower()}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, provider: str, query: str, version: str = "") -> Any | None:
        path = self.cache_dir / f"{self.key(provider, query, version)}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(data.get("cached_at", 0)) > self.ttl:
                return None
            return data.get("value")
        except Exception:
            return None

    def put(self, provider: str, query: str, value: Any, version: str = "") -> None:
        path = self.cache_dir / f"{self.key(provider, query, version)}.json"
        payload = {"cached_at": time.time(), "provider": provider, "query": query, "version": version, "value": value}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def save_session(self, session: dict[str, Any]) -> None:
        sid = str(session.get("id") or "session")
        path = self.sessions_dir / f"{sid}.json"
        path.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")

    def recent_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, limit)]:
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return rows

    def stats(self) -> dict[str, Any]:
        return {
            "cache_entries": len(list(self.cache_dir.glob("*.json"))),
            "sessions": len(list(self.sessions_dir.glob("*.json"))),
            "root": str(self.root),
            "ttl_hours": round(self.ttl / 3600),
        }
