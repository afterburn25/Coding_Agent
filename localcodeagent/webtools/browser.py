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

    def health(self) -> dict:
        if self.available():
            return {"ok": True, "status": "healthy", "detail": "playwright installed; chromium resolved at launch"}
        return {"ok": False, "status": "unhealthy", "detail": "playwright not installed — pip install playwright && playwright install chromium"}

    def session_path(self, session: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(session))[:60]
        if not safe:
            raise ValueError("session name is required")
        return self.artifacts_dir / f"session-{safe}.json"

    ACTIONS = {"click", "type", "press", "wait", "wait_for", "goto", "select", "hover", "evaluate", "upload", "scroll"}

    def run(self, *, url: str, actions: list[dict[str, Any]] | None = None, screenshot: bool = False,
            session: str = "", save_session: bool = False) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("Browser automation requires the optional Playwright dependency and Chromium installation.") from exc

        actions = actions or []
        events: list[dict[str, Any]] = []
        console: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context_kwargs: dict[str, Any] = {}
            session_file = self.session_path(session) if session else None
            if session_file is not None and session_file.is_file():
                context_kwargs["storage_state"] = str(session_file)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.on("console", lambda msg: console.append({"type": msg.type, "text": str(msg.text)[:500]}))
            page.on("requestfailed", lambda req: failures.append({"url": req.url[:300], "failure": str(req.failure)}))
            page.on("pageerror", lambda err: console.append({"type": "error", "text": str(err)[:500]}))
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            for action in actions:
                kind = str(action.get("action", "")).lower()
                selector = str(action.get("selector", ""))
                if kind not in self.ACTIONS:
                    raise ValueError(f"Unsupported browser action: {kind}")
                if kind == "click":
                    page.locator(selector).click(timeout=10000)
                elif kind == "type":
                    page.locator(selector).fill(str(action.get("text", "")), timeout=10000)
                elif kind == "press":
                    page.locator(selector).press(str(action.get("key", "Enter")), timeout=10000)
                elif kind == "wait":
                    page.wait_for_timeout(int(action.get("ms", 1000)))
                elif kind == "wait_for":
                    page.locator(selector).wait_for(timeout=int(action.get("ms", 10000)))
                elif kind == "goto":
                    page.goto(str(action.get("url", url)), wait_until="domcontentloaded", timeout=30000)
                elif kind == "select":
                    page.locator(selector).select_option(str(action.get("value", "")), timeout=10000)
                elif kind == "hover":
                    page.locator(selector).hover(timeout=10000)
                elif kind == "evaluate":
                    events.append({"action": "evaluate", "result": str(page.evaluate(str(action.get("script", "true"))))[:2000]})
                elif kind == "upload":
                    page.locator(selector).set_input_files(str(action.get("path", "")), timeout=10000)
                elif kind == "scroll":
                    page.mouse.wheel(0, int(action.get("y", 800)))
                events.append({"action": kind, "url": page.url})
            shot = ""
            if screenshot:
                name = str(action.get("name", "browser-latest")) if actions else "browser-latest"
                target = self.artifacts_dir / f"{name}.png"
                page.screenshot(path=str(target), full_page=True)
                shot = str(target)
            if save_session and session_file is not None:
                context.storage_state(path=str(session_file))
            result = {
                "url": page.url,
                "title": page.title(),
                "text": page.locator("body").inner_text(timeout=10000)[:30000],
                "events": events,
                "console": console[-50:],
                "console_errors": [c for c in console if c["type"] in {"error", "assert"}][-20:],
                "failed_requests": failures[-20:],
                "screenshot": shot,
                "session": session or "",
            }
            browser.close()
            return result
