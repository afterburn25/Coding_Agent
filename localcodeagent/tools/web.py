from __future__ import annotations

import json
from pathlib import Path

from ..webtools import BrowserRunner, WebResearchClient
from .base import ToolRegistry, ToolSpec


def register_web_tools(registry: ToolRegistry, *, runtime_root: Path,
                       artifacts=None) -> BrowserRunner:
    research = WebResearchClient()
    browser = BrowserRunner(artifacts_dir=runtime_root / ".agent" / "browser",
                            artifacts=artifacts, registry=registry)

    def _cancel_flag():
        """Per-task cancel Event the orchestrator sets when the user
        stops a running command — shared with the browser runner."""
        try:
            tls = registry.context.get("task_tls")
            tid = getattr(tls, "task_id", "") or \
                registry.context.get("task_id") or ""
            flags = registry.context.get("command_cancel") or {}
            return flags.get(tid)
        except Exception:
            return None

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
        "Use a real local browser for websites that require JavaScript or interaction. Actions: click, type, press, wait, wait_for, goto, select, hover, evaluate, upload, scroll. Returns page text, console output, console errors, and failed network requests. Supports persistent sessions (cookies/login) via 'session'.",
        {"type": "object", "properties": {
            "url": {"type": "string"},
            "actions": {"type": "array", "items": {"type": "object"}},
            "screenshot": {"type": "boolean"},
            "screenshot_name": {"type": "string"},
            "session": {"type": "string", "description": "named persistent session (saves cookies/storage state)"},
            "save_session": {"type": "boolean", "default": False},
            "headless": {"type": "boolean", "default": True},
            "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600},
        }, "required": ["url"]},
        "browser.control",
        lambda args: json.dumps(browser.run(
            url=str(args["url"]),
            actions=list(args.get("actions") or []),
            screenshot=bool(args.get("screenshot", False)),
            screenshot_name=str(args.get("screenshot_name") or "browser-latest"),
            session=str(args.get("session", "") or ""),
            save_session=bool(args.get("save_session", False)),
            headless=bool(args.get("headless", True)),
            timeout_s=float(args.get("timeout_s") or 120),
            cancel=_cancel_flag(),
        ), ensure_ascii=False),
    )
    spec.health_check = browser.health
    registry.register(spec)
    registry.register(ToolSpec(
        "browser_verify",
        "End-to-end verify a web page/app in a real browser: navigate, run optional actions, then assert expected text/title/selectors and that the console is error-free. Returns a structured pass/fail verdict with a screenshot artifact. Use to verify web UI work actually renders and functions.",
        {"type": "object", "properties": {
            "url": {"type": "string"},
            "expect_text": {"type": "array", "items": {"type": "string"}},
            "expect_selector": {"type": "array", "items": {"type": "string"}},
            "expect_title_contains": {"type": "string"},
            "forbid_console_errors": {"type": "boolean", "default": True},
            "actions": {"type": "array", "items": {"type": "object"}},
            "screenshot_name": {"type": "string"},
            "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600},
        }, "required": ["url"]},
        "browser.control",
        lambda args: json.dumps(browser.verify(
            url=str(args["url"]),
            expect_text=[str(x) for x in (args.get("expect_text") or [])][:20],
            expect_selector=[str(x) for x in (args.get("expect_selector") or [])][:20],
            expect_title_contains=str(args.get("expect_title_contains") or ""),
            forbid_console_errors=bool(args.get("forbid_console_errors", True)),
            actions=list(args.get("actions") or []),
            screenshot_name=str(args.get("screenshot_name") or "browser-verify"),
            timeout_s=float(args.get("timeout_s") or 120),
            cancel=_cancel_flag(),
        ), ensure_ascii=False),
    ))
    return browser
