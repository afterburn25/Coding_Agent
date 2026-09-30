from __future__ import annotations

import re
from urllib.parse import urlparse

from .types import ResearchSource


AUTHORITY_SCORES = {
    "local_repository": 100,
    "local_docs": 96,
    "installed_metadata": 94,
    "official_docs": 92,
    "official_repo": 90,
    "official_example": 88,
    "release_notes": 87,
    "standard": 86,
    "upstream_issue": 76,
    "technical_docs": 68,
    "community": 54,
    "tutorial": 42,
    "web": 32,
    "unknown": 25,
}
VERSION_SCORES = {"exact": 25, "compatible": 15, "unknown": 0, "mismatch": -40, "conflict": -30}


class SourceRanker:
    def __init__(self, *, trusted_domains: list[str] | None = None, blocked_domains: list[str] | None = None) -> None:
        self.trusted = {d.lower().removeprefix("www.") for d in (trusted_domains or [])}
        self.blocked = {d.lower().removeprefix("www.") for d in (blocked_domains or [])}

    @staticmethod
    def _host(url: str) -> str:
        try:
            return (urlparse(url).hostname or "").lower().removeprefix("www.")
        except Exception:
            return ""

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {x.lower() for x in re.findall(r"[A-Za-z0-9_.+-]{3,}", text)}

    def score(self, source: ResearchSource, query: str) -> ResearchSource:
        host = self._host(source.url)
        if host and any(host == d or host.endswith("." + d) for d in self.blocked):
            source.score = -1000
            source.reliability = "Blocked"
            return source
        score = float(AUTHORITY_SCORES.get(source.authority or source.source_type, AUTHORITY_SCORES["unknown"]))
        score += VERSION_SCORES.get(source.version_relevance, 0)
        qterms = self._terms(query)
        sterms = self._terms(f"{source.title} {source.excerpt[:3000]}")
        if qterms:
            overlap = len(qterms & sterms) / max(1, len(qterms))
            score += min(25.0, overlap * 25.0)
        if host and any(host == d or host.endswith("." + d) for d in self.trusted):
            score += 15
        source.score = round(score, 2)
        if score >= 125:
            source.reliability = "Confirmed"
        elif score >= 100:
            source.reliability = "Strong"
        elif score >= 78:
            source.reliability = "Probable"
        else:
            source.reliability = "Uncertain"
        return source

    def rank(self, sources: list[ResearchSource], query: str) -> list[ResearchSource]:
        rows = [self.score(s, query) for s in sources]
        rows.sort(key=lambda s: (-s.score, s.title.lower(), s.url))
        return rows
