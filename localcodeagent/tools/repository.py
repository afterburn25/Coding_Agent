from __future__ import annotations

import json

from .base import ToolRegistry, ToolSpec
from ..workflow.repository import RepositoryIndex


def register_repository_tools(registry: ToolRegistry, index: RepositoryIndex) -> None:
    def search_repo_index(args: dict) -> str:
        query = str(args["query"])
        limit = max(1, min(int(args.get("limit", 20)), 100))
        rows = index.search(query, limit=limit)
        if not rows:
            return "NO_INDEX_MATCHES"
        return "\n".join(
            f"{row['path']} score={row['score']} symbols={','.join(row.get('symbols', [])[:12])} preview={row.get('preview','')[:240]}"
            for row in rows
        )

    def rebuild_repo_index(_: dict) -> str:
        return json.dumps(index.build())

    registry.register(ToolSpec("search_repo_index", "Search the lightweight repository index by path, symbols, and file preview.", {
        "type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]
    }, "filesystem.read", search_repo_index))
    registry.register(ToolSpec("rebuild_repo_index", "Rebuild the local repository index after substantial file changes.", {
        "type": "object", "properties": {}
    }, "filesystem.read", rebuild_repo_index))
