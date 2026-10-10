from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


def _edge_exe() -> Path | None:
    """Installed Microsoft Edge — present on every supported Windows host
    and on GitHub windows runners; usable as a Playwright channel with no
    browser download at all."""
    if not sys.platform.startswith("win"):
        return None
    for env_key in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData"):
        root = os.environ.get(env_key)
        if not root:
            continue
        p = Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        if p.is_file():
            return p
    return None


def _driver_paths() -> tuple[Path, Path] | None:
    """Locate the bundled Playwright node driver — dev layout and
    PyInstaller ``_internal`` layout both resolve through the package dir."""
    try:
        import playwright  # noqa: F401
        from playwright._impl._driver import compute_driver_executable
        cli_node, cli_js = compute_driver_executable()
        return Path(cli_node), Path(cli_js)
    except Exception:
        return None


def _chromium_exe(browsers_dir: Path) -> Path | None:
    """Managed Chromium previously provisioned into ``browsers_dir``."""
    try:
        for headless in browsers_dir.glob("chromium_headless_shell-*/chrome-win/headless_shell.exe"):
            if headless.is_file():
                return headless
        for exe in browsers_dir.glob("chromium-*/chrome-win/chrome.exe"):
            if exe.is_file():
                return exe
    except OSError:
        pass
    return None


