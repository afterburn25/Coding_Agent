from __future__ import annotations

import re
from urllib.parse import urlparse

from . import evidence
from .trust import SourceTrustRegistry, TopicClassifier
from .types import ResearchSource


# Provider-supplied source_type priors — kept for local/internal sources
# which have no web domain for the trust registry to classify.
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
    def __init__(
        self,
        *,
        trusted_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        registry: SourceTrustRegistry | None = None,
    ) -> None:
        self.trusted = {d.lower().removeprefix("www.") for d in (trusted_domains or [])}
        self.blocked = {d.lower().removeprefix("www.") for d in (blocked_domains or [])}
        self.registry = registry or SourceTrustRegistry()
        self.topics_classifier = TopicClassifier()

    @staticmethod
    def _host(url: str) -> str:
        try:
            return (urlparse(url).hostname or "").lower().removeprefix("www.")
        except Exception:
            return ""

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {x.lower() for x in re.findall(r"[A-Za-z0-9_.+-]{3,}", text)}

    def score(
        self,
        source: ResearchSource,
        query: str,
        topics: list[str] | None = None,
        *,
        registry_profile=None,
        now: float | None = None,
    ) -> ResearchSource:
        host = self._host(source.url)
        comp: dict[str, float] = {}
        if host and any(host == d or host.endswith("." + d) for d in self.blocked):
            source.score = -1000
            source.reliability = evidence.REL_BLOCKED
            source.score_components = {"blocked": -1000.0}
            return source

        topics = topics or list(source.topics) or self.topics_classifier.classify(query)
        source.topics = topics

        profile = registry_profile or self.registry.profile_for(host or source.url)
        is_web = bool(host)

        if is_web:
            # Registry: base authority adjusted by topic fit (Part 4/13).
            authority, profile, topic_reasons = self.registry.topic_authority(
                host, topics)
            comp["domain_prior"] = round(authority, 2)
            if topic_reasons:
                source.metadata.setdefault("trust_notes", []).extend(topic_reasons[:2])
        else:
            authority = float(AUTHORITY_SCORES.get(
                source.authority or source.source_type, AUTHORITY_SCORES["unknown"]))
            comp["domain_prior"] = authority
        # Provider authority still counts — a provider that already knows
        # this is official_docs shouldn't be flattened to domain-only.
        provider_prior = float(AUTHORITY_SCORES.get(
            source.authority or source.source_type, AUTHORITY_SCORES["unknown"]))
        comp["authority"] = round(authority * 0.7 + provider_prior * 0.3, 2)

        comp["version"] = float(VERSION_SCORES.get(source.version_relevance, 0))

        qterms = self._terms(query)
        sterms = self._terms(f"{source.title} {source.excerpt[:3000]}")
        relevance = len(qterms & sterms) / max(1, len(qterms)) if qterms else 0.0
        comp["relevance"] = round(min(30.0, relevance * 30.0), 2)

        if is_web:
            comp["freshness"] = round(
                evidence.freshness_score(
                    source.published_at, source.retrieved_at, topics, now=now)
                * 15.0 - 7.5, 2)
            comp["primary_source"] = (
                12.0 if getattr(profile, "primary_source", False) else 0.0)
            comp["evidence_density"] = round(
                evidence.evidence_density(source.excerpt) * 12.0, 2)
            comp["content_farm_penalty"] = round(
                -evidence.content_farm_penalty(source) * 30.0, 2)
            comp["duplication_penalty"] = (
                -25.0 if source.metadata.get("duplicate_of") else 0.0)
        else:
            comp["primary_source"] = 8.0 if source.authority in {
                "local_repository", "installed_metadata", "official_docs"} else 0.0

        if host and any(host == d or host.endswith("." + d) for d in self.trusted):
            comp["user_trusted"] = 15.0

        score = sum(comp.values())
        source.score = round(score, 2)
        source.source_class = getattr(profile, "category", "")
        source.score_components = comp
        source.reliability = evidence.reliability_label(source, profile)
        source.badges = evidence.source_badges(source, profile, topics)
        return source

    def rank(
        self,
        sources: list[ResearchSource],
        query: str,
        topics: list[str] | None = None,
    ) -> list[ResearchSource]:
        topics = topics or self.topics_classifier.classify(query)
        profiles = {}
        for s in sources:
            profiles[s.id] = self.registry.profile_for(self._host(s.url) or s.url)
        rows = [self.score(s, query, topics, registry_profile=profiles[s.id])
                for s in sources]
        rows.sort(key=lambda s: (-s.score, s.title.lower(), s.url))
        return rows

    @staticmethod
    def diversify(
        sources: list[ResearchSource],
        *,
        max_per_domain: int = 2,
        limit: int = 12,
    ) -> list[ResearchSource]:
        """Source diversity (Part 39): cap same-domain picks so one site
        can't flood the evidence set; duplicates already score badly."""
        counts: dict[str, int] = {}
        out: list[ResearchSource] = []
        for s in sources:
            host = SourceRanker._host(s.url) or f"local:{s.provider}"
            if counts.get(host, 0) >= max_per_domain:
                continue
            counts[host] = counts.get(host, 0) + 1
            out.append(s)
            if len(out) >= limit:
                break
        return out
