from __future__ import annotations

import json
from pathlib import Path

from ..webtools import BrowserRunner, WebResearchClient
from .base import ToolRegistry, ToolSpec


def register_web_tools(registry: ToolRegistry, *, runtime_root: Path) -> None:
    research = WebResearchClient()
    browser = BrowserRunner(artifacts_dir=runtime_root / ".agent" / "browser")

    registry.register(ToolSpec(
        "web_search",
        "Search the public web for current documentation, errors, releases, specifications, issues, or other research. Return source URLs and titles.",
        {"type": "object", "properties": {"query": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"]},
        "network.read",
        lambda args: json.dumps(research.search(str(args["query"]), count=int(args.get("count", 8))), ensure_ascii=False),
    ))
    registry.register(ToolSpec(
        "fetch_url",
        "Read a public URL and return extracted text/JSON. Use after web_search when a source looks relevant.",
        {"type": "object", "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000}}, "required": ["url"]},
        "network.read",
        lambda args: json.dumps(research.fetch(str(args["url"]), max_chars=int(args.get("max_chars", 30000))), ensure_ascii=False),
    ))
    spec = ToolSpec(
        "browser_run",
        "Use a real local Chromium browser for websites that require JavaScript or interaction. Actions: click, type, press, wait, wait_for, goto, select, hover, evaluate, upload, scroll. Returns page text, console output, console errors, and failed network requests. Supports persistent sessions (cookies/login) via 'session'.",
        {"type": "object", "properties": {
            "url": {"type": "string"},
            "actions": {"type": "array", "items": {"type": "object"}},
            "screenshot": {"type": "boolean"},
            "session": {"type": "string", "description": "named persistent session (saves cookies/storage state)"},
            "save_session": {"type": "boolean", "default": False},
        }, "required": ["url"]},
        "browser.control",
        lambda args: json.dumps(browser.run(
            url=str(args["url"]),
            actions=list(args.get("actions") or []),
            screenshot=bool(args.get("screenshot", False)),
            session=str(args.get("session", "") or ""),
            save_session=bool(args.get("save_session", False)),
        ), ensure_ascii=False),
    )
    spec.health_check = browser.health
    registry.register(spec)