class BrowserRunner:
    """Playwright-backed browser automation + lightweight E2E verify.

    Production deployment model for the frozen Windows build:
    - Playwright ships inside the backend bundle (--collect-all).
    - The *browser binary* is resolved at run time, preferring the system
      Microsoft Edge channel (zero download); when Edge is absent the
      runner provisions a managed Chromium into ``browsers_dir`` via the
      bundled driver. Nothing depends on the development machine.
    """

    ACTIONS = {"click", "type", "press", "wait", "wait_for", "goto",
               "select", "hover", "evaluate", "upload", "scroll"}

    INSTALL_TIMEOUT_S = 900

    def __init__(self, *, artifacts_dir: Path,
                 browsers_dir: Path | None = None,
                 artifacts: Any = None,
                 registry: Any = None) -> None:
        self.artifacts_dir = artifacts_dir.resolve()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.browsers_dir = (browsers_dir or
                             artifacts_dir.parent / "browser-browsers").resolve()
        self.artifacts = artifacts
        self._registry = registry  # ToolRegistry — live task context
        self._install_lock = threading.Lock()

    # -- availability ---------------------------------------------------

    @staticmethod
    def playwright_available() -> bool:
        try:
            import playwright.sync_api  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def available() -> bool:
        """Legacy probe — playwright importable. Use :meth:`status` for the
        honest answer (importable AND a resolvable browser)."""
        return BrowserRunner.playwright_available()

    def channel(self) -> str | None:
        """Resolvable browser channel right now, or None."""
        if _edge_exe() is not None:
            return "msedge"
        if _chromium_exe(self.browsers_dir) is not None:
            return "chromium"
        return None

    def status(self) -> dict[str, Any]:
        pw = self.playwright_available()
        ch = self.channel() if pw else None
        return {
            "playwright": pw,
            "channel": ch or "",
            "browsers_dir": str(self.browsers_dir),
            "ready": bool(pw and ch),
            "detail": (
                "ready via Edge channel" if ch == "msedge"
                else "ready via managed Chromium" if ch == "chromium"
                else "playwright installed; no browser — Edge missing, "
                     "Chromium not provisioned" if pw
                else "playwright package not installed"),
        }

    def health(self) -> dict:
        st = self.status()
        if st["ready"]:
            return {"ok": True, "status": "healthy", "detail": st["detail"]}
        if st["playwright"]:
            return {"ok": False, "status": "degraded",
                    "detail": st["detail"] + " — it is provisioned on first use"}
        return {"ok": False, "status": "unhealthy", "detail": st["detail"]}

    # -- browser provisioning -------------------------------------------

    def ensure_browser(self, progress=None) -> str:
        """Return a resolvable channel, provisioning managed Chromium when
        no system browser exists. Raises RuntimeError with the honest
        reason when it cannot."""
        ch = self.channel()
        if ch:
            return ch
        if not self.playwright_available():
            raise RuntimeError(
                "Browser automation is not available in this build — the "
                "Playwright package is missing.")
        drv = _driver_paths()
        if drv is None:
            raise RuntimeError("Playwright driver not found in this build.")
        node, cli = drv
        self.browsers_dir.mkdir(parents=True, exist_ok=True)
        with self._install_lock:
            # Another caller may have finished while we waited.
            ch = self.channel()
            if ch:
                return ch
            env = dict(os.environ)
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browsers_dir)
            from ..procutil import no_window_flags
            proc = subprocess.run(
                [str(node), str(cli), "install", "chromium"],
                env=env, capture_output=True, text=True, timeout=self.INSTALL_TIMEOUT_S,
                creationflags=no_window_flags(), encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip()[-300:]
                raise RuntimeError(f"browser provisioning failed: {tail}")
            ch = self.channel()
            if not ch:
                raise RuntimeError(
                    "browser provisioning finished but no browser was found on disk")
            if progress:
                progress("managed Chromium installed")
            return ch

    # -- internals --------------------------------------------------------

    def session_path(self, session: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(session))[:60]
        if not safe:
            raise ValueError("session name is required")
        return self.artifacts_dir / f"session-{safe}.json"

    def _register_artifact(self, path: Path, *, kind: str,
                           metadata: dict[str, Any]) -> None:
        if self.artifacts is None:
            return
        task_id = ""
        try:
            if self._registry is not None:
                tls = self._registry.context.get("task_tls")
                task_id = str(getattr(tls, "task_id", "") or
                              self._registry.context.get("task_id") or "")
        except Exception:
            pass
        try:
            self.artifacts.register(path, kind=kind, tool="browser",
                                    task_id=task_id, creator="browser",
                                    metadata=metadata)
        except Exception:
            pass

    def _deadline(self, timeout_s: float) -> float:
        return time.monotonic() + max(5.0, float(timeout_s))

    @staticmethod
    def _left(deadline: float) -> float:
        return max(1000.0, (deadline - time.monotonic()) * 1000.0)

    @staticmethod
    def _cancelled(cancel) -> bool:
        try:
            return bool(cancel is not None and cancel.is_set())
        except Exception:
            return False

    def _launch(self, p, headless: bool):
        ch = self.channel()
        if ch == "msedge":
            return p.chromium.launch(channel="msedge", headless=headless)
        exe = _chromium_exe(self.browsers_dir)
        if exe is not None:
            return p.chromium.launch(headless=headless,
                                     executable_path=str(exe))
        # Last resort — playwright's own resolution (dev machines with a
        # user-level `playwright install`).
        return p.chromium.launch(headless=headless)

    # -- run / verify -----------------------------------------------------

    def run(self, *, url: str, actions: list[dict[str, Any]] | None = None,
            screenshot: bool = False, screenshot_name: str = "browser-latest",
            session: str = "", save_session: bool = False,
            headless: bool = True, timeout_s: float = 120.0,
            cancel=None) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError(
                "Browser automation requires Playwright — not present in this build.") from exc

        actions = actions or []
        deadline = self._deadline(timeout_s)
        events: list[dict[str, Any]] = []
        console: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        env_channel = self.ensure_browser()  # may provision managed chromium
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browsers_dir)
        started = time.time()
        with sync_playwright() as p:
            browser = self._launch(p, headless)
            context_kwargs: dict[str, Any] = {}
            session_file = self.session_path(session) if session else None
            if session_file is not None and session_file.is_file():
                context_kwargs["storage_state"] = str(session_file)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()
            page.on("console", lambda msg: console.append(
                {"type": msg.type, "text": str(msg.text)[:500]}))
            page.on("requestfailed", lambda req: failures.append(
                {"url": req.url[:300], "failure": str(req.failure)}))
            page.on("pageerror", lambda err: console.append(
                {"type": "error", "text": str(err)[:500]}))
            page.goto(url, wait_until="domcontentloaded",
                      timeout=self._left(deadline))
            for action in actions:
                if self._cancelled(cancel):
                    browser.close()
                    raise RuntimeError("browser run cancelled")
                if time.monotonic() > deadline:
                    browser.close()
                    raise RuntimeError(
                        f"browser run timed out after {timeout_s:.0f}s")
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
                    page.goto(str(action.get("url", url)),
                              wait_until="domcontentloaded",
                              timeout=self._left(deadline))
                elif kind == "select":
                    page.locator(selector).select_option(str(action.get("value", "")), timeout=10000)
                elif kind == "hover":
                    page.locator(selector).hover(timeout=10000)
                elif kind == "evaluate":
                    events.append({"action": "evaluate",
                                   "result": str(page.evaluate(str(action.get("script", "true"))))[:2000]})
                elif kind == "upload":
                    page.locator(selector).set_input_files(str(action.get("path", "")), timeout=10000)
                elif kind == "scroll":
                    page.mouse.wheel(0, int(action.get("y", 800)))
                events.append({"action": kind, "url": page.url})
            shot = ""
            if screenshot:
                safe = "".join(c if c.isalnum() or c in "-_." else "_"
                               for c in screenshot_name)[:80] or "browser-latest"
                target = self.artifacts_dir / f"{safe}.png"
                page.screenshot(path=str(target), full_page=True)
                shot = str(target)
                self._register_artifact(
                    target, kind="screenshot",
                    metadata={"url": page.url, "tool": "browser_run"})
            if save_session and session_file is not None:
                context.storage_state(path=str(session_file))
            result = {
                "url": page.url,
                "title": page.title(),
                "text": page.locator("body").inner_text(timeout=10000)[:30000],
                "events": events,
                "console": console[-50:],
                "console_errors": [c for c in console
                                   if c["type"] in {"error", "assert"}][-20:],
                "failed_requests": failures[-20:],
                "screenshot": shot,
                "session": session or "",
                "channel": env_channel,
                "elapsed_s": round(time.time() - started, 2),
            }
            browser.close()
            return result

    def verify(self, *, url: str,
               expect_text: list[str] | None = None,
               expect_selector: list[str] | None = None,
               expect_title_contains: str = "",
               forbid_console_errors: bool = True,
               actions: list[dict[str, Any]] | None = None,
               screenshot_name: str = "",
               headless: bool = True,
               timeout_s: float = 120.0,
               cancel=None) -> dict[str, Any]:
        """E2E verification pass for the coding pipeline: navigate, act,
        then check expectations. Returns a structured verdict — never a
        bare exception — so verifiers can report honest pass/fail."""
        checks: list[dict[str, Any]] = []
        try:
            res = self.run(url=url, actions=actions,
                           screenshot=bool(screenshot_name),
                           screenshot_name=screenshot_name or "browser-verify",
                           headless=headless, timeout_s=timeout_s,
                           cancel=cancel)
        except Exception as exc:
            return {
                "pass": False,
                "verdict": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "checks": [{"name": "run", "ok": False,
                            "detail": str(exc)[:400]}],
                "screenshot": "",
            }
        text = res.get("text") or ""
        for expected in (expect_text or []):
            needle = str(expected)
            checks.append({"name": f"text:{needle[:60]}", "ok": needle in text,
                           "detail": "found" if needle in text else "absent"})
        if expect_title_contains:
            ok = expect_title_contains.lower() in (res.get("title") or "").lower()
            checks.append({"name": f"title:{expect_title_contains[:60]}",
                           "ok": ok, "detail": res.get("title") or ""})
        # Selector expectations need a fresh cheap probe — evaluate via a
        # second run only when asked (keeps the common case single-pass).
        if expect_selector:
            try:
                res2 = self.run(url=url, actions=[
                    {"action": "wait_for", "selector": sel,
                     "ms": int(min(10000, timeout_s * 500))}
                    for sel in expect_selector],
                    headless=headless, timeout_s=timeout_s, cancel=cancel)
                for sel in expect_selector:
                    ev = [e for e in res2.get("events", [])
                          if e.get("action") == "wait_for"]
                    checks.append({"name": f"selector:{sel[:60]}",
                                   "ok": len(ev) > 0, "detail": "present"})
            except Exception as exc:
                for sel in expect_selector:
                    checks.append({"name": f"selector:{sel[:60]}",
                                   "ok": False, "detail": str(exc)[:200]})
        if forbid_console_errors:
            errs = res.get("console_errors") or []
            checks.append({"name": "console_errors", "ok": not errs,
                           "detail": f"{len(errs)} console errors",
                           "errors": errs[:10]})
        failed_reqs = res.get("failed_requests") or []
        checks.append({"name": "network", "ok": True,
                       "detail": f"{len(failed_reqs)} failed requests",
                       "failed": failed_reqs[:10]})
        passed = all(c["ok"] for c in checks)
        return {
            "pass": passed,
            "verdict": "pass" if passed else "fail",
            "url": res.get("url"), "title": res.get("title"),
            "channel": res.get("channel"),
            "elapsed_s": res.get("elapsed_s"),
            "checks": checks,
            "screenshot": res.get("screenshot") or "",
        }
