from __future__ import annotations

from pathlib import Path
from typing import Any


class BrowserRunner:
    """Optional Playwright-backed browser automation.

    Playwright is intentionally optional so the core agent remains dependency-light.
    Install with: pip install 'local-code-agent[browser]' && playwright install chromium
    """

    def __init__(self, *, artifacts_dir: Path) -> None:
        self.artifacts_dir = artifacts_dir.resolve()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def available() -> bool:
        try:
            import playwright.sync_api  # noqa: F401
            return True
        except Exception:
            return False

    def run(self, *, url: str, actions: list[dict[str, Any]] | None = None, screenshot: bool = False) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("Browser automation requires the optional Playwright dependency and Chromium installation.") from exc

        actions = actions or []
        events: list[dict[str, Any]] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            for action in actions:
                kind = str(action.get("action", "")).lower()
                selector = str(action.get("selector", ""))
                if kind == "click":
                    page.locator(selector).click(timeout=10000)
                elif kind == "type":
                    page.locator(selector).fill(str(action.get("text", "")), timeout=10000)
                elif kind == "press":
                    page.locator(selector).press(str(action.get("key", "Enter")), timeout=10000)
                elif kind == "wait":
                    page.wait_for_timeout(int(action.get("ms", 1000)))
                elif kind == "goto":
                    page.goto(str(action.get("url", url)), wait_until="domcontentloaded", timeout=30000)
                else:
                    raise ValueError(f"Unsupported browser action: {kind}")
                events.append({"action": kind, "url": page.url})
            shot = ""
            if screenshot:
                target = self.artifacts_dir / "browser-latest.png"
                page.screenshot(path=str(target), full_page=True)
                shot = str(target)
            result = {
                "url": page.url,
                "title": page.title(),
                "text": page.locator("body").inner_text(timeout=10000)[:30000],
                "events": events,
                "screenshot": shot,
            }
            browser.close()
            return result
