from __future__ import annotations

import json

from ..research import ResearchCoordinator
from .base import ToolRegistry, ToolSpec


def register_research_tools(registry: ToolRegistry, research: ResearchCoordinator) -> None:
    registry.register(ToolSpec(
        "research_topic",
        "Research a technical topic repository-first, then authoritative documentation/GitHub/community sources as allowed. Returns ranked sources with provenance and a concise synthesis.",
        {"type": "object", "properties": {"query": {"type": "string"}, "mode": {"type": "string", "enum": ["auto", "local_only", "official", "balanced", "deep", "offline"]}, "version": {"type": "string"}}, "required": ["query"]},
        "network.read",
        lambda args: json.dumps(research.research_topic(str(args["query"]), mode=str(args.get("mode", "auto")), version=str(args.get("version", ""))), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "search_documentation",
        "Search local and official documentation for an API/library. Prefer version-specific authoritative material.",
        {"type": "object", "properties": {"query": {"type": "string"}, "domain": {"type": "string"}, "version": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"]},
        "network.read",
        lambda args: json.dumps(research.search_documentation(str(args["query"]), domain=str(args.get("domain", "")), version=str(args.get("version", "")), limit=int(args.get("limit", 8))), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "search_repository",
        "Research the current repository index first for an existing implementation, convention, config, test, or related module.",
        {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "required": ["query"]},
        "filesystem.read",
        lambda args: json.dumps([s.as_dict() for s in research.repository.search(str(args["query"]), limit=int(args.get("limit", 12)))], ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "search_github",
        "Search GitHub/upstream source, issues, discussions, examples, releases, and changelogs for technical evidence.",
        {"type": "object", "properties": {"query": {"type": "string"}, "repo": {"type": "string", "description": "Optional owner/name upstream repository."}, "kind": {"type": "string", "enum": ["auto", "issues", "prs", "repositories", "code", "releases", "release_notes"]}, "version": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"]},
        "network.read",
        lambda args: json.dumps(research.search_github(str(args["query"]), version=str(args.get("version", "")), limit=int(args.get("limit", 8)), repo=str(args.get("repo", "")), kind=str(args.get("kind", "auto"))), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "search_errors",
        "Research an exact build/runtime error against local project evidence, authoritative docs, and upstream issues. Do not apply the first online fix blindly.",
        {"type": "object", "properties": {"error": {"type": "string"}, "version": {"type": "string"}}, "required": ["error"]},
        "network.read",
        lambda args: json.dumps(research.search_errors(str(args["error"]), version=str(args.get("version", ""))), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "lookup_api",
        "Look up a version-aware API/method/configuration option in local and authoritative documentation.",
        {"type": "object", "properties": {"query": {"type": "string"}, "version": {"type": "string"}}, "required": ["query"]},
        "network.read",
        lambda args: json.dumps(research.search_documentation(str(args["query"]), version=str(args.get("version", "")), limit=10), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "read_release_notes",
        "Find authoritative release notes/changelogs for a library or tool, with version relevance recorded.",
        {"type": "object", "properties": {"query": {"type": "string"}, "version": {"type": "string"}}, "required": ["query"]},
        "network.read",
        lambda args: json.dumps(research.search_documentation(f"{args['query']} release notes changelog", version=str(args.get("version", "")), limit=10), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "check_package_version",
        "Inspect project manifests for the installed/configured version of a dependency without executing downloaded code.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        "filesystem.read",
        lambda args: json.dumps(research.check_package_version(str(args["name"])), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "summarize_research",
        "Return cached research sessions and structured evidence so the coding model can synthesize implementation decisions without repeating searches.",
        {"type": "object", "properties": {"query": {"type": "string"}}},
        "filesystem.read",
        lambda args: json.dumps(research.cached(str(args.get("query", ""))), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "get_cached_research",
        "Retrieve previously cached project research by topic.",
        {"type": "object", "properties": {"query": {"type": "string"}}},
        "filesystem.read",
        lambda args: json.dumps(research.cached(str(args.get("query", ""))), ensure_ascii=False),
    ))
