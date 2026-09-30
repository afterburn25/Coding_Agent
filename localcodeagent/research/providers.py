from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..webtools.research import WebResearchClient
from ..workflow.repository import RepositoryIndex
from .environment import EnvironmentInspector
from .official import host_for, looks_official
from .types import ResearchSource


DOC_EXTENSIONS = {".md", ".txt", ".rst", ".html", ".htm", ".json", ".toml", ".yaml", ".yml"}


class ResearchProvider(ABC):
    name = "provider"
    network = False

    @abstractmethod
    def search(self, query: str, *, limit: int = 8, version: str = "") -> list[ResearchSource]:
        raise NotImplementedError


class RepositoryResearchProvider(ResearchProvider):
    name = "repository"

    def __init__(self, index: RepositoryIndex) -> None:
        self.index = index

    def search(self, query: str, *, limit: int = 8, version: str = "") -> list[ResearchSource]:
        self.index.ensure()
        return [
            ResearchSource(
                title=row["path"], source_type="local_repository", authority="local_repository",
                excerpt=f"Symbols: {', '.join(row.get('symbols', [])[:30])}\n{row.get('preview', '')}",
                provider=self.name, version_relevance="exact" if version else "unknown",
                metadata={"path": row["path"], "score": row.get("score", 0)},
            )
            for row in self.index.search(query, limit=limit)
        ]


class LocalDocumentationProvider(ResearchProvider):
    name = "local_docs"

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def search(self, query: str, *, limit: int = 8, version: str = "") -> list[ResearchSource]:
        terms = [x.lower() for x in re.findall(r"[A-Za-z0-9_.+-]{3,}", query)][:12]
        scored: list[tuple[int, ResearchSource]] = []
        for path in self.workspace.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in DOC_EXTENSIONS:
                continue
            rel = path.relative_to(self.workspace)
            if any(part in {".git", ".agent", "node_modules", ".venv", "venv", "__pycache__"} for part in rel.parts):
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lowered = f"{rel.as_posix()}\n{text}".lower()
            score = sum(8 if term in rel.as_posix().lower() else 1 for term in terms if term in lowered)
            if not score:
                continue
            first = min((lowered.find(t) for t in terms if lowered.find(t) >= 0), default=0)
            start = max(0, first - 800)
            excerpt = text[start:start + 5000]
            scored.append((score, ResearchSource(
                title=rel.as_posix(), source_type="local_docs", authority="local_docs", excerpt=excerpt,
                provider=self.name, version_relevance="exact" if version else "unknown", metadata={"path": rel.as_posix()},
            )))
        scored.sort(key=lambda item: (-item[0], item[1].title))
        return [source for _, source in scored[:max(1, limit)]]


class PackageMetadataProvider(ResearchProvider):
    name = "package_metadata"

    def __init__(self, inspector: EnvironmentInspector) -> None:
        self.inspector = inspector

    def search(self, query: str, *, limit: int = 8, version: str = "") -> list[ResearchSource]:
        matches = self.inspector.find_package(query)
        return [ResearchSource(
            title=f"Installed {row['ecosystem']} package: {row['name']}", source_type="installed_metadata",
            authority="installed_metadata", excerpt=json.dumps(row, ensure_ascii=False), provider=self.name,
            software_version=row.get("version", ""), version_relevance="exact", metadata=row,
        ) for row in matches[:limit]]


class WebSearchProvider(ResearchProvider):
    name = "web"
    network = True

    def __init__(self, client: WebResearchClient | None = None) -> None:
        self.client = client or WebResearchClient()

    def search(self, query: str, *, limit: int = 8, version: str = "") -> list[ResearchSource]:
        rows = self.client.search(query, count=limit)
        result: list[ResearchSource] = []
        for row in rows:
            url = str(row.get("url", ""))
            authority = "official_docs" if looks_official(url, query) else ("official_repo" if host_for(url) == "github.com" else "web")
            result.append(ResearchSource(
                title=str(row.get("title", url)), url=url, source_type="web_search", authority=authority,
                provider=self.name, software_version=version, version_relevance="unknown",
            ))
        return result

    def fetch(self, source: ResearchSource, *, max_chars: int = 20000) -> ResearchSource:
        if not source.url:
            return source
        payload = self.client.fetch(source.url, max_chars=max_chars)
        source.excerpt = str(payload.get("text") or "")
        source.metadata.update({"content_type": payload.get("content_type"), "truncated": payload.get("truncated", False)})
        return source


class GitHubResearchProvider(WebSearchProvider):
    name = "github"

    def search(self, query: str, *, limit: int = 8, version: str = "") -> list[ResearchSource]:
        rows = super().search(f"site:github.com {query}", limit=limit, version=version)
        for source in rows:
            source.provider = self.name
            # A github.com result can be a fork or unrelated repository. Treat generic
            # GitHub evidence conservatively until the coordinator has identified a
            # known upstream repository/domain for the dependency.
            source.authority = "upstream_issue" if "/issues/" in source.url or "/discussions/" in source.url else "technical_docs"
            source.source_type = "github"
        return rows
